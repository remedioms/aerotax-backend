"""
BUD — Budapest Ferenc Liszt Airport. The board React app fetches a clean JSON list
(the departures page itself renders nothing server-side; the data comes via ajax):

    GET https://www.bud.hu/ajax/flight-search?locale=en&direction={departures|arrivals}&date=YYYY-MM-DD
    (LIVE-verified 2026-07-02.  `direction` MUST be the plural word — the schema
     rejects 'departure'/'D'/etc. `date` lets us pull today AND yesterday.)

Per record: planned (HH:MM LOCAL scheduled), expected (HH:MM LOCAL est/actual),
date (dd.mm.yyyy LOCAL), destination/city (the OTHER airport, name only — no IATA),
flightNumber ("BA 870"), registration (often ''), airline {name}, terminal ("2B"),
baggageClaim (arrivals) / gate (departures), status {type, text}, codeshareFlights.
"""
from __future__ import annotations

import re
from .. import scraper as S

BASE = "https://www.bud.hu/ajax/flight-search"
WARMUP = "https://www.bud.hu/en/passengers/flight-information/departures"
REFERER = WARMUP
TZ = "Europe/Budapest"

_CANCEL = ("cancel", "törölt", "torolt")
_DELAY = ("delay", "kés", "kes")


def _split_flight(fn: str):
    fn = (fn or "").strip()
    if " " in fn:
        al = fn.split(" ", 1)[0].upper()
    else:
        m = re.match(r"^([A-Z]+)\d", fn.upper())
        al = m.group(1) if m else fn[:2].upper()
    return al, S.norm_flight(fn, "")


def _row(rec: dict, arr: bool) -> dict | None:
    fn = (rec.get("flightNumber") or "").strip()
    if not fn:
        return None
    al, flight = _split_flight(fn)
    if not flight:
        return None
    r = S.empty_row()
    r["airline"] = al
    airline = rec.get("airline") or {}
    r["airline_name"] = (airline.get("name") or "").strip()
    r["flight"] = flight
    r["dest_name"] = (rec.get("destination") or rec.get("city") or "").strip()
    date = (rec.get("date") or "").replace(".", "/").strip("/")  # dd.mm.yyyy → dd/mm/yyyy
    r["sched"] = S.local_iso(date_ddmmyyyy=date, hhmmss=(rec.get("planned") or "") + ":00")
    exp = (rec.get("expected") or "").strip()
    if re.match(r"^\d{1,2}:\d{2}$", exp):
        esti = S.local_iso(date_ddmmyyyy=date, hhmmss=exp + ":00")
        if esti and esti != r["sched"]:
            r["esti"] = esti
    r["terminal"] = (rec.get("terminal") or "").strip()
    r["gate"] = ((rec.get("baggageClaim") if arr else rec.get("gate")) or "").strip()
    r["reg"] = (rec.get("registration") or "").strip().upper()
    st = rec.get("status") or {}
    r["status"] = (st.get("text") or st.get("type") or "").strip()
    low = (str(st.get("type", "")) + " " + str(st.get("text", ""))).lower()
    r["cancelled"] = any(h in low for h in _CANCEL)
    r["delayed"] = any(h in low for h in _DELAY)
    r["_tz"] = TZ
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r)


def scrape(drv) -> dict:
    drv.warm(WARMUP)
    from datetime import datetime, timedelta
    try:
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo(TZ))
    except Exception:
        today = datetime.utcnow()
    days = [(today - timedelta(days=1)).strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")]
    out = {"departure": [], "arrival": []}
    for direction, key, arr in (("departures", "departure", False), ("arrivals", "arrival", True)):
        seen = set()
        for d in days:
            url = f"{BASE}?locale=en&direction={direction}&date={d}"
            data = drv.get_json(url, referer=REFERER)
            if not isinstance(data, list):
                continue
            for rec in data:
                row = _row(rec, arr)
                if not row:
                    continue
                k = (row["flight"], row["sched"])
                if k in seen:
                    continue
                seen.add(k)
                out[key].append(row)
    return {"BUD": out}
