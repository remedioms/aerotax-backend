from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import app as A


USER = 'AT-PUSH-USER-123456'


class _RPC:
    def __init__(self, data):
        self._data = data

    def execute(self):
        return SimpleNamespace(data=self._data)


class _FluentTable:
    def __init__(self, data=None):
        self.data = data or []
        self.updated = None
        self.filters = []

    def select(self, *_args, **_kwargs):
        return self

    def update(self, patch):
        self.updated = patch
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def execute(self):
        return SimpleNamespace(data=self.data)


def test_migration_revokes_public_and_avoids_unsafe_legacy_timestamp_cast():
    sql = Path('supabase_migrations/20260714_push_installations_outbox.sql').read_text()
    for function in (
        'register_push_installation', 'tombstone_push_installations',
        'tombstone_push_installation_by_secret',
        'enqueue_push_outbox', 'claim_push_outbox', 'cleanup_push_outbox',
    ):
        block_start = sql.index(f'revoke execute on function public.{function}')
        block = sql[block_start:block_start + 300].lower()
        assert 'from public, anon, authenticated' in block
    assert "metadata->>'registered_at'" not in sql
    assert 'select distinct on (apns_token, bundle_id, environment)' in sql.lower()
    assert 'for update skip locked' in sql.lower()
    assert 'unique (apns_token, bundle_id, environment)' in sql.lower()
    assert ('tombstone_push_installations(text,uuid,text,text,text,text)'
            in sql)
    assert ('tombstone_push_installations(text,uuid,text,text,text,text,text)'
            not in sql)
    capability_fn = sql[
        sql.index('create or replace function public.tombstone_push_installation_by_secret'):
        sql.index('create or replace function public.tombstone_push_installations')
    ].lower()
    assert 'returning user_token, apns_token' in capability_fn
    assert 'and active = true' not in capability_fn
    assert 'unregister_secret_hash = null' not in capability_fn
    register_fn = sql[
        sql.index('create or replace function public.register_push_installation'):
        sql.index('create or replace function public.tombstone_push_installation_by_secret')
    ].lower()
    assert 'device_endpoint_replaced' in register_fn
    assert register_fn.count('expo_token = null') >= 1
    assert 'expo_token = null' in capability_fn
    assert 'push_token = null' not in sql.lower()
    for relation in ('push_installations', 'push_outbox',
                     'push_outbox_metrics'):
        marker = f'revoke all on table public.{relation}'
        start = sql.index(marker)
        assert 'from public, anon, authenticated' in sql[start:start + 180].lower()


def test_installation_registration_uses_atomic_rpc_and_returns_id():
    calls = []

    class SB:
        def rpc(self, name, params):
            calls.append((name, params))
            return _RPC('11111111-1111-1111-1111-111111111111')

    reg = {'apns_token': 'abc123', 'bundle_id': 'aerotax.AeroTax',
           'apns_env': 'prod', 'device_id': 'phone-1', 'platform': 'ios'}
    with patch.object(A, 'SB_AVAILABLE', True), patch.object(A, 'sb', SB()):
        installation_id = A._push_installation_register(USER, reg)
    assert installation_id == '11111111-1111-1111-1111-111111111111'
    assert calls[0][0] == 'register_push_installation'
    assert calls[0][1]['p_user_token'] == USER
    assert calls[0][1]['p_environment'] == 'prod'


def test_active_installations_return_every_device():
    table = _FluentTable([
        {'id': 'i1', 'apns_token': 'a1', 'bundle_id': 'bundle',
         'environment': 'prod', 'active': True},
        {'id': 'i2', 'apns_token': 'a2', 'bundle_id': 'bundle',
         'environment': 'sandbox', 'active': True},
    ])
    sb = SimpleNamespace(table=lambda _name: table)
    with patch.object(A, 'SB_AVAILABLE', True), patch.object(A, 'sb', sb):
        rows = A._push_installations_for_user(USER)
    assert [row['installation_id'] for row in rows] == ['i1', 'i2']
    assert ('user_token', USER) in table.filters
    assert ('active', True) in table.filters


def test_authoritative_empty_registry_never_resurrects_legacy_token():
    legacy = {'apns_token': 'stale-device', 'push_token': 'stale-expo'}
    with patch.object(A, '_push_load', return_value=legacy), \
            patch.object(A, '_push_installations_for_user', return_value=[]), \
            patch.object(A, '_push_installation_register') as register:
        rows, loaded_legacy = A._push_delivery_registrations(USER)
    assert rows == []
    assert loaded_legacy['apns_token'] == ''
    assert loaded_legacy['push_token'] == ''
    register.assert_not_called()


def test_registry_outage_fails_closed_and_outbox_retries():
    legacy = {'apns_token': 'stale-device', 'push_token': 'stale-expo'}
    with patch.object(A, '_push_load', return_value=legacy), \
            patch.object(A, '_push_installations_for_user', return_value=None), \
            patch.object(A, 'SB_AVAILABLE', True), patch.object(A, 'sb', object()), \
            patch.object(A, '_send_apns') as send:
        detail = A._send_push_notification(USER, 'T', 'B', _return_detail=True)
    assert detail == {
        'ok': False, 'terminal': False, 'reason': 'push_registry_unavailable',
        'delivered': 0, 'attempted': 0,
    }
    send.assert_not_called()


def test_send_push_fans_out_to_all_active_installations():
    regs = [
        {'installation_id': 'i1', 'apns_token': 'a1', 'bundle_id': 'bundle',
         'apns_env': 'prod'},
        {'installation_id': 'i2', 'apns_token': 'a2', 'bundle_id': 'bundle',
         'apns_env': 'prod'},
    ]
    with patch.object(A, '_push_delivery_registrations',
                      return_value=(regs, {'prefs': {}})), \
            patch.dict(A.os.environ, {'APNS_AUTH_KEY': 'configured'}), \
            patch.object(A, '_send_apns', return_value=(True, None)) as send, \
            patch.object(A, '_push_installation_delivery_update') as health:
        detail = A._send_push_notification(
            USER, 'Title', 'Body', data={'type': 'dm'}, _return_detail=True)
    assert detail['ok'] is True and detail['delivered'] == 2
    assert send.call_count == 2
    assert health.call_count == 2


def test_dead_device_is_tombstoned_without_disabling_other_device():
    regs = [
        {'installation_id': 'dead-id', 'apns_token': 'dead',
         'bundle_id': 'bundle', 'apns_env': 'prod'},
        {'installation_id': 'live-id', 'apns_token': 'live',
         'bundle_id': 'bundle', 'apns_env': 'prod'},
    ]
    sends = [(False, 'BadDeviceToken'), (False, 'BadDeviceToken'), (True, None)]
    with patch.object(A, '_push_delivery_registrations',
                      return_value=(regs, {})), \
            patch.dict(A.os.environ, {'APNS_AUTH_KEY': 'configured'}), \
            patch.object(A, '_send_apns', side_effect=sends), \
            patch.object(A, '_push_installation_delivery_update'), \
            patch.object(A, '_push_installation_tombstone', return_value=1) as tomb:
        detail = A._send_push_notification(USER, 'T', 'B', _return_detail=True)
    assert detail['delivered'] == 1
    assert tomb.call_count == 1
    assert tomb.call_args.kwargs['installation_id'] == 'dead-id'


def test_apns_http2_client_is_process_pooled():
    made = []

    class Client:
        def __init__(self, **kwargs):
            made.append(kwargs)

    fake_httpx = SimpleNamespace(Client=Client)
    with patch.dict('sys.modules', {'httpx': fake_httpx}), \
            patch.object(A, '_APNS_HTTP_CLIENT', None):
        first = A._apns_http_client()
        second = A._apns_http_client()
    assert first is second
    assert made == [{'http2': True, 'timeout': 10.0}]


def test_outbox_explicit_idempotency_key_is_stable_and_rpc_atomic():
    params_seen = []

    class SB:
        def rpc(self, name, params):
            assert name == 'enqueue_push_outbox'
            params_seen.append(params)
            return _RPC([{'outbox_id': 'o1', 'inserted': len(params_seen) == 1}])

    with patch.object(A, 'SB_AVAILABLE', True), patch.object(A, 'sb', SB()):
        first = A._push_outbox_enqueue(USER, 'T', 'B', {'type': 'dm'},
                                       idempotency_key='message-42')
        second = A._push_outbox_enqueue(USER, 'T', 'B', {'type': 'dm'},
                                        idempotency_key='message-42')
    assert first == second == 'o1'
    assert params_seen[0]['p_idempotency_key'] == params_seen[1]['p_idempotency_key']
    assert USER not in params_seen[0]['p_idempotency_key']


def test_chat_request_persists_fanout_job_before_returning():
    with patch.object(A, '_push_outbox_enqueue', return_value='fanout-1') as enqueue, \
            patch.object(A, '_ensure_push_outbox_worker') as wake, \
            patch.object(A, '_push_executor') as executor:
        result = A._chat_push_fanout_async(
            USER, f'dm__{USER}__AT-OTHER-123456', 'hello', message_id='m-1')
    assert result == 'fanout-1'
    assert enqueue.call_args.kwargs['idempotency_key'] == 'chat-fanout:m-1'
    assert enqueue.call_args.kwargs['data']['_internal_job'] == 'chat_fanout'
    wake.assert_called_once()
    executor.assert_not_called()


def test_chat_fanout_worker_enqueues_idempotent_child_rows():
    recipient = 'AT-OTHER-123456'
    with patch.object(A, '_profile_load', return_value={
            'profile': {'name': 'Basti'}}), \
            patch.object(A, '_muted_by', return_value=[]), \
            patch.object(A, '_blocked_by', return_value=[]), \
            patch.object(A, '_push_outbox_enqueue', return_value='child-1') as enqueue:
        ok = A._chat_push_fanout_async(
            USER, f'dm__{USER}__{recipient}', 'hello', message_id='m-1',
            _from_outbox=True)
    assert ok is True
    assert enqueue.call_args.args[:3] == (recipient, 'Basti', 'hello')
    assert enqueue.call_args.kwargs['idempotency_key'] == f'chat:m-1:{recipient}'


def test_outbox_drain_resolves_internal_chat_fanout_job():
    row = {
        'id': 'fanout-1', 'attempts': 1, 'user_token': USER,
        'payload': {
            'title': '__internal_chat_fanout__', 'body': 'hello',
            'data': {'_internal_job': 'chat_fanout',
                     'channel_id': 'dm__a__b', 'message_id': 'm-1'},
        },
    }
    with patch.object(A, '_push_outbox_claim', side_effect=[[row], []]), \
            patch.object(A, '_chat_push_fanout_async', return_value=True) as fanout, \
            patch.object(A, '_push_outbox_mark') as mark:
        A._push_outbox_drain(max_batches=2)
    fanout.assert_called_once_with(
        USER, 'dm__a__b', 'hello', message_id='m-1', _from_outbox=True)
    assert mark.call_args.args[1]['reason'] == 'chat_fanout_enqueued'
    assert mark.call_args.args[1]['terminal'] is True


def test_outbox_retry_then_dead_letter_and_delivered_payload_erasure():
    table = _FluentTable()
    sb = SimpleNamespace(table=lambda _name: table)
    with patch.object(A, 'SB_AVAILABLE', True), patch.object(A, 'sb', sb):
        assert A._push_outbox_mark(
            {'id': 'r1', 'attempts': 2},
            {'ok': False, 'terminal': False, 'reason': 'transport'}) is True
        assert table.updated['status'] == 'retry'
        assert table.updated['available_at']

        assert A._push_outbox_mark(
            {'id': 'd1', 'attempts': A._PUSH_OUTBOX_MAX_ATTEMPTS},
            {'ok': False, 'terminal': False, 'reason': 'transport'}) is True
        assert table.updated['status'] == 'dead'

        assert A._push_outbox_mark(
            {'id': 'ok1', 'attempts': 1},
            {'ok': True, 'terminal': True, 'reason': 'delivered'}) is True
        assert table.updated['status'] == 'delivered'
        assert table.updated['payload'] == {}
        assert table.updated['user_token'] is None


def test_native_registration_requires_durable_installation_when_sb_is_configured():
    client = A.app.test_client()
    body = {'token': USER, 'apns_token': 'abc', 'apns_env': 'prod'}
    headers = {'Authorization': f'Bearer {USER}'}
    with patch.object(A, '_push_load', return_value={}), \
            patch.object(A, '_push_save', return_value=True), \
            patch.object(A, 'SB_AVAILABLE', True), \
            patch.object(A, 'sb', object()), \
            patch.object(A, '_push_installation_register', return_value=None):
        response = client.post('/api/push/register-apns', json=body, headers=headers)
    assert response.status_code == 503
    assert response.get_json()['error'] == 'push_registry_unavailable'
    assert response.headers['Retry-After'] == '5'


def test_native_registration_does_not_save_legacy_before_atomic_rebind():
    client = A.app.test_client()
    body = {'token': USER, 'apns_token': 'abc', 'apns_env': 'prod'}
    headers = {'Authorization': f'Bearer {USER}'}
    calls = []

    def register(*_args, **_kwargs):
        calls.append('rebind')
        return '11111111-1111-1111-1111-111111111111'

    def save(*_args, **_kwargs):
        calls.append('legacy_save')
        return True

    with patch.object(A, '_push_load', return_value={}), \
            patch.object(A, '_push_save', side_effect=save), \
            patch.object(A, 'SB_AVAILABLE', True), \
            patch.object(A, 'sb', object()), \
            patch.object(A, '_push_installation_register', side_effect=register):
        response = client.post('/api/push/register-apns', json=body,
                               headers=headers)
    assert response.status_code == 200
    assert calls == ['rebind', 'legacy_save']


def test_legacy_apns_registration_fails_closed_before_legacy_save():
    client = A.app.test_client()
    headers = {'Authorization': f'Bearer {USER}'}
    with patch.object(A, '_push_load', return_value={}), \
            patch.object(A, '_push_save') as save, \
            patch.object(A, '_validate_token', return_value=A._TokenValidationResult(
                A._TokenValidationState.VALID)), \
            patch.object(A, 'SB_AVAILABLE', True), \
            patch.object(A, 'sb', object()), \
            patch.object(A, '_push_installation_register', return_value=None):
        response = client.post(f'/api/user/push-token/{USER}',
                               json={'apns_token': 'abc'}, headers=headers)
    assert response.status_code == 503
    assert response.headers['Retry-After'] == '5'
    save.assert_not_called()


def test_legacy_logout_without_installation_identity_tombstones_all():
    client = A.app.test_client()
    existing = {'token': USER, 'apns_token': 'abc', 'push_token': ''}
    headers = {'Authorization': f'Bearer {USER}'}
    with patch.object(A, '_push_load', return_value=existing), \
            patch.object(A, '_push_save', return_value=True), \
            patch.object(A, 'SB_AVAILABLE', True), \
            patch.object(A, 'sb', object()), \
            patch.object(A, '_push_installation_tombstone', return_value=2) as tomb:
        response = client.post('/api/push/unregister-apns',
                               json={'token': USER}, headers=headers)
    assert response.status_code == 200
    assert response.get_json()['tombstoned'] == 2
    assert tomb.call_args.kwargs['installation_id'] is None


def test_offline_logout_capability_needs_no_account_bearer():
    client = A.app.test_client()
    with patch.object(A, '_push_installation_tombstone_by_capability',
                      return_value=True) as tomb:
        response = client.post('/api/push/unregister-apns', json={
            'installation_id': '11111111-1111-1111-1111-111111111111',
            'unregister_token': 'installation-secret',
        })
    assert response.status_code == 200
    assert response.get_json()['via'] == 'installation_capability'
    tomb.assert_called_once_with('11111111-1111-1111-1111-111111111111',
                                 'installation-secret')


def test_offline_logout_rejects_malformed_installation_uuid_without_rpc():
    client = A.app.test_client()
    with patch.object(A, '_push_installation_tombstone_by_capability') as tomb:
        response = client.post('/api/push/unregister-apns', json={
            'installation_id': 'not-a-uuid',
            'unregister_token': 'installation-secret',
        })
    assert response.status_code == 400
    assert response.get_json()['error'] == 'invalid_installation_id'
    tomb.assert_not_called()


def test_native_registration_returns_rotating_logout_capability():
    client = A.app.test_client()
    headers = {'Authorization': f'Bearer {USER}'}
    captured = {}

    def register(_user, _registry, unregister_token=None):
        captured['secret'] = unregister_token
        return '11111111-1111-1111-1111-111111111111'

    with patch.object(A, '_push_load', return_value={}), \
            patch.object(A, '_push_save', return_value=True), \
            patch.object(A, 'SB_AVAILABLE', True), \
            patch.object(A, 'sb', object()), \
            patch.object(A, '_push_installation_register', side_effect=register):
        response = client.post('/api/push/register-apns', json={
            'token': USER, 'apns_token': 'abc', 'apns_env': 'prod',
        }, headers=headers)
    body = response.get_json()
    assert response.status_code == 200
    assert body['installation_id'].startswith('11111111')
    assert body['unregister_token'] == captured['secret']
    assert len(captured['secret']) >= 32
