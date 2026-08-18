"""Flutter registration-search contract: read-only aggregate, never paid."""
import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

from unittest.mock import patch

import pytest

import app as A


@pytest.fixture(autouse=True)
def _pin_app_module():
    """Keep the blueprint bound to this app across suite import-order tests."""
    import sys
    previous = sys.modules.get('app')
    sys.modules['app'] = A
    try:
        yield
    finally:
        if previous is not None:
            sys.modules['app'] = previous


@pytest.fixture
def client():
    A.app.testing = True
    return A.app.test_client()


def test_registration_search_aggregates_free_existing_facts(client):
    import blueprints.aerox_data_blueprint as BP

    reference = {
        'reg': 'D-AIZB', 'hex': '3c1234', 'typecode': 'A320',
        'manufacturer': 'Airbus', 'model': 'A320-214', 'operator': 'Lufthansa',
    }
    legs = [{'flight_no': 'LH400', 'src': 'FRA', 'dst': 'JFK',
             'day': '2026-08-17'}]
    position = {'lat': 50.0, 'lon': 8.0, 'on_ground': False,
                'source': 'aircraft_live'}
    photo = {'photo': 'https://example.invalid/aizb.jpg',
             'photographer': 'AeroX'}
    with patch.object(BP, '_registration_reference', return_value=reference), \
         patch.object(BP, '_q1', return_value={'name': 'A320 family'}), \
         patch.object(BP, '_tail_history_warehouse', return_value=legs) as history, \
         patch.object(BP, '_aircraft_live_pos',
                      return_value=(position, None, 'D-AIZB', 'A320')), \
         patch.object(BP, '_cache_get', return_value=photo), \
         patch.object(BP, '_cache_put') as cache_put, \
         patch.object(BP, '_fr24_flights_by_reg') as paid:
        response = client.get('/api/ax/registration/D-AIZB')

    assert response.status_code == 200
    body = response.get_json()
    assert body['ok'] is True
    assert body['reg'] == 'D-AIZB'
    assert body['hex'] == '3c1234'
    assert body['aircraft']['typecode'] == 'A320'
    assert body['aircraft']['type_name'] == 'A320 family'
    assert body['photo'] == photo
    assert body['tail_history']['legs'] == legs
    assert body['live'] == position
    assert body['live_state'] == 'in_flight'
    history.assert_called_once_with(reg='D-AIZB', hexid='3c1234')
    paid.assert_not_called()
    cache_put.assert_not_called()


def test_registration_search_preserves_normalized_request_identity(client):
    import blueprints.aerox_data_blueprint as BP

    # The client checks exact identity after its own normalization.  A reference
    # feed's D-AIZB spelling may therefore not turn a DAIZB request into a
    # mismatched successful response.
    with patch.object(BP, '_registration_reference',
                      return_value={'reg': 'D-AIZB', 'hex': '3c1234'}), \
         patch.object(BP, '_tail_history_warehouse', return_value=[]), \
         patch.object(BP, '_aircraft_live_pos', return_value=(None, None, None, None)), \
         patch.object(BP, '_cache_get', return_value=None):
        response = client.get('/api/ax/registration/daizb')

    assert response.status_code == 200
    body = response.get_json()
    assert body['reg'] == 'DAIZB'
    assert body['tail_history']['legs'] == []
    assert body['live'] is None
    assert body['live_state'] == 'unknown'


def test_registration_search_rejects_invalid_path_before_any_lookup(client):
    import blueprints.aerox_data_blueprint as BP

    with patch.object(BP, '_registration_reference') as reference:
        response = client.get('/api/ax/registration/%21%21')

    assert response.status_code == 400
    assert response.get_json() == {'ok': False, 'error': 'invalid_registration'}
    reference.assert_not_called()
