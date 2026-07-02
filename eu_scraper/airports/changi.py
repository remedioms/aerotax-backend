"""
SIN — Singapore Changi Airport. Rich AWS AppSync GraphQL feed the site's own React
app calls (public x-api-key baked into the bundle):

    POST https://ca-appsync.lz.changiairport.com/graphql
    header  x-api-key: da2-umfoldhfsnhh7e3zgbtyr3p6um   (public, shipped in the site)
    query   getFlights(direction:"dep"|"arr", scheduled_date:"YYYY-MM-DD",
            page_size:"200", next_token:"…")  → { next_token, flights[...] }
    (LIVE-verified 2026-07-03: ~200/page, next_token cursor walks the whole day.)

Per record: flight_number ("TR260"), airline (IATA), airline_details{code,name},
airport (the OTHER airport's IATA!), airport_details{code,name}, display_gate /
current_gate, terminal ("1"/"2"/"3"/"4"), aircraft_type (ICAO type "B78X"/"E290"),
scheduled_date + scheduled_time (LOCAL), estimated_timestamp / actual_timestamp
(LOCAL "YYYY-MM-DD HH:MM"), flight_status ("Gate Closing"/"Landed"/…), via (stopover
IATA). Times are already Asia/Singapore LOCAL. No tail registration in this feed.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from .. import scraper as S

EP = "https://ca-appsync.lz.changiairport.com/graphql"
KEY = "da2-umfoldhfsnhh7e3zgbtyr3p6um"
WARMUP = "https://www.changiairport.com/en/fly/flight-information/departures.html"
ORIGIN = "https://www.changiairport.com"
TZ = "Asia/Singapore"
_MAX_PAGES = 20

_FIELDS = ("actual_timestamp aircraft_type airline airline_details{code name} "
           "airport airport_details{code name} current_gate display_gate direction "
           "estimated_timestamp flight_number flight_status scheduled_date "
           "scheduled_time terminal via")

_CANCEL = ("cancel",)
_DELAY = ("delay", "retim")


def _local(ts: str | None) -> str | None:
    """'2026-07-03 06:10' (already LOCAL) → '2026-07-03T06:10:00'."""
    if not ts:
        return None
    s = str(ts).strip().replace("T", " ")
    if not s or s in ("--", "null"):
        return None
    try:
        d, t = s.split(" ")[0], s.split(" ")[1]
        return f"{d}T{t[:5]}:00"
    except Exception:
        return None


def _row(rec: dict, arr: bool) -> dict | None:
    fn = (rec.get("flight_number") or "").strip().upper()
    if not fn:
        return None
    r = S.empty_row()
    ad = rec.get("airline_details") or {}
    r["airline"] = (rec.get("airline") or ad.get("code") or fn[:2]).strip().upper()
    r["airline_name"] = (ad.get("name") or "").strip()
    r["flight"] = S.norm_flight(fn, "")
    apd = rec.get("airport_details") or {}
    r["dest_iata"] = (rec.get("airport") or apd.get("code") or "").strip().upper()
    r["dest_name"] = (apd.get("name") or "").strip()
    r["sched"] = _local(f"{rec.get('scheduled_date','')} {rec.get('scheduled_time','')}")
    esti = _local(rec.get("actual_timestamp") or rec.get("estimated_timestamp"))
    if esti and esti != r["sched"]:
        r["esti"] = esti
    r["gate"] = (rec.get("display_gate") or rec.get("current_gate") or "").strip()
    r["terminal"] = (str(rec.get("terminal") or "")).strip()
    r["aircraft"] = (rec.get("aircraft_type") or "").strip().upper()
    st = (rec.get("flight_status") or "").strip()
    r["status"] = st
    low = st.lower()
    r["cancelled"] = any(h in low for h in _CANCEL)
    r["delayed"] = any(h in low for h in _DELAY)
    r["_tz"] = TZ
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r)


def _query(drv, direction: str, date: str, token: str | None):
    tok = f', next_token:"{token}"' if token else ""
    q = ('query { getFlights(direction: "%s", scheduled_date:"%s", page_size:"200"%s) '
         '{ next_token flights { %s } } }') % (direction, date, tok, _FIELDS)
    try:
        resp = drv._ctx.request.post(
            EP, data=json.dumps({"query": q}), timeout=25000,
            headers={"x-api-key": KEY, "content-type": "application/json",
                     "Origin": ORIGIN, "Referer": ORIGIN + "/"})
        if not resp.ok:
            return None
        return ((resp.json().get("data") or {}).get("getFlights")) or {}
    except Exception:
        return None


def scrape(drv) -> dict:
    drv.warm(WARMUP, wait_ms=2000)
    now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
    days = [(now - timedelta(days=1)).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")]
    out = {"departure": [], "arrival": []}
    for direction, key, arr in (("dep", "departure", False), ("arr", "arrival", True)):
        seen = set()
        for date in days:
            token = None
            for _ in range(_MAX_PAGES):
                data = _query(drv, direction, date, token)
                if not data:
                    break
                for rec in data.get("flights") or []:
                    row = _row(rec, arr)
                    if not row:
                        continue
                    k = (row["flight"], row["sched"])
                    if k in seen:
                        continue
                    seen.add(k)
                    out[key].append(row)
                token = data.get("next_token")
                if not token:
                    break
    return {"SIN": out}
