"""
SFO — San Francisco Int'l (flySFO). One open JSON endpoint the site's React board
calls directly, ~3000 flights spanning several days (LIVE-verified 2026-07-02):

    GET https://www.flysfo.com/flysfo/api/flight-status  → {last_update, data:[...]}

Per record: flight_kind ("Arrival"/"Departure"), airline{iata_code, airline_name},
flight_number, callsign, gate{gate_number}, terminal{terminal_code},
scheduled_aod_time / estimated_aod_time / actual_aod_time (ISO WITH -07:00 offset =
already local wall-clock), airport{iata_code, airport_city, airport_name} (the OTHER
airport), remark (status). No tail reg / aircraft type code in this feed.
"""
from __future__ import annotations

from .. import scraper as S
from .. import airports_ref as REF

URL = "https://www.flysfo.com/flysfo/api/flight-status"
WARMUP = "https://www.flysfo.com/flight-info/flight-status"
TZ = "America/Los_Angeles"
_CANCEL = ("cancel",)


def _row(rec: dict) -> dict | None:
    arr = (rec.get("flight_kind") or "").lower().startswith("arriv")
    al_obj = rec.get("airline") or {}
    al = (al_obj.get("iata_code") or "").strip().upper()
    num = str(rec.get("flight_number") or "").strip()
    if not num:
        return None
    r = S.empty_row()
    r["airline"] = al or (rec.get("callsign") or "")[:2].upper()
    r["airline_name"] = (al_obj.get("airline_name") or al_obj.get("airline_display_name") or "").strip()
    r["flight"] = S.norm_flight(al or r["airline"], num)
    ap = rec.get("airport") or {}
    code = (ap.get("iata_code") or "").strip().upper()
    r["dest_name"] = (ap.get("airport_city") or ap.get("airport_name") or "").strip()
    r["dest_iata"] = REF.resolve(r["dest_name"], code=code) or code
    # The wall-clock in these ISO strings already carries the local -07:00 offset;
    # utc_to_local_iso re-normalizes to America/Los_Angeles (a no-op) → naive local.
    r["sched"] = S.utc_to_local_iso(rec.get("scheduled_aod_time"), TZ)
    est = rec.get("actual_aod_time") or rec.get("estimated_aod_time")
    esti = S.utc_to_local_iso(est, TZ)
    if esti and esti != r["sched"]:
        r["esti"] = esti
    g = rec.get("gate") or {}
    r["gate"] = (g.get("gate_number") or "").strip()
    t = rec.get("terminal") or {}
    r["terminal"] = (t.get("terminal_code") or "").strip()
    st = (rec.get("remark") or "").strip()
    r["status"] = st
    low = st.lower()
    r["cancelled"] = any(h in low for h in _CANCEL)
    r["delayed"] = "delay" in low
    r["_tz"] = TZ
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r)


def scrape(drv) -> dict:
    drv.warm(WARMUP, wait_ms=1800)
    data = drv.get_json(URL, referer=WARMUP)
    out = {"departure": [], "arrival": []}
    rows = (data or {}).get("data") or []
    seen = set()
    for rec in rows:
        row = _row(rec)
        if not row:
            continue
        arr = (rec.get("flight_kind") or "").lower().startswith("arriv")
        k = (arr, row["flight"], row["sched"])
        if k in seen:
            continue
        seen.add(k)
        out["arrival" if arr else "departure"].append(row)
    return {"SFO": out}
