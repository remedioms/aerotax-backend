-- ════════════════════════════════════════════════════════════════════
--  AeroX Aviation Data Engine — Self-growing Cache-Tabellen
--  In Supabase → SQL Editor einfügen und ausführen. Idempotent.
--
--  Diese zwei Tabellen sind der HEISSE/wachsende Teil der Data Engine:
--  Jeder externe Treffer (adsbdb/hexdb) wird hier zurückgeschrieben, damit
--  dieselbe Tatsache nie zweimal von einer API geholt werden muss.
--  Die Engine läuft auch OHNE diese Tabellen (Cache wird dann übersprungen) —
--  mit ihnen sinkt der API-Verbrauch über Zeit gegen null.
-- ════════════════════════════════════════════════════════════════════

-- ── Aircraft-Cache (Hex → Stammdaten) ────────────────────────────────
-- Befüllt aus adsbdb/hexdb, wenn ein Hex NICHT in der gebackenen 520k-DB
-- liegt (typisch ganz neue Registrierungen). payload = das gemergte Dict.
create table if not exists public.ax_aircraft_cache (
    hex         text         primary key,
    payload     jsonb        not null default '{}'::jsonb,
    updated_at  timestamptz  not null default now()
);
alter table public.ax_aircraft_cache enable row level security;


-- ── Route-Cache (Flugnummer → Strecke) ───────────────────────────────
-- Befüllt aus adsbdb-Callsign-Lookup (z.B. LH506 → DLH506 → FRA/GRU).
-- key = die normalisierte IATA-Flugnummer in Großbuchstaben (z.B. "LH506").
create table if not exists public.ax_route_cache (
    flight      text         primary key,
    payload     jsonb        not null default '{}'::jsonb,
    updated_at  timestamptz  not null default now()
);
alter table public.ax_route_cache enable row level security;

-- ── Foto-Link-Cache (Hex → planespotters-Foto-URL) ───────────────────
-- NUR die URL-Strings (kein Bild-Storage). Ein planespotters-Call je Flieger,
-- danach teilen alle Nutzer denselben Link → wächst die eigene Foto-Link-DB.
create table if not exists public.ax_photo_cache (
    hex         text         primary key,
    payload     jsonb        not null default '{}'::jsonb,
    updated_at  timestamptz  not null default now()
);
alter table public.ax_photo_cache enable row level security;

-- ── Schedule-Cache (Städtepaar → echte Flugnummern/Zeiten) ────────────
-- Befüllt aus AviationStack (Free-Tier 100/Monat), dann FÜR IMMER gecacht →
-- ein Call seedet eine Route für ALLE Nutzer.
create table if not exists public.ax_schedule_cache (
    route       text         primary key,   -- z.B. „FRA-LIS"
    payload     jsonb        not null default '{}'::jsonb,
    updated_at  timestamptz  not null default now()
);
alter table public.ax_schedule_cache enable row level security;

-- ── API-Budget-Zähler (schützt das AviationStack-Free-Limit) ──────────
-- Eine Zeile pro Monat („2026-06"), n = verbrauchte Calls. Backend stoppt bei
-- AVIATIONSTACK_CAP (Default 90) → Free-Tier kann nie überschritten werden.
create table if not exists public.ax_api_budget (
    month       text         primary key,   -- „YYYY-MM"
    n           integer      not null default 0,
    updated_at  timestamptz  not null default now()
);
alter table public.ax_api_budget enable row level security;

-- Fertig. Das Backend schreibt mit dem Service-Role-Key (umgeht RLS),
-- daher sind keine zusätzlichen Policies nötig.
