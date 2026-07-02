"""
YYC — Calgary Int'l. The site's DNN flight module exposes one JSON call that returns
BOTH directions (~2800 flights, LIVE-verified 2026-07-02). NB: the body is a
JSON-encoded STRING (double-encoded), so we json.loads twice.

    GET https://www.yyc.com/desktopmodules/YYC.ModulesDnn.YYC.Flights.Controllers/API/Flights/getFlights?<ts>

Per record: FlightNumber, AirlineIATACode, AirlineName, Leg ("D"/"A"),
AirportCode (the OTHER airport IATA), AirportName, ScheduledTimeLocal /
EstimatedTimeLocal / ActualTimeLocal, PrimaryGate, AircraftCode (type, e.g. "73W"),
LongPrimaryStatusTextEnglish ("Departed"/"Cancelled"/"Delayed"). No tail reg.
"""
from __future__ import annotations

import json
import time

from .. import scraper as S
from .. import airports_ref as REF

BASE = "https://www.yyc.com/desktopmodules/YYC.ModulesDnn.YYC.Flights.Controllers/API/Flights/getFlights"
WARMUP = "https://www.yyc.com/en-us/flights/departures.aspx"
TZ = "America/Edmonton"


def _local(s):
    if not s or str(s).strip() in ("", "null"):
        return None
    s = str(s).strip().replace(" ", "T")
    return (s[:16] + ":00") if len(s) >= 16 else None


def _row(rec: dict) -> dict | None:
    if rec.get("IsDeleted"):
        return None
    al = (rec.get("AirlineIATACode") or rec.get("AirlineCode") or "").strip().upper()
    num = str(rec.get("FlightNumber") or "").strip()
    if not num:
        return None
    arr = (rec.get("Leg") or "").upper().startswith("A")
    r = S.empty_row()
    r["airline"] = al
    r["airline_name"] = (rec.get("AirlineName") or "").strip()
    r["flight"] = S.norm_flight(al or num[:2], num)
    code = (rec.get("AirportCode") or "").strip().upper()
    name = (rec.get("AirportName") or "").strip()
    r["dest_name"] = name
    r["dest_iata"] = REF.resolve(name, code=code) or code
    r["sched"] = _local(rec.get("ScheduledTimeLocal"))
    est = rec.get("ActualTimeLocal") or rec.get("EstimatedTimeLocal")
    esti = _local(est)
    if esti and esti != r["sched"]:
        r["esti"] = esti
    r["gate"] = (rec.get("PrimaryGate") or "").strip()
    r["aircraft"] = (rec.get("AircraftCode") or "").strip().upper()
    st = (rec.get("LongPrimaryStatusTextEnglish") or rec.get("ShortPrimaryStatusTextEnglish") or "").strip()
    r["status"] = st
    low = st.lower()
    r["cancelled"] = "cancel" in low or (rec.get("FlightStatus") or "").upper() == "CX"
    r["delayed"] = "delay" in low
    r["_tz"] = TZ
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r)


def scrape(drv) -> dict:
    drv.warm(WARMUP, wait_ms=1800)
    data = drv.get_json(f"{BASE}?{int(time.time()*1000)}", referer=WARMUP)
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = None
    out = {"departure": [], "arrival": []}
    seen = set()
    for rec in (data or []):
        row = _row(rec)
        if not row:
            continue
        arr = (rec.get("Leg") or "").upper().startswith("A")
        k = (arr, row["flight"], row["sched"])
        if k in seen:
            continue
        seen.add(k)
        out["arrival" if arr else "departure"].append(row)
    return {"YYC": out}
