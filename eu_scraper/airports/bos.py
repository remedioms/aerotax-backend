"""
BOS — Boston Logan (Massport). Clean per-direction JSON the site's board calls
(LIVE-verified 2026-07-02):

    GET https://www.massport.com/massport-flight-updates/flightdata/arrivals/bos
    GET https://www.massport.com/massport-flight-updates/flightdata/departures/bos
    → {"Flights":[...]}

Per record: FlightNumber, AirlineCode, AirlineName, OriginAirportCode /
DestinationAirportCode (IATA!), ScheduledTimeUtc (UTC naive), Gate, Terminal,
Baggage, Delayed ("True"/"False"), Remarks ("Landed"/"On Time"/"Cancelled"). No
per-flight estimated-UTC in the feed, so we carry the Delayed flag; no tail/type.
"""
from __future__ import annotations

from .. import scraper as S
from .. import airports_ref as REF

BASE = "https://www.massport.com/massport-flight-updates/flightdata"
WARMUP = "https://www.massport.com/logan-airport/flights/flight-status"
TZ = "America/New_York"


def _row(rec: dict, arr: bool) -> dict | None:
    al = (rec.get("AirlineCode") or "").strip().upper()
    num = str(rec.get("FlightNumber") or "").strip()
    if not num:
        return None
    r = S.empty_row()
    r["airline"] = al
    r["airline_name"] = (rec.get("AirlineName") or "").strip()
    r["flight"] = S.norm_flight(al or num[:2], num)
    code = ((rec.get("OriginAirportCode") if arr else rec.get("DestinationAirportCode")) or "").strip().upper()
    name = ((rec.get("OriginCity") if arr else rec.get("DestinationCity")) or rec.get("City") or "").strip()
    r["dest_name"] = name
    r["dest_iata"] = REF.resolve(name, code=code) or code
    # ScheduledTimeUtc is naive UTC → airport-local.
    r["sched"] = S.utc_to_local_iso(rec.get("ScheduledTimeUtc"), TZ)
    r["gate"] = (rec.get("Gate") or "").strip()
    r["terminal"] = (rec.get("Terminal") or "").strip()
    st = (rec.get("Remarks") or "").strip()
    r["status"] = st
    low = st.lower()
    r["cancelled"] = "cancel" in low
    r["delayed"] = (str(rec.get("Delayed")).lower() == "true") or "delay" in low
    r["_tz"] = TZ
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r)


def scrape(drv) -> dict:
    drv.warm(WARMUP, wait_ms=1800)
    out = {"departure": [], "arrival": []}
    for mv, key, arr in (("departures", "departure", False), ("arrivals", "arrival", True)):
        data = drv.get_json(f"{BASE}/{mv}/bos", referer=WARMUP)
        seen = set()
        for rec in ((data or {}).get("Flights") or []):
            row = _row(rec, arr)
            if not row:
                continue
            k = (row["flight"], row["sched"])
            if k in seen:
                continue
            seen.add(k)
            out[key].append(row)
    return {"BOS": out}
