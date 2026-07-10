"""
AeroX Aviation Data Engine — die eigene, self-hosted Luftfahrt-Datenquelle.

Zwei Schichten:
  • KALT/statisch  → `data/aerox_reference.sqlite.gz`, ins Docker-Image gebacken,
    beim Boot nach /tmp entpackt (read-only). 85k Flughäfen, 6k Airlines,
    520k Flugzeuge (inkl. Baujahr), 2.7k Muster, 67k Seed-Routen.
  • HEISS/wachsend → Supabase-Cache (`ax_aircraft_cache`, `ax_route_cache`).
    Jeder externe Treffer (adsbdb/hexdb) wird zurückgeschrieben → über echte
    Nutzung wächst die DB selbst, jede Tatsache wird höchstens EINMAL bezahlt.

Endpoints (alle GET):
  /api/ax/stats              Coverage-Dashboard (Zeilen pro Tabelle)
  /api/ax/airport/<code>     IATA(3) oder ICAO(4) → Name/Stadt/Land/Koordinaten
  /api/ax/airline/<code>     IATA(2) oder ICAO(3) → Name/Callsign/Land
  /api/ax/type/<code>        ICAO-Muster → Hersteller/Modell/Triebwerke
  /api/ax/aircraft/<hex>     Hex → Reg/Typ/Halter/Baujahr/Alter (+ Live-Fallback)
  /api/ax/flight/<flightno>  z.B. LH506 → Airline + Route + beide Flughäfen

Ziel: den Großteil der App-Lookups OHNE bezahlte API bedienen. Nur unbekannte
Hexes und echte Live-Routen lösen genau einen externen Call aus, danach Cache.
"""
import gzip
import hashlib
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import threading
import time
import urllib.parse
import urllib.request

from flask import Blueprint, jsonify

aerox_data_bp = Blueprint('aerox_data', __name__)
_log = logging.getLogger('aerox.data')

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
_GZ = os.path.join(_REPO, 'data', 'aerox_reference.sqlite.gz')
_DB_PATH = os.path.join(os.environ.get('AEROX_DB_TMP', '/tmp'), 'aerox_reference.sqlite')

_conn = None
_conn_lock = threading.Lock()
_METAR_CACHE = {}   # icao → (expires_ts, dict)
_MEM_BUDGET = {}    # „YYYY-MM" → verbrauchte AviationStack-Calls (In-Memory-Fallback)


def _ensure_db():
    """Entpackt die gebackene DB einmalig nach /tmp und öffnet sie read-only."""
    global _conn
    if _conn is not None:
        return _conn
    with _conn_lock:
        if _conn is not None:
            return _conn
        if not os.path.exists(_DB_PATH):
            if not os.path.exists(_GZ):
                return None
            tmp = _DB_PATH + '.part'
            with gzip.open(_GZ, 'rb') as f_in, open(tmp, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
            os.replace(tmp, _DB_PATH)
        uri = f'file:{urllib.parse.quote(_DB_PATH)}?mode=ro&immutable=1'
        _conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        return _conn


def _q(sql, params=()):
    db = _ensure_db()
    if db is None:
        return []
    with _conn_lock:
        return [dict(r) for r in db.execute(sql, params).fetchall()]


def _q1(sql, params=()):
    rows = _q(sql, params)
    return rows[0] if rows else None


# ---------------------------------------------------------------- Supabase cache
def _sb():
    try:
        from app import sb, SB_AVAILABLE
        return sb if SB_AVAILABLE else None
    except Exception:
        return None


def _cache_get(table, key_col, key, max_age_days=90):
    """Supabase-Cache-Read MIT Staleness-Gate (Sweep 2026-07-10, Klasse B:
    Einträge galten „für immer", obwohl Flugnummern saisonal umgeroutet und
    Maschinen umregistriert werden — LH1412-„Cache von Juni"-Symptom).
    Default 90 Tage via updated_at; Payloads mit confidence != 'confirmed'
    (geratene/estimated Routen) nur 14 Tage. Abgelaufen ⇒ Miss: die Free-
    Kaskade läuft neu, _cache_put (upsert) überschreibt den Eintrag beim
    nächsten Treffer. max_age_days=None = alte Semantik (für Caller mit
    EIGENER Alterslogik, z.B. ax_schedule_cache/_fetched)."""
    sb = _sb()
    if sb is None:
        return None
    try:
        res = (sb.table(table).select('payload,updated_at')
               .eq(key_col, key).limit(1).execute())
        rows = getattr(res, 'data', None) or []
        if rows and rows[0].get('payload'):
            p = rows[0]['payload']
            p = p if isinstance(p, dict) else json.loads(p)
            if max_age_days is not None and isinstance(p, dict):
                eff_days = max_age_days
                conf = p.get('confidence')
                if conf and conf != 'confirmed':
                    eff_days = min(eff_days, 14)
                ts = _iso_to_epoch(rows[0].get('updated_at'))
                # Legacy-Rows ohne parsebares updated_at bleiben gültig
                # (kein Massen-Miss auf Altbestand).
                if ts is not None and (time.time() - ts) > eff_days * 86400:
                    return None
            return p
    except Exception:
        pass
    return None


def _cache_put(table, row):
    sb = _sb()
    if sb is None:
        return
    try:
        sb.table(table).upsert(row).execute()
    except Exception:
        pass


# ---------------------------------------------------------------- external (free)
def _http_json(url, timeout=8):
    try:
        # UA im Kontakt-Format (Sweep 2026-07-10 J2-P1): planespotters blockt
        # UAs ohne Kontakt-URL/Mail mit 403 → Flugzeug-Fotos waren app-weit tot
        # für neue Flieger. Gilt bewusst auch für adsbdb/hexdb (höflicher Client).
        req = urllib.request.Request(url, headers={
            'User-Agent': 'AeroX/1.0 (+https://aerosteuer.de; aerox@aerosteuer.de)'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8', errors='replace'))
    except Exception:
        return None


def _adsbdb_aircraft(hexid):
    d = _http_json(f'https://api.adsbdb.com/v0/aircraft/{urllib.parse.quote(hexid)}')
    ac = (((d or {}).get('response') or {}).get('aircraft')) if d else None
    if not ac:
        return None
    return {
        'reg': ac.get('registration'),
        'typecode': ac.get('type'),
        'manufacturer': ac.get('manufacturer'),
        'model': ac.get('type'),
        'owner': ac.get('registered_owner'),
        'operator': ac.get('registered_owner'),
    }


def _hexdb_aircraft(hexid):
    d = _http_json(f'https://hexdb.io/api/v1/aircraft/{urllib.parse.quote(hexid)}')
    if not d or not isinstance(d, dict):
        return None
    reg = d.get('Registration')
    typ = d.get('ICAOTypeCode') or d.get('Type')
    if not reg and not typ:
        return None
    return {
        'reg': reg,
        'typecode': typ,
        'manufacturer': d.get('Manufacturer'),
        'model': d.get('Type'),
        'owner': d.get('RegisteredOwners'),
        'operator': d.get('OperatorFlagCode') or d.get('RegisteredOwners'),
    }


def _planespotters_photo(hexid):
    """Foto-URL + Fotograf von planespotters — nur die URL-Strings (KEIN Bild-
    Storage). Frei, kein Key."""
    d = _http_json(f'https://api.planespotters.net/pub/photos/hex/{urllib.parse.quote(hexid)}')
    photos = (d or {}).get('photos') or []
    if not photos:
        return None
    p = photos[0]
    thumb = (p.get('thumbnail_large') or p.get('thumbnail') or {})
    url = thumb.get('src')
    if not url:
        return None
    return {'photo': url, 'photographer': p.get('photographer'), 'link': p.get('link')}


def _adsbdb_route(callsign):
    d = _http_json(f'https://api.adsbdb.com/v0/callsign/{urllib.parse.quote(callsign)}')
    fr = (((d or {}).get('response') or {}).get('flightroute')) if d else None
    if not fr:
        return None
    org, dst = fr.get('origin') or {}, fr.get('destination') or {}
    return {
        'src': org.get('iata_code'), 'src_icao': org.get('icao_code'),
        'dst': dst.get('iata_code'), 'dst_icao': dst.get('icao_code'),
        'callsign': callsign,
    }


def _route_from_obs(callsign):
    """ECHTE Strecke + Gate aus der eigenen Airport-Tafel-DB (`airport_delay_obs`,
    von den flughafen-EIGENEN Boards gepollt, die wir schon ziehen). User-Idee:
    „wir kennen Reg + Standort → der Flughafen weiß woher/wohin/wann". Wir mappen
    den ICAO-Callsign (CFG9XY) auf die IATA-Flugnummer (DE9XY) und schlagen den
    letzten ABFLUG-Record nach. Das ist autoritativ (echte Tafel) → wird VOR adsbdb
    genutzt. None, wenn der Flug (noch) in keiner gepollten Tafel steht."""
    import re as _re
    sb = _sb()
    if sb is None:
        return None
    cs = (callsign or '').upper().strip()
    m = _re.match(r'^([A-Z]{2,3})(\w+)$', cs)
    if not m:
        return None
    prefix, suffix = m.group(1), m.group(2)
    # FRESHNESS-GATE (Wrong-Flight): die Tafel-Historie enthält denselben Callsign
    # auch von GESTERN/vorletzter Woche. Für einen LIVE-Flug jetzt darf nur ein
    # frischer Abflug-Record (heute, oder gestern für Red-Eyes über Mitternacht UTC)
    # als aktive Route gelten — sonst zeigen wir die Strecke von gestern. Konservativ.
    yest = time.strftime('%Y-%m-%d', time.gmtime(time.time() - 86400))
    # +morgen-UTC: an Airports mit UTC+8…+14 (ICN/HND/PVG/SYD/BKK/SIN…) ist das
    # Flughafen-LOKALE Beobachtungs-Datum aus UTC-Sicht oft schon „morgen" — sonst
    # würden genau die neu abgedeckten Asien/Ozeanien-Flüge hier verworfen.
    tmrw = time.strftime('%Y-%m-%d', time.gmtime(time.time() + 86400))
    fresh_dates = {_today_utc(), yest, tmrw}
    cands = []
    al = _airline_row(prefix)
    if al and al.get('iata'):
        cands.append(f"{al['iata']}{suffix}")
    cands.append(cs)                       # falls die Tafel die ICAO-Flugnr führt
    for fn in cands:
        try:
            r = (sb.table('airport_delay_obs')
                 .select('date,airport,dest_iata,gate,terminal,sched,esti')
                 .eq('flight', fn).gte('date', yest)
                 .order('date', desc=True).order('updated_at', desc=True)
                 .limit(6).execute())
            rows = r.data or []
            # Abflug-Record (airport=Origin); Ankunfts-Keys ('<AP>#ARR') überspringen.
            # Nur frische Records (heute/gestern) — ältere gleiche Flugnummer verwerfen.
            dep = next((x for x in rows
                        if '#' not in (x.get('airport') or '')
                        and (x.get('date') in fresh_dates)), None)
            if dep and dep.get('dest_iata'):
                out = {'src': (dep.get('airport') or '').split('#', 1)[0],
                       'dst': dep.get('dest_iata'),
                       'gate': dep.get('gate'), 'terminal': dep.get('terminal'),
                       'source': 'aerox_board', 'callsign': cs}
                # Echte Tafel-Zeiten (station-lokal am Abflug-Airport) durchreichen
                # — nur was die Tafel WIRKLICH kennt, nichts erfinden.
                if dep.get('sched'):
                    out['sched_dep'] = dep['sched']
                if dep.get('esti'):
                    out['est_dep'] = dep['esti']
                return out
        except Exception:
            pass
    return None


def _route_from_warehouse(hexid=None, reg=None):
    """BOARD-verifizierte Route aus dem Flight-Warehouse (`flights`-Tabelle,
    gleiche Supabase): dessen Matcher hat Board-Tail → ICAO-Hex aufgelöst
    (tail-first, hyphen-tolerant). Der Lookup läuft über HEX (bzw. Tail) —
    deckt damit insbesondere ALPHANUMERISCHE Callsigns (LH441 fliegt als
    DLH4CK) ab, an denen jedes Flugnummern-Matching prinzipiell scheitert.
    Nur Flüge im Live-Fenster (Abflug −3 h … +4 h). None bei Miss/SB-down."""
    sb = _sb()
    if sb is None or not (hexid or reg):
        return None
    try:
        q = (sb.table('flights')
             .select('op_flight_no,origin,destination,gate,terminal,status,'
                     'tail,hex,sched_dep,est_dep')
             .gte('service_date',
                  time.strftime('%Y-%m-%d', time.gmtime(time.time() - 86400))))
        if hexid:
            q = q.eq('hex', (hexid or '').lower())
        else:
            raw = (reg or '').replace('-', '').upper()
            variants = {reg, raw}
            if len(raw) >= 3:
                variants.add(raw[:1] + '-' + raw[1:])
                variants.add(raw[:2] + '-' + raw[2:])
            q = q.in_('tail', sorted(v for v in variants if v))
        rows = (q.order('updated_at', desc=True).limit(4).execute()).data or []
        now = time.time()
        from datetime import datetime as _dt
        for f in rows:
            dep_iso = f.get('est_dep') or f.get('sched_dep')
            if not dep_iso:
                continue
            try:
                dep_ts = _dt.fromisoformat(
                    str(dep_iso).replace('Z', '+00:00')).timestamp()
            except (TypeError, ValueError):
                continue
            if now - 3 * 3600 <= dep_ts <= now + 4 * 3600:
                return {'src': f.get('origin'), 'dst': f.get('destination'),
                        'gate': f.get('gate'), 'terminal': f.get('terminal'),
                        'status': f.get('status'), 'reg': f.get('tail'),
                        'flight_no': f.get('op_flight_no'),
                        'source': 'warehouse_board'}
    except Exception:
        return None
    return None


_NAS_DOWN_UNTIL = [0.0]   # Epoch: bis dahin gilt der NAS-Tunnel als down (60s)


def _nas_live_pos(reg=None, flight=None, callsign=None, dep=None, max_age_s=2100):
    """Positions-Snapshot DIREKT aus dem NAS-RAM-Store (via cloudflared-Tunnel,
    `NAS_LIVE_URL`). Owner 2026-07-08 „NAS only RAM": der Harvester hält die
    Positionen im NAS-RAM und serviert sie über einen winzigen HTTP-Endpoint —
    das Backend liest von hier statt aus Supabase → spart Supabase-Disk-IO/Kosten.
    Rückgabe: (pos,(src,dst),reg_display,ac_type) | None (nicht konfiguriert /
    Miss / Fehler → Aufrufer nutzt Supabase-Fallback). Kurzer Timeout (1.5s) +
    prozessweites 60s-„NAS down"-Negativ-Memo: der friends-Fan-out darf bei
    totem Tunnel nicht pro Call den vollen Timeout zahlen."""
    base = os.environ.get('NAS_LIVE_URL', '').strip()
    if not base:
        return None
    if time.time() < _NAS_DOWN_UNTIL[0]:
        return None
    q = {'max_age': str(int(max_age_s))}
    if reg:
        q['reg'] = reg
    if flight:
        q['flight'] = flight
    if callsign:
        q['callsign'] = callsign
    if dep:
        q['dep'] = _norm_iata(dep) or dep
    url = base.rstrip('/') + '/pos?' + urllib.parse.urlencode(q)
    req = urllib.request.Request(url)
    tok = os.environ.get('NAS_LIVE_TOKEN', '')
    if tok:
        req.add_header('Authorization', 'Bearer ' + tok)
    try:
        with urllib.request.urlopen(req, timeout=1.5) as r:
            d = json.loads(r.read().decode())
    except Exception:
        _NAS_DOWN_UNTIL[0] = time.time() + 60
        return None
    p = (d or {}).get('pos')
    if not d.get('found') or not p or p.get('lat') is None or p.get('lon') is None:
        return None
    src = (p.get('origin') or '').strip().upper() or None
    dst = (p.get('dest') or '').strip().upper() or None
    pos = {'lat': p.get('lat'), 'lon': p.get('lon'),
           'track': p.get('track'), 'gs': p.get('gs_kt'), 'alt': p.get('alt_ft'),
           'on_ground': bool(p.get('on_ground')),
           'source': 'aircraft_live_nas', 'seen_ts': p.get('seen_ts')}
    reg_disp = (p.get('reg_display') or p.get('reg') or '').strip().upper() or None
    ac_type = (p.get('ac_type') or '').strip().upper() or None
    return pos, (src, dst), reg_disp, ac_type


def _apply_taxi_gate(pos):
    """TAXI-GATE (Owner 2026-07-09 „Tibor FRA→GVA an ~13:05", Flieger optisch bei
    Mannheim): FR24 meldet beim Pushback/Rollen `on_ground=false`, obwohl die
    Maschine praktisch STEHT (gs ~15 kt, keine Baro-Höhe). So ein Snapshot ist
    KEINE Live-Flugposition — die App extrapoliert sonst aus dem Taxi-Seed einen
    kriechenden Geister-Flieger samt Unsinns-Ankunft. Nur als airborne werten,
    wenn plausibel in der Luft: nennenswerte Höhe ODER Reise-nahe gs (nichts
    cruised unter ~80 kt; selbst Steigflug ist >150). Sonst → on_ground=True,
    dann verwerfen alle Consumer die Position sauber (ehrlicher Fallback).
    EINE gemeinsame Stelle für NAS- UND Supabase-Pfad."""
    if not pos:
        return pos
    _gs, _alt = pos.get('gs'), pos.get('alt')
    _airborne = ((isinstance(_alt, (int, float)) and _alt > 1000)
                 or (isinstance(_gs, (int, float)) and _gs >= 80))
    pos['on_ground'] = bool(pos.get('on_ground')) or not _airborne
    return pos


def _aircraft_live_pos(reg=None, flight=None, callsign=None, dep=None, max_age_min=35):
    """Positions-Snapshot aus dem NAS-Harvester-Store (Supabase `aircraft_live`,
    gefüllt via FR24-**gRPC** — sieht AUCH über Russland/Ozean, wo freies ADS-B
    blind ist). Owner-Idee 2026-07-08: „geht ein Flug offline, simulieren wir aus
    dem letzten Snapshot". BILLIGER Supabase-Read → Positions-Tier VOR dem
    on-demand-FR24-Korridor.

    Match-Reihenfolge: reg (Roster-Tail, korrekt für echte Crews) → flight-Nr →
    callsign. Der Flug-/Callsign-Match FÄNGT den Fall, dass der Roster-Tail
    VERALTET ist (Aircraft-Swap): er findet die Maschine, die die Flugnummer
    GERADE fliegt (Owner 2026-07-08: LH716 heute = D-ABYN, Roster sagte D-ABYM →
    reg-Match verfehlte, Flug-Match trifft). Route-konsistent (dst == dep). Nur
    frische Snapshots (< max_age_min).

    Rückgabe: (pos, (src,dst), reg_display, ac_type) | (None, None, None, None).
    pos-Keys wie iOS AXLifecycleLive (lat/lon/track/gs/alt/on_ground)."""
    # NAS-RAM-Store zuerst (via Tunnel, spart Supabase-Disk-IO). NAS_LIVE_URL
    # gesetzt ⇒ NAS-first; Miss/Timeout/aus ⇒ Supabase-Fallback unten.
    _nas = _nas_live_pos(reg=reg, flight=flight, callsign=callsign, dep=dep,
                         max_age_s=int(max_age_min * 60))
    if _nas is not None:
        # Taxi-Gate auch für den NAS-Pfad (gemeinsame Stelle) — vorher rutschte
        # ein Pushback-Snapshot als „airborne" durch, nur der SB-Pfad gatete.
        _apply_taxi_gate(_nas[0])
        return _nas
    sb = _sb()
    if sb is None:
        return None, None, None, None
    cutoff = time.strftime('%Y-%m-%dT%H:%M:%SZ',
                           time.gmtime(time.time() - max_age_min * 60))
    rn = re.sub(r'[^A-Z0-9]', '', (reg or '').upper())
    fn = (flight or '').strip().upper() or None
    cs = (callsign or '').strip().upper() or None
    sel = 'reg,reg_display,callsign,flight,lat,lon,track,gs_kt,alt_ft,origin,dest,ac_type,on_ground,seen_ts'
    dep_n = _norm_iata(dep) if dep else None

    def _query(col, val):
        try:
            q = (sb.table('aircraft_live').select(sel)
                   .eq(col, val).gt('updated_at', cutoff))
            if dep_n:
                q = q.eq('dest', dep_n)          # Route-Konsistenz serverseitig
            return (q.limit(1).execute()).data or []
        except Exception:
            return []

    rows = []
    if rn:
        rows = _query('reg', rn)
    if not rows and fn:
        rows = _query('flight', fn)
    if not rows and cs:
        rows = _query('callsign', cs)
    if not rows:
        return None, None, None, None
    r = rows[0]
    if r.get('lat') is None or r.get('lon') is None:
        return None, None, None, None
    src = (r.get('origin') or '').strip().upper() or None
    dst = (r.get('dest') or '').strip().upper() or None
    if dep_n and dst and dst != dep_n:
        return None, None, None, None            # anderer Leg → verwerfen
    pos = _apply_taxi_gate({
        'lat': r.get('lat'), 'lon': r.get('lon'),
        'track': r.get('track'), 'gs': r.get('gs_kt'), 'alt': r.get('alt_ft'),
        'on_ground': bool(r.get('on_ground')),
        'source': 'aircraft_live', 'seen_ts': r.get('seen_ts'),
    })
    reg_disp = (r.get('reg_display') or r.get('reg') or '').strip().upper() or None
    ac_type = (r.get('ac_type') or '').strip().upper() or None
    return pos, (src, dst), reg_disp, ac_type


def _aircraft_live_flight(flight=None, callsign=None, max_age_min=40):
    """Aktiven Flug aus dem GRATIS `aircraft_live`-Warehouse (FR24-gRPC-Scraper,
    kein Credit) — echter Funkname + Reg + Route + Typ. Löst genau das Problem
    „Airline-Funkname ≠ Flugnummer" (LH1412 = DLH8UA) OHNE FR24, solange der Flug
    aktiv/geharvestet ist (LH-Group + dt. Carrier). Liefert ein Dict im flight_
    status-Schema oder None. Match by flight-Nr ODER callsign."""
    fn = (flight or '').replace(' ', '').upper() or None
    cs = (callsign or '').replace(' ', '').upper() or None
    if not (fn or cs):
        return None
    sb = _sb()
    if sb is None:
        return None
    cutoff = time.strftime('%Y-%m-%dT%H:%M:%SZ',
                           time.gmtime(time.time() - max_age_min * 60))
    sel = 'flight,callsign,reg,reg_display,ac_type,origin,dest,on_ground,seen_ts'
    try:
        q = sb.table('aircraft_live').select(sel).gt('updated_at', cutoff)
        q = q.eq('flight', fn) if fn else q.eq('callsign', cs)
        rows = (q.order('updated_at', desc=True).limit(1).execute()).data or []
    except Exception:
        return None
    if not rows:
        return None
    a = rows[0]
    src = (a.get('origin') or '').strip().upper() or None
    dst = (a.get('dest') or '').strip().upper() or None
    if not (src and dst):
        return None
    reg = re.sub(r'[^A-Z0-9]', '', (a.get('reg_display') or a.get('reg') or '').upper()) or None
    typ = (a.get('ac_type') or '').strip().upper() or None
    return {
        'flight': fn or (a.get('flight') or '').upper(),
        'callsign': (a.get('callsign') or '').upper() or None,
        'airline': '', 'airline_name': '',
        'dep_iata': src, 'dep_name': '', 'arr_iata': dst, 'arr_name': '',
        'sched_dep': None, 'sched_arr': None, 'est_dep': None, 'est_arr': None,
        'duration_min': None, 'dep_gate': '', 'dep_terminal': '',
        'arr_gate': '', 'arr_terminal': '', 'arr_baggage': '',
        'status': ('on_ground' if a.get('on_ground') else 'enroute'),
        'status_category': '', 'aircraft': typ, 'reg': reg,
        'dep_delay_min': None, 'arr_delay_min': None,
        'delay_min': None, 'delay_side': None, 'source': 'aircraft_live',
    }


def _route_from_fr24(callsign=None, hexid=None):
    """GRATIS-Route aus dem verteilten FR24-Store (`fr24_live`, gleiche Supabase;
    gefüllt vom NAS-Harvester). feed.js trägt Start/Ziel (IATA) pro Flieger →
    deckt Routen, die weder Board noch Warehouse kennen, OHNE AeroDataBox zu
    zahlen. Der Harvester speichert Start/Ziel als eigene Spalten `origin`/`dest`
    (die normalisierte `row` enthält sie nicht). Lookup per hex (PK) bevorzugt,
    sonst Callsign. Nur frische Rows (< 6 min). None bei Miss/SB-down."""
    sb = _sb()
    if sb is None or not (hexid or callsign):
        return None
    cutoff = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - 360))
    try:
        q = sb.table('fr24_live').select('origin,dest').gt('updated_at', cutoff)
        q = q.eq('hex', (hexid or '').lower()) if hexid \
            else q.eq('callsign', (callsign or '').upper())
        rows = (q.limit(1).execute()).data or []
        if not rows:
            return None
        src = (rows[0].get('origin') or '').strip().upper()
        dst = (rows[0].get('dest') or '').strip().upper()
        if src and dst and len(src) == 3 and len(dst) == 3 and src != dst:
            return {'src': src, 'dst': dst, 'source': 'fr24',
                    'confidence': 'estimated'}
    except Exception:
        return None
    return None


def _aviationstack_route(callsign):
    """AUTORITATIVE Live-Route per ICAO-Callsign (AviationStack /flights). Anders
    als die STATISCHE adsbdb-Tabelle kennt das die TATSÄCHLICHE Strecke des Fluges
    (richtungssicher) + Live-Status (active/landed). Budget-geschützt: ein Floor
    reserviert Calls für die Schedule-Funktion; nur bei Cache-Miss aufgerufen und
    FÜR IMMER in ax_route_cache gecacht (Route je Flugnummer stabil) → die Routen-
    DB wächst autoritativ aus dem realen Verkehr, künftige Taps sind gratis."""
    key = os.environ.get('AVIATIONSTACK_KEY', '')
    if not key:
        return None
    # BEZAHLT + quota-limitiert → harter Tages-Budget-Guard (free-first-Constraint).
    if not _paid_budget_ok():
        return None
    month = time.strftime('%Y-%m', time.gmtime())
    remaining, used = _budget_remaining(month)
    floor = int(os.environ.get('AVIATIONSTACK_ROUTE_FLOOR', '25'))
    if remaining <= floor:          # Schedules haben Vorrang → nur aus dem Überschuss
        return None
    url = (f'http://api.aviationstack.com/v1/flights?access_key={urllib.parse.quote(key)}'
           f'&flight_icao={urllib.parse.quote(callsign)}&limit=1')
    _paid_budget_inc(units=2)       # AviationStack ~gleichwertig gewichtet
    d = _http_json(url, timeout=7)  # interaktiv gestrafft (war 12s) — Radar-Tap wartet
    if not isinstance(d, dict):
        return None
    _budget_inc(month, used)        # Call verbraucht (auch bei 0 Treffern)
    rows = d.get('data') or []
    if not rows:
        return None
    r0 = rows[0]
    dep = (r0.get('departure') or {})
    arr = (r0.get('arrival') or {})
    src = ((dep.get('iata') or '').upper() or None)
    dst = ((arr.get('iata') or '').upper() or None)
    if not src or not dst:
        return None
    return {
        'src': src, 'src_icao': ((dep.get('icao') or '').upper() or None),
        'dst': dst, 'dst_icao': ((arr.get('icao') or '').upper() or None),
        'callsign': callsign, 'source': 'aviationstack',
        'status': r0.get('flight_status'),
    }


# ─────────────────────────────────────────────────────────────────────────────
#  LIVE-ROUTE RESOLVER  —  FREE-FIRST cascade (owner's hard constraint)
#
#  ZIEL: „so viel wie möglich mit dem eigenen Backend, gratis, über die Zeit
#  richtig gut." Bezahlte APIs (AeroDataBox BASIC, AviationStack) sind quota-
#  limitiert + fast erschöpft → sie kommen NUR als allerletzter Ausweg und hinter
#  einem harten Tages-Budget-Guard. Jeder aufgelöste Treffer (egal welche Quelle)
#  wird datums-/reg-gekeyt in die eigene Warehouse (ax_route_cache) geschrieben →
#  derselbe Tap ist morgen GRATIS und die eigene Routen-DB wächst weltweit.
#
#  Priorität (siehe _resolve_live_route) — FREI VOR BEZAHLT:
#    1. Eigene Warehouse    — date-/reg-gekeyter ax_route_cache + Airport-Tafel
#       (frei, EIGEN)         (_route_from_obs). ENTHÄLT auch die selbst-berechneten
#                             Routen aus dem eigenen ADS-B-Poll (observe_adsb_
#                             positions schreibt fertige Legs hierher). → gratis.
#    2. Selbst berechnet    — aus dem EIGENEN gepollten ADS-B (adsb.lol/OpenSky):
#       (frei, EIGEN, das     Ab-/Anflug-Erkennung am nächsten Flughafen. Landet
#        Langzeit-Asset)      via Schritt 1 im Cache. DIE Quelle, die das Backend
#                             über die Zeit selbst füllt (kostenlos, weltweit).
#    3. OpenSky             — echter beobachteter ADS-B-Track (dep/arr aus dem
#       (FREI mit Account)    Flug). Env-guarded (OPENSKY_CLIENT_ID/SECRET oder
#                             OPENSKY_USERNAME/PASSWORD); ohne Creds fail-open None.
#    4. adsbdb / adsb.lol / hexdb — generischer Callsign→Route-Lookup. Alle FREI/
#       (frei)                öffentlich → mittlerer Fallback. confidence=estimated.
#    5. AeroDataBox / AviationStack — BEZAHLT, quota-limitiert. NUR wenn nichts
#       (BEZAHLT, LETZTES)    Freies auflöste UND der Tages-Budget-Guard
#                             (_paid_budget_ok, AX_PAID_DAILY_CAP, Default 760
#                             API-Units, Tier-gewichtet) es erlaubt.
#                             NIE im Poller / nie in Bulk. confidence=confirmed.
#
#  confidence im Response (Owner „scraped/eigene Infos sind #1 — nicht prüfen"):
#    'confirmed' — echte heutige Strecke: eigene Tafel / selbst-berechnetes ADS-B /
#                  OpenSky-Track ODER autoritativer bezahlter Treffer. NIE geometrie-
#                  geprüft — eigene/gescrapte Daten gelten immer.
#    'estimated' — generischer Kandidat (adsbdb/hexdb/adsb.lol). Wird GEZEIGT (Route
#                  sichtbar), ES SEI DENN die Live-Geometrie WIDERSPRICHT KLAR: der
#                  Flieger fliegt eindeutig in die falsche Richtung (>115° weg vom
#                  Ziel, fern beider Endpunkte) → nur DANN verworfen. Anflug/Holding/
#                  gerade gestartet/Kurs-Rauschen → Route wird GEZEIGT. Der Client
#                  zeichnet 'estimated' ohne „bestätigt"-Siegel und ohne Angst-Label.
# ─────────────────────────────────────────────────────────────────────────────

# Wrong-Flight-Freshness: ein OpenSky-„recent flight" (36-h-Fenster), dessen
# letzter Kontakt älter als das ist, gilt für eine JETZT airborne Maschine als
# abgeschlossener Vor-Flug → nicht als aktiver Leg akzeptieren.
_LIVE_ROUTE_STALE_S = 3 * 3600


def _today_utc():
    return time.strftime('%Y-%m-%d', time.gmtime())


def _iso_now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _callsign_to_iata_flightno(cs):
    """ICAO-Callsign (DLH506) → IATA-Flugnummer (LH506) via Airline-Referenz.
    None, wenn Präfix unbekannt ODER der Suffix nicht rein numerisch ist
    (z.B. DLH5EF hat keine kommerzielle IATA-Nummer → nur reg-Weg sinnvoll)."""
    import re as _re
    cs = (cs or '').upper().strip()
    m = _re.match(r'^([A-Z]{3})(\d{1,4}[A-Z]?)$', cs) or _re.match(r'^([A-Z]{2})(\d{1,4}[A-Z]?)$', cs)
    if not m:
        return None
    prefix, suffix = m.group(1), m.group(2)
    if not suffix[:1].isdigit():
        return None
    al = _airline_row(prefix)
    if not al or not al.get('iata'):
        return None
    return f"{al['iata']}{suffix}"


def _bearing_deg(lat1, lon1, lat2, lon2):
    """Großkreis-Anfangskurs dep→arr in Grad (0..360). None bei fehlenden Coords."""
    import math
    try:
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dl = math.radians(lon2 - lon1)
        y = math.sin(dl) * math.cos(p2)
        x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
        return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    except Exception:
        return None


# AeroDataBox-Movement-Zeitfelder nach Verlässlichkeit. `actual`/`runway` sind
# BEOBACHTETE Zeiten, `revised`/`predicted`/`estimated` sind die aktuelle
# Erwartung, `scheduled` der Plan. Für die est-Sicht zählt actual mit (eine
# beobachtete Zeit ist die beste „Schätzung"). NIE raten: fehlt das Feld im
# Payload, bleibt es None.
_ADB_ACTUAL_KEYS = ('actualTime', 'runwayTime')
_ADB_EST_KEYS = ('actualTime', 'runwayTime', 'revisedTime', 'predictedTime',
                 'estimatedTime')
_ADB_SCHED_KEYS = ('scheduledTime',)


def _adb_movement_val(mv, keys, which):
    """AeroDataBox departure/arrival-Objekt → Zeit-String (`which` ∈ 'utc'|
    'local'), erstes vorhandenes Feld aus `keys`. Beide Payload-Formen:
    verschachtelte {'utc','local'}-Dicts UND flache '<key>Utc'/'<key>Local'-
    Strings. None wenn nicht im Payload."""
    for k in keys:
        v = (mv or {}).get(k)
        if isinstance(v, dict) and v.get(which):
            return str(v[which]).strip()
        flat = (mv or {}).get(k + ('Utc' if which == 'utc' else 'Local'))
        if flat:
            return str(flat).strip()
    return None


def _adb_ts(s):
    """AeroDataBox-UTC-Zeitstring ('2026-07-04 09:35Z' oder ISO) → Unix-Epoche.
    None bei fehlendem/unparsbarem Wert — nie raten."""
    if not s:
        return None
    from datetime import datetime, timezone
    t = str(s).strip().replace(' ', 'T').replace('Z', '+00:00')
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _adb_local_str(mv, keys):
    """Station-LOKALE Payload-Zeit als ISO-String ('2026-07-04T09:35+01:00')
    oder None. Für die Radar-UI (Owner: Abflug/Ankunft in Ortszeit)."""
    s = _adb_movement_val(mv, keys, 'local')
    return s.replace(' ', 'T') if s else None


def _adb_leg_times(f):
    """Zeit-Prioritäts-Sicht EINES AeroDataBox-Legs (Unix-Epoche, UTC):
    dep/arr je actual > revised/predicted > scheduled. None = im Payload nicht
    vorhanden (Unbekannt bleibt unbekannt)."""
    dep = (f or {}).get('departure') or {}
    arr = (f or {}).get('arrival') or {}
    return {
        'dep_actual': _adb_ts(_adb_movement_val(dep, _ADB_ACTUAL_KEYS, 'utc')),
        'dep_est':    _adb_ts(_adb_movement_val(dep, _ADB_EST_KEYS, 'utc')),
        'dep_sched':  _adb_ts(_adb_movement_val(dep, _ADB_SCHED_KEYS, 'utc')),
        'arr_actual': _adb_ts(_adb_movement_val(arr, _ADB_ACTUAL_KEYS, 'utc')),
        'arr_est':    _adb_ts(_adb_movement_val(arr, _ADB_EST_KEYS, 'utc')),
        'arr_sched':  _adb_ts(_adb_movement_val(arr, _ADB_SCHED_KEYS, 'utc')),
    }


def _adb_flight_to_route(f, cs):
    """AeroDataBox-Flight-Objekt → route-Dict (src/dst IATA+ICAO). None bei Müll.
    Reicht zusätzlich die ECHTEN Payload-Zeiten station-lokal durch (sched_dep/
    sched_arr aus scheduledTime.local, est_dep/est_arr aus actual > revised >
    predicted). Fehlt eine Zeit im Payload, fehlt das Feld — nie erfunden."""
    dep_mv = (f.get('departure') or {})
    arr_mv = (f.get('arrival') or {})
    dep = (dep_mv.get('airport') or {})
    arr = (arr_mv.get('airport') or {})
    src = (dep.get('iata') or '').upper() or None
    dst = (arr.get('iata') or '').upper() or None
    if not src or not dst:
        return None
    r = {
        'src': src, 'src_icao': (dep.get('icao') or '').upper() or None,
        'dst': dst, 'dst_icao': (arr.get('icao') or '').upper() or None,
        'callsign': cs, 'status': f.get('status'),
        'reg': ((f.get('aircraft') or {}).get('reg') or '').upper() or None,
    }
    for key, mv, kinds in (('sched_dep', dep_mv, _ADB_SCHED_KEYS),
                           ('est_dep', dep_mv, _ADB_EST_KEYS),
                           ('sched_arr', arr_mv, _ADB_SCHED_KEYS),
                           ('est_arr', arr_mv, _ADB_EST_KEYS)):
        v = _adb_local_str(mv, kinds)
        if v:
            r[key] = v
    return r


def _adb_pick_active_leg(flights, cs, reg, track, now=None):
    """Aus mehreren AeroDataBox-Flügen (gleiche Nummer/Reg, mehrere Legs am Tag)
    das AKTIVE Leg wählen — Owner-Beweisfoto EZY29CT (flog LGW→SKG, wir zeigten
    das frühere LGW→ACE „bestätigt"): entscheidend ist die ZEIT-PRIORITÄT
    actual > revised/predicted > scheduled AUS DEM BEZAHLTEN PAYLOAD, nicht
    Listen-Reihenfolge oder Status-Strings:
      1. Legs mit actual-Ankunft in der Vergangenheit sind ABGESCHLOSSEN.
      2. Ein Leg OHNE actual-Ankunft bleibt aktiv — auch wenn sched_arr vorbei
         ist (Owner: „nach soll zeit wenn verspätung oder irreg nicht das es
         aus der soll zeit wegfällt").
      3. Haben mehrere offene Legs lt. eigener actual/est-Zeit schon abgehoben,
         gewinnt das SPÄTESTE (das spätere Leg ist der aktuelle Flug).
      4. Ist keins nachweislich abgehoben: das zuletzt fällig gewordene Leg
         (effektive Abflugzeit ≤ jetzt), sonst das nächste bevorstehende.
    Nur wenn der Payload GAR KEINE Zeiten trägt: alter Fallback über Status →
    Kurs-Match (dep→arr-Bearing ~ track ±70°) → erstes Leg (ambiguous).
    Rückgabe (route_dict, ambiguous_bool)."""
    routes = [(f, _adb_flight_to_route(f, cs)) for f in flights]
    routes = [(f, r) for f, r in routes if r]
    if not routes:
        return None, False
    if len(routes) == 1:
        return routes[0][1], False
    reg_u = (reg or '').upper()
    if reg_u:
        rm = [(f, r) for f, r in routes if r.get('reg') == reg_u]
        if len(rm) == 1:
            return rm[0][1], False
        if rm:
            # Mehrere Legs DERSELBEN Maschine (Normalfall bei Kurzstrecken-
            # Rotationen) → NICHT das erste nehmen, die Zeit entscheidet.
            routes = rm
    now = time.time() if now is None else float(now)
    legs = []
    for f, r in routes:
        t = _adb_leg_times(f)
        dep_eff = (t['dep_actual'] if t['dep_actual'] is not None
                   else t['dep_est'] if t['dep_est'] is not None
                   else t['dep_sched'])
        legs.append((f, r, t, dep_eff))
    timed = [x for x in legs if any(v is not None for v in x[2].values())]
    if timed:
        open_legs = [x for x in timed
                     if not (x[2]['arr_actual'] is not None
                             and x[2]['arr_actual'] <= now)]
        if not open_legs:
            # Alle Legs lt. actual-Ankunft abgeschlossen → das zuletzt gelandete
            # (ehrlichster Kandidat; das Geometrie-Gate im Resolver prüft weiter).
            done = max(timed, key=lambda x: x[2]['arr_actual'])
            return done[1], False

        def _dep_ref(x):
            return (x[2]['dep_actual'] if x[2]['dep_actual'] is not None
                    else x[2]['dep_est'])
        airborne = [x for x in open_legs
                    if _dep_ref(x) is not None and _dep_ref(x) <= now]
        if airborne:
            return max(airborne, key=_dep_ref)[1], False
        due = [x for x in open_legs if x[3] is not None and x[3] <= now]
        if due:
            return max(due, key=lambda x: x[3])[1], False
        upcoming = [x for x in open_legs if x[3] is not None]
        if upcoming:
            return min(upcoming, key=lambda x: x[3])[1], False
        # Offene Legs ganz ohne Abflugzeit → Status/Kurs-Fallback nur über sie.
        routes = [(f, r) for f, r, _t, _d in open_legs]
    live = [(f, r) for f, r in routes
            if str(f.get('status') or '').lower() in
            ('enroute', 'en-route', 'departed', 'active', 'boarding', 'expected')]
    pool = live or routes
    if track is not None and len(pool) > 1:
        best, bestd = None, 999
        for f, r in pool:
            a = _airport_row(r.get('src')); b = _airport_row(r.get('dst'))
            if not (a and b and a.get('lat') is not None and b.get('lat') is not None):
                continue
            brg = _bearing_deg(a['lat'], a['lon'], b['lat'], b['lon'])
            if brg is None:
                continue
            d = abs((brg - track + 180) % 360 - 180)
            if d < bestd:
                best, bestd = r, d
        if best is not None and bestd <= 70:
            return best, False
    return pool[0][1], len(pool) > 1


def _aerodatabox_route(cs, reg=None, lat=None, lon=None, track=None, date=None, timeout=7):
    """AeroDataBox (RapidAPI, AERODATABOX_KEY) — die GENAUE Route eines Live-Fluges.
    Reg-gekeyt bevorzugt (an die physische Maschine gebunden → immun gegen
    Flugnummer-Recycling), sonst nummern-gekeyt mit Leg-Disambiguierung.
    Wirft NIE; None bei fehlendem Key, Quota (429) oder keinem Treffer."""
    key = os.environ.get('AERODATABOX_KEY', '')
    if not key:
        return None
    # BEZAHLT + quota-limitiert → harter Tages-Budget-Guard (free-first-Constraint).
    if not _paid_budget_ok():
        return None
    date = date or _today_utc()
    # ZWEI Vertriebskanäle, gleicher Dienst (2026-07-04, Owner-Abo): das
    # api.market-DIREKTPORTAL nutzt kurze cuid-Keys + `x-magicapi-key` und
    # einen anderen Basis-Pfad; RapidAPI die langen Keys + `x-rapidapi-key`.
    # Kanal am Key-Format erkennen — Pfade sind identisch (live verifiziert).
    if len(key) <= 32:
        base = 'https://prod.api.market/api/v1/aedbx/aerodatabox'
        hdr = {'x-magicapi-key': key, 'User-Agent': 'AeroX-DataEngine/1.0'}
    else:
        host = 'aerodatabox.p.rapidapi.com'
        base = f'https://{host}'
        hdr = {'x-rapidapi-key': key, 'x-rapidapi-host': host,
               'User-Agent': 'AeroX-DataEngine/1.0'}

    def _get(path):
        _paid_budget_inc(units=2)   # Flight-Endpoints = Tier 2 (2 Units)
        try:
            req = urllib.request.Request(f'{base}{path}', headers=hdr)
            # INTERAKTIV-Timeout (Owner 2026-07-04 „29 sek für die Route"): der
            # Radar-Tap hängt an dieser Kette. Per `timeout`-Param übergeben (Fast-
            # Path: ~3s); ein wirklich langsamer Anbieter wird gratis vom Warehouse/
            # Crowdsource-Nachtrag beim nächsten Tap gedeckt.
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode('utf-8', 'replace'))
                return d if isinstance(d, list) else []
        except Exception:
            return None      # 429/quota/Netz → still degradieren

    # 1) reg-gekeyt (autoritativ für die physische Maschine)
    if reg:
        flights = _get(f'/flights/reg/{urllib.parse.quote(reg.upper())}/{date}')
        if flights:
            route, amb = _adb_pick_active_leg(flights, cs, reg, track)
            if route:
                route['source'] = 'aerodatabox'
                route['confidence'] = 'estimated' if amb else 'confirmed'
                return route
    # 2) nummern-gekeyt (IATA-Flugnummer aus dem Callsign)
    fn = _callsign_to_iata_flightno(cs)
    if fn:
        flights = _get(f'/flights/number/{urllib.parse.quote(fn)}/{date}')
        if flights:
            route, amb = _adb_pick_active_leg(flights, cs, reg, track)
            if route:
                route['source'] = 'aerodatabox'
                route['confidence'] = 'estimated' if amb else 'confirmed'
                return route
    return None


def _opensky_route(hexid):
    """OpenSky /flights/aircraft — echte beobachtete Ab-/Ankunft aus dem ADS-B-
    Track dieser Maschine (letzte 36h). Braucht OpenSky-Creds (OPENSKY_CLIENT_ID/
    SECRET oder OPENSKY_USERNAME/PASSWORD); anonym = 403 → None (fail-open).
    Wirft NIE. ICAO→IATA über die eigene Airport-Referenz angereichert."""
    if not hexid:
        return None
    try:
        from blueprints.adsb_blueprint import fetch_recent_flight
    except Exception:
        return None
    rec = None
    try:
        rec = fetch_recent_flight(hexid, lookback_hours=36)
    except Exception:
        rec = None
    if not rec:
        return None
    dep_icao = (rec.get('est_departure_icao') or '').upper() or None
    arr_icao = (rec.get('est_arrival_icao') or '').upper() or None
    if not dep_icao and not arr_icao:
        return None

    def _iata(icao):
        if not icao:
            return None
        ap = _airport_row(icao)
        return (ap.get('iata') if ap else None) or None
    route = {
        'src': _iata(dep_icao), 'src_icao': dep_icao,
        'dst': _iata(arr_icao), 'dst_icao': arr_icao,
        'callsign': (rec.get('callsign') or '').strip() or None,
        'source': 'opensky',
        # Beide Enden beobachtet → confirmed. Nur ein Ende (Flug evtl. noch in der
        # Luft, Ziel noch nicht getrackt) → estimated.
        'confidence': 'confirmed' if (dep_icao and arr_icao) else 'estimated',
        # Freshness (Wrong-Flight-Gate): /flights/aircraft schaut 36 h zurück und
        # liefert oft den GERADE ABGESCHLOSSENEN Vor-Flug. last_seen durchreichen,
        # damit der Resolver einen veralteten Kontakt gegen die Live-Zeit prüfen kann.
        '_last_seen': rec.get('last_seen_unix'),
    }
    if not route['src'] and not route['dst']:
        return None
    return route


def _record_resolved_route(cs, reg, route, date=None):
    """Aufgelöste Route in die eigene Warehouse (ax_route_cache) zurückschreiben —
    datums-gekeyt (`CS@YYYYMMDD`, exakte heutige Strecke), reg-gekeyt
    (`REG:<reg>@YYYYMMDD`) UND unter dem nackten Callsign (Rückwärts-Kompat für
    /api/ax/flight + Harvest). So ist derselbe Tap heute gratis, und die eigene
    Routen-DB wächst korrekt aus dem echten Verkehr. Schreibt NICHTS bei
    generischen/leeren Treffern ohne Strecke. Wirft NIE."""
    if not route or not (route.get('src') or route.get('src_icao')):
        return
    date = date or _today_utc()
    dk = date.replace('-', '')
    payload = dict(route)
    payload['resolved_date'] = date
    payload.setdefault('callsign', cs)
    now = _iso_now()
    rows = [
        {'flight': f'{cs}@{dk}', 'payload': payload, 'updated_at': now},
        {'flight': cs, 'payload': payload, 'updated_at': now},
    ]
    if reg:
        rows.append({'flight': f'REG:{reg.upper()}@{dk}',
                     'payload': payload, 'updated_at': now})
    for row in rows:
        _cache_put('ax_route_cache', row)


# ─────────────────────────────────────────────────────────────────────────────
#  PAID-API DAILY BUDGET GUARD  (AeroDataBox + AviationStack)
#  Harter Tages-Deckel (AX_PAID_DAILY_CAP, Default 760 API-Units — Tier-
#  gewichtet, s. _paid_budget_ok; NICHT Requests). Persistiert in
#  ax_api_budget (key='paid:YYYYMMDD') + In-Memory-Safety-Net. Wird NUR aus dem
#  On-Demand-Tap-Pfad angefasst — nie aus dem Poller/Bulk.
# ─────────────────────────────────────────────────────────────────────────────
def _paid_daily_key():
    return 'paid:' + time.strftime('%Y%m%d', time.gmtime())


def _paid_daily_used():
    key = _paid_daily_key()
    used = _MEM_BUDGET.get(key, 0)
    sb = _sb()
    if sb is not None:
        try:
            res = sb.table('ax_api_budget').select('n').eq('month', key).limit(1).execute()
            rows = getattr(res, 'data', None) or []
            if rows:
                used = max(used, int(rows[0].get('n') or 0))
        except Exception:
            pass
    return used


def _paid_budget_ok():
    """True solange heute noch bezahltes Kontingent frei ist. Der Deckel zählt
    seit 2026-07-04 in API-UNITS (AeroDataBox-Tier-Preise: Tier2=2, Tier3=6,
    Tier4=300), nicht mehr in Requests — vorher konnte ein teurer Call blind
    das Monats-HARD-Limit leeren. Default 760 Units/Tag ≈ 95% des 24k-Plans."""
    cap = int(os.environ.get('AX_PAID_DAILY_CAP', '760'))
    return _paid_daily_used() < cap


_BUDGET_RPC_DISABLED = False   # Migration 20260705_budget_increment.sql fehlt → Fallback


def _budget_rpc_add(key, units):
    """ATOMARER Budget-Increment via Postgres-RPC `ax_budget_increment`
    (INSERT … ON CONFLICT … SET n = n + units in EINEM Statement).

    Audit 2026-07-05: das alte read-modify-write (select n → upsert n+units)
    verlor bei parallelen Requests / mehreren Cloud-Run-Instanzen Zählungen —
    AviationStack-Free-Tier (100/Monat) und der AeroDataBox-Tages-Cap konnten
    dadurch real überlaufen. Returns neuer Stand (int) oder None → Caller fällt
    auf den alten Upsert zurück (graceful degrade, solange die Migration noch
    nicht applied ist — gleiches Muster wie ax_open_legs). Wirft nie."""
    global _BUDGET_RPC_DISABLED
    sb = _sb()
    if sb is None or _BUDGET_RPC_DISABLED:
        return None
    try:
        r = sb.rpc('ax_budget_increment',
                   {'p_key': key, 'p_units': max(1, int(units))}).execute()
        d = getattr(r, 'data', None)
        if isinstance(d, list):
            d = d[0] if d else None
        return int(d) if d is not None else None
    except Exception as e:
        msg = str(e)
        if 'PGRST202' in msg or 'Could not find the function' in msg \
                or 'does not exist' in msg:
            _BUDGET_RPC_DISABLED = True
            try:
                print('[aerox_data] ax_budget_increment RPC missing → non-atomic '
                      'upsert fallback (apply supabase_migrations/'
                      '20260705_budget_increment.sql for atomic counters)', flush=True)
            except Exception:
                pass
        return None


def _paid_budget_inc(units=1):
    key = _paid_daily_key()
    used = _MEM_BUDGET.get(key, 0) + max(1, int(units))
    _MEM_BUDGET[key] = used          # In-Memory IMMER zählen (Safety-Net)
    sb = _sb()
    if sb is None:
        return
    # Bevorzugt ATOMAR (Audit 2026-07-05, s. _budget_rpc_add) — der RPC-Stand
    # ist die instanzübergreifende Wahrheit und synct den Memory-Zähler mit.
    n = _budget_rpc_add(key, units)
    if n is not None:
        _MEM_BUDGET[key] = max(used, n)
        return
    try:
        sb.table('ax_api_budget').upsert(
            {'month': key, 'n': max(used, _paid_daily_used()),
             'updated_at': _iso_now()}).execute()
    except Exception:
        pass


# ── Generische Budget-Key-Helper (für andere Blueprints, z.B. adsb_blueprint
#    Tier-3 'adb_position'). GLEICHER Mechanismus wie der Paid-Guard oben
#    (In-Memory-Safety-Net + ax_api_budget + atomarer ax_budget_increment-RPC),
#    nur key-parametrisiert — KEIN Umbau der bestehenden Paid-Pfade. ──────────
def _budget_key_used(key):
    """Aktueller Stand eines beliebigen Budget-Keys (max aus In-Memory und
    ax_api_budget). Wirft NIE; SB down → In-Memory-Stand dieser Instanz."""
    used = _MEM_BUDGET.get(key, 0)
    sb = _sb()
    if sb is not None:
        try:
            res = sb.table('ax_api_budget').select('n').eq('month', key).limit(1).execute()
            rows = getattr(res, 'data', None) or []
            if rows:
                used = max(used, int(rows[0].get('n') or 0))
        except Exception:
            pass
    return used


def _budget_key_inc(key, units=1):
    """Increment eines beliebigen Budget-Keys — bevorzugt ATOMAR via
    ax_budget_increment-RPC (s. _budget_rpc_add), sonst Upsert-Fallback.
    Identische Semantik wie _paid_budget_inc, nur key-parametrisiert.
    Wirft NIE."""
    used = _MEM_BUDGET.get(key, 0) + max(1, int(units))
    _MEM_BUDGET[key] = used          # In-Memory IMMER zählen (Safety-Net)
    sb = _sb()
    if sb is None:
        return
    n = _budget_rpc_add(key, units)
    if n is not None:
        _MEM_BUDGET[key] = max(used, n)
        return
    try:
        sb.table('ax_api_budget').upsert(
            {'month': key, 'n': max(used, _budget_key_used(key)),
             'updated_at': _iso_now()}).execute()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  FR24 OFFICIAL API (bezahlt) — LETZTER Fallback hinter Warehouse/Boards/gRPC.
#  Owner-Direktive 2026-07-09: freies Scraping bleibt Hauptquelle; die bezahlte
#  API NUR für Lücken, und JEDER Credit wird permanent gespeichert (via
#  _crowdsource_flight_obs ins Warehouse → derselbe Flug kommt nächstes Mal GRATIS).
#  Kosten (gemessen): flight-summary/light = günstig (~2 Credits); live/flight-
#  positions = TEUER (~120 pro Box) → wird hier NICHT für die Karte benutzt.
#  Harter Tages-Credit-Deckel FR24_DAILY_CREDIT_CAP schützt vor Ausreißern.
# ─────────────────────────────────────────────────────────────────────────────
_FR24_BASE = 'https://fr24api.flightradar24.com/api'
_FR24_SUMMARY_CREDITS = 2          # flight-summary/light Basiskosten pro Call


def _fr24_token():
    return (os.environ.get('FR24_API_TOKEN') or '').strip()


def _fr24_available():
    return bool(_fr24_token())


def _fr24_budget_key():
    return 'fr24:' + time.strftime('%Y%m%d', time.gmtime())


def _fr24_month_budget_key():
    return 'fr24m:' + time.strftime('%Y%m', time.gmtime())


def _fr24_budget_inc(units):
    """FR24-Credits buchen: Tages- UND Monats-Zähler (Zweitschlüssel) — der
    Monatsdeckel (FR24_MONTHLY_CREDIT_CAP) schützt gegen viele „fast volle" Tage."""
    _budget_key_inc(_fr24_budget_key(), units)
    _budget_key_inc(_fr24_month_budget_key(), units)


_FR24_CAP_WARNED = {}   # 'YYYYMMDD' → True — Cap-Hit-Warnung einmalig pro Tag


def _fr24_budget_ok():
    """True solange heute (Tages-Cap, Default 8000 ≈ 240k/Monat) UND im laufenden
    Monat (FR24_MONTHLY_CREDIT_CAP, Default 200000 < Essential-333k) noch
    FR24-Credit-Kontingent frei ist. Beim ERSTEN Cap-Hit des Tages einmalig
    logger.warning — der Deckel griff bisher lautlos."""
    try:
        cap = int(os.environ.get('FR24_DAILY_CREDIT_CAP', '8000'))
    except Exception:
        cap = 8000
    try:
        mcap = int(os.environ.get('FR24_MONTHLY_CREDIT_CAP', '200000'))
    except Exception:
        mcap = 200000
    day_used = _budget_key_used(_fr24_budget_key())
    month_used = _budget_key_used(_fr24_month_budget_key())
    ok = day_used < cap and month_used < mcap
    if not ok:
        today = time.strftime('%Y%m%d', time.gmtime())
        if not _FR24_CAP_WARNED.get(today):
            _FR24_CAP_WARNED.clear()          # alte Tage raus, Flag bleibt winzig
            _FR24_CAP_WARNED[today] = True
            _log.warning(
                'FR24-Budget-Deckel erreicht: heute %s/%s Credits, Monat %s/%s '
                '— bezahlte FR24-Fallbacks pausieren bis zum Reset.',
                day_used, cap, month_used, mcap)
    return ok


def _fr24_get(path, params):
    """Ein GET gegen die FR24-API (Bearer-Token, Accept-Version v1). None bei
    fehlendem Token / non-200 / Fehler — Caller degradiert ehrlich. Wirft nie."""
    tok = _fr24_token()
    if not tok:
        return None
    try:
        import requests
    except Exception:
        return None
    try:
        r = requests.get(_FR24_BASE + path, params=params,
                         headers={'Authorization': 'Bearer ' + tok,
                                  'Accept': 'application/json',
                                  'Accept-Version': 'v1'},
                         timeout=12)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _fr24_hyphenate_reg(reg):
    """Warehouse-Tail (evtl. ohne Bindestrich, z.B. 'DAINV') → FR24-Schreibweise
    'D-AINV'. Hat der Input schon einen Bindestrich, unverändert lassen. Best-
    effort: Einzelbuchstaben-Präfix (D/G/F/…) bekommt den Bindestrich nach dem 1.
    Zeichen; 2-Buchstaben-Länder (HB-, 9H-) sind selten in der LH-Group-Crew-
    Nutzung und fallen sonst auf den Roh-String zurück."""
    r = (reg or '').strip().upper()
    if not r:
        return None
    if '-' in r:
        return r
    if len(r) >= 3 and r[0].isalpha():
        return r[0] + '-' + r[1:]
    return r


def _fr24_summary_to_leg(f):
    """FR24 flight-summary/light-Record → Leg-/Karten-Dict im Warehouse-Schema
    (kompatibel mit tail-history-Legs UND _crowdsource_flight_obs). ICAO→IATA;
    Ist-Ab/-Ankunft (datetime_takeoff/landed, absolut-UTC) als sched_dep/sched_arr;
    Dauer via _sched_block_min. dest_icao_actual = echtes (evtl. umgeleitetes) Ziel."""
    if not isinstance(f, dict):
        return None
    src = _icao_to_iata((f.get('orig_icao') or '').upper())
    dst_icao = (f.get('dest_icao_actual') or f.get('dest_icao') or '').upper()
    dst = _icao_to_iata(dst_icao)
    tko = f.get('datetime_takeoff') or None
    ldg = f.get('datetime_landed') or None
    sbm = _life_app('_sched_block_min')
    dur = None
    if sbm and tko and ldg and src and dst:
        try:
            dur = sbm(tko, src, ldg, dst)
        except Exception:
            dur = None
    reg = re.sub(r'[^A-Z0-9]', '', (f.get('reg') or '').upper()) or None
    diverted = bool(f.get('dest_icao_actual')
                    and f.get('dest_icao_actual') != f.get('dest_icao'))
    return {
        'flight_no': f.get('flight'), 'flight': f.get('flight'),
        # ECHTER Funkname (LH fliegt LH1412 als „DLH8UA", nicht „DLH1412") — den
        # braucht iOS für die Live-Position (adsb.lol nach Callsign) und die
        # korrekte Identität.
        'callsign': (f.get('callsign') or '').upper() or None,
        'src': src, 'dst': dst, 'dep_iata': src, 'arr_iata': dst,
        'day': (tko or '')[:10] or None,
        'sched_dep': tko, 'sched_arr': ldg, 'duration_min': dur,
        'status': ('landed' if f.get('flight_ended') in (True, 'true') else None),
        'reg': reg, 'type': f.get('type'), 'aircraft': f.get('type'),
        'diverted': diverted, 'source': 'fr24',
    }


_FR24_REG_CACHE = {}               # reg → (ts, legs) — Zero-Double-Spend-Schutz
_FR24_REG_TTL = 6 * 3600.0         # Tail-Historie ändert sich langsam → 6 h


def _fr24_cache_evict():
    """LRU-artig: älteste 25 % raus statt clear() (Muster _memo_put) — ein voller
    Flush hätte alle Hard-Cache-Treffer verworfen = erneutes Credit-Spending."""
    if len(_FR24_REG_CACHE) <= 500:
        return
    try:
        items = sorted(_FR24_REG_CACHE.items(), key=lambda kv: kv[1][0])
        for k, _v in items[:len(items) // 4 or 1]:
            _FR24_REG_CACHE.pop(k, None)
    except Exception:
        _FR24_REG_CACHE.clear()


def _fr24_flights_by_reg(reg, days=7, limit=12):
    """Letzte Legs EINER Maschine über die FR24-flight-summary (by registration).
    Bezahlter LETZTER Fallback für die Kennzeichen-Historie, wenn das Warehouse
    die Maschine nie gesehen hat (z.B. D-AIXS). Budget-gated; leere Liste bei
    kein-Token/Budget-aus/kein-Treffer. Credits werden nach dem Call gezählt.

    HARD-CACHE (Owner „alles was ein Credit kostet, hart speichern"): dasselbe
    Kennzeichen kostet innerhalb von 6 h nur EINMAL Credits — jeder weitere
    Lookup (auch von anderen Usern) kommt aus dem Cache."""
    reg_q = _fr24_hyphenate_reg(reg)
    if not reg_q or len(reg_q.replace('-', '')) < 3:
        return []
    ck = reg_q + '|' + str(days) + '|' + str(limit)
    hit = _FR24_REG_CACHE.get(ck)
    if hit and (time.time() - hit[0]) < _FR24_REG_TTL:
        return list(hit[1])
    if not (_fr24_available() and _fr24_budget_ok()):
        return []
    now = time.time()
    j = _fr24_get('/flight-summary/light', {
        'registrations': reg_q,
        'flight_datetime_from': time.strftime('%Y-%m-%dT%H:%M:%S',
                                              time.gmtime(now - days * 86400)),
        'flight_datetime_to': time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(now)),
    })
    data = (j or {}).get('data') or []
    # FR24 zählt Credits PRO Ergebnis (nicht pauschal) → nach Trefferzahl buchen
    # (mind. Basis auch bei leer, weil der Call trotzdem kostet).
    _fr24_budget_inc(max(_FR24_SUMMARY_CREDITS, len(data)))
    legs = []
    for f in data:
        leg = _fr24_summary_to_leg(f)
        if leg and leg.get('flight_no') and leg.get('src') and leg.get('dst'):
            legs.append(leg)
    legs.sort(key=lambda l: l.get('sched_dep') or '', reverse=True)
    out = legs[:limit]
    # Negativ-Cache inklusive: auch ein leeres Ergebnis wird gecacht (der Call
    # kostete trotzdem) → keine Wiederholung für dieselbe Maschine.
    _FR24_REG_CACHE[ck] = (time.time(), list(out))
    _fr24_cache_evict()
    return out


def _fr24_flight_by_number(flight_no, date=None):
    """EIN Flug (Route/Typ/Reg/Ist-Zeiten) über FR24-flight-summary by number —
    bezahlter Fallback für flight_status, wenn frei (Warehouse/Boards/gRPC) nichts
    hat. Nimmt den jüngsten Treffer im Fenster (heute bzw. ±36 h). Liefert ein
    Dict im flight_status-Schema oder None. Hart gecacht (Zero-Double-Spend);
    Credits nach dem Call gezählt. KEIN Gate/Sollzeit (die kennt flight-summary
    nicht — dafür bleiben Board/Warehouse zuständig)."""
    fn = (flight_no or '').replace(' ', '').upper()
    if len(fn) < 3:
        return None
    # Cache-Key IMMER mit Datum (date=None → heutiger UTC-Tag): flight_status
    # (mit Datum) und resolve-flight (ohne) treffen so denselben Key — vorher
    # divergierten die Keys und derselbe Flug kostete zweimal Credits.
    ck = 'FN|' + fn + '|' + ((date or '')[:10]
                             or time.strftime('%Y-%m-%d', time.gmtime()))
    hit = _FR24_REG_CACHE.get(ck)
    if hit and (time.time() - hit[0]) < _FR24_REG_TTL:
        return hit[1]
    if not (_fr24_available() and _fr24_budget_ok()):
        return None
    if date:
        dt_from, dt_to = date + 'T00:00:00', date + 'T23:59:59'
    else:
        now = time.time()
        dt_from = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(now - 36 * 3600))
        dt_to = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(now))
    j = _fr24_get('/flight-summary/light',
                  {'flights': fn, 'flight_datetime_from': dt_from,
                   'flight_datetime_to': dt_to})
    _data = (j or {}).get('data') or []
    _fr24_budget_inc(max(_FR24_SUMMARY_CREDITS, len(_data)))
    legs = []
    for f in _data:
        leg = _fr24_summary_to_leg(f)
        if leg and leg.get('src') and leg.get('dst'):
            legs.append(leg)
    legs.sort(key=lambda l: l.get('sched_dep') or '', reverse=True)
    out = None
    if legs:
        l = legs[0]
        out = {
            'flight': l['flight_no'], 'callsign': l.get('callsign'),
            'airline': '', 'airline_name': '',
            'dep_iata': l['src'], 'dep_name': '', 'arr_iata': l['dst'], 'arr_name': '',
            'sched_dep': l['sched_dep'], 'sched_arr': l['sched_arr'],
            'est_dep': None, 'est_arr': None, 'duration_min': l['duration_min'],
            'dep_gate': '', 'dep_terminal': '', 'arr_gate': '', 'arr_terminal': '',
            'arr_baggage': '', 'status': l['status'] or '',
            'status_category': (l['status'] or ''),
            'aircraft': l['type'], 'reg': l['reg'],
            'dep_delay_min': None, 'arr_delay_min': None,
            'delay_min': None, 'delay_side': None, 'diverted': l.get('diverted'),
        }
    _FR24_REG_CACHE[ck] = (time.time(), out)
    _fr24_cache_evict()
    return out


def _fr24_flight_by_callsign(callsign, date=None):
    """EIN Flug über FR24-flight-summary by CALLSIGN (ICAO, z.B. OCN601/DLH7AV) —
    die WAHRHEIT für die tatsächlich fliegende Maschine (Owner 2026-07-09: bei
    Discover ist die Callsign-Nummer ≠ IATA-Nummer, „4Y601" ≠ Callsign „OCN601"/
    IATA „4Y60"; die App verwechselte den Flug). Der Funkname ist der eindeutige
    Identifikator → hier holen wir Route/Reg/Typ/echte Flugnummer dazu. Gleiches
    flight_status-Schema + Hard-Cache wie _fr24_flight_by_number."""
    cs = (callsign or '').replace(' ', '').upper()
    if len(cs) < 3:
        return None
    # Datums-normalisierter Key wie bei _fr24_flight_by_number (kein Key-Split
    # zwischen mit-/ohne-Datum-Callern desselben Tages).
    ck = 'CS|' + cs + '|' + ((date or '')[:10]
                             or time.strftime('%Y-%m-%d', time.gmtime()))
    hit = _FR24_REG_CACHE.get(ck)
    if hit and (time.time() - hit[0]) < _FR24_REG_TTL:
        return hit[1]
    if not (_fr24_available() and _fr24_budget_ok()):
        return None
    if date:
        dt_from, dt_to = date + 'T00:00:00', date + 'T23:59:59'
    else:
        now = time.time()
        dt_from = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(now - 36 * 3600))
        dt_to = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(now))
    j = _fr24_get('/flight-summary/light',
                  {'callsigns': cs, 'flight_datetime_from': dt_from,
                   'flight_datetime_to': dt_to})
    _data = (j or {}).get('data') or []
    _fr24_budget_inc(max(_FR24_SUMMARY_CREDITS, len(_data)))
    legs = []
    for f in _data:
        leg = _fr24_summary_to_leg(f)
        if leg and leg.get('src') and leg.get('dst'):
            legs.append(leg)
    legs.sort(key=lambda l: l.get('sched_dep') or '', reverse=True)
    out = None
    if legs:
        l = legs[0]
        out = {
            'flight': l['flight_no'], 'callsign': cs, 'airline': '', 'airline_name': '',
            'dep_iata': l['src'], 'dep_name': '', 'arr_iata': l['dst'], 'arr_name': '',
            'sched_dep': l['sched_dep'], 'sched_arr': l['sched_arr'],
            'est_dep': None, 'est_arr': None, 'duration_min': l['duration_min'],
            'dep_gate': '', 'dep_terminal': '', 'arr_gate': '', 'arr_terminal': '',
            'arr_baggage': '', 'status': l['status'] or '',
            'status_category': (l['status'] or ''),
            'aircraft': l['type'], 'reg': l['reg'],
            'dep_delay_min': None, 'arr_delay_min': None,
            'delay_min': None, 'delay_side': None, 'diverted': l.get('diverted'),
        }
    _FR24_REG_CACHE[ck] = (time.time(), out)
    _fr24_cache_evict()
    return out


def _fr24_flights_by_airline(icao, days=2, chunk_hours=2, max_chunks=40,
                             t_from=None, cover_out=None):
    """ALLE Flüge EINER Airline (operating_as=ICAO) über flight-summary — für den
    Warehouse-Prewarm (Owner „alle von Discover ins Buch speichern"): einmal
    bezahlt holen, permanent speichern, danach gratis nachschlagbar.

    FR24 flight-summary/light gibt PRO Abruf max. ~20 Treffer (kein Pagination-
    Cursor). Darum blättern wir über kleine Zeitfenster (chunk_hours) und
    deduplizieren per fr24_id → so kommen wirklich ALLE Flüge rein. Budget-gated
    (pro Chunk geprüft, Abbruch bei Deckel), Credits PRO Ergebnis. Nur Legs mit
    echter Reg (crowdsourcebar). Kein Cache (Prewarm soll frisch holen).

    t_from: Fenster-Start (Epoch) — der Prewarm-Endpoint reicht hier seine
    persistierte Watermark rein, damit wiederholte Cron-Läufe schon bezahlte
    Zeitfenster NICHT nochmal holen (zero-double-spend). cover_out (dict):
    trägt nach dem Lauf 'to' = letzte VOLLSTÄNDIG abgedeckte Fenstergrenze
    (Epoch) — auch bei Budget-Abbruch mittendrin korrekt resumierbar."""
    if not (_fr24_available() and _fr24_budget_ok()):
        return []
    ic = (icao or '').strip().upper()
    if len(ic) < 2:
        return []
    now = time.time()
    t = now - days * 86400
    if t_from:
        t = max(t, float(t_from))
    step = max(1, int(chunk_hours)) * 3600
    seen, out, chunks = set(), [], 0
    while t < now and chunks < max_chunks:
        if not _fr24_budget_ok():
            break
        t_to = min(t + step, now)
        j = _fr24_get('/flight-summary/light', {
            'operating_as': ic,
            'flight_datetime_from': time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(t)),
            'flight_datetime_to': time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(t_to)),
        })
        data = (j or {}).get('data') or []
        _fr24_budget_inc(max(_FR24_SUMMARY_CREDITS, len(data)))
        for f in data:
            fid = (f.get('fr24_id')
                   or (str(f.get('flight')) + '|' + str(f.get('datetime_takeoff'))))
            if fid in seen:
                continue
            seen.add(fid)
            leg = _fr24_summary_to_leg(f)
            if (leg and leg.get('flight_no') and leg.get('src')
                    and leg.get('dst') and leg.get('reg')):
                out.append(leg)
        t = t_to
        chunks += 1
        if isinstance(cover_out, dict):
            cover_out['to'] = t
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  SELF-COMPUTED ROUTES FROM OWN POLLED ADS-B  —  the long-term FREE data engine
#
#  Wir pollen ohnehin Live-Positionen (adsb.lol via /api/adsb/area, OpenSky-bbox
#  im /api/adsb/poll). observe_adsb_positions() bekommt diese Rows und baut daraus
#  GRATIS echte Ab-/Anflug-Legs: pro Hex eine kleine State-Machine —
#    · am Boden am Flughafen X   → merken (phase=ground, airport=X)
#    · danach abgehoben          → Abflug erkannt: dep=X (phase=air)
#    · später am Boden am Fh. Y  → Ankunft erkannt → Leg X→Y in ax_route_cache
#  Der nächste Flughafen wird über die gebackene Airports-DB (85k) per Bounding-
#  Box + Haversine (≤ ~6 km) bestimmt. So füllt sich die eigene Routen-DB weltweit
#  aus Verkehr, den wir eh schon geladen haben. Best-effort, wirft NIE.
# ─────────────────────────────────────────────────────────────────────────────
_TRACK_STATE = {}                 # hex → {phase, airport, airport_icao, dep, dep_icao, callsign, reg, ts}
_TRACK_LOCK = threading.Lock()
_TRACK_MAX = 5000                 # Cap der In-Memory-Tracks (evict-oldest)
_SELFCOMPUTE_LOW_ALT_FT = 8000    # nur Boden-/Tiefflieger auf Flughäfen snappen

# ── DURABLE OPEN-LEG STORE (übersteht Cloud-Run-Instanz-Recycling) ───────────
#  Das In-Memory _TRACK_STATE geht bei jedem Container-Restart verloren → ein vor
#  dem Restart gestarteter (v.a. Langstrecken-)Flug verliert seinen Abflug und die
#  Landung wird nie zu einem Leg. Deshalb ist der OFFENE Leg (hex hat „von X
#  abgehoben, fliegt noch") in Supabase `ax_open_legs` gespiegelt = Source of Truth.
#  _TRACK_STATE bleibt ein schneller LRU-Front-Cache davor. Degradiert sauber:
#  fehlt die Tabelle (PGRST205) → In-Memory-Fallback + genau EINE Warnung, exakt
#  wie die anderen Fallbacks. Best-effort, wirft NIE.
_OPEN_LEGS_TABLE = 'ax_open_legs'
_OPEN_LEGS_SB_OK = None            # None=ungetestet · True=Tabelle da · False=fehlt→mem-only
_OPEN_LEGS_WARNED = False
_OPEN_LEG_STALE_S = 20 * 3600      # dep_ts älter → Flug verloren/nie gelandet → evicten
_LAST_OPEN_LEG_SWEEP = 0.0         # letzte Stale-Eviction (höchstens 1×/h)


def _iso_from_ts(ts):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(ts))


def _parse_ts(s):
    """ISO-ish Timestamp → epoch seconds. Best-effort; now() bei Müll."""
    if not s:
        return time.time()
    try:
        import calendar
        s2 = str(s).replace('Z', '').split('.')[0].split('+')[0].strip().replace('T', ' ')
        return calendar.timegm(time.strptime(s2, '%Y-%m-%d %H:%M:%S'))
    except Exception:
        return time.time()


def _open_legs_note_error(e):
    """Fehlt die Tabelle → auf In-Memory umschalten + genau EINMAL warnen (wie die
    anderen PGRST205-Fallbacks). Andere Fehler still schlucken (best-effort)."""
    global _OPEN_LEGS_SB_OK, _OPEN_LEGS_WARNED
    msg = str(e)
    if 'PGRST205' in msg or 'schema cache' in msg or 'Could not find the table' in msg:
        _OPEN_LEGS_SB_OK = False
        if not _OPEN_LEGS_WARNED:
            _OPEN_LEGS_WARNED = True
            try:
                print('[aerox_data] ax_open_legs missing → in-memory open-leg fallback '
                      '(apply supabase_migrations/20260702_open_legs.sql for '
                      'restart-durable self-computed legs)', flush=True)
            except Exception:
                pass


def _open_leg_get(hexid):
    """Offener Leg für hex: erst Front-Cache (_TRACK_STATE, phase=air+dep), sonst
    Supabase (Source of Truth → übersteht Restarts). None wenn keiner offen."""
    global _OPEN_LEGS_SB_OK
    st = _TRACK_STATE.get(hexid)
    if st and st.get('phase') == 'air' and st.get('dep'):
        return st
    if _OPEN_LEGS_SB_OK is False:
        return None
    sb = _sb()
    if sb is None:
        return None
    try:
        res = sb.table(_OPEN_LEGS_TABLE).select('*').eq('hex', hexid).limit(1).execute()
        rows = getattr(res, 'data', None) or []
    except Exception as e:
        _open_legs_note_error(e)
        return None
    _OPEN_LEGS_SB_OK = True
    if not rows:
        return None
    r = rows[0]
    if not r.get('origin_iata'):
        return None
    leg = {'phase': 'air', 'dep': r.get('origin_iata'),
           'dep_icao': r.get('origin_icao'),
           'callsign': r.get('callsign'), 'reg': r.get('reg'),
           'dep_ts': _parse_ts(r.get('dep_ts')), 'ts': time.time()}
    _TRACK_STATE[hexid] = leg          # Front-Cache wärmen
    return leg


def _open_leg_put(hexid, origin_iata, origin_icao, cs, reg, lat, lon, alt, dep_ts=None):
    """Abflug erkannt → offenen Leg upserten (Cache + Supabase). Idempotent (PK=hex).
    Genau EIN kleiner Upsert pro erkanntem Abflug — nie für Reiseflug."""
    global _OPEN_LEGS_SB_OK
    dep_ts = dep_ts or time.time()
    _TRACK_STATE[hexid] = {'phase': 'air', 'dep': origin_iata, 'dep_icao': origin_icao,
                           'callsign': cs, 'reg': reg, 'dep_ts': dep_ts, 'ts': time.time()}
    if _OPEN_LEGS_SB_OK is False:
        return
    sb = _sb()
    if sb is None:
        return
    try:
        sb.table(_OPEN_LEGS_TABLE).upsert({
            'hex': hexid, 'origin_iata': origin_iata, 'origin_icao': origin_icao,
            'dep_ts': _iso_from_ts(dep_ts), 'last_lat': lat, 'last_lon': lon,
            'last_alt': alt, 'last_seen': _iso_now(), 'callsign': cs, 'reg': reg,
        }, on_conflict='hex').execute()
        _OPEN_LEGS_SB_OK = True
    except Exception as e:
        _open_legs_note_error(e)


def _open_leg_delete(hexid):
    """Landung verbucht → offenen Leg schliessen (Cache + Supabase). Wirft NIE."""
    _TRACK_STATE.pop(hexid, None)
    if _OPEN_LEGS_SB_OK is False:
        return
    sb = _sb()
    if sb is None:
        return
    try:
        sb.table(_OPEN_LEGS_TABLE).delete().eq('hex', hexid).execute()
    except Exception as e:
        _open_legs_note_error(e)


def _open_legs_evict_stale():
    """Verwaiste offene Legs (dep_ts > ~20h alt = Flug verloren/nie in Sicht
    gelandet) räumen. Höchstens 1×/h, im Cache UND in Supabase. Wirft NIE."""
    global _LAST_OPEN_LEG_SWEEP
    now = time.time()
    if now - _LAST_OPEN_LEG_SWEEP < 3600:
        return
    _LAST_OPEN_LEG_SWEEP = now
    cutoff = now - _OPEN_LEG_STALE_S
    try:
        with _TRACK_LOCK:
            for k, v in list(_TRACK_STATE.items()):
                if v.get('phase') == 'air' and \
                        (v.get('dep_ts') or v.get('ts') or now) < cutoff:
                    _TRACK_STATE.pop(k, None)
    except Exception:
        pass
    if _OPEN_LEGS_SB_OK is False:
        return
    sb = _sb()
    if sb is None:
        return
    try:
        sb.table(_OPEN_LEGS_TABLE).delete().lt('dep_ts', _iso_from_ts(cutoff)).execute()
    except Exception as e:
        _open_legs_note_error(e)


def _haversine_km(lat1, lon1, lat2, lon2):
    import math
    try:
        r = 6371.0
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp = math.radians(lat2 - lat1)
        dl = math.radians(lon2 - lon1)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * r * math.asin(min(1.0, math.sqrt(a)))
    except Exception:
        return 9e9


def _nearest_airport(lat, lon, max_km=6.0):
    """Nächster IATA-Flughafen zu (lat,lon) innerhalb max_km — Bounding-Box-Query
    auf der gebackenen Airports-DB + Haversine-Feinauswahl. None wenn keiner in
    Reichweite."""
    if lat is None or lon is None:
        return None
    import math
    dlat = max_km / 111.0
    dlon = max_km / (111.0 * max(0.15, math.cos(math.radians(lat))))
    rows = _q("SELECT iata, icao, lat, lon, name FROM airports "
              "WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? "
              "AND iata IS NOT NULL AND iata != '' "
              "AND lat IS NOT NULL AND lon IS NOT NULL",
              (lat - dlat, lat + dlat, lon - dlon, lon + dlon))
    best, bestd = None, max_km
    for r in rows:
        d = _haversine_km(lat, lon, r['lat'], r['lon'])
        if d < bestd:
            best, bestd = r, d
    return best


def _obs_is_grounded(row):
    """Heuristik: am Boden? on_ground-Flag zuerst; sonst sehr tief + langsam."""
    if row.get('on_ground') is True:
        return True
    alt = row.get('alt')
    spd = row.get('speed')
    if alt is not None and alt <= 200 and (spd is None or spd < 60):
        return True
    return False


def _maybe_evict_tracks():
    if len(_TRACK_STATE) <= _TRACK_MAX:
        return
    try:
        with _TRACK_LOCK:
            items = sorted(_TRACK_STATE.items(), key=lambda kv: kv[1].get('ts', 0))
            for k, _v in items[:max(1, len(items) // 5)]:
                _TRACK_STATE.pop(k, None)
    except Exception:
        pass


def observe_adsb_positions(rows, max_process=400):
    """Self-computed-route-Engine. Rows = normalisierte Live-Positionen (hex,
    callsign/flight, reg, lat, lon, alt, speed, on_ground). Erkennt Ab-/Anflug
    per Hex-State-Machine und schreibt fertige Legs GRATIS in ax_route_cache
    (source='aerox_adsb', confidence='confirmed'). Gibt die Anzahl neuer Legs
    zurück. Never raises."""
    if not rows:
        return 0
    to_record = []
    processed = 0
    now = time.time()
    for row in rows:
        if processed >= max_process:
            break
        try:
            hexid = (row.get('hex') or '').strip().lower()
            if not hexid:
                continue
            lat, lon = row.get('lat'), row.get('lon')
            if lat is None or lon is None:
                continue
            alt = row.get('alt')
            grounded = _obs_is_grounded(row)
            low = (alt is not None and alt <= _SELFCOMPUTE_LOW_ALT_FT)
            cs = (row.get('callsign') or row.get('flight') or '').strip().upper() or None
            reg = (row.get('reg') or '').strip().upper() or None

            # Reiseflug (hoch, nicht am Boden) → billiger Skip: keine Airports-DB,
            # keine Supabase. Ein offener Leg für diesen hex bleibt unangetastet in
            # ax_open_legs liegen (wird erst bei der Landung geschlossen). GENAU das
            # macht die Engine restart-fest: der offene Leg lebt NICHT im RAM.
            if not grounded and not low:
                continue

            # Nur Boden-/Tiefflieger die Airports-DB anfassen lassen.
            processed += 1
            near = _nearest_airport(lat, lon)

            if grounded:
                if not near:
                    continue    # am Boden fern jedes Flughafens → Zustand halten
                ap = (near.get('iata') or '').upper() or None
                ap_icao = (near.get('icao') or '').upper() or None
                mem = _TRACK_STATE.get(hexid)
                # Schon als „am Boden hier" bekannt → nichts tun (KEIN Supabase-Read
                # pro Tick für parkende Flieger). Nur Zeitstempel frisch halten.
                if mem and mem.get('phase') == 'ground' and mem.get('airport') == ap:
                    mem['ts'] = now
                    continue
                # Frische Landung ODER erste Sichtung am Boden nach Restart → GENAU
                # EIN Open-Leg-Read (Cache→Supabase). Offener Leg mit dep ≠ hier → Leg.
                leg = _open_leg_get(hexid)
                if leg and leg.get('dep') and leg['dep'] != ap:
                    to_record.append({
                        'route': {
                            'src': leg['dep'], 'src_icao': leg.get('dep_icao'),
                            'dst': ap, 'dst_icao': ap_icao,
                            'callsign': leg.get('callsign') or cs,
                            'source': 'aerox_adsb', 'confidence': 'confirmed',
                        },
                        'cs': leg.get('callsign') or cs,
                        'reg': leg.get('reg') or reg,
                        'hex': hexid,
                        # AUFGABE-2: volle Leg-Zeiten für die IST-Zeit-Warehouse-Rows.
                        'dep': leg['dep'], 'dep_icao': leg.get('dep_icao'),
                        'arr': ap, 'arr_icao': ap_icao,
                        'dep_ts': leg.get('dep_ts'), 'arr_ts': now,
                    })
                elif leg:
                    # Offener Leg, aber dep==hier oder unbekannt → verwerfen (kein Leg).
                    _open_leg_delete(hexid)
                # Am Boden hier gemerkt (In-Memory reicht; Ground-Phase ist kurzlebig).
                _TRACK_STATE[hexid] = {
                    'phase': 'ground', 'airport': ap, 'airport_icao': ap_icao,
                    'callsign': cs, 'reg': reg, 'ts': now}
            else:
                # In der Luft & tief (An-/Abflughöhe).
                mem = _TRACK_STATE.get(hexid)
                if mem and mem.get('phase') == 'ground' and mem.get('airport') and near:
                    # Abflug-Kante (ground→air): offenen Leg von diesem Flughafen öffnen.
                    _open_leg_put(hexid, mem['airport'], mem.get('airport_icao'),
                                  mem.get('callsign') or cs, mem.get('reg') or reg,
                                  lat, lon, alt)
                elif mem and mem.get('phase') == 'air' and mem.get('dep'):
                    # Bereits offener Leg (im Cache) → nur Metadaten füllen, KEIN Write.
                    if cs and not mem.get('callsign'):
                        mem['callsign'] = cs
                    if reg and not mem.get('reg'):
                        mem['reg'] = reg
                    mem['ts'] = now
                elif near:
                    # Kein Cache-Zustand (Erst-Sichtung / nach Restart) & tief am
                    # Flughafen. Existiert in Supabase noch ein offener Leg? → wärmen.
                    # Sonst: „first-seen already-climbing low near X" → Abflug X öffnen
                    # (restart-fest). Fehlgriff bei einem Anflug ist selbstheilend:
                    # origin=X, landet an X → dep==dst → kein Leg (oben verworfen).
                    leg = _open_leg_get(hexid)
                    if not (leg and leg.get('dep')):
                        ap = (near.get('iata') or '').upper() or None
                        if ap:
                            _open_leg_put(hexid, ap, (near.get('icao') or '').upper() or None,
                                          cs, reg, lat, lon, alt)
                # tief, aber kein Flughafen in der Nähe & kein Zustand → ignorieren.
        except Exception:
            continue
    recorded = 0
    for item in to_record:
        try:
            if item['cs'] and item['route'].get('src') and item['route'].get('dst'):
                _record_resolved_route(item['cs'], item['reg'], item['route'])
                _open_leg_delete(item['hex'])     # Leg geschlossen → offenen Zustand löschen
                recorded += 1
                # AUFGABE-2: selbst-beobachtete Ist-Zeiten (Ab-/Anflug) zusätzlich
                # nach airport_delay_obs — delay UNBEKANNT (kein Fake-„pünktlich").
                try:
                    _warehouse_write_leg_obs(
                        item.get('dep'), item.get('dep_icao'),
                        item.get('arr'), item.get('arr_icao'),
                        item['cs'], item['reg'],
                        item.get('dep_ts'), item.get('arr_ts'))
                except Exception:
                    pass
        except Exception:
            pass
    _maybe_evict_tracks()
    _open_legs_evict_stale()
    return recorded


def _warehouse_write_leg_obs(dep, dep_icao, arr, arr_icao, cs, reg, dep_ts, arr_ts):
    """AUFGABE-2: bei einer SELBST-beobachteten Ab-/Anflug-Kante (adsb.lol-Sweep)
    zwei IST-ZEIT-Rows nach airport_delay_obs schreiben — analog zum OpenSky-Fill,
    über den bestehenden idempotenten Write-Pfad app._delay_obs_write_through.

    EHRLICH: adsb.lol liefert IST-Zeiten, KEINE SOLL-Zeiten → das Delay bleibt
    UNBEKANNT (max_delay=0, status='', cancelled=False → delay_known bleibt false).
    KEIN erfundenes „pünktlich". Der Finalizer/Resolver zeigt damit Ist-Ab-/Ankunft
    + Tail + Herkunft/Ziel, aber nie eine fabrizierte Verspätung.
      · DEP-Row: airport=<dep>,      dest_iata=<arr> (ZIEL),      sched=IST-Abflug.
      · ARR-Row: airport=<arr>#ARR,  dest_iata=<dep> (HERKUNFT),  sched=IST-Ankunft.
    Zeiten in Flughafen-LOKALZEIT (gleiche Basis wie die Scraper). Schema-safe: die
    `source`-Spalte fehlt evtl. in Prod → der Write-Pfad droppt sie im Fallback.
    Best-effort, wirft NIE."""
    wt = _life_app('_delay_obs_write_through')
    if wt is None:
        return
    # #3-FIX (Review): an board-gedeckten Airports (FRA/MUC/… native Boards mit
    # SOLL-Zeiten) NICHT zusätzlich adsb.lol-IST-Rows schreiben — sonst steht der
    # Flug in route-history doppelt (andere sched) und verwässert die Pünktlich-
    # keits-Quote. adsb.lol füllt so nur board-LOSE Hubs; wo ein Board existiert,
    # bleibt die SOLL-Row die einzige Quelle. (Follow-up: eu_scraper-Airports.)
    _free = _life_app('_FREE_BOARD_CODES') or frozenset()
    dep_covered = bool(dep) and dep.upper() in _free
    arr_covered = bool(arr) and arr.upper() in _free
    to_local = _life_app('_unix_to_airport_local')
    cs_to_fn = _life_app('_wh_callsign_to_iata_flightno')
    cs_u = (cs or '').strip().upper()
    fn = None
    if cs_to_fn is not None:
        try:
            fn = cs_to_fn(cs_u)
        except Exception:
            fn = None
    fn = (fn or cs_u).replace(' ', '').upper()
    if not fn:
        return
    airline = fn[:2].upper() if fn[:2].isalpha() else (cs_u[:3] if cs_u else '')
    reg_u = (reg or '').strip().upper() or ''

    def _local(ts, iata):
        if to_local is None or ts is None or not iata:
            return None
        try:
            dt = to_local(ts, iata)
        except Exception:
            return None
        if dt is None:
            return None
        return dt.strftime('%Y-%m-%d'), dt.strftime('%H:%M')

    # DEP-Row (Herkunftsflughafen, dest=Ziel, IST-Abflugzeit).
    if dep and dep_ts is not None and not dep_covered:
        dl = _local(dep_ts, dep)
        if dl:
            try:
                wt(dl[0], fn, dl[1], 0, False, dep.upper(), '',
                   {'dest_iata': (arr or '').upper(), 'airline': airline,
                    'reg': reg_u, 'source': 'adsb_lol'})
            except Exception:
                pass
    # ARR-Row (Zielflughafen '<AP>#ARR', dest=Herkunft, IST-Ankunftszeit).
    if arr and arr_ts is not None and not arr_covered:
        al = _local(arr_ts, arr)
        if al:
            try:
                wt(al[0], fn, al[1], 0, False, (arr.upper() + '#ARR'), '',
                   {'dest_iata': (dep or '').upper(), 'airline': airline,
                    'reg': reg_u, 'source': 'adsb_lol'})
            except Exception:
                pass


def _geometry_allows_route(candidate, lat, lon, track, gs=None, on_ground=False):
    """GEOMETRIE-GATE (REJECT-ONLY) — Owner-Regel: „scraped/eigene Infos sind #1,
    müssen NICHT geprüft werden." Generische Kandidaten (adsbdb/hexdb/adsb.lol/
    AviationStack) werden GEZEIGT, ES SEI DENN die Live-Geometrie WIDERSPRICHT
    KLAR. Wir verwerfen NUR den eklatanten Fall: Flieger ist in der Luft, fliegt
    eindeutig WEG vom behaupteten Ziel (Kurs vs. Peilung > ~115°) UND ist nicht
    in Endpunkt-Nähe (Anflug/Abflug/Holding, wo Kurs bedeutungslos ist).

    Damit fangen wir weiter die groben Verwechslungen ab (Flieger fliegt in die
    komplett falsche Richtung), hören aber auf, die 90 % plausiblen Routen zu
    verstecken (Anflug-Kurven, gerade gestartet, Holding, Kurs-Rauschen).

    Rückgabe: True  → ZEIGEN (Default; kein klarer Widerspruch)
              False → NUR bei KLAREM Widerspruch verwerfen. Wirft NIE → im
                      Zweifel zeigen.
    """
    try:
        if not candidate:
            return True                      # nichts zu prüfen → nicht blockieren
        # Ziel mit Koordinaten nötig — ohne Ziel-Geometrie NICHTS widerlegbar → zeigen.
        dst = candidate.get('dst') or candidate.get('dst_icao')
        dap = _airport_row(dst)
        if not dap or dap.get('lat') is None or dap.get('lon') is None:
            return True
        if lat is None or lon is None:
            return True                      # keine Live-Position → nicht widerlegbar
        dlat, dlon = float(dap['lat']), float(dap['lon'])
        dist_dest = _haversine_km(lat, lon, dlat, dlon)

        # Endpunkt-Nähe (Anflug/Holding/gerade gelandet): Kurs sagt nichts über die
        # Strecke → NIE widerlegen. Großzügig (60 km), weil ATC-Vektoren/Gegenanflug
        # den Kurs weit weg vom geraden Peilkurs biegen.
        if dist_dest <= 60.0:
            return True

        # Am Boden: Roll-Kurs ist bedeutungslos — ABER die Boden-POSITION ist ein
        # harter Fakt. Steht der Flieger auf einem Flughafen, der WEDER Start NOCH
        # Ziel der behaupteten Route ist, ist die Route stale/falsch (der Callsign
        # der letzten/nächsten Rotation, während der Flieger tatsächlich woanders
        # parkt). Owner-Bug 2026-07-03: D-AIOB / SXS… STEHT in FRA, Route sagte
        # aber ADB-MAN. → als klaren Widerspruch verwerfen.
        if on_ground:
            near = _nearest_airport(lat, lon, max_km=8.0)
            if near:
                near_codes = set()
                for k in ('iata', 'icao'):
                    try:
                        v = (near[k] or '').upper()
                    except Exception:
                        v = ''
                    if v:
                        near_codes.add(v)
                route_codes = set()
                for k in ('src', 'src_icao', 'dst', 'dst_icao'):
                    v = (candidate.get(k) or '').upper()
                    if v:
                        route_codes.add(v)
                # Nur verwerfen, wenn wir den Boden-Flughafen KENNEN und er sich mit
                # KEINEM Endpunkt der Route deckt. Kein/uneindeutiger Flughafen →
                # nicht widerlegen (im Zweifel zeigen).
                if near_codes and route_codes and near_codes.isdisjoint(route_codes):
                    return False
            return True

        # Nahe am Abflug (gerade gestartet, dreht noch ein) → Kurs bedeutungslos → zeigen.
        src = candidate.get('src') or candidate.get('src_icao')
        sap = _airport_row(src)
        if sap and sap.get('lat') is not None and sap.get('lon') is not None:
            if _haversine_km(lat, lon, float(sap['lat']), float(sap['lon'])) <= 60.0:
                return True

        # Ohne Kurs kein Widerspruch feststellbar → zeigen.
        if track is None:
            return True
        brg = _bearing_deg(lat, lon, dlat, dlon)
        if brg is None:
            return True
        diff = abs((brg - track + 180.0) % 360.0 - 180.0)

        # KLARER Widerspruch: Flieger fliegt >115° WEG vom behaupteten Ziel, in
        # Reiseflug-Distanz von beiden Endpunkten. Das ist der eklatante „fliegt in
        # die komplett falsche Richtung"-Fall → verwerfen. Alles darunter → zeigen.
        if diff > 115.0:
            return False

        return True
    except Exception:
        return True                          # im Zweifel: zeigen, nicht verstecken


# _free_generic_route (adsbdb/adsb.lol/hexdb-Generik) GELÖSCHT (Kosten-Review
# 2026-07-09): 0 Aufrufer — der Owner hatte die Quelle schon am 2026-07-03 aus
# der Kaskade genommen („eh immer falsch"), warehouse_reader dokumentiert das.


def _resolve_live_route(callsign, hexid=None, reg=None, lat=None, lon=None,
                        track=None, gs=None, on_ground=False, fast=False,
                        allow_paid=False, for_search=False, date=None):
    """OWN-DATA-FIRST Kaskade → genaue heutige Route eines Live-Fliegers.
    Rückgabe: route-Dict mit src/dst(+_icao), source, confidence(+optional
    status/gate/terminal/reg) — oder None.

    Increment 2 (2026-07-06): DÜNNER ADAPTER auf die kanonische EINE Route-Quelle
    `warehouse_reader.route_for_flight` (free-first, bezahlt nur budget-gedeckelt
    zuletzt). Live-Aufruf → for_search=False (Positions-/Geometrie-Gate wie bisher).

    `allow_paid` gibt der AUFRUFER vor (Default False = FREE-ONLY): der bezahlte
    Notnagel (AeroDataBox/AviationStack, Stufe 6) feuert NUR im eigenen/watch-Pfad
    (mein/getappter EIGENER Flieger), NIE beim generischen Radar-Tap eines fremden
    Fliegers und nie im gratis-Poller-Pfad (`_machine_live`). So entsteht kein
    Paid-per-Radar-Tap; bezahlt bleibt der budget-gedeckelte letzte Ausweg.

    Der frühere `fast`-Pfad (bezahltes AeroDataBox VORGEZOGEN mit 3-s-Timeout, um
    langsames OpenSky zu überspringen) ist entfallen: OpenSky ist nicht mehr in der
    Kaskade, alle freien Quellen sind schnelle Tabellen-Reads und Bezahlt läuft
    ohnehin zuletzt — `fast` bleibt nur aus Signatur-Kompat erhalten (no-op).
    Jeder Treffer wird von route_for_flight via _record_resolved_route in die
    eigene Warehouse geschrieben."""
    from blueprints.warehouse_reader import route_for_flight
    return route_for_flight(callsign=callsign, hex=hexid, reg=reg,
                            lat=lat, lon=lon, track=track, gs=gs,
                            on_ground=on_ground, for_search=for_search,
                            allow_paid=allow_paid, date=date)


# ---------------------------------------------------------------- helpers
def _airport_row(code):
    code = (code or '').strip().upper()
    if not code:
        return None
    if len(code) == 3:
        r = _q1('SELECT * FROM airports WHERE iata=? LIMIT 1', (code,))
        if r:
            return r
    return _q1('SELECT * FROM airports WHERE icao=? LIMIT 1', (code,)) \
        or _q1('SELECT * FROM airports WHERE iata=? LIMIT 1', (code,))


def _airline_row(code):
    code = (code or '').strip().upper()
    if not code:
        return None
    if len(code) == 2:
        r = _q1('SELECT * FROM airlines WHERE iata=? LIMIT 1', (code,))
        if r:
            return r
    return _q1('SELECT * FROM airlines WHERE icao=? LIMIT 1', (code,)) \
        or _q1('SELECT * FROM airlines WHERE iata=? LIMIT 1', (code,))


def _now_year():
    return time.gmtime().tm_year


def _airline_logo(iata):
    """Freies Logo-CDN (avs.io) — externe URL, KEIN eigener Storage."""
    iata = (iata or '').strip().upper()
    return f'https://pics.avs.io/120/120/{iata}.png' if len(iata) == 2 else None


# ---------------------------------------------------------------- city names
# IATA → hübscher Städtename für User-facing Labels (Family-/Friend-Roster,
# 2026-07-03: "immer Frankfurt – San Francisco, nie FRA-SFO-FRA").
# Quelle: gebackene Referenz-DB (airports.city = OurAirports-municipality,
# ~85k Airports weltweit); Fallback airports_compact.json (city/name), dann
# der Airport-`name`, zuletzt der IATA-Code selbst. In-Process-Cache.
_IATA_CITY_CACHE = {}
_IATA_LATLON_CACHE = {}
_COMPACT_CITY_CACHE = None
_COMPACT_CITY_LOCK = threading.Lock()

# Kuratierte Anzeige-Städte (2026-07-04: vollständige Übernahme der iOS-
# `germanCityOverrides` + Audit-Fixes). Vorher zeigte das Backend Municipality-
# Dörfer („Greven", „Spata-Artemida") und englische Exonyme, während iOS längst
# kuratierte — EINE Wahrheit: Quelle ist die iOS-Map (AirportDB.swift), hierher
# portiert; OVD→Asturias etc. per Audit verifiziert (Owner-Bug 2026-07-04).
_IATA_CITY_OVERRIDES = {
    'ACE': 'Lanzarote',
    'ADB': 'Izmir',
    'ADD': 'Addis Abeba',
    'AGA': 'Agadir',
    'ALG': 'Algier',
    'ANR': 'Antwerpen',
    'ATH': 'Athen',
    'BEG': 'Belgrad',
    'BGW': 'Bagdad',
    'BGY': 'Bergamo',
    'BHX': 'Birmingham',
    'BJL': 'Banjul',
    'BOG': 'Bogotá',
    'BRU': 'Brüssel',
    'BSL': 'Basel',
    'CAI': 'Kairo',
    'CAN': 'Guangzhou',
    'CCU': 'Kalkutta',
    'CDG': 'Paris',
    'CFU': 'Korfu',
    'CGN': 'Köln',
    'CHQ': 'Chania',
    'CIA': 'Rom',
    'CPH': 'Kopenhagen',
    'CPT': 'Kapstadt',
    'CTU': 'Chengdu',
    'DCA': 'Washington',
    'DEL': 'Delhi',
    'DFW': 'Dallas',
    'DJE': 'Djerba',
    'DME': 'Moskau',
    'DMM': 'Dammam',
    'DPS': 'Denpasar',
    'EAS': 'San Sebastián',
    'EBL': 'Erbil',
    'EFL': 'Kefalonia',
    'ESB': 'Ankara',
    'EVN': 'Eriwan',
    'EZE': 'Buenos Aires',
    'FCO': 'Rom',
    'FKB': 'Karlsruhe/Baden-Baden',
    'FLR': 'Florenz',
    'FMO': 'Münster',
    'FNA': 'Freetown',
    'FRA': 'Frankfurt',
    'FUE': 'Fuerteventura',
    'GDN': 'Danzig',
    'GIG': 'Rio de Janeiro',
    'GOA': 'Genua',
    'GOI': 'Goa',
    'GRZ': 'Graz',
    'GUA': 'Guatemala-Stadt',
    'GVA': 'Genf',
    'HAN': 'Hanoi',
    'HAV': 'Havanna',
    'HEL': 'Helsinki',
    'HHN': 'Hahn',
    'HKG': 'Hongkong',
    'HND': 'Tokio',
    'HNL': 'Honolulu',
    'IAD': 'Washington',
    'IBZ': 'Ibiza',
    'IEV': 'Kiew',
    'IKA': 'Teheran',
    'ISB': 'Islamabad',
    'IZM': 'Izmir',
    'JED': 'Dschidda',
    'JTR': 'Santorin',
    'KBP': 'Kiew',
    'KEF': 'Reykjavik',
    'KGS': 'Kos',
    'KIX': 'Osaka',
    'KLU': 'Klagenfurt',
    'KRK': 'Krakau',
    'KRS': 'Kristiansand',
    'KSF': 'Kassel',
    'KUL': 'Kuala Lumpur',
    'KUT': 'Kutaissi',
    'KWI': 'Kuwait-Stadt',
    'KZN': 'Kasan',
    'LBA': 'Leeds',
    'LCA': 'Larnaka',
    'LCG': 'A Coruña',
    'LED': 'St. Petersburg',
    'LEJ': 'Leipzig',
    'LGG': 'Lüttich',
    'LIL': 'Lille',
    'LIN': 'Mailand',
    'LIS': 'Lissabon',
    'LJU': 'Ljubljana',
    'LPA': 'Gran Canaria',
    'LTN': 'London',
    'LUX': 'Luxemburg',
    'LWO': 'Lwiw',
    'LYS': 'Lyon',
    'MAH': 'Menorca',
    'MAN': 'Manchester',
    'MCT': 'Maskat',
    'MEX': 'Mexiko-Stadt',
    'MLA': 'Malta',
    'MNL': 'Manila',
    'MPL': 'Montpellier',
    'MRS': 'Marseille',
    'MRU': 'Mauritius',
    'MUC': 'München',
    'MXP': 'Mailand',
    'NAP': 'Neapel',
    'NCE': 'Nizza',
    'NCL': 'Newcastle',
    'NGO': 'Nagoya',
    'NRT': 'Tokio',
    'NUE': 'Nürnberg',
    'ODS': 'Odessa',
    'OLB': 'Olbia',
    'ORY': 'Paris',
    'OSL': 'Oslo',
    'OTP': 'Bukarest',
    'OVD': 'Asturias',
    'PAD': 'Paderborn',
    'PEK': 'Peking',
    'PFO': 'Paphos',
    'PKX': 'Peking',
    'PNH': 'Phnom Penh',
    'PRG': 'Prag',
    'PRN': 'Pristina',
    'PSA': 'Pisa',
    'PTY': 'Panama-Stadt',
    'PVG': 'Shanghai',
    'RAK': 'Marrakesch',
    'RHO': 'Rhodos',
    'RUH': 'Riad',
    'RZE': 'Rzeszów',
    'SAL': 'San Salvador',
    'SAW': 'Istanbul',
    'SCL': 'Santiago de Chile',
    'SGN': 'Ho-Chi-Minh-Stadt',
    'SHA': 'Shanghai',
    'SIN': 'Singapur',
    'SJD': 'Los Cabos',
    'SJO': 'San José',
    'SKP': 'Skopje',
    'STN': 'London',
    'SUF': 'Lamezia Terme',
    'SVO': 'Moskau',
    'SVQ': 'Sevilla',
    'SYD': 'Sydney',
    'TAO': 'Qingdao',
    'TAS': 'Taschkent',
    'TBS': 'Tiflis',
    'TFN': 'Teneriffa',
    'TFS': 'Teneriffa',
    'THR': 'Teheran',
    'TIA': 'Tirana',
    'TLS': 'Toulouse',
    'TPE': 'Taipeh',
    'TRN': 'Turin',
    'TRS': 'Triest',
    'TSA': 'Taipeh',
    'USM': 'Ko Samui',
    'VCE': 'Venedig',
    'VIE': 'Wien',
    'VKO': 'Moskau',
    'VRA': 'Varadero',
    'VRN': 'Verona',
    'WAW': 'Warschau',
    'WRO': 'Breslau',
    'WUH': 'Wuhan',
    'ZAG': 'Zagreb',
    'ZRH': 'Zürich',
    'ZTH': 'Zakynthos',
}


def _compact_city(code):
    """Fallback-Quelle: airports_compact.json (fields iata/name/city …)."""
    global _COMPACT_CITY_CACHE
    if _COMPACT_CITY_CACHE is None:
        with _COMPACT_CITY_LOCK:
            if _COMPACT_CITY_CACHE is None:
                out = {}
                try:
                    with open(os.path.join(_REPO, 'airports_compact.json'),
                              encoding='utf-8') as f:
                        data = json.load(f)
                    fields = data.get('fields') or []
                    i_iata = fields.index('iata')
                    i_name = fields.index('name')
                    i_city = fields.index('city')
                    for r in (data.get('rows') or []):
                        try:
                            ia = (r[i_iata] or '').strip().upper()
                            if len(ia) == 3:
                                out[ia] = ((r[i_city] or r[i_name]) or '').strip()
                        except (TypeError, IndexError):
                            continue
                except Exception:
                    out = {}
                _COMPACT_CITY_CACHE = out
    return _COMPACT_CITY_CACHE.get(code, '')


def _iata_city_name(iata):
    """IATA → Städtename ("FRA" → "Frankfurt", "SFO" → "San Francisco",
    "HND" → "Tokyo"). Fällt auf den Airport-Namen und zuletzt auf den Code
    selbst zurück — gibt für einen echten Code NIE leer/None zurück."""
    code = (iata or '').strip().upper()
    if len(code) != 3 or not code.isalpha():
        return (iata or '').strip()
    hit = _IATA_CITY_CACHE.get(code)
    if hit is not None:
        return hit
    city = _IATA_CITY_OVERRIDES.get(code) or ''
    if not city:
        try:
            row = _q1(
                "SELECT city, name FROM airports WHERE iata=? "
                "ORDER BY CASE type WHEN 'large_airport' THEN 0 "
                "WHEN 'medium_airport' THEN 1 ELSE 2 END LIMIT 1",
                (code,))
        except Exception:
            row = None
        if row:
            city = ((row.get('city') or '').strip()
                    or (row.get('name') or '').strip())
    if not city:
        city = _compact_city(code)
    if city:
        # "Paris (Roissy-en-France, Val-d'Oise)" → "Paris"; Namens-Fallback
        # "… International Airport" → Ort ohne Airport-Suffix.
        city = re.sub(r'\s*\([^)]*\)', '', city).strip()
        city = re.sub(r'\s+(International|Intl\.?|Municipal|Regional)?\s*Airport$',
                      '', city, flags=re.IGNORECASE).strip()
    out = city or code
    _IATA_CITY_CACHE[code] = out
    return out


def _iata_latlon(code):
    """IATA → (lat, lon) aus der Referenz-DB, None wenn unbekannt. Cached."""
    if code in _IATA_LATLON_CACHE:
        return _IATA_LATLON_CACHE[code]
    row = None
    try:
        row = _q1(
            "SELECT lat, lon FROM airports WHERE iata=? "
            "ORDER BY CASE type WHEN 'large_airport' THEN 0 "
            "WHEN 'medium_airport' THEN 1 ELSE 2 END LIMIT 1",
            (code,))
    except Exception:
        row = None
    out = None
    if row and row.get('lat') is not None and row.get('lon') is not None:
        out = (float(row['lat']), float(row['lon']))
    _IATA_LATLON_CACHE[code] = out
    return out


_IATA_ELEV_CACHE = {}


def _iata_elev_ft(code):
    """IATA → Airport-Elevation (ft MSL) aus der Referenz-DB, None wenn
    unbekannt. Cached. Fürs FlightState-alt-Gate an Hochland-Airports
    (MEX/NBO/ADD: alt>1000 MSL allein beweist dort kein Fliegen)."""
    if code in _IATA_ELEV_CACHE:
        return _IATA_ELEV_CACHE[code]
    row = None
    try:
        row = _q1(
            "SELECT elev_ft FROM airports WHERE iata=? "
            "ORDER BY CASE type WHEN 'large_airport' THEN 0 "
            "WHEN 'medium_airport' THEN 1 ELSE 2 END LIMIT 1",
            (code,))
    except Exception:
        row = None
    out = None
    if row and row.get('elev_ft') is not None:
        try:
            out = int(row['elev_ft'])
        except (TypeError, ValueError):
            out = None
    _IATA_ELEV_CACHE[code] = out
    return out


def _gc_km(lat1, lon1, lat2, lon2):
    """Great-Circle-Distanz (Haversine) in km."""
    rl1, rl2 = math.radians(lat1), math.radians(lat2)
    dlat = rl2 - rl1
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rl1) * math.cos(rl2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def _route_label_cities(routing, layover_ort=None):
    """Tour-Label mit Städtenamen aus einer rohen Routing-Kette.

    "FRA-SFO-FRA" (+ layover_ort "SFO") → "Frankfurt – San Francisco".
    Ziel-Auswahl:
      1. layover_ort, wenn er in der Kette vorkommt (der echte Übernachtungs-/
         Wendepunkt der Tour),
      2. Rundreise (Start == Ende) → der vom Start ENTFERNTESTE Zwischenstopp
         (Great-Circle über die Referenz-DB; ohne Koordinaten: mittlerer Stop),
      3. sonst die letzte Station der Kette.
    Ein-Ort-Fälle (nur layover_ort, z.B. Layover-Ruhetag) → nur der Städtename.
    Returns None wenn gar nichts baubar ist. NIE die rohe Token-Kette."""
    chain = [t for t in re.split(r'[^A-Z]+', (routing or '').upper())
             if len(t) == 3]
    lov = (layover_ort or '').strip().upper()
    if not re.fullmatch(r'[A-Z]{3}', lov):
        lov = ''
    if len(chain) < 2:
        place = chain[0] if chain else lov
        return _iata_city_name(place) if place else None
    origin, dest = chain[0], None
    if lov and lov in chain[1:]:
        dest = lov
    elif origin == chain[-1]:
        # Rundreise FRA-…-FRA: Tour-Ziel = entferntester Punkt vom Start.
        o = _iata_latlon(origin)
        best_d = -1.0
        if o:
            for c in dict.fromkeys(chain[1:-1]):
                p = _iata_latlon(c)
                if not p:
                    continue
                d = _gc_km(o[0], o[1], p[0], p[1])
                if d > best_d:
                    best_d, dest = d, c
        if not dest and len(chain) >= 3:
            dest = chain[len(chain) // 2]
    else:
        dest = chain[-1]
    if not dest or dest == origin:
        return _iata_city_name(origin)
    return f"{_iata_city_name(origin)} – {_iata_city_name(dest)}"


# ---------------------------------------------------------------- endpoints
@aerox_data_bp.route('/api/ax/stats', methods=['GET'])
def ax_stats():
    db = _ensure_db()
    if db is None:
        return jsonify({'ok': False, 'error': 'reference db not available'}), 503
    meta = {r['key']: r['value'] for r in _q('SELECT key, value FROM meta')}
    return jsonify({
        'ok': True,
        'engine': 'AeroX Aviation Data Engine',
        'reference': {
            'airports': int(meta.get('count_airports', 0)),
            'airlines': int(meta.get('count_airlines', 0)),
            'aircraft': int(meta.get('count_aircraft', 0)),
            'aircraft_types': int(meta.get('count_aircraft_types', 0)),
            'routes_seed': int(meta.get('count_routes', 0)),
        },
        'self_growing': 'free-first: own ADS-B self-computed legs + adsbdb/hexdb/OpenSky, cached to Supabase (ax_*_cache)',
    })


@aerox_data_bp.route('/api/ax/airport/<code>', methods=['GET'])
def ax_airport(code):
    r = _airport_row(code)
    if not r:
        return jsonify({'ok': False, 'code': code}), 404
    return jsonify({'ok': True, 'iata': r.get('iata'), 'icao': r.get('icao'),
                    'name': r.get('name'), 'city': r.get('city'),
                    'country': r.get('country'), 'lat': r.get('lat'),
                    'lon': r.get('lon'), 'elev_ft': r.get('elev_ft'),
                    'type': r.get('type')})


@aerox_data_bp.route('/api/ax/airline/<code>', methods=['GET'])
def ax_airline(code):
    r = _airline_row(code)
    if not r:
        return jsonify({'ok': False, 'code': code}), 404
    # Bediente Ziele (aus dem Routen-Seed) — füllt die Airline-Seite, NULL API.
    dests = []
    code = (r.get('iata') or '').strip().upper()
    if code:
        seen = set()
        for row in _q('SELECT DISTINCT dst FROM routes WHERE airline=? LIMIT 60', (code,)):
            d = (row.get('dst') or '').strip().upper()
            if not d or d in seen:
                continue
            seen.add(d)
            ap = _airport_row(d)
            dests.append({'iata': d, 'city': (ap or {}).get('city'),
                          'country': (ap or {}).get('country')})
    return jsonify({'ok': True, 'iata': r.get('iata'), 'icao': r.get('icao'),
                    'name': r.get('name'), 'callsign': r.get('callsign'),
                    'country': r.get('country'), 'logo': _airline_logo(r.get('iata')),
                    'destinations': dests, 'destinations_count': len(dests)})


@aerox_data_bp.route('/api/ax/type/<code>', methods=['GET'])
def ax_type(code):
    code = (code or '').strip().upper()
    r = _q1('SELECT * FROM aircraft_types WHERE typecode=? LIMIT 1', (code,))
    if not r:
        return jsonify({'ok': False, 'code': code}), 404
    cnt = _q1('SELECT COUNT(*) AS n FROM aircraft WHERE typecode=?', (code,))
    out = {'ok': True, 'typecode': r.get('typecode'), 'name': r.get('name'),
           'manufacturer': r.get('manufacturer'), 'model': r.get('model'),
           'class': r.get('class'), 'engines': r.get('engines'),
           'fleet_seen': (cnt or {}).get('n', 0)}
    # Kuratierte Eckdaten (Sitze/Reichweite/Cruise/Wake) — offline.
    try:
        from blueprints.aircraft_specs import specs_for_type
        s = specs_for_type(code)
        if s:
            out['specs'] = s
    except Exception:
        pass
    return jsonify(out)


@aerox_data_bp.route('/api/ax/aircraft/<hexid>', methods=['GET'])
def ax_aircraft(hexid):
    hexid = (hexid or '').strip().lower()
    out = {'ok': True, 'hex': hexid, 'source': 'reference'}
    r = _q1('SELECT * FROM aircraft WHERE hex=? LIMIT 1', (hexid,))
    if r:
        out.update({k: r.get(k) for k in
                    ('reg', 'typecode', 'manufacturer', 'model', 'operator', 'owner', 'built', 'built_date', 'category')
                    if r.get(k) is not None})
    else:
        # Cache → sonst genau ein externer Call, dann zurückschreiben.
        cached = _cache_get('ax_aircraft_cache', 'hex', hexid)
        if cached:
            out.update(cached); out['source'] = 'cache'
        else:
            # Freie Quellen der Reihe nach — adsbdb (EU-stark), dann hexdb.
            live, src = None, None
            for fn, name in ((_adsbdb_aircraft, 'adsbdb'), (_hexdb_aircraft, 'hexdb')):
                live = fn(hexid)
                if live:
                    src = name
                    break
            if live:
                out.update({k: v for k, v in live.items() if v}); out['source'] = src
                _cache_put('ax_aircraft_cache',
                           {'hex': hexid, 'payload': live,
                            'updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
            elif (stale := _cache_get('ax_aircraft_cache', 'hex', hexid,
                                      max_age_days=None)):
                # Stale-if-error (Review 2026-07-10): Refetch-Kette tot (adsbdb
                # lt. Audit down, hexdb wackelig) → den abgelaufenen >90d-Payload
                # ausliefern statt funktionierende Stammdaten sichtbar zu
                # verwerfen. Re-put mit frischem updated_at = Negativ-Cache:
                # sonst rennt JEDER Request erneut in 2 tote 8s-Upstream-Calls
                # (Free-Kaskaden-Mehrlast); Retry dann erst nach dem nächsten
                # Ablauf-Fenster.
                out.update({k: v for k, v in stale.items() if v})
                out['source'] = 'cache-stale'
                _cache_put('ax_aircraft_cache',
                           {'hex': hexid, 'payload': stale,
                            'updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
            else:
                # Keine Stammdaten — ABER Land/Flagge aus der ICAO-Hex-Allokation
                # geht immer (offline). So zeigt das Radar selbst für unbekannte
                # Maschinen wenigstens die Flagge, statt eines leeren 404.
                out['source'] = 'icao-hex'
                try:
                    from blueprints.icao_country import country_for_hex
                    c = country_for_hex(hexid)
                    if c:
                        out['country'] = c['iso']; out['country_name'] = c['name']; out['flag'] = c['flag']
                        return jsonify(out)
                except Exception:
                    pass
                return jsonify({'ok': False, 'hex': hexid}), 404
    # Muster-Vollname + Alter anreichern.
    tc = out.get('typecode')
    if tc:
        t = _q1('SELECT name, manufacturer, engines FROM aircraft_types WHERE typecode=?', (tc.upper(),))
        if t:
            out['type_name'] = t.get('name')
            out['engines'] = t.get('engines')
        # Kuratierte Eckdaten (Sitze/Reichweite/Cruise/Wake) — offline.
        try:
            from blueprints.aircraft_specs import specs_for_type
            s = specs_for_type(tc)
            if s:
                out['specs'] = s
        except Exception:
            pass
    # Alter: TAGESGENAU wenn ein built_date (YYYY-MM-DD) vorliegt (LH-Gruppe via
    # planespotters), sonst jahresbasiert aus `built`. age_months ist der Rest-Monat
    # für eine „X Jahre Y Monate"-Anzeige im Radar (User: „Alter mit Tag und Monat").
    bd = out.get('built_date')
    if bd:
        try:
            import datetime
            d = datetime.date.fromisoformat(str(bd)[:10])
            t = datetime.date.today()
            months = (t.year - d.year) * 12 + (t.month - d.month) - (1 if t.day < d.day else 0)
            if 0 <= months < 1200:
                out['age_years'] = months // 12
                out['age_months'] = months % 12
                if not out.get('built'):
                    out['built'] = d.year
        except Exception:
            pass
    if out.get('age_years') is None and out.get('built'):
        try:
            age = _now_year() - int(out['built'])
            if 0 <= age < 100:
                out['age_years'] = age
        except (ValueError, TypeError):
            pass
    # Registrierungsland aus der ICAO-Hex-Allokation — komplett offline, NULL API.
    try:
        from blueprints.icao_country import country_for_hex
        c = country_for_hex(hexid)
        if c:
            out['country'] = c['iso']; out['country_name'] = c['name']; out['flag'] = c['flag']
    except Exception:
        pass
    return jsonify(out)


@aerox_data_bp.route('/api/ax/flight/<flightno>', methods=['GET'])
def ax_flight(flightno):
    raw = (flightno or '').strip().upper().replace(' ', '')
    if not raw:
        return jsonify({'ok': False, 'error': 'empty'}), 400
    # Airline-Präfix (2–3 alphanumerisch) + Nummer trennen.
    i = 0
    while i < len(raw) and not raw[i].isdigit():
        i += 1
    prefix, number = raw[:i], raw[i:]
    out = {'ok': True, 'flight': raw, 'source': 'reference'}

    airline = _airline_row(prefix)
    if airline:
        out['airline'] = {'iata': airline.get('iata'), 'icao': airline.get('icao'),
                          'name': airline.get('name'), 'callsign': airline.get('callsign'),
                          'logo': _airline_logo(airline.get('iata'))}

    # Route: NUR noch der eigene Cache (adsbdb-Live-Nachschlag entfernt,
    # Kosten-Review 2026-07-09: Quelle unzuverlässig + anonymer Endpoint zog
    # externen Traffic; Consumer wandern eh auf /api/ax/uflight).
    route = _cache_get('ax_route_cache', 'flight', raw)
    if route:
        out['source'] = 'cache'

    if route:
        def enrich(code):
            ap = _airport_row(code)
            if not ap:
                return {'iata': code}
            return {'iata': ap.get('iata'), 'icao': ap.get('icao'),
                    'name': ap.get('name'), 'city': ap.get('city'),
                    'country': ap.get('country'), 'lat': ap.get('lat'), 'lon': ap.get('lon')}
        out['origin'] = enrich(route.get('src') or route.get('src_icao'))
        out['destination'] = enrich(route.get('dst') or route.get('dst_icao'))
        out['callsign'] = route.get('callsign')

    if 'airline' not in out and 'origin' not in out:
        return jsonify({'ok': False, 'flight': raw}), 404
    return jsonify(out)


@aerox_data_bp.route('/api/ax/photo/<hexid>', methods=['GET'])
def ax_photo(hexid):
    """Hex → Foto-URL + Fotograf. NUR die URL wird in Supabase gecacht (winziger
    String, kein Bild-Storage) → ein planespotters-Call je Flieger, danach
    teilen alle Nutzer denselben Treffer. Wächst die eigene Foto-Link-DB."""
    hexid = (hexid or '').strip().lower()
    if not hexid:
        return jsonify({'ok': False, 'error': 'empty'}), 400
    cached = _cache_get('ax_photo_cache', 'hex', hexid)
    if cached:
        return jsonify({'ok': True, 'hex': hexid, 'source': 'cache', **cached})
    photo = _planespotters_photo(hexid)
    if not photo:
        return jsonify({'ok': False, 'hex': hexid}), 404
    _cache_put('ax_photo_cache',
               {'hex': hexid, 'payload': photo,
                'updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
    return jsonify({'ok': True, 'hex': hexid, 'source': 'planespotters', **photo})


@aerox_data_bp.route('/api/ax/photo-reg/<reg>', methods=['GET'])
def ax_photo_reg(reg):
    """Registrierung (z.B. D-ATCC) → Foto-URL. Für den Kein-Live-Signal-Fall, wo
    wir keinen Hex haben, aber die Reg (User: „kein Signal → Foto vom Flieger").
    planespotters /reg/, in ax_photo_cache gecacht (geteilt, free)."""
    rg = (reg or '').strip().upper()
    if not rg:
        return jsonify({'ok': False, 'error': 'empty'}), 400
    cached = _cache_get('ax_photo_cache', 'hex', rg)
    if cached:
        return jsonify({'ok': True, 'reg': rg, 'source': 'cache', **cached})
    d = _http_json(f'https://api.planespotters.net/pub/photos/reg/{urllib.parse.quote(rg)}')
    photos = (d or {}).get('photos') or []
    if not photos:
        return jsonify({'ok': False, 'reg': rg}), 404
    p = photos[0]
    thumb = (p.get('thumbnail_large') or p.get('thumbnail') or {})
    url = thumb.get('src')
    if not url:
        return jsonify({'ok': False, 'reg': rg}), 404
    photo = {'photo': url, 'photographer': p.get('photographer'), 'link': p.get('link')}
    _cache_put('ax_photo_cache',
               {'hex': rg, 'payload': photo,
                'updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
    return jsonify({'ok': True, 'reg': rg, 'source': 'planespotters', **photo})


def _ax_rate_limited(endpoint, limit, window_sec):
    """Per-IP-Rate-Limit (Best-Effort) über das adsb_blueprint-Muster
    (_rate_limited → app._ip_rate_limited). Großzügig dimensioniert fürs
    normale App-Polling — gedacht gegen anonyme Budget-Drains, nicht gegen
    User. Fail-open: sind Helper/Request-Kontext nicht verfügbar, NIE blocken."""
    try:
        from flask import request
        from blueprints.adsb_blueprint import _rate_limited, _req_ip
        return _rate_limited(ip=_req_ip(request), endpoint=endpoint,
                             limit=limit, window_sec=window_sec)
    except Exception:
        return False


# Zeit-Felder im /api/ax/callsign-Response (Owner 2026-07-05: „warum steht
# nicht abflug und ankunft uhrzeit im radar"). Merged-Board-Key → Response-Key.
_CS_TIME_KEYS = (('sched_dep', 'sched_dep'), ('esti_dep', 'est_dep'),
                 ('sched_arr', 'sched_arr'), ('esti_arr', 'est_arr'))


def _merged_times_for(fn, dep_iata, arr_iata):
    """sched/est-Zeiten (station-lokal, wie die Boards sie führen) aus dem
    EIGENEN Dual-Side-Merge (app._flight_obs_merged, free_only → strukturell
    spend-frei). NUR echte Board-Werte; unbekannte Felder fehlen. Wirft nie."""
    if not fn:
        return {}
    merged_fn = _life_app('_flight_obs_merged')
    if merged_fn is None:
        return {}
    try:
        rec = merged_fn(fn, dep_iata=dep_iata, arr_iata=arr_iata,
                        free_only=True) or {}
    except Exception:
        return {}
    out = {}
    for src_k, dst_k in _CS_TIME_KEYS:
        v = rec.get(src_k)
        if v:
            out[dst_k] = v
    return out


@aerox_data_bp.route('/api/ax/callsign/<callsign>', methods=['GET'])
def ax_callsign(callsign):
    """ICAO-Callsign (z.B. DLH506) → GENAUE heutige Route. Das Radar fragt für
    jeden angetippten Flieger hier an. Die FREE-FIRST-Kaskade (_resolve_live_route)
    bevorzugt EIGENE + FREIE Quellen (Warehouse/Tafel + selbst-berechnetes ADS-B →
    adsbdb/adsb.lol/hexdb). BEZAHLTE APIs (AeroDataBox/AviationStack) feuern hier
    per DEFAULT NICHT — der generische Radar-Tap eines fremden Fliegers bleibt
    FREE-ONLY (kein Paid-per-Radar-Tap). Nur der EIGENE/beobachtete Flieger
    (?own=1 / ?watch=1) darf den budget-gedeckelten bezahlten Notnagel als letzten
    Ausweg ziehen. Jeder Treffer wird datums-/reg-gekeyt in ax_route_cache
    zurückgeschrieben → derselbe Tap ist morgen gratis und die eigene Routen-DB
    wächst weltweit.

    Optionale Query-Params (schalten höhere Genauigkeit frei, alle abwärts-
    kompatibel — ohne sie funktioniert der Call wie bisher):
      hex=<icao24>  reg=<D-AIZJ>  lat= lon=/lng=  track=<heading°>
      gs=<groundspeed_kt>  on_ground=<0|1>   (schalten die Geometrie-Bestätigung
                                              generischer Kandidaten frei)
      own=<0|1> / watch=<0|1>  (EIGENER/beobachteter Flieger → erlaubt den
                                budget-gedeckelten bezahlten Notnagel)

    Response (Owner „scraped/eigene Infos sind #1 — zeigen, nicht verstecken"):
      ok, callsign, source, origin{}, destination{}, [gate, terminal, status]
      confidence == 'confirmed' (eigene/gescrapte/bezahlte Daten) oder 'estimated'
      (generischer Kandidat, GEZEIGT). Nur wenn wir GAR keine Strecke haben ODER
      die Live-Geometrie KLAR widerspricht (Flieger fliegt in die falsche Richtung)
      → 404 (keine Route). Sonst wird die Route zurückgegeben.
    """
    from flask import request
    # Per-IP-Limit (Audit: anonymer Endpoint kann bezahlte Provider ziehen).
    # 120/min deckt jedes legitime Radar-Tippen; drüber = Drain-Verdacht.
    if _ax_rate_limited('ax_callsign', limit=120, window_sec=60):
        return jsonify({'ok': False, 'error': 'rate_limited'}), 429
    cs = (callsign or '').strip().upper().replace(' ', '')
    if not cs:
        return jsonify({'ok': False, 'error': 'empty'}), 400
    hexid = (request.args.get('hex') or '').strip() or None
    reg = (request.args.get('reg') or '').strip() or None

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    lat = _f(request.args.get('lat'))
    lon = _f(request.args.get('lon') or request.args.get('lng'))
    track = _f(request.args.get('track') or request.args.get('heading'))
    gs = _f(request.args.get('gs') or request.args.get('speed') or request.args.get('gspeed'))
    og = (request.args.get('on_ground') or request.args.get('ground') or '').strip().lower()
    on_ground_known = bool(og)
    on_ground = og in ('1', 'true', 'yes', 'y')
    # EIGENER/beobachteter Flieger? (nur dann darf der budget-gedeckelte bezahlte
    # Notnagel feuern — der generische Radar-Tap eines fremden Fliegers bleibt
    # FREE-ONLY: kein Paid-per-Radar-Tap.)
    _own = (request.args.get('own') or request.args.get('watch')
            or request.args.get('mine') or '').strip().lower()
    # ZUSÄTZLICH Bearer-Pflicht (Sweep 2026-07-10, Klasse A: own=1 war ein
    # reiner Client-Query-Param — anonymes curl konnte Credits schalten).
    # Muster wie ax_unified_flight: kein voller Token-Check, es geht nur darum,
    # dass ein anonymer Scan nie Paid zieht. Die App sendet den Header immer.
    _authed = (request.headers.get('Authorization') or '').strip().lower() \
        .startswith('bearer ')
    allow_paid = _authed and _own in ('1', 'true', 'yes', 'y')

    # FREE-FIRST-Kaskade (eine Quelle: warehouse_reader.route_for_flight). Bezahlt
    # nur wenn EIGENER/watch-Flieger UND Tages-Budget frei — sonst rein aus unseren
    # Tabellen. Der Background-Poller füllt die Tabellen weiter free-first.
    route = _resolve_live_route(cs, hexid=hexid, reg=reg, lat=lat, lon=lon,
                                track=track, gs=gs, on_ground=on_ground,
                                allow_paid=allow_paid)
    # SHOW-Contract (Owner: „scraped/eigene Infos sind #1"): der Resolver hat den
    # Reject-Only-Geometrie-Filter bereits angewandt (nur der eklatante „fliegt in
    # die falsche Richtung"-Fall wird verworfen). Was hier ankommt, WIRD gezeigt —
    # sobald es eine Strecke hat. Nur wenn wir GAR NICHTS haben → 404.
    if not route or not (route.get('src') or route.get('src_icao')
                         or route.get('dst') or route.get('dst_icao')):
        return jsonify({'ok': False, 'callsign': cs}), 404

    out = {'ok': True, 'callsign': cs,
           'source': route.get('source', 'cache'),
           'confidence': route.get('confidence', 'confirmed')}
    if route.get('status'):
        out['status'] = route.get('status')
    if route.get('reg'):
        out['reg'] = route.get('reg')

    def enrich(code):
        ap = _airport_row(code)
        if not ap:
            return {'iata': code}
        return {'iata': ap.get('iata'), 'icao': ap.get('icao'),
                'name': ap.get('name'), 'city': ap.get('city'),
                'country': ap.get('country'), 'lat': ap.get('lat'), 'lon': ap.get('lon')}
    out['origin'] = enrich(route.get('src') or route.get('src_icao'))
    out['destination'] = enrich(route.get('dst') or route.get('dst_icao'))
    # Gate/Terminal (nur aus der echten Airport-Tafel, _route_from_obs) → Live-Map.
    if route.get('gate'):
        out['gate'] = route.get('gate')
    if route.get('terminal'):
        out['terminal'] = route.get('terminal')
    # STATUS aus DERSELBEN einen Quelle wie die Route (warehouse_reader —
    # Board autoritativ, ADS-B nur ergänzend, Delay aus dem Dual-Side-Merge).
    # FREE-ONLY (allow_paid=False): Status kostet nie pro Tap. So sind Route UND
    # Phase/Delay konsistent statt aus verschiedenen (teils bezahlten) Quellen.
    origin_iata = route.get('src') or _icao_to_iata(route.get('src_icao'))
    dest_iata = route.get('dst') or _icao_to_iata(route.get('dst_icao'))
    try:
        from blueprints.warehouse_reader import status_for_flight
        st = status_for_flight(
            callsign=cs, reg=(route.get('reg') or reg),
            origin=origin_iata, dest=dest_iata,
            on_ground=(on_ground if on_ground_known else None),
            lat=lat, lon=lon, allow_paid=False)
    except Exception:
        st = None
    if st:
        if st.get('phase') and st['phase'] != 'unknown':
            out['phase'] = st['phase']
        # Delay nur wenn wirklich bekannt (unbekannt ≠ pünktlich).
        out['delay_known'] = bool(st.get('delay_known'))
        if st.get('delay_known'):
            out['delay_min'] = st.get('delay_min')
        # Gate/Terminal aus der Board-Status-Seite ergänzen, falls die Route
        # sie nicht schon trug (echte Werte, nie erfunden).
        if not out.get('gate') and st.get('gate'):
            out['gate'] = st.get('gate')
        if not out.get('terminal') and st.get('terminal'):
            out['terminal'] = st.get('terminal')
    # Abflug-/Ankunfts-Zeiten (Owner: „warum steht nicht abflug und ankunft
    # uhrzeit im radar"): station-lokal, NUR echte Werte. Quelle 1: das auf-
    # gelöste Leg selbst (AeroDataBox-Payload / Tafel-Record trägt sched/est
    # bereits). Quelle 2: eigener Dual-Side-Board-Merge (_flight_obs_merged,
    # free_only). Unbekannte Felder fehlen — Unbekannt ist nicht pünktlich.
    times = {k: route.get(k)
             for k in ('sched_dep', 'est_dep', 'sched_arr', 'est_arr')
             if route.get(k)}
    if len(times) < 4:
        fn = (route.get('flight_no') or _callsign_to_iata_flightno(cs) or cs)
        for k, v in _merged_times_for(fn, route.get('src'),
                                      route.get('dst')).items():
            times.setdefault(k, v)
    out.update(times)

    # Reiche FR24-Live-Detail (opt-in via ?rich=1) für die Tap-Karte: ETA/Progress/
    # Delay-Ampel/Muster-Langname/Airline/Foto. NUR beim expliziten Detail-Tap (nicht
    # im Radar-Batch) — je ein extra gRPC-Call, Timeout-geschützt. Fehlt still, wenn
    # FR24 nichts hat; Route/Status oben bleiben board-autoritativ (nicht überschrieben).
    if (request.args.get('rich') or '').strip() in ('1', 'true', 'yes') \
            and lat is not None and lon is not None:
        try:
            from blueprints import fr24_grpc
            card = fr24_grpc.detail_card(callsign=cs, hex=hexid,
                                         reg=((route or {}).get('reg') or reg),
                                         lat=lat, lon=lon)
        except Exception:
            card = None
        if card:
            out['live'] = card

    return jsonify(out)


@aerox_data_bp.route('/api/ax/radar-enrich', methods=['POST'])
def ax_radar_enrich():
    """Batch-Anreicherung ALLER sichtbaren Radar-Flieger (Owner 2026-07-04:
    „diese Rechnung direkt für alle Flieger in Sicht machen — dann sind sie
    beim Antippen sofort da"). Body {"hexes":[…]} (≤80). EIN Warehouse-Query
    (BOARD-verifizierte Tail↔Hex-Matches, Live-Fenster dep −3h…+4h), kein
    Paid-Spend, keine Einzel-Lookups. iOS füllt damit beim Area-Poll den
    Route-Cache → der Tap zeigt Strecke/Gate ohne Wartezeit."""
    from flask import request
    # Per-IP-Limit (Audit): Batch-Endpoint = 1 Call pro Area-Poll (~15–30 s).
    # 60/min ist großzügig fürs App-Polling, stoppt anonyme Bulk-Drains.
    if _ax_rate_limited('ax_radar_enrich', limit=60, window_sec=60):
        return jsonify({'ok': False, 'error': 'rate_limited'}), 429
    body = request.get_json(silent=True) or {}
    hexes = [str(h).lower().strip() for h in (body.get('hexes') or []) if h][:80]
    if not hexes:
        return jsonify({'ok': False, 'error': 'no_hexes'}), 400
    sb = _sb()
    out = {}
    if sb is not None:
        try:
            from datetime import datetime as _dt
            yday = time.strftime('%Y-%m-%d', time.gmtime(time.time() - 86400))
            r = (sb.table('flights')
                 .select('hex,op_flight_no,origin,destination,gate,status,tail,'
                         'sched_dep,est_dep,sched_arr,est_arr')
                 .in_('hex', hexes).gte('service_date', yday)
                 .order('updated_at', desc=True).limit(200).execute())
            now = time.time()
            for f in (r.data or []):
                hx = (f.get('hex') or '').lower()
                if not hx or hx in out:
                    continue
                dep_iso = f.get('est_dep') or f.get('sched_dep')
                try:
                    dep_ts = _dt.fromisoformat(
                        str(dep_iso).replace('Z', '+00:00')).timestamp()
                except (TypeError, ValueError):
                    continue
                if now - 3 * 3600 <= dep_ts <= now + 4 * 3600:
                    entry = {'flight_no': f.get('op_flight_no'),
                             'src': f.get('origin'), 'dst': f.get('destination'),
                             'gate': f.get('gate'), 'status': f.get('status'),
                             'tail': f.get('tail'),
                             'source': 'warehouse_board',
                             'confidence': 'confirmed'}
                    # Abflug-/Ankunftszeiten mitgeben (UTC-ISO — die flights-
                    # Warehouse-Zeiten sind UTC mit Z/Offset, NICHT station-lokal),
                    # damit der Tap SOFORT „ab HH:MM · an HH:MM" + Header-Zeit „HH:MM →
                    # HH:MM" + Delay zeigt — ohne einen Einzel-Call. iOS leitet das Delay
                    # aus est_arr − sched_arr ab. Nur echte Werte (nie erfunden).
                    for k in ('sched_dep', 'est_dep', 'sched_arr', 'est_arr'):
                        if f.get(k):
                            entry[k] = f.get(k)
                    out[hx] = entry
            # ANKUNFT-VERDRAHTUNG (Owner „steht die Ankunft nicht im Scraping?" —
            # DOCH): die geplante/erwartete Ankunft steht im Airport-Scrape als
            # '<Ziel>#ARR'-Row (airport=Ziel+#ARR, sched/esti = Ankunftszeit). Die
            # flights-Warehouse-Row trägt nur die Abflugseite → sched_arr/est_arr
            # blieben leer. EIN Batch-Query auf die ARR-Obs füllt die Lücke, sodass
            # der Radar-Callout „HH:MM → HH:MM" zeigt statt nur Abflug (kein Paid,
            # nur eigener Scrape). Timeout-sicher: eigener try, Fehler → einfach ohne.
            try:
                today = time.strftime('%Y-%m-%d', time.gmtime())
                missing = [(e.get('flight_no'), e.get('dst'), hx)
                           for hx, e in out.items()
                           if e.get('flight_no') and e.get('dst')
                           and not e.get('sched_arr') and not e.get('est_arr')]
                if missing:
                    arr_keys = sorted({(d or '').upper() + '#ARR'
                                       for _, d, _ in missing if d})
                    fns = sorted({(fn or '').upper() for fn, _, _ in missing if fn})
                    ao = (sb.table('airport_delay_obs')
                          .select('airport,flight,sched,esti,date')
                          .in_('date', [yday, today])
                          .in_('airport', arr_keys)
                          .in_('flight', fns)
                          .limit(300).execute())
                    aidx = {}
                    for a in (ao.data or []):
                        k = ((a.get('flight') or '').upper(),
                             (a.get('airport') or '').upper())
                        # HEUTIGE Row gewinnt (nie eine gestrige über die heutige).
                        if k not in aidx or (a.get('date') or '') > (aidx[k].get('date') or ''):
                            aidx[k] = a
                    for fn, dst, hx in missing:
                        a = aidx.get(((fn or '').upper(),
                                      (dst or '').upper() + '#ARR'))
                        if not a:
                            continue
                        # Über den geteilten Mapper (P0): normalisiert die Board-
                        # Zeiten auf ISO mit Station-Offset statt Roh-Durchreiche.
                        _fa = _obs_rows_to_facts(None, a)
                        if _fa.get('sched_arr'):
                            out[hx]['sched_arr'] = _fa['sched_arr']
                        if _fa.get('est_arr'):
                            out[hx]['est_arr'] = _fa['est_arr']
            except Exception:
                pass
        except Exception:
            pass
    return jsonify({'ok': True, 'count': len(out), 'routes': out})


# ─────────────────────────────────────────────────────────────────────────────
#  UNIFIED FLIGHT-INFO — P0: die EINE geteilte Board-Fakten-Quelle
#  (Ultraplan docs/unified-flight-info-ultraplan-v2.md). Statt drei Merge-Patches
#  pro Screen liest künftig alles hier: Soll/Ist Ab+Ankunft, Gate, Terminal, Delay,
#  Status, Reg/Typ, cancelled — aus airport_delay_obs (DEP-Row <dep> + ARR-Row
#  <arr>#ARR). Gratis (eigener Scrape), nur echte Werte, nie erfunden. FR24 (erst
#  gratis-gRPC, dann paid) ist NICHT hier — das ist der on-demand-Fallback (P1/P3),
#  der NUR feuert wenn dieser Gratis-Merge eine Lücke lässt UND der Flug gebraucht wird.
# ─────────────────────────────────────────────────────────────────────────────

def _obs_station_dt(v, iata, service_date):
    """Board-Zeitwert (bare 'HH:MM' | naive Lokal-ISO | Offset-ISO) → (aware
    datetime in Station-TZ von `iata`, hatte_eigenes_Datum). Bare Zeiten bekommen
    das Servicedatum der Row. (None, False) bei unparsbar/unbekannter TZ — der
    Aufrufer behält dann den Rohwert (nichts erfinden)."""
    from datetime import datetime as _dt
    try:
        from zoneinfo import ZoneInfo
        from airport_tz import airport_tz as _atz
        tzn = _atz((iata or '').upper().strip())
        tz = ZoneInfo(tzn) if tzn else None
    except Exception:
        tz = None
    if tz is None or not v:
        return None, False
    s = str(v).strip()
    m = re.match(r'^(\d{1,2}):(\d{2})$', s)
    if m:
        d = (service_date or '')[:10]
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', d):
            return None, False
        try:
            dt = _dt.fromisoformat('%sT%02d:%02d:00'
                                   % (d, int(m.group(1)), int(m.group(2))))
        except ValueError:
            return None, False
        return dt.replace(tzinfo=tz), False
    try:
        dt = _dt.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        try:      # '+0200' ohne Doppelpunkt (fromisoformat erst ab 3.11)
            dt = _dt.strptime(s, '%Y-%m-%dT%H:%M:%S%z')
        except ValueError:
            return None, False
    return (dt.replace(tzinfo=tz) if dt.tzinfo is None
            else dt.astimezone(tz)), True


def _obs_rows_to_facts(dep_row, arr_row):
    """Reiner Mapper (kein DB-Zugriff → trivial testbar): DEP-/ARR-Obs-Row →
    normalisiertes Fakten-Dict. Delay kommt direkt aus `max_delay_min` (kein
    Rechnen über formatgemischte Zeiten). Leere/None-Werte werden weggelassen.
    ZEIT-NORMALISIERUNG (Ultraplan-P0): sched/esti kommen roh gemischt (bare
    '16:50' station-lokal, naive Lokal-ISO, Offset-ISO) → hier zentral auf ISO
    MIT Station-Offset ('…T16:50:00+02:00'; Wanduhr bleibt Station-lokal lesbar,
    der Offset macht sie eindeutig). Station-TZ = Airport der jeweiligen Seite
    (dep-Row → deren airport, arr-Row → Ziel), Datum = Servicedatum der Row,
    Mitternachts-Wrap est<sched−4h → +1 Tag. Unnormalisierbares bleibt roh."""
    def _s(v):
        v = v.strip() if isinstance(v, str) else v
        return v or None

    def _time_pair(row, iata):
        # (sched, esti) der Row → normalisierte ISO-Strings (oder Rohwert).
        from datetime import timedelta as _td
        svc = (row.get('date') or '')[:10]
        sv, ev = _s(row.get('sched')), _s(row.get('esti'))
        out_s = out_e = None
        s_dt = None
        if sv is not None:
            s_dt, _ = _obs_station_dt(sv, iata, svc)
            out_s = s_dt.isoformat() if s_dt is not None else sv
        if ev is not None:
            e_dt, e_had_date = _obs_station_dt(ev, iata, svc)
            if (e_dt is not None and s_dt is not None and not e_had_date
                    and (s_dt - e_dt).total_seconds() > 4 * 3600):
                e_dt += _td(days=1)          # Mitternachts-Wrap (est nach 00:00)
            out_e = e_dt.isoformat() if e_dt is not None else ev
        return out_s, out_e

    facts = {}
    if dep_row:
        _dep_ap = (_s(dep_row.get('airport')) or '').split('#', 1)[0] or None
        sd, ed = _time_pair(dep_row, _dep_ap)
        if sd is not None:
            facts['sched_dep'] = sd
        if ed is not None:
            facts['est_dep'] = ed
        for out_k, in_k in (('gate', 'gate'), ('terminal', 'terminal'),
                            ('dep_status', 'status'), ('reg', 'reg'),
                            ('type', 'type_code')):
            v = _s(dep_row.get(in_k))
            if v is not None:
                facts[out_k] = v
        if dep_row.get('max_delay_min') is not None:
            facts['dep_delay_min'] = dep_row.get('max_delay_min')
        if dep_row.get('cancelled'):
            facts['cancelled'] = True
        # Route steckt in der DEP-Row: airport=Start, dest_iata=Ziel.
        _o = _s(dep_row.get('airport'))
        if _o:
            facts['dep_iata'] = _o.split('#', 1)[0]
        _d = _s(dep_row.get('dest_iata'))
        if _d:
            facts['arr_iata'] = _d
    if arr_row:
        # ARR-Row: airport='<Ziel>#ARR' → Station-TZ der Ankunftsseite = Ziel.
        _arr_ap = (_s(arr_row.get('airport')) or '').split('#', 1)[0] or None
        sa, ea = _time_pair(arr_row, _arr_ap)
        if sa is not None:
            facts['sched_arr'] = sa
        if ea is not None:
            facts['est_arr'] = ea
        for out_k, in_k in (('arr_gate', 'gate'), ('arr_terminal', 'terminal'),
                            ('arr_status', 'status')):
            v = _s(arr_row.get(in_k))
            if v is not None:
                facts[out_k] = v
        if arr_row.get('max_delay_min') is not None:
            facts['arr_delay_min'] = arr_row.get('max_delay_min')
        if arr_row.get('cancelled'):
            facts['cancelled'] = True
        # Route auch aus der ARR-Row ableitbar (falls keine DEP-Row): airport=Ziel+#ARR,
        # dest_iata=Herkunft. Nur setzen, wenn die DEP-Row sie nicht schon lieferte.
        _a = _s(arr_row.get('airport'))
        if _a and not facts.get('arr_iata'):
            facts['arr_iata'] = _a.split('#', 1)[0]
        _oa = _s(arr_row.get('dest_iata'))
        if _oa and not facts.get('dep_iata'):
            facts['dep_iata'] = _oa
    return facts


def _flight_facts_from_obs(flight_no, date, dep_iata=None, arr_iata=None):
    """Board-Fakten EINES Flugs aus airport_delay_obs (DEP + <arr>#ARR gemergt).
    Timeout-sicher (eigener try, Fehler → {}), indizierte Filter (date+flight)."""
    fn = (flight_no or '').replace(' ', '').upper().strip()
    d = ((date or '').strip()[:10]) or time.strftime('%Y-%m-%d', time.gmtime())
    if not fn:
        return {}
    sb = _sb()
    if sb is None:
        return {}
    dep = (dep_iata or '').upper().strip() or None
    arr = (arr_iata or '').upper().strip() or None
    from datetime import datetime as _dt, timedelta as _td
    try:
        yday = (_dt.strptime(d, '%Y-%m-%d') - _td(days=1)).strftime('%Y-%m-%d')
    except Exception:
        yday = None
    dates = [d] + ([yday] if yday else [])
    try:
        q = (sb.table('airport_delay_obs')
             .select('airport,flight,dest_iata,sched,esti,gate,terminal,'
                     'status,max_delay_min,cancelled,reg,type_code,date')
             .in_('date', dates).eq('flight', fn)
             .order('updated_at', desc=True).limit(20).execute())
    except Exception:
        return {}
    dep_cands, arr_cands = [], []
    for r in (q.data or []):
        ap = (r.get('airport') or '').upper()
        if ap.endswith('#ARR'):
            if arr is None or ap == arr + '#ARR':
                arr_cands.append(r)
        elif dep is None or ap == dep:
            dep_cands.append(r)

    def _best(cands):
        # (1) ANGEFRAGTES Datum bevorzugen — sonst kann eine Vortags-Beobachtung
        #     desselben täglichen Flugs eine falsche (gestrige) Ist-Zeit liefern.
        # (2) dann die inhaltsreichste Row (Gate, dann Soll) — nicht blind die jüngste.
        if not cands:
            return None
        same_day = [r for r in cands if (r.get('date') or '')[:10] == d]
        pool = same_day or cands
        for r in pool:
            if (r.get('gate') or '').strip():
                return r
        for r in pool:
            if (r.get('sched') or '').strip():
                return r
        return pool[0]

    best_dep, best_arr = _best(dep_cands), _best(arr_cands)
    facts = _obs_rows_to_facts(best_dep, best_arr)
    # Transparenz: stammen die gewählten Rows NICHT vom angefragten Datum
    # (Overnight-Fallback auf yday), das ehrlich markieren — Consumer können
    # gestrige Ist-Zeiten dann als potenziell veraltet behandeln.
    if facts:
        _dates = [(r.get('date') or '')[:10] for r in (best_dep, best_arr)
                  if r is not None and (r.get('date') or '')[:10]]
        _off = [x for x in _dates if x != d]
        if _off and not any(x == d for x in _dates):
            facts['stale'] = True
            facts['obs_date'] = min(_off)
    return facts


_FREE_TIMES_MEMO = {}          # (flight,date) -> (times_dict_or_None, at)
_FREE_TIMES_TTL = 300          # s — Drossel: max 1 gRPC/paid pro Flug/Tag alle 5min


def _epoch_to_local_iso(epoch, iata):
    """UNIX-Epoch (UTC) → naiver Stationszeit-ISO am Flughafen `iata` (Format wie die
    Board-Obs: '2026-07-09T17:35:00'). None bei ungültig/unbekannter TZ."""
    try:
        e = int(epoch)
    except (TypeError, ValueError):
        return None
    if e <= 0:
        return None
    try:
        from airport_tz import airport_tz
        from zoneinfo import ZoneInfo
        from datetime import datetime as _dt2, timezone as _tz2
        tzn = airport_tz((iata or '').upper()) or 'UTC'
        loc = _dt2.fromtimestamp(e, tz=_tz2.utc).astimezone(ZoneInfo(tzn))
        return loc.strftime('%Y-%m-%dT%H:%M:%S')
    except Exception:
        return None


def _grpc_times_free(callsign, origin, dest):
    """GRATIS sched_dep/sched_arr/eta (Epoch) via FR24-gRPC-Korridor entlang
    origin→dest — KEIN Credit. Airborne + kurze/mittlere Route nötig (Langstrecke =
    Riesenkorridor → Miss, dann Paid-Backup). None wenn nicht gefunden."""
    cs = (callsign or '').strip().upper()
    if not cs or not origin or not dest:
        return None
    o = _airport_row(origin) or {}
    d = _airport_row(dest) or {}
    if None in (o.get('lat'), o.get('lon'), d.get('lat'), d.get('lon')):
        return None
    # Korridor funktioniert nur kurz/mittel — Langstrecke = Riesenbox → immer Miss.
    # Dann gar nicht erst gRPC (spart ~1s), direkt Paid-Backup. Grenze ~3500 km.
    try:
        from math import radians, sin, cos, asin, sqrt
        dlat = radians(d['lat'] - o['lat'])
        dlon = radians(d['lon'] - o['lon'])
        a = (sin(dlat / 2) ** 2 + cos(radians(o['lat'])) * cos(radians(d['lat']))
             * sin(dlon / 2) ** 2)
        if 2 * 6371 * asin(sqrt(a)) > 3500:
            return None
    except Exception:
        pass
    try:
        from blueprints import fr24_grpc
        return fr24_grpc.inbound_by_route(o['lat'], o['lon'], d['lat'], d['lon'],
                                          callsign=cs)
    except Exception:
        return None


def _flight_times_free_first(flight_no, date, origin, dest,
                             callsign=None, allow_paid=False):
    """DIE zentrale Zeiten-Auflösung (Owner: free FR24 zuerst, paid nur Backup, an
    EINER Stelle). Free gRPC (gratis, Epoch→stationslokal normalisiert) → paid
    flight-summary NUR wenn free leer UND allow_paid. In-Process-Drossel 5min/Flug
    (kein Hämmern, geteilt über alle Screens). Returns {sched_dep,est_dep,sched_arr,
    est_arr} stationslokal — oder {}."""
    fn = (flight_no or '').replace(' ', '').upper()
    d = ((date or '')[:10]) or time.strftime('%Y-%m-%d', time.gmtime())
    if not fn:
        return {}
    key = (fn, d)
    now = time.time()
    hit = _FREE_TIMES_MEMO.get(key)
    if hit is not None and (now - hit[1]) < _FREE_TIMES_TTL:
        return hit[0] or {}
    cs = (callsign or '').strip().upper()
    if not cs:
        i = 0
        while i < len(fn) and not fn[i].isdigit():
            i += 1
        al = _airline_row(fn[:i]) or {}
        if al.get('icao') and fn[i:]:
            cs = al['icao'] + fn[i:]
    out = {}
    # 1) FREE gRPC (gratis) — Korridor braucht Route + Funkname
    g = _grpc_times_free(cs, origin, dest) if (cs and origin and dest) else None
    if g:
        sd = _epoch_to_local_iso(g.get('sched_dep'), origin)
        sa = _epoch_to_local_iso(g.get('sched_arr'), dest)
        ea = _epoch_to_local_iso(g.get('eta'), dest)
        if sd:
            out['sched_dep'] = sd
        if sa:
            out['sched_arr'] = sa
        if ea:
            out['est_arr'] = ea
    # 2) PAID-Backup wenn free das Paar NICHT komplett hatte (Langstrecke/gelandet).
    #    Paid liefert ISO-Z (UTC) → auf Stationszeit normalisieren (wie die Obs).
    if allow_paid and not (out.get('sched_dep') and out.get('sched_arr')):
        try:
            p = _fr24_flight_by_number(fn, date=d) or {}
        except Exception:
            p = {}

        def _norm(v, iata):
            try:
                from datetime import datetime as _dt3
                dt = _dt3.fromisoformat(str(v).replace('Z', '+00:00'))
                if dt.tzinfo is None:
                    return v
                return _epoch_to_local_iso(dt.timestamp(), iata) or v
            except Exception:
                return v
        for k in ('sched_dep', 'est_dep', 'sched_arr', 'est_arr'):
            if p.get(k) and not out.get(k):
                out[k] = _norm(p[k], origin if 'dep' in k else dest)
    _FREE_TIMES_MEMO[key] = (out or None, now)
    if len(_FREE_TIMES_MEMO) > 800:
        for k in list(_FREE_TIMES_MEMO.keys())[:400]:
            _FREE_TIMES_MEMO.pop(k, None)
    return out


def _enrich_flight_status_with_obs(flight, date=None):
    """P4-Verdrahtung: füllt fehlende Zeit-/Gate-/Status-Felder eines
    FlightStatusInfo-Dicts (resolve-flight/-callsign → iOS `schedule.info`) aus den
    geteilten Board-Fakten. So zeigt auch der Detail-Screen Soll/Ist-Ankunft + Gate,
    wenn aircraft_live/board keine Zeiten trug. Nur LEERE Felder (FR24-Wahrheit nie
    überschrieben). Die EINE Merge-Quelle (_flight_facts_from_obs) — kein neuer Pfad."""
    if not isinstance(flight, dict):
        return flight
    fn = (flight.get('flight') or '').replace(' ', '').upper()
    if not fn:
        return flight
    d = ((date or '')[:10]) or time.strftime('%Y-%m-%d', time.gmtime())
    # NUR per Flugnummer matchen (nicht per dep/arr constrainen): ein bezahlter
    # FR24-Treffer kann eine andere Leg-Route liefern → würde die Obs-Row sonst
    # verfehlen. _flight_facts_from_obs nimmt eh die jüngste DEP/ARR-Row des Flugs.
    facts = _flight_facts_from_obs(fn, d) or {}
    if facts.get('stale'):
        # Vortags-Fallback der Obs-Quelle → gestrige Ist-Zeiten/Status wären
        # Geister-Daten für den angefragten Tag. Lieber leer (free-first-Zeiten
        # unten greifen dann) als falsch.
        facts = {}

    def _fill(k, v):
        if v is not None and not flight.get(k):
            flight[k] = v
    _fill('sched_dep', facts.get('sched_dep'))
    _fill('est_dep', facts.get('est_dep'))
    _fill('sched_arr', facts.get('sched_arr'))
    _fill('est_arr', facts.get('est_arr'))
    _fill('dep_gate', facts.get('gate'))
    _fill('dep_terminal', facts.get('terminal'))
    _fill('arr_gate', facts.get('arr_gate'))
    _fill('arr_terminal', facts.get('arr_terminal'))
    _fill('status', facts.get('dep_status'))
    if flight.get('dep_delay_min') is None and facts.get('dep_delay_min') is not None:
        flight['dep_delay_min'] = facts['dep_delay_min']
    if flight.get('arr_delay_min') is None and facts.get('arr_delay_min') is not None:
        flight['arr_delay_min'] = facts['arr_delay_min']
    if not flight.get('status_category'):
        _as = (facts.get('arr_status') or '').lower()
        _ds = (facts.get('dep_status') or '').lower()
        if any(w in _as for w in ('land', 'arriv', 'gelandet', 'angekomm')):
            flight['status_category'] = 'arrived'
        elif facts.get('cancelled'):
            flight['status_category'] = 'cancelled'
        elif any(w in _ds for w in ('abgeflog', 'departed', 'airborne', 'started')):
            flight['status_category'] = 'enroute'
    # FREE-FIRST-ZEITEN (Owner: free FR24 zuerst, paid Backup, EINE Stelle): fehlen
    # nach den Board-Obs die Soll-Zeiten (typisch Auslandsflug — Abflughafen nicht
    # gescraped), hol sie ZUERST gratis via FR24-gRPC-Korridor, paid nur wenn das
    # leer bleibt. Stationslokal normalisiert. Gedrosselt/gecached (kein Hämmern).
    if not flight.get('sched_dep') and not flight.get('sched_arr'):
        _t = _flight_times_free_first(fn, d, flight.get('dep_iata'),
                                      flight.get('arr_iata'),
                                      callsign=flight.get('callsign'), allow_paid=True)
        for k in ('sched_dep', 'est_dep', 'sched_arr', 'est_arr'):
            if _t.get(k) and not flight.get(k):
                flight[k] = _t[k]
    return flight


# ─────────────────────────────────────────────────────────────────────────────
#  UNIFIED FLIGHT-INFO — P1: der EINE Resolver (Ultraplan v2)
#  Free-First-Kaskade, per Feldgruppe der beste Treffer → EIN UnifiedFlight-Dict
#  mit allem (Identität, Route, Soll/Ist-Zeiten, Status/Gate/Delay, Flugzeug) +
#  Herkunft. Consumer (Detail/Radar/Dienstplan/MyPlane/Suche) sollen künftig NUR
#  das hier lesen. FR24 (erst gratis-gRPC, dann paid) ist der on-demand-Fallback
#  (P3) — feuert NUR bei Lücke UND wenn gebraucht.
# ─────────────────────────────────────────────────────────────────────────────

_UFLIGHT_MEMO = {}
_UFLIGHT_MEMO_TTL = 60  # s — kurz, damit Ist-Zeiten/Delay frisch bleiben
_UFLIGHT_PAID_MISS = {}             # (q,date,cs?) → ts — Paid-Versuch blieb leer
_UFLIGHT_PAID_MISS_TTL = 12 * 3600  # 12 h: unauflösbarer Query re-spendet nicht


def resolve_unified_flight(query, date=None, callsign_query=False,
                           lat=None, lon=None, allow_paid=False):
    """Read-Through (P2): 60s In-Process-Memo vor dem Free-First-Zusammenbau. Der
    BEZAHLTE Teil (FR24 in der Route-Kaskade) wird zusätzlich von route_for_flight
    persistent hart gecached (_record_resolved_route) → zero double-spend übersteht
    auch Worker-Restarts. Reine Gratis-Reads sind billig → In-Process reicht.
    Paid-MISSES werden 12 h negativ gecacht: ein unauflösbarer Query darf nicht
    nach jedem 60s-Memo-Ablauf erneut Credits ziehen (der Free-Teil läuft weiter)."""
    q = (query or '').replace(' ', '').upper().strip()
    if not q:
        return {'ok': False, 'error': 'no_query'}
    # REG-Query (D-ABYN / N123AB): das ist KEINE Flugnummer — ehrlich found:false
    # mit identity.reg statt Fantasie-flight_no (kein Geister-Flug aus einer Reg).
    if not callsign_query and re.match(
            r'^(?:[A-Z]{1,2}-[A-Z]{2,4}|N\d{1,5}[A-Z]{0,2})$', q):
        return {'ok': True, 'found': False, 'query': q,
                'date': ((date or '')[:10]) or None,
                'identity': {'callsign': None, 'flight_no': None, 'reg': q}}
    # Default-Datum STATION-LOKAL statt UTC (nachts nach 00 UTC zeigte gmtime
    # schon „morgen" für EU-Flüge): Heimat-Default Europe/Berlin; kennt der Core
    # den Origin, verfeinert er auf dessen TZ (date_auto).
    date_auto = not ((date or '')[:10])
    if date_auto:
        try:
            from zoneinfo import ZoneInfo
            from datetime import datetime as _dtz
            date = _dtz.now(ZoneInfo('Europe/Berlin')).strftime('%Y-%m-%d')
        except Exception:
            date = time.strftime('%Y-%m-%d', time.gmtime())
    else:
        date = (date or '')[:10]
    now = time.time()
    miss_key = (q, date, bool(callsign_query))
    if allow_paid:
        mt = _UFLIGHT_PAID_MISS.get(miss_key)
        if mt and (now - mt) < _UFLIGHT_PAID_MISS_TTL:
            allow_paid = False          # Negativ-Cache: paid war schon erfolglos
    # lat/lon (auf 1° gerundet) gehören in den Key: der Geometrie-Hint fließt in
    # die Route-Kaskade ein — ein Memo-Hit einer ANDEREN Position wäre falsch.
    _latk = round(lat) if isinstance(lat, (int, float)) else None
    _lonk = round(lon) if isinstance(lon, (int, float)) else None
    key = (q, date, bool(callsign_query), bool(allow_paid), _latk, _lonk)
    hit = _UFLIGHT_MEMO.get(key)
    if hit is not None and (now - hit[1]) < _UFLIGHT_MEMO_TTL:
        return hit[0]
    res = _resolve_unified_flight_core(q, date, callsign_query, lat, lon,
                                       allow_paid, date_auto=date_auto)
    if allow_paid and isinstance(res, dict) and not res.get('found'):
        _UFLIGHT_PAID_MISS[miss_key] = now
        if len(_UFLIGHT_PAID_MISS) > 500:
            for k in list(_UFLIGHT_PAID_MISS.keys())[:250]:
                _UFLIGHT_PAID_MISS.pop(k, None)
    _UFLIGHT_MEMO[key] = (res, now)
    if len(_UFLIGHT_MEMO) > 500:
        for k in list(_UFLIGHT_MEMO.keys())[:250]:
            _UFLIGHT_MEMO.pop(k, None)
    return res


def _resolve_unified_flight_core(q, date, callsign_query, lat, lon, allow_paid,
                                 date_auto=False):
    """Der eigentliche Free-First-Zusammenbau (q/date bereits normalisiert):
    aircraft_live (Route/Reg/Typ/Funkname) → route_for_flight (Lücken, inkl. FR24
    gratis→paid, hart gecached) → _flight_facts_from_obs (Soll/Ist-Zeiten, Gate,
    Delay, Status; trägt auch die Route aus den Obs). Nur echte Werte.
    date_auto=True: Datum war nicht angefragt → sobald der Origin bekannt ist,
    auf dessen station-lokales Heute verfeinern."""

    # 1) aircraft_live: echter Funkname + Route + Reg + Typ (gratis, wenn aktiv)
    try:
        alf = (_aircraft_live_flight(callsign=q) if callsign_query
               else _aircraft_live_flight(flight=q))
        if alf is None:
            alf = _aircraft_live_flight(callsign=q) or _aircraft_live_flight(flight=q)
    except Exception:
        alf = None
    alf = alf or {}
    callsign = alf.get('callsign') or (q if callsign_query else None)
    flight_no = alf.get('flight') or (None if callsign_query else q)
    reg = alf.get('reg')
    origin = alf.get('dep_iata')
    dest = alf.get('arr_iata')
    ac_type = alf.get('aircraft')
    route_src = 'aircraft_live' if (origin and dest) else None
    route_conf = 'confirmed' if route_src else None

    # 2) Route-Kaskade füllt Lücken (aircraft_live inaktiv → Board/Warehouse/
    #    FR24-gRPC gratis → FR24-paid nur bei allow_paid). route_for_flight cached
    #    JEDEN Treffer hart (auch den bezahlten) → „nur wenn man es braucht" + kein
    #    Doppel-Verbrauch. Funkname aus Flugnummer ableiten, damit die Kaskade
    #    (die einen Callsign braucht) auch bei reiner Flugnummer FR24 erreichen kann.
    callsign_derived = False
    if not (origin and dest):
        if not callsign and flight_no:
            _i = 0
            while _i < len(flight_no) and not flight_no[_i].isdigit():
                _i += 1
            _pfx, _num = flight_no[:_i], flight_no[_i:]
            _al = _airline_row(_pfx) or {}
            if _al.get('icao') and _num:
                # ABGELEITET (ICAO+Nummer) — kann falsch sein (LH1412=DLH8UA):
                # als derived markieren und den PAID-Tier dafür sperren (kein
                # Credit-Spend auf einen geratenen Funknamen).
                callsign = _al['icao'] + _num
                callsign_derived = True
        try:
            from blueprints.warehouse_reader import route_for_flight
            rt = route_for_flight(callsign=callsign or (q if callsign_query else None),
                                  reg=reg, lat=lat, lon=lon,
                                  allow_paid=(allow_paid and not callsign_derived),
                                  date=date) or {}
        except Exception:
            rt = {}
        if rt.get('src') and rt.get('dst'):
            origin, dest = rt['src'], rt['dst']
            route_src = rt.get('source')
            route_conf = rt.get('confidence')
            reg = reg or rt.get('reg')

    # Datum war nicht angefragt (date_auto): jetzt, wo der Origin bekannt sein
    # kann, auf DESSEN station-lokales Heute verfeinern (Berlin war nur Heimat-
    # Default) — sonst matcht ?date=heute nachts das falsche Servicedatum.
    if date_auto and origin:
        try:
            from zoneinfo import ZoneInfo
            from airport_tz import airport_tz as _atz
            from datetime import datetime as _dtz
            _tzn = _atz((origin or '').upper())
            if _tzn:
                date = _dtz.now(ZoneInfo(_tzn)).strftime('%Y-%m-%d')
        except Exception:
            pass

    # 3) Board-Fakten (Soll/Ist Ab+Ankunft, Gate, Delay, Status) — die eine Quelle
    facts = {}
    if flight_no:
        facts = _flight_facts_from_obs(flight_no, date, dep_iata=origin, arr_iata=dest)
    reg = reg or facts.get('reg')
    ac_type = ac_type or facts.get('type')
    # Route aus den Board-Fakten, wenn aircraft_live + Kaskade nichts hatten (die
    # Obs kennen Start/Ziel: DEP-Row airport→dest_iata). Board = confirmed.
    if not (origin and dest) and facts.get('dep_iata') and facts.get('arr_iata'):
        origin, dest = facts['dep_iata'], facts['arr_iata']
        route_src = route_src or 'airport_delay_obs'
        route_conf = route_conf or 'confirmed'

    if not (origin and dest) and not facts:
        return {'ok': True, 'found': False, 'query': q, 'date': date}

    def _ap(code):
        r = (_airport_row(code) or {}) if code else {}
        return {'iata': (r.get('iata') or code), 'icao': r.get('icao'),
                'city': r.get('city'), 'name': r.get('name'),
                'lat': r.get('lat'), 'lon': r.get('lon')}

    out = {
        'ok': True, 'found': True, 'query': q, 'date': date,
        'identity': {'callsign': callsign, 'flight_no': flight_no, 'reg': reg,
                     'callsign_derived': callsign_derived or None},
        'route': ({'origin': _ap(origin), 'destination': _ap(dest)}
                  if (origin and dest) else None),
        'times': {k: facts.get(k) for k in
                  ('sched_dep', 'est_dep', 'sched_arr', 'est_arr')},
        'status': {
            'gate': facts.get('gate'), 'terminal': facts.get('terminal'),
            'arr_gate': facts.get('arr_gate'), 'arr_terminal': facts.get('arr_terminal'),
            'dep_status': facts.get('dep_status'), 'arr_status': facts.get('arr_status'),
            'dep_delay_min': facts.get('dep_delay_min'),
            'arr_delay_min': facts.get('arr_delay_min'),
            'cancelled': facts.get('cancelled'),
        },
        'aircraft': {'reg': reg, 'type': ac_type},
        'meta': {'route_source': route_src, 'route_confidence': route_conf,
                 'facts_source': 'airport_delay_obs' if facts else None},
    }
    # Facts stammen vom Vortag (Overnight-Fallback) → transparent markieren.
    if facts.get('stale'):
        out['stale'] = True
        out['obs_date'] = facts.get('obs_date')
    return out


@aerox_data_bp.route('/api/ax/uflight/<query>', methods=['GET'])
def ax_unified_flight(query):
    """DER eine Unified-Read (Ultraplan v2): alles zu einem Flug an einem Ort.
    Eigener Pfad `/api/ax/uflight/` — der alte `/api/ax/flight/<flightno>` (nur
    Airline+Route via adsbdb) bleibt unangetastet; Consumer wandern in P4 hierher.
    ?date=YYYY-MM-DD ?callsign=1 (Query ist Funkname) ?lat=&lon= (Geometrie-Hint)
    ?paid=1 (erlaubt die Route-Kaskade bezahlt zu ziehen; Default gratis —
    zählt NUR mit Authorization-Bearer-Header: anonym darf nie Credits schalten)."""
    from flask import request
    if _ax_rate_limited('ax_unified_flight', limit=120, window_sec=60):
        return jsonify({'ok': False, 'error': 'rate_limited'}), 429
    date = request.args.get('date')
    callsign_query = (request.args.get('callsign') or '') in ('1', 'true', 'yes')
    lat = request.args.get('lat', type=float)
    lon = request.args.get('lon', type=float)
    # paid nur für App-Clients (Bearer-Header vorhanden). Kein voller Token-
    # Check nötig — es geht darum, dass ein anonymer Scan ?paid=1 nicht zieht.
    _authed = (request.headers.get('Authorization') or '').strip().lower() \
        .startswith('bearer ')
    allow_paid = _authed and (request.args.get('paid') or '') in ('1', 'true', 'yes')
    return jsonify(resolve_unified_flight(
        query, date=date, callsign_query=callsign_query,
        lat=lat, lon=lon, allow_paid=allow_paid))


@aerox_data_bp.route('/api/ax/tail-history', methods=['GET'])
def ax_tail_history():
    """„Zuletzt geflogen" EINER Maschine: die letzten ~10 Legs by Tail/Reg aus
    dem Flight-Warehouse (`flights`, board-verifiziert). Für die Kennzeichen-
    Detailseite, wenn die Maschine gerade NICHT sendet — statt leerer Seite.

    Query: reg=D-AIZB (bevorzugt, hyphen-tolerant) ODER hex=<icao24>.
    Response: {ok, reg, count, legs:[{flight_no,src,dst,day,sched_dep,status}]}
    KEIN Tail-Raten: nur exakte Tail-/Hex-Matches aus dem Warehouse; leer wenn
    die Maschine dort nie beobachtet wurde.

    own=1/watch=1 (wie ax_callsign): NUR dann darf der bezahlte FR24-Fallback
    feuern — der anonyme Tap auf eine fremde Maschine bleibt Warehouse-only
    (kein Paid-per-Tap). iOS sendet own=1 für die eigene Maschine."""
    from flask import request
    # Per-IP-Limit (Kosten-Review 2026-07-09: Endpoint war ungedeckelt und
    # konnte pro Miss einen Paid-Call ziehen).
    if _ax_rate_limited('tail_history', limit=30, window_sec=60):
        return jsonify({'ok': False, 'error': 'rate_limited'}), 429
    reg = (request.args.get('reg') or '').strip().upper()
    hexid = (request.args.get('hex') or '').strip().lower()
    if not reg and not hexid:
        return jsonify({'ok': False, 'error': 'reg_or_hex_required'}), 400
    _own = (request.args.get('own') or request.args.get('watch')
            or request.args.get('mine') or '').strip().lower()
    # Bearer-Pflicht fürs Paid-Gate (Sweep 2026-07-10, Klasse A) — Muster wie
    # ax_unified_flight: anonym darf nie Credits schalten, own=1 allein reicht
    # nicht mehr. Warehouse-Teil bleibt für alle (auch Alt-Builds) unverändert.
    _authed = (request.headers.get('Authorization') or '').strip().lower() \
        .startswith('bearer ')
    allow_paid = _authed and _own in ('1', 'true', 'yes', 'y')
    sb = _sb()
    legs = []
    if sb is not None:
        try:
            q = (sb.table('flights')
                 .select('op_flight_no,origin,destination,service_date,'
                         'sched_dep,est_dep,sched_arr,est_arr,status,tail,hex'))
            if reg:
                # Gleiche hyphen-tolerante Varianten wie _route_from_warehouse
                # (Warehouse führt Tails teils mit, teils ohne Bindestrich).
                raw = reg.replace('-', '')
                variants = {reg, raw}
                if len(raw) >= 3:
                    variants.add(raw[:1] + '-' + raw[1:])
                    variants.add(raw[:2] + '-' + raw[2:])
                q = q.in_('tail', sorted(v for v in variants if v))
            else:
                q = q.eq('hex', hexid)
            rows = (q.order('service_date', desc=True)
                     .order('sched_dep', desc=True)
                     .limit(40).execute()).data or []
            seen = set()
            for f in rows:
                src, dst = f.get('origin'), f.get('destination')
                if not (src and dst):
                    continue
                key = (f.get('service_date'), f.get('op_flight_no'), src, dst)
                if key in seen:
                    continue
                seen.add(key)
                # Ankunftszeit + planmäßige Flugdauer GRATIS: die flights-Row trägt
                # sched_arr (absolut-UTC, +00:00) → _sched_block_min ist robust
                # (absolute Strings werden nicht nochmal TZ-verschoben). None wenn
                # arr fehlt oder unplausibel — nie erfunden.
                _sa = f.get('sched_arr')
                _dur = None
                if _sa:
                    _sbm = _life_app('_sched_block_min')
                    if _sbm:
                        try:
                            _dur = _sbm(f.get('sched_dep'), src, _sa, dst)
                        except Exception:
                            _dur = None
                legs.append({'flight_no': f.get('op_flight_no'),
                             'src': src, 'dst': dst,
                             'day': f.get('service_date'),
                             'sched_dep': f.get('sched_dep'),
                             'sched_arr': _sa,
                             'duration_min': _dur,
                             'status': f.get('status')})
                if len(legs) >= 10:
                    break
        except Exception:
            pass
    source = 'warehouse'
    # ── LETZTER Fallback (bezahlt, budget-gated, hart gecacht): kennt das
    #    Warehouse die Maschine NICHT (z.B. D-AIXS nie getafelt), die FR24-API
    #    by-registration fragen — NUR für die eigene/beobachtete Maschine
    #    (own=1/watch=1), nie pro anonymem Tap. Die Treffer werden PERMANENT ins
    #    Warehouse geschrieben (_crowdsource_flight_obs) → derselbe Flug kommt
    #    für alle anderen Endpoints (flight_status/route-history) GRATIS. ──
    if not legs and reg and allow_paid and _fr24_available() and _fr24_budget_ok():
        # Fenster dynamisch 3→7→14 Tage (Sweep 2026-07-10): FR24 zählt Credits
        # PRO Ergebnis — das kleine Fenster ist für aktive Maschinen deutlich
        # billiger (Kurzstrecke: 3d ≈ 15 Legs statt 10d ≈ 50), nur wirklich
        # stille Tails eskalieren. Jede (reg,days)-Stufe ist in
        # _fr24_flights_by_reg 6h (auch negativ) gecacht → zero-double-spend.
        fr = []
        for _days in (3, 7, 14):
            try:
                fr = _fr24_flights_by_reg(reg, days=_days, limit=10)
            except Exception:
                fr = []
            if fr:
                break
        if fr:
            legs = [{'flight_no': l.get('flight_no'), 'src': l.get('src'),
                     'dst': l.get('dst'), 'day': l.get('day'),
                     'sched_dep': l.get('sched_dep'), 'sched_arr': l.get('sched_arr'),
                     'duration_min': l.get('duration_min'), 'status': l.get('status')}
                    for l in fr]
            source = 'fr24'
            cs = _life_app('_crowdsource_flight_obs')
            if cs:
                for l in fr:
                    try:
                        cs(l, l.get('day'), source='fr24')
                    except Exception:
                        pass
    return jsonify({'ok': True, 'reg': reg or None, 'hex': hexid or None,
                    'count': len(legs), 'legs': legs, 'source': source})


def _iso_to_epoch(s):
    """ISO-8601 (mit +00:00 oder Z) → Epoch-Sekunden (int) | None."""
    if not s:
        return None
    try:
        from datetime import datetime, timezone
        return int(datetime.fromisoformat(str(s).replace('Z', '+00:00'))
                   .astimezone(timezone.utc).timestamp())
    except Exception:
        return None


def _flown_track_db(reg, flight_no, dep, arr, lo_iso, hi_iso):
    """Tier 1: die ECHTE geflogene Spur aus der eigenen aircraft_track-Tabelle
    (Breadcrumbs vom Harvester + FR24-Rückschreibungen). Isoliert EIN Leg:
    dep/arr-Filter falls gegeben, sonst das jüngste Leg (== origin/dest des
    letzten Punkts). Liefert (points, reg_used, dep_used, arr_used)."""
    sb = _sb()
    if sb is None:
        return [], reg, dep, arr
    if not reg and not flight_no:
        return [], reg, dep, arr

    def _fetch(col, val):
        try:
            q = (sb.table('aircraft_track').select(
                 'reg,lat,lon,alt_ft,gs_kt,track_deg,seen_ts,origin,dest,flight')
                 .eq(col, val))
            return (q.gte('seen_ts', lo_iso).lt('seen_ts', hi_iso)
                     .order('seen_ts').limit(4000).execute()).data or []
        except Exception:
            return []

    rows = _fetch('reg', reg) if reg else []
    if not rows and flight_no:
        # Reg-Query leer (Tail-Auflösung war falsch/Track flight-gekeyt) →
        # Fallback auf den Flight-Match statt sofort Großkreis.
        rows = _fetch('flight', flight_no)
    if not rows:
        return [], reg, dep, arr
    # Leg isolieren: explizit dep/arr, sonst das jüngste beobachtete Leg.
    if dep or arr:
        rows = [r for r in rows
                if (not dep or (r.get('origin') or '') == dep)
                and (not arr or (r.get('dest') or '') == arr)]
    else:
        last = rows[-1]
        lo_, ld_ = last.get('origin'), last.get('dest')
        rows = [r for r in rows if r.get('origin') == lo_ and r.get('dest') == ld_]
        dep, arr = lo_, ld_
    # LEG-ISOLIERUNG II: dieselbe Strecke kann im Fenster mehrfach geflogen sein
    # (Kurzstrecken-Rotation) — origin/dest-Filter allein mischt dann zwei Spuren.
    # An Zeitlücken >45 min splitten und das jüngste (zur Anfrage passende)
    # Segment nehmen.
    if rows:
        segs, cur, prev = [], [], None
        for r in rows:
            ts = _iso_to_epoch(r.get('seen_ts'))
            if cur and prev is not None and ts is not None and ts - prev > 45 * 60:
                segs.append(cur)
                cur = []
            cur.append(r)
            if ts is not None:
                prev = ts
        segs.append(cur)
        rows = segs[-1]
    reg_used = (rows[0].get('reg') if rows else None) or reg
    pts = [{'lat': r['lat'], 'lon': r['lon'], 'alt': r.get('alt_ft'),
            'gs': r.get('gs_kt'), 'trk': r.get('track_deg'),
            'ts': _iso_to_epoch(r.get('seen_ts'))}
           for r in rows if r.get('lat') is not None and r.get('lon') is not None]
    return pts, reg_used, dep, arr


def _flown_track_writeback(reg, trail):
    """FR24-Trail (Tier 2) dauerhaft in aircraft_track zurückschreiben → wächst.
    Idempotent via PK (reg, seen_ts). Best-effort, nie werfen."""
    sb = _sb()
    if sb is None or not reg or not trail.get('points'):
        return
    rows = []
    for p in trail['points']:
        ts = p.get('ts')
        if not ts:
            continue
        rows.append({
            'reg': reg,
            'seen_ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(int(ts))),
            'flight': trail.get('flight'),
            'origin': trail.get('origin'), 'dest': trail.get('dest'),
            'lat': p['lat'], 'lon': p['lon'],
            'alt_ft': p.get('alt_ft'), 'gs_kt': p.get('gs_kt'),
            'track_deg': p.get('track_deg'),
            'on_ground': False, 'source': 'fr24_trail',
        })
    if not rows:
        return
    try:
        sb.table('aircraft_track').upsert(
            rows, on_conflict='reg,seen_ts', ignore_duplicates=True).execute()
    except Exception:
        pass


@aerox_data_bp.route('/api/ax/flown-track', methods=['GET'])
def ax_flown_track():
    """Die ECHTE geflogene Route eines Legs als Polyline. Kaskade (billig-zuerst):
      Tier 1  aircraft_track  — eigene Breadcrumbs (auch historisch, gratis)
      Tier 2  FR24-flight_trail_list on-demand (jede Airline) + Rückschreibung
      Tier 3  Großkreis dep→arr  (source='great_circle', „approx")
    Query: reg (bevorzugt) UND/ODER flight_no + date (+ optional dep,arr zur
    Leg-Disambiguierung). Public + per-IP rate-limited (Positionsdaten, nicht
    sensibel — wie /api/ax/tail-history)."""
    from flask import request
    from blueprints.adsb_blueprint import _rate_limited, _req_ip, _great_circle_points
    if _rate_limited(ip=_req_ip(request), endpoint='flown_track', limit=60, window_sec=60):
        return jsonify({'ok': False, 'error': 'rate_limited'}), 429
    reg = re.sub(r'[^A-Z0-9]', '', (request.args.get('reg') or '').upper())
    flight_no = (request.args.get('flight_no') or request.args.get('flight') or '').strip().upper() or None
    date = (request.args.get('date') or '').strip() or None      # YYYY-MM-DD (UTC)
    dep = _norm_iata(request.args.get('dep')) if request.args.get('dep') else None
    arr = _norm_iata(request.args.get('arr')) if request.args.get('arr') else None
    if not reg and not flight_no:
        return jsonify({'ok': False, 'error': 'reg_or_flight_required'}), 400

    memo_key = ('flown_track', reg, flight_no or '', date or '', dep or '', arr or '')
    cached = _memo_get(memo_key)
    if cached is not None:
        return jsonify(cached)

    # Reg auflösen, wenn nur Flugnummer da: aircraft_track.flight ist in den
    # Prod-Daten die IATA-Nummer (LH174) — der Reg-Key ist trotzdem verlässlicher
    # (Breadcrumbs sind reg-gekeyt, flight kann fehlen). Der Warehouse (flights,
    # op_flight_no+Tag → tail) liefert die echte Maschine. Ohne ?date IMMER das
    # heutige Servicedatum erzwingen — sonst gewinnt irgendein alter Tag und der
    # Track zeigt die Spur einer fremden Rotation.
    if not reg and flight_no:
        try:
            sb = _sb()
            if sb is not None:
                fr = (sb.table('flights').select('tail')
                      .eq('op_flight_no', flight_no)
                      .eq('service_date',
                          date or time.strftime('%Y-%m-%d', time.gmtime()))
                      .order('service_date', desc=True).limit(3).execute()
                      ).data or []
                tail = next((r.get('tail') for r in fr if r.get('tail')), None)
                if tail:
                    reg = re.sub(r'[^A-Z0-9]', '', tail.upper())
        except Exception:
            pass

    # Zeitfenster: ganzer UTC-Tag (date) oder die letzten 20 h (laufender Flug).
    import datetime as _dt
    if date:
        try:
            d0 = _dt.datetime.strptime(date, '%Y-%m-%d').replace(tzinfo=_dt.timezone.utc)
            lo_iso = d0.strftime('%Y-%m-%dT00:00:00Z')
            hi_iso = (d0 + _dt.timedelta(days=1)).strftime('%Y-%m-%dT00:00:00Z')
        except Exception:
            date = None
    if not date:
        now = time.time()
        lo_iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now - 20 * 3600))
        hi_iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now + 3600))

    source = 'aircraft_track'
    points, reg_used, dep, arr = _flown_track_db(reg, flight_no, dep, arr, lo_iso, hi_iso)
    reg = reg or (reg_used or '')

    # Tier 2: zu wenig eigene Spur → FR24-Trail on-demand (+ Rückschreibung).
    # Position bevorzugt aus mitgegebenen Radar-Koordinaten (lat/lon/hex) → so
    # klappt der Trail für JEDE Airline (Radar-Tap eines fremden Fliegers), nicht
    # nur für die LH-Group-Fleet in aircraft_live. Sonst Fallback auf aircraft_live.
    is_today = (not date) or (date == time.strftime('%Y-%m-%d', time.gmtime()))
    if len(points) < 3 and is_today:
        def _f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        q_lat, q_lon = _f(request.args.get('lat')), _f(request.args.get('lon'))
        q_hex = (request.args.get('hex') or '').strip().lower() or None
        reg_disp = None
        if q_lat is None or q_lon is None:
            try:
                pos, _route, reg_disp, _ac = _aircraft_live_pos(
                    reg=reg or None, flight=flight_no, max_age_min=60)
            except Exception:
                pos = None
            if pos and pos.get('lat') is not None:
                q_lat, q_lon = pos.get('lat'), pos.get('lon')
        if q_lat is not None and q_lon is not None:
            try:
                from blueprints import fr24_grpc
                trail = fr24_grpc.flown_trail(
                    reg=reg or (reg_disp or None), hex=q_hex,
                    flight=flight_no, lat=q_lat, lon=q_lon)
            except Exception:
                trail = None
            if trail and trail.get('points'):
                tw_reg = re.sub(r'[^A-Z0-9]', '', (trail.get('reg') or reg or '').upper())
                _flown_track_writeback(tw_reg, trail)
                if len(trail['points']) >= len(points):
                    source = 'fr24_trail'
                    reg = reg or tw_reg
                    dep = dep or trail.get('origin')
                    arr = arr or trail.get('dest')
                    points = [{'lat': p['lat'], 'lon': p['lon'], 'alt': p.get('alt_ft'),
                               'gs': p.get('gs_kt'), 'trk': p.get('track_deg'),
                               'ts': p.get('ts')} for p in trail['points']]

    # Tier 3: keine echte Spur → Großkreis dep→arr (ehrlich als „approx").
    if len(points) < 2 and dep and arr:
        a, b = _iata_latlon(dep), _iata_latlon(arr)
        if a and b:
            source = 'great_circle'
            points = [{'lat': la, 'lon': lo, 'alt': None, 'gs': None, 'trk': None, 'ts': None}
                      for la, lo in _great_circle_points(a[0], a[1], b[0], b[1], 40)]

    # Endpunkte an dep/arr anbinden — NUR wenn der Track-Endpunkt schon NAH am
    # Flughafen liegt (An-/Abflugphase), damit die Linie sauber am Airport
    # beginnt/endet. NIEMALS anbinden, wenn der letzte Punkt weit weg ist: ein
    # Flug, der noch unterwegs ist, endet an seiner AKTUELLEN Position — wir
    # erfinden NICHT die Rest-Strecke bis zum Ziel (Owner: „für einen Flug, der
    # seit 1 h los ist, hast du nicht die volle Map"). So bleibt die Spur ehrlich:
    # gelandet → bis zum Zielflughafen; noch fliegend → bis zum letzten Fix.
    SNAP_KM = 150.0   # ~80 nm; darüber gilt der Endpunkt als „unterwegs"
    if source in ('aircraft_track', 'fr24_trail') and len(points) >= 2:
        if dep:
            a = _iata_latlon(dep)
            if a:
                d0 = _haversine_km(a[0], a[1], points[0]['lat'], points[0]['lon'])
                if 2.0 < d0 < SNAP_KM:
                    points.insert(0, {'lat': a[0], 'lon': a[1], 'alt': None, 'gs': None, 'trk': None, 'ts': None})
        if arr:
            b = _iata_latlon(arr)
            if b:
                d1 = _haversine_km(b[0], b[1], points[-1]['lat'], points[-1]['lon'])
                if 2.0 < d1 < SNAP_KM:
                    points.append({'lat': b[0], 'lon': b[1], 'alt': None, 'gs': None, 'trk': None, 'ts': None})

    # „Noch in der Luft?" = echte Spur, heutiges Datum, letzter Fix WEIT vom Ziel
    # (also nicht ans arr angebunden). Dann endet die Linie an der aktuellen
    # Position → iOS setzt dort einen Flugzeug-Marker (Owner: „end with airplane").
    in_flight = False
    if source in ('aircraft_track', 'fr24_trail') and points:
        today = time.strftime('%Y-%m-%d', time.gmtime())
        if (not date) or date >= today:
            bb = _iata_latlon(arr) if arr else None
            if bb:
                in_flight = _haversine_km(bb[0], bb[1], points[-1]['lat'], points[-1]['lon']) > 150.0
            # Nur „in der Luft", wenn der letzte echte Fix frisch ist (<30 min) —
            # eine Stunden-alte Spur weit vom Ziel ist ein abgerissener Track,
            # kein fliegender Flieger (kein Geister-Marker).
            if in_flight:
                _lts = next((p.get('ts') for p in reversed(points) if p.get('ts')),
                            None)
                in_flight = bool(_lts) and (time.time() - _lts) < 30 * 60

    out = {'ok': True, 'reg': reg or None, 'flight': flight_no, 'date': date,
           'dep': dep, 'arr': arr, 'source': source, 'in_flight': in_flight,
           'count': len(points), 'points': points}
    _memo_put(memo_key, out)
    resp = jsonify(out)
    # Historische, echte Spur ist unveränderlich → lange Edge-TTL; laufend/approx kurz.
    past = bool(date) and date < time.strftime('%Y-%m-%d', time.gmtime())
    ttl = 86400 if (past and source in ('aircraft_track', 'fr24_trail')) else 45
    resp.headers['Cache-Control'] = 'public, max-age=%d' % ttl
    return resp


@aerox_data_bp.route('/api/internal/track-prune', methods=['POST'])
def ax_track_prune():
    """Retention: aircraft_track-Breadcrumbs älter als TRACK_RETENTION_DAYS (10)
    löschen. Geschützt per X-Poll-Secret (== ADSB_POLL_SECRET), Cron-getriggert.
    Ohne konfiguriertes Secret 403 (kein fail-open Lösch-Endpoint). Der Delete
    läuft in 6h-Zeitscheiben (je eigener PostgREST-Call, max. 20) statt als
    Mega-DELETE — ein Riesen-Statement hielt Locks/Timeouts auf der Tabelle."""
    from flask import request
    secret = os.environ.get('ADSB_POLL_SECRET', '').strip()
    if not secret:
        return jsonify({'ok': False, 'error': 'secret_not_configured'}), 403
    if (request.headers.get('X-Poll-Secret') or '').strip() != secret:
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401
    days = int(os.environ.get('TRACK_RETENTION_DAYS', '10'))
    cutoff_epoch = time.time() - days * 86400
    cutoff = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(cutoff_epoch))
    sb = _sb()
    if sb is None:
        return jsonify({'ok': False, 'error': 'no_db'}), 503
    batches = deleted = 0
    try:
        for _ in range(20):
            # Älteste Row unterhalb des Cutoffs finden → 6h-Scheibe ab dort löschen.
            rows = (sb.table('aircraft_track').select('seen_ts')
                    .order('seen_ts').limit(1).execute()).data or []
            if not rows:
                break
            oldest = _iso_to_epoch(rows[0].get('seen_ts'))
            if oldest is None or oldest >= cutoff_epoch:
                break
            hi = time.strftime('%Y-%m-%dT%H:%M:%SZ',
                               time.gmtime(min(oldest + 6 * 3600, cutoff_epoch)))
            try:
                # return=minimal: die gelöschten Rows NICHT als Payload zurück-
                # schaufeln (eine 6h-Scheibe können zehntausende Breadcrumbs sein).
                res = (sb.table('aircraft_track')
                       .delete(count='exact', returning='minimal')
                       .lt('seen_ts', hi).execute())
            except TypeError:      # ältere postgrest-py ohne die Kwargs
                res = sb.table('aircraft_track').delete().lt('seen_ts', hi).execute()
            batches += 1
            try:
                n = getattr(res, 'count', None)
                deleted += (int(n) if n is not None
                            else len(getattr(res, 'data', None) or []))
            except Exception:
                pass
        return jsonify({'ok': True, 'pruned_before': cutoff, 'days': days,
                        'batches': batches, 'deleted': deleted})
    except Exception as e:
        return jsonify({'ok': False, 'error': type(e).__name__,
                        'batches': batches, 'deleted': deleted}), 500


def _fr24_prewarm_mark_get(icao):
    """Prewarm-Watermark (Epoch der letzten abgedeckten Fenstergrenze) pro
    Airline-ICAO — persistiert im ax_api_budget-KV (key='fr24pw:<ICAO>', n=Epoch,
    gleiche Tabelle wie die Budget-Keys → KEINE neue Tabelle). 0 wenn unbekannt."""
    return _budget_key_used('fr24pw:' + (icao or '').strip().upper())


def _fr24_prewarm_mark_set(icao, epoch):
    """Watermark auf max(bestehend, epoch) heben. Direkter Upsert statt
    ax_budget_increment-RPC: der RPC zählt nur hoch — eine Watermark ist aber
    ein monotoner Absolutwert. max() macht den Upsert race-tolerant."""
    key = 'fr24pw:' + (icao or '').strip().upper()
    try:
        e = int(epoch)
    except (TypeError, ValueError):
        return
    _MEM_BUDGET[key] = max(int(_MEM_BUDGET.get(key, 0)), e)
    sb = _sb()
    if sb is None:
        return
    try:
        sb.table('ax_api_budget').upsert(
            {'month': key, 'n': max(e, _budget_key_used(key)),
             'updated_at': _iso_now()}).execute()
    except Exception:
        pass


@aerox_data_bp.route('/api/ax/fr24-prewarm', methods=['POST'])
def ax_fr24_prewarm():
    """INTERNER Warehouse-Prewarm (Owner „alle von Discover ins Buch"): holt ALLE
    Flüge einer Airline (operating_as=ICAO) EINMAL bezahlt von FR24 und schreibt
    sie permanent ins Warehouse (_crowdsource_flight_obs) → danach gratis
    nachschlagbar für alle. Geschützt wie die Poller (X-Poll-Secret == ADSB_POLL_
    SECRET; ohne gesetztes Secret nur localhost). Query: ?icao=OCN&days=2.
    Antwort: {ok, icao, fetched, imported, credits_used}."""
    import os as _os
    from flask import request
    secret = (_os.environ.get('ADSB_POLL_SECRET') or '').strip()
    if secret:
        if (request.headers.get('X-Poll-Secret') or '').strip() != secret:
            return jsonify({'ok': False, 'error': 'unauthorized'}), 401
    else:
        ra = (request.remote_addr or '')
        if ra not in ('127.0.0.1', '::1', 'localhost'):
            return jsonify({'ok': False, 'error': 'localhost_only'}), 403
    icao = (request.args.get('icao') or '').strip().upper()
    if len(icao) < 2:
        return jsonify({'ok': False, 'error': 'icao_required'}), 400
    try:
        days = max(1, min(int(request.args.get('days') or 2), 3))
    except Exception:
        days = 2
    # Watermark (persistiert): bereits bezahlte Zeitfenster nicht nochmal holen —
    # wiederholte Cron-Läufe re-spendeten sonst denselben Zeitraum. from_override=1
    # ignoriert die Watermark bewusst (voller days-Rückblick, z.B. nach Datenloch).
    force = ((request.args.get('from_override') or request.args.get('force') or '')
             .strip().lower() in ('1', 'true', 'yes'))
    t_from = None
    if not force:
        wm = _fr24_prewarm_mark_get(icao)
        if wm:
            t_from = float(wm)
    before = _budget_key_used(_fr24_budget_key())
    cover = {}
    legs = _fr24_flights_by_airline(icao, days=days, t_from=t_from,
                                    cover_out=cover)
    cs = _life_app('_crowdsource_flight_obs')
    imported = 0
    if cs:
        for l in legs:
            try:
                if cs(l, l.get('day'), source='fr24'):
                    imported += 1
            except Exception:
                pass
    out = {'ok': True, 'icao': icao, 'days': days,
           'fetched': len(legs), 'imported': imported,
           'credits_used': _budget_key_used(_fr24_budget_key()) - before}
    if cover.get('to'):
        _fr24_prewarm_mark_set(icao, cover['to'])
        out['covered_to'] = time.strftime('%Y-%m-%dT%H:%M:%SZ',
                                          time.gmtime(cover['to']))
    return jsonify(out)


def _paid_prefix_plausible(q):
    """Plausi-Gate vor dem Paid-Zweig der resolve-Endpoints: das Airline-Präfix
    (ICAO 3 / IATA 2) des Queries muss in der airlines-Referenz existieren —
    Fantasie-/Scan-Queries erreichen FR24 so gar nicht erst (jeder Miss kostete
    trotzdem Credits)."""
    s = (q or '').strip().upper()
    for n in (3, 2):
        if len(s) > n and _airline_row(s[:n]):
            return True
    return False


@aerox_data_bp.route('/api/ax/resolve-callsign/<callsign>', methods=['GET'])
def ax_resolve_callsign(callsign):
    """Funkname (ICAO-Callsign wie OCN601/DLH7AV) → TATSÄCHLICHER Flug (Route/Reg/
    Typ/echte IATA-Nummer) über FR24 by-callsign — die eindeutige Wahrheit, wenn
    die freie IATA-Nummer-Zuordnung unsicher/verwechselbar ist (Owner 2026-07-09:
    „4Y601" ≠ Callsign „OCN601"). Löst zugleich „Suche DLH7AV → keine Treffer".
    Ergebnis wird permanent ins Warehouse gespiegelt. Hart gecacht (Zero-Double-
    Spend). {ok, callsign, flight, source}."""
    from flask import request
    if _ax_rate_limited('resolve_callsign', limit=30, window_sec=60):
        return jsonify({'ok': False, 'error': 'rate_limited'}), 429
    cs = (callsign or '').strip().upper()
    if len(cs) < 3:
        return jsonify({'ok': False, 'error': 'bad_callsign'}), 400
    date_q = (request.args.get('date') or '').strip()[:10] or None
    # FREE-FIRST: aktiver Flug gratis im aircraft_live (by callsign) → kein FR24.
    # BEWUSST kein Obs-Vorab-Check per abgeleiteter IATA-Nummer: die naive
    # Präfix+Suffix-Ableitung (OCN601→„4Y601") ist genau die Verwechslung, die
    # dieser Endpoint fixt (echte IATA wäre 4Y60) — lieber paid-Wahrheit als
    # gratis-falsch (kein Geister-Flieger).
    flight = _aircraft_live_flight(callsign=cs)
    src = 'aircraft_live'
    if not flight:
        # Paid nur hinter dem Plausi-Gate (Airline-Präfix muss existieren).
        flight = _fr24_flight_by_callsign(cs) if _paid_prefix_plausible(cs) else None
        src = 'fr24'
    if flight:
        if src == 'fr24':
            cs_fn = _life_app('_crowdsource_flight_obs')
            if cs_fn:
                try:
                    cs_fn(flight, None, source='fr24')
                except Exception:
                    pass
        # P4: Soll/Ist-Zeiten+Gate — ?date= (z.B. vom Detail-Aggregat) durchreichen
        flight = _enrich_flight_status_with_obs(flight, date=date_q)
        return jsonify({'ok': True, 'callsign': cs, 'flight': flight,
                        'source': src})
    return jsonify({'ok': False, 'callsign': cs, 'error': 'not_found'}), 200


@aerox_data_bp.route('/api/ax/resolve-flight/<flightno>', methods=['GET'])
def ax_resolve_flight(flightno):
    """Flugnummer (z.B. LH1412) → tatsächlicher Flug via FR24 by-number: echte
    Route + Reg + **echter Funkname** (Owner 2026-07-09: LH1412 fliegt als „DLH8UA",
    NICHT „DLH1412" → freie Callsign-Ableitung ergab falsche Route FRA→SPU statt
    FRA→BEG und keine Live-Position). iOS nimmt Route/Reg als Top-Wahrheit und den
    echten Callsign für die adsb.lol-Live-Position. Permanent gespiegelt, gecacht.
    {ok, number, flight, source}."""
    from flask import request
    if _ax_rate_limited('resolve_flight', limit=30, window_sec=60):
        return jsonify({'ok': False, 'error': 'rate_limited'}), 429
    fn = (flightno or '').strip().upper().replace(' ', '')
    if len(fn) < 3:
        return jsonify({'ok': False, 'error': 'bad_flight'}), 400
    date_q = (request.args.get('date') or '').strip()[:10] or None
    # FREE-FIRST: aktiver Flug steht mit echtem Funknamen gratis im aircraft_live
    # (gRPC-Scraper) → kein FR24-Credit. FR24 nur wenn nicht aktiv/geharvestet.
    flight = _aircraft_live_flight(flight=fn)
    src = 'aircraft_live'
    if not flight:
        # FREE zuerst (Owner „free FR24 mit Backup paid"): kennt der Board-Scrape die
        # Route (Obs), bauen wir ein Minimal-Dict → der Enrich holt die Zeiten gratis
        # via FR24-gRPC. Paid erst, wenn nicht mal die Route frei bekannt ist.
        _ff = _flight_facts_from_obs(fn, date_q)
        if _ff.get('dep_iata') and _ff.get('arr_iata'):
            flight = {'ok': True, 'found': True, 'flight': fn,
                      'dep_iata': _ff['dep_iata'], 'arr_iata': _ff['arr_iata'],
                      'reg': _ff.get('reg'), 'aircraft': _ff.get('type'),
                      'status': _ff.get('dep_status') or '', 'status_category': ''}
            src = 'aerox_obs'
        else:
            # Paid nur hinter dem Plausi-Gate (Airline-Präfix muss existieren —
            # Fantasie-/Scan-Queries erreichen FR24 nicht, jeder Miss kostete).
            flight = (_fr24_flight_by_number(fn)
                      if _paid_prefix_plausible(fn) else None)
            src = 'fr24'
    if flight:
        if src == 'fr24':          # nur bezahlte Auflösung permanent spiegeln
            cs_fn = _life_app('_crowdsource_flight_obs')
            if cs_fn:
                try:
                    cs_fn(flight, None, source='fr24')
                except Exception:
                    pass
        # P4: Soll/Ist-Zeiten+Gate — ?date= (z.B. vom Detail-Aggregat) durchreichen
        flight = _enrich_flight_status_with_obs(flight, date=date_q)
        return jsonify({'ok': True, 'number': fn, 'flight': flight, 'source': src})
    return jsonify({'ok': False, 'number': fn, 'error': 'not_found'}), 200


def _detail_subcall(app_obj, path, view_fn, *view_args):
    """Ruft eine bestehende /api/ax/*-View intern auf (eigener Request-Kontext, damit
    ihre `request.args` + `_public_cache_headers(request.path)` funktionieren) und gibt
    ihr JSON-Dict zurück. So bündelt das Aggregat die Einzel-Endpoints OHNE Netz-
    Roundtrip und OHNE ihre Logik zu duplizieren (Zero-Double-Spend: dieselben Calls,
    dieselben Caches). Fehler/None → None; ein Ausfall darf das Bündel nie kippen.

    `app_obj` MUSS das konkrete App-Objekt sein (nicht `current_app`): die Fan-out-
    Calls laufen in ThreadPool-Workern OHNE gepushten App-Kontext — `test_request_
    context` auf dem echten App-Objekt pusht App- UND Request-Kontext im Worker selbst."""
    if not view_fn or app_obj is None:
        return None
    try:
        with app_obj.test_request_context(path):
            resp = view_fn(*view_args)
            if isinstance(resp, tuple):          # (jsonify(...), status) — z.B. 404
                resp = resp[0]
            if hasattr(resp, 'get_json'):
                return resp.get_json(silent=True)
    except Exception:
        return None
    return None


def _route_history_windowed(app_obj, origin, dest):
    """route-history mit DYNAMISCHEM Fenster (Sweep 2026-07-10): days=3 ist der
    Latenz-Sweet-Spot, aber dünne Strecken (FRA-NBJ, 2×/Woche) haben in 3 Tagen
    oft 0 Beobachtungen → bei 0 Treffern automatisch auf 7 Tage weiten. Mehr
    kappt der Endpoint selbst (app.ax_route_history clamped ?days auf 7 — ein
    14er-Call wäre nur ein identischer Doppel-Call). Weiterhin ausschließlich
    eigene Beobachtungen, 0 Spend. None wenn auch das weite Fenster nichts hat
    UND der Call scheiterte (ok-aber-leer wird durchgereicht, ehrlich)."""
    view = _life_app('ax_route_history')
    h = None
    for nd in (3, 7):
        h = _detail_subcall(app_obj, '/api/ax/route-history/%s/%s?days=%d'
                            % (urllib.parse.quote(origin),
                               urllib.parse.quote(dest), nd),
                            view, origin, dest)
        if (h or {}).get('ok') and (h.get('total') or 0) > 0:
            return h
    return h if (h or {}).get('ok') else None


@aerox_data_bp.route('/api/ax/flight-detail/<query>', methods=['GET'])
def ax_flight_detail(query):
    """EIN-Call-Aggregat für die Flug-Detailseite (Owner 2026-07-09: „Detailseite lädt
    gestückelt und langsam"). Bündelt die bisher SECHS Einzel-Endpoints — resolve-
    flight/-callsign + flight-info + flight-route + route-history + photo-reg — server-
    seitig in EINE Antwort. Statt sechs Handy→Backend-Roundtrips nur noch einer, und die
    Karten erscheinen zusammen statt einzeln einzufaden. Backend↔Quelle ist co-located
    (Supabase-Pooler/Warehouse), also viel schneller als das Gerät ×6.

    Die teure Live-Position (adsb.lol/FR24-gRPC) bleibt BEWUSST draussen — iOS lädt sie
    getrennt/non-blocking, damit der Screen nicht auf den langsamsten Anbieter wartet.
    Alles free-first (identisch zu den Einzel-Views, kein neuer Spend), 45s-memoisiert.
    Query = IATA-Flugnummer (LH1412) oder — mit ?callsign=1 — roher ICAO-Funkname
    (DLH7AV). Sub-Objekte spiegeln 1:1 die bestehenden Endpoint-Shapes."""
    from flask import request
    q = (query or '').strip().upper().replace(' ', '')
    if len(q) < 3:
        return jsonify({'ok': False, 'error': 'bad_query'}), 400
    if _ax_rate_limited('flight_detail', limit=90, window_sec=60):
        return jsonify({'ok': False, 'error': 'rate_limited'}), 429
    is_cs = str(request.args.get('callsign', '')).lower() in ('1', 'true', 'yes')
    date_q = (request.args.get('date') or '').strip() or None
    fresh = str(request.args.get('fresh', '')).lower() in ('1', 'true', 'yes')

    memo_key = ('flight_detail', q, '1' if is_cs else '0', date_q or '')
    if not fresh:
        cached = _memo_get(memo_key)
        if cached is not None:
            return jsonify(cached)

    def _qs(**kw):
        parts = ["%s=%s" % (k, urllib.parse.quote(str(v))) for k, v in kw.items() if v]
        return ("?" + "&".join(parts)) if parts else ""

    def _pick(*vals):
        for v in vals:
            if v:
                s = str(v).strip().upper()
                if s:
                    return s
        return None

    out = {'ok': True, 'query': q, 'callsign_query': is_cs, 'date': date_q}

    from flask import current_app
    _app = current_app._get_current_object()   # konkretes App-Objekt für die Worker-Threads

    # Die Quellen als abgeschlossene Callables (jede isoliert via _detail_subcall).
    def _resolve_call():
        # ?date= mitgeben: der resolve-Enrich (_flight_facts_from_obs) matcht
        # sonst immer nur „heute", auch wenn das Aggregat einen Tag anfragt.
        if is_cs:
            return _detail_subcall(_app, '/api/ax/resolve-callsign/%s%s'
                                   % (urllib.parse.quote(q), _qs(date=date_q)),
                                   ax_resolve_callsign, q)
        return _detail_subcall(_app, '/api/ax/resolve-flight/%s%s'
                               % (urllib.parse.quote(q), _qs(date=date_q)),
                               ax_resolve_flight, q)

    def _info_call(fn):
        i = _detail_subcall(_app, '/api/ax/flight-info/%s%s' % (urllib.parse.quote(fn), _qs(date=date_q)),
                            _life_app('ax_flight_info'), fn)
        return i if (i or {}).get('found') else None

    def _route_call(cs):
        r = _detail_subcall(_app, '/api/ax/flight-route/%s%s' % (urllib.parse.quote(cs), _qs(date=date_q)),
                            _life_app('ax_flight_route'), cs)
        return r if (r or {}).get('found') else None

    def _history_call(o, d):
        # days=3 statt 7 (Owner 2026-07-09 „Detail 13s"): route-history ist der
        # Latenz-Pol (FRA/JFK 7d=4.4s vs 3d~1.1s — Dual-Side-Board-Merge × Tage).
        # Bei 0 Treffern weitet _route_history_windowed das Fenster automatisch
        # (dünne Strecken wie FRA-NBJ bekämen sonst NIE Flugzeit/Landung).
        return _route_history_windowed(_app, o, d)

    def _photo_call(rg):
        p = _detail_subcall(_app, '/api/ax/photo-reg/%s' % urllib.parse.quote(rg), ax_photo_reg, rg)
        return p if (p or {}).get('ok') else None

    # PARALLEL FAN-OUT — die einzige echte Abhängigkeit ist der ECHTE Funkname aus
    # `resolve` (treibt flight-route). Alles andere überlappt. Wall-Clock ≈ der lange
    # Pol (resolve/FR24-Fallback bei nicht-fliegenden Flügen), nicht die Summe — sonst
    # wäre das Aggregat langsamer als die 6 parallelen Handy-Calls von vorher. Flask-
    # Kontexte sind thread-local; jeder _detail_subcall pusht seinen eigenen.
    from concurrent.futures import ThreadPoolExecutor

    def _res(f, timeout=10):
        # Timeout pro Subcall (hängender Provider darf das Bündel nicht halten);
        # Fehler wie bisher pro Subcall isolieren → None.
        if f is None:
            return None
        try:
            return f.result(timeout=timeout)
        except Exception:
            return None

    # Kein `with` (= shutdown(wait=True) würde auf den hängenden Worker warten
    # und den result-Timeout entwerten) — Worker laufen ggf. im Hintergrund aus.
    ex = ThreadPoolExecutor(max_workers=4)
    try:
        # Phase A: resolve + (flight-info schon jetzt, wenn die IATA-Nummer feststeht —
        # bei Flugnummer-Suche = die Query selbst; bei Funkname-Suche erst nach resolve).
        f_resolve = ex.submit(_resolve_call)
        f_info = ex.submit(_info_call, q) if not is_cs else None
        resolve = _res(f_resolve)
        resolve_flight = (resolve or {}).get('flight') if (resolve or {}).get('ok') else None

        fn_iata = ((resolve_flight or {}).get('flight') or '').upper().replace(' ', '') or None
        real_cs = ((resolve_flight or {}).get('callsign') or '').upper().replace(' ', '') or None
        if not fn_iata and not is_cs:
            fn_iata = q
        if not real_cs and is_cs:
            real_cs = q
        cs_for_route = real_cs or (q if is_cs else None)

        # Phase B: route + history + photo (+ info für den Funkname-Fall) — alle parallel.
        # Origin/Dest kommen aus resolve/info (NICHT aus route) → history wartet nicht
        # auf flight-route.
        if f_info is None and fn_iata:
            f_info = ex.submit(_info_call, fn_iata)
        info = _res(f_info)

        origin = _pick((resolve_flight or {}).get('dep_iata'), (info or {}).get('origin'))
        dest = _pick((resolve_flight or {}).get('arr_iata'), (info or {}).get('dest'))
        reg = _pick((resolve_flight or {}).get('reg'), (info or {}).get('reg'))

        # ROUTE lokal anreichern statt erneut auflösen (Owner 2026-07-09 „Detail 13s":
        # resolve/aircraft_live trägt die Strecke SCHON — flight-route re-resolvte sie
        # via FR24-gRPC nochmal = 5s, wenn der Callsign aus aircraft_live gefallen ist.
        # Airport-Details kommen aus der lokalen Referenz-DB (0ms). flight-route bleibt
        # NUR Fallback, falls resolve/info gar keine Strecke lieferte.
        route = None
        if origin and dest:
            def _ap(code):
                r = _airport_row(code) or {}
                return {'iata': r.get('iata') or code, 'icao': r.get('icao'),
                        'name': r.get('name'), 'city': r.get('city'),
                        'country': r.get('country'),
                        'lat': r.get('lat'), 'lon': r.get('lon')}
            _air = (_airline_row((real_cs or cs_for_route or '')[:3])
                    or _airline_row((real_cs or fn_iata or '')[:2]) or {})
            route = {'ok': True, 'found': True, 'callsign': real_cs,
                     'flight_iata': fn_iata, 'flight_icao': real_cs,
                     'airline': _air.get('name'), 'airline_iata': _air.get('iata'),
                     'airline_icao': _air.get('icao'),
                     'origin': _ap(origin), 'destination': _ap(dest)}

        f_route = (ex.submit(_route_call, cs_for_route)
                   if (route is None and cs_for_route) else None)
        f_hist = ex.submit(_history_call, origin, dest) if (origin and dest) else None
        f_photo = ex.submit(_photo_call, reg) if reg else None
        if f_route is not None:
            route = _res(f_route)
        history = _res(f_hist)
        photo = _res(f_photo)
    finally:
        ex.shutdown(wait=False)

    out['resolve'] = resolve_flight
    out['callsign'] = real_cs
    out['flight_iata'] = fn_iata
    out['route'] = route
    out['info'] = info
    out['history'] = history
    out['photo'] = photo

    # LEERES Ergebnis (weder resolve noch info) NICHT 45s memoisieren — sonst
    # klebt ein transienter Ausfall/Timeout als Negativ-Antwort im Cache.
    if resolve_flight or info:
        _memo_put(memo_key, out)
    return jsonify(out)


@aerox_data_bp.route('/api/ax/harvest-routes', methods=['POST'])
def ax_harvest_routes():
    """DEAKTIVIERT (adsbdb-Kappung, Kosten-Review 2026-07-09): der Harvester zog
    pro App-Poll bis zu 12 adsbdb-Calls — die Quelle ist unzuverlässig (Owner
    2026-07-03: „eh immer falsch") und die Routen-DB wächst längst über die
    eigenen Poller/Boards. Der Endpoint BLEIBT (alte Builds rufen ihn weiter)
    und antwortet ok — aber als No-op OHNE externe Calls und ohne DB-Reads."""
    from flask import request
    body = request.get_json(silent=True) or {}
    csigns = body.get('callsigns') or []
    if not isinstance(csigns, list):
        return jsonify({'ok': False, 'error': 'bad_body'}), 400
    return jsonify({'ok': True, 'checked': 0, 'cached': 0, 'harvested': 0})


def _airport_full(code):
    ap = _airport_row(code)
    if not ap:
        return {'iata': code}
    return {'iata': ap.get('iata'), 'icao': ap.get('icao'), 'name': ap.get('name'),
            'city': ap.get('city'), 'country': ap.get('country'),
            'lat': ap.get('lat'), 'lon': ap.get('lon')}


@aerox_data_bp.route('/api/ax/route/<frm>/<to>', methods=['GET'])
def ax_route(frm, to):
    """Städtepaar (z.B. FRA/LIS) → welche Airlines die Strecke fliegen, plus
    beide Flughäfen. Quelle: 67k-Routen-Seed (lokal, NULL API). Behebt die
    leere „FRA-LIS"-Suche."""
    a = (frm or '').strip().upper()
    b = (to or '').strip().upper()
    if len(a) < 3 or len(b) < 3:
        return jsonify({'ok': False, 'error': 'need IATA codes'}), 400
    rows = _q('SELECT DISTINCT airline FROM routes WHERE src=? AND dst=?', (a, b))
    airlines = []
    seen = set()
    for r in rows:
        code = (r.get('airline') or '').strip().upper()
        if not code or code in seen:
            continue
        seen.add(code)
        al = _airline_row(code)
        airlines.append({'iata': code, 'name': (al or {}).get('name'),
                         'icao': (al or {}).get('icao'), 'logo': _airline_logo(code)})
    airlines.sort(key=lambda x: (x['name'] is None, x['name'] or x['iata']))
    return jsonify({'ok': True, 'origin': _airport_full(a), 'destination': _airport_full(b),
                    'airlines': airlines, 'count': len(airlines)})


@aerox_data_bp.route('/api/ax/metar/<icao>', methods=['GET'])
def ax_metar(icao):
    """METAR-Wetter eines Flughafens (aviationweather.gov, frei). 10-min-Cache
    im Prozess. Für die Airport-Seite der Suche."""
    code = (icao or '').strip().upper()
    if len(code) < 3:
        return jsonify({'ok': False, 'error': 'need ICAO'}), 400
    now = time.time()
    hit = _METAR_CACHE.get(code)
    if hit and hit[0] > now:
        return jsonify({'ok': True, 'icao': code, 'source': 'cache', **hit[1]})
    d = _http_json(f'https://aviationweather.gov/api/data/metar?ids={urllib.parse.quote(code)}&format=json', timeout=8)
    rows = d if isinstance(d, list) else []
    if not rows:
        return jsonify({'ok': False, 'icao': code}), 404
    m = rows[0]
    out = {
        'raw': m.get('rawOb'),
        'temp_c': m.get('temp'),
        'dewpoint_c': m.get('dewp'),
        'wind_dir': m.get('wdir'),
        'wind_kt': m.get('wspd'),
        'visibility': m.get('visib'),
        'flight_category': m.get('fltCat'),   # VFR/MVFR/IFR/LIFR
        'name': m.get('name'),
    }
    _METAR_CACHE[code] = (now + 600, out)
    return jsonify({'ok': True, 'icao': code, 'source': 'aviationweather', **out})


def _budget_remaining(month):
    """Wie viele AviationStack-Calls bleiben diesen Monat (Free-Tier-Schutz).
    Nutzt Supabase (persistent) UND einen In-Memory-Zähler als Fallback, damit
    das Limit auch dann greift, wenn die Budget-Tabelle noch nicht existiert."""
    cap = int(os.environ.get('AVIATIONSTACK_CAP', '90'))   # < 100 Free-Limit
    used = _MEM_BUDGET.get(month, 0)
    sb = _sb()
    if sb is not None:
        try:
            res = sb.table('ax_api_budget').select('n').eq('month', month).limit(1).execute()
            rows = getattr(res, 'data', None) or []
            if rows:
                used = max(used, int(rows[0].get('n') or 0))
        except Exception:
            pass
    return max(0, cap - used), used


def _budget_inc(month, used):
    _MEM_BUDGET[month] = used + 1   # In-Memory IMMER zählen (Safety-Net)
    sb = _sb()
    if sb is None:
        return
    # Bevorzugt ATOMAR (Audit 2026-07-05, s. _budget_rpc_add): der alte Upsert
    # mit `used+1` (used = vorher gelesener Stand) konnte parallele Calls/
    # Instanzen gegenseitig überschreiben → Free-Tier-Zähler lief real über.
    if _budget_rpc_add(month, 1) is not None:
        return
    try:
        sb.table('ax_api_budget').upsert(
            {'month': month, 'n': used + 1,
             'updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}).execute()
    except Exception:
        pass


# ax_schedule: Fehl-Antworten (AviationStack error/timeout) kurz negativ cachen —
# sonst rennt JEDE Suche desselben Paars erneut in den toten Call (Sweep
# 2026-07-10; live: TRD/BOJ → source:error bei jedem Request).
_SCHEDULE_NEG = {}                    # route → ts des letzten Fehlversuchs
_SCHEDULE_NEG_TTL = 45 * 60           # 30-60min-Fenster → Mitte
_SCHEDULE_NEG_MAX = 512


def _schedule_neg_hit(route):
    ts = _SCHEDULE_NEG.get(route)
    return bool(ts and (time.time() - ts) < _SCHEDULE_NEG_TTL)


def _schedule_neg_put(route):
    _SCHEDULE_NEG[route] = time.time()
    if len(_SCHEDULE_NEG) > _SCHEDULE_NEG_MAX:
        try:
            for k, _ in sorted(_SCHEDULE_NEG.items(),
                               key=lambda kv: kv[1])[:_SCHEDULE_NEG_MAX // 4]:
                _SCHEDULE_NEG.pop(k, None)
        except Exception:
            _SCHEDULE_NEG.clear()


def _schedule_fallback_flights(a, b):
    """FREE-Fallback für ax_schedule (Sweep 2026-07-10): scheitert/fehlt
    AviationStack, die EIGENEN Warehouse-Beobachtungen (route-history-Dual-
    Side-Merge) als Schedule-Liste im gewohnten Shape ausliefern statt leerer
    Seite — die Daten SIND da (FRA/JFK trägt dort z.B. 55 Flüge). Dedupe per
    Flugnummer, jüngster Tag gewinnt. 0 Spend, wirft nie."""
    view = _life_app('ax_route_history')
    if view is None:
        return []
    try:
        from flask import current_app
        _app = current_app._get_current_object()
    except Exception:
        return []
    h = _detail_subcall(_app, '/api/ax/route-history/%s/%s?days=7'
                        % (urllib.parse.quote(a), urllib.parse.quote(b)),
                        view, a, b)
    if not (h or {}).get('ok'):
        return []
    seen, out = set(), []
    for day in (h.get('recent_days') or []):        # Tag 0 = heute, dann älter
        for f in (day.get('flights') or []):
            no = (f.get('flight') or '').upper()
            if not no or no in seen:
                continue
            seen.add(no)
            # Reine arr-Rows tragen die ANKUNFTS-Zeit als 'sched' — nie als
            # Abflugzeit ausgeben (confirmed-or-hidden).
            obs = f.get('obs')
            dep_sched = f.get('sched') if obs in ('dep', 'both') else None
            arr_sched = (f.get('sched_arr')
                         or (f.get('sched') if obs == 'arr' else None))
            out.append({
                'flight': no,
                'airline': f.get('airline'),
                'airline_iata': no[:2],
                'dep_scheduled': dep_sched,
                'arr_scheduled': arr_sched,
                'dep_estimated': None, 'dep_actual': None,
                'dep_delay': f.get('dep_delay_min'),
                'arr_estimated': None, 'arr_actual': None,
                'arr_delay': f.get('arr_delay_min'),
                'status': 'cancelled' if f.get('cancelled') else None,
            })
    return out


@aerox_data_bp.route('/api/ax/schedule/<frm>/<to>', methods=['GET'])
def ax_schedule(frm, to):
    """Echte Flugnummern + geplante Zeiten auf einem Städtepaar (AviationStack).
    Architektur: Supabase-Cache FÜR IMMER (Schedules ändern sich kaum) → nur bei
    Cache-Miss UND solange das Monats-Budget reicht ein einziger externer Call,
    Ergebnis wird gecacht. So bleibt AeroX im Free-Tier (100/Monat) und ALLE
    Nutzer ziehen danach aus unserem Backend. Scheitert der externe Call oder
    ist er leer/ohne Budget → FREE-Fallback aus den eigenen route-history-
    Beobachtungen statt leerer Seite (Sweep 2026-07-10)."""
    a = (frm or '').strip().upper()
    b = (to or '').strip().upper()
    if len(a) < 3 or len(b) < 3:
        return jsonify({'ok': False, 'error': 'need IATA'}), 400
    # Per-IP-Limit (Sweep 2026-07-10, Klasse A: EINZIGER Paid-Verbraucher OHNE
    # Drossel — die 100er-Monatsquote war anonym in Minuten drainbar). 30/min
    # deckt jedes legitime Suchen; Cache-Hits sind davon praktisch unberührt.
    if _ax_rate_limited('ax_schedule', limit=30, window_sec=60):
        return jsonify({'ok': False, 'error': 'rate_limited'}), 429
    route = f'{a}-{b}'
    # Cache-Key mit Schema-Version: '#cs3' = Codeshares gefiltert + estimated/actual
    # Zeiten ergänzt. Schema-Bump umgeht alte Cache-Einträge (Duplikate / ohne
    # actual) → erster Abruf zieht frisch + sauber neu.
    cache_key = f'{route}#cs3'
    key = os.environ.get('AVIATIONSTACK_KEY', '')
    month = time.strftime('%Y-%m', time.gmtime())
    remaining, used = _budget_remaining(month)

    # max_age_days=None: ax_schedule_cache hat die EIGENE _fetched-Alterslogik
    # unten — das generische 90d-Gate würde alte Einträge sonst trotz leeren
    # Budgets wegwerfen (Cache ist hier besser als nichts).
    cached = _cache_get('ax_schedule_cache', 'route', cache_key, max_age_days=None)
    if cached is not None:
        # Schedules driften saisonal: nur wenn der Cache SEHR alt ist (>180 Tage)
        # UND noch reichlich Budget frei ist (>=30), einmal neu ziehen. Sonst
        # immer aus dem Cache (0 Budget) — die 90/Monat sind nur für NEUE Routen.
        stale_days = int(os.environ.get('AVIATIONSTACK_REFRESH_DAYS', '180'))
        fetched = cached.get('_fetched', 0)
        age_days = (time.time() - fetched) / 86400.0 if fetched else 0
        if not (key and remaining >= 30 and age_days > stale_days):
            if not cached.get('flights'):
                # Gecachter Leer-Treffer → Warehouse kennt die Strecke evtl.
                # trotzdem (Cache bleibt stehen, kein neuer Paid-Call).
                fb = _schedule_fallback_flights(a, b)
                if fb:
                    return jsonify({'ok': True, 'route': route,
                                    'source': 'route-history',
                                    'flights': fb, 'count': len(fb)})
            return jsonify({'ok': True, 'route': route, 'source': 'cache', **cached})
        # sonst: durchfallen und einmal auffrischen
    if not key or remaining <= 0 or _schedule_neg_hit(route):
        # Kein Budget/Key bzw. frischer Fehlversuch → NICHT leer: erst die
        # eigenen Beobachtungen probieren (free-first).
        fb = _schedule_fallback_flights(a, b)
        src = ('route-history' if fb else
               'error-cached' if _schedule_neg_hit(route) else 'budget-exhausted')
        return jsonify({'ok': True, 'route': route, 'source': src,
                        'flights': fb, 'count': len(fb),
                        'budget_remaining': remaining})

    # Free-Tier = HTTP (kein HTTPS). dep_iata + arr_iata Filter.
    url = (f'http://api.aviationstack.com/v1/flights?access_key={urllib.parse.quote(key)}'
           f'&dep_iata={a}&arr_iata={b}&limit=100')
    d = _http_json(url, timeout=12)
    rows = (d or {}).get('data') if isinstance(d, dict) else None
    if rows is None:
        _schedule_neg_put(route)   # 45min nicht erneut in den toten Call rennen
        fb = _schedule_fallback_flights(a, b)
        return jsonify({'ok': True, 'route': route,
                        'source': 'route-history' if fb else 'error',
                        'flights': fb, 'count': len(fb),
                        'budget_remaining': remaining})
    _budget_inc(month, used)   # Call gezählt (auch bei 0 Treffern — er wurde verbraucht)

    seen, flights = set(), []
    for r in rows:
        fl = (r.get('flight') or {})
        al = (r.get('airline') or {})
        dep = (r.get('departure') or {})
        arr = (r.get('arrival') or {})
        # Codeshares überspringen: derselbe PHYSISCHE Flug wird von vielen
        # Marketing-Airlines unter eigener Nummer verkauft (gleiche Zeiten) →
        # nur den operierenden Carrier behalten, sonst sieht die Liste aus wie
        # Fake-Duplikate (z.B. 6×„06:05 → 08:20" für FRA→LIS).
        if fl.get('codeshared'):
            continue
        no = (fl.get('iata') or '').upper()
        if not no or no in seen:
            continue
        seen.add(no)
        flights.append({
            'flight': no,
            'airline': al.get('name'),
            'airline_iata': al.get('iata'),
            'dep_scheduled': dep.get('scheduled'),
            'arr_scheduled': arr.get('scheduled'),
            # Tatsächliche/erwartete Zeiten + Verspätung (AviationStack liefert sie,
            # vorher weggeworfen → App zeigte nur „geplant"). actual = abgeflogen/
            # gelandet, estimated = erwartet; delay in Minuten.
            'dep_estimated': dep.get('estimated'),
            'dep_actual': dep.get('actual'),
            'dep_delay': dep.get('delay'),
            'arr_estimated': arr.get('estimated'),
            'arr_actual': arr.get('actual'),
            'arr_delay': arr.get('delay'),
            'status': r.get('flight_status'),
        })
    payload = {'flights': flights, 'count': len(flights), '_fetched': int(time.time())}
    _cache_put('ax_schedule_cache',
               {'route': cache_key, 'payload': payload,
                'updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
    if not flights:
        # AviationStack kennt das Paar nicht (Leer-Treffer wurde trotzdem
        # gecacht/gezählt) → eigene Beobachtungen statt leerer Seite.
        fb = _schedule_fallback_flights(a, b)
        if fb:
            return jsonify({'ok': True, 'route': route, 'source': 'route-history',
                            'flights': fb, 'count': len(fb),
                            'budget_remaining': remaining - 1})
    return jsonify({'ok': True, 'route': route, 'source': 'aviationstack',
                    'budget_remaining': remaining - 1, **payload})


@aerox_data_bp.route('/api/ax/suggest', methods=['GET'])
def ax_suggest():
    """Type-ahead: Präfix → bis zu ~10 Vorschläge über Flughäfen / Airlines /
    Muster. Komplett lokal (gebackene DB), NULL API."""
    from flask import request
    q = (request.args.get('q') or '').strip()
    if len(q) < 2:
        return jsonify({'ok': True, 'suggestions': []})
    qu = q.upper()
    like = q + '%'
    likeu = qu + '%'
    out = []
    # Flughäfen: Code-Präfix zuerst, dann Stadt/Name.
    for r in _q('''SELECT iata, icao, name, city, country FROM airports
                   WHERE iata=? OR icao=? OR city LIKE ? OR name LIKE ?
                   ORDER BY (iata=?) DESC, (city LIKE ?) DESC LIMIT 6''',
                (qu, qu, like, like, qu, likeu)):
        if not (r.get('iata') or r.get('icao')):
            continue
        out.append({'type': 'airport', 'code': r.get('iata') or r.get('icao'),
                    'label': r.get('city') or r.get('name'),
                    'sub': f"{r.get('iata') or r.get('icao')} · {r.get('country') or ''}".strip(' ·')})
    # Airlines: IATA/ICAO/Name.
    for r in _q('''SELECT iata, icao, name FROM airlines
                   WHERE iata=? OR icao=? OR name LIKE ? LIMIT 4''', (qu, qu, like)):
        if not r.get('name'):
            continue
        out.append({'type': 'airline', 'code': r.get('iata') or r.get('icao'),
                    'label': r.get('name'), 'sub': r.get('iata') or r.get('icao') or ''})
    # Muster: Typecode/Name.
    for r in _q('''SELECT typecode, name FROM aircraft_types
                   WHERE typecode=? OR name LIKE ? LIMIT 3''', (qu, like)):
        out.append({'type': 'aircraft_type', 'code': r.get('typecode'),
                    'label': r.get('name') or r.get('typecode'), 'sub': r.get('typecode')})
    return jsonify({'ok': True, 'suggestions': out})


# ─────────────────────────────────────────────────────────────────────────────
# Crowdsourced Crewbus-Transferzeiten (Flughafen → Crew-Hotel), pro IATA.
#
# User-Wunsch: Crew gibt die TATSÄCHLICHE Crewbus-Fahrzeit zur Destination ein;
# die App zeigt den DURCHSCHNITT aller Eingaben. Die erste Eingabe IST der
# Schnitt (n=1), jede weitere verfeinert ihn. Speist die Hotel-Ankunft-Schätzung
# im Feed mit echten Crowd-Daten statt der statischen Tabelle.
#
# Storage: DURABEL in Supabase `ax_crewbus_obs` (APPEND-ONLY, eine Zeile je
# Eingabe = Source of Truth). Der Schnitt wird aus ALLEN Zeilen einer Station
# gemittelt, sodass der Pool Cloud-Run-Restarts überlebt und über alle Instanzen
# aggregiert. Der In-Memory-Cache ist nur noch ein KURZLEBIGER Read-Through-
# Accelerator (kein Storage mehr). Ist Supabase mal weg, wird die Eingabe
# trotzdem angenommen und nur im Memory-Fallback gehalten — NIE ein 500.
_CREWBUS_MIN, _CREWBUS_MAX = 1, 240   # sane Range (Minuten)
_CREWBUS_CAP = 200         # je IATA die letzten 200 Eingaben mitteln (Drift-Schutz)
_CREWBUS_TTL = 60          # Read-Through-Cache: 60 s frisch, dann re-fetch aus SB
_CREWBUS_CACHE = {}        # iata -> (fetched_at, [minutes])  (nur Accelerator)
_CREWBUS_MEM = {}          # iata -> [minutes]  (Fallback, wenn SB nicht erreichbar)
_CREWBUS_LOCK = threading.Lock()


def _crewbus_anon_id():
    """Stabile, NICHT-umkehrbare Pseudo-ID aus dem Bearer-Token (Light-Dedup +
    Herkunfts-Signal, ohne Klartext-Identität zu speichern). None ohne Token."""
    from flask import request
    try:
        auth = request.headers.get('Authorization') or ''
        parts = auth.split()
        tok = parts[1] if len(parts) == 2 and parts[0].lower() == 'bearer' else ''
        if not tok:
            return None
        return hashlib.sha256(tok.encode('utf-8')).hexdigest()[:24]
    except Exception:
        return None


def _crewbus_sb_recent(iata):
    """Alle (bis _CREWBUS_CAP jüngste) gemeldeten Minuten einer Station aus dem
    durablen Store. None → SB nicht erreichbar/Tabelle fehlt (Caller fällt auf
    Memory zurück). []/Liste → autoritativer Pool (auch leer)."""
    sb = _sb()
    if sb is None:
        return None
    try:
        res = (sb.table('ax_crewbus_obs')
                 .select('minutes')
                 .eq('iata', iata)
                 .order('created_at', desc=True)
                 .limit(_CREWBUS_CAP)
                 .execute())
        rows = getattr(res, 'data', None) or []
        return [int(r['minutes']) for r in rows
                if isinstance(r.get('minutes'), (int, float))]
    except Exception:
        return None            # Tabelle nicht angelegt / SB down → graceful degrade


def _crewbus_recent(iata):
    """Read-Through: Memory-Cache (frisch < TTL) → Supabase → Memory-Fallback."""
    now = time.time()
    with _CREWBUS_LOCK:
        hit = _CREWBUS_CACHE.get(iata)
        if hit and (now - hit[0]) < _CREWBUS_TTL:
            return list(hit[1])
    mins = _crewbus_sb_recent(iata)
    if mins is None:
        # SB nicht verfügbar → best-effort Memory-Fallback (per-Instance).
        return list(_CREWBUS_MEM.get(iata) or [])
    with _CREWBUS_LOCK:
        _CREWBUS_CACHE[iata] = (now, list(mins))
    return mins


def _crewbus_is_dup(iata, minutes, anon_id):
    """Light-Dedup: derselbe Nutzer meldet für dieselbe Station denselben Wert
    innerhalb 24 h → als Doppel werten (kein neuer Insert, aber Stats zurück)."""
    if not anon_id:
        return False
    sb = _sb()
    if sb is None:
        return False
    try:
        import datetime
        since = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(hours=24)).isoformat()
        res = (sb.table('ax_crewbus_obs')
                 .select('minutes')
                 .eq('iata', iata).eq('anon_id', anon_id).eq('minutes', minutes)
                 .gte('created_at', since)
                 .limit(1).execute())
        return bool(getattr(res, 'data', None))
    except Exception:
        return False


def _crewbus_insert(iata, minutes, anon_id):
    """Durabler PRIMARY-Write: eine Zeile je Eingabe in ax_crewbus_obs.
    True bei Erfolg. False → SB weg/Tabelle fehlt (Caller nutzt Memory-Fallback)."""
    sb = _sb()
    if sb is None:
        return False
    try:
        sb.table('ax_crewbus_obs').insert({
            'iata': iata, 'minutes': int(minutes),
            'direction': 'transfer', 'anon_id': anon_id,
        }).execute()
        return True
    except Exception:
        return False


def _crewbus_avg(minutes):
    """Robuster Schnitt = MEDIAN (Audit 2026-07-05): beim arithmetischen Mittel
    konnten wenige Ausreißer-/Bot-Eingaben den Wert einer Station kippen; der
    Median braucht >50% manipulierte Eingaben. JSON-Key bleibt 'avg' (iOS liest
    ihn bereits, keine Client-Änderung nötig)."""
    if not minutes:
        return None
    s = sorted(minutes)
    n = len(s)
    mid = n // 2
    return int(round(s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0))


@aerox_data_bp.route('/api/ax/crewbus/<iata>', methods=['GET'])
def ax_crewbus_get(iata):
    iata = (iata or '').upper().strip()[:4]
    if not iata:
        return jsonify({'ok': False, 'error': 'bad_iata'}), 400
    mins = _crewbus_recent(iata)
    return jsonify({'ok': True, 'iata': iata,
                    'avg': _crewbus_avg(mins), 'count': len(mins)})


@aerox_data_bp.route('/api/ax/crewbus/<iata>', methods=['POST'])
def ax_crewbus_post(iata):
    from flask import request
    iata = (iata or '').upper().strip()[:4]
    if not iata:
        return jsonify({'ok': False, 'error': 'bad_iata'}), 400
    # HÄRTUNG (Audit 2026-07-05): der Crowd-Write war komplett offen — ohne
    # Bearer griff auch der Dup-Check nicht (anon=None), und ein anonymer Bot
    # konnte mit ein paar hundert Posts den Wert einer Station kippen.
    # (a) Bearer-Pflicht: die App sendet ihn auf JEDEM Request (APIClient) →
    #     kein legitimer Client verliert etwas; anon_id ist damit immer gesetzt.
    # (b) per-IP-Limit (Muster wie ax_callsign): Crew meldet EINEN Wert pro
    #     Ankunft — 10/min ist großzügig, stoppt aber Flutungs-Skripte.
    anon = _crewbus_anon_id()
    if not anon:
        return jsonify({'ok': False, 'error': 'auth_required'}), 401
    if _ax_rate_limited('ax_crewbus_post', limit=10, window_sec=60):
        return jsonify({'ok': False, 'error': 'rate_limited'}), 429
    body = request.get_json(silent=True) or {}
    try:
        m = int(round(float(body.get('minutes'))))
    except Exception:
        return jsonify({'ok': False, 'error': 'bad_minutes'}), 400
    if not (_CREWBUS_MIN <= m <= _CREWBUS_MAX):
        return jsonify({'ok': False, 'error': 'out_of_range',
                        'message': 'Minuten müssen zwischen 1 und 240 liegen.'}), 400

    # Light-Dedup: identische Wiederholung desselben Nutzers zählt nicht doppelt.
    if not _crewbus_is_dup(iata, m, anon):
        if not _crewbus_insert(iata, m, anon):
            # SB nicht erreichbar → Eingabe NICHT verlieren: Memory-Fallback.
            with _CREWBUS_LOCK:
                lst = _CREWBUS_MEM.setdefault(iata, [])
                lst.append(m)
                del lst[:-_CREWBUS_CAP]
    # Read-Through-Cache invalidieren, damit der neue Wert sofort im Schnitt ist.
    with _CREWBUS_LOCK:
        _CREWBUS_CACHE.pop(iata, None)

    mins = _crewbus_recent(iata)
    if not mins:                       # SB-Insert lief, Read noch nicht sichtbar
        mins = [m]
    return jsonify({'ok': True, 'iata': iata,
                    'avg': _crewbus_avg(mins), 'count': len(mins), 'your_minutes': m})


# ═══════════════════════════════════════════════════════════════════════════
# FLUG-LEBENSZYKLUS  (Owner-Direktive 2026-07-03: „voller Flug-Lebenszyklus,
# sehr smart mit vorhandenen Daten, NUR gratis")
# ---------------------------------------------------------------------------
# Baut AUSSCHLIESSLICH auf bestehenden gratis Bausteinen auf, NICHTS dupliziert:
#   app.py     _flight_obs_merged (Dual-Side reg/type/gate/dep+arr-delay/known),
#              _airport_local_now, _parse_local_iso, _DELAY_THRESHOLD_MIN,
#              _icao_to_iata_best
#   adsb bp    resolve_reg_to_hex / fetch_live_state / fetch_recent_flight
#              (OpenSky→adsb.lol, permanent gratis, kein bezahlter Provider)
#   dieses bp  _resolve_live_route (free-first-Kaskade), _iata_city_name,
#              _iata_latlon, _gc_km, _callsign_to_iata_flightno, _airport_row,
#              aircraft_specs.specs_for_type
# Ehrlichkeits-Regel durchgezogen: ein Feld ist `null`, wenn es (noch) nicht
# bestimmbar ist — NIE „pünktlich"/erfundene Zeiten. Alles gecacht: die
# darunterliegenden Board/Track/Route-Caches PLUS ein kurzer Prozess-Memo pro
# Endpoint-Key (iOS pollt ~30–60 s). free_only=True auf JEDEM Merge → strukturell
# spend-frei (kein AeroDataBox auf diesen Pfaden).
# ═══════════════════════════════════════════════════════════════════════════

_LIFECYCLE_MEMO = {}        # key-tuple → (ts, payload dict)
_LIFECYCLE_TTL = 45         # s — deckt einen 30–60 s-Poll-Zyklus ab
_LIFECYCLE_MEMO_MAX = 400


def _life_app(name, default=None):
    """app.py-Attribut zur Request-Zeit auflösen (app ist beim Import evtl. noch
    nicht fertig geladen — Muster wie family_watch._app_attr)."""
    try:
        import app as _app_mod
        return getattr(_app_mod, name, default)
    except Exception:
        return default


def _memo_get(key):
    hit = _LIFECYCLE_MEMO.get(key)
    if hit and (time.time() - hit[0]) < _LIFECYCLE_TTL:
        return dict(hit[1])
    return None


def _memo_put(key, payload):
    _LIFECYCLE_MEMO[key] = (time.time(), dict(payload))
    if len(_LIFECYCLE_MEMO) > _LIFECYCLE_MEMO_MAX:
        try:
            items = sorted(_LIFECYCLE_MEMO.items(), key=lambda kv: kv[1][0])
            for k, _v in items[:len(items) // 4 or 1]:
                _LIFECYCLE_MEMO.pop(k, None)
        except Exception:
            _LIFECYCLE_MEMO.clear()
    return payload


def _parse_local_iso(s):
    """Naiver Lokalzeit-Parser (Board-`sched`/`esti`-Strings) — delegiert an
    app._parse_local_iso; robuster Eigen-Fallback, falls app noch nicht geladen."""
    f = _life_app('_parse_local_iso')
    if f is not None and f is not _parse_local_iso:
        return f(s)
    if not s:
        return None
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(str(s))
        return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt
    except Exception:
        try:
            return datetime.strptime(str(s)[:19], '%Y-%m-%dT%H:%M:%S')
        except Exception:
            return None


def _norm_iata(x):
    """→ gültiger IATA(3)-Code (ICAO(4) wird via DB/DE-Map aufgelöst) oder None."""
    c = (x or '').strip().upper()
    if len(c) == 3 and c.isalpha():
        return c
    if len(c) == 4 and c.isalpha():
        return _icao_to_iata(c)
    return None


def _icao_to_iata(code):
    """ICAO(4) → IATA(3) über die gebackene Airports-DB (weltweit), Fallback auf
    die DE-Map aus app.py. Gibt bei 3-stelligem Input diesen zurück; None-safe."""
    c = (code or '').strip().upper()
    if len(c) == 3 and c.isalpha():
        return c
    if len(c) != 4:
        return None
    try:
        row = _airport_row(c)
        if row and (row.get('iata') or '').strip():
            return row['iata'].strip().upper()
    except Exception:
        pass
    f = _life_app('_icao_to_iata_best')
    r = (f(c) if f else None) or c
    return r if (len(r) == 3 and r.isalpha()) else None


def _airport_brief(iata):
    """{'iata','city'} für einen Code — None wenn kein gültiger IATA-Code."""
    ia = _norm_iata(iata)
    if not ia:
        return None
    return {'iata': ia, 'city': _iata_city_name(ia)}


def _turnaround_min_for_type(aircraft_type):
    """Konservative Mindest-Bodenzeit (Min.) nach Rumpf — Owner-Vorgabe:
    Narrowbody 35, Widebody 60, unbekannt 45. `aircraft_type` darf ICAO-Typecode
    (A320/B77W) ODER Freitext ('Airbus A320-200') sein."""
    t = (aircraft_type or '').strip().upper()
    body = None
    if t:
        try:
            from blueprints.aircraft_specs import specs_for_type
        except Exception:
            specs_for_type = None
        if specs_for_type is not None:
            sp = specs_for_type(t) or specs_for_type(re.split(r'[\s/\-]+', t)[0])
            if sp:
                body = sp.get('body')
        if body is None:
            wide = ('A330', 'A340', 'A350', 'A380', 'B747', 'B767', 'B777',
                    'B787', '747', '767', '777', '787', 'A33', 'A34', 'A35', 'A38')
            narrow = ('A318', 'A319', 'A320', 'A321', 'A220', 'B737', 'B738',
                      'B739', 'B73', '737', 'CRJ', 'E170', 'E175', 'E190', 'E195',
                      'EMB', 'ATR', 'DH8')
            if any(w in t for w in wide):
                body = 'wide'
            elif any(w in t for w in narrow):
                body = 'narrow'
    if body == 'wide':
        return 60
    if body == 'narrow':
        return 35
    return 45


def _reg_candidates(reg):
    """Kandidaten-Schreibweisen einer Registration — Board-Quellen liefern sie mal
    MIT ('D-AIFF'), mal OHNE Bindestrich ('DAIFF'); die gebackene aircraft-Tabelle
    hält die ICAO-Form MIT Strich. Wir probieren raw, strichlos und Strich nach
    Pos 1/2 (deckt D-/G-/F- sowie OE-/HB-/OK-/US-N-Regs ab)."""
    r = (reg or '').strip().upper()
    if not r:
        return []
    bare = re.sub(r'[^A-Z0-9]', '', r)
    out = [r, bare]
    if '-' not in r and len(bare) >= 3:
        out.append(bare[:1] + '-' + bare[1:])
        out.append(bare[:2] + '-' + bare[2:])
    seen, uniq = set(), []
    for c in out:
        if c and c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def _reg_hex_typecode_free(reg):
    """Reg → (hex, typecode) NUR aus gratis/eigenen Quellen: erst der ADS-B-Reg→Hex-
    Resolver (In-Proc-Cache → Supabase tail_hex → hartkodierte Map), dann als
    Fallback die gebackene 520k-aircraft-Referenz-DB (offline, kostenlos), die
    zugleich den ICAO-Typecode liefert. (None, None) wenn nirgends bekannt."""
    r2h, _fls, _frf, _tw = _adsb_helpers()
    hexid = None
    if r2h is not None:
        try:
            hexid = r2h(reg)
        except Exception:
            hexid = None
    typecode = None
    cands = _reg_candidates(reg)
    if cands and (hexid is None or typecode is None):
        try:
            ph = ','.join('?' * len(cands))
            row = _q1(f'SELECT hex, typecode FROM aircraft WHERE reg IN ({ph}) LIMIT 1',
                      tuple(cands))
            if row:
                hexid = hexid or ((row.get('hex') or '').strip().lower() or None)
                typecode = (row.get('typecode') or '').strip().upper() or None
        except Exception:
            pass
    return hexid, typecode


def _adsb_helpers():
    """(resolve_reg_to_hex, fetch_live_state, fetch_recent_flight, _touch_watch)
    aus dem ADS-B-Blueprint — alle gratis (OpenSky/adsb.lol). (None,…) wenn das
    Blueprint nicht geladen ist (ehrlich degradieren, nie werfen)."""
    try:
        from blueprints.adsb_blueprint import (
            resolve_reg_to_hex, fetch_live_state, fetch_recent_flight, _touch_watch)
        return resolve_reg_to_hex, fetch_live_state, fetch_recent_flight, _touch_watch
    except Exception:
        return None, None, None, None


def _live_pos_from_state(row):
    """OpenSky-State-Row → {lat,lon,alt,gs,track,on_ground} (Einheiten: ft/kt/°)
    oder None. Layout siehe fetch_live_state/_fetch_adsb_lol."""
    if not (row and isinstance(row, (list, tuple)) and len(row) > 6):
        return None
    lat, lon = row[6], row[5]
    if lat is None or lon is None:
        return None
    on_ground = bool(row[8]) if (len(row) > 8 and row[8] is not None) else False

    def _num(i, conv):
        try:
            v = row[i] if len(row) > i else None
            return conv(v) if v is not None else None
        except Exception:
            return None
    return {
        'lat': lat, 'lon': lon,
        'alt': _num(7, lambda v: round(float(v) / 0.3048)),          # m → ft
        'gs': _num(9, lambda v: round(float(v) / 0.514444, 1)),      # m/s → kt
        'track': _num(10, lambda v: round(float(v), 1)),
        'on_ground': on_ground,
    }


def _machine_live(reg, want_route=True, targeted=False):
    """Für EINE Registration → (hex, callsign, pos_dict, route_dict). REIN GRATIS:
    die Position kommt aus dem EINEN Resolver (position_for_flight), Default NUR
    aus den vom Harvester/Poller gefüllten Tabellen (targeted=False) — KEIN
    bezahlter AeroDataBox-Ping (allow_paid=False IMMER) und kein ungedeckelter
    Extern-Call im Bulk-Pfad (Kern-Regel 5000 User). `targeted=True` ist für die
    EIGENE Maschine (Inbound-Kette / flight-live, per-User-gepollter, memoized
    Endpoint) und schaltet den GRATIS-Extern-Tier 3 (adsb.lol-Mirrors) frei —
    exakt die im Resolver-Docstring benannte Inbound-/Watch-Klasse; sonst hängt
    „wo ist mein Flieger" daran, ob der Poller den Tail zufällig schon trackt
    (Owner-Fall D-ABYN: airborne laut adsb.lol, Tabellen leer). Alle Rückgaben
    None-safe; wirft nie."""
    _r2h, _fls, frf, touch = _adsb_helpers()  # _fls/frf: im gratis-Pfad NICHT genutzt
    reg = (reg or '').strip().upper() or None
    if not reg:
        return None, None, None, None
    hexid, _typecode = _reg_hex_typecode_free(reg)
    if not hexid:
        return None, None, None, None
    if touch is not None:
        try:
            touch(hexid, registration=reg, priority=1)
        except Exception:
            pass
    row = None
    _src = _obs_ts = None
    try:
        from blueprints.warehouse_reader import position_for_flight
        # targeted=False → NUR Tabellen (Tier 1+2); targeted=True (eigene
        # Maschine) zusätzlich GRATIS-Tier 3 (adsb.lol). Bezahlt NIE.
        row, _src, _obs_ts, _tried = position_for_flight(
            hex=hexid, reg=reg, targeted=targeted, allow_paid=False)
    except Exception:
        row = None
        _src = _obs_ts = None
    pos = _live_pos_from_state(row)
    if pos is not None:
        # ECHTEN Beobachtungs-Zeitstempel + Provenienz durchreichen (P1-4b):
        # position_for_flight liefert beide, sie gingen hier bisher verloren —
        # die FlightState-Collectors stempelten dann fälschlich „jetzt".
        # Additiv (Konsumenten lesen nur lat/lon/alt/gs/track/on_ground).
        pos['seen_ts'] = _obs_ts
        pos['source'] = _src
    cs = None
    if row and len(row) > 1 and row[1]:
        cs = str(row[1]).strip().upper() or None
    # Callsign-Fallback via OpenSky (fetch_recent_flight) ist ein UNGEDECKELTER
    # Extern-Call — im User-Request AUS (Kosten/Bot-Risiko). Nur mit explizitem
    # Env-Guard (Notbetrieb/Debug), NIE im 5000-User-Default.
    if (not cs and frf is not None
            and os.environ.get('AEROX_MACHINE_LIVE_OPENSKY', '0') == '1'):
        try:
            fl = frf(hexid)
            if fl and fl.get('callsign'):
                cs = str(fl['callsign']).strip().upper() or None
        except Exception:
            pass
    route = None
    if want_route and cs:
        try:
            # allow_paid=False explizit: „Wo ist mein/nächster Flieger" darf KEINE
            # API-Kosten pro Aufruf erzeugen (Kern-Regel). Rein aus unseren Tabellen;
            # der bezahlte Notnagel bleibt dem interaktiven ?own=1-Tap vorbehalten.
            route = _resolve_live_route(
                cs, hexid=hexid, reg=reg,
                lat=(pos or {}).get('lat'), lon=(pos or {}).get('lon'),
                track=(pos or {}).get('track'), gs=(pos or {}).get('gs'),
                on_ground=bool((pos or {}).get('on_ground')),
                allow_paid=False)
        except Exception:
            route = None
    return hexid, cs, pos, route


def _inbound_arr_row_by_reg(dep_iata, reg):
    """Ankunfts-Board-Zeile an dep_iata, deren Reg == reg (bindestrich-tolerant)
    und die noch nicht gelandet ist → der physische Zubringer mitsamt echter
    IATA-Flugnummer/Herkunft/Soll+Ist-Ankunft. GRATIS: nur der bereits gefüllte
    In-Memory-Board-Cache (kein Fetch, kein Spend — der Poller hält die Basis-
    Boards warm). None-safe."""
    cached = _life_app('_cached_board_rows')
    if cached is None or not dep_iata or not reg:
        return None
    try:
        rows = cached(dep_iata, 'arrival') or []
    except Exception:
        rows = []
    target = re.sub(r'[^A-Z0-9]', '', (reg or '').upper())
    if not target:
        return None
    landed = ('gelandet', 'landed', 'arrived', 'gepäck', 'baggage', 'on blocks',
              'at gate')
    for r in rows:
        rr = re.sub(r'[^A-Z0-9]', '', (r.get('reg') or '').upper())
        if rr and rr == target:
            st = (r.get('status') or '').lower()
            if any(m in st for m in landed):
                return None      # schon da → kein „kommender" Zubringer mehr
            return r
    return None


def _route_endpoints(route):
    """route-Dict → (src_iata, dst_iata) best-effort (IATA bevorzugt, sonst ICAO
    aufgelöst). (None,None) bei fehlender Route."""
    if not route:
        return None, None
    src = _norm_iata(route.get('src')) or _icao_to_iata(route.get('src_icao'))
    dst = _norm_iata(route.get('dst')) or _icao_to_iata(route.get('dst_icao'))
    return src, dst


def _progress_along_route(dep_iata, dst_iata, pos):
    """Großkreis-Fortschritt 0..1 der Live-Position zwischen dep und dst. None
    wenn Koordinaten fehlen. Geklemmt (Anflug-Overshoot/Rauschen → 0..1)."""
    if not pos or pos.get('lat') is None or pos.get('lon') is None:
        return None
    o = _iata_latlon((dep_iata or '').upper())
    d = _iata_latlon((dst_iata or '').upper())
    if not o or not d:
        return None
    total = _gc_km(o[0], o[1], d[0], d[1])
    if total < 1.0:
        return None
    done = _gc_km(o[0], o[1], pos['lat'], pos['lon'])
    return round(max(0.0, min(1.0, done / total)), 3)


def _sb_day_reg(flight_no, date):
    """Tail/Typ/Route eines Fluges für EXAKT einen Flugtag DIREKT aus den
    SB-Tages-Rows (`airport_delay_obs`, flight+date, airport-agnostisch) — der
    gleiche Lookup, den /api/ax/flight-info macht. Der Dual-Side-Resolver
    (`_flight_obs_merged`) braucht dep/arr-IATA als Store-Keys und findet ohne
    sie NICHTS; hier reicht die Flugnummer. NUR echte Beobachtungen des
    angefragten Tages — nie ein Tail eines anderen Datums (stale Reg wäre eine
    fremde Maschine). → (reg, type_code, dep_iata, arr_iata) — alles None-safe.

    ARR-Rows ('<AP>#ARR') sind gespiegelt: airport=ZIEL, dest_iata=HERKUNFT —
    wird hier entspiegelt. Dep-Row-Reg gewinnt (dort tail-gefüllt am
    verlässlichsten), sonst die erste Row mit Reg."""
    fn = (flight_no or '').replace(' ', '').upper().strip()
    d = (date or '').strip()[:10]
    sb = _life_app('sb')
    if sb is None or len(fn) < 3 or not d:
        return None, None, None, None
    try:
        r = (sb.table('airport_delay_obs').select('*')
             .eq('flight', fn).eq('date', d)
             .order('updated_at', desc=True).limit(20).execute())
        rows = r.data or []
    except Exception:
        rows = []
    reg = tc = dep = arr = None
    reg_from_dep = False
    for row in rows:
        ap = (row.get('airport') or '').upper().strip()
        is_arr = '#' in ap
        ap_clean = ap.split('#', 1)[0] or None
        other = ((row.get('dest_iata') or '').upper().strip() or None)
        r_dep = other if is_arr else ap_clean
        r_arr = ap_clean if is_arr else other
        dep = dep or r_dep
        arr = arr or r_arr
        rg = ((row.get('reg') or '').strip().upper() or None)
        if rg and (reg is None or (not reg_from_dep and not is_arr)):
            reg = rg
            reg_from_dep = not is_arr
            tc = ((row.get('type_code') or '').strip().upper() or tc)
    return reg, tc, dep, arr


def _rotation_positioning_row(flight_no, date, dep_iata, arr_iata, my_dep_utc=None):
    """Außenstations-Rotation (Owner 2026-07-07, Layover-Fall „LH717 ab HND"):
    der eigene Abflughafen wird nicht gepollt → die Reg-Kaskade bleibt leer und
    die Kette starb, obwohl die Maschine längst bekannt ist. Bei Out-and-back-
    Rotationen positioniert die GEGENROUTE den Flieger ein: gleiche Airline,
    arr_iata→dep_iata (LH716 FRA→HND bringt die Maschine für LH717 HND→FRA) —
    und die Homebase-Seite (arr_iata, DE/EU) IST gepollt. Kandidaten: Abflug-
    Rows an arr_iata (kein '#ARR') mit dest_iata==dep_iata, Carrier-Prefix
    identisch, Flugtag D-1/D, Reg gesetzt, nicht annulliert. Auswahl: die
    JÜNGSTE Row, deren Abflug (UTC) noch VOR dem eigenen Abflug liegt — sonst
    wäre es die Rotation NACH meinem Flug (heutige LH716 kann nicht die
    Maschine der heutigen LH717 sein). Ohne my_dep_utc: jüngste Row (Long-haul-
    Layover-Fall, dort ist sie immer richtig). Ehrlich: None statt geraten."""
    fn = (flight_no or '').replace(' ', '').upper().strip()
    dep = _norm_iata(dep_iata)
    arr = _norm_iata(arr_iata)
    d = (date or '').strip()[:10]
    sb = _life_app('sb')
    if sb is None or not dep or not arr or len(fn) < 3 or not d:
        return None
    carrier = fn[:2]
    from datetime import datetime as _dt, timedelta as _td
    try:
        d0 = _dt.strptime(d, '%Y-%m-%d')
        days = [(d0 - _td(days=1)).strftime('%Y-%m-%d'), d]
    except Exception:
        days = [d]
    try:
        r = (sb.table('airport_delay_obs').select('*')
             .eq('airport', arr).eq('dest_iata', dep)
             .in_('date', days)
             .order('date', desc=True).order('sched', desc=True)
             .limit(40).execute())
        rows = r.data or []
    except Exception:
        rows = []
    for row in rows:
        rfn = (row.get('flight') or '').replace(' ', '').upper()
        if not rfn.startswith(carrier) or rfn == fn:
            continue
        if not (row.get('reg') or '').strip():
            continue
        if row.get('cancelled') is True:
            continue
        if my_dep_utc is not None:
            # Abflug der Rotation (station-lokal an arr_iata) → UTC; muss VOR
            # meinem eigenen Abflug liegen. Unparsbar ⇒ Kandidat überspringen
            # (lieber nichts behaupten als die falsche Maschine).
            rot_dep = _local_to_utc(
                f"{row.get('date')}T{(row.get('sched') or '')}:00", arr)
            if rot_dep is None or rot_dep >= my_dep_utc:
                continue
        return row
    return None


def _build_inbound_chain(flight_no, date, dep_iata, reg_hint=None,
                         arr_iata=None, my_dep_utc=None):
    """KERN-Trick (#1): (a) welche Maschine ist meinem Abflug zugeteilt (Reg aus
    Warehouse/Live-Board des Abflugs, gratis); (b) wo ist dieselbe Reg GERADE —
    ist ihre aktuelle Live-Route → dep_iata, ist das der Zubringer; (c) dessen
    Zeiten/Delay aus der Ankunfts-Seite an dep_iata. PLUS Abflug-Delay-Prognose
    (#2). Gibt (chain, forecast, my_merged). Ehrlich: null statt erfunden."""
    from datetime import timedelta
    merged_fn = _life_app('_flight_obs_merged')
    dep = _norm_iata(dep_iata)
    chain = {
        'inbound_flight_no': None, 'inbound_origin': None,
        'inbound_sched_arr': None, 'inbound_est_arr': None,
        'inbound_delay_min': None, 'inbound_live': None,
        'reg': None, 'aircraft_type': None,
    }
    forecast = {
        'forecast_dep_delay_min': None, 'confidence': 'keine',
        'reason': 'Zubringer-Maschine noch nicht bestimmbar.',
        'sched_dep': None, 'min_turnaround_min': None,
    }
    my = (merged_fn(flight_no, date=date, dep_iata=dep, free_only=True)
          if merged_fn else None)
    # Reg-Kaskade — NIE geraten, nur echte Quellen, in dieser Reihenfolge:
    #   1) Dual-Side-Merge (Live-Board + Store des heutigen Tages),
    #   2) SB-Tages-Rows des EXAKTEN Datums (wie /flight-info — der Merge braucht
    #      Store-Keys/Airports und sieht airport-agnostische SB-Rows nicht),
    #   3) vom Client mitgegebener ECHTER Roster-Tail (Owner 2026-07-05: „die
    #      maschine ist live warum wird sie nicht angezeigt" — ohne Reg-Match
    #      blieb die Karte leer, obwohl der Flieger via ADS-B auffindbar ist).
    reg = (my or {}).get('reg') or None
    ac_type = (my or {}).get('aircraft') or None
    if not reg:
        _sb_reg, _sb_tc, _sb_dep, _sb_arr = _sb_day_reg(flight_no, date)
        reg = _sb_reg
        ac_type = ac_type or _sb_tc
    if not reg:
        reg = (str(reg_hint or '').strip().upper() or None)
    #   4) Außenstations-Rotation (Owner 2026-07-07): Abflughafen ungepollt +
    #      Roster ohne Tail → die Gegenroute der Homebase kennt die Maschine
    #      (heutige LH716 FRA→HND = mein LH717-Flieger morgen). Nur mit
    #      arr_iata (neuer Client schickt es mit) — sonst wie bisher.
    # Rotations-Row IMMER holen, wenn arr_iata bekannt ist (nicht nur bei fehlender
    # Reg): sie IDENTIFIZIERT den Zubringer (die Gegenroute LH716 FRA→HND) für
    # inbound_origin/inbound_fn — auch wenn schon eine Reg für MEINEN Flug (LH717)
    # aufgelöst wurde. Vorher (`if not reg`) blieb bei vorhandener Reg der Zubringer
    # unbestimmt → inbound_origin null → Positions-Tier übersprungen → „LH716 wird
    # nicht gefunden" (Owner 2026-07-08). Reg/Typ NUR übernehmen, wenn noch keine da.
    rot_row = None
    if arr_iata:
        rot_row = _rotation_positioning_row(flight_no, date, dep, arr_iata,
                                            my_dep_utc=my_dep_utc)
        if rot_row is not None and not reg:
            reg = ((rot_row.get('reg') or '').strip().upper() or None)
            if not ac_type:
                ac_type = ((rot_row.get('type_code') or '').strip().upper()
                           or None)
    sched_dep = (my or {}).get('sched_dep')
    if reg and not ac_type:
        # Typecode gratis aus der gebackenen aircraft-DB (für den Turnaround-Puffer).
        _hx, tc = _reg_hex_typecode_free(reg)
        ac_type = tc or ac_type
    chain['reg'] = reg
    chain['aircraft_type'] = ac_type
    forecast['sched_dep'] = sched_dep
    if not reg or not dep:
        return chain, forecast, my

    # (b) Der physische Zubringer = die Ankunfts-Board-Zeile an dep_iata mit
    # DERSELBEN Reg (das Board hat die Maschine dem Inbound bereits zugeteilt) —
    # autoritativer als die ADS-B-Callsign-Ableitung und liefert die echte
    # IATA-Flugnummer + Herkunft + Soll/Ist-Ankunft. Gratis (cache-only, der
    # Poller hält die Basis-Boards warm). Live-Position/-Route dienen als
    # Bestätigung + In-der-Luft-Marker.
    # targeted=True: die EIGENE Maschine (Inbound-Klasse laut Resolver) —
    # gratis adsb.lol-Tier erlaubt, memoized + per-User-gepollt (kein Bulk).
    _hex, cs, pos, route = _machine_live(reg, targeted=True)
    arr_row = _inbound_arr_row_by_reg(dep, reg)
    inbound_fn = inbound_origin = None
    row_sched = row_esti = row_delay = None
    if arr_row:
        inbound_fn = (arr_row.get('flight') or '').replace(' ', '').upper() or None
        inbound_origin = _norm_iata(arr_row.get('dest_iata'))  # arr-Board: dest=Herkunft
        row_sched = arr_row.get('sched') or None
        row_esti = arr_row.get('esti') or None
        if not ac_type and (arr_row.get('aircraft') or '').strip():
            ac_type = arr_row['aircraft'].strip()
            chain['aircraft_type'] = ac_type
    if not inbound_origin:
        # Fallback: ADS-B-Live-Route dieser Reg → Ziel == mein Abflughafen?
        src, dst = _route_endpoints(route)
        if dst and dst == dep:
            inbound_origin = src
            if cs:
                inbound_fn = _callsign_to_iata_flightno(cs) or cs
    if not inbound_origin and rot_row is not None:
        # Rotations-Fallback (Owner 2026-07-07): weder Ankunfts-Board (Außen-
        # station ungepollt) noch Live-Route (Maschine steht noch an der
        # Homebase) kennen den Zubringer — aber die Gegenroute-Row IST er:
        # sie startet an arr_iata (Homebase) und landet an meinem Abflughafen.
        # Herkunft = Homebase; Zeiten NICHT aus der Row (sched/esti dort sind
        # ABFLUG-Zeiten, keine Ankunft an dep — nichts erfinden, der
        # Dual-Side-Resolver unten liefert die Ankunft, wenn er sie kennt).
        inbound_fn = (rot_row.get('flight') or '').replace(' ', '').upper() or None
        inbound_origin = _norm_iata(arr_iata)
        # Die Gegenroute-Row IST der Zubringer — ihr Tail ist der einlaufende
        # Flieger (die zuvor aufgelöste Reg gehört zu MEINEM Flug, kann abweichen).
        # Auf die Zubringer-Reg/-Typ ziehen, damit Snapshot/Korridor (die per Reg
        # filtern) den RICHTIGEN Flieger finden, nicht meinen.
        _rot_reg = (rot_row.get('reg') or '').strip().upper() or None
        if _rot_reg:
            reg = _rot_reg
            chain['reg'] = _rot_reg
            _rot_tc = (rot_row.get('type_code') or '').strip().upper() or None
            if _rot_tc:
                ac_type = _rot_tc
                chain['aircraft_type'] = _rot_tc
    if not inbound_origin:
        # Zubringer (noch) nicht eindeutig bestimmbar → ehrlich null lassen.
        return chain, forecast, my

    chain['inbound_flight_no'] = inbound_fn
    chain['inbound_origin'] = _airport_brief(inbound_origin)
    # Live-Position NUR als Zubringer übernehmen, wenn die LIVE-Route DIESER
    # Maschine auch WIRKLICH an meinem Abflughafen landet (dst == dep). Sonst
    # fliegt der Tail gerade einen ANDEREN Leg — reg-only-ADS-B findet ihn
    # irgendwo (Owner-Screenshot 2026-07-08 „stimmt nicht": D-ABYM real über
    # Miami, während die Karte FRA→HND behauptet). Off-route ⇒ Position verwerfen;
    # der Korridor-Resolver unten sucht den Flieger auf der ECHTEN Route (FRA→HND)
    # und lässt inbound_live ehrlich null, wenn der Tail dort gar nicht fliegt.
    _live_src, _live_dst = _route_endpoints(route)
    pos_on_route = (bool(pos) and not pos.get('on_ground')
                    and bool(_live_dst) and _live_dst == dep)
    if pos_on_route:
        chain['inbound_live'] = pos      # nur wenn wirklich in der Luft & on-route

    # (c) Ankunfts-Seite des Zubringers AN meinem Abflughafen: bevorzugt der
    # zentrale Dual-Side-Resolver (ehrliche delay_known-Semantik), sonst direkt
    # aus der Board-Zeile (Soll/Ist), Delay aus esti−sched (esti gesetzt = bekannt).
    merged_in = (merged_fn(inbound_fn, dep_iata=inbound_origin, arr_iata=dep,
                           free_only=True)
                 if (merged_fn and inbound_fn) else None)
    if merged_in:
        chain['inbound_sched_arr'] = merged_in.get('sched_arr') or row_sched
        chain['inbound_est_arr'] = merged_in.get('esti_arr') or row_esti
        if merged_in.get('delay_known'):
            chain['inbound_delay_min'] = merged_in.get('arr_delay_min')
        if not ac_type and merged_in.get('aircraft'):
            ac_type = merged_in.get('aircraft')
            chain['aircraft_type'] = ac_type
    else:
        chain['inbound_sched_arr'] = row_sched
        chain['inbound_est_arr'] = row_esti
    # FR24-gRPC-Nachschlag (Owner 2026-07-07 „haben doch ein fr24 scraping"):
    # an einer ungepollten Außenstation (Layover HND) gibt es KEIN Ankunfts-
    # Board → sched/est-Ankunft des Zubringers blieben null („Ankunftszeit noch
    # offen"). Sobald der Zubringer AIRBORNE ist, kennt FR24s flight_details
    # sched_arr/eta (Epoch-Sekunden UTC) — echte Werte, kein Raten. EIN gRPC-
    # Call (detail_card ist available()/-Rate-Limit-gegated, Timeout 8 s),
    # still bei Fehlschlag. Übernahme NUR bei Reg-Match (die Karte muss DIESEN
    # Flieger beschreiben, nicht irgendeinen Callsign-Nachbarn im Suchfenster).
    # Stale Position ⇒ FR24-Box enthält den Flieger nicht ⇒ kein Match ⇒ null
    # bleibt null (ehrlich degradiert).
    if (chain['inbound_est_arr'] is None and pos_on_route
            and pos.get('lat') is not None and pos.get('lon') is not None):
        try:
            from blueprints import fr24_grpc
            card = fr24_grpc.detail_card(callsign=cs, reg=reg,
                                         lat=pos.get('lat'), lon=pos.get('lon'))
        except Exception:
            card = None
        _creg = re.sub(r'[^A-Z0-9]', '', ((card or {}).get('reg') or '').upper())
        _treg = re.sub(r'[^A-Z0-9]', '', (reg or '').upper())
        if card and _creg and _creg == _treg:
            def _epoch_iso(v):
                try:
                    v = int(v)
                    if v <= 0:
                        return None
                    from datetime import datetime as _dt2, timezone as _tz2
                    return _dt2.fromtimestamp(v, tz=_tz2.utc).isoformat()
                except Exception:
                    return None
            _fr_eta = _epoch_iso(card.get('eta'))
            _fr_sa = _epoch_iso(card.get('sched_arr'))
            if _fr_eta:
                chain['inbound_est_arr'] = _fr_eta
                # sched IMMER aus derselben Quelle/Uhr wie est (UTC) — ein
                # Board-sched (station-lokale Wanduhr) neben einem FR24-est
                # (UTC-Wanduhr) würde die Delay-Differenz um den TZ-Offset
                # verfälschen. Fehlt FR24-sched ⇒ sched ehrlich null (iOS
                # zeigt ohnehin nur EINE Zeit: est bevorzugt).
                chain['inbound_sched_arr'] = _fr_sa

    # NAS-Harvester-Snapshot-Tier (Owner-Idee 2026-07-08 „Supabase-Speicher der
    # Live-Map"): freshester FR24-gRPC-Snapshot dieses Tails aus `aircraft_live` —
    # BILLIGER Supabase-Read, sieht über Russland/Ozean, route-konsistent. Füllt die
    # Live-Position, BEVOR wir einen on-demand-gRPC-Korridor-Call machen. Der
    # Korridor unten greift dann nur noch, wenn der Snapshot alt/leer ist ODER die
    # Ankunftszeit noch fehlt.
    if inbound_origin and not chain['inbound_live'] and (reg or inbound_fn or cs):
        _snap_pos, _snap_route, _snap_reg, _snap_type = _aircraft_live_pos(
            reg=reg, flight=inbound_fn, callsign=cs, dep=dep)
        if _snap_pos and not _snap_pos.get('on_ground'):
            chain['inbound_live'] = _snap_pos
            # Wurde die Maschine per FLUGNUMMER gefunden und trägt einen ANDEREN
            # Tail als der (veraltete Roster-)Reg → DIESER ist der echte Zubringer
            # (Aircraft-Swap). Reg/Typ auf die Live-Wahrheit ziehen, damit Label
            # und Position zusammenpassen (Owner „stimmt nicht"-Konsistenz).
            _snap_rn = re.sub(r'[^A-Z0-9]', '', (_snap_reg or '').upper())
            _cur_rn = re.sub(r'[^A-Z0-9]', '', (reg or '').upper())
            if _snap_rn and _snap_rn != _cur_rn:
                reg = _snap_reg
                chain['reg'] = _snap_reg
                if _snap_type:
                    # Tail-Swap: der alte ac_type gehört zur ALTEN Maschine —
                    # der Snapshot-Typ des echten Tails gewinnt.
                    ac_type = _snap_type
                    chain['aircraft_type'] = _snap_type
            # SCHNELLE ETA aus dem Snapshot (gs + Großkreis-Reststrecke zum Ziel) —
            # Owner 2026-07-08 „mach das so": ersetzt den teuren Korridor-ETA-Call
            # über ungepollten Außenstationen. NUR wenn keine echte Board-ETA da ist
            # (die gewinnt). Cruise-Extrapolation ohne Sink-/Wind-Modell → grob,
            # daher KEINE Pünktlichkeits-Aussage (inbound_delay_min bleibt null,
            # weil kein sched_arr). Besser eine Richtzeit als „Ankunftszeit offen".
            if chain['inbound_est_arr'] is None and _snap_pos.get('gs'):
                _dl = _iata_latlon((dep or '').upper())
                if (_dl and None not in _dl
                        and _snap_pos.get('lat') is not None and _snap_pos.get('lon') is not None):
                    _rem_km = _gc_km(_snap_pos['lat'], _snap_pos['lon'], _dl[0], _dl[1])
                    _gs = _snap_pos['gs']
                    # Anker = seen_ts des Snapshots (nicht „jetzt": die Position ist
                    # bis 35 min alt, ab dort wurde die Reststrecke geflogen). Ohne
                    # seen_ts oder älter → NICHT schätzen (ehrlich offen lassen).
                    _seen = _snap_pos.get('seen_ts')
                    _seen_ep = None
                    if _seen is not None:
                        try:
                            _seen_ep = float(_seen)
                        except (TypeError, ValueError):
                            _seen_ep = _iso_to_epoch(_seen)
                    if (_rem_km and _gs and _gs > 80 and _seen_ep
                            and (time.time() - _seen_ep) <= 35 * 60):
                        from datetime import datetime as _de, timedelta as _tde, timezone as _tze
                        _eta = (_de.fromtimestamp(_seen_ep, tz=_tze.utc)
                                + _tde(hours=(_rem_km / 1.852) / _gs))
                        chain['inbound_est_arr'] = _eta.isoformat()
                        chain['inbound_est_estimated'] = True   # transparent: Richtzeit

    # FR24-gRPC KORRIDOR-Nachschlag (Owner-Durchbruch 2026-07-08 „haben doch eine
    # website die über Russland/Ozean liefert"): über Sibirien/Ozean ist FREIES
    # ADS-B blind → `pos` bleibt None → der pos-gegatete Block oben greift NICHT,
    # Ankunftszeit + Live-Position blieben null. ABER: wir kennen die Route
    # (inbound_origin=FRA → dep=HND). Ein live_feed über einer BoundingBox ENTLANG
    # des Großkreis-Korridors findet die Maschine per Reg AUCH über Russland — plus
    # flight_details (echte sched_arr/eta). EIN gRPC-Call (available()/Rate-Limit-
    # gegated, Timeout je 8 s), still bei Fehlschlag. Übernahme NUR bei Reg-Match.
    # LATENZ (Owner 2026-07-08 „findet LH716 nicht"): der Korridor-Call ist teuer
    # (~25 s: live_feed über Groß-Box + flight_details) und blockiert die ganze
    # Antwort → App-Timeout = Flieger erscheint gar nicht. Deshalb NUR noch als
    # POSITIONS-Beschaffer laufen lassen, wenn weder Snapshot noch freies ADS-B
    # eine Position lieferten. Hat der Snapshot die Position schon (Normalfall über
    # Russland), überspringen wir den Korridor → Antwort ~2 s, Flieger zeigt sofort.
    # (Die echte ETA über ungepollten Außenstationen ist ein separater Follow-up.)
    if reg and inbound_origin and not chain['inbound_live']:
        _oll = _iata_latlon((inbound_origin or '').upper())
        _dll = _iata_latlon((dep or '').upper())
        if _oll and _dll and None not in _oll and None not in _dll:
            try:
                from blueprints import fr24_grpc
                corr = fr24_grpc.inbound_by_route(
                    _oll[0], _oll[1], _dll[0], _dll[1], callsign=cs, reg=reg)
            except Exception:
                corr = None
            _creg = re.sub(r'[^A-Z0-9]', '', ((corr or {}).get('reg') or '').upper())
            _treg = re.sub(r'[^A-Z0-9]', '', (reg or '').upper())
            # Zusätzlich zum Reg-Match: die FR24-Route des Treffers muss WIRKLICH
            # an meinem Abflughafen enden (route_to==dep) — sonst fliegt der Tail
            # gerade einen anderen Leg im Korridor (kein Geister-Zubringer).
            # Fehlt die Route bei FR24 komplett (_cto is None), ist das KEIN
            # Mismatch: der Reg-Match bleibt gültig — sonst verlöre der Owner-
            # kritische Russland/Ozean-Zubringer still Position+ETA.
            _cto = _norm_iata((corr or {}).get('route_to'))
            if corr and _creg and _creg == _treg and (_cto is None or _cto == dep):
                def _epoch_iso2(v):
                    try:
                        v = int(v)
                        if v <= 0:
                            return None
                        from datetime import datetime as _d3, timezone as _t3
                        return _d3.fromtimestamp(v, tz=_t3.utc).isoformat()
                    except Exception:
                        return None
                if chain['inbound_est_arr'] is None:
                    _ce = _epoch_iso2(corr.get('eta'))
                    _cs2 = _epoch_iso2(corr.get('sched_arr'))
                    if _ce:
                        chain['inbound_est_arr'] = _ce
                        chain['inbound_sched_arr'] = _cs2
                # Live-Position über Russland/Ozean → Karte zeigt den echten Flieger.
                if (not chain['inbound_live']
                        and corr.get('lat') is not None and corr.get('lon') is not None
                        and (corr.get('flight_stage') or '').upper() == 'AIRBORNE'):
                    # Keys 1:1 wie der übrige inbound_live-Pfad (iOS AXLifecycleLive
                    # decodiert lat/lon/alt/gs/track/on_ground — `gs`, nicht `speed`).
                    chain['inbound_live'] = {
                        'lat': corr.get('lat'), 'lon': corr.get('lon'),
                        'track': corr.get('track'), 'alt': corr.get('alt'),
                        'gs': corr.get('speed'), 'on_ground': False,
                        'source': 'fr24_grpc_corridor',
                    }

    if chain['inbound_delay_min'] is None and chain['inbound_sched_arr'] and chain['inbound_est_arr']:
        # Delay nur aus GLEICHARTIGEN Zeitstempeln (beide naiv-lokal ODER beide
        # mit Offset) — _parse_local_iso strippt tzinfo, ein Mix wäre ±TZ falsch.
        def _has_off(s):
            s = str(s)
            return s.endswith('Z') or ('+' in s[10:]) or ('-' in s[19:])
        if _has_off(chain['inbound_sched_arr']) == _has_off(chain['inbound_est_arr']):
            _sa = _parse_local_iso(chain['inbound_sched_arr'])
            _ea = _parse_local_iso(chain['inbound_est_arr'])
            if _sa is not None and _ea is not None:
                # esti gesetzt → Delay ist bekannt (auch wenn 0/negativ = pünktlich/früh).
                chain['inbound_delay_min'] = int(round((_ea - _sa).total_seconds() / 60.0))

    # ── #2 Abflug-Delay-Prognose ──────────────────────────────────────────────
    # Beide Zeiten in STATIONSZEIT an `dep` vergleichen: sched_dep ist Board-
    # Wanduhr (naiv-lokal), die ETA kann aber aus FR24/Snapshot als UTC-Offset-ISO
    # kommen — _parse_local_iso strippt tzinfo, der Mix wäre um den TZ-Offset
    # falsch. Wandeln (statt Guard) hält die Prognose gerade im FR24-Fall am Leben.
    def _dep_local(s):
        if not s:
            return None
        from datetime import datetime as _dtx
        try:
            dt = _dtx.fromisoformat(str(s).replace('Z', '+00:00'))
        except (TypeError, ValueError):
            dt = _parse_local_iso(s)
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt                     # schon Station-Wanduhr (Board)
        try:
            from airport_tz import airport_tz as _atz
            from zoneinfo import ZoneInfo
            _tzn = _atz((dep or '').upper())
            if _tzn:
                return dt.astimezone(ZoneInfo(_tzn)).replace(tzinfo=None)
        except Exception:
            pass
        return None   # Offset-Zeit ohne bekannte Station-TZ → keine Prognose

    sd = _dep_local(sched_dep)
    eta = _dep_local(chain['inbound_est_arr']) if chain['inbound_est_arr'] else None
    if eta is None and chain['inbound_sched_arr'] and chain['inbound_delay_min'] is not None:
        base = _dep_local(chain['inbound_sched_arr'])
        if base is not None:
            eta = base + timedelta(minutes=int(chain['inbound_delay_min']))
    city = (chain['inbound_origin'] or {}).get('city') or inbound_origin
    if sd is not None and eta is not None:
        # sched_dep UND Ankunft am selben Flughafen (dep_iata) → identische TZ,
        # naiver Vergleich ist korrekt (keine Zeitzonen-Mathematik nötig).
        turn = _turnaround_min_for_type(ac_type)
        earliest = max(sd, eta + timedelta(minutes=turn))
        forecast['forecast_dep_delay_min'] = max(0, int(round(
            (earliest - sd).total_seconds() / 60.0)))
        forecast['min_turnaround_min'] = turn
        airborne = bool(chain['inbound_live'])
        forecast['confidence'] = 'hoch' if (chain['inbound_est_arr'] and airborne) else 'mittel'
        d = chain['inbound_delay_min']
        if d is not None and d > 0:
            forecast['reason'] = f'Maschine kommt +{d} aus {city}.'
        elif d is not None:
            forecast['reason'] = f'Maschine kommt pünktlich aus {city}.'
        else:
            forecast['reason'] = f'Maschine kommt aus {city}.'
    else:
        # Zubringer identifiziert, aber keine belastbare Ankunftszeit → KEINE
        # Prognose (niemals „pünktlich" behaupten), Herkunft trotzdem nennen.
        forecast['reason'] = f'Zubringer aus {city} — Ankunftszeit noch offen.'
    return chain, forecast, my


@aerox_data_bp.route('/api/ax/flight-inbound-chain/<token>', methods=['GET'])
def ax_flight_inbound_chain(token):
    """#1 Tail-Verkettung + #2 Abflug-Delay-Prognose in EINEM Payload.
    Query: flight_no, date=YYYY-MM-DD, dep_iata. Gratis (Warehouse/Board-Enrich +
    OpenSky-Live). iOS zeigt „Deine Maschine kommt +40 aus Madrid, Abflug vsl.
    +15" auf der Vorflug-Karte; pollt ~60 s."""
    from flask import request
    flight_no = (request.args.get('flight_no') or '').replace(' ', '').upper().strip()
    date = (request.args.get('date') or '').strip()[:10] or None
    dep_iata = _norm_iata(request.args.get('dep_iata'))
    reg_hint = (request.args.get('reg') or '').strip().upper() or None
    # Optional (neuer Client, Owner 2026-07-07): eigenes Ziel + eigener Roster-
    # Abflug (UTC-ISO aus ical_sectors) → schaltet den Außenstations-Rotations-
    # Tier frei (Gegenroute arr_iata→dep_iata) und wählt dort die Maschine,
    # die VOR dem eigenen Abflug Richtung Außenstation startet.
    arr_iata = _norm_iata(request.args.get('arr_iata'))
    my_dep_utc = None
    dep_iso = (request.args.get('dep_iso') or '').strip()
    if dep_iso:
        try:
            from datetime import datetime as _dt, timezone as _tz
            my_dep_utc = _dt.fromisoformat(dep_iso.replace('Z', '+00:00'))
            if my_dep_utc.tzinfo is None:
                my_dep_utc = my_dep_utc.replace(tzinfo=_tz.utc)
            my_dep_utc = my_dep_utc.astimezone(_tz.utc)
        except Exception:
            my_dep_utc = None
    if len(flight_no) < 3 or not dep_iata:
        return jsonify({'ok': False, 'error': 'need_flight_no_and_dep_iata'}), 400
    mkey = ('chain', flight_no, date or '', dep_iata, reg_hint or '',
            arr_iata or '', dep_iso)
    memo = _memo_get(mkey)
    if memo is not None:
        return jsonify(memo)
    chain, forecast, _my = _build_inbound_chain(
        flight_no, date, dep_iata, reg_hint,
        arr_iata=arr_iata, my_dep_utc=my_dep_utc)
    payload = {
        'ok': True, 'flight': flight_no, 'date': date, 'dep_iata': dep_iata,
        **chain, 'dep_delay_forecast': forecast,
    }
    return jsonify(_memo_put(mkey, payload))


@aerox_data_bp.route('/api/ax/flight-live/<token>', methods=['GET'])
def ax_flight_live(token):
    """#3 Live-Track der EIGENEN Maschine für die In-Flight-Karte. Query:
    flight_no, date, reg (optional, echter Roster-Tail — Owner-Algorithmus
    „Plan sagt D-ABYO → Reg→Hex→ADS-B findet ihn, wo auch immer er ist"),
    dep_iata/arr_iata (optional, Leg-Airports fürs Board-/Store-Keying).
    Reg→Hex→OpenSky-Position + free-first-Route,
    dazu dep/dest (IATA+Stadt), sched/est-Ankunft, Ankunfts-Delay, Ziel-Gate und
    Großkreis-Fortschritt 0..1. Gratis, cachebar, iOS pollt ~30–60 s."""
    from flask import request
    flight_no = (request.args.get('flight_no') or '').replace(' ', '').upper().strip()
    date = (request.args.get('date') or '').strip()[:10] or None
    reg = (request.args.get('reg') or '').strip().upper() or None
    # Optionale Leg-Airports vom Client (Roster kennt sie) — damit kann der
    # Dual-Side-Merge die richtigen Boards/Store-Keys scannen. Ohne sie sah
    # `_flight_obs_merged` früher NICHTS (keine Airports = keine Store-Keys)
    # und die Kette starb an reg=null, obwohl das Warehouse den Tag kannte.
    q_dep = _norm_iata(request.args.get('dep_iata'))
    q_arr = _norm_iata(request.args.get('arr_iata'))
    if len(flight_no) < 3:
        return jsonify({'ok': False, 'error': 'need_flight_no'}), 400
    mkey = ('live', flight_no, date or '', reg or '', q_dep or '', q_arr or '')
    memo = _memo_get(mkey)
    if memo is not None:
        return jsonify(memo)
    merged_fn = _life_app('_flight_obs_merged')
    my = (merged_fn(flight_no, date=date, dep_iata=q_dep, arr_iata=q_arr,
                    free_only=True) if merged_fn else None)
    # Reg-Kaskade (nie geraten): expliziter ?reg= (echter Roster-Tail) →
    # Dual-Side-Merge → SB-Tages-Rows des EXAKTEN Datums (wie /flight-info).
    sb_dep = sb_arr = None
    if not reg:
        reg = (my or {}).get('reg') or None
    if not reg:
        reg, _sb_tc, sb_dep, sb_arr = _sb_day_reg(flight_no, date)
    hexid, cs, pos, route = (_machine_live(reg, targeted=True)
                             if reg else (None, None, None, None))
    src, dst = _route_endpoints(route)
    dep = src or _norm_iata((my or {}).get('dep_iata')) or q_dep or _norm_iata(sb_dep)
    dest = dst or _norm_iata((my or {}).get('arr_iata')) or q_arr or _norm_iata(sb_arr)
    # Freies ADS-B ist über der SÜDROUTE (LH meidet russischen Luftraum!) und über
    # Ozean oft blind → pos=None → die iOS-Karte hängt in „Dein Flug live wird
    # geladen" und simuliert notfalls einen Großkreis ÜBER RUSSLAND (falsch, Owner
    # 2026-07-09 „wir dürfen nicht mal über Russland fliegen"). Fallback: die ECHTE
    # Position aus dem NAS-Harvester-Store (aircraft_live, FR24-gRPC) — sie liegt
    # real auf der Südroute und kommt schnell (kein on-demand-gRPC-Call).
    if pos is None or pos.get('on_ground'):
        # Ohne bekanntes Ziel (dest=None) ist der reg-Match NICHT route-konsistent
        # prüfbar → der Tail könnte gerade einen fremden Leg fliegen. Dann nur den
        # flight-Match zulassen (die Flugnummer identifiziert den Leg selbst).
        _snap, _srt, _sreg, _stype = _aircraft_live_pos(
            reg=(reg if dest else None), flight=flight_no, dep=dest)
        if _snap and not _snap.get('on_ground'):
            pos = _snap
            if _srt and _srt[0] and not src:
                src2, dst2 = _srt
                dep = dep or src2
                dest = dest or dst2
    # Ankunfts-Seite (Zeiten/Delay/Gate) frisch für die konkrete Strecke.
    merged = (merged_fn(flight_no, date=date, dep_iata=dep, arr_iata=dest,
                        free_only=True) if merged_fn else None) or my or {}
    in_flight = bool(pos and not pos.get('on_ground'))
    payload = {
        'ok': True, 'flight': flight_no, 'date': date,
        'reg': reg, 'hex': hexid, 'callsign': cs,
        # Flugzeugtyp aus dem Board/Warehouse (nie geraten) — für die „Flieger"-
        # Zeile der In-Flight-Karte (reg · Muster).
        'aircraft_type': (merged.get('aircraft') or None),
        'dep': _airport_brief(dep), 'dest': _airport_brief(dest),
        # ABFLUG Soll/Ist (station-lokal, wie die Ankunfts-Seite) — Owner-Wunsch
        # „Abflug Soll und Ist fehlt". Die Werte liegen schon im Dual-Side-Merge,
        # wurden nur nicht durchgereicht. dep_delay_min nur bei bekanntem Delay.
        'sched_dep': merged.get('sched_dep'),
        'est_dep': merged.get('esti_dep'),
        'dep_delay_min': (merged.get('dep_delay_min')
                          if merged.get('delay_known') else None),
        'dep_gate': merged.get('gate_dep'),
        'sched_arr': merged.get('sched_arr'),
        'est_arr': merged.get('esti_arr'),
        'arr_delay_min': (merged.get('arr_delay_min')
                          if merged.get('delay_known') else None),
        'dest_gate': merged.get('gate_arr'),
        'live': pos,
        'in_flight': in_flight,
        'progress': (_progress_along_route(dep, dest, pos) if in_flight else None),
        'source': (route.get('source') if route else None),
    }
    # FLIGHTSTATE-Engine (Kill-Switch FLIGHTSTATE_LIVE_FLIGHT=1): die Engine
    # entscheidet live/in_flight/progress (Taxi ⇒ live=None ⇒ kein Geist) und
    # liefert die ehrliche Phase. Reuse von merged + pos (kein Extra-Read).
    try:
        if os.environ.get('FLIGHTSTATE_LIVE_FLIGHT', '') in ('1', 'true', 'yes'):
            from blueprints.flight_state_collectors import (
                build_keys as _fs_bk, obs_from_board_merged as _fs_obm,
                obs_from_pos as _fs_op)
            from blueprints.flight_state import (
                resolve_flight_state as _fs_resolve, project_flight_live as _fs_proj,
                prior_state as _fs_prior, remember_state as _fs_remember)
            _to_utc = _life_app('_board_local_to_utc_iso')
            _fs_keys = _fs_bk(
                flight_no, date, dep, dest, roster_tail=reg, callsign=cs,
                sched_dep_iso=(_to_utc(merged.get('sched_dep'), dep) if _to_utc else None),
                sched_arr_iso=(_to_utc(merged.get('sched_arr'), dest) if (_to_utc and dest) else None),
                dep_ll=_iata_latlon(dep or ''), arr_ll=_iata_latlon(dest or ''),
                dep_elev_ft=_iata_elev_ft(dep or ''))
            _fs_obs = _fs_obm(merged, _fs_keys, board_to_iso=_to_utc)
            if pos:
                # pos trägt seen_ts+source aus position_for_flight bzw.
                # _aircraft_live_pos (P1-4b) — obs_from_pos mappt die
                # Resolver-Tags (fr24/aircraft_positions/…) selbst.
                _fs_obs += _fs_op(pos, (pos.get('source') or 'adsb'))
            # prior = letztes Resultat dieses Flug-Tages → Monotonie/Sticky wirken
            _fs = _fs_resolve(_fs_keys, _fs_obs,
                              prior=_fs_prior(flight_no, date))
            _fs_remember(_fs)
            _pl = _fs_proj(_fs)
            payload['live'] = _pl['live']
            payload['in_flight'] = _pl['in_flight']
            payload['progress'] = _fs['progress']
            payload['phase'] = _fs['phase']
            payload['phase_conf'] = _fs['phase_conf']
            # additiv: 'lost' ⇒ iOS kann die Vorwärts-Simulation ehrlich stoppen
            payload['live_status'] = _fs.get('live_status')
            payload['eta_iso'] = _fs['times']['eta_iso']
            payload['eta_conf'] = _fs['times']['eta_conf']
    except Exception:
        pass
    return jsonify(_memo_put(mkey, payload))


def _derive_on_time(delay_known, delay_min, cancelled):
    """PÜNKTLICH-Verdikt aus echten Board-Daten — NIE erfunden.
      • cancelled → False (annulliert schlägt jeden Delay-Wert).
      • delay_known False → None (neutral: „Status wird ermittelt", kein +0,
        kein „PÜNKTLICH"-Claim — unbekannt ≠ pünktlich).
      • delay_known True → D15-Schwelle: <15 min = pünktlich, sonst verspätet.
    """
    if cancelled:
        return False
    if not delay_known:
        return None
    try:
        return int(delay_min or 0) < 15
    except Exception:
        return None


@aerox_data_bp.route('/api/ax/my-flight-status/<token>', methods=['GET'])
def ax_my_flight_status(token):
    """Dünner Wrapper um _flight_obs_merged für die „Wo ist mein Flieger"-Karte:
    Tail (reg) + Pünktlich-Verdikt des EIGENEN abgehenden Legs. Query: flight_no,
    date=YYYY-MM-DD, dep_iata. free_only=True (kein AeroDataBox-Spend), memoisiert
    (~90 s). Nur echte Board/Warehouse-Daten — nie Position/Delay erfunden:
    delay_known=False → on_time=None (neutral), cancelled → on_time=False.
    est_dep_iso/est_arr_iso stehen als echt-UTC (…Z), station-lokal erst beim
    iOS-Rendern — kein Doppel-Shift."""
    from flask import request
    flight_no = (request.args.get('flight_no') or '').replace(' ', '').upper().strip()
    date = (request.args.get('date') or '').strip()[:10] or None
    dep_iata = _norm_iata(request.args.get('dep_iata'))
    if len(flight_no) < 3 or not dep_iata:
        return jsonify({'ok': False, 'error': 'need_flight_no_and_dep_iata'}), 400
    mkey = ('mystatus', flight_no, date or '', dep_iata)
    memo = _memo_get(mkey)
    if memo is not None:
        return jsonify(memo)
    merged_fn = _life_app('_flight_obs_merged')
    to_utc = _life_app('_board_local_to_utc_iso')
    m = (merged_fn(flight_no, date=date, dep_iata=dep_iata, free_only=True)
         if merged_fn else None)
    if not m:
        # Kein Signal → EHRLICH: kein Tail, kein Verdikt (iOS versteckt/Route-only).
        payload = {
            'ok': True, 'flight': flight_no, 'date': date, 'dep_iata': dep_iata,
            'reg': None, 'delay_known': False, 'status': None, 'cancelled': False,
            'delay_min': None, 'est_dep_iso': None, 'est_arr_iso': None,
            'on_time': None,
        }
        return jsonify(_memo_put(mkey, payload))
    delay_known = bool(m.get('delay_known'))
    cancelled = bool(m.get('cancelled'))
    delay_min = m.get('delay_min') if delay_known else None
    arr_iata = _norm_iata(m.get('arr_iata'))
    payload = {
        'ok': True, 'flight': flight_no, 'date': date, 'dep_iata': dep_iata,
        'arr_iata': arr_iata,
        'reg': m.get('reg'),          # NUR echt aus Board/Warehouse, nie geraten
        'aircraft': m.get('aircraft'),
        'delay_known': delay_known,
        'status': m.get('status'),
        'cancelled': cancelled,
        'delay_min': delay_min,
        'delay_side': m.get('delay_side'),
        'est_dep_iso': (to_utc(m.get('esti_dep'), dep_iata) if to_utc else None),
        'est_arr_iso': (to_utc(m.get('esti_arr'), arr_iata) if (to_utc and arr_iata)
                        else None),
        'on_time': _derive_on_time(delay_known, delay_min, cancelled),
    }
    # FLIGHTSTATE-Engine (Kill-Switch FLIGHTSTATE_LIVE_MYSTATUS=1): ehrliche Phase
    # aus dem Board (kein Positions-Feed hier) — additiv, Legacy-Felder unberührt.
    try:
        if os.environ.get('FLIGHTSTATE_LIVE_MYSTATUS', '') in ('1', 'true', 'yes'):
            from blueprints.flight_state_collectors import (
                build_keys as _fs_bk, obs_from_board_merged as _fs_obm)
            from blueprints.flight_state import (
                resolve_flight_state as _fs_resolve,
                prior_state as _fs_prior, remember_state as _fs_remember)
            _fs_keys = _fs_bk(
                flight_no, date, dep_iata, arr_iata, roster_tail=m.get('reg'),
                sched_dep_iso=(to_utc(m.get('sched_dep'), dep_iata) if to_utc else None),
                sched_arr_iso=(to_utc(m.get('sched_arr'), arr_iata) if (to_utc and arr_iata) else None),
                dep_ll=_iata_latlon(dep_iata or ''), arr_ll=_iata_latlon(arr_iata or ''),
                dep_elev_ft=_iata_elev_ft(dep_iata or ''))
            # prior → Monotonie/Sticky-Airborne über Requests hinweg
            _fs = _fs_resolve(_fs_keys, _fs_obm(m, _fs_keys, board_to_iso=to_utc),
                              prior=_fs_prior(flight_no, date))
            _fs_remember(_fs)
            payload['phase'] = _fs['phase']
            payload['phase_conf'] = _fs['phase_conf']
            payload['eta_iso'] = _fs['times']['eta_iso']
            payload['eta_conf'] = _fs['times']['eta_conf']
    except Exception:
        pass
    return jsonify(_memo_put(mkey, payload))


@aerox_data_bp.route('/api/ax/turnaround/<token>', methods=['GET'])
def ax_turnaround(token):
    """#4 Turnaround → nächster Sektor. Query: flight_no, dep, arr (=Wende-
    Flughafen), date, next_flight_no, next_arr. Gleiche Reg → same_aircraft:true +
    Bodenzeit + next_gate; neue Maschine → deren Inbound-Chain (#1) + Prognose
    (#2). Ehrlich: same_aircraft:null wenn eine Reg-Seite unbekannt ist."""
    from flask import request
    cur_fn = (request.args.get('flight_no') or '').replace(' ', '').upper().strip()
    cur_dep = _norm_iata(request.args.get('dep'))
    turn = _norm_iata(request.args.get('arr'))          # Wende-Flughafen
    date = (request.args.get('date') or '').strip()[:10] or None
    next_fn = (request.args.get('next_flight_no') or '').replace(' ', '').upper().strip()
    next_arr = _norm_iata(request.args.get('next_arr'))
    if len(cur_fn) < 3 or not turn or len(next_fn) < 3:
        return jsonify({'ok': False, 'error': 'need_current_and_next_sector'}), 400
    mkey = ('turn', cur_fn, date or '', turn, next_fn)
    memo = _memo_get(mkey)
    if memo is not None:
        return jsonify(memo)
    from datetime import timedelta
    merged_fn = _life_app('_flight_obs_merged')
    # Reg der ANKOMMENDEN (aktuellen) Maschine + Soll-Ankunft am Wende-Flughafen.
    cur = (merged_fn(cur_fn, date=date, dep_iata=cur_dep, arr_iata=turn,
                     free_only=True) if merged_fn else None) or {}
    reg_cur = cur.get('reg') or None
    cur_sched_arr = cur.get('sched_arr')
    cur_est_arr = cur.get('esti_arr')
    # Nächster Sektor: Inbound-Chain (löst zugleich reg_next am Wende-Flughafen auf).
    chain, forecast, next_my = _build_inbound_chain(next_fn, date, turn)
    reg_next = chain.get('reg') or (next_my or {}).get('reg')
    next_sched_dep = (next_my or {}).get('sched_dep')
    next_gate = (next_my or {}).get('gate_dep')
    # Bodenzeit (Soll) — Ankunft & Abflug am selben Flughafen → gleiche TZ.
    ground_min = None
    a = _parse_local_iso(cur_est_arr or cur_sched_arr)
    dpt = _parse_local_iso(next_sched_dep)
    if a is not None and dpt is not None:
        gm = int(round((dpt - a).total_seconds() / 60.0))
        if -180 <= gm <= 24 * 60:
            ground_min = gm
    if reg_cur and reg_next:
        same = (reg_cur == reg_next)
    else:
        same = None                       # eine Seite unbekannt → ehrlich offen
    payload = {
        'ok': True, 'turnaround_airport': _airport_brief(turn), 'date': date,
        'current_flight': cur_fn, 'next_flight': next_fn,
        'next_dest': _airport_brief(next_arr),
        'same_aircraft': same,
        'reg': reg_cur or reg_next,
        'ground_time_min': ground_min,
        'next_gate': next_gate,
        'next_sched_dep': next_sched_dep,
        'current_sched_arr': cur_sched_arr,
        'current_est_arr': cur_est_arr,
    }
    if same is not True:
        # Neue (oder unbestimmte) Maschine → welcher Zubringer bringt sie?
        payload['inbound_chain'] = chain
        payload['dep_delay_forecast'] = forecast
    return jsonify(_memo_put(mkey, payload))


def _local_to_utc(s, iata):
    """Naiver Lokalzeit-String am Flughafen `iata` → aware UTC-datetime (via
    airport_tz + zoneinfo). None bei Unparsbar/unbekannter TZ."""
    from datetime import timezone
    dt = _parse_local_iso(s)
    if dt is None:
        return None
    try:
        from airport_tz import airport_tz
        from zoneinfo import ZoneInfo
        tzn = airport_tz((iata or '').upper()) or 'UTC'
        return dt.replace(tzinfo=ZoneInfo(tzn)).astimezone(timezone.utc)
    except Exception:
        try:
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None


@aerox_data_bp.route('/api/ax/flight-recap/<token>', methods=['GET'])
def ax_flight_recap(token):
    """#5 Post-Flight-Recap. Query: flight_no, date, dep_iata, arr_iata (die
    beiden Airports gibt der Roster-Leg mit — nötig, damit der Dual-Side-Resolver
    die richtige Board/Warehouse-Zeile findet). Finalizer-Wahrheit (on_time/late/
    cancelled, delay_known), Block-/Flugzeit wenn aus Obs ableitbar, tatsächliche
    Ab-/Ankunftszeiten. Gratis. Solange nichts Bekanntes vorliegt: status='pending'
    + „wird noch ermittelt" (NIE „pünktlich" behaupten)."""
    from flask import request
    flight_no = (request.args.get('flight_no') or '').replace(' ', '').upper().strip()
    date = (request.args.get('date') or '').strip()[:10] or None
    q_dep = _norm_iata(request.args.get('dep_iata'))
    q_arr = _norm_iata(request.args.get('arr_iata'))
    if len(flight_no) < 3:
        return jsonify({'ok': False, 'error': 'need_flight_no'}), 400
    mkey = ('recap', flight_no, date or '', q_dep or '', q_arr or '')
    memo = _memo_get(mkey)
    if memo is not None:
        return jsonify(memo)
    merged_fn = _life_app('_flight_obs_merged')
    m = (merged_fn(flight_no, date=date, dep_iata=q_dep, arr_iata=q_arr,
                   free_only=True) if merged_fn else None)
    thr = _life_app('_DELAY_THRESHOLD_MIN', 15)
    if not m:
        payload = {'ok': True, 'flight': flight_no, 'date': date,
                   'status': 'pending', 'delay_known': False,
                   'message': 'wird noch ermittelt'}
        return jsonify(_memo_put(mkey, payload))
    dep = _norm_iata(m.get('dep_iata'))
    dest = _norm_iata(m.get('arr_iata'))
    cancelled = bool(m.get('cancelled'))
    known = bool(m.get('delay_known'))
    best = m.get('delay_min')
    if cancelled:
        status = 'cancelled'
    elif known:
        status = 'on_time' if (best is not None and best < int(thr)) else 'late'
    else:
        status = 'pending'
    # Block-/Flugzeit nur wenn tatsächliche (IST) Zeiten beider Seiten vorliegen.
    actual_dep = m.get('esti_dep')
    actual_arr = m.get('esti_arr')
    block_min = None
    du = _local_to_utc(actual_dep, dep) if (actual_dep and dep) else None
    au = _local_to_utc(actual_arr, dest) if (actual_arr and dest) else None
    if du is not None and au is not None:
        bm = int(round((au - du).total_seconds() / 60.0))
        if 0 < bm <= 20 * 60:
            block_min = bm
    payload = {
        'ok': True, 'flight': flight_no, 'date': date,
        'dep': _airport_brief(dep), 'dest': _airport_brief(dest),
        'status': status, 'delay_known': known, 'cancelled': cancelled,
        'delay_min': (best if known else None),
        'sched_dep': m.get('sched_dep'), 'sched_arr': m.get('sched_arr'),
        'actual_dep': actual_dep, 'actual_arr': actual_arr,
        'block_time_min': block_min,
        'message': ('wird noch ermittelt' if status == 'pending' else None),
    }
    return jsonify(_memo_put(mkey, payload))
