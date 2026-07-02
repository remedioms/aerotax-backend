"""
ZRH — Zurich Airport. One open JSON endpoint, ~4400 flights spanning ~5 days,
with FULL fields incl. tail registration and gate/terminal:

    GET https://flightdata.flughafen-zuerich.ch/flights   (LIVE-verified 2026-07-02)

Per record: flightType (A=arrival / D=departure), FLC (airline IATA), FLN (flight
no), POR (other airport IATA), cityEn, STA/STD (scheduled UTC ISO), ETA/ETD
(estimated UTC), ATA/ATD (actual UTC), GAT (gate), TER (terminal), REG (tail!),
ICT/TYS/TYP (aircraft type), statusCode / statusTextEn, airline (name).

Times are UTC → converted to Europe/Zurich local. This feed is not bot-walled but
we still fetch via the warmed context for uniformity.
"""
from __future__ import annotations

from .. import scraper as S

URL = "https://flightdata.flughafen-zuerich.ch/flights"
TZ = "Europe/Zurich"
_CANCEL_HINT = ("cancel", "annull")


def _row(rec: dict) -> dict | None:
    arr = (rec.get("flightType") or "").upper().startswith("A")
    al = (rec.get("FLC") or "").strip().upper()
    num = (rec.get("FLN") or "").strip()
    if not (al and num):
        return None
    r = S.empty_row()
    r["airline"] = al
    r["airline_name"] = (rec.get("airline") or "").strip()
    r["flight"] = S.norm_flight(al, num)
    # The "other" airport lives in POR for arrivals (origin) and PDS for
    # departures (destination) — POR is always null on departure records.
    r["dest_iata"] = ((rec.get("POR") if arr else rec.get("PDS")) or "").strip().upper()
    r["dest_name"] = (rec.get("cityEn") or rec.get("airportName") or "").strip()
    sched_utc = rec.get("STA") if arr else rec.get("STD")
    r["sched"] = S.utc_to_local_iso(sched_utc or rec.get("STA") or rec.get("STD"), TZ)
    # Prefer the ACTUAL time once known (real delay), else the estimate.
    est_utc = (rec.get("ATA") if arr else rec.get("ATD")) \
        or (rec.get("ETA") if arr else rec.get("ETD"))
    esti = S.utc_to_local_iso(est_utc, TZ)
    if esti and esti != r["sched"]:
        r["esti"] = esti
    r["gate"] = (rec.get("GAT") or "").strip()
    r["terminal"] = (rec.get("TER") or "").strip()
    r["reg"] = (rec.get("REG") or "").strip().upper()
    r["aircraft"] = (rec.get("ICT") or rec.get("TYS") or rec.get("TYP") or "").strip().upper()
    st = (rec.get("statusTextEn") or rec.get("statusCode") or "").strip()
    r["status"] = st
    low = st.lower()
    r["cancelled"] = any(h in low for h in _CANCEL_HINT)
    r["delayed"] = ("delay" in low)
    r["_tz"] = TZ
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r)


def scrape(drv) -> dict:
    drv.warm("https://www.flughafen-zuerich.ch/", wait_ms=1500)
    data = drv.get_json(URL, referer="https://www.flughafen-zuerich.ch/")
    out = {"departure": [], "arrival": []}
    if not isinstance(data, list):
        return {"ZRH": out}
    seen = set()
    for rec in data:
        if rec.get("isCommercial") is False:
            continue
        row = _row(rec)
        if not row:
            continue
        arr = (rec.get("flightType") or "").upper().startswith("A")
        k = (arr, row["flight"], row["sched"])
        if k in seen:
            continue
        seen.add(k)
        out["arrival" if arr else "departure"].append(row)
    return {"ZRH": out}
