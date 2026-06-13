-- Korrektur-Migration (2026-06-13): airport_delay_obs Unique-Key reparieren.
--
-- ROOT CAUSE: In PROD schlug JEDER Write-Through nach airport_delay_obs mit
-- Postgres-Fehler 42P10 fehl:
--   "there is no unique or exclusion constraint matching the ON CONFLICT
--    specification"
-- (delay_obs_write_fail_count=132, write_ok_count=0 im /api/health/full).
--
-- Grund: Die Live-Tabelle wurde irgendwann OHNE den zusammengesetzten Primary
-- Key (date, airport, flight, sched) angelegt; das spätere
-- `create table if not exists` aus 20260606_airport_delay_obs.sql war dann ein
-- No-Op und konnte den fehlenden Key nicht mehr nachrüsten. Folge: der
-- `upsert(on_conflict='date,airport,flight,sched')` im Backend hatte kein
-- passendes Constraint-Target → NICHTS wurde je persistiert. Die Tafel-Historie
-- und die Pünktlichkeits-Stichprobe lebten nur im flüchtigen In-Memory-Store
-- einer einzelnen Cloud-Run-Instanz und verschwanden bei jedem Restart →
-- „Radar-Tafel speichert nicht vollständig".
--
-- Diese Migration ist idempotent und repariert eine bereits existierende Tabelle
-- ohne Datenverlust:
--   1) etwaige Duplikate auf dem Schlüssel zusammenführen (max delay behalten),
--   2) den fehlenden Unique-Key als benanntes Constraint nachrüsten.
--
-- Hinweis: Seit 2026-06-13 schreibt das Backend zusätzlich einen manuellen
-- Upsert (UPDATE→INSERT) der NICHT vom Constraint abhängt — die Persistenz
-- funktioniert also auch ohne diese Migration. Der Key hier verhindert nur noch
-- seltene Race-Duplikate bei gleichzeitigen Schreibern.

-- Falls die Tabelle noch gar nicht existiert: vollständig mit PK anlegen.
create table if not exists public.airport_delay_obs (
    date          text        not null,
    airport       text        not null,
    flight        text        not null,
    sched         text        not null,
    max_delay_min integer     not null default 0,
    cancelled     boolean     not null default false,
    status        text,
    updated_at    timestamptz not null default now(),
    constraint airport_delay_obs_pkey primary key (date, airport, flight, sched)
);

-- 1) Duplikate auf dem Schlüssel kollabieren (höchstes max_delay_min / cancelled
--    behalten), damit das Unique-Constraint anschließend gesetzt werden kann.
do $$
begin
    if exists (
        select 1
        from public.airport_delay_obs
        group by date, airport, flight, sched
        having count(*) > 1
    ) then
        -- Aggregierten Bestand in eine temporäre Tabelle, dann zurückschreiben.
        create temp table _ado_dedup on commit drop as
        select date, airport, flight, sched,
               max(max_delay_min) as max_delay_min,
               bool_or(cancelled) as cancelled,
               max(status)        as status,
               max(updated_at)    as updated_at
        from public.airport_delay_obs
        group by date, airport, flight, sched;

        delete from public.airport_delay_obs;

        insert into public.airport_delay_obs
            (date, airport, flight, sched, max_delay_min, cancelled, status, updated_at)
        select date, airport, flight, sched, max_delay_min, cancelled, status, updated_at
        from _ado_dedup;
    end if;
end $$;

-- 2) Fehlenden Unique-Key nachrüsten (nur wenn noch kein PK/Unique existiert).
do $$
begin
    if not exists (
        select 1
        from pg_constraint
        where conrelid = 'public.airport_delay_obs'::regclass
          and contype in ('p', 'u')
    ) then
        alter table public.airport_delay_obs
            add constraint airport_delay_obs_pkey
            primary key (date, airport, flight, sched);
    end if;
end $$;

-- Indizes (idempotent) wie in der Ursprungs-Migration.
create index if not exists idx_airport_delay_obs_date_airport
    on public.airport_delay_obs(date, airport);
create index if not exists idx_airport_delay_obs_date
    on public.airport_delay_obs(date);

alter table public.airport_delay_obs enable row level security;
