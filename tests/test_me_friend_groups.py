"""Contracts for the credential-free Android crew-group API."""

import json

import app as A


OWNER = 'AT-1111111111111111'
FRIEND = 'AT-2222222222222222'


def _valid(_token):
    return A._TokenValidationResult(
        A._TokenValidationState.VALID, 'owner@example.test')


def _state():
    return {
        'token': OWNER,
        'friends': [FRIEND],
        'groups': [
            {
                'id': 'layover1',
                'name': 'FRA Layover',
                'members': [FRIEND],
            },
        ],
    }


def test_me_groups_never_serialize_owner_or_member_credentials(monkeypatch):
    monkeypatch.setenv('RECOVERY_SECRET', 'r' * 64)
    monkeypatch.setattr(A, '_validate_token', _valid)
    monkeypatch.setattr(A, '_friends_load', lambda _token: _state())
    with A.app.test_request_context(
        '/api/me/friend-groups', headers={'Authorization': f'Bearer {OWNER}'}):
        response = A.me_friend_groups()
    text = response.get_data(as_text=True)
    body = json.loads(text)
    assert OWNER not in text
    assert FRIEND not in text
    assert body['groups'][0]['members'][0].startswith('AXU-')
    assert body['groups'][0]['member_count'] == 1


def test_me_groups_create_accepts_only_public_member_refs(monkeypatch):
    monkeypatch.setenv('RECOVERY_SECRET', 'r' * 64)
    monkeypatch.setattr(A, '_validate_token', _valid)
    saved = []
    state = _state()
    monkeypatch.setattr(A, '_friends_load', lambda _token: state)
    monkeypatch.setattr(A, '_friends_save', lambda _token, data: saved.append(data) or True)
    ref = A._public_user_ref(FRIEND)
    with A.app.test_request_context(
        '/api/me/friend-groups', method='POST',
        headers={'Authorization': f'Bearer {OWNER}'},
        json={'name': 'MUC', 'member_refs': [ref]}):
        response = A.me_friend_groups_create()
    body = response.get_json()
    assert response.status_code == 200
    assert body['group']['members'] == [ref]
    assert saved[-1]['groups'][-1]['members'] == [FRIEND]

    with A.app.test_request_context(
        '/api/me/friend-groups', method='POST',
        headers={'Authorization': f'Bearer {OWNER}'},
        json={'name': 'Raw', 'member_refs': [FRIEND]}):
        rejected = A.me_friend_groups_create()
    assert rejected[1] == 400
    assert rejected[0].get_json()['error'] == 'invalid_members'


def test_me_groups_rejects_unknown_or_non_owner_group_ids(monkeypatch):
    monkeypatch.setenv('RECOVERY_SECRET', 'r' * 64)
    monkeypatch.setattr(A, '_validate_token', _valid)
    monkeypatch.setattr(A, '_friends_load', lambda _token: _state())
    with A.app.test_request_context(
        '/api/me/friend-groups/other', method='PATCH',
        headers={'Authorization': f'Bearer {OWNER}'}, json={'name': 'No'}):
        response = A.me_friend_groups_update('other')
    assert response[1] == 404

    with A.app.test_request_context(
        '/api/me/friend-groups/../../bad', method='PATCH',
        headers={'Authorization': f'Bearer {OWNER}'}, json={'name': 'No'}):
        invalid = A.me_friend_groups_update('../../bad')
    assert invalid[1] == 400
