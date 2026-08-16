"""SEC-Regression: /api/me-Read-Routen muessen fremde AT-Credentials projizieren.

Review 16.08.2026: `/api/me` liegt bewusst ausserhalb des URL-Prefix-Redactors.
Vier Read-Contracts leakten sonst fremde AT-Credentials (= Account-Uebernahme):
Inbox (`friend_token`), Layover-Kommentare (`author_token`), Profil
(`friend_visibility`-Dict-Keys) und Hangouts (`owner_token`). Fix: jede Route
projiziert ueber `_header_only_public_dispatch` (fremdes AT -> AXU, eigenes
Credential bleibt). Diese Tests pinnen das und verhindern den Rueckfall auf
`_header_only_dispatch`.

Bekannter, dokumentierter Rest: `channel_id` (dm__AT__AT) bleibt Vorbestand,
weil die Channel-Routen ihn roh zuruecknehmen — hier bewusst NICHT getestet.
"""

import app as A

OWNER = 'AT-1234567890abcdef'
FRIEND = 'AT-cafebabedeadbeef'


def _valid(_token):
    return A._TokenValidationResult(
        A._TokenValidationState.VALID, 'owner@example.test')


def _text(resp):
    return A.app.make_response(resp).get_data(as_text=True)


def test_me_inbox_publicizes_friend_token(monkeypatch):
    monkeypatch.setattr(A, '_validate_token', _valid)
    monkeypatch.setattr(
        A, 'get_dm_inbox',
        lambda token: A.jsonify({'inbox': [{'friend_token': FRIEND}]}))
    with A.app.test_request_context(
            '/api/me/crew-chat/inbox',
            headers={'Authorization': f'Bearer {OWNER}'}):
        body = _text(A.me_chat_inbox())
    assert FRIEND not in body
    assert A._public_user_ref(FRIEND) in body


def test_me_profile_publicizes_friend_visibility_keys(monkeypatch):
    monkeypatch.setattr(A, '_validate_token', _valid)
    monkeypatch.setattr(
        A, 'get_user_profile',
        lambda token: A.jsonify({'profile': {
            'name': 'Owner',
            'friend_visibility': {FRIEND: {'sees': 'roster'}}}}))
    with A.app.test_request_context(
            '/api/me/profile',
            headers={'Authorization': f'Bearer {OWNER}'}):
        body = _text(A.me_profile())
    assert FRIEND not in body
    assert A._public_user_ref(FRIEND) in body


def test_me_layover_comments_publicizes_author_token(monkeypatch):
    monkeypatch.setattr(A, '_validate_token', _valid)
    monkeypatch.setattr(
        A, 'layover_rec_get_comments',
        lambda token, rec_id: A.jsonify(
            {'comments': [{'author_token': FRIEND, 'text': 'hi'}]}))
    with A.app.test_request_context(
            '/api/me/layover-recs/r1/comments',
            headers={'Authorization': f'Bearer {OWNER}'}):
        body = _text(A.me_layover_comments('r1'))
    assert FRIEND not in body
    assert A._public_user_ref(FRIEND) in body


def test_me_hangouts_publicizes_owner_token(monkeypatch):
    monkeypatch.setattr(A, '_validate_token', _valid)
    monkeypatch.setattr(
        A, 'list_hangouts',
        lambda token: A.jsonify(
            {'hangouts': [{'id': 'h1', 'owner_token': FRIEND}]}))
    with A.app.test_request_context(
            '/api/me/hangouts',
            headers={'Authorization': f'Bearer {OWNER}'}):
        body = _text(A.me_hangouts())
    assert FRIEND not in body
    assert A._public_user_ref(FRIEND) in body


def test_me_profile_put_stays_on_thin_dispatch(monkeypatch):
    # Mutationen duerfen NICHT durch die Projektion: AXU-Ziele werden vor dem
    # Handler aufgeloest, die Response bleibt unangetastet.
    seen = []
    monkeypatch.setattr(A, '_validate_token', _valid)
    monkeypatch.setattr(
        A, 'put_user_profile',
        lambda token: seen.append(token) or A.jsonify({'ok': True}))
    with A.app.test_request_context(
            '/api/me/profile', method='PUT', json={'name': 'x'},
            headers={'Authorization': f'Bearer {OWNER}'}):
        resp = A.me_profile()
    assert seen == [OWNER]
    assert A.app.make_response(resp).get_json()['ok'] is True
