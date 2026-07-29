import base64
import json
from types import SimpleNamespace
from unittest.mock import patch

import app as A


USER = 'AT-PLAY-BILLING-123456'
HEADERS = {'Authorization': f'Bearer {USER}'}


def _response(status, payload=None):
    response = SimpleNamespace(status_code=status)
    response.json = lambda: payload or {}
    return response


def test_google_play_verification_is_account_bound_active_and_acknowledged():
    payload = {
        'subscriptionState': 'SUBSCRIPTION_STATE_ACTIVE',
        'acknowledgementState': 'ACKNOWLEDGEMENT_STATE_PENDING',
        'externalAccountIdentifiers': {
            'obfuscatedExternalAccountId': A._google_play_account_ref(USER),
        },
        'lineItems': [{
            'productId': 'aerox.pro.yearly',
            'expiryTime': '2099-12-31T23:59:59Z',
        }],
    }
    credentials = SimpleNamespace(token='oauth')
    with patch.object(A, '_google_play_credentials',
                      return_value=credentials), \
            patch('requests.get', return_value=_response(200, payload)) as get, \
            patch('requests.post', return_value=_response(204)) as post:
        verified, error = A._google_play_subscription_verify(
            USER, 'purchase-token')

    assert error is None
    assert verified['active'] is True
    assert verified['acknowledged'] is True
    assert verified['purchase_token_hash'] != 'purchase-token'
    assert 'purchase-token' in get.call_args.args[0]
    assert post.call_args.args[0].endswith(
        '/aerox.pro.yearly/tokens/purchase-token:acknowledge')


def test_google_play_verification_rejects_purchase_from_another_account():
    payload = {
        'subscriptionState': 'SUBSCRIPTION_STATE_ACTIVE',
        'acknowledgementState': 'ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED',
        'externalAccountIdentifiers': {
            'obfuscatedExternalAccountId': A._google_play_account_ref('OTHER'),
        },
        'lineItems': [{
            'productId': 'aerox.pro.yearly',
            'expiryTime': '2099-12-31T23:59:59Z',
        }],
    }
    with patch.object(
            A, '_google_play_credentials',
            return_value=SimpleNamespace(token='oauth')), \
            patch('requests.get', return_value=_response(200, payload)), \
            patch('requests.post') as post:
        verified, error = A._google_play_subscription_verify(
            USER, 'stolen-token')

    assert verified is None
    assert error == 'account_binding_mismatch'
    post.assert_not_called()


def test_play_verify_endpoint_requires_owner_and_persists_only_verified_shape():
    client = A.app.test_client()
    verified = {
        'active': True,
        'state': 'SUBSCRIPTION_STATE_ACTIVE',
        'product_id': 'aerox.pro.yearly',
        'package_name': 'de.aerosteuer.aerox',
        'valid_until': '2099-12-31T23:59:59+00:00',
        'acknowledged': True,
        'purchase_token_hash': 'hash-only',
        'verified_at': '2026-07-29T00:00:00+00:00',
    }
    with patch.object(
            A, '_google_play_subscription_verify',
            return_value=(verified, None)) as verify, \
            patch.object(A, '_profile_sidekey_set', return_value=True) as save:
        denied = client.post('/api/play/subscription/verify', json={
            'token': USER, 'purchase_token': 'purchase-token',
        })
        accepted = client.post('/api/play/subscription/verify', json={
            'token': USER, 'purchase_token': 'purchase-token',
        }, headers=HEADERS)

    assert denied.status_code == 401
    assert accepted.status_code == 200
    verify.assert_called_once_with(USER, 'purchase-token')
    assert save.call_args.args == (
        USER, 'google_play_subscription', verified)
    assert 'purchase_token' not in save.call_args.args[2]


def test_entitlement_exposes_unexpired_google_play_subscription():
    client = A.app.test_client()
    profile = {
        'account_type': 'crew',
        'pro_first_seen': '2026-01-01T00:00:00Z',
        'google_play_subscription': {
            'active': True,
            'valid_until': '2099-12-31T23:59:59+00:00',
        },
    }
    with patch.object(
            A, '_profile_load',
            return_value={'profile': profile}):
        response = client.get(
            f'/api/user/entitlement/{USER}', headers=HEADERS)

    assert response.status_code == 200
    body = response.get_json()
    assert body['subscription_active'] is True
    assert body['subscription_platform'] == 'google_play'


def test_entitlement_requires_owner_bearer():
    client = A.app.test_client()

    response = client.get(f'/api/user/entitlement/{USER}')

    assert response.status_code == 401
    assert response.get_json()['error'] == 'token_binding_required'


def _rtdn_envelope(notification):
    raw = json.dumps(notification).encode('utf-8')
    return {
        'message': {
            'messageId': 'rtdn-1',
            'data': base64.b64encode(raw).decode('ascii'),
        },
        'subscription': 'projects/aerox/subscriptions/play-rtdn',
    }


def test_rtdn_reverifies_and_persists_cancelled_lifecycle_state():
    client = A.app.test_client()
    notification = {
        'version': '1.0',
        'packageName': 'de.aerosteuer.aerox',
        'eventTimeMillis': '1785320000000',
        'subscriptionNotification': {
            'version': '1.0',
            'notificationType': 3,
            'purchaseToken': 'rtdn-purchase-token',
            'subscriptionId': 'aerox.pro.yearly',
        },
    }
    verified = {
        'active': False,
        'state': 'SUBSCRIPTION_STATE_EXPIRED',
        'product_id': 'aerox.pro.yearly',
        'package_name': 'de.aerosteuer.aerox',
        'valid_until': '2026-07-28T00:00:00+00:00',
        'acknowledged': True,
        'purchase_token_hash': 'hash-only',
        'verified_at': '2026-07-29T00:00:00+00:00',
    }
    with patch.object(A, '_google_play_rtdn_authorized',
                      return_value=True), \
            patch.object(A, '_google_play_subscription_owner',
                         return_value=USER), \
            patch.object(A, '_google_play_subscription_verify',
                         return_value=(verified, None)) as verify, \
            patch.object(A, '_google_play_subscription_index_upsert',
                         return_value=True) as index, \
            patch.object(A, '_profile_sidekey_set',
                         return_value=True) as save:
        response = client.post(
            '/api/play/rtdn', json=_rtdn_envelope(notification))

    assert response.status_code == 204
    verify.assert_called_once_with(USER, 'rtdn-purchase-token')
    index.assert_called_once_with(USER, verified)
    save.assert_called_once_with(
        USER, 'google_play_subscription', verified)


def test_rtdn_rejects_unauthenticated_pubsub_push():
    client = A.app.test_client()

    response = client.post('/api/play/rtdn', json={})

    assert response.status_code == 401


def test_rtdn_rejects_wrong_package_before_purchase_lookup():
    client = A.app.test_client()
    notification = {
        'packageName': 'attacker.example',
        'subscriptionNotification': {
            'purchaseToken': 'purchase-token',
            'subscriptionId': 'aerox.pro.yearly',
        },
    }
    with patch.object(A, '_google_play_rtdn_authorized',
                      return_value=True), \
            patch.object(A, '_google_play_subscription_owner') as owner:
        response = client.post(
            '/api/play/rtdn', json=_rtdn_envelope(notification))

    assert response.status_code == 403
    owner.assert_not_called()


def test_google_play_index_contains_hash_but_never_raw_purchase_token():
    query = SimpleNamespace(
        execute=lambda: SimpleNamespace(data=[]),
    )
    table = SimpleNamespace(
        upsert=lambda row, on_conflict: (
            setattr(table, 'row', row) or query
        ),
    )
    verified = {
        'active': True,
        'state': 'SUBSCRIPTION_STATE_ACTIVE',
        'product_id': 'aerox.pro.yearly',
        'package_name': 'de.aerosteuer.aerox',
        'valid_until': '2099-12-31T23:59:59+00:00',
        'acknowledged': True,
        'purchase_token_hash': 'a' * 64,
        'verified_at': '2026-07-29T00:00:00+00:00',
    }
    fake_sb = SimpleNamespace(table=lambda _name: table)
    with patch.object(A, 'SB_AVAILABLE', True), \
            patch.object(A, 'sb', fake_sb):
        assert A._google_play_subscription_index_upsert(
            USER, verified) is True

    assert table.row['purchase_token_hash'] == 'a' * 64
    assert 'purchase_token' not in table.row
