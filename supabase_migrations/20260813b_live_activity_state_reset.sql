-- Zustands-RESET beim Wechsel der Activity (13.08.2026, Nachtrag zu
-- 20260813_live_activity_last_state.sql).
--
-- WARUM: die Registry-ZEILE ist langlebig, die ACTIVITY nicht. Identität ist
-- (user_token, kind, coalesce(activity_id,'')) — eine `update`-Zeile ohne
-- activity_id und jede Zeile nach einem Token-Wechsel bedient damit die
-- NÄCHSTE Live Activity weiter. `last_content_state` blieb dabei stehen und
-- die Feld-Erhaltung schrieb die Dienst-Kette des VORIGEN Dienstes in die
-- neue Karte („Ketten-Geist"). Genau dieselbe Logik, aus der
-- 20260727_live_activities.sql schon `content_digest`/`failure_count` bei
-- Token-Rotation nullt: ein neuer Zustand hat keine Vergangenheit.
--
-- Beide Funktionen sind 1:1-Kopien aus 20260727_live_activities.sql, ergänzt
-- um `last_content_state = null`. Idempotent (create or replace); die Spalte
-- wird hier nochmals abgesichert, damit diese Datei allein lauffähig ist.
alter table public.live_activities
    add column if not exists last_content_state jsonb;


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
            -- NEU: derselbe Grund für den erhaltenen Zustand. Ein rotierter
            -- Token gehört zu einer anderen Karte — der alte Zustand wäre ab
            -- hier eine Erfindung.
            last_content_state = case when excluded.la_token <> la.la_token
                                      then null else la.last_content_state end,
            updated_at  = now()
    returning la.id into v_id;

    return v_id;
end;
$$;


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
           -- Die Karte ist weg — ihr Zustand darf die nächste nicht befüllen.
           last_content_state = null,
           updated_at = now()
     where user_token = p_user_token
       and kind = 'update'
       and coalesce(activity_id, '') = coalesce(nullif(p_activity_id, ''), '')
       and active;
    get diagnostics v_count = row_count;
    return v_count;
end;
$$;


-- Grants wie in 20260727_live_activities.sql: `create or replace` erhält sie
-- zwar, aber diese Datei muss auch auf einer frisch aufgesetzten DB allein
-- richtig sein.
revoke all on function public.upsert_live_activity(text, text, text, text, text, text, text, text) from public, anon;
grant execute on function public.upsert_live_activity(text, text, text, text, text, text, text, text) to service_role;

revoke all on function public.end_live_activity(text, text, text) from public, anon;
grant execute on function public.end_live_activity(text, text, text) to service_role;
