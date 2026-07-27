-- Live Activities (P6, 2026-07-27) — Registry der ActivityKit-Push-Tokens.
--
-- WARUM eine eigene Tabelle und nicht push_installations:
-- ActivityKit-Tokens sind KEINE Device-Tokens. Es gibt zwei Sorten, und sie
-- sind NICHT austauschbar:
--
--   kind='start'   Push-to-Start-Token. GERÄTE-WEIT, genau einer pro
--                  ActivityAttributes-Typ. Damit kann das Backend eine
--                  Activity ERZEUGEN, ohne dass die App läuft. Trägt keine
--                  activity_id (es gibt noch keine Activity).
--   kind='update'  Update-Token EINER konkreten laufenden Activity. Der
--                  ROTIERT während der Laufzeit (bis zu 8 h): iOS liefert per
--                  `Activity.pushTokenUpdates` immer wieder einen neuen. Jede
--                  Erneuerung MUSS die vorige Zeile derselben activity_id
--                  überschreiben — sonst pusht das Backend gegen einen toten
--                  Token und die Activity friert still ein (ActivityKit meldet
--                  nichts, Apple antwortet nur BadDeviceToken).
--
-- Deshalb ist der Identitäts-Schlüssel (user_token, kind, activity_id) — mit
-- coalesce(activity_id,'') als Expression-Index, weil NULL in Postgres in
-- einem normalen UNIQUE nicht mit NULL kollidiert (jede Start-Token-
-- Registrierung hätte sonst eine NEUE Zeile angelegt und der Fanout hätte
-- gegen n tote Tokens gepusht).
--
-- content_digest + last_timestamp sitzen bewusst AN DER ZEILE:
--   content_digest  sha256 des normalisierten content-state OHNE generatedAt.
--                   Apple drosselt Live-Activity-Pushes hart → nur bei ECHTER
--                   Änderung senden. Der Digest überlebt Deploys/Worker, ein
--                   In-Process-Dict nicht.
--   last_timestamp  aps.timestamp MUSS strikt monoton steigen, sonst verwirft
--                   iOS das Update als veraltet. Mehrere Gunicorn-Worker
--                   können in derselben Sekunde senden → die Zeile ist die
--                   einzige gemeinsame Wahrheit.
create table if not exists public.live_activities (
    id              uuid primary key default gen_random_uuid(),
    user_token      text not null,
    kind            text not null check (kind in ('start', 'update')),
    activity_id     text,
    la_token        text not null,
    bundle_id       text,
    environment     text check (environment in ('prod', 'sandbox', 'unknown')),
    device_id       text,
    platform        text default 'ios',
    active          boolean not null default true,
    content_digest  text,
    last_timestamp  bigint,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    last_success_at timestamptz,
    last_failure_at timestamptz,
    failure_count   int not null default 0,
    ended_at        timestamptz,
    end_reason      text
);

-- Identität: ein Start-Token pro User, eine Update-Zeile pro Activity.
create unique index if not exists live_activities_ident_uidx
    on public.live_activities (user_token, kind, coalesce(activity_id, ''));

-- Fanout-Pfad: „alle aktiven Update-Zeilen dieses Users".
create index if not exists live_activities_active_idx
    on public.live_activities (user_token, kind)
    where active;

-- Aufräum-/Reporting-Pfad.
create index if not exists live_activities_updated_idx
    on public.live_activities (updated_at desc);


-- ─────────────────────────────────────────────────────────────────────────────
-- Registrierung = EIN atomares Statement. PostgREST-Upsert kann das nicht:
-- der Konflikt-Target ist ein Expression-Index, und failure_count/ended_at
-- müssen beim Token-Wechsel zurückgesetzt werden (ein frisch rotierter Token
-- darf nicht die Fehlerhistorie des alten erben und sofort wieder als tot
-- gelten).
-- ─────────────────────────────────────────────────────────────────────────────
create or replace function public.upsert_live_activity(
    p_user_token  text,
    p_kind        text,
    p_la_token    text,
    p_activity_id text default null,
    p_bundle_id   text default null,
    p_environment text default null,
    p_device_id   text default null,
    p_platform    text default 'ios'
) returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
    v_id uuid;
begin
    if p_user_token is null or p_user_token = ''
       or p_la_token is null or p_la_token = ''
       or p_kind not in ('start', 'update') then
        raise exception 'upsert_live_activity: bad arguments';
    end if;

    insert into public.live_activities as la (
        user_token, kind, activity_id, la_token, bundle_id, environment,
        device_id, platform, active, created_at, updated_at
    ) values (
        p_user_token, p_kind, nullif(p_activity_id, ''), p_la_token,
        nullif(p_bundle_id, ''),
        coalesce(nullif(p_environment, ''), 'unknown'),
        nullif(p_device_id, ''), coalesce(nullif(p_platform, ''), 'ios'),
        true, now(), now()
    )
    on conflict (user_token, kind, coalesce(activity_id, '')) do update
        set la_token    = excluded.la_token,
            bundle_id   = coalesce(excluded.bundle_id, la.bundle_id),
            environment = case
                              -- 'unknown' vom Client darf einen GELERNTEN Wert
                              -- nicht überschreiben (der Retry-Pfad lernt die
                              -- echte Umgebung aus der APNs-Antwort).
                              when excluded.environment = 'unknown'
                                   then coalesce(la.environment, 'unknown')
                              else excluded.environment
                          end,
            device_id   = coalesce(excluded.device_id, la.device_id),
            platform    = coalesce(excluded.platform, la.platform),
            active      = true,
            ended_at    = null,
            end_reason  = null,
            -- Token-Rotation ⇒ Fehlerhistorie UND Digest sind wertlos: der
            -- neue Token hat noch nie einen Zustand gesehen, also muss das
            -- nächste Update auch bei unverändertem Inhalt durchgehen.
            failure_count  = case when excluded.la_token <> la.la_token
                                  then 0 else la.failure_count end,
            content_digest = case when excluded.la_token <> la.la_token
                                  then null else la.content_digest end,
            updated_at  = now()
    returning la.id into v_id;

    return v_id;
end;
$$;


-- ─────────────────────────────────────────────────────────────────────────────
-- Client hat die Activity beendet/verworfen. Idempotent; liefert die Anzahl
-- der geschlossenen Zeilen (0 = war schon zu / gab es nie → kein Fehler).
-- ─────────────────────────────────────────────────────────────────────────────
create or replace function public.end_live_activity(
    p_user_token  text,
    p_activity_id text,
    p_reason      text default 'client'
) returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
    v_count integer;
begin
    update public.live_activities
       set active     = false,
           ended_at   = coalesce(ended_at, now()),
           end_reason = coalesce(nullif(p_reason, ''), 'client'),
           updated_at = now()
     where user_token = p_user_token
       and kind = 'update'
       and coalesce(activity_id, '') = coalesce(nullif(p_activity_id, ''), '')
       and active;
    get diagnostics v_count = row_count;
    return v_count;
end;
$$;


-- ─────────────────────────────────────────────────────────────────────────────
-- Ergebnis EINES Sende-Versuchs festschreiben. Ein Statement, weil Digest +
-- Timestamp + Erfolgsstempel zusammengehören: ein halber Write (Digest
-- gesetzt, Push fehlgeschlagen) würde den nächsten ECHTEN Versuch als
-- „unverändert" wegwerfen.
--   p_ok=true   → Digest/Timestamp fortschreiben, Fehlerzähler auf 0
--   p_ok=false  → Fehlerzähler +1, Digest NICHT anfassen
--   p_dead=true → Token in BEIDEN Umgebungen tot ⇒ Zeile stilllegen
-- ─────────────────────────────────────────────────────────────────────────────
create or replace function public.live_activity_mark_result(
    p_id          uuid,
    p_ok          boolean,
    p_digest      text default null,
    p_timestamp   bigint default null,
    p_dead        boolean default false,
    p_reason      text default null,
    p_environment text default null
) returns void
language plpgsql
security definer
set search_path = public
as $$
begin
    update public.live_activities
       set content_digest  = case when p_ok and p_digest is not null
                                  then p_digest else content_digest end,
           last_timestamp  = case when p_timestamp is not null
                                       and p_timestamp > coalesce(last_timestamp, 0)
                                  then p_timestamp else last_timestamp end,
           last_success_at = case when p_ok then now() else last_success_at end,
           last_failure_at = case when p_ok then last_failure_at else now() end,
           failure_count   = case when p_ok then 0 else failure_count + 1 end,
           environment     = coalesce(nullif(p_environment, ''), environment),
           active          = case when p_dead then false else active end,
           ended_at        = case when p_dead then coalesce(ended_at, now())
                                  else ended_at end,
           end_reason      = case when p_dead
                                  then coalesce(nullif(p_reason, ''), 'apns_dead')
                                  else end_reason end,
           updated_at      = now()
     where id = p_id;
end;
$$;


-- Nur das Backend (service_role) darf hier ran — anon/authenticated hätten
-- über eine dieser RPCs fremde Live-Activity-Tokens setzen können.
revoke all on function public.upsert_live_activity(text, text, text, text, text, text, text, text) from public, anon;
grant execute on function public.upsert_live_activity(text, text, text, text, text, text, text, text) to service_role;

revoke all on function public.end_live_activity(text, text, text) from public, anon;
grant execute on function public.end_live_activity(text, text, text) to service_role;

revoke all on function public.live_activity_mark_result(uuid, boolean, text, bigint, boolean, text, text) from public, anon;
grant execute on function public.live_activity_mark_result(uuid, boolean, text, bigint, boolean, text, text) to service_role;

-- RLS an, ohne Policy: kein Zugriff außer service_role (Backend). Die Tabelle
-- enthält user_token + Push-Tokens — beides niemals client-lesbar.
alter table public.live_activities enable row level security;
