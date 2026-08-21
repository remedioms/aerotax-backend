"""Strict FlightLog Pilot Logbook EASA parsing and watchdog routing."""

import os
import sys

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas
from reportlab.platypus import Table, TableStyle

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools", "logbook-parsers"))

import logbook_watchdog  # noqa: E402
import parse_flightlog_easa  # noqa: E402


def _fixture(path, printed_total="01:30"):
    pdf = canvas.Canvas(str(path), pagesize=landscape(A4))
    pdf.setFont("Helvetica", 12)
    pdf.drawString(300, 350, "F l i g h t L o g")
    pdf.drawString(300, 330, "P I L O T  L O G B O O K")
    pdf.drawString(260, 300, "Flights from 01 Jan 2025 through 02 Jan 2025")
    pdf.showPage()

    first = [
        "DATE", "DEPARTURE", "", "ARRIVAL", "", "AIRCRAFT", "",
        "SINGLE PILOT TIME", "", "MULTI PILOT TIME", "HELICOPTER",
        "TOTAL TIME", "NAME PIC", "T/O", "", "LNDGS", "",
        "PILOT FUNCTION TIME", "", "", "", "CONDITION OF FLIGHT", "",
        "SIMULATOR", "", "REMARKS AND ENDORSEMENTS",
    ]
    second = [
        "", "PLACE", "TIME", "PLACE", "TIME", "MODEL", "REG.", "SE",
        "ME", "", "", "", "", "DAY", "NIGHT", "DAY", "NIGHT",
        "PIC", "SIC", "DUAL", "INS", "NIGHT", "ACTUAL INST", "TYPE",
        "TIME", "",
    ]
    flight = [
        "01/01/25", "EDDF", "10:00", "EDDM", "11:30", "A320", "D-TEST",
        "", "", "01:30", "", "01:30", "", "1", "", "1", "", "",
        "01:30", "", "", "00:30", "01:30", "", "", "line check",
    ]
    simulator = ["" for _ in range(26)]
    simulator[0] = "02/01/25"
    simulator[23] = "FFS"
    simulator[24] = "04:00"

    def control(label, total):
        row = ["" for _ in range(26)]
        row[0] = label
        if total:
            row[9] = total
            row[11] = total
            row[13] = "1"
            row[15] = "1"
            row[18] = total
            row[21] = "00:30"
            row[22] = total
            row[24] = "04:00"
        return row

    rows = [first, second, flight, simulator,
            control("TOTAL THIS PAGE", printed_total),
            control("TOTAL PREVIOUS PAGES", None),
            control("TOTAL", printed_total)]
    widths = [42, 28, 28, 28, 28, 34, 34, 25, 25, 32, 32, 32, 34,
              21, 21, 21, 21, 24, 24, 24, 24, 27, 34, 27, 27, 48]
    table = Table(rows, colWidths=widths,
                  rowHeights=[24, 18, 18, 18, 20, 20, 20])
    spans = [
        ("SPAN", (0, 0), (0, 1)), ("SPAN", (1, 0), (2, 0)),
        ("SPAN", (3, 0), (4, 0)), ("SPAN", (5, 0), (6, 0)),
        ("SPAN", (7, 0), (8, 0)), ("SPAN", (9, 0), (9, 1)),
        ("SPAN", (10, 0), (10, 1)), ("SPAN", (11, 0), (11, 1)),
        ("SPAN", (12, 0), (12, 1)), ("SPAN", (13, 0), (14, 0)),
        ("SPAN", (15, 0), (16, 0)), ("SPAN", (17, 0), (20, 0)),
        ("SPAN", (21, 0), (22, 0)), ("SPAN", (23, 0), (24, 0)),
        ("SPAN", (25, 0), (25, 1)),
    ]
    table.setStyle(TableStyle(spans + [
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("FONT", (0, 0), (-1, -1), "Helvetica", 2.3),
        ("LEFTPADDING", (0, 0), (-1, -1), 0.5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0.5),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    table.wrapOn(pdf, 760, 500)
    table.drawOn(pdf, 45, 320)
    pdf.setFont("Helvetica", 6)
    pdf.drawRightString(805, 20, "Page 1 / 1")
    pdf.save()


def test_flightlog_reconciles_every_page_control_and_routes(tmp_path):
    path = tmp_path / "flightlog.pdf"
    _fixture(path)

    assert parse_flightlog_easa.matches_pdf(path)
    legs, sims, report = parse_flightlog_easa.parse_pdf(path)
    assert legs == [{
        "date": "2025-01-01", "from": "EDDF", "to": "EDDM",
        "block_min": 90, "_source_format": "flightlog_easa",
        "type": "A320", "reg": "D-TEST", "to_day": 1, "ldg_day": 1,
        "night_min": 30, "ifr_min": 90, "role": "FO",
        "remarks": "line check",
    }]
    assert sims == [{
        "date": "2025-01-02", "duration_min": 240, "code": "FFS",
        "_source_format": "flightlog_easa",
    }]
    assert report["control"] == "OK"
    assert report["totals"] == {
        "legs": 1, "block_min": 90, "landings": 1,
        "night_min": 30, "instrument_min": 90,
        "sim_sessions": 1, "sim_min": 240,
    }
    name, routed_legs, routed_sims, _ = logbook_watchdog._try_parsers(path)
    assert name == "flightlog_easa"
    assert routed_legs == legs and routed_sims == sims


def test_flightlog_rejects_changed_page_total(tmp_path):
    path = tmp_path / "flightlog-bad.pdf"
    _fixture(path, printed_total="01:31")
    try:
        parse_flightlog_easa.parse_pdf(path)
    except ValueError as exc:
        assert "page-total mismatch" in str(exc)
    else:
        raise AssertionError("changed FlightLog page total was accepted")
