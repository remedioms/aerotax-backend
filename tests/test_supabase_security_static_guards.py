"""Static security guards for server-only Supabase migrations.

No database is contacted here.  The checks cover the public-schema defaults
that otherwise make SECURITY DEFINER functions and backend-only tables callable
through the Data API.
"""

from pathlib import Path
import re


_ROOT = Path(__file__).resolve().parents[1]
_TABLE_DECLARATION = re.compile(
    r'^\s*create\s+(?:unlogged\s+)?table'
    r'(?:\s+if\s+not\s+exists)?\s+(?:public\.)?([a-z0-9_]+)',
    re.IGNORECASE | re.MULTILINE,
)


def _schema_sql() -> str:
    parts = [(_ROOT / 'supabase_schema.sql').read_text().lower()]
    parts.extend(path.read_text().lower() for path in (_ROOT / 'supabase_migrations').glob('*.sql'))
    return '\n'.join(parts)


def test_every_locally_declared_table_has_a_static_rls_enable_path():
    sql = _schema_sql()
    tables = set(_TABLE_DECLARATION.findall(sql))

    # This is an inventory assertion, not a claim about deployed schema state.
    # It catches every table currently declared in source, including old schema
    # bootstrap files and optional warehouse migrations.
    assert len(tables) >= 95
    for table in tables:
        direct_rls = re.search(
            rf'alter\s+table\s+(?:if\s+exists\s+)?(?:public\.)?{re.escape(table)}'
            r'\s+enable\s+row\s+level\s+security\s*;',
            sql,
        )
        # job_chunks is intentionally guarded in a dynamic compatibility block.
        dynamic_rls = f'alter table public.{table} enable row level security' in sql
        assert direct_rls or dynamic_rls, table


def test_budget_increment_definer_is_service_role_only():
    sql = Path('supabase_migrations/20260705_budget_increment.sql').read_text().lower()

    assert 'security definer' in sql
    assert 'set search_path = public' in sql
    assert (
        'revoke all on function public.ax_budget_increment(text, integer)\n'
        '    from public, anon, authenticated;'
    ) in sql
    assert (
        'grant execute on function public.ax_budget_increment(text, integer)\n'
        '    to service_role;'
    ) in sql


def test_layover_guest_tables_and_trigger_are_not_data_api_callable():
    sql = Path('supabase_migrations/20260802_layover_web_guests.sql').read_text().lower()

    for table in ('layover_guest_invites', 'layover_guest_sessions'):
        assert f'alter table public.{table} enable row level security;' in sql
        assert (
            f'revoke all on table public.{table}\n'
            '    from public, anon, authenticated;'
        ) in sql
    assert 'security definer' in sql
    assert 'set search_path = public' in sql
    assert (
        'revoke all on function public.enforce_layover_guest_limit()\n'
        '    from public, anon, authenticated;'
    ) in sql
    assert 'grant execute on function public.enforce_layover_guest_limit' not in sql


def test_legacy_schema_and_family_shares_are_backend_only():
    schema = Path('supabase_schema.sql').read_text().lower()
    shares = Path('supabase_migrations/20260602_family_shares.sql').read_text().lower()

    for table in (
        'questions', 'answers', 'upvotes', 'sessions', 'audit_logs', 'pdfs',
        'uploaded_files',
    ):
        assert f'alter table public.{table} enable row level security;' in schema
        assert (
            f'revoke all on table public.{table} from public, anon, authenticated;'
        ) in schema
    assert 'disable row level security' not in schema
    assert 'create policy family_shares_service_all' in shares
    assert (
        'revoke all on table public.family_shares from public, anon, authenticated;'
    ) in shares


def test_existing_public_definer_guards_stay_explicit():
    rls_guard = Path(
        'supabase_migrations/20260816b_restrict_rls_guard_function.sql'
    ).read_text().lower()
    push = Path(
        'supabase_migrations/20260714_push_installations_outbox.sql'
    ).read_text().lower()

    assert 'revoke all on function public.aerox_rls_auto_enable() from public;' in rls_guard
    assert 'revoke execute on function public.register_push_installation' in push
    assert 'from public, anon, authenticated' in push


def test_forward_only_server_only_acl_repair_is_idempotent_and_non_public():
    migration = Path(
        'supabase_migrations/20260818104120_server_only_rls_acl_followup.sql'
    ).read_text().lower()

    # This is the deployable repair for databases which had applied the older
    # migrations before their source files were hardened.  It must tolerate a
    # partially provisioned project rather than failing on an absent object.
    assert "to_regclass(format('public.%i', table_name)) is not null" in migration
    assert "to_regprocedure('public.ax_budget_increment(text,integer)') is not null" in migration
    assert "to_regprocedure('public.enforce_layover_guest_limit()') is not null" in migration
    assert "to_regrole('anon') is not null" in migration
    assert "to_regrole('authenticated') is not null" in migration

    for table in (
        'questions', 'answers', 'upvotes', 'sessions', 'audit_logs', 'pdfs',
        'uploaded_files', 'family_shares', 'family_requests', 'feed_statuses',
        'flight_observations', 'layover_guest_invites', 'layover_guest_sessions',
    ):
        assert f"'{table}'" in migration
    assert 'alter table public.%i enable row level security' in migration
    assert 'revoke all on table public.%i from public' in migration
    assert 'revoke all on table public.%i from anon' in migration
    assert 'revoke all on table public.%i from authenticated' in migration

    assert 'revoke all on function public.ax_budget_increment(text, integer) from public;' in migration
    assert 'grant execute on function public.ax_budget_increment(text, integer)' in migration
    assert 'to service_role;' in migration
    assert 'revoke all on function public.enforce_layover_guest_limit() from public;' in migration
    assert 'grant execute on function public.enforce_layover_guest_limit' not in migration

    # Do not turn this emergency forward repair into a guessed client API.
    assert 'create policy' not in migration
    assert not re.search(r'grant\\s+.+?\\s+to\\s+(?:anon|authenticated)\\b', migration)


def test_public_view_inventory_is_invoker_safe_or_unreachable():
    invoker = Path(
        'supabase_migrations/20260816c_public_views_security_invoker.sql'
    ).read_text().lower()
    outbox = Path(
        'supabase_migrations/20260714_push_installations_outbox.sql'
    ).read_text().lower()

    for view in ('v_aircraft_latest', 'v_flight_feed', 'v_radar_pins'):
        assert f'alter view public.{view} set (security_invoker = true);' in invoker
    assert 'create or replace view public.push_outbox_metrics as' in outbox
    assert (
        'revoke all on table public.push_outbox_metrics\n'
        '    from public, anon, authenticated;'
    ) in outbox
