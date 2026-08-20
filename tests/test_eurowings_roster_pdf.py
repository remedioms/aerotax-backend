"""Eurowings NetLine/Crew roster -> deterministic calendar events."""

import io
import os
import sys
from unittest.mock import patch

import pdfplumber
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as backend
from eurowings_roster_pdf import parse_eurowings_netline_calendar


def _synthetic_pdf():
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream, pagesize=landscape(A4))
    pdf.setFont("Helvetica", 7)
    pdf.drawString(30, 575, "Individual duty plan")
    pdf.drawString(300, 575, "NetLine/Crew(EWG) printed by CREWLINK")
    pdf.drawString(30, 558, "Period: 01Aug26 - 03Aug26")

    days = ((37, "Sat01", "FlD", "1000", "1400"),
            (85, "Sun02", "Off", None, None),
            (133, "Mon03", "Sby", "0830", "1630"))
    for x, day, status, start, end in days:
        pdf.drawString(x, 530, day)
        pdf.drawString(x + 2, 519, status)
        if start:
            pdf.drawString(x + 2, 511, start)
            pdf.drawString(x + 2, 505, end)

    pdf.drawString(31, 465, "Sat01")
    pdf.drawString(73, 455, "EW 100 DUS 1100 1300 PMI 320")
    pdf.drawString(300, 300, "Flight time 02:00 Duty time 04:00")
    pdf.save()
    return stream.getvalue()


def _parse(data=None):
    data = data or _synthetic_pdf()
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    return parse_eurowings_netline_calendar(data, text), text


def test_eurowings_parses_checked_flight_and_complete_month_strip():
    (events, year, month, report, error), _ = _parse()
    assert error is None
    assert (year, month) == (2026, 8)
    assert report == {
        "format": "eurowings_netline_individual_duty_plan",
        "period": "2026-08-01..2026-08-03",
        "timescale": "UTC",
        "flight_count": 1,
        "marker_count": 2,
        "block_minutes": 120,
    }
    ics = backend._pdf_events_to_ics(events, year, month)
    assert "10:00 UTC Briefing DUS · EW100 DUS - PMI" in ics
    assert "DTSTART:20260801T110000Z" in ics
    assert "SUMMARY:Off Day" in ics
    assert "SUMMARY:Standby" in ics
    assert "DTSTART:20260803T083000Z" in ics


def test_eurowings_passes_calendar_display_contract():
    (events, year, month, _, error), _ = _parse()
    assert error is None
    ics = backend._pdf_events_to_ics(events, year, month)
    report = backend._airline_display_contract(
        backend._parse_ics_to_events(ics))
    assert report["ok"] is True
    assert report["sector_count"] == 1
    assert report["flight_days"] == 1


def test_eurowings_rejects_foreign_and_checksum_changes():
    assert parse_eurowings_netline_calendar(
        _synthetic_pdf(), "some other roster")[-1] == "unsupported_pdf_format"
    (result, text) = _parse()
    assert result[-1] is None
    changed = text.replace("Flight time 02:00", "Flight time 02:01")
    assert parse_eurowings_netline_calendar(
        _synthetic_pdf(), changed)[-1] == "eurowings_flight_checksum_mismatch"


def test_roster_pdf_endpoint_dispatches_eurowings_parser():
    token = "AT-TEST-EUROWINGS-PDF-1"
    valid = backend._TokenValidationResult(
        backend._TokenValidationState.VALID, "ew@example.test")
    saved = {}

    with patch.object(backend, "_validate_token", return_value=valid), \
            patch.object(backend, "_BUG004_REQUIRE_TOKEN_BINDING", False), \
            patch.object(backend, "_roster_pdf_upload_store", return_value=744), \
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
                return_value={"feed_dates": 3, "cleared": 0,
                              "removed_dates": [], "window": "test"}):
        response = backend.app.test_client().post(
            f"/api/user/roster-pdf/{token}/import",
            data={"airline": "Eurowings", "homebase": "DUS",
                  "pdf": (io.BytesIO(_synthetic_pdf()), "roster.pdf")},
            content_type="multipart/form-data")

    payload = response.get_json() or {}
    assert response.status_code == 200, payload
    assert payload.get("ok") is True
    assert payload.get("period") == "2026-08-01..2026-08-03"
    stored = saved["profile"]["calendar_feed"]["events"]
    assert any("EW100 DUS - PMI" in event.get("summary", "")
               for event in stored)
