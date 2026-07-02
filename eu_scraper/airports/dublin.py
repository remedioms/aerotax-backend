"""
DUB — Dublin Airport (daa). Next.js/app-router site backed by a clean public JSON
API the client calls directly:

    GET https://api.dublinairport.com/dap/flight-listing/{arrivals|departures}
        ?date=YYYY-MM-DD&limit=100
        &before=<ISO-UTC>   ← page BACKWARD through the day (pagination cursor)
    (LIVE-verified 2026-07-02.  limit caps ~200; the base call returns the live
     window near 'now', and `before=<earliestTimestamp>` walks to earlier flights.)

Per record: flightIdentity ("EI183"), carrierCode ("EI"), carrierName, airportCode
(the OTHER airport IATA!), scheduledDateTime / estimatedDateTime (UTC ISO Z),
origin/destinationAirportName, terminalName ("T2"), gate ("307", departures),
baggageBelt ("5", arrivals), statusMessage ("DELAYED"/"CANCELLED"/…), isDelayed.
No tail reg / aircraft type in this feed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from .. import scraper as S

API = "https://api.dublinairport.com/dap/flight-listing"
WARMUP = "https://www.dublinairport.com/flight-information/live-arrivals"
REFERER = WARMUP
TZ = "Europe/Dublin"
_MAX_PAGES = 8  # 8×100 ≈ a full day per direction/date


def _row(rec: dict, arr: bool) -> dict | None:
    al = (rec.get("carrierCode") or "").strip().upper()
    fid = (rec.get("flightIdentity") or "").strip().upper()
    if not fid:
        return None
    r = S.empty_row()
    r["airline"] = al or fid[:2]
    r["airline_name"] = (rec.get("carrierName") or "").strip()
    r["flight"] = S.norm_flight(fid, "")
    r["dest_iata"] = (rec.get("airportCode") or "").strip().upper()
    r["dest_name"] = ((rec.get("originAirportName") if arr else rec.get("destinationAirportName")) or "").strip()
    r["sched"] = S.utc_to_local_iso(rec.get("scheduledDateTime"), TZ)
    esti = S.utc_to_local_iso(rec.get("estimatedDateTime"), TZ)
    if esti and esti != r["sched"]:
        r["esti"] = esti
    r["terminal"] = (rec.get("terminalName") or "").strip()
    r["gate"] = ((rec.get("baggageBelt") if arr else rec.get("gate")) or "").strip()
    msg = (rec.get("statusMessage") or "").strip()
    r["status"] = msg.title() if msg else ""
    low = msg.lower()
    r["cancelled"] = "cancel" in low
    r["delayed"] = bool(rec.get("isDelayed")) or "delay" in low
    r["_tz"] = TZ
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r)


def _fetch_day(drv, mv: str, date_str: str, arr: bool, out_key: str, out: dict, seen: set):
    before = None
    for _ in range(_MAX_PAGES):
        url = f"{API}/{mv}?date={date_str}&limit=100"
        if before:
            url += f"&before={before}"
        data = drv.get_json(url, referer=REFERER)
        if not isinstance(data, dict):
            break
        content = data.get("content") or []
        for rec in content:
            row = _row(rec, arr)
            if not row:
                continue
            k = (row["flight"], row["sched"])
            if k in seen:
                continue
            seen.add(k)
            out[out_key].append(row)
        pag = data.get("pagination") or {}
        if not pag.get("hasPrevious") or not content:
            break
        nxt = pag.get("earliestTimestamp")
        if not nxt or nxt == before:
            break
        before = nxt


def scrape(drv) -> dict:
    drv.warm(WARMUP, wait_ms=2500)
    now = datetime.now(timezone.utc)
    days = [(now - timedelta(days=1)).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")]
    out = {"departure": [], "arrival": []}
    for mv, key, arr in (("departures", "departure", False), ("arrivals", "arrival", True)):
        seen = set()
        for d in days:
            _fetch_day(drv, mv, d, arr, key, out, seen)
    return {"DUB": out}
