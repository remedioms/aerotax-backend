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
import json
import os
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
    month = time.strftime('%Y-%m', time.gmtime())
    remaining, used = _budget_remaining(month)
    floor = int(os.environ.get('AVIATIONSTACK_ROUTE_FLOOR', '25'))
    if remaining <= floor:          # Schedules haben Vorrang → nur aus dem Überschuss
        return None
    url = (f'http://api.aviationstack.com/v1/flights?access_key={urllib.parse.quote(key)}'
           f'&flight_icao={urllib.parse.quote(callsign)}&limit=1')
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
        'self_growing': 'adsbdb/hexdb hits cached to Supabase (ax_*_cache)',
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


@aerox_data_bp.route('/api/ax/callsign/<callsign>', methods=['GET'])
def ax_callsign(callsign):
    """ICAO-Callsign (z.B. DLH506) → Route. Das Radar fragt für jeden
    angetippten Flieger hier an → Treffer werden in ax_route_cache zurück-
    geschrieben, d.h. die Routen-DB wächst aus dem realen Verkehr, den die
    Crew sieht (cache → adsbdb → cache, höchstens ein externer Call je Callsign)."""
    cs = (callsign or '').strip().upper().replace(' ', '')
    if not cs:
        return jsonify({'ok': False, 'error': 'empty'}), 400
    out = {'ok': True, 'callsign': cs, 'source': 'cache'}
    route = _cache_get('ax_route_cache', 'flight', cs)
    if route:
        out['source'] = route.get('source', 'cache')
    else:
        # AUTORITATIV zuerst (AviationStack, budget-gated), sonst statische adsbdb-DB.
        route = _aviationstack_route(cs) or _adsbdb_route(cs)
        if route:
            out['source'] = route.get('source', 'adsbdb')
            out['status'] = route.get('status')
            _cache_put('ax_route_cache',
                       {'flight': cs, 'payload': route,
                        'updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
    if not route:
        return jsonify({'ok': False, 'callsign': cs}), 404

    def enrich(code):
        ap = _airport_row(code)
        if not ap:
            return {'iata': code}
        return {'iata': ap.get('iata'), 'icao': ap.get('icao'),
                'name': ap.get('name'), 'city': ap.get('city'),
                'country': ap.get('country'), 'lat': ap.get('lat'), 'lon': ap.get('lon')}
    out['origin'] = enrich(route.get('src') or route.get('src_icao'))
    out['destination'] = enrich(route.get('dst') or route.get('dst_icao'))
    return jsonify(out)


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
# Storage: In-Memory (sofort nutzbar) + best-effort Supabase (`ax_crewbus`,
# key=iata, payload={minutes:[...]}). Ohne Tabelle no-op't der SB-Write (durable
# sobald die Tabelle angelegt ist) — die App funktioniert trotzdem.
_CREWBUS_MEM = {}
_CREWBUS_CAP = 50          # je IATA die letzten 50 Eingaben mitteln (Drift-Schutz)


def _crewbus_get(iata):
    cached = _cache_get('ax_crewbus', 'iata', iata)
    if isinstance(cached, dict) and isinstance(cached.get('minutes'), list):
        return [int(x) for x in cached['minutes'] if isinstance(x, (int, float))]
    return list(_CREWBUS_MEM.get(iata) or [])


def _crewbus_put(iata, minutes):
    minutes = minutes[-_CREWBUS_CAP:]
    _CREWBUS_MEM[iata] = minutes
    _cache_put('ax_crewbus', {'iata': iata, 'payload': {'minutes': minutes}})


def _crewbus_avg(minutes):
    return round(sum(minutes) / len(minutes)) if minutes else None


@aerox_data_bp.route('/api/ax/crewbus/<iata>', methods=['GET'])
def ax_crewbus_get(iata):
    iata = (iata or '').upper().strip()[:4]
    if not iata:
        return jsonify({'ok': False, 'error': 'bad_iata'}), 400
    mins = _crewbus_get(iata)
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
    if not (1 <= m <= 240):
        return jsonify({'ok': False, 'error': 'out_of_range',
                        'message': 'Minuten müssen zwischen 1 und 240 liegen.'}), 400
    mins = _crewbus_get(iata)
    mins.append(m)
    _crewbus_put(iata, mins)
    mins = _crewbus_get(iata)
    return jsonify({'ok': True, 'iata': iata,
                    'avg': _crewbus_avg(mins), 'count': len(mins), 'your_minutes': m})
