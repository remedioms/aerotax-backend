from unittest.mock import patch

import app as A


TOKEN = 'AT-CHAT-IDEMPOTENCY-123456'
CHANNEL = 'group__offline'
CLIENT_ID = '6b8a4c80-8cf5-4d5e-bd8a-e8f0dba12345'


def test_chat_retry_returns_existing_message_without_save_or_push():
    existing = {
        'id': 'server-1',
        'channel_id': CHANNEL,
        'author_token': TOKEN[:16] + '…',
        'text': 'Offline replay',
        'ts': 123.0,
        'iso': '2026-07-29T12:00:00',
        'client_message_id': CLIENT_ID,
    }
    with (
        A.app.test_request_context(
            method='POST',
            json={
                'text': 'Offline replay',
                'client_message_id': CLIENT_ID,
            },
        ),
        patch.object(A, '_chat_path', return_value='/tmp/chat.json'),
        patch.object(A, '_channel_access_error', return_value=None),
        patch.object(A, '_dm_load_messages', return_value=[existing]),
        patch.object(A, '_dm_messages_save_to_supabase') as save,
        patch.object(A, '_chat_push_fanout_async') as push,
        patch.object(A, '_token_rate_limited') as rate_limit,
    ):
        response = A.send_chat_message(TOKEN, CHANNEL)

    assert response.status_code == 200
    assert response.get_json()['message']['id'] == 'server-1'
    save.assert_not_called()
    push.assert_not_called()
    rate_limit.assert_not_called()


def test_chat_client_message_id_cannot_be_reused_for_other_text():
    existing = {
        'id': 'server-1',
        'author_token': TOKEN[:16] + '…',
        'text': 'First text',
        'client_message_id': CLIENT_ID,
    }
    with (
        A.app.test_request_context(
            method='POST',
            json={
                'text': 'Different text',
                'client_message_id': CLIENT_ID,
            },
        ),
        patch.object(A, '_chat_path', return_value='/tmp/chat.json'),
        patch.object(A, '_channel_access_error', return_value=None),
        patch.object(A, '_dm_load_messages', return_value=[existing]),
    ):
        payload, status = A.send_chat_message(TOKEN, CHANNEL)

    assert status == 409
    assert payload.get_json()['error'] == 'client_message_id_conflict'


def test_chat_rejects_malformed_client_message_id_before_storage():
    with A.app.test_request_context(
        method='POST',
        json={'text': 'Hello', 'client_message_id': '../unsafe'},
    ):
        payload, status = A.send_chat_message(TOKEN, CHANNEL)

    assert status == 400
    assert payload.get_json()['error'] == 'invalid_client_message_id'
