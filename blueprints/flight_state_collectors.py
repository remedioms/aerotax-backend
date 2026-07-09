"""FlightState collectors — Layer 2 of the unified engine rollout.

The engine (blueprints/flight_state.py) is a PURE reducer over Observations.
These collectors are the ADAPTER layer: they map the outputs our endpoints
ALREADY fetch (the `_flight_obs_merged` board record, the `_aircraft_live_pos`
snapshot tuple, an ADS-B `_machine_live` result, the roster leg) into normalized
`Observation`s. They stay PURE (no I/O of their own) so they are fixture-testable
and so the endpoint keeps owning cost/budget/targeted decisions — a collector
never re-fetches, it just re-shapes what the caller already has.

Key encoding (the ghost-bug fix lives here): a departure board that reads
"Abgeflogen"/"Departed" means OFF-BLOCK, not airborne. It is emitted as
`phase_hard=TAXI_OUT (side=dep)`, never as proven airborne — so a taxiing plane
can never trigger AIRBORNE in the reducer.
"""
from __future__ import annotations

import re
import time
from typing import Optional

from blueprints.flight_state import (
    Observation, TAXI_OUT, AIRBORNE, LANDED, ARRIVED, BOARDING, CANCELLED,
)

# ── board status classification (side-aware, keyword-based, pure) ──────────
_LANDED_KW = ("landed", "gelandet", "arrived", "angekommen", "at gate", "am gate",
              "on block", "on-block", "aufgesetzt", "baggage", "gepäck")
_DEPARTED_KW = ("departed", "abgeflogen", "en route", "en-route", "im flug",
                "airborne", "gestartet", "in air")
_BOARDING_KW = ("boarding", "gate open", "final call", "letzter aufruf",
                "gate closed", "gate closing", "go to gate", "boarding complete")
_CANCELLED_KW = ("cancel", "annull", "gestrichen")
_ENROUTE_KW = ("en route", "en-route", "im flug", "airborne", "in air")   # truly flying


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def classify_board_status(status, side: str):
    """Map a board status string + side -> (phase_token, is_hard, proven_airborne).

    side='dep' | 'arr'. Returns (None, False, False) when the status carries no
    phase signal (e.g. 'scheduled', 'estimated 12:40'). This is the SINGLE place
    the 'Abgeflogen = off-block, not airborne' rule is encoded."""
    s = _norm(status)
    if not s:
        return None, False, False
    if any(k in s for k in _CANCELLED_KW):
        return CANCELLED, True, False
    if any(k in s for k in _LANDED_KW):
        # arr-side landed is authoritative; dep-side 'landed' is nonsensical -> ignore
        return (LANDED, True, False) if side == "arr" else (None, False, False)
    if any(k in s for k in _DEPARTED_KW):
        proven = any(k in s for k in _ENROUTE_KW)   # only true en-route counts as airborne
        if side == "dep":
            # off-block: TAXI_OUT unless the board explicitly says en-route/in-flight
            return (AIRBORNE if proven else TAXI_OUT), True, proven
        return None, False, False                    # 'departed' on the arr board = noise
    if any(k in s for k in _BOARDING_KW):
        return BOARDING, False, False
    return None, False, False


# ── collectors: fetched data -> Observations (pure) ────────────────────────

def obs_from_board_merged(m: dict, keys: dict, now: Optional[float] = None,
                          board_to_iso=None) -> list:
    """Map a `_flight_obs_merged` record into Observations.

    `board_to_iso(hhmm, iata)` converts a board-local time string to a UTC ISO-Z
    string (pass app._board_local_to_utc_iso); if None, raw strings are kept."""
    if not m:
        return []
    now = now or time.time()
    dep_iata = keys.get("dep_iata")
    arr_iata = keys.get("arr_iata")

    def iso(hhmm, iata):
        if board_to_iso and hhmm and iata:
            return board_to_iso(hhmm, iata)
        return hhmm

    out = []
    # -- phase (side-aware, hard/soft) --
    ph_dep, hard_dep, proven_dep = classify_board_status(m.get("status_dep"), "dep")
    ph_arr, hard_arr, _ = classify_board_status(m.get("status_arr"), "arr")
    if m.get("cancelled"):
        out.append(Observation("phase_hard", CANCELLED, "board", now,
                               meta={"side": "dep", "cancelled": True}))
    if ph_arr:
        out.append(Observation("phase_hard" if hard_arr else "phase_soft", ph_arr,
                               "board", now, meta={"side": "arr"}))
    if ph_dep:
        kind = "phase_hard" if hard_dep else "phase_soft"
        out.append(Observation(kind, ph_dep, "board", now,
                               meta={"side": "dep", "proven_airborne": proven_dep}))

    # -- times --
    dep_val = {}
    if m.get("sched_dep"):
        dep_val["sched"] = iso(m.get("sched_dep"), dep_iata)
    if m.get("esti_dep"):
        dep_val["est"] = iso(m.get("esti_dep"), dep_iata)
    if dep_val:
        out.append(Observation("dep_time", dep_val, "board", now))
    arr_val = {}
    if m.get("sched_arr"):
        arr_val["sched"] = iso(m.get("sched_arr"), arr_iata)
    if m.get("esti_arr"):
        arr_val["est"] = iso(m.get("esti_arr"), arr_iata)
    if arr_val:
        out.append(Observation("arr_time", arr_val, "board", now))
        out.append(Observation("eta", {"eta": arr_val.get("est") or arr_val.get("sched")},
                               "board", now))

    # -- delay (single source of truth; delay_known gated) --
    if m.get("delay_known"):
        out.append(Observation("delay", {
            "delay_known": True,
            "dep_delay_min": m.get("dep_delay_min"),
            "arr_delay_min": m.get("arr_delay_min"),
        }, "board", now))

    # -- reg / route --
    if m.get("reg"):
        out.append(Observation("reg", {"reg": m.get("reg"), "ac_type": m.get("aircraft")},
                               "board", now, meta={"flightno_matched": True}))
    if dep_iata and arr_iata:
        out.append(Observation("route", {"dep": dep_iata, "dst": arr_iata,
                                         "confidence": "confirmed"}, "board", now))
    return out


def obs_from_aircraft_live(pos: dict, route, reg_disp, ac_type,
                           now: Optional[float] = None) -> list:
    """Map an `_aircraft_live_pos` result into Observations.

    pos keys: lat/lon/track/gs/alt/on_ground/source/seen_ts. route=(src,dst)."""
    if not pos:
        return []
    now = now or time.time()
    seen_ts = _iso_or_epoch(pos.get("seen_ts")) or now
    out = [Observation("position", {
        "lat": pos.get("lat"), "lon": pos.get("lon"), "track": pos.get("track"),
        "gs_kt": pos.get("gs"), "alt_ft": pos.get("alt"),
        "on_ground_raw": pos.get("on_ground"), "position_source": 3,
    }, "aircraft_live", seen_ts)]
    if reg_disp:
        out.append(Observation("reg", {"reg": reg_disp, "ac_type": ac_type},
                               "aircraft_live", seen_ts, meta={"flightno_matched": True}))
    if route and route[0] and route[1]:
        out.append(Observation("route", {"dep": route[0], "dst": route[1],
                                         "confidence": "estimated"}, "aircraft_live", seen_ts))
    return out


def obs_from_adsb(live: dict, route=None, now: Optional[float] = None) -> list:
    """Map a `_machine_live` ADS-B result into Observations. `live` has
    lat/lon/track/gs/alt (+ optional position_source, ts)."""
    if not live:
        return []
    now = now or time.time()
    ts = _iso_or_epoch(live.get("ts") or live.get("obs_ts")) or now
    out = [Observation("position", {
        "lat": live.get("lat"), "lon": live.get("lon"), "track": live.get("track"),
        "gs_kt": live.get("gs") or live.get("gs_kt"),
        "alt_ft": live.get("alt") or live.get("alt_ft"),
        "on_ground_raw": live.get("on_ground"),
        "position_source": live.get("position_source", 0),
    }, "adsb", ts)]
    if route and route.get("src") and route.get("dst"):
        conf = "confirmed" if route.get("confidence") == "confirmed" else "estimated"
        out.append(Observation("route", {"dep": route["src"], "dst": route["dst"],
                                         "confidence": conf}, "adsb", ts))
    return out


def obs_from_pos(pos: dict, source: str, now: Optional[float] = None,
                 position_source: Optional[int] = None) -> list:
    """Map a generic position dict (lat/lon/track/gs/alt/on_ground/seen_ts, as
    returned by _machine_live or _aircraft_live_pos) into a position Observation.
    `source` is the engine source tag ('adsb' | 'aircraft_live' | 'fr24_bulk').
    position_source defaults to 0 for adsb (real ADS-B) else 3."""
    if not pos:
        return []
    now = now or time.time()
    ts = _iso_or_epoch(pos.get("seen_ts") or pos.get("obs_ts")) or now
    psrc = position_source if position_source is not None else (0 if source == "adsb" else 3)
    return [Observation("position", {
        "lat": pos.get("lat"), "lon": pos.get("lon"), "track": pos.get("track"),
        "gs_kt": pos.get("gs") if pos.get("gs") is not None else pos.get("gs_kt"),
        "alt_ft": pos.get("alt") if pos.get("alt") is not None else pos.get("alt_ft"),
        "on_ground_raw": pos.get("on_ground"), "position_source": psrc,
    }, source, ts)]


def obs_absent(source: str, kind: str = "position") -> Observation:
    """The collector ran and there is genuinely nothing (e.g. ADS-B over Siberia).
    A legitimate miss — lets precedence fall through, unlike `unavailable`."""
    return Observation(kind, None, source, None, status="absent")


def obs_unavailable(source: str, kind: str = "position") -> Observation:
    """The collector errored/timed out. NOT evidence the plane is on the ground —
    the reducer holds the prior phase rather than downgrading."""
    return Observation(kind, None, source, None, status="unavailable")


# ── keys builder ───────────────────────────────────────────────────────────

def build_keys(flight, date, dep_iata, arr_iata, *, roster_tail=None,
               sched_dep_iso=None, sched_arr_iso=None, callsign=None,
               dep_ll=None, arr_ll=None, leg_index=0,
               sched_dep_ts=None, sched_flight_min=None) -> dict:
    """Assemble the immutable `keys` the reducer needs. dep_ll/arr_ll are
    (lat,lon) tuples for great-circle math (pass from _iata_latlon)."""
    return {
        "flight": (flight or "").replace(" ", "").upper() or None,
        "date": date, "dep_iata": dep_iata, "arr_iata": arr_iata,
        "roster_tail": roster_tail, "callsign": callsign,
        "sched_dep": sched_dep_iso, "sched_arr": sched_arr_iso,
        "dep_ll": dep_ll, "arr_ll": arr_ll, "leg_index": leg_index,
        "sched_dep_ts": sched_dep_ts, "sched_flight_min": sched_flight_min,
    }


def _iso_or_epoch(v):
    """Parse a UTC ISO string ('...Z') or epoch to a UTC epoch. Uses calendar.timegm
    (treats the parsed struct as UTC) — NOT time.mktime, which assumes local time and
    would shift a UTC seen_ts by the local offset+DST, wrongly aging fresh fixes."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    import calendar
    try:
        return float(calendar.timegm(time.strptime(str(v)[:19], "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, TypeError):
        return None
