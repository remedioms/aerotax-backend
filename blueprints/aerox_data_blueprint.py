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


def _cache_get(table, key_col, key):
    sb = _sb()
    if sb is None:
        return None
    try:
        res = sb.table(table).select('payload').eq(key_col, key).limit(1).execute()
        rows = getattr(res, 'data', None) or []
        if rows and rows[0].get('payload'):
            p = rows[0]['payload']
            return p if isinstance(p, dict) else json.loads(p)
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
        req = urllib.request.Request(url, headers={'User-Agent': 'AeroX-DataEngine/1.0'})
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
    cands = []
    al = _airline_row(prefix)
    if al and al.get('iata'):
        cands.append(f"{al['iata']}{suffix}")
    cands.append(cs)                       # falls die Tafel die ICAO-Flugnr führt
    for fn in cands:
        try:
            r = (sb.table('airport_delay_obs')
                 .select('airport,dest_iata,gate,terminal')
                 .eq('flight', fn)
                 .order('date', desc=True).order('updated_at', desc=True)
                 .limit(6).execute())
            rows = r.data or []
            # Abflug-Record (airport=Origin); Ankunfts-Keys ('<AP>#ARR') überspringen.
            dep = next((x for x in rows if '#' not in (x.get('airport') or '')), None)
            if dep and dep.get('dest_iata'):
                return {'src': (dep.get('airport') or '').split('#', 1)[0],
                        'dst': dep.get('dest_iata'),
                        'gate': dep.get('gate'), 'terminal': dep.get('terminal'),
                        'source': 'aerox_board', 'callsign': cs}
        except Exception:
            pass
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
    _paid_budget_inc()              # zählt gegen das Tages-Paid-Budget
    d = _http_json(url, timeout=12)
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
#                             (_paid_budget_ok, AX_PAID_DAILY_CAP=50) es erlaubt.
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


def _adb_flight_to_route(f, cs):
    """AeroDataBox-Flight-Objekt → route-Dict (src/dst IATA+ICAO). None bei Müll."""
    dep = ((f.get('departure') or {}).get('airport') or {})
    arr = ((f.get('arrival') or {}).get('airport') or {})
    src = (dep.get('iata') or '').upper() or None
    dst = (arr.get('iata') or '').upper() or None
    if not src or not dst:
        return None
    return {
        'src': src, 'src_icao': (dep.get('icao') or '').upper() or None,
        'dst': dst, 'dst_icao': (arr.get('icao') or '').upper() or None,
        'callsign': cs, 'status': f.get('status'),
        'reg': ((f.get('aircraft') or {}).get('reg') or '').upper() or None,
    }


def _adb_pick_active_leg(flights, cs, reg, track):
    """Aus mehreren AeroDataBox-Flügen (gleiche Nummer/Reg, mehrere Legs am Tag)
    das AKTUELLE Leg wählen: 1) exakter reg-Match, 2) Status enroute/departed/
    active, 3) Kurs-Match (dep→arr-Bearing ~ track ±70°), sonst der erste.
    Rückgabe (route_dict, ambiguous_bool)."""
    routes = [(f, _adb_flight_to_route(f, cs)) for f in flights]
    routes = [(f, r) for f, r in routes if r]
    if not routes:
        return None, False
    if len(routes) == 1:
        return routes[0][1], False
    reg_u = (reg or '').upper()
    if reg_u:
        for f, r in routes:
            if r.get('reg') == reg_u:
                return r, False
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


def _aerodatabox_route(cs, reg=None, lat=None, lon=None, track=None, date=None):
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
    host = 'aerodatabox.p.rapidapi.com'
    hdr = {'x-rapidapi-key': key, 'x-rapidapi-host': host,
           'User-Agent': 'AeroX-DataEngine/1.0'}

    def _get(path):
        _paid_budget_inc()      # jeder Request zählt gegen das Tages-Budget
        try:
            req = urllib.request.Request(f'https://{host}{path}', headers=hdr)
            with urllib.request.urlopen(req, timeout=10) as r:
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
#  Harter Tages-Deckel (AX_PAID_DAILY_CAP, Default 50). Persistiert in
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
    """True solange heute noch bezahlte Calls frei sind. Über dem Deckel → False
    (blockt AeroDataBox + AviationStack hart)."""
    cap = int(os.environ.get('AX_PAID_DAILY_CAP', '50'))
    return _paid_daily_used() < cap


def _paid_budget_inc():
    key = _paid_daily_key()
    used = _MEM_BUDGET.get(key, 0) + 1
    _MEM_BUDGET[key] = used          # In-Memory IMMER zählen (Safety-Net)
    sb = _sb()
    if sb is None:
        return
    try:
        sb.table('ax_api_budget').upsert(
            {'month': key, 'n': max(used, _paid_daily_used()),
             'updated_at': _iso_now()}).execute()
    except Exception:
        pass


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
        except Exception:
            pass
    _maybe_evict_tracks()
    _open_legs_evict_stale()
    return recorded


def _confirmed_origin_from_tracking(hexid):
    """Wenn unsere EIGENE ADS-B-State-Machine (observe_adsb_positions) diese Maschine
    aus einem Flughafen hat abheben sehen, KENNEN wir ihren Abflug — selbst
    beobachtet, also CONFIRMED. Gibt (iata, icao) zurück oder None. Wirft NIE.
    Damit lässt sich ein eigener bestätigter Abflug mit einer geometrisch
    bestätigten Ziel-Richtung zu einer voll-eigenen confirmed-Route kombinieren."""
    if not hexid:
        return None
    try:
        # Erst Front-Cache, dann durabler Open-Leg-Store (übersteht Restarts) →
        # ein vor dem Container-Recycle beobachteter Abflug bleibt bekannt.
        st = _open_leg_get(str(hexid).strip().lower())
        if st and st.get('phase') == 'air' and st.get('dep'):
            return (st.get('dep'), st.get('dep_icao'))
    except Exception:
        pass
    return None


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

        # Am Boden weit weg vom Ziel → Roll-Kurs bedeutungslos → nicht widerlegbar → zeigen.
        if on_ground:
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


def _free_generic_route(cs, lat=None, lon=None):
    """Freie, generische Callsign→Route-Fallbacks: adsbdb → adsb.lol routeset →
    hexdb. Alle frei/öffentlich. Route-Dict (confidence='estimated') oder None."""
    try:
        ad = _adsbdb_route(cs)
    except Exception:
        ad = None
    if ad and (ad.get('src') or ad.get('src_icao')):
        ad['source'] = 'adsbdb'
        ad['confidence'] = 'estimated'
        return ad
    # adsb.lol routeset + hexdb liegen im adsb_blueprint (frei, community).
    try:
        from blueprints.adsb_blueprint import _resolve_route_adsb_lol, _resolve_route_hexdb
    except Exception:
        return None
    for fn, name in ((_resolve_route_adsb_lol, 'adsb.lol'), (_resolve_route_hexdb, 'hexdb')):
        try:
            o, d = (fn(cs, lat, lon) if name == 'adsb.lol' else fn(cs))
        except Exception:
            o, d = None, None
        if o and d and (o.get('iata') or o.get('icao')) and (d.get('iata') or d.get('icao')):
            return {
                'src': o.get('iata'), 'src_icao': o.get('icao'),
                'dst': d.get('iata'), 'dst_icao': d.get('icao'),
                'callsign': cs, 'source': name, 'confidence': 'estimated',
            }
    return None


def _resolve_live_route(callsign, hexid=None, reg=None, lat=None, lon=None,
                        track=None, gs=None, on_ground=False):
    """OWN-DATA-FIRST Kaskade → genaue heutige Route eines Live-Fliegers.
    Rückgabe: route-Dict mit src/dst(+_icao), source, confidence(+optional
    status/gate/terminal/reg) — oder None. Siehe Header-Block für die Priorität.
    Jeder externe Treffer wird via _record_resolved_route in die Warehouse
    geschrieben (fills our own DB)."""
    cs = (callsign or '').strip().upper().replace(' ', '')
    if not cs:
        return None
    reg = (reg or '').strip().upper() or None
    hexid = (hexid or '').strip().lower() or None
    date = _today_utc()
    dk = date.replace('-', '')

    # ── 1. Eigene Warehouse: date-gekeyter Cache (exakt heute) ──────────────
    cached = _cache_get('ax_route_cache', 'flight', f'{cs}@{dk}')
    if cached and (cached.get('src') or cached.get('src_icao')):
        cached.setdefault('confidence', 'confirmed')
        cached['_from'] = 'cache_date'
        return cached
    # 1b. reg-gekeyter Cache (physische Maschine, heute)
    if reg:
        rc = _cache_get('ax_route_cache', 'flight', f'REG:{reg}@{dk}')
        if rc and (rc.get('src') or rc.get('src_icao')):
            rc.setdefault('confidence', 'confirmed')
            rc['_from'] = 'cache_reg'
            return rc
    # 1c. Eigene Airport-Tafel (autoritativ, flughafen-eigene Daten)
    try:
        obs = _route_from_obs(cs)
    except Exception:
        obs = None
    if obs and (obs.get('src') or obs.get('dst')):
        obs['source'] = 'aerox_board'
        obs['confidence'] = 'confirmed'
        _record_resolved_route(cs, reg, obs, date)
        return obs

    # ── 2. Selbst berechnet aus eigenem ADS-B ───────────────────────────────
    #  Fertige Legs landen via observe_adsb_positions() bereits in ax_route_cache
    #  → Schritt 1 (cache_date/cache_reg) serviert sie GRATIS. Kein separater
    #  Netz-Call hier; die Engine füllt den Cache im Poller/Area-Consumer.

    # ── 3. OpenSky (FREI mit Account; env-guarded, ohne Creds → None) ───────
    osky = _opensky_route(hexid)
    if osky and (osky.get('src') or osky.get('dst')):
        _record_resolved_route(cs, reg, osky, date)
        return osky

    # ── 4. Freie generische Lookups (adsbdb / adsb.lol / hexdb) ─────────────
    #  Das sind GENERISCHE Flugplan-Kandidaten. Owner-Regel: „scraped/eigene Infos
    #  sind #1 — müssen nicht geprüft werden; generische ZEIGEN, außer die Geometrie
    #  widerspricht KLAR." Wir zeigen die Route also DEFAULT, und verwerfen sie NUR,
    #  wenn der Flieger eindeutig in die falsche Richtung fliegt (>115° weg, fern
    #  beider Endpunkte). Alles andere (Anflug/Holding/gerade gestartet/Rauschen)
    #  → Route wird gezeigt.
    gen = _free_generic_route(cs, lat, lon)
    if gen and (gen.get('src') or gen.get('src_icao')):
        if _geometry_allows_route(gen, lat, lon, track, gs, on_ground):
            # Eigenen bestätigten Abflug (aus unserem Tracking) bevorzugen → dann
            # ist der Abflug eigen-beobachtet (unsere #1-Quelle).
            own = _confirmed_origin_from_tracking(hexid)
            if own and own[0]:
                gen['src'], gen['src_icao'] = own[0], own[1]
                gen['source'] = (gen.get('source') or 'generic') + '+own_origin'
            # confidence bleibt 'estimated' (interner Wert): Client zeichnet die
            # Route OHNE „bestätigt"-Siegel und ohne „geschätzt"-Label — nur die
            # Strecke selbst (Owner: Routen sichtbar, kein Angst-Label).
            _record_resolved_route(cs, reg, gen, date)
            return gen
        # Geometrie WIDERSPRICHT KLAR (Flieger fliegt in die falsche Richtung) →
        # diese Route nicht zeigen (kein return: der bezahlte Pfad hat evtl. echte
        # Live-Daten für die richtige Strecke).

    # ── 5. BEZAHLT (LETZTER Ausweg) — nur mit Tages-Budget, nur getippter Flieger
    if _paid_budget_ok():
        adb = _aerodatabox_route(cs, reg=reg, lat=lat, lon=lon, track=track, date=date)
        if adb:
            # AeroDataBox ist reg-gekeyt autoritativ; ein eindeutiges Leg ist
            # bereits 'confirmed'. Nur ein MEHRDEUTIGES ('estimated') Leg muss die
            # Geometrie noch bestätigen, sonst verwerfen (keine Schätzung zeigen).
            if adb.get('confidence') == 'confirmed' or \
                    _geometry_allows_route(adb, lat, lon, track, gs, on_ground):
                adb['confidence'] = 'confirmed'
                _record_resolved_route(cs, adb.get('reg') or reg, adb, date)
                return adb
        try:
            avs = _aviationstack_route(cs)
        except Exception:
            avs = None
        if avs and (avs.get('src') or avs.get('dst')):
            st = str(avs.get('status') or '').lower()
            if st in ('active', 'en-route', 'landed') or \
                    _geometry_allows_route(avs, lat, lon, track, gs, on_ground):
                avs['confidence'] = 'confirmed'
                _record_resolved_route(cs, reg, avs, date)
                return avs
    return None


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

# Wenige bewusste Overrides wo die DB-Municipality fürs Label unschön ist
# ("Frankfurt am Main" → "Frankfurt"; IAD/DCA-Municipality ist "Dulles"/
# "Arlington" statt der Stadt, für die der Airport steht).
_IATA_CITY_OVERRIDES = {
    'FRA': 'Frankfurt',
    'IAD': 'Washington',
    'DCA': 'Washington',
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

    # Route: Cache → adsbdb-Callsign (ICAO-Präfix + Nummer) → zurückschreiben.
    route = _cache_get('ax_route_cache', 'flight', raw)
    if route:
        out['source'] = 'cache'
    elif airline and airline.get('icao') and number:
        callsign = f"{airline['icao']}{number}"
        live = _adsbdb_route(callsign)
        if live:
            route = live; out['source'] = 'adsbdb'
            _cache_put('ax_route_cache',
                       {'flight': raw, 'payload': live, 'updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})

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


@aerox_data_bp.route('/api/ax/callsign/<callsign>', methods=['GET'])
def ax_callsign(callsign):
    """ICAO-Callsign (z.B. DLH506) → GENAUE heutige Route. Das Radar fragt für
    jeden angetippten Flieger hier an. Die FREE-FIRST-Kaskade (_resolve_live_route)
    bevorzugt EIGENE + FREIE Quellen (Warehouse/Tafel + selbst-berechnetes ADS-B →
    OpenSky → adsbdb/adsb.lol/hexdb) und ruft BEZAHLTE APIs (AeroDataBox/
    AviationStack) nur als letzten Ausweg hinter einem Tages-Budget-Guard.
    Jeder Treffer wird datums-/reg-gekeyt in ax_route_cache zurückgeschrieben →
    derselbe Tap ist morgen gratis und die eigene Routen-DB wächst weltweit.

    Optionale Query-Params (schalten höhere Genauigkeit frei, alle abwärts-
    kompatibel — ohne sie funktioniert der Call wie bisher):
      hex=<icao24>  reg=<D-AIZJ>  lat= lon=/lng=  track=<heading°>
      gs=<groundspeed_kt>  on_ground=<0|1>   (schalten die Geometrie-Bestätigung
                                              generischer Kandidaten frei)

    Response (Owner „scraped/eigene Infos sind #1 — zeigen, nicht verstecken"):
      ok, callsign, source, origin{}, destination{}, [gate, terminal, status]
      confidence == 'confirmed' (eigene/gescrapte/bezahlte Daten) oder 'estimated'
      (generischer Kandidat, GEZEIGT). Nur wenn wir GAR keine Strecke haben ODER
      die Live-Geometrie KLAR widerspricht (Flieger fliegt in die falsche Richtung)
      → 404 (keine Route). Sonst wird die Route zurückgegeben.
    """
    from flask import request
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
    on_ground = og in ('1', 'true', 'yes', 'y')

    route = _resolve_live_route(cs, hexid=hexid, reg=reg, lat=lat, lon=lon,
                                track=track, gs=gs, on_ground=on_ground)
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
    return jsonify(out)


@aerox_data_bp.route('/api/ax/harvest-routes', methods=['POST'])
def ax_harvest_routes():
    """Route-Harvester (User-Wunsch: „die restlichen Flugnummern suchen + von wo
    wohin speichern", öffentlich verfügbar). Die App schickt die Callsigns, die sie
    ohnehin vom Radar-Area-Poll hat; wir speichern für jeden NOCH NICHT gecachten
    die Strecke dauerhaft in `ax_route_cache` — Quelle ausschließlich `adsbdb`
    (frei + öffentlich, KEIN AviationStack-Budget). Pro Request hart gedeckelt
    (Rate-Schutz für adsbdb), der Rest kommt beim nächsten Poll dran → die Routen-
    DB wächst über echten Verkehr auf ganz Europa, ohne Bulk-/Budget-Limit."""
    from flask import request
    body = request.get_json(silent=True) or {}
    csigns = body.get('callsigns') or []
    if not isinstance(csigns, list):
        return jsonify({'ok': False, 'error': 'bad_body'}), 400
    MAX_NEW = 12               # höchstens 12 neue adsbdb-Calls pro Request
    harvested = cached = checked = 0
    seen = set()
    for raw in csigns[:300]:
        cs = (str(raw) or '').strip().upper().replace(' ', '')
        if not cs or len(cs) < 4 or cs in seen:
            continue
        seen.add(cs)
        checked += 1
        if _cache_get('ax_route_cache', 'flight', cs):
            cached += 1
            continue
        if harvested >= MAX_NEW:
            continue           # Rest beim nächsten Poll
        route = _adsbdb_route(cs)
        if route and (route.get('src') or route.get('src_icao')) \
           and (route.get('dst') or route.get('dst_icao')):
            _cache_put('ax_route_cache',
                       {'flight': cs, 'payload': route,
                        'updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
            harvested += 1
    return jsonify({'ok': True, 'checked': checked,
                    'cached': cached, 'harvested': harvested})


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
    try:
        sb.table('ax_api_budget').upsert(
            {'month': month, 'n': used + 1,
             'updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}).execute()
    except Exception:
        pass


@aerox_data_bp.route('/api/ax/schedule/<frm>/<to>', methods=['GET'])
def ax_schedule(frm, to):
    """Echte Flugnummern + geplante Zeiten auf einem Städtepaar (AviationStack).
    Architektur: Supabase-Cache FÜR IMMER (Schedules ändern sich kaum) → nur bei
    Cache-Miss UND solange das Monats-Budget reicht ein einziger externer Call,
    Ergebnis wird gecacht. So bleibt AeroX im Free-Tier (100/Monat) und ALLE
    Nutzer ziehen danach aus unserem Backend."""
    a = (frm or '').strip().upper()
    b = (to or '').strip().upper()
    if len(a) < 3 or len(b) < 3:
        return jsonify({'ok': False, 'error': 'need IATA'}), 400
    route = f'{a}-{b}'
    # Cache-Key mit Schema-Version: '#cs3' = Codeshares gefiltert + estimated/actual
    # Zeiten ergänzt. Schema-Bump umgeht alte Cache-Einträge (Duplikate / ohne
    # actual) → erster Abruf zieht frisch + sauber neu.
    cache_key = f'{route}#cs3'
    key = os.environ.get('AVIATIONSTACK_KEY', '')
    month = time.strftime('%Y-%m', time.gmtime())
    remaining, used = _budget_remaining(month)

    cached = _cache_get('ax_schedule_cache', 'route', cache_key)
    if cached is not None:
        # Schedules driften saisonal: nur wenn der Cache SEHR alt ist (>180 Tage)
        # UND noch reichlich Budget frei ist (>=30), einmal neu ziehen. Sonst
        # immer aus dem Cache (0 Budget) — die 90/Monat sind nur für NEUE Routen.
        stale_days = int(os.environ.get('AVIATIONSTACK_REFRESH_DAYS', '180'))
        fetched = cached.get('_fetched', 0)
        age_days = (time.time() - fetched) / 86400.0 if fetched else 0
        if not (key and remaining >= 30 and age_days > stale_days):
            return jsonify({'ok': True, 'route': route, 'source': 'cache', **cached})
        # sonst: durchfallen und einmal auffrischen
    if not key or remaining <= 0:
        # Kein Budget/Key → ehrlich leer (App zeigt dann nur die Airlines-Liste).
        return jsonify({'ok': True, 'route': route, 'source': 'budget-exhausted',
                        'flights': [], 'budget_remaining': remaining})

    # Free-Tier = HTTP (kein HTTPS). dep_iata + arr_iata Filter.
    url = (f'http://api.aviationstack.com/v1/flights?access_key={urllib.parse.quote(key)}'
           f'&dep_iata={a}&arr_iata={b}&limit=100')
    d = _http_json(url, timeout=12)
    rows = (d or {}).get('data') if isinstance(d, dict) else None
    if rows is None:
        return jsonify({'ok': True, 'route': route, 'source': 'error',
                        'flights': [], 'budget_remaining': remaining})
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
    return round(sum(minutes) / len(minutes)) if minutes else None


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
    body = request.get_json(silent=True) or {}
    try:
        m = int(round(float(body.get('minutes'))))
    except Exception:
        return jsonify({'ok': False, 'error': 'bad_minutes'}), 400
    if not (_CREWBUS_MIN <= m <= _CREWBUS_MAX):
        return jsonify({'ok': False, 'error': 'out_of_range',
                        'message': 'Minuten müssen zwischen 1 und 240 liegen.'}), 400

    anon = _crewbus_anon_id()
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
