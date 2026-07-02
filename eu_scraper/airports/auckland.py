"""
AKL — Auckland Airport. One AEM-backed JSON endpoint returns the WHOLE board (both
directions) in a single call the site makes on load:

    GET .../content/aial/api/v1/flights?date=YYYY-MM-DD&terminal={I|D}&flightDirection={D|A}
    (LIVE-verified 2026-07-03. terminal I=international / D=domestic; flightDirection
     D=departures / A=arrivals. A bare `?` defaults to international arrivals only, so
     we fetch all 4 terminal×direction combos. The endpoint sits behind Cloudflare bot
     management: a raw ctx.request.get gets a 403 "Just a moment" challenge, so we
     fetch it from INSIDE the warmed browser page via page.evaluate, which carries the
     full browser fingerprint + cf_clearance cookie → 200.)

Per record: FlightNumber (digits only), Airline (IATA), Terminal ("T1"/"T2"),
PassengerGate, DepartureOrArrival ("A"/"D"), Airport:[{Code(IATA),Name}] (the OTHER
airport), OperationTime:[{Type:Scheduled|Estimated|Actual, DateTime:"Jul 2, 2026,
10:55:00 PM"}] (LOCAL, human format), FlightStatusComment ("Landed"/"Cancelled"/…),
BaggageClaim, CodeShare. No tail registration / aircraft type in this feed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from .. import scraper as S

API = "/content/aial/api/v1/flights"
WARMUP = "https://www.aucklandairport.co.nz/flights"
REFERER = WARMUP
TZ = "Pacific/Auckland"


def _parse_dt(s: str | None) -> str | None:
    """'Jul 2, 2026, 10:55:00 PM' (LOCAL) → '2026-07-02T22:55:00'."""
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%b %d, %Y, %I:%M:%S %p", "%b %d, %Y, %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%dT%H:%M:00")
        except Exception:
            continue
    return None


def _times(rec: dict) -> dict:
    out = {}
    for t in rec.get("OperationTime") or []:
        typ = (t.get("Type") or "").lower()
        if typ:
            out[typ] = _parse_dt(t.get("DateTime"))
    return out


def _row(rec: dict) -> tuple[dict, bool] | None:
    num = (str(rec.get("FlightNumber") or "")).strip()
    al = (rec.get("Airline") or "").strip().upper()
    if not num or not al:
        return None
    arr = (rec.get("DepartureOrArrival") or "").upper().startswith("A")
    r = S.empty_row()
    r["airline"] = al
    r["flight"] = S.norm_flight(al, num)
    ap = (rec.get("Airport") or [{}])[0] or {}
    r["dest_iata"] = (ap.get("Code") or "").strip().upper()
    r["dest_name"] = (ap.get("Name") or "").strip()
    tm = _times(rec)
    r["sched"] = tm.get("scheduled")
    esti = tm.get("actual") or tm.get("estimated")
    if esti and esti != r["sched"]:
        r["esti"] = esti
    r["terminal"] = (rec.get("Terminal") or "").strip()
    r["gate"] = (str(rec.get("PassengerGate") or "")).strip()
    st = (rec.get("FlightStatusComment") or rec.get("FlightStatus") or "").strip()
    r["status"] = st
    low = st.lower()
    r["cancelled"] = "cancel" in low
    r["delayed"] = "delay" in low
    r["_tz"] = TZ
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r), arr


def _fetch(pg, url: str):
    try:
        return pg.evaluate(
            "async (u) => { const r = await fetch(u, {headers:{'Accept':'application/json'}});"
            " return r.ok ? await r.json() : null; }", url)
    except Exception:
        return None


def scrape(drv) -> dict:
    pg = drv.render(WARMUP, wait_ms=5000)
    now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=12)))
    days = [(now - timedelta(days=1)).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")]
    out = {"departure": [], "arrival": []}
    seen = set()
    for direction in ("D", "A"):
        for term in ("I", "D"):
            for date in days:
                url = f"{API}?date={date}&terminal={term}&flightDirection={direction}"
                data = _fetch(pg, url)
                if not isinstance(data, list):
                    continue
                for rec in data:
                    res = _row(rec)
                    if not res:
                        continue
                    row, arr = res
                    k = (arr, row["flight"], row["sched"])
                    if k in seen:
                        continue
                    seen.add(k)
                    out["arrival" if arr else "departure"].append(row)
    return {"AKL": out}
