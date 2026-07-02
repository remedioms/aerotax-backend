"""
VIE — Vienna Airport. JSON feed the site's own board calls (needs the warmed
browser context; plain curl gets redirected/empty):

    GET https://www.viennaairport.com/jart/prj3/va/data/flights/out.json  (departures)

Verified 2026-07-02: 476 departures spanning today + 2 days. Per record: fn
("OS 867"), scheduledatetime (UTC) + schedule (LOCAL), actualdatetime/actual,
gate ("F16"), status {code,description}, aircraft {type "E95"}, airline {iataCode},
destinations[0].iataCode + nameEN, checkin.terminal.

ARRIVALS: the arrivals board endpoint is not exposed under the same jart path
(in.json 404s) — VIE ships DEPARTURES only for now (honest). A `dummy` cache-buster
is appended like the site does.
"""
from __future__ import annotations

import time
from .. import scraper as S

BASE = "https://www.viennaairport.com/jart/prj3/va/data/flights/"
WARMUP = "https://www.viennaairport.com/en/passengers/arrivals__and__departures/departures"
REFERER = "https://www.viennaairport.com/"
TZ = "Europe/Vienna"


def _row(rec: dict, arr: bool) -> dict | None:
    fn = (rec.get("fn") or "").strip()
    if not fn:
        return None
    r = S.empty_row()
    al = fn[:2].strip().upper()
    r["airline"] = al
    airline = rec.get("airline") or {}
    r["airline_name"] = (airline.get("name") or "").strip()
    r["flight"] = S.norm_flight(fn, "")
    dests = rec.get("destinations") or []
    if dests:
        r["dest_iata"] = (dests[0].get("iataCode") or "").strip().upper()
        r["dest_name"] = (dests[0].get("nameEN") or dests[0].get("name") or "").strip()
    # `schedule` is already LOCAL wall-clock (no Z) → take the clock directly.
    r["sched"] = S.utc_to_local_iso(rec.get("scheduledatetime"), TZ)
    esti = S.utc_to_local_iso(rec.get("actualdatetime"), TZ)
    if esti and esti != r["sched"]:
        r["esti"] = esti
    r["gate"] = (rec.get("gate") or "").strip()
    ci = rec.get("checkin") or {}
    r["terminal"] = (ci.get("terminal") or "").strip()
    ac = rec.get("aircraft") or {}
    r["aircraft"] = (ac.get("type") or "").strip().upper()
    st = rec.get("status") or {}
    desc = (st.get("description") or st.get("code") or "").strip()
    r["status"] = desc.title() if desc else ""
    low = desc.lower()
    r["cancelled"] = "cancel" in low
    r["delayed"] = "delay" in low
    r["_tz"] = TZ
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r)


def scrape(drv) -> dict:
    drv.warm(WARMUP)
    out = {"departure": [], "arrival": []}
    for fname, key, arr in (("out.json", "departure", False), ("in.json", "arrival", True)):
        url = f"{BASE}{fname}?dummy=ax{int(time.time())}"
        data = drv.get_json(url, referer=REFERER)
        if not isinstance(data, dict):
            continue
        rows = (data.get("monitor") or {}).get(key) or []
        seen = set()
        for rec in rows:
            row = _row(rec, arr)
            if not row:
                continue
            k = (row["flight"], row["sched"])
            if k in seen:
                continue
            seen.add(k)
            out[key].append(row)
    return {"VIE": out}
