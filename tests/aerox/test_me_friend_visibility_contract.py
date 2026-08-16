"""Security and persistence contract for Android per-friend roster sharing."""
import os
import sys

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app as A


OWNER = 'AT-AAAAAAAAAAAAAAAA'
FRIEND = 'AT-BBBBBBBBBBBBBBBB'
REF = 'AXU-safe-friend-reference'


def _response(value):
    if isinstance(value, tuple):
        return value[0], value[1]
    return value, value.status_code


def _prepare(monkeypatch, *, friends=(FRIEND,), profile=None):
    saved = {}
    monkeypatch.setattr(A, '_header_only_owner', lambda: (OWNER, None))
    monkeypatch.setattr(A, '_token_from_public_user_ref',
                        lambda value: FRIEND if value == REF else None)
    monkeypatch.setattr(A, '_friends_load', lambda _: {'friends': list(friends)})
    monkeypatch.setattr(A, '_profile_load', lambda _, fresh=False: {
        'token': OWNER, 'profile': profile or {}})
    monkeypatch.setattr(A, '_profile_save', lambda _, value, **__: saved.update(value) is None)
    monkeypatch.setattr(A, '_profile_memo_invalidate', lambda _: None)
    monkeypatch.setattr(A, '_invalidate_friend_visibility_memos', lambda *_: None)
    return saved


def test_me_visibility_requires_header_owner_and_rejects_queries(monkeypatch):
    with A.app.test_request_context('/api/me/friend-visibility/' + REF):
        monkeypatch.setattr(A, '_header_only_owner', lambda: (None, (A.jsonify({'ok': False}), 401)))
        _, status = _response(A.me_friend_visibility(REF))
    assert status == 401
    _prepare(monkeypatch)
    with A.app.test_request_context('/api/me/friend-visibility/' + REF + '?token=' + OWNER):
        _, status = _response(A.me_friend_visibility(REF))
    assert status == 400


def test_me_visibility_accepts_only_axu_mutual_friend_and_strict_bool(monkeypatch):
    _prepare(monkeypatch)
    with A.app.test_request_context('/api/me/friend-visibility/' + FRIEND):
        _, status = _response(A.me_friend_visibility(FRIEND))
    assert status == 400
    with A.app.test_request_context('/api/me/friend-visibility/' + REF, method='PUT', json={'share_sick_status': 'false'}):
        _, status = _response(A.me_friend_visibility(REF))
    assert status == 400
    _prepare(monkeypatch, friends=())
    with A.app.test_request_context('/api/me/friend-visibility/' + REF):
        _, status = _response(A.me_friend_visibility(REF))
    assert status == 403


def test_me_visibility_persists_server_truth_without_credential_leak(monkeypatch):
    saved = _prepare(monkeypatch)
    with A.app.test_request_context('/api/me/friend-visibility/' + REF, method='PUT', json={'share_sick_status': False}):
        response, status = _response(A.me_friend_visibility(REF))
    assert status == 200
    payload = response.get_json()
    assert payload['ok'] is True and payload['share_sick_status'] is False
    assert OWNER not in str(payload) and FRIEND not in str(payload)
    assert saved['friend_visibility'][FRIEND]['share_sick_status'] is False
