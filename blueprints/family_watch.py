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
import hashlib
import logging
import datetime as _dt
from flask import Blueprint, request, jsonify, current_app

# Städtenamen-Labels (2026-07-03): Family sieht IMMER "Frankfurt – San
# Francisco" statt roher IATA-Ketten. Kein Zirkel: aerox_data_blueprint
# importiert app nur lazy in Funktionen.
from blueprints.aerox_data_blueprint import _route_label_cities, _iata_city_name

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


# ── Family-Watch-ANFRAGEN (Suche statt Code) ─────────────────────────────────
# Familie sucht die Crew-Person (searchUsers → token), schickt eine Anfrage; die
# Crew bestätigt → daraus entsteht der normale family_shares-Grant. Disk-Store
# (klein, kurzlebig); SB optional. Kein Code mehr nötig.
def _requests_disk_path():
    hist = _get_history_dir()
    os.makedirs(hist, exist_ok=True)
    return os.path.join(hist, 'family_requests.json')


def _requests_load_from_disk():
    p = _requests_disk_path()
    if not os.path.exists(p):
        return []
    try:
        with open(p) as f:
            return json.load(f) or []
    except Exception:
        return []


def _requests_load_from_sb():
    """SB-Read der offenen Family-Anfragen. None wenn SB nicht verfügbar/Tabelle
    fehlt → Caller fällt auf Disk zurück."""
    sb_avail, sb = _get_sb()
    if not sb_avail or sb is None:
        return None
    try:
        out = []
        r = sb.table('family_requests').select('*').execute()
        for row in (r.data or []):
            out.append({
                'crew_token': row.get('crew_token'),
                'family_token': row.get('family_token'),
                'relation': row.get('relation'),
                'requester_name': row.get('requester_name'),
                'requester_avatar': row.get('requester_avatar'),
                'created_at': row.get('created_at'),
            })
        return out
    except Exception as e:
        _log().warning(f'[family-req] sb_load_fail {type(e).__name__}: {str(e)[:120]}')
        return None


def _requests_save_to_sb(reqs):
    """UPSERT-ONLY: upsertet die vorhandenen (crew,family)-Paare in SB.
    KEIN Reconcile-Delete mehr (#7/#19): auf Cloud Run multi-instance hätte das
    Löschen von "fehlenden" Keys aus einem stalen In-Memory-Snapshot Anfragen
    anderer User/Container weggewischt (Instance A löscht was Instance B gerade
    angelegt hat). Echte Deletes (Zurückziehen/Approve/Reject) machen die Caller
    jetzt per gezieltem .delete().eq(...)."""
    sb_avail, sb = _get_sb()
    if not sb_avail or sb is None:
        return False
    try:
        want = {}
        for r in (reqs or []):
            ct, ft = r.get('crew_token'), r.get('family_token')
            if ct and ft:
                want[(ct, ft)] = r
        rows = [{
            'crew_token': r.get('crew_token'),
            'family_token': r.get('family_token'),
            'relation': r.get('relation') or 'family',
            'requester_name': r.get('requester_name'),
            'requester_avatar': r.get('requester_avatar'),
            'created_at': r.get('created_at') or _now_utc_z(),
        } for r in want.values()]
        for i in range(0, len(rows), 500):
            sb.table('family_requests').upsert(
                rows[i:i+500], on_conflict='crew_token,family_token').execute()
        return True
    except Exception as e:
        _log().warning(f'[family-req] sb_save_fail {type(e).__name__}: {str(e)[:160]}')
        return False


def _requests_load():
    """SB-primary + Disk-merge, dedupliziert nach (crew_token, family_token).
    KRITISCH auf Cloud Run: die Disk ist ephemer/multi-instance → eine nur auf Disk
    gespeicherte Anfrage sah die Crew-Person (anderer Container) NIE („Anfrage von
    der Familie poppt nicht auf"). Jetzt SB-primary."""
    sb_data = _requests_load_from_sb()
    disk_data = _requests_load_from_disk()
    if sb_data is None:
        return disk_data or []
    merged = {}
    for r in (sb_data or []):
        key = (r.get('crew_token'), r.get('family_token'))
        if key[0] and key[1]:
            merged[key] = r
    for r in (disk_data or []):
        key = (r.get('crew_token'), r.get('family_token'))
        if key[0] and key[1] and key not in merged:
            merged[key] = r
    return list(merged.values())


def _requests_save(reqs):
    sb_ok = _requests_save_to_sb(reqs)
    disk_ok = False
    try:
        _atomic_write_json(_requests_disk_path(), reqs, max_items=2000)
        disk_ok = True
    except Exception:
        pass
    return bool(sb_ok or disk_ok)


# Last-known-good In-Process-Cache (2026-07-03): Ein transienter SB-Flake
# (RemoteProtocolError auf stale Keep-Alive) darf den Family-Feed NICHT auf
# den ephemeren (auf Cloud Run meist leeren) Disk-Fallback stürzen lassen —
# das war der "keine aktuelle Info"-Bug. Wir behalten den letzten
# erfolgreichen SB-Stand im Prozess (TTL-begrenzt, damit Revokes nicht ewig
# nachhallen) und servieren den bei SB-Fail statt einer leeren Liste.
_SB_LAST_GOOD_TTL_S = 15 * 60
_shares_last_good = {'data': None, 'at': 0.0}


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
        _shares_last_good['data'] = [dict(s) for s in out]
        _shares_last_good['at'] = time.time()
        return out
    except Exception as e:
        _log().warning(f'[family-share] sb_load_fail {type(e).__name__}: {str(e)[:120]}')
        lg = _shares_last_good.get('data')
        if lg is not None and (time.time() - _shares_last_good.get('at', 0)) < _SB_LAST_GOOD_TTL_S:
            _log().info(f'[family-share] serving last-good snapshot ({len(lg)} shares)')
            return [dict(s) for s in lg]
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
                'created_at': s.get('created_at') or _now_utc_z(),
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


def _iso_or_none(v):
    """Roher DB-Zeitwert → getrimmter ISO-String (oder None). NICHT abschneiden —
    der Offset (+00:00)/Mikrosekunden müssen erhalten bleiben, sonst parst die
    Zeitzone falsch."""
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _parse_iso(s):
    """Toleranter ISO→aware-UTC-Parser. Naive (ohne Zone) wird als UTC gewertet."""
    if not s:
        return None
    try:
        t = str(s).strip().replace('Z', '+00:00')
        d = _dt.datetime.fromisoformat(t)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return d.astimezone(_dt.timezone.utc)
    except Exception:
        return None


def _iso_utc_z(v):
    """Roher Zeitwert → kanonischer API-Zeitstempel 'YYYY-MM-DDTHH:MM:SSZ'
    (UTC, ohne Mikrosekunden). DER eine Ausgabe-Weg für alle Status-Zeiten
    (Audit 2026-07-05): vorher gingen SB-Werte roh per 25-Zeichen-Chop raus —
    bei Mikrosekunden ('…T10:30:00.123456+00:00') schnitt das den Offset ab
    und ließ verstümmelte Bruchteile stehen, iOS-Parser scheiterten.
    Unparsebares wird UNGEKÜRZT durchgereicht (nie mitten im String choppen),
    None bleibt None."""
    if v is None:
        return None
    d = _parse_iso(v)
    if d is not None:
        return d.strftime('%Y-%m-%dT%H:%M:%SZ')
    return _iso_or_none(v)


def _now_utc_z():
    """Jetzt als kanonischer API-Zeitstempel (UTC-Z, sekundengenau) — statt
    datetime.now().isoformat() (naiv-lokal + Mikrosekunden, Format hing vom
    Container-TZ ab)."""
    return _dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# Last-known-good Status-Cache pro (crew_token, granted-fields) — Privacy:
# der Fieldset ist Teil des Keys, damit ein Family-Member mit weniger Grants
# nie den volleren Status eines anderen Members serviert bekommt. Einträge
# tragen ihr eigenes as_of (Zeitpunkt des erfolgreichen Reads) → die App kann
# ehrlich "Stand von HH:MM" zeigen statt "Status unbekannt".
_crew_status_last_good = {}   # (crew_token, frozenset(fields)) → status-dict (inkl. as_of)
_CREW_STATUS_CACHE_MAX = 500


def _status_has_signal(status):
    """True wenn der Status irgendeine echte Info trägt (nicht der All-None-
    'Status unbekannt'-Fall)."""
    if not isinstance(status, dict):
        return False
    if status.get('flying_now') is True or status.get('home_now') is True:
        return True
    for k in ('layover_place', 'layover_place_city', 'current_city', 'landed',
              'next_flight_dep_iata', 'today_dep_iata', 'today_route_label',
              'last_seen_iso'):
        if status.get(k) is not None:
            return True
    return False


def _fallback_next_tour_from_disk(status, crew_token, allowed_fields):
    """Aller-letzter Fallback ('es gibt immer eine Info'): SB unlesbar UND kein
    Cache → nächste Tour aus dem Disk-Mirror der iCal-Briefings (app.py
    schreibt bei jedem Import nach SB UND Disk; im lebenden Container liegt
    da der letzte Stand). Respektiert die Privacy-Gates: nur wenn die Crew
    'next_flight' gegranted hat. True wenn etwas gefüllt wurde."""
    if 'next_flight' not in allowed_fields:
        return False
    loader = _app_attr('_ical_briefings_load_from_disk')
    if not callable(loader):
        return False
    try:
        events = loader(crew_token) or {}
    except Exception:
        return False
    today = _dt.datetime.now().date().isoformat()
    for datum in sorted(k for k in events if isinstance(k, str) and k[:10] >= today):
        ev = events.get(datum) or {}
        if not isinstance(ev, dict):
            continue
        summ = str(ev.get('ical_summary') or '')
        loc = str(ev.get('ical_location') or '')
        legs = re.findall(r'\b([A-Z]{3})-([A-Z]{3})\b', summ)
        if not legs:
            legs = re.findall(r'\b([A-Z]{3})-([A-Z]{3})\b', loc)
        if not legs:
            continue
        chain = [legs[0][0]] + [b for _, b in legs]
        status['next_flight_dep_iata'] = legs[0][0]
        status['next_flight_arr_iata'] = legs[0][1]
        status['next_flight_dep_city'] = _iata_city_name(legs[0][0])
        status['next_flight_arr_city'] = _iata_city_name(legs[0][1])
        status['today_route_label'] = _route_label_cities('-'.join(chain))
        st = ev.get('ical_start_iso')
        if st:
            status['next_flight_etd_iso'] = _iso_utc_z(st)
        return True
    return False


def _parse_roster_day(row):
    """Ein user_ical_briefings-Row → strukturierte Tages-Fakten (PUR, testbar).
    Kette bevorzugt aus dem Summary („FRA-MUC-BIO 14:30-…"), sonst aus der
    Routing-Kette in ical_location („BCN-BIO-MUC").

    WICHTIG (Root-Fix 2026-07-06): ical_location trägt die TOUR-Kette auch an
    reinen LAYOVER-TagEN („HND, FRA-HND" am Ruhetag in Tokio). Eine Kette
    allein macht den Tag also NICHT zum Flugtag — ein Ganztags-Fenster (≥20h)
    ist ein Layover-/Ruhetag, kein Dienst."""
    loc = (row.get('ical_location') or '').strip()
    summ = row.get('ical_summary') or ''
    legs = re.findall(r'\b([A-Z]{3})-([A-Z]{3})\b', summ)
    chain = None
    if legs:
        chain = [legs[0][0]] + [b for _, b in legs]
    else:
        mchain = re.search(r'\b([A-Z]{3}(?:-[A-Z]{3})+)\b', loc)
        if mchain:
            chain = mchain.group(1).split('-')
    st_iso = _iso_utc_z(row.get('ical_start'))
    en_iso = _iso_utc_z(row.get('ical_end'))
    st = _parse_iso(st_iso)
    en = _parse_iso(en_iso)
    is_flight = bool(chain) and len(chain) >= 2
    if is_flight and st and en and (en - st).total_seconds() >= 20 * 3600:
        is_flight = False
    # Pro-Leg-Sektoren (Root-Fix 2026-07-10): SB legt ical_sectors im
    # raw_event-jsonb ab (_ical_briefings_save_to_supabase whitelistet nur die
    # Spalten); Disk-/Snapshot-Rows tragen den Key flach. Beides tolerieren.
    raw = row.get('raw_event')
    secs = raw.get('ical_sectors') if isinstance(raw, dict) else None
    if not (isinstance(secs, list) and secs):
        secs = row.get('ical_sectors')
        secs = secs if isinstance(secs, list) and secs else None
    return {'datum': row.get('datum'), 'chain': chain,
            'first': loc.split(',')[0].strip().upper(),
            'st': st, 'en': en, 'st_iso': st_iso, 'en_iso': en_iso,
            'is_flight': is_flight, 'sectors': secs}


def _flight_window_state(day, legs_live, now):
    """(fliegt_jetzt, effektives_ende, landung_beobachtet) für einen Flugtag
    (PUR, testbar): Dienst-Fenster aus dem Plan, korrigiert durch ECHTE
    Beobachtungen — eine beobachtete Verspätung verlängert das Fenster, eine
    beobachtete Landung beendet es. Ohne Beobachtung gilt der Plan (kein Raten)."""
    st, en = day['st'], day['en']
    # Fehlt das Ende → grobes Fenster Start … Start+10h (Langstrecke abgedeckt).
    if st and not en:
        en = st + _dt.timedelta(hours=10)
    en_eff = en
    landed_obs = False
    if legs_live:
        last = legs_live[-1]
        d = last.get('delay_min')
        if en_eff is not None and isinstance(d, (int, float)):
            en_eff = en_eff + _dt.timedelta(minutes=float(d))
        stx = str(last.get('status') or '').lower()
        if any(k in stx for k in ('landed', 'gelandet', 'arrived', 'angekommen')):
            landed_obs = True
    flying = bool(st and en_eff and st <= now <= en_eff and not landed_obs)
    return flying, en_eff, landed_obs


def _canonical_flight_phase(legs_live):
    """Kanonische Flug-Phase (fliegt/gelandet/rollt/am Gate) für die Family-/
    Freunde-Karte — aus der Board-Beobachtung des letzten (Ankunfts-)Legs via
    warehouse_reader._status_phase_of. EINE Wahrheit, SEITEN-BEWUSST (side='arr':
    'at gate'/'on ground' am ZIEL = gelandet, nicht die grobe Substring-Erkennung).
    Nutzt die legs_live, die family_watch ohnehin schon geholt hat — kein neuer
    Netz-/SB-Call. None wenn kein/unklares Signal (Karte bleibt bei flying_now)."""
    if not legs_live:
        return None
    try:
        from blueprints.warehouse_reader import _status_phase_of
        stx = str((legs_live[-1] or {}).get('status') or '')
        return _status_phase_of(stx, 'arr')
    except Exception:
        return None


# ── Live-Positions-Fix für die „Fliegt gerade"-Karte (Owner 2026-07-06) ──────
#
# „die familie/freunde sind wichtiger als ich — ich sehe meinen flug kaum, da
# ich arbeite. aber wenn es sein muss kann man mal 1 pingen um dann zu
# berechnen und route korrigieren." — Der Hauptfall für den bezahlten Tier-3-
# Ping ist der VON FAMILIE/FREUNDEN BEOBACHTETE Flug. Wenn flying_now, holen
# wir EINEN echten Positions-Fix (freie ADS-B-Kaskade zuerst, bei Coverage-
# Lücke Tier 3 AeroDataBox purpose=watch, budget-bewacht) und liefern ihn als
# live_*-Felder — iOS korrigiert damit die Großkreis-Interpolation.
#
# Der Family-Feed ist ein FAN-OUT (N Watcher × Refresh) → ohne Memo würde
# jeder Refresh die Kaskade (und im Lücken-Fall den BEZAHLTEN Ping) erneut
# zahlen. In-Process-Memo pro (reg, datum): max. 1 Kaskaden-Lauf / 10 min,
# egal wie viele Familien-Mitglieder schauen (auch Fehlschläge werden
# memoisiert). Ehrlichkeits-Gates:
#   · Registration NUR aus echten Beobachtungen (Board-/Warehouse-Merge des
#     Legs bzw. SB-Tages-Rows via _sb_day_reg) — keine Reg → kein Fix.
#   · Aktiver Leg nur bei EINDEUTIGKEIT (genau ein Leg, oder beobachtete
#     Leg-Zeiten schließen genau einen ein) — sonst kein Paid-Ping und keine
#     Fix-Felder (die Karte bleibt rein interpoliert, kein Raten).
#   · Beobachtung ≥ 45 min alt → keine Fix-Felder.
#   · Privacy: die live_*-Felder hängen am 'next_flight'-Grant (wie flying_now).

_LIVE_FIX_MEMO = {}            # (reg, datum) → (attempt_unix, fix_dict | None)
_LIVE_FIX_MEMO_TTL = 600       # 10 min — N Watcher = max. 1 Kaskaden-Lauf/Ping
_LIVE_FIX_MEMO_MAX = 512       # simpler Cap (wie _crew_status_last_good)
_LIVE_FIX_MAX_AGE_S = 45 * 60  # ältere Beobachtung → nicht ausliefern
_KT_PER_MS = 1.0 / 0.514444    # m/s → Knoten


def _pick_active_leg(chain, legs_live, now):
    """Aktiven Leg-Index EINDEUTIG bestimmen — sonst None (kein Raten):
      · genau ein Leg → Index 0 (trivial eindeutig).
      · Mehr-Leg-Tag → nur wenn beobachtete Leg-Zeiten (esti/sched aus den
        Board-/Warehouse-Beobachtungen, ISO-parsebar) GENAU EINEN Leg
        einschließen, dessen Fenster `now` enthält.
    Unklare/fehlende/unparsebare Zeiten → None → kein Paid-Ping."""
    if not chain or len(chain) < 2:
        return None
    if len(chain) - 1 == 1:
        return 0
    cands = []
    for leg in (legs_live or []):
        st = _parse_iso(leg.get('esti_dep') or leg.get('sched_dep'))
        en = _parse_iso(leg.get('esti_arr') or leg.get('sched_arr'))
        idx = leg.get('leg_index')
        if st and en and isinstance(idx, int) and st <= now <= en:
            cands.append(idx)
    return cands[0] if len(cands) == 1 else None


_LEG_ARR_BUFFER_MIN = 40   # Verspätungs-Puffer ohne Beobachtung (Leg-Fenster)


def _day_sectors_aligned(chain, sectors):
    """Pro-Leg-Sektoren (ical_sectors[]) gegen die Tages-Kette AUSRICHTEN (PUR,
    testbar): nur wenn Anzahl UND from/to exakt zur Kette passen und jede
    dep_iso/arr_iso parsebar ist (echt-UTC, siehe _build_ical_sectors in app.py),
    liefern wir [(dep_dt, arr_dt)] (aware UTC). Sonst None → der Aufrufer bleibt
    beim alten Tages-Fenster-Verhalten (kein Raten, kein halber Datensatz)."""
    if not (chain and len(chain) >= 2 and isinstance(sectors, list)
            and len(sectors) == len(chain) - 1):
        return None
    out = []
    for idx, s in enumerate(sectors):
        if not isinstance(s, dict):
            return None
        frm = str(s.get('from') or '').strip().upper()
        to = str(s.get('to') or '').strip().upper()
        if frm != str(chain[idx]).upper() or to != str(chain[idx + 1]).upper():
            return None
        dep = _parse_iso(s.get('dep_iso'))
        arr = _parse_iso(s.get('arr_iso'))
        if not (dep and arr and dep < arr):
            return None
        out.append((dep, arr))
    return out


def _pick_current_sector(sector_times, legs_live, now,
                         buffer_min=_LEG_ARR_BUFFER_MIN):
    """EINEN kohärenten aktuellen Sektor zeitbasiert wählen (PUR, testbar) —
    Root-Fix 2026-07-10 (Tibor-iPad: „Fliegt gerade BCN→FRA · Ankunft 15:20"
    war die ROUTE des ersten Legs mit der ANKUNFT des letzten; der zeitlich
    aktuelle Leg FRA→ARN fehlte komplett).
      · 'inflight' idx: dep ≤ now < arr_eff — arr_eff bevorzugt die BEOBACHTETE
        Verspätung (arr_delay_min/delay_min des Legs), sonst Plan; ohne
        Beobachtung toleriert ein 2. Pass +buffer_min (Delay-Puffer). Eine
        beobachtete LANDUNG beendet den Leg sofort (Board schlägt Uhr).
      · 'pre' idx: vor dem nächsten kommenden Leg (wartet/boarding).
      · 'done' idx: alle Legs (+Puffer) vorbei → gelandet am Tagesziel.
    Alle Zeiten aware-UTC (dep_iso/arr_iso sind echt-UTC, now = UTC).
    → (state, idx, dep_dt, arr_dt, arr_est_dt|None) | None."""
    if not sector_times:
        return None
    obs_by_idx = {}
    for l in (legs_live or []):
        if isinstance(l, dict) and isinstance(l.get('leg_index'), int):
            obs_by_idx[l['leg_index']] = l
    legs = []
    for idx, (dep, arr) in enumerate(sector_times):
        o = obs_by_idx.get(idx) or {}
        d = o.get('arr_delay_min')
        if not isinstance(d, (int, float)):
            d = o.get('delay_min')
        arr_est = (arr + _dt.timedelta(minutes=float(d))
                   if isinstance(d, (int, float)) and d > 0 else None)
        stx = str(o.get('status') or '').lower()
        landed = any(k in stx for k in ('landed', 'gelandet',
                                        'arrived', 'angekommen'))
        legs.append((dep, arr, arr_est, landed))
    buf = _dt.timedelta(minutes=float(buffer_min))
    # Pass 1: hartes Fenster (est bevorzugt), beobachtete Landung beendet sofort.
    for idx, (dep, arr, arr_est, landed) in enumerate(legs):
        if not landed and dep <= now < (arr_est or arr):
            return ('inflight', idx, dep, arr, arr_est)
    # Pass 2: Verspätungs-Puffer NUR ohne beobachtete Landung.
    for idx, (dep, arr, arr_est, landed) in enumerate(legs):
        if not landed and dep <= now < (arr_est or arr) + buf:
            return ('inflight', idx, dep, arr, arr_est)
    # Kein Leg aktiv → der nächste kommende („wartet"), sonst alles geflogen.
    for idx, (dep, arr, arr_est, _landed) in enumerate(legs):
        if now < dep:
            return ('pre', idx, dep, arr, arr_est)
    dep, arr, arr_est, _landed = legs[-1]
    return ('done', len(legs) - 1, dep, arr, arr_est)


def _snapshot_day_sectors(crew_token, datum_s):
    """ical_sectors[] des Tages aus dem Roster-Snapshot (gleiche Quelle wie
    _live_legs_for die Flugnummern zieht). None = ehrlich keine."""
    try:
        snap_read = _app_attr('_roster_snapshot_read')
        if not callable(snap_read):
            return None
        tage = (snap_read(crew_token) or {}).get('tage') or []
        dday = next((t for t in tage if isinstance(t, dict)
                     and t.get('datum') == datum_s), {})
        secs = dday.get('ical_sectors')
        return secs if isinstance(secs, list) and secs else None
    except Exception:
        return None


def _reg_for_leg(leg_obs, flight_no, datum):
    """Registration NUR aus echten Beobachtungen: erst die Board-/Warehouse-
    Beobachtung des Legs selbst (reg im Dual-Side-Merge), sonst die SB-Tages-
    Rows des EXAKTEN Datums (airport_delay_obs via _sb_day_reg — der gleiche
    Lookup wie /api/ax/flight-info). None = ehrlich keine → kein Fix."""
    reg = str((leg_obs or {}).get('reg') or '').strip().upper() or None
    if reg:
        return reg
    if not flight_no:
        return None
    try:
        from blueprints.aerox_data_blueprint import _sb_day_reg
        sb_reg, _tc, _dep, _arr = _sb_day_reg(flight_no, datum)
        return str(sb_reg or '').strip().upper() or None
    except Exception:
        return None


def _live_fix_for_reg(reg, datum):
    """Memoisierter Positions-Fix pro (reg, datum) — FAMILY = NUR TABELLEN
    (Owner 2026-07-06: „familie könnte sogar kostenlos bleiben, er muss halt
    nur richtig sein mit abflug und ankunft"): geht über denselben Resolver wie
    Freunde/eigen (resolve_position_for_watch → position_for_flight), aber mit
    allow_paid=False ⇒ targeted=False ⇒ liest NUR die Tabellen (fr24_live +
    aircraft_positions, frischeste echte Position gewinnt), NIE ein bezahlter
    Ping und keine externen Mirror-Fetches. Korrekt bleibt die Karte trotzdem,
    weil Abflug/Ankunft delay-korrigiert aus den Gratis-Board-Beobachtungen
    kommen; im Coverage-Loch zeigt sie die verankerte Interpolation. Der
    bezahlte purpose=watch-Tier bleibt den FREUNDE-Karten vorbehalten.
    EIN Kaskaden-Lauf alle 10 Minuten, egal wie viele Watcher anfragen. Auch
    Fehlschläge werden memoisiert (sonst würde jeder Feed-Refresh erneut
    kaskadieren).
    Der Slot wird VOR dem Lauf reserviert — parallele Watcher sehen für diesen
    Refresh None statt einen zweiten Ping auszulösen.
    → {'lat','lon','track','speed_kt','ts','source'} | None. Das 45-min-
    Frische-Gate macht der Caller (_flying_live_fix) zur Serve-Zeit."""
    key = (str(reg).upper(), str(datum))
    now = time.time()
    hit = _LIVE_FIX_MEMO.get(key)
    if hit and (now - hit[0]) < _LIVE_FIX_MEMO_TTL:
        return hit[1]
    if len(_LIVE_FIX_MEMO) >= _LIVE_FIX_MEMO_MAX:
        _LIVE_FIX_MEMO.clear()
    _LIVE_FIX_MEMO[key] = (now, None)   # Slot reservieren (Fan-out-Race)
    fix = None
    try:
        from blueprints.adsb_blueprint import resolve_position_for_watch
        row, source, fetch_ts = resolve_position_for_watch(reg=key[0],
                                                           allow_paid=False)
        if row is not None and len(row) >= 11:
            lat = row[6]
            lon = row[5]
            if lat is not None and lon is not None:
                # ECHTER Beobachtungszeitpunkt: time_position → last_contact →
                # Resolver-obs_ts (row[3]=echter Fix-Zeitstempel aus fr24_live/
                # aircraft_positions; NIE „jetzt" für einen alten Fix).
                ts = None
                for cand in (row[3], row[4], fetch_ts):
                    try:
                        ts = float(cand)
                        break
                    except (TypeError, ValueError):
                        continue
                # Höhe für das Kinematik-Gate: Row-Layout [7]=baro_altitude_m,
                # [13]=geo_altitude_m (Meter!) → alt_ft. Ohne sie verwarf das
                # Gate jeden Fix, dessen Quelle keine Ground-Speed trägt.
                alt_m = row[7] if row[7] is not None else (
                    row[13] if len(row) > 13 else None)
                fix = {
                    'lat': float(lat),
                    'lon': float(lon),
                    'alt_ft': (round(float(alt_m) / 0.3048)
                               if alt_m is not None else None),
                    'track': (float(row[10]) if row[10] is not None else None),
                    'speed_kt': (round(float(row[9]) * _KT_PER_MS, 1)
                                 if row[9] is not None else None),
                    'ts': ts,
                    'source': source,
                }
    except Exception as e:
        _log().info(f'[family-watch] live_fix_skip {type(e).__name__}')
        fix = None
    _LIVE_FIX_MEMO[key] = (now, fix)
    return fix


def _flying_live_fix(chain, datum, fns, legs_live, now=None, forced_idx=None):
    """Orchestrierung für die Status-Felder: aktiven Leg eindeutig wählen →
    Reg aus echten Beobachtungen → memoisierten Fix holen → Frische-Gate.
    None bei JEDEM Zweifel — die Karte funktioniert dann rein interpoliert
    weiter, nichts wird geraten und nichts bezahlt.
    forced_idx (2026-07-10): hat der Loader den aktuellen Leg bereits kohärent
    aus den Roster-Sektoren gewählt, gilt GENAU dieser — Fix-Route und
    Karten-Route stammen dann garantiert aus demselben Leg."""
    try:
        if not (chain and len(chain) >= 2 and datum
                and fns and len(fns) == len(chain) - 1):
            return None
        now_dt = now if now is not None else _dt.datetime.now(_dt.timezone.utc)
        idx = (forced_idx if isinstance(forced_idx, int)
               else _pick_active_leg(chain, legs_live, now_dt))
        if idx is None or not (0 <= idx < len(fns)):
            return None
        leg_obs = next((l for l in (legs_live or [])
                        if l.get('leg_index') == idx), None)
        reg = _reg_for_leg(leg_obs, fns[idx], datum)
        if not reg:
            return None
        fix = _live_fix_for_reg(reg, datum)
        if not fix or not isinstance(fix.get('ts'), (int, float)):
            return None
        if (time.time() - fix['ts']) > _LIVE_FIX_MAX_AGE_S:
            return None   # Beobachtung zu alt — ehrlich keine Live-Position
        # Aktives Leg (Callsign + Reg + dep/arr) anhängen → erlaubt dem Aufrufer
        # den Live-Routen-Confirm DIESES Legs mit der Live-Position. Die Reg ist
        # wichtig: der Roster trägt die IATA-Flugnummer (LH716), route_for_flight
        # matcht Cache/Warehouse/aircraft_live aber über ATC-Funknamen (DLH716)
        # ODER die Reg — ohne Reg wäre der Confirm praktisch immer ein Miss.
        fix = dict(fix)
        fix['callsign'] = fns[idx]
        fix['reg'] = reg
        fix['leg_dep'] = chain[idx]
        fix['leg_arr'] = chain[idx + 1]
        return fix
    except Exception as e:
        _log().info(f'[family-watch] live_fix_skip {type(e).__name__}')
        return None


def _load_crew_status_for_family(crew_token, allowed_fields):
    """Liest aus dem Crew-Profile + briefing-state nur die erlaubten Felder.
    Returns dict mit den status-feldern fuer die WatchedCrew.CrewStatus
    iOS struct (alle felder Optional)."""
    if not crew_token:
        return {}
    # LOCATION-WIE-FREUNDE (Owner 2026-07-07: „funktioniert doch wie Freunde bei
    # der Location?"): bei Freunden hängt die Standort-Freigabe an EINEM Profil-
    # Flag (`share_location`, Default AN) — es gibt keinen separaten Feld-Grant.
    # Bei Family lief bisher ALLES über den per-Feld-Grant, sodass die Family den
    # Ort NUR sah, wenn der Crew `current_city`/`layover_place` explizit gegranted
    # hatte → „Papa weiß nie wo ich bin", obwohl Freunde denselben Ort sehen.
    # Angleichung: die ORTS-Felder gelten als freigegeben, sobald der Crew
    # `share_location` nicht explizit ausgeschaltet hat — genau wie bei Freunden.
    # Sensible Felder (Fotos/Voice/Reg) bleiben weiter am expliziten Family-Grant.
    allowed_fields = set(allowed_fields)
    try:
        if (_load_crew_profile(crew_token) or {}).get('share_location') is not False:
            allowed_fields |= {'current_city', 'layover_place'}
    except Exception:
        pass
    src_fail = False   # mind. eine SB-/Profil-Quelle war trotz Retry unlesbar
    status = {
        'layover_place': None,
        'layover_place_city': None,   # "San Francisco" statt "SFO" (2026-07-03)
        'current_city': None,
        'landed': None,
        'next_flight_no': None,
        'next_flight_dep_iata': None,
        'next_flight_arr_iata': None,
        'next_flight_dep_city': None,
        'next_flight_arr_city': None,
        'next_flight_etd_iso': None,
        'photo_count_today': None,
        'last_seen_iso': None,
        # „Fliegt gerade"-Block (User 2026-06-25): heute aktiver Flugtag → die
        # Family sieht ein Radar-Widget mit interpoliertem Flieger statt „In <Abflug>".
        # iOS rechnet Position/Animation selbst aus dep/arr-IATA + den Zeiten.
        'flying_now': None,
        # Kanonische Flug-Phase aus DER Status-Quelle (status_for_flight-Logik):
        # 'airborne'|'landed'|'grounded'|'cancelled'|None → iOS zeigt „Fliegt
        # gerade / Gelandet / Am Gate / rollt" statt selbst zu raten.
        'flight_phase': None,
        'today_dep_iata': None,
        'today_arr_iata': None,
        'today_dep_city': None,
        'today_arr_city': None,
        # Fertiges Tour-Label des heutigen Tages mit Städtenamen
        # ("Frankfurt – San Francisco") — iOS soll NIE selbst aus rohen
        # IATA-Ketten einen Label bauen (2026-07-03).
        'today_route_label': None,
        'today_dep_iso': None,
        'today_arr_iso': None,
        # ECHTER Positions-Fix während flying_now (Owner 2026-07-06, „wenn es
        # sein muss kann man mal 1 pingen"): korrigiert die iOS-Interpolation.
        # None = kein Fix verfügbar → Karte bleibt rein interpoliert.
        'live_lat': None,
        'live_lon': None,
        'live_track': None,
        'live_speed_kt': None,
        'live_ts_iso': None,      # ECHTER Beobachtungszeitpunkt (UTC-Z)
        'live_source': None,      # 'fr24' | 'aircraft_positions' (Family: nur Tabellen)
        # Zuhause/Feierabend: heute an der Homebase (reiner Heimtag) ODER nach
        # Landung an der Homebase (Dienst vorbei) — die Card zeigt „Feierabend"
        # statt eines falschen Layovers (User 2026-06-25).
        'home_now': None,
        # ADDITIV (Neubau 2026-07-10): EINE Wahrheit — expliziter Live-Zustand
        # inkl. SERVERSEITIGEM Text aus blueprints/crew_live_state (gleicher
        # Resolver wie friends-today). iOS zeigt crew_state.text 1:1.
        'crew_state': None,
    }
    prof = {}   # vor-initialisiert: wirft der try-Block, darf Z. „hb = prof.get“ nicht NameError-n
    try:
        # SB-primary statt Disk: auf Cloud Run ist die Profil-Disk-Datei ephemer/
        # leer → die Family-Watcher sahen weder current_city noch last_seen
        # („selbst wenn verbunden, sieht sie nichts vom Plan"). _load_crew_profile
        # + _profile_load lesen Supabase-first.
        prof = _load_crew_profile(crew_token) or {}
        if 'current_city' in allowed_fields:
            status['current_city'] = prof.get('current_city')
        # #36: landed/photo_count aus dem bereits geladenen Crew-Profil wiren —
        # vorher blieben beide selbst bei Freigabe IMMER None. Privacy-gegated:
        # nur setzen wenn das jeweilige Feld in allowed_fields ist.
        if 'landed_status' in allowed_fields:
            status['landed'] = prof.get('landed')
        if 'photos' in allowed_fields:
            # Kein dedizierter Foto-Zähler-Quell-Tisch vorhanden; wir wiren nur
            # einen ggf. existierenden Profilschlüssel. Fehlt er → bleibt None
            # (keine erfundenen Daten).
            status['photo_count_today'] = prof.get('photo_count_today')
        # last_seen_iso ist selbst eine Aktivitäts-Info ("zuletzt online/aktiv")
        # und darf NUR geleakt werden wenn der Crew ein relevantes Live-Feld
        # freigegeben hat. Vorher wurde es bedingungslos gesetzt (ausserhalb des
        # allowed_fields-Gates) → Family sah Aktivität auch ohne jede Freigabe.
        _ls_allowed = bool(allowed_fields & {'current_city', 'last_seen', 'landed_status', 'next_flight'})
        if _ls_allowed:
            _pl = _app_attr('_profile_load')
            if callable(_pl):
                full = _pl(crew_token) or {}
                # _updated_at kommt je nach Pfad als SB-timestamptz (Mikro-
                # sekunden + Offset) oder Disk-isoformat (naiv) → kanonisch
                # UTC-Z ausgeben, iOS soll nie Format-Raten müssen.
                status['last_seen_iso'] = _iso_utc_z(full.get('_updated_at'))
    except Exception as e:
        src_fail = True
        _log().info(f'[family-watch] profile_read_skip {type(e).__name__}')
    # next_flight: nur wenn 'next_flight' in allowed_fields. Best-effort read aus
    # briefings/roster state via SB. Wenn nicht ladbar → bleibt None.
    sb_avail, sb = _get_sb()
    # FIX (Bug-Hunt #3): die Tabelle 'briefings' EXISTIERT NICHT — die echten
    # Briefing-Daten liegen in user_ical_briefings (ical_summary „FRA-JFK 10:30-
    # 13:20, …", ical_location „JFK, FRA-JFK-FRA", ical_start). Vorher las dieser
    # Code eine Phantom-Tabelle → next_flight/layover blieben IMMER leer, auch
    # wenn die Crew die Felder freigegeben hatte.
    if 'next_flight' in allowed_fields and sb_avail and sb is not None:
        try:
            today = _dt.datetime.now().date().isoformat()
            # FIX „FRA-HND obwohl die Crew auf SFO-Tour ist" (2026-07-03):
            # vorher limit(1) → wenn der NÄCHSTGELEGENE Roster-Tag KEIN Leg-Paar
            # im Summary hat (Layover-Ruhetag „LAYOVER SFO", OFF) blieb
            # next_flight leer — und wenn der heutige Tag GANZ fehlte, gewann
            # der erste ZUKÜNFTIGE Tag, gerne der Start der NÄCHSTEN Tour
            # (FRA-HND), obwohl der Rückflug SFO-FRA der Wahrheit entspricht.
            # Jetzt: die nächsten Tage scannen und das ERSTE echte Leg-Paar
            # nehmen (datum-aufsteigend → der Rückflug schlägt die nächste Tour).
            r = (sb.table('user_ical_briefings')
                 .select('datum,ical_summary,ical_location,ical_start')
                 .eq('token', crew_token)
                 .gte('datum', today)
                 .order('datum', desc=False)
                 .limit(10).execute())
            for br in (r.data or []):
                summ = br.get('ical_summary') or ''
                mleg = re.search(r'\b([A-Z]{3})-([A-Z]{3})\b', summ)
                if not mleg:
                    continue
                status['next_flight_dep_iata'] = mleg.group(1)
                status['next_flight_arr_iata'] = mleg.group(2)
                status['next_flight_dep_city'] = _iata_city_name(mleg.group(1))
                status['next_flight_arr_city'] = _iata_city_name(mleg.group(2))
                st = br.get('ical_start')
                if st:
                    status['next_flight_etd_iso'] = _iso_utc_z(st)
                break
        except Exception as e:
            src_fail = True
            _log().info(f'[family-watch] briefing_read_skip {type(e).__name__}')
    # Roster-derived Layover-Stadt + Reconcile von current_city.
    #
    # THEME-B FIX (Family-Watch zeigt falsche Stadt): `current_city` (oben, Z.389)
    # ist NUR ein vom iOS-LocationStore gepushter, reverse-geocodeter GPS-String
    # (POST /api/user/location, ~1×/h) bzw. ein PUT-Wert. Er wird vom Roster-/
    # Briefing-Import NIE aktualisiert oder gelöscht → ein alter GPS-Sample
    # („München", früher „San Francisco") friert ein und widerspricht dem echten
    # Plan (z.B. OPO-FRA). Der Roster (user_ical_briefings.ical_location) ist die
    # zuverlässige, plan-verankerte Quelle. Die iOS-Family-Card bevorzugt ohnehin
    # `layover_place ?? current_city` (CrewStatusBigCard.swift) — wir machen die
    # layover_place-Ableitung robuster und unterdrücken eine stale current_city,
    # die dem Roster widerspricht.
    hb = (prof.get('homebase') or prof.get('home_base') or '').strip().upper()
    roster_layover = None   # IATA des heutigen Layover-Orts (≠ Homebase), wenn ermittelbar
    roster_today_home = False  # heutiger Roster-Tag liegt POSITIV an der Homebase
    flying_now = False          # NOW im aktiven Flug-/Dienst-Fenster → „Fliegt gerade"
    today_dep = today_arr = None
    today_dep_iso = today_arr_iso = None
    today_arr_est_iso = None    # Dienst-Ende + beobachtete Verspätung (nur echte Obs)
    today_chain = None          # volle IATA-Kette des aktiven Tags (Städte-Label)
    legs_live_cached = None     # Board-/Warehouse-Beobachtungen je Leg (einmal geholt)
    flight_phase = None         # kanonische Phase (fliegt/gelandet/rollt/am Gate)
    active_datum = None         # datum des AKTIVEN Flugtags (Red-Eye: ggf. gestern)
    day_fns = {}                # datum → 1:1 gemappte Flugnummern (aus _live_legs_for)
    leg_picked = False          # EIN kohärenter aktueller Leg gewählt (2026-07-10)
    active_leg_idx = None       # dessen Index — NUR wenn er JETZT fliegt (Live-Fix)
    current_leg_obs = None      # Board-/Warehouse-Beobachtung DIESES Legs

    # ROOT-FIX 2026-07-06 (Owner: „Family hat weder die Verspätung noch die
    # Landung bemerkt") — der alte Block hatte drei strukturelle Fehler:
    #  1) ical_location trägt die TOUR-Kette auch an reinen LAYOVER-Tagen
    #     („HND, FRA-HND" am Ruhetag in Tokio) → der GANZE Tag (00:00–23:5x)
    #     galt als Flug-Fenster → „Fliegt gerade" den kompletten Layover-Tag,
    #     „Ankunft 23:58" war schlicht das Tagesende. Ein Ganztags-Fenster
    #     (≥20h) ist jetzt KEIN Dienst-Fenster mehr.
    #  2) Das Fenster war das starre Dienst-Fenster ohne Verspätungs-Wissen —
    #     jetzt verlängert eine ECHT BEOBACHTETE Verspätung (Board/Warehouse,
    #     free-only) das Fenster, und eine beobachtete Landung beendet es.
    #     Kein Raten: ohne Beobachtung gilt weiter der Plan.
    #  3) Red-Eye über UTC-Mitternacht: um 00:00 UTC wurde „heute" der
    #     Folgetag und der noch LAUFENDE Flug von gestern unsichtbar → der
    #     Vortag wird mitgelesen und gewinnt, solange sein Fenster läuft.
    def _live_legs_for(chain, datum_s):
        """Echte Beobachtungen je Leg (Dual-Side-Resolver, free-only, memoisiert).
        Flugnummern aus dem Roster-Snapshot; nur bei sauberer 1:1-Zuordnung zur
        Kette (ehrlich, kein Raten). None = keine Beobachtung."""
        try:
            resolver = _app_attr('_flight_obs_merged')
            snap_read = _app_attr('_roster_snapshot_read')
            if not (callable(resolver) and callable(snap_read)
                    and chain and len(chain) >= 2):
                return None
            tage = (snap_read(crew_token) or {}).get('tage') or []
            dday = next((t for t in tage if isinstance(t, dict)
                         and t.get('datum') == datum_s), {})
            fns = [str(f).replace(' ', '').upper()
                   for f in ((dday.get('reader_facts') or {})
                             .get('flight_numbers') or [])
                   if str(f or '').strip()]
            if not fns or len(fns) != len(chain) - 1:
                return None
            day_fns[datum_s] = fns   # für den Live-Positions-Fix (Reg-Lookup)
            out = []
            for idx, fno in enumerate(fns[:4]):
                m = resolver(fno, date=datum_s, dep_iata=chain[idx],
                             arr_iata=chain[idx + 1], free_only=True)
                if m:
                    out.append({
                        'flight': fno,
                        # leg_index/reg/Zeiten (ADDITIV 2026-07-06): eindeutige
                        # Aktiv-Leg-Wahl + Reg-Auflösung für den Positions-Fix.
                        'leg_index': idx,
                        'dep_iata': chain[idx],
                        'arr_iata': chain[idx + 1],
                        'dep_delay_min': m.get('dep_delay_min'),
                        'arr_delay_min': m.get('arr_delay_min'),
                        'delay_min': m.get('delay_min'),
                        'delay_side': m.get('delay_side'),
                        'status': m.get('status'),
                        'cancelled': m.get('cancelled'),
                        'sides': m.get('sides'),
                        'reg': m.get('reg'),
                        'sched_dep': m.get('sched_dep'),
                        'esti_dep': m.get('esti_dep'),
                        'sched_arr': m.get('sched_arr'),
                        'esti_arr': m.get('esti_arr'),
                    })
            return out or None
        except Exception as e:
            _log().info(f'[family-watch] live_enrich_skip {type(e).__name__}')
            return None

    def _window_state(day, now):
        """(fliegt_jetzt, effektives_ende, legs_live, landung_beobachtet) —
        holt die Beobachtungen und wertet das Fenster pur aus."""
        legs_live = _live_legs_for(day['chain'], day['datum'])
        flying, en_eff, landed_obs = _flight_window_state(day, legs_live, now)
        return flying, en_eff, legs_live, landed_obs

    def _current_leg_overlay(day, legs_live, now):
        """EIN kohärenter aktueller Leg des Tages (Root-Fix 2026-07-10):
        Sektoren aus dem Roster (SB raw_event, sonst Snapshot) gegen die Kette
        ausrichten und zeitbasiert wählen. None → alter Tages-Fenster-Pfad.
        → (state, idx, dep_iso_z, arr_iso_z, arr_est_iso_z|None, leg_obs|None)"""
        secs = day.get('sectors') or _snapshot_day_sectors(crew_token,
                                                           day.get('datum'))
        times = _day_sectors_aligned(day.get('chain'), secs)
        if not times:
            return None
        cur = _pick_current_sector(times, legs_live, now)
        if not cur:
            return None
        state, idx, dep_dt, arr_dt, arr_est_dt = cur

        def _z(d):
            return d.strftime('%Y-%m-%dT%H:%M:%SZ') if d else None

        leg_obs = next((l for l in (legs_live or [])
                        if isinstance(l, dict) and l.get('leg_index') == idx),
                       None)
        return state, idx, _z(dep_dt), _z(arr_dt), _z(arr_est_dt), leg_obs

    if sb_avail and sb is not None:
        # ical_location-Format: „JFK, FRA-JFK-FRA". Erstes Token = Aufenthaltsort
        # des Tages. An einem HOMEBASE-Tag (Tag-Trip FRA-LUX-FRA → erstes Token =
        # FRA) ist man NICHT im Layover, sondern zuhause → nicht als Layover werten.
        try:
            now = _dt.datetime.now(_dt.timezone.utc)
            today_d = _dt.datetime.now().date()
            days = [today_d.isoformat(),
                    (today_d - _dt.timedelta(days=1)).isoformat()]
            # raw_event trägt die Pro-Leg-Sektoren (ical_sectors, echt-UTC-
            # Zeiten je Leg) — Grundlage der kohärenten Aktuell-Leg-Wahl.
            r = (sb.table('user_ical_briefings')
                 .select('datum,ical_location,ical_summary,ical_start,ical_end,'
                         'raw_event')
                 .eq('token', crew_token).in_('datum', days).execute())
            by_date = {rw.get('datum'): rw for rw in (r.data or [])}
            prim = _parse_roster_day(by_date[days[0]]) if by_date.get(days[0]) else None
            prev = _parse_roster_day(by_date[days[1]]) if by_date.get(days[1]) else None

            # Aktives Flug-Fenster: heutiger Flugtag zuerst, sonst der gestrige
            # (Red-Eye über UTC-Mitternacht, Fix 3). Zustände nur je Bedarf rechnen.
            prim_state = _window_state(prim, now) if (prim and prim['is_flight']) else None
            active_day, active_state = None, None
            if prim_state and prim_state[0]:
                active_day, active_state = prim, prim_state
            elif prev and prev['is_flight'] and not (prim_state and prim_state[0]):
                pst = _window_state(prev, now)
                if pst[0]:
                    active_day, active_state = prev, pst

            if active_day is not None:
                flying_now = True
                _, en_eff, legs_live_cached, _ = active_state
                active_datum = active_day['datum']
                today_chain = list(active_day['chain'])
                today_dep, today_arr = today_chain[0], today_chain[-1]
                today_dep_iso = active_day['st_iso']
                today_arr_iso = active_day['en_iso']
                # Effektive Ankunft (Plan + beobachtete Verspätung) nur wenn sie
                # vom Plan abweicht — iOS zeigt dann „Ankunft ~HH:mm · +N min".
                if en_eff and active_day['en'] and en_eff != active_day['en']:
                    today_arr_est_iso = en_eff.isoformat().replace('+00:00', 'Z')
                # ROOT-FIX 2026-07-10 (Tibor-iPad „Fliegt gerade BCN→FRA ·
                # Ankunft 15:20" bei BCN-FRA-ARN-FRA): oben ist ein FELD-MIX aus
                # zwei Legs — Route = Ketten-Enden (erster Abflug/letzte
                # Station), Ankunft = DIENST-Ende (= Ankunft des LETZTEN Legs);
                # der zeitlich AKTUELLE Leg (FRA→ARN) fehlte komplett. Mit
                # Pro-Leg-Sektoren kommen Route, Zeiten UND Live-Fix jetzt alle
                # aus EINEM zeitbasiert gewählten Leg; zwischen den Legs ist die
                # Crew ehrlich am Boden („wartet" auf den nächsten Leg), nach
                # dem letzten Leg gelandet. Ohne Sektoren: altes Verhalten.
                ov = _current_leg_overlay(active_day, legs_live_cached, now)
                if ov:
                    lstate, lidx, dep_z, arr_z, arr_est_z, current_leg_obs = ov
                    leg_picked = True
                    today_dep = today_chain[lidx]
                    today_arr = today_chain[lidx + 1]
                    today_dep_iso, today_arr_iso = dep_z, arr_z
                    today_arr_est_iso = arr_est_z
                    if lstate == 'inflight':
                        active_leg_idx = lidx
                    else:
                        flying_now = False
                        if lstate == 'done' and today_arr:
                            # Alle Legs (+Puffer) geflogen → am Tagesziel, auch
                            # wenn das Dienst-Fenster (Debriefing) noch läuft.
                            if today_arr == hb and hb:
                                roster_today_home = True
                            else:
                                roster_layover = today_arr
            elif prim and prim['is_flight']:
                # Heutiger Flugtag, aber Fenster läuft (noch) nicht: Felder für
                # die Vorschau setzen; nach dem (ggf. verspäteten/beobachteten)
                # Ende ist die Crew am ZIEL — Homebase-Ziel = zuhause (Fix 2).
                _, en_eff, legs_live_cached, landed_obs = prim_state
                today_chain = list(prim['chain'])
                today_dep, today_arr = today_chain[0], today_chain[-1]
                today_dep_iso = prim['st_iso']
                today_arr_iso = prim['en_iso']
                # Kohärenter Leg auch für die Vorschau/Nachlauf-Karte (sonst
                # zeigt „wartet auf den ersten Leg" die Ankunft des LETZTEN).
                ov = _current_leg_overlay(prim, legs_live_cached, now)
                if ov:
                    _lstate, lidx, dep_z, arr_z, arr_est_z, current_leg_obs = ov
                    leg_picked = True
                    today_dep = today_chain[lidx]
                    today_arr = today_chain[lidx + 1]
                    today_dep_iso, today_arr_iso = dep_z, arr_z
                    today_arr_est_iso = arr_est_z
                ended = (en_eff and now > en_eff) or landed_obs
                if ended and today_chain[-1]:
                    # Tagesziel = letzte Station der KETTE (nicht der ggf. auf
                    # einen Zwischen-Leg gestellte today_arr).
                    if today_chain[-1] == hb and hb:
                        roster_today_home = True
                    else:
                        roster_layover = today_chain[-1]
            elif prim and len(prim['first']) == 3 and prim['first'].isalpha():
                # Kein Flugtag → erstes Token ist der echte Aufenthaltsort.
                if prim['first'] == hb and hb:
                    roster_today_home = True
                elif prim['first'] != hb:
                    roster_layover = prim['first']
            elif prim is None and prev and prev['is_flight'] and prev['chain']:
                # Kein heutiger Roster-Eintrag, gestriger Flug ist vorbei →
                # die Crew ist am gestrigen Ziel (Red-Eye-Ankunft ohne Folge-Row).
                dest = prev['chain'][-1]
                if dest == hb and hb:
                    roster_today_home = True
                elif dest:
                    roster_layover = dest
        except Exception as e:
            # Vorher stumm (bare pass) — DER Zweig produzierte bei SB-Flakes
            # den All-None-„Status unbekannt" ohne jede Log-Spur.
            src_fail = True
            _log().info(f'[family-watch] roster_read_skip {type(e).__name__}')
    # Kanonische Phase aus der Board-Beobachtung (Owner: „Family/Freunde sollen
    # gelandet/rollt/am Gate aus DER Quelle zeigen, nicht selbst raten") — dieselbe
    # status_for_flight-Logik wie überall, seiten-bewusst. Nur wenn Obs vorliegt.
    # Kohärenz-Fix 2026-07-10: ist EIN aktueller Leg gewählt, kommt die Phase
    # aus DESSEN Beobachtung — nicht (wie vorher) aus dem LETZTEN Leg des Tages
    # (der bei Mehr-Leg-Tagen noch gar nicht geflogen ist). Ohne Obs des
    # gewählten Legs ehrlich None statt Fremd-Leg-Phase.
    if leg_picked:
        flight_phase = (_canonical_flight_phase([current_leg_obs])
                        if current_leg_obs else None)
    else:
        flight_phase = _canonical_flight_phase(legs_live_cached)
    if 'next_flight' in allowed_fields:
        status['flying_now'] = flying_now
        status['flight_phase'] = flight_phase   # 'airborne'|'landed'|'grounded'|'cancelled'|None
        status['today_dep_iata'] = today_dep
        status['today_arr_iata'] = today_arr
        status['today_dep_city'] = _iata_city_name(today_dep) if today_dep else None
        status['today_arr_city'] = _iata_city_name(today_arr) if today_arr else None
        # Fertiges Städte-Label der heutigen Tour: aus der VOLLEN Kette
        # (FRA-SFO-FRA → "Frankfurt – San Francisco", Ziel = Layover/entfern-
        # tester Punkt), nicht aus dep/arr (die wären bei Rundreisen FRA/FRA).
        if today_chain:
            status['today_route_label'] = _route_label_cities(
                '-'.join(today_chain), roster_layover)
        elif roster_layover and 'layover_place' in allowed_fields:
            # Layover-Stadt nur, wenn der layover_place-Grant sie erlaubt —
            # sonst leakt der next_flight-Grant die Stadt am Ruhetag.
            status['today_route_label'] = _iata_city_name(roster_layover)
        status['today_dep_iso'] = today_dep_iso
        status['today_arr_iso'] = today_arr_iso
        # LIVE-DELAY der heutigen Legs (Owner-Direktive 2026-07-03: „alle
        # Live-Sachen anbinden") — zentraler Dual-Side-Resolver in app.py
        # (Board+Warehouse am Abflugs- UND Ankunftsort, free_only = kein
        # bezahlter API-Spend im Family-Feed-Fan-out, memoisiert). Flugnummern
        # kommen aus dem Roster-Snapshot (reader_facts.flight_numbers, gleiche
        # Quelle wie /friends-today) und werden nur genutzt, wenn sie sich
        # sauber den Legs der heutigen Kette zuordnen lassen (ehrlich, kein
        # Raten). Rein ADDITIV: today_flights_live (Liste) + today_delay_min
        # (letztes Leg mit Daten = „kommt sie/er pünktlich an?"). None = keine
        # Beobachtung → iOS zeigt wie bisher.
        # Die Beobachtungen wurden bereits OBEN für das Flug-Fenster geholt
        # (_window_state → _live_legs_for) — hier nur noch durchreichen, kein
        # zweiter Resolver-Lauf.
        status['today_flights_live'] = legs_live_cached or None
        # Delay-Anzeige gehört zum GEWÄHLTEN Leg (Kohärenz 2026-07-10) — sonst
        # trägt die FRA→ARN-Karte die Verspätung des ARN→FRA-Rückflugs.
        if leg_picked:
            status['today_delay_min'] = ((current_leg_obs or {}).get('delay_min')
                                         if current_leg_obs else None)
        else:
            status['today_delay_min'] = (legs_live_cached[-1].get('delay_min')
                                         if legs_live_cached else None)
        # Plan-Ende + beobachtete Verspätung (None wenn keine Abweichung) —
        # iOS zeigt damit die korrigierte Ankunft statt der Plan-Zeit.
        status['today_arr_est_iso'] = today_arr_est_iso
        # ECHTER Positions-Fix (Owner 2026-07-06): nur während flying_now, nur
        # unter dem next_flight-Grant (wie flying_now selbst). Ehrlichkeits-
        # Gates + (reg, datum)-Memo (10 min, Fan-out-sicher) + freie Kaskade
        # vor Tier-3-purpose=watch: siehe _flying_live_fix. Bei None bleibt
        # die Karte rein interpoliert — kein Raten, keine Felder.
        if flying_now and today_chain and active_datum:
            # forced_idx (2026-07-10): der Live-Fix nutzt DENSELBEN zeitbasiert
            # gewählten Leg wie die Karten-Felder — kein Feld-Mix zwischen
            # Fix-Route und angezeigter Route.
            _fix = _flying_live_fix(today_chain, active_datum,
                                    day_fns.get(active_datum),
                                    legs_live_cached,
                                    forced_idx=active_leg_idx)
            # FLIGHTSTATE-Gate (Kill-Switch FLIGHTSTATE_LIVE_FAMILY=1): der Fix
            # läuft durch die ENGINE (statt nur durchs rohe Kinematik-Gate) —
            # nur eine WIRKLICH fliegende Position an die Family (Taxi/Boden ⇒
            # kein Geister-Dot), die PHASE kommt aus der Engine (P1-4e) und ein
            # ehrliches live_status='lost' (P1-4c, additiv) erlaubt iOS, die
            # Großkreis-Simulation zu stoppen. Flag bleibt default AUS.
            if _fix and os.environ.get('FLIGHTSTATE_LIVE_FAMILY', '') in ('1', 'true', 'yes'):
                try:
                    from blueprints.flight_state_collectors import (
                        build_keys as _fs_bk, obs_from_pos as _fs_op)
                    from blueprints.flight_state import resolve_flight_state as _fs_resolve
                    try:
                        from blueprints.aerox_data_blueprint import _iata_latlon as _fs_ll
                    except Exception:
                        def _fs_ll(_c):
                            return None
                    _fs_keys = _fs_bk(
                        _fix.get('callsign'), active_datum,
                        _fix.get('leg_dep'), _fix.get('leg_arr'),
                        roster_tail=_fix.get('reg'),
                        dep_ll=_fs_ll(_fix.get('leg_dep') or ''),
                        arr_ll=_fs_ll(_fix.get('leg_arr') or ''))
                    _fs = _fs_resolve(_fs_keys, _fs_op({
                        'lat': _fix.get('lat'), 'lon': _fix.get('lon'),
                        'track': _fix.get('track'), 'gs': _fix.get('speed_kt'),
                        'alt': _fix.get('alt_ft'), 'seen_ts': _fix.get('ts'),
                    }, (_fix.get('source') or 'adsb')))
                    # additiv: 'lost' = Coverage weg und Fliegen nicht mehr
                    # beweisbar → iOS KANN die Simulation ehrlich beenden.
                    status['live_status'] = _fs.get('live_status')
                    # Engine-Phase → Family-Vokabular (Kontrakt: 'airborne'|
                    # 'landed'|'grounded'|'cancelled'|None); None ⇒ die Board-
                    # basierte _canonical_flight_phase bleibt stehen.
                    _eng_ph = {'AIRBORNE': 'airborne', 'APPROACH': 'airborne',
                               'DIVERTED': 'airborne', 'LANDED': 'landed',
                               'ARRIVED': 'landed', 'TAXI_OUT': 'grounded',
                               'BOARDING': 'grounded',
                               'CANCELLED': 'cancelled'}.get(_fs.get('phase'))
                    if _eng_ph is not None:
                        status['flight_phase'] = _eng_ph
                    if _fs.get('live') is None:
                        _fix = None      # Engine-gegatet: Taxi/Boden/lost ⇒ kein Dot
                except Exception:
                    # Fallback = altes rohes Kinematik-Gate (nie schlechter als
                    # vorher: kein Geister-Dot, auch wenn die Engine wirft).
                    try:
                        from blueprints.flight_state import is_airborne_kinematic as _fs_air
                        if not _fs_air({'gs_kt': _fix.get('speed_kt'),
                                        'alt_ft': _fix.get('alt_ft')}):
                            _fix = None
                    except Exception:
                        pass
            if _fix:
                status['live_lat'] = _fix['lat']
                status['live_lon'] = _fix['lon']
                status['live_track'] = _fix['track']
                status['live_speed_kt'] = _fix['speed_kt']
                status['live_ts_iso'] = _iso_utc_z(_dt.datetime.fromtimestamp(
                    _fix['ts'], _dt.timezone.utc))
                status['live_source'] = _fix['source']
                # LIVE-BESTÄTIGTES aktuelles Leg (free-only, geometrie-gegated):
                # überschreibt today_dep/arr mit der WIRKLICH gerade geflogenen
                # Strecke (diversion-fest), Tour-Label bleibt. Reg mitgeben —
                # die Roster-IATA-Flugnummer (LH716) matcht die ATC-Callsign-
                # Keys (DLH716) sonst nie. Übernommen wird JEDE live-bestätigte
                # Quelle (confidence='confirmed': aircraft_live/Board/Warehouse/
                # fr24_grpc — alle gratis, allow_paid=False bleibt), nicht nur
                # exakt fr24_grpc; 'estimated' (fr24_live) bleibt draußen.
                _cs = _fix.get('callsign')
                if _cs:
                    try:
                        from blueprints.warehouse_reader import route_for_flight
                        _lr = route_for_flight(callsign=_cs, reg=_fix.get('reg'),
                                               lat=_fix.get('lat'),
                                               lon=_fix.get('lon'), track=_fix.get('track'),
                                               gs=_fix.get('speed_kt'), allow_paid=False)
                    except Exception:
                        _lr = None
                    if (_lr and _lr.get('confidence') == 'confirmed'
                            and _lr.get('src') and _lr.get('dst')):
                        status['today_dep_iata'] = _lr['src']
                        status['today_arr_iata'] = _lr['dst']
                        status['today_dep_city'] = _iata_city_name(_lr['src'])
                        status['today_arr_city'] = _iata_city_name(_lr['dst'])
    # ── EINE Wahrheit (Neubau 2026-07-10): crew_state aus dem zentralen
    # Resolver blueprints/crew_live_state — DERSELBE wie in friends-today.
    # Expliziter Zustand (home|standby|pre_flight|flying|landed|layover) +
    # SERVERSEITIGER Text („Fliegt gerade" / „Gelandet in …" / „Wartet auf
    # LH… · HH:MM" / „Layover …" / „Basis …" nur ohne Dienst) — iOS zeigt ihn
    # 1:1. ADDITIV: alle Altfelder oben bleiben für alte Builds unverändert.
    # Grant-Gate wie flying_now/today_* (next_flight). Best-effort, wirft nie.
    if 'next_flight' in allowed_fields:
        try:
            from blueprints.crew_live_state import (resolve_crew_live_state,
                                                    build_obs_lookup,
                                                    build_live_lookup,
                                                    build_local_hhmm)
            _cs_day = None
            try:
                # Aktiver Flugtag zuerst (Red-Eye: ggf. der gestrige), sonst
                # der heutige Flugtag (Vorschau/Nachlauf). Kein Flugtag →
                # Leg-loser Resolver-Pfad (Layover-Ruhetag/Basis).
                _cs_day = active_day if active_day is not None else (
                    prim if (prim and prim.get('is_flight')) else None)
            except NameError:
                _cs_day = None   # SB-Zweig übersprungen/abgebrochen
            _cs_secs, _cs_datum = [], None
            if _cs_day is not None:
                _cs_datum = _cs_day.get('datum')
                _cs_secs = (_cs_day.get('sectors')
                            or _snapshot_day_sectors(crew_token, _cs_datum)
                            or [])
            status['crew_state'] = resolve_crew_live_state(
                _cs_secs,
                build_obs_lookup(_app_attr('_flight_obs_merged'), _cs_datum),
                build_live_lookup(),
                _dt.datetime.now(_dt.timezone.utc),
                homebase=hb or None,
                layover_iata=roster_layover,
                city_lookup=_iata_city_name,
                local_hhmm=build_local_hhmm(_app_attr('airport_tz')),
                status_bucket=_app_attr('_flight_status_bucket'))
        except Exception as e:
            _log().info(f'[family-watch] crew_state_skip {type(e).__name__}')
    if 'layover_place' in allowed_fields:
        status['layover_place'] = roster_layover
        status['layover_place_city'] = (_iata_city_name(roster_layover)
                                        if roster_layover else None)
        # Feierabend/Zuhause: heute an der Homebase und NICHT gerade in der Luft.
        status['home_now'] = bool(roster_today_home) and not flying_now
    # Reconcile current_city gegen den Roster: wenn der heutige Plan POSITIV an
    # der Homebase liegt (roster_today_home — erstes ical_location-Token == hb),
    # ist die Crew laut Plan zuhause — eine widersprechende GPS-Stadt (anderer
    # Ort) ist dann ein veralteter Sample und wird NICHT an die Family geleakt.
    # Wir erfinden keine Stadt, wir entfernen nur eine nachweislich plan-widrige.
    # WICHTIG: nur bei POSITIVEM Home-Signal — ein OFF-/Rest-Tag OHNE IATA-Code
    # (z.B. mehrtägiger Layover-Ruhetag fern der Base) lässt current_city stehen,
    # sonst würden wir eine legitime Layover-GPS-Stadt fälschlich unterdrücken.
    # Hat der Roster einen echten Layover (roster_layover gesetzt), ist DER die
    # Wahrheit; eine current_city, die diese IATA nicht erkennbar enthält (ein
    # einfacher Contains scheitert für ausgeschriebene Städtenamen wie „München"),
    # wird unterdrückt, weil die Family bereits layover_place sieht und zwei
    # widersprüchliche Orte nur verwirren.
    # PRIVACY HARD-GATE (User-Anweisung 2026-06-29): im Dienstplan-Modus
    # (location_source != 'gps' ODER fehlend → Default) NIE die gespeicherte
    # GPS-current_city an die Family leaken — sie soll nicht erfahren, dass die
    # Crew während eines Layovers tatsächlich woanders hingeflogen ist. Der
    # Roster-Ort steht bereits in layover_place; current_city wird komplett
    # verworfen. Nur bei explizitem GPS-Modus bleibt die reverse-geocodete Stadt.
    _loc_src = prof.get('location_source') if isinstance(prof, dict) else None
    _loc_src = _loc_src.strip().lower() if isinstance(_loc_src, str) else ''
    if _loc_src != 'gps':
        status['current_city'] = None
    if 'current_city' in allowed_fields and status.get('current_city'):
        if roster_today_home:
            # Plan = Homebase-Tag → keine widersprechende GPS-Stadt servieren.
            status['current_city'] = None
        elif roster_layover is not None:
            # Plan = Layover an roster_layover → wenn die GPS-Stadt diesen Ort
            # nicht erkennbar enthält, ist sie stale → unterdrücken (layover_place
            # trägt die korrekte Info).
            cc = str(status['current_city']).strip().upper()
            if roster_layover not in cc:
                status['current_city'] = None
    # Felder die NICHT in allowed_fields sind: explicit auf None setzen
    # (Privacy-Garantie: Server filtert, Client kann nicht durchgeben was nicht gegranted).
    if 'current_city' not in allowed_fields:
        status['current_city'] = None
    if 'next_flight' not in allowed_fields:
        status['next_flight_no'] = None
        status['next_flight_dep_iata'] = None
        status['next_flight_arr_iata'] = None
        status['next_flight_dep_city'] = None
        status['next_flight_arr_city'] = None
        status['next_flight_etd_iso'] = None
        status['today_dep_city'] = None
        status['today_arr_city'] = None
        status['today_route_label'] = None
        # Live-Positions-Fix hängt am next_flight-Grant (Defense-in-Depth —
        # gesetzt wird er ohnehin nur im Grant-Block oben).
        status['live_lat'] = None
        status['live_lon'] = None
        status['live_track'] = None
        status['live_speed_kt'] = None
        status['live_ts_iso'] = None
        status['live_source'] = None
        # crew_state hängt am selben Grant (Defense-in-Depth — gesetzt wird
        # es ohnehin nur im Grant-Block oben).
        status['crew_state'] = None
    if 'layover_place' not in allowed_fields:
        status['layover_place'] = None
        status['layover_place_city'] = None
    if 'landed_status' not in allowed_fields:
        status['landed'] = None
    if 'photos' not in allowed_fields:
        status['photo_count_today'] = None
    if not (allowed_fields & {'current_city', 'last_seen', 'landed_status', 'next_flight'}):
        status['last_seen_iso'] = None
    # ── „Es gibt immer eine Info" (2026-07-03, SB-RemoteProtocolError-Flakes) ──
    # as_of = ehrliches Frische-Feld: Zeitpunkt, zu dem dieser Status berechnet
    # wurde. Bei einem Cache-Hit unten trägt der Status das ÄLTERE as_of seines
    # erfolgreichen Reads — die App kann „Stand von HH:MM" zeigen.
    status['as_of'] = _dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    cache_key = (crew_token, frozenset(allowed_fields))
    if not src_fail:
        # Voll erfolgreicher Read → als last-known-good merken (auch ein
        # legitimer Leer-Status, z.B. keine Grants — der ist dann die Wahrheit).
        if len(_crew_status_last_good) >= _CREW_STATUS_CACHE_MAX:
            _crew_status_last_good.clear()   # simpler Cap, kein LRU nötig
        _crew_status_last_good[cache_key] = dict(status)
        return status
    if _status_has_signal(status):
        # Teilweise gelesen (z.B. Profil ok, Roster-Flake): live-Stand servieren,
        # aber NICHT als last-good cachen (würde vollen Stand verwässern).
        return status
    # Quelle trotz Retry unlesbar UND kein Signal → letzter bekannter Stand.
    cached = _crew_status_last_good.get(cache_key)
    if cached and _status_has_signal(cached):
        _log().info(f'[family-watch] status src_fail → last-good (as_of={cached.get("as_of")})')
        return dict(cached)
    # GAR nichts da → wenigstens die nächste Tour aus dem Disk-Roster-Mirror.
    if _fallback_next_tour_from_disk(status, crew_token, allowed_fields):
        _log().info('[family-watch] status src_fail → disk-roster next-tour fallback')
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


def _crew_avatar(crew_token):
    """Profilfoto-URL der Crew für die Family-Avatare (User 2026-06-25: „Avatars
    mit Profilfoto oben, ein Klick wechselt zwischen mehreren Crew"). Nur die
    öffentliche avatar_url — kein Token/PII."""
    prof = _load_crew_profile(crew_token) or {}
    a = prof.get('avatar_url')
    return a if (isinstance(a, str) and a.strip()) else None


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
_kv_last_good = {}  # table → {'data': dict, 'at': epoch} (siehe _shares_last_good)


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
        _kv_last_good[table] = {'data': dict(out), 'at': time.time()}
        return out
    except Exception as e:
        _log().warning(f'[family-kv] {table} sb_load_fail {type(e).__name__}: {str(e)[:120]}')
        lg = _kv_last_good.get(table)
        if lg and (time.time() - lg.get('at', 0)) < _SB_LAST_GOOD_TTL_S:
            _log().info(f'[family-kv] {table} serving last-good snapshot ({len(lg["data"])} keys)')
            return dict(lg['data'])
        return None


def _kv_save_to_sb(table, key_col, data):
    """UPSERT-ONLY: upsertet die vorhandenen Keys in SB. KEIN Reconcile-Delete
    mehr (#7/#19): auf Cloud Run multi-instance hätte das Löschen von Keys, die
    in einem stalen In-Memory-Snapshot fehlen, Pair-Codes/Scoped-Tokens anderer
    Crews weggewischt (Race: Instance A löscht was Instance B gerade angelegt
    hat → fremder Redeem brach). Echte Deletes (konsumierter/abgelaufener Code)
    machen die Caller jetzt per gezieltem .delete().eq(...)."""
    sb_avail, sb = _get_sb()
    if not sb_avail or sb is None:
        return False
    try:
        rows = [{key_col: k, 'data': v} for k, v in (data or {}).items() if k]
        for i in range(0, len(rows), 500):
            sb.table(table).upsert(rows[i:i+500], on_conflict=key_col).execute()
        return True
    except Exception as e:
        _log().warning(f'[family-kv] {table} sb_save_fail {type(e).__name__}: {str(e)[:160]}')
        return False


def _kv_delete_from_sb(table, key_col, key):
    """Gezieltes Löschen eines einzelnen Keys (#7/#19): ersetzt das frühere
    blanket Reconcile-Delete. Guarded, wirft nie."""
    if not key:
        return False
    sb_avail, sb = _get_sb()
    if not sb_avail or sb is None:
        return False
    try:
        sb.table(table).delete().eq(key_col, key).execute()
        return True
    except Exception as e:
        _log().warning(f'[family-kv] {table} sb_delete_fail {type(e).__name__}: {str(e)[:160]}')
        return False


def _requests_delete_from_sb(crew_token, family_token):
    """Gezieltes Löschen einer Anfrage in SB (#7): ersetzt das Reconcile-Delete
    in _requests_save_to_sb. Guarded, wirft nie."""
    if not crew_token or not family_token:
        return False
    sb_avail, sb = _get_sb()
    if not sb_avail or sb is None:
        return False
    try:
        (sb.table('family_requests').delete()
         .eq('crew_token', crew_token).eq('family_token', family_token).execute())
        return True
    except Exception as e:
        _log().warning(f'[family-req] sb_delete_fail {type(e).__name__}: {str(e)[:160]}')
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
        # SECURITY (2026-06 Audit): NIE das echte Crew-Bearer-Token an die
        # Family-Seite ausliefern — damit wäre voller Account-Zugriff möglich
        # (Roster, DMs, Grants widerrufen, Account löschen). Der Client nutzt
        # das Feld nur als stabile Identifiable-ID, also liefern wir unter dem
        # alten Key einen opaken, pro Pairing stabilen Hash (Salt = Family-
        # Token, damit IDs nicht über Familien hinweg korrelierbar sind).
        opaque_id = hashlib.sha256(
            f'{crew_token}:{token}'.encode('utf-8')
        ).hexdigest()[:16]
        out.append({
            'crew_token': opaque_id,
            'crew_short_name': _crew_short_name(crew_token),
            'crew_avatar_url': _crew_avatar(crew_token),
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
    _pl = _app_attr('_profile_load')
    _rel_label = {'papa': 'Papa', 'mama': 'Mama', 'partner': 'Partner',
                  'freund': 'Freund/in', 'kind': 'Kind', 'family': 'Familie'}
    grants = []
    seen = set()
    for s in own:
        ft = s.get('family_token')
        # DEDUPE nach family_token: dieselbe Person darf nicht doppelt erscheinen
        # (User #48: „2 Family-Zeilen für eine Person").
        if ft in seen:
            continue
        seen.add(ft)
        rel = (s.get('relation') or '').strip().lower()
        # Profil der Family-Person EINMAL laden — für Name UND Avatar (damit die
        # „Familie"-Karte genauso aussieht wie die normalen Crew-Karten: Foto + Name,
        # User #48/#10 „kein Name, kein Foto, nicht uniform").
        prof = {}
        if ft and _pl:
            try:
                prof = (_pl(ft) or {}).get('profile', {}) or {}
            except Exception:
                prof = {}
        # Echter Anzeigename statt Token-Fragment: gespeicherter Anfrage-Name →
        # Profilname → Relation-Label.
        disp = s.get('requester_name') or prof.get('name')
        if not disp:
            disp = _rel_label.get(rel, 'Familie')
        grants.append({
            'family_token': ft,
            'family_short_name': disp,
            'family_relation': s.get('relation'),
            'avatar_url': prof.get('avatar_url'),
            'fields': [f for f in (s.get('fields') or []) if f in ALLOWED_FIELDS],
            'created_at': _iso_utc_z(s.get('created_at')),
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
            s['updated_at'] = _now_utc_z()
            found = True
            break
    if not found:
        shares.append({
            'crew_token': token,
            'family_token': family_token,
            'relation': relation,
            'fields': fields,
            'created_at': _now_utc_z(),
        })
    if not _shares_save(shares):
        return jsonify({'ok': False, 'error': 'persist_failed'}), 500
    return jsonify({'ok': True, 'fields': fields, 'relation': relation})


@family_watch_bp.route('/api/family-request/<family_token>', methods=['POST'])
def family_request_create(family_token):
    """Familie schickt einer GESUCHTEN Crew-Person eine Beobachtungs-Anfrage.
    Body: {crew_token, relation}. Ersetzt den Pair-Code-Flow."""
    if not _safe_token(family_token):
        return jsonify({'ok': False, 'error': 'invalid_token'}), 400
    body = request.get_json(silent=True) or {}
    crew_token = (body.get('crew_token') or '').strip()
    if not crew_token or crew_token == family_token:
        return jsonify({'ok': False, 'error': 'bad_crew_token'}), 400
    relation = (body.get('relation') or 'family').strip().lower()
    if relation not in ALLOWED_RELATIONS:
        relation = 'family'
    fam_prof = _load_crew_profile(family_token) or {}
    reqs = _requests_load()
    if any(r.get('crew_token') == crew_token and r.get('family_token') == family_token
           for r in reqs):
        return jsonify({'ok': True, 'already': True})
    reqs.append({
        'crew_token': crew_token, 'family_token': family_token, 'relation': relation,
        'requester_name': (fam_prof.get('name') or 'Familie'),
        'requester_avatar': fam_prof.get('avatar_url'),
        'created_at': _now_utc_z(),
    })
    _requests_save(reqs)
    return jsonify({'ok': True})


@family_watch_bp.route('/api/family-request/<crew_token>/pending', methods=['GET'])
def family_request_pending(crew_token):
    """Crew-Person: offene Familie-Anfragen zum Bestätigen/Ablehnen."""
    if not _safe_token(crew_token):
        return jsonify({'ok': False, 'error': 'invalid_token'}), 400
    reqs = _requests_load()
    out = [{
        'family_token': r['family_token'],
        'requester_name': r.get('requester_name') or 'Familie',
        'requester_avatar': r.get('requester_avatar'),
        'relation': r.get('relation') or 'family',
        'created_at': _iso_utc_z(r.get('created_at')),
    } for r in reqs if r.get('crew_token') == crew_token]
    return jsonify({'ok': True, 'requests': out})


@family_watch_bp.route('/api/family-request/<crew_token>/approve', methods=['POST'])
def family_request_approve(crew_token):
    """Crew bestätigt eine Familie-Anfrage → erstellt den family_shares-Grant.
    Body: {family_token, fields?}. fields default = alle erlaubten."""
    if not _safe_token(crew_token):
        return jsonify({'ok': False, 'error': 'invalid_token'}), 400
    body = request.get_json(silent=True) or {}
    family_token = (body.get('family_token') or '').strip()
    if not family_token:
        return jsonify({'ok': False, 'error': 'missing_family_token'}), 400
    reqs = _requests_load()
    match = next((r for r in reqs
                  if r.get('crew_token') == crew_token
                  and r.get('family_token') == family_token), None)
    if not match:
        return jsonify({'ok': False, 'error': 'no_request'}), 404
    raw_fields = body.get('fields') or list(ALLOWED_FIELDS)
    fields = [f for f in raw_fields if f in ALLOWED_FIELDS] or list(ALLOWED_FIELDS)
    relation = match.get('relation') or 'family'
    shares = _shares_load()
    if not any(s.get('crew_token') == crew_token and s.get('family_token') == family_token
               for s in shares):
        shares.append({
            'crew_token': crew_token, 'family_token': family_token,
            'relation': relation, 'fields': fields,
            # Anzeigename der Familien-Person mitnehmen (sonst zeigte die „Familie"-
            # Verwaltung nur ein Token-Fragment, User #48 „hat keinen Namen").
            'requester_name': match.get('requester_name'),
            'created_at': _now_utc_z(),
        })
        _shares_save(shares)
    # Genuine Delete: die genehmigte Anfrage gezielt aus SB entfernen (#7).
    # _requests_save upsertet nur noch (kein Reconcile-Delete), darum hier
    # explizit. Disk wird über _requests_save mitgeschrieben.
    _requests_delete_from_sb(crew_token, family_token)
    _requests_save([r for r in reqs
                    if not (r.get('crew_token') == crew_token
                            and r.get('family_token') == family_token)])
    return jsonify({'ok': True, 'fields': fields})


@family_watch_bp.route('/api/family-request/<crew_token>/reject', methods=['POST'])
def family_request_reject(crew_token):
    """Crew lehnt eine Familie-Anfrage ab (Anfrage entfernen). Body: {family_token}."""
    if not _safe_token(crew_token):
        return jsonify({'ok': False, 'error': 'invalid_token'}), 400
    body = request.get_json(silent=True) or {}
    family_token = (body.get('family_token') or '').strip()
    reqs = _requests_load()
    # Genuine Delete: die abgelehnte Anfrage gezielt aus SB entfernen (#7).
    _requests_delete_from_sb(crew_token, family_token)
    _requests_save([r for r in reqs
                    if not (r.get('crew_token') == crew_token
                            and r.get('family_token') == family_token)])
    return jsonify({'ok': True})


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

def _resolve_crews_for_family(family_token):
    """ALLE Crew-Tokens zu einem Family-Token (scoped + alle Grants), dedupliziert."""
    out = []
    seen = set()
    scoped = _scoped_token_crew(family_token)
    if scoped and scoped not in seen:
        seen.add(scoped); out.append(scoped)
    try:
        for s in _shares_load():
            if s.get('family_token') == family_token:
                ct = s.get('crew_token')
                if ct and ct not in seen:
                    seen.add(ct); out.append(ct)
    except Exception:
        pass
    return out


def _resolve_crew_for_family(family_token, opaque_id=None):
    """Findet den Crew-Token zu einem Family-Token. Mit `opaque_id` (der pro
    Pairing stabile sha256(crew:family)[:16]-Hash aus dem Watch-Feed) wird GENAU
    der gemeinte Crew aufgelöst → Family kann mehreren Crew folgen. Ohne id:
    der primäre (scoped) Crew."""
    crews = _resolve_crews_for_family(family_token)
    if opaque_id:
        for ct in crews:
            h = hashlib.sha256(f'{ct}:{family_token}'.encode('utf-8')).hexdigest()[:16]
            if h == opaque_id:
                return ct
    return crews[0] if crews else None


def _load_crew_roster_days(crew_token, days_limit):
    """Lädt das Roster der Crew als Tag-Detail-Liste — EXAKT die gleiche Quelle
    wie GET /api/user/friend-roster (in-memory _store, Fallback roster_snapshot).
    Privacy: liefert KEINE Geld-Felder (eur), nur Plan-Infos (Klasse, Routing,
    Layover, Zeiten) — Family sieht den Plan, nicht die Steuer."""
    from datetime import date as _date, timedelta as _td
    _store = _app_attr('_store', {}) or {}
    sess = _store.get(crew_token) or {}
    tage = (sess.get('result_data') or {}).get('_tage_detail') or []
    if not tage:
        snap_fn = _app_attr('_roster_snapshot_read')
        if callable(snap_fn):
            try:
                tage = (snap_fn(crew_token) or {}).get('tage') or []
            except Exception:
                tage = []
    today = _date.today()
    cutoff = today + _td(days=days_limit)
    out = []
    for day in tage:
        if not isinstance(day, dict):
            continue
        d = day.get('datum')
        if not d:
            continue
        try:
            day_date = _date.fromisoformat(d[:10])
            if day_date > cutoff or day_date < today - _td(days=45):
                continue
        except Exception:
            continue
        rf = day.get('reader_facts') or {}
        out.append({
            'datum': d,
            'klass': day.get('klass'),
            'marker': day.get('marker'),
            'routing': day.get('routing'),
            # bewusst KEIN 'eur' — Family sieht keine Geld-Daten
            'layover_ort': rf.get('layover_ort'),
            # Hübsches Tour-Label mit Städtenamen ("Frankfurt – San Francisco")
            # statt roher Token-Ketten (2026-07-03). IATA bleibt in routing/
            # layover_ort für Clients, die beides zeigen wollen.
            'route_label': _route_label_cities(day.get('routing'),
                                               rf.get('layover_ort')),
            'layover_city': (_iata_city_name(rf.get('layover_ort'))
                             if rf.get('layover_ort') else None),
            'start_time': rf.get('start_time'),
            'end_time': rf.get('end_time'),
            # Pro-Leg-Sektoren durchreichen → Family-Sheet zeigt die echten Legs
            # (volle Namen/keine Briefing-Zeiten macht der Client). Ohne das nur 1 Route/Tag.
            'ical_sectors': day.get('ical_sectors'),
        })

    # FALLBACK (User: „bin bei Family drin und kann den Kalender nicht sehen"):
    # kein Tax-_tage_detail und kein roster_snapshot → den Plan DIREKT aus
    # user_ical_briefings bauen (gleiche Quelle wie der Live-Status). So sieht die
    # Family den Kalender auch ohne dass die Crew je eine Steuer-Auswertung lief.
    if not out:
        sb_avail, sb = _get_sb()
        if sb_avail and sb is not None:
            try:
                start = (today - _td(days=45)).isoformat()
                end = cutoff.isoformat()
                # Nur EXISTIERENDE Spalten selektieren (ical_klass gibt es NICHT —
                # ein Select darauf wirft 42703 → Fallback lieferte leer).
                r = (sb.table('user_ical_briefings')
                     .select('datum,ical_summary,ical_location,ical_start,ical_end')
                     .eq('token', crew_token)
                     .gte('datum', start).lte('datum', end)
                     .order('datum').limit(150).execute())

                def _hhmm(x):
                    m = re.search(r'T(\d{2}:\d{2})', str(x or ''))
                    return m.group(1) if m else None

                for row in (r.data or []):
                    d = row.get('datum')
                    if not d:
                        continue
                    summ = (row.get('ical_summary') or '')
                    up = summ.upper()
                    klass = 'OFF' if 'OFF DAY' in up else ('Z76' if 'LAYOVER' in up else None)
                    codes = re.findall(r'\b[A-Z]{3}\b', (row.get('ical_location') or '').upper())
                    dedup = []
                    for c in codes:
                        if not dedup or dedup[-1] != c:
                            dedup.append(c)
                    routing = '-'.join(dedup) if len(dedup) >= 2 else None
                    out.append({
                        'datum': d,
                        'klass': klass,
                        'marker': summ,
                        'routing': routing,
                        'layover_ort': None,
                        'route_label': _route_label_cities(routing),
                        'layover_city': None,
                        'start_time': _hhmm(row.get('ical_start')),
                        'end_time': _hhmm(row.get('ical_end')),
                    })
            except Exception as e:
                _log().info(f'[family-roster] ical_fallback_skip {type(e).__name__}')
    # TAIL-ANREICHERUNG (Owner 2026-07-04: „Tails auf jedem Leg im Kalender bei
    # Crew UND Freunde"). Family-Sektoren laufen nicht durch die Delay-Anreicherung
    # → pro sichtbarem Leg das echte Board/Warehouse-Kennzeichen additiv anhängen.
    # Nur today ±1 (Guard in _enrich_leg_tails), free_only + Memo → billiger Fan-out.
    # Rein additiv, defensiv; ändert NICHTS an der Sichtbarkeits-/Privacy-Logik.
    enrich_tails = _app_attr('_enrich_leg_tails')
    if callable(enrich_tails):
        for _e in out:
            try:
                _secs = _e.get('ical_sectors')
                if isinstance(_secs, list) and _secs:
                    enrich_tails(_secs, _e.get('datum'))
            except Exception:
                pass
    return out


@family_watch_bp.route('/api/family-roster/<family_token>', methods=['GET'])
def family_roster(family_token):
    """Family-Person holt den (read-only) Kalender/Plan der gepairten Crew.
    Query: ?days=60 (default 60, max 120).
    Privacy: nur Plan-Infos, keine Geld-/Steuer-Daten. Respektiert ein explizites
    share_roster=False als Opt-Out (honest empty)."""
    if not _safe_token(family_token):
        return jsonify({'ok': False, 'error': 'invalid_token', 'days': []}), 400
    try:
        days_limit = min(max(int(request.args.get('days') or 60), 1), 120)
    except Exception:
        days_limit = 60
    opaque_id = (request.args.get('crew') or '').strip() or None
    crew_token = _resolve_crew_for_family(family_token, opaque_id=opaque_id)
    if not crew_token:
        return jsonify({'ok': False, 'shared': False,
                        'error': 'not_paired', 'days': []}), 404
    # KEIN Opt-Out mehr (Produkt-Entscheidung 2026-06-25): das Annehmen einer
    # Familie-Anfrage IST die Zustimmung, den Plan zu teilen. Der share_roster-
    # Aus-Schalter ist im Client entfernt — eine bestätigte Familie sieht den
    # Plan immer (read-only, weiterhin OHNE Geld-/Steuer-Daten).
    days = _load_crew_roster_days(crew_token, days_limit)
    return jsonify({
        'ok': True, 'shared': True, 'count': len(days), 'days': days,
        'crew_name': _crew_short_name(crew_token),
        'crew_homebase': _crew_homebase(crew_token),
        # Profilfoto der Crew → die Family-App zeigt das echte Foto im Header statt
        # nur Initialen (User: „Name UND Foto vom Crew-Member fehlt").
        'crew_avatar_url': _crew_avatar(crew_token),
    })


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
        # Abgelaufen → aufräumen. Gezieltes SB-Delete (#7/#19): _pair_codes_save
        # upsertet nur noch, darum den Key hier explizit aus SB entfernen.
        codes.pop(code, None)
        _kv_delete_from_sb('family_pair_codes', 'code', code)
        _pair_codes_save(codes)
        return jsonify({'ok': False, 'error': 'code_expired'}), 410

    # Single-Use: ein bereits eingelöster Code mintet KEIN zweites Family-Token.
    # (Security-Audit 2026-06-07: vorher blieb ein konsumierter Code bis TTL
    # einlösbar → wer den Code abfängt, könnte nach dem legitimen User ein
    # eigenes Family-Token münzen.)
    if rec.get('consumed'):
        return jsonify({'ok': False, 'error': 'code_already_used'}), 409

    crew_token = rec.get('crew_token')
    if not crew_token:
        return jsonify({'ok': False, 'error': 'code_invalid'}), 400

    family_name = (body.get('family_name') or '').strip()[:60] or None

    # --- TOCTOU-Schutz (#20): Single-Use atomar durchsetzen, BEVOR ein Token
    # gemünzt wird. Vorher war read→check→mint→mark-consumed nicht atomar: zwei
    # parallele Redeems desselben Codes konnten beide zwei Family-Tokens münzen.
    # Strategie:
    #   1) Wenn SB verfügbar: atomares conditional update
    #        update(data=consumed) WHERE code=? AND data->>consumed='false'
    #      Nur wenn das genau diese Zeile getroffen hat (r.data nicht leer),
    #      haben WIR das Rennen gewonnen → mint erlaubt. Trifft es 0 Zeilen,
    #      hat ein anderer Redeemer den Code schon konsumiert (oder der Code
    #      existiert nur auf Disk) → wir verweigern bzw. fallen auf Disk zurück.
    #   2) Ohne SB: consumed=True setzen und ZUERST speichern, DANN minten —
    #      das verkleinert das Fenster auf die Disk-Schreiblatenz.
    consumed_rec = dict(rec)
    consumed_rec['consumed'] = True
    consumed_rec['consumed_at'] = now

    sb_avail, sb = _get_sb()
    won_via_sb = False
    if sb_avail and sb is not None:
        try:
            r = (sb.table('family_pair_codes')
                 .update({'data': consumed_rec})
                 .eq('code', code)
                 .eq('data->>consumed', 'false')
                 .execute())
            if r.data:
                won_via_sb = True
            else:
                # 0 Zeilen getroffen. Existiert der Code in SB bereits als
                # consumed → eindeutig schon eingelöst → ablehnen. Existiert er
                # gar nicht in SB (nur Disk) → unten auf Disk-Pfad zurückfallen.
                exists = (sb.table('family_pair_codes').select('code')
                          .eq('code', code).limit(1).execute())
                if exists.data:
                    return jsonify({'ok': False, 'error': 'code_already_used'}), 409
        except Exception as e:
            _log().warning(f'[family-pair] redeem_sb_consume_fail {type(e).__name__}: {str(e)[:140]}')

    if not won_via_sb:
        # Disk-Pfad (SB nicht verfügbar / Code nur auf Disk): consumed FIRST
        # speichern, DANN minten — verkleinert das TOCTOU-Fenster maximal ohne
        # echte Atomarität zu garantieren.
        codes[code] = consumed_rec
        _pair_codes_save(codes)

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

    if won_via_sb:
        # SB ist bereits konsumiert (atomar). Disk-Spiegel best-effort
        # nachziehen, damit ein späterer Disk-only-Read nicht „unconsumed" sieht.
        codes[code] = consumed_rec
        try:
            _atomic_write_json(_pair_codes_disk_path(), codes)
        except Exception:
            pass

    return jsonify({
        'ok': True,
        'family_token': family_token,
        'crew_name': _crew_short_name(crew_token),
        'crew_homebase': _crew_homebase(crew_token),
    })
