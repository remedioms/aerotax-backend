# ═══════════════════════════════════════════════════════════════
#  Feed-Status Blueprint — "verschwindende" 24h-Updates (2026-06)
#
#  Crew postet einen kurzen Status-Text (+ optionales Emoji), der genau
#  24 Stunden im Feed sichtbar ist und danach serverseitig verfällt.
#  Freunde sehen die aktiven Status der eigenen Crew-Friends in ihrem Feed.
#
#  Architektur (gleich wie family_watch / friends):
#      SB-Primary  → Tabelle feed_statuses (user_token PK, text, emoji,
#                    created_at, expires_at)
#      Disk-Fallback → _USER_HISTORY_DIR/feed_status_<token>.json
#      Late-binding auf app.py-Helper via _app_attr (sb, _friends_load,
#      _profile_load, _atomic_write_json, _USER_HISTORY_DIR).
#
#  Endpunkte:
#      POST   /api/feed-status/<token>           body {text, emoji?}
#      GET    /api/feed-status/<token>           → eigener aktiver Status (oder null)
#      DELETE /api/feed-status/<token>           → eigenen Status löschen
#      GET    /api/feed-status/<token>/friends   → aktive Status der Friends
#
#  Wiring in app.py:
#      ('blueprints.feed_status_blueprint', 'feed_status_bp'),
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


def _now_iso():
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _atomic_write_json(path, data):
    fn = _app_attr('_atomic_write_json')
    if callable(fn) and fn is not _atomic_write_json:
        return fn(path, data)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _disk_path(token):
    safe = _safe_token(token) or 'unknown'
    return os.path.join(_history_dir(), f'feed_status_{safe}.json')


def _is_active(rec):
    """True wenn der Status existiert und noch nicht abgelaufen ist."""
    if not isinstance(rec, dict):
        return False
    exp = rec.get('expires_at')
    if not exp:
        return False
    try:
        exp_dt = _dt.datetime.fromisoformat(exp.replace('Z', '+00:00'))
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=_dt.timezone.utc)
        return exp_dt > _dt.datetime.now(_dt.timezone.utc)
    except Exception:
        return False


# ── Persistenz ────────────────────────────────────────────────────────────────
def _status_load(token):
    """Liefert den aktuellen (evtl. abgelaufenen) Status-Record oder None.
    SB-primary, Disk-Fallback."""
    sb_avail, sb = _get_sb()
    if sb_avail and sb is not None:
        try:
            r = (sb.table('feed_statuses').select('*')
                 .eq('user_token', token).limit(1).execute())
            rows = r.data or []
            if rows:
                return rows[0]
        except Exception as e:
            _log().info(f'[feed-status] sb_load_skip {type(e).__name__}')
    # Disk-Fallback
    p = _disk_path(token)
    if os.path.exists(p):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            return None
    return None


def _status_save(token, rec):
    sb_avail, sb = _get_sb()
    sb_ok = False
    if sb_avail and sb is not None:
        try:
            sb.table('feed_statuses').upsert(
                {'user_token': token, **rec}, on_conflict='user_token').execute()
            sb_ok = True
        except Exception as e:
            _log().info(f'[feed-status] sb_save_skip {type(e).__name__}')
    disk_ok = False
    try:
        _atomic_write_json(_disk_path(token), rec)
        disk_ok = True
    except Exception as e:
        _log().warning(f'[feed-status] disk_save_fail {e}')
    return bool(sb_ok or disk_ok)


def _status_delete(token):
    sb_avail, sb = _get_sb()
    if sb_avail and sb is not None:
        try:
            sb.table('feed_statuses').delete().eq('user_token', token).execute()
        except Exception as e:
            _log().info(f'[feed-status] sb_del_skip {type(e).__name__}')
    try:
        p = _disk_path(token)
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass
    return True


def _public_view(rec):
    """Was nach aussen geht (kein user_token-Leak in friends-Liste über die ID hinaus)."""
    return {
        'text': rec.get('text'),
        'emoji': rec.get('emoji'),
        'created_at': rec.get('created_at'),
        'expires_at': rec.get('expires_at'),
    }


# ── Routes ──────────────────────────────────────────────────────────────────
@feed_status_bp.route('/api/feed-status/<token>', methods=['POST'])
def post_status(token):
    """Crew postet ein 24h-Update. Body {text, emoji?}. Ersetzt vorigen Status."""
    if not _safe_token(token):
        return jsonify({'ok': False, 'error': 'invalid_token'}), 400
    body = request.get_json(silent=True) or {}
    text = (body.get('text') or '').strip()
    if not text:
        return jsonify({'ok': False, 'error': 'empty_text'}), 400
    text = text[:_MAX_TEXT_LEN]
    emoji = (body.get('emoji') or '').strip()[:_MAX_EMOJI_LEN] or None
    now = _dt.datetime.now(_dt.timezone.utc)
    rec = {
        'text': text,
        'emoji': emoji,
        'created_at': now.isoformat(),
        'expires_at': (now + _dt.timedelta(seconds=_TTL_SECONDS)).isoformat(),
    }
    if not _status_save(token, rec):
        return jsonify({'ok': False, 'error': 'persist_failed'}), 500
    return jsonify({'ok': True, 'status': _public_view(rec)})


@feed_status_bp.route('/api/feed-status/<token>', methods=['GET'])
def get_status(token):
    """Eigener aktiver Status (oder null wenn keiner/abgelaufen)."""
    if not _safe_token(token):
        return jsonify({'ok': False, 'error': 'invalid_token'}), 400
    rec = _status_load(token)
    if rec and _is_active(rec):
        return jsonify({'ok': True, 'status': _public_view(rec)})
    # abgelaufen → best-effort aufräumen
    if rec:
        _status_delete(token)
    return jsonify({'ok': True, 'status': None})


@feed_status_bp.route('/api/feed-status/<token>', methods=['DELETE'])
def delete_status(token):
    """Eigenen Status früher entfernen."""
    if not _safe_token(token):
        return jsonify({'ok': False, 'error': 'invalid_token'}), 400
    _status_delete(token)
    return jsonify({'ok': True})


@feed_status_bp.route('/api/feed-status/<token>/friends', methods=['GET'])
def friends_statuses(token):
    """Aktive Status der eigenen Friends (für den Feed). Nur Friends, nur
    nicht-abgelaufene Status. Name/Avatar aus dem Profil."""
    if not _safe_token(token):
        return jsonify({'ok': False, 'error': 'invalid_token', 'statuses': []}), 400
    friends_fn = _app_attr('_friends_load')
    profile_fn = _app_attr('_profile_load')
    friends = []
    if callable(friends_fn):
        try:
            friends = (friends_fn(token) or {}).get('friends') or []
        except Exception:
            friends = []
    out = []
    for ft in friends:
        rec = _status_load(ft)
        if not (rec and _is_active(rec)):
            continue
        name, avatar = None, None
        if callable(profile_fn):
            try:
                prof = (profile_fn(ft) or {}).get('profile') or {}
                name = prof.get('name')
                avatar = prof.get('avatar_url')
            except Exception:
                pass
        view = _public_view(rec)
        view['friend_name'] = name or 'Crew'
        view['friend_avatar_url'] = avatar
        out.append(view)
    # Neueste zuerst
    out.sort(key=lambda v: v.get('created_at') or '', reverse=True)
    return jsonify({'ok': True, 'statuses': out, 'count': len(out)})
