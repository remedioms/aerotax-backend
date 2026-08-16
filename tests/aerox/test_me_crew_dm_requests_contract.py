"""Header-only Android aliases for the established crew-DM request contract."""
import os
import sys

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app as A


OWNER = 'AT-AAAAAAAAAAAAAAAA'
PEER = 'AT-BBBBBBBBBBBBBBBB'
REF = 'AXU-crew-dm-peer'


def _response(value):
    if isinstance(value, tuple):
        return value[0], value[1]
    return value, value.status_code


def _prepare(monkeypatch):
    monkeypatch.setattr(A, '_header_only_owner', lambda: (OWNER, None))
    monkeypatch.setattr(A, '_token_from_public_user_ref',
                        lambda value: PEER if value == REF else None)
    monkeypatch.setattr(A, '_publicize_foreign_user_refs',
                        lambda value, viewer_token=None: value)


def test_dm_request_aliases_require_header_owner(monkeypatch):
    monkeypatch.setattr(A, '_header_only_owner',
                        lambda: (None, (A.jsonify({'ok': False}), 401)))
    with A.app.test_request_context('/api/me/crew-chat/requests'):
        _, status = _response(A.me_crew_dm_requests())
    assert status == 401
    with A.app.test_request_context('/api/me/crew-chat/requests/' + REF + '/status'):
        _, status = _response(A.me_crew_dm_request_status(REF))
    assert status == 401


def test_dm_request_target_must_be_opaque_validated_axu(monkeypatch):
    _prepare(monkeypatch)
    called = []
    monkeypatch.setattr(A, 'crew_dm_request_status',
                        lambda owner, target: called.append((owner, target)) or A.jsonify({'ok': True}))
    with A.app.test_request_context('/api/me/crew-chat/requests/' + PEER + '/status'):
        _, status = _response(A.me_crew_dm_request_status(PEER))
    assert status == 400 and called == []
    with A.app.test_request_context('/api/me/crew-chat/requests/' + REF + '/status?token=' + OWNER):
        _, status = _response(A.me_crew_dm_request_status(REF))
    assert status == 400 and called == []
    with A.app.test_request_context('/api/me/crew-chat/requests/' + REF + '/status'):
        response, status = _response(A.me_crew_dm_request_status(REF))
    assert status == 200 and response.get_json()['ok'] is True
    assert called == [(OWNER, PEER)]


def test_dm_request_send_and_decision_use_header_owner_and_public_response(monkeypatch):
    _prepare(monkeypatch)
    calls = []
    monkeypatch.setattr(
        A, 'send_crew_dm_request',
        lambda owner, target: calls.append(('send', owner, target)) or A.jsonify(
            {'ok': True, 'request': {'peer_token': PEER}}),
    )
    monkeypatch.setattr(
        A, 'decide_crew_dm_request',
        lambda owner, target: calls.append(('decision', owner, target)) or A.jsonify(
            {'ok': True, 'peer_token': PEER}),
    )
    monkeypatch.setattr(
        A, '_publicize_foreign_user_refs',
        lambda value, viewer_token=None: {
            **value,
            'viewer': viewer_token,
            'publicized': True,
        },
    )
    with A.app.test_request_context('/api/me/crew-chat/requests/' + REF,
                                    method='POST', json={'message': 'Hello'}):
        sent, sent_status = _response(A.me_crew_dm_request_send(REF))
    with A.app.test_request_context('/api/me/crew-chat/requests/' + REF + '/decision',
                                    method='POST', json={'action': 'accept'}):
        decided, decision_status = _response(A.me_crew_dm_request_decision(REF))
    assert sent_status == 200 and decision_status == 200
    assert sent.get_json()['viewer'] == OWNER and sent.get_json()['publicized'] is True
    assert decided.get_json()['viewer'] == OWNER and decided.get_json()['publicized'] is True
    assert calls == [('send', OWNER, PEER), ('decision', OWNER, PEER)]
