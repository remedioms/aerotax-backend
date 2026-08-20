"""Strict regressions for SWISS Historical published roster imports."""

import os
import sys

import pytest
from reportlab.pdfgen import canvas


TOOLS = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                     "tools", "logbook-parsers")
sys.path.insert(0, TOOLS)

import logbook_watchdog  # noqa: E402
import parse_swiss_historical as parser  # noqa: E402


COLUMNS = {
    "date": 45, "report": 77, "pos": 155, "activity": 208,
    "from": 262, "to": 287, "dep": 311, "arr": 340,
    "type": 379, "layover": 408, "flt": 481, "dp": 522,
}


def _pdf(tmp_path, total="14:17", unknown_airport=False, incomplete=False):
    path = tmp_path / "swiss.pdf"
    doc = canvas.Canvas(str(path), pagesize=(595, 842))
    doc.setFont("Helvetica", 8)

    def text(x, top, value):
        doc.drawString(x, 842 - top, value)

    text(44, 45, "Historical published roster")
    text(44, 65, "Period: May 2026")
    text(44, 85, "Crew member: 12345, Example, Crew")
    text(502, 100, "All times local")
    for key, value in (("date", "Date"), ("report", "Report"),
                       ("pos", "Pos"), ("activity", "Activity"),
                       ("from", "From"), ("to", "To"),
                       ("dep", "Dep"), ("arr", "Arr"),
                       ("type", "A/C"), ("layover", "Layover"),
                       ("flt", "Flt hrs"), ("dp", "DP hrs")):
        text(COLUMNS[key], 120, value)

    # LX177 is deliberately split over two calendar rows.
    for key, value in (("date", "01 Fri"), ("report", "22:05"),
                       ("pos", "MC"), ("activity", "LX177"),
                       ("from", "ZZZ" if unknown_airport else "SIN"),
                       ("dep", "23:23"), ("type", "77W")):
        text(COLUMNS[key], 145, value)
    if not incomplete:
        for key, value in (("date", "02 Sat"), ("pos", "MC"),
                           ("to", "ZRH"), ("arr", "06:12")):
            text(COLUMNS[key], 160, value)

    for key, value in (("date", "10 Sun"), ("report", "06:15"),
                       ("pos", "FA"), ("activity", "LX2802"),
                       ("from", "ZRH"), ("to", "GVA"),
                       ("dep", "07:33"), ("arr", "08:16"),
                       ("type", "320")):
        text(COLUMNS[key], 185, value)
    for key, value in (("pos", "FA"), ("activity", "LX2807"),
                       ("from", "GVA"), ("to", "ZRH"),
                       ("dep", "10:03"), ("arr", "10:48"),
                       ("type", "320")):
        text(COLUMNS[key], 200, value)
    # Deadhead has valid clocks but must not enter the monthly total.
    for key, value in (("date", "15 Fri"), ("pos", "DH"),
                       ("activity", "LX2817"), ("from", "GVA"),
                       ("to", "ZRH"), ("dep", "18:45"),
                       ("arr", "19:37")):
        text(COLUMNS[key], 225, value)

    text(208, 260, f"Total flight time in May: {total}")
    text(44, 800, "Created 20Aug2026 15:01 (ZRH) by 12345")
    doc.save()
    return path


def test_swiss_roster_reconstructs_local_clocks_and_excludes_deadhead(tmp_path):
    path = _pdf(tmp_path)
    assert parser.matches_pdf(path)
    legs, sims, report = parser.parse_pdf(path)
    assert sims == []
    assert len(legs) == 3
    assert sum(leg["block_min"] for leg in legs) == 14 * 60 + 17
    assert legs[0]["flight"] == "LX177"
    assert legs[0]["from"] == "SIN" and legs[0]["to"] == "ZRH"
    assert legs[0]["block_min"] == 12 * 60 + 49
    assert legs[0]["dep_iso"] == "2026-05-01T15:23:00Z"
    assert legs[0]["arr_iso"] == "2026-05-02T04:12:00Z"
    assert all(leg["flight"] != "LX2817" for leg in legs)
    assert report["deadheads_skipped"] == 1
    assert report["verified_source_block_total"] == 14 * 60 + 17


def test_watchdog_routes_swiss_document_by_content(tmp_path):
    name, legs, sims, report = logbook_watchdog._try_parsers(_pdf(tmp_path))
    assert name == "swiss_historical_roster"
    assert len(legs) == 3 and sims == []
    assert report["month"] == "2026-05"


def test_source_total_is_a_hard_control(tmp_path):
    with pytest.raises(ValueError, match="Flugzeit-Summe"):
        parser.parse_pdf(_pdf(tmp_path, total="14:16"))


def test_unknown_airport_timezone_is_never_guessed(tmp_path):
    with pytest.raises(ValueError, match="Zeitzone"):
        parser.parse_pdf(_pdf(tmp_path, unknown_airport=True))


def test_split_overnight_leg_must_have_an_explicit_arrival_row(tmp_path):
    with pytest.raises(ValueError, match="unvollstaendiges Overnight-Leg|endet ohne Ankunft"):
        parser.parse_pdf(_pdf(tmp_path, incomplete=True, total="1:28"))
