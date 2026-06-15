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
        'text': rec.get('text'),
        'emoji': rec.get('emoji'),
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
    fw = _fw()
    crew_token = fw._scoped_token_crew(family_token) if fw else None
    if not crew_token:
        return jsonify({'ok': False, 'error': 'not_paired'}), 404
    body = request.get_json(silent=True) or {}
    text = (body.get('text') or '').strip()
    if not text:
        return jsonify({'ok': False, 'error': 'empty_text'}), 400
    text = text[:_MAX_TEXT_LEN]
    emoji = (body.get('emoji') or '').strip()[:_MAX_EMOJI_LEN] or None
    # Absender-Identität (Name/Foto) aus dem Family-Profil.
    fam_prof = (fw._load_crew_profile(family_token) if fw else {}) or {}
    # Relation aus dem scoped-Token-Record (mama/papa/partner …) best-effort.
    relation = None
    try:
        toks = fw._scoped_tokens_load() if fw else {}
        rec = toks.get(family_token) or {}
        relation = rec.get('relation') or rec.get('family_name')
    except Exception:
        pass
    now = _dt.datetime.now(_dt.timezone.utc)
    rec = {
        'crew_token': crew_token,
        'from_name': fam_prof.get('name') or 'Familie',
        'from_avatar': fam_prof.get('avatar_url'),
        'relation': relation,
        'text': text,
        'emoji': emoji,
        'created_at': now.isoformat(),
        'expires_at': (now + _dt.timedelta(seconds=_TTL_SECONDS)).isoformat(),
    }
    if not _status_save(family_token, rec):
        return jsonify({'ok': False, 'error': 'persist_failed'}), 500
    return jsonify({'ok': True, 'status': _public_view(rec)})


@feed_status_bp.route('/api/feed-status/family/<family_token>', methods=['GET'])
def get_family_status(family_token):
    """Family sieht die eigene aktive Nachricht (zum Bearbeiten/Entfernen)."""
    if not _safe_token(family_token):
        return jsonify({'ok': False, 'error': 'invalid_token'}), 400
    p = _disk_path(family_token)
    rec = None
    sb_avail, sb = _get_sb()
    if sb_avail and sb is not None:
        try:
            r = sb.table('feed_statuses').select('*').eq('family_token', family_token).limit(1).execute()
            rows = r.data or []
            if rows:
                rec = rows[0]
        except Exception:
            rec = None
    if rec is None and os.path.exists(p):
        try:
            with open(p) as f:
                rec = json.load(f)
        except Exception:
            rec = None
    if rec and _is_active(rec):
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


@feed_status_bp.route('/api/feed-status/<crew_token>/incoming', methods=['GET'])
def incoming_statuses(crew_token):
    """Crew holt die aktiven Family-Nachrichten an sich (für den Feed)."""
    if not _safe_token(crew_token):
        return jsonify({'ok': False, 'error': 'invalid_token', 'statuses': []}), 400
    rows = _incoming_for_crew(crew_token)
    out = [_public_view(r) for r in rows]
    out.sort(key=lambda v: v.get('created_at') or '', reverse=True)
    return jsonify({'ok': True, 'statuses': out, 'count': len(out)})
