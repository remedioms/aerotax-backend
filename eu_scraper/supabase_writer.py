"""
supabase_writer — writes scraped board rows into the SAME warehouse the native
German/Norway scrapers feed: `airport_delay_obs` (+ best-effort `ax_route_cache`).

Mirrors app.py `_delay_obs_write_through`:
  - store-key: departures under '<IATA>', arrivals under '<IATA>#ARR'
  - primary key (date, airport, flight, sched) where sched = 'HH:MM' (local)
  - manual upsert: PATCH by the key; if it matched no row → POST insert
    (the live table historically lacked the composite PK, so we never rely on
     on_conflict — same reason the app does a manual update-then-insert)
  - full fields (dest_iata/dest_name/gate/terminal/airline/esti/reg/type_code)
    written only when non-empty, so a poorer later poll never blanks a richer row
  - `source` column is OPTIONAL: prod doesn't have it yet, so on a schema error we
    auto-drop it and retry (matches the app's schema-safe fallback)

HONESTY GUARD: only rows whose scheduled LOCAL time has already passed (or that are
departed/landed/cancelled) are persisted — writing future flights as max_delay=0
would falsely record them as "on time". Same rule the app's merge-cycle applies.

Uses SUPABASE_URL + SUPABASE_SERVICE_KEY from env (service key bypasses RLS). Only
the `requests` lib — no supabase SDK, keeps the image slim.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from urllib.parse import quote

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

import requests

SB_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY") or ""
TABLE = "airport_delay_obs"

# Flip to False after the first "column source does not exist" so we stop sending it.
_SEND_SOURCE = True

_DEPARTED_HINTS = ("depart", "airborne", "flew", "flown", "gestartet", "abgeflogen",
                   "land", "arriv", "final", "baggage", "gelandet")
_CANCEL_HINTS = ("cancel", "annull")


def _headers():
    return {
        "apikey": SB_KEY,
        "Authorization": f"Bearer {SB_KEY}",
        "Content-Type": "application/json",
    }


def available() -> bool:
    return bool(SB_URL and SB_KEY)


def _local_now(tzname):
    if tzname and ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(tzname)).replace(tzinfo=None)
        except Exception:
            pass
    return datetime.utcnow()


def _passed(row: dict) -> bool:
    """Already-departed / landed / cancelled, or sched-local in the past."""
    if row.get("cancelled"):
        return True
    st = (row.get("status") or "").lower()
    if any(h in st for h in _DEPARTED_HINTS):
        return True
    if row.get("esti"):  # a revised time means it's an active/known movement
        pass
    sched = row.get("sched")
    if not sched:
        return False
    try:
        ds = datetime.fromisoformat(str(sched)[:19])
    except Exception:
        return True  # can't tell → keep it (defensive, like the app)
    now = _local_now(row.get("_tz"))
    return ds <= now  # scheduled moment has passed in airport-local time


def _type_code(row: dict) -> str:
    ac = (row.get("aircraft") or "").strip().upper()
    # keep short type codes only (A320, B38M, E95, 738W) — not long model names
    if ac and len(ac) <= 4 and " " not in ac:
        return ac
    return ""


def _store_key(iata: str, arr: bool) -> str:
    ap = (iata or "").upper().strip().split("#", 1)[0]
    return f"{ap}#ARR" if arr else ap


def _payload(row: dict, iata: str, arr: bool, source: str):
    global _SEND_SOURCE
    sched = row.get("sched") or ""
    hhmm = sched[11:16] if len(sched) >= 16 else sched
    fn = (row.get("flight") or "").replace(" ", "").upper()
    if not fn or not hhmm:
        return None
    # VERKEHRSTAG statt Poll-Tag (Folgetags-Kontaminations-Fix, s. scraper.service_day
    # + LH867-Beweis). Trägt sched ein volles Datum (Normalfall in jedem Airport-
    # Modul), ist das ein no-op = sched[:10]; nur date-lose Plan-Rows werden korrekt
    # auf den Folgetag verschoben.
    from . import scraper as _S
    date_str = _S.service_day(
        sched, tzname=row.get("_tz"),
        status=(row.get("status") or ""),
        esti=(row.get("esti") if isinstance(row.get("esti"), str) else ""),
        cancelled=bool(row.get("cancelled")))
    max_delay = int(row.get("delay_min") or 0)
    base = {
        "date": date_str,
        "airport": _store_key(iata, arr),
        "flight": fn,
        "sched": hhmm,
        "max_delay_min": max_delay,
        "cancelled": bool(row.get("cancelled")),
        "status": (row.get("status") or None),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    rich = {
        "dest_iata": (row.get("dest_iata") or "").strip(),
        "dest_name": (row.get("dest_name") or "").strip(),
        "gate": (row.get("gate") or "").strip(),
        "terminal": (row.get("terminal") or "").strip(),
        "airline": (row.get("airline") or "").strip(),
        "esti": (row.get("esti") or "").strip() if isinstance(row.get("esti"), str) else "",
        "reg": (row.get("reg") or "").strip().upper()[:12],
        "type_code": _type_code(row),
    }
    for k, v in rich.items():
        if v:
            base[k] = v
    if _SEND_SOURCE and source:
        base["source"] = source
    return base


def _rest(method, path, **kw):
    url = f"{SB_URL}/rest/v1/{path}"
    headers = {**_headers(), **(kw.pop("headers", None) or {})}
    return requests.request(method, url, headers=headers, timeout=25, **kw)


def _upsert_one(payload: dict) -> str:
    """Manual upsert: PATCH by key; if nothing matched, POST insert. Returns
    'update' / 'insert' / 'skip' / 'fail:<reason>'. Never raises."""
    global _SEND_SOURCE
    # CRITICAL: URL-encode filter values. The arrivals store-key contains '#'
    # ('ALC#ARR'); unencoded, '#' starts a URL fragment and PostgREST silently
    # sees only 'airport=eq.ALC' → arrivals would PATCH the DEPARTURE rows and
    # never land under '#ARR'. quote(safe='') encodes '#' → '%23'.
    def _q(v):
        return quote(str(v), safe="")
    key = (f"date=eq.{_q(payload['date'])}&airport=eq.{_q(payload['airport'])}"
           f"&flight=eq.{_q(payload['flight'])}&sched=eq.{_q(payload['sched'])}")
    upd = {k: v for k, v in payload.items()
           if k not in ("date", "airport", "flight", "sched")}
    try:
        r = _rest("PATCH", f"{TABLE}?{key}", json=upd,
                  headers={**_headers(), "Prefer": "return=representation"})
        if r.status_code == 400 and "source" in r.text and _SEND_SOURCE:
            _SEND_SOURCE = False
            payload.pop("source", None)
            upd.pop("source", None)
            r = _rest("PATCH", f"{TABLE}?{key}", json=upd,
                      headers={**_headers(), "Prefer": "return=representation"})
        if r.status_code in (200, 204):
            body = r.json() if r.text.strip().startswith("[") else []
            if body:
                return "update"
            # no row matched → insert
            ri = _rest("POST", TABLE, json=payload,
                       headers={**_headers(), "Prefer": "return=minimal"})
            if ri.status_code in (201, 204):
                return "insert"
            if ri.status_code == 409:
                return "update"  # raced with another writer; the row exists
            return f"fail:insert {ri.status_code} {ri.text[:80]}"
        return f"fail:patch {r.status_code} {r.text[:80]}"
    except Exception as e:
        return f"fail:{type(e).__name__} {str(e)[:80]}"


def write_airport(iata: str, dirs: dict, source_suffix="_hl") -> dict:
    """Write one airport's departures+arrivals. Returns a stats dict."""
    stats = {"iata": iata, "insert": 0, "update": 0, "skip_future": 0,
             "skip_invalid": 0, "fail": 0, "errors": []}
    if not available():
        stats["errors"].append("no supabase creds")
        return stats
    source = f"{iata.lower()}{source_suffix}"
    for arr, key in ((False, "departure"), (True, "arrival")):
        for row in (dirs.get(key) or []):
            if not _passed(row):
                stats["skip_future"] += 1
                continue
            payload = _payload(row, iata, arr, source)
            if not payload:
                stats["skip_invalid"] += 1
                continue
            res = _upsert_one(payload)
            if res in ("insert", "update"):
                stats[res] += 1
            else:
                stats["fail"] += 1
                if len(stats["errors"]) < 3:
                    stats["errors"].append(res)
    return stats


def write_all(scraped: dict, source_suffix="_hl") -> dict:
    """scraped = output of scraper.scrape_targets. Writes every real airport."""
    total = {"airports": 0, "insert": 0, "update": 0, "skip_future": 0,
             "skip_invalid": 0, "fail": 0, "per_airport": []}
    for iata, dirs in scraped.items():
        if iata.startswith("_") or not isinstance(dirs, dict):
            continue
        if "departure" not in dirs and "arrival" not in dirs:
            continue
        st = write_airport(iata, dirs, source_suffix=source_suffix)
        total["airports"] += 1
        for k in ("insert", "update", "skip_future", "skip_invalid", "fail"):
            total[k] += st[k]
        total["per_airport"].append(st)
    return total
