"""Strict parsing and watchdog routing for ForeFlight's EASA PDF export."""

import io
import os
import sys

from reportlab.lib.pagesizes import landscape, letter
from reportlab.pdfgen import canvas

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools", "logbook-parsers"))

import logbook_watchdog  # noqa: E402
import parse_foreflight_easa  # noqa: E402


def _draw_text(c, x, y, value):
    c.drawString(x, y, value)


def _fixture_pdf(path, printed_total="1:00"):
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=landscape(letter))
    c.setFont("Helvetica", 6)
    _draw_text(c, 20, 570, "Pilot Logbook | Test Pilot")
    _draw_text(c, 20, 550, "01 Jan 2025 - 02 Jan 2025")
    c.showPage()
    _draw_text(c, 20, 570, "Endorsements")
    c.showPage()
    _draw_text(c, 20, 570, "Certificates")
    c.showPage()

    # A side. Two dated rows: one simulator-only record, then one flight.
    c.setFont("Helvetica", 6)
    _draw_text(c, 397, 585, "TYPE OF PILOTING TIME")
    _draw_text(c, 626, 585, "CATEGORY / CLASS")
    _draw_text(c, 730, 585, "LANDINGS")
    _draw_text(c, 22, 550, "01/01/25")
    _draw_text(c, 55, 550, "SIM-A (A320)")
    _draw_text(c, 119, 550, "No Dept - No Dest")
    _draw_text(c, 22, 528, "02/01/25")
    _draw_text(c, 55, 528, "D-TEST (A320)")
    _draw_text(c, 119, 528, "EDDF - EDDM")
    _draw_text(c, 250, 528, "1:00")
    _draw_text(c, 310, 528, "1:00")
    _draw_text(c, 340, 528, "1:00")
    _draw_text(c, 400, 528, "0:10")
    _draw_text(c, 736, 528, "1")
    _draw_text(c, 119, 490, "TOTALS THIS PAGE")
    _draw_text(c, 250, 490, printed_total)
    _draw_text(c, 736, 490, "1")
    _draw_text(c, 119, 480, "TOTALS TO DATE")
    _draw_text(c, 250, 480, printed_total)
    _draw_text(c, 736, 480, "1")
    c.showPage()

    # B side rows are printed 8.2pt higher than their A-side partners.
    c.setFont("Helvetica", 6)
    _draw_text(c, 50, 585, "INSTRUMENT TRAINING")
    _draw_text(c, 460, 575, "Additional Comments and Remarks")
    _draw_text(c, 226, 558.2, "4:00")
    _draw_text(c, 35, 536.2, "1:00")
    _draw_text(c, 226, 498.2, "4:00")
    _draw_text(c, 226, 488.2, "4:00")
    c.save()
    path.write_bytes(buffer.getvalue())


def test_foreflight_easa_pdf_reconciles_flights_landings_and_sim(tmp_path):
    path = tmp_path / "foreflight.pdf"
    _fixture_pdf(path)
    assert parse_foreflight_easa.matches_pdf(path)
    legs, sims, report = parse_foreflight_easa.parse_pdf(path)
    assert legs == [{
        "date": "2025-01-02", "from": "EDDF", "to": "EDDM",
        "block_min": 60, "_source_format": "foreflight_easa",
        "reg": "D-TEST", "type": "A320", "role": "FO",
        "night_min": 10, "ifr_min": 60, "ldg_day": 1,
    }]
    assert sims == [{
        "date": "2025-01-01", "duration_min": 240, "code": "A320",
        "_source_format": "foreflight_easa",
    }]
    assert report["control"] == "OK"
    assert report["totals"] == {
        "legs": 1, "block_min": 60, "landings": 1,
        "sim_sessions": 1, "sim_min": 240,
    }
    name, routed_legs, routed_sims, _ = logbook_watchdog._try_parsers(path)
    assert name == "foreflight_easa"
    assert routed_legs == legs and routed_sims == sims


def test_foreflight_easa_pdf_rejects_wrong_printed_total(tmp_path):
    path = tmp_path / "foreflight-bad.pdf"
    _fixture_pdf(path, printed_total="1:01")
    try:
        parse_foreflight_easa.parse_pdf(path)
    except ValueError as exc:
        assert "flight-total mismatch" in str(exc)
    else:
        raise AssertionError("mismatched cumulative control was accepted")
