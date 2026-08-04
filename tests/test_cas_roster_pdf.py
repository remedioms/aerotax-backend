"""Synthetic regressions for Lufthansa CAS roster-calendar imports."""
import io
import os
from datetime import datetime
from unittest.mock import patch

import cas_roster_parser as cas


def _rows(*body, period='JUN 2025', printed='27 MAY 2025 16:14'):
    header = [
        'Crew Assignment System v18.1',
        'Persönlicher Einsatzplan',
        f'Monat {period}',
        f'Druckdatum {printed}',
        'UTC Kalender/Homebase FRA',
        'Datum Umlauf Event Flug-Nr Routing Flugzeit',
    ]
    return [(0, line) for line in header + list(body)]


def test_single_month_resolves_leading_previous_month_by_weekday():
    result, error = cas.rows_to_calendar_events(_rows(
        'Do 29 A',
        'Fr 30 A',
        'Sa 31 OFF',
        'So 01 120268 FB',
        'Briefingzeit(LT FRA): 01/06/25 11:20',
        '1 LH492-1 B74B FRA 12:15-21:20 YVR 10:00 00:00',
    ))
    assert error is None
    assert result['coverage_dates'] == [
        '2025-05-29', '2025-05-30', '2025-05-31', '2025-06-01']
    flight = [event for event in result['events'] if 'LH492' in event[3]]
    assert len(flight) == 1
    assert flight[0][1] == datetime(2025, 6, 1, 12, 15)
    assert flight[0][2] == datetime(2025, 6, 1, 21, 20)
    assert flight[0][3] == '11:20 LT Briefing FRA · LH492 FRA - YVR'


def test_two_month_change_plan_keeps_first_month_tail_in_first_month():
    result, error = cas.rows_to_calendar_events(_rows(
        'Fr 29 OFF',
        'Sa 30 OFF',
        'So 31 FRS',
        'Mo 01 77809 FB',
        '1 LH511-1 B748 EZE 19:40-09:25 FRA 13:20 00:00',
        period='AUG-SEP 2025',
        printed='29 AUG 2025 19:42',
    ))
    assert error is None
    assert result['coverage_dates'] == [
        '2025-08-29', '2025-08-30', '2025-08-31', '2025-09-01']
    assert any(event[1] == datetime(2025, 9, 1, 19, 40)
               for event in result['events'])


def test_overnight_departure_closes_on_undated_followup_row():
    result, error = cas.rows_to_calendar_events(_rows(
        'So 01 89705 FB',
        '1 LH506-1 B748 FRA 20:05- --:--',
        'Mo 02 X',
        '-07:50 GRU 11:45 00:00 10:30',
    ))
    assert error is None
    flights = [event for event in result['events'] if 'LH506' in event[3]]
    assert len(flights) == 1
    assert flights[0][1] == datetime(2025, 6, 1, 20, 5)
    assert flights[0][2] == datetime(2025, 6, 2, 7, 50)
    assert flights[0][3] == 'LH506 FRA - GRU'
    assert result['warnings'] == []


def test_later_revision_replaces_complete_covered_day_including_deletion():
    old, old_error = cas.rows_to_calendar_events(_rows(
        'So 01 120268 FB',
        '1 LH492-1 B74B FRA 12:15-21:20 YVR 10:00 00:00',
        printed='27 MAY 2025 16:14',
    ))
    new, new_error = cas.rows_to_calendar_events(_rows(
        'So 01',
        printed='29 MAY 2025 09:00',
    ))
    assert old_error is None
    # A completely blank change day is a valid deletion, even though the
    # individual parser truthfully reports no event for it.
    assert new_error == 'no_roster_days'
    new = {
        'printed_at': datetime(2025, 5, 29, 9, 0),
        'coverage_dates': ['2025-06-01'],
        'event_days': {'2025-06-01': []},
        'events': [], 'warnings': [], 'period': 'JUN 2025',
    }
    merged = cas.merge_cas_roster_results([old, new])
    assert merged['event_days']['2025-06-01'] == []
    assert merged['events'] == []


def test_foreign_layout_is_rejected():
    result, error = cas.rows_to_calendar_events([(0, 'Unrelated invoice')])
    assert result is None
    assert error == 'unsupported_pdf_format'


def test_archive_protection_extends_to_recent_durable_briefings():
    import app as backend

    events = [
        {'start': '2026-07-02'},
        {'start': '2026-08-28'},
    ]
    briefings = {
        '2024-08-01': {'summary': 'old history'},
        '2026-03-01': {'summary': 'current lower edge'},
        '2026-08-29': {'summary': 'current trailing day'},
    }
    with patch.object(backend, '_ical_briefings_load',
                      return_value=briefings):
        assert backend._archive_protected_date_bounds('AT-TEST', events) == (
            '2026-03-01', '2026-08-29')


def _pdf_bytes(lines):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=A4)
    y = 820
    for line in lines:
        pdf.drawString(30, y, line)
        y -= 14
    pdf.save()
    return buf.getvalue()


def test_multi_pdf_endpoint_resolves_revision_and_preserves_active_feed():
    import app as backend

    old_pdf = _pdf_bytes([
        'Crew Assignment System v18.1', 'Persönlicher Einsatzplan',
        'Monat JUN 2025', 'Druckdatum 27 MAY 2025 16:14',
        'UTC Kalender/Homebase FRA',
        'So 01 120268 FB',
        '1 LH492-1 B74B FRA 12:15-21:20 YVR 10:00 00:00',
        'Mo 02 120269 FB',
        '1 LH400-1 B74B FRA 09:05-17:10 JFK 08:05 00:00',
    ])
    changed_pdf = _pdf_bytes([
        'Crew Assignment System v18.1', 'Änderung Einsatzplan',
        'Monat JUN 2025', 'Druckdatum 29 MAY 2025 09:00',
        'UTC Kalender/Homebase FRA',
        'So 01 OFF',
    ])
    token = 'AT-TEST-CAS-ARCHIVE-1'
    for path in (
        backend._user_profile_path(token),
        os.path.join(backend._USER_HISTORY_DIR, 'briefings', f'{token}.json'),
    ):
        try:
            os.remove(path)
        except OSError:
            pass
    current_event = {
        'uid': 'current-20260801',
        'summary': 'LH100 FRA - MUC',
        'start': '2026-08-01', 'end': '2026-08-01',
        'start_iso': '2026-08-01T10:00:00Z',
        'end_iso': '2026-08-01T11:00:00Z',
        '_dtstart_value': '20260801T100000Z',
        '_dtend_value': '20260801T110000Z',
        '_dtstart_params': {}, '_dtend_params': {},
    }
    assert backend._profile_save(token, {
        'airline': 'Lufthansa',
        'calendar_feed': {'events': [current_event]},
    })
    protected_briefings = {
        '2026-07-31': {
            'ical_summary': 'Protected edge day',
            'ical_location': 'FRA',
            'sentinel': 'keep-exactly',
        },
        '2026-08-01': {
            'ical_summary': 'LH100 FRA - MUC',
            'ical_location': 'FRA - MUC',
            'ical_start_iso': '2026-08-01T10:00:00+00:00',
            'ical_end_iso': '2026-08-01T11:00:00+00:00',
            'sentinel': 'keep-exactly',
        },
    }
    assert backend._ical_briefings_save(token, protected_briefings)

    client = backend.app.test_client()
    valid = backend._TokenValidationResult(
        backend._TokenValidationState.VALID, 'cas-archive@example.test')
    with patch.object(backend, '_validate_token', return_value=valid), \
            patch.object(backend, '_BUG004_REQUIRE_TOKEN_BINDING', False), \
            patch.object(backend, '_roster_pdf_upload_store', return_value=True):
        response = client.post(
            f'/api/user/roster-pdf/{token}/import',
            data={
                'pdf': [(io.BytesIO(old_pdf), 'old.pdf'),
                        (io.BytesIO(changed_pdf), 'changed.pdf')],
                'airline': 'Lufthansa',
                'merge_existing': '1',
            },
            content_type='multipart/form-data',
        )
    payload = response.get_json() or {}
    assert response.status_code == 200, payload
    assert payload['ok'] is True
    assert payload['archive_files'] == 2
    assert payload['archive_events'] == 2
    assert payload['existing_events_preserved'] == 1
    assert payload['events_count'] == 3

    full = backend._profile_load(token) or {}
    feed = ((full.get('profile') or {}).get('calendar_feed')
            or full.get('calendar_feed') or {})
    summaries = {event.get('summary') for event in feed.get('events') or []}
    assert summaries == {'Off Day', 'LH400 FRA - JFK', 'LH100 FRA - MUC'}
    saved_briefings = backend._ical_briefings_load(token) or {}
    for day, expected in protected_briefings.items():
        assert saved_briefings[day]['ical_summary'] == expected['ical_summary']
        assert saved_briefings[day]['sentinel'] == 'keep-exactly'


def test_roster_queue_reuses_pending_identical_pdf():
    import app as backend

    class _Pending:
        data = [{'id': 123}]

    class _Query:
        def select(self, *_args): return self
        def eq(self, *_args): return self
        def limit(self, *_args): return self
        def execute(self): return _Pending()

    class _Sb:
        def table(self, name):
            assert name == 'ax_logbook_upload'
            return _Query()

    with patch.object(backend, 'SB_AVAILABLE', True), \
            patch.object(backend, 'sb', _Sb()), \
            patch.object(backend, '_supabase_execute_with_timeout',
                         side_effect=lambda _name, fn: (fn(), False)), \
            patch.object(backend, '_logbook_upload_store') as store:
        assert backend._roster_pdf_upload_store(
            'AT-TEST-QUEUE', 'same.pdf', b'%PDF-same') is True
    store.assert_not_called()
