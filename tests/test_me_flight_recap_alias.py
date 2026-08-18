"""Contracts for the header-only Android Flight Recap alias."""

import app as A
from blueprints import aerox_data_blueprint as data


OWNER = 'AT-1234567890abcdef'


def _valid(_token):
    return A._TokenValidationResult(
        A._TokenValidationState.VALID, 'owner@example.test')


def _request(client, query=''):
    return client.get('/api/me/ax/flight-recap' + query,
                      headers={'Authorization': 'Bearer ' + OWNER})


def test_me_flight_recap_requires_valid_header_bearer(monkeypatch):
    monkeypatch.setattr(A, '_validate_token', _valid)
    response = A.app.test_client().get('/api/me/ax/flight-recap?flight_no=LH400')
    assert response.status_code == 401
    assert response.get_json() == {'ok': False, 'error': 'unauthorized'}


def test_me_flight_recap_rejects_url_credentials_and_invalid_facts(monkeypatch):
    monkeypatch.setattr(A, '_validate_token', _valid)
    client = A.app.test_client()
    cases = (
        ('?flight_no=LH400&token=AT-other', 'query_not_allowed'),
        ('?flight_no=LH', 'need_flight_no'),
        ('?flight_no=LH400&date=17-08-2026', 'invalid_date'),
        ('?flight_no=LH400&dep_iata=Frankfurt', 'invalid_dep_iata'),
        ('?flight_no=LH400&arr_iata=123', 'invalid_arr_iata'),
    )
    for query, error in cases:
        response = _request(client, query)
        assert response.status_code == 400
        assert response.get_json() == {'ok': False, 'error': error}


def test_me_flight_recap_uses_shared_owner_scoped_payload_builder(monkeypatch):
    monkeypatch.setattr(A, '_validate_token', _valid)
    seen = {}

    def build(**kwargs):
        seen.update(kwargs)
        return {
            'ok': True, 'flight': kwargs['flight_no'], 'date': kwargs['date'],
            'dep': {'iata': kwargs['dep_iata'], 'city': 'Frankfurt'},
            'dest': {'iata': kwargs['arr_iata'], 'city': 'New York'},
            'status': 'on_time', 'delay_known': True, 'cancelled': False,
            'delay_min': 0, 'actual_dep': '2026-08-17T10:00:00Z',
            'actual_arr': '2026-08-17T18:00:00Z', 'block_time_min': 480,
        }

    monkeypatch.setattr(data, 'flight_recap_payload', build)
    response = _request(A.app.test_client(),
                        '?flight_no=lh%20400&date=2026-08-17&dep_iata=fra&arr_iata=jfk')
    assert response.status_code == 200
    assert response.get_json()['block_time_min'] == 480
    assert seen == {
        'owner_token': OWNER, 'flight_no': 'LH400', 'date': '2026-08-17',
        'dep_iata': 'FRA', 'arr_iata': 'JFK',
    }
    assert OWNER not in str(response.get_json())


def test_legacy_flight_recap_route_stays_registered():
    source = open(data.__file__).read()
    assert "@aerox_data_bp.route('/api/ax/flight-recap/<token>', methods=['GET'])" in source
    assert "@app.route('/api/me/ax/flight-recap', methods=['GET'])" in open(A.__file__).read()


def test_legacy_and_header_alias_dispatch_the_same_normalized_payload_builder(
    monkeypatch,
):
    monkeypatch.setattr(A, '_validate_token', _valid)
    calls = []

    def build(**kwargs):
        calls.append(kwargs)
        return {'ok': True, 'flight': kwargs['flight_no'], 'status': 'pending'}

    monkeypatch.setattr(data, 'flight_recap_payload', build)
    client = A.app.test_client()
    legacy = client.get('/api/ax/flight-recap/' + OWNER +
                        '?flight_no=lh%20400&date=2026-08-17&dep_iata=fra&arr_iata=jfk')
    header = _request(client,
                      '?flight_no=lh%20400&date=2026-08-17&dep_iata=fra&arr_iata=jfk')
    assert legacy.status_code == header.status_code == 200
    assert legacy.get_json() == header.get_json()
    assert calls == [
        {'owner_token': OWNER, 'flight_no': 'LH400', 'date': '2026-08-17',
         'dep_iata': 'FRA', 'arr_iata': 'JFK'},
        {'owner_token': OWNER, 'flight_no': 'LH400', 'date': '2026-08-17',
         'dep_iata': 'FRA', 'arr_iata': 'JFK'},
    ]
