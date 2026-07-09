"""FlightState shadow-mode — Layer 3 step 1 (observe before you flip).

When env `FLIGHTSTATE_SHADOW=1`, endpoints compute the unified engine result
ALONGSIDE their legacy output and log any disagreement — with ZERO change to the
response the user gets. After ~48 h of real traffic the logs tell us exactly
where the engine and the legacy heuristics differ (and which is right) before we
flip any endpoint to projections. Everything here is best-effort and never throws
into the request path.
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("flightstate.shadow")

_PHASE_TO_LEGACY_INAIR = {"AIRBORNE", "APPROACH"}


def shadow_enabled() -> bool:
    return os.environ.get("FLIGHTSTATE_SHADOW", "") in ("1", "true", "yes")


def _legacy_in_air(legacy: dict) -> bool:
    """Best-effort 'is the legacy output showing this as flying / live'."""
    if legacy.get("in_flight") is not None:
        return bool(legacy["in_flight"])
    if legacy.get("live") is not None:
        return True
    st = str(legacy.get("status") or "").lower()
    return any(k in st for k in ("airborne", "en route", "en-route", "im flug"))


def shadow_record(endpoint: str, keys: dict, observations: list, legacy: dict,
                  fs: dict = None) -> None:
    """Compute the engine state from the same observations the endpoint used and
    log a structured disagreement vs the legacy output. Never raises.

    fs: bereits berechnetes Engine-Resultat (aktiver Flip) — wird wiederver-
    wendet statt doppelt zu resolven (Shadow+Flip = EIN resolve)."""
    if not shadow_enabled():
        return
    try:
        if fs is None:
            from blueprints.flight_state import resolve_flight_state
            fs = resolve_flight_state(keys, observations)

        eng_in_air = fs["phase"] in _PHASE_TO_LEGACY_INAIR
        eng_live = fs["live"] is not None
        leg_in_air = _legacy_in_air(legacy)
        leg_live = legacy.get("live") is not None

        diffs = {}
        # The ghost signature: legacy shows a live/flying plane the engine says is on the ground.
        if eng_live != leg_live:
            diffs["live_present"] = {"engine": eng_live, "legacy": leg_live}
        if eng_in_air != leg_in_air:
            diffs["in_air"] = {"engine": eng_in_air, "legacy": leg_in_air}
        # ETA honesty: engine refusing to extrapolate where legacy invented a time.
        eng_eta_conf = (fs.get("times") or {}).get("eta_conf")
        if legacy.get("eta_iso") and not fs.get("times", {}).get("eta_iso") \
                and fs["phase"] in ("TAXI_OUT", "BOARDING", "SCHEDULED"):
            diffs["eta_ghost"] = {"legacy_eta": legacy.get("eta_iso"),
                                  "engine_phase": fs["phase"]}

        if diffs:
            log.warning("flightstate_shadow_diff %s", json.dumps({
                "endpoint": endpoint,
                "flight": keys.get("flight"), "date": keys.get("date"),
                "dep": keys.get("dep_iata"), "arr": keys.get("arr_iata"),
                "engine_phase": fs["phase"], "engine_phase_conf": fs["phase_conf"],
                "engine_eta_conf": eng_eta_conf,
                "diffs": diffs,
                "sources": fs.get("sources"), "unavailable": fs.get("unavailable"),
            }, default=str))
    except Exception as e:                     # never break the request path
        log.debug("shadow_record failed: %s", e)
