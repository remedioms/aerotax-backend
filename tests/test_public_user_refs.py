"""Regression: fremde AT-Credentials dürfen Social-APIs nie verlassen.

Die Swift-/Android-Modelle behandeln User-IDs als opake Strings. AXU bleibt
deshalb wire-kompatibel, während intern alle bestehenden AT-Edges erhalten
bleiben.
"""

import json
from unittest.mock import patch

import app as A


OWNER = 'AT-1111111111111111'
FRIEND = 'AT-2222222222222222'


def test_public_user_ref_is_stable_reversible_and_not_plaintext(monkeypatch):
    monkeypatch.setenv('RECOVERY_SECRET', 'r' * 64)
    first = A._public_user_ref(FRIEND)
    second = A._public_user_ref(FRIEND)

    assert first == second
    assert first.startswith('AXU-')
    assert FRIEND not in first
    assert A._token_from_public_user_ref(first) == FRIEND


def test_public_user_ref_rejects_tampering(monkeypatch):
    monkeypatch.setenv('RECOVERY_SECRET', 'r' * 64)
    ref = A._public_user_ref(FRIEND)
    changed = ref[:-1] + ('A' if ref[-1] != 'A' else 'B')
    assert A._token_from_public_user_ref(changed) is None


def test_social_response_keeps_owner_but_hides_every_foreign_token(monkeypatch):
    monkeypatch.setenv('RECOVERY_SECRET', 'r' * 64)
    with A.app.test_request_context(
            '/api/user/friends/' + OWNER,
            headers={'Authorization': 'Bearer ' + OWNER}):
        response = A.jsonify({
            'token': OWNER,
            'friends': [{
                'token': FRIEND,
                'nested': {'friend_token': FRIEND},
                'list': [FRIEND],
            }],
        })
        safe_response = A._hide_foreign_user_credentials(response)
        payload = json.loads(safe_response.get_data(as_text=True))

    assert payload['token'] == OWNER
    public_ref = payload['friends'][0]['token']
    assert public_ref.startswith('AXU-')
    assert payload['friends'][0]['nested']['friend_token'] == public_ref
    assert payload['friends'][0]['list'] == [public_ref]
    assert FRIEND not in safe_response.get_data(as_text=True)


def test_short_token_labels_and_message_text_stay_byte_identical(monkeypatch):
    """Nur echte Credentials maskieren, keine UI-Kurzform oder Chat-Nachricht."""
    monkeypatch.setenv('RECOVERY_SECRET', 'r' * 64)
    with A.app.test_request_context(
            '/api/crew-chat/' + OWNER + '/inbox',
            headers={'Authorization': 'Bearer ' + OWNER}):
        response = A.jsonify({
            'author_short': 'AT-22222',
            'text': 'AT-HELLO ist normaler Nachrichtentext',
            'sender_token': FRIEND,
        })
        safe_response = A._hide_foreign_user_credentials(response)
        payload = json.loads(safe_response.get_data(as_text=True))

    assert payload['author_short'] == 'AT-22222'
    assert payload['text'] == 'AT-HELLO ist normaler Nachrichtentext'
    assert payload['sender_token'].startswith('AXU-')


def test_legacy_nearly_complete_token_prefix_gets_nonreversible_stable_id(
        monkeypatch):
    monkeypatch.setenv('RECOVERY_SECRET', 'r' * 64)
    legacy = FRIEND[:16] + '…'
    first = A._public_legacy_prefix_ref(legacy)
    assert first == A._public_legacy_prefix_ref(legacy)
    assert first.startswith('AXP-')
    assert '2222222222222' not in first
    assert A._token_from_public_user_ref(first) is None


def test_chat_author_id_supports_new_and_legacy_rows(monkeypatch):
    monkeypatch.setenv('RECOVERY_SECRET', 'r' * 64)
    public = A._chat_author_id(FRIEND)
    assert public.startswith('AXU-')
    assert A._chat_author_matches(public, FRIEND)
    assert A._chat_author_matches(FRIEND[:16] + '…', FRIEND)


def test_public_ref_in_target_json_is_resolved_before_route(monkeypatch):
    monkeypatch.setenv('RECOVERY_SECRET', 'r' * 64)
    ref = A._public_user_ref(FRIEND)
    with A.app.test_request_context(
            '/api/user/friend-requests/' + OWNER + '/send',
            method='POST', json={'friend_token': ref}):
        A._resolve_public_user_refs_at_boundary()
        assert A.request.get_json()['friend_token'] == FRIEND


def test_public_refs_in_group_members_and_crew_list_round_trip(monkeypatch):
    monkeypatch.setenv('RECOVERY_SECRET', 'r' * 64)
    ref = A._public_user_ref(FRIEND)
    with A.app.test_request_context(
            '/api/user/friend-groups/' + OWNER + '/create', method='POST',
            json={'member_tokens': [ref],
                  'crew_list': [{'token': ref, 'short_name': 'Friend F.'}],
                  'token': OWNER}):
        A._resolve_public_user_refs_at_boundary()
        body = A.request.get_json()
        assert body['member_tokens'] == [FRIEND]
        assert body['crew_list'][0]['token'] == FRIEND
        assert body['token'] == OWNER


def test_public_refs_round_trip_as_push_preference_dictionary_keys(monkeypatch):
    monkeypatch.setenv('RECOVERY_SECRET', 'r' * 64)
    with A.app.test_request_context(
            '/api/push/prefs/' + OWNER,
            headers={'Authorization': 'Bearer ' + OWNER}):
        response = A.jsonify({'friend_prefs': {
            FRIEND: {'level': 'important'},
        }})
        safe_response = A._hide_foreign_user_credentials(response)
        safe = json.loads(safe_response.get_data(as_text=True))
    public_key = next(iter(safe['friend_prefs']))
    assert public_key.startswith('AXU-')

    with A.app.test_request_context(
            '/api/push/prefs', method='POST',
            json={'user_token': OWNER,
                  'friend_prefs': {public_key: {'level': 'important'}}}):
        A._resolve_public_user_refs_at_boundary()
        assert A.request.get_json()['friend_prefs'] == {
            FRIEND: {'level': 'important'},
        }


def test_public_ref_in_friend_path_is_resolved_before_route(monkeypatch):
    monkeypatch.setenv('RECOVERY_SECRET', 'r' * 64)
    ref = A._public_user_ref(FRIEND)
    with A.app.test_request_context(
            f'/api/user/friend-roster/{OWNER}/{ref}', method='GET'):
        A.request.view_args = {'token': OWNER, 'friend_token': ref}
        A._resolve_public_user_refs_at_boundary()
        assert A.request.view_args['token'] == OWNER
        assert A.request.view_args['friend_token'] == FRIEND


def test_public_profile_may_resolve_axu_but_owner_routes_may_not(monkeypatch):
    monkeypatch.setenv('RECOVERY_SECRET', 'r' * 64)
    ref = A._public_user_ref(FRIEND)

    with A.app.test_request_context(f'/api/user/profile/{ref}', method='GET'):
        assert A.request.endpoint == 'get_user_profile'
        A._resolve_public_user_refs_at_boundary()
        assert A.request.view_args['token'] == FRIEND

    with A.app.test_request_context(f'/api/user/friends/{ref}', method='GET'):
        A.request.view_args = {'token': ref}
        A._resolve_public_user_refs_at_boundary()
        assert A.request.view_args['token'] == ref


def test_axu_cannot_authenticate_chat_owner_route(monkeypatch):
    """Regression fuer den live gemeldeten AXU-Impersonation-Request."""
    monkeypatch.setenv('RECOVERY_SECRET', 'r' * 64)
    public_ref = A._public_user_ref(FRIEND)
    client = A.app.test_client()

    with patch.object(A, '_dm_messages_save_to_supabase', return_value=True) as save:
        response = client.post(
            f'/api/crew-chat/{public_ref}/channel/group__known/send',
            headers={'Authorization': 'Bearer ' + public_ref},
            json={'text': 'forged message'},
        )

    assert response.status_code == 401
    assert response.get_json()['error'] == 'unauthorized'
    save.assert_not_called()


def test_axu_cannot_authenticate_nonstandard_crew_owner_route(monkeypatch):
    """Family-Endpunkte nennen den Owner crew_token statt token."""
    monkeypatch.setenv('RECOVERY_SECRET', 'r' * 64)
    public_ref = A._public_user_ref(FRIEND)

    response = A.app.test_client().post(
        f'/api/feed-status/{public_ref}/react',
        headers={'Authorization': 'Bearer ' + public_ref},
        json={'emoji': 'x'},
    )

    assert response.status_code == 401
    assert response.get_json()['error'] == 'unauthorized'


def test_axu_cannot_read_family_roster_as_account_owner(monkeypatch):
    monkeypatch.setenv('RECOVERY_SECRET', 'r' * 64)
    public_ref = A._public_user_ref(FRIEND)

    response = A.app.test_client().get(
        f'/api/family-roster/{public_ref}',
        headers={'Authorization': 'Bearer ' + public_ref},
    )

    assert response.status_code == 401
    assert response.get_json()['error'] == 'unauthorized'
