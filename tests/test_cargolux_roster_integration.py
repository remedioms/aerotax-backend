"""End-to-end contracts for the Cargolux PDF import path."""

import io
from datetime import date, datetime
from unittest.mock import patch

import app as backend


def test_cargolux_mixed_timed_and_all_day_events_serialize():
    events = [
        ("leg-20260902-6293-1", datetime(2026, 9, 2, 11, 20),
         datetime(2026, 9, 2, 22, 55),
         "10:10 UTC Briefing LUX · CV6293 LUX - LAX", False),
        ("layover-20260903-LAX", date(2026, 9, 3), date(2026, 9, 4),
         "LAYOVER", True, None, "LAX"),
        ("off-20260909", date(2026, 9, 9), date(2026, 9, 10),
         "Off Day", True),
    ]

    ics = backend._pdf_events_to_ics(
        events, 2026, 9, prodid="AeroX Cargolux Roster PDF Import")
    parsed = backend._parse_ics_to_events(ics, token="tok-cargolux-test")

    assert len(parsed) == 3
    assert any(event.get("summary") == "CV6293 LUX - LAX"
               or "CV6293 LUX - LAX" in event.get("summary", "")
               for event in parsed)
    assert any(event.get("summary") == "LAYOVER"
               and event.get("location") == "LAX" for event in parsed)
    assert any(event.get("summary") == "Off Day" for event in parsed)


def _synthetic_cargolux_pdf():
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.pdfgen import canvas

    stream = io.BytesIO()
    pdf = canvas.Canvas(stream, pagesize=landscape(A4))
    pdf.drawString(20, 560, "CARGOLUX")
    pdf.drawString(300, 560, "Personal Crew Schedule Report")
    pdf.drawString(300, 535, "01/09/2026 - 30/09/2026 (All times in UTC)")
    pdf.drawString(20, 490, "Schedule Details")
    pdf.setFont("Helvetica", 7)
    for x, label in ((20, "Date"), (90, "Duties"), (170, "Details"),
                     (285, "Report times"), (355, "Actual times/Delays"),
                     (470, "Debrief times"), (550, "Credits"),
                     (615, "Indicators"), (670, "Crew")):
        pdf.drawString(x, 465, label)
    for x, value in ((20, "02/09/2026"), (66, "Wed"), (90, "6293"),
                     (170, "LUX - LAX"), (285, "10:10"),
                     (355, "11:20 - 22:55"), (470, "23:25"),
                     (550, "13:15"), (615, "s"),
                     (670, "PRIVATE SYNTHETIC CREW")):
        pdf.drawString(x, 435, value)
    for x, value in ((20, "03/09/2026"), (66, "Thu"), (90, "LAX")):
        pdf.drawString(x, 405, value)
    pdf.drawString(20, 60, "Generated on synthetic test")
    pdf.save()
    return stream.getvalue()


def test_roster_pdf_endpoint_dispatches_cargolux_parser_without_crew_data():
    token = "AT-TEST-CARGOLUX-PDF-1"
    valid = backend._TokenValidationResult(
        backend._TokenValidationState.VALID, "cargolux@example.test")
    saved = {}

    with patch.object(backend, "_validate_token", return_value=valid), \
            patch.object(backend, "_BUG004_REQUIRE_TOKEN_BINDING", False), \
            patch.object(backend, "_roster_pdf_upload_store", return_value=991), \
            patch.object(backend, "_roster_pdf_upload_finish", return_value=True), \
            patch.object(backend, "_profile_load", return_value={}), \
            patch.object(backend, "_profile_load_from_disk", return_value={}), \
            patch.object(backend, "_profile_save",
                         side_effect=lambda _t, profile, full_disk_payload=None:
                         saved.update({"profile": profile}) or True), \
            patch.object(backend, "_ical_briefings_load", return_value={}), \
            patch.object(backend, "_ical_briefings_save", return_value=True), \
            patch.object(backend, "_reconcile_month_briefings",
                         return_value={"feed_dates": 2, "cleared": 0,
                                       "removed_dates": [], "window": "test"}):
        response = backend.app.test_client().post(
            f"/api/user/roster-pdf/{token}/import",
            data={"airline": "Cargolux", "homebase": "DUS",
                  "pdf": (io.BytesIO(_synthetic_cargolux_pdf()), "roster.pdf")},
            content_type="multipart/form-data",
        )

    payload = response.get_json() or {}
    assert response.status_code == 200, payload
    assert payload.get("ok") is True
    assert payload.get("source") == "pdf"
    assert payload.get("events_count") == 2
    stored_events = saved["profile"]["calendar_feed"]["events"]
    serialized = repr(stored_events)
    assert "CV6293 LUX - LAX" in serialized
    assert "PRIVATE SYNTHETIC CREW" not in serialized
