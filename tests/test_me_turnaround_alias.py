"""Contracts for the header-only Android turnaround alias."""

import app as A
from blueprints import aerox_data_blueprint as data


OWNER = 'AT-1234567890abcdef'


def _valid(_token):
    return A._TokenValidationResult(
        A._TokenValidationState.VALID, 'owner@example.test')


def _request(client, query=''):
    return client.get(
        f'/api/me/ax/turnaround{query}',
        headers={'Authorization': f'Bearer {OWNER}'},
    )


def test_me_turnaround_requires_a_valid_header_bearer(monkeypatch):
    monkeypatch.setattr(A, '_validate_token', _valid)
    response = A.app.test_client().get(
        '/api/me/ax/turnaround?flight_no=LH400&arr=FRA&next_flight_no=LH401',
    )
    assert response.status_code == 401
    assert response.get_json() == {'ok': False, 'error': 'unauthorized'}


def test_me_turnaround_validates_only_bounded_operational_facts(monkeypatch):
    monkeypatch.setattr(A, '_validate_token', _valid)
    client = A.app.test_client()
    cases = (
        ('?flight_no=LH400&arr=FRA&next_flight_no=LH401&token=AT-other',
         'query_not_allowed'),
        ('?flight_no=LH&arr=FRA&next_flight_no=LH401',
         'need_current_and_next_sector'),
        ('?flight_no=LH400&arr=Frankfurt&next_flight_no=LH401',
         'need_current_and_next_sector'),
        ('?flight_no=LH400&arr=FRA&next_flight_no=LH&next_arr=JFK',
         'need_current_and_next_sector'),
        ('?flight_no=LH400&arr=FRA&next_flight_no=LH401&date=17-08-2026',
         'invalid_date'),
        ('?flight_no=LH400&dep=Frankfurt&arr=FRA&next_flight_no=LH401',
         'invalid_dep_iata'),
        ('?flight_no=LH400&arr=FRA&next_flight_no=LH401&next_arr=123',
         'invalid_next_arr_iata'),
    )
    for query, error in cases:
        response = _request(client, query)
        assert response.status_code == 400
        assert response.get_json() == {'ok': False, 'error': error}


def test_me_turnaround_has_legacy_payload_parity_without_url_credential(monkeypatch):
    monkeypatch.setattr(A, '_validate_token', _valid)
    seen = []

    def build(**kwargs):
        seen.append(kwargs)
        return {
            'ok': True, 'turnaround_airport': {'iata': kwargs['turnaround_airport']},
            'current_flight': kwargs['current_flight'],
            'next_flight': kwargs['next_flight'], 'same_aircraft': None,
            'inbound_chain': {'reg': None}, 'dep_delay_forecast': None,
        }

    monkeypatch.setattr(data, 'turnaround_payload', build)
    query = ('?flight_no=lh%20400&dep=muc&arr=fra&date=2026-08-17'
             '&next_flight_no=lh%20401&next_arr=jfk')
    legacy = A.app.test_client().get(f'/api/ax/turnaround/legacy-token{query}')
    modern = _request(A.app.test_client(), query)

    assert legacy.status_code == modern.status_code == 200
    assert modern.get_json() == legacy.get_json()
    assert seen == [
        {
            'current_flight': 'LH400', 'current_dep': 'MUC',
            'turnaround_airport': 'FRA', 'date': '2026-08-17',
            'next_flight': 'LH401', 'next_arr': 'JFK',
        },
        {
            'current_flight': 'LH400', 'current_dep': 'MUC',
            'turnaround_airport': 'FRA', 'date': '2026-08-17',
            'next_flight': 'LH401', 'next_arr': 'JFK',
        },
    ]
    assert OWNER not in str(modern.get_json())


def test_legacy_turnaround_route_stays_registered_for_ios():
    source = open(data.__file__).read()
    assert "@aerox_data_bp.route('/api/ax/turnaround/<token>', methods=['GET'])" in source
    assert "@app.route('/api/me/ax/turnaround', methods=['GET'])" in open(A.__file__).read()
