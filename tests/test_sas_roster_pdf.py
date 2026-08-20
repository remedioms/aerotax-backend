"""SAS Airside roster export -> deterministic calendar events."""

import io
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as backend
from sas_roster_pdf import parse_sas_airside_calendar


SYN_TEXT = """Airside Roster Export | Exported Date: August 19, 2026, 16:04
Day Date Duty Meal Activity C/I From STD STA ATD ATA PIC To C/O Stop
time
Thu 01JAN26 F14 00:00 23:59 00:00
Fri 02JAN26 VA 00:00 23:59 00:00
Mon 05JAN26 checkin 11:45 11:45 11:45 00:00
Mon 05JAN26 SK0927 CPH 13:15 15:50 13:29 16:51 25731 BOS 25:45
Tue 06JAN26 SK0928 BOS 17:35 07:00 18:31 07:25 25731 CPH 103:30
Wed 07JAN26 checkout 07:55 07:55 07:55 00:00
Mon 20APR26 R2 04:20 14:20 00:00
Tue 21APR26 P X SK1466 CPH 13:45 14:55 13:45 14:50 99752 OSL 03:05
Tue 21APR26 X SK0461 OSL 18:00 19:10 17:55 18:58 23642 CPH 90:15
Page 1 of 1
Note: Private: This document is intended for the designated recipient. For assistance, contact Airside Support: airside@sas.se
"""


def _calendar(text=SYN_TEXT, homebase=None):
    events, year, month, report, error = parse_sas_airside_calendar(
        text, homebase=homebase)
    assert error is None, error
    return backend._pdf_events_to_ics(
        events, year, month, prodid='AeroX SAS Test'), report


def test_sas_airside_flights_use_station_local_scheduled_times():
    ics, report = _calendar()
    assert report['format'] == 'sas_airside_roster'
    assert report['homebase'] == 'CPH'
    assert report['period'] == '2026-01-01..2026-04-21'
    assert report['flight_count'] == 4

    # Printed leading zero is normalized for the flight-data pipeline.
    assert 'SK927 CPH - BOS' in ics
    assert 'SK0927' not in ics
    # Check-in is an exact printed local briefing, not a guessed offset.
    assert '11:45 LT Briefing CPH · SK927 CPH - BOS' in ics
    # CPH 13:15 CET = 12:15Z. DTSTART retains the authoritative local station.
    assert 'DTSTART;TZID=Europe/Copenhagen:20260105T131500' in ics
    # BOS 15:50 EST = 20:50Z on the same day.
    assert 'DTEND:20260105T205000Z' in ics
    # BOS 17:35 EST -> CPH 07:00 CET on the following date.
    assert 'DTSTART;TZID=America/New_York:20260106T173500' in ics
    assert 'DTEND:20260107T060000Z' in ics


def test_sas_airside_markers_and_timed_ground_duties():
    ics, report = _calendar(homebase='CPH')
    assert report['marker_count'] == 3
    assert 'SUMMARY:Off Day' in ics
    assert 'SUMMARY:Urlaub' in ics
    assert 'SUMMARY:Reserve' in ics
    assert 'DTSTART;TZID=Europe/Copenhagen:20260420T042000' in ics
    assert 'DTEND:20260420T122000Z' in ics


def test_sas_airside_optional_duty_and_meal_columns_before_flight():
    ics, _ = _calendar()
    assert 'SK1466 CPH - OSL' in ics
    assert 'SK461 OSL - CPH' in ics


def test_sas_airside_passes_the_new_airline_calendar_display_contract():
    ics, _ = _calendar()
    events = backend._parse_ics_to_events(ics)

    report = backend._airline_display_contract(events)

    assert report['ok'] is True
    assert report['version'] == 'calendar-v1'
    assert report['flight_days'] == 3
    assert report['sector_count'] == 4


def test_sas_airside_rejects_foreign_and_changed_layouts():
    result = parse_sas_airside_calendar('Irgendein anderer Dienstplan')
    assert result[-1] == 'unsupported_pdf_format'

    changed = SYN_TEXT.replace(
        'Mon 05JAN26 SK0927 CPH 13:15 15:50 13:29 16:51 25731 BOS 25:45',
        'Mon 05JAN26 SK0927 CPH UNKNOWN ROW')
    assert parse_sas_airside_calendar(changed)[-1] == \
        'sas_malformed_flight_row'


def test_sas_airside_rejects_wrong_weekday_instead_of_moving_date():
    changed = SYN_TEXT.replace('Thu 01JAN26', 'Fri 01JAN26')
    assert parse_sas_airside_calendar(changed)[-1] == 'sas_weekday_mismatch'


def test_sas_airside_rejects_unmapped_dated_row():
    changed = SYN_TEXT.replace(
        'Mon 20APR26 R2 04:20 14:20 00:00',
        'Mon 20APR26 text with a changed column layout')
    assert parse_sas_airside_calendar(changed)[-1] == 'sas_unparsed_row'


def _synthetic_pdf():
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.pdfgen import canvas

    stream = io.BytesIO()
    pdf = canvas.Canvas(stream, pagesize=landscape(A4))
    pdf.setFont('Helvetica', 6)
    y = 570
    for line in SYN_TEXT.splitlines():
        pdf.drawString(20, y, line)
        y -= 12
    pdf.save()
    return stream.getvalue()


def test_roster_pdf_endpoint_dispatches_sas_parser():
    token = 'AT-TEST-SAS-PDF-1'
    valid = backend._TokenValidationResult(
        backend._TokenValidationState.VALID, 'sas@example.test')
    saved = {}

    with patch.object(backend, '_validate_token', return_value=valid), \
            patch.object(backend, '_BUG004_REQUIRE_TOKEN_BINDING', False), \
            patch.object(backend, '_roster_pdf_upload_store', return_value=650), \
            patch.object(backend, '_roster_pdf_upload_finish', return_value=True), \
            patch.object(backend, '_profile_load', return_value={}), \
            patch.object(backend, '_profile_load_from_disk', return_value={}), \
            patch.object(
                backend, '_profile_save',
                side_effect=lambda _token, profile, full_disk_payload=None:
                saved.update({'profile': profile}) or True), \
            patch.object(backend, '_ical_briefings_load', return_value={}), \
            patch.object(backend, '_ical_briefings_save', return_value=True), \
            patch.object(
                backend, '_reconcile_month_briefings',
                return_value={'feed_dates': 6, 'cleared': 0,
                              'removed_dates': [], 'window': 'test'}):
        response = backend.app.test_client().post(
            f'/api/user/roster-pdf/{token}/import',
            data={'airline': 'SAS', 'homebase': 'CPH',
                  'pdf': (io.BytesIO(_synthetic_pdf()), 'roster.pdf')},
            content_type='multipart/form-data')

    payload = response.get_json() or {}
    assert response.status_code == 200, payload
    assert payload.get('ok') is True
    assert payload.get('source') == 'pdf'
    assert payload.get('period') == '2026-01-01..2026-04-21'
    stored_events = saved['profile']['calendar_feed']['events']
    assert any('SK927 CPH - BOS' in event.get('summary', '')
               for event in stored_events)
