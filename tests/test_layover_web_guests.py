"""Sicherheits-/Kompatibilitaetsvertrag fuer installierbare Layover-Webgaeste."""
import os
import sys
import tempfile
from contextlib import ExitStack
from unittest.mock import patch
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import app as A


OWNER = 'AT-OWNER'
GROUP = 'abcd1234'


def _owner_auth():
    return {'Authorization': f'Bearer {OWNER}'}


def _valid_auth_result():
    return A._TokenValidationResult(A._TokenValidationState.VALID,
                                    'owner@example.com')


class WebGuestHarness:
    def __init__(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.disk_path = os.path.join(self.tempdir.name, 'web-guests.json')
        self.saved_messages = []
        self.stack = ExitStack()

    def __enter__(self):
        row = {'id': GROUP, 'owner_token': OWNER,
               'name': '🌴 JFK Layover', 'members': []}
        self.stack.enter_context(patch.object(A, 'SB_AVAILABLE', False))
        self.stack.enter_context(patch.object(A, 'sb', None))
        self.stack.enter_context(patch.object(
            A, '_LAYOVER_WEB_INVITES_DISK', self.disk_path))
        self.stack.enter_context(patch.object(
            A, '_validate_token', return_value=_valid_auth_result()))
        self.stack.enter_context(patch.object(
            A, '_friend_group_row_by_id', return_value=row))
        self.stack.enter_context(patch.object(
            A, '_token_rate_limited', return_value=False))
        self.stack.enter_context(patch.object(
            A, '_ip_rate_limited', return_value=False))
        self.stack.enter_context(patch.object(
            A, '_dm_messages_save_to_supabase', side_effect=self._save_messages))
        self.stack.enter_context(patch.object(
            A, '_dm_load_messages_from_disk', return_value=[]))
        self.stack.enter_context(patch.object(A, '_dm_save_messages_disk'))
        self.stack.enter_context(patch.object(A, '_chat_push_fanout_async'))
        return self

    def _save_messages(self, _channel, rows):
        self.saved_messages.extend(rows)
        return True

    def __exit__(self, *args):
        self.stack.close()
        self.tempdir.cleanup()

    def create_invite(self, client):
        response = client.post(
            f'/api/layover-web/{OWNER}/groups/{GROUP}/invite',
            headers=_owner_auth(), json={})
        assert response.status_code == 201
        secret = urlparse(response.get_json()['web_url']).path.rsplit('/', 1)[1]
        return response, secret

    def join(self, client, secret, name='Alex', avatar='✈️'):
        return client.post(
            f'/api/layover-web/invites/{secret}/join',
            json={'name': name, 'avatar': avatar})


def test_invite_secret_is_hashed_and_public_meta_hides_group_id():
    with WebGuestHarness() as h:
        client = A.app.test_client()
        created, secret = h.create_invite(client)
        assert secret.startswith('lw_')
        with open(h.disk_path) as f:
            disk = f.read()
        assert secret not in disk
        assert A._layover_web_hash(secret) in disk

        meta = client.get(f'/api/layover-web/invites/{secret}')
        assert meta.status_code == 200
        assert meta.get_json()['group_name'] == '🌴 JFK Layover'
        assert 'group_id' not in meta.get_json()
        assert meta.headers['Cache-Control'] == 'no-store'
        assert created.get_json()['path'] == f'/layover/{secret}'


def test_guest_can_join_and_write_into_same_native_group_channel():
    with WebGuestHarness() as h:
        client = A.app.test_client()
        _, secret = h.create_invite(client)
        joined = h.join(client, secret, name='Alex', avatar='🌴')
        assert joined.status_code == 201
        session = joined.get_json()['session_token']

        sent = client.post(
            f'/api/layover-web/invites/{secret}/messages',
            headers={'Authorization': f'Bearer {session}'},
            json={'text': 'Hallo aus dem Browser!'})
        assert sent.status_code == 201
        assert len(h.saved_messages) == 1
        stored = h.saved_messages[0]
        assert stored['channel_id'] == f'group__{GROUP}'
        assert stored['author_name'] == '🌴 Alex'
        assert stored['kind'] == 'web_guest'

        # Der normale native Channel-Read sieht exakt dieselbe persistierte Row.
        # (Anzeige-GET nutzt seit 06.08. den Schnellpfad _dm_load_recent.)
        with patch.object(A, '_dm_load_recent', return_value=h.saved_messages), \
             patch.object(A, '_chat_author_identities', return_value={}):
            native = client.get(
                f'/api/crew-chat/{OWNER}/channel/group__{GROUP}',
                headers=_owner_auth())
        assert native.status_code == 200
        assert native.get_json()['messages'][0]['text'] == 'Hallo aus dem Browser!'


def test_message_requires_session_bound_to_this_invite():
    with WebGuestHarness() as h:
        client = A.app.test_client()
        _, secret = h.create_invite(client)
        no_session = client.get(
            f'/api/layover-web/invites/{secret}/messages')
        assert no_session.status_code == 401
        assert no_session.get_json()['error'] == 'guest_session_required'

        wrong_session = client.get(
            f'/api/layover-web/invites/{secret}/messages',
            headers={'Authorization': 'Bearer lw_not-a-real-session-token-123456789'})
        assert wrong_session.status_code == 401


def test_message_list_rejects_invalid_since_timestamp():
    with WebGuestHarness() as h:
        client = A.app.test_client()
        _, secret = h.create_invite(client)
        joined = h.join(client, secret)
        session = joined.get_json()['session_token']
        response = client.get(
            f'/api/layover-web/invites/{secret}/messages?since_ts=not-a-number',
            headers={'Authorization': f'Bearer {session}'})
        assert response.status_code == 400
        assert response.get_json()['error'] == 'invalid_since'


def test_guest_cannot_forge_native_rich_message_markers():
    with WebGuestHarness() as h:
        client = A.app.test_client()
        _, secret = h.create_invite(client)
        joined = h.join(client, secret)
        session = joined.get_json()['session_token']
        response = client.post(
            f'/api/layover-web/invites/{secret}/messages',
            headers={'Authorization': f'Bearer {session}'},
            json={'text': '[aerox:loc]{"lat": 0, "lng": 0}'})
        assert response.status_code == 400
        assert response.get_json()['error'] == 'invalid_text'
        assert h.saved_messages == []


def test_join_rejects_invalid_name_and_avatar():
    with WebGuestHarness() as h:
        client = A.app.test_client()
        _, secret = h.create_invite(client)
        assert h.join(client, secret, name='').status_code == 400
        assert h.join(client, secret, avatar='👿').status_code == 400


def test_non_member_cannot_mint_web_invite():
    with WebGuestHarness():
        client = A.app.test_client()
        with patch.object(A, '_friend_group_row_by_id', return_value={
            'id': GROUP, 'owner_token': 'AT-SOMEONE-ELSE',
            'name': 'Private', 'members': []}), \
             patch.object(A, '_friends_load', return_value={'groups': []}):
            response = client.post(
                f'/api/layover-web/{OWNER}/groups/{GROUP}/invite',
                headers=_owner_auth(), json={})
        assert response.status_code == 404


def test_pwa_shell_manifest_and_service_worker_are_served():
    secret = 'lw_' + ('a' * 43)
    client = A.app.test_client()
    page = client.get(f'/layover/{secret}')
    assert page.status_code == 200
    assert b'apple-mobile-web-app-capable' in page.data
    assert page.headers['Referrer-Policy'] == 'no-referrer'

    manifest = client.get(f'/layover/{secret}/manifest.webmanifest')
    assert manifest.status_code == 200
    payload = manifest.get_json()
    assert payload['display'] == 'standalone'
    assert payload['start_url'] == f'/layover/{secret}'

    worker = client.get('/layover-sw.js')
    assert worker.status_code == 200
    assert worker.headers['Service-Worker-Allowed'] == '/layover/'
