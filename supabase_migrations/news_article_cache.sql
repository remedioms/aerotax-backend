-- Persistent server-side cache for extracted news article fulltext.
--
-- Purpose: the article endpoint (/api/news/article) scrapes the source site
-- and runs _news_extract_best_fulltext() to get a clean reading text. Without a
-- shared cache every user load re-scrapes the source — which flags / rate-limits
-- our server IP. This table makes each article get scraped at most ~once per TTL
-- (14 days, enforced in app.py) instead of once per user.
--
-- Keyed by url_key = sha256(article_url)[:32] (hex). The app tolerates this table
-- being absent (falls back to live scrape), so applying this migration is
-- optional-but-recommended.

create table if not exists public.news_article_cache (
    url_key      text primary key,            -- sha256(url) hex, first 32 chars
    url          text not null,
    fulltext     text,
    title        text,
    source       text,                         -- article host, e.g. aerotelegraph.com
    image_url    text,                         -- og:image (preserved so cache hits keep the hero image)
    published_at text,                         -- article:published_time (best effort)
    fetched_at   timestamptz not null default now()
);

-- TTL lookups order by fetched_at; index keeps staleness checks cheap.
create index if not exists news_article_cache_fetched_at_idx
    on public.news_article_cache (fetched_at);

-- Service-role (backend) bypasses RLS; enable RLS with no public policies so the
-- table is not exposed via the anon/public API surface.
alter table public.news_article_cache enable row level security;
