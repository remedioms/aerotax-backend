from types import SimpleNamespace

import app as A


class _SessionTable:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.inserted = []
        self.filters = {}

    def select(self, *_args):
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def limit(self, _value):
        return self

    def insert(self, row):
        self.inserted.append(dict(row))
        return self

    def execute(self):
        if self.filters:
            rows = [row for row in self.rows
                    if all(row.get(k) == v for k, v in self.filters.items())]
        else:
            rows = self.rows
        return SimpleNamespace(data=rows)


class _FakeSB:
    def __init__(self, table, rpc_result=None):
        self.session_table = table
        self.rpc_result = rpc_result
        self.rpc_calls = []

    def table(self, name):
        assert name == 'auth_sessions'
        return self.session_table

    def rpc(self, name, params):
        assert name == 'consume_auth_refresh_token'
        self.rpc_calls.append(dict(params))
        result = self.rpc_result

        class _RPC:
            def execute(inner_self):
                return SimpleNamespace(data=result)

        return _RPC()


def test_modern_bearer_is_normalized_before_legacy_handlers(monkeypatch):
    monkeypatch.setattr(
        A, '_auth_session_access_principal',
        lambda token: ('valid', 'AT-LEGACY123456')
    )
    with A.app.test_request_context(
            '/api/user/history/AT-LEGACY123456',
            headers={'Authorization': 'Bearer AXA-modern'}):
        assert A._normalize_modern_session_bearer() is None
        assert A._request_bearer_token() == 'AT-LEGACY123456'


def test_modern_bearer_passes_strict_owner_gate_after_normalization(monkeypatch):
    """Neue AXA-Sessions und alte AT-URL-Vertraege funktionieren zusammen."""
    owner = 'AT-LEGACY123456'
    valid = A._TokenValidationResult(A._TokenValidationState.VALID,
                                     'known@example.test')
    monkeypatch.setattr(
        A, '_auth_session_access_principal',
        lambda token: ('valid', owner)
    )
    monkeypatch.setattr(A, '_validate_token', lambda token: valid)
    monkeypatch.setattr(A, '_BUG004_REQUIRE_TOKEN_BINDING', True)

    with A.app.test_request_context(
            f'/api/crew-chat/{owner}/channel/group__known/send',
            method='POST', headers={'Authorization': 'Bearer AXA-modern'}):
        assert A._normalize_modern_session_bearer() is None
        assert A._bug004_token_auth_gate() is None


def test_expired_modern_bearer_is_rejected(monkeypatch):
    monkeypatch.setattr(
        A, '_auth_session_access_principal',
        lambda token: ('expired', None)
    )
    with A.app.test_request_context(
            '/api/user/history/AT-LEGACY123456',
            headers={'Authorization': 'Bearer AXA-expired'}):
        response, status = A._normalize_modern_session_bearer()
        assert status == 401
        assert response.get_json()['error'] == 'token_expired'


def test_legacy_bearer_passes_through_unchanged():
    with A.app.test_request_context(
            '/api/user/history/AT-LEGACY123456',
            headers={'Authorization': 'Bearer AT-LEGACY123456'}):
        assert A._normalize_modern_session_bearer() is None
        assert A._request_bearer_token() == 'AT-LEGACY123456'


def test_legacy_bearer_can_be_retired_after_rollout(monkeypatch):
    monkeypatch.setattr(A, '_AUTH_ACCEPT_LEGACY_BEARER', False)
    with A.app.test_request_context(
            '/api/user/history/AT-LEGACY123456',
            headers={'Authorization': 'Bearer AT-LEGACY123456'}):
        response, status = A._normalize_modern_session_bearer()
        assert status == 401
        assert response.get_json()['error'] == 'legacy_token_retired'


def test_me_crew_alias_derives_owner_only_from_bearer(monkeypatch):
    """Android's URL has no owner credential; legacy handler keeps ownership."""
    owner = 'AT-LEGACY123456'
    valid = A._TokenValidationResult(A._TokenValidationState.VALID,
                                     'known@example.test')
    monkeypatch.setattr(A, '_validate_token', lambda token: valid)
    seen = []
    monkeypatch.setattr(
        A, 'get_dm_inbox',
        lambda token: (seen.append(token), A.jsonify({'ok': True}))[1],
    )

    response = A.app.test_client().get(
        '/api/me/crew-chat/inbox',
        headers={'Authorization': 'Bearer ' + owner},
    )

    assert response.status_code == 200
    assert seen == [owner]


def test_me_routes_never_put_owner_credential_in_url_templates():
    rules = {rule.rule for rule in A.app.url_map.iter_rules()}
    expected = {
        '/api/me/friends', '/api/me/crew-chat/inbox',
        '/api/me/hotel-rooms/report', '/api/me/trade/post',
        '/api/me/forum/threads', '/api/me/roster',
        '/api/me/friend-roster/<friend_token>',
        '/api/me/ax/daily-briefing', '/api/me/push/prefs',
        '/api/me/entitlement', '/api/me/flight-ops',
    }
    assert expected <= rules
    assert not any('<token>' in rule for rule in rules if rule.startswith('/api/me/'))


def test_issued_session_persists_hashes_not_raw_tokens(monkeypatch):
    table = _SessionTable()
    monkeypatch.setattr(A, 'SB_AVAILABLE', True)
    monkeypatch.setattr(A, 'sb', _FakeSB(table))

    issued = A._auth_session_issue('AT-LEGACY123456', user_id='USR-123')

    assert issued['access_token'].startswith('AXA-')
    assert issued['refresh_token'].startswith('AXR-')
    row = table.inserted[0]
    assert row['access_hash'] == A._auth_session_hash(issued['access_token'])
    assert row['refresh_hash'] == A._auth_session_hash(issued['refresh_token'])
    assert issued['access_token'] not in row.values()
    assert issued['refresh_token'] not in row.values()
    assert A._auth_parse_time(row['access_expires_at']) < A._auth_parse_time(
        row['refresh_expires_at'])


def test_access_lookup_enforces_expiry(monkeypatch):
    token = 'AXA-expired-test'
    table = _SessionTable([{
        'access_hash': A._auth_session_hash(token),
        'user_token': 'AT-LEGACY123456',
        'access_expires_at': '2020-01-01T00:00:00Z',
        'revoked_at': None,
    }])
    monkeypatch.setattr(A, 'SB_AVAILABLE', True)
    monkeypatch.setattr(A, 'sb', _FakeSB(table))
    A._AUTH_SESSION_ACCESS_CACHE.clear()

    assert A._auth_session_access_principal(token) == ('expired', None)


def test_refresh_rotation_uses_atomic_single_consumer(monkeypatch):
    refresh = 'AXR-refresh-test'
    access_hash = A._auth_session_hash('AXA-old-test')
    table = _SessionTable([{
        'access_hash': access_hash,
        'refresh_hash': A._auth_session_hash(refresh),
        'user_token': 'AT-LEGACY123456',
        'user_id': 'USR-123',
        'refresh_expires_at': '2099-01-01T00:00:00Z',
        'revoked_at': None,
        'rotated_at': None,
    }])
    fake_sb = _FakeSB(table, rpc_result=[{
        'access_hash': access_hash,
        'user_token': 'AT-LEGACY123456',
        'user_id': 'USR-123',
    }])
    monkeypatch.setattr(A, 'SB_AVAILABLE', True)
    monkeypatch.setattr(A, 'sb', fake_sb)
    monkeypatch.setattr(A, '_auth_session_issue', lambda *_args, **_kwargs: {
        'access_token': 'AXA-new',
        'refresh_token': 'AXR-new',
        'access_expires_at': '2026-08-07T00:00:00Z',
        'refresh_expires_at': '2026-11-06T00:00:00Z',
        'token_type': 'Bearer',
    })

    state, issued = A._auth_session_refresh(refresh)

    assert state == 'valid'
    assert issued['access_token'] == 'AXA-new'
    assert fake_sb.rpc_calls[0]['p_refresh_hash'] == A._auth_session_hash(refresh)


def test_refresh_replay_cannot_issue_second_session(monkeypatch):
    refresh = 'AXR-replayed-test'
    table = _SessionTable([{
        'refresh_hash': A._auth_session_hash(refresh),
        'refresh_expires_at': '2099-01-01T00:00:00Z',
        'revoked_at': None,
        'rotated_at': None,
    }])
    monkeypatch.setattr(A, 'SB_AVAILABLE', True)
    monkeypatch.setattr(A, 'sb', _FakeSB(table, rpc_result=[]))
    monkeypatch.setattr(
        A, '_auth_session_issue',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError('replayed refresh must not issue a session')))

    assert A._auth_session_refresh(refresh) == ('invalid', None)


def test_auth_payload_keeps_legacy_contract_and_adds_modern_fields(monkeypatch):
    user = {'token': 'AT-LEGACY123456', 'user_id': 'USR-123'}
    monkeypatch.setattr(A, '_auth_session_issue', lambda *_args, **_kwargs: {
        'access_token': 'AXA-new',
        'refresh_token': 'AXR-new',
        'access_expires_at': '2026-08-07T00:00:00Z',
        'refresh_expires_at': '2026-11-06T00:00:00Z',
        'token_type': 'Bearer',
    })

    payload = A._auth_success_payload('crew@example.com', user)

    assert payload['token'] == 'AT-LEGACY123456'
    assert payload['user_id'] == 'USR-123'
    assert payload['access_token'] == 'AXA-new'


def test_auth_routing_hint_is_minimal_and_omitted_without_profile(monkeypatch):
    user = {'token': 'AT-ROUTING123456', 'user_id': 'USR-ROUTING'}
    monkeypatch.setattr(A, '_profile_load', lambda _token: {
        'profile': {'account_type': 'crew', 'airline': 'LH'}})
    assert A._auth_routing_hint(user) == {
        'account_kind': 'crew', 'onboarding_completed': True}

    monkeypatch.setattr(A, '_profile_load', lambda _token: {'profile': {}})
    assert A._auth_routing_hint(user) is None


def test_auth_routing_hint_preserves_family_and_incomplete_crew(monkeypatch):
    user = {'token': 'AT-ROUTING123456', 'user_id': 'USR-ROUTING'}
    monkeypatch.setattr(A, '_profile_load', lambda _token: {
        'profile': {'account_type': 'family'}})
    assert A._auth_routing_hint(user) == {
        'account_kind': 'family', 'onboarding_completed': True}

    monkeypatch.setattr(A, '_profile_load', lambda _token: {
        'profile': {'account_type': 'crew', 'name': 'New Crew'}})
    assert A._auth_routing_hint(user) == {
        'account_kind': 'crew', 'onboarding_completed': False}


def test_auth_routing_hint_accepts_established_employer_and_fails_closed(monkeypatch):
    user = {'token': 'AT-ROUTING123456', 'user_id': 'USR-ROUTING'}
    monkeypatch.setattr(A, '_profile_load', lambda _token: {
        'profile': {'employers': [{'airline_icao': 'DLH'}]}})
    assert A._auth_routing_hint(user) == {
        'account_kind': 'crew', 'onboarding_completed': True}

    def unavailable(_token):
        raise RuntimeError('profile unavailable')

    monkeypatch.setattr(A, '_profile_load', unavailable)
    assert A._auth_routing_hint(user) is None
