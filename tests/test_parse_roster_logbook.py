"""Regression tests for historical Lufthansa/Condor roster-logbook PDFs."""

import importlib.util
import os
from datetime import datetime, timezone


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


def test_condor_individual_plan_columns_reconcile_total_and_exclude_future():
    header = """Individual duty plan for [redacted]
NetLine/Crew(CFG) printed by CREWLINK 03Aug26 19:18 Page 1
Period: 01Jul26 - 31Aug26
Flight time 13:38 Off days 14
"""
    segments = [
        """Fri10 C/I DUS 0235
DE 1616 DUS 0417 0722 HER 32N JU
DE 1663 HER 0838 1200 FRA 320 JU
Sat11 C/I FRA 1110
DE 1662 R FRA 1320 1622 HER 32N JU
""",
        """[BZW 0:00] Tue04 C/I DUS 1000
DE 1414 DUS 1221 1630 FNC 32H JU
""",
    ]
    legs, meta = PARSER.parse_condor_individual_segments(
        header, segments, "ST")
    assert [leg["flight"] for leg in legs] == [
        "DE1616", "DE1663", "DE1662"]
    assert legs[0]["dep_iso"] == "2026-07-10T04:17:00Z"
    assert legs[0]["arr_iso"] == "2026-07-10T07:22:00Z"
    assert all(leg["role"] == "FB" for leg in legs)
    assert meta["future_legs_excluded"] == 1
    assert meta["verified_source_block_total"] == 818


def test_condor_individual_plan_rejects_block_total_mismatch():
    header = """Individual duty plan for [redacted]
NetLine/Crew(CFG) printed by CREWLINK 03Aug26 19:18 Page 1
Period: 01Jul26 - 31Aug26
Flight time 01:00 Off days 14
"""
    try:
        PARSER.parse_condor_individual_segments(
            header,
            ["Fri10 C/I DUS 0235\nDE 1616 DUS 0417 0722 HER 32N JU\n"],
            "ST",
        )
    except ValueError as exc:
        assert "block total mismatch" in str(exc)
    else:
        raise AssertionError("mismatched source total was accepted")


def test_condor_individual_plan_accepts_summary_touching_final_flight():
    header = """Individual duty plan for [redacted]
NetLine/Crew(CFG) printed by CREWLINK 12Aug26 18:39 Page 1
Period: 01May26 - 31Aug26
Flight time 09:21 Off days 14
"""
    legs, meta = PARSER.parse_condor_individual_segments(
        header,
        ["Tue28 C/I YYC 2100\n"
         "DE 2443 R YYC 2233 0754 FRA 339 [FT 09:21]\n"],
        "PU",
    )
    assert len(legs) == 1
    assert legs[0]["flight"] == "DE2443"
    assert legs[0]["block_min"] == 561
    assert meta["verified_source_block_total"] == 561


def test_condor_individual_plan_accepts_column_prefix_day_and_role_suffix():
    """Five valid rows touched adjacent-column text in a real long roster."""
    header = """Individual duty plan for [redacted]
NetLine/Crew(CFG) printed by CREWLINK 01Feb26 12:00 Page 1
Period: 01Jan26 - 31Jan26
Flight time 51:01 Off days 14
"""
    segments = [
        ":00] Sat03 Pick Up 1300\n"
        ":00] DE 2315 R MRU 1511 0310 FRA 339\n",
        ":08] Mon05 Pick Up 2005\n"
        ":58] DE 3807 MBJ 2208 0720 FRA 339\n",
        "Fri09 C/I FRA 0400\n"
        "DE 2116 FRA 0955 2143 CUN 339 ST\n",
        "Sun11 DE 2115 CUN 0042 1014 FRA 339 ST\n",
        "Tue13 DE 3827 R LRM 0034 0904 FRA 339\n"
        "DH/DE 2227 PUJ 0310 1225 FRA\n",
    ]
    legs, meta = PARSER.parse_condor_individual_segments(
        header, segments, "PU")
    assert [leg["flight"] for leg in legs] == [
        "DE2315", "DE3807", "DE2116", "DE2115", "DE3827"]
    assert sum(leg["block_min"] for leg in legs) == 3061
    assert meta["verified_source_block_total"] == 3061


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


def test_cargo_released_roster_uses_processing_cutoff_not_print_cutoff():
    text = """Released Roster
Month: July 2026
Company Name: YF
Jul 99:00
Date (LT) Trip ID Report (LT) Pos Activity From To Start (UTC) End (UTC) A/C Layover
04 Sat 12345 01:10 SF 8387 ICN FRA (03) 17:25 06:45 77X
Created 22Jun2026 12:34 (UTC) by Jeppesen
"""
    # CLI/backfill default stays conservative: at print time this was future.
    old, _ = PARSER.parse_acknowledged_text(text)
    assert old == []
    legs, meta = PARSER.parse_acknowledged_text(
        text, completed_at=datetime(2026, 8, 12, tzinfo=timezone.utc))
    assert len(legs) == 1
    assert legs[0]["flight"] == "LH8387"
    assert legs[0]["block_min"] == 800
    # YF's monthly value is contractual credit, not a leg-time checksum.
    assert meta["monthly_total_control"] == "not_applicable_yf_credit_time"


def test_complete_newer_roster_month_replaces_changed_old_assignment():
    old = {
        "date": "2026-06-11", "flight": "LH8364", "from": "FRA",
        "to": "HYD", "dep_iso": "2026-06-11T08:45:00Z",
        "arr_iso": "2026-06-11T17:35:00Z", "block_min": 530,
        "_roster_month": "2026-06",
    }
    new = {
        "date": "2026-06-11", "flight": "LH8160", "from": "FRA",
        "to": "JFK", "dep_iso": "2026-06-11T17:20:00Z",
        "arr_iso": "2026-06-12T01:45:00Z", "block_min": 505,
        "_roster_month": "2026-06",
    }
    legs, superseded = PARSER._merge_source_legs([
        ("old", [old], {"created_at": "2026-05-20T13:00:00+00:00",
                         "coverage_months": ["2026-06"]}),
        ("new", [new], {"created_at": "2026-06-03T17:26:00+00:00",
                         "coverage_months": ["2026-06"]}),
    ])
    assert legs == [new]
    assert superseded == 1


def test_cas_calendar_rows_convert_all_flights_without_invented_fields(
        monkeypatch, tmp_path):
    import cas_roster_parser
    source = tmp_path / "cas.pdf"
    source.write_bytes(b"%PDF-CAS")
    result = {
        "period": "FEB 2025",
        "printed_at": datetime(2025, 2, 26, 16, 21),
        "coverage_dates": ["2025-02-01"],
        "counts": {"flight_legs": 1},
        "warnings": [],
        "events": [("x", datetime(2025, 2, 1, 11, 21),
                    datetime(2025, 2, 1, 19, 30),
                    "10:25 LT Briefing FRA · LH756 FRA - BOM", False)],
    }
    monkeypatch.setattr(
        cas_roster_parser, "parse_cas_roster_pdf",
        lambda *_args, **_kwargs: (result, None))
    legs, meta = PARSER.parse_cas_pdf(
        str(source), completed_at=datetime(2026, 1, 1,
                                           tzinfo=timezone.utc))
    assert legs == [{
        "date": "2025-02-01", "flight": "LH756", "from": "FRA",
        "to": "BOM", "dep_iso": "2025-02-01T11:21:00Z",
        "arr_iso": "2025-02-01T19:30:00Z", "block_min": 489,
        "remarks": "Lufthansa CAS roster; UTC schedule row",
    }]
    assert meta["flight_rows_verified"] == 1
