"""Cargolux Personal Crew Schedule Report parser regressions.

All fixtures are synthetic.  No real crew names, IDs, hotel details or source
PDF bytes are committed.
"""

from datetime import date

import cargolux_roster_pdf as parser


def _line(top, **columns):
    row = {name: "" for name in parser._COLUMN_NAMES}
    row.update(columns)
    row["top"] = float(top)
    row["text"] = " ".join(str(value) for value in columns.values())
    return row


def test_multileg_utc_rotation_and_day_offset():
    lines = [
        _line(100, date="19/09/2026 Sat"),
        _line(93, duties="2616", details="NBO - AMS",
              actual="23:40 - 09:00⁺¹"),
        _line(100, report="22:40", debrief="12:15⁺¹", credits="13:35",
              indicators="l"),
        _line(107, duties="2616", details="AMS - LUX",
              actual="10:30⁺¹ - 11:45⁺¹"),
    ]
    records = parser._records_from_lines(lines)
    events = parser._record_events(records[0])

    assert len(events) == 2
    assert events[0][1].isoformat() == "2026-09-19T23:40:00"
    assert events[0][2].isoformat() == "2026-09-20T09:00:00"
    assert events[1][1].isoformat() == "2026-09-20T10:30:00"
    assert events[1][2].isoformat() == "2026-09-20T11:45:00"
    assert events[0][3] == "22:40 UTC Briefing NBO · CV2616 NBO - AMS"
    assert events[1][3] == "CV2616 AMS - LUX"


def test_training_off_day_and_layover_are_preserved_without_private_details():
    training = {
        "day": date(2026, 9, 1), "weekday": "Tue", "top": 100.0,
        "lines": [
            _line(94, details="FFS Difference Training 747-"),
            _line(100, duties="DT4S", report="04:00",
                  actual="05:00 - 09:00", debrief="09:30"),
            _line(106, details="400F"),
            # A real PDF has coworker names in x >= 665.  Coordinate cropping
            # excludes that column before records reach this pure function.
        ],
    }
    off = {
        "day": date(2026, 9, 9), "weekday": "Wed", "top": 120.0,
        "lines": [_line(120, duties="B", details="5 Immobilized Off Days")],
    }
    layover = {
        "day": date(2026, 9, 15), "weekday": "Tue", "top": 140.0,
        "lines": [_line(140, duties="JNB")],
    }

    training_events = parser._record_events(training)
    assert training_events[0][1].isoformat() == "2026-09-01T04:00:00"
    assert training_events[0][2].isoformat() == "2026-09-01T09:30:00"
    assert training_events[0][3] == "Training DT4S FFS Difference Training 747- 400F"
    assert parser._record_events(off)[0][3] == "Off Day"
    assert parser._record_events(layover)[0][3:] == ("LAYOVER", True, None, "JNB")


def test_wrong_weekday_fails_closed():
    record = {
        "day": date(2026, 9, 2), "weekday": "Thu", "top": 100.0,
        "lines": [_line(100, date="02/09/2026 Thu", duties="6293",
                       details="LUX - LAX", report="10:10",
                       actual="11:20 - 22:55", debrief="23:25")],
    }
    try:
        parser._record_events(record)
    except ValueError as exc:
        assert str(exc) == "invalid_roster_day"
    else:
        raise AssertionError("contradictory weekday must not be guessed")


def test_privacy_boundary_drops_crew_column_words():
    class FakePage:
        def extract_words(self, **_kwargs):
            return [
                {"text": "Schedule", "x0": 16, "top": 10},
                {"text": "Details", "x0": 52, "top": 10},
                {"text": "02/09/2026", "x0": 16, "top": 40},
                {"text": "Wed", "x0": 69, "top": 40},
                {"text": "6293", "x0": 90, "top": 40},
                {"text": "LUX", "x0": 170, "top": 40},
                {"text": "-", "x0": 198, "top": 40},
                {"text": "LAX", "x0": 208, "top": 40},
                {"text": "11:20", "x0": 355, "top": 40},
                {"text": "-", "x0": 389, "top": 40},
                {"text": "22:55", "x0": 401, "top": 40},
                {"text": "PRIVATE-CREW-NAME", "x0": 670, "top": 40},
                {"text": "Generated", "x0": 16, "top": 70},
                {"text": "on", "x0": 67, "top": 70},
            ]

    lines = parser._page_schedule_lines(FakePage())
    combined = " ".join(line["text"] for line in lines)
    assert "PRIVATE-CREW-NAME" not in combined
