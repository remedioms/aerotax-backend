"""Lufthansa City GC Initial/OCC/TYPE training-plan regression tests."""

import io
import os
import sys
from datetime import date, timedelta
from unittest.mock import patch

from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as backend
from lhx_training_schedule_pdf import parse_lhx_training_calendar


WEEKDAYS = (
    "Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag",
    "Samstag", "Sonntag",
)


def _day(pdf, x, day, activity, time_range=None, location=None,
         category=None, print_date=True):
    pdf.setFont("Helvetica-Bold", 8)
    header = WEEKDAYS[day.weekday()] + ("," if print_date else "")
    pdf.drawString(x, 500, header)
    if print_date:
        pdf.drawString(
            x + pdf.stringWidth(header, "Helvetica-Bold", 8) + 4,
            500, day.strftime("%d.%m.%Y"))
    pdf.setFont("Helvetica", 8)
    pdf.drawString(x, 489, activity)
    if time_range:
        pdf.drawString(x, 478, time_range + " Uhr")
    if location:
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawString(x, 458, location)
    if category:
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawString(x, 438, category)


def _synthetic_pdf(second_week_start=date(2026, 8, 10)):
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream, pagesize=landscape(A4))
    pdf.setFont("Helvetica", 11)
    pdf.drawString(36, 550, "Übersicht GC Initial, OCC und TYPE Cabin Crews")
    pdf.drawString(620, 550, "Lufthansa City Airlines")
    pdf.showPage()

    starts = (date(2026, 8, 3), second_week_start)
    for week_index, monday in enumerate(starts):
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(36, 530, "Kursplan LHX - INI / OCC")
        for index in range(7):
            day = monday + timedelta(days=index)
            x = 40 + index * 113
            if index in (5, 6):
                _day(pdf, x, day, "OFF", print_date=False)
            elif index == 0 and week_index == 0:
                _day(pdf, x, day, "OFF", print_date=False)
            elif index in (1, 2, 3):
                _day(pdf, x, day, f"Tag {week_index * 3 + index}",
                     "09:00-17:00",
                     "MUC Schwaig TC", "CRM")
            else:
                _day(pdf, x, day, "Home Study Day", print_date=True)
        pdf.showPage()
    pdf.save()
    return stream.getvalue()


def _parse(data):
    import pdfplumber
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    return parse_lhx_training_calendar(data, text)


def test_lhx_training_matrix_builds_local_training_and_all_day_events():
    events, year, month, report, error = _parse(_synthetic_pdf())
    assert error is None
    assert (year, month) == (2026, 8)
    assert report == {
        "format": "lhx_cabin_initial_training_schedule",
        "period": "2026-08-03..2026-08-16",
        "timescale": "Europe/Berlin",
        "event_count": 14,
        "timed_count": 6,
        "off_count": 5,
        "home_study_count": 3,
    }
    ics = backend._pdf_events_to_ics(events, year, month)
    assert "DTSTART:20260804T070000Z" in ics
    assert "SUMMARY:Tag 1 · CRM" in ics
    assert "LOCATION:MUC Schwaig TC" in ics
    assert "DTSTART;VALUE=DATE:20260803" in ics
    assert "SUMMARY:Off Day" in ics


def test_lhx_training_rejects_foreign_and_non_contiguous_week_matrix():
    data = _synthetic_pdf()
    assert parse_lhx_training_calendar(data, "some other document")[-1] == \
        "unsupported_pdf_format"
    broken = _synthetic_pdf(second_week_start=date(2026, 8, 17))
    assert _parse(broken)[-1] == "lhx_training_date_strip_mismatch"


def test_roster_pdf_endpoint_dispatches_training_schedule_parser():
    token = "AT-TEST-LHX-TRAINING-1"
    valid = backend._TokenValidationResult(
        backend._TokenValidationState.VALID, "training@example.test")
    saved = {}

    with patch.object(backend, "_validate_token", return_value=valid), \
            patch.object(backend, "_BUG004_REQUIRE_TOKEN_BINDING", False), \
            patch.object(backend, "_roster_pdf_upload_store", return_value=776), \
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
                return_value={"feed_dates": 14, "cleared": 0,
                              "removed_dates": [], "window": "test"}):
        response = backend.app.test_client().post(
            f"/api/user/roster-pdf/{token}/import",
            data={"airline": "Lufthansa City", "homebase": "MUC",
                  "pdf": (io.BytesIO(_synthetic_pdf()), "training.pdf")},
            content_type="multipart/form-data")

    payload = response.get_json() or {}
    assert response.status_code == 200, payload
    assert payload.get("ok") is True
    assert payload.get("period") == "2026-08-03..2026-08-16"
    stored = saved["profile"]["calendar_feed"]["events"]
    assert len(stored) == 14
    assert any(event.get("summary") == "Tag 1 · CRM" for event in stored)
    assert any(event.get("summary") == "Home Study Day" for event in stored)
