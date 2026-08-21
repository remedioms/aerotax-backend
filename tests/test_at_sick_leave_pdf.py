"""Austrian sick-leave certificate roster-import regressions."""

import io
import os
import sys
from unittest.mock import patch

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as backend
from at_sick_leave_pdf import parse_at_sick_leave_calendar


TEXT = """ARBEITSUNFÄHIGKEITSMELDUNG
Versicherungsträger: Test
Arbeitsunfähig von: 23.01.2025
Letzter Tag der Arbeitsunfähigkeit: 24.01.2025
Grund der Arbeitsunfähigkeit:
Krankheit
23.01.2025 Ausstellungsdatum
"""


def _synthetic_pdf():
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream, pagesize=A4)
    y = 800
    for line in TEXT.splitlines():
        pdf.drawString(30, y, line)
        y -= 18
    pdf.save()
    return stream.getvalue()


def test_parser_imports_only_the_inclusive_sick_day_period():
    events, year, month, report, error = parse_at_sick_leave_calendar(TEXT)
    assert error is None
    assert (year, month) == (2025, 1)
    assert report == {
        "format": "at_sick_leave_certificate",
        "period": "2025-01-23..2025-01-24",
        "event_count": 2,
        "privacy": "dates_only",
    }
    assert [(event[1].isoformat(), event[3], event[4]) for event in events] == [
        ("2025-01-23", "Krank", True),
        ("2025-01-24", "Krank", True),
    ]


def test_parser_rejects_non_certificate_and_ambiguous_periods():
    assert parse_at_sick_leave_calendar("ordinary roster")[-1] == \
        "unsupported_pdf_format"
    assert parse_at_sick_leave_calendar(
        TEXT.replace("24.01.2025", "22.01.2025"))[-1] == \
        "at_sick_leave_invalid_period"


def test_parser_accepts_pdfplumber_column_reading_order():
    column_text = TEXT.replace(
        "Arbeitsunfähig von: 23.01.2025\n"
        "Letzter Tag der Arbeitsunfähigkeit: 24.01.2025",
        "Arbeitsunfähig von: Letzter Tag der Arbeitsunfähigkeit: Ausgehzeit:\n"
        "23.01.2025 24.01.2025 von -:00 Uhr bis -:00 Uhr",
    )
    events, _year, _month, report, error = \
        parse_at_sick_leave_calendar(column_text)
    assert error is None
    assert len(events) == 2
    assert report["period"] == "2025-01-23..2025-01-24"


def test_roster_endpoint_persists_sick_days_without_medical_details():
    token = "AT-TEST-SICK-CERTIFICATE-1"
    valid = backend._TokenValidationResult(
        backend._TokenValidationState.VALID, "sick@example.test")
    state = {"full": {}, "briefings": {}}

    def save_profile(_token, profile, full_disk_payload=None):
        state["full"] = dict(
            full_disk_payload or {"profile": dict(profile or {})})
        return True

    def save_briefings(_token, values):
        state["briefings"] = {
            day: dict(value) for day, value in (values or {}).items()
        }
        return True

    with patch.object(backend, "_validate_token", return_value=valid), \
            patch.object(backend, "_BUG004_REQUIRE_TOKEN_BINDING", False), \
            patch.object(backend, "_roster_pdf_upload_store", return_value=795), \
            patch.object(backend, "_roster_pdf_upload_finish", return_value=True), \
            patch.object(backend, "_profile_load", side_effect=lambda _token: state["full"]), \
            patch.object(
                backend, "_profile_load_from_disk",
                side_effect=lambda _token: state["full"]), \
            patch.object(backend, "_profile_save", side_effect=save_profile), \
            patch.object(
                backend, "_ical_briefings_load",
                side_effect=lambda _token: state["briefings"]), \
            patch.object(backend, "_ical_briefings_save", side_effect=save_briefings):
        response = backend.app.test_client().post(
            f"/api/user/roster-pdf/{token}/import",
            data={"airline": "Austrian", "homebase": "VIE",
                  "pdf": (io.BytesIO(_synthetic_pdf()), "sick.pdf")},
            content_type="multipart/form-data")

    payload = response.get_json() or {}
    assert response.status_code == 200, payload
    assert payload["events_count"] == 2
    assert payload["period"] == "2025-01-23..2025-01-24"
    assert set(state["briefings"]) == {"2025-01-23", "2025-01-24"}
    for value in state["briefings"].values():
        assert value["ical_summary"] == "Krank"
        serialized = str(value).casefold()
        assert "versicherung" not in serialized
        assert "krankheit" not in serialized
