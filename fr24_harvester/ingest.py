"""
fr24-ingest — rate-limit-respektierender Async-Harvester für den FR24-Feed auf
einer Synology-NAS (Docker), Positionen/Routen/Tails → Supabase `fr24_live`.

Adaptiert aus einem production-Blueprint (Owner 2026-07-06). Übernommen wurde das
robuste NETZWERK-Herz — es löst genau unser FR24-Drosselungs-Problem:
  · EIN langlebiger httpx.AsyncClient: TCP+TLS einmal pro Container-Leben statt
    pro Request (der größte CPU-Sparer auf ARM/NAS).
  · EIN geteilter asyncio-Token-Bucket = einziger Choke-Point für ALLE FR24-
    Requests. Bursts bis RATE_LIMIT_BURST, Dauerrate gekappt bei RATE_LIMIT_RPS.
  · HTTP 429/403 → GLOBALER Freeze (Retry-After-bewusst) + sanftes Hochrampen aus
    leer (nie sofort zurück in einen Server, der gerade gedrosselt hat).
  · Transport/5xx → dekorrelierter Jitter-Backoff (AWS-Stil, keine Retry-Herden).
  · Soft-Block-Erkennung: FR24 antwortet bei IP-Drosselung 200 mit NUR full_count
    (keine ac). Mehrere leere Kacheln in Folge = Drosselung → Bucket-Freeze.

BEWUSST WEGGELASSEN ggü. dem Blueprint: der zweistufige SQLite-Cache/Single-Flight.
Der ist für „ein Poll + Handvoll Per-Airframe-Metadaten-Lookups" gedacht. Wir
holen BULK-Kacheln (feed.js liefert alle Flieger einer Box inkl. Route+Tail auf
einmal) und unser Store IST `fr24_live` in Supabase — ein lokaler Cache wäre
redundant.

Einzige Dritt-Abhängigkeit: httpx. Alles über Env konfigurierbar (Settings).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import signal
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("fr24-ingest")


# --------------------------------------------------------------------------- #
# Konfiguration (alles via Env)
# --------------------------------------------------------------------------- #
# Korridor-Kacheln (lat_n,lat_s,lon_w,lon_e). 0-7 = Coverage-Löcher (dort liefert
# freies ADS-B nichts → FR24 einzige Quelle). 8-14 = Europa-Enrichment (Route/Tail).
_DEFAULT_TILES = [
    (55, 20, 55, 110), (72, 45, 55, 140), (55, 20, 110, 145), (45, 8, 30, 65),
    (35, -10, 60, 100), (72, 35, -60, -10), (60, 15, 140, 180), (40, -40, -25, 55),
    (60, 48, -11, 3), (52, 42, -3, 10), (56, 45, 9, 20), (72, 55, 4, 32),
    (45, 35, -10, 5), (47, 35, 6, 30), (52, 44, 20, 40),
]

_UAS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


def _parse_tiles(raw: str) -> list[tuple]:
    """TILES-Env: "all" | "*" → alle; "0,1,2" → Teilmenge (für verteilte Nodes)."""
    raw = (raw or "all").strip().lower()
    if raw in ("all", "*", ""):
        return list(_DEFAULT_TILES)
    idxs = [int(x) for x in raw.replace(";", ",").split(",")
            if x.strip().isdigit() and 0 <= int(x) < len(_DEFAULT_TILES)]
    return [_DEFAULT_TILES[i] for i in idxs] or list(_DEFAULT_TILES)


@dataclass(frozen=True)
class Settings:
    fr24_base: str = os.getenv("FR24_BASE_URL", "https://data-cloud.flightradar24.com")
    feed_path: str = os.getenv("FR24_FEED_PATH", "/zones/fcgi/feed.js")
    referer: str = os.getenv("FR24_REFERER", "https://www.flightradar24.com/")
    maxage: str = os.getenv("MAXAGE", "14400")

    supabase_url: str = os.getenv("SUPABASE_URL", "")
    supabase_key: str = os.getenv("SUPABASE_KEY", "")

    tiles: list = field(default_factory=lambda: _parse_tiles(os.getenv("TILES", "all")))

    # Kadenz — LANGSAM (Owner 2026-07-06: FR24 flaggt hohe Frequenz; die stabilen
    # Routen/Tails brauchen nur seltenes Holen). 90s × 15 Kacheln ≈ 22min/Zyklus.
    poll_interval: float = float(os.getenv("POLL_INTERVAL_S", "90"))
    prune_interval: float = float(os.getenv("PRUNE_INTERVAL_S", "900"))
    prune_age: float = float(os.getenv("PRUNE_AGE_S", "1800"))

    # Rate-Limit — SEHR niedrig (Dauerrate < das, was FR24 flaggt).
    rate_rps: float = float(os.getenv("RATE_LIMIT_RPS", "0.02"))     # ~1 Request/50s Dauerrate
    rate_burst: float = float(os.getenv("RATE_LIMIT_BURST", "2"))

    backoff_base: float = float(os.getenv("BACKOFF_BASE_S", "60"))
    backoff_cap: float = float(os.getenv("BACKOFF_CAP_S", "1800"))
    max_retries: int = int(os.getenv("MAX_RETRIES", "4"))
    softblock_threshold: int = int(os.getenv("SOFTBLOCK_THRESHOLD", "5"))

    max_connections: int = int(os.getenv("MAX_CONNECTIONS", "4"))
    keepalive_expiry: float = float(os.getenv("KEEPALIVE_EXPIRY_S", "120"))
    connect_timeout: float = float(os.getenv("CONNECT_TIMEOUT_S", "6"))
    read_timeout: float = float(os.getenv("READ_TIMEOUT_S", "15"))

    heartbeat_file: str = os.getenv("HEARTBEAT_FILE", "/tmp/heartbeat")
    summary_interval: float = float(os.getenv("SUMMARY_INTERVAL_S", "1800"))  # Log-Summary alle 30min
    log_level: str = os.getenv("LOG_LEVEL", "INFO")


# --------------------------------------------------------------------------- #
# 1. Rate-Limiting — asyncio Token-Bucket mit globalem Freeze (429/403) — 1:1 aus dem Blueprint
# --------------------------------------------------------------------------- #
class AsyncTokenBucket:
    """Token-Bucket: Bursts bis `capacity`, Dauerrate `rate` Tokens/s. ALLE
    Request-Coroutines teilen EINE Instanz → einziger Choke-Point. freeze_for()
    ist ein harter, prozessweiter Cool-Down bei serverseitigem Rate-Signal
    (429/403): IP-Drosselung betrifft jede Verbindung, also pausiert JEDER."""

    def __init__(self, rate: float, capacity: float) -> None:
        if rate <= 0 or capacity <= 0:
            raise ValueError("rate and capacity must be > 0")
        self._rate = rate
        self._capacity = capacity
        self._tokens = capacity
        self._stamp = time.monotonic()
        self._frozen_until = 0.0
        self._lock = asyncio.Lock()

    def freeze_for(self, seconds: float) -> None:
        until = time.monotonic() + max(0.0, seconds)
        if until > self._frozen_until:
            self._frozen_until = until
        # Aus LEER neu starten wenn der Freeze abläuft → sanftes Hochrampen.
        self._tokens = 0.0
        self._stamp = self._frozen_until

    async def acquire(self, cost: float = 1.0) -> None:
        if cost > self._capacity:
            raise ValueError("cost exceeds bucket capacity")
        while True:
            async with self._lock:
                now = time.monotonic()
                if now >= self._frozen_until:
                    self._tokens = min(
                        self._capacity, self._tokens + (now - self._stamp) * self._rate)
                    self._stamp = now
                    if self._tokens >= cost:
                        self._tokens -= cost
                        return
                    wait = (cost - self._tokens) / self._rate
                else:
                    wait = self._frozen_until - now
            await asyncio.sleep(wait + random.uniform(0.0, 0.05))


def decorrelated_jitter(prev_sleep: float, base: float, cap: float) -> float:
    """AWS 'decorrelated jitter': min(cap, uniform(base, prev·3))."""
    return min(cap, random.uniform(base, max(base, prev_sleep * 3)))


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After: delta-Sekunden ODER HTTP-Date."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        return max(0.0, dt.timestamp() - time.time())
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# 2. HTTP-Client — ein getunter AsyncClient pro Prozess (adaptiert für FR24)
# --------------------------------------------------------------------------- #
class Fr24Blocked(Exception):
    """FR24 hat gedrosselt (429/403 oder Soft-Block) — Aufrufer weiß: pausieren."""


class ApiClient:
    """Dünner Wrapper um httpx.AsyncClient mit Pacing + Retry. FR24-Anpassung:
    403 gilt (wie 429) als Drosselung → Bucket-Freeze, nicht Fail-Fast."""

    RETRYABLE_STATUS = frozenset({500, 502, 503, 504})
    FREEZE_STATUS = frozenset({429, 403})   # FR24 blockt per 403 UND 429

    def __init__(self, settings: Settings, bucket: AsyncTokenBucket) -> None:
        self._s = settings
        self._bucket = bucket
        self._client = httpx.AsyncClient(
            base_url=settings.fr24_base,
            headers={"Accept": "application/json",
                     "Accept-Language": "en-US,en;q=0.9",
                     "Referer": settings.referer},
            limits=httpx.Limits(
                max_connections=settings.max_connections,
                max_keepalive_connections=settings.max_connections,
                keepalive_expiry=settings.keepalive_expiry),
            timeout=httpx.Timeout(
                connect=settings.connect_timeout, read=settings.read_timeout,
                write=settings.connect_timeout, pool=settings.read_timeout),
            http2=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        backoff = self._s.backoff_base
        last_exc: Exception | None = None
        for attempt in range(1, self._s.max_retries + 1):
            await self._bucket.acquire()
            hdrs = {"User-Agent": random.choice(_UAS)}   # UA pro Request rotieren
            try:
                resp = await self._client.get(path, params=params, headers=hdrs)
            except httpx.TransportError as exc:
                last_exc = exc
                backoff = decorrelated_jitter(backoff, self._s.backoff_base, self._s.backoff_cap)
                log.warning("GET %s transport error (%d/%d): %s — retry in %.0fs",
                            path, attempt, self._s.max_retries, exc, backoff)
                await asyncio.sleep(backoff)
                continue

            if resp.status_code in self.FREEZE_STATUS:
                ra = parse_retry_after(resp.headers.get("Retry-After"))
                cooldown = ra if ra is not None else decorrelated_jitter(
                    backoff, self._s.backoff_base, self._s.backoff_cap)
                backoff = min(max(cooldown, backoff), self._s.backoff_cap)
                self._bucket.freeze_for(cooldown)   # JEDER Worker pausiert
                log.warning("GET %s -> %d; GLOBALER Cool-Down %.0fs (%d/%d)",
                            path, resp.status_code, cooldown, attempt, self._s.max_retries)
                last_exc = Fr24Blocked(f"http {resp.status_code}")
                continue   # acquire() oben erzwingt den Wait

            if resp.status_code in self.RETRYABLE_STATUS:
                ra = parse_retry_after(resp.headers.get("Retry-After"))
                backoff = ra if ra is not None else decorrelated_jitter(
                    backoff, self._s.backoff_base, self._s.backoff_cap)
                log.warning("GET %s -> %d; retry in %.0fs (%d/%d)",
                            path, resp.status_code, backoff, attempt, self._s.max_retries)
                last_exc = Fr24Blocked(f"http {resp.status_code}")
                await asyncio.sleep(backoff)
                continue

            resp.raise_for_status()   # andere 4xx: Fail-Fast (unser Bug)
            return resp.json()
        raise (last_exc or RuntimeError(f"GET {path}: {self._s.max_retries} Versuche erschöpft"))

    def freeze(self, seconds: float) -> None:
        self._bucket.freeze_for(seconds)


# --------------------------------------------------------------------------- #
# 3. Normalisierung FR24-feed.js-Zeile → OpenSky-State-Row (wie im Backend)
# --------------------------------------------------------------------------- #
def _n(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _row_to_opensky(v):
    """FR24-feed.js-Zeile → OpenSky-State-Row. IDENTISCH zu _fr24_row_to_opensky
    im Backend, damit die gespeicherte `row` 0 Transformation braucht."""
    try:
        if not isinstance(v, list) or len(v) < 17:
            return None
        lat, lon = v[1], v[2]
        if lat in (None, 0) and lon in (None, 0):
            return None
        alt_ft = _n(v[4]); gs_kt = _n(v[5]); vs_fpm = _n(v[15])
        ts = _n(v[10]) or time.time()
        cs = (str(v[16]).strip() or None) if v[16] else None
        reg = (str(v[9]).strip().upper() or None) if v[9] else None
        return [
            (str(v[0]).strip().lower() or None), cs, reg, ts, ts,
            _n(lon), _n(lat),
            (alt_ft * 0.3048) if alt_ft is not None else None,
            bool(v[14]),
            (gs_kt * 0.514444) if gs_kt is not None else None,
            _n(v[3]),
            (vs_fpm * 0.00508) if vs_fpm is not None else None,
            None,
            (alt_ft * 0.3048) if alt_ft is not None else None,
            (str(v[6]).strip() or None) if v[6] else None,
            False, 0,
        ]
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 4. FR24-Ingest-Service — Round-Robin-Kacheln → Supabase fr24_live
# --------------------------------------------------------------------------- #
class Fr24Ingestor:
    def __init__(self, settings: Settings, api: ApiClient, sb: httpx.AsyncClient) -> None:
        self._s = settings
        self._api = api
        self._sb = sb          # eigener Client für Supabase (unser Store, nicht rate-limitiert)
        self._tile_idx = 0
        self._consec_empty = 0
        self._last_prune = 0.0
        self._win_rows = 0
        self._last_summary = time.monotonic()

    async def run(self, stop: asyncio.Event) -> None:
        heartbeat = Path(self._s.heartbeat_file)
        while not stop.is_set():
            started = time.monotonic()
            try:
                await self.poll_once()
            except Fr24Blocked:
                # ApiClient hat den Bucket bereits eingefroren; nur weiterlaufen.
                pass
            except Exception:
                log.exception("poll cycle failed; retry next interval")
            heartbeat.touch()   # Liveness fürs Docker-Healthcheck (tmpfs = RAM)
            # Periodische Log-Summary statt pro-Poll (schont die NAS-Platten).
            if time.monotonic() - self._last_summary >= self._s.summary_interval:
                log.info("summary: %d rows upserted in letzten %.0fmin",
                         self._win_rows, self._s.summary_interval / 60)
                self._win_rows = 0
                self._last_summary = time.monotonic()
            # Jitter ±30% aufs Poll-Intervall → kein maschinelles Muster.
            delay = self._s.poll_interval * random.uniform(0.7, 1.3)
            delay = max(5.0, delay - (time.monotonic() - started))
            await sleep_or_stop(stop, delay)
            await self._maybe_prune()

    async def poll_once(self) -> None:
        idx = self._tile_idx
        self._tile_idx = (idx + 1) % len(self._s.tiles)
        n, s, w, e = self._s.tiles[idx]
        params = {"bounds": f"{n},{s},{w},{e}", "faa": 1, "mlat": 1, "flarm": 1,
                  "adsb": 1, "gnd": 0, "air": 1, "vehicles": 0, "estimated": 1,
                  "maxage": self._s.maxage, "gliders": 0, "stats": 0}
        obj = await self._api.get_json(self._s.feed_path, params=params)

        items = []
        for k, v in obj.items():
            if not isinstance(v, list):
                continue
            row = _row_to_opensky(v)
            if row is None or row[0] is None:
                continue
            origin = (str(v[11]).strip().upper() or None) if len(v) > 11 and v[11] else None
            dest = (str(v[12]).strip().upper() or None) if len(v) > 12 and v[12] else None
            flight = (str(v[13]).strip().upper() or None) if len(v) > 13 and v[13] else None
            items.append({"row": row, "origin": origin, "dest": dest, "flight": flight})

        # SOFT-BLOCK: FR24 gibt bei IP-Drosselung 200 mit NUR full_count (keine ac).
        # Eine einzelne leere Kachel ist legitim (Ozean/Nacht); VIELE verschiedene
        # in Folge = Drosselung → Bucket-Freeze (löst die Drosselung).
        if not items:
            self._consec_empty += 1
            if self._consec_empty >= self._s.softblock_threshold:
                self._api.freeze(self._s.backoff_cap)
                log.warning("SOFT-BLOCK vermutet (%d leere Kacheln in Folge) -> Freeze %.0fs",
                            self._consec_empty, self._s.backoff_cap)
                self._consec_empty = 0
            return
        self._consec_empty = 0

        n_up = await self._upsert(items, idx)
        self._win_rows += n_up
        log.debug("tile%d rows=%d upserted=%d", idx, len(items), n_up)

    async def _upsert(self, items: list, tile_idx: int) -> int:
        now_iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        payload = [{
            "hex": it["row"][0], "callsign": it["row"][1],
            "lat": it["row"][6], "lon": it["row"][5],
            "origin": it["origin"], "dest": it["dest"], "flight": it["flight"],
            "row": it["row"], "tile": str(tile_idx), "updated_at": now_iso,
        } for it in items]
        r = await self._sb.post(
            "/rest/v1/fr24_live?on_conflict=hex", content=json.dumps(payload),
            headers={"Content-Type": "application/json",
                     "Prefer": "resolution=merge-duplicates,return=minimal"})
        r.raise_for_status()
        return len(payload)

    async def _maybe_prune(self) -> None:
        if time.monotonic() - self._last_prune < self._s.prune_interval:
            return
        self._last_prune = time.monotonic()
        cutoff = time.strftime('%Y-%m-%dT%H:%M:%SZ',
                               time.gmtime(time.time() - self._s.prune_age))
        try:
            await self._sb.delete(f"/rest/v1/fr24_live?updated_at=lt.{cutoff}",
                                  headers={"Prefer": "return=minimal"})
        except Exception as exc:
            log.warning("prune failed: %s", str(exc)[:80])


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
async def sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    if seconds <= 0:
        return
    with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def heartbeat_loop(stop: asyncio.Event, path: str, interval: float = 60.0) -> None:
    """Heartbeat UNABHÄNGIG vom Poll-Loop ticken. Während eines langen Bucket-
    Freezes (z.B. 30 min bei anhaltender FR24-Drosselung) hängt poll_once im
    acquire() — der Prozess LEBT, wartet nur höflich. Ein separater Ticker hält
    das Healthcheck grün, statt den Container fälschlich als „unhealthy" zu
    markieren (Neustart würde die Drosselung eh nicht beheben)."""
    p = Path(path)
    while not stop.is_set():
        with contextlib.suppress(Exception):
            p.touch()
        await sleep_or_stop(stop, interval)


async def main() -> None:
    settings = Settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx loggt sonst JEDE Anfrage auf INFO → weckt die NAS-Platten pro Poll.
    # Auf WARNING drosseln (unser eigener Summary/Warning-Log reicht).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if not settings.supabase_url or not settings.supabase_key:
        log.error("SUPABASE_URL und SUPABASE_KEY erforderlich")
        raise SystemExit(2)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    bucket = AsyncTokenBucket(rate=settings.rate_rps, capacity=settings.rate_burst)
    api = ApiClient(settings, bucket)
    sb = httpx.AsyncClient(
        base_url=settings.supabase_url.rstrip("/"),
        headers={"apikey": settings.supabase_key,
                 "Authorization": f"Bearer {settings.supabase_key}"},
        limits=httpx.Limits(max_connections=4, keepalive_expiry=120),
        timeout=httpx.Timeout(connect=6, read=20, write=20, pool=20))
    ingestor = Fr24Ingestor(settings, api, sb)

    log.info("fr24-ingest start: %d Kacheln, poll=%.0fs, rate=%.3f rps (burst %.0f) -> %s",
             len(settings.tiles), settings.poll_interval, settings.rate_rps,
             settings.rate_burst, settings.supabase_url)
    hb = asyncio.create_task(heartbeat_loop(stop, settings.heartbeat_file))
    try:
        await ingestor.run(stop)
    finally:
        hb.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hb
        await api.aclose()
        await sb.aclose()
        log.info("shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
