from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import app as A
import blueprints.family_watch as FW
import blueprints.feed_status_blueprint as F


CREW = 'AT-CREW-1234567890'
FAMILY = 'AT-FAMILY-12345678'
CREATED = '2026-07-14T10:00:00+00:00'


def _reply_row(outcome='saved'):
    now = datetime.now(timezone.utc)
    return {
        'outcome': outcome,
        'family_token': FAMILY,
        'crew_token': CREW,
        'created_at': CREATED,
        'expires_at': (now + timedelta(hours=1)).isoformat(),
        'reply_body': 'Bin gut angekommen.',
        'reply_created_at': now.isoformat(),
        'reply_expires_at': (now + timedelta(hours=24)).isoformat(),
        'reply_idempotency_key': 'reply_operation_123',
    }


def test_reply_endpoint_saves_and_pushes_once():
    push = MagicMock()
    with A.app.test_request_context(json={
        'text': 'Bin gut angekommen.',
        'created_at': CREATED,
        'idempotency_key': 'reply_operation_123',
    }), patch.object(F, '_reply_result_from_rpc', return_value=_reply_row()), \
            patch.object(F, '_notify_family_of_reply', push):
        response = F.reply_to_status(CREW)

    assert response.status_code == 200
    assert response.get_json()['reply_text'] == 'Bin gut angekommen.'
    push.assert_called_once()
    assert push.call_args.args[0] == FAMILY


def test_reply_retry_is_idempotent_and_never_pushes_twice():
    push = MagicMock()
    with A.app.test_request_context(json={
        'text': 'Bin gut angekommen.',
        'created_at': CREATED,
        'idempotency_key': 'reply_operation_123',
    }), patch.object(F, '_reply_result_from_rpc',
                     return_value=_reply_row('idempotent')), \
            patch.object(F, '_notify_family_of_reply', push):
        response = F.reply_to_status(CREW)

    assert response.status_code == 200
    assert response.get_json()['idempotent'] is True
    push.assert_not_called()


def test_reply_rejects_missing_identity_and_oversized_text():
    with A.app.test_request_context(json={
        'text': 'x' * 281,
        'created_at': CREATED,
        'idempotency_key': 'reply_operation_123',
    }):
        response, status = F.reply_to_status(CREW)
    assert status == 400
    assert response.get_json()['error'] == 'text_too_long'

    with A.app.test_request_context(json={
        'text': 'ok', 'created_at': CREATED, 'idempotency_key': 'short',
    }):
        response, status = F.reply_to_status(CREW)
    assert status == 400
    assert response.get_json()['error'] == 'invalid_idempotency_key'


def test_family_get_keeps_reply_for_full_ttl_without_extending_parent():
    now = datetime.now(timezone.utc)
    row = {
        'body': 'Alter Family-Text',
        'created_at': (now - timedelta(hours=25)).isoformat(),
        'expires_at': (now - timedelta(hours=1)).isoformat(),
        'reply_body': 'Crew-Antwort',
        'reply_created_at': (now - timedelta(minutes=5)).isoformat(),
        'reply_expires_at': (now + timedelta(hours=23, minutes=55)).isoformat(),
    }
    with A.app.test_request_context(), \
            patch.object(F, '_status_for_family', return_value=row), \
            patch.object(F, '_status_delete') as delete:
        response = F.get_family_status(FAMILY)
    body = response.get_json()
    assert body['status']['message_active'] is False
    assert body['status']['reply_active'] is True
    assert body['status']['reply_text'] == 'Crew-Antwort'
    delete.assert_not_called()


def test_family_post_has_stable_message_id_and_exact_crew_push():
    class FakeFamilyWatch:
        @staticmethod
        def _resolve_crew_for_family(_token, opaque_id=None):
            return CREW

        @staticmethod
        def _scoped_token_crew(_token):
            return None

        @staticmethod
        def _load_crew_profile(_token):
            return {'name': 'Papa'}

        @staticmethod
        def _shares_load():
            return [{'crew_token': CREW, 'family_token': FAMILY,
                     'relation': 'papa'}]

    saved = MagicMock(return_value=True)
    pushed = MagicMock()
    payload = {'text': 'Guten Flug', 'message_id': 'message_operation_123'}
    with A.app.test_request_context(json=payload), \
            patch.object(F, '_fw', return_value=FakeFamilyWatch), \
            patch.object(F, '_status_for_family', return_value=None), \
            patch.object(F, '_status_save', saved), \
            patch.object(F, '_notify_crew_of_family_message', pushed):
        response = F.post_family_status(FAMILY)
    assert response.status_code == 200
    assert saved.call_args.args[1]['message_id'] == 'message_operation_123'
    pushed.assert_called_once()
    assert pushed.call_args.args[0] == CREW

    existing = {'message_id': 'message_operation_123', 'body': 'Guten Flug',
                'created_at': CREATED,
                'expires_at': (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()}
    saved.reset_mock(); pushed.reset_mock()
    with A.app.test_request_context(json=payload), \
            patch.object(F, '_fw', return_value=FakeFamilyWatch), \
            patch.object(F, '_status_for_family', return_value=existing), \
            patch.object(F, '_status_save', saved), \
            patch.object(F, '_notify_crew_of_family_message', pushed):
        response = F.post_family_status(FAMILY)
    assert response.get_json()['idempotent'] is True
    saved.assert_not_called()
    pushed.assert_not_called()


def test_scoped_push_recipient_is_bound_or_safely_suppressed():
    scoped = 'AT-FAM-CAPABILITY-123'
    fw = SimpleNamespace(_scoped_tokens_load=lambda: {
        scoped: {'owner_token': FAMILY},
    })
    with patch.object(F, '_fw', return_value=fw):
        assert F._family_push_recipient(scoped) == FAMILY
    fw_unbound = SimpleNamespace(_scoped_tokens_load=lambda: {scoped: {}})
    with patch.object(F, '_fw', return_value=fw_unbound):
        assert F._family_push_recipient(scoped) is None


def test_existing_scoped_capability_can_bind_once_to_authenticated_family():
    scoped = 'AT-FAM-CAPABILITY-123'
    tokens = {scoped: {'crew_token': CREW, 'scope': 'family_read'}}
    saved = MagicMock(return_value=True)
    fw = SimpleNamespace(
        _authenticated_family_owner_token=lambda: FAMILY,
        _scoped_tokens_load=lambda: tokens,
        _scoped_tokens_save=saved,
    )
    with A.app.test_request_context(json={}), patch.object(F, '_fw', return_value=fw):
        response = F.bind_family_push_recipient(scoped)
    assert response.status_code == 200
    assert tokens[scoped]['owner_token'] == FAMILY
    saved.assert_called_once()

    foreign_fw = SimpleNamespace(
        _authenticated_family_owner_token=lambda: 'AT-OTHER-FAMILY-123',
        _scoped_tokens_load=lambda: tokens,
        _scoped_tokens_save=MagicMock(),
    )
    with A.app.test_request_context(json={}), \
            patch.object(F, '_fw', return_value=foreign_fw):
        response, status = F.bind_family_push_recipient(scoped)
    assert status == 409
    assert response.get_json()['error'] == 'already_bound'
    foreign_fw._scoped_tokens_save.assert_not_called()


def test_redeem_owner_binding_accepts_only_authenticated_family():
    def attrs(name, default=None):
        return {
            '_request_bearer_token': lambda: FAMILY,
            '_auth_find_user_by': lambda _column, _token: (
                'family@example.test', {'account_type': 'family'}),
        }.get(name, default)

    with patch.object(FW, '_app_attr', side_effect=attrs), \
            patch.object(FW, '_load_crew_profile', return_value={}):
        assert FW._authenticated_family_owner_token() == FAMILY

    def crew_attrs(name, default=None):
        return {
            '_request_bearer_token': lambda: CREW,
            '_auth_find_user_by': lambda _column, _token: (
                'crew@example.test', {'account_type': 'crew'}),
        }.get(name, default)

    with patch.object(FW, '_app_attr', side_effect=crew_attrs), \
            patch.object(FW, '_load_crew_profile', return_value={}):
        assert FW._authenticated_family_owner_token() is None


def test_reply_migration_is_atomic_and_service_role_only():
    sql = Path('supabase_migrations/20260714_family_status_replies.sql').read_text().lower()
    assert 'for update' in sql
    assert "interval '24 hours'" in sql
    assert "interval '3 seconds'" in sql
    assert 'reply_expires_at' in sql
    assert 'add column if not exists reaction text' in sql
    assert 'add column if not exists reacted_at timestamptz' in sql
    assert ('revoke execute on function public.set_family_status_reply'
            in sql)
    assert 'from public, anon, authenticated' in sql
    assert 'to service_role' in sql


def test_push_types_use_dedicated_family_preference():
    for push_type in ('family_message', 'family_reply', 'family_reaction'):
        assert A._PUSH_TYPE_TO_PREF[push_type] == 'family_message'
    assert 'family_message' in A._PUSH_PREF_KEYS
