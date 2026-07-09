#!/usr/bin/env python3
"""Validate the FlightState engine against FR24 ground truth on REAL flights.

For each real aircraft_live snapshot: run the engine -> (phase, shows-live?), and
independently fetch FR24 flight_details.flight_progress.flight_stage (FR24's own
label). Compare whether the engine renders a live/airborne position exactly when
FR24 says the aircraft is airborne. This is the crux the ghost bug got wrong.

Usage:  python3 tools/validate_flightstate_vs_fr24.py /tmp/aclive_all.json [max]
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blueprints.flight_state import resolve_flight_state
from blueprints.flight_state_collectors import build_keys, obs_from_aircraft_live
from blueprints.aerox_data_blueprint import _iata_latlon

AIR_KW = ("air", "climb", "cruis", "descen", "enroute", "en route", "approach", "flight")
GND_KW = ("ground", "taxi", "park", "gate", "land", "arrived", "stand")


def fr24_stage_class(stage: str):
    s = (stage or "").strip().lower()
    if not s:
        return None
    if any(k in s for k in GND_KW) and not any(k in s for k in ("airborne", "en route")):
        return "ground"
    if any(k in s for k in AIR_KW):
        return "airborne"
    return None


async def fetch_stage(fid):
    from fr24 import FR24
    try:
        async with FR24() as f:
            det = await asyncio.wait_for(f.flight_details.fetch(flight_id=int(fid)), timeout=8)
            d = det.to_dict()
            fp = d.get("flight_progress") or {}
            return fp.get("flight_stage")
    except Exception as e:
        return f"ERR:{type(e).__name__}"


def run_engine(row, now):
    pos = {"lat": row.get("lat"), "lon": row.get("lon"), "track": row.get("track"),
           "gs": row.get("gs_kt"), "alt": row.get("alt_ft"),
           "on_ground": row.get("on_ground"), "source": "aircraft_live",
           "seen_ts": row.get("seen_ts")}
    keys = build_keys(row.get("flight"), None, row.get("origin"), row.get("dest"),
                      roster_tail=row.get("reg_display") or row.get("reg"),
                      callsign=row.get("callsign"),
                      dep_ll=_iata_latlon(row.get("origin") or ""),
                      arr_ll=_iata_latlon(row.get("dest") or ""))
    obs = obs_from_aircraft_live(pos, (row.get("origin"), row.get("dest")),
                                 row.get("reg_display") or row.get("reg"),
                                 row.get("ac_type"), now=now)
    fs = resolve_flight_state(keys, obs, now=now)
    return fs


async def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/aclive_all.json"
    cap = int(sys.argv[2]) if len(sys.argv) > 2 else 999
    rows = json.load(open(path))[:cap]
    now = time.time()

    match = mismatch = no_truth = 0
    rows_out = []
    print(f"{'reg':9} {'callsign':9} {'gs':>4} {'alt':>6} {'eng_phase':10} "
          f"{'eng_live':8} {'FR24_stage':16} {'FR24':8} {'verdict'}")
    print("-" * 92)
    for r in rows:
        fs = run_engine(r, now)
        eng_live = fs["live"] is not None
        fid = r.get("flightid")
        stage = await fetch_stage(fid) if fid else None
        cls = fr24_stage_class(stage)
        if cls is None:
            verdict = "no-truth"
            no_truth += 1
        else:
            fr24_air = (cls == "airborne")
            ok = (eng_live == fr24_air)
            verdict = "OK" if ok else "MISMATCH"
            if ok:
                match += 1
            else:
                mismatch += 1
        rows_out.append({"reg": r.get("reg_display") or r.get("reg"),
                         "callsign": r.get("callsign"), "gs": r.get("gs_kt"),
                         "alt": r.get("alt_ft"), "eng_phase": fs["phase"],
                         "eng_live": eng_live, "fr24_stage": stage, "verdict": verdict})
        print(f"{str(r.get('reg_display') or r.get('reg') or ''):9} "
              f"{str(r.get('callsign') or ''):9} {str(r.get('gs_kt') or ''):>4} "
              f"{str(r.get('alt_ft') or ''):>6} {fs['phase']:10} "
              f"{str(eng_live):8} {str(stage or '')[:16]:16} {str(cls or '')[:8]:8} {verdict}")
        await asyncio.sleep(0.35)   # throttle FR24

    total_judged = match + mismatch
    print("-" * 92)
    print(f"JUDGED: {total_judged}  MATCH: {match}  MISMATCH: {mismatch}  "
          f"NO-TRUTH: {no_truth}  "
          f"({100*match/total_judged:.1f}% match)" if total_judged else "no truth available")
    json.dump(rows_out, open("/tmp/fs_validation_result.json", "w"), indent=1)
    if mismatch:
        print("\nMISMATCHES:")
        for x in rows_out:
            if x["verdict"] == "MISMATCH":
                print(f"  {x['reg']} {x['callsign']} gs={x['gs']} alt={x['alt']} "
                      f"engine_live={x['eng_live']} phase={x['eng_phase']} fr24={x['fr24_stage']}")


if __name__ == "__main__":
    asyncio.run(main())
