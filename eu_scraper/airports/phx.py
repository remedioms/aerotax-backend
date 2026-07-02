"""
PHX — Phoenix Sky Harbor. One JSON call returns BOTH directions (LIVE-verified
2026-07-02):

    GET https://api.phx.aero/flight-information?Key=<public static key>

Per record: Flightnumber, LineCode (airline IATA), Airline (name), AD ("A"/"D"),
Destination ("ATLANTA (ATL)" — other airport, IATA in parens → airports_ref token),
ScheduledTime (UTC ISO Z), Estimated / Actual (LOCAL clock strings), Terminal, Gate,
Status ("Now 11:50 PM"/"Cancelled"), StatusCode, BagClaim. No tail/aircraft type.
"""
from __future__ import annotations

from .. import scraper as S
from .. import airports_ref as REF

KEY = "4f85fe2ef5a240d59809b63de94ef536"
URL = f"https://api.phx.aero/flight-information?Key={KEY}"
WARMUP = "https://www.skyharbor.com/flights/"
TZ = "America/Phoenix"


def _row(rec: dict) -> dict | None:
    arr = (rec.get("AD") or "").upper().startswith("A")
    al = (rec.get("LineCode") or rec.get("StatusCode") or "").strip().upper()
    num = str(rec.get("Flightnumber") or "").strip()
    if not num:
        return None
    r = S.empty_row()
    r["airline"] = al
    r["airline_name"] = (rec.get("Airline") or "").strip()
    r["flight"] = S.norm_flight(al or num[:2], num)
    dest = (rec.get("Destination") or "").strip()  # "ATLANTA (ATL)"
    r["dest_name"] = dest
    r["dest_iata"] = REF.resolve(dest) or ""
    # ScheduledTime is UTC ISO ("2026-07-02T09:00:00Z") → airport-local.
    r["sched"] = S.utc_to_local_iso(rec.get("ScheduledTime"), TZ)
    st = (rec.get("Status") or "").strip()
    r["gate"] = (rec.get("Gate") or "").strip()
    r["terminal"] = (rec.get("Terminal") or "").strip()
    r["status"] = st
    low = st.lower()
    r["cancelled"] = "cancel" in low
    r["delayed"] = "delay" in low
    r["_tz"] = TZ
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r)


def scrape(drv) -> dict:
    drv.warm(WARMUP, wait_ms=1800)
    data = drv.get_json(URL, referer="https://www.skyharbor.com/")
    out = {"departure": [], "arrival": []}
    if not isinstance(data, list):
        return {"PHX": out}
    seen = set()
    for rec in data:
        row = _row(rec)
        if not row:
            continue
        arr = (rec.get("AD") or "").upper().startswith("A")
        k = (arr, row["flight"], row["sched"])
        if k in seen:
            continue
        seen.add(k)
        out["arrival" if arr else "departure"].append(row)
    return {"PHX": out}
