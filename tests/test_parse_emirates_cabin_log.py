"""Strict regressions for Emirates Cabin Crew Flight Log imports."""

import os
import sys

import pytest
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas


TOOLS = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                     "tools", "logbook-parsers")
sys.path.insert(0, TOOLS)

import logbook_watchdog  # noqa: E402
import parse_emirates_cabin_log as parser  # noqa: E402


HEADER = ("Date    Flight From To      Departure        Arrival             "
          "Aircraft         Registration")


def _pdf(tmp_path, *, page_number=1, malformed=False, unknown=False,
         date_mismatch=False):
    path = tmp_path / "emirates-cabin-log.pdf"
    doc = canvas.Canvas(str(path), pagesize=landscape(A4))
    doc.setFont("Helvetica", 8)
    rows = [
        ("28 Jul 2016", "EK342", "DXB", "ZZZ" if unknown else "CMB",
         "29 Jul 2016 10:30" if date_mismatch else "28 Jul 2016 10:30",
         "28 Jul 2016 18:30", "Airbus A380-800", "A388", ""),
        ("30 Jul 2016", "EK343", "KUL", "DXB", "30 Jul 2016 02:10",
         "30 Jul 2016 04:55", "Boeing 777-300ER", "B77W", "A6-EQH"),
    ]
    y = 550
    doc.drawString(40, y, "Emirates Cabin Crew Flight Log")
    doc.drawString(40, y - 24, HEADER)
    for row in rows:
        y -= 18
        line = (f"{row[0]} {row[1]} {row[2]} {row[3]} {row[4]} {row[5]} "
                f"{row[6]} ({row[7]}) {row[8]}").rstrip()
        if malformed and row[1] == "EK343":
            line = line.replace("Boeing 777-300ER (B77W)", "")
        doc.drawString(40, y, line)
    doc.drawString(760, 20, f"Page {page_number}")
    doc.save()
    return path


def test_parses_local_airport_times_and_optional_registration(tmp_path):
    path = _pdf(tmp_path)
    assert parser.matches_pdf(path)
    legs, sims, report = parser.parse_pdf(path)
    assert sims == [] and len(legs) == 2
    assert legs[0] == {
        "date": "2016-07-28", "flight": "EK342", "from": "DXB",
        "to": "CMB", "dep_iso": "2016-07-28T06:30:00Z",
        "arr_iso": "2016-07-28T13:00:00Z", "block_min": 390,
        "type": "A388", "role": "FB",
    }
    assert legs[1]["reg"] == "A6-EQH"
    assert report == {
        "month": "2016-07",
        "document_type": "emirates_cabin_crew_flight_log",
        "pages": 1, "source_rows": 2, "verified_rows": 2,
        "block_min": 390 + 405,
        "control": "all_source_rows_and_pages_verified",
    }


def test_watchdog_routes_emirates_log_by_document_content(tmp_path):
    name, legs, sims, report = logbook_watchdog._try_parsers(_pdf(tmp_path))
    assert name == "emirates_cabin_log"
    assert len(legs) == 2 and sims == []
    assert report["verified_rows"] == 2


def test_missing_airport_timezone_is_never_guessed(tmp_path):
    with pytest.raises(ValueError, match="Zeitzone"):
        parser.parse_pdf(_pdf(tmp_path, unknown=True))


def test_every_candidate_row_must_be_complete(tmp_path):
    with pytest.raises(ValueError, match="Flugzeile ist unvollstaendig"):
        parser.parse_pdf(_pdf(tmp_path, malformed=True))


def test_printed_date_must_equal_local_departure_date(tmp_path):
    with pytest.raises(ValueError, match="Datum widerspricht"):
        parser.parse_pdf(_pdf(tmp_path, date_mismatch=True))


def test_page_sequence_is_a_hard_control(tmp_path):
    with pytest.raises(ValueError, match="Seitennummern"):
        parser.parse_pdf(_pdf(tmp_path, page_number=2))


def test_filename_alone_is_not_a_format_signature(tmp_path):
    path = tmp_path / "Emirates_Cabin_Crew_Flight_Log.pdf"
    path.write_bytes(b"%PDF-not-really-the-export")
    assert parser.matches_pdf(path) is False
