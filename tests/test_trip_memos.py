"""Private, accountgebundene Zimmer-/Parkplatz-Erinnerungen."""

from copy import deepcopy
from unittest.mock import patch

import app as A


TOK = 'AT-TRIP-MEMO-OWNER'
OTHER = 'AT-TRIP-MEMO-OTHER'


class _Store:
    def __init__(self):
        self.profile = {'name': 'Tim'}
        self.disk = {'token': TOK, 'profile': deepcopy(self.profile)}

    def load(self, token, fresh=False):
        return {'token': token, 'profile': deepcopy(self.profile)}

    def load_disk(self, token):
        return deepcopy(self.disk)

    def save(self, token, profile, full_disk_payload=None):
        self.profile = deepcopy(profile)
        self.disk = deepcopy(full_disk_payload)
        return True

    def run(self, fn):
        with patch.object(A, '_profile_load', side_effect=self.load), \
             patch.object(A, '_profile_load_from_disk', side_effect=self.load_disk), \
             patch.object(A, '_profile_save', side_effect=self.save), \
             patch.object(A, '_validate_token', side_effect=lambda token:
                          A._TokenValidationResult(A._TokenValidationState.VALID)
                          if token in (TOK, OTHER)
                          else A._TokenValidationResult(A._TokenValidationState.INVALID)):
            return fn()


def _headers(token=TOK):
    return {'Authorization': f'Bearer {token}'}


def _put(payload, bearer=TOK):
    return A.app.test_client().put(
        f'/api/user/trip-memos/{TOK}', json=payload, headers=_headers(bearer))


def _list(bearer=TOK):
    return A.app.test_client().get(
        f'/api/user/trip-memos/{TOK}', headers=_headers(bearer))


def _put_path(kind, anchor, scope, payload, bearer=TOK):
    return A.app.test_client().put(
        f'/api/user/trip-memos/{TOK}/{kind}/{anchor}/{scope}',
        json=payload, headers=_headers(bearer))


def test_room_and_parking_are_separate_and_durable():
    store = _Store()

    def flow():
        room = _put({'kind': 'room', 'anchor': '2026-08-10',
                     'date': '2026-08-11', 'iata': 'ICN', 'value': '815',
                     'hotel_name': 'Crew Hotel Seoul'})
        parking = _put({'kind': 'parking', 'anchor': '2026-08-10',
                        'date': '2026-08-10', 'location': 'MUC',
                        'value': 'P4 · Ebene 3'})
        return room, parking, _list()

    room, parking, listing = store.run(flow)
    assert room.status_code == 200
    assert parking.status_code == 200
    items = listing.get_json()['items']
    assert {(item['kind'], item['value']) for item in items} == {
        ('room', '815'), ('parking', 'P4 · Ebene 3')}
    assert store.profile['trip_memos']['parking|2026-08-10']['location'] == 'MUC'


def test_same_station_on_two_trips_is_room_history_not_overwrite():
    store = _Store()

    def flow():
        _put({'kind': 'room', 'anchor': '2026-07-01', 'date': '2026-07-02',
              'iata': 'ICN', 'value': '415'})
        _put({'kind': 'room', 'anchor': '2026-08-10', 'date': '2026-08-11',
              'iata': 'ICN', 'value': '815'})
        return _list()

    items = store.run(flow).get_json()['items']
    assert [item['value'] for item in items] == ['815', '415']


def test_empty_value_deletes_only_selected_memo():
    store = _Store()

    def flow():
        _put({'kind': 'room', 'anchor': '2026-08-10', 'date': '2026-08-11',
              'iata': 'ICN', 'value': '815'})
        _put({'kind': 'parking', 'anchor': '2026-08-10', 'date': '2026-08-10',
              'location': 'MUC', 'value': 'P4'})
        _put({'kind': 'room', 'anchor': '2026-08-10', 'date': '2026-08-11',
              'iata': 'ICN', 'value': ''})
        return _list()

    items = store.run(flow).get_json()['items']
    assert [(item['kind'], item['value']) for item in items] == [('parking', 'P4')]


def test_trip_memos_are_owner_only_and_pii_gated():
    assert A._bug004_get_route_needs_auth(f'/api/user/trip-memos/{TOK}') is True
    store = _Store()
    response = store.run(lambda: _list(bearer=OTHER))
    assert response.status_code in (401, 403)


def test_invalid_room_station_and_date_are_rejected():
    store = _Store()
    bad_iata = store.run(lambda: _put(
        {'kind': 'room', 'anchor': '2026-08-10', 'date': '2026-08-11',
         'iata': 'I', 'value': '815'}))
    bad_date = store.run(lambda: _put(
        {'kind': 'parking', 'anchor': 'morgen', 'date': 'morgen',
         'location': 'MUC', 'value': 'P4'}))
    assert bad_iata.status_code == 400
    assert bad_date.status_code == 400


def test_resource_specific_paths_and_last_write_wins():
    """Der iOS-Sync nutzt eindeutige Pfade; ein alter Queue-PUT bleibt wirkungslos."""
    store = _Store()

    def flow():
        newer = _put_path('room', '2026-08-10', 'ICN', {
            'kind': 'room', 'anchor': '2026-08-10', 'date': '2026-08-11',
            'iata': 'ICN', 'value': '815',
            'updated_at': '2026-08-13T18:00:00Z'})
        stale = _put_path('room', '2026-08-10', 'ICN', {
            'kind': 'room', 'anchor': '2026-08-10', 'date': '2026-08-11',
            'iata': 'ICN', 'value': '415',
            'updated_at': '2026-08-13T17:00:00Z'})
        parking = _put_path('parking', '2026-08-10', 'parking', {
            'kind': 'parking', 'anchor': '2026-08-10', 'date': '2026-08-10',
            'location': 'MUC', 'value': 'P4'})
        return newer, stale, parking, _list()

    newer, stale, parking, listing = store.run(flow)
    assert newer.status_code == parking.status_code == 200
    assert stale.get_json()['stale_ignored'] is True
    items = listing.get_json()['items']
    assert {(item['kind'], item['value']) for item in items} == {
        ('room', '815'), ('parking', 'P4')}
