"""Contracts for the header-only Android Flight Live alias."""

import app as A
from blueprints import aerox_data_blueprint as data


OWNER = 'AT-1234567890abcdef'


def _valid(_token):
    return A._TokenValidationResult(
        A._TokenValidationState.VALID, 'owner@example.test')


def _request(client, query=''):
    return client.get(
        f'/api/me/ax/flight-live{query}',
        headers={'Authorization': f'Bearer {OWNER}'},
    )


def test_me_flight_live_requires_a_valid_header_bearer(monkeypatch):
    monkeypatch.setattr(A, '_validate_token', _valid)
    response = A.app.test_client().get('/api/me/ax/flight-live?flight_no=LH400')
    assert response.status_code == 401
    assert response.get_json() == {'ok': False, 'error': 'unauthorized'}


def test_me_flight_live_accepts_only_valid_operational_facts(monkeypatch):
    monkeypatch.setattr(A, '_validate_token', _valid)
    client = A.app.test_client()
    cases = (
        ('?flight_no=LH400&token=AT-other', 'query_not_allowed'),
        ('?flight_no=LH', 'need_flight_no'),
        ('?flight_no=LH400&date=17-08-2026', 'invalid_date'),
        ('?flight_no=LH400&reg=not/a-tail', 'invalid_registration'),
        ('?flight_no=LH400&dep_iata=Frankfurt', 'invalid_dep_iata'),
        ('?flight_no=LH400&arr_iata=123', 'invalid_arr_iata'),
    )
    for query, error in cases:
        response = _request(client, query)
        assert response.status_code == 400
        assert response.get_json() == {'ok': False, 'error': error}


def test_me_flight_live_reuses_payload_builder_without_bearer(monkeypatch):
    monkeypatch.setattr(A, '_validate_token', _valid)
    seen = {}

    def build(**kwargs):
        seen.update(kwargs)
        return {
            'ok': True, 'flight': kwargs['flight_no'], 'date': kwargs['date'],
            'reg': kwargs['reg'], 'hex': '3c1234', 'callsign': 'DLH400',
            'aircraft_type': 'A320',
            'dep': {'iata': kwargs['dep_iata'], 'city': 'Frankfurt'},
            'dest': {'iata': kwargs['arr_iata'], 'city': 'New York'},
            'sched_dep': '2026-08-17T10:00:00', 'est_dep': None,
            'actual_dep': None, 'dep_delay_min': None, 'dep_gate': 'A12',
            'sched_arr': '2026-08-17T18:00:00', 'est_arr': None,
            'actual_arr': None, 'arr_delay_min': None, 'dest_gate': None,
            'live': {'lat': 50.0, 'lon': 4.0, 'track': 270, 'gs': 455},
            'in_flight': True, 'progress': 0.5, 'source': 'aircraft_live',
        }

    monkeypatch.setattr(data, 'flight_live_payload', build)
    response = _request(
        A.app.test_client(),
        '?flight_no=lh%20400&date=2026-08-17&reg=d-aixy'
        '&dep_iata=fra&arr_iata=jfk',
    )

    assert response.status_code == 200
    assert response.get_json()['live']['track'] == 270
    assert response.get_json()['dep'] == {'iata': 'FRA', 'city': 'Frankfurt'}
    assert seen == {
        'flight_no': 'LH400', 'date': '2026-08-17', 'reg': 'D-AIXY',
        'dep_iata': 'FRA', 'arr_iata': 'JFK',
    }
    assert OWNER not in str(response.get_json())


def test_legacy_ios_flight_live_route_stays_registered():
    source = open(data.__file__).read()
    assert "@aerox_data_bp.route('/api/ax/flight-live/<token>', methods=['GET'])" in source
    assert "@app.route('/api/me/ax/flight-live', methods=['GET'])" in open(A.__file__).read()
