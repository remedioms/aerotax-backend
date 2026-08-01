-- SMP-Flashcards: User erstellen eigene Karten in der iOS-App, laden sie hoch,
-- der Owner prüft sie (Review-Queue), freigegebene Karten werden ANONYMISIERT
-- an ALLE User ausgeliefert (community-cards). Kein Klarname/Token je in einer
-- Multi-User-Antwort — Token=Credential-Regel (ein fremdes Token im Response-
-- Body ist eine Account-Übernahme, siehe user_search-Härtung 2026-08-01).
--
-- `id` ist CLIENT-generiert (iOS erzeugt die UUID lokal, damit Offline-
-- Erstellung + spätere Sync-Upserts idempotent sind — exakt das Muster von
-- license_wallet/user_licenses). Der Server erzwingt Owner-Bindung selbst
-- (SELECT-vor-UPDATE in der App-Schicht, nicht per RLS) — Zugriff läuft
-- ausschließlich über den service_role-Client des Backends, das vorher den
-- Bearer gegen den eigenen Token prüft (gleiche Bauart wie flight_checkins/
-- live_activities/push_outbox).
create table if not exists public.ax_smp_user_cards (
    id          uuid primary key,
    owner_token text not null,
    module      text not null
                check (module in ('bwl', 'kommunikation', 'fuehren', 'servicemanagement')),
    topic       text,
    front       text not null,
    back        text not null,
    status      text not null default 'pending'
                check (status in ('pending', 'approved', 'rejected')),
    deleted     boolean not null default false,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

-- "Meine Karten" (GET /api/ax/smp/user-cards) — der einzige Owner-scoped Read.
create index if not exists idx_smp_user_cards_owner
    on public.ax_smp_user_cards (owner_token);

-- Community-Feed (GET /api/ax/smp/community-cards) UND Review-Queue
-- (GET /api/ax/smp/review/pending) filtern beide über (status, deleted).
create index if not exists idx_smp_user_cards_status
    on public.ax_smp_user_cards (status, deleted);

alter table public.ax_smp_user_cards enable row level security;
-- Kein policy-Grant: der Zugriff läuft ausschließlich über den service_role-
-- Client des Backends (siehe Kommentar oben).

NOTIFY pgrst, 'reload schema';
