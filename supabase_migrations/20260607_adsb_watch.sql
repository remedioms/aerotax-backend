-- ADS-B Watch-Set + Poll-State (credit-optimierter Shared-Poller, 2026-06-07).
--
-- Kontext: EIN gemeinsames Backend-Konto pollt OpenSky und serviert ALLE User
-- aus dem aircraft_positions-Cache. Die Credit-Last ist damit UNABHÄNGIG von der
-- Nutzerzahl (10k User = gleiche Kosten). Welche Maschinen gepollt werden, ist
-- NUTZER-getrieben und beschränkt: jeder Client-Request auf /api/adsb/state oder
-- /api/aircraft/<token>/by-reg upsertet den Hex in adsb_watch. Rows die ~4h nicht
-- mehr angefragt wurden, fallen aus dem aktiven Set (TTL).
--
-- Cloud-Run-Serverless-Zwang (BUG-002/005 lessons-learned): KEIN Hintergrund-
-- Thread, KEIN In-Process-Scheduler. ALLER Cross-Request-State MUSS in Supabase
-- liegen, nicht im Prozess-Speicher (Container ist ephemer, kann jederzeit neu
-- starten). adsb_watch = was pollen, poll_state = wann zuletzt + Budget.
--
-- Idempotent, safe zu re-runnen. Service-Role umgeht RLS; Anon bleibt geblockt
-- (Tabellen werden nur vom Blueprint via Service-Key beschrieben/gelesen).

-- ── adsb_watch: nutzer-getriebenes, TTL-beschränktes Watch-Set ──────────────
-- PK hex24 (lowercase ICAO-24-Bit) — eine Maschine = eine Row. Upsert
-- on_conflict='hex24' aktualisiert last_requested_at bei jedem Client-Request.
create table if not exists public.adsb_watch (
    hex24               text          primary key,
    registration        text,
    -- priority: höher = dringender pollen (z.B. explizit erwarteter Inbound).
    -- Default 0; der Poller leitet Hot/Warm/Cold primär aus Live-Fakten ab,
    -- priority>0 hebt eine Maschine zusätzlich in den Hot-Tier.
    priority            integer       not null default 0,
    added_at            timestamptz   not null default now(),
    last_requested_at   timestamptz   not null default now(),
    -- Wann zuletzt /flights/aircraft (Inbound-Origin) für diesen Hex geholt —
    -- separat sparsam rate-limitet (einmal pro neu-gewatchtem Hex, nicht jeden
    -- Cycle). NULL = noch nie geholt.
    flights_fetched_at  timestamptz
);

-- Aktives-Set-Query: "alle Rows mit last_requested_at innerhalb TTL". Index
-- beschleunigt das Laden des aktiven Sets + den TTL-Cleanup-Delete.
create index if not exists idx_adsb_watch_last_requested
    on public.adsb_watch(last_requested_at desc);

alter table public.adsb_watch enable row level security;

-- ── poll_state: persistenter Scheduler-/Budget-Zustand (Key-Value) ──────────
-- Cloud-Run-Container sind ephemer → der "wann zuletzt gepollt"-Zustand pro
-- Bounding-Box + der Tages-Budget-Zähler + der OAuth2-Token-Cache MÜSSEN hier
-- liegen, nicht im RAM. Key-Value statt fixer Spalten, damit neue Felder (neue
-- Box, neuer Budget-Counter, Token-Cache) ohne Migration dazukommen können.
--
-- Konventionen für `key`:
--   'bbox:<NAME>'        → value_json {last_polled_at, last_tier, last_count}
--   'budget:<YYYYMMDD>'  → value_json {calls_made, remaining_seen, updated_at}
--   'oauth_token'        → value_json {access_token, expires_at_unix}
create table if not exists public.poll_state (
    key            text          primary key,
    value_json     jsonb         not null default '{}'::jsonb,
    updated_at     timestamptz   not null default now()
);

alter table public.poll_state enable row level security;
