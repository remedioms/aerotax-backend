from datetime import datetime, timedelta
from unittest.mock import patch

import app as A


EMAIL = 'jenni@example.test'


def _forgot(store, sent):
    def get_user(email):
        row = store.get(email)
        return dict(row) if row is not None else None

    def upsert(email, row):
        store[email] = dict(row)
        return True

    with patch.object(A, '_auth_get_user', side_effect=get_user), \
         patch.object(A, '_auth_upsert_user', side_effect=upsert), \
         patch.object(A, '_send_password_reset_email',
                      side_effect=lambda email, token: sent.append(token) or True), \
         patch.object(A, '_token_rate_limited', return_value=False), \
         patch.object(A, '_ip_rate_limited', return_value=False), \
         patch.object(A, '_durable_auth_rate_limited', return_value=False):
        with A.app.test_request_context(
                '/api/auth/forgot', method='POST', json={'email': EMAIL}):
            response = A.auth_forgot()
    assert response.get_json()['ok'] is True


def test_repeated_forgot_keeps_the_still_valid_code():
    expires = (datetime.now() + timedelta(minutes=45)).isoformat()
    store = {EMAIL: {
        'token': 'AT-RESET-TEST-1234',
        'reset_token': 'abc123abc123abc123abc123',
        'reset_expires': expires,
    }}
    sent = []

    _forgot(store, sent)
    _forgot(store, sent)

    assert sent == ['abc123abc123abc123abc123'] * 2
    assert store[EMAIL]['reset_expires'] == expires
    assert store[EMAIL]['reset_used_at'] is None


def test_new_forgot_explicitly_clears_previous_used_marker():
    store = {EMAIL: {
        'token': 'AT-RESET-TEST-1234',
        'reset_token': 'old-code',
        'reset_expires': (datetime.now() + timedelta(minutes=45)).isoformat(),
        'reset_used_at': datetime.now().isoformat(),
    }}
    sent = []

    _forgot(store, sent)

    assert len(sent) == 1
    assert sent[0] != 'old-code'
    assert store[EMAIL]['reset_token'] == sent[0]
    assert store[EMAIL]['reset_used_at'] is None
    # `None` muss als Spaltenwert in den Supabase-Upsert gelangen; bloßes
    # Entfernen des Keys würde den alten Marker erhalten.
    assert A._auth_user_row(EMAIL, store[EMAIL])['reset_used_at'] is None


def test_successful_reset_explicitly_nulls_token_columns():
    row = {
        'token': 'AT-RESET-TEST-1234',
        'reset_token': 'abc123abc123abc123abc123',
        'reset_expires': (datetime.now() + timedelta(minutes=45)).isoformat(),
    }
    saved = {}

    with patch.object(A, '_auth_get_user', return_value=dict(row)), \
         patch.object(A, '_password_hash', return_value='hashed'), \
         patch.object(A, '_auth_upsert_user',
                      side_effect=lambda email, user: saved.update(user) or True), \
         patch.object(A, '_auth_session_revoke_all', return_value=True):
        with A.app.test_request_context('/api/auth/reset', method='POST', json={
                'email': EMAIL,
                'reset_token': row['reset_token'],
                'new_password': 'SafePassword123',
        }):
            response = A.auth_reset()

    assert response.get_json()['ok'] is True
    assert saved['password_hash'] == 'hashed'
    assert saved['reset_token'] is None
    assert saved['reset_expires'] is None
    assert saved.get('reset_used_at')
