"""Reine Schutztests für den LH-Flugstundenübersicht-Parser."""

import importlib.util
import os
import sys
from datetime import datetime, timezone


TOOLS = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                     "tools", "logbook-parsers")
sys.path.insert(0, TOOLS)
SPEC = importlib.util.spec_from_file_location(
    "parse_lh_flugstunden", os.path.join(TOOLS, "parse_lh_flugstunden.py")
)
PARSER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PARSER)


def test_sap_decimal_hours_round_to_minutes():
    assert PARSER.decimal_hours_minutes("36,13") == 2168
    assert PARSER.decimal_hours_minutes("0,75") == 45
    assert PARSER.decimal_hours_minutes(None) == 0


def test_sap_month_control_accepts_only_verified_three_minute_rounding_drift():
    assert PARSER.control_minutes_match(2211, 2213)
    assert PARSER.control_minutes_match(4474, 4477)
    assert not PARSER.control_minutes_match(4473, 4477)


def test_summary_month_anchor_ignores_equal_landing_count():
    class Page:
        @staticmethod
        def extract_words(**_kwargs):
            return [
                {"text": "11", "x0": 65.9, "top": 771.8},
                {"text": "11", "x0": 169.6, "top": 771.8},
                {"text": "41,73", "x0": 195.0, "top": 771.8},
                {"text": "1,18", "x0": 315.0, "top": 771.8},
            ]

    assert PARSER._summary(Page(), 11) == {
        "landings": 11,
        "effective_min": 2504,
        "deadhead_min": 71,
    }


def test_sap_flight_padding_matches_existing_import_convention():
    assert PARSER.normalized_flight("LH0046") == "LH046"
    assert PARSER.normalized_flight("LH0982") == "LH982"
    assert PARSER.normalized_flight("LH1670") == "LH1670"
    assert PARSER.normalized_flight("LH1788/23") == "LH1788"


def test_sap_short_ft_device_is_a_simulator():
    assert PARSER.RE_SIM_DEVICE.fullmatch("FT51")


def test_empty_sap_month_accepts_omitted_effective_total_only_without_rows():
    assert PARSER.effective_summary_minutes(None, 0) == 0
    try:
        PARSER.effective_summary_minutes(None, 1)
    except ValueError as exc:
        assert "Effektive Flugzeit fehlt" in str(exc)
    else:
        raise AssertionError("missing effective total with source rows must fail")


def test_german_registration_gets_display_hyphen():
    assert PARSER.normalized_registration("DAIUL") == "D-AIUL"
    assert PARSER.normalized_registration("MUC327") == "MUC327"


def test_legacy_cabin_traffic_code_distinguishes_deadhead():
    assert not PARSER.legacy_cabin_traffic_code("00")
    assert PARSER.legacy_cabin_traffic_code("01")
    try:
        PARSER.legacy_cabin_traffic_code("02")
    except ValueError as exc:
        assert "Traffic-Code" in str(exc)
    else:
        raise AssertionError("unknown cabin traffic code must fail")


def test_civil_night_split_matches_verified_florian_reference_landings():
    # Bereits am 03/2026-Import manuell verifiziert: BIO→MUC 19:59Z Nacht,
    # MRS→MUC 08:57Z Tag. Diese beiden Punkte schützen die -6°-Grenze.
    assert PARSER.is_civil_night(
        datetime(2026, 3, 9, 19, 59, tzinfo=timezone.utc), "MUC"
    )
    assert not PARSER.is_civil_night(
        datetime(2026, 3, 11, 8, 57, tzinfo=timezone.utc), "MUC"
    )


def test_arrival_wraps_to_next_utc_day():
    dep, arr = PARSER._clock_instants(2019, 12, 31, "23:40-01:10")
    assert dep.isoformat() == "2019-12-31T23:40:00+00:00"
    assert arr.isoformat() == "2020-01-01T01:10:00+00:00"


def test_sap_midnight_2400_is_next_utc_day():
    dep, arr = PARSER._clock_instants(2026, 5, 7, "20:44-24:00")
    assert dep.isoformat() == "2026-05-07T20:44:00+00:00"
    assert arr.isoformat() == "2026-05-08T00:00:00+00:00"
