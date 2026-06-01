-- Layover-Reviews (Worker P6, 2026-06-01).
-- Sterne-Ratings pro (iata, user_token, category) — User darf in jeder
-- Kategorie genau ein Rating pro Airport abgeben. Re-Bewertung = Upsert.
--
-- Schema-Entscheidungen:
--  · PK (iata, user_token, category) → ein Eintrag pro User/Spot/Kategorie,
--    Upsert für Re-Rating, idempotent.
--  · category als CHECK statt FK auf eine kategorie-Tabelle — Kategorien
--    ändern sich selten, App-Side-Enum reicht (siehe LAYOVER_REVIEW_CATEGORIES
--    im Backend).
--  · stars 1..5 hart constrained · 0 ist explizit kein-Rating.
--  · Index auf iata für die Aggregate-Query (avg group by category).
create table if not exists public.layover_reviews (
    iata        text         not null,
    user_token  text         not null,
    category    text         not null,
    stars       smallint     not null,
    created_at  timestamptz  not null default now(),
    updated_at  timestamptz  not null default now(),
    primary key (iata, user_token, category),
    check (category in ('overall','hotel','food','safety','nightlife')),
    check (stars between 1 and 5)
);
create index if not exists idx_layover_reviews_iata
    on public.layover_reviews(iata);
create index if not exists idx_layover_reviews_iata_cat
    on public.layover_reviews(iata, category);
alter table public.layover_reviews enable row level security;
