"""Tests for the collector adapter layer (blueprints/flight_state_collectors.py).

Proves that the REAL data shapes our endpoints already fetch (`_flight_obs_merged`
record, `_aircraft_live_pos` tuple) map into Observations that the pure engine
then resolves correctly — i.e. the collector+engine pipeline fixes the ghost bug
end-to-end, not just the hand-built fixtures in test_flight_state.py.
"""
from blueprints.flight_state import (
    resolve_flight_state, TAXI_OUT, AIRBORNE, LANDED, BOARDING, CANCELLED, SIMULATED,
)
from blueprints.flight_state_collectors import (
    classify_board_status, obs_from_board_merged, obs_from_aircraft_live,
    obs_from_adsb, build_keys,
)

NOW = 1_783_584_000
FRA = (50.03, 8.57)
GVA = (46.24, 6.11)


def test_classify_departed_is_offblock_not_airborne():
    # THE ghost-bug encoding: dep-side "Abgeflogen" = off-block = TAXI_OUT
    assert classify_board_status("Abgeflogen", "dep") == (TAXI_OUT, True, False)
    assert classify_board_status("Departed", "dep") == (TAXI_OUT, True, False)
    # only an explicit en-route status counts as proven airborne
    ph, hard, proven = classify_board_status("En route", "dep")
    assert (ph, hard, proven) == (AIRBORNE, True, True)


def test_classify_sides_and_specials():
    assert classify_board_status("Gelandet", "arr") == (LANDED, True, False)
    assert classify_board_status("Landed", "dep")[0] is None       # nonsensical on dep board
    assert classify_board_status("Boarding", "dep") == (BOARDING, False, False)
    assert classify_board_status("Annulliert", "dep") == (CANCELLED, True, False)
    assert classify_board_status("Estimated 12:40", "dep")[0] is None
    assert classify_board_status("", "dep")[0] is None


def test_daibv_taxi_ghost_fixed_end_to_end():
    """The REAL D-AINV case, built from the actual fetched shapes:
      board merged: status_dep='Abgeflogen', sched_arr present
      aircraft_live tuple: pos gs=15 alt=None near FRA (taxiing)
    -> engine must resolve TAXI_OUT, live=None, no gs-extrapolated ETA."""
    keys = build_keys("LH2557", "2026-07-09", "FRA", "GVA", roster_tail="D-AINV",
                      sched_dep_iso="2026-07-09T12:50:00Z", sched_arr_iso="2026-07-09T13:55:00Z",
                      dep_ll=FRA, arr_ll=GVA, sched_dep_ts=NOW - 300)
    merged = {
        "status_dep": "Abgeflogen", "status_arr": None, "cancelled": False,
        "sched_dep": None, "esti_dep": None,
        "sched_arr": "2026-07-09T13:55:00Z", "esti_arr": None,
        "delay_known": False, "reg": "D-AINV", "aircraft": "A320",
    }
    pos = {"lat": 49.48, "lon": 8.52, "track": 200, "gs": 15, "alt": None,
           "on_ground": False, "source": "aircraft_live", "seen_ts": NOW - 30}
    obs = (obs_from_board_merged(merged, keys, now=NOW)
           + obs_from_aircraft_live(pos, ("FRA", "GVA"), "D-AINV", "A320", now=NOW))
    fs = resolve_flight_state(keys, obs, now=NOW)
    assert fs["phase"] == TAXI_OUT
    assert fs["live"] is None
    assert fs["in_flight"] is False
    assert fs["times"]["eta_conf"] != SIMULATED


def test_aircraft_live_cruise_maps_to_airborne():
    keys = build_keys("LH716", "2026-07-09", "FRA", "HND", roster_tail="D-AIXA",
                      dep_ll=FRA, arr_ll=(35.55, 139.78), sched_dep_ts=NOW - 5 * 3600)
    pos = {"lat": 61.7, "lon": 90.2, "track": 61, "gs": 476, "alt": 37000,
           "on_ground": False, "source": "aircraft_live", "seen_ts": NOW - 90}
    obs = obs_from_aircraft_live(pos, ("FRA", "HND"), "D-AIXA", "A359", now=NOW)
    fs = resolve_flight_state(keys, obs, now=NOW)
    assert fs["phase"] == AIRBORNE
    assert fs["live"] is not None
    assert fs["live"]["source"] == "aircraft_live"


def test_board_delay_only_when_known():
    keys = build_keys("LHX", "2026-07-09", "FRA", "GVA", dep_ll=FRA, arr_ll=GVA)
    obs_known = obs_from_board_merged(
        {"status_arr": "Gelandet", "delay_known": True, "arr_delay_min": 12,
         "dep_delay_min": 8}, keys, now=NOW)
    fs = resolve_flight_state(keys, obs_known, now=NOW)
    assert fs["delay"]["known"] is True and fs["delay"]["min"] == 12

    obs_unknown = obs_from_board_merged(
        {"status_dep": "Abgeflogen", "delay_known": False}, keys, now=NOW)
    fs2 = resolve_flight_state(keys, obs_unknown, now=NOW)
    assert fs2["delay"]["known"] is False and fs2["on_time"] is None


def test_adsb_fix_wins_position():
    keys = build_keys("LH1", "2026-07-09", "FRA", "GVA", dep_ll=FRA, arr_ll=GVA,
                      sched_dep_ts=NOW - 1800)
    obs = obs_from_adsb({"lat": 47.9, "lon": 7.4, "track": 205, "gs": 431,
                         "alt": 34000, "position_source": 0}, now=NOW)
    obs.append(__import__("blueprints.flight_state", fromlist=["Observation"]).Observation(
        "phase_hard", AIRBORNE, "board", NOW - 90, meta={"side": "dep", "proven_airborne": True}))
    fs = resolve_flight_state(keys, obs, now=NOW)
    assert fs["phase"] == AIRBORNE
    assert fs["live"]["source"] == "adsb"
