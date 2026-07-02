"""
eu_scraper.scraper — Playwright headless-Chromium driver + shared row helpers for
the AeroX European airport-board scraper.

WHY THIS EXISTS
    The German (Fraport/Aviay JSON feeds) and Norwegian (Avinor XmlFeed) boards are
    plain-HTTP scrapable and live in the main Flask app. The big European hubs
    (AENA/Spain, Groupe ADP/Paris, Zurich, Vienna, Copenhagen, Schiphol, …) sit
    behind Akamai bot-walls or render their board only via client-side JS, so plain
    `requests` gets a 403 / an empty shell. A headless Chromium loads past the wall
    (verified) and — crucially — once the page has run the Akamai sensor JS the
    browser context holds the `_abck`/`bm_sz` cookies, so the SAME internal JSON
    endpoints the site's own React app calls become fetchable through
    `context.request` (shares the cookie jar). That is the core trick: warm a page,
    then fetch the site's own JSON with the warmed context.

CONTRACT
    Every airport module returns rows in the SAME shape the native German/Norway
    scrapers use (see app.py `_empty_board_row`), so `supabase_writer` can write them
    into the identical `airport_delay_obs` warehouse:

        flight, airline, airline_name, dest_iata, dest_name,
        sched (naive LOCAL 'YYYY-MM-DDTHH:MM:00'), esti (same form or ''),
        delay_min, gate, terminal, reg, aircraft (type code), status,
        delayed (bool), cancelled (bool)

    plus a per-direction split: an airport module yields
        { 'IATA': {'departure': [rows...], 'arrival': [rows...]}, ... }
    (group operators like AENA return many IATA keys from one crack).

NEVER CRASHES
    One airport failing must never break the batch. Every airport call is wrapped;
    a failure returns an empty result + an error string.
"""
from __future__ import annotations

import time
import traceback
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

from playwright.sync_api import sync_playwright

# Real desktop Chrome UA — the sensor JS fingerprints this; a stale/headless UA
# gets challenged harder.
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

DELAY_THRESHOLD_MIN = 15  # mirror app._DELAY_THRESHOLD_MIN


# ─────────────────────────────────────────────────────────────────────────────
#  Row helpers (shared by every airport module)
# ─────────────────────────────────────────────────────────────────────────────
def empty_row() -> dict:
    return {
        "airline": "", "airline_name": "", "flight": "", "dest_iata": "",
        "dest_name": "", "sched": None, "esti": None, "delay_min": 0,
        "gate": "", "terminal": "", "hall": "", "status": "",
        "delayed": False, "cancelled": False, "reg": "", "aircraft": "",
    }


def norm_flight(airline: str, number: str) -> str:
    """('LH','411') / ('DE 4348','') → 'LH411' — no space, upper. Accepts a
    pre-joined 'DE 4348' in `airline` when `number` is empty."""
    if not number:
        raw = (airline or "")
    else:
        raw = f"{airline or ''}{number or ''}"
    return raw.replace(" ", "").upper().strip()


def _tz(tzname):
    if not tzname or ZoneInfo is None:
        return None
    try:
        return ZoneInfo(tzname)
    except Exception:
        return None


def utc_to_local_iso(iso_utc, tzname) -> str | None:
    """'2026-07-05T07:40:00Z' (UTC) → naive airport-LOCAL 'YYYY-MM-DDTHH:MM:00'.
    Falls back to the raw UTC clock if the tz is unknown. Returns None on junk."""
    if not iso_utc:
        return None
    s = str(iso_utc).strip()
    if not s or s in ("--", "null"):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    z = _tz(tzname)
    if z is not None:
        try:
            dt = dt.astimezone(z)
        except Exception:
            pass
    return dt.strftime("%Y-%m-%dT%H:%M:00")


def local_iso(date_ddmmyyyy=None, hhmmss=None, ymd=None) -> str | None:
    """Build a naive-local iso from already-LOCAL parts.
    - AENA: date_ddmmyyyy='02/07/2026', hhmmss='18:45:00'
    - or ymd='2026-07-02' + hhmmss='18:45'"""
    try:
        if ymd:
            y = str(ymd)[:10]
        elif date_ddmmyyyy:
            d, m, y4 = str(date_ddmmyyyy).split("/")
            y = f"{y4}-{int(m):02d}-{int(d):02d}"
        else:
            return None
        hh = (hhmmss or "")[:5]
        if len(hh) < 4:
            return None
        return f"{y}T{hh}:00"
    except Exception:
        return None


def delay_min(sched_iso, esti_iso) -> int:
    if not sched_iso or not esti_iso:
        return 0
    try:
        ds = datetime.fromisoformat(str(sched_iso).replace("Z", "+00:00"))
        de = datetime.fromisoformat(str(esti_iso).replace("Z", "+00:00"))
        return max(0, int((de - ds).total_seconds() / 60))
    except Exception:
        return 0


def finalize(row: dict) -> dict:
    """Fill delay_min/delayed from sched vs esti; keep the row honest."""
    if not row.get("delay_min"):
        row["delay_min"] = delay_min(row.get("sched"), row.get("esti"))
    if row.get("cancelled"):
        row["delayed"] = False
    else:
        row["delayed"] = bool(row.get("delayed")) or row["delay_min"] >= DELAY_THRESHOLD_MIN
    row["flight"] = (row.get("flight") or "").replace(" ", "").upper()
    return row


# ─────────────────────────────────────────────────────────────────────────────
#  Driver — one Chromium, reused across airports
# ─────────────────────────────────────────────────────────────────────────────
class Driver:
    """Wraps a single headless Chromium + a stealthed browser context. Reused for
    the whole batch (one browser launch, N airports). Exposes `warm(url)` and
    `get_json(url, referer)` — the warm-then-fetch pattern that beats the bot wall."""

    def __init__(self, ua: str = DEFAULT_UA, locale: str = "en-US", headless: bool = True):
        self.ua = ua
        self.locale = locale
        self.headless = headless
        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = None
        self._warmed = set()

    def __enter__(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
            ],
        )
        self._ctx = self._browser.new_context(
            user_agent=self.ua,
            locale=self.locale,
            viewport={"width": 1366, "height": 900},
            timezone_id="Europe/Berlin",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9,de;q=0.7,es;q=0.6"},
        )
        # Hide the automation tell that some bot-walls sniff.
        self._ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "window.chrome={runtime:{}};"
            "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});"
            "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
        )
        self._page = self._ctx.new_page()
        return self

    def __exit__(self, *a):
        for closer in (self._ctx, self._browser):
            try:
                closer and closer.close()
            except Exception:
                pass
        try:
            self._pw and self._pw.stop()
        except Exception:
            pass

    def warm(self, url: str, wait_ms: int = 3500, once: bool = True):
        """Load a page so the Akamai/bot sensor runs and drops its cookies into the
        context. `once=True` skips re-warming the same origin within a batch."""
        origin = "/".join(url.split("/")[:3])
        if once and origin in self._warmed:
            return
        try:
            self._page.goto(url, timeout=30000, wait_until="domcontentloaded")
            self._page.wait_for_timeout(wait_ms)
            self._warmed.add(origin)
        except Exception:
            # Even a partial load usually sets the cookie; don't fail hard.
            self._warmed.add(origin)

    def get_json(self, url: str, referer: str | None = None, retries: int = 2):
        """Fetch JSON through the WARMED context (inherits bot cookies). Returns the
        decoded JSON or None. Never raises."""
        headers = {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
        }
        if referer:
            headers["Referer"] = referer
        for attempt in range(retries + 1):
            try:
                resp = self._ctx.request.get(url, headers=headers, timeout=25000)
                if resp.ok:
                    try:
                        return resp.json()
                    except Exception:
                        txt = resp.text()
                        import json as _j
                        return _j.loads(txt)
            except Exception:
                pass
            time.sleep(1.0 + attempt)
        return None

    def get_text(self, url: str, referer: str | None = None):
        headers = {"Accept": "*/*"}
        if referer:
            headers["Referer"] = referer
        try:
            resp = self._ctx.request.get(url, headers=headers, timeout=25000)
            if resp.ok:
                return resp.text()
        except Exception:
            pass
        return None

    def render(self, url: str, wait_selector: str | None = None, wait_ms: int = 6000):
        """Full DOM render fallback for airports with no JSON endpoint. Returns the
        active page (already navigated). Never raises."""
        try:
            self._page.goto(url, timeout=30000, wait_until="domcontentloaded")
            if wait_selector:
                try:
                    self._page.wait_for_selector(wait_selector, timeout=wait_ms)
                except Exception:
                    pass
            else:
                self._page.wait_for_timeout(wait_ms)
        except Exception:
            pass
        return self._page


# ─────────────────────────────────────────────────────────────────────────────
#  Registry + batch runner
# ─────────────────────────────────────────────────────────────────────────────
# Populated by airports/__init__.py. Maps a *group/airport key* → callable
# fn(driver) -> {IATA: {'departure':[...], 'arrival':[...]}}.
REGISTRY: dict = {}


def register(key: str, fn):
    REGISTRY[key] = fn


def _load_registry():
    if REGISTRY:
        return
    from . import airports  # noqa: F401  (registers on import)


def scrape_targets(targets=None, headless=True, ua=DEFAULT_UA, driver_attrs=None):
    """Run the given registry keys (or all). Returns:
        { 'IATA': {'departure':[rows], 'arrival':[rows], 'error': str|None,
                   'group': key} , ... }
    `driver_attrs` sets attributes on the driver (e.g. aena_subset / aena_all) that
    airport modules read for per-request config. A single airport/group failure is
    captured, never raised."""
    _load_registry()
    keys = list(targets) if targets else list(REGISTRY.keys())
    out = {}
    with Driver(ua=ua, headless=headless) as drv:
        for _k, _v in (driver_attrs or {}).items():
            setattr(drv, _k, _v)
        for key in keys:
            fn = REGISTRY.get(key)
            if not fn:
                out.setdefault("_unknown", []).append(key)
                continue
            t0 = time.time()
            try:
                res = fn(drv) or {}
                for iata, dirs in res.items():
                    rec = out.setdefault(iata, {"departure": [], "arrival": [],
                                                "error": None, "group": key})
                    rec["departure"].extend(dirs.get("departure") or [])
                    rec["arrival"].extend(dirs.get("arrival") or [])
                print(f"[eu_scraper] {key}: {sum(len(v.get('departure',[]))+len(v.get('arrival',[])) for v in res.values())} "
                      f"rows across {len(res)} airport(s) in {time.time()-t0:.1f}s")
            except Exception as e:
                print(f"[eu_scraper] {key} FAILED: {type(e).__name__}: {e}")
                traceback.print_exc()
                out.setdefault(f"_group:{key}", {"error": f"{type(e).__name__}: {e}"})
    return out
