"""Virgin Atlantic flight-based roster -> deterministic calendar events."""

import io
import os
import sys
from unittest.mock import patch

import pdfplumber
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as backend
from virgin_atlantic_roster_pdf import parse_virgin_atlantic_calendar


def _synthetic_pdf():
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream, pagesize=landscape(A4))
    pdf.setFont("Helvetica", 7)
    pdf.drawString(30, 575, "Roster Report (flight based)")
    pdf.drawString(30, 563, "Planning Period 01 - 05Sep2026")
    pdf.drawString(30, 551, "Rule Set Rostering_Cabin")

    for index, day in enumerate(range(1, 6)):
        pdf.drawString(145 + index * 30, 465, str(day))
    pdf.drawString(139, 450, "LVE")
    pdf.drawString(169, 450, "RDO")

    pdf.drawString(30, 420, "FLIGHT DETAILS")
    pdf.drawString(30, 405, "Briefing (10:00)09:00 02:00")
    pdf.drawString(
        30, 393,
        "3 VS 100 LHR CDG (11:00)10:00 (12:00)11:00 01:00 00:00 789")
    pdf.drawString(30, 375, "Briefing (13:00)12:00 01:20")
    pdf.drawString(
        30, 363,
        "5 VS 101 CDG LHR (14:00)13:00 (15:00)14:00 01:00 00:00 789")
    pdf.drawString(30, 330, "Flight duty this month 02:00")
    pdf.drawString(30, 318, "Days off this month 2")
    pdf.save()
    return stream.getvalue()


def _parse(data=None):
    data = data or _synthetic_pdf()
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    return parse_virgin_atlantic_calendar(data, text), text


def test_virgin_parses_utc_legs_and_checked_ground_markers():
    (events, year, month, report, error), _ = _parse()
    assert error is None
    assert (year, month) == (2026, 9)
    assert report == {
        "format": "virgin_atlantic_flight_based_roster",
        "period": "2026-09-01..2026-09-05",
        "timescale": "UTC",
        "flight_count": 2,
        "marker_count": 2,
        "block_minutes": 120,
    }
    ics = backend._pdf_events_to_ics(events, year, month)
    assert "10:00 LT Briefing LHR · VS100 LHR - CDG" in ics
    assert "DTSTART:20260903T100000Z" in ics
    assert "SUMMARY:Urlaub" in ics
    assert "SUMMARY:Off Day" in ics


def test_virgin_passes_calendar_display_contract():
    (events, year, month, _, error), _ = _parse()
    assert error is None
    ics = backend._pdf_events_to_ics(events, year, month)
    report = backend._airline_display_contract(
        backend._parse_ics_to_events(ics))
    assert report["ok"] is True
    assert report["sector_count"] == 2
    assert report["flight_days"] == 2


def test_virgin_rejects_foreign_and_checksum_changes():
    assert parse_virgin_atlantic_calendar(
        _synthetic_pdf(), "some other roster")[-1] == "unsupported_pdf_format"
    (result, text) = _parse()
    assert result[-1] is None
    changed = text.replace("Flight duty this month 02:00",
                           "Flight duty this month 02:01")
    assert parse_virgin_atlantic_calendar(
        _synthetic_pdf(), changed)[-1] == "virgin_flight_checksum_mismatch"


def test_roster_pdf_endpoint_dispatches_virgin_parser():
    token = "AT-TEST-VIRGIN-PDF-1"
    valid = backend._TokenValidationResult(
        backend._TokenValidationState.VALID, "vs@example.test")
    saved = {}

    with patch.object(backend, "_validate_token", return_value=valid), \
            patch.object(backend, "_BUG004_REQUIRE_TOKEN_BINDING", False), \
            patch.object(backend, "_roster_pdf_upload_store", return_value=746), \
            patch.object(backend, "_roster_pdf_upload_finish", return_value=True), \
            patch.object(backend, "_profile_load", return_value={}), \
            patch.object(backend, "_profile_load_from_disk", return_value={}), \
            patch.object(
                backend, "_profile_save",
                side_effect=lambda _token, profile, full_disk_payload=None:
                saved.update({"profile": profile}) or True), \
            patch.object(backend, "_ical_briefings_load", return_value={}), \
            patch.object(backend, "_ical_briefings_save", return_value=True), \
            patch.object(
                backend, "_reconcile_month_briefings",
                return_value={"feed_dates": 5, "cleared": 0,
                              "removed_dates": [], "window": "test"}):
        response = backend.app.test_client().post(
            f"/api/user/roster-pdf/{token}/import",
            data={"airline": "Virgin Atlantic", "homebase": "LHR",
                  "pdf": (io.BytesIO(_synthetic_pdf()), "roster.pdf")},
            content_type="multipart/form-data")

    payload = response.get_json() or {}
    assert response.status_code == 200, payload
    assert payload.get("ok") is True
    assert payload.get("period") == "2026-09-01..2026-09-05"
    stored = saved["profile"]["calendar_feed"]["events"]
    assert any("VS100 LHR - CDG" in event.get("summary", "")
               for event in stored)
