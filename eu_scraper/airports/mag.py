"""
MAG — Manchester Airports Group. ONE AWS AppSync GraphQL endpoint (with a public
x-api-key baked into the sites) covers ALL three MAG airports:
    MAN (Manchester), STN (London Stansted), EMA (East Midlands).

Endpoint (LIVE-verified 2026-07-02):
    POST https://nihwye5mfbajrg54x3fjcy4q5e.appsync-api.eu-west-1.amazonaws.com/graphql
    header  x-api-key: da2-wr4hf6b2frdfdisv7ugsvdmo3a   (public, shipped in the bundle)
    query   allDeparturesWithinMonth / allArrivalsWithinMonth(tenant, startDate,
            endDate, size)  — a date RANGE, so we pull yesterday+today in full.

Per record: flightNumber, scheduled/estimated/actual{Departure|Arrival}DateTime (UTC),
arrival/departureAirport{cityName, code}, airline{name, code}, departureGate{number},
arrival/departureTerminal, status ("Arrived 02:53"/"Departed"/"Cancelled"). Full gate
coverage on departures.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from .. import scraper as S

GQL = "https://nihwye5mfbajrg54x3fjcy4q5e.appsync-api.eu-west-1.amazonaws.com/graphql"
APIKEY = "da2-wr4hf6b2frdfdisv7ugsvdmo3a"
WARMUP = "https://www.manchesterairport.co.uk/flight-information/departures/"
ORIGIN = "https://www.manchesterairport.co.uk"
TZ = "Europe/London"
DEFAULT = ["MAN", "STN", "EMA"]

_DEP_Q = ("query($t:String!,$s:AWSDateTime,$e:AWSDateTime,$size:Int){"
          "allDeparturesWithinMonth(tenant:$t,startDate:$s,endDate:$e,size:$size){"
          "flightNumber scheduledDepartureDateTime estimatedDepartureDateTime "
          "actualDepartureDateTime arrivalAirport{cityName code} airline{name code} "
          "departureGate{number} departureTerminal status}}")
_ARR_Q = ("query($t:String!,$s:AWSDateTime,$e:AWSDateTime,$size:Int){"
          "allArrivalsWithinMonth(tenant:$t,startDate:$s,endDate:$e,size:$size){"
          "flightNumber scheduledArrivalDateTime estimatedArrivalDateTime "
          "actualArrivalDateTime departureAirport{cityName code} airline{name code} "
          "arrivalTerminal status}}")


def _row(rec: dict, arr: bool) -> dict | None:
    fn = (rec.get("flightNumber") or "").strip()
    if not fn:
        return None
    r = S.empty_row()
    al = rec.get("airline") or {}
    r["airline"] = (al.get("code") or fn[:2]).strip().upper()
    r["airline_name"] = (al.get("name") or "").strip().title()
    r["flight"] = S.norm_flight(fn, "")
    other = (rec.get("departureAirport") if arr else rec.get("arrivalAirport")) or {}
    r["dest_iata"] = (other.get("code") or "").strip().upper()
    r["dest_name"] = (other.get("cityName") or "").strip().title()
    if arr:
        sched = rec.get("scheduledArrivalDateTime")
        est = rec.get("actualArrivalDateTime") or rec.get("estimatedArrivalDateTime")
        r["terminal"] = (rec.get("arrivalTerminal") or "").strip()
    else:
        sched = rec.get("scheduledDepartureDateTime")
        est = rec.get("actualDepartureDateTime") or rec.get("estimatedDepartureDateTime")
        r["terminal"] = (rec.get("departureTerminal") or "").strip()
        g = rec.get("departureGate") or {}
        r["gate"] = (g.get("number") or "").strip()
    r["sched"] = S.utc_to_local_iso(sched, TZ)
    esti = S.utc_to_local_iso(est, TZ)
    if esti and esti != r["sched"]:
        r["esti"] = esti
    st = (rec.get("status") or "").strip()
    r["status"] = st
    low = st.lower()
    r["cancelled"] = "cancel" in low
    r["delayed"] = "delay" in low
    r["_tz"] = TZ
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r)


def _query(drv, tenant: str, q: str, field: str, sd: str, ed: str):
    body = json.dumps({"query": q, "variables": {"t": tenant, "s": sd, "e": ed, "size": 3000}})
    try:
        resp = drv._ctx.request.post(
            GQL, data=body, timeout=25000,
            headers={"x-api-key": APIKEY, "Content-Type": "application/json",
                     "Origin": ORIGIN, "Referer": WARMUP})
        if not resp.ok:
            return []
        j = resp.json()
        return ((j.get("data") or {}).get(field)) or []
    except Exception:
        return []


def scrape(drv, airports=None) -> dict:
    drv.warm(WARMUP, wait_ms=1500)
    now = datetime.now(timezone.utc)
    sd = (now - timedelta(days=2)).strftime("%Y-%m-%dT00:00:00.000Z")
    ed = (now + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00.000Z")
    codes = airports or DEFAULT
    res = {}
    for tenant in codes:
        out = {"departure": [], "arrival": []}
        for q, field, key, arr in (
                (_DEP_Q, "allDeparturesWithinMonth", "departure", False),
                (_ARR_Q, "allArrivalsWithinMonth", "arrival", True)):
            seen = set()
            for rec in _query(drv, tenant, q, field, sd, ed):
                row = _row(rec, arr)
                if not row:
                    continue
                k = (row["flight"], row["sched"])
                if k in seen:
                    continue
                seen.add(k)
                out[key].append(row)
        res[tenant] = out
    return res
