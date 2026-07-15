-- Account-bound, immutable legal-consent ledger.
-- Apply before registering blueprints/legal_consent_blueprint.py.
-- The app authenticates with its existing Bearer token; the SECURITY DEFINER
-- RPC resolves that credential to the stable account_id and never stores the
-- Bearer token in the audit table.

create extension if not exists pgcrypto;

alter table public.auth_users
    add column if not exists account_id uuid;

update public.auth_users
   set account_id = gen_random_uuid()
 where account_id is null;

alter table public.auth_users
    alter column account_id set default gen_random_uuid(),
    alter column account_id set not null;

create unique index if not exists auth_users_account_id_uidx
    on public.auth_users(account_id);

create table if not exists public.user_legal_consents (
    id                bigint generated always as identity primary key,
    account_id        uuid        not null
                                  references public.auth_users(account_id)
                                  on delete cascade,
    document_id       text        not null,
    document_version  text        not null,
    document_hash     text        not null,
    manifest_version  text        not null,
    manifest_hash     text        not null,
    accepted_at       timestamptz not null default now(),
    locale            text,
    app_build         text,
    acceptance_source text        not null default 'ios',
    constraint legal_consent_document_id_format
        check (document_id ~ '^[a-z0-9][a-z0-9-]{1,63}$'),
    constraint legal_consent_document_version_length
        check (length(document_version) between 1 and 64),
    constraint legal_consent_manifest_version_length
        check (length(manifest_version) between 1 and 64),
    constraint legal_consent_document_hash_sha256
        check (document_hash ~ '^[0-9a-f]{64}$'),
    constraint legal_consent_manifest_hash_sha256
        check (manifest_hash ~ '^[0-9a-f]{64}$'),
    constraint legal_consent_locale_length
        check (locale is null or length(locale) between 1 and 35),
    constraint legal_consent_app_build_length
        check (app_build is null or length(app_build) between 1 and 64),
    constraint legal_consent_source_length
        check (length(acceptance_source) between 1 and 32),
    unique (account_id, document_id, document_version, document_hash)
);

create index if not exists user_legal_consents_account_manifest_idx
    on public.user_legal_consents(account_id, manifest_hash, accepted_at desc);

alter table public.user_legal_consents enable row level security;

drop policy if exists user_legal_consents_service_all
    on public.user_legal_consents;
create policy user_legal_consents_service_all
    on public.user_legal_consents
    for all to service_role
    using (true)
    with check (true);

revoke all on table public.user_legal_consents from public, anon, authenticated;
grant select, insert on table public.user_legal_consents to service_role;
revoke all on sequence public.user_legal_consents_id_seq from public, anon, authenticated;
grant usage, select on sequence public.user_legal_consents_id_seq to service_role;

create or replace function public.accept_legal_manifest(
    p_user_token text,
    p_manifest_version text,
    p_manifest_hash text,
    p_documents jsonb,
    p_locale text default null,
    p_app_build text default null
)
returns integer
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_account_id uuid;
    v_document_count integer;
    v_inserted integer;
begin
    if nullif(p_user_token, '') is null then
        raise exception 'missing user token' using errcode = '22023';
    end if;
    if p_manifest_version is null or length(p_manifest_version) not between 1 and 64 then
        raise exception 'invalid manifest version' using errcode = '22023';
    end if;
    if p_manifest_hash is null or p_manifest_hash !~ '^[0-9a-f]{64}$' then
        raise exception 'invalid manifest hash' using errcode = '22023';
    end if;
    if jsonb_typeof(p_documents) <> 'array' then
        raise exception 'documents must be an array' using errcode = '22023';
    end if;

    v_document_count := jsonb_array_length(p_documents);
    if v_document_count < 1 or v_document_count > 10 then
        raise exception 'invalid document count' using errcode = '22023';
    end if;

    select account_id into v_account_id
      from public.auth_users
     where token = p_user_token
     limit 1;
    if v_account_id is null then
        raise exception 'unknown account' using errcode = '22023';
    end if;

    if exists (
        select 1
          from jsonb_array_elements(p_documents) d
         where coalesce(d->>'id', '') !~ '^[a-z0-9][a-z0-9-]{1,63}$'
            or length(coalesce(d->>'version', '')) not between 1 and 64
            or coalesce(d->>'hash', '') !~ '^[0-9a-f]{64}$'
    ) then
        raise exception 'invalid document manifest' using errcode = '22023';
    end if;

    insert into public.user_legal_consents (
        account_id, document_id, document_version, document_hash,
        manifest_version, manifest_hash, locale, app_build, acceptance_source
    )
    select
        v_account_id,
        d->>'id',
        d->>'version',
        d->>'hash',
        p_manifest_version,
        p_manifest_hash,
        nullif(left(coalesce(p_locale, ''), 35), ''),
        nullif(left(coalesce(p_app_build, ''), 64), ''),
        'ios'
      from jsonb_array_elements(p_documents) d
    on conflict (account_id, document_id, document_version, document_hash)
    do nothing;

    get diagnostics v_inserted = row_count;
    return v_inserted;
end;
$$;

revoke all on function public.accept_legal_manifest(
    text, text, text, jsonb, text, text
) from public, anon, authenticated;
grant execute on function public.accept_legal_manifest(
    text, text, text, jsonb, text, text
) to service_role;
