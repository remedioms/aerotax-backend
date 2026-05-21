-- ════════════════════════════════════════════════════════════════════════════
-- P0 #96 Fix — PaymentIntent Replay-Schutz Multi-Container
--
-- Problem:
--   `_consumed_payment_intents` ist Python-Dict pro Container (Cloud Run scale
--   ≥ 2). Parallele /api/process-Requests mit selber pi_id auf 2 Container
--   bestehen beide den in-memory Replay-Check und erzeugen 2 Jobs für 1 PI.
--
-- Lösung:
--   Persistente Lock-Tabelle mit Primary Key auf payment_intent_id. Atomic
--   claim via INSERT — 2. Insert raised 23505 (unique violation). Caller
--   liest existing record und antwortet mit reason_code PAYMENT_ALREADY_USED.
--
-- Status-Wert:
--   'claimed'           initial nach erfolgreichem Insert
--   'failed_retryable'  Job ist failed aber Recovery-Token verfügbar
--   'failed_support'    Job ist failed ohne Recovery — Support-Case
--   'done'              Job erfolgreich abgeschlossen
--
-- Cleanup-Policy:
--   - 'claimed'           nie löschen (orphan-Race-Indikator)
--   - 'failed_retryable'  nie löschen (Recovery-Token könnte noch greifen)
--   - 'done' / 'failed_support'  löschen nach 30 Tagen (Audit-Window)
-- ════════════════════════════════════════════════════════════════════════════

create table if not exists public.payment_intent_consumptions (
    payment_intent_id  text         primary key,
    ref                text,
    job_id             uuid,
    consumed_at        timestamptz  not null default now(),
    status             text         not null default 'claimed',
    metadata           jsonb        not null default '{}'::jsonb
);

create index if not exists idx_pi_consumptions_ref
    on public.payment_intent_consumptions(ref);

create index if not exists idx_pi_consumptions_status
    on public.payment_intent_consumptions(status);

create index if not exists idx_pi_consumptions_consumed_at
    on public.payment_intent_consumptions(consumed_at);

alter table public.payment_intent_consumptions enable row level security;

-- Bewusst keine policies: nur Service-Role-Key kann lesen/schreiben. Frontend
-- darf diese Tabelle weder lesen noch schreiben.
