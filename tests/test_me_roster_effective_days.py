"""Authenticated Android roster parity with iOS effectiveDays."""

import app as A


TOKEN = 'AT-1234567890abcdef'
HEADERS = {'Authorization': f'Bearer {TOKEN}'}


def _valid(_token):
    return A._TokenValidationResult(
        A._TokenValidationState.VALID, 'owner@example.test')


def _base(monkeypatch, *, session=None, timed_out=False, snapshot=None,
          briefings=None, briefing_status=200):
    monkeypatch.setattr(A, '_validate_token', _valid)
    monkeypatch.setattr(
        A, '_load_session_safe', lambda _token: (session, timed_out))
    monkeypatch.setattr(
        A, '_roster_snapshot_read', lambda _token: snapshot or {})
    monkeypatch.setattr(A, '_profile_homebase_cached', lambda _token: 'FRA')

    def briefing_response(_token):
        return A.jsonify({'briefings': briefings or {},
                          'count': len(briefings or {})}), briefing_status

    monkeypatch.setattr(A, 'get_briefings', briefing_response)


def test_me_roster_merges_session_snapshot_and_briefings(monkeypatch):
    tax_day = {
        'datum': '2026-08-20',
        'klass': 'FLUG',
        'marker': 'evaluated marker',
        'begruendung': 'tax evidence',
        'eur': 42.5,
    }
    snapshot_day = {
        'datum': '2026-08-20',
        'marker': 'snapshot marker',
        'routing': 'FRA-JFK',
        'ical_sectors': [{
            'flight': 'LH400', 'from': 'FRA', 'to': 'JFK',
            'dep_iso': '2026-08-20T08:00:00Z',
            'arr_iso': '2026-08-20T16:00:00Z',
        }],
    }
    briefing_day = {
        'ical_summary': 'LH400 FRA-JFK',
        'ical_location': 'FRA - JFK',
        'ical_start_iso': '2026-08-20T08:00:00Z',
        'ical_end_iso': '2026-08-20T16:00:00Z',
        'ical_sectors': snapshot_day['ical_sectors'],
    }
    _base(
        monkeypatch,
        session={'result_data': {'_tage_detail': [tax_day], 'other': 'kept'}},
        snapshot={'tage': [snapshot_day]},
        briefings={'2026-08-20': briefing_day},
    )

    response = A.app.test_client().get('/api/me/roster', headers=HEADERS)

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['roster_source'] == 'session+snapshot+briefings'
    assert payload['result_data']['other'] == 'kept'
    assert len(payload['result_data']['_tage_detail']) == 1
    day = payload['result_data']['_tage_detail'][0]
    assert day['datum'] == '2026-08-20'
    assert day['marker'] == 'LH400 FRA-JFK'
    assert day['routing'] == 'FRA-JFK'
    assert day['begruendung'] == 'tax evidence'
    assert day['eur'] == 42.5
    assert day['ical_sectors'][0]['flight'] == 'LH400'


def test_me_roster_synthesizes_briefings_without_tax_session(monkeypatch):
    _base(
        monkeypatch,
        session=None,
        snapshot={},
        briefings={
            '2026-08-21': {
                'ical_summary': 'LH402 FRA-JFK',
                'ical_location': 'FRA - JFK',
                'ical_start_iso': '2026-08-21T09:00:00Z',
                'ical_end_iso': '2026-08-21T17:15:00Z',
                'ical_sectors': [{
                    'flight': 'LH402', 'from': 'FRA', 'to': 'JFK',
                    'dep_iso': '2026-08-21T09:00:00Z',
                    'arr_iso': '2026-08-21T17:15:00Z',
                }],
            },
            'not-a-date': {'ical_summary': 'must be rejected'},
        },
    )

    response = A.app.test_client().get('/api/me/roster', headers=HEADERS)

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['roster_source'] == 'briefings'
    assert len(payload['result_data']['_tage_detail']) == 1
    day = payload['result_data']['_tage_detail'][0]
    assert day['datum'] == '2026-08-21'
    assert day['marker'] == 'LH402 FRA-JFK'
    assert day['routing'] == 'FRA-JFK'
    assert day.get('klass') is None
    assert day['reader_facts']['start_time'] == '11:00'


def test_me_roster_returns_empty_contract_for_account_without_roster(monkeypatch):
    _base(monkeypatch, session=None, snapshot={}, briefings={})

    response = A.app.test_client().get('/api/me/roster', headers=HEADERS)

    assert response.status_code == 200
    assert response.get_json() == {
        'result_data': {'_tage_detail': []},
        'roster_source': 'empty',
    }


def test_me_roster_returns_503_only_when_all_live_sources_fail(monkeypatch):
    _base(
        monkeypatch,
        session=None,
        timed_out=True,
        snapshot={},
        briefings={},
        briefing_status=503,
    )

    response = A.app.test_client().get('/api/me/roster', headers=HEADERS)

    assert response.status_code == 503
    assert response.get_json()['canonical_state'] == 'fetch_error'


def test_me_roster_rejects_query_body_and_missing_bearer(monkeypatch):
    _base(monkeypatch, session=None, snapshot={}, briefings={})
    client = A.app.test_client()

    assert client.get('/api/me/roster').status_code == 401
    query = client.get('/api/me/roster?token=AT-attacker', headers=HEADERS)
    body = client.open(
        '/api/me/roster', method='GET', headers=HEADERS,
        json={'token': 'AT-attacker'},
    )

    assert query.status_code == 400
    assert query.get_json()['error'] == 'query_not_allowed'
    assert body.status_code == 400
    assert body.get_json()['error'] == 'body_not_allowed'
