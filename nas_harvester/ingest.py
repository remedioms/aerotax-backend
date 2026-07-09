"""
fr24-ingest v2 — FR24-**gRPC**-Positions-Harvester für die Synology-NAS.

WARUM NEU (Owner 2026-07-08): die v1 nutzte den feed.js-HTTP-Endpoint — der wird
von FR24 auf der NAS-Residential-IP SOFT-GEBLOCKT (200 mit nur full_count, 0 ac →
Dauer-Freeze, 24 h lang 0 Zeilen). Der gRPC-LiveFeed (AWS-ELB) umgeht genau diesen
Block (derselbe Durchbruch wie im Backend, blueprints/fr24_grpc.py) und liefert
Position + Route + Reg + Typ pro Flug in einem Bulk-Call pro Kachel.

Aufgabe: round-robin über Korridor-Kacheln (Russland/Ozean-Löcher + Europa),
FR24-gRPC live_feed je Kachel, filter auf LH-Group + deutsche Carrier (Callsign-
Prefix), upsert last-known Position/Route pro Airframe → Supabase `aircraft_live`
(reg-keyed). RAM-only (tmpfs /tmp Heartbeat) — kein NAS-Disk-Write (HDDs schlafen);
der durable Store IST Supabase. Das Backend liest den freshesten Snapshot, wenn
freies ADS-B blind ist (über Russland/Ozean) → iOS simuliert von dort vorwärts.

Abhängigkeiten: fr24 (gRPC-Client) + httpx (Supabase-PostgREST). Alles via Env.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import re
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

log = logging.getLogger("fr24-ingest")


# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #
# Korridor-Kacheln (lat_n, lat_s, lon_w, lon_e). 0-7 = ADS-B-Coverage-Löcher
# (Russland/Sibirien/Ozeane/Naher Osten — dort ist freies ADS-B blind, FR24 die
# EINZIGE Quelle). 8-14 = Europa (dichter LH-Group-Verkehr, Route/Tail-Anreicherung).
_DEFAULT_TILES = [
    (55, 20, 55, 110), (72, 45, 55, 140), (55, 20, 110, 145), (45, 8, 30, 65),
    (35, -10, 60, 100), (72, 35, -60, -10), (60, 15, 140, 180), (40, -40, -25, 55),
    (60, 48, -11, 3), (52, 42, -3, 10), (56, 45, 9, 20), (72, 55, 4, 32),
    (45, 35, -10, 5), (47, 35, 6, 30), (52, 44, 20, 40),
]

# LH Group + deutsche Carrier (ICAO-3-Letter-Callsign-Prefix). Owner-Scope
# 2026-07-08 „+ Deutsche Carrier". Erweiterbar via CARRIER_PREFIXES-Env.
_DEFAULT_PREFIXES = {
    "DLH",  # Lufthansa
    "CLH",  # Lufthansa CityLine
    "GEC",  # Lufthansa Cargo
    "EWG",  # Eurowings
    "EWE",  # Eurowings Europe
    "OCN",  # Discover Airlines (ex Ocean)
    "AUA",  # Austrian
    "SWR",  # Swiss
    "EDW",  # Edelweiss
    "BEL",  # Brussels Airlines
    "DLA",  # Air Dolomiti
    "SXS",  # SunExpress (LH JV)
    "BOX",  # AeroLogic (LH/DHL)
    "CFG",  # Condor
    "TUI",  # TUIfly
    "TFL",  # (TUIfly hist.)
}


def _parse_tiles(raw: str) -> list[tuple]:
    raw = (raw or "all").strip().lower()
    if raw in ("all", "*", ""):
        return list(_DEFAULT_TILES)
    idxs = [int(x) for x in raw.replace(";", ",").split(",")
            if x.strip().isdigit() and 0 <= int(x) < len(_DEFAULT_TILES)]
    return [_DEFAULT_TILES[i] for i in idxs] or list(_DEFAULT_TILES)


def _parse_prefixes(raw: str) -> set:
    raw = (raw or "").strip().upper()
    if not raw:
        return set(_DEFAULT_PREFIXES)
    if raw in ("ALL", "*"):
        return set()   # leer = kein Filter (ALLE Carrier)
    return {p.strip() for p in raw.replace(";", ",").split(",") if p.strip()}


@dataclass(frozen=True)
class Settings:
    supabase_url: str = os.getenv("SUPABASE_URL", "https://jyrbijvmwacuivssbxlg.supabase.co")
    supabase_key: str = os.getenv("SUPABASE_KEY", "")
    table: str = os.getenv("TABLE", "aircraft_live")
    # WRITE_SUPABASE=0 → RAM-only (Backend liest via HTTP-Tunnel, keine SB-Writes).
    write_supabase: bool = os.getenv("WRITE_SUPABASE", "1") not in ("0", "false", "no", "")
    http_port: int = int(os.getenv("HTTP_PORT", "8787"))
    api_token: str = os.getenv("NAS_API_TOKEN", "")   # Bearer-Schutz des /pos-Endpoints

    tiles: list = field(default_factory=lambda: _parse_tiles(os.getenv("TILES", "all")))
    prefixes: set = field(default_factory=lambda: _parse_prefixes(os.getenv("CARRIER_PREFIXES", "")))

    # Kadenz: eine Kachel pro poll_interval. 60s × 15 ≈ 11 min/Voll-Sweep — der
    # gRPC-Endpoint ist NICHT geblockt, aber wir bleiben höflich/unauffällig.
    poll_interval: float = float(os.getenv("POLL_INTERVAL_S", "60"))
    fetch_limit: int = int(os.getenv("FETCH_LIMIT", "1500"))
    fetch_timeout: float = float(os.getenv("FETCH_TIMEOUT_S", "12"))

    # Prune: Snapshots älter als prune_age fliegen raus (der Flug ist längst gelandet).
    prune_interval: float = float(os.getenv("PRUNE_INTERVAL_S", "1800"))
    prune_age: float = float(os.getenv("PRUNE_AGE_S", "10800"))     # 3 h

    # Track-Breadcrumbs (Owner 2026-07-09): pro Poll je airborne, BEWEGtem Airframe
    # einen Punkt in aircraft_track anhängen → die ECHTE geflogene Route wächst
    # dauerhaft. Nur airborne + > track_min_nm seit letztem gespeicherten Punkt
    # (drosselt Volumen). aircraft_live (Snapshot) bleibt unberührt.
    track_enabled: bool = os.getenv("TRACK_ENABLED", "1") not in ("0", "false", "no", "")
    track_table: str = os.getenv("TRACK_TABLE", "aircraft_track")
    track_min_nm: float = float(os.getenv("TRACK_MIN_NM", "1.0"))
    # Zusätzliches Zeit-Gate: das 1-nm-Gate drosselt im Cruise (8 nm/min) nichts →
    # Punkt nur wenn seit dem letzten geschriebenen Punkt AUCH >= track_min_sec
    # vergangen sind (senkt die ~1 Mio Rows/Tag, Kurven bleiben bei ~2-min-Raster).
    track_min_sec: float = float(os.getenv("TRACK_MIN_SEC", "120"))
    # Track ALLE Airlines (Owner 2026-07-09 „jeden Flug speichern") — nicht nur die
    # LH-Group-Prefixe. Der Kachel-Zyklus (~15 min/Airframe) hält das Volumen grob;
    # Retention deckelt bei 10 Tagen. aircraft_live (Snapshot) bleibt LH-Group-only.
    track_all_carriers: bool = os.getenv("TRACK_ALL_CARRIERS", "1") not in ("0", "false", "no", "")

    backoff_base: float = float(os.getenv("BACKOFF_BASE_S", "30"))
    backoff_cap: float = float(os.getenv("BACKOFF_CAP_S", "900"))

    heartbeat_file: str = os.getenv("HEARTBEAT_FILE", "/tmp/heartbeat")
    summary_interval: float = float(os.getenv("SUMMARY_INTERVAL_S", "1800"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _n(v):
    try:
        f = float(v)
        return f if f == f else None   # NaN raus
    except (TypeError, ValueError):
        return None


def _norm_reg(reg) -> str | None:
    r = (str(reg or "").strip().upper().replace("-", "").replace(" ", ""))
    return r or None


def _dist_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Großkreis-Distanz in NM (Haversine) — für das Track-Bewegungs-Gate."""
    from math import radians, sin, cos, asin, sqrt
    rla1, rlo1, rla2, rlo2 = map(radians, (lat1, lon1, lat2, lon2))
    h = sin((rla2 - rla1) / 2) ** 2 + cos(rla1) * cos(rla2) * sin((rlo2 - rlo1) / 2) ** 2
    return 2 * 3440.065 * asin(min(1.0, sqrt(h)))   # Erdradius in NM


def _to_int(v):
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def decorrelated_jitter(prev: float, base: float, cap: float) -> float:
    return min(cap, random.uniform(base, max(base, prev * 3)))


# --------------------------------------------------------------------------- #
# FR24-gRPC-Tile-Fetch → normalisierte Snapshot-Zeilen
# --------------------------------------------------------------------------- #
async def _fetch_tile(fr24, tile: tuple, s: Settings) -> list[dict]:
    """Ein gRPC live_feed-Call über einer Kachel → Liste roher Flug-Dicts."""
    from fr24 import BoundingBox
    n, so, w, e = tile
    box = BoundingBox(north=float(n), south=float(so), west=float(w), east=float(e))
    res = await asyncio.wait_for(
        fr24.live_feed.fetch(box, limit=s.fetch_limit), timeout=s.fetch_timeout)
    d = res.to_dict()
    return d.get("flights_list") or d.get("flights") or []


def _flight_to_snapshot(fl: dict, prefixes: set) -> dict | None:
    """FR24-gRPC-Flug → aircraft_live-Zeile. None wenn kein Reg / kein Carrier-Match."""
    xi = fl.get("extra_info") or {}
    reg = _norm_reg(xi.get("reg"))
    if not reg:
        return None
    cs = (str(fl.get("callsign") or "").strip().upper()) or None
    # Carrier-Filter: leerer prefixes-Set = alle. Sonst Callsign-Prefix (3 Buchst.).
    if prefixes and (not cs or cs[:3] not in prefixes):
        return None
    route = xi.get("route") or {}
    lat, lon = _n(fl.get("lat")), _n(fl.get("lon"))
    if lat is None or lon is None:
        return None
    alt_ft = _n(fl.get("alt"))
    gs_kt = _n(fl.get("speed"))
    ts = _n(fl.get("timestamp"))
    seen_iso = (time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(ts)) if ts else None)
    # on_ground ehrlich (Owner 2026-07-09 „Tibor FRA→GVA an ~13:05", stand am
    # FRA-Rollweg): FR24 liefert beim Pushback/Taxi KEINE Baro-Höhe (alt=None) →
    # die alte Regel `alt<50` verfehlte das und markierte den rollenden Flieger als
    # fliegend. Ohne Höhe entscheidet die Geschwindigkeit: gs < 80 kt = am Boden
    # (nichts cruised so langsam; selbst Steigflug ist >150). Mit Höhe zählt <50 ft.
    _on_ground = bool(
        (alt_ft is not None and alt_ft < 50)
        or (alt_ft is None and (gs_kt is None or gs_kt < 80))
    )
    return {
        "reg": reg,
        "reg_display": (str(xi.get("reg") or "").strip().upper() or None),
        "callsign": cs,
        "flight": (str(xi.get("flight") or "").strip().upper() or None),
        "lat": lat, "lon": lon,
        "track": _n(fl.get("track")),
        "gs_kt": gs_kt,
        "alt_ft": alt_ft,
        "origin": (str(route.get("from") or "").strip().upper() or None),
        "dest": (str(route.get("to") or "").strip().upper() or None),
        "ac_type": (str(xi.get("type") or "").strip().upper() or None),
        "flightid": (int(fl["flightid"]) if str(fl.get("flightid") or "").isdigit() else None),
        "on_ground": _on_ground,
        "source": "fr24_grpc",
        "seen_ts": seen_iso,
    }


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
class Ingest:
    def __init__(self, s: Settings) -> None:
        self._s = s
        self._tile_idx = 0
        self._stop = asyncio.Event()
        self._win_rows = 0
        self._win_start = time.monotonic()
        self._last_prune = 0.0
        self._sb = httpx.AsyncClient(
            base_url=s.supabase_url,
            headers={"apikey": s.supabase_key,
                     "Authorization": f"Bearer {s.supabase_key}"},
            timeout=httpx.Timeout(15.0, connect=6.0),
            limits=httpx.Limits(max_connections=4, keepalive_expiry=120))
        # RAM-Store (Owner 2026-07-08 „NAS only RAM, HDDs still"): letzter Snapshot
        # pro Reg IM SPEICHER — kein NAS-Disk. Wird über einen winzigen HTTP-Endpoint
        # serviert (via bestehenden cloudflared-Tunnel), damit das Backend von hier
        # liest statt Supabase → spart Supabase-Disk-IO/Kosten. reg → (snap, mono_ts).
        self._latest: dict[str, tuple] = {}
        # Letzter GESPEICHERTER Track-Punkt pro Reg (lat, lon, mono_ts) — Zeit- und
        # Bewegungs-Gate, damit stehende/kaum-bewegte Airframes keine redundanten
        # Punkte anhängen. Wird erst NACH erfolgreichem Write aktualisiert.
        self._last_track: dict[str, tuple] = {}
        self._runner = None

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._sb.aclose()
        if self._runner is not None:
            with contextlib.suppress(Exception):
                await self._runner.cleanup()

    def request_stop(self) -> None:
        self._stop.set()

    async def _upsert(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        now_iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        for r in rows:
            r["updated_at"] = now_iso
        if not self._s.write_supabase:
            return 0                      # RAM-only-Modus: keine Supabase-Writes
        r = await self._sb.post(
            f"/rest/v1/{self._s.table}?on_conflict=reg",
            content=json.dumps(rows),
            headers={"Content-Type": "application/json",
                     "Prefer": "resolution=merge-duplicates,return=minimal"})
        r.raise_for_status()
        return len(rows)

    async def _append_track(self, rows: list[dict]) -> int:
        """Hängt pro airborne, BEWEGtem Airframe einen Breadcrumb an aircraft_track
        an (echte geflogene Route, append-only). Idempotent via PK (reg, seen_ts).
        Kein Write im RAM-only-Modus oder wenn track deaktiviert."""
        if not (self._s.track_enabled and self._s.write_supabase):
            return 0
        now_iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        now_mono = time.monotonic()
        pts = []
        pending: dict[str, tuple] = {}         # reg → (lat, lon, mono) NACH Write-Erfolg übernehmen
        for r in rows:
            if r.get("on_ground"):
                continue                       # nur Flug, keine Boden-/Parkpunkte
            lat, lon, reg = r.get("lat"), r.get("lon"), r.get("reg")
            if lat is None or lon is None or not reg:
                continue
            prev = self._last_track.get(reg)
            # Zeit-UND-Distanz-Gate: Punkt nur wenn >= track_min_sec vergangen UND
            # >= track_min_nm bewegt — Distanz allein drosselte im Cruise (8 nm/min)
            # nichts; das Zeit-Gate senkt die Schreiblast, Kurven bleiben erhalten.
            if prev is not None and (
                    (now_mono - prev[2]) < self._s.track_min_sec
                    or _dist_nm(prev[0], prev[1], lat, lon) < self._s.track_min_nm):
                continue                       # zu früh / zu nah am letzten Punkt → drosseln
            pts.append({
                "reg": reg,
                "seen_ts": r.get("seen_ts") or now_iso,
                "flight": r.get("flight"),
                "origin": r.get("origin"),
                "dest": r.get("dest"),
                "lat": lat, "lon": lon,
                "alt_ft": _to_int(r.get("alt_ft")),
                "gs_kt": _to_int(r.get("gs_kt")),
                "track_deg": _to_int(r.get("track")),
                "on_ground": False,
                "source": "fr24_grpc",
            })
            pending[reg] = (lat, lon, now_mono)
        if not pts:
            return 0
        try:
            resp = await self._sb.post(
                f"/rest/v1/{self._s.track_table}?on_conflict=reg,seen_ts",
                content=json.dumps(pts),
                headers={"Content-Type": "application/json",
                         "Prefer": "resolution=ignore-duplicates,return=minimal"})
            resp.raise_for_status()
            # Gate erst NACH Write-Erfolg fortschreiben — bei Fehler bleibt der alte
            # Stand, damit der nächste Poll den verlorenen Breadcrumb nachholen kann.
            self._last_track.update(pending)
            return len(pts)
        except Exception as ex:
            log.warning("track-append fehlgeschlagen (%d Punkte verworfen, Gate unverändert): %s",
                        len(pts), ex)
            return 0

    async def _prune(self) -> None:
        cutoff = time.strftime('%Y-%m-%dT%H:%M:%SZ',
                               time.gmtime(time.time() - self._s.prune_age))
        with contextlib.suppress(Exception):
            await self._sb.delete(f"/rest/v1/{self._s.table}?updated_at=lt.{cutoff}",
                                  headers={"Prefer": "return=minimal"})
        # RAM-Gate der Track-Bewegung mitprunen (verhindert unbegrenztes Wachstum
        # des Dicts über die Container-Laufzeit).
        for k in [k for k in self._last_track if k not in self._latest]:
            self._last_track.pop(k, None)

    # ── In-RAM HTTP-API (vom Backend via cloudflared-Tunnel gelesen) ──────────
    def _match(self, reg, flight, cs, dep, max_age):
        now = time.monotonic()
        def fresh(t): return (now - t) <= max_age
        # Route-Konsistenz PRO Tier (wie im Supabase-Pfad, aerox_data_blueprint):
        # ein Reg-Hit mit falschem dest (Swap-Maschine, anderer Leg) wird verworfen
        # und die flight-/callsign-Stufe darf noch matchen — nicht terminal abbrechen.
        def route_ok(snap): return not (dep and snap.get("dest") and snap["dest"] != dep)
        if reg:
            it = self._latest.get(reg)
            if it and fresh(it[1]) and route_ok(it[0]):
                return it[0]
        if flight:
            for snap, t in self._latest.values():
                if fresh(t) and (snap.get("flight") or "") == flight and route_ok(snap):
                    return snap
        if cs:
            for snap, t in self._latest.values():
                if fresh(t) and (snap.get("callsign") or "") == cs and route_ok(snap):
                    return snap
        return None

    async def _http_pos(self, request):
        from aiohttp import web
        if self._s.api_token:
            if request.headers.get("Authorization", "") != f"Bearer {self._s.api_token}":
                return web.json_response({"error": "unauthorized"}, status=401)
        q = request.rel_url.query
        reg = re.sub(r"[^A-Z0-9]", "", (q.get("reg") or "").upper())
        flight = (q.get("flight") or "").strip().upper()
        cs = (q.get("callsign") or "").strip().upper()
        dep = (q.get("dep") or "").strip().upper()
        try:
            max_age = float(q.get("max_age") or 2100)
        except ValueError:
            max_age = 2100
        m = self._match(reg, flight, cs, dep, max_age)
        if m is None:
            return web.json_response({"found": False}, status=404)
        return web.json_response({"found": True, "pos": m})

    async def _http_health(self, request):
        from aiohttp import web
        return web.json_response({"ok": True, "ram": len(self._latest),
                                  "write_supabase": self._s.write_supabase})

    async def _start_http(self):
        from aiohttp import web
        app = web.Application()
        app.router.add_get("/pos", self._http_pos)
        app.router.add_get("/health", self._http_health)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self._s.http_port)
        await site.start()
        log.info("HTTP-API auf :%d (RAM-Store, write_supabase=%s)",
                 self._s.http_port, self._s.write_supabase)

    async def _poll_once(self, fr24) -> None:
        idx = self._tile_idx
        self._tile_idx = (idx + 1) % len(self._s.tiles)
        tile = self._s.tiles[idx]
        flights = await _fetch_tile(fr24, tile, self._s)
        rows, seen = [], set()
        track_rows, track_seen = [], set()
        mono = time.monotonic()
        for fl in flights:
            snap = _flight_to_snapshot(fl, self._s.prefixes)
            if snap and snap["reg"] not in seen:
                seen.add(snap["reg"])
                rows.append(snap)
                self._latest[snap["reg"]] = (snap, mono)   # RAM-Store aktuell halten
            # Track: ALLE Airlines → eigener, filterloser Snapshot (set() = kein Filter).
            if self._s.track_all_carriers:
                tsnap = _flight_to_snapshot(fl, set())
                if tsnap and tsnap["reg"] not in track_seen:
                    track_seen.add(tsnap["reg"])
                    track_rows.append(tsnap)
        # RAM-Prune: alte Einträge raus (der Flug ist gelandet / aus der Kachel).
        cut = mono - self._s.prune_age
        stale = [k for k, (_, t) in self._latest.items() if t < cut]
        for k in stale:
            self._latest.pop(k, None)
        n_up = await self._upsert(rows)
        n_tr = await self._append_track(track_rows if self._s.track_all_carriers else rows)
        self._win_rows += n_up
        log.debug("tile%d flights=%d matched=%d ram=%d upserted=%d track+=%d",
                  idx, len(flights), len(rows), len(self._latest), n_up, n_tr)

    async def run(self) -> None:
        from fr24 import FR24
        Path(self._s.heartbeat_file).touch()
        backoff = self._s.backoff_base
        log.info("fr24-ingest v2 (gRPC) gestartet — %d Kacheln, %d Carrier-Prefixe, poll=%.0fs",
                 len(self._s.tiles), len(self._s.prefixes) or 0, self._s.poll_interval)
        await self._start_http()
        # EIN FR24-gRPC-Client über die ganze Container-Lebensdauer (TCP/TLS-Reuse).
        async with FR24() as fr24:
            while not self._stop.is_set():
                try:
                    await self._poll_once(fr24)
                    backoff = self._s.backoff_base          # Erfolg → Backoff-Reset
                    Path(self._s.heartbeat_file).touch()
                    if time.monotonic() - self._last_prune > self._s.prune_interval:
                        await self._prune()
                        self._last_prune = time.monotonic()
                    if time.monotonic() - self._win_start > self._s.summary_interval:
                        log.info("summary: %d Zeilen upserted in letzten %.0fmin",
                                 self._win_rows, self._s.summary_interval / 60)
                        self._win_rows = 0
                        self._win_start = time.monotonic()
                    sleep_s = self._s.poll_interval
                except asyncio.TimeoutError:
                    log.warning("tile-fetch timeout → backoff %.0fs", backoff)
                    sleep_s = backoff
                    backoff = decorrelated_jitter(backoff, self._s.backoff_base, self._s.backoff_cap)
                except Exception as ex:
                    log.warning("poll-Fehler: %s → backoff %.0fs", ex, backoff)
                    sleep_s = backoff
                    backoff = decorrelated_jitter(backoff, self._s.backoff_base, self._s.backoff_cap)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(),
                                           timeout=sleep_s + random.uniform(0, 2))


async def _amain() -> None:
    s = Settings()
    logging.basicConfig(level=getattr(logging, s.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx-Request-Logs auf WARNING (sonst pro Poll INFO → NAS-Platten wach).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if not s.supabase_key:
        raise SystemExit("SUPABASE_KEY env fehlt")

    ing = Ingest(s)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, ing.request_stop)
    try:
        await ing.run()
    finally:
        await ing.close()


if __name__ == "__main__":
    asyncio.run(_amain())
