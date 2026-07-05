-- 20260705_budget_increment.sql — Atomarer API-Budget-Zähler (Audit 2026-07-05)
--
-- PROBLEM: _paid_budget_inc/_budget_inc (blueprints/aerox_data_blueprint.py)
-- machten read-modify-write: SELECT n → UPSERT n+units. Bei mehreren Cloud-Run-
-- Instanzen bzw. parallelen Requests überschreiben sich die Writes gegenseitig
-- → Zählungen gehen verloren, der AviationStack-Free-Tier (100/Monat) und der
-- AeroDataBox-Tages-Cap (AX_PAID_DAILY_CAP, Units) können real überlaufen.
--
-- FIX: serverseitiger Increment in EINEM Statement (INSERT … ON CONFLICT …
-- SET n = n + units) — atomar pro Row, unabhängig von Instanzen-Anzahl.
-- Der Blueprint ruft `ax_budget_increment` bevorzugt und fällt auf den alten
-- Upsert zurück, solange diese Migration nicht applied ist (PGRST202-Detect,
-- gleiches Degrade-Muster wie ax_open_legs).
--
-- Die Tabelle existiert in PROD bereits (Zähler laufen seit Juni) —
-- `if not exists` macht die Migration idempotent/frisch-DB-tauglich.
-- Key-Konvention im Feld `month`: 'YYYY-MM' (AviationStack-Monat) und
-- 'paid:YYYYMMDD' (AeroDataBox-Tages-Units).

create table if not exists public.ax_api_budget (
    month       text primary key,
    n           integer not null default 0,
    updated_at  timestamptz default now()
);

-- Service-Role-Key umgeht RLS; Anon-Client bleibt geblockt (wie crew_edges).
alter table public.ax_api_budget enable row level security;

create or replace function public.ax_budget_increment(p_key text, p_units integer)
returns integer
language sql
security definer
set search_path = public
as $$
    insert into public.ax_api_budget (month, n, updated_at)
    values (p_key, greatest(1, p_units), now())
    on conflict (month) do update
        set n          = public.ax_api_budget.n + greatest(1, p_units),
            updated_at = now()
    returning n;
$$;
