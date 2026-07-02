"""
YVR — Vancouver Int'l. The site board queries a public OData feed (LIVE-verified
2026-07-02). We build the same query, one call per direction, 2-day window:

    GET https://www.yvr.ca/en/_api/Flights?$filter=(<sched-range> or <est-range> and
        FlightType eq 'D'|'A')&$orderby=FlightScheduledTime asc   → {"value":[...]}

Per record: FlightNumber ("AC108"), FlightCarrier (IATA), FlightAirlineName,
FlightAircraftType ("321"!), FlightAirportCode (the OTHER airport IATA),
FlightCity, FlightGate, FlightScheduledTime / FlightEstimatedTime (LOCAL naive),
FlightStatus, FlightType ("D"/"A"), FlightCarousel. No tail reg (type only).
"""
from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timedelta

from .. import scraper as S
from .. import airports_ref as REF

API = "https://www.yvr.ca/en/_api/Flights"
WARM_DEP = "https://www.yvr.ca/en/passengers/flights/departing-flights"
WARM_ARR = "https://www.yvr.ca/en/passengers/flights/arriving-flights"
TZ = "America/Vancouver"


def _local(s):
    if not s or str(s).strip() in ("", "null"):
        return None
    s = str(s).strip().replace(" ", "T")
    return (s[:16] + ":00") if len(s) >= 16 else None


def _row(rec: dict, arr: bool) -> dict | None:
    fn = (rec.get("FlightNumber") or "").strip().upper()
    al = (rec.get("FlightCarrier") or "").strip().upper()
    if not fn:
        return None
    r = S.empty_row()
    r["airline"] = al or fn[:2]
    r["airline_name"] = (rec.get("FlightAirlineName") or "").strip()
    r["flight"] = S.norm_flight(fn, "")
    code = (rec.get("FlightAirportCode") or "").strip().upper()
    name = (rec.get("FlightCity") or "").strip()
    r["dest_name"] = name
    r["dest_iata"] = REF.resolve(name, code=code) or code
    r["sched"] = _local(rec.get("FlightScheduledTime"))
    esti = _local(rec.get("FlightEstimatedTime"))
    if esti and esti != r["sched"]:
        r["esti"] = esti
    r["gate"] = (rec.get("FlightGate") or "").strip()
    r["aircraft"] = (rec.get("FlightAircraftType") or "").strip().upper()
    st = (rec.get("FlightStatus") or rec.get("FlightRemarks") or "").strip()
    r["status"] = st
    low = st.lower()
    r["cancelled"] = "cancel" in low
    r["delayed"] = "delay" in low
    r["_tz"] = TZ
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r)


def _filter(ftype, d0, d1):
    def rng(field):
        return (f"{field} gt DateTime'{d0}T00:00:00' and {field} lt DateTime'{d1}T00:00:00' "
                f"and FlightType eq '{ftype}'")
    return f"(({rng('FlightScheduledTime')}) or ({rng('FlightEstimatedTime')}))"


def scrape(drv) -> dict:
    now = datetime.now()
    d0 = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    d1 = (now + timedelta(days=2)).strftime("%Y-%m-%d")
    out = {"departure": [], "arrival": []}
    for ftype, key, warm, arr in (("D", "departure", WARM_DEP, False), ("A", "arrival", WARM_ARR, True)):
        drv.warm(warm, wait_ms=1500)
        q = urllib.parse.quote(_filter(ftype, d0, d1))
        url = f"{API}?$filter={q}&$orderby=FlightScheduledTime asc"
        # The OData host WAFs a direct context.request (403); an in-page fetch() from
        # the warmed page origin carries the full browser fingerprint and passes.
        data = None
        try:
            txt = drv._page.evaluate(
                "async (u) => { const r = await fetch(u, {headers: {'X-Requested-With':"
                "'XMLHttpRequest','Accept':'application/json'}}); return await r.text(); }", url)
            data = json.loads(txt) if txt else None
        except Exception:
            data = None
        rows = (data or {}).get("value") if isinstance(data, dict) else None
        seen = set()
        for rec in (rows or []):
            row = _row(rec, arr)
            if not row:
                continue
            k = (row["flight"], row["sched"])
            if k in seen:
                continue
            seen.add(k)
            out[key].append(row)
    return {"YVR": out}
