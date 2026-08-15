"""Schutztests fuer den Edelweiss-Logbook-XLSX-Parser."""

import importlib.util
import os
import sys
from datetime import datetime, time

import openpyxl
import pytest


TOOLS = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                     "tools", "logbook-parsers")
sys.path.insert(0, TOOLS)
import logbook_watchdog  # noqa: E402
SPEC = importlib.util.spec_from_file_location(
    "parse_edw_xlsx", os.path.join(TOOLS, "parse_edw_xlsx.py")
)
PARSER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PARSER)


def workbook(tmp_path, rows):
    path = tmp_path / "edw.xlsx"
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Logbook"
    sheet.append(PARSER.EXPECTED_HEADERS)
    for row in rows:
        sheet.append(row)
    book.save(path)
    return path


def row(*, day=datetime(2026, 7, 2), dep="LSZH", dep_time=time(11, 40),
        arr="CYVR", arr_time=time(21, 48), flight=time(10, 8),
        pic=time(0, 0), copilot=time(6, 45), takeoffs=0, landings=1):
    return (
        day, dep, dep_time, arr, arr_time, "LH", "350", "HB-JJN",
        takeoffs, landings, flight, pic, copilot, "AA / BB / CC", "",
    )


def test_augmented_crew_uses_personal_function_time(tmp_path):
    path = workbook(tmp_path, [row()])
    assert PARSER.matches_workbook(path)
    parsed, controls = PARSER.parse_workbook(path)
    leg = parsed["legs"][0]
    assert leg["block_min"] == 405
    assert leg["role"] == "FO"
    assert leg["from"] == "ZRH"
    assert leg["to"] == "YVR"
    assert leg["type"] == "A350"
    assert controls["source_flight_min"] == 608
    assert controls["credited_min"] == 405


def test_unrelated_workbook_is_not_claimed(tmp_path):
    path = tmp_path / "other.xlsx"
    book = openpyxl.Workbook()
    book.active.append(("Date", "From", "To"))
    book.save(path)
    assert not PARSER.matches_workbook(path)


def test_broken_workbook_is_not_claimed(tmp_path):
    path = tmp_path / "broken.xlsx"
    path.write_bytes(b"PK not actually a workbook")
    assert not PARSER.matches_workbook(path)


def test_watchdog_routes_neutral_suffix_and_adds_period(tmp_path):
    source = workbook(tmp_path, [row()])
    path = tmp_path / "payload.upload"
    path.write_bytes(source.read_bytes())
    name, legs, sims, report = logbook_watchdog._try_parsers(path)
    assert name == "edelweiss_xlsx"
    assert len(legs) == 1
    assert sims == []
    assert report["month"] == "2026-07"


def test_zero_function_observer_row_is_not_imported(tmp_path):
    observer = row(
        day=datetime(2024, 4, 2), dep="LSZH", dep_time=time(10, 34),
        arr="LEPA", arr_time=time(12, 19), flight=time(0, 0),
        copilot=time(0, 0), landings=0,
    )
    parsed, controls = PARSER.parse_workbook(workbook(tmp_path, [observer, row()]))
    assert len(parsed["legs"]) == 1
    assert controls["skipped_zero_function_rows"] == 1


def test_zero_function_with_landing_is_rejected(tmp_path):
    invalid = row(flight=time(0, 0), copilot=time(0, 0), landings=1)
    with pytest.raises(ValueError, match="Null-Funktionszeit"):
        PARSER.parse_workbook(workbook(tmp_path, [invalid]))


def test_source_flight_time_must_match_utc_clocks(tmp_path):
    invalid = row(flight=time(10, 7))
    with pytest.raises(ValueError, match="UTC-Zeiten"):
        PARSER.parse_workbook(workbook(tmp_path, [invalid]))


def test_unknown_airport_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="nicht aufloesbar"):
        PARSER.parse_workbook(workbook(tmp_path, [row(arr="ZZZZ")]))


def test_repeated_route_gets_backend_key_suffix(tmp_path):
    second = row(dep_time=time(22, 0), arr_time=time(8, 8))
    parsed, controls = PARSER.parse_workbook(workbook(tmp_path, [row(), second]))
    assert "flight" not in parsed["legs"][0]
    assert parsed["legs"][1]["flight"] == "(2)"
    assert controls["key_collisions"] == 1
