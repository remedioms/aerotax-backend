"""Unit + property tests for the unified FlightState engine (blueprints/flight_state.py).

The engine is a PURE reducer: (keys, observations) -> one canonical FlightState.
These tests are the authority for the rollout — they encode the load-bearing
invariants the whole 5k-user rewrite depends on:

  INV-1  AIRBORNE must be EARNED: raw on_ground bit is ignored; airborne only if
         (alt>1000 OR gs>=80) with a near-origin guard on the gs-only branch.
  INV-2  Position renders ONLY when phase in {AIRBORNE,APPROACH,DIVERTED}; a fix
         failing the gate -> live=None AND no gs-extrapolated ETA.
  INV-3  delay_known=False -> delay.min=None, on_time=None. unknown != on-time.
  INV-4  A stale board 'landed' cannot un-fly a plane; only a FRESH hard signal
         may regress a terminal phase back to AIRBORNE (monotonicity).
  INV-5  An empty observation list still yields a valid UNKNOWN (never crashes).
"""
from blueprints import flight_state as E
from blueprints.flight_state import (
    Observation, resolve_flight_state, is_airborne_kinematic,
    project_my_flight_status, project_flight_live, project_crew_status,
    project_friend_leg,
    SCHEDULED, BOARDING, TAXI_OUT, AIRBORNE, APPROACH, LANDED, ARRIVED,
    CANCELLED, DIVERTED, UNKNOWN, OBSERVED, ESTIMATED, SIMULATED,
)

NOW = 1_783_584_000  # fixed clock, 2026-07-09T04:00:00Z
FRA = (50.03, 8.57)
GVA = (46.24, 6.11)
HND = (35.55, 139.78)
LHR = (51.47, -0.45)
ZRH = (47.46, 8.55)
SIBERIA = (61.7, 90.2)
MANNHEIM = (49.48, 8.52)


# ─────────────────────────── SCENARIOS ────────────────────────────────────

def _s1_taxi():
    return resolve_flight_state(
        keys={"flight": "LH2557", "date": "2026-07-09", "dep_iata": "FRA",
              "arr_iata": "GVA", "dep_ll": FRA, "arr_ll": GVA, "roster_tail": "D-AINV",
              "sched_dep": "2026-07-09T12:50:00Z", "sched_arr": "2026-07-09T13:55:00Z",
              "sched_dep_ts": NOW - 300},
        observations=[
            Observation("route", {"dep": "FRA", "dst": "GVA", "confidence": "confirmed"},
                        "fr24_bulk", NOW - 60, 0.9),
            Observation("reg", {"reg": "D-AINV", "ac_type": "A321"}, "aircraft_live", NOW - 30, 0.8),
            Observation("phase_hard", TAXI_OUT, "board", NOW - 120, 0.9, meta={"side": "dep"}),
            Observation("position", {"lat": MANNHEIM[0], "lon": MANNHEIM[1], "track": 200,
                                     "gs_kt": 15, "alt_ft": None, "on_ground_raw": False,
                                     "position_source": 3}, "aircraft_live", NOW - 30, 0.8),
        ], now=NOW)


def test_scenario1_taxi_no_ghost():
    """The D-AINV ghost bug: a 15kt taxiing plane must NOT render as cruise and
    must NOT get a gs-extrapolated 13:05 ETA."""
    fs = _s1_taxi()
    assert fs["phase"] == TAXI_OUT
    assert fs["live"] is None                      # INV-2
    assert fs["in_flight"] is False
    assert fs["times"]["eta_conf"] != SIMULATED    # INV-2: no gs-extrapolation
    # ETA falls back to the scheduled arrival (estimated), not an invented time
    assert fs["times"]["eta_iso"] == "2026-07-09T13:55:00Z"
    assert fs["times"]["eta_conf"] == ESTIMATED


def test_scenario2_over_siberia_snapshot_fills():
    """ADS-B blind over Russia -> aircraft_live southern-route snapshot fills."""
    fs = resolve_flight_state(
        keys={"flight": "LH716", "date": "2026-07-09", "dep_iata": "FRA",
              "arr_iata": "HND", "dep_ll": FRA, "arr_ll": HND, "roster_tail": "D-AIXA",
              "sched_dep": "2026-07-09T13:15:00Z", "sched_arr": "2026-07-10T09:20:00Z",
              "sched_dep_ts": NOW - 5 * 3600},
        observations=[
            Observation("route", {"dep": "FRA", "dst": "HND", "confidence": "confirmed"},
                        "fr24_bulk", NOW - 200, 0.9),
            Observation("reg", {"reg": "D-AIXA", "ac_type": "A359", "hex": "3C6DXX"},
                        "aircraft_live", NOW - 200, 0.8),
            Observation("event", {"event": "takeoff", "airport": "EDDF"},
                        "warehouse_event", NOW - 5 * 3600, 0.95),
            Observation("position", None, "adsb", NOW, status="absent"),
            Observation("position", {"lat": SIBERIA[0], "lon": SIBERIA[1], "track": 61,
                                     "gs_kt": 476, "alt_ft": 37000, "position_source": 3},
                        "aircraft_live", NOW - 90, 0.8),
        ], now=NOW)
    assert fs["phase"] == AIRBORNE
    assert fs["live"] is not None
    assert fs["live"]["source"] == "aircraft_live"


def test_scenario3_codeshare_and_swap():
    """Roster LH2557 (marketing) operated as LX1071/HB-JCA (swap) — engine keys
    fold to the operating flight and the tail is corrected."""
    fs = resolve_flight_state(
        keys={"flight": "LH2557", "date": "2026-07-09", "dep_iata": "ZRH",
              "arr_iata": "FRA", "dep_ll": ZRH, "arr_ll": FRA, "roster_tail": "HB-OLD",
              "sched_dep": "2026-07-09T10:00:00Z", "sched_arr": "2026-07-09T11:05:00Z",
              "sched_dep_ts": NOW - 1200},
        observations=[
            Observation("reg", {"reg": "HB-JCA", "ac_type": "BCS3"}, "board", NOW - 60, 0.98,
                        meta={"flightno_matched": True,
                              "codeshare": {"oper_flight": "LX1071", "oper_callsign": "SWR1071",
                                            "oper_carrier": "LX"}}),
            Observation("route", {"dep": "ZRH", "dst": "FRA", "confidence": "confirmed"},
                        "board", NOW - 60, 0.9),
            Observation("phase_hard", AIRBORNE, "board", NOW - 120, 0.9,
                        meta={"side": "dep", "proven_airborne": True}),
            Observation("position", {"lat": 48.5, "lon": 8.4, "track": 20, "gs_kt": 420,
                                     "alt_ft": 33000, "position_source": 0}, "adsb", NOW - 40, 0.95),
            Observation("delay", {"delay_known": True, "arr_delay_min": 8, "dep_delay_min": 6},
                        "board", NOW - 60, 0.9),
        ], now=NOW)
    assert fs["phase"] == AIRBORNE
    assert fs["live"] is not None
    assert fs["reg"] == "HB-JCA"
    assert fs["reg_swap"] is True
    assert fs["keys"]["flight"] == "LX1071"


def _s4_cruise():
    return resolve_flight_state(
        keys={"flight": "LH1000", "date": "2026-07-09", "dep_iata": "FRA",
              "arr_iata": "GVA", "dep_ll": FRA, "arr_ll": GVA, "roster_tail": "D-AIRB",
              "sched_dep": "2026-07-09T12:00:00Z", "sched_arr": "2026-07-09T13:00:00Z",
              "sched_dep_ts": NOW - 1800},
        observations=[
            Observation("route", {"dep": "FRA", "dst": "GVA", "confidence": "confirmed"},
                        "fr24_bulk", NOW - 30, 0.9),
            Observation("reg", {"reg": "D-AIRB", "ac_type": "A320", "hex": "3C66AA"},
                        "warehouse_flight", NOW - 30, 0.98),
            Observation("phase_hard", AIRBORNE, "board", NOW - 90, 0.9,
                        meta={"side": "dep", "proven_airborne": True}),
            Observation("position", {"lat": 47.9, "lon": 7.4, "track": 205, "gs_kt": 431,
                                     "alt_ft": 34000, "position_source": 0}, "adsb", NOW - 17, 0.95),
            Observation("delay", {"delay_known": True, "arr_delay_min": 3, "dep_delay_min": 5},
                        "board", NOW - 60, 0.9),
        ], now=NOW)


def test_scenario4_clean_cruise_ontime():
    fs = _s4_cruise()
    assert fs["phase"] == AIRBORNE
    assert fs["live"]["source"] == "adsb"
    assert fs["live"]["conf"] == OBSERVED
    assert fs["on_time"] is True
    assert fs["delay"]["known"] is True and fs["delay"]["min"] == 3


def test_scenario5_cancelled():
    fs = resolve_flight_state(
        keys={"flight": "LH444", "date": "2026-07-09", "dep_iata": "MUC", "arr_iata": "JFK",
              "dep_ll": (48.35, 11.78), "arr_ll": (40.64, -73.78), "roster_tail": "D-AIMA",
              "sched_dep": "2026-07-09T12:00:00Z", "sched_arr": "2026-07-09T21:00:00Z",
              "sched_dep_ts": NOW - 600},
        observations=[
            Observation("phase_hard", CANCELLED, "board", NOW - 120, 0.95,
                        meta={"side": "dep", "cancelled": True}),
            Observation("route", {"dep": "MUC", "dst": "JFK", "confidence": "confirmed"},
                        "board", NOW - 120, 0.9),
        ], now=NOW)
    assert fs["phase"] == CANCELLED
    assert fs["cancelled"] is True
    assert fs["live"] is None


def test_scenario6_monotonicity_fresh_hard_reflies():
    """Prior terminal LANDED (stale) + a FRESH takeoff event -> AIRBORNE (real
    return-to-service), not stuck landed."""
    prior = {"phase": LANDED, "conf": OBSERVED, "obs_ts": NOW - 4000, "sticky_airborne": False}
    fs = resolve_flight_state(
        keys={"flight": "LH880", "date": "2026-07-09", "dep_iata": "FRA", "arr_iata": "LHR",
              "dep_ll": FRA, "arr_ll": LHR, "roster_tail": "D-AITA",
              "sched_dep": "2026-07-09T12:00:00Z", "sched_arr": "2026-07-09T12:55:00Z",
              "sched_dep_ts": NOW - 1800},
        observations=[
            Observation("event", {"event": "takeoff", "airport": "EDDF"},
                        "warehouse_event", NOW - 200, 0.95),
            Observation("position", {"lat": 50.5, "lon": 5.0, "track": 280, "gs_kt": 440,
                                     "alt_ft": 35000, "position_source": 0}, "adsb", NOW - 20, 0.95),
            Observation("route", {"dep": "FRA", "dst": "LHR", "confidence": "confirmed"},
                        "fr24_bulk", NOW - 30, 0.9),
        ], now=NOW, prior=prior)
    assert fs["phase"] == AIRBORNE
    assert fs["live"] is not None


# ─────────────────────────── PROPERTY / INVARIANTS ─────────────────────────

def test_inv1_raw_on_ground_bit_ignored():
    """A fix with on_ground_raw=False but taxi kinematics is NOT airborne."""
    assert is_airborne_kinematic({"alt_ft": None, "gs_kt": 15}) is False
    assert is_airborne_kinematic({"alt_ft": None, "gs_kt": 476}) is True
    assert is_airborne_kinematic({"alt_ft": 35000, "gs_kt": 60}) is True
    # near-origin high-speed taxi / rejected T/O (gs>=80, no alt, on the field)
    assert is_airborne_kinematic({"alt_ft": None, "gs_kt": 120}, near_origin=True) is False
    assert is_airborne_kinematic({"alt_ft": None, "gs_kt": 120}, near_origin=False) is True


def test_inv2_failing_gate_never_renders_and_never_extrapolates():
    """For ANY position that fails the airborne gate, the resolved state must have
    live=None and must not produce a simulated (gs-extrapolated) ETA."""
    ground_positions = [
        {"lat": MANNHEIM[0], "lon": MANNHEIM[1], "gs_kt": 15, "alt_ft": None},
        {"lat": FRA[0], "lon": FRA[1], "gs_kt": 0, "alt_ft": None},
        {"lat": FRA[0], "lon": FRA[1], "gs_kt": 40, "alt_ft": 50},
        {"lat": FRA[0], "lon": FRA[1], "gs_kt": 120, "alt_ft": None},  # high-speed taxi near origin
    ]
    for pos in ground_positions:
        pos = {**pos, "position_source": 3}
        fs = resolve_flight_state(
            keys={"flight": "LHX", "date": "2026-07-09", "dep_iata": "FRA", "arr_iata": "GVA",
                  "dep_ll": FRA, "arr_ll": GVA, "sched_dep": "2026-07-09T12:00:00Z",
                  "sched_arr": "2026-07-09T13:00:00Z", "sched_dep_ts": NOW - 300},
            observations=[
                Observation("phase_hard", TAXI_OUT, "board", NOW - 60, 0.9, meta={"side": "dep"}),
                Observation("position", pos, "aircraft_live", NOW - 20, 0.8),
            ], now=NOW)
        assert fs["live"] is None, f"gate-failing pos rendered: {pos}"
        assert fs["times"]["eta_conf"] != SIMULATED, f"extrapolated ETA for ground pos: {pos}"


def test_inv3_unknown_delay_is_not_ontime():
    fs = resolve_flight_state(
        keys={"flight": "LHY", "date": "2026-07-09", "dep_iata": "FRA", "arr_iata": "GVA",
              "dep_ll": FRA, "arr_ll": GVA, "sched_dep": "2026-07-09T12:00:00Z",
              "sched_arr": "2026-07-09T13:00:00Z", "sched_dep_ts": NOW - 300},
        observations=[
            Observation("position", {"lat": 47.9, "lon": 7.4, "gs_kt": 430, "alt_ft": 34000,
                                     "position_source": 0}, "adsb", NOW - 20, 0.95),
        ], now=NOW)
    assert fs["delay"]["known"] is False
    assert fs["delay"]["min"] is None
    assert fs["on_time"] is None


def test_inv5_empty_observations_is_unknown():
    fs = resolve_flight_state(
        keys={"flight": "LHZ", "date": "2026-07-09", "dep_iata": "FRA", "arr_iata": "GVA"},
        observations=[], now=NOW)
    assert fs["ok"] is True
    assert fs["phase"] == UNKNOWN
    assert fs["live"] is None
    assert fs["on_time"] is None


def test_scheduled_when_only_roster_clock():
    fs = resolve_flight_state(
        keys={"flight": "LHS", "date": "2026-07-09", "dep_iata": "FRA", "arr_iata": "GVA",
              "sched_dep": "2026-07-09T12:00:00Z", "sched_dep_ts": NOW + 600},
        observations=[], now=NOW)
    assert fs["phase"] == SCHEDULED


def test_boarding_soft_signal():
    fs = resolve_flight_state(
        keys={"flight": "LHB", "date": "2026-07-09", "dep_iata": "FRA", "arr_iata": "GVA",
              "sched_dep": "2026-07-09T04:20:00Z", "sched_dep_ts": NOW + 1200},
        observations=[
            Observation("phase_soft", BOARDING, "board", NOW - 60, 0.7, meta={"side": "dep"}),
        ], now=NOW)
    assert fs["phase"] == BOARDING


def test_diverted_landed_elsewhere():
    fs = resolve_flight_state(
        keys={"flight": "LHD", "date": "2026-07-09", "dep_iata": "FRA", "arr_iata": "HND",
              "dep_ll": FRA, "arr_ll": HND, "sched_dep_ts": NOW - 3 * 3600},
        observations=[
            Observation("event", {"event": "landed", "airport": "UUEE"},  # != HND
                        "warehouse_event", NOW - 120, 0.95),
        ], now=NOW)
    assert fs["phase"] == DIVERTED


def test_stale_landed_not_reflown_by_soft_signal():
    """Monotonicity guard: a prior LANDED must NOT flip to AIRBORNE on a stale/soft
    signal (only a fresh HARD one may — covered by scenario 6)."""
    prior = {"phase": ARRIVED, "conf": OBSERVED, "obs_ts": NOW - 100, "sticky_airborne": False}
    fs = resolve_flight_state(
        keys={"flight": "LHM", "date": "2026-07-09", "dep_iata": "FRA", "arr_iata": "LHR",
              "dep_ll": FRA, "arr_ll": LHR, "sched_dep_ts": NOW - 4000},
        observations=[
            # only a plan clock + an old soft boarding — nothing hard & fresh
            Observation("phase_soft", BOARDING, "board", NOW - 3000, 0.6, meta={"side": "dep"}),
        ], now=NOW, prior=prior)
    assert fs["phase"] == ARRIVED  # stays terminal


# ─────────────────────────── FIND vs SIMULATE ──────────────────────────────

def test_prefers_real_fresh_fix_over_stale_snapshot():
    """When the flight IS findable live, show the REAL fresh fix — never simulate."""
    fs = resolve_flight_state(
        keys={"flight": "LH716", "date": "2026-07-09", "dep_iata": "FRA", "arr_iata": "HND",
              "dep_ll": FRA, "arr_ll": HND, "sched_dep_ts": NOW - 5 * 3600},
        observations=[
            Observation("position", {"lat": 50.5, "lon": 12.0, "track": 90, "gs_kt": 470,
                                     "alt_ft": 36000, "position_source": 0}, "adsb", NOW - 30),
            Observation("position", {"lat": SIBERIA[0], "lon": SIBERIA[1], "track": 90,
                                     "gs_kt": 470, "alt_ft": 37000, "position_source": 3},
                        "aircraft_live", NOW - 2400),  # stale snapshot
        ], now=NOW)
    assert fs["phase"] == AIRBORNE
    assert fs["live"]["source"] == "adsb"
    assert fs["live"]["conf"] == OBSERVED           # real, not simulated
    assert fs["live"]["lat"] == 50.5                # the real fix, not the stale one


def test_simulate_forward_only_when_lost():
    """Flight fell off live coverage (only a stale snapshot): fly it FORWARD along
    its own track + estimate time-to-landing, flagged simulated."""
    last = {"lat": SIBERIA[0], "lon": SIBERIA[1], "track": 90, "gs_kt": 470,
            "alt_ft": 37000, "position_source": 3}
    fs = resolve_flight_state(
        keys={"flight": "LH716", "date": "2026-07-09", "dep_iata": "FRA", "arr_iata": "HND",
              "dep_ll": FRA, "arr_ll": HND, "sched_dep_ts": NOW - 5 * 3600},
        observations=[
            Observation("position", last, "aircraft_live", NOW - 2400),  # stale (>35min, <45min)
        ], now=NOW)
    assert fs["phase"] == AIRBORNE
    assert fs["live"] is not None
    assert fs["live"]["conf"] == SIMULATED
    # position was flown FORWARD along track 90 (east) -> lon increased past the last fix
    assert fs["live"]["lon"] > SIBERIA[1]
    assert fs["live"]["stale_since"] is not None
    assert fs["live_status"] == "simulated"
    # a time-to-landing estimate exists, flagged simulated
    assert fs["times"]["eta_iso"] is not None
    assert fs["times"]["eta_conf"] == SIMULATED


def test_lost_when_unsure_shows_honest_not_ghost():
    """Coverage lost near the destination / in descent -> we don't know if it's
    still flying or already landed -> honest live_status='lost' (FR24-style
    'gelandet oder außer Reichweite'), NO simulated dot."""
    last = {"lat": 36.0, "lon": 139.2, "track": 200, "gs_kt": 250,
            "alt_ft": 8000, "position_source": 3}   # low + near HND = descending to land
    fs = resolve_flight_state(
        keys={"flight": "LH716", "date": "2026-07-09", "dep_iata": "FRA", "arr_iata": "HND",
              "dep_ll": FRA, "arr_ll": HND, "sched_dep_ts": NOW - 12 * 3600},
        observations=[
            Observation("position", last, "aircraft_live", NOW - 2400),  # stale, near dest, low
        ], now=NOW)
    assert fs["live"] is None
    assert fs["live_status"] == "lost"


def test_too_long_gone_no_ghost_dot():
    """Gone longer than the sim horizon (>45 min) -> no live dot (honest offline),
    never an indefinite phantom. Scharfgezogen (P3): war der Flug airborne
    (sticky prior) und die Phase rendert Position, muss live_status='lost'
    kommen (nicht None) — iOS kann seine Vorwärts-Simulation dann stoppen."""
    prior = {"phase": AIRBORNE, "conf": OBSERVED, "obs_ts": NOW - 3000,
             "sticky_airborne": True}
    fs = resolve_flight_state(
        keys={"flight": "LH716", "date": "2026-07-09", "dep_iata": "FRA", "arr_iata": "HND",
              "dep_ll": FRA, "arr_ll": HND, "sched_dep_ts": NOW - 6 * 3600},
        observations=[
            Observation("position", {"lat": SIBERIA[0], "lon": SIBERIA[1], "track": 90,
                                     "gs_kt": 470, "alt_ft": 37000, "position_source": 3},
                        "aircraft_live", NOW - 3000),  # 50 min > SIM horizon
        ], now=NOW, prior=prior)
    assert fs["live"] is None
    assert fs["live_status"] == "lost"     # ehrliches lost, kein stilles None


def test_render_pos_phase_without_candidate_is_lost():
    """Board says proven airborne but NO position candidate exists at all ->
    live=None AND live_status='lost' (RENDER_POS phase must never be a silent
    None — clients would keep simulating a ghost)."""
    fs = resolve_flight_state(
        keys={"flight": "LH717", "date": "2026-07-09", "dep_iata": "FRA", "arr_iata": "HND",
              "dep_ll": FRA, "arr_ll": HND, "sched_dep_ts": NOW - 2 * 3600},
        observations=[
            Observation("phase_hard", AIRBORNE, "board", NOW - 120,
                        meta={"side": "dep", "proven_airborne": True}),
        ], now=NOW)
    assert fs["phase"] == AIRBORNE
    assert fs["live"] is None
    assert fs["live_status"] == "lost"


def test_taxi_phase_live_status_stays_none():
    """Nicht-RENDER_POS-Phasen bleiben live_status=None (kein falsches 'lost')."""
    fs = _s1_taxi()
    assert fs["phase"] == TAXI_OUT
    assert fs["live_status"] is None


# ─────────────────────────── ON-TIME (Owner-Regel D15) ─────────────────────

def _fs_with_delay(arr_delay_min, cancelled=False):
    obs = [Observation("delay", {"delay_known": True, "arr_delay_min": arr_delay_min,
                                 "dep_delay_min": arr_delay_min}, "board", NOW - 60)]
    if cancelled:
        obs.append(Observation("phase_hard", CANCELLED, "board", NOW - 60,
                               meta={"side": "dep", "cancelled": True}))
    return resolve_flight_state(
        keys={"flight": "LHT", "date": "2026-07-09", "dep_iata": "FRA", "arr_iata": "GVA",
              "dep_ll": FRA, "arr_ll": GVA, "sched_dep_ts": NOW - 600},
        observations=obs, now=NOW)


def test_on_time_d15_threshold():
    """Owner-Regel D15: delay < 15 = pünktlich (nicht <=5), >=15 = verspätet."""
    assert _fs_with_delay(12)["on_time"] is True
    assert _fs_with_delay(14)["on_time"] is True
    assert _fs_with_delay(15)["on_time"] is False
    assert _fs_with_delay(40)["on_time"] is False


def test_cancelled_is_never_on_time():
    """annulliert schlägt jeden Delay-Wert — auch delay 0 ist nicht 'pünktlich'."""
    fs = _fs_with_delay(0, cancelled=True)
    assert fs["phase"] == CANCELLED
    assert fs["on_time"] is False


# ─────────────────────────── PRIOR-MEMO ────────────────────────────────────

def test_prior_memo_roundtrip_and_ttl():
    E._PRIOR_STORE.clear()
    fs = _s4_cruise()
    E.remember_state(fs, now=NOW)
    p = E.prior_state("LH1000", "2026-07-09", now=NOW + 60)
    assert p is not None
    assert p["phase"] == AIRBORNE
    assert p["sticky_airborne"] is True
    # abgelaufen (> TTL) → None
    assert E.prior_state("LH1000", "2026-07-09", now=NOW + E._PRIOR_TTL_S + 1) is None
    E._PRIOR_STORE.clear()


def test_prior_memo_feeds_monotonicity():
    """remember_state → prior_state → resolve: ein terminal LANDED prior hält
    gegen ein stales Soft-Signal (der Memo-Weg, nicht nur das prior=-Argument)."""
    E._PRIOR_STORE.clear()
    landed = resolve_flight_state(
        keys={"flight": "LH881", "date": "2026-07-09", "dep_iata": "FRA",
              "arr_iata": "LHR", "dep_ll": FRA, "arr_ll": LHR,
              "sched_dep_ts": NOW - 4000},
        observations=[
            Observation("phase_hard", LANDED, "board", NOW - 300, meta={"side": "arr"}),
        ], now=NOW)
    assert landed["phase"] == LANDED
    E.remember_state(landed, now=NOW)
    fs2 = resolve_flight_state(
        keys={"flight": "LH881", "date": "2026-07-09", "dep_iata": "FRA",
              "arr_iata": "LHR", "dep_ll": FRA, "arr_ll": LHR,
              "sched_dep_ts": NOW - 4000},
        observations=[
            Observation("phase_soft", BOARDING, "board", NOW - 3000, meta={"side": "dep"}),
        ], now=NOW + 120, prior=E.prior_state("LH881", "2026-07-09", now=NOW + 120))
    assert fs2["phase"] == LANDED          # kein Rückfall auf stale BOARDING
    E._PRIOR_STORE.clear()


# ─────────────────────────── HOCHLAND-ALT-GATE ─────────────────────────────

def test_high_elevation_airport_parked_plane_not_airborne():
    """alt_ft ist MSL: ein GEPARKTER Flieger in MEX (7316 ft) darf das
    alt>1000-Gate nicht bestehen — weder mit Elevation (relativ) noch ohne
    (konservativ: alt-only verlangt gs>=50, sofern gs gemeldet)."""
    parked_mex = {"alt_ft": 7400, "gs_kt": 3}
    assert is_airborne_kinematic(parked_mex, dep_elev_ft=7316) is False
    assert is_airborne_kinematic(parked_mex) is False          # konservativ
    # wirklich über MEX (Feld + >1000 ft) → airborne
    assert is_airborne_kinematic({"alt_ft": 9000, "gs_kt": 210},
                                 dep_elev_ft=7316) is True
    # alt-only-Fix OHNE gs bleibt airborne (echte Cruise-Fixe nicht un-fliegen)
    assert is_airborne_kinematic({"alt_ft": 35000, "gs_kt": None}) is True


# ─────────────────────────── PROJECTIONS CONSISTENCY ───────────────────────

def test_projections_agree_on_shared_truth():
    """The four surfaces reduce the SAME FlightState -> they cannot disagree on
    phase/position/delay."""
    fs = _s4_cruise()
    mine = project_my_flight_status(fs)
    live = project_flight_live(fs)
    crew = project_crew_status(fs)
    leg = project_friend_leg(fs)

    # phase agreement (each surface's phase-ish field derives from fs["phase"])
    assert live["phase"] == fs["phase"] == AIRBORNE
    assert leg["status"] == AIRBORNE
    assert mine["phase"] == AIRBORNE
    assert crew["flying_now"] is True
    # delay agreement
    assert mine["delay_min"] == leg["arr_delay_min"] == crew["today_delay_min"] == 3
    # position agreement: all that render position use the SAME fix
    assert leg["live"]["lat"] == crew["live_lat"] == fs["live"]["lat"]


def test_taxi_projections_never_leak_position():
    """The ghost bug at the projection layer: TAXI_OUT must give every surface a
    null live position."""
    fs = _s1_taxi()
    assert project_flight_live(fs)["live"] is None
    assert project_friend_leg(fs)["live"] is None
    assert project_crew_status(fs).get("live_lat") is None
    assert project_flight_live(fs)["in_flight"] is False
