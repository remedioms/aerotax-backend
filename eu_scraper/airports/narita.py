"""
NRT — Tokyo Narita International Airport. Next.js site behind an Akamai sensor; once
the board page is warmed, its own BFF JSON is fetchable through the warmed context:

    GET https://www.narita-airport.jp/api/bff/searchFlight/
        ?locale=en&domInter={I|D}&flightDepArr={D|A}&date=YYYY-MM-DD&page=0&size=100
    (LIVE-verified 2026-07-03. domInter I=international / D=domestic; flightDepArr
     D=departures / A=arrivals. Response { flights:{ data:[...], hasNextPage } } —
     page-walk until hasNextPage is false. All 4 domInter×dir combos cover the airport.)

Per record: flightCode ("UO0857"), airline{2LetterCode(IATA),3LetterCode(ICAO),name},
date + scheduledTime (LOCAL), airport.original.{3LetterCode(IATA),name} (the OTHER
airport), displayTerminal ("T2"/"T1"/"T3"), gate:[{gateNo}], status{status:"BOARDING"/
"DELAYED"/"CANCELED"/"LANDED"/…}, codeShare[]. This feed exposes no estimated/actual
clock (delay lives only in the status text) and no tail registration / aircraft type.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from .. import scraper as S
from .. import airports_ref as REF

API = "https://www.narita-airport.jp/api/bff/searchFlight/"
WARMUP = "https://www.narita-airport.jp/en/flight/"
REFERER = WARMUP
TZ = "Asia/Tokyo"
_MAX_PAGES = 30

_CANCEL = ("cancel", "canceled", "cancelled")
_DELAY = ("delay",)


def _row(rec: dict, arr: bool) -> dict | None:
    code = (rec.get("flightCode") or rec.get("displayFlightCode") or "").strip().upper()
    if not code:
        return None
    r = S.empty_row()
    al = rec.get("airline") or {}
    r["airline"] = (al.get("2LetterCode") or code[:2]).strip().upper()
    r["airline_name"] = (al.get("name") or "").strip().title()
    r["flight"] = S.norm_flight(code, "")
    ap = ((rec.get("airport") or {}).get("original")) or {}
    code3 = (ap.get("3LetterCode") or "").strip().upper()
    r["dest_name"] = (ap.get("name") or "").strip().title()
    r["dest_iata"] = REF.resolve(ap.get("name") or "", code=code3) or ""
    date = (rec.get("date") or "").strip()
    tm = (rec.get("scheduledTime") or "").strip()
    r["sched"] = f"{date}T{tm[:5]}:00" if (date and len(tm) >= 4) else None
    r["terminal"] = (rec.get("displayTerminal") or rec.get("terminalKey") or "").strip()
    gates = rec.get("gate") or []
    if gates and isinstance(gates, list):
        r["gate"] = (gates[0].get("gateNo") or "").strip()
    st = ((rec.get("status") or {}).get("status") or "").strip()
    r["status"] = st.title() if st else ""
    low = st.lower()
    r["cancelled"] = any(h in low for h in _CANCEL)
    r["delayed"] = any(h in low for h in _DELAY)
    r["_tz"] = TZ
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r)


def scrape(drv) -> dict:
    drv.warm(WARMUP, wait_ms=3000)
    now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9)))
    days = [(now - timedelta(days=1)).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")]
    out = {"departure": [], "arrival": []}
    for da, key, arr in (("D", "departure", False), ("A", "arrival", True)):
        seen = set()
        for di in ("I", "D"):
            for date in days:
                for page in range(_MAX_PAGES):
                    url = (f"{API}?locale=en&domInter={di}&flightDepArr={da}"
                           f"&date={date}&page={page}&size=100")
                    data = drv.get_json(url, referer=REFERER)
                    fl = (data or {}).get("flights") if isinstance(data, dict) else None
                    if not isinstance(fl, dict):
                        break
                    rows = fl.get("data") or []
                    for rec in rows:
                        row = _row(rec, arr)
                        if not row:
                            continue
                        k = (row["flight"], row["sched"])
                        if k in seen:
                            continue
                        seen.add(k)
                        out[key].append(row)
                    if not fl.get("hasNextPage") or not rows:
                        break
    return {"NRT": out}
