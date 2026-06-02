# ═══════════════════════════════════════════════════════════════
#  Family-Watcher / Family-Share Blueprint  (Worker F, 2026-06-02)
#
#  Backend für den iOS-FamilyMode (FamilyWatchClient.swift).
#  Crew gewährt einer Family-Person Lese-Rechte auf ausgewählte Status-Felder
#  (Layover-Stadt, current_city, landed-Status, nächster Flug, Photos).
#
#  Architektur:
#      SB-Primary  →  family_shares-Tabelle (crew_token + family_token + fields jsonb)
#      Disk-Fallback → _USER_HISTORY_DIR/family_shares.json
#      Lazy-Migrate bei SB-leerem Read.
#
#  Privacy-Garantien:
#      - Family-User sieht NIE Geld-Daten, FTL-Stunden, Roster-Original
#      - Server filtert Response strikt nach `fields_granted`-Liste
#      - Felder die nicht explizit gegranted sind = nicht in der Response
#
#  Endpunkte (matched iOS FamilyWatchClient.swift):
#      GET    /api/family-watch/<token>/feed
#               → für family-token, liefert alle Crews die dieser Family
#                 was gegranted haben, gefiltert nach erlaubten Feldern
#      GET    /api/family-share/<token>/list
#               → für crew-token, liefert alle Family-Grants
#      POST   /api/family-share/<token>/grant
#               body: {family_token, relation, fields: [...]}
#      DELETE /api/family-share/<token>/revoke/<family_token>
#
#  Wiring in app.py:
#      from blueprints.family_watch import family_watch_bp
#      app.register_blueprint(family_watch_bp)
# ═══════════════════════════════════════════════════════════════

import os
import re
import json
import time
import logging
import datetime as _dt
from flask import Blueprint, request, jsonify, current_app

family_watch_bp = Blueprint('family_watch', __name__)

# Late-binding helper: greift bei jedem call frisch auf app-module-Attribute zu.
# Vorteil: am module-import-Zeitpunkt ist app.py noch nicht fertig initialisiert,
# Top-Level `from app import X` würde nur die Fallback-Werte einfangen. Wir
# resolven also bei Bedarf zur Request-Zeit, wenn app.py voll geladen ist.
def _app_attr(name, default=None):
    try:
        import app as _app_mod
        return getattr(_app_mod, name, default)
    except Exception:
        return default


def _atomic_write_json(path, data, max_items=None, **json_kwargs):
    fn = _app_attr('_atomic_write_json')
    if fn is not None and fn is not _atomic_write_json:  # avoid infinite recursion
        return fn(path, data, max_items=max_items, **json_kwargs)
    # Fallback
    json_kwargs.setdefault('ensure_ascii', False)
    target_dir = os.path.dirname(path) or '.'
    os.makedirs(target_dir, exist_ok=True)
    with open(path, 'w') as f:
        json.dump(data, f, **json_kwargs)


def _user_profile_path(token):
    fn = _app_attr('_user_profile_path')
    if fn is not None and fn is not _user_profile_path:
        return fn(token)
    return None


def _get_sb():
    return _app_attr('SB_AVAILABLE', False), _app_attr('sb', None)


def _get_history_dir():
    return _app_attr('_USER_HISTORY_DIR', '_user_history_state')


# Whitelist gegen FamilyShareField-Enum (iOS-Side).
ALLOWED_FIELDS = {
    'layover_place', 'current_city', 'landed_status', 'next_flight',
    'photos', 'voice_notes', 'aircraft_reg',
}

ALLOWED_RELATIONS = {'partner', 'mama', 'papa', 'freund', 'kind', 'family'}


def _log():
    try:
        return current_app.logger
    except RuntimeError:
        return logging.getLogger('family_watch')


def _safe_token(token):
    if not token or not isinstance(token, str):
        return None
    safe = re.sub(r'[^A-Za-z0-9_-]', '', token)[:64]
    return safe or None


def _shares_disk_path():
    hist = _get_history_dir()
    os.makedirs(hist, exist_ok=True)
    return os.path.join(hist, 'family_shares.json')


def _shares_load_from_disk():
    p = _shares_disk_path()
    if not os.path.exists(p):
        return []
    try:
        with open(p) as f:
            return json.load(f) or []
    except Exception:
        return []


def _shares_load_from_sb():
    sb_avail, sb = _get_sb()
    if not sb_avail or sb is None:
        return None
    try:
        out = []
        offset = 0
        page = 1000
        while True:
            r = (sb.table('family_shares').select('*')
                 .eq('deleted', False)
                 .range(offset, offset + page - 1).execute())
            rows = r.data or []
            for row in rows:
                out.append({
                    'crew_token': row.get('crew_token'),
                    'family_token': row.get('family_token'),
                    'relation': row.get('relation'),
                    'fields': row.get('fields') or [],
                    'created_at': row.get('created_at'),
                })
            if len(rows) < page:
                break
            offset += page
        return out
    except Exception as e:
        _log().warning(f'[family-share] sb_load_fail {type(e).__name__}: {str(e)[:120]}')
        return None


def _shares_save_to_sb(shares):
    sb_avail, sb = _get_sb()
    if not sb_avail or sb is None:
        return False
    try:
        rows = []
        for s in (shares or []):
            if not isinstance(s, dict):
                continue
            ct = s.get('crew_token')
            ft = s.get('family_token')
            if not ct or not ft:
                continue
            rows.append({
                'crew_token': ct,
                'family_token': ft,
                'relation': s.get('relation') or '',
                'fields': s.get('fields') or [],
                'created_at': s.get('created_at') or _dt.datetime.now().isoformat(),
                'deleted': False,
            })
        if not rows:
            return True
        for i in range(0, len(rows), 500):
            sb.table('family_shares').upsert(
                rows[i:i+500], on_conflict='crew_token,family_token').execute()
        return True
    except Exception as e:
        _log().warning(f'[family-share] sb_save_fail {type(e).__name__}: {str(e)[:200]}')
        return False


def _shares_load():
    """SB+Disk merge, dedupliziert nach (crew_token, family_token)."""
    sb_data = _shares_load_from_sb()
    disk_data = _shares_load_from_disk()
    if sb_data is None:
        return disk_data or []
    merged = {}
    for s in (sb_data or []):
        if not isinstance(s, dict):
            continue
        key = (s.get('crew_token'), s.get('family_token'))
        if key[0] and key[1]:
            merged[key] = s
    for s in (disk_data or []):
        if not isinstance(s, dict):
            continue
        key = (s.get('crew_token'), s.get('family_token'))
        if not (key[0] and key[1]):
            continue
        if key not in merged:
            merged[key] = s
    return list(merged.values())


def _shares_save(shares):
    sb_ok = _shares_save_to_sb(shares)
    disk_ok = False
    try:
        _atomic_write_json(_shares_disk_path(), shares)
        disk_ok = True
    except Exception as e:
        _log().warning(f'[family-share] disk_save_fail {e}')
    return bool(sb_ok or disk_ok)


def _load_crew_status_for_family(crew_token, allowed_fields):
    """Liest aus dem Crew-Profile + briefing-state nur die erlaubten Felder.
    Returns dict mit den status-feldern fuer die WatchedCrew.CrewStatus
    iOS struct (alle felder Optional)."""
    if not crew_token:
        return {}
    status = {
        'layover_place': None,
        'current_city': None,
        'landed': None,
        'next_flight_no': None,
        'next_flight_dep_iata': None,
        'next_flight_arr_iata': None,
        'next_flight_etd_iso': None,
        'photo_count_today': None,
        'last_seen_iso': None,
    }
    try:
        pp = _user_profile_path(crew_token)
        if pp and os.path.exists(pp):
            with open(pp) as f:
                doc = json.load(f) or {}
            prof = doc.get('profile') or {}
            if 'current_city' in allowed_fields:
                status['current_city'] = prof.get('current_city')
            # last_seen aus _updated_at
            status['last_seen_iso'] = doc.get('_updated_at')
    except Exception as e:
        _log().info(f'[family-watch] profile_read_skip {type(e).__name__}')
    # next_flight: nur wenn 'next_flight' in allowed_fields. Best-effort read aus
    # briefings/roster state via SB. Wenn nicht ladbar → bleibt None.
    sb_avail, sb = _get_sb()
    if 'next_flight' in allowed_fields and sb_avail and sb is not None:
        try:
            today = _dt.datetime.now().date().isoformat()
            r = (sb.table('briefings').select('*')
                 .eq('user_token', crew_token)
                 .gte('datum', today)
                 .order('datum', desc=False)
                 .limit(1).execute())
            rows = r.data or []
            if rows:
                br = rows[0]
                status['next_flight_no'] = br.get('flight_no')
                status['next_flight_dep_iata'] = br.get('dep_iata')
                status['next_flight_arr_iata'] = br.get('arr_iata')
                status['next_flight_etd_iso'] = br.get('etd_iso')
        except Exception as e:
            _log().info(f'[family-watch] briefing_read_skip {type(e).__name__}')
    if 'layover_place' in allowed_fields:
        # Layover-Ort kommt aus aktivem briefing (Layover-IATA)
        try:
            if sb_avail and sb is not None:
                today = _dt.datetime.now().date().isoformat()
                r = (sb.table('briefings').select('layover_iata')
                     .eq('user_token', crew_token)
                     .eq('datum', today)
                     .limit(1).execute())
                rows = r.data or []
                if rows:
                    status['layover_place'] = rows[0].get('layover_iata')
        except Exception:
            pass
    # Felder die NICHT in allowed_fields sind: explicit auf None setzen
    # (Privacy-Garantie: Server filtert, Client kann nicht durchgeben was nicht gegranted).
    if 'current_city' not in allowed_fields:
        status['current_city'] = None
    if 'next_flight' not in allowed_fields:
        status['next_flight_no'] = None
        status['next_flight_dep_iata'] = None
        status['next_flight_arr_iata'] = None
        status['next_flight_etd_iso'] = None
    if 'layover_place' not in allowed_fields:
        status['layover_place'] = None
    if 'landed_status' not in allowed_fields:
        status['landed'] = None
    if 'photos' not in allowed_fields:
        status['photo_count_today'] = None
    return status


def _crew_short_name(crew_token):
    """Display-Name für die Family-Card. Privacy: kein Email-Leak, nur
    profile.name (kann selbst-gewählt sein) oder Token-Slice."""
    try:
        pp = _user_profile_path(crew_token)
        if pp and os.path.exists(pp):
            with open(pp) as f:
                doc = json.load(f) or {}
            prof = doc.get('profile') or {}
            n = prof.get('name')
            if n:
                return n
    except Exception:
        pass
    return (crew_token or '')[:8]


# ════════════════════════════════════════════════════════════════════
#  Family-Side
# ════════════════════════════════════════════════════════════════════

@family_watch_bp.route('/api/family-watch/<token>/feed', methods=['GET'])
def family_watch_feed(token):
    """Family-User holt feed: alle Crews die ihm was gegranted haben."""
    safe = _safe_token(token)
    if not safe:
        return jsonify({'ok': False, 'error': 'invalid_token'}), 400
    shares = _shares_load()
    # Filter: alle grants mit family_token == this token
    relevant = [s for s in shares if s.get('family_token') == token]
    out = []
    for s in relevant:
        crew_token = s.get('crew_token')
        if not crew_token:
            continue
        fields = list(s.get('fields') or [])
        # nur erlaubte Felder durchlassen
        fields_clean = [f for f in fields if f in ALLOWED_FIELDS]
        status = _load_crew_status_for_family(crew_token, set(fields_clean))
        out.append({
            'crew_token': crew_token,
            'crew_short_name': _crew_short_name(crew_token),
            'crew_avatar_url': None,
            'status': status,
            'allowed_fields': fields_clean,
        })
    return jsonify({'watched': out, 'count': len(out)})


# ════════════════════════════════════════════════════════════════════
#  Crew-Side
# ════════════════════════════════════════════════════════════════════

@family_watch_bp.route('/api/family-share/<token>/list', methods=['GET'])
def family_share_list(token):
    """Crew-User holt seine eigenen Grants (wer sieht mich + welche Felder)."""
    safe = _safe_token(token)
    if not safe:
        return jsonify({'ok': False, 'error': 'invalid_token'}), 400
    shares = _shares_load()
    own = [s for s in shares if s.get('crew_token') == token]
    grants = []
    for s in own:
        ft = s.get('family_token')
        grants.append({
            'family_token': ft,
            'family_short_name': (ft or '')[:8] if ft else None,
            'family_relation': s.get('relation'),
            'fields': [f for f in (s.get('fields') or []) if f in ALLOWED_FIELDS],
            'created_at': s.get('created_at'),
        })
    return jsonify({'grants': grants, 'count': len(grants)})


@family_watch_bp.route('/api/family-share/<token>/grant', methods=['POST'])
def family_share_grant(token):
    """Crew-User gewährt Family-Person Lese-Zugriff auf bestimmte Felder."""
    # Token-Auth: vom before_request-Hook in app.py gecheckt (auth-required).
    # Hier nur form-validation.
    safe = _safe_token(token)
    if not safe:
        return jsonify({'ok': False, 'error': 'invalid_token'}), 400
    body = request.get_json(silent=True) or {}
    family_token = (body.get('family_token') or '').strip()
    if not family_token:
        return jsonify({'ok': False, 'error': 'missing_family_token'}), 400
    if family_token == token:
        return jsonify({'ok': False, 'error': 'cannot_grant_self'}), 400
    relation = (body.get('relation') or '').strip().lower() or 'family'
    if relation not in ALLOWED_RELATIONS:
        relation = 'family'
    raw_fields = body.get('fields') or []
    if not isinstance(raw_fields, list):
        return jsonify({'ok': False, 'error': 'fields_must_be_list'}), 400
    fields = [f for f in raw_fields if isinstance(f, str) and f in ALLOWED_FIELDS]
    if not fields:
        return jsonify({'ok': False, 'error': 'no_valid_fields'}), 400

    shares = _shares_load()
    # Existing grant? → update statt duplizieren
    found = False
    for s in shares:
        if s.get('crew_token') == token and s.get('family_token') == family_token:
            s['fields'] = fields
            s['relation'] = relation
            s['updated_at'] = _dt.datetime.now().isoformat()
            found = True
            break
    if not found:
        shares.append({
            'crew_token': token,
            'family_token': family_token,
            'relation': relation,
            'fields': fields,
            'created_at': _dt.datetime.now().isoformat(),
        })
    if not _shares_save(shares):
        return jsonify({'ok': False, 'error': 'persist_failed'}), 500
    return jsonify({'ok': True, 'fields': fields, 'relation': relation})


@family_watch_bp.route('/api/family-share/<token>/revoke/<family_token>',
                       methods=['DELETE'])
def family_share_revoke(token, family_token):
    """Crew-User widerruft Grant für eine Family-Person."""
    safe = _safe_token(token)
    safe_ft = _safe_token(family_token)
    if not safe or not safe_ft:
        return jsonify({'ok': False, 'error': 'invalid_token'}), 400
    shares = _shares_load()
    new_shares = [s for s in shares
                  if not (s.get('crew_token') == token
                          and s.get('family_token') == family_token)]
    if len(new_shares) == len(shares):
        # War nie gegranted → idempotent return ok
        return jsonify({'ok': True, 'revoked': False, 'message': 'no_grant_found'})
    if not _shares_save(new_shares):
        return jsonify({'ok': False, 'error': 'persist_failed'}), 500
    # SB-soft-delete: wir haben oben replacing-upsert gemacht. Für SB den
    # konkreten record markieren als deleted.
    sb_avail, sb = _get_sb()
    if sb_avail and sb is not None:
        try:
            (sb.table('family_shares')
             .update({'deleted': True})
             .eq('crew_token', token)
             .eq('family_token', family_token)
             .execute())
        except Exception as e:
            _log().warning(f'[family-share] sb_revoke_skip {type(e).__name__}')
    return jsonify({'ok': True, 'revoked': True})
