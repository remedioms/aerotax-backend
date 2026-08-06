"""Reine Schutztests für den OffBlock-Duties-Parser."""

import importlib.util
import os
import sys
from datetime import date


TOOLS = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                     "tools", "logbook-parsers")
sys.path.insert(0, TOOLS)
SPEC = importlib.util.spec_from_file_location(
    "parse_duties_v8", os.path.join(TOOLS, "parse_duties_v8.py")
)
PARSER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PARSER)


def _row(**updates):
    row = {
        "Type": "Flight",
        "Departure place": "MUC",
        "Arrival place": "MUC",
        "Departure time": "17:17",
        "Arrival time": "17:17",
        "Total time": "00:00",
        "Flight number": "",
        "Aircraft registration": "",
        "Aircraft ICAO": "",
    }
    row.update(updates)
    return row


def test_zero_duration_same_station_marker_is_not_a_flight():
    assert PARSER.is_zero_duration_marker(_row())


def test_real_same_station_flight_is_not_treated_as_marker():
    assert not PARSER.is_zero_duration_marker(
        _row(**{"Flight number": "LH2572", "Total time": "01:10"})
    )


def test_midnight_arrival_is_not_treated_as_zero_duration_marker():
    assert not PARSER.is_zero_duration_marker(
        _row(**{"Departure time": "23:30", "Arrival time": "00:00",
                "Total time": "00:30"})
    )


def test_fstd_device_with_same_station_is_a_simulator():
    assert PARSER.fstd_row_is_sim("DE-1A-040", "FRA", "FRA", "MCC 3")


def test_fstd_field_does_not_erase_confirmed_operating_leg():
    assert not PARSER.fstd_row_is_sim(
        "DE-1A-079", "MUC", "GRU", "LH504"
    )


def test_fstd_code_is_never_persisted_as_aircraft_registration():
    source = open(os.path.join(TOOLS, "parse_duties_v8.py"), encoding="utf-8").read()
    assert "not RE_FSTD.fullmatch(reg_raw)" in source


def test_future_duty_is_planned_even_with_prefilled_crew_and_times():
    row = _row(**{
        "Departure place": "FRA",
        "Arrival place": "JFK",
        "Departure time": "08:00",
        "Arrival time": "16:00",
        "Total time": "08:00",
        "Flight number": "LH400",
        "PIC": "Vorbefuellt",
        "FO": "Vorbefuellt",
        "PIC time": "08:00",
    })

    assert not PARSER.looks_unflown(row)
    assert PARSER.is_planned_for_cutoff(
        row, date(2026, 8, 18), date(2026, 8, 6)
    )


def test_confirmed_cutoff_day_duty_remains_importable():
    row = _row(**{
        "Departure place": "FRA",
        "Arrival place": "JFK",
        "Flight number": "LH400",
        "Aircraft registration": "D-AIXA",
    })

    assert not PARSER.is_planned_for_cutoff(
        row, date(2026, 8, 6), date(2026, 8, 6)
    )


def test_unflown_past_row_is_reported_separately_from_planned_rows():
    row = _row(**{
        "Departure place": "FRA",
        "Arrival place": "JFK",
        "Flight number": "DE2016",
    })

    assert not PARSER.is_planned_for_cutoff(
        row, date(2026, 8, 5), date(2026, 8, 6)
    )
    assert PARSER.is_unflown_past(
        row, date(2026, 8, 5), date(2026, 8, 6)
    )


def test_historical_row_with_aircraft_evidence_is_not_warned():
    row = _row(**{
        "Departure place": "FRA",
        "Arrival place": "JFK",
        "Flight number": "DE2016",
        "Aircraft registration": "D-ABOA",
    })

    assert not PARSER.is_unflown_past(
        row, date(2026, 8, 5), date(2026, 8, 6)
    )
