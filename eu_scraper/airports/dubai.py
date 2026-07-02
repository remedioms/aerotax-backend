"""
DXB — Dubai International (Dubai Airports). The public flight board is a React app
that reads ONE static, CDN-hosted JSON containing the WHOLE live board (both
directions, ~2900 flights spanning a rolling multi-day window):

    GET https://dubaiairports.ae/docs/passengerslibraries/flights-library/flights-data.json
    (LIVE-verified 2026-07-03.  Not date-parametrised — the file already carries a
     window around 'now'; past-day history accrues by repeated polling, same model
     as the German boards.)

Per record: flightNumber ("FZ 845"), airlineCode_iata / airlineName,
arrivalDepartureFlag (A/D), aircraftTerminal, origin_iata/originName (the OTHER
airport on arrivals), destination_iata/destinationName (the OTHER on departures),
scheduledoffblockTime/estimatedOffBlockTime (departures, UTC Z),
scheduledOnblockTime/estimatedOnBlockTime/actualOnBlockTime/actualLandingTime
(arrivals, UTC Z), gateNumber (dep) / baggageBeltNumber (arr),
aircraftParkingPosition (stand), flightStatus. No tail reg / aircraft type.

The feed is a plain static JSON (no bot wall) but we fetch it through the warmed
context for uniformity.
"""
from __future__ import annotations

from .. import scraper as S

URL = "https://dubaiairports.ae/docs/passengerslibraries/flights-library/flights-data.json"
WARMUP = "https://dubaiairports.ae/flight-status"
TZ = "Asia/Dubai"


def _row(rec: dict) -> dict | None:
    arr = (rec.get("arrivalDepartureFlag") or "").strip().upper().startswith("A")
    al = (rec.get("airlineCode_iata") or "").strip().upper()
    fn = (rec.get("flightNumber") or "").strip()
    if not fn:
        return None
    r = S.empty_row()
    r["airline"] = al or fn.replace(" ", "")[:2]
    r["airline_name"] = (rec.get("airlineName") or "").strip()
    r["flight"] = S.norm_flight(fn, "")
    if arr:
        r["dest_iata"] = (rec.get("origin_iata") or "").strip().upper()
        r["dest_name"] = (rec.get("originName") or "").strip()
        sched_utc = rec.get("scheduledOnblockTime")
        est_utc = (rec.get("actualOnBlockTime") or rec.get("estimatedOnBlockTime")
                   or rec.get("actualLandingTime"))
        r["gate"] = (rec.get("baggageBeltNumber") or "").strip()
    else:
        r["dest_iata"] = (rec.get("destination_iata") or "").strip().upper()
        r["dest_name"] = (rec.get("destinationName") or "").strip()
        sched_utc = rec.get("scheduledoffblockTime")
        est_utc = rec.get("estimatedOffBlockTime")
        r["gate"] = (rec.get("gateNumber") or "").strip()
    r["sched"] = S.utc_to_local_iso(sched_utc, TZ)
    esti = S.utc_to_local_iso(est_utc, TZ)
    if esti and esti != r["sched"]:
        r["esti"] = esti
    r["terminal"] = (rec.get("aircraftTerminal") or "").strip()
    st = (rec.get("flightStatus") or "").strip()
    r["status"] = st
    low = st.lower()
    r["cancelled"] = "cancel" in low
    r["delayed"] = "delay" in low
    r["_tz"] = TZ
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r)


def scrape(drv) -> dict:
    drv.warm(WARMUP, wait_ms=2500)
    data = drv.get_json(URL, referer=WARMUP)
    out = {"departure": [], "arrival": []}
    flights = (data or {}).get("Flights") if isinstance(data, dict) else None
    if not isinstance(flights, list):
        return {"DXB": out}
    seen = set()
    for rec in flights:
        row = _row(rec)
        if not row:
            continue
        arr = (rec.get("arrivalDepartureFlag") or "").strip().upper().startswith("A")
        k = (arr, row["flight"], row["sched"])
        if k in seen:
            continue
        seen.add(k)
        out["arrival" if arr else "departure"].append(row)
    return {"DXB": out}
