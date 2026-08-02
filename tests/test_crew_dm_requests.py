"""Vertrag und Sicherheitsregeln fuer einmalige Crew-DM-Anfragen."""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import app as A


ALICE = 'AT-ALICE'
BOB = 'AT-BOB'


def _auth(token):
    return {'Authorization': f'Bearer {token}'}


def _base_patches(**extra):
    values = {
        '_validate_token': A._TokenValidationResult(
            A._TokenValidationState.VALID, 'crew@example.com'),
        '_validate_token_exists': 'crew@example.com',
        '_is_family_account': False,
        '_blocked_by': set(),
        '_token_rate_limited': False,
        '_profile_load': {'profile': {'name': 'Crew User'}},
    }
    values.update(extra)
    stack = []
    for name, value in values.items():
        stack.append(patch.object(A, name, return_value=value))
    return stack


class Patches:
    def __init__(self, **values):
        self.items = _base_patches(**values)

    def __enter__(self):
        for item in self.items:
            item.start()

    def __exit__(self, *args):
        for item in reversed(self.items):
            item.stop()


def _row(status='pending'):
    return {
        'id': '65d07b76-44ff-4ba0-9e33-f232c38b9b01',
        'sender_token': ALICE,
        'recipient_token': BOB,
        'message': 'Hi, wir sind heute zusammen auf LH400.',
        'status': status,
        'flight_number': 'LH400',
        'flight_date': '2026-08-02',
        'created_at': '2026-08-02T10:00:00+00:00',
        'created_ts': 1785664800.0,
        'decided_at': None,
    }


def test_any_valid_unblocked_account_can_receive_one_request():
    created = _row()
    with Patches(_crew_dm_pair_authorized=False,
                 _crew_dm_request_get=None,
                 _crew_dm_request_insert=(created, True),
                 _push_notify_async=None):
        client = A.app.test_client()
        response = client.post(
            f'/api/crew-chat/{ALICE}/requests/{BOB}',
            headers=_auth(ALICE), json={'message': 'Hallo'})
    assert response.status_code == 201
    assert response.get_json()['ok'] is True


def test_status_allows_one_request_without_friendship_or_shared_flight():
    with Patches(_crew_dm_pair_authorized=False,
                 _crew_dm_request_get=None):
        client = A.app.test_client()
        response = client.get(
            f'/api/crew-chat/{ALICE}/requests/{BOB}/status',
            headers=_auth(ALICE))
    assert response.status_code == 200
    assert response.get_json() == {
        'ok': True,
        'can_chat': False,
        'can_request': True,
        'status': 'available',
        'flight_number': None,
        'flight_date': None,
    }


def test_request_insert_is_one_message_only_and_returns_201():
    created = _row()
    with Patches(_crew_dm_pair_authorized=False,
                 _crew_dm_request_get=None,
                 _crew_dm_request_insert=(created, True),
                 _push_notify_async=None):
        client = A.app.test_client()
        response = client.post(
            f'/api/crew-chat/{ALICE}/requests/{BOB}',
            headers=_auth(ALICE), json={'message': created['message']})
    assert response.status_code == 201
    body = response.get_json()
    assert body['ok'] is True
    assert body['request']['status'] == 'pending'
    assert body['request']['direction'] == 'outgoing'


def test_second_request_is_rejected_even_after_decline():
    with Patches(_crew_dm_pair_authorized=False,
                 _crew_dm_request_get=_row('declined')):
        client = A.app.test_client()
        response = client.post(
            f'/api/crew-chat/{ALICE}/requests/{BOB}',
            headers=_auth(ALICE), json={'message': 'Noch einmal'})
    assert response.status_code == 409
    assert response.get_json() == {
        'ok': False, 'error': 'request_already_used', 'status': 'declined'}


def test_reverse_request_cannot_create_a_second_preview_message():
    incoming = _row('pending')

    def lookup(sender, recipient):
        if (sender, recipient) == (ALICE, BOB):
            return incoming
        return None

    with Patches(_crew_dm_pair_authorized=False), \
         patch.object(A, '_crew_dm_request_get', side_effect=lookup):
        client = A.app.test_client()
        response = client.post(
            f'/api/crew-chat/{BOB}/requests/{ALICE}',
            headers=_auth(BOB), json={'message': 'Meine zweite Vorschau'})
    assert response.status_code == 409
    assert response.get_json()['error'] == 'request_already_used'


def test_recipient_accept_materializes_initial_message():
    pending = _row('pending')
    accepted = dict(pending, status='accepted')
    with Patches(_crew_dm_request_get=pending,
                 _crew_dm_request_set_status=accepted,
                 _crew_dm_materialize_initial=True,
                 _push_notify_async=None):
        client = A.app.test_client()
        response = client.post(
            f'/api/crew-chat/{BOB}/requests/{ALICE}/decision',
            headers=_auth(BOB), json={'action': 'accept'})
    assert response.status_code == 200
    assert response.get_json()['status'] == 'accepted'


def test_sender_cannot_decide_own_outgoing_request():
    # Handler sucht strikt sender=<Pfad>, recipient=<Bearer-Owner>. Fuer ALICE als
    # Owner existiert daher keine eingehende Kante von BOB.
    with Patches(_crew_dm_request_get=None):
        client = A.app.test_client()
        response = client.post(
            f'/api/crew-chat/{ALICE}/requests/{BOB}/decision',
            headers=_auth(ALICE), json={'action': 'accept'})
    assert response.status_code == 404


def test_dm_gate_accepts_connection_without_friendship():
    with Patches(_crew_dm_pair_authorized=True,
                 _dm_load_messages=[]):
        client = A.app.test_client()
        response = client.get(
            f'/api/crew-chat/{ALICE}/dm/{BOB}', headers=_auth(ALICE))
    assert response.status_code == 200
    assert response.get_json()['messages'] == []


def test_dm_gate_still_rejects_unconnected_stranger():
    with Patches(_crew_dm_pair_authorized=False):
        client = A.app.test_client()
        response = client.get(
            f'/api/crew-chat/{ALICE}/dm/{BOB}', headers=_auth(ALICE))
    assert response.status_code == 403
    assert response.get_json()['error'] == 'not_friends_or_accepted'


def test_generic_channel_cannot_bypass_stranger_dm_gate():
    channel = A._dm_channel(ALICE, BOB)
    with Patches(_crew_dm_pair_authorized=False):
        client = A.app.test_client()
        read = client.get(
            f'/api/crew-chat/{ALICE}/channel/{channel}',
            headers=_auth(ALICE))
        send = client.post(
            f'/api/crew-chat/{ALICE}/channel/{channel}/send',
            headers=_auth(ALICE), json={'text': 'Bypass'})
    assert read.status_code == 403
    assert send.status_code == 403
    assert send.get_json()['error'] == 'not_friends_or_accepted'


def test_generic_channel_allows_accepted_crew_pair():
    channel = A._dm_channel(ALICE, BOB)
    with Patches(_crew_dm_pair_authorized=True,
                 _dm_load_messages=[]):
        client = A.app.test_client()
        response = client.get(
            f'/api/crew-chat/{ALICE}/channel/{channel}',
            headers=_auth(ALICE))
    assert response.status_code == 200
