-- Per-User Evaluation-History Persistence (2026-06-07).
--
-- Die Auswertungs-Historie (R44, "Verlauf"-View) lebte bisher NUR auf
-- ephemeral Container-Disk (_user_history_state/<token>.json). Auf Cloud Run
-- ist diese Disk bei jedem Redeploy/Container-Restart weg → der User verlor
-- seine komplette Monats-Historie. Pattern wie user_licenses/layover_recs:
-- SB-primary, Disk-Mirror, Lazy-Migrate beim ersten SB-leeren Read.
--
-- Schema-Entscheidungen:
--  · PK (token, key) — eine Row pro Auswertung. `key` ist die Dedup-Identität:
--    die job_id wenn vorhanden, sonst das datum. Spiegelt die bestehende
--    Disk-Dedup ("entries by job_id") 1:1.
--  · Die fachlichen Felder (gesamt/vma_aus/hotel_naechte/fahr_tage/arbeitstage/
--    year/month/summary_label/datum/job_id) liegen in `entry` jsonb — die
--    Entry-Shape wächst mit der App (neue KPIs) ohne Tabellen-Migration. Der
--    Reader faltet `entry` 1:1 zurück in die alte entries-Liste.
--  · created_at für Sortierung (neueste zuerst), exakt wie die Disk-Variante
--    (data['entries'].insert(0, ...)).

create table if not exists public.user_history (
    token       text         not null,
    key         text         not null,
    entry       jsonb        not null default '{}'::jsonb,
    created_at  timestamptz  not null default now(),
    updated_at  timestamptz  not null default now(),
    primary key (token, key)
);

-- Häufigste Query: "alle Einträge dieses Tokens, neueste zuerst".
create index if not exists idx_user_history_token
    on public.user_history(token, created_at desc);

alter table public.user_history enable row level security;
