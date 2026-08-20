"""Lufthansa City NetLine/Crew ground-duty roster regression tests."""

import io
import os
import sys
from unittest.mock import patch

import pdfplumber
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as backend
from lhx_netline_roster_pdf import parse_lhx_netline_calendar


def _synthetic_pdf():
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream, pagesize=landscape(A4))
    pdf.setFont("Helvetica", 7)
    pdf.drawString(30, 575, "Individual duty plan")
    pdf.drawString(300, 575, "NetLine/Crew(LHX) printed by PRINTIDP")
    pdf.drawString(30, 558, "Period: 01Nov25 - 03Nov25")

    # Same coordinate contract as the three real NetLine schedule columns.
    rows = (
        (28, "Sat01", "GC19", "MUC", "0800", "1600"),
        (295, "Sun02", "O", "MUC", None, None),
        (562, "Mon03", "CRM", "MUC", "0645", "1430"),
    )
    for x, day, duty, station, start, end in rows:
        pdf.drawString(x, 400, day)
        pdf.drawString(x + 42, 400, duty)
        pdf.drawString(x + 103, 400, station)
        if start:
            pdf.drawString(x + 125, 400, start)
            pdf.drawString(x + 146, 400, end)
    pdf.drawString(560, 250, "Flight time 0:00 Duty time 15:45")
    pdf.drawString(560, 240, "Off days 1")
    pdf.save()
    return stream.getvalue()


def _parse(data=None):
    data = data or _synthetic_pdf()
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    return parse_lhx_netline_calendar(data, text), text


def test_lhx_parses_all_ground_days_and_checks_month_totals():
    (events, year, month, report, error), _ = _parse()
    assert error is None
    assert (year, month) == (2025, 11)
    assert report == {
        "format": "lhx_netline_ground_duty_plan",
        "period": "2025-11-01..2025-11-03",
        "timescale": "Europe/Berlin",
        "event_count": 3,
        "duty_count": 2,
        "off_count": 1,
        "duty_minutes": 945,
    }
    ics = backend._pdf_events_to_ics(events, year, month)
    # November MUC local time is UTC+1: 08:00 local is persisted as 07:00Z.
    assert "DTSTART:20251101T070000Z" in ics
    assert "SUMMARY:GC19 · MUC" in ics
    assert "SUMMARY:CRM · MUC" in ics
    assert "SUMMARY:Off Day" in ics


def test_lhx_rejects_foreign_flight_and_checksum_changes():
    assert parse_lhx_netline_calendar(
        _synthetic_pdf(), "some other roster")[-1] == "unsupported_pdf_format"
    (_, text) = _parse()
    assert parse_lhx_netline_calendar(
        _synthetic_pdf(), text.replace("Flight time 0:00", "Flight time 1:00")
    )[-1] == "lhx_flight_plan_not_supported"
    assert parse_lhx_netline_calendar(
        _synthetic_pdf(), text.replace("Duty time 15:45", "Duty time 15:44")
    )[-1] == "lhx_duty_checksum_mismatch"


def test_roster_pdf_endpoint_dispatches_lhx_parser():
    token = "AT-TEST-LHX-NETLINE-1"
    valid = backend._TokenValidationResult(
        backend._TokenValidationState.VALID, "lhx@example.test")
    saved = {}

    with patch.object(backend, "_validate_token", return_value=valid), \
            patch.object(backend, "_BUG004_REQUIRE_TOKEN_BINDING", False), \
            patch.object(backend, "_roster_pdf_upload_store", return_value=684), \
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
            data={"airline": "Lufthansa City", "homebase": "MUC",
                  "pdf": (io.BytesIO(_synthetic_pdf()), "roster.pdf")},
            content_type="multipart/form-data")

    payload = response.get_json() or {}
    assert response.status_code == 200, payload
    assert payload.get("ok") is True
    assert payload.get("period") == "2025-11-01..2025-11-03"
    stored = saved["profile"]["calendar_feed"]["events"]
    assert len(stored) == 3
    assert any(event.get("summary") == "GC19 · MUC" for event in stored)
