"""
Fruition FIDS vendor (app.flyfruition.com). The DEN (Denver) site's board calls it
directly; the vendor also powers other US airport sites. One public REST call with a
site-scoped api-key returns the FULL board incl. TAIL registration + aircraft type
(LIVE-verified 2026-07-02):

    GET https://app.flyfruition.com/api/public/flights
        header  x-api-key: <site key shipped in the bundle>

Per record: DIRECTION ("A"/"D"), AIRLINE_CODE, AIRLINE_NAME, FLIGHT_NUMBER,
SCHEDULED_TIME / ESTIMATED_TIME / ACTUAL_TIME (LOCAL naive ISO), AIRPORT_CODE (the
OTHER airport IATA), AIRPORT_CITY, GATE, FLIGHT_STATUS, REMARKS,
AIRCRAFT_REGISTRATION (TAIL!), AIRCRAFT_SUBTYPECODE (e.g. "32N"). ~2900 flights.
"""
from __future__ import annotations

from .. import scraper as S
from .. import airports_ref as REF

API = "https://app.flyfruition.com/api/public/flights"

# key → (x-api-key, warm-referer, tz)
AIRPORTS = {
    "DEN": ("vqw8ruvwqpv02pqu938bh5p028", "https://www.flydenver.com/arrivals/", "America/Denver"),
}
DEFAULT = list(AIRPORTS.keys())


def _local(iso, tzname):
    """Fruition times are already LOCAL naive ISO; normalize to :00 seconds."""
    if not iso or str(iso).strip() in ("", "null"):
        return None
    s = str(iso).strip().replace(" ", "T")
    return (s[:16] + ":00") if len(s) >= 16 else None


def _row(rec: dict, tzname: str) -> dict | None:
    al = (rec.get("AIRLINE_CODE") or "").strip().upper()
    num = str(rec.get("FLIGHT_NUMBER") or "").strip()
    if not num:
        return None
    arr = (rec.get("DIRECTION") or "").upper().startswith("A")
    r = S.empty_row()
    r["airline"] = al
    r["airline_name"] = (rec.get("AIRLINE_NAME") or "").strip()
    r["flight"] = S.norm_flight(al or num[:2], num)
    code = (rec.get("AIRPORT_CODE") or "").strip().upper()
    r["dest_name"] = (rec.get("AIRPORT_CITY") or "").strip()
    r["dest_iata"] = REF.resolve(r["dest_name"], code=code) or code
    r["sched"] = _local(rec.get("SCHEDULED_TIME"), tzname)
    est = rec.get("ACTUAL_TIME") or rec.get("ESTIMATED_TIME")
    esti = _local(est, tzname)
    if esti and esti != r["sched"]:
        r["esti"] = esti
    r["gate"] = (rec.get("GATE") or "").strip()
    r["reg"] = (rec.get("AIRCRAFT_REGISTRATION") or "").strip().upper()
    r["aircraft"] = (rec.get("AIRCRAFT_SUBTYPECODE") or "").strip().upper()
    st = (rec.get("FLIGHT_STATUS") or "").strip()
    r["status"] = st
    low = (st + " " + (rec.get("REMARKS") or "")).lower()
    r["cancelled"] = "cancel" in low
    r["delayed"] = "delay" in low
    r["_tz"] = tzname
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r)


def scrape(drv, airports=None) -> dict:
    res = {}
    for code in (airports or DEFAULT):
        code = code.upper()
        if code not in AIRPORTS:
            continue
        key, warm, tzname = AIRPORTS[code]
        drv.warm(warm, wait_ms=1800)
        out = {"departure": [], "arrival": []}
        try:
            resp = drv._ctx.request.get(API, timeout=25000, headers={
                "x-api-key": key, "Accept": "application/json", "Referer": warm})
            data = resp.json() if resp.ok else []
        except Exception:
            data = []
        seen = set()
        for rec in (data or []):
            row = _row(rec, tzname)
            if not row:
                continue
            arr = (rec.get("DIRECTION") or "").upper().startswith("A")
            k = (arr, row["flight"], row["sched"])
            if k in seen:
                continue
            seen.add(k)
            out["arrival" if arr else "departure"].append(row)
        res[code] = out
    return res
