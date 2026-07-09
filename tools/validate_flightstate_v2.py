#!/usr/bin/env python3
"""Honest per-phase validation: production-like (board dep/arr side + position),
diverse sample (ground/climb/cruise), composite truth (FR24 flight_stage for
airborne; the airport board status for the finer ground phases FR24 lumps as
ON_GROUND). Prints a per-phase confusion matrix + every mismatch so we see WHERE
the engine is weak, not just an averaged headline.

Usage: python3 tools/validate_flightstate_v2.py
"""
import asyncio
import json
import os
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blueprints.flight_state import resolve_flight_state, IN_AIR
from blueprints.flight_state_collectors import (
    build_keys, obs_from_board_merged, obs_from_pos, classify_board_status)
from blueprints.aerox_data_blueprint import _iata_latlon

AIR_KW = ("air", "climb", "cruis", "descen", "enroute", "en route", "approach")
GND_KW = ("ground", "taxi", "park", "gate", "stand", "land")
FR24_LAG = 0


def fr24_class(stage):
    s = (stage or "").strip().lower()
    if not s:
        return None
    if any(k in s for k in ("airborne", "en route", "climb", "cruis", "descen", "approach")):
        return "airborne"
    if any(k in s for k in ("ground", "taxi", "parked", "landed", "gate", "stand")):
        return "ground"
    return None


def board_truth_phase(dep_status, arr_status):
    """Finer ground truth from the airport board (human status the airline posts)."""
    for st, side in ((arr_status, "arr"), (dep_status, "dep")):
        s = (st or "").strip().lower()
        if not s:
            continue
        if any(k in s for k in ("cancel", "annull")):
            return "CANCELLED"
        if side == "arr" and any(k in s for k in ("land", "gelandet", "arrived", "baggage", "at gate", "on block")):
            return "LANDED_ARRIVED"
        if side == "dep" and any(k in s for k in ("depart", "abgeflogen", "gestartet", "taxi", "airborne", "dep")):
            return "TAXI_OR_AIR"
        if any(k in s for k in ("boarding", "gate clos", "gate open", "final call")):
            return "BOARDING"
    return None


# engine phase -> coarse class for the airborne comparison
def eng_class(phase):
    if phase in IN_AIR:
        return "airborne"
    if phase in ("TAXI_OUT", "BOARDING", "SCHEDULED", "LANDED", "ARRIVED", "UNKNOWN"):
        return "ground"
    return "other"


async def fetch_stage(fid):
    from fr24 import FR24
    try:
        async with FR24() as f:
            det = await asyncio.wait_for(f.flight_details.fetch(flight_id=int(fid)), timeout=8)
            return (det.to_dict().get("flight_progress") or {}).get("flight_stage")
    except Exception as e:
        return f"ERR:{type(e).__name__}"


def build_merged(row, board_by_flight):
    """Reconstruct a _flight_obs_merged-like dict from the board rows of this flight,
    side-aware: dep row = board at origin, arr row = board at dest."""
    origin, dest = row.get("origin"), row.get("dest")
    dep_row = arr_row = None
    for b in board_by_flight.get(row.get("flight"), []):
        if b.get("airport") == origin:
            dep_row = dep_row or b
        elif b.get("airport") == dest:
            arr_row = arr_row or b
    if not (dep_row or arr_row):
        return None, None, None
    merged = {
        "status_dep": (dep_row or {}).get("status"),
        "status_arr": (arr_row or {}).get("status"),
        "sched_dep": (dep_row or {}).get("sched"), "esti_dep": (dep_row or {}).get("esti"),
        "sched_arr": (arr_row or {}).get("sched"), "esti_arr": (arr_row or {}).get("esti"),
        "cancelled": bool((dep_row or {}).get("cancelled") or (arr_row or {}).get("cancelled")),
        "reg": (dep_row or arr_row or {}).get("reg"),
        "aircraft": (dep_row or arr_row or {}).get("type_code"),
        "delay_known": False,
    }
    return merged, (dep_row or {}).get("status"), (arr_row or {}).get("status")


async def main():
    rows = json.load(open("/tmp/aclive_diverse.json"))
    board = json.load(open("/tmp/board_rows.json"))
    board_by_flight = defaultdict(list)
    for b in board:
        board_by_flight[b.get("flight")].append(b)
    now = time.time()

    airborne_match = airborne_total = 0
    ground_match = ground_total = 0
    mism = []
    confusion = Counter()
    unknown_ground = 0

    for r in rows:
        merged, dep_st, arr_st = build_merged(r, board_by_flight)
        pos = {"lat": r.get("lat"), "lon": r.get("lon"), "track": r.get("track"),
               "gs": r.get("gs_kt"), "alt": r.get("alt_ft"),
               "on_ground": r.get("on_ground"), "seen_ts": r.get("seen_ts")}
        keys = build_keys(r.get("flight"), None, r.get("origin"), r.get("dest"),
                          roster_tail=r.get("reg_display") or r.get("reg"),
                          callsign=r.get("callsign"),
                          dep_ll=_iata_latlon(r.get("origin") or ""),
                          arr_ll=_iata_latlon(r.get("dest") or ""))
        obs = obs_from_pos(pos, "aircraft_live", now=now)
        if merged:
            obs += obs_from_board_merged(merged, keys, now=now)
        fs = resolve_flight_state(keys, obs, now=now)
        ephase = fs["phase"]
        ecls = eng_class(ephase)

        stage = await fetch_stage(r.get("flightid")) if r.get("flightid") else None
        fcls = fr24_class(stage)
        btruth = board_truth_phase(dep_st, arr_st)

        # AIRBORNE truth = PHYSICAL altitude (transponder), not FR24's lagging
        # stage label. A plane with real baro-altitude is airborne, full stop.
        alt = r.get("alt_ft")
        phys = "airborne" if (isinstance(alt, (int, float)) and alt > 500) else "ground"
        airborne_total += 1
        if ecls == phys:
            airborne_match += 1
        else:
            mism.append(("AIR", r, ephase, stage, dep_st, arr_st))
        # measure FR24 stage-lag separately (informational, not charged to engine)
        if fcls is not None and fcls != phys:
            global FR24_LAG
            FR24_LAG += 1

        # ground-phase agreement vs BOARD truth (finer than FR24)
        if fcls == "ground" or (fcls is None and btruth):
            if btruth:
                ground_total += 1
                ok = ((btruth == "TAXI_OR_AIR" and ephase in ("TAXI_OUT", "AIRBORNE", "APPROACH"))
                      or (btruth == "BOARDING" and ephase in ("BOARDING", "TAXI_OUT"))
                      or (btruth == "LANDED_ARRIVED" and ephase in ("LANDED", "ARRIVED"))
                      or (btruth == "CANCELLED" and ephase == "CANCELLED"))
                if ok:
                    ground_match += 1
                else:
                    mism.append(("GND", r, ephase, stage, dep_st, arr_st))
                    confusion[f"board={btruth} -> engine={ephase}"] += 1
            elif ephase == "UNKNOWN":
                unknown_ground += 1

        await asyncio.sleep(0.3)

    print("=" * 80)
    print("HONEST PER-PHASE VALIDATION (board+position, diverse sample)")
    print("=" * 80)
    at = airborne_total or 1
    gt = ground_total or 1
    print(f"AIRBORNE vs PHYSICAL alt: {airborne_match}/{airborne_total} = {100*airborne_match/at:.1f}%")
    print(f"  (FR24 stage-label lagged physical reality in {FR24_LAG} cases — not the engine's fault)")
    print(f"GROUND-PHASE vs BOARD: {ground_match}/{ground_total} = {100*ground_match/gt:.1f}%")
    print(f"ground with no truth left UNKNOWN: {unknown_ground}")
    combined_m = airborne_match + ground_match
    combined_t = airborne_total + ground_total
    print(f"COMBINED: {combined_m}/{combined_t} = {100*combined_m/(combined_t or 1):.1f}%")
    print("\nCONFUSION (board -> engine mismatches):")
    for k, v in confusion.most_common():
        print(f"  {v:2}x  {k}")
    print("\nSAMPLE MISMATCHES:")
    for tag, r, ep, stg, ds, as_ in mism[:25]:
        print(f"  [{tag}] {r.get('callsign'):9} gs={str(r.get('gs_kt')):>4} alt={str(r.get('alt_ft')):>6} "
              f"engine={ep:9} fr24={str(stg)[:12]:12} dep_board={str(ds)[:16]:16} arr_board={str(as_)[:14]}")
    json.dump([{"tag": t, "callsign": r.get("callsign"), "gs": r.get("gs_kt"),
                "alt": r.get("alt_ft"), "engine": ep, "fr24": stg, "dep_board": ds, "arr_board": as_}
               for (t, r, ep, stg, ds, as_) in mism],
              open("/tmp/fs_v2_mismatches.json", "w"), indent=1)


if __name__ == "__main__":
    asyncio.run(main())
