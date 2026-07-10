-- 20260710_news_fulltext.sql — permanenter News-Volltext-Speicher.
--
-- Kontext (Owner 2026-07-10): "Nachrichten-Text ist nicht gespeichert/gescrapt
-- im Backend, damit es schneller lädt und der Text direkt voll da ist."
-- Der News-Feed (blueprints/news_blueprint.py) erntet Artikel-Volltexte jetzt
-- beim Aggregat im Hintergrund (nur neue Artikel, höflich gedrosselt) und
-- liefert sie direkt im Feed-Payload mit.
--
-- Speicher = die BESTEHENDE Tabelle public.news_article_cache (angelegt durch
-- supabase_migrations/news_article_cache.sql als L2-Cache des On-Demand-Readers
-- /api/news/article in app.py). Diese Migration:
--   1. legt die Tabelle an, falls die Basis-Migration nie applied wurde
--      (identisches DDL, idempotent), und
--   2. ergänzt die Spalte `harvested_at` — markiert Zeilen, die vom
--      Feed-Harvester geschrieben wurden.
--
-- Permanenz-Semantik: Zeilen werden NIE gelöscht. Das 14-Tage-TTL in app.py
-- (`NEWS_ARTICLE_SB_CACHE_TTL_SECONDS`) steuert nur, wann der On-Demand-Reader
-- die Quellseite erneut anfasst; der Feed-Pfad (news_blueprint:
-- _fulltext_store_get_many) liest fulltext OHNE TTL — einmal geernteter Text
-- bleibt lieferbar, auch wenn die Quelle ihn depubliziert.
--
-- Idempotent — mehrfaches Anwenden ist safe. NICHT automatisch applied;
-- der Backend-Code toleriert sowohl fehlende Tabelle als auch fehlende
-- harvested_at-Spalte (Retry ohne Spalte) und degradiert auf Teaser+Link.

create table if not exists public.news_article_cache (
    url_key      text primary key,            -- sha256(url) hex, erste 32 Zeichen
    url          text not null,
    fulltext     text,
    title        text,
    source       text,                         -- Artikel-Host, z.B. aerotelegraph.com
    image_url    text,                         -- og:image (Hero-Bild bleibt bei Cache-Hits)
    published_at text,                         -- article:published_time (best effort)
    fetched_at   timestamptz not null default now()
);

-- Feed-Harvester-Marker: wann der Hintergrund-Harvest diese Zeile (zuletzt)
-- geschrieben hat. NULL = Zeile stammt (bisher) nur vom On-Demand-Reader.
alter table public.news_article_cache
    add column if not exists harvested_at timestamptz;

-- TTL-/Staleness-Checks des On-Demand-Readers ordnen nach fetched_at.
create index if not exists news_article_cache_fetched_at_idx
    on public.news_article_cache (fetched_at);

-- Service-Role (Backend) umgeht RLS; RLS ohne Public-Policies aktivieren,
-- damit die Tabelle nicht über die anon/public-API-Fläche exponiert ist.
alter table public.news_article_cache enable row level security;
