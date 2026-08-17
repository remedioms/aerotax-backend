"""Regressionen fuer die Chat-Latenz-Hotpaths (2026-08-07)."""
from contextlib import contextmanager
from unittest.mock import patch

import app as A


ME = 'AT-1111111111111111'
PEER = 'AT-2222222222222222'


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, owner, table):
        self.owner = owner
        self.table = table
        self.filters = []
        self.max_rows = None

    def select(self, *args, **kwargs):
        return self

    def eq(self, column, value):
        self.filters.append(('eq', column, value))
        return self

    def in_(self, column, values):
        self.filters.append(('in', column, set(values)))
        return self

    def limit(self, value):
        self.max_rows = value
        return self

    def execute(self):
        self.owner.calls.append(self.table)
        if self.owner.error:
            raise RuntimeError('temporary database failure')
        rows = []
        for row in self.owner.rows.get(self.table, []):
            if all((row.get(col) == val if kind == 'eq'
                    else row.get(col) in val)
                   for kind, col, val in self.filters):
                rows.append(row)
        return _Result(rows[:self.max_rows] if self.max_rows else rows)


class _SB:
    def __init__(self, rows=None, error=False):
        self.rows = rows or {}
        self.error = error
        self.calls = []

    def table(self, name):
        return _Query(self, name)


@contextmanager
def _fast_sb(rows=None, error=False):
    fake = _SB(rows, error)
    with patch.multiple(A, SB_AVAILABLE=True, sb=fake):
        yield fake


def test_friend_pair_authorizes_with_one_database_read():
    rows = {'user_friends': [{
        'owner_token': ME, 'friend_token': PEER, 'status': 'accepted'}]}
    with _fast_sb(rows) as fake, patch.object(A, '_blocked_by', return_value=[]):
        assert A._crew_dm_pair_authorized(ME, PEER) is True
    assert fake.calls == ['user_friends']


def test_accepted_request_authorizes_with_two_database_reads():
    rows = {'crew_dm_requests': [{
        'sender_token': PEER, 'recipient_token': ME, 'status': 'accepted'}]}
    with _fast_sb(rows) as fake, patch.object(A, '_blocked_by', return_value=[]):
        assert A._crew_dm_pair_authorized(ME, PEER) is True
    assert fake.calls == ['user_friends', 'crew_dm_requests']


def test_authoritative_negative_does_not_expand_friend_payloads():
    with (
        _fast_sb() as fake,
        patch.object(A, '_blocked_by', return_value=[]),
        patch.object(A, '_friends_load', side_effect=AssertionError(
            'authoritative negative must not use the slow legacy path')),
    ):
        assert A._crew_dm_pair_authorized(ME, PEER) is False
    assert fake.calls == ['user_friends', 'crew_dm_requests']


def test_database_failure_keeps_legacy_fallback():
    def friends(token):
        return {'friends': [PEER] if token == ME else []}

    with (
        _fast_sb(error=True),
        patch.object(A, '_blocked_by', return_value=[]),
        patch.object(A, '_friends_load', side_effect=friends),
        patch.object(A, '_crew_dm_pair_accepted', return_value=False),
    ):
        assert A._crew_dm_pair_authorized(ME, PEER) is True


def test_full_internal_friend_token_skips_friend_list_resolution():
    with patch.object(A, '_friends_load', side_effect=AssertionError(
            'a full internal token needs no list lookup')):
        assert A._resolve_friend_token(ME, PEER) == PEER


def test_dm_wrapper_does_not_repeat_the_pair_gate():
    captured = {}

    def messages(token, channel, _access_checked=False):
        captured.update(token=token, channel=channel,
                        access_checked=_access_checked)
        return A.jsonify({'messages': []})

    with (
        A.app.test_request_context(method='GET'),
        patch.object(A, '_crew_dm_pair_authorized', return_value=True) as gate,
        patch.object(A, 'get_chat_messages', side_effect=messages),
    ):
        response = A.get_dm(ME, PEER)
    assert response.get_json() == {'messages': []}
    gate.assert_called_once_with(ME, PEER)
    assert captured['access_checked'] is True


def test_inbox_uses_edge_only_read_and_trusts_accepted_request_row():
    rows = {
        'user_friends': [{
            'owner_token': ME, 'friend_token': PEER, 'status': 'accepted'}],
        'user_profiles': [{
            'token': PEER, 'name': 'Alex Crew', 'metadata': {}}],
    }
    with (
        _fast_sb(rows) as fake,
        A.app.test_request_context(method='GET'),
        patch.object(A, '_friends_load', side_effect=AssertionError(
            'successful edge read must not expand the friend payload')),
        patch.object(A, '_crew_dm_requests_for', return_value=[]),
        patch.object(A, '_dm_lastseen_load', return_value={}),
        patch.object(A, '_dm_load_recent', return_value=[]),
    ):
        response = A.get_dm_inbox(ME)
    payload = response.get_json()
    assert payload['count'] == 1
    assert payload['inbox'][0]['friend_name'] == 'Alex Crew'
    assert fake.calls == ['user_friends', 'user_profiles']
