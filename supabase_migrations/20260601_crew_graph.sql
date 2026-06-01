-- Worker-P6a Crew-Graph Edges — Server-side aggregation of "who-flew-with-whom".
--
-- Pendant zur iOS-CrewGraphEdge (SwiftData @Model). Lokal auf dem Device gilt
-- das Privacy-by-Default-Modell: Klarnamen werden NICHT gespeichert, nur
-- otherShortName ("Schumann M.") + opaker otherToken. Server-side spiegeln wir
-- exakt dasselbe Modell, plus eine `other_id` Spalte die als stabiler Composite-
-- Key-Part dient (otherToken wenn App-User, sonst sha256(self_token+shortname)
-- truncated — siehe blueprint).
--
-- Composite-PK statt einzelne id: pro (self_token, other_id) gibt es genau eine
-- Edge — verhindert Race-induzierte Duplikate. Counter-Increment läuft
-- entweder via RPC (atomic update) oder SELECT-then-UPSERT-fallback in der App.
--
-- shared_layovers/shared_routes sind jsonb-Arrays (max 20 Einträge, capped im
-- Blueprint), nicht 1:N Tabellen — Cross-Edge-Queries auf Layover gibt es nicht,
-- und ein Edge-Read fasst beide atomar an.

create table if not exists public.crew_edges (
    self_token          text         not null,
    other_id            text         not null,
    other_token         text,
    other_display_name  text,
    other_position      text,
    tour_count          int          not null default 1,
    last_flown_date     date,
    shared_layovers     jsonb        not null default '[]'::jsonb,
    shared_routes       jsonb        not null default '[]'::jsonb,
    created_at          timestamptz  not null default now(),
    updated_at          timestamptz  not null default now(),
    primary key (self_token, other_id)
);

-- Hot-path: "Top-N strongest connections for me" sortiert nach tour_count desc.
create index if not exists idx_crew_edges_self
    on public.crew_edges(self_token, tour_count desc);

-- Reverse-Lookup: "ist <other_token> bekannt im Graph?" Bei NULL otherToken
-- (= nicht-App-User, nur shortname-hash) kein Sinn — partial index.
create index if not exists idx_crew_edges_other_token
    on public.crew_edges(other_token) where other_token is not null;

-- Service-Role-Key umgeht RLS. Anon-Client bleibt geblockt (kein Read/Write).
alter table public.crew_edges enable row level security;

-- Atomic-Counter-RPC (optional, blueprint nutzt SELECT-then-UPSERT als Fallback
-- wenn die RPC nicht existiert). Inkrementiert tour_count und merged die jsonb-
-- Arrays atomar in einer Transaction. Bei Race kein Doppel-Insert dank
-- ON CONFLICT.
create or replace function public.crew_edges_upsert_increment(
    p_self_token          text,
    p_other_id            text,
    p_other_token         text,
    p_other_display_name  text,
    p_other_position      text,
    p_tour_date           date,
    p_new_layovers        jsonb,
    p_new_routes          jsonb
) returns void
language plpgsql
as $$
begin
    insert into public.crew_edges (
        self_token, other_id, other_token, other_display_name, other_position,
        tour_count, last_flown_date, shared_layovers, shared_routes
    ) values (
        p_self_token, p_other_id, p_other_token, p_other_display_name,
        coalesce(p_other_position, ''),
        1, p_tour_date,
        coalesce(p_new_layovers, '[]'::jsonb),
        coalesce(p_new_routes,   '[]'::jsonb)
    )
    on conflict (self_token, other_id) do update
    set tour_count          = public.crew_edges.tour_count + 1,
        other_token         = coalesce(excluded.other_token, public.crew_edges.other_token),
        other_display_name  = coalesce(excluded.other_display_name, public.crew_edges.other_display_name),
        other_position      = coalesce(nullif(excluded.other_position, ''), public.crew_edges.other_position),
        last_flown_date     = greatest(
                                  coalesce(excluded.last_flown_date, public.crew_edges.last_flown_date),
                                  coalesce(public.crew_edges.last_flown_date, excluded.last_flown_date)
                              ),
        shared_layovers     = (
            select coalesce(jsonb_agg(distinct val), '[]'::jsonb)
            from (
                select val from jsonb_array_elements_text(public.crew_edges.shared_layovers) as val
                union
                select val from jsonb_array_elements_text(coalesce(excluded.shared_layovers, '[]'::jsonb)) as val
            ) merged
        ),
        shared_routes       = (
            select coalesce(jsonb_agg(distinct val), '[]'::jsonb)
            from (
                select val from jsonb_array_elements_text(public.crew_edges.shared_routes) as val
                union
                select val from jsonb_array_elements_text(coalesce(excluded.shared_routes, '[]'::jsonb)) as val
            ) merged
        ),
        updated_at          = now();
end;
$$;
