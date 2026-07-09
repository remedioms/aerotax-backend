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

# ── board status classification (side-aware, TOKEN-based, pure) ────────────
# Wort-Tokenisierung statt Substring-Match (Vorbild warehouse_reader.
# _tokenize_status): "departure delayed" darf NICHT über das Teilwort "dep"
# als TAXI_OUT feuern, "arrival expected" NICHT über "arrival" als LANDED.
# Mehrwort-Signale stehen als Phrasen (konsekutive Token-Folgen).
_CANCELLED_TOK = frozenset(("cancelled", "canceled", "cancel", "annulliert",
                            "gestrichen", "storniert"))
_ENROUTE_TOK = frozenset(("airborne",))                     # truly flying
_ENROUTE_PH = (("en", "route"), ("im", "flug"), ("in", "air"), ("in", "flight"))
_LANDED_TOK = frozenset(("landed", "gelandet", "arrived", "angekommen",
                         "aufgesetzt", "baggage", "gepaeck", "deboard",
                         "deboarding", "ausstieg"))
_LANDED_PH = (("at", "gate"), ("am", "gate"), ("on", "block"), ("on", "blocks"))
# taxi/off-block tokens — side decides meaning (dep = TAXI_OUT, arr = LANDED/taxiing in).
_TAXI_TOK = frozenset(("taxi", "taxiing", "rollt", "rolling", "pushback"))
_TAXI_PH = (("off", "block"), ("off", "blocks"), ("push", "back"))
# 'dep' bleibt als EXAKTES Token (Board-Kürzel "DEP") — via Tokenisierung
# matcht es 'departure …' nicht mehr.
_DEPARTED_TOK = frozenset(("departed", "abgeflogen", "gestartet", "started",
                           "lifted", "dep"))
_BOARDING_TOK = frozenset(("boarding", "einsteigen"))
_BOARDING_PH = (("gate", "open"), ("gate", "closed"), ("gate", "closing"),
                ("final", "call"), ("letzter", "aufruf"), ("last", "call"),
                ("go", "to", "gate"))


def _tokenize(s) -> list:
    """Board-Status-String → kleingeschriebene Wort-Tokens (Umlaute entfaltet,
    Nicht-Alphanumerik trennt, reine Zahlen-Tokens wie Zeit-Suffixe fallen weg)."""
    s = str(s or "").strip().lower()
    if not s:
        return []
    s = (s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
         .replace("ß", "ss"))
    return [t for t in re.split(r"[^a-z0-9]+", s) if t and not t.isdigit()]


def _has_phrase(toks, phrases) -> bool:
    for ph in phrases:
        n = len(ph)
        if any(tuple(toks[i:i + n]) == ph for i in range(len(toks) - n + 1)):
            return True
    return False


def classify_board_status(status, side: str):
    """Map a board status string + side -> (phase_token, is_hard, proven_airborne).

    side='dep' | 'arr'. Returns (None, False, False) when the status carries no
    phase signal (e.g. 'scheduled', 'estimated 12:40', 'departure delayed',
    'arrival expected'). This is the SINGLE place the 'Abgeflogen = off-block,
    not airborne' rule is encoded. Verified against a live board-status sample
    (Abgeflogen/gestartet/DEP/taxiing/End of Boarding/Gate Closed/Airborne/
    Gelandet/baggage delivery finished/Expected HH:MM …)."""
    toks = _tokenize(status)
    if not toks:
        return None, False, False
    ts = set(toks)
    if ts & _CANCELLED_TOK:
        return CANCELLED, True, False
    # explicit en-route/airborne wins on either side (board KNOWS it's flying)
    if ts & _ENROUTE_TOK or _has_phrase(toks, _ENROUTE_PH):
        return AIRBORNE, True, True
    if ts & _LANDED_TOK or _has_phrase(toks, _LANDED_PH):
        # arr-side landed is authoritative; dep-side 'landed' is nonsensical -> ignore
        return (LANDED, True, False) if side == "arr" else (None, False, False)
    # taxi / off-block: dep side -> TAXI_OUT, arr side -> taxiing in after landing.
    if ts & _TAXI_TOK or _has_phrase(toks, _TAXI_PH):
        return (TAXI_OUT, True, False) if side == "dep" else (LANDED, True, False)
    if ts & _DEPARTED_TOK:
        if side == "dep":
            return TAXI_OUT, True, False            # off-block, not proven airborne
        return None, False, False                    # 'departed' on the arr board = noise
    if ts & _BOARDING_TOK or _has_phrase(toks, _BOARDING_PH):
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
    # Beobachtungszeit: trägt der Merged-Record einen echten Zeitstempel
    # (obs_ts/updated_at), stempeln wir DEN. Sonst now — bewusst: der Record
    # kommt aus dem In-Process-Merge-Cache (TTL 90 s) und nur vom HEUTIGEN
    # Betriebstag, 'now' ist also höchstens Cache-TTL daneben (bounded), es
    # gibt schlicht keinen per-Row-Zeitstempel im Merged-Shape.
    obs_ts = _iso_or_epoch(m.get("obs_ts") or m.get("updated_at"))
    if obs_ts is None:
        obs_ts = now
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
        out.append(Observation("phase_hard", CANCELLED, "board", obs_ts,
                               meta={"side": "dep", "cancelled": True}))
    if ph_arr:
        out.append(Observation("phase_hard" if hard_arr else "phase_soft", ph_arr,
                               "board", obs_ts, meta={"side": "arr"}))
    if ph_dep:
        kind = "phase_hard" if hard_dep else "phase_soft"
        out.append(Observation(kind, ph_dep, "board", obs_ts,
                               meta={"side": "dep", "proven_airborne": proven_dep}))

    # -- times --
    dep_val = {}
    if m.get("sched_dep"):
        dep_val["sched"] = iso(m.get("sched_dep"), dep_iata)
    if m.get("esti_dep"):
        dep_val["est"] = iso(m.get("esti_dep"), dep_iata)
    if dep_val:
        out.append(Observation("dep_time", dep_val, "board", obs_ts))
    arr_val = {}
    if m.get("sched_arr"):
        arr_val["sched"] = iso(m.get("sched_arr"), arr_iata)
    if m.get("esti_arr"):
        arr_val["est"] = iso(m.get("esti_arr"), arr_iata)
    if arr_val:
        out.append(Observation("arr_time", arr_val, "board", obs_ts))
        out.append(Observation("eta", {"eta": arr_val.get("est") or arr_val.get("sched")},
                               "board", obs_ts))

    # -- delay (single source of truth; delay_known gated) --
    if m.get("delay_known"):
        out.append(Observation("delay", {
            "delay_known": True,
            "dep_delay_min": m.get("dep_delay_min"),
            "arr_delay_min": m.get("arr_delay_min"),
        }, "board", obs_ts))

    # -- reg / route --
    if m.get("reg"):
        out.append(Observation("reg", {"reg": m.get("reg"), "ac_type": m.get("aircraft")},
                               "board", obs_ts, meta={"flightno_matched": True}))
    if dep_iata and arr_iata:
        out.append(Observation("route", {"dep": dep_iata, "dst": arr_iata,
                                         "confidence": "confirmed"}, "board", obs_ts))
    return out


def obs_from_aircraft_live(pos: dict, route, reg_disp, ac_type,
                           now: Optional[float] = None) -> list:
    """Map an `_aircraft_live_pos` result into Observations.

    pos keys: lat/lon/track/gs/alt/on_ground/source/seen_ts. route=(src,dst)."""
    if not pos:
        return []
    now = now or time.time()
    seen_ts = _obs_ts_or_none(pos.get("seen_ts"), now)
    out = []
    # obs_ts-Laundering-Schutz (P1-4b): unparsebarer seen_ts ⇒ KEIN Positions-
    # Kandidat (verworfen), statt einer fälschlich „frischen" now-Position.
    if seen_ts is not None:
        out.append(Observation("position", {
            "lat": pos.get("lat"), "lon": pos.get("lon"), "track": pos.get("track"),
            "gs_kt": pos.get("gs"), "alt_ft": pos.get("alt"),
            "on_ground_raw": pos.get("on_ground"), "position_source": 3,
        }, "aircraft_live", seen_ts))
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
    ts = _obs_ts_or_none(live.get("ts") or live.get("obs_ts"), now)
    if ts is None:
        # unparsebarer Zeitstempel ⇒ Fix verwerfen (kein now-Laundering)
        return []
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


# Provenienz-Tags der Positions-Resolver (position_for_flight/_live_fix_for_reg)
# → Engine-Source-Alphabet (MAX_AGE-Schlüssel). Unbekannte Tags laufen 1:1 durch
# (schon Engine-Tags wie 'adsb'/'aircraft_live'), leer ⇒ konservativ 'adsb'.
_ENGINE_SOURCE = {
    "fr24": "fr24_bulk", "fr24_live": "fr24_bulk", "fr24_grpc": "fr24_bulk",
    "aircraft_positions": "adsb", "adsb.lol": "adsb",
    "adb": "paid_adb", "aerodatabox": "paid_adb",
}


def engine_source(tag) -> str:
    t = str(tag or "").strip().lower()
    return _ENGINE_SOURCE.get(t, t or "adsb")


def obs_from_pos(pos: dict, source: str, now: Optional[float] = None,
                 position_source: Optional[int] = None) -> list:
    """Map a generic position dict (lat/lon/track/gs/alt/on_ground/seen_ts, as
    returned by _machine_live or _aircraft_live_pos) into a position Observation.
    `source` is the engine source tag ('adsb' | 'aircraft_live' | 'fr24_bulk')
    ODER ein Resolver-Tag ('fr24'/'aircraft_positions'/'adsb.lol' — wird über
    engine_source() gemappt). position_source defaults to 0 for adsb else 3."""
    if not pos:
        return []
    now = now or time.time()
    ts = _obs_ts_or_none(pos.get("seen_ts") or pos.get("obs_ts"), now)
    if ts is None:
        # unparsebarer Zeitstempel ⇒ kein Kandidat (kein now-Laundering)
        return []
    source = engine_source(source)
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
               sched_dep_ts=None, sched_flight_min=None,
               dep_elev_ft=None) -> dict:
    """Assemble the immutable `keys` the reducer needs. dep_ll/arr_ll are
    (lat,lon) tuples for great-circle math (pass from _iata_latlon);
    dep_elev_ft = Airport-Elevation (ft MSL, aus der Referenz-DB) fürs
    Hochland-alt-Gate (MEX/NBO/ADD)."""
    # sched_dep_ts aus der vorhandenen ISO ableiten (P1-4d) — damit lebt T6
    # SCHEDULED vor Abflug in ALLEN Flips, ohne dass jeder Aufrufer selbst
    # epoch-rechnen muss. Ein explizit übergebener Wert gewinnt.
    if sched_dep_ts is None and sched_dep_iso:
        sched_dep_ts = _iso_or_epoch(sched_dep_iso)
    return {
        "flight": (flight or "").replace(" ", "").upper() or None,
        "date": date, "dep_iata": dep_iata, "arr_iata": arr_iata,
        "roster_tail": roster_tail, "callsign": callsign,
        "sched_dep": sched_dep_iso, "sched_arr": sched_arr_iso,
        "dep_ll": dep_ll, "arr_ll": arr_ll, "leg_index": leg_index,
        "sched_dep_ts": sched_dep_ts, "sched_flight_min": sched_flight_min,
        "dep_elev_ft": dep_elev_ft,
    }


def _iso_or_epoch(v):
    """Parse einen Beobachtungs-Zeitstempel → UTC-Epoch oder None (Parse-Fehler).

    Akzeptiert: Epoch (int/float/numerischer String, ms-Epochen werden erkannt),
    ISO mit 'T' ODER Space-Separator, mit 'Z'/Offset ODER naiv (= als UTC gelesen
    — NIE time.mktime, das würde einen UTC-seen_ts um Lokal-Offset+DST altern)."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        ts = float(v)
        # ms-Epoch heuristisch (als Sekunden gelesen wäre das Jahr > 2100)
        return ts / 1000.0 if ts > 4102444800 else ts
    s = str(v).strip()
    if not s:
        return None
    try:                                   # numerischer String = Epoch
        ts = float(s)
    except ValueError:
        pass
    else:
        # ms-Epoch heuristisch (Jahr > 2100 in Sekunden gelesen) → Sekunden
        return ts / 1000.0 if ts > 4102444800 else ts
    from datetime import datetime, timezone
    iso = s.replace(" ", "T", 1) if (" " in s and "T" not in s) else s
    if iso.endswith("Z") or iso.endswith("z"):
        iso = iso[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        # Fallback (altes Verhalten): erste 19 Zeichen als UTC lesen — fängt
        # exotische Fraktions-Suffixe (z.B. 9 Nachkomma-Stellen), die ältere
        # fromisoformat-Versionen ablehnen. Erst wenn AUCH das scheitert, ist
        # der Stempel wirklich unparsebar → None (Observation verwerfen).
        import calendar
        try:
            return float(calendar.timegm(
                time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")))
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _obs_ts_or_none(v, now):
    """Beobachtungs-ts für einen Positions-Fix: None (Quelle liefert schlicht
    keinen ts, z.B. frisch geholter Live-Read) → now; VORHANDENER aber
    unparsebarer ts → None = Observation verwerfen (nie „jetzt" laundern)."""
    if v is None:
        return now
    return _iso_or_epoch(v)
