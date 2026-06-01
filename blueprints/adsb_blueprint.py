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
#  Fallback-Chain (2026-06-01):
#      OpenSky-anon ist unzuverlässig (400/day-Limit, häufige 5xx unter Last).
#      Statt direkt 502 zu liefern, kaskadieren wir:
#        1) OpenSky `/api/states/all?icao24=<hex>` (3s timeout)
#        2) adsb.lol `/v2/icao/<hex>` (5s timeout, kein Auth, gentleman's API)
#        3) last-known-state aus In-Memory-Cache (TTL 30 min)
#      Nur wenn alle 3 scheitern → 502 mit detaillierter `tried`-Liste.
#
#  Rate-Limit-Strategie:
#      Mit 60s Fresh-Cache + N Clients pro Hex bündeln wir die Calls auf
#      1/min/Hex. OpenSky-Quota wird durch adsb.lol-Fallback nicht weiter
#      belastet wenn OpenSky bereits 4xx/5xx geliefert hat.
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
# Struktur: {hex: {"fetched_at": float_unix, "row": list|None, "source": str}}
# `row` ist die normalisierte State-Row (OpenSky-Layout-kompatibel) — egal
# welche Upstream-Quelle sie geliefert hat. `None` bedeutet "Hex ist
# bekannt aber gerade kein Live-Signal" (Maschine am Boden ohne ADS-B-Out).
# `source` ist informativ: 'opensky' | 'adsb.lol' — landet im JSON-Response
# damit Clients debuggen können wer gerade liefert.
_CACHE = {}
_CACHE_TTL_SECONDS = 60
# Last-Known-State Cache hält längere TTL für Fallback-Use-Case: wenn alle
# Upstreams down sind, geben wir den letzten erfolgreichen Ping zurück,
# markiert als `stale_due_to_upstream_outage`. 30 min ist ein vernünftiges
# Time-Window — länger wäre irreführend (Maschine könnte längst gelandet
# sein), kürzer würde Cold-Start-Recoverys nicht überbrücken.
_LAST_KNOWN_TTL_SECONDS = 1800
_CACHE_LOCK = threading.Lock()

# Rate-Limit-Tracking: wenn OpenSky uns 429't, blocken wir global für die
# vom Retry-After-Header angegebene Dauer (oder 60s default). adsb.lol hat
# kein hard rate-limit dokumentiert; wir tracken nur OpenSky.
_BACKOFF = {"until": 0.0, "lock": threading.Lock()}

# Hardcoded Reg→Hex Map als Last-Line-of-Defense. iOS hat eine eigene
# umfangreichere Tabelle — die hier ist nur dafür gedacht, dass Client-Calls
# mit ?reg=… auch dann funktionieren wenn ein anderer Client das Backend
# direkt nutzt (Web-Frontend, Curl-Debug). Stand 2026-05.
# Quelle: planespotters.net + jetphotos.com Cross-Check.
_BACKEND_REG_HEX = {
    # Lufthansa A320-Family
    "D-AIPA": "3c64a8", "D-AIPB": "3c64a9", "D-AIPC": "3c64aa",
    "D-AIPD": "3c64ab", "D-AIPE": "3c64ac", "D-AIPF": "3c64ad",
    "D-AIPH": "3c64af", "D-AIPK": "3c64b1", "D-AIPL": "3c64b2",
    "D-AIQA": "3c656e", "D-AIQB": "3c656f", "D-AIQC": "3c6570",
    "D-AIQD": "3c6571", "D-AIQE": "3c6572", "D-AIQF": "3c6573",
    "D-AIUA": "3c66c1", "D-AIUB": "3c66c2", "D-AIUC": "3c66c3",
    "D-AIUD": "3c66c4", "D-AIUE": "3c66c5",
    # Lufthansa A330/A340
    "D-AIKA": "3c4dc8", "D-AIKB": "3c4dc9", "D-AIKC": "3c4dca",
    "D-AIKD": "3c4dcb", "D-AIKE": "3c4dcc",
    "D-AIHA": "3c4dad", "D-AIHB": "3c4dae", "D-AIHC": "3c4daf",
    # Lufthansa A350-900
    "D-AIXA": "3c675c", "D-AIXB": "3c675d", "D-AIXC": "3c675e",
    "D-AIXD": "3c675f", "D-AIXE": "3c6760", "D-AIXF": "3c6761",
    # Lufthansa A380
    "D-AIMA": "3c4dd9", "D-AIMB": "3c4dda", "D-AIMC": "3c4ddb",
    "D-AIMD": "3c4ddc", "D-AIME": "3c4ddd",
    # Lufthansa 747-8
    "D-ABYA": "3c4a85", "D-ABYB": "3c4a86", "D-ABYC": "3c4a87",
    "D-ABYD": "3c4a88", "D-ABYE": "3c4a89",
    # Eurowings A320
    "D-AEWA": "3c4d4f", "D-AEWB": "3c4d50", "D-AEWC": "3c4d51",
    "D-AIZA": "3c674a", "D-AIZB": "3c674b",
    # SWISS A220 + A330
    "HB-JCA": "4b1903", "HB-JCB": "4b1904", "HB-JCC": "4b1905",
    "HB-JHA": "4b1813", "HB-JHB": "4b1814",
    # Austrian A320
    "OE-LBA": "440189", "OE-LBB": "44018a", "OE-LBC": "44018b",
    # Brussels A320
    "OO-SNA": "4485c1", "OO-SNB": "4485c2",
}


def resolve_reg_to_hex(reg):
    """Public helper für andere Blueprints (z.B. aircraft_info_blueprint).
    Liefert lowercase Hex oder None. Reg wird upper-cased."""
    if not reg:
        return None
    return _BACKEND_REG_HEX.get(reg.strip().upper())

OPENSKY_URL = "https://opensky-network.org/api/states/all"
ADSB_LOL_URL = "https://api.adsb.lol/v2/icao"
# Per-Upstream Timeouts (kürzer als vorher 10s — User wartet sonst zu lange
# wenn OpenSky hängt):
OPENSKY_TIMEOUT = 3
ADSB_LOL_TIMEOUT = 5
USER_AGENT = "AeroTax-Backend/1.1 (ADS-B-Proxy; mailto:ops@aerotax.de)"


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

    # Wenn Client beides mitschickt (reg+hex) bevorzugen wir den expliziten
    # Hex — sonst Server-Lookup über Backend-Reg-Map.
    if not hex_param and reg_param:
        hex_param = _BACKEND_REG_HEX.get(reg_param)
        if not hex_param:
            return jsonify({
                "error": f"unknown registration {reg_param}",
                "hint": "pass ?hex=<icao24> if client knows the mapping",
            }), 404

    # Fresh-Cache-Hit (60s TTL) — sofort raus.
    cached = _cache_get(hex_param)
    if cached is not None:
        return jsonify({
            "hex": hex_param,
            "position": cached["row"],
            "fetched_at": cached["fetched_at"],
            "cached": True,
            "source": cached.get("source", "cache"),
        }), 200

    # Backoff-Status für OpenSky tracken — wenn aktiv, OpenSky-Step
    # überspringen aber adsb.lol weiter probieren.
    now = time.time()
    with _BACKOFF["lock"]:
        backoff_until = _BACKOFF["until"]
    opensky_skipped = now < backoff_until

    tried = []
    row = None
    source = None

    # ─── Step 1: OpenSky (außer wenn im Backoff) ───
    if not opensky_skipped:
        try:
            row = _fetch_opensky(hex_param)
            source = "opensky"
            tried.append({"upstream": "opensky", "ok": True})
        except _OpenSkyRateLimit as e:
            # 429 → globaler Backoff setzen, dann adsb.lol versuchen.
            with _BACKOFF["lock"]:
                _BACKOFF["until"] = time.time() + e.retry_after
            tried.append({"upstream": "opensky", "ok": False,
                          "reason": f"rate_limited(retry={e.retry_after}s)"})
        except _OpenSkyError as e:
            tried.append({"upstream": "opensky", "ok": False,
                          "reason": str(e)[:80]})
    else:
        tried.append({"upstream": "opensky", "ok": False,
                      "reason": f"backoff_active({int(backoff_until - now)}s)"})

    # ─── Step 2: adsb.lol (wenn OpenSky nichts brauchbares lieferte) ───
    # `row is None` heißt entweder Upstream-Fehler oder "kein Signal".
    # Wir unterscheiden: bei Upstream-Fehler tried[-1].ok == False, dann
    # macht adsb.lol Sinn. Bei "kein Signal" (ok=True, row=None) NICHT
    # nochmal probieren — der Client soll "Maschine ist gerade nicht in
    # der Luft" sehen, nicht eine zweite leere Antwort von einer anderen
    # Quelle.
    if row is None and tried and not tried[-1].get("ok"):
        try:
            row = _fetch_adsb_lol(hex_param)
            if row is not None:
                source = "adsb.lol"
                tried.append({"upstream": "adsb.lol", "ok": True})
            else:
                tried.append({"upstream": "adsb.lol", "ok": True,
                              "reason": "no_signal"})
        except _UpstreamError as e:
            tried.append({"upstream": "adsb.lol", "ok": False,
                          "reason": str(e)[:80]})

    # ─── Erfolg: cachen + ausgeben ───
    if source is not None:
        _cache_put(hex_param, row, source)
        return jsonify({
            "hex": hex_param,
            "position": row,
            "fetched_at": time.time(),
            "cached": False,
            "source": source,
            "tried": tried,
        }), 200

    # ─── Step 3: Last-known-state aus 30-min-Cache ───
    last_known = _last_known_get(hex_param)
    if last_known is not None:
        return jsonify({
            "hex": hex_param,
            "position": last_known["row"],
            "fetched_at": last_known["fetched_at"],
            "cached": True,
            "stale_due_to_upstream_outage": True,
            "stale_age_seconds": int(time.time() - last_known["fetched_at"]),
            "source": last_known.get("source", "cache"),
            "tried": tried,
        }), 200

    # ─── Alles fehlgeschlagen → 502 mit detaillierter Diagnose ───
    return jsonify({
        "error": "all_upstreams_failed",
        "hex": hex_param,
        "tried": tried,
    }), 502


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
    """Gibt eine frische Cache-Row (innerhalb _CACHE_TTL_SECONDS) zurück."""
    now = time.time()
    with _CACHE_LOCK:
        entry = _CACHE.get(hex_id)
        if entry is None:
            return None
        if now - entry["fetched_at"] > _CACHE_TTL_SECONDS:
            return None
        return entry


def _last_known_get(hex_id):
    """Gibt einen Eintrag innerhalb _LAST_KNOWN_TTL_SECONDS zurück, auch
    wenn er älter als 60s ist. Wird nur als Last-Resort genutzt wenn alle
    Upstreams scheitern. Liefert nicht zurück wenn row=None (es macht
    keinen Sinn, "kein Signal" als stale-fallback zu reportieren)."""
    now = time.time()
    with _CACHE_LOCK:
        entry = _CACHE.get(hex_id)
        if entry is None:
            return None
        if entry.get("row") is None:
            return None
        if now - entry["fetched_at"] > _LAST_KNOWN_TTL_SECONDS:
            return None
        return entry


def _cache_put(hex_id, row, source="opensky"):
    with _CACHE_LOCK:
        _CACHE[hex_id] = {
            "fetched_at": time.time(),
            "row": row,
            "source": source,
        }
        # Cache-Cap: halte max 200 Einträge. Bei Überlauf evicte die
        # ältesten 50 — kein LRU-Overhead, einfach Bulk-Cleanup.
        if len(_CACHE) > 200:
            items = sorted(_CACHE.items(), key=lambda kv: kv[1]["fetched_at"])
            for k, _ in items[:50]:
                _CACHE.pop(k, None)


# ─── OpenSky Fetch ──────────────────────────────────────────

class _UpstreamError(Exception):
    """Base für alle Upstream-Fetch-Fehler — egal welche Quelle."""
    pass


class _OpenSkyError(_UpstreamError):
    pass


class _OpenSkyRateLimit(_OpenSkyError):
    def __init__(self, retry_after):
        super().__init__("rate limited")
        self.retry_after = int(retry_after)


class _AdsbLolError(_UpstreamError):
    pass


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
        with urllib.request.urlopen(req, timeout=OPENSKY_TIMEOUT) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 429:
            retry_after = e.headers.get("Retry-After", "60")
            try:
                retry_after = int(retry_after)
            except (TypeError, ValueError):
                retry_after = 60
            raise _OpenSkyRateLimit(retry_after) from e
        raise _OpenSkyError(f"opensky http {e.code}") from e
    except urllib.error.URLError as e:
        raise _OpenSkyError(f"opensky network: {e.reason}") from e
    except Exception as e:  # socket timeout etc.
        raise _OpenSkyError(f"opensky transport: {type(e).__name__}") from e

    try:
        obj = json.loads(data)
    except (ValueError, json.JSONDecodeError) as e:
        raise _OpenSkyError("opensky invalid json") from e

    states = obj.get("states") or []
    if not states:
        return None
    return states[0]


def _fetch_adsb_lol(hex_id):
    """
    Fallback-Upstream: adsb.lol `/v2/icao/<hex24>`.

    adsb.lol antwortet `{"ac": [{ ...aircraft-fields... }], "msg": "...", ...}`.
    Wir normalisieren das in das OpenSky-State-Row-Layout (siehe
    AircraftPosition.from(openSkyRow:) in iOS) damit Clients KEINE
    Quellen-bedingte Parser-Variante brauchen.

    Field-Mapping (adsb.lol → OpenSky-Index):
        hex          → [0] icao24
        flight       → [1] callsign
        r (registration) → benutzt für [2] origin_country (best-effort)
        seen_pos     → [3] time_position (negativer Offset → unix)
        seen         → [4] last_contact
        lon          → [5]
        lat          → [6]
        alt_baro     → [7] baro_altitude (ft → m)
        alt_geom     → [13] geo_altitude (ft → m)
        gs           → [9] velocity (kts → m/s)
        track        → [10] true_track
        baro_rate    → [11] vertical_rate (fpm → m/s)
        squawk       → [14]
        ground       → [8] on_ground (alt_baro == "ground")

    Returns: list im OpenSky-Layout oder None wenn `ac` leer.
    Raises: _AdsbLolError bei HTTP-/Parse-/Timeout-Fehler.
    """
    safe_hex = urllib.parse.quote(hex_id, safe='')
    url = f"{ADSB_LOL_URL}/{safe_hex}"
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=ADSB_LOL_TIMEOUT) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        raise _AdsbLolError(f"adsb.lol http {e.code}") from e
    except urllib.error.URLError as e:
        raise _AdsbLolError(f"adsb.lol network: {e.reason}") from e
    except Exception as e:
        raise _AdsbLolError(f"adsb.lol transport: {type(e).__name__}") from e

    try:
        obj = json.loads(data)
    except (ValueError, json.JSONDecodeError) as e:
        raise _AdsbLolError("adsb.lol invalid json") from e

    ac_list = obj.get("ac") or []
    if not ac_list:
        return None
    ac = ac_list[0]

    # Numeric helpers — adsb.lol sendet "ground" als String wenn am Boden,
    # sonst float. Wir parsen defensiv.
    def _f(v):
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    alt_baro_raw = ac.get("alt_baro")
    on_ground = alt_baro_raw == "ground"
    alt_baro_ft = _f(alt_baro_raw) if not on_ground else None
    alt_geom_ft = _f(ac.get("alt_geom"))
    gs_kts = _f(ac.get("gs"))
    baro_rate_fpm = _f(ac.get("baro_rate"))

    # ft → m für altitude (1 ft = 0.3048 m)
    alt_baro_m = alt_baro_ft * 0.3048 if alt_baro_ft is not None else None
    alt_geom_m = alt_geom_ft * 0.3048 if alt_geom_ft is not None else None
    # kts → m/s (1 kts = 0.514444 m/s)
    velocity_ms = gs_kts * 0.514444 if gs_kts is not None else None
    # fpm → m/s (1 fpm = 0.00508 m/s)
    vertical_rate_ms = baro_rate_fpm * 0.00508 if baro_rate_fpm is not None else None

    now = time.time()
    seen_age = _f(ac.get("seen"))
    last_contact = (now - seen_age) if seen_age is not None else now
    seen_pos_age = _f(ac.get("seen_pos"))
    time_position = (now - seen_pos_age) if seen_pos_age is not None else last_contact

    flight = (ac.get("flight") or "").strip() or None
    reg = (ac.get("r") or "").strip() or None  # adsb.lol's "r" = registration

    # OpenSky-State-Row Layout (siehe AircraftPosition.from):
    # [0] icao24, [1] callsign, [2] origin_country, [3] time_position,
    # [4] last_contact, [5] lon, [6] lat, [7] baro_altitude_m, [8] on_ground,
    # [9] velocity_m_s, [10] true_track, [11] vertical_rate_m_s, [12] sensors,
    # [13] geo_altitude_m, [14] squawk, [15] spi, [16] position_source
    row = [
        (ac.get("hex") or hex_id).lower(),    # 0
        flight,                                # 1
        reg,                                   # 2  (Reg statt origin_country — best-effort)
        time_position,                         # 3
        last_contact,                          # 4
        _f(ac.get("lon")),                     # 5
        _f(ac.get("lat")),                     # 6
        alt_baro_m,                            # 7
        on_ground,                             # 8
        velocity_ms,                           # 9
        _f(ac.get("track")),                   # 10
        vertical_rate_ms,                      # 11
        None,                                  # 12 sensors
        alt_geom_m,                            # 13
        (ac.get("squawk") or None),            # 14
        False,                                 # 15 spi
        0,                                     # 16 position_source
    ]
    # adsb.lol sendet manchmal Records ohne lat/lon (Mode-S only, kein ADS-B).
    # Wir geben dann None zurück — kein "Position" verfügbar.
    if row[5] is None or row[6] is None:
        return None
    return row


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
