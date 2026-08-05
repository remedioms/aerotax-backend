"""Reine Schutztests für den OffBlock-Duties-Parser."""

import importlib.util
import os
import sys


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
