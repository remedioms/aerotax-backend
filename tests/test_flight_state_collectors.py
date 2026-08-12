"""Tests for the collector adapter layer (blueprints/flight_state_collectors.py).

Proves that the REAL data shapes our endpoints already fetch (`_flight_obs_merged`
record, `_aircraft_live_pos` tuple) map into Observations that the pure engine
then resolves correctly — i.e. the collector+engine pipeline fixes the ghost bug
end-to-end, not just the hand-built fixtures in test_flight_state.py.
"""
from blueprints.flight_state import (
    resolve_flight_state, TAXI_OUT, AIRBORNE, APPROACH, LANDED, BOARDING, CANCELLED, SIMULATED,
)
from blueprints.flight_state_collectors import (
    classify_board_status, obs_from_board_merged, obs_from_aircraft_live,
    obs_from_adsb, obs_from_pos, build_keys, engine_source, _iso_or_epoch,
)

NOW = 1_783_584_000
FRA = (50.03, 8.57)
GVA = (46.24, 6.11)


def _t_iso(ts):
    """Epoch → UTC-ISO ('…Z'), wie die Quellen sie liefern."""
    import time as _t
    return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(ts))


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


def test_classify_tokenized_no_substring_false_positives():
    """P2-Fix: Wort-Tokenisierung statt Substring-Match. 'departure delayed'
    feuerte über das Teilwort 'dep' als TAXI_OUT, 'arrival expected' über
    'arrival' als LANDED — beides sind KEINE Phasen-Signale."""
    assert classify_board_status("Departure delayed", "dep") == (None, False, False)
    assert classify_board_status("Arrival expected", "arr") == (None, False, False)
    assert classify_board_status("Expected 13:05", "arr") == (None, False, False)
    # Das exakte Board-Kürzel 'DEP' bleibt off-block (echtes Token)
    assert classify_board_status("DEP 12:41", "dep") == (TAXI_OUT, True, False)
    # Phrasen/Umlaute weiter erkannt
    assert classify_board_status("Pushback", "dep") == (TAXI_OUT, True, False)
    assert classify_board_status("off-block", "dep") == (TAXI_OUT, True, False)
    assert classify_board_status("Gate closed", "dep") == (BOARDING, False, False)
    assert classify_board_status("Geschlossen", "dep") == (BOARDING, False, False)
    assert classify_board_status("Gate zu", "dep") == (BOARDING, False, False)
    assert classify_board_status("baggage delivery finished", "arr") == (LANDED, True, False)
    assert classify_board_status("im Flug", "dep") == (AIRBORNE, True, True)
    assert classify_board_status("Final approach", "arr") == (APPROACH, True, True)
    assert classify_board_status("Im Anflug", "arr") == (APPROACH, True, True)


def test_arrival_final_approach_beats_departure_taxi_out():
    keys = build_keys("LH1126", None, "FRA", "BCN")
    obs = obs_from_board_merged({
        "status_dep": "Abgeflogen",
        "status_arr": "Final approach",
    }, keys, now=NOW)
    fs = resolve_flight_state(keys, obs, now=NOW)
    assert fs["phase"] == APPROACH


def test_stale_final_approach_converges_to_estimated_landed():
    """A stale arrival-board phase must not outlive the canonical crew state."""
    keys = build_keys(
        "LH1126", "2026-07-15", "FRA", "BCN",
        sched_dep_iso="2026-07-15T07:50:00Z",
        sched_arr_iso="2026-07-15T09:55:00Z",
    )
    merged = {
        "status_dep": "Abgeflogen",
        "status_arr": "Final approach",
        "esti_arr": "2026-07-15T10:08:00Z",
        "delay_known": True,
        "arr_delay_min": 13,
    }
    expected_arrival = 1_784_110_080  # 2026-07-15T10:08:00Z
    obs = obs_from_board_merged(merged, keys, now=expected_arrival + 41 * 60)
    fs = resolve_flight_state(keys, obs, now=expected_arrival + 41 * 60)
    assert fs["phase"] == LANDED
    assert fs["phase_conf"] == "estimated"
    assert fs["live"] is None


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


def test_utc_seen_ts_not_wrongly_aged():
    """Regression: a UTC seen_ts ('...Z') 60s old must be treated as FRESH, not
    shifted by the local tz+DST and dropped as stale (the bug the 115-flight FR24
    validation caught — it was un-flying real cruising planes)."""
    import time as _t
    seen_iso = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(NOW - 60))
    keys = build_keys("LH9", "2026-07-09", "FRA", "GVA", dep_ll=FRA, arr_ll=GVA)
    pos = {"lat": 47.9, "lon": 7.4, "track": 205, "gs": 431, "alt": 34000,
           "on_ground": False, "source": "aircraft_live", "seen_ts": seen_iso}
    obs = obs_from_aircraft_live(pos, ("FRA", "GVA"), "D-X", "A320", now=NOW)
    fs = resolve_flight_state(keys, obs, now=NOW)
    assert fs["phase"] == AIRBORNE       # fresh fix, not dropped
    assert fs["live"] is not None


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


# ── obs_ts-Laundering (P1-4b): unparsebare Zeitstempel VERWERFEN ────────────

def test_unparseable_seen_ts_discards_position_candidate():
    """Ein VORHANDENER aber unparsebarer seen_ts darf die Position nicht als
    'jetzt' laundern — die Observation wird verworfen (kein Kandidat), die
    Engine rendert dann ehrlich nichts statt eines frischen Geists."""
    pos = {"lat": 61.7, "lon": 90.2, "track": 61, "gs": 476, "alt": 37000,
           "on_ground": False, "source": "aircraft_live", "seen_ts": "kaputt!!"}
    obs = obs_from_aircraft_live(pos, ("FRA", "HND"), "D-AIXA", "A359", now=NOW)
    assert not [o for o in obs if o.kind == "position"]
    assert obs_from_pos({**pos, "seen_ts": "not-a-ts"}, "aircraft_live", now=NOW) == []
    assert obs_from_adsb({"lat": 47.9, "lon": 7.4, "gs": 431, "alt": 34000,
                          "ts": "garbage"}, now=NOW) == []
    # Engine-Ende-zu-Ende: kein Kandidat -> kein live
    keys = build_keys("LH716", "2026-07-09", "FRA", "HND", dep_ll=FRA,
                      arr_ll=(35.55, 139.78), sched_dep_ts=NOW - 3600)
    fs = resolve_flight_state(keys, obs, now=NOW)
    assert fs["live"] is None


def test_missing_seen_ts_still_counts_as_fresh_fetch():
    """KEIN ts geliefert (frisch geholter Live-Read) ist kein Parse-Fehler —
    die Position bleibt Kandidat mit obs_ts=now."""
    obs = obs_from_pos({"lat": 47.9, "lon": 7.4, "gs": 431, "alt": 34000}, "adsb", now=NOW)
    assert len(obs) == 1 and obs[0].obs_ts == NOW


def test_iso_or_epoch_variants():
    import time as _t
    base = _t.strftime("%Y-%m-%dT%H:%M:%S", _t.gmtime(NOW))
    plus2 = _t.strftime("%Y-%m-%dT%H:%M:%S", _t.gmtime(NOW + 7200)) + "+02:00"
    assert _iso_or_epoch(NOW) == float(NOW)
    assert _iso_or_epoch(str(NOW)) == float(NOW)                # Epoch-String
    assert _iso_or_epoch(NOW * 1000) == float(NOW)              # ms-Epoch
    assert _iso_or_epoch(base + "Z") == float(NOW)
    assert _iso_or_epoch(base.replace("T", " ")) == float(NOW)  # Space-Separator
    assert _iso_or_epoch(plus2) == float(NOW)                   # Offset-ISO
    assert _iso_or_epoch(base) == float(NOW)                    # naiv = UTC
    assert _iso_or_epoch("nonsense") is None
    assert _iso_or_epoch("") is None
    assert _iso_or_epoch(None) is None


def test_board_obs_stamped_with_record_updated_at():
    """Trägt der Merged-Record einen echten Zeitstempel, stempeln die Board-
    Observations DEN (nicht now)."""
    import time as _t
    upd = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(NOW - 300))
    keys = build_keys("LHX", "2026-07-09", "FRA", "GVA", dep_ll=FRA, arr_ll=GVA)
    obs = obs_from_board_merged(
        {"status_arr": "Gelandet", "delay_known": True, "arr_delay_min": 2,
         "updated_at": upd}, keys, now=NOW)
    assert obs and all(o.obs_ts == float(NOW - 300) for o in obs)


# ── Resolver-Provenienz → Engine-Source-Alphabet ────────────────────────────

def test_engine_source_mapping():
    assert engine_source("fr24") == "fr24_bulk"
    assert engine_source("aircraft_positions") == "adsb"
    assert engine_source("adsb.lol") == "adsb"
    assert engine_source("adb") == "paid_adb"
    assert engine_source("aircraft_live") == "aircraft_live"    # 1:1 durch
    assert engine_source(None) == "adsb"                        # konservativ
    obs = obs_from_pos({"lat": 47.9, "lon": 7.4, "gs": 431, "alt": 34000,
                        "seen_ts": NOW - 30}, "fr24", now=NOW)
    assert obs[0].source == "fr24_bulk"
    # 'aircraft_positions' = echter ADS-B-Poller → position_source 0 (real)
    obs2 = obs_from_pos({"lat": 47.9, "lon": 7.4, "gs": 431, "alt": 34000,
                         "seen_ts": NOW - 30}, "aircraft_positions", now=NOW)
    assert obs2[0].source == "adsb" and obs2[0].value["position_source"] == 0


# ── build_keys: sched_dep_ts aus sched_dep_iso (P1-4d) ─────────────────────

def test_build_keys_derives_sched_dep_ts():
    import time as _t
    dep_iso = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(NOW + 600))
    k = build_keys("LH2557", "2026-07-09", "FRA", "GVA", sched_dep_iso=dep_iso)
    assert k["sched_dep_ts"] == float(NOW + 600)
    # T6 SCHEDULED lebt damit vor Abflug (statt UNKNOWN)
    fs = resolve_flight_state(k, [], now=NOW)
    assert fs["phase"] == "SCHEDULED"
    # expliziter Wert gewinnt
    k2 = build_keys("LH2557", "2026-07-09", "FRA", "GVA",
                    sched_dep_iso=dep_iso, sched_dep_ts=123.0)
    assert k2["sched_dep_ts"] == 123.0


# ── ECHTE IST-ZEITEN → eta_conf=observed (2026-08-13) ──────────────────────
# Vorher konnte `_resolve_eta` seinen ersten Rang (arr_time.actual ⇒ OBSERVED)
# strukturell NIE erreichen: obs_from_board_merged ist der einzige Erzeuger von
# `arr_time` und füllte nur sched/est. flight-live meldete damit nie eine
# gemessene Ankunft, flight-recap.actual_arr war in Wahrheit die est-Zahl.

def test_actual_arrival_macht_eta_observed():
    """Quelle liefert eine ECHTE Ist-Ankunft (LH ActualTimeUTC) ⇒ observed."""
    keys = build_keys("LH400", "2026-07-09", "FRA", "JFK",
                      sched_arr_iso="2026-07-09T17:35:00Z")
    landed = _t_iso(NOW - 600)
    obs = obs_from_board_merged({
        "status_arr": "Gelandet", "sched_arr": "2026-07-09T17:35:00Z",
        "esti_arr": "2026-07-09T17:03:00Z", "actual_arr_iso": landed,
    }, keys, now=NOW)
    arr = [o for o in obs if o.kind == "arr_time"][0]
    assert arr.value["actual"] == landed
    fs = resolve_flight_state(keys, obs, now=NOW)
    assert fs["times"]["eta_conf"] == "observed"
    assert fs["times"]["eta_iso"] == landed
    assert fs["times"]["act_arr_iso"] == landed
    assert fs["times"]["arr_conf"] == "observed"


def test_nur_estimated_bleibt_estimated():
    """Dieselbe Zeile OHNE Ist-Wert: unverändert `estimated` (kein Etikett)."""
    keys = build_keys("LH400", "2026-07-09", "FRA", "JFK",
                      sched_arr_iso="2026-07-09T17:35:00Z")
    obs = obs_from_board_merged({
        "status_arr": "Gelandet", "sched_arr": "2026-07-09T17:35:00Z",
        "esti_arr": "2026-07-09T17:03:00Z",
    }, keys, now=NOW)
    assert "actual" not in [o for o in obs if o.kind == "arr_time"][0].value
    fs = resolve_flight_state(keys, obs, now=NOW)
    assert fs["times"]["eta_conf"] == "estimated"
    assert fs["times"]["act_arr_iso"] is None
    assert fs["times"]["arr_conf"] == "estimated"


def test_actual_departure_setzt_dep_conf_observed():
    keys = build_keys("LH400", "2026-07-09", "FRA", "JFK")
    off_block = _t_iso(NOW - 3600)
    obs = obs_from_board_merged({
        "status_dep": "Abgeflogen", "sched_dep": "2026-07-09T08:55:00Z",
        "actual_dep_iso": off_block,
    }, keys, now=NOW)
    fs = resolve_flight_state(keys, obs, now=NOW)
    assert fs["times"]["act_dep_iso"] == off_block
    assert fs["times"]["dep_conf"] == "observed"


def test_ist_zeit_in_der_zukunft_ist_keine_messung():
    """Eine Landung kann nicht gemessen worden sein, bevor sie stattfand —
    der Wert wird verworfen, nicht als `observed` durchgereicht."""
    keys = build_keys("LH400", "2026-07-09", "FRA", "JFK",
                      sched_arr_iso="2026-07-09T17:35:00Z")
    obs = obs_from_board_merged({
        "sched_arr": "2026-07-09T17:35:00Z",
        "actual_arr_iso": _t_iso(NOW + 3600),
    }, keys, now=NOW)
    assert "actual" not in [o for o in obs if o.kind == "arr_time"][0].value
    fs = resolve_flight_state(keys, obs, now=NOW)
    assert fs["times"]["eta_conf"] != "observed"
    # Uhren-Versatz innerhalb der Toleranz bleibt dagegen eine Messung
    ok = _t_iso(NOW + 60)
    obs2 = obs_from_board_merged({"actual_arr_iso": ok}, keys, now=NOW)
    assert [o for o in obs2 if o.kind == "arr_time"][0].value["actual"] == ok


def test_unparsebare_ist_zeit_wird_verworfen():
    """Fail-closed: was wir nicht in die Zeit legen können, belegt nichts."""
    keys = build_keys("LH400", "2026-07-09", "FRA", "JFK")
    obs = obs_from_board_merged({
        "esti_arr": "2026-07-09T17:03:00Z", "actual_arr_iso": "kaputt!!",
    }, keys, now=NOW)
    assert "actual" not in [o for o in obs if o.kind == "arr_time"][0].value


def test_station_lokale_ist_zeit_laeuft_durch_board_to_iso():
    """Board-Form (bare Stationszeit) wird wie sched/est konvertiert."""
    keys = build_keys("LH400", "2026-07-09", "FRA", "JFK")
    landed = _t_iso(NOW - 300)
    obs = obs_from_board_merged(
        {"actual_arr": "13:03"}, keys, now=NOW,
        board_to_iso=lambda hhmm, iata: landed if hhmm == "13:03" else None)
    assert [o for o in obs if o.kind == "arr_time"][0].value["actual"] == landed


def test_actual_allein_erzeugt_die_arr_time_observation():
    """Ohne sched/est darf die Messung nicht durchs Raster fallen."""
    keys = build_keys("LH400", "2026-07-09", "FRA", "JFK")
    landed = _t_iso(NOW - 120)
    obs = obs_from_board_merged({"actual_arr_iso": landed}, keys, now=NOW)
    arr = [o for o in obs if o.kind == "arr_time"]
    assert len(arr) == 1 and arr[0].value == {"actual": landed}


def test_gepaeckausgabe_kompositum_ist_landung():
    # Sebastian LH1139 (2026-07-16): 'Gepäckausgabe beendet' tokenisiert zu
    # 'gepaeckausgabe' — das exakte Token 'gepaeck' matchte nie, die Engine
    # blieb auf TAXI_OUT und der Kalender-Sektor stand als „Erwartet".
    from blueprints.flight_state import LANDED
    assert classify_board_status("Gepäckausgabe beendet", "arr") == (LANDED, True, False)
    assert classify_board_status("Gepäckausgabe", "arr") == (LANDED, True, False)
    # dep-seitig bleibt ein Landed-Wort bedeutungslos (kein Rueckwaerts-Signal).
    assert classify_board_status("Gepäckausgabe beendet", "dep") == (None, False, False)
