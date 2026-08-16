"""Header-only contracts for server-confirmed Flutter chat mutations."""

import app as A


TOKEN = 'AT-chat-owner'


def _valid(_token):
    return A._TokenValidationResult(
        A._TokenValidationState.VALID, 'chat-owner@example.test')


def test_me_chat_mutations_use_only_validated_bearer(monkeypatch):
    monkeypatch.setattr(A, '_validate_token', _valid)
    seen = []
    monkeypatch.setattr(
        A, 'edit_chat_message',
        lambda token, channel, message: seen.append(
            ('edit', token, channel, message)) or A.jsonify({'ok': True}),
    )
    monkeypatch.setattr(
        A, 'toggle_chat_message_reaction',
        lambda token, channel, message: seen.append(
            ('reaction', token, channel, message)) or A.jsonify({'ok': True}),
    )
    with A.app.test_request_context(
        '/api/me/crew-chat/channel/group__fra/message/m1', method='PATCH',
        headers={'Authorization': f'Bearer {TOKEN}'}, json={'text': 'Hi'},
    ):
        assert A.me_chat_channel_message('group__fra', 'm1').status_code == 200
    with A.app.test_request_context(
        '/api/me/crew-chat/channel/group__fra/message/m1/reaction',
        method='POST', headers={'Authorization': f'Bearer {TOKEN}'},
        json={'emoji': '❤️'},
    ):
        assert A.me_chat_channel_message_reaction('group__fra', 'm1').status_code == 200
    assert seen == [
        ('edit', TOKEN, 'group__fra', 'm1'),
        ('reaction', TOKEN, 'group__fra', 'm1'),
    ]


def test_me_wall_upload_rejects_missing_bearer(monkeypatch):
    monkeypatch.setattr(A, '_validate_token', _valid)
    with A.app.test_request_context('/api/me/wall/upload-image', method='POST'):
        response, status = A.me_wall_upload_image()
    assert status == 401
    assert response.get_json()['error'] == 'unauthorized'


def test_reaction_response_never_contains_a_bearer_or_actor_map(monkeypatch):
    message = {
        'id': 'm1', 'author_token': A._chat_author_id(TOKEN), 'text': 'Hi',
    }
    monkeypatch.setattr(A, '_chat_path', lambda _channel: True)
    monkeypatch.setattr(A, '_channel_access_error', lambda *_args: None)
    monkeypatch.setattr(A, '_dm_load_messages', lambda _channel: [message])
    monkeypatch.setattr(A, '_dm_messages_save_to_supabase', lambda *_args: True)
    monkeypatch.setattr(A, '_dm_save_messages_disk', lambda *_args: None)
    with A.app.test_request_context(
        '/api/me/crew-chat/channel/group__fra/message/m1/reaction', method='POST',
        json={'emoji': '❤️'},
    ):
        response = A.toggle_chat_message_reaction(TOKEN, 'group__fra', 'm1')
    body = response.get_json()
    assert body['ok'] is True
    assert body['reactions'] == {'❤️': 1}
    assert body['my_reactions'] == ['❤️']
    assert TOKEN not in str(body)
    assert 'reaction_actors' not in body


def test_edit_is_author_only_and_server_persists_the_new_text(monkeypatch):
    message = {
        'id': 'm1', 'author_token': A._chat_author_id(TOKEN), 'text': 'Before',
    }
    persisted = []
    monkeypatch.setattr(A, '_chat_path', lambda _channel: True)
    monkeypatch.setattr(A, '_channel_access_error', lambda *_args: None)
    monkeypatch.setattr(A, '_dm_load_messages', lambda _channel: [message])
    monkeypatch.setattr(
        A, '_dm_messages_save_to_supabase',
        lambda _channel, values: persisted.extend(values) or True,
    )
    monkeypatch.setattr(A, '_dm_save_messages_disk', lambda *_args: None)
    with A.app.test_request_context(
        '/api/me/crew-chat/channel/group__fra/message/m1', method='PATCH',
        json={'text': 'After'},
    ):
        response = A.edit_chat_message(TOKEN, 'group__fra', 'm1')
    assert response.status_code == 200
    assert message['text'] == 'After'
    assert message['edited'] is True
    assert persisted == [message]
