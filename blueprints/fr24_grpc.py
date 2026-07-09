"""FR24 gRPC — per-flight Route/Detail-Resolver (anonym, gratis).

Nutzt den gRPC-LiveFeed (data-feed.flightradar24.com, hinter AWS-ELB, NICHT der
Cloudflare-gewallte feed.js-Pfad). Verifiziert 2026-07-07: läuft anonym aus
Google-DC-IP (Cloud Run) UND Residential — der DC-ASN-Block, der feed.js killt,
greift auf dem gRPC-Pfad nicht.

Zweck (Increment #31): per-flight Route-Autorität. Die Route (IATA from/to) kommt
aus live_feed.extra_info.route DES AKTUELLEN FLUGS — nicht aus einer statischen
Callsign->Route-Tabelle (adsbdb/hexdb, deaktiviert, „falsches Leg"). Verifiziert
10/10 korrekt inkl. Ozean + reused-Callsigns.

RESILIENZ: Provider-abstrahiert — derselbe Fetch läuft über austauschbare Egress-
Punkte (direct=Cloud-Run-DC verifiziert; später cloudflare-worker / nas-relay /
oracle-vm), damit ein einzelner geflaggter Pfad nie alles lahmlegt. httpx-Transport
ist der Seam: Default=direct; alternative Provider re-routen die identischen
gRPC-Bytes über einen Byte-Proxy und lassen die fr24-Lib normal dekodieren.

Sync-Fassade: der Flask/gunicorn-Worker ist sync → asyncio.run() pro Aufruf. Für
on-demand Einzel-Lookups (Tap/Route, niedrige Frequenz) ok; kein 24/7-Sweep.
"""
import os
import asyncio
import logging
import threading

log = logging.getLogger("fr24_grpc")

# Kill-switch (Muster wie FR24_BACKEND_SELFHARVEST). Default AN, da verifiziert.
_ENABLED = os.environ.get("FR24_GRPC_ENABLED", "1") not in ("0", "false", "False", "")
# Box-Halbkante um eine Live-Position (Grad). ~0.4° ≈ 44 km — fängt den Flieger
# auch bei leichter Feed-Staleness / Box-Rand.
_BOX_HALF = float(os.environ.get("FR24_GRPC_BOX_HALF", "0.4"))
# Harte Obergrenze pro Fetch — der gRPC-Call sitzt im Resolver-Hot-Path
# (route_for_flight): ein hängender Upstream darf NIE einen Request stallen.
_TIMEOUT_S = float(os.environ.get("FR24_GRPC_TIMEOUT_S", "8"))

_lib_ok = None


def available():
    """True wenn die fr24-Lib importierbar & Feature aktiv ist (memoisiert)."""
    global _lib_ok
    if not _ENABLED:
        return False
    if _lib_ok is None:
        try:
            import fr24  # noqa: F401
            _lib_ok = True
        except Exception as e:  # pragma: no cover
            log.warning("fr24-Lib nicht verfügbar: %s", e)
            _lib_ok = False
    return _lib_ok


# ── Skalierungs-Schutz: Token-Bucket + Circuit-Breaker (pro Prozess) ──────────
# Kappt den FR24-gRPC-Fußabdruck UNSERER EINEN IP unabhängig von der Nutzerzahl
# („mehr User ⇒ höheres Flag-Risiko"): max N Calls/Min; nach K aufeinanderfolgenden
# Leer-Antworten (Soft-Block-Verdacht) Freeze → Resolver fällt sauber auf ADS-B/
# Cache zurück, keine Salve gegen FR24. Pro gunicorn-Worker (Prozess) — Gesamt-Cap
# = Worker × Limit, weiterhin beschränkt und far unter jeder Flag-Schwelle.
import threading
_RATE_LOCK = threading.Lock()
_MAX_PER_MIN = int(os.environ.get("FR24_GRPC_MAX_PER_MIN", "90"))
_FREEZE_S = float(os.environ.get("FR24_GRPC_FREEZE_S", "300"))
_EMPTY_TRIP = int(os.environ.get("FR24_GRPC_EMPTY_TRIP", "8"))
_rate = None  # lazy init (kein time.time() bei Import)


def _allow_call():
    """False → Aufrufer überspringt fr24_grpc (Fallback ADS-B/Cache)."""
    import time as _t
    global _rate
    now = _t.time()
    with _RATE_LOCK:
        if _rate is None:
            _rate = {"tokens": float(_MAX_PER_MIN), "last": now,
                     "frozen_until": 0.0, "empties": 0}
        if now < _rate["frozen_until"]:
            return False
        _rate["tokens"] = min(_MAX_PER_MIN,
                              _rate["tokens"] + (now - _rate["last"]) * (_MAX_PER_MIN / 60.0))
        _rate["last"] = now
        if _rate["tokens"] < 1.0:
            return False
        _rate["tokens"] -= 1.0
        return True


def _note_result(got_data):
    """Circuit-Breaker: K Leer-Antworten in Folge ⇒ Freeze (Soft-Block-Schutz)."""
    import time as _t
    with _RATE_LOCK:
        if _rate is None:
            return
        if got_data:
            _rate["empties"] = 0
        else:
            _rate["empties"] += 1
            if _rate["empties"] >= _EMPTY_TRIP:
                _rate["frozen_until"] = _t.time() + _FREEZE_S
                _rate["empties"] = 0
                log.warning("fr24_grpc circuit-breaker: %s Leer-Antworten → Freeze %ss",
                            _EMPTY_TRIP, _FREEZE_S)


# ── Provider-Registry (Egress-Diversität) ─────────────────────────────────────
# v1: nur 'direct' (eigene Prozess-IP = Cloud-Run-DC, verifiziert). Weitere
# Provider (cloudflare/nas/oracle) hängen sich als custom httpx-Transport hier
# ein und re-routen dieselben gRPC-Bytes. Reihenfolge = Failover-Reihenfolge.
def _providers():
    raw = os.environ.get("FR24_GRPC_PROVIDERS", "direct")
    return [p.strip() for p in raw.split(",") if p.strip()]


def _client_for(provider):
    """Erzeugt eine FR24-Instanz für den Provider. 'direct' = Default-Transport.
    Andere Provider setzen einen custom httpx-Transport (Byte-Proxy) — Stub bis
    der jeweilige Relay/Worker deployt ist."""
    from fr24 import FR24
    if provider == "direct":
        return FR24()
    # TODO(resilienz): cloudflare-worker / nas-relay / oracle-vm Transport.
    # Bis dahin sauf direct zurückfallen, damit nie ein toter Provider blockt.
    log.debug("Provider %s noch nicht verdrahtet → direct", provider)
    return FR24()


# ── Async-Kern ────────────────────────────────────────────────────────────────
def _norm_cs(s):
    return (s or "").strip().upper().replace(" ", "")


async def _corridor_detail_async(provider, s, n, w, e, cs, rg):
    """live_feed über einer KORRIDOR-Box (Großkreis from→to) + flight_details des
    Treffers. Findet den Flieger AUCH über Russland/Ozean OHNE Vorab-Position."""
    from fr24 import FR24, BoundingBox  # noqa: F811
    box = BoundingBox(north=n, south=s, west=w, east=e)
    async with _client_for(provider) as f:
        res = await asyncio.wait_for(f.live_feed.fetch(box, limit=1500), timeout=_TIMEOUT_S)
        rows = res.to_dict().get("flights_list") or []
        match = None
        if cs:
            for r in rows:
                if _norm_cs(r.get("callsign")) == cs:
                    match = r
                    break
        if match is None and rg:
            for r in rows:
                xi = r.get("extra_info") or {}
                if (xi.get("reg") or "").strip().upper().replace("-", "") == rg:
                    match = r
                    break
        if match is None:
            return None
        detail = None
        try:
            det = await asyncio.wait_for(
                f.flight_details.fetch(flight_id=match.get("flightid")), timeout=_TIMEOUT_S)
            detail = det.to_dict()
        except Exception:
            detail = None
        return {"row": match, "detail": detail}


def inbound_by_route(from_lat, from_lon, to_lat, to_lon, callsign=None, reg=None,
                     margin=6.0):
    """Owner-Durchbruch 2026-07-08 („wir haben doch fr24, das über Russland
    liefert"): die Maschine liegt auf dem Großkreis from→to. Wir kennen die Route
    (z.B. FRA→HND), fragen fr24 mit einer Box ENTLANG des Korridors (+margin) und
    filtern per callsign/reg — findet den Flieger AUCH über Russland/Ozean OHNE
    Vorab-Position, PLUS flight_details (echte sched_arr/eta). Rückgabe:
    {lat,lon,track,alt,speed,route_from,route_to,sched_dep,sched_arr,eta,
    flight_stage,reg,callsign,flight_id} | None."""
    if not available() or None in (from_lat, from_lon, to_lat, to_lon):
        return None
    if not _allow_call():
        return None
    cs = _norm_cs(callsign)
    rg = (reg or "").strip().upper().replace("-", "") or None
    s = min(from_lat, to_lat) - margin
    n = max(from_lat, to_lat) + margin
    w = min(from_lon, to_lon) - margin
    e = max(from_lon, to_lon) + margin
    for provider in _providers():
        try:
            td = _run(_corridor_detail_async(provider, s, n, w, e, cs, rg))
        except Exception as ex:
            log.warning("fr24 corridor provider=%s: %s", provider, ex)
            td = None
        if td and td.get("row"):
            row = td["row"]
            xi = row.get("extra_info") or {}
            route = xi.get("route") or {}
            d = td.get("detail") or {}
            si = d.get("schedule_info") or {}
            fp = d.get("flight_progress") or {}
            _note_result(True)
            return {
                "lat": row.get("lat"), "lon": row.get("lon"),
                "track": row.get("track"), "alt": row.get("alt"),
                "speed": row.get("speed"),
                "route_from": (route.get("from") or "").strip().upper() or None,
                "route_to": (route.get("to") or "").strip().upper() or None,
                "sched_dep": si.get("scheduled_departure"),
                "sched_arr": si.get("scheduled_arrival"),
                "eta": fp.get("eta"),
                "flight_stage": fp.get("flight_stage"),
                "reg": (xi.get("reg") or "").strip().upper() or None,
                "callsign": _norm_cs(row.get("callsign")) or None,
                "flight_id": row.get("flightid"),
                "source": "fr24_grpc_corridor",
            }
    _note_result(False)
    return None


async def _livefeed_row_async(provider, callsign, reg, lat, lon):
    """Ein live_feed-Row DES gesuchten Fluges (Match per callsign, sonst reg)."""
    from fr24 import FR24, BoundingBox  # noqa: F811
    cs = _norm_cs(callsign)
    rg = (reg or "").strip().upper() or None
    if lat is None or lon is None:
        return None
    box = BoundingBox(north=lat + _BOX_HALF, south=lat - _BOX_HALF,
                      west=lon - _BOX_HALF, east=lon + _BOX_HALF)
    async with _client_for(provider) as f:
        res = await asyncio.wait_for(f.live_feed.fetch(box, limit=1500), timeout=_TIMEOUT_S)
        rows = res.to_dict().get("flights_list") or []
    match = None
    for r in rows:
        if cs and _norm_cs(r.get("callsign")) == cs:
            match = r
            break
    if match is None and rg:
        for r in rows:
            xi = r.get("extra_info") or {}
            if (xi.get("reg") or "").strip().upper() == rg:
                match = r
                break
    return match


def _row_to_route(row):
    if not row:
        return None
    xi = row.get("extra_info") or {}
    route = xi.get("route") or {}
    src = (route.get("from") or "").strip().upper() or None
    dst = (route.get("to") or "").strip().upper() or None
    if not (src or dst):
        return None
    return {
        "src": src, "dst": dst,
        "reg": (xi.get("reg") or "").strip().upper() or None,
        "flight_number": (xi.get("flight") or "").strip().upper() or None,
        "ac_type": (xi.get("type") or "").strip().upper() or None,
        "flightid": row.get("flightid"),
        "source": "fr24_grpc", "confidence": "confirmed", "_from": "fr24_grpc",
    }


# ── Sync-Fassade mit Provider-Failover ────────────────────────────────────────
def _run(coro):
    """Async-Coro aus sync-Kontext. Nutzt einen dedizierten Loop-Thread, falls
    (unerwartet) schon ein Loop läuft; sonst asyncio.run()."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Fallback: eigener Thread mit eigenem Loop (defensiv).
    box = {}
    def worker():
        box["v"] = asyncio.run(coro)
    t = threading.Thread(target=worker, daemon=True)
    t.start(); t.join(timeout=25)
    return box.get("v")


def resolve_route_live(callsign=None, hex=None, reg=None, lat=None, lon=None):
    """Per-flight Route (from/to IATA) für einen Live-Flug (Position vorhanden).
    Rückgabe: route-Dict (src/dst/reg/flight_number/confidence='confirmed') oder
    None. Failover über alle konfigurierten Provider."""
    if not available() or lat is None or lon is None:
        return None
    if not _allow_call():
        return None
    for provider in _providers():
        try:
            row = _run(_livefeed_row_async(provider, callsign, reg, lat, lon))
        except Exception as e:
            log.warning("fr24_grpc route provider=%s fehlgeschlagen: %s", provider, e)
            row = None
        route = _row_to_route(row)
        if route:
            _note_result(True)
            return route
    _note_result(False)
    return None


# ── Reiche Tap-Detail (für Increment #32) ─────────────────────────────────────
async def _tap_detail_async(provider, callsign, reg, lat, lon):
    from fr24 import FR24  # noqa: F811
    row = await _livefeed_row_async(provider, callsign, reg, lat, lon)
    if not row:
        return None
    fid = row.get("flightid")
    detail = None
    async with _client_for(provider) as f:
        try:
            det = await asyncio.wait_for(f.flight_details.fetch(flight_id=fid), timeout=_TIMEOUT_S)
            detail = det.to_dict()
        except Exception as e:
            log.debug("flight_details fehlgeschlagen (%s) — nur live_feed", e)
    return {"row": row, "detail": detail}


def tap_detail(callsign=None, hex=None, reg=None, lat=None, lon=None):
    """Rohdaten für die reiche Karte: {row, detail}. Normalisierung → #32."""
    if not available() or lat is None or lon is None:
        return None
    if not _allow_call():
        return None
    for provider in _providers():
        try:
            out = _run(_tap_detail_async(provider, callsign, reg, lat, lon))
        except Exception as e:
            log.warning("fr24_grpc detail provider=%s fehlgeschlagen: %s", provider, e)
            out = None
        if out:
            _note_result(True)
            return out
    _note_result(False)
    return None


def detail_card(callsign=None, hex=None, reg=None, lat=None, lon=None):
    """Normalisierte REICHE Karte für den Live-Map-Tap (Increment #32): ETA,
    Progress, Delay-Ampel (GREEN/YELLOW/RED), Muster-Langname, Airline, Foto,
    sched/actual-Zeiten, Gate/Terminal. Nur echte Felder — None wird entfernt."""
    td = tap_detail(callsign=callsign, hex=hex, reg=reg, lat=lat, lon=lon)
    if not td:
        return None
    row = td.get("row") or {}
    d = td.get("detail") or {}
    xi = row.get("extra_info") or {}
    route = xi.get("route") or {}
    ai = d.get("aircraft_info") or {}
    si = d.get("schedule_info") or {}
    fp = d.get("flight_progress") or {}
    photo = None
    imgs = ai.get("images_list") or []
    if imgs:
        im = imgs[0]
        photo = {"thumb": im.get("thumbnail"), "medium": im.get("medium"),
                 "large": im.get("large"), "link": im.get("url"),
                 "copyright": im.get("copyright")}
        photo = {k: v for k, v in photo.items() if v}
    card = {
        "flight_number": (si.get("flight_number") or xi.get("flight") or "").strip() or None,
        "reg": (ai.get("reg") or xi.get("reg") or "").strip().upper() or None,
        "ac_type": (ai.get("type") or xi.get("type") or "").strip().upper() or None,
        "ac_description": ai.get("full_description") or None,
        "airline": ai.get("registered_owners") or None,
        "route_from": (route.get("from") or "").strip().upper() or None,
        "route_to": (route.get("to") or "").strip().upper() or None,
        "sched_dep": si.get("scheduled_departure") or None,
        "actual_dep": si.get("actual_departure") or None,
        "sched_arr": si.get("scheduled_arrival") or None,
        "eta": fp.get("eta") or None,
        "progress_pct": fp.get("progress_pct"),
        "delay_status": fp.get("delay_status") or None,
        "flight_stage": fp.get("flight_stage") or None,
        "arr_gate": si.get("arr_gate") or None,
        "arr_terminal": si.get("arr_terminal") or None,
        "baggage_belt": si.get("baggage_belt") or None,
        "photo": photo,
        "source": "fr24_grpc",
    }
    return {k: v for k, v in card.items() if v is not None}


def flown_trail(callsign=None, hex=None, reg=None, lat=None, lon=None):
    """Die ECHTE jüngste geflogene Spur eines Flugs aus FR24-`flight_details`
    (`flight_trail_list`) — Punkte lat/lon/alt/gs/track/ts. Für /api/ax/flown-track
    Tier 2 (jede Airline, on-demand) + Rückschreibung in aircraft_track. Gratis/
    anonym (gleicher gRPC-Pfad wie tap_detail). None wenn keine Spur.

    Rückgabe: {reg, flight, origin, dest, points:[{lat,lon,alt_ft,gs_kt,track_deg,ts}]}."""
    td = tap_detail(callsign=callsign, hex=hex, reg=reg, lat=lat, lon=lon)
    if not td:
        return None
    d = td.get("detail") or {}
    trail = d.get("flight_trail_list") or []
    if not trail:
        return None
    row = td.get("row") or {}
    xi = row.get("extra_info") or {}
    route = xi.get("route") or {}

    def _i(v):
        try:
            return int(round(float(v)))
        except (TypeError, ValueError):
            return None

    pts = []
    for tp in trail:
        la, lo = tp.get("latitude"), tp.get("longitude")
        if la is None or lo is None:
            continue
        pts.append({
            "lat": la, "lon": lo,
            "alt_ft": _i(tp.get("altitude")),
            "gs_kt": _i(tp.get("ground_speed")),
            "track_deg": _i(tp.get("track")),
            "ts": tp.get("timestamp"),
        })
    if not pts:
        return None
    return {
        "reg": (str(xi.get("reg") or reg or "").strip().upper()) or None,
        "flight": (str(xi.get("flight") or "").strip().upper()) or None,
        "origin": (str(route.get("from") or "").strip().upper()) or None,
        "dest": (str(route.get("to") or "").strip().upper()) or None,
        "points": pts,
    }


# ── Area-Positionen für die Live-Map (Satelliten/Ozean-Füllung, Increment #29) ─
def _row_to_pos(row):
    """live_feed-Row → normalisierte Positions-Zeile für Map/Store."""
    if not row or row.get("lat") is None or row.get("lon") is None:
        return None
    xi = row.get("extra_info") or {}
    route = xi.get("route") or {}
    return {
        "callsign": _norm_cs(row.get("callsign")) or None,
        "reg": (xi.get("reg") or "").strip().upper() or None,
        "ac_type": (xi.get("type") or "").strip().upper() or None,
        "flight_number": (xi.get("flight") or "").strip().upper() or None,
        "lat": row.get("lat"), "lon": row.get("lon"),
        "track": row.get("track"), "alt": row.get("alt"), "speed": row.get("speed"),
        "route_from": (route.get("from") or "").strip().upper() or None,
        "route_to": (route.get("to") or "").strip().upper() or None,
        "flightid": row.get("flightid"),
        "obs_ts": row.get("timestamp"),
        "source": "fr24_grpc",
    }


async def _area_async(provider, north, south, west, east, limit):
    from fr24 import FR24, BoundingBox  # noqa: F811
    async with _client_for(provider) as f:
        res = await asyncio.wait_for(
            f.live_feed.fetch(
                BoundingBox(north=north, south=south, west=west, east=east), limit=limit),
            timeout=_TIMEOUT_S)
        return res.to_dict().get("flights_list") or []


def area(north, south, west, east, limit=1500):
    """Alle Flieger einer Bounding-Box als normalisierte Positionen — INKL. der
    Satelliten/Ozean-Zonen, die adsb.lol/fi physisch nicht sehen. Für die Live-Map
    und den verteilten Harvester (fr24_live-Store). Failover über Provider.

    Hinweis: Box möglichst ≤ ~10-25° Kantenlänge (dichte Regionen kappen bei
    limit=1500 → sonst gröber tilen). Ozean-Boxen sind dünn → eine reicht."""
    if not available():
        return []
    if not _allow_call():
        return []
    for provider in _providers():
        try:
            rows = _run(_area_async(provider, north, south, west, east, limit))
        except Exception as e:
            log.warning("fr24_grpc area provider=%s fehlgeschlagen: %s", provider, e)
            rows = None
        if rows:
            out = [p for p in (_row_to_pos(r) for r in rows) if p]
            if out:
                _note_result(True)
                return out
    _note_result(False)
    return []
