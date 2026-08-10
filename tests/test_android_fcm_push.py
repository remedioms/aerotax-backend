from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

import app as A


USER = 'AT-ANDROID-PUSH-123456'
HEADERS = {'Authorization': f'Bearer {USER}'}


class _RPC:
    def __init__(self, data):
        self.data = data

    def execute(self):
        return SimpleNamespace(data=self.data)


def test_fcm_migration_is_additive_multi_device_and_service_role_only():
    sql = Path(
        'supabase_migrations/20260729_android_fcm_installations.sql'
    ).read_text().lower()
    assert 'add column if not exists fcm_token text' in sql
    assert 'where fcm_token is not null' in sql
    assert 'device_endpoint_replaced' in sql
    assert 'from public, anon, authenticated' in sql
    assert 'to service_role' in sql
    assert 'fcm_service_account' not in sql


def test_durable_fcm_registration_uses_atomic_rpc():
    calls = []

    class SB:
        def rpc(self, name, params):
            calls.append((name, params))
            return _RPC('22222222-2222-2222-2222-222222222222')

    with patch.object(A, 'SB_AVAILABLE', True), patch.object(A, 'sb', SB()):
        installation_id = A._push_fcm_installation_register(
            USER,
            {
                'fcm_token': 'fcm-one',
                'bundle_id': 'de.aerosteuer.aerox',
                'device_id': 'pixel-1',
            },
            unregister_token='logout-secret',
        )

    assert installation_id == '22222222-2222-2222-2222-222222222222'
    assert calls[0][0] == 'register_fcm_installation'
    assert calls[0][1]['p_user_token'] == USER
    assert calls[0][1]['p_fcm_token'] == 'fcm-one'
    assert calls[0][1]['p_unregister_secret_hash']
    assert calls[0][1]['p_unregister_secret_hash'] != 'logout-secret'


def test_fcm_registration_persists_normalized_device_language():
    calls = []

    class SB:
        def rpc(self, name, params):
            calls.append((name, params))
            return _RPC('22222222-2222-2222-2222-222222222222')

    with patch.object(A, 'SB_AVAILABLE', True), patch.object(A, 'sb', SB()):
        A._push_fcm_installation_register(USER, {
            'fcm_token': 'fcm-one',
            'language': 'fr-CA',
        })

    assert calls[0][1]['p_metadata']['language'] == 'fr'


def test_register_fcm_requires_owner_and_preserves_preferences():
    client = A.app.test_client()
    existing = {
        'token': USER,
        'prefs': {'dm': False},
        'friend_prefs': {'AT-FRIEND': {'level': 'none'}},
        'apns_token': 'ios-stays-intact',
    }
    saved = {}

    def save(token, registry):
        saved.update(token=token, registry=registry)
        return True

    with patch.object(A, '_push_load', return_value=existing), \
            patch.object(A, '_push_save', side_effect=save):
        denied = client.post('/api/push/register-fcm', json={
            'token': USER, 'fcm_token': 'fcm-android-token',
        })
        response = client.post('/api/push/register-fcm', json={
            'token': USER,
            'fcm_token': 'fcm-android-token',
            'device_id': 'android-installation-1',
            'bundle_id': 'de.aerosteuer.aerox',
            'language': 'it-IT',
        }, headers=HEADERS)

    assert denied.status_code == 401
    assert response.status_code == 200
    registry = saved['registry']
    assert registry['push_token'] == 'fcm-android-token'
    assert registry['platform'] == 'android'
    assert registry['prefs'] == {'dm': False}
    assert registry['friend_prefs']['AT-FRIEND']['level'] == 'none'
    assert registry['apns_token'] == 'ios-stays-intact'
    assert registry['language'] == 'it'


def test_unregister_fcm_only_clears_matching_android_installation():
    client = A.app.test_client()
    existing = {
        'token': USER,
        'push_token': 'fcm-android-token',
        'platform': 'android',
        'device_id': 'android-installation-1',
    }
    saved = []
    with patch.object(A, '_push_load', return_value=existing), \
            patch.object(
                A, '_push_save',
                side_effect=lambda _token, registry: saved.append(registry) or True,
            ):
        wrong = client.post('/api/push/unregister-fcm', json={
            'token': USER,
            'fcm_token': 'different-token',
            'device_id': 'android-installation-1',
        }, headers=HEADERS)
        matching = client.post('/api/push/unregister-fcm', json={
            'token': USER,
            'fcm_token': 'fcm-android-token',
            'device_id': 'android-installation-1',
        }, headers=HEADERS)

    assert wrong.status_code == 200 and wrong.get_json()['noop'] is True
    assert matching.status_code == 200
    assert len(saved) == 1
    assert saved[0]['push_token'] == ''


def test_send_push_delivers_to_fcm_in_addition_to_native_installations():
    registrations = [{
        'installation_id': 'ios-1',
        'apns_token': 'apns-token',
        'bundle_id': 'aerotax.AeroTax',
        'apns_env': 'prod',
    }]
    legacy = {
        'push_token': 'fcm-android-token',
        'platform': 'android',
        'prefs': {},
    }
    with patch.object(
            A, '_push_delivery_registrations',
            return_value=(registrations, legacy)), \
            patch.dict(A.os.environ, {'APNS_AUTH_KEY': 'configured'}), \
            patch.object(A, '_send_apns', return_value=(True, None)), \
            patch.object(A, '_send_fcm', return_value=(True, None)) as fcm, \
            patch.object(A, '_push_installation_delivery_update'):
        detail = A._send_push_notification(
            USER, 'Neue Nachricht', 'Hallo',
            data={'type': 'dm', 'thread_id': 'dm__1'},
            _return_detail=True,
        )

    assert detail['ok'] is True
    assert detail['delivered'] == 2
    assert detail['attempted'] == 2
    fcm.assert_called_once_with(
        'fcm-android-token', 'Neue Nachricht', 'Hallo',
        data={'type': 'dm', 'thread_id': 'dm__1'}, thread_id=None,
    )


def test_send_push_fans_out_to_every_durable_android_installation():
    registrations = [
        {
            'installation_id': 'android-1',
            'fcm_token': 'fcm-one',
            'bundle_id': 'de.aerosteuer.aerox',
            'platform': 'android',
        },
        {
            'installation_id': 'android-2',
            'fcm_token': 'fcm-two',
            'bundle_id': 'de.aerosteuer.aerox',
            'platform': 'android',
        },
    ]
    with patch.object(
            A, '_push_delivery_registrations',
            return_value=(registrations, {'prefs': {}})), \
            patch.object(A, '_send_fcm', return_value=(True, None)) as fcm, \
            patch.object(A, '_push_installation_delivery_update') as health:
        detail = A._send_push_notification(
            USER, 'T', 'B', data={'type': 'dm'}, _return_detail=True)

    assert detail['delivered'] == 2
    assert detail['attempted'] == 2
    assert [call.args[0] for call in fcm.call_args_list] == [
        'fcm-one', 'fcm-two']
    assert health.call_count == 2


def test_dead_fcm_token_is_cleared_and_terminal():
    legacy = {'push_token': 'dead-fcm', 'platform': 'android'}
    with patch.object(A, '_push_delivery_registrations',
                      return_value=([], legacy)), \
            patch.object(A, '_send_fcm',
                         return_value=(False, 'UNREGISTERED')), \
            patch.object(A, '_push_save', return_value=True) as save:
        detail = A._send_push_notification(
            USER, 'T', 'B', _return_detail=True)

    assert detail['terminal'] is True
    assert detail['reason'] == 'all_installations_tombstoned'
    assert save.call_args.args[1]['push_token'] == ''


def test_fcm_http_v1_payload_stringifies_data_and_uses_message_channel():
    credentials = SimpleNamespace(token='oauth-token')
    response = SimpleNamespace(status_code=200)
    response.json = lambda: {}
    with patch.object(A, '_fcm_credentials',
                      return_value=(credentials, 'aerox-project')), \
            patch('requests.post', return_value=response) as post:
        ok, reason = A._send_fcm(
            'fcm-token', 'Titel', 'Text',
            data={'count': 3, 'silent': False, 'nested': {'id': 7}},
            thread_id='dm__thread',
        )

    assert ok is True and reason is None
    request = post.call_args
    assert request.args[0].endswith(
        '/v1/projects/aerox-project/messages:send')
    message = request.kwargs['json']['message']
    assert message['data'] == {
        'count': '3',
        'silent': 'false',
        'nested': '{"id":7}',
        'thread_id': 'dm__thread',
    }
    assert message['android']['notification']['channel_id'] == 'aerox_messages'
    assert message['android']['notification']['click_action'] == (
        'de.aerosteuer.aerox.NOTIFICATION_OPEN')


def test_fcm_roster_notifications_use_operations_channel():
    credentials = SimpleNamespace(token='oauth-token')
    response = SimpleNamespace(status_code=200)
    response.json = lambda: {}
    with patch.object(A, '_fcm_credentials',
                      return_value=(credentials, 'aerox-project')), \
            patch('requests.post', return_value=response) as post:
        ok, _ = A._send_fcm(
            'fcm-token', 'Dienstplan', 'Geändert',
            data={'type': 'roster_change'},
        )

    assert ok is True
    message = post.call_args.kwargs['json']['message']
    assert message['android']['notification']['channel_id'] == 'aerox_operations'
