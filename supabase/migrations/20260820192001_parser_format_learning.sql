-- Runtime learning state for unknown roster/logbook layouts.
--
-- This is deliberately NOT a source-document or model-output store. Only
-- structural SHA-256 fingerprints, server-keyed source proofs and counters are
-- retained. The Flask backend reads formats through the service role and
-- writes state through one atomic SECURITY DEFINER RPC. Mobile/anon users get
-- neither table privileges nor an RPC grant.

create table if not exists public.ax_parser_formats (
    kind text not null,
    fingerprint text not null,
    fingerprint_version smallint not null default 1,
    status text not null default 'candidate',
    generation integer not null default 1,
    verified_documents integer not null default 0,
    successful_uses bigint not null default 0,
    audited_uses bigint not null default 0,
    failure_count bigint not null default 0,
    model text,
    prompt_version text,
    last_failure_code text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    last_verified_at timestamptz,
    primary key (kind, fingerprint),
    constraint ax_parser_formats_kind_check
        check (kind in ('roster', 'logbook')),
    constraint ax_parser_formats_fingerprint_check
        check (fingerprint ~ '^[0-9a-f]{64}$'),
    constraint ax_parser_formats_status_check
        check (status in ('candidate', 'active', 'quarantined')),
    constraint ax_parser_formats_generation_check check (generation >= 1),
    constraint ax_parser_formats_counters_check check (
        verified_documents >= 0 and successful_uses >= 0
        and audited_uses >= 0 and failure_count >= 0
    )
);

create table if not exists public.ax_parser_format_evidence (
    kind text not null,
    fingerprint text not null,
    generation integer not null,
    source_sha text not null,
    verified_at timestamptz not null default now(),
    primary key (kind, fingerprint, generation, source_sha),
    foreign key (kind, fingerprint)
        references public.ax_parser_formats (kind, fingerprint)
        on delete cascade,
    constraint ax_parser_format_evidence_source_sha_check
        check (source_sha ~ '^[0-9a-f]{64}$'),
    constraint ax_parser_format_evidence_generation_check
        check (generation >= 1)
);

create index if not exists ax_parser_formats_status_updated_idx
    on public.ax_parser_formats (status, updated_at desc);

alter table public.ax_parser_formats enable row level security;
alter table public.ax_parser_format_evidence enable row level security;

revoke all on table public.ax_parser_formats from public, anon, authenticated;
revoke all on table public.ax_parser_format_evidence
    from public, anon, authenticated;

-- The backend must look up a contract before deciding between one and two
-- reads. All state mutations still go through the atomic RPC below.
grant select on table public.ax_parser_formats to service_role;

create or replace function public.ax_parser_learning_record(
    p_kind text,
    p_fingerprint text,
    p_source_sha text,
    p_outcome text,
    p_model text default null,
    p_prompt_version text default null,
    p_failure_code text default null
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public
as $function$
declare
    state_row public.ax_parser_formats%rowtype;
    document_count integer;
begin
    if p_kind not in ('roster', 'logbook') then
        raise exception 'invalid parser kind' using errcode = '22023';
    end if;
    if p_fingerprint !~ '^[0-9a-f]{64}$'
            or p_source_sha !~ '^[0-9a-f]{64}$' then
        raise exception 'invalid parser/source fingerprint'
            using errcode = '22023';
    end if;
    if p_outcome not in ('double_verified', 'single_verified', 'failed') then
        raise exception 'invalid parser learning outcome'
            using errcode = '22023';
    end if;

    insert into public.ax_parser_formats (
        kind, fingerprint, model, prompt_version
    ) values (
        p_kind, p_fingerprint, nullif(left(p_model, 80), ''),
        nullif(left(p_prompt_version, 80), '')
    )
    on conflict (kind, fingerprint) do nothing;

    select * into state_row
      from public.ax_parser_formats
     where kind = p_kind and fingerprint = p_fingerprint
     for update;

    if p_outcome = 'failed' then
        update public.ax_parser_formats
           set status = 'quarantined',
               generation = generation + 1,
               verified_documents = 0,
               failure_count = failure_count + 1,
               model = coalesce(nullif(left(p_model, 80), ''), model),
               prompt_version = coalesce(
                   nullif(left(p_prompt_version, 80), ''), prompt_version),
               last_failure_code = nullif(left(p_failure_code, 120), ''),
               updated_at = now()
         where kind = p_kind and fingerprint = p_fingerprint
         returning * into state_row;

    elsif p_outcome = 'double_verified' then
        insert into public.ax_parser_format_evidence (
            kind, fingerprint, generation, source_sha
        ) values (
            p_kind, p_fingerprint, state_row.generation, p_source_sha
        ) on conflict do nothing;

        select count(*)::integer into document_count
          from public.ax_parser_format_evidence
         where kind = p_kind
           and fingerprint = p_fingerprint
           and generation = state_row.generation;

        update public.ax_parser_formats
           set status = case when document_count >= 2
                             then 'active' else 'candidate' end,
               verified_documents = document_count,
               successful_uses = successful_uses + 1,
               audited_uses = audited_uses + 1,
               model = coalesce(nullif(left(p_model, 80), ''), model),
               prompt_version = coalesce(
                   nullif(left(p_prompt_version, 80), ''), prompt_version),
               last_failure_code = null,
               last_verified_at = now(),
               updated_at = now()
         where kind = p_kind and fingerprint = p_fingerprint
         returning * into state_row;

    else
        -- A single-read result is legitimate only while the row is still
        -- active. If another origin quarantined it concurrently, do not let a
        -- stale in-flight request reactivate or advance the contract.
        update public.ax_parser_formats
           set successful_uses = successful_uses + 1,
               model = coalesce(nullif(left(p_model, 80), ''), model),
               prompt_version = coalesce(
                   nullif(left(p_prompt_version, 80), ''), prompt_version),
               last_verified_at = now(),
               updated_at = now()
         where kind = p_kind and fingerprint = p_fingerprint
           and status = 'active'
         returning * into state_row;

        if not found then
            select * into state_row
              from public.ax_parser_formats
             where kind = p_kind and fingerprint = p_fingerprint;
        end if;
    end if;

    return to_jsonb(state_row);
end
$function$;

revoke all on function public.ax_parser_learning_record(
    text, text, text, text, text, text, text
) from public, anon, authenticated;
grant execute on function public.ax_parser_learning_record(
    text, text, text, text, text, text, text
) to service_role;

comment on table public.ax_parser_formats is
    'Private verification state for AI-assisted document layout contracts; contains no source text or user identifiers.';
comment on table public.ax_parser_format_evidence is
    'Distinct server-keyed source proofs that passed two independent reads in one contract generation.';
comment on function public.ax_parser_learning_record(
    text, text, text, text, text, text, text
) is 'Service-role-only atomic promotion, audit and quarantine transition.';

notify pgrst, 'reload schema';
