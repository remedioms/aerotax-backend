"""Authenticated Wall-comment ownership projection stays credential-free."""
import os

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import app as A


def test_comment_projection_derives_is_mine_without_exposing_author_token(
    monkeypatch,
):
    rows = [
        {'id': 'mine', 'text': 'Meine Antwort', 'author_token': 'AT-owner'},
        {'id': 'other', 'text': 'Fremde Antwort', 'author_token': 'AT-other'},
    ]
    monkeypatch.setattr(A, '_wall_comments_path', lambda _post: '/tmp/wall')
    monkeypatch.setattr(A, '_wall_comments_load', lambda _post: rows)
    monkeypatch.setattr(A, '_blocked_by', lambda _token: set())
    monkeypatch.setattr(A, '_profile_load', lambda _token: {})

    with A.app.test_request_context('/api/me/wall/post/post_1/comments'):
        payload = A.get_comments('AT-owner', 'post_1').get_json()
    by_id = {comment['id']: comment for comment in payload['comments']}

    assert by_id['mine']['is_mine'] is True
    assert by_id['other']['is_mine'] is False
    assert all('author_token' not in comment for comment in payload['comments'])


def test_header_wall_comments_binds_only_the_authenticated_owner(monkeypatch):
    owner = 'AT-owner'
    seen = []
    monkeypatch.setattr(
        A,
        '_validate_token',
        lambda token: A._TokenValidationResult(
            A._TokenValidationState.VALID if token == owner
            else A._TokenValidationState.INVALID,
            'owner@example.test' if token == owner else None,
        ),
    )
    monkeypatch.setattr(
        A,
        'get_comments',
        lambda token, post_id: seen.append((token, post_id)) or A.jsonify({
            'post_id': post_id,
            'comments': [{'id': 'comment_1', 'text': 'Hi', 'is_mine': True}],
        }),
    )

    response = A.app.test_client().get(
        '/api/me/wall/post/post_1/comments',
        headers={'Authorization': f'Bearer {owner}'},
    )

    assert response.status_code == 200
    assert response.get_json()['comments'][0]['is_mine'] is True
    assert seen == [(owner, 'post_1')]
