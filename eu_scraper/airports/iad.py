"""
IAD — Washington Dulles (MWAA). One JSON call returns BOTH directions (LIVE-verified
2026-07-02):

    GET https://www.flydulles.com/arrivals-and-departures/json
    → {"arrivals":[...], "departures":[...]}

Per record: IATA (airline code), flightnumber, airline (name), status
("InAir"/"InGate"/"Scheduled"/"Cancelled"), gate + mod_gate, dep_airport_code /
arr_airport_code (IATA — populated on the ORIGIN side; the far end may be null so we
resolve the city name), city, publishedTime (LOCAL naive), actualtime, dep_terminal /
arr_terminal. No tail/aircraft type. (Same MWAA feed shape also serves DCA.)
"""
from __future__ import annotations

from .. import scraper as S
from .. import airports_ref as REF

URL = "https://www.flydulles.com/arrivals-and-departures/json"
WARMUP = "https://www.flydulles.com/arrivals-and-departures"
TZ = "America/New_York"


def _local(s):
    if not s or str(s).strip() in ("", "null"):
        return None
    s = str(s).strip().replace(" ", "T")
    return (s[:16] + ":00") if len(s) >= 16 else None


def _row(rec: dict, arr: bool) -> dict | None:
    al = (rec.get("IATA") or "").strip().upper()
    num = str(rec.get("flightnumber") or "").strip()
    if not num:
        return None
    r = S.empty_row()
    r["airline"] = al
    r["airline_name"] = (rec.get("airline") or "").strip()
    r["flight"] = S.norm_flight(al or num[:2], num)
    # The far-end airport: on ARRIVALS it's dep_airport_code (origin); on DEPARTURES
    # the top-level `airportcode` holds the destination IATA (arr_airport_code is
    # usually null). City is ALL-CAPS → Title-cased so the resolver's IATA-token
    # heuristic can't mistake a 3-letter caps word for a code.
    code = ((rec.get("dep_airport_code") if arr else rec.get("airportcode")) or "").strip().upper()
    name = (rec.get("city") or "").strip().title()
    r["dest_name"] = name
    r["dest_iata"] = REF.resolve(name, code=code) or code
    r["sched"] = _local(rec.get("publishedTime"))
    esti = _local(rec.get("actualtime"))
    if esti and esti != r["sched"]:
        r["esti"] = esti
    r["gate"] = (rec.get("mod_gate") or rec.get("gate") or rec.get("arr_gate") or "").strip()
    r["terminal"] = (rec.get("dep_terminal") or rec.get("arr_terminal") or "").strip()
    ai = rec.get("aircraftInfo")
    ac = ai[0] if isinstance(ai, list) and ai else (ai if isinstance(ai, dict) else {})
    r["reg"] = (ac.get("tail_number") or "").strip().upper()
    r["aircraft"] = (ac.get("aircraft_code") or "").strip().upper()
    st = (rec.get("mod_status") or rec.get("status") or "").strip()
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
    data = drv.get_json(URL, referer=WARMUP)
    out = {"departure": [], "arrival": []}
    for key, field, arr in (("arrival", "arrivals", True), ("departure", "departures", False)):
        seen = set()
        for rec in ((data or {}).get(field) or []):
            row = _row(rec, arr)
            if not row:
                continue
            k = (row["flight"], row["sched"])
            if k in seen:
                continue
            seen.add(k)
            out[key].append(row)
    return {"IAD": out}
