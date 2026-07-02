"""
SYD — Sydney Airport. The React board calls a clean paginated JSON API directly:

    GET https://www.sydneyairport.com.au/_a/flights
        ?flightType={departure|arrival}&terminalType={domestic|international}
        &filter=&date=YYYY-MM-DD&count=50&startFrom=0&seq=1
        &sortColumn=scheduled_time&ascending=true
    (LIVE-verified 2026-07-03. `totalFlightCount` drives pagination via startFrom;
     both terminalType values must be fetched to cover the whole airport.)

Per record: airlineCode (IATA), airline (name), flightNumbers[] (first = operating),
destinations[] (city NAMES → resolved to IATA via airports_ref; a list when the
flight routes via a stop), terminalNumber ("T1"), scheduledTime/scheduledDate (LOCAL),
estimatedTime/estimatedDate (or "-"), status ("Departed"/"Delayed"/"Cancelled"/…).
No gate / tail / aircraft type in this feed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from .. import scraper as S
from .. import airports_ref as REF

API = "https://www.sydneyairport.com.au/_a/flights"
# Warm the ARRIVALS flavour of the board — that session lets the ctx fetch BOTH
# directions/terminals (warming only the departures page leaves arrivals 403/empty).
WARMUP = "https://www.sydneyairport.com.au/flights/?flightType=arrival&terminalType=international"
REFERER = "https://www.sydneyairport.com.au/flights/"
TZ = "Australia/Sydney"
_PAGE = 50
_MAX_PAGES = 12


def _mk_iso(date_str: str, hhmm: str) -> str | None:
    if not date_str or date_str == "-" or not hhmm or hhmm == "-":
        return None
    return f"{date_str[:10]}T{hhmm[:5]}:00"


def _row(rec: dict, arr: bool) -> dict | None:
    nums = [n for n in (rec.get("flightNumbers") or []) if n]
    if not nums:
        return None
    r = S.empty_row()
    r["airline"] = (rec.get("airlineCode") or nums[0][:2]).strip().upper()
    r["airline_name"] = (rec.get("airline") or "").strip()
    r["flight"] = S.norm_flight(nums[0], "")
    dests = [d for d in (rec.get("destinations") or []) if d]
    name = (dests[0] if arr else dests[-1]) if dests else ""
    r["dest_name"] = name.strip()
    r["dest_iata"] = REF.resolve(name) or ""
    r["terminal"] = (rec.get("terminalNumber") or "").strip()
    r["sched"] = _mk_iso(rec.get("scheduledDate"), rec.get("scheduledTime"))
    esti = _mk_iso(rec.get("estimatedDate"), rec.get("estimatedTime"))
    if esti and esti != r["sched"]:
        r["esti"] = esti
    st = (rec.get("status") or "").strip()
    r["status"] = st
    low = st.lower()
    r["cancelled"] = "cancel" in low
    r["delayed"] = "delay" in low
    r["_tz"] = TZ
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r)


def _fetch(drv, direction: str, tt: str, date: str, key: str, out: dict, seen: set):
    start = 0
    for _ in range(_MAX_PAGES):
        url = (f"{API}?flightType={direction}&terminalType={tt}&filter=&date={date}"
               f"&count={_PAGE}&startFrom={start}&seq=1&sortColumn=scheduled_time&ascending=true")
        data = drv.get_json(url, referer=REFERER)
        if not isinstance(data, dict):
            break
        rows = data.get("flightData") or []
        for rec in rows:
            row = _row(rec, direction == "arrival")
            if not row:
                continue
            k = (row["flight"], row["sched"])
            if k in seen:
                continue
            seen.add(k)
            out[key].append(row)
        total = int(data.get("totalFlightCount") or 0)
        start += _PAGE
        if start >= total or not rows:
            break


def scrape(drv) -> dict:
    drv.warm(WARMUP, wait_ms=2500)
    now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=10)))
    days = [(now - timedelta(days=1)).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")]
    out = {"departure": [], "arrival": []}
    for direction, key in (("departure", "departure"), ("arrival", "arrival")):
        seen = set()
        for tt in ("domestic", "international"):
            for d in days:
                _fetch(drv, direction, tt, d, key, out, seen)
    return {"SYD": out}
