"""
KWI — Kuwait International Airport. The flight-status pages call ONE in-house JSON
that returns BOTH directions at once; each record is tagged by whether its
`departure` or `arrival` sub-object is populated:

    GET https://www.kuwaitairport.gov.kw/api/flights
    (LIVE-verified 2026-07-03.  Requires a STATIC Basic-auth header the site
     hard-codes in its own frontend: 'Basic a2lhd2ViOmRnY2FAMTIzNDU='  (kiaweb:…).
     Cloudflare-fronted → fetched via get_json_inpage from the warmed page.)

Per record: airline.iata ("KAC632" full) / code (ICAO) / name / number, and a
`departure` or `arrival` object with routes[0].airportCode (the OTHER IATA) +
airportName + city, terminal, gate, scheduled/estimated/actual (LOCAL naive iso),
flightStatus.status. No tail reg / aircraft type.
"""
from __future__ import annotations

from .. import scraper as S
from .. import airports_ref as REF

API = "https://www.kuwaitairport.gov.kw/api/flights"
WARMUP = "https://www.kuwaitairport.gov.kw/en/flights-info/flight-status/departures/"
AUTH = {"Authorization": "Basic a2lhd2ViOmRnY2FAMTIzNDU="}
TZ = "Asia/Kuwait"


def _iso(s, allow_midnight: bool = True) -> str | None:
    """'2026-07-03T14:55:00' (already LOCAL) → 'YYYY-MM-DDTHH:MM:00'. The feed uses
    'T00:00:00' as a NO-VALUE placeholder for estimated/actual, so those callers pass
    allow_midnight=False to drop it rather than invent a midnight estimate."""
    if not s:
        return None
    t = str(s).strip().replace(" ", "T")
    if len(t) < 16 or t[:4] < "2000":
        return None
    if not allow_midnight and t[11:16] == "00:00":
        return None
    return t[:16] + ":00"


def _row(sub: dict, airline: dict, arr: bool) -> dict | None:
    routes = sub.get("routes") or []
    other_code = (routes[0].get("airportCode") if routes else "") or ""
    other_name = (routes[0].get("airportName") if routes else "") or ""
    other_city = (routes[0].get("city") if routes else "") or ""
    iata_full = (airline.get("iata") or "").strip().upper()   # e.g. 'KAC632'
    code = (airline.get("code") or "").strip().upper()        # ICAO 'KAC'
    num = (airline.get("number") or "").strip()
    r = S.empty_row()
    # Build IATA-style flight: prefer an explicit 2-letter carrier if resolvable,
    # else fall back to the ICAO+number the feed gives.
    r["airline"] = code
    r["airline_name"] = (airline.get("name") or "").strip()
    r["flight"] = S.norm_flight(code, num) if code and num else S.norm_flight(iata_full, "")
    r["dest_iata"] = other_code.strip().upper() or (REF.resolve(other_name) or "")
    r["dest_name"] = (other_city or other_name).strip()
    r["sched"] = _iso(sub.get("scheduled"))
    est = _iso(sub.get("estimated"), allow_midnight=False) \
        or _iso(sub.get("actual"), allow_midnight=False)
    if est and est != r["sched"]:
        r["esti"] = est
    r["terminal"] = (sub.get("terminal") or "").strip()
    g = (sub.get("gate") or "").strip()
    r["gate"] = "" if g == r["terminal"] else g   # feed repeats the terminal as gate when unknown
    fs = (sub.get("flightStatus") or {}) or {}
    st = (fs.get("status") or "").strip()
    r["status"] = st
    low = st.lower()
    r["cancelled"] = "cancel" in low
    r["delayed"] = "delay" in low
    r["_tz"] = TZ
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r)


def scrape(drv) -> dict:
    drv.warm(WARMUP, wait_ms=5000)
    data = drv.get_json_inpage(API, headers=AUTH)
    out = {"departure": [], "arrival": []}
    result = (data or {}).get("result") if isinstance(data, dict) else None
    if not isinstance(result, list):
        return {"KWI": out}
    seen = {"departure": set(), "arrival": set()}
    for rec in result:
        airline = (rec.get("airline") or {}) or {}
        for key, arr in (("departure", False), ("arrival", True)):
            sub = rec.get(key)
            if not isinstance(sub, dict):
                continue
            row = _row(sub, airline, arr)
            if not row:
                continue
            k = (row["flight"], row["sched"])
            if k in seen[key]:
                continue
            seen[key].add(k)
            out[key].append(row)
    return {"KWI": out}
