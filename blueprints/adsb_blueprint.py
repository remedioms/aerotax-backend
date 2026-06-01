# ═══════════════════════════════════════════════════════════════
#  ADS-B Live-Tracking Blueprint
#
#  Self-contained Flask Blueprint für Live-Aircraft-Position-Proxy.
#  Wraps OpenSky-Network /api/states/all mit 60s server-side Cache
#  (dict + threading.Lock — kein Redis, kein DB-Roundtrip).
#
#  Wiring in app.py:
#      from blueprints.adsb_blueprint import adsb_bp
#      app.register_blueprint(adsb_bp)
#
#  Endpunkte:
#      GET /api/adsb/state?hex=<icao24>   → Live-Position oder cache
#      GET /api/adsb/state?reg=<reg>      → Lookup-Reg→Hex → Position
#      GET /api/adsb/route?dep=&arr=      → Great-Circle-Waypoints
#
#  Rate-Limit-Strategie:
#      OpenSky-anon: 400 calls/day. Mit 60s cache + N Clients pro Hex
#      bündeln wir die Calls auf 1/min/Hex → ~1440/day pro getrackter
#      Maschine, also pro AeroTax-User maximal eine Maschine zur Zeit.
#      Bei "viele Crew tracken dieselbe inbound Maschine" hat das Cache
#      sogar coalescing-Effekt.
# ═══════════════════════════════════════════════════════════════

import json
import math
import time
import threading
import urllib.parse
import urllib.request
import urllib.error
from flask import Blueprint, request, jsonify

adsb_bp = Blueprint('adsb', __name__)

# ── Cache ─────────────────────────────────────────────────────
# Struktur: {hex: {"fetched_at": float_unix, "row": list|None}}
# `row` ist die OpenSky-State-Row direkt durchgereicht (kein Re-Encoding).
# `None` bedeutet "wir haben gerade gepollt aber kein Signal" — ist legitim
# (Maschine am Boden ohne ADS-B-Out aktiv). 404 wäre missverständlich.
_CACHE = {}
_CACHE_TTL_SECONDS = 60
_CACHE_LOCK = threading.Lock()

# Rate-Limit-Tracking: wenn OpenSky uns 429't, blocken wir global für die
# vom Retry-After-Header angegebene Dauer (oder 60s default).
_BACKOFF = {"until": 0.0, "lock": threading.Lock()}

# Hardcoded Reg→Hex Map als Last-Line-of-Defense. iOS hat eine eigene
# umfangreichere Tabelle — die hier ist nur dafür gedacht, dass Client-Calls
# mit ?reg=… auch dann funktionieren wenn ein anderer Client das Backend
# direkt nutzt (Web-Frontend, Curl-Debug). Wir halten sie absichtlich kurz.
_BACKEND_REG_HEX = {
    "D-AIPA": "3c64a8", "D-AIPB": "3c64a9", "D-AIPC": "3c64aa",
    "D-AIXA": "3c675c", "D-AIXB": "3c675d",
    "D-AIMA": "3c4dd9",
    "HB-JCA": "4b1903", "HB-JHA": "4b1813",
}

OPENSKY_URL = "https://opensky-network.org/api/states/all"
HTTP_TIMEOUT = 10  # Sekunden — OpenSky kann unter Last langsam sein
USER_AGENT = "AeroTax-Backend/1.0 (ADS-B-Proxy)"


# ─── /api/adsb/state ─────────────────────────────────────────

@adsb_bp.route('/api/adsb/state', methods=['GET'])
def get_adsb_state():
    """
    Liefert die letzte bekannte ADS-B-Position einer Maschine.

    Query:
        hex=<icao24>    — direkter Hex-Lookup (bevorzugt, Client weiß was er will)
        reg=<reg>       — Reg→Hex Mapping (Fallback für Web-Clients)

    Antwort 200:
        {"hex": "<hex>", "position": <openSky-row> | null, "fetched_at": <unix>, "cached": <bool>}

    Antwort 400 wenn weder hex noch reg gegeben.
    Antwort 429 wenn OpenSky uns geratelimited hat — `Retry-After`-Header gesetzt.
    """
    hex_param = (request.args.get('hex') or '').strip().lower()
    reg_param = (request.args.get('reg') or '').strip().upper()

    if not hex_param and not reg_param:
        return jsonify({"error": "missing hex or reg parameter"}), 400

    if not hex_param and reg_param:
        hex_param = _BACKEND_REG_HEX.get(reg_param)
        if not hex_param:
            return jsonify({"error": f"unknown registration {reg_param}"}), 404

    # Backoff check
    now = time.time()
    with _BACKOFF["lock"]:
        backoff_until = _BACKOFF["until"]
    if now < backoff_until:
        retry_after = int(backoff_until - now) + 1
        cached_row = _cache_get(hex_param)
        # Wenn wir noch einen halbwegs frischen Cache haben, geben wir den raus.
        if cached_row is not None:
            return jsonify({
                "hex": hex_param,
                "position": cached_row["row"],
                "fetched_at": cached_row["fetched_at"],
                "cached": True,
                "stale_due_to_backoff": True,
            }), 200
        resp = jsonify({"error": "rate_limited", "retry_after": retry_after})
        resp.headers["Retry-After"] = str(retry_after)
        return resp, 429

    # Cache check
    cached = _cache_get(hex_param)
    if cached is not None:
        return jsonify({
            "hex": hex_param,
            "position": cached["row"],
            "fetched_at": cached["fetched_at"],
            "cached": True,
        }), 200

    # Fetch live
    try:
        row = _fetch_opensky(hex_param)
    except _OpenSkyRateLimit as e:
        with _BACKOFF["lock"]:
            _BACKOFF["until"] = time.time() + e.retry_after
        resp = jsonify({"error": "rate_limited", "retry_after": e.retry_after})
        resp.headers["Retry-After"] = str(e.retry_after)
        return resp, 429
    except _OpenSkyError as e:
        # Bei transient errors geben wir 502 — Client soll exponentiell
        # nochmal probieren, aber wir lassen den Backoff aus (das Problem
        # ist bei OpenSky, nicht bei uns).
        return jsonify({"error": "upstream_error", "detail": str(e)}), 502

    _cache_put(hex_param, row)
    return jsonify({
        "hex": hex_param,
        "position": row,
        "fetched_at": time.time(),
        "cached": False,
    }), 200


# ─── /api/adsb/route ─────────────────────────────────────────

@adsb_bp.route('/api/adsb/route', methods=['GET'])
def get_route_polyline():
    """
    Liefert eine Great-Circle-Waypoint-Liste zwischen zwei IATA-Codes.
    Nutzt eine kleine Built-in Airport-Tabelle. Wenn ein Code unbekannt
    ist und der Caller eigene Koordinaten hat, kann er stattdessen
    ?dep_lat=&dep_lon=&arr_lat=&arr_lon= mitgeben.

    Query (Variante A — IATA-Lookup):
        dep=FRA arr=JFK [points=30]

    Query (Variante B — Coords direkt):
        dep_lat=50.03 dep_lon=8.55 arr_lat=40.64 arr_lon=-73.78 [points=30]

    Antwort 200:
        {"points": [[lat,lon], ...], "distance_nm": <float>}
    """
    try:
        points_n = int(request.args.get('points', '30'))
    except (TypeError, ValueError):
        points_n = 30
    points_n = max(2, min(150, points_n))

    dep_lat = request.args.get('dep_lat')
    dep_lon = request.args.get('dep_lon')
    arr_lat = request.args.get('arr_lat')
    arr_lon = request.args.get('arr_lon')

    if dep_lat and dep_lon and arr_lat and arr_lon:
        try:
            lat1, lon1 = float(dep_lat), float(dep_lon)
            lat2, lon2 = float(arr_lat), float(arr_lon)
        except ValueError:
            return jsonify({"error": "invalid coordinates"}), 400
    else:
        dep = (request.args.get('dep') or '').strip().upper()
        arr = (request.args.get('arr') or '').strip().upper()
        if not dep or not arr:
            return jsonify({"error": "missing dep/arr or coords"}), 400
        dep_coord = _AIRPORTS.get(dep)
        arr_coord = _AIRPORTS.get(arr)
        if not dep_coord or not arr_coord:
            return jsonify({
                "error": "unknown_iata",
                "detail": f"need built-in mapping for {dep} or {arr} — "
                          "pass dep_lat/dep_lon/arr_lat/arr_lon instead"
            }), 404
        lat1, lon1 = dep_coord
        lat2, lon2 = arr_coord

    points = _great_circle_points(lat1, lon1, lat2, lon2, points_n)
    distance_nm = _great_circle_nm(lat1, lon1, lat2, lon2)
    return jsonify({
        "points": points,
        "distance_nm": round(distance_nm, 1),
    }), 200


# ─── /api/adsb/health (debug) ─────────────────────────────────

@adsb_bp.route('/api/adsb/health', methods=['GET'])
def get_health():
    """Sanity-check Endpoint — zeigt Cache-Belegung und Backoff-Status."""
    now = time.time()
    with _CACHE_LOCK:
        cache_size = len(_CACHE)
        fresh = sum(1 for v in _CACHE.values() if now - v["fetched_at"] < _CACHE_TTL_SECONDS)
    with _BACKOFF["lock"]:
        backoff_until = _BACKOFF["until"]
    return jsonify({
        "ok": True,
        "cache_entries": cache_size,
        "cache_fresh": fresh,
        "cache_ttl_seconds": _CACHE_TTL_SECONDS,
        "backoff_active": now < backoff_until,
        "backoff_remaining": max(0, int(backoff_until - now)),
    }), 200


# ─── Cache Helpers ──────────────────────────────────────────

def _cache_get(hex_id):
    """Gibt eine frische Cache-Row zurück oder None."""
    now = time.time()
    with _CACHE_LOCK:
        entry = _CACHE.get(hex_id)
        if entry is None:
            return None
        if now - entry["fetched_at"] > _CACHE_TTL_SECONDS:
            return None
        return entry


def _cache_put(hex_id, row):
    with _CACHE_LOCK:
        _CACHE[hex_id] = {"fetched_at": time.time(), "row": row}
        # Cache-Cap: halte max 200 Einträge. Bei Überlauf evicte die
        # ältesten 50 — kein LRU-Overhead, einfach Bulk-Cleanup.
        if len(_CACHE) > 200:
            items = sorted(_CACHE.items(), key=lambda kv: kv[1]["fetched_at"])
            for k, _ in items[:50]:
                _CACHE.pop(k, None)


# ─── OpenSky Fetch ──────────────────────────────────────────

class _OpenSkyError(Exception):
    pass


class _OpenSkyRateLimit(Exception):
    def __init__(self, retry_after):
        super().__init__("rate limited")
        self.retry_after = int(retry_after)


def _fetch_opensky(hex_id):
    """
    Holt eine einzelne State-Row von OpenSky.

    Returns:
        list (die OpenSky-Row) oder None wenn kein Live-Signal vorhanden.

    Raises:
        _OpenSkyRateLimit bei 429
        _OpenSkyError bei anderen Fehlern
    """
    qs = urllib.parse.urlencode({"icao24": hex_id})
    url = f"{OPENSKY_URL}?{qs}"
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 429:
            retry_after = e.headers.get("Retry-After", "60")
            try:
                retry_after = int(retry_after)
            except (TypeError, ValueError):
                retry_after = 60
            raise _OpenSkyRateLimit(retry_after) from e
        raise _OpenSkyError(f"http {e.code}") from e
    except urllib.error.URLError as e:
        raise _OpenSkyError(f"network: {e.reason}") from e

    try:
        obj = json.loads(data)
    except (ValueError, json.JSONDecodeError) as e:
        raise _OpenSkyError("invalid json") from e

    states = obj.get("states") or []
    if not states:
        return None
    return states[0]


# ─── Great-Circle Math ──────────────────────────────────────

_EARTH_RADIUS_NM = 3440.065


def _great_circle_nm(lat1, lon1, lat2, lon2):
    """Haversine, output in nautical miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return _EARTH_RADIUS_NM * c


def _great_circle_points(lat1, lon1, lat2, lon2, count):
    """
    Sphärische lineare Interpolation (slerp) zwischen zwei Geo-Koordinaten.
    Liefert eine Liste [[lat,lon], ...] mit `count` Punkten inklusive
    Start und Ende. Mathematik identisch zur iOS-Side (ETAComputer.swift)
    damit Backend und Client dieselbe Polyline rendern.
    """
    if count < 2:
        return [[lat1, lon1], [lat2, lon2]]

    r_lat1, r_lon1 = math.radians(lat1), math.radians(lon1)
    r_lat2, r_lon2 = math.radians(lat2), math.radians(lon2)

    d = 2 * math.asin(math.sqrt(
        math.sin((r_lat1 - r_lat2) / 2) ** 2 +
        math.cos(r_lat1) * math.cos(r_lat2) *
        math.sin((r_lon1 - r_lon2) / 2) ** 2
    ))

    if d < 1e-9:
        return [[lat1, lon1] for _ in range(count)]

    out = []
    for i in range(count):
        f = i / (count - 1)
        a = math.sin((1 - f) * d) / math.sin(d)
        b = math.sin(f * d) / math.sin(d)
        x = a * math.cos(r_lat1) * math.cos(r_lon1) + b * math.cos(r_lat2) * math.cos(r_lon2)
        y = a * math.cos(r_lat1) * math.sin(r_lon1) + b * math.cos(r_lat2) * math.sin(r_lon2)
        z = a * math.sin(r_lat1) + b * math.sin(r_lat2)
        lat = math.atan2(z, math.sqrt(x * x + y * y))
        lon = math.atan2(y, x)
        out.append([round(math.degrees(lat), 6), round(math.degrees(lon), 6)])
    return out


# ─── Mini Airport-DB ──────────────────────────────────────────
#
# Reicht für die häufigsten LH-Drehkreuze + ausgewählte Long-Hauls.
# Wenn ein Caller einen unbekannten Code mitgibt, geben wir 404 — der
# Client soll dann die Variante-B-Route mit dep_lat/dep_lon/... nutzen.
# Format: IATA → (lat, lon)
_AIRPORTS = {
    "FRA": (50.0379, 8.5622),
    "MUC": (48.3538, 11.7861),
    "DUS": (51.2895, 6.7668),
    "TXL": (52.5597, 13.2877),
    "BER": (52.3667, 13.5033),
    "HAM": (53.6304, 9.9882),
    "STR": (48.6899, 9.2220),
    "CGN": (50.8659, 7.1427),
    "ZRH": (47.4647, 8.5492),
    "VIE": (48.1103, 16.5697),
    "BRU": (50.9014, 4.4844),
    "LHR": (51.4700, -0.4543),
    "CDG": (49.0097, 2.5479),
    "AMS": (52.3086, 4.7639),
    "MAD": (40.4983, -3.5676),
    "FCO": (41.8003, 12.2389),
    "IST": (41.2753, 28.7519),
    "JFK": (40.6413, -73.7781),
    "EWR": (40.6925, -74.1687),
    "ORD": (41.9742, -87.9073),
    "LAX": (33.9416, -118.4085),
    "SFO": (37.6213, -122.3790),
    "MIA": (25.7959, -80.2870),
    "YYZ": (43.6777, -79.6248),
    "GRU": (-23.4356, -46.4731),
    "EZE": (-34.8222, -58.5358),
    "DXB": (25.2532, 55.3657),
    "DOH": (25.2611, 51.5650),
    "SIN": (1.3644, 103.9915),
    "HKG": (22.3080, 113.9185),
    "PEK": (40.0799, 116.6031),
    "NRT": (35.7720, 140.3929),
    "ICN": (37.4602, 126.4407),
    "SYD": (-33.9399, 151.1753),
    "JNB": (-26.1392, 28.2460),
    "BOM": (19.0896, 72.8656),
    "DEL": (28.5562, 77.1000),
}
