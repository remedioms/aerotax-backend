"""
DOH — Hamad International Airport, Doha. The flight-status page (a jQuery app) calls
a clean in-house FIDS webservice with an explicit date-range window per direction:

    GET https://dohahamadairport.com/webservices/fids
        ?type={departures|arrivals}
        &startTime=DD-MM-YYYY 00:00:00 &endTime=DD-MM-YYYY 23:59:59
    (LIVE-verified 2026-07-03.  `type` picks direction; the date range lets us pull
     yesterday + today. All times are UNIX EPOCH SECONDS, UTC.)

The endpoint sits behind Cloudflare: a raw context.request 403s, but a `fetch()` run
INSIDE the warmed page (get_json_inpage) passes cleanly.

Per record: flightNumber ("QR436", IATA-prefixed), airlineCode (ICAO "QTR"),
aircraft ("Airbus A320", type NAME), originCode/destinationCode (IATA), viaCode,
scheduledTime / estimateTime / latestTime / actualTimeOfDep|actualTimeOfArr
(epoch UTC), Stand, gateNoGeneral (dep) / BaggageBelt (arr), checkInCounterDisplay,
lang.en.airlineName + lang.en.flightStatus.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from .. import scraper as S
from .. import airports_ref as REF

BASE = "https://dohahamadairport.com/webservices/fids"
WARMUP = "https://dohahamadairport.com/airlines/flight-status"
TZ = "Asia/Qatar"


def _epoch_iso(v) -> str | None:
    """epoch-seconds (UTC) → naive Asia/Qatar iso. None on junk/zero."""
    if v in (None, "", "0", 0):
        return None
    try:
        ts = int(str(v).strip())
    except Exception:
        return None
    if ts <= 0:
        return None
    iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    return S.utc_to_local_iso(iso, TZ)


def _row(rec: dict, arr: bool) -> dict | None:
    fn = (rec.get("flightNumber") or "").strip().upper()
    if not fn:
        return None
    en = (rec.get("lang") or {}).get("en") or {}
    r = S.empty_row()
    r["airline"] = fn[:2]
    r["airline_name"] = (en.get("airlineName") or "").strip()
    r["flight"] = S.norm_flight(fn, "")
    other = (rec.get("originCode") if arr else rec.get("destinationCode")) or ""
    other = other.strip().upper()
    name = (en.get("originName") if arr else en.get("destinationName")) or ""
    r["dest_iata"] = other or (REF.resolve(name) or "")
    r["dest_name"] = (en.get("originCity") if arr else en.get("destinationCity")) or ""
    r["dest_name"] = (r["dest_name"] or "").strip()
    r["sched"] = _epoch_iso(rec.get("scheduledTime"))
    est = _epoch_iso(rec.get("actualTimeOfArr") if arr else rec.get("actualTimeOfDep")) \
        or _epoch_iso(rec.get("estimateTime")) or _epoch_iso(rec.get("latestTime"))
    if est and est != r["sched"]:
        r["esti"] = est
    r["aircraft"] = (rec.get("aircraft") or "").strip()
    stand = (rec.get("Stand") or "").strip()
    if arr:
        belt = (rec.get("BaggageBelt") or "").strip()
        r["gate"] = belt
    else:
        g = (rec.get("gateNoGeneral") or "").strip()
        r["gate"] = "" if g.upper() in ("NA", "") else g
    r["terminal"] = "" if stand.upper() in ("HIA", "") else stand
    st = (en.get("flightStatus") or "").strip()
    r["status"] = st
    low = st.lower()
    r["cancelled"] = "cancel" in low
    r["delayed"] = "delay" in low
    r["_tz"] = TZ
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r)


def scrape(drv) -> dict:
    drv.warm(WARMUP, wait_ms=4500)
    now = datetime.now(timezone.utc)
    days = [(now - timedelta(days=1)), now]
    out = {"departure": [], "arrival": []}
    for typ, key, arr in (("departures", "departure", False), ("arrivals", "arrival", True)):
        seen = set()
        for d in days:
            ds = d.strftime("%d-%m-%Y")
            url = (f"{BASE}?type={typ}&startTime={ds}%2000:00:00"
                   f"&endTime={ds}%2023:59:59&timestamp=1&_=1")
            data = drv.get_json_inpage(url)
            flights = (data or {}).get("flights") if isinstance(data, dict) else None
            if not isinstance(flights, list):
                continue
            for rec in flights:
                row = _row(rec, arr)
                if not row:
                    continue
                k = (row["flight"], row["sched"])
                if k in seen:
                    continue
                seen.add(k)
                out[key].append(row)
    return {"DOH": out}
