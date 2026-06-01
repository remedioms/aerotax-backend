-- Hotel-Room-Database — USP-1 (Network-Effect Killer), 2026-06-01.
--
-- Crews tauschen taeglich room-tipps via WhatsApp ("Im Sheraton FRA NIE Room
-- 4xx — Autobahn-Seite, Laerm bis 02:00"). AeroX wird die Single-Source-of-Truth:
-- jeder Tipp ist ein Report, Upvotes ranken die besten oben.
--
-- Schema-Entscheidungen:
--  · id text primary key (uuid-string vom Backend generiert — kein gen_random_uuid()
--    server-side, damit die Disk-Fallback-Row die gleiche ID wie der DB-Row hat).
--  · `reported_by_token` gespeichert (Abuse/Owner-Delete), NIEMALS im Listing
--    ausgegeben (Blueprint stripped).
--  · soft-delete via `deleted` boolean — wir wollen Vote-Counts und Owner-Audit
--    erhalten falls Report missbraeuchlich war.
--  · `upvote_count` denormalized auf reports (Hot-Read), Wahrheit in `hotel_room_upvotes`.
--  · Partial indexes WHERE deleted=false → Listing-Queries skippen tombstones.

create table if not exists public.hotel_room_reports (
    id                 text          primary key,
    reported_by_token  text          not null,
    hotel_name         text          not null,
    hotel_iata         text,
    room_number_low    int,
    room_number_high   int,
    side               text,
    noise_rating       int,
    view_rating        int,
    comfort_rating     int,
    note               text,
    renovated_year     int,
    upvote_count       int           not null default 0,
    deleted            boolean       not null default false,
    created_at         timestamptz   not null default now(),
    check (side is null or side in ('street','courtyard','highway','runway','inner')),
    check (noise_rating   is null or noise_rating   between 1 and 5),
    check (view_rating    is null or view_rating    between 1 and 5),
    check (comfort_rating is null or comfort_rating between 1 and 5),
    check (char_length(hotel_name) between 1 and 120),
    check (note is null or char_length(note) <= 500),
    check (renovated_year is null or renovated_year between 1900 and 2100)
);

-- Hot-path Listing pro IATA, sortiert nach Upvotes.
create index if not exists idx_hotel_room_reports_lookup
    on public.hotel_room_reports(hotel_iata, upvote_count desc)
    where deleted = false;

-- Hot-path Listing pro Hotel-Name (Cross-City Marken wie "Marriott", "Hilton").
create index if not exists idx_hotel_room_reports_hotel
    on public.hotel_room_reports(hotel_name, upvote_count desc)
    where deleted = false;

-- Owner-Index fuer DELETE (token + id lookups).
create index if not exists idx_hotel_room_reports_owner
    on public.hotel_room_reports(reported_by_token, created_at desc);

alter table public.hotel_room_reports enable row level security;

-- Upvotes: composite-PK garantiert 1 Vote pro (report, token).
create table if not exists public.hotel_room_upvotes (
    report_id    text         not null,
    voter_token  text         not null,
    created_at   timestamptz  not null default now(),
    primary key (report_id, voter_token)
);

create index if not exists idx_hotel_room_upvotes_report
    on public.hotel_room_upvotes(report_id);

alter table public.hotel_room_upvotes enable row level security;
