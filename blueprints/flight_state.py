#!/usr/bin/env python3
"""
AeroX Unified FlightState Engine — production module (pure reducer).

Single source of truth for flight phase/position/ETA. Collectors (I/O) emit
Observations; this pure function reduces them; the four surfaces only project.
Runnable demo + full scenario coverage live in tests/test_flight_state.py.
Design: docs/flightstate/DESIGN.md.

Architecture (synthesized from the SIMPLICITY skeleton + AUTHORITY precedence +
COVERAGE never-lose sim):

    signal collectors (I/O, budget, targeted/allow_paid)   <-- NOT here
        -> normalized Observation list
            -> resolve_flight_state()   <-- THIS pure function (zero I/O)
                -> one canonical FlightState
                    -> thin per-surface projections           <-- see project_*()

This file is self-contained and runnable with plain `python3`. The data-fetch
layer is stubbed: each __main__ scenario hands the reducer a plain Observation
list, exactly like a real collector would after hitting NAS RAM / Supabase / an
API. No network, no DB.

Load-bearing invariants (each has a dedicated check in __main__):
  1. AIRBORNE must be EARNED. The raw on_ground bit is ignored entirely.
     A position is airborne only if (alt>1000 OR gs>=80), with a near-origin
     guard on the gs-only branch (rejects high-speed taxi / rejected take-off).
  2. Position is rendered ONLY when phase in {AIRBORNE, APPROACH, DIVERTED}.
     Any position failing the airborne gate => live=None AND no ETA extrapolation.
  3. delay_known=False => delay=None, on_time=None. "unknown != on-time".
     gs-extrapolated ETA NEVER yields a delay number.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# ---------------------------------------------------------------------------
# 0. CONSTANTS / CONFIG
# ---------------------------------------------------------------------------

# Canonical phase alphabet (10 tokens, mutually exclusive, exhaustive).
SCHEDULED = "SCHEDULED"
BOARDING = "BOARDING"
TAXI_OUT = "TAXI_OUT"
AIRBORNE = "AIRBORNE"
APPROACH = "APPROACH"
LANDED = "LANDED"
ARRIVED = "ARRIVED"
CANCELLED = "CANCELLED"
DIVERTED = "DIVERTED"
UNKNOWN = "UNKNOWN"

IN_AIR = {AIRBORNE, APPROACH}
RENDER_POS = {AIRBORNE, APPROACH, DIVERTED}

# Confidence ladder (3 values -> map 1:1 to UI '~' / '(geschätzt)').
OBSERVED = "observed"
ESTIMATED = "estimated"
SIMULATED = "simulated"

# Per-source freshness gates, seconds. A signal older than its gate is DEMOTED
# (a hard signal becomes soft; a position becomes sim/dropped).
MAX_AGE = {
    "adsb": 120,
    "opensky": 300,
    "aircraft_live": 2100,      # 35 min served snapshot
    "fr24_bulk": 2100,
    "fr24_details": 600,
    "warehouse_flight": 900,
    "warehouse_event": 3600,    # events judged by their own `at`
    "board": 720,               # 12 min in-process board cache
    "paid_adb": 600,
    "paid_avstack": 600,
    "roster": 6 * 3600,
    "reference": 10 ** 9,
}

SIM_MAX_AGE_S = 45 * 60           # cap forward-sim at 45 min of last real fix
ETA_OVERRUN_S = 30 * 60           # >30 min past ETA with no arrival -> UNKNOWN
NEAR_AIRPORT_KM = 8.0
# LOST_NEAR_DEST -> LANDED(estimated): Funkstille nach tiefem Fix nahe dem Ziel
# + ETA erreicht (Lane/LX1719 2026-07-17 — Harvester verlor die Maschine in der
# Warteschleife 12 min vor Touchdown, Phase blieb 30+ min AIRBORNE).
LOST_NEAR_DEST_MIN_AGE_S = 10 * 60   # so lange Funkstille, bevor die Regel greift
LOST_NEAR_DEST_KM = 50.0             # letzter Fix höchstens so weit vom Ziel
LOST_NEAR_DEST_MAX_ALT_FT = 8000     # letzter Fix tief (Anflug), kein Überflieger

# Plausible taxi-out window (off-block -> take-off). If a flight has been
# off-block LONGER than this, is still before its expected arrival, and there is
# no landing signal AND no live position (nothing to fail the airborne gate on),
# the engine promotes TAXI_OUT -> AIRBORNE with confidence=ESTIMATED. This is
# TIME-EVIDENCE, not an invented position (live stays None): a plane that pushed
# back 40 min ago on an 11h long-haul with no ADS-B is airborne even though we
# can't see it. Bounded by expected arrival so it can never outlive the flight.
TAXI_OUT_MAX_S = 25 * 60
# A board can leave ``Final approach`` stale after touchdown.  Without a live
# airborne fix we stop carrying that phase beyond the same 40-minute arrival
# grace used by the crew-state resolver.  This keeps flights_live/crew_state
# monotonic without inventing an actual touchdown timestamp.
APPROACH_ARRIVAL_GRACE_S = 40 * 60

# Physik-Schranke gegen eine STALE Board-Landung (owner 2026-07-13, LH454
# FRA->SFO: die Crew stand als "in San Francisco" auf der Live-Map, während der
# +185 min verspätete Flieger noch über dem Atlantik war — die Ankunftsseite
# trug eine Vortags-"Arrived"-Zeile). Eine board-arr "landed/arrived" gilt in T2
# nur, wenn `now` die früheste physikalisch mögliche Ankunft erreicht hat:
# eff. Abflug + Großkreis / v_max + Boden-Overhead − Slack. Bewusst großzügige
# v_max (~950 km/h ≈ 513 kt Block-Schnitt, schneller als real) → fängt NUR den
# krass-unmöglichen Fall, nie einen knappen Grenzfall. Dieselben Zahlen wie
# blueprints/leg_status_gate (die Kalender-/axFlightInfo-Fläche).
_ARR_MAX_EFF_KMH = 950.0
_ARR_GROUND_OVERHEAD_S = 12 * 60
_ARR_LANDED_SLACK_S = 15 * 60


# ---------------------------------------------------------------------------
# 1. OBSERVATION — the only thing the reducer ingests
# ---------------------------------------------------------------------------

@dataclass
class Observation:
    kind: str            # position | phase_hard | phase_soft | dep_time | arr_time
                         # | eta | delay | route | reg | event
    value: Any
    source: str          # roster|board|warehouse_event|warehouse_flight|aircraft_live
                         # |fr24_bulk|fr24_details|adsb|opensky|paid_adb|paid_avstack|reference
    obs_ts: Optional[float] = None
    conf: float = 0.5
    meta: dict = field(default_factory=dict)
    # availability: "ok" (real signal), "absent" (collector ran, nothing found),
    # "unavailable" (collector errored/timed out -> must NOT be read as evidence).
    status: str = "ok"


def _fresh(obs: Observation, now: float) -> bool:
    if obs.obs_ts is None:
        return False
    return (now - obs.obs_ts) <= MAX_AGE.get(obs.source, 600)


def _by(observations, kind=None, source=None):
    out = []
    for o in observations:
        if o.status != "ok":
            continue
        if kind is not None and o.kind != kind:
            continue
        if source is not None and o.source != source:
            continue
        out.append(o)
    return out


def _unavailable_sources(observations) -> set:
    return {o.source for o in observations if o.status == "unavailable"}


# ---------------------------------------------------------------------------
# 2. KINEMATIC GATES — the single airborne test used everywhere
# ---------------------------------------------------------------------------

def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def is_airborne_kinematic(pos: dict, near_origin: bool = False,
                          dep_elev_ft=None) -> bool:
    """THE one true airborne test. Ignores the raw on_ground bit by design.

    alt_ft ist MSL — an Hochland-Airports (MEX 7316 ft, NBO, ADD) besteht ein
    GEPARKTER Flieger sonst das alt>1000-Gate. dep_elev_ft (Referenz-DB) gatet
    die Höhe relativ zur Abflug-Elevation; ohne Elevation konservativ: die
    alt-only-Schiene verlangt zusätzlich gs>=50, sofern gs ÜBERHAUPT gemeldet
    ist (ein alt-only-Fix ohne gs bleibt airborne — kein Un-Fliegen echter
    Cruise-Fixe, ein geparkter Flieger meldet gs≈0, nicht None)."""
    if not pos:
        return False
    alt = _num(pos.get("alt_ft"))
    gs = _num(pos.get("gs_kt"))
    elev = _num(dep_elev_ft)
    if alt is not None:
        if elev is not None:
            if alt > elev + 1000:
                return True
        elif alt > 1000 and (gs is None or gs >= 50):
            return True
    if gs is not None and gs >= 80:
        # gs-only branch: a fast, alt-less fix sitting on the departure field is
        # a high-speed taxi / rejected take-off, NOT cruise. Suppress it.
        if alt is None and near_origin:
            return False
        return True
    return False


def is_grounded_kinematic(pos: dict) -> bool:
    if not pos:
        return False
    alt = _num(pos.get("alt_ft"))
    gs = _num(pos.get("gs_kt"))
    return alt is not None and alt <= 200 and (gs is not None and gs < 60)


def _gc_km(a_lat, a_lon, b_lat, b_lon):
    r = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = math.radians(b_lat - a_lat)
    dl = math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _advance_along_track(lat, lon, track_deg, dist_km):
    """Move a point dist_km along a constant heading (great-circle step). We follow
    the aircraft's OWN last track — not a fresh great-circle to the destination —
    so the simulation continues the real trajectory (e.g. the southern route around
    Russia) instead of snapping onto a forbidden-airspace shortcut."""
    R = 6371.0
    brng = math.radians(track_deg)
    d = dist_km / R
    lat1, lon1 = math.radians(lat), math.radians(lon)
    lat2 = math.asin(math.sin(lat1) * math.cos(d)
                     + math.cos(lat1) * math.sin(d) * math.cos(brng))
    lon2 = lon1 + math.atan2(math.sin(brng) * math.sin(d) * math.cos(lat1),
                             math.cos(d) - math.sin(lat1) * math.sin(lat2))
    return math.degrees(lat2), math.degrees(lon2)


def _simulate_forward(pos, last_ts, arr_ll, now):
    """Flight fell off live coverage: fly its LAST known fix forward along its own
    track by gs x elapsed, and estimate time-to-landing. Returns (projected_pos,
    eta_iso) or None if we can't (no gs/track). Never overshoots the destination.
    This is the ONLY place the engine invents a position — and only when the real
    flight can no longer be found (the caller gates on staleness)."""
    gs = _num(pos.get("gs_kt"))
    track = _num(pos.get("track"))
    if not (gs and gs > 80 and track is not None
            and pos.get("lat") is not None and pos.get("lon") is not None):
        return None
    elapsed_h = max(0.0, (now - (last_ts or now)) / 3600.0)
    dist_km = gs * 1.852 * elapsed_h
    if arr_ll and None not in arr_ll:
        dist_km = min(dist_km, _gc_km(pos["lat"], pos["lon"], arr_ll[0], arr_ll[1]))
    nlat, nlon = _advance_along_track(pos["lat"], pos["lon"], track, dist_km)
    eta_iso = None
    if arr_ll and None not in arr_ll and gs > 80:
        rem_km = _gc_km(nlat, nlon, arr_ll[0], arr_ll[1])
        eta_iso = _ts_to_iso(now + (rem_km / (gs * 1.852)) * 3600)
    proj = dict(pos)
    proj["lat"], proj["lon"] = nlat, nlon
    return proj, eta_iso


def _progress(dep_ll, dst_ll, pos):
    if not (dep_ll and dst_ll and pos and pos.get("lat") is not None):
        return None
    total = _gc_km(dep_ll[0], dep_ll[1], dst_ll[0], dst_ll[1])
    if total <= 1:
        return None
    remaining = _gc_km(pos["lat"], pos["lon"], dst_ll[0], dst_ll[1])
    return max(0.0, min(1.0, 1.0 - remaining / total))


# ---------------------------------------------------------------------------
# 3. CODESHARE — marketing -> operating flight/callsign (closes the shared gap)
# ---------------------------------------------------------------------------

def resolve_operating_key(keys: dict, observations) -> dict:
    """Rosters carry the MARKETING flight number; boards / ADS-B callsigns carry
    the OPERATING one. A codeshare collector emits kind='reg' or a dedicated
    'codeshare' observation carrying oper_flight/oper_callsign. We fold it into
    the query keys so downstream board/position/reg lookups match the metal."""
    out = dict(keys)
    for o in observations:
        cs = o.meta.get("codeshare")
        if cs:
            out.setdefault("mkt_flight", keys.get("flight"))
            out["flight"] = cs.get("oper_flight", keys.get("flight"))
            out["callsign"] = cs.get("oper_callsign", out.get("callsign"))
            out["oper_carrier"] = cs.get("oper_carrier")
    return out


# ---------------------------------------------------------------------------
# 4. PER-OUTPUT PRECEDENCE REDUCERS
# ---------------------------------------------------------------------------

# authority rank: lower index = higher authority (used only to break obs_ts ties)
POSITION_RANK = ["adsb", "aircraft_live", "warehouse_flight", "fr24_bulk",
                 "fr24_details", "paid_adb"]
ROUTE_RANK = ["ax_route_cache", "board", "warehouse_flight", "fr24_bulk",
              "aircraft_live", "roster", "paid_adb", "paid_avstack"]
REG_RANK = ["board", "warehouse_flight", "aircraft_live", "fr24_bulk",
            "paid_adb", "reference"]


def _pick_position(observations, now):
    """Precedence. Returns (pos_value, source, obs_ts) of the best RAW candidate.

    Includes fixes up to the SIM horizon (not just the per-source freshness gate)
    so a flight that fell off live coverage still has a last-known fix to fly
    forward from (the caller decides observed-vs-simulated by freshness). A FRESH
    real ADS-B fix always outranks a stale snapshot — so when the flight IS
    findable we show the real one and never simulate."""
    cands = [o for o in _by(observations, kind="position")
             if o.obs_ts is not None and (now - o.obs_ts) <= SIM_MAX_AGE_S]
    if not cands:
        return None, None, None

    def key(o):
        try:
            rank = POSITION_RANK.index(o.source)
        except ValueError:
            rank = len(POSITION_RANK)
        fresh = 0 if _fresh(o, now) else 1          # fresh beats stale outright
        real = 0 if o.value.get("position_source", 3) == 0 else 1
        return (fresh, real, rank, -(o.obs_ts or 0))

    best = sorted(cands, key=key)[0]
    return best.value, best.source, best.obs_ts


def _pick_route(observations, keys):
    cands = _by(observations, kind="route")
    if not cands:
        # fall back to the roster leg
        return {"dep": keys.get("dep_iata"), "dst": keys.get("arr_iata"),
                "source": "roster", "conf": ESTIMATED, "label_locked_to_plan": True}

    def key(o):
        try:
            rank = ROUTE_RANK.index(o.source)
        except ValueError:
            rank = len(ROUTE_RANK)
        confirmed = 0 if o.value.get("confidence") == "confirmed" else 1
        return (rank, confirmed)

    best = sorted(cands, key=key)[0]
    v = dict(best.value)
    # consistency gate (Miami "stimmt nicht"): a live route replaces the plan
    # only if confirmed AND its dep matches the roster leg's dep.
    plan_dep = keys.get("dep_iata")
    locked = False
    if best.source in ("fr24_bulk", "aircraft_live") and v.get("confidence") != "confirmed":
        v = {"dep": plan_dep, "dst": keys.get("arr_iata")}
        locked = True
    v.setdefault("source", best.source)
    v.setdefault("conf", OBSERVED if v.get("confidence") == "confirmed" else ESTIMATED)
    v["label_locked_to_plan"] = locked
    return v


def _pick_reg(observations, keys):
    cands = _by(observations, kind="reg")
    # drop warehouse tails proven wrong
    cands = [o for o in cands if not (o.source == "warehouse_flight"
                                      and o.meta.get("verify") is False)]
    if not cands:
        return {"reg": keys.get("roster_tail"), "conf": ESTIMATED,
                "ac_type": None, "hex": None, "swap": False}

    def key(o):
        try:
            rank = REG_RANK.index(o.source)
        except ValueError:
            rank = len(REG_RANK)
        return (rank, -(o.value.get("tail_confidence", 0)))

    best = sorted(cands, key=key)[0]
    v = best.value
    swap = (keys.get("roster_tail") is not None
            and v.get("reg") is not None
            and v["reg"] != keys["roster_tail"]
            and best.meta.get("flightno_matched", False))
    return {"reg": v.get("reg"), "hex": v.get("hex"), "ac_type": v.get("ac_type"),
            "conf": OBSERVED if best.source in ("board", "warehouse_flight",
                                                "aircraft_live") else ESTIMATED,
            "swap": swap, "source": best.source}


def _pick_times_delay(observations, keys, now):
    """dep/arr/eta ladder + the single delay truth."""
    times = {
        "sched_dep_iso": keys.get("sched_dep"), "est_dep_iso": None, "act_dep_iso": None,
        "sched_arr_iso": keys.get("sched_arr"), "est_arr_iso": None, "act_arr_iso": None,
        "eta_iso": None, "dep_conf": ESTIMATED, "arr_conf": ESTIMATED, "eta_conf": None,
    }
    for o in _by(observations, kind="dep_time"):
        v = o.value
        if v.get("actual"):
            times["act_dep_iso"] = v["actual"]; times["dep_conf"] = OBSERVED
        if v.get("est"):
            times["est_dep_iso"] = times["est_dep_iso"] or v["est"]
    for o in _by(observations, kind="arr_time"):
        v = o.value
        if v.get("actual"):
            times["act_arr_iso"] = v["actual"]; times["arr_conf"] = OBSERVED
        if v.get("est"):
            times["est_arr_iso"] = times["est_arr_iso"] or v["est"]

    # DELAY — single source of truth: board dual-side merge, delay_known gated.
    delay = {"known": False, "min": None, "side": None,
             "dep_delay_min": None, "arr_delay_min": None, "conf": None}
    for o in sorted(_by(observations, kind="delay"), key=lambda x: -(x.obs_ts or 0)):
        v = o.value
        if v.get("delay_known"):
            delay = {
                "known": True,
                "dep_delay_min": v.get("dep_delay_min"),
                "arr_delay_min": v.get("arr_delay_min"),
                "min": v.get("arr_delay_min") if v.get("arr_delay_min") is not None
                       else v.get("dep_delay_min"),
                "side": "arr" if v.get("arr_delay_min") is not None else "dep",
                "conf": OBSERVED,
            }
            break
    return times, delay


def _derive_on_time(delay, cancelled=False):
    """Owner-Regel D15 (wie aerox_data_blueprint._derive_on_time): annulliert ⇒
    nie pünktlich; unbekannt ⇒ None (unknown != on-time); delay < 15 = on_time."""
    if cancelled:
        return False
    if not delay.get("known"):
        return None
    m = delay.get("min")
    return None if m is None else (m < 15)


# ---------------------------------------------------------------------------
# 5. PHASE STATE MACHINE (trigger-priority reducer)
# ---------------------------------------------------------------------------

def _board_hard(observations, side):
    for o in _by(observations, kind="phase_hard", source="board"):
        if o.meta.get("side") == side:
            return o
    return None


def _event(observations, kind):
    for o in _by(observations, kind="event"):
        if o.value.get("event") == kind:
            return o
    return None


def _offblock_ts(observations, keys, board_dep):
    """Best estimate of the OFF-BLOCK unix time (when the plane pushed back).
    Preference: revised board dep est > sched_dep + observed dep_delay >
    sched_dep_ts > the board dep-side observation's own obs_ts. None if nothing.
    Used only by the TAXI_OUT->AIRBORNE time-elevation (never invents a time)."""
    # 1) revised board departure estimate
    for o in _by(observations, kind="dep_time"):
        est = o.value.get("est")
        ts = _iso_to_ts(est) if est else None
        if ts is not None:
            return ts
    # 2) sched_dep + observed dep_delay
    base = keys.get("sched_dep_ts")
    if base is None and keys.get("sched_dep"):
        base = _iso_to_ts(keys.get("sched_dep"))
    if base is not None:
        for o in _by(observations, kind="delay"):
            if o.value.get("delay_known") and o.value.get("dep_delay_min") is not None:
                return base + float(o.value["dep_delay_min"]) * 60.0
        return base
    # 3) the off-block board observation timestamp itself
    return board_dep.obs_ts if board_dep else None


def _expected_arr_ts(observations, keys):
    """Best estimate of the expected ARRIVAL unix time. Preference: revised board
    arr est/actual > sched_arr + observed arr_delay > bare sched_arr. None if
    unknown -> the arrival guard is then skipped (we don't block the elevation on
    a missing arrival clock). The delay term matters for the crew obs shape, where
    the board record carries an arr_delay but no revised est_arr string."""
    for o in _by(observations, kind="arr_time"):
        for k in ("est", "actual"):
            ts = _iso_to_ts(o.value.get(k)) if o.value.get(k) else None
            if ts is not None:
                return ts
    base = _iso_to_ts(keys.get("sched_arr")) if keys.get("sched_arr") else None
    if base is not None:
        for o in _by(observations, kind="delay"):
            if o.value.get("delay_known") and o.value.get("arr_delay_min") is not None:
                return base + float(o.value["arr_delay_min"]) * 60.0
    return base


def _arr_landed_physically_possible(observations, keys, now):
    """Darf eine board-arr "landed/arrived" (T2) als wahr gelten? NUR wenn `now`
    die früheste physikalisch mögliche Ankunft erreicht hat (eff. Abflug +
    Großkreis / v_max + Boden-Overhead − Slack). Fängt die STALE Vortags-Ankunft,
    die eine noch fliegende Langstrecke fälschlich auf LANDED kippt (owner
    2026-07-13, LH454 FRA->SFO: Crew stand auf der Live-Map schon in San
    Francisco, während der +185 min verspätete Flieger noch über dem Atlantik
    war). FAIL-OPEN (True), wenn Abflugzeit ODER eine Koordinate fehlt — wir
    unterdrücken nur nachweisbar Unmögliches, erfinden nie eine Phase. Wirft nie.

    Abflug-Schätzung: der BESTE (revidierte/verspätete) Off-Block-Zeitpunkt via
    `_offblock_ts` (revised board est > sched+delay > sched) — so bekommt ein
    verspäteter Flug die korrekt SPÄTERE Schranke, nicht die des Plan-Abflugs."""
    dep_ll = keys.get("dep_ll")
    arr_ll = keys.get("arr_ll")
    if not (dep_ll and arr_ll and None not in dep_ll and None not in arr_ll):
        return True
    # NUR ein ECHTER Abflug-Zeitstempel taugt als Schranke: revidierte Board-Esti >
    # sched_dep + beobachteter dep_delay > sched_dep(_ts). NIE der board-obs-Stempel
    # (`_offblock_ts`-Fallback) — der ist vom Collector auf `now` gesetzt (der Merge
    # trägt keinen Row-Zeitstempel) und würde die Schranke auf ~now legen → eine
    # echte, gerade gelandete Kurzstrecke (MUC->FRA "Landed 14:23") fiele fälschlich
    # raus. Fehlt eine echte Abflugzeit → fail-open (nichts beweisbar).
    dep_ts = None
    for o in _by(observations, kind="dep_time"):
        est = o.value.get("est")
        ts = _iso_to_ts(est) if est else None
        if ts is not None:
            dep_ts = ts
            break
    if dep_ts is None:
        base = keys.get("sched_dep_ts")
        if base is None and keys.get("sched_dep"):
            base = _iso_to_ts(keys.get("sched_dep"))
        if base is not None:
            dep_ts = base
            for o in _by(observations, kind="delay"):
                if o.value.get("delay_known") and o.value.get("dep_delay_min") is not None:
                    dep_ts = base + float(o.value["dep_delay_min"]) * 60.0
                    break
    if dep_ts is None:
        return True
    try:
        dist_km = _gc_km(dep_ll[0], dep_ll[1], arr_ll[0], arr_ll[1])
    except (TypeError, ValueError):
        return True
    earliest = dep_ts + (dist_km / _ARR_MAX_EFF_KMH) * 3600.0 + _ARR_GROUND_OVERHEAD_S
    return now >= (earliest - _ARR_LANDED_SLACK_S)


def _phase_machine(observations, keys, raw_pos, near_origin, prior, now):
    """Returns dict(phase, conf, source, obs_ts, sticky_airborne)."""
    airborne_kin = is_airborne_kinematic(
        raw_pos, near_origin=near_origin,
        dep_elev_ft=keys.get("dep_elev_ft")) if raw_pos else False

    def R(phase, conf, source, ts):
        return {"phase": phase, "conf": conf, "source": source, "obs_ts": ts,
                "airborne_kin": airborne_kin}

    # ---- T0 cancelled (absolute) ----
    for o in _by(observations, kind="phase_hard"):
        if o.value == CANCELLED or o.meta.get("cancelled"):
            return R(CANCELLED, OBSERVED, o.source, o.obs_ts)

    # ---- T1 diverted: landed event / ground fix at airport != dest ----
    ev_land = _event(observations, "landed")
    if ev_land and ev_land.value.get("airport") and \
            ev_land.value["airport"] not in (keys.get("arr_iata"), keys.get("arr_icao")):
        return R(DIVERTED, OBSERVED, "warehouse_event", ev_land.obs_ts)

    # ---- T2 arrived / landed (arr-side hard wins outright) ----
    board_arr = _board_hard(observations, "arr")
    if ev_land:
        onblock = ev_land.meta.get("on_block")
        return R(ARRIVED if onblock else LANDED, OBSERVED, "warehouse_event", ev_land.obs_ts)
    # PHYSIK-GATE (owner 2026-07-13, LH454->SFO): eine board-arr "landed/arrived"
    # gewinnt nur, wenn die Landung physikalisch schon möglich ist. Eine STALE
    # Vortags-Ankunft (arr-Seite ohne heutige Obs) darf eine noch fliegende
    # Langstrecke NICHT auf LANDED kippen — sonst steht die Crew auf der Live-Map
    # am Ziel, während sie noch über dem Ozean ist. Unplausibel → NICHT hier
    # terminieren, sondern zur Airborne-/Taxi-Erkennung (T3/T4) durchfallen.
    # Ausgenommen bleibt das echte warehouse "landed"-EVENT oben (ev_land) —
    # ein tatsächliches Landeereignis, kein flippiger Board-Status.
    if board_arr and board_arr.value in (LANDED, ARRIVED) and \
            _arr_landed_physically_possible(observations, keys, now):
        return R(board_arr.value, OBSERVED, "board", board_arr.obs_ts)
    for o in _by(observations, kind="arr_time"):
        if o.value.get("actual") and \
                _arr_landed_physically_possible(observations, keys, now):
            return R(ARRIVED, OBSERVED, o.source, o.obs_ts)

    # ---- T3 airborne: REQUIRES PROOF ----
    ev_off = _event(observations, "takeoff")
    fr24_stage = None
    for o in _by(observations, kind="phase_hard", source="fr24_details"):
        if o.value == AIRBORNE:
            fr24_stage = o
    board_enroute = None
    for o in _by(observations, kind="phase_hard", source="board"):
        # Explicit airborne/en-route/approach on either board side is proof
        # (NOT bare departure-side 'Abgeflogen'=off-block).
        if o.value in (AIRBORNE, APPROACH) and o.meta.get("proven_airborne"):
            board_enroute = o
    # Provider boards occasionally never replace ``Final approach`` with a
    # landed token.  Once its expected arrival is more than 40 minutes old and
    # there is no current airborne telemetry, the only defensible lifecycle
    # state is an *estimated* landing.  Otherwise flights_live remained
    # APPROACH for hours while crew_state had already advanced to LANDED.
    if board_enroute and board_enroute.value == APPROACH and not airborne_kin:
        exp_arr = _expected_arr_ts(observations, keys)
        if exp_arr is not None and now >= exp_arr + APPROACH_ARRIVAL_GRACE_S:
            return R(LANDED, ESTIMATED, "approach_timeout", board_enroute.obs_ts)
    airborne_trigger = ev_off or fr24_stage or board_enroute or (airborne_kin and raw_pos)
    if airborne_trigger:
        src = ("warehouse_event" if ev_off else
               "fr24_details" if fr24_stage else
               "board" if board_enroute else "kinematic")
        ts = (ev_off or fr24_stage or board_enroute or Observation("", None, "", now)).obs_ts \
            if (ev_off or fr24_stage or board_enroute) else now
        conf = OBSERVED
        res = R(AIRBORNE, conf, src, ts)
        if board_enroute and board_enroute.value == APPROACH:
            res["phase"] = APPROACH
        # APPROACH refinement (wording only)
        if raw_pos:
            dep_ll, dst_ll = keys.get("dep_ll"), keys.get("arr_ll")
            p = _progress(dep_ll, dst_ll, raw_pos)
            vr = _num(raw_pos.get("vertical_rate"))
            if (p is not None and p >= 0.80) or (vr is not None and vr < -500):
                res["phase"] = APPROACH
        return res

    # ---- Sticky-airborne: a prior gate-passing sample survives one slow/low dip ----
    if prior and prior.get("phase") in IN_AIR and prior.get("sticky_airborne"):
        # only a HARD ground signal (handled in T1/T2 above) leaves AIRBORNE.
        return R(prior["phase"], SIMULATED if not airborne_kin else OBSERVED,
                 "sticky", prior.get("obs_ts") or now)

    # ---- T4 TAXI_OUT: off-block but not proven airborne (the taxi trap) ----
    board_dep = _board_hard(observations, "dep")
    dep_offblock = (board_dep is not None and board_dep.value in (TAXI_OUT, AIRBORNE)) \
        or _event(observations, "offblock") is not None
    if (raw_pos and not airborne_kin and dep_offblock) or \
       (board_dep is not None and board_dep.value == TAXI_OUT):
        # TIME-ELEVATION (owner 2026-07-13): a plane that has been off-block far
        # longer than a plausible taxi window, is still before its expected
        # arrival, and has NO live position we can see (over ocean / no ADS-B) is
        # airborne even without a fix. We elevate to AIRBORNE with conf=ESTIMATED
        # and NO position (live stays None -> no ghost dot). If there IS a live
        # position it must fail the gate to reach here (visible on the ground) —
        # then we do NOT elevate: the plane really is still taxiing (ghost-fix).
        if raw_pos is None and dep_offblock:
            offb = _offblock_ts(observations, keys, board_dep)
            exp_arr = _expected_arr_ts(observations, keys)
            long_offblock = offb is not None and (now - offb) > TAXI_OUT_MAX_S
            before_arr = exp_arr is None or now < exp_arr
            if long_offblock and before_arr:
                return R(AIRBORNE, ESTIMATED, "offblock_time",
                         (board_dep.obs_ts if board_dep else offb))
        return R(TAXI_OUT, OBSERVED, "board", (board_dep.obs_ts if board_dep else now))

    # ---- T5 BOARDING (soft) ----
    for o in _by(observations, kind="phase_soft", source="board"):
        if o.value == BOARDING and o.meta.get("side", "dep") == "dep":
            return R(BOARDING, ESTIMATED, "board", o.obs_ts)

    # ---- T6 SCHEDULED (plan clock) ----
    if keys.get("sched_dep_ts") is not None:
        return R(SCHEDULED, ESTIMATED, "roster", keys.get("sched_dep_ts"))

    # ---- T7 UNKNOWN ----
    return R(UNKNOWN, OBSERVED, "none", now)


# Normal-lifecycle ordering (special phases CANCELLED/DIVERTED/UNKNOWN excluded).
_LIFECYCLE_RANK = {SCHEDULED: 0, BOARDING: 1, TAXI_OUT: 2, AIRBORNE: 3,
                   APPROACH: 4, LANDED: 5, ARRIVED: 6}


def _apply_monotonicity(cur, prior, now):
    """Once a flight reaches a terminal phase (LANDED/ARRIVED) it must not regress
    to ANY earlier lifecycle phase (AIRBORNE, TAXI_OUT, BOARDING, SCHEDULED)
    except on a FRESH HARD signal newer than the one that set the terminal phase
    (a real go-around / return-to-service). This blocks a stale board from either
    un-landing a flight (LANDED->AIRBORNE) OR un-arriving it (ARRIVED->BOARDING).
    CANCELLED/DIVERTED are terminal-exempt (decided as T0/T1 before this)."""
    if not prior:
        return cur
    if prior.get("phase") in (LANDED, ARRIVED) and cur["phase"] in _LIFECYCLE_RANK:
        cur_rank = _LIFECYCLE_RANK[cur["phase"]]
        prior_rank = _LIFECYCLE_RANK.get(prior["phase"], 99)
        if cur_rank < prior_rank:                       # genuine backward move
            cur_hard = cur["conf"] == OBSERVED and cur["source"] in (
                "warehouse_event", "board", "fr24_details")
            newer = (cur.get("obs_ts") or 0) > (prior.get("obs_ts") or 0)
            if not (cur_hard and newer):
                # keep the terminal phase (stale board can't un-land/un-arrive)
                return {"phase": prior["phase"], "conf": prior.get("conf", OBSERVED),
                        "source": prior.get("source", "hysteresis"),
                        "obs_ts": prior.get("obs_ts"),
                        "airborne_kin": cur.get("airborne_kin", False)}
    return cur


# ---------------------------------------------------------------------------
# 6. THE REDUCER
# ---------------------------------------------------------------------------

def resolve_flight_state(keys: dict, observations: list, *, now: Optional[float] = None,
                         prior: Optional[dict] = None) -> dict:
    now = now or time.time()
    observations = [o if isinstance(o, Observation) else Observation(**o)
                    for o in observations]

    # 0. codeshare: marketing -> operating keys
    keys = resolve_operating_key(keys, observations)

    # 1. route (+consistency gate)
    route = _pick_route(observations, keys)
    # 2. reg (+swap detection)
    reg = _pick_reg(observations, keys)
    # 3. best RAW position candidate
    raw_pos, pos_src, pos_ts = _pick_position(observations, now)

    # near-origin guard for the gs-only airborne branch
    near_origin = False
    if raw_pos and keys.get("dep_ll") and raw_pos.get("lat") is not None:
        near_origin = _gc_km(raw_pos["lat"], raw_pos["lon"],
                             keys["dep_ll"][0], keys["dep_ll"][1]) < NEAR_AIRPORT_KM

    # 4. PHASE machine
    ph = _phase_machine(observations, keys, raw_pos, near_origin, prior, now)
    ph = _apply_monotonicity(ph, prior, now)

    phase = ph["phase"]
    phase_conf = ph["conf"]
    phase_source = ph["source"]

    # sticky flag: set once a fix passed the gate this leg
    sticky = ph.get("airborne_kin") or (prior and prior.get("sticky_airborne") and
                                        phase in IN_AIR)

    # 5. hard cap: airborne with no fresh signal too long -> UNKNOWN (never phantom)
    times, delay = _pick_times_delay(observations, keys, now)
    eta_iso, eta_conf = _resolve_eta(observations, keys, raw_pos, phase, now)

    # 6. POSITION output gate + forward-sim (COVERAGE never-lose, bounded).
    # live_status: 'live' = real fresh fix · 'simulated' = flown forward (we're
    # confident it's still airborne) · 'lost' = coverage gone AND we are NOT sure
    # it's still flying (near destination / descending / ETA reached) -> honest
    # "gelandet oder außer Reichweite", never a guessed dot (FR24-style) · None =
    # not a position-rendering phase.
    live = None
    progress = None
    live_status = None
    pos_fresh = raw_pos is not None and pos_ts is not None and \
        (now - pos_ts) <= MAX_AGE.get(pos_src, 600)
    airborne_ok = raw_pos is not None and is_airborne_kinematic(
        raw_pos, near_origin, dep_elev_ft=keys.get("dep_elev_ft"))

    render_pos = raw_pos                       # the position we actually render
    if phase in RENDER_POS:
        if raw_pos and airborne_ok and pos_fresh:
            # REAL, fresh fix — always preferred. We found the flight; show it.
            live = _mk_live(raw_pos, pos_src, OBSERVED if pos_src == "adsb" else
                            (OBSERVED if (now - pos_ts) < 300 else ESTIMATED),
                            pos_ts, None)
            live_status = "live"
        elif raw_pos and airborne_ok and not pos_fresh:
            # Live coverage lost. Simulate forward ONLY IF we are confident the
            # plane is still airborne: it was in cruise, mid-route, and the ETA is
            # not near. If it was descending / near the destination / past ETA, we
            # genuinely don't know whether it's still flying OR already landed —
            # so be HONEST (live_status='lost') instead of drawing a ghost dot.
            age = now - pos_ts
            _alt = _num(raw_pos.get("alt_ft"))
            _prog = _progress(keys.get("dep_ll"), keys.get("arr_ll"), raw_pos)
            _eta_ts = _iso_to_ts(eta_iso) if eta_iso else None
            confident_airborne = (
                age <= SIM_MAX_AGE_S
                and not (_prog is not None and _prog >= 0.90)            # near destination
                and not (_alt is not None and _alt < 18000)              # descending / low
                and not (_eta_ts is not None and now >= _eta_ts - 300)   # ETA basically reached
            )
            if confident_airborne:
                sim = _simulate_forward(raw_pos, pos_ts, keys.get("arr_ll"), now)
                if sim:
                    render_pos, sim_eta = sim
                    live = _mk_live(render_pos, pos_src, SIMULATED, pos_ts, stale_since=pos_ts)
                    if sim_eta and eta_conf in (None, SIMULATED):
                        eta_iso, eta_conf = sim_eta, SIMULATED
                else:
                    live = _mk_live(raw_pos, pos_src, SIMULATED, pos_ts, stale_since=pos_ts)
                live_status = "simulated"
            else:
                live = None                     # honest: landed or out of coverage
                live_status = "lost"
        # a fix that fails the airborne gate is NEVER rendered (invariant #2)

        if live is None and live_status is None:
            # EHRLICHES lost (P3): Phase sagt „rendert Position", aber es gibt
            # keinen (renderbaren) Kandidaten — 'lost' statt None, damit die
            # Clients ihre Vorwärts-Simulation stoppen KÖNNEN (kein Geister-Dot).
            live_status = "lost"

        if live:
            progress = _progress(keys.get("dep_ll"), keys.get("arr_ll"), render_pos)

    # LOST_NEAR_DEST -> LANDED(estimated) (Lane/LX1719 2026-07-17): der FR24-
    # Harvester verlor die Maschine in der ZRH-Warteschleife auf 5300 ft, 12 min
    # vor Touchdown — danach kam NIE wieder ein Fix und (euscraper-Lücke) keine
    # ARR-Obs. Die 35-min-Freshness von aircraft_live hielt den 31-min-Fix für
    # „live" → Feed zeigte 30+ min nach Landung „IM FLUG" bei 244 kt. Das
    # Freshness-Fenster ist für Cruise-Abdeckungslöcher (Ozean) gebaut — im
    # dichten Terminal-Luftraum unter ~8000 ft bedeutet >10 min Funkstille bei
    # erreichter ETA: gelandet. Physik überstimmt hier die Freshness (analog
    # approach_timeout, conf=ESTIMATED). Distanz+Höhen-Gate schützt Überflieger;
    # ein echter frischer airborne-Fix ginge als neue Obs wieder durch T3.
    if phase in IN_AIR and raw_pos and pos_ts:
        _lost_age = now - pos_ts
        _lost_alt = _num(raw_pos.get("alt_ft"))
        _lost_arr_ll = keys.get("arr_ll")
        _lost_dist = None
        if _lost_arr_ll and None not in _lost_arr_ll and raw_pos.get("lat") is not None:
            _lost_dist = _gc_km(raw_pos["lat"], raw_pos["lon"],
                                _lost_arr_ll[0], _lost_arr_ll[1])
        _lost_eta = (_iso_to_ts(eta_iso) if eta_iso else None) \
            or _expected_arr_ts(observations, keys)
        if (_lost_age > LOST_NEAR_DEST_MIN_AGE_S
                and _lost_dist is not None and _lost_dist < LOST_NEAR_DEST_KM
                and _lost_alt is not None and _lost_alt < LOST_NEAR_DEST_MAX_ALT_FT
                and _lost_eta is not None and now >= _lost_eta):
            phase, phase_conf, phase_source = LANDED, ESTIMATED, "signal_lost_near_dest"
            live, progress, live_status = None, None, "lost"

    # hard cap: sim running too long past ETA with no arrival -> UNKNOWN
    if phase in IN_AIR and live and live["conf"] == SIMULATED:
        if eta_iso and _iso_to_ts(eta_iso) and (now - _iso_to_ts(eta_iso)) > ETA_OVERRUN_S:
            phase, phase_conf, live, progress = UNKNOWN, OBSERVED, None, None
            live_status = "lost"
        elif keys.get("sched_flight_min") and \
                (now - (keys.get("sched_dep_ts") or now)) > (keys["sched_flight_min"] + 45) * 60 \
                and pos_ts and (now - pos_ts) > SIM_MAX_AGE_S:
            phase, phase_conf, live, progress = UNKNOWN, OBSERVED, None, None
            live_status = "lost"

    on_time = _derive_on_time(delay, cancelled=(phase == CANCELLED))

    fs = {
        "ok": True,
        "schema": "flightstate/1",
        "keys": {
            "flight": keys.get("flight"), "mkt_flight": keys.get("mkt_flight"),
            "date": keys.get("date"), "dep_iata": route.get("dep"),
            "arr_iata": route.get("dst"), "leg_index": keys.get("leg_index", 0),
        },
        "phase": phase,
        "phase_conf": phase_conf,
        "phase_source": phase_source,
        "in_flight": phase in IN_AIR,
        "on_time": on_time,
        "cancelled": phase == CANCELLED,
        "diverted_to": (raw_pos or {}).get("landed_at") if phase == DIVERTED else None,
        "route": {"dep": route.get("dep"), "dst": route.get("dst"),
                  "conf": route.get("conf"), "source": route.get("source"),
                  "label_locked_to_plan": route.get("label_locked_to_plan", False)},
        "reg": reg.get("reg"), "reg_conf": reg.get("conf"),
        "reg_swap": reg.get("swap"), "hex": reg.get("hex"), "ac_type": reg.get("ac_type"),
        "callsign": keys.get("callsign"),
        "times": {**times, "eta_iso": eta_iso, "eta_conf": eta_conf},
        "delay": delay,
        "live": live,
        "live_status": live_status,   # live | simulated | lost | None
        "progress": progress,
        "sticky_airborne": bool(sticky),
        "obs_ts": ph.get("obs_ts"),
        "freshness": {"as_of": _ts_to_iso(now),
                      "degraded": bool(live and live.get("conf") == SIMULATED)},
        "sources": sorted({o.source for o in observations if o.status == "ok"}),
        "unavailable": sorted(_unavailable_sources(observations)),
    }
    return fs


def _resolve_eta(observations, keys, raw_pos, phase, now):
    # 1 paid actual/revised  2 board est_arr  3 fr24 details eta  4 warehouse est
    # 5 roster arr  6 gs-extrapolation (ONLY when airborne, flagged estimated)
    for o in _by(observations, kind="arr_time"):
        if o.value.get("actual"):
            return o.value["actual"], OBSERVED
    for src in ("paid_adb", "board", "fr24_details", "warehouse_flight"):
        for o in _by(observations, kind="eta", source=src):
            return o.value.get("eta"), ESTIMATED
        for o in _by(observations, kind="arr_time", source=src):
            if o.value.get("est"):
                return o.value["est"], ESTIMATED
    # roster plan
    if keys.get("sched_arr"):
        # only gs-extrapolate when genuinely airborne with a real gs
        if phase in IN_AIR and raw_pos and _num(raw_pos.get("gs_kt")) and \
                _num(raw_pos.get("gs_kt")) >= 80 and keys.get("arr_ll"):
            remaining = _gc_km(raw_pos["lat"], raw_pos["lon"],
                               keys["arr_ll"][0], keys["arr_ll"][1])
            hrs = remaining / (_num(raw_pos["gs_kt"]) * 1.852)
            return _ts_to_iso(now + hrs * 3600), SIMULATED
        return keys["sched_arr"], ESTIMATED
    return None, None


def _mk_live(pos, source, conf, obs_ts, stale_since):
    return {
        "lat": pos.get("lat"), "lon": pos.get("lon"),
        "track": pos.get("track"), "gs": pos.get("gs_kt"), "alt": pos.get("alt_ft"),
        "on_ground": False,  # engine-decided: only airborne fixes reach here
        "conf": conf, "source": source,
        "obs_ts": _ts_to_iso(obs_ts), "stale_since": _ts_to_iso(stale_since) if stale_since else None,
        "position_source": pos.get("position_source", 3),
    }


# ---------------------------------------------------------------------------
# 6b. PRIOR-MEMO — in-process (flight, date) → letztes Engine-Resultat
# ---------------------------------------------------------------------------
# Monotonie (LANDED regressiert nie auf stale Signale) und Sticky-Airborne
# wirken nur MIT prior — die Flips reichen ihn hierüber durch. Best-effort
# In-Process-Memo (wie die Endpoint-Memos): TTL + Größen-Kappung, nie werfend.

_PRIOR_TTL_S = 30 * 60
_PRIOR_MAX = 4096
_PRIOR_STORE: dict = {}      # (FLIGHT, date) → (stored_unix, prior_dict)


def _prior_key(flight, date):
    return ((flight or "").replace(" ", "").upper(), date or "")


def prior_state(flight, date, *, now: Optional[float] = None) -> Optional[dict]:
    """Letztes gemerktes Engine-Resultat für (flight, date) als prior=-Dict für
    resolve_flight_state, None wenn unbekannt/abgelaufen."""
    now = now or time.time()
    v = _PRIOR_STORE.get(_prior_key(flight, date))
    if not v or (now - v[0]) > _PRIOR_TTL_S:
        return None
    return dict(v[1])


def remember_state(fs: dict, *, now: Optional[float] = None) -> None:
    """FlightState-Resultat fürs nächste Resolve merken (unter operating- UND
    marketing-Flugnummer — Codeshare-Folds dürfen den prior nicht verlieren)."""
    try:
        now = now or time.time()
        k = fs.get("keys") or {}
        prior = {"phase": fs.get("phase"), "conf": fs.get("phase_conf"),
                 "source": fs.get("phase_source"), "obs_ts": fs.get("obs_ts"),
                 "sticky_airborne": bool(fs.get("sticky_airborne"))}
        if len(_PRIOR_STORE) > _PRIOR_MAX:
            cutoff = now - _PRIOR_TTL_S
            for kk in [kk for kk, vv in _PRIOR_STORE.items() if vv[0] < cutoff]:
                _PRIOR_STORE.pop(kk, None)
            if len(_PRIOR_STORE) > _PRIOR_MAX:
                _PRIOR_STORE.clear()          # harte Kappung, memo ist best-effort
        for fl in {k.get("flight"), k.get("mkt_flight")}:
            if fl:
                _PRIOR_STORE[_prior_key(fl, k.get("date"))] = (now, prior)
    except Exception:
        pass                                  # Memo darf nie den Request brechen


# ---------------------------------------------------------------------------
# 7. TIME HELPERS
# ---------------------------------------------------------------------------

def _ts_to_iso(ts):
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _iso_to_ts(iso):
    """UTC ISO ('...Z') -> UTC epoch via calendar.timegm (treats struct as UTC).
    NOT time.mktime, which assumes local time and shifts by the local offset+DST."""
    if not iso:
        return None
    import calendar
    try:
        return float(calendar.timegm(time.strptime(str(iso)[:19], "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# 8. PER-SURFACE PROJECTIONS (pure dict-shuffles; no logic)
# ---------------------------------------------------------------------------

LEGACY_STATUS = {SCHEDULED: "SCHEDULED", BOARDING: "BOARDING", TAXI_OUT: "GROUNDED",
                 AIRBORNE: "AIRBORNE", APPROACH: "AIRBORNE", LANDED: "LANDED",
                 ARRIVED: "LANDED", CANCELLED: "CANCELLED", DIVERTED: "DIVERTED",
                 UNKNOWN: "UNKNOWN"}

FAMILY_PHASE = {SCHEDULED: None, BOARDING: "am_gate", TAXI_OUT: "rollt",
                AIRBORNE: "fliegt", APPROACH: "fliegt", LANDED: "gelandet",
                ARRIVED: "gelandet", CANCELLED: "annulliert", DIVERTED: "umgeleitet",
                UNKNOWN: None}


def project_my_flight_status(fs):
    d = fs["delay"]
    return {"reg": fs["reg"], "aircraft": fs["ac_type"],
            "status": LEGACY_STATUS[fs["phase"]], "phase": fs["phase"],
            "phase_conf": fs["phase_conf"], "cancelled": fs["cancelled"],
            "delay_known": d["known"], "delay_min": d["min"] if d["known"] else None,
            "delay_side": d["side"], "on_time": fs["on_time"],
            "est_dep_iso": fs["times"]["est_dep_iso"], "est_arr_iso": fs["times"]["est_arr_iso"]}


def project_flight_live(fs):
    d = fs["delay"]
    return {"ok": True, "reg": fs["reg"], "hex": fs["hex"], "callsign": fs["callsign"],
            "dep": fs["route"]["dep"], "dest": fs["route"]["dst"],
            "live": fs["live"], "in_flight": fs["in_flight"], "progress": fs["progress"],
            "live_status": fs.get("live_status"),
            "source": fs["route"]["source"], "phase": fs["phase"], "phase_conf": fs["phase_conf"],
            "sched_arr": fs["times"]["sched_arr_iso"], "est_arr": fs["times"]["est_arr_iso"],
            "eta_iso": fs["times"]["eta_iso"], "eta_conf": fs["times"]["eta_conf"],
            "arr_delay_min": d["arr_delay_min"] if d["known"] else None}


def project_crew_status(fs, allowed=("next_flight",)):
    d = fs["delay"]
    s = {"flying_now": fs["phase"] in (AIRBORNE, APPROACH, TAXI_OUT),
         "flight_phase": FAMILY_PHASE[fs["phase"]],
         "today_delay_min": d["min"] if d["known"] else None}
    if "next_flight" in allowed and fs["live"]:
        s |= {"live_lat": fs["live"]["lat"], "live_lon": fs["live"]["lon"],
              "live_track": fs["live"]["track"], "live_conf": fs["live"]["conf"]}
    return s


def project_friend_leg(fs):
    d = fs["delay"]
    live = fs["live"]
    return {"flight": fs["keys"]["flight"], "dep_iata": fs["keys"]["dep_iata"],
            "arr_iata": fs["keys"]["arr_iata"], "status": fs["phase"],
            # additiv (P1-4c): phase + live_status ('lost' ⇒ iOS KANN die
            # Großkreis-Simulation stoppen statt einen Geist weiterzufliegen)
            "phase": fs["phase"], "live_status": fs.get("live_status"),
            "phase_conf": fs["phase_conf"], "cancelled": fs["cancelled"],
            "dep_delay_min": d["dep_delay_min"], "arr_delay_min": d["arr_delay_min"],
            "delay_min": d["min"], "delay_side": d["side"], "delay_known": d["known"],
            "sched_dep_iso": fs["times"]["sched_dep_iso"], "est_dep_iso": fs["times"]["est_dep_iso"],
            "sched_arr_iso": fs["times"]["sched_arr_iso"], "est_arr_iso": fs["times"]["est_arr_iso"],
            "live": {k: live[k] for k in ("lat", "lon", "track", "gs", "alt", "on_ground")}
                    if live else None}
