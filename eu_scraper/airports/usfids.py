"""
US-FIDS vendor — one Next.js "airport.mobi/aero" REST API shape powers FOUR US
airports (LIVE-verified 2026-07-02). Same request contract everywhere:

    GET https://api.<airport>.<tld>/flights?scheduledTimestamp=<epoch>..<epoch>
        header  api-key:     <per-airport public key, shipped in the site bundle>
        header  api-version: <per-airport int>

Cracked airports (host / key / version / tz):
    DFW  api.dfwairport.mobi   87856E0636AA4BF282150FCBE1AD63DE  180  America/Chicago
    CLT  api.cltairport.mobi   5ccb418715f9428ca6cb4df1635d4815  150  America/New_York
    MCO  api.goaa.aero         8eaac7209c824616a8fe58d22268cd59  150  America/New_York
    LAS  api.hriairport.com    c54a8aab24174fe3ae17166e38daf399  100  America/Los_Angeles

ONE call returns BOTH directions for the whole timestamp window. Per record:
iataOperatingAirline / operatingAirlineTrackNumber, departureAirport /
arrivalAirport / viaAirport (all IATA), terminal, gate, baggageBelt, status,
isDelayed, aircraftRegistration (TAIL!), scheduled/estimated/actual Timestamp
(epoch UTC), baseAirport. No aircraft type code in this feed.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from .. import scraper as S
from .. import airports_ref as REF

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

# key → (host, api-key, api-version, warm-referer, tz)
AIRPORTS = {
    "DFW": ("api.dfwairport.mobi", "87856E0636AA4BF282150FCBE1AD63DE", "180",
            "https://www.dfwairport.com/", "America/Chicago"),
    "CLT": ("api.cltairport.mobi", "5ccb418715f9428ca6cb4df1635d4815", "150",
            "https://www.cltairport.com/", "America/New_York"),
    "MCO": ("api.goaa.aero", "8eaac7209c824616a8fe58d22268cd59", "150",
            "https://flymco.com/", "America/New_York"),
    "LAS": ("api.hriairport.com", "c54a8aab24174fe3ae17166e38daf399", "100",
            "https://www.harryreidairport.com/", "America/Los_Angeles"),
}
DEFAULT = list(AIRPORTS.keys())


def _epoch_local(ts, tzname):
    if not ts:
        return None
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        if ZoneInfo is not None:
            try:
                dt = dt.astimezone(ZoneInfo(tzname))
            except Exception:
                pass
        return dt.strftime("%Y-%m-%dT%H:%M:00")
    except Exception:
        return None


def _row(rec: dict, tzname: str) -> dict | None:
    if rec.get("isDeleted") or rec.get("isVisible") is False:
        return None
    arr = bool(rec.get("arrival"))
    al = (rec.get("iataOperatingAirline") or rec.get("iataCodeShareAirline") or "").strip().upper()
    num = (rec.get("operatingAirlineTrackNumber") or rec.get("codeShareAirlineTrackNumber") or "").strip()
    if not (al and num):
        fn = (rec.get("operatingAirlineFlightNumber") or "").strip()
        if not fn:
            return None
        r_flight = S.norm_flight(fn, "")
        if not al:
            al = fn[:2]
    else:
        r_flight = S.norm_flight(al, num)
    r = S.empty_row()
    r["airline"] = al
    r["flight"] = r_flight
    # "other" airport: origin for arrivals, destination for departures. viaAirport
    # is the immediate other end and equals dep/arr for direct flights; fall back to
    # it when the primary field is missing.
    other = (rec.get("departureAirport") if arr else rec.get("arrivalAirport")) or ""
    other = other.strip().upper() or (rec.get("viaAirport") or "").strip().upper()
    r["dest_iata"] = REF.resolve("", code=other) or other
    sched_ts = rec.get("scheduledTimestamp")
    r["sched"] = _epoch_local(sched_ts, tzname)
    # Prefer actual, fall back to estimate. Guard the feed's occasional garbage
    # actualTimestamp (a handful of rows report an "actual" many hours before the
    # scheduled time): drop any estimate that sits >6h before schedule.
    est = rec.get("actualTimestamp") or rec.get("estimatedTimestamp") or rec.get("bestKnownTimestamp")
    try:
        if est and sched_ts and int(est) < int(sched_ts) - 6 * 3600:
            alt = rec.get("estimatedTimestamp")
            est = alt if (alt and int(alt) >= int(sched_ts) - 6 * 3600) else None
    except Exception:
        pass
    esti = _epoch_local(est, tzname)
    if esti and esti != r["sched"]:
        r["esti"] = esti
    r["gate"] = (rec.get("gate") or "").strip()
    r["terminal"] = (rec.get("terminal") or "").strip()
    r["reg"] = (rec.get("aircraftRegistration") or "").strip().upper()
    st = (rec.get("status") or "").strip()
    r["status"] = st
    low = st.lower()
    r["cancelled"] = "cancel" in low or (rec.get("originalStatus") or "").upper() in ("CNCL", "CX", "CAN")
    r["delayed"] = bool(rec.get("isDelayed")) or "delay" in low
    r["_tz"] = tzname
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r)


def _scrape_one(drv, code: str) -> dict:
    host, key, ver, warm, tzname = AIRPORTS[code]
    drv.warm(warm, wait_ms=1800)
    now = int(time.time())
    lo = now - 24 * 3600
    hi = now + 48 * 3600
    url = f"https://{host}/flights?scheduledTimestamp={lo}..{hi}"
    out = {"departure": [], "arrival": []}
    try:
        resp = drv._ctx.request.get(url, timeout=25000, headers={
            "api-key": key, "api-version": ver,
            "Accept": "application/json, text/plain, */*", "Referer": warm})
        if not resp.ok:
            return {code: out}
        data = resp.json()
    except Exception:
        return {code: out}
    flights = ((data or {}).get("data") or {}).get("flights") or []
    seen = set()
    for rec in flights:
        row = _row(rec, tzname)
        if not row:
            continue
        arr = bool(rec.get("arrival"))
        k = (arr, row["flight"], row["sched"], row["dest_iata"])
        if k in seen:
            continue
        seen.add(k)
        out["arrival" if arr else "departure"].append(row)
    return {code: out}


def scrape(drv, airports=None) -> dict:
    res = {}
    for code in (airports or DEFAULT):
        code = code.upper()
        if code not in AIRPORTS:
            continue
        try:
            res.update(_scrape_one(drv, code))
        except Exception:
            res.setdefault(code, {"departure": [], "arrival": []})
    return res
