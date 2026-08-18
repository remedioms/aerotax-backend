"""Static guard for backend-owned public tables with bearer-like identifiers.

This test intentionally never contacts Supabase.  It protects the migration
contract that removes every Data API role's inherited table privileges while
the backend retains the service-role-only persistence path.
"""

from pathlib import Path


_TABLES = ('family_requests', 'feed_statuses', 'flight_observations')


def test_user_data_rls_guard_enables_rls_and_revokes_all_data_api_roles():
    sql = Path(
        'supabase_migrations/20260817_user_data_tables_rls_guard.sql'
    ).read_text().lower()

    for table in _TABLES:
        assert (
            f'alter table if exists public.{table} enable row level security;'
            in sql
        )
        assert (
            f'revoke all on table public.{table} '
            'from public, anon, authenticated;'
        ) in sql


def test_guard_is_explicitly_service_role_backend_only():
    sql = Path(
        'supabase_migrations/20260817_user_data_tables_rls_guard.sql'
    ).read_text().lower()

    # The migration must not create a browser/mobile policy or grant.  These
    # tables contain internal tokens and are accessed by backend service role.
    assert 'create policy' not in sql
    assert not any(
        line.lstrip().startswith('grant ')
        for line in sql.splitlines()
    )
    assert 'service_role' in sql
