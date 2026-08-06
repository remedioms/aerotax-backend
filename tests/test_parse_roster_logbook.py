"""Regression tests for historical Lufthansa/Condor roster-logbook PDFs."""

import importlib.util
import os


PARSER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tools", "logbook-parsers", "parse_roster_logbook.py",
)
SPEC = importlib.util.spec_from_file_location("parse_roster_logbook", PARSER_PATH)
PARSER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PARSER)


def test_acknowledged_cabin_leg_is_kept_but_future_and_deadhead_are_not():
    text = """Acknowledged Roster
Month: August 2026
Company Name: LH
Date (LT) Trip ID Report (LT) Pos Activity From To Start (UTC) End (UTC) A/C Layover
05 Wed 84253 15:50 PU 084 FRA DUS 16:22 17:11 319
PU 087 DUS FRA 17:50 18:41 319
06 Thu 09:00 DH 187 BER FRA 08:00 09:00 320
PU 916 FRA LHR 16:21 17:53 32N
Created 06Aug2026 11:04 (UTC) by Jeppesen
"""
    legs, meta = PARSER.parse_acknowledged_text(text)
    assert [leg["flight"] for leg in legs] == ["LH84", "LH87"]
    assert all(leg["role"] == "FB" for leg in legs)
    assert sum(leg["block_min"] for leg in legs) == 100
    assert meta["future_legs_excluded"] == 1
    assert meta["deadheads_excluded"] == 1


def test_acknowledged_explicit_utc_marker_and_split_leg():
    text = """Acknowledged Roster
Month: January 2026
Company Name: LH
Date (LT) Trip ID Report (LT) Pos Activity From To Start (UTC) End (UTC) A/C Layover
16 Fri 77364 11:00 FO 752 FRA 11:57 78Q
17 Sat FO 752 HYD (16) 20:21 78Q
18 Sun 02:40 FO 753 HYD FRA (17) 22:13 07:52 78Q
Created 05Aug2026 11:45 (UTC) by Jeppesen
"""
    legs, _ = PARSER.parse_acknowledged_text(text)
    assert len(legs) == 2
    assert legs[0]["dep_iso"] == "2026-01-16T11:57:00Z"
    assert legs[0]["arr_iso"] == "2026-01-16T20:21:00Z"
    assert legs[1]["dep_iso"] == "2026-01-17T22:13:00Z"
    assert legs[1]["arr_iso"] == "2026-01-18T07:52:00Z"


def test_acknowledged_split_leg_accepts_non_787_aircraft():
    text = """Acknowledged Roster
Month: March 2026
Company Name: LH
Date (LT) Trip ID Report (LT) Pos Activity From To Start (UTC) End (UTC) A/C Layover
11 Wed 77364 11:00 PU 400 FRA 11:57 346
PU 400 JFK 20:21 346
Created 05Aug2026 11:45 (UTC) by Jeppesen
"""
    legs, _ = PARSER.parse_acknowledged_text(text)
    assert len(legs) == 1
    assert legs[0]["type"] == "346"
    assert legs[0]["block_min"] == 504


def test_acknowledged_p1_cabin_position_is_a_flying_leg():
    text = """Acknowledged Roster
Month: January 2026
Company Name: LH
Date (LT) Trip ID Report (LT) Pos Activity From To Start (UTC) End (UTC) A/C Layover
07 Wed 74333 09:10 P1 400 FRA JFK 11:19 20:12 346
Created 05Aug2026 11:45 (UTC) by Jeppesen
"""
    legs, _ = PARSER.parse_acknowledged_text(text)
    assert len(legs) == 1
    assert legs[0]["role"] == "FB"
    assert legs[0]["block_min"] == 533


def test_acknowledged_four_digit_trip_id_is_supported():
    text = """Acknowledged Roster
Month: June 2026
Company Name: LH
Date (LT) Trip ID Report (LT) Pos Activity From To Start (UTC) End (UTC) A/C Layover
27 Sat 1234 11:00 PU 100 FRA MUC 12:00 14:04 32N
Created 05Aug2026 11:45 (UTC) by Jeppesen
"""
    legs, _ = PARSER.parse_acknowledged_text(text)
    assert len(legs) == 1
    assert legs[0]["block_min"] == 124


def test_acknowledged_continuation_page_keeps_previous_day_anchor():
    text = """Acknowledged Roster
Month: August 2025
Company Name: LH
Date (LT) Trip ID Report (LT) Pos Activity From To Start (UTC) End (UTC) A/C Layover
08 Fri 12345 08:00 PU 1400 FRA BEG 09:00 10:00 32V
Created 06Aug2026 11:07 (UTC) by Jeppesen
Acknowledged Roster
Month: August 2025
Company Name: LH
Date (LT) Trip ID Report (LT) Pos Activity From To Start (UTC) End (UTC) A/C Layover
PU 1401 BEG FRA 11:00 12:00 32V
Created 06Aug2026 11:07 (UTC) by Jeppesen
"""
    legs, _ = PARSER.parse_acknowledged_text(text)
    assert len(legs) == 2
    assert legs[1]["date"] == "2025-08-08"


def test_acknowledged_adjacent_month_carry_row_is_deduplicated():
    text = """Acknowledged Roster
Month: November 2025
Company Name: LH
Date (LT) Trip ID Report (LT) Pos Activity From To Start (UTC) End (UTC) A/C Layover
30 Sun 12345 08:00 PU 002 FRA HAM 09:00 10:00 320
Created 06Aug2026 11:07 (UTC) by Jeppesen
Acknowledged Roster
Month: December 2025
Company Name: LH
Date (LT) Trip ID Report (LT) Pos Activity From To Start (UTC) End (UTC) A/C Layover
30 Sun 12345 08:00 PU 002 FRA HAM 09:00 10:00 320
Created 06Aug2026 11:07 (UTC) by Jeppesen
"""
    legs, meta = PARSER.parse_acknowledged_text(text)
    assert len(legs) == 1
    assert meta["duplicate_carry_rows_excluded"] == 1


def test_condor_uses_local_fra_time_and_excludes_future_plan_values():
    text = """Duty plan requested at 19JUL26 10:34z - All times: Local FRA
07/2026
BT DH LSW BZW Off claim Off assigned
08:57 00:00 12:00 08:57 0 0
P Fr 10 SB90 DUS 03:00 - 04:35
P DE1616 32N DANCW JU C1 04:35 DUS 06:17 - 09:22 HER +01:00
P DE1663 320 DAICU JU C1 HER 10:38 - 14:00 FRA -01:00
P Mo 20 DE1142 32B DAIAG JU C5 12:30 DUS 13:45 - 16:15 SUF
"""
    legs, meta = PARSER.parse_condor_text(text)
    assert [leg["flight"] for leg in legs] == ["DE1616", "DE1663"]
    assert legs[0]["dep_iso"] == "2026-07-10T04:17:00Z"
    assert legs[0]["arr_iso"] == "2026-07-10T07:22:00Z"
    assert legs[0]["reg"] == "D-ANCW"
    assert legs[0]["role"] == "FB"
    assert meta["future_legs_excluded"] == 1
    assert meta["verified_source_block_total"] == 537


def test_merge_prefers_newest_document_revision():
    old = {
        "date": "2026-01-01", "flight": "LH84", "from": "FRA", "to": "DUS",
        "dep_iso": "2026-01-01T10:00:00Z", "arr_iso": "2026-01-01T11:00:00Z",
        "block_min": 60, "type": "319", "role": "FB",
    }
    new = {**old, "dep_iso": "2026-01-01T10:10:00Z",
           "arr_iso": "2026-01-01T11:15:00Z", "block_min": 65}
    legs, superseded = PARSER._merge_source_legs([
        ("old", [old], {"created_at": "2026-02-01T00:00:00+00:00"}),
        ("new", [new], {"created_at": "2026-03-01T00:00:00+00:00"}),
    ])
    assert legs == [new]
    assert superseded == 1


def test_unknown_pdf_text_is_rejected():
    try:
        PARSER.parse_text("some unrelated report")
    except ValueError as exc:
        assert "unsupported" in str(exc)
    else:
        raise AssertionError("unknown format was claimed")
