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

import base64
import concurrent.futures
import hmac
import json
import math
import os
import time
import logging
import threading
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from flask import Blueprint, request, jsonify, current_app

adsb_bp = Blueprint('adsb', __name__)

# ── Supabase-Anbindung (lazy-resolve wie aircraft_health_blueprint) ──
# Persistiert die letzte bekannte Aircraft-Position pro Registration als
# Fallback, wenn Live-ADS-B (OpenSky/adsb.lol) gerade nichts liefert.
try:
    from app import sb as _sb, SB_AVAILABLE as _SB_AVAILABLE
except ImportError:
    _sb = None
    _SB_AVAILABLE = False

# Fallback-Position gilt 24h als brauchbar — danach ist sie zu alt um sie
# der Crew als "letzte bekannte Position" zu zeigen.
_FALLBACK_TTL_SECONDS = 24 * 3600

# Persistenz-Diagnose (vom /api/health/full via get_persist_stats() exponiert).
# Ein still scheiternder Upsert war die Wurzel des "Flieger verschwinden aus dem
# Cache"-Bugs — die Counter machen das sichtbar, ohne Cloud-Run-Logs zu graben.
_PERSIST_STATS = {
    'persist_ok_count':   0,
    'persist_fail_count': 0,
    'backfill_ok_count':  0,
    'backfill_miss_count': 0,
    'last_error':         None,
}


def get_persist_stats():
    """Snapshot der Persistenz-Counter für den Health-Endpoint (app.py)."""
    _sb, ok = _sb_client()
    snap = dict(_PERSIST_STATS)
    snap['available'] = bool(ok)
    return snap


def _rate_limited(*, ip=None, token=None, endpoint='adsb', limit=60, window_sec=60):
    """Best-effort Rate-Limit über die app.py-Helper (_ip_rate_limited /
    _token_rate_limited). Lazy-Import wie _sb_client, damit init-Order egal ist.
    Liefert True wenn das Limit erreicht ist. Wenn die Helper (noch) nicht
    auflösbar sind, NIE blocken (False) — der Live-/Anon-Pfad bleibt unberührt."""
    try:
        from app import _ip_rate_limited as _ipl, _token_rate_limited as _tkl
    except ImportError:
        return False
    try:
        if token and _tkl(token, endpoint, limit, window_sec):
            return True
        if ip and _ipl(ip, endpoint=endpoint, limit=limit, window_sec=window_sec):
            return True
    except Exception:
        return False
    return False


def _req_ip(req):
    """Client-IP hinter Cloudflare. CF-Connecting-IP ist der vom Proxy gesetzte,
    vertrauenswürdige Header. In X-Forwarded-For hängt der Proxy die echte IP
    hinten AN — das ERSTE Element ist Client-kontrolliert (Spoof-Prefix würde
    das Rate-Limit umgehen) → LETZTES Element nehmen."""
    cf = (req.headers.get('CF-Connecting-IP', '') or '').strip()
    if cf:
        return cf
    xff = req.headers.get('X-Forwarded-For', '')
    if xff:
        return xff.split(',')[-1].strip()
    return req.remote_addr or ''


def _sb_client():
    """Lazy re-resolve, damit init-Order zwischen app.py und Blueprint egal ist."""
    global _sb, _SB_AVAILABLE
    if _sb is not None and _SB_AVAILABLE:
        return _sb, True
    try:
        from app import sb as live_sb, SB_AVAILABLE as live_av
        _sb = live_sb
        _SB_AVAILABLE = bool(live_av)
        return _sb, _SB_AVAILABLE
    except ImportError:
        return None, False


def _normalize_registration(raw):
    """Reg-Normalisierung: uppercase, strip. None bei leer/zu kurz."""
    if not raw or not isinstance(raw, str):
        return None
    reg = raw.strip().upper()
    if len(reg) < 2 or len(reg) > 12:
        return None
    return reg


def _coerce_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _persist_position_row(row):
    """Upsert einer aircraft_positions-Row (keyed on registration).
    Returns True bei Erfolg, False bei SB-down/Fehler.

    Fehler werden LAUT (logger.error) geloggt + in _PERSIST_STATS gezählt, damit
    ein dauerhaft scheiternder Upsert (Schema-Mismatch, RLS, SB down) im
    /api/health/full sichtbar wird statt still den Cache-Inhalt zu verlieren."""
    sb, ok = _sb_client()
    if not ok:
        return False
    try:
        sb.table('aircraft_positions').upsert(
            row, on_conflict='registration').execute()
        _PERSIST_STATS['persist_ok_count'] += 1
        return True
    except Exception as e:
        _PERSIST_STATS['persist_fail_count'] += 1
        _PERSIST_STATS['last_error'] = f'persist {type(e).__name__}: {str(e)[:140]}'
        try:
            current_app.logger.error(
                f'[adsb] persist_position_FAIL reg={row.get("registration", "?")} '
                f'err={type(e).__name__}: {str(e)[:160]}'
            )
        except Exception:
            pass
        return False


def _warm_persist_from_opensky_row(hex_id, opensky_row, source):
    """Best-effort: schreibt eine frisch gefetchte Live-Position in
    aircraft_positions, damit die Tabelle auch ohne iOS-POST warm bleibt.
    Wird im Live-Fetch-Pfad aufgerufen und darf NIE die Live-Response brechen
    (alles in try/except).

    OpenSky-State-Row-Layout (siehe _fetch_adsb_lol):
      [0] icao24, [1] callsign, [5] lon, [6] lat, [7] baro_altitude_m,
      [8] on_ground, [9] velocity_m_s, [10] true_track, [14] squawk,
      [3] time_position (unix).
    """
    try:
        if not opensky_row or not isinstance(opensky_row, (list, tuple)):
            return
        lat = _coerce_float(opensky_row[6]) if len(opensky_row) > 6 else None
        lon = _coerce_float(opensky_row[5]) if len(opensky_row) > 5 else None
        if lat is None or lon is None:
            return  # keine Position → nicht persistieren
        # Reg-Auflösung: NUR adsb.lol und Tier-3 AeroDataBox ('adb') legen die
        # Registration in [2]. OpenSky-State-Vektoren haben dort origin_country
        # (Ländername, z.B. "Germany") — der ist KEINE Registration und darf
        # NICHT als PK landen. Wir trauen [2] daher ausschließlich bei diesen
        # Quellen; bei OpenSky (inkl. 'opensky-poll') lösen wir die Reg
        # ausschließlich über die Hex-Map auf.
        reg = None
        src = (source or '').lower()
        if (('adsb.lol' in src or src == 'adb' or src == 'fr24')
                and len(opensky_row) > 2 and isinstance(opensky_row[2], str)):
            cand = opensky_row[2].strip().upper()
            # Heuristik: Reg enthält keine Leerzeichen und ist <=12 Zeichen.
            if cand and ' ' not in cand and 2 <= len(cand) <= 12:
                reg = cand
        if not reg:
            reg = _hex_to_reg(hex_id)
        if not reg:
            # #24: Hex außerhalb der winzigen hardcoded Map → inverse SB-Lookup
            # in tail_hex, sonst würde die Position NIE warm-persistiert und der
            # Cold-Start-Backfill liefe leer. Guarded → degradiert sauber.
            reg = _hex_to_reg_sb(hex_id)
        if not reg:
            return  # ohne Reg kein PK → skip (Live-Response bleibt unberührt)

        callsign = None
        if len(opensky_row) > 1 and isinstance(opensky_row[1], str):
            callsign = opensky_row[1].strip() or None
        vel_ms = _coerce_float(opensky_row[9]) if len(opensky_row) > 9 else None
        gs_kts = (vel_ms / 0.514444) if vel_ms is not None else None
        alt_m = _coerce_float(opensky_row[7]) if len(opensky_row) > 7 else None
        hdg = _coerce_float(opensky_row[10]) if len(opensky_row) > 10 else None
        on_ground = bool(opensky_row[8]) if len(opensky_row) > 8 and opensky_row[8] is not None else None
        squawk = None
        if len(opensky_row) > 14 and opensky_row[14]:
            squawk = str(opensky_row[14])
        last_seen = _coerce_float(opensky_row[3]) if len(opensky_row) > 3 else None

        row = {
            'registration': reg,
            'hex24': (hex_id or '').lower() or None,
            'callsign': callsign,
            'latitude': lat,
            'longitude': lon,
            'altitude_m': alt_m,
            'ground_speed_kts': round(gs_kts, 1) if gs_kts is not None else None,
            'heading_deg': hdg,
            'on_ground': on_ground,
            'squawk': squawk,
            'last_seen_unix': last_seen,
            'aircraft_type': None,
            'fetched_at': datetime.now(timezone.utc).isoformat(),
        }
        _persist_position_row(row)
    except Exception:
        # Warm-Persist ist best-effort — niemals die Live-Response brechen.
        pass


def _hex_to_reg(hex_id):
    """Inverse-Lookup Hex→Reg aus der Backend-Map. None wenn unbekannt."""
    if not hex_id:
        return None
    target = hex_id.strip().lower()
    for reg, hx in _BACKEND_REG_HEX.items():
        if hx == target:
            return reg
    return None


# #24: Inverse Hex→Reg-Auflösung über die SB-Tabelle `tail_hex` (icao24→
# registration). Deckt Flugzeuge ab, die NICHT in der winzigen hardcoded
# _BACKEND_REG_HEX-Map stehen — ohne sie blieb deren Live-Position un-persistiert
# und der Cold-Start-Backfill lief leer. Kleiner Prozess-lokaler Cache (auch für
# Misses), damit wir nicht pro Live-Fetch dieselbe Hex erneut gegen SB schicken.
_HEX_REG_SB_CACHE = {}
_HEX_REG_SB_LOCK = threading.Lock()


def _hex_to_reg_sb(hex_id):
    """Inverse-Lookup Hex→Reg aus SB (`tail_hex`). Gecacht (inkl. Misses).
    Guarded → wirft nie, gibt None bei SB-down/Fehler/unbekannt."""
    if not hex_id:
        return None
    target = hex_id.strip().lower()
    if not target:
        return None
    with _HEX_REG_SB_LOCK:
        if target in _HEX_REG_SB_CACHE:
            return _HEX_REG_SB_CACHE[target]
    reg = None
    try:
        sb, ok = _sb_client()
        if ok and sb is not None:
            r = (sb.table('tail_hex').select('registration')
                 .eq('icao24', target).limit(1).execute())
            rows = r.data or []
            if rows:
                reg = _normalize_registration(rows[0].get('registration'))
    except Exception as e:
        try:
            current_app.logger.info(
                f'[adsb] hex_to_reg_sb_skip hex={target} '
                f'err={type(e).__name__}: {str(e)[:120]}'
            )
        except Exception:
            pass
        # Miss NICHT cachen wenn der Lookup wegen eines Fehlers (SB down)
        # fehlschlug — sonst bliebe die Hex bis Prozess-Restart geblockt.
        return None
    with _HEX_REG_SB_LOCK:
        _HEX_REG_SB_CACHE[target] = reg
    return reg

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
# Frische-Floor: eine aus den Warehouse-Tabellen (fr24_live/aircraft_positions/
# adsb.lol) gewählte Position darf nur dann als LIVE (cached:false) ausgeliefert
# werden, wenn ihre ECHTE Beobachtung jünger als das ist. Ältere echte Fixe
# werden ehrlich `stale_due_to_upstream_outage` markiert statt als „jetzt
# aktuell" — verhindert den 2026-07-05-Bug (2,3h-alter Atlantik-Punkt als live).
_LIVE_TABLE_FLOOR_S = 900
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


# ── Reg→Hex Resolver-Cache (in-process, TTL) ─────────────────
# Cacht Supabase-tail_hex-Treffer (und Misses) pro Reg, damit nicht jeder
# Tracker-Poll dieselbe Reg erneut gegen SB auflöst. Wert None = "in SB nicht
# gefunden" (negativer Cache, kürzere TTL — die Maschine könnte beim nächsten
# Monats-Import dazukommen).
_REG_HEX_CACHE = {}                 # reg(upper) -> {"hex": str|None, "at": float}
_REG_HEX_CACHE_LOCK = threading.Lock()
_REG_HEX_TTL_HIT = 12 * 3600        # bestätigte Treffer 12h cachen
_REG_HEX_TTL_MISS = 600             # negatives Ergebnis nur 10 min cachen


def _reg_hex_cache_get(reg_u):
    now = time.time()
    with _REG_HEX_CACHE_LOCK:
        e = _REG_HEX_CACHE.get(reg_u)
        if e is None:
            return False, None
        ttl = _REG_HEX_TTL_HIT if e["hex"] else _REG_HEX_TTL_MISS
        if now - e["at"] > ttl:
            return False, None
        return True, e["hex"]


def _reg_hex_cache_put(reg_u, hex_val):
    with _REG_HEX_CACHE_LOCK:
        _REG_HEX_CACHE[reg_u] = {"hex": hex_val, "at": time.time()}
        if len(_REG_HEX_CACHE) > 5000:
            items = sorted(_REG_HEX_CACHE.items(), key=lambda kv: kv[1]["at"])
            for k, _ in items[:1000]:
                _REG_HEX_CACHE.pop(k, None)


def _sb_lookup_tail_hex(reg_u):
    """Supabase-tail_hex-Lookup → lowercase Hex oder None. Graceful: SB down /
    Tabelle fehlt / Fehler → None (Caller fällt auf die hartkodierte Map zurück).
    Wirft NIE."""
    sb, ok = _sb_client()
    if not ok:
        return None
    try:
        r = (sb.table('tail_hex')
             .select('icao24')
             .eq('registration', reg_u)
             .limit(1)
             .execute())
        rows = r.data or []
    except Exception:
        return None
    if not rows:
        return None
    hx = (rows[0].get('icao24') or '').strip().lower()
    return hx or None


def resolve_reg_to_hex(reg):
    """Public helper für andere Blueprints (z.B. aircraft_info_blueprint).
    Liefert lowercase Hex oder None. Reg wird upper-cased.

    Auflösungs-Reihenfolge:
      1) In-Process-TTL-Cache (Treffer 12h, Miss 10min)
      2) Supabase `tail_hex` (OpenSky-Aircraft-DB-Import, ~hunderttausende Tails)
      3) hartkodierte `_BACKEND_REG_HEX`-Map (Last-Line-of-Defense)
    Graceful: SB down/Miss → hartkodierte Map. Crasht NIE auf fehlendem Tail."""
    if not reg:
        return None
    reg_u = reg.strip().upper()
    if not reg_u:
        return None

    cached, val = _reg_hex_cache_get(reg_u)
    if cached:
        # Negativer Cache-Hit: trotzdem noch die hartkodierte Map prüfen (sie
        # ist statisch und kostet nichts — könnte einen Tail kennen, den SB nicht hat).
        return val or _BACKEND_REG_HEX.get(reg_u)

    hx = _sb_lookup_tail_hex(reg_u)
    if hx:
        _reg_hex_cache_put(reg_u, hx)
        return hx

    # GEBACKENE 520k-Referenz-DB (offline, gratis) — hält die echten icao24 der
    # ganzen LH/Airline-Flotte (z.B. D-ABYO → 3c4b2f), die Supabase `tail_hex`
    # oft (noch) NICHT kennt. Ohne diese Quelle scheiterte Reg→Hex komplett und
    # ADS-B (das per HEX abfragt, NICHT per Tail) fand den Flieger nie → die
    # „Wo ist mein Flieger"-Karte blieb leer (Owner 2026-07-05: „er sucht nach
    # tail und adsb funktioniert so nicht"). Lazy-Import vermeidet den Blueprint-
    # Zirkel (aerox_data ruft umgekehrt resolve_reg_to_hex).
    try:
        from blueprints.aerox_data_blueprint import _q1, _reg_candidates
        cands = _reg_candidates(reg_u)
        if cands:
            ph = ','.join('?' * len(cands))
            row = _q1(f'SELECT hex FROM aircraft WHERE reg IN ({ph}) LIMIT 1',
                      tuple(cands))
            baked = ((row or {}).get('hex') or '').strip().lower() or None
            if baked:
                _reg_hex_cache_put(reg_u, baked)
                return baked
    except Exception:
        pass

    _reg_hex_cache_put(reg_u, hx)   # negativ cachen (kurz), damit Miss nicht spammt
    return _BACKEND_REG_HEX.get(reg_u)


# ── Reg→Hex NUR aus billigen In-Process-Quellen (für Bulk-Pfade) ─────────────
# Eigener Cache statt _REG_HEX_CACHE: ein baked-Miss darf den Supabase-tail_hex-
# Lookup in resolve_reg_to_hex NICHT 10 min unterdrücken. Die gebackene DB ist
# statisch → kein TTL nötig.
_BAKED_REG_HEX_CACHE = {}           # reg(upper) -> hex(lower)|None
_BAKED_REG_HEX_LOCK = threading.Lock()


def _baked_hex_for_reg(reg):
    """Reg → echter ICAO24-Hex, NUR aus In-Process-Quellen (Cache, gebackene
    520k-Referenz-SQLite, hartkodierte Map) — bewusst OHNE Supabase-Roundtrip:
    der Rauszoom-Overview (_area_from_aircraft_live) ruft das für bis zu ~3000
    Rows pro Request. Rückgabe lowercase Hex oder None. Wirft NIE."""
    reg_u = (reg or '').strip().upper()
    if not reg_u:
        return None
    with _BAKED_REG_HEX_LOCK:
        if reg_u in _BAKED_REG_HEX_CACHE:
            return _BAKED_REG_HEX_CACHE[reg_u] or _BACKEND_REG_HEX.get(reg_u)
    hx = None
    try:
        from blueprints.aerox_data_blueprint import _q1, _reg_candidates
        cands = _reg_candidates(reg_u)
        if cands:
            ph = ','.join('?' * len(cands))
            row = _q1(f'SELECT hex FROM aircraft WHERE reg IN ({ph}) LIMIT 1',
                      tuple(cands))
            hx = ((row or {}).get('hex') or '').strip().lower() or None
    except Exception:
        hx = None
    with _BAKED_REG_HEX_LOCK:
        _BAKED_REG_HEX_CACHE[reg_u] = hx
        if len(_BAKED_REG_HEX_CACHE) > 20000:
            _BAKED_REG_HEX_CACHE.clear()
    return hx or _BACKEND_REG_HEX.get(reg_u)


# ─── Watch-Set + Poll-State (Cloud-Run-safe, alles in Supabase) ──────────────
#
# adsb_watch  = nutzer-getriebenes Set "welche Maschinen pollen wir aktiv".
# poll_state  = persistenter Scheduler-/Budget-/Token-Zustand (Key-Value).
#
# Cloud-Run ist serverless+ephemer: KEIN Hintergrund-Thread, KEIN In-Process-
# Scheduler. Aller Cross-Request-State liegt in Supabase, damit jeder /poll-Tick
# (von Cloud Scheduler getriggert) den korrekten Zustand sieht — egal welche
# Instanz ihn abarbeitet.

# Wie lange eine zuletzt angefragte Maschine im aktiven Watch-Set bleibt.
_WATCH_TTL_SECONDS = 4 * 3600           # ~4h
# Wie selten /flights/aircraft (Inbound-Origin) pro Hex erneut geholt wird.
_FLIGHTS_REFRESH_SECONDS = 2 * 3600     # ~2h


def _touch_watch(hex_id, registration=None, priority=None):
    """UPSERT eines Hex ins adsb_watch-Set (nutzer-getrieben). Setzt
    last_requested_at=now, damit die Maschine im aktiven TTL-Fenster bleibt.
    Best-effort: SB-down/Fehler → still (NIE die aufrufende Response brechen).

    Wird aus /api/adsb/state und /api/aircraft/<token>/by-reg aufgerufen — so
    wächst das Poll-Set genau um die Maschinen, die User gerade ansehen, und
    schrumpft per TTL wieder. Kein Roster-Scan, beschränkt + selbst-regulierend."""
    sb, ok = _sb_client()
    if not ok:
        return
    hex_l = (hex_id or '').strip().lower()
    if not hex_l:
        return
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        row = {
            'hex24': hex_l,
            'last_requested_at': now_iso,
        }
        if registration:
            reg_u = (registration or '').strip().upper()
            if reg_u:
                row['registration'] = reg_u
        if priority is not None:
            try:
                row['priority'] = int(priority)
            except (TypeError, ValueError):
                pass
        sb.table('adsb_watch').upsert(row, on_conflict='hex24').execute()
    except Exception as e:
        try:
            current_app.logger.warning(
                f'[adsb] touch_watch_FAIL hex={hex_l} '
                f'{type(e).__name__}: {str(e)[:120]}')
        except Exception:
            pass


def _load_active_watch():
    """Liest das aktive Watch-Set (last_requested_at innerhalb TTL) aus Supabase.
    Liefert eine Liste von Dicts. Best-effort: SB-down/Fehler → []. Wirft NIE."""
    sb, ok = _sb_client()
    if not ok:
        return []
    try:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(seconds=_WATCH_TTL_SECONDS)).isoformat()
        r = (sb.table('adsb_watch')
             .select('*')
             .gte('last_requested_at', cutoff)
             .order('priority', desc=True)
             .limit(2000)
             .execute())
        return r.data or []
    except Exception as e:
        try:
            current_app.logger.warning(
                f'[adsb] load_watch_FAIL {type(e).__name__}: {str(e)[:120]}')
        except Exception:
            pass
        return []


def _prune_stale_watch():
    """Löscht Watch-Rows die länger als TTL nicht mehr angefragt wurden. Hält das
    Set beschränkt. Best-effort, wirft NIE."""
    sb, ok = _sb_client()
    if not ok:
        return
    try:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(seconds=_WATCH_TTL_SECONDS)).isoformat()
        sb.table('adsb_watch').delete().lt('last_requested_at', cutoff).execute()
    except Exception:
        pass


def _poll_state_get(key):
    """Liest value_json einer poll_state-Row. None bei Miss/SB-down. Wirft NIE."""
    sb, ok = _sb_client()
    if not ok:
        return None
    try:
        r = (sb.table('poll_state').select('value_json')
             .eq('key', key).limit(1).execute())
        rows = r.data or []
        if not rows:
            return None
        v = rows[0].get('value_json')
        return v if isinstance(v, dict) else None
    except Exception:
        return None


def _poll_state_put(key, value_json):
    """UPSERT einer poll_state-Row. Best-effort, wirft NIE."""
    sb, ok = _sb_client()
    if not ok:
        return
    try:
        sb.table('poll_state').upsert({
            'key': key,
            'value_json': value_json,
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }, on_conflict='key').execute()
    except Exception:
        pass


# ─── Regionale Bounding-Boxes ────────────────────────────────────────────────
#
# Statt pro Maschine einzeln /states/all?icao24= zu callen (1 Credit/Maschine),
# gruppieren wir gewatchte Hexes in wenige grobe Regionen und holen je Region
# EINEN bbox-Request (/states/all?lamin&lomin&lamax&lomax). Ein bbox-Call kostet
# weniger Credits als ein globaler Call und deckt viele Maschinen auf einmal ab.
# (lamin, lomin, lamax, lomax) = (min lat, min lon, max lat, max lon).
_BBOXES = {
    'europe':        (33.0,  -12.0,  72.0,   45.0),
    'north_atlantic': (20.0,  -65.0,  72.0,  -12.0),
    'north_america': (10.0, -170.0,  72.0,  -50.0),
    'asia_pacific':  (-50.0,  60.0,   60.0,  180.0),
    'row':           (-90.0, -180.0,  90.0,  180.0),  # Rest-of-World Catch-All
}


def _bbox_for_point(lat, lon):
    """Ordnet eine Position der ersten passenden Region zu (row als Catch-All
    immer zuletzt). None nur wenn lat/lon fehlt."""
    if lat is None or lon is None:
        return None
    for name in ('europe', 'north_atlantic', 'north_america', 'asia_pacific'):
        lamin, lomin, lamax, lomax = _BBOXES[name]
        if lamin <= lat <= lamax and lomin <= lon <= lomax:
            return name
    return 'row'


OPENSKY_URL = "https://opensky-network.org/api/states/all"
OPENSKY_FLIGHTS_URL = "https://opensky-network.org/api/flights/aircraft"
# OpenSky 2025 OAuth2 client-credentials Token-Endpoint (Keycloak).
OPENSKY_TOKEN_URL = ("https://auth.opensky-network.org/auth/realms/"
                     "opensky-network/protocol/openid-connect/token")
ADSB_LOL_URL = "https://api.adsb.lol/v2/icao"
# Per-Upstream Timeouts (kürzer als vorher 10s — User wartet sonst zu lange
# wenn OpenSky hängt):
OPENSKY_TIMEOUT = 3
OPENSKY_FLIGHTS_TIMEOUT = 6
ADSB_LOL_TIMEOUT = 5
OPENSKY_TOKEN_TIMEOUT = 15  # Token 25min gecacht → einmaliger Fetch darf länger
                            # dauern; 6s war zu knapp für den Keycloak-TLS-
                            # Handshake (oauth_token_fetch_FAIL: timed out).
USER_AGENT = "AeroTax-Backend/1.1 (ADS-B-Proxy; mailto:ops@aerotax.de)"

# In-Process OAuth2-Token-Cache (zusätzlich zum persistenten poll_state-Cache).
# Spart pro Cloud-Run-Instanz den Token-Roundtrip; bei Cold-Start re-fetcht die
# erste Anfrage. Token leben ~30min, wir nutzen 25min als sichere TTL.
_OAUTH_CACHE = {"token": None, "expires_at": 0.0, "lock": threading.Lock()}
_OAUTH_TTL_SAFETY = 25 * 60  # 25 min, OpenSky-Token gilt ~30min


def _opensky_oauth_token():
    """Holt/cacht ein OAuth2-Bearer-Token via client_credentials, wenn
    OPENSKY_CLIENT_ID + OPENSKY_CLIENT_SECRET gesetzt sind. None wenn keine
    Creds gesetzt ODER der Token-Fetch scheitert (Caller fällt dann auf Basic-
    Auth bzw. anonym zurück). Wirft NIE.

    Caching-Strategie (zweistufig):
      1) In-Process-Cache (_OAUTH_CACHE) — schnellster Pfad pro Instanz.
      2) poll_state['oauth_token'] — überlebt Cold-Starts, von allen Instanzen
         geteilt (eine Instanz fetcht, der Rest liest).
    """
    cid = os.environ.get('OPENSKY_CLIENT_ID', '').strip()
    secret = os.environ.get('OPENSKY_CLIENT_SECRET', '')
    if not cid or not secret:
        return None

    now = time.time()
    # 1) In-Process-Cache.
    with _OAUTH_CACHE["lock"]:
        if _OAUTH_CACHE["token"] and now < _OAUTH_CACHE["expires_at"]:
            return _OAUTH_CACHE["token"]

    # 2) Persistenter poll_state-Cache (geteilt über Instanzen).
    cached = _poll_state_get('oauth_token')
    if cached:
        tok = cached.get('access_token')
        exp = _coerce_float(cached.get('expires_at_unix')) or 0.0
        if tok and now < exp:
            with _OAUTH_CACHE["lock"]:
                _OAUTH_CACHE["token"] = tok
                _OAUTH_CACHE["expires_at"] = exp
            return tok

    # 3) Frisch holen (client_credentials grant). Den Netzwerk-Fetch unter dem
    #    In-Process-Lock serialisieren, damit bei einem Cache-Miss nicht mehrere
    #    Threads parallel ein Token ziehen (Thundering-Herd). Nach dem Acquire
    #    Cache nochmals prüfen — ein anderer Thread könnte ihn gerade gefüllt
    #    haben, während wir gewartet haben.
    with _OAUTH_CACHE["lock"]:
        now = time.time()
        if _OAUTH_CACHE["token"] and now < _OAUTH_CACHE["expires_at"]:
            return _OAUTH_CACHE["token"]
        try:
            body = urllib.parse.urlencode({
                "grant_type": "client_credentials",
                "client_id": cid,
                "client_secret": secret,
            }).encode('utf-8')
            req = urllib.request.Request(
                OPENSKY_TOKEN_URL, data=body, method='POST',
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                })
            with urllib.request.urlopen(req, timeout=OPENSKY_TOKEN_TIMEOUT) as resp:
                obj = json.loads(resp.read())
        except Exception as e:
            try:
                current_app.logger.warning(
                    f'[adsb] oauth_token_fetch_FAIL {type(e).__name__}: {str(e)[:120]}')
            except Exception:
                pass
            return None

        tok = obj.get('access_token')
        if not tok:
            return None
        expires_in = _coerce_float(obj.get('expires_in')) or 1800.0
        ttl = min(_OAUTH_TTL_SAFETY, max(60.0, expires_in - 60.0))
        exp = now + ttl
        _OAUTH_CACHE["token"] = tok
        _OAUTH_CACHE["expires_at"] = exp

    # Best-effort in poll_state spiegeln (geteilt über Instanzen) — außerhalb des
    # Locks, der Netzwerk-/DB-Write muss den Token-Lock nicht halten.
    _poll_state_put('oauth_token', {
        'access_token': tok,
        'expires_at_unix': exp,
    })
    return tok


def _opensky_auth_header():
    """Auth-Header für OpenSky-Calls. Priorität:
      1) OAuth2-Bearer (OPENSKY_CLIENT_ID/SECRET) — 2025-Standard, mehr Credits.
      2) HTTP-Basic (OPENSKY_USERNAME/PASSWORD) — Legacy-Konto.
      3) {} → anonymer Call (aktuelles Default-Verhalten, ZERO Creds).
    Wird konsistent auf /states/all UND /flights/aircraft angewandt."""
    token = _opensky_oauth_token()
    if token:
        return {"Authorization": f"Bearer {token}"}
    user = os.environ.get('OPENSKY_USERNAME', '').strip()
    pw = os.environ.get('OPENSKY_PASSWORD', '')
    if not user or not pw:
        return {}
    basic = base64.b64encode(f"{user}:{pw}".encode('utf-8')).decode('ascii')
    return {"Authorization": f"Basic {basic}"}


# ─── Tier 3 (BEZAHLT): AeroDataBox-Live-Position ─────────────────────────────
#
# Owner-Fall 2026-07-05: LH716 FRA→HND über China — FR24 zeigte den Flieger,
# wir nicht. adsb.lol/OpenSky haben dort echte Coverage-Lücken (keine Feeder).
# Der BEZAHLTE Dienst AeroDataBox liefert im Flight-Status-Payload eine echte
# Live-Position (`location`: lat/lon + reportedAtUtc + optional altitude/
# groundSpeed/trueTrack — gespeist u.a. aus satellitengestütztem ADS-B).
#
# Regeln (streng, damit Radar-Browsing das Budget NIE leerzieht):
#   · Läuft NUR wenn der Request gezielt markiert ist (?own=1 oder
#     ?purpose=own|inbound|watch) — eigener Flug / Inbound-Maschine des Users /
#     von Familie oder Freunden beobachteter Flug (Family-„Fliegt gerade").
#   · NUR wenn die freien Quellen (OpenSky + adsb.lol) keine Position lieferten.
#   · Eigener Tages-Budget-Key 'adb_position:YYYYMMDD' (ADB_POSITION_DAILY_CAP,
#     Default 200 Calls/Tag) + der globale Paid-Guard aus aerox_data_blueprint
#     (_paid_budget_ok) müssen BEIDE frei sein. Zählung atomar via
#     ax_budget_increment-RPC (_budget_key_inc / _paid_budget_inc — importiert,
#     nicht dupliziert).
#   · EHRLICH: Position ohne reportedAtUtc-Zeitstempel oder älter als
#     _ADB_POS_MAX_AGE_S (~10 min) wird NICHT als live geliefert → Kaskade
#     läuft normal weiter (stale/ocean-bridge-Fallback). Nichts erfinden.

ADB_POSITION_TIMEOUT = 6
_ADB_POS_MAX_AGE_S = 600          # Position älter als ~10 min gilt nicht als live


def _adb_position_daily_cap():
    try:
        return int(os.environ.get('ADB_POSITION_DAILY_CAP', '200'))
    except (TypeError, ValueError):
        return 200


def _adb_position_budget_key():
    return 'adb_position:' + time.strftime('%Y%m%d', time.gmtime())


def _adb_position_budget_ok():
    """True solange das eigene Tages-Kontingent (adb_position:YYYYMMDD) frei ist
    UND der globale Paid-Deckel (aerox_data._paid_budget_ok) nicht erschöpft ist.
    Fail-CLOSED bei Import-/Helper-Problemen — ein bezahlter Call ohne
    funktionierenden Zähler wäre unbudgetierter Spend."""
    try:
        from blueprints.aerox_data_blueprint import _budget_key_used, _paid_budget_ok
        if not _paid_budget_ok():
            return False
        return _budget_key_used(_adb_position_budget_key()) < _adb_position_daily_cap()
    except Exception:
        return False


def _adb_position_budget_inc():
    """Zählt EINEN ADB-Positions-Call: eigener Tages-Key (atomar via
    ax_budget_increment-RPC) + globales Paid-Unit-Konto (Flight-Endpoint =
    Tier 2 = 2 Units, identisch zu _aerodatabox_route). Wirft NIE."""
    try:
        from blueprints.aerox_data_blueprint import _budget_key_inc, _paid_budget_inc
        _budget_key_inc(_adb_position_budget_key(), 1)
        _paid_budget_inc(units=2)
    except Exception:
        pass


def _adb_num(v, *keys):
    """AeroDataBox-Mengenfeld → float. Der Payload liefert Mengen wahlweise als
    nackte Zahl ODER als Einheiten-Dict ({'meter':…,'feet':…} / {'kt':…} /
    {'deg':…}) — erstes vorhandenes `keys`-Feld gewinnt. None wenn nicht
    vorhanden/unparsebar — nie raten."""
    if v is None:
        return None
    if isinstance(v, dict):
        for k in keys:
            if v.get(k) is not None:
                return _coerce_float(v[k])
        return None
    return _coerce_float(v)


def _adb_position_http(reg, date):
    """GET /flights/reg/{reg}/{date}?withLocation=true → Liste der Flight-
    Objekte oder None bei Fehler/Quota/Netz (still degradieren). Kanal-
    Erkennung identisch zu _aerodatabox_route (aerox_data_blueprint):
    kurzer cuid-Key = api.market-Direktportal, langer Key = RapidAPI."""
    key = os.environ.get('AERODATABOX_KEY', '')
    if not key:
        return None
    if len(key) <= 32:
        base = 'https://prod.api.market/api/v1/aedbx/aerodatabox'
        hdr = {'x-magicapi-key': key, 'User-Agent': USER_AGENT}
    else:
        host = 'aerodatabox.p.rapidapi.com'
        base = f'https://{host}'
        hdr = {'x-rapidapi-key': key, 'x-rapidapi-host': host,
               'User-Agent': USER_AGENT}
    url = (f'{base}/flights/reg/{urllib.parse.quote(reg)}/{date}'
           f'?withAircraftImage=false&withLocation=true')
    try:
        req = urllib.request.Request(url, headers=hdr)
        with urllib.request.urlopen(req, timeout=ADB_POSITION_TIMEOUT) as r:
            d = json.loads(r.read().decode('utf-8', 'replace'))
            return d if isinstance(d, list) else None
    except Exception:
        return None      # 429/quota/Netz → still degradieren (Kaskade läuft weiter)


def _adb_position_attempt(hex_id, reg_hint):
    """Tier-3-Versuch: AeroDataBox-Live-Position für die Maschine.

    Returns:
        (row, obs_ts, None)          — Erfolg: OpenSky-Layout-Row (siehe
                                       _fetch_adsb_lol) + ECHTER Beobachtungs-
                                       Zeitstempel (unix) aus reportedAtUtc.
        (None, None, skip_reason)    — kein Versuch / kein brauchbares Ergebnis;
                                       Caller läuft die alte Kaskade weiter.

    Budget wird VOR dem HTTP-Call geprüft und (nur) bei tatsächlichem Call
    inkrementiert. Positionen ohne Zeitstempel oder älter als
    _ADB_POS_MAX_AGE_S werden verworfen (nichts erfinden)."""
    if not os.environ.get('AERODATABOX_KEY', ''):
        return None, None, 'no_key'
    reg = (_normalize_registration(reg_hint)
           or _hex_to_reg(hex_id) or _hex_to_reg_sb(hex_id))
    if not reg:
        return None, None, 'no_registration'
    if not _adb_position_budget_ok():
        return None, None, 'budget_exhausted'
    _adb_position_budget_inc()
    date = time.strftime('%Y-%m-%d', time.gmtime())
    flights = _adb_position_http(reg, date)
    if not flights:
        return None, None, 'no_data'

    try:
        from blueprints.aerox_data_blueprint import _adb_ts
    except Exception:
        return None, None, 'helper_unavailable'

    # Jüngste brauchbare location über alle Legs des Tages; Legs mit fremder
    # Reg (sollte beim reg-gekeyten Endpoint nicht vorkommen) werden übersprungen.
    best = None     # (obs_ts, lat, lon, location_dict, flight_dict)
    for f in flights:
        if not isinstance(f, dict):
            continue
        f_reg = (((f.get('aircraft') or {}).get('reg')) or '').strip().upper()
        if f_reg and f_reg != reg:
            continue
        loc = f.get('location')
        if not isinstance(loc, dict):
            continue
        lat = _coerce_float(loc.get('lat'))
        lon = _coerce_float(loc.get('lon'))
        obs_ts = _adb_ts(loc.get('reportedAtUtc'))
        # Ohne Zeitstempel keine Freshness-Garantie → Position NICHT verwenden.
        if lat is None or lon is None or obs_ts is None:
            continue
        if best is None or obs_ts > best[0]:
            best = (obs_ts, lat, lon, loc, f)

    if best is None:
        return None, None, 'no_location'
    obs_ts, lat, lon, loc, f = best
    age = time.time() - obs_ts
    if age > _ADB_POS_MAX_AGE_S:
        return None, None, f'stale_position({int(age)}s)'

    alt_m = _adb_num(loc.get('altitude'), 'meter')
    if alt_m is None:
        alt_m = _adb_num(loc.get('pressureAltitude'), 'meter')
        if alt_m is None:
            alt_ft = _adb_num(loc.get('pressureAltitude'), 'feet')
            alt_m = alt_ft * 0.3048 if alt_ft is not None else None
    gs_kt = _adb_num(loc.get('groundSpeed'), 'kt')
    vel_ms = gs_kt * 0.514444 if gs_kt is not None else None
    track = _adb_num(loc.get('trueTrack'), 'deg')
    callsign = ((f.get('callSign') or f.get('number') or '')
                .replace(' ', '').strip().upper() or None)
    # on_ground nur ableiten wenn belegbar (Höhe klar über Boden), sonst None.
    on_ground = False if (alt_m is not None and alt_m > 100) else None

    # OpenSky-State-Row-Layout (identisch zu _fetch_adsb_lol) — Clients brauchen
    # KEINE Quellen-bedingte Parser-Variante:
    row = [
        (hex_id or '').lower() or None,   # 0 icao24
        callsign,                          # 1 callsign
        reg,                               # 2 reg (best-effort, wie adsb.lol)
        obs_ts,                            # 3 time_position (ECHTER Obs-Zeitpunkt)
        obs_ts,                            # 4 last_contact
        lon,                               # 5 lon
        lat,                               # 6 lat
        alt_m,                             # 7 baro_altitude_m
        on_ground,                         # 8 on_ground
        vel_ms,                            # 9 velocity_m_s
        track,                             # 10 true_track
        None,                              # 11 vertical_rate
        None,                              # 12 sensors
        None,                              # 13 geo_altitude_m
        None,                              # 14 squawk (liefert ADB nicht)
        False,                             # 15 spi
        0,                                 # 16 position_source
    ]
    return row, obs_ts, None


# ─── Live-Kaskade als aufrufbare Funktion (Route + interner Fan-out) ─────────

# ─── FR24-Grauzonen-Snapshot: schließt das China/Russland/Ozean-Coverage-Loch ──
#  Die freien ADS-B-Netze (OpenSky/adsb.lol/adsb.fi/airplanes.live) haben dort
#  KEINE Feeder → Langstrecke (FRA-HND) verschwindet. FR24 aggregiert zusätzlich
#  MLAT + terrestrische Partner-Feeds in genau diesen Regionen (Owner-Beweis LH716
#  über China: FR24 sah den Flieger, wir nicht). data-cloud.flightradar24.com/
#  zones/fcgi/feed.js ist ein Bounding-Box-BULK-Feed: EIN Call liefert alle Flieger
#  einer Box mit hex/reg/typ/callsign/route/pos. Wir holen einen WELT-Snapshot und
#  cachen ihn ~45s → egal wie viele Crew wir beobachten, FR24 sieht nur ~1 Call/45s
#  (rate-freundlich + höflich). GRAUZONE: FR24-ToS untersagt Scraping; NUR als
#  Lücken-Fallback hinter den freien Netzen, browser-UA nötig (generischer UA → 32B
#  leer). Datacenter-IP-Risiko (Cloud Run) → bei Block still degradieren, nie Crash.
FR24_FEED_URL = "https://data-cloud.flightradar24.com/zones/fcgi/feed.js"
FR24_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
           "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15")
FR24_TIMEOUT = 8
FR24_REFRESH_MIN = 40           # s — frühestens so oft EINE Kachel nachladen (~1 Call/40s)
FR24_ENTRY_TTL = 360            # s — Einträge älter als 6 min aus dem Merge-Index werfen
# Korridor-Kacheln (lat_n,lat_s,lon_w,lon_e) NUR über den Coverage-Löchern, wo die
# freien ADS-B-Netze (OpenSky/adsb.lol/adsb.fi) keine Feeder haben — Europa/USA
# fehlen bewusst (dort ist frei besser + FR24 würde clippen). FR24 cappt bei 1500
# Rows/Call; diese Loch-Kacheln sind sparsam (Ozean/Sibirien/Zentralasien) → kein
# Clipping-Verlust unseres Ziel-Fliegers. Round-Robin: 1 Kachel pro Refresh, damit
# EINE IP nur ~1 Call/40s macht (FR24-ToS-Grauzone → höflich bleiben; die verteilte
# Multi-IP-Harvester-Variante liest denselben Merge-Index aus Supabase).
FR24_TILES = [
    (55, 20, 55, 110),    # Zentralasien/West-China (LH716-FRA-HND-Fall)
    (72, 45, 55, 140),    # Trans-Sibirien
    (55, 20, 110, 145),   # Ost-China/Korea/Japan-Anflug
    (45, 8, 30, 65),      # Naher Osten / Kaspisch
    (35, -10, 60, 100),   # Indien / Indischer Ozean
    (72, 35, -60, -10),   # Nordatlantik (ozeanisch)
    (60, 15, 140, 180),   # Nordpazifik-West (Dateline)
    (40, -40, -25, 55),   # Afrika / Südatlantik-Anflug
]
# entries: hex → (row, inserted_ts). by_cs: callsign → hex. Merge über alle Kacheln.
# store_fresh_at: wann der verteilte Harvester-Store (Supabase fr24_live) zuletzt
# frische Daten lieferte — ist er warm, harvestet das Backend NICHT selbst.
FR24_STORE_READ_MIN = 30        # s — frühestens so oft den Supabase-Store nachladen
FR24_STORE_FRESH = 120          # s — solange Store-Daten frisch sind, kein Selbst-Fetch
_FR24 = {"lock": threading.Lock(), "last_at": 0.0, "tile_idx": 0,
         "entries": {}, "by_cs": {}, "cooldown_until": 0.0,
         "store_at": 0.0, "store_fresh_at": 0.0}


def _fr24_row_to_opensky(v):
    """FR24-feed.js-Zeile → OpenSky-State-Row (identisch zu _normalize_adsb_lol_ac).
    FR24-Layout: [0]hex [1]lat [2]lon [3]track [4]alt_ft [5]gs_kt [6]squawk
    [7]radar [8]type [9]reg [10]ts [11]orig [12]dest [13]flight_iata [14]on_gnd
    [15]vspeed_fpm [16]callsign [17]? [18]airline_icao. Ohne lat/lon → None."""
    try:
        if not isinstance(v, list) or len(v) < 17:
            return None
        lat, lon = v[1], v[2]
        if lat in (None, 0) and lon in (None, 0):
            return None
        def _n(x):
            try:
                return float(x)
            except (TypeError, ValueError):
                return None
        alt_ft = _n(v[4]); gs_kt = _n(v[5]); vs_fpm = _n(v[15])
        ts = _n(v[10]) or time.time()
        cs = (str(v[16]).strip() or None) if v[16] else None
        reg = (str(v[9]).strip().upper() or None) if v[9] else None
        # Der Feed wird mit &estimated=1 geholt (MLAT/extrapolierte Rows möglich).
        # v[7] = radar/Receiver-Code: bei einem echten ADS-B-Fix steht ein
        # Receiver-String drin; MLAT/estimated haben KEINEN direkten Receiver
        # (leer/None). position_source folgt OpenSky: 0=ADS-B (echt), 2=MLAT/
        # estimated. So kann der Resolver einen geschätzten Fix down-ranken, statt
        # ihn nur über Frische einen echten überranken zu lassen.
        _radar = str(v[7]).strip() if (len(v) > 7 and v[7] not in (None, '')) else ''
        position_source = 0 if _radar and _radar.upper() != 'MLAT' else 2
        return [
            (str(v[0]).strip().lower() or None),                 # 0 hex
            cs,                                                   # 1 callsign
            reg,                                                  # 2 reg (whitelist fr24)
            ts,                                                   # 3 time_position
            ts,                                                   # 4 last_contact
            _n(lon),                                              # 5 lon
            _n(lat),                                              # 6 lat
            (alt_ft * 0.3048) if alt_ft is not None else None,   # 7 baro_alt_m
            bool(v[14]),                                          # 8 on_ground
            (gs_kt * 0.514444) if gs_kt is not None else None,   # 9 velocity_m_s
            _n(v[3]),                                             # 10 true_track
            (vs_fpm * 0.00508) if vs_fpm is not None else None,  # 11 vertical_rate
            None,                                                 # 12 sensors
            (alt_ft * 0.3048) if alt_ft is not None else None,   # 13 geo_alt_m
            (str(v[6]).strip() or None) if v[6] else None,       # 14 squawk
            False,                                                # 15 spi
            position_source,                                      # 16 position_source
        ]
    except Exception:
        return None


def _fr24_fetch_tile(tile):
    """Holt EINE FR24-Kachel → Liste normalisierter Rows. Wirft NIE (bei
    Block/Fehler: []). Grauzone → höflicher Browser-UA + Referer."""
    n, s, w, e = tile
    url = (f"{FR24_FEED_URL}?bounds={n},{s},{w},{e}"
           "&faa=1&mlat=1&flarm=1&adsb=1&gnd=0&air=1&vehicles=0"
           "&estimated=1&maxage=14400&gliders=0&stats=0")
    req = urllib.request.Request(url, headers={
        "User-Agent": FR24_UA, "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.flightradar24.com/",
    })
    try:
        with urllib.request.urlopen(req, timeout=FR24_TIMEOUT) as resp:
            obj = json.loads(resp.read())
    except Exception as e:
        raise _AdsbLolError(f"fr24 tile {type(e).__name__}: {str(e)[:60]}")
    out = []
    for k, v in obj.items():
        if not isinstance(v, list):
            continue                       # full_count/version/stats-Metakeys
        row = _fr24_row_to_opensky(v)
        if row is not None and row[0] is not None:
            out.append(row)
    return out


def _fr24_refresh_one_tile():
    """Round-Robin: lädt die NÄCHSTE Korridor-Kachel und merged sie in den
    Index (Einträge mit Insert-Zeit; alte >TTL werden geräumt). Frühestens alle
    FR24_REFRESH_MIN Sekunden → EINE IP macht ~1 Call/40s. Bei Block: Cooldown."""
    now = time.time()
    with _FR24["lock"]:
        if now - _FR24["last_at"] < FR24_REFRESH_MIN or now < _FR24["cooldown_until"]:
            return
        idx = _FR24["tile_idx"]
        _FR24["tile_idx"] = (idx + 1) % len(FR24_TILES)
        _FR24["last_at"] = now
    tile = FR24_TILES[idx]
    try:
        rows = _fr24_fetch_tile(tile)
    except _AdsbLolError as ex:
        with _FR24["lock"]:
            _FR24["cooldown_until"] = time.time() + 120.0
        logging.getLogger('aerotax.adsb').info(f"[fr24] tile{idx}_skip {str(ex)[:80]}")
        return
    ins = time.time()
    with _FR24["lock"]:
        ent = _FR24["entries"]
        for row in rows:
            ent[row[0]] = (row, ins)
            if row[1]:
                _FR24["by_cs"][row[1].upper()] = row[0]
        # TTL-Räumung (Einträge, die keine Kachel mehr auffrischt)
        cutoff = ins - FR24_ENTRY_TTL
        stale = [h for h, (_, t) in ent.items() if t < cutoff]
        for h in stale:
            ent.pop(h, None)
    logging.getLogger('aerotax.adsb').info(
        f"[fr24] tile{idx} {tile} rows={len(rows)} index={len(_FR24['entries'])}")


def _parse_iso_to_epoch(val):
    """ISO-8601-String (z.B. Supabase updated_at '2026-07-06T12:34:56Z') oder
    bereits numerischer Unix-Wert → Unix-Epoch (float). None wenn unparsebar —
    der Caller behandelt das dann ehrlich als „kein Zeitstempel"."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        try:
            return float(val)
        except (TypeError, ValueError):
            return None
    try:
        dt = datetime.fromisoformat(str(val).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _fr24_warm_from_store():
    """Liest die verteilte Harvester-Flotte (Supabase fr24_live) warm in den
    In-Memory-Index. Das ist der PRIMÄRE Pfad: mehrere IPs pollen FR24 und
    schreiben hierher; das Backend fasst FR24 dann selbst NICHT an. Throttled
    (FR24_STORE_READ_MIN). Setzt store_fresh_at, wenn frische Rows kamen. Wirft NIE."""
    now = time.time()
    with _FR24["lock"]:
        if now - _FR24["store_at"] < FR24_STORE_READ_MIN:
            return
        _FR24["store_at"] = now
    sb, ok = _sb_client()
    if not ok or sb is None:
        return
    cutoff = time.strftime('%Y-%m-%dT%H:%M:%SZ',
                           time.gmtime(now - FR24_ENTRY_TTL))
    try:
        res = (sb.table('fr24_live')
               .select('hex,callsign,row,updated_at')
               .gt('updated_at', cutoff)
               .limit(8000).execute())
        data = res.data or []
    except Exception as e:
        logging.getLogger('aerotax.adsb').info(
            f"[fr24] store_read_skip {type(e).__name__}: {str(e)[:60]}")
        return
    if not data:
        return
    ins = time.time()
    with _FR24["lock"]:
        ent = _FR24["entries"]
        for d in data:
            row = d.get('row')
            if not isinstance(row, list) or not row or not row[0]:
                continue
            hx = str(row[0]).strip().lower()
            # ECHTE Beobachtungszeit sicherstellen: fehlt row[3] (time_position),
            # den WAHREN Store-Zeitstempel (fr24_live.updated_at) einsetzen — NIE
            # „jetzt" fabrizieren. Ist auch updated_at nicht parsebar, bleibt row[3]
            # None → der Resolver behandelt die Row dann als ältest (nie fake-fresh).
            while len(row) <= 3:
                row.append(None)
            if row[3] in (None, ''):
                row[3] = _parse_iso_to_epoch(d.get('updated_at'))
                if row[3] is not None and (len(row) <= 4 or row[4] in (None, '')):
                    while len(row) <= 4:
                        row.append(None)
                    row[4] = row[3]
            ent[hx] = (row, ins)
            cs = d.get('callsign') or (row[1] if len(row) > 1 else None)
            if cs:
                _FR24["by_cs"][str(cs).upper()] = hx
        cutoff_ts = ins - FR24_ENTRY_TTL
        for h in [h for h, (_, t) in ent.items() if t < cutoff_ts]:
            ent.pop(h, None)
        _FR24["store_fresh_at"] = ins
    logging.getLogger('aerotax.adsb').info(
        f"[fr24] store_warm rows={len(data)} index={len(_FR24['entries'])}")


def _fetch_fr24(hex_id, callsign=None):
    """FR24-Grauzonen-Lookup aus dem gemergten Korridor-Index: erst per hex,
    sonst per Callsign. Primär aus der verteilten Harvester-Flotte (Supabase);
    nur falls die kalt ist (keine VMs/alle tot), selbst EINE Kachel harvesten.
    Row (frisch genug) oder None. Wirft NIE."""
    if not hex_id and not callsign:
        return None
    try:
        _fr24_warm_from_store()
        with _FR24["lock"]:
            store_live = (time.time() - _FR24["store_fresh_at"]) < FR24_STORE_FRESH
        # KILL-SWITCH (Owner 2026-07-06, BAN-BLOCKER): der FR24-Selbst-Harvest im
        # synchronen User-Pfad ist AUS. Der verteilte NAS/VM-Harvester füllt
        # fr24_live; das Backend liest hier NUR den Store. Nur mit explizitem
        # FR24_BACKEND_SELFHARVEST=1 springt das Backend bei kaltem Store selbst
        # ein (Notbetrieb, wenn keine Harvester-VM läuft).
        if not store_live and os.environ.get('FR24_BACKEND_SELFHARVEST', '0') == '1':
            _fr24_refresh_one_tile()
    except Exception:
        pass
    now = time.time()
    with _FR24["lock"]:
        ent = _FR24["entries"]
        hx = str(hex_id).strip().lower() if hex_id else None
        if hx and hx in ent:
            row, t = ent[hx]
            if now - t < FR24_ENTRY_TTL:
                return row
        if callsign:
            hx2 = _FR24["by_cs"].get(str(callsign).strip().upper())
            if hx2 and hx2 in ent:
                row, t = ent[hx2]
                if now - t < FR24_ENTRY_TTL:
                    return row
    return None


def _live_position_cascade(hex_param, reg_param='', targeted=False, callsign=''):
    """Dünner Adapter auf die EINE Positions-Quelle (warehouse_reader.
    position_for_flight). Signatur & Rückgabe UNVERÄNDERT, damit alle
    bestehenden Caller (Route get_adsb_state + Family-Watch-Fan-out
    resolve_position_for_watch) weiterlaufen.

    Die Kaskaden-Logik selbst (Tier 1 fr24_live → Tier 2 aircraft_positions →
    Tier 3 adsb.lol[targeted&miss] → Tier 4 AeroDataBox[targeted&paid], Auswahl
    nach max echtem obs_ts) lebt jetzt zentral in warehouse_reader — genutzt von
    ALLEN „wo ist der Flieger"-Pfaden (eigene Position, nächster Flieger,
    Family-„fliegt gerade", Freunde/Crew-Radar), damit sie nie mehr
    widersprechen.

    OpenSky ist bewusst NICHT mehr im synchronen User-Pfad: der Hintergrund-
    ADS-B-Poller hält aircraft_positions frisch; der User-Request liest Tabellen
    (Bulk) bzw. fasst extern nur als budget-gedeckelten targeted-Notnagel an
    (KERN-REGEL 5000 User).

    Returns (row, source, obs_ts, tried):
        row     — OpenSky-State-Array oder None.
        source  — 'fr24' | 'aircraft_positions' | 'adsb.lol' | 'adb' bei Treffer;
                  'none' (non-None) wenn nichts gefunden (Route fährt dann ihren
                  ehrlichen Stale-Fallback). Der 'adb'-Zweig der Route bleibt
                  unverändert scharf.
        obs_ts  — ECHTER Beobachtungs-Zeitstempel (unix) der Row (= row[3]); nie
                  „jetzt" für alte Fixe. None bei Miss.
        tried   — Diagnose-Liste (pro Tier + finaler selected/confirmed-Eintrag).
    """
    from blueprints.warehouse_reader import position_for_flight
    # allow_paid folgt targeted: der bezahlte Tier 4 darf nur feuern, wenn die
    # Abfrage überhaupt gezielt ist (resolve_position_for_watch mappt
    # allow_paid→targeted; Family kommt so mit targeted=False = kein Paid).
    return position_for_flight(
        hex=(hex_param or '').strip().lower() or None,
        reg=(reg_param or '').strip().upper() or None,
        callsign=(callsign or '').strip().upper() or None,
        targeted=bool(targeted),
        allow_paid=bool(targeted),
    )


def resolve_position_for_watch(reg=None, hex_id=None, allow_paid=True, callsign=None):
    """Interner Positions-Lookup für beobachtete Flüge (Family/Freunde) ohne
    Flask/HTTP-Self-Call: Cache → freie Kaskade → optional Tier 3.

    OWNER-DIREKTIVE 2026-07-06 (zweistufig): „familie könnte sogar kostenlos
    bleiben … aber freunde kann man mal pingen, sehr überwacht" →
    - FAMILY ruft mit allow_paid=False: NUR Cache + freie Quellen, NIE ein
      bezahlter Ping. Korrekt ist die Family-Karte trotzdem, weil Abflug/
      Ankunft delay-korrigiert aus den (gratis) Board-Beobachtungen kommen.
    - FREUNDE (purpose=watch) dürfen mit allow_paid=True den budget-bewachten
      Tier 3 nutzen (eigener Tages-Cap + globaler Paid-Deckel).
    Gleiche Seiteneffekte wie die Route (Fresh-Cache + Warm-Persist). Der
    Caller memoisiert zusätzlich pro (reg, datum), damit N Watcher nicht
    N-fach kaskadieren/zahlen.

    → (row, source, obs_ts) oder (None, None, None). obs_ts = bester bekannter
    Beobachtungs-/Fetch-Zeitpunkt (unix); die Row selbst trägt in [3]/[4] den
    upstream-eigenen Beobachtungszeitstempel."""
    reg_n = _normalize_registration(reg) if reg else None
    hexp = (hex_id or '').strip().lower()
    if not hexp and reg_n:
        hexp = resolve_reg_to_hex(reg_n) or ''
    if not hexp:
        return None, None, None
    cached = _cache_get(hexp)
    if cached is not None and cached.get("row") is not None:
        try:
            ts = float(cached.get("fetched_at") or 0) or time.time()
        except (TypeError, ValueError):
            ts = time.time()
        return cached["row"], cached.get("source", "cache"), ts
    row, source, obs_ts, _tried = _live_position_cascade(
        hexp, reg_n or '', targeted=bool(allow_paid),
        callsign=(callsign or '').strip().upper())
    if row is None:
        return None, None, None
    # PERF-Fix: Anzeige-Zeit (fetched_at=ECHTER obs_ts, für den Client) von der
    # Cache-Frische (cached_at=jetzt, 60s-TTL) TRENNEN. Sonst hebelt ein
    # minutenalter echter obs_ts den 60s-Cache aus (now-obs_ts>60 → Cache-Miss) und
    # JEDER Watcher-Request re-kaskadiert. So bleibt der Eintrag 60s gültig, zeigt
    # aber die ehrliche Beobachtungszeit.
    _cache_put(hexp, row, source, fetched_at=obs_ts, cached_at=time.time())
    try:
        _warm_persist_from_opensky_row(hexp, row, source)
    except Exception:
        pass
    return row, source, (obs_ts if obs_ts is not None else time.time())


# ─── /api/adsb/state ─────────────────────────────────────────

@adsb_bp.route('/api/adsb/state', methods=['GET'])
def get_adsb_state():
    """
    Liefert die letzte bekannte ADS-B-Position einer Maschine.

    Query:
        hex=<icao24>    — direkter Hex-Lookup (bevorzugt, Client weiß was er will)
        reg=<reg>       — Reg→Hex Mapping (Fallback für Web-Clients)
        own=1           — GEZIELTE Abfrage (eigener Flug / Inbound-Maschine des
                          Users). Schaltet Tier 3 frei: liefern die freien
                          Quellen (OpenSky/adsb.lol) keine Position, wird EIN
                          budget-bewachter AeroDataBox-Call versucht
                          (source='adb'). Radar-Sweeps/fremde Flieger setzen
                          den Parameter NICHT → nie Paid-Spend.
        purpose=own|inbound|watch — Alternative zu own=1 (gleiche Semantik).
                          'watch' = von Familie/Freunden BEOBACHTETER Flug
                          (Owner 2026-07-06: „die familie/freunde sind
                          wichtiger als ich — wenn es sein muss kann man mal
                          1 pingen"), gleiche Budget-Mechanik.

    Antwort 200:
        {"hex": "<hex>", "position": <openSky-row> | null, "fetched_at": <unix>, "cached": <bool>}

    Antwort 400 wenn weder hex noch reg gegeben.
    Antwort 429 wenn OpenSky uns geratelimited hat — `Retry-After`-Header gesetzt.
    """
    # Rate-Limit pro IP (token-los, da öffentlicher Live-Endpoint). Großzügig,
    # damit normales App-Polling (mehrere Flieger/Minute) nicht getroffen wird,
    # aber Brute-Force/Scraper abgefangen werden.
    ip = _req_ip(request)
    if _rate_limited(ip=ip, endpoint='adsb_state', limit=120, window_sec=60):
        return jsonify({"error": "rate_limited"}), 429

    hex_param = (request.args.get('hex') or '').strip().lower()
    reg_param = (request.args.get('reg') or '').strip().upper()

    # Tier-3-Freigabe (BEZAHLTE Quelle, s. _adb_position_attempt): NUR gezielte
    # Abfragen des eigenen Fluges / der Inbound-Maschine / des von Familie
    # oder Freunden beobachteten Fluges. iOS setzt ?own=1 (oder
    # ?purpose=own|inbound|watch); Radar-Sweeps setzen nichts.
    own_raw = (request.args.get('own') or '').strip().lower()
    purpose = (request.args.get('purpose') or '').strip().lower()
    targeted = (own_raw in ('1', 'true', 'yes')
                or purpose in ('own', 'inbound', 'watch'))

    if not hex_param and not reg_param:
        return jsonify({"error": "missing hex or reg parameter"}), 400

    # Wenn Client beides mitschickt (reg+hex) bevorzugen wir den expliziten
    # Hex — sonst Server-Lookup über Backend-Reg-Map.
    if not hex_param and reg_param:
        hex_param = resolve_reg_to_hex(reg_param)
        if not hex_param:
            return jsonify({
                "error": f"unknown registration {reg_param}",
                "hint": "pass ?hex=<icao24> if client knows the mapping",
            }), 404

    # Fresh-Cache-Hit (60s TTL) — sofort raus. Watch-Touch (Supabase-Upsert)
    # bewusst NICHT vor diesem Early-Return: ein frischer Cache-Hit darf KEINE
    # DB-Schreiblast erzeugen. Wir touchen das Watch-Set erst, wenn wir gleich
    # tatsächlich upstream fetchen (s. unten).
    cached = _cache_get(hex_param)
    if cached is not None:
        return jsonify({
            "hex": hex_param,
            "position": cached["row"],
            "fetched_at": cached["fetched_at"],
            "cached": True,
            "source": cached.get("source", "cache"),
        }), 200

    # Nutzer-getriebenes Watch-Set füttern: dieser Client interessiert sich
    # gerade für diesen Hex → in adsb_watch upserten, damit der Shared-Poller
    # ihn ins aktive Poll-Set aufnimmt (best-effort, bricht die Response nie).
    # Erst NACH dem Fresh-Cache-Hit, damit Cache-Hits keine DB-Writes auslösen.
    _touch_watch(hex_param, registration=reg_param or None)

    # Cold-Start-Backfill (aircraft_positions, < 24h) wird NUR noch als Datenquelle
    # für den ehrlichen Stale-/Ozean-Fallback WEITER UNTEN vorgeladen — KEIN
    # eigener <90s-Early-Return mehr. Ein solcher Early-Return war eine „eigene
    # abweichende Quellen-Reihenfolge": er lieferte eine leicht-frische
    # aircraft_positions-Position (Tier 2) und übersprang Tier 1 (fr24_live, das
    # der Owner PRIMÄR haben will) komplett — genau die „widersprüchlichen
    # Anzeigen", die hier vereinheitlicht werden. Die Frische-Auswahl über ALLE
    # Tiers (fr24 vor aircraft_positions, max echtem obs_ts) trifft jetzt
    # ausschließlich der einheitliche Resolver (_live_position_cascade →
    # position_for_flight); der Cold-Start-Fall ist dort als Tier 2
    # (aircraft_positions) mit abgedeckt, kann also nicht mehr verloren gehen, und
    # der 2026-07-05-Root-Cause („2,3h-alter Atlantik-Punkt als aktuell") bleibt
    # ausgeschlossen, weil der Resolver stale Tabellen-Fixe gar nicht erst wählt.
    backfilled = _backfill_cache_from_sb(hex_param)

    # ─── Steps 1 / 2 / 2b: EINE Positions-Quelle für alle „wo ist der Flieger"-
    # Pfade (fr24_live → aircraft_positions → [targeted] adsb.lol → [targeted+paid]
    # AeroDataBox, Auswahl nach max echtem obs_ts). Der Resolver ist der EINZIGE
    # Weg nach extern (targeted-Notnagel); dieser Route-Handler geht selbst nie
    # direkt upstream. ───
    row, source, obs_ts, tried = _live_position_cascade(
        hex_param, reg_param, targeted=targeted)

    # BREAK A-Fix: der Resolver liefert bei Total-Miss den STRING 'none' (row=None),
    # nie mehr Python-None. Hier auf None zurückmappen, sonst ist der `source is not
    # None`-Gate immer wahr und die ehrlichen Fallbacks unten (30-min-Bridge,
    # <24h-Backfill, no_signal, 502) wären toter Code.
    if source == "none":
        source = None

    if source == "adb":
        # Tier-4-Treffer: in den normalen Cache — mit dem ECHTEN Beobachtungs-
        # Zeitstempel (reportedAtUtc) als Anzeige-Zeit, Fetch-Zeit=jetzt für die TTL.
        _cache_put(hex_param, row, "adb", fetched_at=obs_ts, cached_at=time.time())
        _warm_persist_from_opensky_row(hex_param, row, "adb")
        return jsonify({
            "hex": hex_param,
            "position": row,
            "fetched_at": obs_ts,
            "cached": False,
            "source": "adb",
            "tried": tried,
        }), 200

    # ─── Erfolg: cachen + ausgeben ───
    # HINWEIS: der Resolver gibt eine non-None source NUR mit row != None zurück
    # (Treffer). Ein „ok, aber kein Signal" existiert im Resolver nicht mehr — das
    # fällt als source=None (oben gemappt) durch auf die Bridge-/no_signal-Zweige.
    if source is not None and row is not None:
        # BREAK B-Fix: den ECHTEN obs_ts der Tabellen-Row als fetched_at liefern UND
        # cachen (fetched_at=obs_ts), NICHT time.time() — sonst ist „stale-as-fresh"
        # (2026-07-05-Bug) zurück. Fetch-Zeit=jetzt (cached_at) trägt die 60s-TTL.
        try:
            _row_age = time.time() - float(obs_ts) if obs_ts is not None else 1e9
        except (TypeError, ValueError):
            _row_age = 1e9
        # Best-effort warm-keep der Supabase-Fallback-Tabelle — bricht die
        # Live-Response nie.
        _warm_persist_from_opensky_row(hex_param, row, source)
        # Frische-Floor: eine Tabellen-Row darf nur als LIVE (cached:false)
        # serviert werden, wenn sie jung genug ist. Ältere echte Beobachtungen
        # werden ehrlich stale-markiert ausgeliefert statt als „jetzt aktuell" —
        # und NICHT in den 60s-Fresh-Cache geseedet (sonst pinnt die stale Position
        # 60s lang jeden Folge-Request und blockt die Live-Recovery). Sie bleibt
        # aber via _warm_persist als Last-Resort-Backfill erhalten.
        if _row_age > _LIVE_TABLE_FLOOR_S:
            return jsonify({
                "hex": hex_param,
                "position": row,
                "fetched_at": obs_ts,
                "cached": True,
                "stale_due_to_upstream_outage": True,
                "stale_age_seconds": int(max(0, _row_age)),
                "source": source,
                "tried": tried,
            }), 200
        # Frische Tabellen-Row → in den 60s-Fresh-Cache: fetched_at=ECHTER obs_ts
        # (Anzeige/BREAK-B), cached_at=jetzt (TTL). Kein Re-Kaskadieren für 60s.
        _cache_put(hex_param, row, source, fetched_at=obs_ts, cached_at=time.time())
        return jsonify({
            "hex": hex_param,
            "position": row,
            "fetched_at": obs_ts,
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

    # ─── Step 3b: alter Supabase-Backfill als LETZTER Fallback (ehrlich stale
    # markiert) — überbrückt Ozean-/Coverage-Lücken, in denen kein Upstream die
    # Maschine sieht, ohne je frische Live-Daten zu verdrängen (die Kaskade oben
    # hat Vorrang und lief bereits). ───
    if backfilled is not None:
        return jsonify({
            "hex": hex_param,
            "position": backfilled["row"],
            "fetched_at": backfilled["fetched_at"],
            "cached": True,
            "stale_due_to_upstream_outage": True,
            "stale_age_seconds": int(time.time() - float(backfilled.get("fetched_at") or time.time())),
            "source": backfilled.get("source", "supabase-backfill"),
            "tried": tried,
        }), 200

    # ─── Ehrlich: hat ein Upstream SAUBER geantwortet (ok=True, aber row=None,
    # d.h. "kein ADS-B-Signal / gerade nicht in der Luft"), ist das KEIN Fehler.
    # Dann 200 mit position=null statt 502 — die Maschine ist nur am Boden ohne
    # ADS-B-Out. 502 nur, wenn ALLE Upstreams wirklich gescheitert sind. ───
    if any(t.get("ok") for t in tried):
        return jsonify({
            "hex": hex_param,
            "position": None,
            "fetched_at": time.time(),
            "cached": False,
            "source": "no_signal",
            "tried": tried,
        }), 200

    # ─── Alles fehlgeschlagen → 502 mit detaillierter Diagnose ───
    return jsonify({
        "error": "all_upstreams_failed",
        "hex": hex_param,
        "tried": tried,
    }), 502


# ─── /api/adsb/persist-position ──────────────────────────────

@adsb_bp.route('/api/adsb/persist-position', methods=['POST'])
def persist_position():
    """Persistiert die zuletzt bekannte Aircraft-Position (iOS-Push).

    Body:
        {
          "registration": "D-AIPB",        # required (PK)
          "hex24": "3c64a9",               # optional
          "callsign": "DLH439",            # optional
          "position": {                     # required: lat + lon
            "lat": 50.03, "lon": 8.55,
            "altM": 11000, "gsKts": 450, "hdgDeg": 270,
            "onGround": false, "lastSeenUnix": 1717689600, "squawk": "1000"
          },
          "route_start_iata": "FRA",       # optional
          "aircraft_type": "A320"           # optional
        }

    Response 200: {"ok": true}
    Response 400: registration / lat / lon fehlt.
    Response 503: Supabase nicht verfügbar.
    """
    body = request.get_json(silent=True) or {}
    reg = _normalize_registration(body.get('registration'))
    pos = body.get('position') or {}
    if not isinstance(pos, dict):
        pos = {}
    lat = _coerce_float(pos.get('lat'))
    lon = _coerce_float(pos.get('lon'))
    if not reg or lat is None or lon is None:
        return jsonify({
            "ok": False,
            "error": "missing registration, lat or lon",
        }), 400

    callsign = body.get('callsign')
    callsign = callsign.strip() if isinstance(callsign, str) and callsign.strip() else None
    hex24 = body.get('hex24')
    hex24 = hex24.strip().lower() if isinstance(hex24, str) and hex24.strip() else None
    rsi = body.get('route_start_iata')
    rsi = rsi.strip().upper() if isinstance(rsi, str) and rsi.strip() else None
    atype = body.get('aircraft_type')
    atype = atype.strip() if isinstance(atype, str) and atype.strip() else None
    squawk = pos.get('squawk')
    squawk = str(squawk).strip() if squawk not in (None, '') else None
    on_ground_raw = pos.get('onGround')
    on_ground = bool(on_ground_raw) if on_ground_raw is not None else None

    row = {
        'registration': reg,
        'hex24': hex24,
        'callsign': callsign,
        'latitude': lat,
        'longitude': lon,
        'altitude_m': _coerce_float(pos.get('altM')),
        'ground_speed_kts': _coerce_float(pos.get('gsKts')),
        'heading_deg': _coerce_float(pos.get('hdgDeg')),
        'on_ground': on_ground,
        'squawk': squawk,
        'last_seen_unix': _coerce_float(pos.get('lastSeenUnix')),
        'route_start_iata': rsi,
        'aircraft_type': atype,
        'fetched_at': datetime.now(timezone.utc).isoformat(),
    }

    if _persist_position_row(row):
        return jsonify({"ok": True}), 200
    return jsonify({"ok": False, "error": "supabase_unavailable"}), 503


# ─── /api/adsb/fallback-position ─────────────────────────────

@adsb_bp.route('/api/adsb/fallback-position', methods=['GET'])
def fallback_position():
    """Liefert die persistierte letzte Position einer Registration, wenn sie
    innerhalb der letzten 24h gespeichert wurde.

    Query: reg=<registration>

    Response 200 (Hit):
        {"ok": true, "position": {lat,lon,altM,gsKts,hdgDeg,onGround,
         lastSeenUnix,squawk}, "registration": ..., "route_start_iata": ...,
         "aircraft_type": ..., "last_seen_unix": ..., "on_ground": ...}
    Response 200 (kein Treffer / stale): {"ok": false}
    """
    reg = _normalize_registration(request.args.get('reg'))
    if not reg:
        return jsonify({"ok": False, "error": "missing reg parameter"}), 400

    sb, ok = _sb_client()
    if not ok:
        return jsonify({"ok": False, "error": "supabase_unavailable"}), 503

    try:
        r = (sb.table('aircraft_positions')
             .select('*')
             .eq('registration', reg)
             .limit(1)
             .execute())
        rows = r.data or []
    except Exception as e:
        try:
            current_app.logger.warning(
                f'[adsb] fallback_position_fail reg={reg} '
                f'err={type(e).__name__}: {str(e)[:120]}'
            )
        except Exception:
            pass
        return jsonify({"ok": False, "error": "lookup_failed"}), 200

    if not rows:
        return jsonify({"ok": False}), 200
    row = rows[0]

    # Staleness-Check: fetched_at innerhalb 24h?
    fetched_at = row.get('fetched_at')
    if fetched_at:
        try:
            dt = datetime.fromisoformat(str(fetched_at).replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - dt
            if age > timedelta(seconds=_FALLBACK_TTL_SECONDS):
                return jsonify({"ok": False, "stale": True}), 200
        except (ValueError, TypeError):
            # Unparsebar → vorsichtshalber als stale behandeln.
            return jsonify({"ok": False}), 200

    return jsonify({
        "ok": True,
        "registration": row.get('registration'),
        "position": {
            "lat": row.get('latitude'),
            "lon": row.get('longitude'),
            "altM": row.get('altitude_m'),
            "gsKts": row.get('ground_speed_kts'),
            "hdgDeg": row.get('heading_deg'),
            "onGround": row.get('on_ground'),
            "lastSeenUnix": row.get('last_seen_unix'),
            "squawk": row.get('squawk'),
        },
        "route_start_iata": row.get('route_start_iata'),
        "aircraft_type": row.get('aircraft_type'),
        "last_seen_unix": row.get('last_seen_unix'),
        "on_ground": row.get('on_ground'),
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
        except (TypeError, ValueError):
            return jsonify({"error": "invalid coordinates"}), 400
        # Wertebereich prüfen — NaN/Inf und out-of-range (lat∈[-90,90],
        # lon∈[-180,180]) ablehnen statt Müll in die Great-Circle-Rechnung zu
        # geben (float() schluckt 'nan'/'inf' klaglos).
        if not (math.isfinite(lat1) and math.isfinite(lon1)
                and math.isfinite(lat2) and math.isfinite(lon2)):
            return jsonify({"error": "invalid coordinates"}), 400
        if not (-90.0 <= lat1 <= 90.0 and -90.0 <= lat2 <= 90.0
                and -180.0 <= lon1 <= 180.0 and -180.0 <= lon2 <= 180.0):
            return jsonify({"error": "coordinates out of range"}), 400
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
        fresh = sum(1 for v in _CACHE.values() if now - _cache_ttl_ts(v) < _CACHE_TTL_SECONDS)
    with _BACKOFF["lock"]:
        backoff_until = _BACKOFF["until"]
    return jsonify({
        "ok": True,
        "cache_entries": cache_size,
        "cache_fresh": fresh,
        "cache_ttl_seconds": _CACHE_TTL_SECONDS,
        "backoff_active": now < backoff_until,
        "backoff_remaining": max(0, int(backoff_until - now)),
        "persistence": get_persist_stats(),
    }), 200


# ─── Cache Helpers ──────────────────────────────────────────

def _cache_ttl_ts(entry):
    """Zeitbasis für die 60s-Cache-TTL = FETCH-Zeit (wann wir die Position
    besorgt haben), NICHT die (oft ältere) Beobachtungs-/Anzeige-Zeit
    `fetched_at`. Sonst hebelt eine minutenalte echte Beobachtung den Cache aus
    und jeder Request re-kaskadiert (PERF-Bug). Backward-compat: alte Einträge
    ohne `cached_at` fallen auf `fetched_at` zurück."""
    return entry.get("cached_at", entry.get("fetched_at", 0.0))


def _cache_get(hex_id):
    """Gibt eine frische Cache-Row (innerhalb _CACHE_TTL_SECONDS) zurück."""
    now = time.time()
    with _CACHE_LOCK:
        entry = _CACHE.get(hex_id)
        if entry is None:
            return None
        if now - _cache_ttl_ts(entry) > _CACHE_TTL_SECONDS:
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
        if now - _cache_ttl_ts(entry) > _LAST_KNOWN_TTL_SECONDS:
            return None
        return entry


def _cache_put(hex_id, row, source="opensky", fetched_at=None, cached_at=None):
    """Zwei getrennte Zeitachsen:
      `fetched_at` = ECHTE Beobachtungs-/Anzeige-Zeit (row[3] bzw. reportedAtUtc);
                     wird dem Client als fetched_at ausgeliefert — eine ältere
                     Beobachtung darf sich NICHT als frisch ausgeben.
      `cached_at`  = FETCH-Zeit (wann wir sie besorgt haben); Basis der 60s-TTL.
                     Default = jetzt. So bleibt ein Cache-Eintrag 60s gültig, auch
                     wenn seine Beobachtung minutenalt ist (kein Re-Kaskadieren
                     pro Request), zeigt dem Client aber die ehrliche Beobachtungszeit."""
    now = time.time()
    with _CACHE_LOCK:
        _CACHE[hex_id] = {
            "fetched_at": fetched_at if fetched_at is not None else now,
            "cached_at": cached_at if cached_at is not None else now,
            "row": row,
            "source": source,
        }
        # Cache-Cap: halte max 200 Einträge. Bei Überlauf evicte die
        # ältesten 50 (nach Fetch-Zeit) — kein LRU-Overhead, einfach Bulk-Cleanup.
        if len(_CACHE) > 200:
            items = sorted(_CACHE.items(), key=lambda kv: _cache_ttl_ts(kv[1]))
            for k, _ in items[:50]:
                _CACHE.pop(k, None)


def _row_from_aircraft_positions(rec):
    """Baut aus einer aircraft_positions-Row (Supabase) eine OpenSky-State-Row,
    damit der Cold-Start-Backfill dasselbe Layout in den _CACHE legt wie ein
    Live-Fetch. None wenn keine Position (lat/lon fehlt).

    OpenSky-State-Row Layout (siehe _fetch_adsb_lol):
      [0] icao24, [1] callsign, [2] reg, [3] time_position, [4] last_contact,
      [5] lon, [6] lat, [7] baro_altitude_m, [8] on_ground, [9] velocity_m_s,
      [10] true_track, [14] squawk.
    """
    lat = _coerce_float(rec.get('latitude'))
    lon = _coerce_float(rec.get('longitude'))
    if lat is None or lon is None:
        return None
    gs_kts = _coerce_float(rec.get('ground_speed_kts'))
    velocity_ms = (gs_kts * 0.514444) if gs_kts is not None else None
    last_seen = _coerce_float(rec.get('last_seen_unix'))
    on_ground_raw = rec.get('on_ground')
    on_ground = bool(on_ground_raw) if on_ground_raw is not None else None
    squawk = rec.get('squawk')
    squawk = str(squawk) if squawk not in (None, '') else None
    return [
        (rec.get('hex24') or '').lower() or None,          # 0 icao24
        (rec.get('callsign') or None),                     # 1 callsign
        (rec.get('registration') or None),                 # 2 reg
        last_seen,                                         # 3 time_position
        last_seen,                                         # 4 last_contact
        lon,                                               # 5 lon
        lat,                                               # 6 lat
        _coerce_float(rec.get('altitude_m')),              # 7 baro_altitude_m
        on_ground,                                         # 8 on_ground
        velocity_ms,                                       # 9 velocity_m_s
        _coerce_float(rec.get('heading_deg')),             # 10 true_track
        None,                                              # 11 vertical_rate
        None,                                              # 12 sensors
        None,                                              # 13 geo_altitude_m
        squawk,                                            # 14 squawk
        False,                                             # 15 spi
        0,                                                 # 16 position_source
    ]


def _backfill_cache_from_sb(hex_id):
    """Cold-Start-Backfill: liest die frischeste persistierte Position aus
    aircraft_positions (per hex24, sonst per Reg über die Backend-Map) und seedet
    damit den In-Memory-_CACHE, BEVOR externe APIs (OpenSky/adsb.lol) probiert
    werden. So überleben Flieger-Positionen einen Cloud-Run-Restart, statt nach
    jedem Instanz-Wechsel aus dem Cache zu verschwinden.

    Nutzt dieselbe SB-Read-Logik wie fallback_position(): Freshness < 24h via
    fetched_at. Gibt die geseedete _CACHE-Entry zurück oder None bei Miss/SB-down.
    Best-effort — Fehler werden gezählt + geloggt, brechen aber nie den Call."""
    sb, ok = _sb_client()
    if not ok:
        return None
    hex_l = (hex_id or '').strip().lower()
    if not hex_l:
        return None
    try:
        # Primär per hex24 suchen; wenn die Backend-Map die Reg kennt, auch per
        # Reg (Tabelle ist reg-PK, der iOS-POST schreibt evtl. ohne hex24).
        rows = []
        r = (sb.table('aircraft_positions').select('*')
             .eq('hex24', hex_l).limit(1).execute())
        rows = r.data or []
        if not rows:
            reg = _hex_to_reg(hex_l)
            if reg:
                r = (sb.table('aircraft_positions').select('*')
                     .eq('registration', reg).limit(1).execute())
                rows = r.data or []
    except Exception as e:
        _PERSIST_STATS['persist_fail_count'] += 1
        _PERSIST_STATS['last_error'] = f'backfill {type(e).__name__}: {str(e)[:140]}'
        try:
            current_app.logger.warning(
                f'[adsb] backfill_lookup_FAIL hex={hex_l} '
                f'err={type(e).__name__}: {str(e)[:160]}'
            )
        except Exception:
            pass
        return None

    if not rows:
        _PERSIST_STATS['backfill_miss_count'] += 1
        return None
    rec = rows[0]

    # Staleness-Check (identisch zu fallback_position): fetched_at < 24h.
    fetched_at = rec.get('fetched_at')
    if fetched_at:
        try:
            dt = datetime.fromisoformat(str(fetched_at).replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - dt > timedelta(seconds=_FALLBACK_TTL_SECONDS):
                _PERSIST_STATS['backfill_miss_count'] += 1
                return None  # zu alt, nicht in den Cache seeden
        except (ValueError, TypeError):
            _PERSIST_STATS['backfill_miss_count'] += 1
            return None

    row = _row_from_aircraft_positions(rec)
    if row is None:
        _PERSIST_STATS['backfill_miss_count'] += 1
        return None
    # ECHTEN Beobachtungs-Zeitstempel des Records bestimmen (Root-Cause-Fix
    # 2026-07-05, Teil 2): `_cache_put` stempelt fetched_at=JETZT — dadurch sah
    # ein 2,3h alter Atlantik-Punkt für den Freshness-Check im Handler wie
    # „29s frisch" aus und verdrängte weiter die Live-Kaskade. Wir geben daher
    # den WAHREN Record-Zeitstempel zurück und seeden NUR wirklich frische
    # Positionen in den 60s-Fresh-Cache (sonst vergiftet der Seed jeden
    # Folge-Request für 60s mit der stalen Position).
    rec_ts = None
    try:
        _dt = datetime.fromisoformat(str(rec.get('fetched_at')).replace('Z', '+00:00'))
        if _dt.tzinfo is None:
            _dt = _dt.replace(tzinfo=timezone.utc)
        rec_ts = _dt.timestamp()
    except (ValueError, TypeError):
        rec_ts = None
    _PERSIST_STATS['backfill_ok_count'] += 1
    age = (time.time() - rec_ts) if rec_ts is not None else 1e9
    if age < 90:
        _cache_put(hex_l, row, source='supabase-backfill')
        return _cache_get(hex_l)
    return {
        'row': row,
        'fetched_at': rec_ts if rec_ts is not None else time.time() - 1e9,
        'source': 'supabase-backfill',
    }


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
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    headers.update(_opensky_auth_header())
    req = urllib.request.Request(url, headers=headers)
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


def _fetch_opensky_bbox(lamin, lomin, lamax, lomax):
    """EIN batched /states/all-Call über eine Bounding-Box. Liefert
    (states_list, credits_remaining):
      states_list      = Liste von OpenSky-State-Rows (kann [] sein)
      credits_remaining = int aus X-Rate-Limit-Remaining oder None

    Raises:
        _OpenSkyRateLimit bei 429 (retry_after aus X-Rate-Limit-Retry-After-
            Seconds oder Retry-After).
        _OpenSkyError bei anderen Fehlern.

    Nutzt denselben Auth-Header-Pfad (OAuth2/Basic/anon) wie der Einzel-Fetch."""
    qs = urllib.parse.urlencode({
        "lamin": lamin, "lomin": lomin, "lamax": lamax, "lomax": lomax,
    })
    url = f"{OPENSKY_URL}?{qs}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    headers.update(_opensky_auth_header())
    req = urllib.request.Request(url, headers=headers)
    try:
        # bbox-Calls können größer sein → etwas großzügigeres Timeout.
        with urllib.request.urlopen(req, timeout=max(OPENSKY_TIMEOUT, 8)) as resp:
            data = resp.read()
            remaining = _parse_rate_remaining(resp.headers)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            ra = (e.headers.get("X-Rate-Limit-Retry-After-Seconds")
                  or e.headers.get("Retry-After") or "60")
            try:
                ra = int(ra)
            except (TypeError, ValueError):
                ra = 60
            raise _OpenSkyRateLimit(ra) from e
        raise _OpenSkyError(f"opensky bbox http {e.code}") from e
    except urllib.error.URLError as e:
        raise _OpenSkyError(f"opensky bbox network: {e.reason}") from e
    except Exception as e:
        raise _OpenSkyError(f"opensky bbox transport: {type(e).__name__}") from e

    try:
        obj = json.loads(data)
    except (ValueError, json.JSONDecodeError) as e:
        raise _OpenSkyError("opensky bbox invalid json") from e

    return (obj.get("states") or []), remaining


def _parse_rate_remaining(headers):
    """Liest X-Rate-Limit-Remaining (OpenSky Budget-Governor-Signal). None wenn
    Header fehlt/unparsebar."""
    try:
        raw = headers.get("X-Rate-Limit-Remaining")
        if raw is None:
            return None
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def fetch_live_state(hex_id):
    """Public wrapper um den OpenSky→adsb.lol-Live-Fetch (Tier-1 für by-reg).
    Liefert die OpenSky-State-Row (Layout siehe _fetch_adsb_lol) oder None, ohne
    je zu werfen — Upstream-Fehler/RateLimit → None. Nutzt KEINE bezahlte API."""
    try:
        row = _fetch_opensky(hex_id)
        if row is not None:
            return row
    except _OpenSkyRateLimit:
        with _BACKOFF["lock"]:
            _BACKOFF["until"] = time.time() + 60
    except _OpenSkyError:
        pass
    try:
        return _fetch_adsb_lol(hex_id)
    except _UpstreamError:
        return None


def fetch_recent_flight(hex_id, lookback_hours=36):
    """OpenSky `/api/flights/aircraft` über die letzten `lookback_hours` Stunden →
    der JÜNGSTE Flug dieser Maschine als kompaktes Dict, oder None.

    EHRLICH: OpenSky liefert HISTORISCHE/tatsächliche Flüge, KEINE geplanten ETAs.
    Wir erfinden hier keine Ankunftsprognose. Felder:
        {
          'icao24', 'callsign',
          'est_departure_icao',  # Inbound-Herkunft (ICAO), kann None sein
          'est_arrival_icao',    # Ziel (ICAO), kann None sein
          'first_seen_unix',     # Start des Tracks (unix)
          'last_seen_unix',      # Ende des Tracks (unix) = "zuletzt gesehen"
        }
    None bei Fehler/leerer Antwort — der Caller degradiert dann auf Tier-3/4.
    Wirft NIE (best-effort)."""
    if not hex_id:
        return None
    now = int(time.time())
    begin = now - int(lookback_hours * 3600)
    qs = urllib.parse.urlencode({
        "icao24": hex_id.lower(),
        "begin": begin,
        "end": now,
    })
    url = f"{OPENSKY_FLIGHTS_URL}?{qs}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    headers.update(_opensky_auth_header())
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=OPENSKY_FLIGHTS_TIMEOUT) as resp:
            data = resp.read()
    except Exception:
        # 404 (kein Flug im Fenster), 429, Timeout, Netzfehler → still degrade.
        return None
    try:
        flights = json.loads(data)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(flights, list) or not flights:
        return None
    # OpenSky sortiert nicht garantiert — wir nehmen den mit dem größten lastSeen.
    def _last_seen(f):
        try:
            return int(f.get("lastSeen") or 0)
        except (TypeError, ValueError):
            return 0
    f = max(flights, key=_last_seen)
    callsign = (f.get("callsign") or "").strip() or None
    dep = (f.get("estDepartureAirport") or "").strip().upper() or None
    arr = (f.get("estArrivalAirport") or "").strip().upper() or None
    return {
        "icao24": (f.get("icao24") or hex_id).lower(),
        "callsign": callsign,
        "est_departure_icao": dep,
        "est_arrival_icao": arr,
        "first_seen_unix": f.get("firstSeen"),
        "last_seen_unix": f.get("lastSeen"),
    }


def _lol_on_ground(alt_baro_raw, alt_ft, gs_kts):
    """on_ground aus adsb.lol-Feldern. WICHTIG (Rollfeld-Zwilling, Korrektheit):
    adsb.lol sendet den String "ground" NUR wenn der Receiver das Ground-Bit
    bekommt — eine rollende/gerade gelandete Maschine meldet oft stattdessen eine
    kleine numerische Höhe (10–50 ft) bei niedriger Geschwindigkeit und würde sonst
    fälschlich als airborne gelten. Wir vereinheitlichen die Boden-Erkennung über
    aerox_data_blueprint._obs_is_grounded (single source of truth: alt ≤ ~200 ft
    UND gs < ~60 kt). Wirft NIE."""
    if alt_baro_raw == "ground":
        return True
    try:
        from blueprints.aerox_data_blueprint import _obs_is_grounded
        return _obs_is_grounded({'on_ground': False, 'alt': alt_ft, 'speed': gs_kts})
    except Exception:
        # Fallback (Import-Reihenfolge/Zirkular): dieselbe Schwelle inline.
        return (alt_ft is not None and alt_ft <= 200
                and (gs_kts is None or gs_kts < 60))


# ─── Gratis ADSBX-v2-Mirror-Netz (Deep-Research 2026-07-06, live-verifiziert) ──
#  adsb.fi und airplanes.live sind ADSBExchange-v2-kompatible Gratis-APIs mit
#  IDENTISCHEM ac-Schema wie adsb.lol (Curl-Beweis: gleicher Hex, beide 200 mit
#  ac[0].lat). Coverage der drei Community-Netze unterscheidet sich (verschiedene
#  Feeder) → ein Miss bei adsb.lol ist oft ein Hit bei adsb.fi/airplanes.live.
#  Limits: ~1 req/s pro Host, non-commercial-Grauzone → NUR als Fallback in
#  Reihenfolge, NIE parallel; 429/5xx setzt einen per-Host-Cooldown.
#  (adsb.one ist tot — Repo archiviert, Cloudflare 403; NICHT aufnehmen.)
_ADSB_MIRROR_COOLDOWN = {}   # host → unix-ts, bis wann der Host pausiert


def _adsb_v2_try_hosts(urls, timeout, what):
    """Versucht die (host, url)-Liste sequentiell. Erste Antwort mit nicht-leerem
    `ac` gewinnt → (obj, host). Alle erreichbar aber leer → (None, None) — das
    ist eine ECHTE „nirgends gesehen"-Aussage. ALLE Hosts down/Fehler → wirft
    _AdsbLolError (Kaskade behandelt das als Quell-Ausfall, nicht als Miss)."""
    now = time.time()
    last_err = None
    any_answered = False
    for host, url in urls:
        if _ADSB_MIRROR_COOLDOWN.get(host, 0) > now:
            continue
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT, "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                try:
                    ra = float(e.headers.get('Retry-After') or 60)
                except (TypeError, ValueError):
                    ra = 60.0
                _ADSB_MIRROR_COOLDOWN[host] = now + min(max(ra, 10.0), 300.0)
            last_err = _AdsbLolError(f"{host} {what} http {e.code}")
            continue
        except urllib.error.URLError as e:
            last_err = _AdsbLolError(f"{host} {what} network: {e.reason}")
            continue
        except Exception as e:
            last_err = _AdsbLolError(f"{host} {what} transport: {type(e).__name__}")
            continue
        try:
            obj = json.loads(data)
        except (ValueError, json.JSONDecodeError):
            last_err = _AdsbLolError(f"{host} {what} invalid json")
            continue
        any_answered = True
        if obj.get("ac"):
            return obj, host
    if any_answered:
        return None, None
    raise (last_err or _AdsbLolError(f"{what}: keine Mirror-Hosts verfügbar"))


def _fetch_adsb_lol(hex_id):
    """
    Fallback-Upstream: freies ADSBX-v2-Mirror-Netz, `/v2/icao|hex/<hex24>`
    (adsb.lol → adsb.fi → airplanes.live, sequentiell; siehe _adsb_v2_try_hosts).

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
    obj, _host = _adsb_v2_try_hosts([
        ('adsb.lol', f"{ADSB_LOL_URL}/{safe_hex}"),
        ('adsb.fi', f"https://opendata.adsb.fi/api/v2/hex/{safe_hex}"),
        ('airplanes.live', f"https://api.airplanes.live/v2/hex/{safe_hex}"),
    ], ADSB_LOL_TIMEOUT, "hex")
    if obj is None:
        return None
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
    string_ground = alt_baro_raw == "ground"
    alt_baro_ft = _f(alt_baro_raw) if not string_ground else None
    alt_geom_ft = _f(ac.get("alt_geom"))
    gs_kts = _f(ac.get("gs"))
    baro_rate_fpm = _f(ac.get("baro_rate"))
    # Rollfeld-Zwilling: String "ground" ODER tief&langsam (rollende Maschine mit
    # kleiner numerischer Höhe) → am Boden. Sonst gilt sie fälschlich als airborne.
    on_ground = _lol_on_ground(alt_baro_raw, alt_baro_ft, gs_kts)

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


# =============================================================================
#  Flight-Number → Aircraft-Registration (best-effort, live)
#  ---------------------------------------------------------------------------
#  GET /api/flight-reg?flight=LH439
#    → { ok:true,  flight, callsign, reg, hex, type, source }   (200)
#    → { ok:false, flight, reason }                             (200, nicht airborne)
#
#  Quelle: adsb.lol /v2/callsign/<ICAO-Callsign> — liefert die Registration des
#  Flugzeugs das GERADE diesen Callsign fliegt. Nutzbar für Inbound-Tracking,
#  Delay-Impact und Aircraft-Health, die alle die Tail-Reg brauchen. Funktioniert
#  wenn der Flug airborne ist UND die Airline ihren Flugnummer-Callsign nutzt
#  (Langstrecke meist ja; manche Kurzstrecke scrambled → dann ok:false, KEIN
#  Fake-Wert). IATA-Flugnummer → ICAO-Callsign via Airline-Map (LH→DLH etc.).
# =============================================================================

_IATA_ICAO_AIRLINE = {
    # Lufthansa Group
    "LH": "DLH", "CL": "CLH", "EN": "DLA", "EW": "EWG", "4Y": "OCN",
    "OS": "AUA", "LX": "SWR", "SN": "BEL", "WK": "EDW", "DE": "CFG",
    # Europa
    "BA": "BAW", "AF": "AFR", "KL": "KLM", "IB": "IBE", "AZ": "ITY",
    "TP": "TAP", "AY": "FIN", "SK": "SAS", "LO": "LOT", "OK": "CSA",
    "UX": "AEA", "VY": "VLG", "FR": "RYR", "U2": "EZY",
    # Langstrecke
    "EK": "UAE", "QR": "QTR", "TK": "THY", "EY": "ETD", "SQ": "SIA",
    "CX": "CPA", "UA": "UAL", "AA": "AAL", "DL": "DAL", "AC": "ACA",
    "NH": "ANA", "JL": "JAL", "ET": "ETH", "QF": "QFA",
}


def _parse_flight_number(flight):
    """IATA-Flugnummer → (Airline-Code, Nummer). IATA-Airline-Codes sind immer
    2-stellig (alphanumerisch), der Rest ist die Flugnummer."""
    f = "".join((flight or "").upper().split())
    if len(f) < 3:
        return None, None
    code, num = f[:2], f[2:]
    if not num[:1].isdigit():
        return None, None
    return code, num


def _adsb_lol_callsign_reg(callsign):
    """Aktuell unter `callsign` fliegendes Flugzeug (reg/hex/type) von adsb.lol.
    None bei nichts-airborne oder Fehler — bewusst weich (kein Raise)."""
    safe = urllib.parse.quote(callsign, safe="")
    url = f"https://api.adsb.lol/v2/callsign/{safe}"
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=ADSB_LOL_TIMEOUT) as resp:
            obj = json.loads(resp.read())
    except Exception:
        return None
    ac_list = obj.get("ac") or []
    if not ac_list:
        return None
    ac = ac_list[0]
    reg = (ac.get("r") or "").strip()
    if not reg:
        return None
    return {"reg": reg, "hex": ac.get("hex"), "type": ac.get("t")}


# =============================================================================
#  Shared-Poller: POST /api/adsb/poll  (Cloud-Scheduler-getriggert, ~60s)
#  ---------------------------------------------------------------------------
#  EIN gemeinsames Konto pollt OpenSky; ALLE User werden aus dem
#  aircraft_positions-Cache serviert. Credit-Last unabhängig von Nutzerzahl.
#
#  Ein /poll-Tick = EIN Cycle:
#    1) aktives Watch-Set laden (last_requested_at < TTL).
#    2) Hexes in regionale Bounding-Boxes gruppieren.
#    3) pro Box Cadence-Tier (hot/warm/cold) aus den dringendsten gewatchten
#       Maschinen ableiten; nur callen wenn die Cadence abgelaufen ist
#       (last_polled_at aus poll_state).
#    4) Budget-Governor: X-Rate-Limit-Remaining lesen, bei Knappheit alle
#       Cadences strecken; 429 → globaler _BACKOFF.
#    5) je fällige Box EIN bbox-Call; Ergebnisse auf gewatchte Hexes filtern;
#       als Keyframe in aircraft_positions persistieren.
#    6) sparsam pro neu-gewatchtem Hex /flights/aircraft (Inbound-Origin).
#
#  Serverless: KEIN Hintergrund-Thread. Cloud Scheduler ruft diesen Endpoint
#  zyklisch auf (siehe manuelle Schritte). Aller Zustand in Supabase.
# =============================================================================

# Cadence-Tiers in Sekunden (Default / unter Budget-Druck "stretched").
_CADENCE = {
    'hot':  {'normal': 45,  'stretched': 60},
    'warm': {'normal': 150, 'stretched': 270},
    'cold': {'normal': 300, 'stretched': 10 ** 9},  # cold gestretcht = on-demand/aus
}
# Konservatives Tages-Credit-Ziel (anon OpenSky ~400/Tag; OAuth2 mehr). Der
# Governor streckt Cadences sobald die verbleibenden Credits unter dem
# zeit-anteiligen Soll liegen.
_DAILY_CREDIT_TARGET = 350


def _poll_authorized(req):
    """Shared-Secret-Check für /api/adsb/poll.
      · ADSB_POLL_SECRET gesetzt → Header X-Poll-Secret MUSS exakt matchen.
      · ADSB_POLL_SECRET NICHT gesetzt → nur localhost erlauben (deny remote),
        damit der Endpoint ohne Konfiguration nicht offen im Netz steht.
    """
    secret = os.environ.get('ADSB_POLL_SECRET', '').strip()
    if secret:
        provided = (req.headers.get('X-Poll-Secret') or '').strip()
        # Konstant-Zeit-Vergleich gegen Timing-Attacks (statt naivem '==').
        return hmac.compare_digest(provided, secret)
    # Kein Secret konfiguriert → nur lokale Aufrufe.
    remote = (req.remote_addr or '')
    return remote in ('127.0.0.1', '::1', 'localhost')


def _budget_stretched(credits_remaining):
    """Budget-Governor-Entscheidung: sollen wir Cadences strecken?
    True wenn die verbleibenden Credits unter dem zeit-anteiligen Tages-Soll
    liegen. Ohne bekanntes `credits_remaining` (anon-Header fehlt) → False
    (nicht künstlich drosseln, wenn OpenSky uns nichts sagt)."""
    if credits_remaining is None:
        return False
    # Anteil des Tages, der noch übrig ist (UTC).
    now = datetime.now(timezone.utc)
    secs_into_day = now.hour * 3600 + now.minute * 60 + now.second
    frac_remaining_of_day = max(0.0, 1.0 - secs_into_day / 86400.0)
    # Soll: mindestens so viele Credits wie der Rest-Tag anteilig braucht.
    target_floor = _DAILY_CREDIT_TARGET * frac_remaining_of_day
    return credits_remaining < target_floor


def _tier_for_watch_rows(rows):
    """Leitet das Cadence-Tier einer Box aus ihren gewatchten Maschinen ab.
      hot  = irgendeine priority>0 (explizit erwartet) ODER airborne & landet
             bald (Heuristik: niedrige Höhe & sinkend — hier vereinfacht auf
             priority/aktuelle Höhe aus dem letzten Keyframe).
      warm = irgendeine Maschine airborne.
      cold = sonst (alle am Boden / kein Signal).

    Wir nutzen den zuletzt persistierten Keyframe (aircraft_positions) als
    Zustandsquelle — der Poller liest keine rohen Dokumente, nur Fakten."""
    tier = 'cold'
    for r in rows:
        if (r.get('priority') or 0) > 0:
            return 'hot'
        kf = r.get('_keyframe') or {}
        on_ground = kf.get('on_ground')
        alt_m = kf.get('altitude_m')
        if on_ground is False:
            # Airborne. Niedrig (<3000m ≈ FL100) → wahrscheinlich An-/Abflug → hot.
            if alt_m is not None and alt_m < 3000:
                return 'hot'
            tier = 'warm'
    return tier


def _attach_keyframes(rows):
    """Reichert Watch-Rows um ihren letzten persistierten Keyframe an
    (on_ground/altitude_m/lat/lon für die Tier-Heuristik + bbox-Zuordnung).
    Best-effort: pro Hex ein Lookup; fehlende Keyframes bleiben leer."""
    sb, ok = _sb_client()
    if not ok:
        return rows
    for r in rows:
        hex_l = (r.get('hex24') or '').strip().lower()
        if not hex_l:
            continue
        try:
            res = (sb.table('aircraft_positions')
                   .select('latitude,longitude,altitude_m,on_ground')
                   .eq('hex24', hex_l).limit(1).execute())
            data = res.data or []
            if data:
                r['_keyframe'] = data[0]
        except Exception:
            pass
    return rows


def _persist_state_row_as_keyframe(state_row):
    """Persistiert eine OpenSky-State-Row als aircraft_positions-Keyframe.
    Reuse von _warm_persist_from_opensky_row (kennt das Row-Layout + Reg-
    Auflösung + best-effort-Semantik)."""
    try:
        hex_id = (state_row[0] or '').lower() if state_row and state_row[0] else None
        if not hex_id:
            return
        _warm_persist_from_opensky_row(hex_id, state_row, 'opensky-poll')
    except Exception:
        pass


@adsb_bp.route('/api/adsb/poll', methods=['POST'])
def adsb_poll():
    """EIN Poll-Cycle für den Shared-Poller. Geschützt per X-Poll-Secret.
    Antwort: {polled_boxes, calls_made, credits_remaining, watch_size, ...}."""
    if not _poll_authorized(request):
        return jsonify({"error": "unauthorized"}), 403

    # Rate-Limit pro IP zusätzlich zum Shared-Secret — der Scheduler tickt ~60s,
    # also ist ein knappes Limit sicher und fängt Fehlkonfig/Loops ab.
    if _rate_limited(ip=_req_ip(request), endpoint='adsb_poll', limit=10, window_sec=60):
        return jsonify({"error": "rate_limited"}), 429

    now = time.time()

    # Immer-an Europa-Sweep (adsb.lol, FREI, kein OpenSky-Credit): läuft JEDEN Tick,
    # auch ohne aktive Nutzer und auch während OpenSky-Backoff. Füttert die
    # self-computed Route-Engine breit → ax_route_cache wächst gratis weltweit.
    sweep_legs = 0
    sweep_points = 0
    try:
        sweep_rows = _european_sweep_rows(now)
        # Weltweiter Crew-Hub-Ring (env-gated, EIGENE Rotation → EU-Frequenz bleibt
        # unangetastet). Board-lose Übersee-Hubs gratis & auth-frei (kein OpenSky).
        world_rows = _world_sweep_rows(now)
        if world_rows:
            sweep_rows = sweep_rows + world_rows
        sweep_points = len(sweep_rows)
        if sweep_rows:
            from blueprints.aerox_data_blueprint import observe_adsb_positions
            sweep_legs = observe_adsb_positions(sweep_rows)
    except Exception:
        sweep_legs = 0

    # Unified-Track-Layer C1 (2026-07-11): dieselben adsb.lol-Sweep-Punkte ALSO als
    # Flughafen-Breadcrumbs (Taxi/Kurven) in aircraft_track schreiben — vorher nach
    # der Leg-Erkennung verworfen. PLUS dedizierte DICHTE FRA/MUC-Abfrage jeden Tick
    # (~60 s Raster statt ~7,5-min-Rotation). adsb.lol = gratis, kein Drossel-Risiko,
    # gleiche Quelle wie die Karte. Weltweiter FR24-Baseline bleibt unberührt.
    try:
        from blueprints.aerox_data_blueprint import observe_adsb_breadcrumbs
        # HUB-ZEILEN ZUERST (Owner 2026-07-12 „Taxi wird nicht gespeichert"):
        # der Europa-Sweep liefert oft >600 airborne Zeilen — stehen die dichten
        # FRA/MUC-Punkte HINTEN, frisst der max_process-Cap in
        # observe_adsb_breadcrumbs das Budget, BEVOR eine einzige Taxi-Zeile
        # dran ist (live bewiesen: D-AIXF rollte 20 min ohne einen Crumb).
        # Die Hubs sind der Sinn des Features → sie kommen zuerst.
        crumb_rows = []
        for _hlat, _hlon in _ADSB_HUB_POINTS:
            try:
                crumb_rows.extend(_fetch_adsb_lol_point(_hlat, _hlon, 40))
            except Exception:
                continue
        if sweep_rows:
            crumb_rows.extend(sweep_rows)
        if crumb_rows:
            observe_adsb_breadcrumbs(crumb_rows)
    except Exception:
        pass

    # Globaler OpenSky-Backoff aktiv? (429 zuvor) → OpenSky-Teil aussetzen (der
    # freie Sweep oben ist schon gelaufen).
    with _BACKOFF["lock"]:
        backoff_until = _BACKOFF["until"]
    if now < backoff_until:
        return jsonify({
            "ok": True, "skipped": "backoff_active",
            "backoff_remaining": int(backoff_until - now),
            "polled_boxes": [], "calls_made": 0,
            "credits_remaining": None, "watch_size": 0,
            "sweep_aircraft": sweep_points, "sweep_legs": sweep_legs,
        }), 200

    # 1) aktives Watch-Set laden + Keyframes anhängen.
    watch = _load_active_watch()
    watch = _attach_keyframes(watch)
    watch_size = len(watch)

    # 2) Hexes in Bounding-Boxes gruppieren (per letztem Keyframe; ohne Position
    #    → 'europe' als Default-Heimatregion der Nutzerbasis).
    boxes = {}
    for r in watch:
        kf = r.get('_keyframe') or {}
        box = _bbox_for_point(kf.get('latitude'), kf.get('longitude')) or 'europe'
        boxes.setdefault(box, []).append(r)

    calls_made = 0
    credits_remaining = None
    polled_boxes = []
    all_states = []          # ALLE bbox-Rows (nicht nur gewatchte) → Self-Compute
    today_key = 'budget:' + datetime.now(timezone.utc).strftime('%Y%m%d')

    # Budget-Governor mit dem zuletzt gesehenen Rest-Credit aus poll_state seeden,
    # damit schon die ERSTE Box des Ticks die Budget-Lage respektiert (vorher lief
    # Box #1 immer auf 'normal', weil X-Rate-Limit-Remaining erst nach dem ersten
    # bbox-Call bekannt war). Best-effort: ohne bekannten Wert → False (kein
    # künstliches Drosseln).
    _prev_budget = _poll_state_get(today_key) or {}
    _prev_remaining = _coerce_float(_prev_budget.get('remaining_seen'))
    stretched = _budget_stretched(_prev_remaining) if _prev_remaining is not None else False

    # 3+4+5) pro Box: Cadence prüfen, ggf. bbox-Call, persistieren.
    for box_name, rows in boxes.items():
        tier = _tier_for_watch_rows(rows)
        cadence = _CADENCE[tier]['stretched' if stretched else 'normal']

        st = _poll_state_get('bbox:' + box_name) or {}
        last_polled = _coerce_float(st.get('last_polled_at')) or 0.0
        if (now - last_polled) < cadence:
            continue  # Cadence noch nicht abgelaufen → diese Box überspringen

        lamin, lomin, lamax, lomax = _BBOXES[box_name]
        watched_hexes = {(r.get('hex24') or '').lower() for r in rows}
        try:
            states, remaining = _fetch_opensky_bbox(lamin, lomin, lamax, lomax)
            calls_made += 1
            if remaining is not None:
                credits_remaining = remaining
                # Budget-Governor: ab jetzt ggf. strecken (gilt für Folge-Boxen).
                stretched = stretched or _budget_stretched(remaining)
        except _OpenSkyRateLimit as e:
            with _BACKOFF["lock"]:
                _BACKOFF["until"] = time.time() + e.retry_after
            polled_boxes.append({"box": box_name, "tier": tier,
                                 "ok": False, "reason": f"rate_limited({e.retry_after}s)"})
            break  # 429 → restliche Boxen diesen Tick nicht mehr callen
        except _OpenSkyError as e:
            polled_boxes.append({"box": box_name, "tier": tier,
                                 "ok": False, "reason": str(e)[:80]})
            continue

        # Auf gewatchte Hexes filtern + als Keyframes persistieren.
        matched = 0
        for srow in states:
            if not srow:
                continue
            shex = (srow[0] or '').lower() if srow[0] else ''
            if shex in watched_hexes:
                _persist_state_row_as_keyframe(srow)
                matched += 1
        all_states.extend(states)   # ALLE Rows der Box → Self-Compute-Engine

        _poll_state_put('bbox:' + box_name, {
            'last_polled_at': time.time(),
            'last_tier': tier,
            'last_count': matched,
        })
        polled_boxes.append({"box": box_name, "tier": tier, "ok": True,
                             "matched": matched, "in_box_watched": len(watched_hexes)})

    # 5b) SELF-COMPUTED ROUTES (frei): jede Maschine, die wir eh schon aus den
    #     bbox-Calls haben (NICHT nur die gewatchten), in die Route-Engine geben →
    #     Ab-/Anflug-Erkennung füllt ax_route_cache weltweit gratis.
    legs_recorded = 0
    if all_states:
        try:
            from blueprints.aerox_data_blueprint import observe_adsb_positions
            obs_rows = [r for r in (_normalize_opensky_state(s) for s in all_states) if r]
            legs_recorded = observe_adsb_positions(obs_rows)
        except Exception:
            legs_recorded = 0

    # 6) sparsam: /flights/aircraft pro neu-gewatchtem Hex (Inbound-Origin).
    #    Rate-limit per flights_fetched_at (≥ _FLIGHTS_REFRESH_SECONDS her).
    flights_fetched = 0
    sb, sb_ok = _sb_client()
    for r in watch:
        if flights_fetched >= 5:  # pro Tick deckeln (Credit-Schonung)
            break
        ff = r.get('flights_fetched_at')
        stale = True
        if ff:
            try:
                dt = datetime.fromisoformat(str(ff).replace('Z', '+00:00'))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                stale = (datetime.now(timezone.utc) - dt
                         > timedelta(seconds=_FLIGHTS_REFRESH_SECONDS))
            except (ValueError, TypeError):
                stale = True
        if not stale:
            continue
        hex_l = (r.get('hex24') or '').lower()
        if not hex_l:
            continue
        try:
            fetch_recent_flight(hex_l)  # best-effort, Ergebnis nutzt by-reg live
            flights_fetched += 1
            if sb_ok:
                sb.table('adsb_watch').update({
                    'flights_fetched_at': datetime.now(timezone.utc).isoformat()
                }).eq('hex24', hex_l).execute()
        except Exception:
            pass

    # Budget-Counter persistieren + Stale-Watch prunen (TTL-Hygiene).
    if calls_made:
        budget = _poll_state_get(today_key) or {}
        budget['calls_made'] = int(budget.get('calls_made', 0)) + calls_made
        if credits_remaining is not None:
            budget['remaining_seen'] = credits_remaining
        budget['updated_at'] = datetime.now(timezone.utc).isoformat()
        _poll_state_put(today_key, budget)
    _prune_stale_watch()

    return jsonify({
        "ok": True,
        "polled_boxes": polled_boxes,
        "calls_made": calls_made,
        "legs_recorded": legs_recorded,
        "sweep_aircraft": sweep_points,
        "sweep_legs": sweep_legs,
        "flights_fetched": flights_fetched,
        "credits_remaining": credits_remaining,
        "budget_stretched": stretched,
        "watch_size": watch_size,
    }), 200


@adsb_bp.route("/api/flight-reg", methods=["GET"])
def get_flight_reg():
    flight = (request.args.get("flight") or "").strip()
    if not flight:
        return jsonify({"ok": False, "error": "missing flight parameter"}), 400
    code, num = _parse_flight_number(flight)
    if not code or not num:
        return jsonify({"ok": False, "error": f"unparseable flight '{flight}'"}), 400
    # Kandidaten: ICAO-Mapping bevorzugt, IATA-Callsign als Fallback.
    candidates = []
    icao = _IATA_ICAO_AIRLINE.get(code)
    if icao:
        candidates.append(icao + num)
    candidates.append(code + num)
    for cs in candidates:
        hit = _adsb_lol_callsign_reg(cs)
        if hit:
            return jsonify({
                "ok": True, "flight": flight, "callsign": cs,
                "reg": hit["reg"], "hex": hit["hex"], "type": hit["type"],
                "source": "adsb.lol",
            }), 200
    return jsonify({
        "ok": False, "flight": flight,
        "reason": "no live aircraft for this callsign (not airborne or non-standard callsign)",
    }), 200


# =============================================================================
#  FREE-Datenquellen-Erweiterung (2026-06-14) — mehr Radar-Power ohne paid API
#  ---------------------------------------------------------------------------
#  Drei zusätzliche Live-Endpunkte, alle gegen die COMMUNITY-Quelle adsb.lol
#  (non-commercial, gentleman's API → aggressiv server-cachen!) mit OpenSky als
#  Fallback wo sinnvoll. Wir wiederverwenden:
#    · _coerce_float / _opensky_auth_header / _fetch_opensky_bbox / USER_AGENT
#    · die Backoff-Mechanik (_BACKOFF) wie der Single-Aircraft-Pfad
#    · die Great-Circle-Helfer für die Route-Linie (route-info)
#
#  Normalisierte Aircraft-Row-Form (FLACHES Dict, NICHT die OpenSky-Index-Liste!):
#    {hex, flight, lat, lon, alt, speed, heading, squawk, reg, type}
#  Das ist die im Task gewünschte client-freundliche Form für die Area-/Alert-
#  Listen. Der Single-Aircraft-/state-Endpoint behält bewusst sein OpenSky-Index-
#  Listen-Layout (iOS AircraftPosition.from(openSkyRow:) hängt daran) — diese
#  Listen-Endpunkte liefern die handlichere Dict-Form.
# =============================================================================

# ── Area-/Alert-Cache (getrennt vom Single-Aircraft-_CACHE) ──────────────────
# adsb.lol ist community-betrieben → wir cachen Area-Queries 45s und Alerts 30s,
# gekeyed auf gerundete Parameter, damit N Clients ≈ 1 Upstream-Call/Fenster
# auslösen. Eigener Lock, damit Area-Traffic den Single-Aircraft-Cache nicht
# blockiert.
_AREA_CACHE = {}                 # key -> {"at": float, "payload": dict}
_AREA_CACHE_LOCK = threading.Lock()
_AREA_TTL_SECONDS = 45
_ALERTS_TTL_SECONDS = 30
_ROUTEINFO_TTL_SECONDS = 6 * 3600  # Routen sind statisch genug für lange TTL

ADSB_LOL_BASE = "https://api.adsb.lol"
ADSB_LOL_AREA_TIMEOUT = 8


def _parse_hub_points(raw):
    """"lat,lon; lat,lon" → [(lat,lon)]. Leer → Default FRA+MUC."""
    default = [(50.033, 8.570), (48.353, 11.786)]      # FRA, MUC
    raw = (raw or "").strip()
    if not raw:
        return default
    out = []
    for grp in raw.split(";"):
        parts = grp.replace(",", " ").split()
        if len(parts) >= 2:
            try:
                out.append((float(parts[0]), float(parts[1])))
            except ValueError:
                continue
    return out or default


# Dedizierte DICHTE adsb.lol-Airport-Abfrage jeden Poll-Tick (Unified-Track C1) —
# 40 nm um FRA/MUC → ~60-s-Breadcrumbs inkl. Taxi in aircraft_track. Env
# ADSB_HUB_POINTS="lat,lon; …" erweiterbar (adsb.lol-Boden-Coverage = Europa/US).
_ADSB_HUB_POINTS = _parse_hub_points(os.environ.get("ADSB_HUB_POINTS", ""))
HEXDB_TIMEOUT = 5
# adsb.lol point-radius cap (nm). Über ~250nm wird die Antwort riesig und die
# community-API unfair belastet → hart deckeln. Gilt NUR für die externen Mirror-
# /OpenSky-Pfade (die die API real cappt), NICHT für den fr24_live-Overview.
_AREA_RADIUS_CAP_NM = 250
# Overview-Cap (nm) für den fr24_live-Geo-Read: unsere Welt-Tabelle hat KEIN
# 250nm-Limit (Supabase-bbox, limit(3000)) → beim Rauszoomen auf ganz Europa darf
# der Radius groß sein, damit ALLE geharvesteten Flieger im Fenster erscheinen
# (Owner: „mein Backend scrappt doch alles, also kann die App alles anzeigen").
_AREA_OVERVIEW_MAX_NM = 1500
# Freshness (s) fuer den aircraft_live-Overview-Read. Der FR24-gRPC-Harvester
# schreibt ~minuetlich; 900 s toleriert eine kurze Harvester-Delle, ohne Geister.
_AREA_LIVE_MAX_AGE_S = 900.0


def _area_cache_get(key, ttl):
    now = time.time()
    with _AREA_CACHE_LOCK:
        e = _AREA_CACHE.get(key)
        if e is None:
            return None
        if now - e["at"] > ttl:
            return None
        return e["payload"]


def _area_cache_put(key, payload):
    with _AREA_CACHE_LOCK:
        _AREA_CACHE[key] = {"at": time.time(), "payload": payload}
        if len(_AREA_CACHE) > 300:
            items = sorted(_AREA_CACHE.items(), key=lambda kv: kv[1]["at"])
            for k, _ in items[:80]:
                _AREA_CACHE.pop(k, None)


# ── Tile-Micro-Cache (gröber, kürzer) ────────────────────────────────────────
# Zweite Cache-Ebene für /api/adsb/area: der feine Cache oben keyed auf 0.1°
# (~6nm) — zwei User, die 10nm auseinander aufs Radar schauen, verfehlen ihn.
# Diese Ebene quantisiert auf 0.5°-Kacheln (~30nm) mit nur 10s TTL: bei
# typischen Radien (80-250nm) ist der Flieger-Set praktisch identisch, und
# 10s alt ist für ADS-B frisch genug (Poll-Intervall der App ist 25s).
# Ergebnis: Cache-Hit → Antwort in <10ms statt 1-3s Upstream-Roundtrip.
_AREA_TILE_CACHE = {}                # key -> {"at": float, "payload": dict}
_AREA_TILE_CACHE_LOCK = threading.Lock()
_AREA_TILE_TTL_SECONDS = 10
_AREA_TILE_CACHE_CAP = 200


def _area_tile_key(lat, lon, radius):
    # 0.5°-Kachel (round auf halbe Grade) + Radius-Bucket wie beim feinen Key.
    return f"tile:{round(lat * 2) / 2}:{round(lon * 2) / 2}:{int(round(radius / 10) * 10)}"


def _area_tile_cache_get(key):
    now = time.time()
    with _AREA_TILE_CACHE_LOCK:
        e = _AREA_TILE_CACHE.get(key)
        if e is None:
            return None
        if now - e["at"] > _AREA_TILE_TTL_SECONDS:
            return None
        return e["payload"]


def _area_tile_cache_put(key, payload):
    # Soft-Cap + evict-oldest, gleiches Muster wie _area_cache_put oben.
    with _AREA_TILE_CACHE_LOCK:
        _AREA_TILE_CACHE[key] = {"at": time.time(), "payload": payload}
        if len(_AREA_TILE_CACHE) > _AREA_TILE_CACHE_CAP:
            items = sorted(_AREA_TILE_CACHE.items(), key=lambda kv: kv[1]["at"])
            for k, _ in items[:50]:
                _AREA_TILE_CACHE.pop(k, None)


def _area_response(payload):
    """200-Response für /api/adsb/area mit Edge-Cache-Header.

    Der Endpoint ist token-frei (Query = nur lat/lon/radius, Antwort für alle
    Caller identisch) und läuft clientseitig über die CDN-Domain →
    `Cache-Control: public, max-age=8` lässt Cloudflare-Edge + URLCache kurz
    mitcachen (unter dem 10s-Tile-TTL, deutlich unter dem 25s-App-Poll).
    `public` ist nötig, weil die App einen Authorization-Header mitschickt —
    ohne `public` würden Shared-Caches solche Antworten nicht speichern."""
    resp = jsonify(payload)
    resp.headers['Cache-Control'] = 'public, max-age=8'
    return resp, 200


def _normalize_adsb_lol_ac(ac):
    """adsb.lol-Aircraft-Dict → flache normalisierte Row.
    {hex, flight, lat, lon, alt, speed, heading, squawk, reg, type}.
    None wenn keine Position (lat/lon fehlt) — Mode-S-only-Records droppen wir.

    alt_baro kann "ground" (String) sein → alt=0, on_ground=True."""
    if not isinstance(ac, dict):
        return None
    lat = _coerce_float(ac.get("lat"))
    lon = _coerce_float(ac.get("lon"))
    if lat is None or lon is None:
        return None
    alt_raw = ac.get("alt_baro")
    string_ground = (alt_raw == "ground")
    alt_num = None if string_ground else _coerce_float(alt_raw)
    gs_kts = _coerce_float(ac.get("gs"))
    # Rollfeld-Zwilling: String "ground" ODER tief&langsam → am Boden (sonst gilt
    # eine rollende Maschine mit kleiner numerischer Höhe fälschlich als airborne).
    on_ground = _lol_on_ground(alt_raw, alt_num, gs_kts)
    alt = 0 if string_ground else alt_num
    flight = (ac.get("flight") or "").strip() or None
    reg = (ac.get("r") or "").strip() or None
    return {
        "hex": (ac.get("hex") or "").strip().lower() or None,
        "flight": flight,
        "callsign": flight,           # Alias — manche Clients erwarten `callsign`
        "lat": lat,
        "lon": lon,
        "alt": alt,                   # ft (baro), 0 wenn am Boden (String-ground)
        "speed": gs_kts,              # ground speed kts
        "heading": _coerce_float(ac.get("track")),  # track deg
        "squawk": (ac.get("squawk") or None),
        "reg": reg,
        "type": (ac.get("t") or None),
        "on_ground": on_ground,
    }


def _normalize_opensky_state(s):
    """OpenSky /states/all-Row (Index-Liste) → dieselbe flache normalisierte Row.
    Wird im Area-Fallback genutzt (Einheiten an adsb.lol angeglichen:
    alt in ft, speed in kts). None wenn keine Position."""
    if not s or not isinstance(s, (list, tuple)) or len(s) < 11:
        return None
    lat = _coerce_float(s[6])
    lon = _coerce_float(s[5])
    if lat is None or lon is None:
        return None
    on_ground = bool(s[8]) if s[8] is not None else False
    alt_m = _coerce_float(s[7])
    alt_ft = (alt_m / 0.3048) if alt_m is not None else None
    if on_ground:
        alt_ft = 0
    vel_ms = _coerce_float(s[9])
    gs_kts = (vel_ms / 0.514444) if vel_ms is not None else None
    callsign = (s[1] or "").strip() if s[1] else None
    squawk = s[14] if len(s) > 14 and s[14] else None
    return {
        "hex": (s[0] or "").strip().lower() or None,
        "flight": callsign,
        "callsign": callsign,
        "lat": lat,
        "lon": lon,
        "alt": round(alt_ft) if alt_ft is not None else None,
        "speed": round(gs_kts, 1) if gs_kts is not None else None,
        "heading": _coerce_float(s[10]),
        "squawk": str(squawk) if squawk else None,
        "reg": None,                  # OpenSky-State liefert keine Reg
        "type": None,
        "on_ground": on_ground,
    }


def _fetch_adsb_lol_point(lat, lon, radius_nm):
    """Punkt-Sweep übers Mirror-Netz (adsb.lol → adsb.fi → airplanes.live) →
    Liste normalisierter Rows. adsb.fi hat ein eigenes Pfad-Schema
    (/lat/{}/lon/{}/dist/{}; live-verifiziert). Leere Antwort überall → [].
    Raises _AdsbLolError nur wenn ALLE Hosts down sind."""
    r = int(radius_nm)
    try:
        obj, _host = _adsb_v2_try_hosts([
            ('adsb.lol', f"{ADSB_LOL_BASE}/v2/point/{lat}/{lon}/{r}"),
            ('adsb.fi', f"https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{r}"),
            ('airplanes.live', f"https://api.airplanes.live/v2/point/{lat}/{lon}/{r}"),
        ], ADSB_LOL_AREA_TIMEOUT, "point")
    except _AdsbLolError:
        raise
    if obj is None:
        return []
    out = []
    for ac in (obj.get("ac") or []):
        row = _normalize_adsb_lol_ac(ac)
        if row is not None:
            out.append(row)
    return out


# ─── Merged Punkt-Sweep über ALLE freien Mirrors (Radar „so voll wie möglich") ─
#  adsb.lol, adsb.fi und airplanes.live speisen sich aus TEILWEISE DISJUNKTEN
#  Feeder-Netzen → dieselbe bbox liefert bei jedem Host andere Flieger. Der
#  first-wins-Pfad (_fetch_adsb_lol_point) erreichte adsb.fi/airplanes.live in der
#  Praxis nie, weil adsb.lol über Land fast immer zuerst antwortete. Für die
#  interaktive Radar-Ansicht fragen wir daher ALLE drei parallel ab und mergen
#  per Hex — mehr Flieger im selben Ausschnitt, v.a. an den Abdeckungsrändern.
#  (Die Hintergrund-Sweeps nutzen weiter das billigere first-wins _fetch_adsb_lol_point.)
_ADSB_POINT_MIRRORS = (
    ('adsb.lol',
     lambda lat, lon, r: f"{ADSB_LOL_BASE}/v2/point/{lat}/{lon}/{r}"),
    ('adsb.fi',
     lambda lat, lon, r: f"https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{r}"),
    ('airplanes.live',
     lambda lat, lon, r: f"https://api.airplanes.live/v2/point/{lat}/{lon}/{r}"),
)


def _row_richness(row):
    """Grobe Vollständigkeits-Punktzahl für den Dedup-Tiebreak: bei zwei Mirrors,
    die denselben Hex melden, gewinnt die Row mit mehr Detail (Reg/Typ/Squawk)."""
    return ((1 if row.get("reg") else 0)
            + (1 if row.get("type") else 0)
            + (1 if row.get("squawk") else 0))


def _adsb_point_fetch_one(host, url, timeout):
    """EIN Mirror-Host, Punkt-Sweep → obj-dict oder None (leer/Cooldown/Fehler).
    Honoriert + setzt den geteilten _ADSB_MIRROR_COOLDOWN (429/503). Wirft NIE —
    der Merge-Aufrufer sammelt Teilergebnisse und toleriert einzelne Ausfälle."""
    now = time.time()
    if _ADSB_MIRROR_COOLDOWN.get(host, 0) > now:
        return None
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (429, 503):
            try:
                ra = float(e.headers.get('Retry-After') or 60)
            except (TypeError, ValueError):
                ra = 60.0
            _ADSB_MIRROR_COOLDOWN[host] = now + min(max(ra, 10.0), 300.0)
        return None
    except Exception:
        return None
    try:
        return json.loads(data)
    except (ValueError, json.JSONDecodeError):
        return None


def _fetch_adsb_point_merged(lat, lon, radius_nm):
    """Union-Sweep über ALLE freien ADSBX-v2-Mirrors (parallel, 3 Threads),
    dedupliziert per Hex → maximal volle Fliegerliste für die Radar-Ansicht.
    Rows ohne Hex behalten wir einzeln (kein Dedup-Key). Bei Hex-Kollision gewinnt
    die detailreichere Row (_row_richness). Mind. ein Host antwortete (auch leer)
    → normale Liste (ggf. []). ALLE Hosts down/Cooldown → _AdsbLolError, damit der
    Area-Endpoint sauber auf OpenSky/FR24 zurückfällt (statt fälschlich „leer")."""
    r = int(radius_nm)
    merged = {}
    hexless = []
    answered = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futs = {
            ex.submit(_adsb_point_fetch_one, host, build(lat, lon, r),
                      ADSB_LOL_AREA_TIMEOUT): host
            for host, build in _ADSB_POINT_MIRRORS
        }
        for fut in concurrent.futures.as_completed(futs):
            try:
                obj = fut.result()
            except Exception:
                obj = None
            if obj is None:
                continue
            answered += 1
            for ac in (obj.get("ac") or []):
                row = _normalize_adsb_lol_ac(ac)
                if row is None:
                    continue
                hx = row.get("hex")
                if not hx:
                    hexless.append(row)
                    continue
                prev = merged.get(hx)
                if prev is None or _row_richness(row) > _row_richness(prev):
                    merged[hx] = row
    if answered == 0:
        raise _AdsbLolError("point-merge: alle Mirror-Hosts down/cooldown")
    return list(merged.values()) + hexless


# ─── Always-on Europa-Sweep (adsb.lol, FREI) ────────────────────────────────
#  Problem: der /poll-Cycle bildet seine bbox-Calls NUR aus dem Watch-Set aktiver
#  Nutzer. Ohne aktive Nutzer → keine Box → keine Beobachtung → die self-computed
#  Route-Engine bekommt gar nichts und ax_route_cache wächst nicht. Fix: ein
#  immer-an Europa-Sweep über adsb.lol (kostenlos, KEIN OpenSky-Credit). Wir
#  rotieren pro Tick durch Hub-Cluster (250-nm-Radien decken die Terminalräume
#  ab, wo Flieger tief/am Boden sind = genau was die Ab-/Anflug-Erkennung
#  braucht). Zeit-rotiert → stateless (serverless-freundlich).
_EU_SWEEP_POINTS = [
    (51.47,   0.45),   # London / Amsterdam / Brüssel
    (49.50,   8.57),   # Frankfurt / Zürich / Luxemburg
    (48.35,  11.79),   # München / Wien / Prag
    (48.86,   2.55),   # Paris / Lille
    (45.63,   9.28),   # Mailand / Turin / Genf
    (41.80,  12.25),   # Rom / Neapel
    (40.47,  -3.57),   # Madrid / Lissabon-Ost
    (41.30,   2.08),   # Barcelona / Palma / Valencia
    (52.55,  13.29),   # Berlin / Warschau-West / Kopenhagen
    (55.62,  12.65),   # Kopenhagen / Malmö / Hamburg
    (59.65,  17.92),   # Stockholm / Oslo-Ost
    (53.42,  -6.27),   # Dublin / Manchester / Glasgow-Süd
    (40.98,  28.82),   # Istanbul / Athen-Nord
    (37.94,  23.94),   # Athen / Ägäis
    (47.44,  19.26),   # Budapest / Belgrad / Bukarest-West
]
_EU_SWEEP_RADIUS_NM = 250

# ─── Weltweiter Crew-Hub-Sweep (adsb.lol, FREI, EIGENE Rotation) ─────────────
#  Owner-Problem: board-lose Übersee-Hubs (ORD/ICN/BKK/GRU/…) haben KEINE freie
#  Board-JSON und der OpenSky-Fill ist in Prod tot (Basic-Auth 2025 abgeschaltet,
#  keine OAuth2-Creds). adsb.lol ist gratis, ohne Cap, ohne Auth und verifiziert
#  (ORD/ICN/BKK/GRU) → wir decken diese Hubs mit einem EIGENEN, separat rotierenden
#  Ring ab. GETRENNT vom Europa-Sweep, damit die EU-Abdeckung/Frequenz NICHT
#  verwässert (eigenes Env + eigener Rotations-Cursor). Default AUS — der Owner
#  aktiviert per AX_WORLD_SWEEP=1. Jeder Treffer läuft durch observe_adsb_positions
#  → self-computed Leg + (neu) IST-Zeit-Row nach airport_delay_obs, gratis & auth-frei.
_WORLD_SWEEP_POINTS = [
    (41.98,  -87.90),  # ORD Chicago (verifiziert)
    (40.70,  -73.90),  # JFK/EWR/LGA New York
    (33.94, -118.41),  # LAX Los Angeles
    (37.62, -122.38),  # SFO San Francisco
    (33.64,  -84.43),  # ATL Atlanta
    (25.79,  -80.29),  # MIA Miami
    (43.68,  -79.63),  # YYZ Toronto
    (19.44,  -99.07),  # MEX Mexico City
    (-23.43, -46.47),  # GRU São Paulo (verifiziert)
    (-34.82, -58.54),  # EZE Buenos Aires
    (4.70,   -74.15),  # BOG Bogotá
    (30.11,   31.40),  # CAI Kairo
    (-26.14,  28.25),  # JNB Johannesburg
    (25.25,   55.36),  # DXB Dubai
    (24.44,   54.65),  # AUH Abu Dhabi
    (25.27,   51.61),  # DOH Doha
    (28.56,   77.10),  # DEL Delhi
    (19.09,   72.87),  # BOM Mumbai
    (13.69,  100.75),  # BKK Bangkok (verifiziert)
    (1.36,   103.99),  # SIN Singapur
    (22.31,  113.91),  # HKG Hongkong
    (37.46,  126.44),  # ICN Seoul (verifiziert)
    (35.68,  139.90),  # NRT/HND Tokyo
    (31.15,  121.80),  # PVG Shanghai
    (40.08,  116.60),  # PEK Peking
    (-33.95, 151.18),  # SYD Sydney
]
_WORLD_SWEEP_RADIUS_NM = 250


def _european_sweep_rows(now_ts):
    """Immer-an, freie Europa-Abdeckung via adsb.lol. Rotiert zeit-basiert durch
    _EU_SWEEP_POINTS (kein persistenter State nötig) und liefert normalisierte
    Rows für observe_adsb_positions. AX_EU_SWEEP_POINTS_PER_TICK Punkte je Tick
    (Default 2). Env AX_EU_SWEEP=0 schaltet den Sweep ab. Wirft NIE."""
    if os.environ.get('AX_EU_SWEEP', '1').strip() in ('0', 'false', 'off'):
        return []
    try:
        n = max(1, min(len(_EU_SWEEP_POINTS),
                       int(os.environ.get('AX_EU_SWEEP_POINTS_PER_TICK', '2'))))
    except (TypeError, ValueError):
        n = 2
    base = int(now_ts // 60) * n            # jede Minute n neue Punkte, rundlaufend
    rows = []
    for k in range(n):
        lat, lon = _EU_SWEEP_POINTS[(base + k) % len(_EU_SWEEP_POINTS)]
        try:
            rows.extend(_fetch_adsb_lol_point(lat, lon, _EU_SWEEP_RADIUS_NM))
        except Exception:
            continue
    return rows


def _world_sweep_rows(now_ts):
    """Freier weltweiter Crew-Hub-Sweep via adsb.lol — EIGENE, separat rotierende
    Abdeckung board-loser Übersee-Hubs (ORD/ICN/BKK/GRU/…). Getrennt vom Europa-
    Sweep, damit dessen Frequenz nicht verwässert. Env AX_WORLD_SWEEP=1 schaltet ihn
    EIN (Default AUS). AX_WORLD_SWEEP_POINTS_PER_TICK Punkte je Tick (Default 1).
    Rotiert zeit-basiert (stateless, serverless-freundlich). 429/Netz → still
    tolerieren (kein Cap bei adsb.lol, aber weiche IP-Limits respektieren). Wirft NIE."""
    if os.environ.get('AX_WORLD_SWEEP', '0').strip() not in ('1', 'true', 'on'):
        return []
    try:
        n = max(1, min(len(_WORLD_SWEEP_POINTS),
                       int(os.environ.get('AX_WORLD_SWEEP_POINTS_PER_TICK', '1'))))
    except (TypeError, ValueError):
        n = 1
    # Eigener Cursor (60-s-Takt × n) → unabhängig vom EU-Sweep-Cursor.
    base = int(now_ts // 60) * n
    rows = []
    for k in range(n):
        lat, lon = _WORLD_SWEEP_POINTS[(base + k) % len(_WORLD_SWEEP_POINTS)]
        try:
            rows.extend(_fetch_adsb_lol_point(lat, lon, _WORLD_SWEEP_RADIUS_NM))
        except Exception:
            continue      # 429/Timeout/Netz → weicher Skip, nächster Tick rotiert weiter
    return rows


def _bbox_from_point(lat, lon, radius_nm):
    """Grobe Bounding-Box (lamin, lomin, lamax, lomax) um einen Punkt für den
    OpenSky-Area-Fallback. 1 nm Breitengrad ≈ 1/60°; Längengrad mit cos(lat)
    korrigiert. Breite gedeckelt; Länge wird am Antimeridian GEWRAPPT statt
    geklemmt (Audit B10): lomin > lomax signalisiert den Dateline-Übergang,
    den die Nutzer (_area_from_aircraft_live/_area_from_fr24_live) mit ihrem
    or_-Zweig abfragen — vorher war der toter Code und dem Welt-Overview nahe
    ±180° fehlte die andere Seite."""
    dlat = radius_nm / 60.0
    cos_lat = max(0.01, math.cos(math.radians(lat)))
    dlon = radius_nm / (60.0 * cos_lat)
    lamin = max(-90.0, lat - dlat)
    lamax = min(90.0, lat + dlat)
    if dlon >= 180.0:
        # Box umspannt (z.B. polnah) den ganzen Längenkreis → voller Bereich.
        lomin, lomax = -180.0, 180.0
    else:
        lomin = lon - dlon
        lomax = lon + dlon
        if lomin < -180.0:
            lomin += 360.0        # Überlauf nach Westen → wrappen (lomin > lomax)
        if lomax > 180.0:
            lomax -= 360.0        # Überlauf nach Osten → wrappen (lomin > lomax)
    return lamin, lomin, lamax, lomax


# Ab diesem Radius (nm) liest der Radar-Viewport aus fr24_live (Übersicht,
# instant, Welt-Tabelle) statt extern. Darunter (Zoom-in) bleibt adsb.lol real-time.
_AREA_FR24_MIN_RADIUS_NM = 90.0
# Store-Rows älter als das gelten fürs Radar als tot (Übersicht toleriert Minuten).
_AREA_FR24_MAX_AGE_S = 600.0


def _area_from_fr24_live(lat, lon, radius):
    """Flieger einer bbox aus fr24_live (unsere Welt-Tabelle) — INSTANT, kein
    Extern-Call. Rückgabe: Liste OpenSky-State-Rows (identisch zu adsb.lol/point,
    weil fr24_live.row genau dieses Layout speichert) oder None (SB-down/leer).
    Dateline-Wrap (lomin>lomax) wird als zwei Fälle behandelt."""
    sb, ok = _sb_client()
    if not ok or sb is None:
        return None
    lamin, lomin, lamax, lomax = _bbox_from_point(lat, lon, radius)
    cutoff = time.strftime('%Y-%m-%dT%H:%M:%SZ',
                           time.gmtime(time.time() - _AREA_FR24_MAX_AGE_S))
    try:
        q = (sb.table('fr24_live').select('row')
             .gte('lat', lamin).lte('lat', lamax)
             .gt('updated_at', cutoff))
        if lomin <= lomax:
            q = q.gte('lon', lomin).lte('lon', lomax)
        else:
            # Dateline-Übergang: lon >= lomin ODER lon <= lomax
            q = q.or_(f'lon.gte.{lomin},lon.lte.{lomax}')
        rows = (q.limit(3000).execute()).data or []
    except Exception:
        return None
    out = []
    for d in rows:
        r = d.get('row')
        if isinstance(r, list) and len(r) > 6 and r[5] is not None and r[6] is not None:
            out.append(r)
    return out or None


def _area_from_aircraft_live(lat, lon, radius):
    """Flieger einer bbox aus aircraft_live (unser FR24-gRPC-Warehouse, global vom
    NAS-Harvester gefuellt, ~817 Maschinen weltweit, frisch). Fuer den Rauszoom-
    Overview: adsb.lol cappt bei 250 NM -> jenseits davon waeren die Raender leer,
    obwohl wir die Flieger laengst gespeichert haben (Owner: „Backend scrappt alles").
    Rueckgabe: normalisierte Dicts (identisches Schema wie _normalize_adsb_lol_ac,
    damit die Response + der iOS-Parser unveraendert bleiben) oder None (leer/SB-down).

    aircraft_live ist REG-keyed (kein ICAO-Hex) -> `hex` wird per gebackener
    Referenz-DB (reg -> icao24) aufgeloest; nur wenn das scheitert, faellt der
    Key auf die kleingeschriebene Reg zurueck (stabil fuer iOS-Annotationen,
    kein Row-Drop — aber kein Fake-Hex mehr, wo ein echter existiert)."""
    sb, ok = _sb_client()
    if not ok or sb is None:
        return None
    lamin, lomin, lamax, lomax = _bbox_from_point(lat, lon, radius)
    cutoff = time.strftime('%Y-%m-%dT%H:%M:%SZ',
                           time.gmtime(time.time() - _AREA_LIVE_MAX_AGE_S))
    try:
        q = (sb.table('aircraft_live')
             .select('reg,reg_display,callsign,flight,lat,lon,track,gs_kt,'
                     'alt_ft,ac_type,on_ground,origin,dest')
             .gte('lat', lamin).lte('lat', lamax)
             .gt('updated_at', cutoff))
        if lomin <= lomax:
            q = q.gte('lon', lomin).lte('lon', lomax)
        else:
            q = q.or_(f'lon.gte.{lomin},lon.lte.{lomax}')
        rows = (q.limit(3000).execute()).data or []
    except Exception:
        return None
    out = []
    for d in rows:
        la = _coerce_float(d.get('lat'))
        lo = _coerce_float(d.get('lon'))
        if la is None or lo is None:
            continue
        # Echter ICAO24-Hex aus der gebackenen Referenz-DB (Audit B2a) — der
        # frühere Fake-„hex" (kleingeschriebene Reg ohne Bindestrich) konnte
        # downstream Hex-Gates mit einem 6-Zeichen-Lookalike öffnen.
        hexkey = _baked_hex_for_reg(d.get('reg') or d.get('reg_display'))
        if hexkey is None:
            # Fallback wie bisher: stabiler Annotation-Key, kein Row-Drop.
            hexkey = ((d.get('reg') or d.get('callsign') or d.get('flight') or '')
                      .strip().lower().replace('-', '')) or None
        if hexkey is None:
            continue
        flight = (d.get('flight') or d.get('callsign') or '').strip() or None
        out.append({
            "hex": hexkey,
            "flight": flight,
            "callsign": (d.get('callsign') or flight),
            "lat": la, "lon": lo,
            "alt": _coerce_float(d.get('alt_ft')) or 0,
            "speed": _coerce_float(d.get('gs_kt')),
            "heading": _coerce_float(d.get('track')),
            "squawk": None,
            "reg": (d.get('reg_display') or d.get('reg') or '').strip() or None,
            "type": (d.get('ac_type') or None),
            "on_ground": bool(d.get('on_ground')),
            "origin": (d.get('origin') or None),
            "dest": (d.get('dest') or None),
        })
    return out or None


def _area_from_fr24_grpc(lat, lon, radius):
    """Satelliten-Fallback für die Live-Map: FR24 gRPC area() über eine bbox —
    füllt Ozean/China/Sibirien (Aireon-Satellit + Partner-Feeds), wo adsb.lol/fi/
    OpenSky PHYSISCH leer sind (kein Bodenempfänger). Rückgabe: normalisierte
    Dicts (identisches Schema wie _normalize_adsb_lol_ac → downstream unverändert),
    oder None. FR24 liefert alt in ft + speed in kts = dieselben Einheiten."""
    try:
        from blueprints import fr24_grpc
    except Exception:
        return None
    if not fr24_grpc.available():
        return None
    lamin, lomin, lamax, lomax = _bbox_from_point(lat, lon, radius)
    try:
        if lomin > lomax:
            # Dateline-Wrap (B10): die FR24-BoundingBox kann west>east nicht →
            # zwei Boxen (…→180 und -180→…) abfragen und mergen.
            positions = ((fr24_grpc.area(north=lamax, south=lamin,
                                         west=lomin, east=180.0, limit=1500) or [])
                         + (fr24_grpc.area(north=lamax, south=lamin,
                                           west=-180.0, east=lomax, limit=1500) or []))
        else:
            positions = fr24_grpc.area(north=lamax, south=lamin, west=lomin, east=lomax,
                                       limit=1500)
    except Exception:
        return None
    out = []
    for p in (positions or []):
        if p.get('lat') is None or p.get('lon') is None:
            continue
        out.append({
            'hex': None,                       # FR24-Row trägt kein hex → callsign-Key
            'flight': p.get('callsign'),
            'callsign': p.get('callsign'),
            'lat': p.get('lat'), 'lon': p.get('lon'),
            'alt': p.get('alt'),               # ft
            'speed': p.get('speed'),           # kts
            'heading': p.get('track'),
            'squawk': None,
            'reg': p.get('reg'),
            'type': p.get('ac_type'),
            'on_ground': False,
            'route_from': p.get('route_from'), # Bonus: Route inline (Tap sofort da)
            'route_to': p.get('route_to'),
        })
    return out or None


@adsb_bp.route('/api/adsb/area', methods=['GET'])
def get_adsb_area():
    """Live-Aircraft in einem Radius um einen Punkt.

    Query: lat= lon= radius= (nm, default 100, cap 250).
    Quelle: adsb.lol /v2/point/{lat}/{lon}/{radius} (community, cache 45s).
    Fallback: OpenSky /states/all über eine bbox aus lat/lon/radius.

    200: {ok, count, aircraft:[{hex,flight,lat,lon,alt,speed,heading,squawk,reg,type,on_ground}], source, cached}
    400: lat/lon fehlt oder out-of-range.
    """
    if _rate_limited(ip=_req_ip(request), endpoint='adsb_area', limit=120, window_sec=60):
        return jsonify({"ok": False, "error": "rate_limited"}), 429

    lat = _coerce_float(request.args.get('lat'))
    lon = _coerce_float(request.args.get('lon'))
    if lat is None or lon is None:
        return jsonify({"ok": False, "error": "missing lat/lon"}), 400
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return jsonify({"ok": False, "error": "invalid lat/lon"}), 400
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return jsonify({"ok": False, "error": "lat/lon out of range"}), 400

    radius = _coerce_float(request.args.get('radius'))
    if radius is None or radius <= 0:
        radius = 100.0
    # Overview-Cap: der fr24_live-Pfad (>= _AREA_FR24_MIN_RADIUS_NM) darf groß sein
    # (kein Extern-Limit). Die externen Mirror-/OpenSky-Fallbacks unten klemmen
    # zusätzlich lokal auf _AREA_RADIUS_CAP_NM.
    radius = min(_AREA_OVERVIEW_MAX_NM, max(1.0, radius))

    # Cache-Key auf ~0.1° (≈6nm) gerundet, damit leicht abweichende GPS-Punkte
    # denselben Cache-Eintrag treffen (N Clients → 1 Upstream-Call/Fenster).
    ck = f"area:{round(lat, 1)}:{round(lon, 1)}:{int(round(radius / 10) * 10)}"
    cached = _area_cache_get(ck, _AREA_TTL_SECONDS)
    if cached is not None:
        out = dict(cached)
        out["cached"] = True
        return _area_response(out)

    # Zweite Ebene: 0.5°-Tile-Micro-Cache (10s TTL) — fängt Requests ab, deren
    # Zentrum den feinen 0.1°-Key knapp verfehlt (z.B. leicht gepannte Karte
    # oder zwei User wenige nm auseinander). Instant statt Upstream-Roundtrip.
    tk = _area_tile_key(lat, lon, radius)
    tile_cached = _area_tile_cache_get(tk)
    if tile_cached is not None:
        out = dict(tile_cached)
        out["cached"] = True
        return _area_response(out)

    tried = []
    aircraft = None
    source = None

    # ─── Primär beim RAUSZOOMEN (> 250 NM): aircraft_live-Geo-Read ───
    # Der Owner-Schmerz „ganz rausgezoomt = leere Ränder, nur ein Flieger-Block":
    # adsb.lol point-radius ist bei 250 NM gecappt — jenseits davon leer, obwohl der
    # FR24-gRPC-Harvester die Flieger (global, ~800 Maschinen) längst in aircraft_live
    # speichert. Der bbox-Read liest sie INSTANT aus Supabase, ohne Extern-Call, ohne
    # 250-Cap. NUR beim echten Overview (> 250 NM): bei moderatem Zoom (<= 250) bleibt
    # der dichte adsb.lol-Echtzeit-Merge unten (mehr Verkehr als unsere 800er-Flotte).
    # aircraft_live leer/kalt (unabgedeckte Region) → adsb.lol/OpenSky-Fallback unten.
    # (fr24_live ist derzeit leer — der verteilte Harvester läuft nicht; aircraft_live
    # ist die real gefüllte Welt-Tabelle, s. _area_from_aircraft_live.)
    if radius > _AREA_RADIUS_CAP_NM:
        try:
            fr_ac = _area_from_aircraft_live(lat, lon, radius)
        except Exception as e:
            fr_ac = None
            tried.append({"upstream": "aircraft_live", "ok": False, "reason": str(e)[:80]})
        if fr_ac:
            aircraft = fr_ac
            source = "aircraft_live"
            tried.append({"upstream": "aircraft_live", "ok": True, "count": len(fr_ac)})

    # ─── Primär (Zoom-in) / Fallback: freie Mirrors GEMERGED (voller Radar) ───
    #  Union aus adsb.lol + adsb.fi + airplanes.live (parallel, dedup per Hex) statt
    #  first-wins — verschiedene Feeder-Netze sehen verschiedene Flieger.
    if aircraft is None:
        try:
            # Externe Mirror cappen real bei 250nm → lokal klemmen (kein Riesen-URL).
            aircraft = _fetch_adsb_point_merged(
                lat, lon, min(radius, _AREA_RADIUS_CAP_NM))
            source = "adsb-merged"
            tried.append({"upstream": "adsb-merged", "ok": True,
                          "count": len(aircraft)})
        except _AdsbLolError as e:
            tried.append({"upstream": "adsb-merged", "ok": False,
                          "reason": str(e)[:80]})

    # ─── Fallback: OpenSky bbox (außer im Backoff) ───
    if aircraft is None:
        now = time.time()
        with _BACKOFF["lock"]:
            backoff_until = _BACKOFF["until"]
        if now < backoff_until:
            tried.append({"upstream": "opensky", "ok": False,
                          "reason": f"backoff_active({int(backoff_until - now)}s)"})
        else:
            lamin, lomin, lamax, lomax = _bbox_from_point(
                lat, lon, min(radius, _AREA_RADIUS_CAP_NM))
            # OpenSky kann keinen Dateline-Wrap (lomin>lomax) — auf die alte,
            # am ±180° geklemmte Box zurückfallen (Seite mit dem Zentrum; B10).
            if lomin > lomax:
                if lomax < lon:
                    lomax = 180.0
                if lomin > lon:
                    lomin = -180.0
            try:
                states, _rem = _fetch_opensky_bbox(lamin, lomin, lamax, lomax)
                aircraft = []
                for s in states:
                    row = _normalize_opensky_state(s)
                    if row is not None:
                        aircraft.append(row)
                source = "opensky"
                tried.append({"upstream": "opensky", "ok": True})
            except _OpenSkyRateLimit as e:
                with _BACKOFF["lock"]:
                    _BACKOFF["until"] = time.time() + e.retry_after
                tried.append({"upstream": "opensky", "ok": False,
                              "reason": f"rate_limited({e.retry_after}s)"})
            except _OpenSkyError as e:
                tried.append({"upstream": "opensky", "ok": False, "reason": str(e)[:80]})

    # ─── Satelliten-Fallback (Ozean/China/remote) ───
    # adsb.lol/OpenSky sind über Ozean/China leer (kein Bodenempfänger) → sie geben
    # eine LEERE Liste zurück (kein Fehler). FR24 gRPC füllt genau diese Löcher via
    # Aireon-Satellit/Partner-Feeds. Nur beim Rauszoomen (Übersicht) und nur wenn
    # bisher NICHTS da ist (None ODER leer) — normaler Land-Traffic bleibt adsb.lol.
    if (not aircraft) and radius >= _AREA_FR24_MIN_RADIUS_NM:
        try:
            g_ac = _area_from_fr24_grpc(lat, lon, radius)
        except Exception as e:
            g_ac = None
            tried.append({"upstream": "fr24_grpc", "ok": False, "reason": str(e)[:80]})
        if g_ac:
            aircraft = g_ac
            source = "fr24_grpc"
            tried.append({"upstream": "fr24_grpc", "ok": True, "count": len(g_ac)})

    if aircraft is None:
        return jsonify({"ok": False, "error": "all_upstreams_failed",
                        "lat": lat, "lon": lon, "radius_nm": radius,
                        "tried": tried}), 502

    # SELF-COMPUTED ROUTES (frei): frische Live-Rows (Cache-Miss) in die Route-
    # Engine geben → jeder Karten-Pan eines Users füllt die eigene Routen-DB.
    try:
        from blueprints.aerox_data_blueprint import observe_adsb_positions
        observe_adsb_positions(aircraft)
    except Exception:
        pass

    payload = {
        "ok": True,
        "count": len(aircraft),
        "aircraft": aircraft,
        "lat": lat, "lon": lon, "radius_nm": radius,
        "source": source,
        "cached": False,
        "tried": tried,
    }
    _area_cache_put(ck, payload)
    _area_tile_cache_put(tk, payload)
    return _area_response(payload)


@adsb_bp.route('/api/adsb/alerts', methods=['GET'])
def get_adsb_alerts():
    """Emergency-/Special-Squawks (+ optional Military), gemerged & normalisiert.

    Quelle: adsb.lol /v2/squawk/{7700,7600,7500} (+ /v2/mil wenn ?mil=1),
    Community-API → cache 30s. Jede Row bekommt `alert_type`:
      7700 → emergency, 7600 → radio, 7500 → hijack, mil → military.

    200: {ok, count, aircraft:[{...row..., alert_type}], cached}
    """
    if _rate_limited(ip=_req_ip(request), endpoint='adsb_alerts', limit=60, window_sec=60):
        return jsonify({"ok": False, "error": "rate_limited"}), 429

    include_mil = (request.args.get('mil') or '').strip() in ('1', 'true', 'yes')
    ck = f"alerts:{1 if include_mil else 0}"
    cached = _area_cache_get(ck, _ALERTS_TTL_SECONDS)
    if cached is not None:
        out = dict(cached)
        out["cached"] = True
        return jsonify(out), 200

    sources = [
        ("/v2/squawk/7700", "emergency"),
        ("/v2/squawk/7600", "radio"),
        ("/v2/squawk/7500", "hijack"),
    ]
    if include_mil:
        sources.append(("/v2/mil", "military"))

    merged = {}          # hex (oder synthetischer Key) -> row
    tried = []
    any_ok = False
    for path, alert_type in sources:
        url = f"{ADSB_LOL_BASE}{path}"
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT, "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=ADSB_LOL_AREA_TIMEOUT) as resp:
                obj = json.loads(resp.read())
            any_ok = True
            tried.append({"path": path, "ok": True})
        except Exception as e:
            tried.append({"path": path, "ok": False, "reason": f"{type(e).__name__}"})
            continue
        for ac in (obj.get("ac") or []):
            row = _normalize_adsb_lol_ac(ac)
            if row is None:
                continue
            row["alert_type"] = alert_type
            # Dedupe per Hex; emergency hat Vorrang vor military, wenn ein Flieger
            # in mehreren Listen auftaucht (Reihenfolge der `sources`-Liste).
            key = row.get("hex") or f"{row.get('flight')}:{row.get('lat')}:{row.get('lon')}"
            if key not in merged:
                merged[key] = row

    if not any_ok:
        return jsonify({"ok": False, "error": "all_upstreams_failed",
                        "tried": tried}), 502

    aircraft = list(merged.values())
    payload = {
        "ok": True,
        "count": len(aircraft),
        "aircraft": aircraft,
        "source": "adsb.lol",
        "cached": False,
        "tried": tried,
    }
    _area_cache_put(ck, payload)
    return jsonify(payload), 200


# ── Route-Resolve: callsign → origin/destination airport ─────────────────────
#
# Zwei Quellen:
#   1) adsb.lol /api/0/routeset (POST {planes:[{callsign,lat,lng}]}) — liefert
#      _airports[]-Liste (erstes = origin, letztes = destination) mit
#      iata/icao/lat/lon. lat/lng des Planes verbessert die Disambiguierung bei
#      mehrdeutigen Callsigns; wir senden 0/0 wenn unbekannt.
#   2) hexdb.io /api/v1/route/iata/<callsign> — Fallback, liefert "route":"FRA-JFK".
#      hexdb kennt aber nur Codes, keine Koordinaten → wir reichern lat/lon aus
#      der eingebauten _AIRPORTS-Map an (best-effort, kann None bleiben).


def _airport_obj_from_adsb_lol(a):
    """adsb.lol routeset-_airports-Eintrag → {iata,icao,lat,lon}. None bei Müll."""
    if not isinstance(a, dict):
        return None
    iata = (a.get("iata") or "").strip().upper() or None
    icao = (a.get("icao") or "").strip().upper() or None
    if not iata and not icao:
        return None
    return {
        "iata": iata,
        "icao": icao,
        "lat": _coerce_float(a.get("lat")),
        "lon": _coerce_float(a.get("lon")),
    }


def _resolve_route_adsb_lol(callsign, lat, lng):
    """adsb.lol /api/0/routeset POST-Batch. Liefert (origin, destination) als
    Dicts oder (None, None). Wirft NIE — Fehler → (None, None)."""
    body = json.dumps({"planes": [{
        "callsign": callsign,
        "lat": lat if lat is not None else 0,
        "lng": lng if lng is not None else 0,
    }]}).encode("utf-8")
    req = urllib.request.Request(
        f"{ADSB_LOL_BASE}/api/0/routeset", data=body, method="POST",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
    try:
        with urllib.request.urlopen(req, timeout=ADSB_LOL_AREA_TIMEOUT) as resp:
            obj = json.loads(resp.read())
    except Exception:
        return None, None
    # routeset liefert eine Liste pro Plane; jedes Element hat _airports[].
    entries = obj if isinstance(obj, list) else obj.get("planes") or []
    if not entries:
        return None, None
    first = entries[0] if isinstance(entries[0], dict) else {}
    airports = first.get("_airports") or []
    if not isinstance(airports, list) or len(airports) < 2:
        return None, None
    origin = _airport_obj_from_adsb_lol(airports[0])
    destination = _airport_obj_from_adsb_lol(airports[-1])
    return origin, destination


def _resolve_route_hexdb(callsign):
    """hexdb.io Fallback: /api/v1/route/iata/<callsign> → "route":"FRA-JFK".
    lat/lon werden aus der eingebauten _AIRPORTS-Map angereichert (IATA-keyed).
    Liefert (origin, destination) oder (None, None). Wirft NIE."""
    safe = urllib.parse.quote(callsign, safe="")
    url = f"https://hexdb.io/api/v1/route/iata/{safe}"
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=HEXDB_TIMEOUT) as resp:
            obj = json.loads(resp.read())
    except Exception:
        return None, None
    route = (obj.get("route") or "").strip().upper()
    if not route or "-" not in route:
        return None, None
    parts = [p.strip() for p in route.split("-") if p.strip()]
    if len(parts) < 2:
        return None, None
    dep_iata, arr_iata = parts[0], parts[-1]

    def _mk(iata):
        coord = _AIRPORTS.get(iata)
        return {
            "iata": iata,
            "icao": None,
            "lat": coord[0] if coord else None,
            "lon": coord[1] if coord else None,
        }
    return _mk(dep_iata), _mk(arr_iata)


@adsb_bp.route('/api/adsb/route-info', methods=['GET'])
def get_route_info():
    """Resolve callsign → origin/destination airport (zum Zeichnen der Linie).

    Query: callsign= (required), optional lat= lng= (verbessert Disambiguierung).
    Quelle: adsb.lol /api/0/routeset → Fallback hexdb.io /api/v1/route/iata.
    Cache: 6h (Routen sind statisch).

    200 (Treffer): {ok, callsign, origin:{iata,icao,lat,lon}, destination:{...}, source}
    200 (kein Treffer): {ok:false, callsign, reason:"no_route_data"}
    """
    if _rate_limited(ip=_req_ip(request), endpoint='adsb_route_info', limit=120, window_sec=60):
        return jsonify({"ok": False, "error": "rate_limited"}), 429

    callsign = (request.args.get('callsign') or '').strip().upper()
    if not callsign:
        return jsonify({"ok": False, "error": "missing callsign"}), 400
    lat = _coerce_float(request.args.get('lat'))
    lng = _coerce_float(request.args.get('lng') or request.args.get('lon'))

    ck = f"route:{callsign}"
    cached = _area_cache_get(ck, _ROUTEINFO_TTL_SECONDS)
    if cached is not None:
        out = dict(cached)
        out["cached"] = True
        return jsonify(out), 200

    source = None
    origin, destination = _resolve_route_adsb_lol(callsign, lat, lng)
    if origin and destination:
        source = "adsb.lol"
    else:
        origin, destination = _resolve_route_hexdb(callsign)
        if origin and destination:
            source = "hexdb.io"

    if not (origin and destination):
        # Kein Cache für Misses — Routen können nachträglich in den DBs auftauchen.
        return jsonify({"ok": False, "callsign": callsign,
                        "reason": "no_route_data"}), 200

    payload = {
        "ok": True,
        "callsign": callsign,
        "origin": origin,
        "destination": destination,
        "source": source,
        "cached": False,
    }
    _area_cache_put(ck, payload)
    return jsonify(payload), 200
