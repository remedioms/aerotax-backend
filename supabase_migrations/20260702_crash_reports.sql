-- MetricKit Crash/Hang-Reports vom iOS-Client (2026-07-02).
-- Intake: POST /api/telemetry/diagnostics (app.py) · Client: Storage/CrashTelemetry.swift.
--
-- WICHTIG: app.py funktioniert auch OHNE diese Migration — der Insert
-- degradiert dann zu Logging-only (Report landet als [mk-diag] FALLBACK-LOG
-- in den Cloud-Run-Logs, Alert-Mail geht trotzdem raus). Für durable Reports
-- die Datei einmal im Supabase SQL-Editor ausführen.

create table if not exists public.ax_crash_reports (
    id          bigint generated always as identity primary key,
    created_at  timestamptz not null default now(),
    user_token  text,                       -- optionales Bearer-Token (AT-…), NULL = anonym
    app_version text,                       -- z.B. "1.3"
    build       text,                       -- CFBundleVersion, z.B. "47"
    os          text,                       -- z.B. "iPhone OS 26.0 (23A340)"
    device      text,                       -- z.B. "iPhone17,2"
    kind        text not null check (kind in ('crash', 'hang')),
    payload     jsonb                       -- voller Report inkl. call_stack (callStackTree)
);

-- Neueste zuerst (Admin-Sichtung) + Gruppierung pro Build für Trends/Throttle-Checks.
create index if not exists ax_crash_reports_created_idx
    on public.ax_crash_reports (created_at desc);
create index if not exists ax_crash_reports_kind_build_idx
    on public.ax_crash_reports (kind, build);

-- RLS an, KEINE Policies: nur der Service-Role-Key des Backends darf
-- lesen/schreiben (gleiche Konvention wie die anderen ax_-Tabellen).
alter table public.ax_crash_reports enable row level security;
