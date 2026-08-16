"""Header-authenticated Android forum mutation contract."""

import app as A


def test_me_forum_mutation_routes_have_no_owner_path_parameter():
    rules = {rule.rule for rule in A.app.url_map.iter_rules()}
    expected = {
        '/api/me/forum/threads/<thread_id>',
        '/api/me/forum/replies/<reply_id>',
        '/api/me/forum/replies/<reply_id>/like',
    }
    assert expected <= rules
    assert not any(
        '<token>' in rule for rule in rules if rule.startswith('/api/me/forum/')
    )


def test_me_forum_delete_thread_rejects_invalid_resource_id(monkeypatch):
    monkeypatch.setattr(
        A, '_header_only_dispatch',
        lambda *_args: (_ for _ in ()).throw(AssertionError('must not dispatch')),
    )
    with A.app.test_request_context('/api/me/forum/threads/invalid', method='DELETE'):
        response, status = A.me_forum_delete_thread('not a valid id')
    assert status == 400
    assert response.get_json()['error'] == 'invalid_thread_id'


def test_me_forum_delete_reply_dispatches_authenticated_owner_only(monkeypatch):
    seen = []
    monkeypatch.setattr(
        A, '_header_only_dispatch',
        lambda handler, *args: seen.append((handler, args)) or A.jsonify({'ok': True}),
    )
    with A.app.test_request_context('/api/me/forum/replies/r_123', method='DELETE'):
        response = A.me_forum_delete_reply('r_123')
    assert response.status_code == 200
    assert seen == [(A.forum_delete_reply, ('r_123',))]


def test_me_forum_reply_like_requires_visible_unblocked_parent(monkeypatch):
    owner = 'AT-OWNER123456'
    seen = []
    monkeypatch.setattr(A, '_header_only_owner', lambda: (owner, None))
    monkeypatch.setattr(
        A, '_me_forum_reply_get',
        lambda _reply_id: {'id': 'reply-1', 'thread_id': 'thread-1', 'author_token': 'AT-OTHER'},
    )
    monkeypatch.setattr(A, '_me_forum_thread_visible_to', lambda *_args: True)
    monkeypatch.setattr(A, '_blocked_by', lambda _token: set())
    monkeypatch.setattr(
        A, 'forum_toggle_reply_like',
        lambda token, reply_id: seen.append((token, reply_id)) or A.jsonify({
            'ok': True, 'like_count': 1, 'liked_by_me': True,
        }),
    )
    with A.app.test_request_context('/api/me/forum/replies/reply-1/like', method='POST'):
        response = A.me_forum_toggle_reply_like('reply-1')
    assert response.status_code == 200
    assert seen == [(owner, 'reply-1')]


def test_me_forum_reply_like_hides_non_member_target(monkeypatch):
    monkeypatch.setattr(A, '_header_only_owner', lambda: ('AT-OWNER123456', None))
    monkeypatch.setattr(
        A, '_me_forum_reply_get',
        lambda _reply_id: {'id': 'reply-1', 'thread_id': 'thread-1', 'author_token': 'AT-OTHER'},
    )
    monkeypatch.setattr(A, '_me_forum_thread_visible_to', lambda *_args: False)
    monkeypatch.setattr(A, '_blocked_by', lambda _token: set())
    with A.app.test_request_context('/api/me/forum/replies/reply-1/like', method='POST'):
        response, status = A.me_forum_toggle_reply_like('reply-1')
    assert status == 404
    assert response.get_json()['error'] == 'not_found'
