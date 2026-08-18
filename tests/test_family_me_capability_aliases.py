"""Focused contracts for Family capability header aliases."""

import sys

import app as A
import pytest
from blueprints import family_watch as FW
from blueprints import feed_status_blueprint as FS


SCOPED = 'AT-FAM-abcdefghijk'
ACCOUNT = 'AT-abcdefghijk'


def _valid(_token):
    return A._TokenValidationResult(A._TokenValidationState.VALID, 'family@test')


def test_family_header_aliases_require_the_capability_not_current_session(monkeypatch):
    monkeypatch.setattr(FW, '_scoped_token_crew', lambda token: 'AT-crew' if token == SCOPED else None)
    seen = []
    monkeypatch.setattr(FW, 'family_watch_feed', lambda token: seen.append(token) or A.jsonify({'ok': True}))
    with A.app.test_request_context('/api/me/family-watch/feed'):
        response = A.app.make_response(FW.me_family_watch_feed())
    assert response.status_code == 401
    with A.app.test_request_context('/api/me/family-watch/feed', headers={'Authorization': f'Bearer {SCOPED}'}):
        response = A.app.make_response(FW.me_family_watch_feed())
    assert response.status_code == 200
    assert seen == [SCOPED]


def test_family_account_bearer_is_validated_before_alias_dispatch(monkeypatch):
    # Blueprint helpers resolve app lazily through sys.modules so they remain
    # correct after test_calculation's deliberate re-import. Pin this test's
    # monkeypatched module for the request and let monkeypatch restore it.
    monkeypatch.setitem(sys.modules, 'app', A)
    monkeypatch.setattr(A, '_validate_token', _valid)
    monkeypatch.setattr(
        A,
        '_auth_find_user_by',
        lambda column, token: (
            'family@test',
            {'token': token, 'account_type': 'family'},
        ) if column == 'token' and token == ACCOUNT else (None, None),
    )
    monkeypatch.setattr(FW, '_load_crew_profile', lambda _token: {})
    seen = []
    monkeypatch.setattr(FW, 'family_roster', lambda token: seen.append(token) or A.jsonify({'ok': True}))
    with A.app.test_request_context('/api/me/family-roster', headers={'Authorization': f'Bearer {ACCOUNT}'}):
        response = A.app.make_response(FW.me_family_roster())
    assert response.status_code == 200
    assert seen == [ACCOUNT]


@pytest.mark.parametrize('account_type', ['crew', None, ''])
def test_family_account_alias_forbids_valid_non_family_accounts(
    monkeypatch, account_type
):
    """Validity alone never grants Family roster access."""
    monkeypatch.setitem(sys.modules, 'app', A)
    monkeypatch.setattr(A, '_validate_token', _valid)
    monkeypatch.setattr(
        A,
        '_auth_find_user_by',
        lambda column, token: (
            'other@test',
            {'token': token, **(
                {'account_type': account_type}
                if account_type is not None else {}
            )},
        ) if column == 'token' and token == ACCOUNT else (None, None),
    )
    monkeypatch.setattr(FW, '_load_crew_profile', lambda _token: {})
    seen = []
    monkeypatch.setattr(
        FW,
        'family_roster',
        lambda token: seen.append(token) or A.jsonify({'ok': True}),
    )

    with A.app.test_request_context(
        '/api/me/family-roster', headers={'Authorization': f'Bearer {ACCOUNT}'}
    ):
        response = A.app.make_response(FW.me_family_roster())

    assert response.status_code == 403
    assert response.get_json()['error'] == 'family_account_required'
    assert seen == []


def test_scoped_family_capability_remains_accepted_without_account_lookup(monkeypatch):
    """Legacy `AT-FAM-…` capabilities stay least-privilege Family credentials."""
    monkeypatch.setitem(sys.modules, 'app', A)
    monkeypatch.setattr(FW, '_scoped_token_crew', lambda token: 'AT-crew' if token == SCOPED else None)
    finder = []
    monkeypatch.setattr(A, '_auth_find_user_by', lambda *_args: finder.append(_args))

    with A.app.test_request_context(
        '/api/me/family-roster', headers={'Authorization': f'Bearer {SCOPED}'}
    ):
        token, error = FW.family_bearer_capability()

    assert token == SCOPED
    assert error is None
    assert finder == []


def test_combined_week_forbids_a_valid_crew_account_before_roster_lookup(
    monkeypatch,
):
    """The new batch route inherits the same Family-only server boundary."""
    monkeypatch.setitem(sys.modules, 'app', A)
    monkeypatch.setattr(A, '_validate_token', _valid)
    monkeypatch.setattr(
        A,
        '_auth_find_user_by',
        lambda column, token: (
            'crew@test', {'token': token, 'account_type': 'crew'}
        ) if column == 'token' and token == ACCOUNT else (None, None),
    )
    monkeypatch.setattr(FW, '_load_crew_profile', lambda _token: {})
    calls = []
    monkeypatch.setattr(
        FW,
        'family_roster_week_payload',
        lambda *args: calls.append(args) or {'ok': True},
    )

    with A.app.test_request_context(
        '/api/me/family-roster/week?days=7',
        headers={'Authorization': f'Bearer {ACCOUNT}'},
    ):
        response = A.app.make_response(FW.me_family_roster_week())

    assert response.status_code == 403
    assert response.get_json()['error'] == 'family_account_required'
    assert calls == []


def test_feed_status_alias_delegates_only_the_verified_capability(monkeypatch):
    monkeypatch.setattr(FS, '_family_bearer_capability', lambda: (SCOPED, None))
    seen = []
    monkeypatch.setattr(FS, 'get_family_status', lambda token: seen.append(token) or A.jsonify({'ok': True}))
    with A.app.test_request_context('/api/me/feed-status/family'):
        response = A.app.make_response(FS.me_get_family_status())
    assert response.status_code == 200
    assert seen == [SCOPED]


@pytest.mark.parametrize(
    ('path', 'method', 'alias_name', 'handler_name'),
    [
        ('/api/me/feed-status/incoming', 'GET', 'me_incoming_statuses', 'incoming_statuses'),
        ('/api/me/feed-status/react', 'POST', 'me_react_to_status', 'react_to_status'),
        ('/api/me/feed-status/reply', 'POST', 'me_reply_to_status', 'reply_to_status'),
    ],
)
def test_crew_feed_status_aliases_derive_only_the_verified_owner(
    monkeypatch, path, method, alias_name, handler_name
):
    crew = 'AT-crew-owner'
    monkeypatch.setattr(FS, '_crew_bearer_owner', lambda: (crew, None))
    seen = []
    monkeypatch.setattr(
        FS,
        handler_name,
        lambda token: seen.append(token) or A.jsonify({'ok': True}),
    )
    with A.app.test_request_context(path, method=method, json={}):
        response = A.app.make_response(getattr(FS, alias_name)())
    assert response.status_code == 200
    assert seen == [crew]


def test_family_connection_revoke_resolves_opaque_id_and_removes_only_own_share(monkeypatch):
    crew = 'AT-crew-owner'
    other_crew = 'AT-other-crew'
    other_family = 'AT-FAM-other'
    opaque = '0123456789abcdef'
    shares = [
        {'crew_token': crew, 'family_token': SCOPED},
        {'crew_token': other_crew, 'family_token': SCOPED},
        {'crew_token': crew, 'family_token': other_family},
    ]
    saved = []
    monkeypatch.setattr(FW, 'family_bearer_capability', lambda: (SCOPED, None))
    monkeypatch.setattr(
        FW, '_resolve_crew_for_family',
        lambda family, opaque_id=None: crew
        if family == SCOPED and opaque_id == opaque else None,
    )
    monkeypatch.setattr(FW, '_shares_load', lambda: list(shares))
    monkeypatch.setattr(FW, '_shares_save', lambda value: saved.append(value) or True)
    monkeypatch.setattr(FW, '_scoped_tokens_load', lambda: {})
    monkeypatch.setattr(FW, '_get_sb', lambda: (False, None))

    with A.app.test_request_context(
        f'/api/me/family-watch/connection/{opaque}', method='DELETE'
    ):
        response = A.app.make_response(
            FW.me_family_watch_revoke_connection(opaque))

    assert response.status_code == 200
    assert saved == [[
        {'crew_token': other_crew, 'family_token': SCOPED},
        {'crew_token': crew, 'family_token': other_family},
    ]]


def test_revoke_removes_the_crew_from_a_subsequent_combined_roster_read(monkeypatch):
    crew = 'AT-crew-owner'
    opaque = '0123456789abcdef'
    shares = [{'crew_token': crew, 'family_token': SCOPED}]

    def save(value):
        shares[:] = value
        return True

    monkeypatch.setattr(FW, 'family_bearer_capability', lambda: (SCOPED, None))
    monkeypatch.setattr(FW, '_shares_load', lambda: list(shares))
    monkeypatch.setattr(FW, '_shares_save', save)
    monkeypatch.setattr(FW, '_scoped_tokens_load', lambda: {})
    monkeypatch.setattr(FW, '_get_sb', lambda: (False, None))
    monkeypatch.setattr(
        FW,
        '_resolve_crew_for_family',
        lambda family, opaque_id=None: crew
        if family == SCOPED and opaque_id == opaque and shares else None,
    )
    monkeypatch.setattr(
        FW,
        '_resolve_crews_for_family',
        lambda family: [row['crew_token'] for row in shares
                        if row.get('family_token') == family],
    )

    with A.app.test_request_context(
        f'/api/me/family-watch/connection/{opaque}', method='DELETE'
    ):
        response = A.app.make_response(FW.me_family_watch_revoke_connection(opaque))
    assert response.status_code == 200
    assert FW.family_roster_week_payload(SCOPED, 7)['crew'] == []
