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


def _load_crew_profile(token):
    """Lädt das Crew-Profil-Dict ({name, homebase, ...}) GENAU so wie der
    Endpoint GET /api/user/profile/<token> es liest:
      Supabase-primary (user_profiles) → Disk-Fallback (_user_profile_path).
    Vorher lasen die Family-Helper NUR die Disk-Datei — auf Render/Cloud-Run
    liegt das Profil aber in Supabase, die Disk-Datei ist ephemeral/leer, daher
    kamen Token-Slice als Name + None als Homebase zurück.
    Returns dict (kann leer sein), nie None."""
    if not token:
        return {}
    # 1) Bevorzugt _profile_load aus app.py (SB-primary, Disk-Fallback) — exakt
    #    der Pfad den GET /api/user/profile nutzt.
    fn = _app_attr('_profile_load')
    if callable(fn):
        try:
            doc = fn(token) or {}
            prof = doc.get('profile')
            if isinstance(prof, dict):
                return prof
        except Exception as e:
            _log().info(f'[family-pair] profile_load_skip {type(e).__name__}')
    # 2) Disk-only Fallback (falls _profile_load nicht verfügbar).
    try:
        pp = _user_profile_path(token)
        if pp and os.path.exists(pp):
            with open(pp) as f:
                doc = json.load(f) or {}
            prof = doc.get('profile')
            if isinstance(prof, dict):
                return prof
    except Exception:
        pass
    return {}


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
        # SB-primary statt Disk: auf Cloud Run ist die Profil-Disk-Datei ephemer/
        # leer → die Family-Watcher sahen weder current_city noch last_seen
        # („selbst wenn verbunden, sieht sie nichts vom Plan"). _load_crew_profile
        # + _profile_load lesen Supabase-first.
        prof = _load_crew_profile(crew_token) or {}
        if 'current_city' in allowed_fields:
            status['current_city'] = prof.get('current_city')
        _pl = _app_attr('_profile_load')
        if callable(_pl):
            full = _pl(crew_token) or {}
            status['last_seen_iso'] = full.get('_updated_at')
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
    profile.name (kann selbst-gewählt sein). Liest SB-primary via
    _load_crew_profile (gleicher Pfad wie GET /api/user/profile).
    Fallback NIE der rohe Token (der ist ein Auth-Slice, kein Anzeigename),
    sondern ein neutraler Platzhalter."""
    prof = _load_crew_profile(crew_token)
    n = prof.get('name')
    if isinstance(n, str) and n.strip():
        return n.strip()
    return 'AeroX-Crew'


def _crew_homebase(crew_token):
    """Homebase-IATA aus dem Crew-Profil (für die Redeem-Bestätigung der
    Family-Person). Liest SB-primary via _load_crew_profile (gleicher Pfad wie
    GET /api/user/profile). None wenn nicht gesetzt."""
    prof = _load_crew_profile(crew_token)
    hb = prof.get('homebase') or prof.get('home_base')
    if isinstance(hb, str) and hb.strip():
        return hb.strip()
    return None


# ════════════════════════════════════════════════════════════════════
#  Pairing-Code + Scoped Family-Token  (Security-Fix 2026-06-05)
#
#  Vorher teilte die iOS-App den ROHEN Crew-Bearer (appState.token) als
#  "Verbindungs-Code" — ein App-weites Auth-Credential. Wer es abfing,
#  konnte sich als der Crew-Account ausgeben.
#
#  Neuer Flow:
#    1) Crew ruft  POST /api/family/pair-code/<crew_token>/create
#       → kurzer Code (6 Zeichen, A-Z2-9 ohne Ambiguität), TTL 30 min,
#         regenerieren invalidiert den vorherigen Code dieses Crews.
#    2) Family ruft POST /api/family/pair-code/redeem  body={code,family_name}
#       → erzeugt einen SCOPED, read-only family_token (NICHT der Crew-Bearer),
#         der nur die family-watch-Read-Pfade für diesen Crew freischaltet.
#         Returns {family_token, crew_name, crew_homebase}.
#
#  Der scoped family_token (Prefix AT-FAM-) wird in einer eigenen Tabelle
#  family_token -> crew_token (read-only scope) gehalten. Der bestehende
#  /api/family-watch/<token>/feed akzeptiert BEIDE: den scoped Token (neu,
#  bevorzugt) und — back-compat — jeden family_token der via grant existiert.
# ════════════════════════════════════════════════════════════════════

_PAIR_CODE_TTL_SEC = 30 * 60          # 30 Minuten
_PAIR_CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'  # ohne I/O/0/1
_PAIR_CODE_LEN = 6


def _pair_codes_disk_path():
    hist = _get_history_dir()
    os.makedirs(hist, exist_ok=True)
    return os.path.join(hist, 'family_pair_codes.json')


def _scoped_tokens_disk_path():
    hist = _get_history_dir()
    os.makedirs(hist, exist_ok=True)
    return os.path.join(hist, 'family_scoped_tokens.json')


# --- SB-Persistenz für Pair-Codes + Scoped-Tokens (#31) ------------------------
# Vorher waren beide NUR auf Disk → auf Cloud Run (ephemer, multi-instance, bei
# jedem Deploy gewiped) verloren sich Pair-Codes (Redeem schlug fehl) und
# Scoped-Family-Tokens (Family-Watcher sah „seinen" Plan nicht mehr). Jetzt
# SB-primary mit Disk-Fallback. SICHER: existieren die SB-Tabellen noch nicht
# (User hat PASTE_ME_IN_SUPABASE.sql noch nicht ausgeführt), werfen die SB-Calls
# → None/False → es bleibt beim bisherigen Disk-Verhalten (keine Regression).
# `data` ist ein jsonb-Blob (der komplette Record), Key ist code bzw. family_token.
def _kv_load_from_sb(table, key_col):
    sb_avail, sb = _get_sb()
    if not sb_avail or sb is None:
        return None
    try:
        out = {}
        r = sb.table(table).select('*').execute()
        for row in (r.data or []):
            k = row.get(key_col)
            if k:
                out[k] = row.get('data') or {}
        return out
    except Exception as e:
        _log().warning(f'[family-kv] {table} sb_load_fail {type(e).__name__}: {str(e)[:120]}')
        return None


def _kv_save_to_sb(table, key_col, data):
    """Reconciliert SB exakt auf `data` (kleine Tabellen): entfernte Keys löschen,
    vorhandene upserten. So propagieren auch Deletes (z.B. eingelöster Pair-Code)."""
    sb_avail, sb = _get_sb()
    if not sb_avail or sb is None:
        return False
    try:
        new_keys = {k for k in (data or {}).keys() if k}
        existing = sb.table(table).select(key_col).execute()
        existing_keys = {row.get(key_col) for row in (existing.data or []) if row.get(key_col)}
        for rk in (existing_keys - new_keys):
            sb.table(table).delete().eq(key_col, rk).execute()
        rows = [{key_col: k, 'data': v} for k, v in (data or {}).items() if k]
        for i in range(0, len(rows), 500):
            sb.table(table).upsert(rows[i:i+500], on_conflict=key_col).execute()
        return True
    except Exception as e:
        _log().warning(f'[family-kv] {table} sb_save_fail {type(e).__name__}: {str(e)[:160]}')
        return False


def _pair_codes_load_from_disk():
    p = _pair_codes_disk_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p) as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _pair_codes_load():
    sb = _kv_load_from_sb('family_pair_codes', 'code')
    disk = _pair_codes_load_from_disk()
    if sb is None:
        return disk
    merged = dict(disk); merged.update(sb)   # SB-primary
    return merged


def _pair_codes_save(codes):
    sb_ok = _kv_save_to_sb('family_pair_codes', 'code', codes)
    disk_ok = False
    try:
        _atomic_write_json(_pair_codes_disk_path(), codes)
        disk_ok = True
    except Exception as e:
        _log().warning(f'[family-pair] codes_save_fail {e}')
    return bool(sb_ok or disk_ok)


def _scoped_tokens_load_from_disk():
    p = _scoped_tokens_disk_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p) as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _scoped_tokens_load():
    sb = _kv_load_from_sb('family_scoped_tokens', 'family_token')
    disk = _scoped_tokens_load_from_disk()
    if sb is None:
        return disk
    merged = dict(disk); merged.update(sb)   # SB-primary
    return merged


def _scoped_tokens_save(toks):
    sb_ok = _kv_save_to_sb('family_scoped_tokens', 'family_token', toks)
    disk_ok = False
    try:
        _atomic_write_json(_scoped_tokens_disk_path(), toks)
        disk_ok = True
    except Exception as e:
        _log().warning(f'[family-pair] scoped_save_fail {e}')
    return bool(sb_ok or disk_ok)


def _scoped_token_crew(family_token):
    """Returns crew_token wenn family_token ein gültiger scoped read-only
    Family-Token ist (Prefix AT-FAM- in family_scoped_tokens), sonst None."""
    if not family_token:
        return None
    toks = _scoped_tokens_load()
    rec = toks.get(family_token)
    if isinstance(rec, dict):
        return rec.get('crew_token')
    return None


def _gen_pair_code():
    import secrets
    return ''.join(secrets.choice(_PAIR_CODE_ALPHABET) for _ in range(_PAIR_CODE_LEN))


def _gen_scoped_family_token():
    import secrets
    return 'AT-FAM-' + secrets.token_urlsafe(18)


def _normalize_code(raw):
    """Uppercase, Whitespace/Bindestriche weg, dann tolerant gegen die typischen
    Tipp-Verwechsler mappen (0→O, 1→I, I→? ...). Da das Generator-Alphabet
    weder I/O/0/1 enthält, mappen wir die wahrscheinlichen Verwechsler auf ihr
    Alphabet-Pendant: 0→O ist NICHT im Alphabet, also nutzen wir die andere
    Richtung — wir behandeln O als 0-Tippfehler? Beides ambig. Einfacher: wir
    werfen alles raus was NICHT im Alphabet ist (A-HJ-NP-Z + 2-9)."""
    if not raw or not isinstance(raw, str):
        return ''
    s = re.sub(r'[^A-Za-z0-9]', '', raw).upper()
    # Häufige Tippfehler auf gültige Alphabet-Zeichen korrigieren:
    #   0 (Null)  → O ist NICHT im Alphabet → verwerfen
    #   1 (Eins)  → I ist NICHT im Alphabet → verwerfen
    # Wir verwerfen daher schlicht alle Nicht-Alphabet-Zeichen.
    s = re.sub(r'[^A-HJ-NP-Z2-9]', '', s)
    return s[:_PAIR_CODE_LEN]


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
    # Scoped read-only Family-Token (neu, bevorzugt): er ist NICHT der Crew-Bearer,
    # sondern ein per Pairing-Code gemünzter Token der genau auf einen Crew zeigt.
    # Wenn das eingehende Token ein solcher ist, verwenden wir die scoped-Tabelle
    # als zusätzliche Quelle (ein Crew-Eintrag, auch wenn noch kein grant existiert).
    scoped_crew = _scoped_token_crew(token)
    # Filter: alle grants mit family_token == this token (back-compat)
    relevant = [s for s in shares if s.get('family_token') == token]
    if scoped_crew:
        # Sicherstellen dass der gepairte Crew im Feed auftaucht, auch falls der
        # Crew noch keine Felder explizit gegranted hat → dann mit leerer
        # allowed_fields-Liste (Card zeigt "wartet auf Freigabe").
        if not any(s.get('crew_token') == scoped_crew for s in relevant):
            relevant.append({'crew_token': scoped_crew, 'family_token': token,
                             'fields': []})
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


# ════════════════════════════════════════════════════════════════════
#  Pairing-Code-Endpunkte
# ════════════════════════════════════════════════════════════════════

@family_watch_bp.route('/api/family/pair-code/<token>/create', methods=['POST'])
def family_pair_code_create(token):
    """Crew erzeugt einen kurzen, kurzlebigen Pairing-Code.

    Auth: der Crew-Bearer steht im Pfad (<token>) → der zentrale
    before_request-Gate (_bug004_token_auth_gate) validiert ihn gegen
    auth_users, weil das AT-...-Pattern matched und es ein POST ist.

    Regenerieren invalidiert den vorherigen Code dieses Crews (1 aktiver Code
    pro Crew). Returns {code, expires_in}.
    """
    safe = _safe_token(token)
    if not safe:
        return jsonify({'ok': False, 'error': 'invalid_token'}), 400

    codes = _pair_codes_load()
    now = time.time()
    # Alte/abgelaufene Codes ausmisten + jeden vorhandenen Code DIESES Crews
    # entfernen (Regenerate invalidiert den vorigen).
    codes = {c: rec for c, rec in codes.items()
             if isinstance(rec, dict)
             and (now - float(rec.get('created_at', 0))) < _PAIR_CODE_TTL_SEC
             and rec.get('crew_token') != token}

    # Neuen, kollisionsfreien Code generieren.
    code = _gen_pair_code()
    tries = 0
    while code in codes and tries < 10:
        code = _gen_pair_code()
        tries += 1

    codes[code] = {
        'crew_token': token,
        'created_at': now,
        'consumed': False,
    }
    _pair_codes_save(codes)
    return jsonify({'ok': True, 'code': code, 'expires_in': _PAIR_CODE_TTL_SEC})


@family_watch_bp.route('/api/family/pair-code/redeem', methods=['POST'])
def family_pair_code_redeem():
    """Family-Person löst einen Pairing-Code ein.

    Public (kein Auth-Gate): der Family-User hat noch keinen Crew-Token; der
    kurze Code IST das Geheimnis. body={code, family_name?}.

    Bei gültigem, nicht-abgelaufenem Code wird ein SCOPED, read-only
    family_token (Prefix AT-FAM-) gemünzt der nur auf diesen einen Crew zeigt —
    NICHT der Crew-Bearer. Returns {family_token, crew_name, crew_homebase}.
    """
    body = request.get_json(silent=True) or {}
    code = _normalize_code(body.get('code') or '')
    if not code or len(code) != _PAIR_CODE_LEN:
        return jsonify({'ok': False, 'error': 'invalid_code'}), 400

    codes = _pair_codes_load()
    now = time.time()
    rec = codes.get(code)
    if not isinstance(rec, dict):
        return jsonify({'ok': False, 'error': 'code_not_found'}), 404
    if (now - float(rec.get('created_at', 0))) >= _PAIR_CODE_TTL_SEC:
        # Abgelaufen → aufräumen.
        codes.pop(code, None)
        _pair_codes_save(codes)
        return jsonify({'ok': False, 'error': 'code_expired'}), 410

    crew_token = rec.get('crew_token')
    if not crew_token:
        return jsonify({'ok': False, 'error': 'code_invalid'}), 400

    family_name = (body.get('family_name') or '').strip()[:60] or None

    # Scoped, read-only Family-Token münzen und persistieren.
    family_token = _gen_scoped_family_token()
    toks = _scoped_tokens_load()
    toks[family_token] = {
        'crew_token': crew_token,
        'scope': 'family_read',
        'family_name': family_name,
        'created_at': now,
    }
    _scoped_tokens_save(toks)

    # Code als konsumiert markieren (bleibt bis TTL einlösbar für den Fall eines
    # Retry, aber wir merken consumed für Audit).
    rec['consumed'] = True
    rec['consumed_at'] = now
    codes[code] = rec
    _pair_codes_save(codes)

    return jsonify({
        'ok': True,
        'family_token': family_token,
        'crew_name': _crew_short_name(crew_token),
        'crew_homebase': _crew_homebase(crew_token),
    })
