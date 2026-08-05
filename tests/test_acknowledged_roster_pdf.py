"""Lufthansa Jeppesen Acknowledged-Roster parser regressions."""

import io
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as backend


HEADER = (
    'Acknowledged Roster\n'
    'Month: January 2026\n'
    'Company Name: LH\n'
    'Date (LT) Trip ID Report (LT) Pos Activity From To Start (UTC) End (UTC) '
    'A/C Layover\n'
)


def events(body):
    ics, error = backend._acknowledged_roster_text_to_ics(HEADER + body)
    assert error is None
    return ics


def test_complete_leg_uses_utc_times_and_local_report_token():
    ics = events(
        '11 Sun 77187 11:50 AC 470 FRA YYZ 12:59 21:38 78S\n'
        'Created 05Aug2026 11:45 (UTC) by Jeppesen\n',
    )
    assert 'DTSTART:20260111T125900Z' in ics
    assert 'DTEND:20260111T213800Z' in ics
    assert 'SUMMARY:11:50 LT Briefing FRA · LH470 FRA - YYZ' in ics


def test_split_leg_honours_explicit_utc_day_marker():
    ics = events(
        '16 Fri 77364 11:00 FO 752 FRA 11:57 78Q\n'
        '17 Sat FO 752 HYD (16) 20:21 78Q\n'
        'Created 05Aug2026 11:45 (UTC) by Jeppesen\n',
    )
    assert 'DTSTART:20260116T115700Z' in ics
    assert 'DTEND:20260116T202100Z' in ics
    assert 'LH752 FRA - HYD' in ics


def test_previous_utc_day_on_complete_outstation_leg():
    ics = events(
        '18 Sun 02:40 FO 753 HYD FRA (17) 22:13 07:52 78Q\n'
        'Created 05Aug2026 11:45 (UTC) by Jeppesen\n',
    )
    assert 'DTSTART:20260117T221300Z' in ics
    assert 'DTEND:20260118T075200Z' in ics


def test_explicit_next_utc_day_before_end_time():
    ics = events(
        '02 Fri 89956 12:05 SF 542 FRA BOG 13:20 (03) 01:13 78Q\n'
        'Created 05Aug2026 11:45 (UTC) by Jeppesen\n',
    )
    assert 'DTSTART:20260102T132000Z' in ics
    assert 'DTEND:20260103T011300Z' in ics


def test_ground_off_leave_and_layover_are_preserved():
    ics = events(
        '01 Thu DT_GC FRA 09:00 15:00\n'
        '02 Fri FREE 00:00 00:00 ORTSTAG\n'
        '03 Sat U\n'
        '04 Sun Layover: YYZ\n'
        'Created 05Aug2026 11:45 (UTC) by Jeppesen\n',
    )
    assert 'SUMMARY:DT_GC FRA' in ics
    assert 'SUMMARY:Off Day' in ics
    assert 'SUMMARY:Urlaub' in ics
    assert 'SUMMARY:Layover YYZ' in ics
    assert ics.count('BEGIN:VEVENT') == 4


def test_unclosed_split_leg_is_rejected():
    text = HEADER + (
        '16 Fri 77364 11:00 FO 752 FRA 11:57 78Q\n'
        'Created 05Aug2026 11:45 (UTC) by Jeppesen\n'
    )
    ics, error = backend._acknowledged_roster_text_to_ics(text)
    assert ics is None
    assert error == 'unclosed_split_leg'


def test_non_lufthansa_foreign_pdf_is_not_claimed():
    text = HEADER.replace('Company Name: LH', 'Company Name: XX')
    ics, error = backend._acknowledged_roster_text_to_ics(text)
    assert ics is None
    assert error == 'unsupported_pdf_format'


def test_merge_existing_drops_ack_events_inside_active_roster_span():
    """A historical ACK archive must not duplicate the active roster."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    text = HEADER + (
        '11 Sun 77187 11:50 AC 470 FRA YYZ 12:59 21:38 78S\n'
        'Created 05Aug2026 11:45 (UTC) by Jeppesen\n'
        'Month: July 2026\n'
        'Company Name: LH\n'
        'Date (LT) Trip ID Report (LT) Pos Activity From To Start (UTC) End (UTC) '
        'A/C Layover\n'
        '04 Sat 88123 08:00 AC 100 FRA MUC 09:00 10:00 78S\n'
        'Created 05Aug2026 11:45 (UTC) by Jeppesen\n'
    )
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=A4)
    y = 820
    for line in text.splitlines():
        pdf.drawString(30, y, line)
        y -= 12
    pdf.save()

    token = 'AT-TEST-ACK-ARCHIVE-1'
    for path in (
        backend._user_profile_path(token),
        os.path.join(backend._USER_HISTORY_DIR, 'briefings', f'{token}.json'),
    ):
        try:
            os.remove(path)
        except OSError:
            pass
    current_event = {
        'uid': 'current-20260704',
        'summary': 'Authoritative current event',
        'start': '2026-07-04', 'end': '2026-07-04',
        'start_iso': '2026-07-04T11:00:00Z',
        'end_iso': '2026-07-04T12:00:00Z',
    }
    assert backend._profile_save(token, {
        'airline': 'Lufthansa',
        'calendar_feed': {'events': [current_event]},
    })
    client = backend.app.test_client()
    valid = backend._TokenValidationResult(
        backend._TokenValidationState.VALID, 'ack-archive@example.test')
    with patch.object(backend, '_validate_token', return_value=valid), \
            patch.object(backend, '_BUG004_REQUIRE_TOKEN_BINDING', False), \
            patch.object(backend, '_roster_pdf_upload_store', return_value=True):
        response = client.post(
            f'/api/user/roster-pdf/{token}/import',
            data={
                'pdf': (io.BytesIO(buf.getvalue()), 'ack.pdf'),
                'airline': 'Lufthansa',
                'merge_existing': '1',
            },
            content_type='multipart/form-data',
        )
    payload = response.get_json() or {}
    assert response.status_code == 200, payload
    assert payload['events_count'] == 2
    assert payload['existing_events_preserved'] == 1

    full = backend._profile_load(token) or {}
    feed = ((full.get('profile') or {}).get('calendar_feed')
            or full.get('calendar_feed') or {})
    summaries = {event.get('summary') for event in feed.get('events') or []}
    assert summaries == {
        '11:50 LT Briefing FRA · LH470 FRA - YYZ',
        'Authoritative current event',
    }
