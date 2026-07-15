# ═══════════════════════════════════════════════════════════════
#  Feed-Status Blueprint — Family → Crew „verschwindende" 24h-Nachricht
#
#  Eine Family-Person schreibt der gepairten Crew-Person eine kurze Nachricht
#  (+ optionales Emoji), die genau 24h im FEED der Crew erscheint und danach
#  serverseitig verfällt. KEIN Crew-Selbst-Post (User: „ich wollte nur von
#  family zu crew ins feed posten").
#
#  Architektur:
#      SB-Primary  → Tabelle feed_statuses (family_token PK → eine aktive
#                    Nachricht pro Family-Person; crew_token = Empfänger)
#      Disk-Fallback → _USER_HISTORY_DIR/feed_status_<family_token>.json
#      Crew-Auflösung über blueprints.family_watch._scoped_token_crew.
#
#  Endpunkte:
#      POST   /api/feed-status/family/<family_token>   body {text, emoji?}
#               → Nachricht an die gepairte Crew (resolved via scoped token)
#      DELETE /api/feed-status/family/<family_token>   → eigene Nachricht löschen
#      GET    /api/feed-status/<crew_token>/incoming    → aktive Nachrichten an mich
#      POST   /api/feed-status/<crew_token>/reply       → 24h-Textantwort an Family
# ═══════════════════════════════════════════════════════════════

import os
import re
import json
import logging
import datetime as _dt
from flask import Blueprint, request, jsonify, current_app

feed_status_bp = Blueprint('feed_status', __name__)

_TTL_SECONDS = 24 * 60 * 60          # 24 Stunden
_MAX_TEXT_LEN = 180
_MAX_EMOJI_LEN = 8
_MAX_REPLY_LEN = 280
_MIN_REPLY_INTERVAL_SECONDS = 3
_IDEMPOTENCY_RE = re.compile(r'^[A-Za-z0-9_-]{8,80}$')


def _app_attr(name, default=None):
    try:
        import app as _app_mod
        return getattr(_app_mod, name, default)
    except Exception:
        return default


def _fw():
    """blueprints.family_watch Modul (für _scoped_token_crew + _load_crew_profile)."""
    try:
        import blueprints.family_watch as fw
        return fw
    except Exception:
        return None


def _log():
    try:
        return current_app.logger
    except RuntimeError:
        return logging.getLogger('feed_status')


def _get_sb():
    return _app_attr('SB_AVAILABLE', False), _app_attr('sb', None)


def _history_dir():
    d = _app_attr('_USER_HISTORY_DIR', '_user_history_state')
    os.makedirs(d, exist_ok=True)
    return d


def _safe_token(token):
    if not token or not isinstance(token, str):
        return None
    safe = re.sub(r'[^A-Za-z0-9_-]', '', token)[:64]
    return safe or None


def _atomic_write_json(path, data):
    fn = _app_attr('_atomic_write_json')
    if callable(fn) and fn is not _atomic_write_json:
        return fn(path, data)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _disk_path(family_token):
    safe = _safe_token(family_token) or 'unknown'
    return os.path.join(_history_dir(), f'feed_status_{safe}.json')


def _is_active(rec):
    if not isinstance(rec, dict):
        return False
    exp = rec.get('expires_at')
    if not exp:
        return False
    try:
        exp_dt = _dt.datetime.fromisoformat(str(exp).replace('Z', '+00:00'))
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=_dt.timezone.utc)
        return exp_dt > _dt.datetime.now(_dt.timezone.utc)
    except Exception:
        return False


def _parse_iso(value):
    if not value:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        return parsed.astimezone(_dt.timezone.utc)
    except Exception:
        return None


def _utc_now():
    return _dt.datetime.now(_dt.timezone.utc)


def _reply_is_active(rec):
    exp = _parse_iso((rec or {}).get('reply_expires_at'))
    return bool(exp and exp > _utc_now() and (rec or {}).get('reply_body'))


def _family_push_recipient(family_token):
    """Return the exact account token that may receive a Family push.

    Search/grant pairings use the account token directly. Pair-code pairings
    use an AT-FAM capability; new capabilities retain their authenticated
    owner in the scoped-token JSON. Old unbound capabilities intentionally
    return None instead of guessing and risking a foreign-account push.
    """
    if not family_token:
        return None
    if not family_token.startswith('AT-FAM-'):
        return family_token
    fw = _fw()
    try:
        rec = (fw._scoped_tokens_load() or {}).get(family_token, {}) if fw else {}
        return (rec.get('owner_token') or '').strip() or None
    except Exception:
        return None


def _status_for_family(family_token):
    """One status row, Supabase-primary with the existing disk fallback."""
    sb_avail, sb = _get_sb()
    if sb_avail and sb is not None:
        try:
            r = (sb.table('feed_statuses').select('*')
                 .eq('family_token', family_token).limit(1).execute())
            rows = r.data or []
            if rows:
                return rows[0]
        except Exception as e:
            _log().info(f'[feed-status] sb_get_skip {type(e).__name__}')
    p = _disk_path(family_token)
    if os.path.exists(p):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            pass
    return None


# ── Persistenz ────────────────────────────────────────────────────────────────
def _status_save(family_token, rec):
    sb_avail, sb = _get_sb()
    sb_ok = False
    if sb_avail and sb is not None:
        try:
            sb.table('feed_statuses').upsert(
                {'family_token': family_token, **rec}, on_conflict='family_token').execute()
            sb_ok = True
        except Exception as e:
            _log().info(f'[feed-status] sb_save_skip {type(e).__name__}')
    disk_ok = False
    try:
        _atomic_write_json(_disk_path(family_token), {'family_token': family_token, **rec})
        disk_ok = True
    except Exception as e:
        _log().warning(f'[feed-status] disk_save_fail {e}')
    return bool(sb_ok or disk_ok)


def _status_delete(family_token):
    sb_avail, sb = _get_sb()
    if sb_avail and sb is not None:
        try:
            sb.table('feed_statuses').delete().eq('family_token', family_token).execute()
        except Exception as e:
            _log().info(f'[feed-status] sb_del_skip {type(e).__name__}')
    try:
        p = _disk_path(family_token)
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass
    return True


def _incoming_for_crew(crew_token):
    """Alle aktiven Nachrichten, die auf diesen crew_token zeigen.
    SB-primary (eq crew_token); Disk-Fallback scannt feed_status_*.json."""
    out = []
    sb_avail, sb = _get_sb()
    if sb_avail and sb is not None:
        try:
            r = sb.table('feed_statuses').select('*').eq('crew_token', crew_token).execute()
            for row in (r.data or []):
                if _is_active(row):
                    out.append(row)
            return out
        except Exception as e:
            _log().info(f'[feed-status] sb_incoming_skip {type(e).__name__}')
    # Disk-Fallback (Single-Instance): alle feed_status_*.json scannen.
    try:
        d = _history_dir()
        for fn in os.listdir(d):
            if not fn.startswith('feed_status_') or not fn.endswith('.json'):
                continue
            try:
                with open(os.path.join(d, fn)) as f:
                    rec = json.load(f)
                if rec.get('crew_token') == crew_token and _is_active(rec):
                    out.append(rec)
            except Exception:
                continue
    except Exception:
        pass
    return out


def _public_view(rec):
    return {
        # DB-Spalte heißt `body` (das Wort `text` ist ein Postgres-Typname);
        # die API gibt das Feld weiterhin als `text` an die iOS-App.
        # ROBUSTHEIT (User 2026-06-30: „Family-Nachricht kam OHNE Text an"): `or`
        # statt `get(key, default)` — fällt auch dann auf `text`/`message` zurück,
        # wenn `body` zwar als Spalte EXISTIERT, aber null/leer ist (alte Zeilen /
        # SB-Schema ohne body-Wert). So geht der Text nie verloren.
        'text': rec.get('body') or rec.get('text') or rec.get('message') or '',
        'emoji': rec.get('emoji'),
        # Crew-Reaktion (❤️ zurück) — Family sieht sie auf ihrer Compose-Karte.
        'reaction': rec.get('reaction'),
        'reacted_at': rec.get('reacted_at'),
        # Echte Crew-Textantwort. Eigene 24h-Laufzeit; sie verlängert den
        # ursprünglichen Family-Post im Crew-Feed ausdrücklich nicht.
        'reply_text': rec.get('reply_body'),
        'reply_created_at': rec.get('reply_created_at'),
        'reply_expires_at': rec.get('reply_expires_at'),
        'reply_active': _reply_is_active(rec),
        'message_active': _is_active(rec),
        'from_name': rec.get('from_name'),
        'from_avatar': rec.get('from_avatar'),
        'relation': rec.get('relation'),
        'created_at': rec.get('created_at'),
        'expires_at': rec.get('expires_at'),
    }


# ── Routes ──────────────────────────────────────────────────────────────────
@feed_status_bp.route('/api/feed-status/family/<family_token>', methods=['POST'])
def post_family_status(family_token):
    """Family postet eine 24h-Nachricht in den Feed der gepairten Crew."""
    if not _safe_token(family_token):
        return jsonify({'ok': False, 'error': 'invalid_token'}), 400
    body = request.get_json(silent=True) or {}
    fw = _fw()
    # `crew` (opaker Hash aus dem Watch-Feed) wählt bei mehreren gepairten Crew
    # GENAU eine an (#9). Ohne id → der primäre Crew. _resolve_crew_for_family
    # deckt beide Pairing-Wege ab (Pair-Code + Such-/Anfrage-Grant).
    opaque = (body.get('crew') or '').strip() or None
    crew_token = None
    if fw:
        if hasattr(fw, '_resolve_crew_for_family'):
            try:
                crew_token = fw._resolve_crew_for_family(family_token, opaque_id=opaque)
            except TypeError:
                crew_token = fw._resolve_crew_for_family(family_token)
        if not crew_token:
            crew_token = fw._scoped_token_crew(family_token)
    if not crew_token:
        return jsonify({'ok': False, 'error': 'not_paired'}), 404
    text = (body.get('text') or '').strip()
    if not text:
        return jsonify({'ok': False, 'error': 'empty_text'}), 400
    text = text[:_MAX_TEXT_LEN]
    emoji = (body.get('emoji') or '').strip()[:_MAX_EMOJI_LEN] or None
    message_id = (body.get('message_id') or '').strip()
    if message_id and not _IDEMPOTENCY_RE.fullmatch(message_id):
        return jsonify({'ok': False, 'error': 'invalid_message_id'}), 400
    # Retry derselben Client-Operation: bestehende Antwort zurückgeben und vor
    # allem KEINEN zweiten Push erzeugen.
    if message_id:
        existing = _status_for_family(family_token)
        if existing and existing.get('message_id') == message_id:
            return jsonify({'ok': True, 'status': _public_view(existing),
                            'idempotent': True})
    # Absender-Identität (Name/Foto) aus dem Family-Profil.
    fam_prof = (fw._load_crew_profile(family_token) if fw else {}) or {}
    # Relation (mama/papa/partner …) aus dem share-GRANT lesen — der Scoped-Token
    # speichert KEINE relation, daher fiel der Wert vorher auf den NAMEN zurück
    # (Bug-Hunt #17/21/25). Grant ist die Wahrheit; sonst neutral 'family'.
    relation = None
    try:
        if fw and hasattr(fw, '_shares_load'):
            for s in (fw._shares_load() or []):
                if s.get('crew_token') == crew_token and s.get('family_token') == family_token:
                    relation = s.get('relation')
                    break
        if not relation and fw:
            relation = (fw._scoped_tokens_load() or {}).get(family_token, {}).get('relation')
    except Exception:
        pass
    if not relation:
        relation = 'family'
    now = _dt.datetime.now(_dt.timezone.utc)
    rec = {
        'crew_token': crew_token,
        'message_id': message_id or None,
        'from_name': fam_prof.get('name') or 'Familie',
        'from_avatar': fam_prof.get('avatar_url'),
        'relation': relation,
        'body': text,                 # DB-Spalte `body` (nicht `text` = PG-Typname)
        'emoji': emoji,
        'created_at': now.isoformat(),
        'expires_at': (now + _dt.timedelta(seconds=_TTL_SECONDS)).isoformat(),
        # Neuer Post = neuer Thread. Reaktion/Antwort des ersetzten Posts darf
        # nicht am neuen Text weiterleben.
        'reaction': None,
        'reacted_at': None,
        'reply_body': None,
        'reply_created_at': None,
        'reply_expires_at': None,
        'reply_idempotency_key': None,
    }
    if not _status_save(family_token, rec):
        return jsonify({'ok': False, 'error': 'persist_failed'}), 500
    _notify_crew_of_family_message(crew_token, rec)
    return jsonify({'ok': True, 'status': _public_view(rec)})


@feed_status_bp.route('/api/feed-status/family/<family_token>', methods=['GET'])
def get_family_status(family_token):
    """Family sieht die eigene aktive Nachricht (zum Bearbeiten/Entfernen)."""
    if not _safe_token(family_token):
        return jsonify({'ok': False, 'error': 'invalid_token'}), 400
    rec = _status_for_family(family_token)
    # Antwort bleibt volle 24h ab Antwort sichtbar, selbst wenn der Family-Post
    # inzwischen aus dem Crew-Feed abgelaufen ist.
    if rec and (_is_active(rec) or _reply_is_active(rec)):
        return jsonify({'ok': True, 'status': _public_view(rec)})
    if rec:
        _status_delete(family_token)
    return jsonify({'ok': True, 'status': None})


@feed_status_bp.route('/api/feed-status/family/<family_token>', methods=['DELETE'])
def delete_family_status(family_token):
    if not _safe_token(family_token):
        return jsonify({'ok': False, 'error': 'invalid_token'}), 400
    _status_delete(family_token)
    return jsonify({'ok': True})


@feed_status_bp.route('/api/feed-status/family/<family_token>/bind-push',
                      methods=['POST'])
def bind_family_push_recipient(family_token):
    """Bind an existing AT-FAM capability to its signed-in Family account.

    This upgrades old pair-code connections without guessing. The caller must
    possess the capability and authenticate as a Family account. Existing
    bindings are immutable, preventing a later foreign account from taking
    over reply notifications.
    """
    if not _safe_token(family_token):
        return jsonify({'ok': False, 'error': 'invalid_token'}), 400
    if not family_token.startswith('AT-FAM-'):
        # Normal search/grant pairing already uses the account token itself;
        # the central gate binds its Bearer to this path token.
        return jsonify({'ok': True, 'bound': False, 'account_token': True})
    fw = _fw()
    owner = (fw._authenticated_family_owner_token()
             if fw and hasattr(fw, '_authenticated_family_owner_token') else None)
    if not owner:
        return jsonify({'ok': False, 'error': 'family_auth_required'}), 401
    try:
        tokens = fw._scoped_tokens_load() or {}
        rec = tokens.get(family_token)
        if not isinstance(rec, dict) or not rec.get('crew_token'):
            return jsonify({'ok': False, 'error': 'unauthorized'}), 401
        existing = (rec.get('owner_token') or '').strip()
        if existing and existing != owner:
            return jsonify({'ok': False, 'error': 'already_bound'}), 409
        if existing == owner:
            return jsonify({'ok': True, 'bound': True, 'idempotent': True})
        rec = dict(rec)
        rec['owner_token'] = owner
        rec['owner_bound_at'] = _utc_now().isoformat()
        tokens[family_token] = rec
        if not fw._scoped_tokens_save(tokens):
            return jsonify({'ok': False, 'error': 'persist_failed'}), 503
        return jsonify({'ok': True, 'bound': True})
    except Exception as e:
        _log().info(f'[feed-status] bind_push_skip {type(e).__name__}')
        return jsonify({'ok': False, 'error': 'persist_failed'}), 503


@feed_status_bp.route('/api/feed-status/<crew_token>/incoming', methods=['GET'])
def incoming_statuses(crew_token):
    """Crew holt die aktiven Family-Nachrichten an sich (für den Feed)."""
    if not _safe_token(crew_token):
        return jsonify({'ok': False, 'error': 'invalid_token', 'statuses': []}), 400
    rows = _incoming_for_crew(crew_token)
    out = [_public_view(r) for r in rows]
    out.sort(key=lambda v: v.get('created_at') or '', reverse=True)
    return jsonify({'ok': True, 'statuses': out, 'count': len(out)})


def _notify_crew_of_family_message(crew_token, rec):
    """One durable/idempotent push to the exact Crew recipient."""
    if not crew_token:
        return
    try:
        from app import _push_notify_async
        sender = rec.get('from_name') or 'Familie'
        text = (rec.get('body') or '').strip()
        message_id = rec.get('message_id') or rec.get('created_at') or ''
        _push_notify_async(
            crew_token,
            f'Nachricht von {sender}',
            text[:140],
            data={'type': 'family_message', 'created_at': rec.get('created_at')},
            idempotency_key=f'family-message:{crew_token}:{message_id}')
    except Exception as e:
        _log().info(f'[feed-status] message_push_skip {type(e).__name__}')


def _notify_family_of_reaction(family_token, emoji, created_at=None):
    """Best-effort Push an die Familie, dass die Crew reagiert hat. No-op wenn
    kein Family-Push registriert ist (die Reaktion steckt ohnehin in der Family-
    Status-Response → beim nächsten Öffnen sichtbar)."""
    if not family_token:
        return
    try:
        # ASYNC (Audit 2026-07-12): der synchrone Send konnte den React-Request
        # bis in den APNs-Timeout blockieren — Checklisten-Regel ist push_notify_ASYNC.
        from app import _push_notify_async
        recipient = _family_push_recipient(family_token)
        if not recipient:
            return
        _push_notify_async(
            recipient,
            'Antwort von der Crew',
            f'{emoji} auf deine Nachricht',
            data={'type': 'family_reaction', 'emoji': emoji},
            idempotency_key=(
                f'family-reaction:{family_token}:{created_at or "current"}:{emoji}'))
    except Exception as e:
        _log().info(f'[feed-status] react_push_skip {type(e).__name__}')


def _reply_result_from_rpc(crew_token, created_at, text, idempotency_key):
    """Atomic production path supplied by the companion Supabase migration.

    None means the RPC is unavailable and the disk/legacy fallback should run.
    The RPC row-locks the parent status, so two concurrent retries cannot send
    two replies or two pushes.
    """
    sb_avail, sb = _get_sb()
    if not (sb_avail and sb is not None):
        return None
    try:
        r = sb.rpc('set_family_status_reply', {
            'p_crew_token': crew_token,
            'p_created_at': created_at,
            'p_reply_body': text,
            'p_idempotency_key': idempotency_key,
        }).execute()
        data = r.data
        if isinstance(data, list):
            data = data[0] if data else None
        return data if isinstance(data, dict) else None
    except Exception as e:
        _log().info(f'[feed-status] reply_rpc_skip {type(e).__name__}')
        return None


def _reply_patch(now, text, idempotency_key):
    return {
        'reply_body': text,
        'reply_created_at': now.isoformat(),
        'reply_expires_at': (now + _dt.timedelta(seconds=_TTL_SECONDS)).isoformat(),
        'reply_idempotency_key': idempotency_key,
    }


def _legacy_set_reply(crew_token, created_at, text, idempotency_key):
    """Compatibility path until the atomic RPC migration is installed."""
    sb_avail, sb = _get_sb()
    if sb_avail and sb is not None:
        try:
            q = (sb.table('feed_statuses').select('*')
                 .eq('crew_token', crew_token).eq('created_at', created_at)
                 .limit(1).execute())
            rows = q.data or []
            if not rows:
                return {'outcome': 'not_found'}
            rec = rows[0]
            if not _is_active(rec):
                return {'outcome': 'expired'}
            if rec.get('reply_idempotency_key') == idempotency_key:
                return {'outcome': 'idempotent', **rec}
            last = _parse_iso(rec.get('reply_created_at'))
            now = _utc_now()
            if last and (now - last).total_seconds() < _MIN_REPLY_INTERVAL_SECONDS:
                return {'outcome': 'rate_limited'}
            patch = _reply_patch(now, text, idempotency_key)
            (sb.table('feed_statuses').update(patch)
             .eq('family_token', rec.get('family_token'))
             .eq('crew_token', crew_token).eq('created_at', created_at).execute())
            merged = {**rec, **patch}
            try:
                _atomic_write_json(_disk_path(rec.get('family_token')), merged)
            except Exception:
                pass
            return {'outcome': 'saved', **merged}
        except Exception as e:
            _log().info(f'[feed-status] reply_sb_fallback {type(e).__name__}')

    # Local/dev single-instance fallback.
    try:
        for fn in os.listdir(_history_dir()):
            if not fn.startswith('feed_status_') or not fn.endswith('.json'):
                continue
            p = os.path.join(_history_dir(), fn)
            try:
                with open(p) as f:
                    rec = json.load(f)
            except Exception:
                continue
            if rec.get('crew_token') != crew_token or rec.get('created_at') != created_at:
                continue
            if not _is_active(rec):
                return {'outcome': 'expired'}
            if rec.get('reply_idempotency_key') == idempotency_key:
                return {'outcome': 'idempotent', **rec}
            last = _parse_iso(rec.get('reply_created_at'))
            now = _utc_now()
            if last and (now - last).total_seconds() < _MIN_REPLY_INTERVAL_SECONDS:
                return {'outcome': 'rate_limited'}
            rec.update(_reply_patch(now, text, idempotency_key))
            _atomic_write_json(p, rec)
            return {'outcome': 'saved', **rec}
    except Exception:
        pass
    return {'outcome': 'not_found'}


def _notify_family_of_reply(family_token, crew_token, rec):
    recipient = _family_push_recipient(family_token)
    if not recipient:
        return
    try:
        from app import _push_notify_async
        fw = _fw()
        prof = (fw._load_crew_profile(crew_token) if fw else {}) or {}
        crew_name = prof.get('name') or 'deiner Crew'
        reply_text = (rec.get('reply_body') or '').strip()
        key = rec.get('reply_idempotency_key') or rec.get('reply_created_at') or ''
        _push_notify_async(
            recipient,
            f'Antwort von {crew_name}',
            reply_text[:140],
            data={'type': 'family_reply',
                  'reply_created_at': rec.get('reply_created_at'),
                  'reply_expires_at': rec.get('reply_expires_at')},
            idempotency_key=f'family-reply:{family_token}:{key}')
    except Exception as e:
        _log().info(f'[feed-status] reply_push_skip {type(e).__name__}')


@feed_status_bp.route('/api/feed-status/<crew_token>/reply', methods=['POST'])
def reply_to_status(crew_token):
    """Crew sends a real text reply, visible to Family for 24h.

    The central token gate binds the path token to the Crew bearer. The lookup
    additionally filters by crew_token + created_at, so a Crew account cannot
    answer or discover another account's Family message.
    """
    if not _safe_token(crew_token):
        return jsonify({'ok': False, 'error': 'invalid_token'}), 400
    body = request.get_json(silent=True) or {}
    text = (body.get('text') or '').strip()
    if not text:
        return jsonify({'ok': False, 'error': 'empty_text'}), 400
    if len(text) > _MAX_REPLY_LEN:
        return jsonify({'ok': False, 'error': 'text_too_long',
                        'max_length': _MAX_REPLY_LEN}), 400
    created_at = (body.get('created_at') or '').strip()
    if not _parse_iso(created_at):
        return jsonify({'ok': False, 'error': 'invalid_created_at'}), 400
    idempotency_key = (body.get('idempotency_key') or '').strip()
    if not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
        return jsonify({'ok': False, 'error': 'invalid_idempotency_key'}), 400

    result = _reply_result_from_rpc(
        crew_token, created_at, text, idempotency_key)
    if result is None:
        result = _legacy_set_reply(
            crew_token, created_at, text, idempotency_key)
    outcome = result.get('outcome')
    if outcome == 'not_found':
        return jsonify({'ok': False, 'error': 'not_found'}), 404
    if outcome == 'expired':
        return jsonify({'ok': False, 'error': 'message_expired'}), 410
    if outcome == 'rate_limited':
        return jsonify({'ok': False, 'error': 'rate_limited'}), 429
    if outcome not in ('saved', 'idempotent'):
        return jsonify({'ok': False, 'error': 'persist_failed'}), 503

    if outcome == 'saved':
        _notify_family_of_reply(result.get('family_token'), crew_token, result)
    return jsonify({
        'ok': True,
        'idempotent': outcome == 'idempotent',
        'reply_text': result.get('reply_body'),
        'reply_created_at': result.get('reply_created_at'),
        'reply_expires_at': result.get('reply_expires_at'),
    })


@feed_status_bp.route('/api/feed-status/<crew_token>/react', methods=['POST'])
def react_to_status(crew_token):
    """Crew schickt eine Reaktion (z. B. ❤️) auf eine Family-Nachricht zurück.
    Identifiziert die Nachricht über created_at (eindeutig genug, KEIN family_token-
    Leak an die Crew). Die Family sieht die Reaktion auf ihrer Compose-Karte."""
    if not _safe_token(crew_token):
        return jsonify({'ok': False, 'error': 'invalid_token'}), 400
    body = request.get_json(silent=True) or {}
    emoji = (body.get('emoji') or '❤️').strip()[:8] or '❤️'
    created_at = (body.get('created_at') or '').strip()
    now = _utc_now().isoformat()
    sb_avail, sb = _get_sb()
    if sb_avail and sb is not None:
        try:
            q = sb.table('feed_statuses').update(
                {'reaction': emoji, 'reacted_at': now}).eq('crew_token', crew_token)
            if created_at:
                q = q.eq('created_at', created_at)
            q.execute()
            # Family-Token server-seitig ermitteln (NICHT an die Crew geleakt) → Push.
            try:
                sel = sb.table('feed_statuses').select('family_token').eq('crew_token', crew_token)
                if created_at:
                    sel = sel.eq('created_at', created_at)
                _rows = (sel.limit(1).execute()).data or []
                _notify_family_of_reaction(
                    _rows[0].get('family_token') if _rows else None,
                    emoji, created_at)
            except Exception:
                pass
            return jsonify({'ok': True, 'reaction': emoji})
        except Exception as e:
            _log().info(f'[feed-status] react_skip {type(e).__name__}')
    # Disk-Fallback: passende(n) Note(s) finden + Reaktion setzen.
    try:
        d = _history_dir()
        for fn in os.listdir(d):
            if not fn.startswith('feed_status_') or not fn.endswith('.json'):
                continue
            p = os.path.join(d, fn)
            try:
                with open(p) as f:
                    rec = json.load(f)
            except Exception:
                continue
            if rec.get('crew_token') != crew_token:
                continue
            if created_at and rec.get('created_at') != created_at:
                continue
            rec['reaction'] = emoji
            rec['reacted_at'] = now
            _atomic_write_json(p, rec)
            _notify_family_of_reaction(
                rec.get('family_token'), emoji, rec.get('created_at'))
    except Exception:
        pass
    return jsonify({'ok': True, 'reaction': emoji})
