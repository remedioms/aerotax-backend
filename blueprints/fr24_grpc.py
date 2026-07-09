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
# Provider, die schon einen ECHTEN eigenen Transport haben. Alles andere ist
# Stub → _client_for liefert einen identischen direct-Client.
_WIRED_PROVIDERS = frozenset({"direct"})


def _providers():
    raw = os.environ.get("FR24_GRPC_PROVIDERS", "direct")
    # Dedupe auf EFFEKTIVEN Client: solange Nicht-direct-Provider Stubs sind
    # (identische FR24()-Instanz), würde der Failover sinnlos denselben Egress
    # mehrfach abfragen — und dabei Token/Timeout-Budget verbrennen.
    seen, out = set(), []
    for p in (x.strip() for x in raw.split(",") if x.strip()):
        eff = p if p in _WIRED_PROVIDERS else "direct"
        if eff in seen:
            continue
        seen.add(eff)
        out.append(p)
    return out


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


def _gc_sample(lat1, lon1, lat2, lon2, count=16):
    """`count` Punkte auf dem Großkreis (slerp, inkl. Endpunkte) als [(lat,lon)].
    Mathe wie adsb_blueprint._great_circle_points — dort nicht importierbar, ohne
    den ganzen Blueprint (Flask/app-Abhängigkeiten) zu laden."""
    import math
    r_lat1, r_lon1 = math.radians(lat1), math.radians(lon1)
    r_lat2, r_lon2 = math.radians(lat2), math.radians(lon2)
    d = 2 * math.asin(math.sqrt(
        math.sin((r_lat1 - r_lat2) / 2) ** 2 +
        math.cos(r_lat1) * math.cos(r_lat2) *
        math.sin((r_lon1 - r_lon2) / 2) ** 2))
    if d < 1e-9:
        return [(lat1, lon1), (lat2, lon2)]
    out = []
    for i in range(count):
        f = i / (count - 1)
        a = math.sin((1 - f) * d) / math.sin(d)
        b = math.sin(f * d) / math.sin(d)
        x = a * math.cos(r_lat1) * math.cos(r_lon1) + b * math.cos(r_lat2) * math.cos(r_lon2)
        y = a * math.cos(r_lat1) * math.sin(r_lon1) + b * math.cos(r_lat2) * math.sin(r_lon2)
        z = a * math.sin(r_lat1) + b * math.sin(r_lat2)
        out.append((math.degrees(math.atan2(z, math.sqrt(x * x + y * y))),
                    math.degrees(math.atan2(y, x))))
    return out


def _split_antimeridian(s, n, w, e):
    """w/e nach [-180,180] normalisieren; schneidet die Box die Datumsgrenze,
    zweiteilen (BoundingBox erwartet west<=east)."""
    def _norm(x):
        while x > 180.0:
            x -= 360.0
        while x < -180.0:
            x += 360.0
        return x
    if e - w >= 360.0:  # defensiv: Voll-Umrundung
        return [(s, n, -180.0, 180.0)]
    w2, e2 = _norm(w), _norm(e)
    if w2 <= e2:
        return [(s, n, w2, e2)]
    return [(s, n, w2, 180.0), (s, n, -180.0, e2)]


def _corridor_boxes(from_lat, from_lon, to_lat, to_lon, margin, max_boxes=3):
    """Suchboxen ENTLANG des Großkreises from→to als [(s,n,w,e)].

    Das alte Endpunkt-Rechteck (min/max der beiden Endpunkte + margin) verlor
    Großkreis-Routen: FRA→HND fliegt die Nordroute über Sibirien und kulminiert
    bei ~66°N — weit über max(from_lat,to_lat)+margin. Deshalb: Großkreis
    sampeln und die Box aus min/max ALLER Sample-Punkte bauen. Sehr breite
    Korridore (>60° Lon) werden in 2-3 Teil-Boxen entlang der Route gesplittet
    — das entschärft zugleich die fetch-limit-1500-Trunkierung über dichten
    Regionen. Lons werden relativ zum Start entrollt (kein ±180-Sprung), Boxen
    über der Datumsgrenze zweigeteilt."""
    pts = _gc_sample(from_lat, from_lon, to_lat, to_lon, count=16)
    unwrapped, prev = [], None
    for la, lo in pts:
        if prev is not None:
            while lo - prev > 180.0:
                lo -= 360.0
            while lo - prev < -180.0:
                lo += 360.0
        unwrapped.append((la, lo))
        prev = lo
    span = max(lo for _, lo in unwrapped) - min(lo for _, lo in unwrapped)
    n_seg = 1 if span <= 60.0 else min(max_boxes, 2 if span <= 120.0 else 3)
    boxes = []
    per = (len(unwrapped) - 1) / n_seg
    for i in range(n_seg):
        # Zusammenhängende Stücke mit 1 Punkt Überlappung — kein margin-loses
        # Loch an den Nahtstellen.
        seg = unwrapped[int(round(i * per)):int(round((i + 1) * per)) + 1]
        s = max(-89.9, min(la for la, _ in seg) - margin)
        n = min(89.9, max(la for la, _ in seg) + margin)
        w = min(lo for _, lo in seg) - margin
        e = max(lo for _, lo in seg) + margin
        boxes.extend(_split_antimeridian(s, n, w, e))
    return boxes


def _match_in_rows(rows, cs, rg):
    """Ziel-Flug in live_feed-Rows: erst callsign, dann reg."""
    if cs:
        for r in rows:
            if _norm_cs(r.get("callsign")) == cs:
                return r
    if rg:
        for r in rows:
            xi = r.get("extra_info") or {}
            if (xi.get("reg") or "").strip().upper().replace("-", "") == rg:
                return r
    return None


async def _corridor_detail_async(provider, boxes, cs, rg):
    """live_feed über Teil-Boxen ENTLANG des Großkreises from→to + flight_details
    des Treffers. Findet den Flieger AUCH über Russland/Ozean OHNE Vorab-Position.
    Die erste Box ist über das _allow_call des Aufrufers bezahlt; jede weitere
    zieht ein eigenes Token (kein Salven-Bypass am Bucket vorbei)."""
    from fr24 import FR24, BoundingBox  # noqa: F811
    async with _client_for(provider) as f:
        match = None
        for idx, (s, n, w, e) in enumerate(boxes):
            if idx > 0 and not _allow_call():
                break
            box = BoundingBox(north=n, south=s, west=w, east=e)
            res = await asyncio.wait_for(f.live_feed.fetch(box, limit=1500), timeout=_TIMEOUT_S)
            match = _match_in_rows(res.to_dict().get("flights_list") or [], cs, rg)
            if match is not None:
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
    (z.B. FRA→HND), fragen fr24 mit 1-3 Boxen ENTLANG des Großkreises (+margin) und
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
    # P2-12: Boxen ENTLANG des Großkreises statt Endpunkt-Rechteck — sonst liegt
    # die Nordroute (FRA→HND kulminiert ~66°N) AUSSERHALB der Suchbox.
    boxes = _corridor_boxes(from_lat, from_lon, to_lat, to_lon, margin)
    for provider in _providers():
        try:
            td = _run(_corridor_detail_async(provider, boxes, cs, rg))
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
