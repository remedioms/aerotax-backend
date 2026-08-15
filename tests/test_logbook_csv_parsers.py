"""Regression tests for newly observed production CSV export formats."""

import csv
import os
import sys


TOOLS = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                     "tools", "logbook-parsers")
sys.path.insert(0, TOOLS)

import parse_foreflight_csv as foreflight  # noqa: E402
import parse_simple_flights_csv as compact  # noqa: E402


def test_compact_history_keeps_only_positive_duty_rows(tmp_path):
    path = tmp_path / "history.csv"
    path.write_text(
        "2026-01-01,FRA,JFK,LH400,C,480,Line check,Duty\n"
        "2026-01-03,JFK,FRA,LH401,Y,0,Cancelled,Duty\n"
        "2026-01-04,FRA,MUC,LH100,Y,60,Passenger,Private\n"
        "2026-01-05,FRA,HAM,LH010,Y,55,,Duty\n"
        "2026-01-05,HAM,FRA,LH017,Y,60,,Duty\n",
        encoding="utf-8",
    )
    assert compact.matches_csv(path)
    legs, sims, report = compact.parse_csv(path)
    assert sims == []
    assert [leg["flight"] for leg in legs] == ["LH400", "LH010", "LH017"]
    assert report["private_rows_skipped"] == 1
    assert report["zero_time_rows_skipped"] == 1
    assert report["block_min"] == 595


def test_foreflight_multitable_export_preserves_flight_and_sim_totals(tmp_path):
    path = tmp_path / "foreflight.csv"
    rows = [
        ["ForeFlight Logbook Import"],
        ["AircraftID", "TypeCode", "Model"],
        ["D-AIXA", "A359", "A350-900"],
        ["Flights Table"],
        ["Date", "AircraftID", "From", "To", "TotalTime", "Night",
         "SimulatedFlight", "Landing Full-Stop Day",
         "Landing Full-Stop Night", "DayLandingsFullStop",
         "NightLandingsFullStop", "PIC", "PICUS", "SIC",
         "DualReceived", "PilotComments"],
        ["2026-01-02", "D-AIXA", "FRA", "JFK", "08:10", "03:00", "",
         "1", "", "", "", "", "", "08:10", "", "Oceanic"],
        ["2026-01-04", "FFS01", "FRA", "FRA", "", "", "04:00",
         "", "", "", "", "", "", "04:00", "", "Recurrent"],
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(rows)
    assert foreflight.matches_csv(path)
    legs, sims, report = foreflight.parse_csv(path)
    assert legs[0]["block_min"] == 490
    assert legs[0]["type"] == "A359"
    assert legs[0]["role"] == "SIC"
    assert sims[0]["duration_min"] == 240
    assert report["block_min"] == 490
    assert report["sim_min"] == 240
    assert report["control"] == "OK"
