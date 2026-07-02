"""
ANA Aeroportos de Portugal — THE Portuguese group win. ONE proxy endpoint pattern
covers EVERY ANA airport (LIS/OPO/FAO/FNC/PDL/TER/HOR/PXO/SMA/FLW/…) — mainland,
Madeira and the Azores.

Endpoint (discovered from the ANA board React app, LIVE-verified 2026-07-02):
    GET https://www.lisbonairport.pt/en/flights_proxy?day={hoje|ontem}&movtype={A|D}&IATA=<IATA>
    movtype A = Arrivals, D = Departures.
    day hoje = today (rolling window from ~now forward), ontem = yesterday (full day).
    (The proxy lives on the lisbonairport.pt host but serves ANY ANA airport by IATA;
     it is Imperva/Incapsula-fronted → fetch THROUGH the warmed browser context.)

Each record: day (dd/mm/yyyy LOCAL), time (HH:MM LOCAL scheduled), terminal ("T1"),
flightNumber ("QR 343" / "EZY3164"), destination (the OTHER city, name only — no IATA),
movtype, airline (name), state {label, value} where value is the actual/estimated
HH:MM (leading space). NO gate, NO aircraft type, NO tail reg in this feed (honest).
"""
from __future__ import annotations

import json
import re
from .. import scraper as S
from .. import airports_ref as REF

HOST = "https://www.lisbonairport.pt"
PROXY = HOST + "/en/flights_proxy"
WARMUP = HOST + "/en/lis/flights-destinations/find-flights/real-time-arrivals"
REFERER = HOST + "/"

# Every ANA-operated airport with a live board. Small aerodromes (BGC/VRL/VSE/CHV)
# stay in the sweep but usually return 0 rows — kept so a new route lights up
# automatically. tz documents each island group (times are already LOCAL).
_TZ = {
    "LIS": "Europe/Lisbon", "OPO": "Europe/Lisbon", "FAO": "Europe/Lisbon",
    "FNC": "Atlantic/Madeira", "PXO": "Atlantic/Madeira",
    "PDL": "Atlantic/Azores", "TER": "Atlantic/Azores", "HOR": "Atlantic/Azores",
    "SMA": "Atlantic/Azores", "FLW": "Atlantic/Azores", "CVU": "Atlantic/Azores",
}
DEFAULT = ["LIS", "OPO", "FAO", "FNC", "PDL", "TER", "HOR", "PXO", "SMA", "FLW", "CVU"]

_CANCEL_HINT = ("cancel", "cancelad")
_DELAY_HINT = ("delay", "atras")


def _split_flight(fn: str):
    """'QR 343' → ('QR','QR343'); 'EZY3164' → ('EZY','EZY3164')."""
    fn = (fn or "").strip()
    if " " in fn:
        al = fn.split(" ", 1)[0].upper()
    else:
        m = re.match(r"^([A-Z]+)\d", fn.upper())
        al = m.group(1) if m else fn[:2].upper()
    return al, S.norm_flight(fn, "")


def _row(rec: dict, iata: str, tz: str, arr: bool) -> dict | None:
    fn = (rec.get("flightNumber") or "").strip()
    if not fn:
        return None
    al, flight = _split_flight(fn)
    if not flight:
        return None
    r = S.empty_row()
    r["airline"] = al
    r["airline_name"] = (rec.get("airline") or "").strip().title()
    r["flight"] = flight
    # ANA gives only a city/airport NAME (no IATA field). Resolve it → IATA so the
    # row is route-usable (origin for arrivals, destination for departures). Fail-safe:
    # unresolved → "" (never a wrong code).
    r["dest_name"] = (rec.get("destination") or "").strip()
    r["dest_iata"] = REF.resolve(r["dest_name"]) or ""
    r["sched"] = S.local_iso(date_ddmmyyyy=(rec.get("day") or "").replace(".", "/"),
                             hhmmss=(rec.get("time") or "") + ":00")
    st = rec.get("state") or {}
    label = (st.get("label") or "").strip()
    val = (st.get("value") or "").strip()
    if re.match(r"^\d{1,2}:\d{2}$", val):  # actual/estimated clock on the same day
        esti = S.local_iso(date_ddmmyyyy=(rec.get("day") or "").replace(".", "/"),
                           hhmmss=val + ":00")
        if esti and esti != r["sched"]:
            r["esti"] = esti
    r["terminal"] = (rec.get("terminal") or "").strip()
    r["status"] = label
    low = label.lower()
    r["cancelled"] = any(h in low for h in _CANCEL_HINT)
    r["delayed"] = any(h in low for h in _DELAY_HINT)
    r["_tz"] = tz
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r)


def _airport(drv, iata: str) -> dict:
    tz = _TZ.get(iata, "Europe/Lisbon")
    out = {"departure": [], "arrival": []}
    for mv, key, arr in (("D", "departure", False), ("A", "arrival", True)):
        seen = set()
        for day in ("ontem", "hoje"):
            url = f"{PROXY}?day={day}&movtype={mv}&IATA={iata}"
            data = drv.get_json(url, referer=REFERER)
            if not isinstance(data, dict):
                # proxy sometimes returns text/html-typed JSON → get_json handles it
                continue
            for rec in (data.get("flights") or []):
                row = _row(rec, iata, tz, arr)
                if not row:
                    continue
                k = (row["flight"], row["sched"])
                if k in seen:
                    continue
                seen.add(k)
                out[key].append(row)
    return out


def scrape(drv, airports=None) -> dict:
    drv.warm(WARMUP)
    codes = airports or DEFAULT
    res = {}
    for iata in codes:
        try:
            res[iata] = _airport(drv, iata)
        except Exception as e:
            res[iata] = {"departure": [], "arrival": [], "error": str(e)}
    return res
