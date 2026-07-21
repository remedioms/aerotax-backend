"""LH Open API Flight-Facts-Enrichment (Engine A). Rein offline — kein Netz,
kein Key nötig: die Parser/Merge-Logik ist pur, HTTP wird gemockt. Fixture-
Responses sind exakt die verifizierte echte API-Shape (2026-07-21)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blueprints import lh_open_api as lh


# ── verifizierte echte FlightStatus-Response (Discover 4Y136, FRA→MBA→…) ──────
FS_4Y136 = {"FlightStatusResource": {"Flights": {"Flight": [
    {"Departure": {"AirportCode": "FRA",
                   "ScheduledTimeLocal": {"DateTime": "2026-07-21T19:25"},
                   "ScheduledTimeUTC": {"DateTime": "2026-07-21T17:25Z"},
                   "Terminal": {"Name": "1", "Gate": "C16"}},
     "Arrival": {"AirportCode": "MBA",
                 "ScheduledTimeLocal": {"DateTime": "2026-07-22T05:10"},
                 "ScheduledTimeUTC": {"DateTime": "2026-07-22T02:10Z"},
                 "Terminal": {"Name": "1"}},
     "Equipment": {"AircraftCode": "333", "AircraftRegistration": "DAIKP"},
     "FlightStatus": {"Code": "NA", "Definition": "No status"}},
    {"Departure": {"AirportCode": "MBA",
                   "ScheduledTimeLocal": {"DateTime": "2026-07-22T06:25"},
                   "ScheduledTimeUTC": {"DateTime": "2026-07-22T03:25Z"}},
     "Arrival": {"AirportCode": "JRO",
                 "ScheduledTimeLocal": {"DateTime": "2026-07-22T07:20"},
                 "ScheduledTimeUTC": {"DateTime": "2026-07-22T04:20Z"}},
     "Equipment": {"AircraftCode": "333", "AircraftRegistration": "DAIKP"},
     "FlightStatus": {"Code": "NA", "Definition": "No status"}},
]}}}

# LH400 abgeflogen (Ist-Zeiten + Delay)
FS_LH400 = {"FlightStatusResource": {"Flights": {"Flight":
    {"Departure": {"AirportCode": "FRA",
                   "ScheduledTimeLocal": {"DateTime": "2026-07-21T10:55"},
                   "ScheduledTimeUTC": {"DateTime": "2026-07-21T08:55Z"},
                   "ActualTimeLocal": {"DateTime": "2026-07-21T11:05"},
                   "ActualTimeUTC": {"DateTime": "2026-07-21T09:05Z"},
                   "Terminal": {"Name": "1", "Gate": "Z16"}},
     "Arrival": {"AirportCode": "JFK",
                 "ScheduledTimeLocal": {"DateTime": "2026-07-21T13:35"},
                 "ScheduledTimeUTC": {"DateTime": "2026-07-21T17:35Z"},
                 "EstimatedTimeLocal": {"DateTime": "2026-07-21T13:03"},
                 "EstimatedTimeUTC": {"DateTime": "2026-07-21T17:03Z"},
                 "Terminal": {"Name": "1"}},
     "Equipment": {"AircraftCode": "346", "AircraftRegistration": "DAIHY"},
     "FlightStatus": {"Code": "DP", "Definition": "Flight Departed"}}}}}


def test_is_lh_group():
    assert lh.is_lh_group("4Y136")
    assert lh.is_lh_group("LH 400")
    assert lh.is_lh_group("LX16")
    assert lh.is_lh_group("EW8")
    assert lh.is_lh_group("OS1")
    assert not lh.is_lh_group("AB123")   # nicht Group
    assert not lh.is_lh_group("UA900")   # Partner, aber Budget-Filter aus
    assert not lh.is_lh_group("4Y")      # keine Nummer
    assert not lh.is_lh_group("")


def test_offset_iso():
    assert lh._offset_iso("2026-07-21T10:55", "2026-07-21T08:55Z") == "2026-07-21T10:55:00+02:00"
    assert lh._offset_iso("2026-07-22T05:10", "2026-07-22T02:10Z") == "2026-07-22T05:10:00+03:00"
    # JFK Sommer = UTC-4
    assert lh._offset_iso("2026-07-21T13:35", "2026-07-21T17:35Z") == "2026-07-21T13:35:00-04:00"
    # ohne UTC → naiv, aber :00 aufgefüllt
    assert lh._offset_iso("2026-07-21T13:35", None) == "2026-07-21T13:35:00"
    assert lh._offset_iso(None, None) is None


def test_norm_reg():
    assert lh._norm_reg("DAIKP") == "D-AIKP"
    assert lh._norm_reg("HBJHA") == "HB-JHA"
    assert lh._norm_reg("D-AIKP") == "D-AIKP"     # schon normalisiert
    assert lh._norm_reg("") == ""


def test_leg_to_facts_future():
    facts = lh._leg_to_facts(FS_4Y136["FlightStatusResource"]["Flights"]["Flight"][0])
    assert facts["sched_dep"] == "2026-07-21T19:25:00+02:00"
    assert facts["sched_arr"] == "2026-07-22T05:10:00+03:00"
    assert facts["gate"] == "C16"
    assert facts["terminal"] == "1"
    assert facts["type"] == "333"
    assert facts["reg"] == "D-AIKP"
    assert facts["dep_iata"] == "FRA" and facts["arr_iata"] == "MBA"
    # kein Ist / Delay bei Zukunftsflug
    assert "est_dep" not in facts and "dep_delay_min" not in facts
    # 'No status' wird NICHT als dep_status durchgereicht
    assert "dep_status" not in facts


def test_leg_to_facts_departed_with_delay():
    facts = lh._leg_to_facts(FS_LH400["FlightStatusResource"]["Flights"]["Flight"])
    assert facts["est_dep"] == "2026-07-21T11:05:00+02:00"
    assert facts["dep_delay_min"] == 10
    assert facts["arr_delay_min"] == -32       # verfrüht
    assert facts["reg"] == "D-AIHY"
    assert facts["gate"] == "Z16"
    assert facts["dep_status"] == "Flight Departed"


def test_flight_facts_picks_matching_leg(monkeypatch):
    monkeypatch.setattr(lh, "_KEY", "k"); monkeypatch.setattr(lh, "_SECRET", "s")
    monkeypatch.setattr(lh, "_get", lambda path: FS_4Y136)
    # dep/arr wählt das RICHTIGE Leg (FRA-MBA, nicht MBA-JRO)
    f = lh.lh_flight_facts("4Y136", "2026-07-21", "FRA", "MBA")
    assert f["dep_iata"] == "FRA" and f["arr_iata"] == "MBA"
    f2 = lh.lh_flight_facts("4Y136", "2026-07-21", "MBA", "JRO")
    assert f2["dep_iata"] == "MBA" and f2["arr_iata"] == "JRO"


def test_flight_facts_noop_when_unconfigured(monkeypatch):
    monkeypatch.setattr(lh, "_KEY", ""); monkeypatch.setattr(lh, "_SECRET", "")
    assert lh.lh_flight_facts("4Y136", "2026-07-21", "FRA", "MBA") == {}


def test_flight_facts_noop_for_non_group(monkeypatch):
    monkeypatch.setattr(lh, "_KEY", "k"); monkeypatch.setattr(lh, "_SECRET", "s")
    # kein Netz-Call für Nicht-Group-Flug
    called = {"n": 0}
    monkeypatch.setattr(lh, "_get", lambda p: called.__setitem__("n", called["n"] + 1) or {})
    assert lh.lh_flight_facts("AB123", "2026-07-21", "X", "Y") == {}
    assert called["n"] == 0


def test_merge_precedence():
    from blueprints.aerox_data_blueprint import _merge_lh_into_facts
    obs = {"sched_dep": "OBS-DEP", "est_dep": "OBS-EST", "reg": "D-OLD",
           "dep_status": "Board-Status"}
    lh_facts = {"sched_dep": "LH-DEP", "gate": "C16", "reg": "D-NEW",
                "est_dep": "LH-EST", "arr_status": "LH-ARR"}
    out = _merge_lh_into_facts(obs, lh_facts)
    # LH autoritativ: sched_dep + reg überschrieben, gate neu
    assert out["sched_dep"] == "LH-DEP"
    assert out["reg"] == "D-NEW"
    assert out["gate"] == "C16"
    # Board-Ist bleibt (LH füllt nur Lücken): est_dep NICHT überschrieben
    assert out["est_dep"] == "OBS-EST"
    # dep_status (Board) bleibt, arr_status (Lücke) von LH gefüllt
    assert out["dep_status"] == "Board-Status"
    assert out["arr_status"] == "LH-ARR"


def test_merge_empty_lh_is_noop():
    from blueprints.aerox_data_blueprint import _merge_lh_into_facts
    obs = {"sched_dep": "X"}
    assert _merge_lh_into_facts(obs, {}) == obs
    assert _merge_lh_into_facts(obs, None) == obs
