"""
BLR — Kempegowda International Airport, Bengaluru (BIAL). The flight board reads a
clean REST FIDS on the airport's API gateway, with the DATE baked into the path
(so two calls = yesterday + today) and direction as the final segment:

    GET https://gateway.bengaluruairport.com/fis/v2/api/aodb/flight-infos/{YYYYMMDD}/{Departure|Arrival}
    (LIVE-verified 2026-07-03.  Cloudflare-fronted: a raw context.request returns
     nothing, but a `fetch()` inside the warmed page passes.)

RICH feed incl. TAIL REG. Per record: flightNumber ("QP1811"), airline.code/name,
scheduledDate/estimatedDate ("YYYYMMDDHHMM", LOCAL Asia/Kolkata), flightStatus.name
/ displayName, originAirport.code (the OTHER on arrivals) / destinationAirport.code
(the OTHER on departures), terminal, gates[].gateNumber (dep) / baggageBelts[] (arr),
aircraftRegistrationNumber (tail!). No aircraft type code.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from .. import scraper as S

API = "https://gateway.bengaluruairport.com/fis/v2/api/aodb/flight-infos"
WARMUP = "https://www.bengaluruairport.com/travellers/flights/flight-information"
TZ = "Asia/Kolkata"
_CANCEL = ("cancel", "CANCELLED")


def _local_iso(v) -> str | None:
    """'YYYYMMDDHHMM' (already LOCAL) → naive 'YYYY-MM-DDTHH:MM:00'."""
    s = (str(v or "")).strip()
    if len(s) < 12 or not s[:12].isdigit():
        return None
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}T{s[8:10]}:{s[10:12]}:00"


def _row(rec: dict, arr: bool) -> dict | None:
    fn = (rec.get("flightNumber") or "").strip().upper()
    if not fn:
        return None
    al = (rec.get("airline") or {}) or {}
    other = (rec.get("originAirport") if arr else rec.get("destinationAirport")) or {}
    r = S.empty_row()
    r["airline"] = (al.get("code") or fn[:2]).strip().upper()
    r["airline_name"] = (al.get("name") or al.get("displayName") or "").strip()
    r["flight"] = S.norm_flight(fn, "")
    r["dest_iata"] = (other.get("code") or "").strip().upper()
    r["dest_name"] = (other.get("city") or other.get("name") or "").strip()
    r["sched"] = _local_iso(rec.get("scheduledDate"))
    esti = _local_iso(rec.get("estimatedDate"))
    if esti and esti != r["sched"]:
        r["esti"] = esti
    r["terminal"] = (rec.get("terminal") or "").strip()
    if arr:
        belts = rec.get("baggageBelts") or []
        r["gate"] = str(belts[0].get("beltNumber") or belts[0].get("number") or "") if belts else ""
    else:
        gates = rec.get("gates") or []
        r["gate"] = str(gates[0].get("gateNumber") or "") if gates else ""
    r["reg"] = (rec.get("aircraftRegistrationNumber") or "").strip().upper()
    fs = (rec.get("flightStatus") or {}) or {}
    st = (fs.get("displayName") or fs.get("name") or "").strip()
    r["status"] = st
    nm = (fs.get("name") or "").upper()
    r["cancelled"] = any(c.upper() in nm for c in _CANCEL)
    r["delayed"] = "DELAY" in nm
    r["_tz"] = TZ
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r)


def scrape(drv) -> dict:
    drv.warm(WARMUP, wait_ms=5000)
    now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)  # IST for date roll
    dates = [(now - timedelta(days=1)).strftime("%Y%m%d"), now.strftime("%Y%m%d")]
    out = {"departure": [], "arrival": []}
    for seg, key, arr in (("Departure", "departure", False), ("Arrival", "arrival", True)):
        seen = set()
        for d in dates:
            data = drv.get_json_inpage(f"{API}/{d}/{seg}")
            rows = (data or {}).get("data") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                continue
            for rec in rows:
                row = _row(rec, arr)
                if not row:
                    continue
                k = (row["flight"], row["sched"])
                if k in seen:
                    continue
                seen.add(k)
                out[key].append(row)
    return {"BLR": out}
