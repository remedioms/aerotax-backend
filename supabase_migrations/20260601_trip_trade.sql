-- Trip-Trade Board (Worker P6b, 2026-06-01).
-- Open-Time / Swap / Pickup-Marketplace für Crew-Touren.
--
-- Schema-Entscheidungen:
--  · trade_posts.id als text-PK (UUID-string vom Backend generiert) statt
--    bigserial — passt zu unserer Token-Welt + leichter idempotent zu handeln.
--  · soft-delete via deleted-Boolean statt DELETE-Row → Audit-Spur bleibt,
--    Author-Side kann sehen welche Posts er zurückgezogen hat.
--  · status open|in_negotiation|closed als text statt enum-Type — Migrations
--    bei zusätzlichen Status sind einfacher (kein ALTER TYPE).
--  · swap_or_dump = 'swap' | 'dump' | 'pickup'. Default 'swap' weil das der
--    häufigste Use-Case ist.
--  · Partial-Index `WHERE deleted=false AND status='open'` weil das Board
--    nur diese Posts listet — Index ist kompakt und Query selektiv.
--  · trade_interests separat (1:N): ein Post kann viele Interessenten haben.
--    Self-Interest wird im Backend geblockt (Self-Trade-Prevention), nicht
--    via DB-Constraint, weil der Author-Token nicht denormalisiert ist.
create table if not exists public.trade_posts (
    id                     text         primary key,
    author_token           text         not null,
    author_short_name      text,
    position               text,
    base                   text,
    airline                text,
    tour_start_date        date         not null,
    tour_end_date          date,
    routing                text,
    swap_or_dump           text         not null default 'swap',
    compensation_offered   text,
    qualification_required text,
    message                text,
    status                 text         not null default 'open',
    deleted                boolean      not null default false,
    created_at             timestamptz  not null default now(),
    updated_at             timestamptz  not null default now()
);

create index if not exists idx_trade_posts_filter
    on public.trade_posts(airline, base, tour_start_date)
    where deleted = false and status = 'open';

create index if not exists idx_trade_posts_author
    on public.trade_posts(author_token);

create table if not exists public.trade_interests (
    id                text         primary key,
    post_id           text         not null,
    interested_token  text         not null,
    message           text,
    created_at        timestamptz  not null default now()
);

create index if not exists idx_trade_interests_post
    on public.trade_interests(post_id);

create index if not exists idx_trade_interests_token
    on public.trade_interests(interested_token);

alter table public.trade_posts enable row level security;
alter table public.trade_interests enable row level security;
