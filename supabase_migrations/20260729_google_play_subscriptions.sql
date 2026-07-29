-- Durable Google Play subscription index for RTDN. Purchase tokens are never
-- stored; Pub/Sub tokens are SHA-256 hashed before lookup.

create table if not exists public.google_play_subscriptions (
    purchase_token_hash text primary key
        check (length(purchase_token_hash) = 64),
    user_token text not null,
    product_id text not null,
    package_name text not null,
    active boolean not null default false,
    state text,
    valid_until timestamptz,
    acknowledged boolean not null default false,
    verified_at timestamptz not null,
    updated_at timestamptz not null default now()
);

create index if not exists ix_google_play_subscriptions_user
    on public.google_play_subscriptions(user_token);

alter table public.google_play_subscriptions enable row level security;
revoke all on table public.google_play_subscriptions
    from public, anon, authenticated;
grant select, insert, update, delete on table public.google_play_subscriptions
    to service_role;
