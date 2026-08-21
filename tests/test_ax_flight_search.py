"""Confirmed prefix/codeshare search contract for the Aero X radar."""
import os

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

from unittest.mock import patch

import app as A


def test_prefix_search_returns_only_confirmed_warehouse_aliases():
    mapping = {
        'LH5060': 'AZ1619',
        'LH5061': 'AZ715',
        'LH5069': 'AZ1312',
        'LH5070': 'UA999',
        'UA8840': 'LH400',
    }
    with patch.object(A, '_ax_codeshare_map', return_value=mapping):
        rows = A._ax_flight_search_rows('lh 506')

    assert rows == [
        {'flight': 'LH5060', 'operating_flight': 'AZ1619', 'kind': 'codeshare'},
        {'flight': 'LH5061', 'operating_flight': 'AZ715', 'kind': 'codeshare'},
        {'flight': 'LH5069', 'operating_flight': 'AZ1312', 'kind': 'codeshare'},
    ]


def test_operating_number_search_also_finds_its_marketing_aliases():
    with patch.object(A, '_ax_codeshare_map', return_value={
            'UA8840': 'LH400', 'AC9092': 'LH400'}):
        rows = A._ax_flight_search_rows('LH400')

    assert {(r['flight'], r['operating_flight']) for r in rows} == {
        ('UA8840', 'LH400'), ('AC9092', 'LH400')}


def test_search_endpoint_shape_and_validation():
    A.app.testing = True
    client = A.app.test_client()
    with patch.object(A, '_ax_codeshare_map', return_value={'LH5060': 'AZ1619'}):
        response = client.get('/api/ax/flight-search?q=LH506')
    assert response.status_code == 200
    assert response.get_json() == {
        'ok': True,
        'query': 'LH506',
        'source': 'warehouse_codeshares',
        'count': 1,
        'results': [
            {'flight': 'LH5060', 'operating_flight': 'AZ1619', 'kind': 'codeshare'}
        ],
    }
    assert client.get('/api/ax/flight-search?q=FRA-GRU').status_code == 400
