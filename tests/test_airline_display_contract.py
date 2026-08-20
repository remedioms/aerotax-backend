"""Release gate for newly learned airline calendar formats."""

import app as backend


def _event(summary='XY123 AAA - BBB', *, start='2026-08-20T08:00:00Z',
           end='2026-08-20T10:00:00Z', location='AAA - BBB'):
    return {
        'summary': summary,
        'location': location,
        'start': start[:10],
        'end': end[:10],
        'start_iso': start,
        'end_iso': end,
        '_multiday_dates': [start[:10]],
    }


def test_display_contract_proves_the_final_calendar_payload_shape():
    report = backend._airline_display_contract([
        _event(),
        {
            'summary': 'Off Day', 'location': '',
            'start': '2026-08-21', 'end': '2026-08-21',
            'start_iso': '', 'end_iso': '',
            '_multiday_dates': ['2026-08-21'],
        },
    ])

    assert report == {
        'version': 'calendar-v1',
        'ok': True,
        'error': None,
        'events_count': 2,
        'briefing_days': 2,
        'flight_days': 1,
        'sector_count': 1,
    }


def test_display_contract_rejects_an_easyjet_like_unparsed_numeric_flight():
    report = backend._airline_display_contract([
        _event(summary='8111 LGW - ALC', location='(12:20) LGW')
    ])

    assert report['ok'] is False
    assert report['error'] == 'display_contract_unparsed_route_event'


def test_display_contract_rejects_missing_flight_number_and_bad_times():
    missing_flight = backend._airline_display_contract([
        _event(summary='AAA - BBB')
    ])
    backwards = backend._airline_display_contract([
        _event(start='2026-08-20T11:00:00Z',
               end='2026-08-20T10:00:00Z')
    ])

    assert missing_flight['error'] == \
        'display_contract_missing_flight_number'
    assert backwards['error'] == 'display_contract_invalid_duration'


def test_catalog_promotion_requires_a_current_contract_capability(monkeypatch):
    monkeypatch.setattr(backend, 'SB_AVAILABLE', True)
    monkeypatch.setattr(
        backend, '_supabase_execute_with_timeout',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError('blocked promotion must not touch Supabase')))

    assert backend._airline_catalog_promote(
        'Example Air', 'example-air', 'ical_url',
        display_contract={'ok': True, 'version': 'old', 'sector_count': 1}
    ) is False
