"""
HYD — Rajiv Gandhi International Airport, Hyderabad (GMR). The Next.js live board
calls a clean same-origin JSON, one call per direction:

    GET https://www.hyderabad.aero/next-api/flights?type={arrivals|departures}&flightType=all&limit=1000
    (LIVE-verified 2026-07-03.  Returns either a bare list or {success, data:[...]}.
     Not date-parametrised — a rolling window of ~yesterday→tomorrow; history accrues
     by repeated polling.)

Per record: flightNumber ("6E 6601"), airlineCode ("6E") / airlineName,
origin (IATA, arrivals) / destination (IATA, departures) + originCity/destinationCity,
rawScheduledTime/rawEstimatedTime/rawActualTime ("YYYYMMDDHHMMSS", LOCAL
Asia/Kolkata), belt (arrivals) / gate (departures), status code + statusDescription
(ARRIVED/ONTIME/DELAYED/CANCELLED). NB `flightType` here means Domestic/Intl, NOT
direction — direction is the request `type`. No tail reg / aircraft type.
"""
from __future__ import annotations

from .. import scraper as S

API = "https://www.hyderabad.aero/next-api/flights"
WARMUP = "https://www.hyderabad.aero/live-flight-information"
TZ = "Asia/Kolkata"


def _raw_iso(v) -> str | None:
    """'YYYYMMDDHHMMSS' (LOCAL) → 'YYYY-MM-DDTHH:MM:00'."""
    s = (str(v or "")).strip()
    if len(s) < 12 or not s[:12].isdigit():
        return None
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}T{s[8:10]}:{s[10:12]}:00"


def _row(rec: dict, arr: bool) -> dict | None:
    fn = (rec.get("flightNumber") or "").strip().upper()
    if not fn:
        return None
    r = S.empty_row()
    r["airline"] = (rec.get("airlineCode") or fn.replace(" ", "")[:2]).strip().upper()
    r["airline_name"] = (rec.get("airlineName") or "").strip()
    r["flight"] = S.norm_flight(fn, "")
    r["dest_iata"] = ((rec.get("origin") if arr else rec.get("destination")) or "").strip().upper()
    r["dest_name"] = ((rec.get("originCity") if arr else rec.get("destinationCity")) or "").strip()
    r["sched"] = _raw_iso(rec.get("rawScheduledTime"))
    esti = _raw_iso(rec.get("rawActualTime")) or _raw_iso(rec.get("rawEstimatedTime"))
    if esti and esti != r["sched"]:
        r["esti"] = esti
    r["gate"] = ((rec.get("belt") if arr else rec.get("gate")) or "").strip()
    st = (rec.get("statusDescription") or rec.get("remarks") or "").strip()
    r["status"] = st.title() if st.isupper() else st
    low = st.lower()
    r["cancelled"] = "cancel" in low
    r["delayed"] = "delay" in low
    r["_tz"] = TZ
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r)


def scrape(drv) -> dict:
    drv.warm(WARMUP, wait_ms=4000)
    out = {"departure": [], "arrival": []}
    for typ, key, arr in (("departures", "departure", False), ("arrivals", "arrival", True)):
        data = drv.get_json(f"{API}?type={typ}&flightType=all&limit=1000", referer=WARMUP)
        if isinstance(data, dict):
            rows = data.get("data") or data.get("flights") or []
        elif isinstance(data, list):
            rows = data
        else:
            rows = []
        seen = set()
        for rec in rows:
            row = _row(rec, arr)
            if not row:
                continue
            k = (row["flight"], row["sched"])
            if k in seen:
                continue
            seen.add(k)
            out[key].append(row)
    return {"HYD": out}
