-- Likes + Kommentare für AeroX-Redaktions-News (Owner 2026-08-11:
-- „option zum komentieren und liken").
--
-- ARTIKEL-ID: `article_id` ist die im Feed/Redaktions-Store verwendete ID,
-- also sha256(canonical_url)[:16] (news_blueprint._parse_entry). Sie ist an
-- die kanonische Quell-URL gebunden und damit über Re-Ingest, Container-
-- Restart und Store-Rebuild hinweg STABIL — deshalb ist sie hier ein
-- schlichter `text`-Key ohne FK: die Artikel selbst leben im flüchtigen
-- In-Memory-/Datei-Store (Cap 200), die Interaktionen sollen sie überleben.
-- Ein Artikel, der aus dem Store fällt, verliert seine Likes NICHT.
--
-- STORY-CLUSTER: die Ausspiel-Liste dedupliziert Stories (`_story_dedupe_*`,
-- 10.08.) und der Cluster-GEWINNER kann wechseln, sobald eine neuere Quelle
-- dieselbe Meldung bringt. Deshalb wird NICHT auf eine „Story-ID" geschrieben
-- (die gibt es im Repo nicht und sie wäre nicht stabil), sondern weiter auf
-- die Artikel-ID; die App-Schicht aggregiert beim Ausspielen über alle IDs
-- des Clusters. Ein Like überlebt damit auch einen Gewinner-Wechsel.
--
-- Zugriff wie ax_smp_user_cards / flight_checkins / crew_dm_requests:
-- ausschließlich über den service_role-Client des Backends, das vorher den
-- Bearer gegen auth_users geprüft hat. RLS an, KEIN policy-Grant.
-- Ein Token ist das Credential — es verlässt diese Tabellen nie als Response-
-- Feld (die App-Schicht liefert AXU-Public-Refs aus).

-- ── Likes ────────────────────────────────────────────────────────────────
-- Ein Like je (Artikel, User). Der Primärschlüssel IST die Idempotenz:
-- doppeltes POST kann keinen zweiten Like erzeugen, ein Toggle löscht die
-- Zeile. Kein Zähler-Feld am Artikel — es gibt keine Artikel-Zeile, die einen
-- Zähler tragen könnte, und ein separater Zähler könnte von der Wahrheit
-- abdriften (Forum-Lehre: Counter + Rows müssen sonst versöhnt werden).
create table if not exists public.ax_news_likes (
    article_id  text        not null check (char_length(article_id) between 1 and 64),
    user_token  text        not null,
    created_at  timestamptz not null default now(),
    primary key (article_id, user_token)
);

-- Zähl-Aggregat über eine Liste von Artikel-IDs (Story-Cluster) und
-- „habe ich geliked?" für den Betrachter.
create index if not exists idx_ax_news_likes_article
    on public.ax_news_likes (article_id);

-- „Meine Likes" beim Aufräumen/Account-Löschen.
create index if not exists idx_ax_news_likes_user
    on public.ax_news_likes (user_token);

alter table public.ax_news_likes enable row level security;

-- ── Kommentare ───────────────────────────────────────────────────────────
-- `body` ist bereits sanitisierter Text (app._sanitize_user_text: HTML-Escape
-- + Control-Char-Strip). Der Längen-Check ist die zweite Sicherung hinter dem
-- 2000-Zeichen-Deckel der App-Schicht.
--
-- WARUM 12000 UND NICHT 2000 (13.08.2026): der Deckel der App-Schicht misst
-- den ROHTEXT (413 `too_long` bei > 2000). Was hier ankommt, ist der ESCAPTE
-- Text — ein `'` wird zu `&#x27;` (6 Zeichen). 2000 rohe Zeichen können also
-- bis zu 12000 gespeicherte werden. Mit dem alten 2000er-Check wäre ein
-- normaler Kommentar mit vielen Apostrophen abgelehnt worden (503); vorher
-- schnitt der Sanitizer ihn dafür still MITTEN in eine Entity. Beides falsch:
-- der Rohtext-Deckel ist die eine Wahrheit, dieser Check nur die Notbremse.
create table if not exists public.ax_news_comments (
    id           uuid        primary key,
    article_id   text        not null check (char_length(article_id) between 1 and 64),
    author_token text        not null,
    body         text        not null check (char_length(body) between 1 and 12000),
    created_at   timestamptz not null default now()
);

-- Bestandstabellen (Migration lief schon mit dem 2000er-Check) nachziehen.
-- Idempotent: erst den alten Auto-Namen weg, dann den benannten Check nur,
-- wenn er fehlt.
alter table public.ax_news_comments
    drop constraint if exists ax_news_comments_body_check;
do $$
begin
    if not exists (
        select 1 from pg_constraint
         where conname = 'ax_news_comments_body_len'
           and conrelid = 'public.ax_news_comments'::regclass
    ) then
        alter table public.ax_news_comments
            add constraint ax_news_comments_body_len
            check (char_length(body) between 1 and 12000);
    end if;
end $$;

-- Kommentar-Liste je Artikel (neueste zuerst, keyset über created_at) UND
-- comment_count je Story-Cluster.
create index if not exists idx_ax_news_comments_article
    on public.ax_news_comments (article_id, created_at desc);

-- Eigene Kommentare (Löschen / Account-Löschung).
create index if not exists idx_ax_news_comments_author
    on public.ax_news_comments (author_token);

alter table public.ax_news_comments enable row level security;

-- ── Zähl-Aggregat als RPC ────────────────────────────────────────────────
-- Warum überhaupt eine Funktion: PostgREST liefert bei einem einfachen
-- SELECT höchstens 1000 Zeilen (Repo-Lehre) — bei 100 ausgespielten Artikeln
-- würden Likes ab der 1000. Zeile still fehlen und der Zähler wäre ZU KLEIN.
-- Ein stiller Zahlendreher ist schlimmer als eine fehlende Zahl. Die Funktion
-- zählt in Postgres und liefert genau eine Zeile je Artikel-ID.
--
-- Die App-Schicht hat einen Fallback auf einfache SELECTs, falls diese
-- Migration auf einem Origin noch nicht durch ist (Lehre fcm_token 01.08.:
-- eine fehlende Spalte darf ein Feature nicht komplett töten).
create or replace function public.ax_news_interaction_counts(
    p_article_ids text[],
    p_viewer_token text default null
) returns table (
    article_id   text,
    like_count   bigint,
    comment_count bigint,
    liked_by_me  boolean
)
language sql
stable
security definer
set search_path = public
as $$
    select
        ids.article_id,
        coalesce(l.cnt, 0)                     as like_count,
        coalesce(c.cnt, 0)                     as comment_count,
        coalesce(mine.hit, false)              as liked_by_me
    from unnest(p_article_ids) as ids(article_id)
    left join lateral (
        select count(*) as cnt from public.ax_news_likes k
        where k.article_id = ids.article_id
    ) l on true
    left join lateral (
        select count(*) as cnt from public.ax_news_comments m
        where m.article_id = ids.article_id
    ) c on true
    left join lateral (
        select true as hit from public.ax_news_likes k
        where k.article_id = ids.article_id
          and p_viewer_token is not null
          and k.user_token = p_viewer_token
        limit 1
    ) mine on true;
$$;


-- ── Zähl-Aggregat je STORY-CLUSTER ───────────────────────────────────────
-- Die Variante oben zählt je ARTIKEL; die App-Schicht summierte danach über
-- den Cluster — und zählte damit denselben Menschen doppelt, sobald er zwei
-- Artikel derselben Story geliked hatte (der Like-PK ist (article_id,
-- user_token), zwei Zeilen sind völlig legal). Ein Like ist aber ein MENSCH,
-- keine Zeile: hier `count(distinct user_token)` über alle IDs des Clusters.
-- Kommentare bleiben `count(*)` — zwei Kommentare sind zwei Kommentare.
--
-- `p_groups` = {"<schlüssel>": ["artikel_id", …], …}; der Schlüssel ist die
-- ausgespielte Karten-ID. Eigene Funktion statt Signatur-Änderung, damit ein
-- Origin mit der alten Migration weiterläuft (die App-Schicht fällt auf den
-- SELECT-Pfad zurück, s. news_blueprint._news_counts).
create or replace function public.ax_news_interaction_counts_grouped(
    p_groups jsonb,
    p_viewer_token text default null
) returns table (
    group_key    text,
    like_count   bigint,
    comment_count bigint,
    liked_by_me  boolean
)
language sql
stable
security definer
set search_path = public
as $$
    with g as (
        select key as group_key,
               array(select jsonb_array_elements_text(value)) as ids
          from jsonb_each(coalesce(p_groups, '{}'::jsonb))
    )
    select
        g.group_key,
        coalesce(l.cnt, 0)        as like_count,
        coalesce(c.cnt, 0)        as comment_count,
        coalesce(mine.hit, false) as liked_by_me
    from g
    left join lateral (
        select count(distinct k.user_token) as cnt from public.ax_news_likes k
        where k.article_id = any(g.ids)
    ) l on true
    left join lateral (
        select count(*) as cnt from public.ax_news_comments m
        where m.article_id = any(g.ids)
    ) c on true
    left join lateral (
        select true as hit from public.ax_news_likes k
        where k.article_id = any(g.ids)
          and p_viewer_token is not null
          and k.user_token = p_viewer_token
        limit 1
    ) mine on true;
$$;


-- ── Grants ───────────────────────────────────────────────────────────────
-- PFLICHT, nicht Kosmetik: `security definer` ohne Entzug ist per Default für
-- PUBLIC ausführbar — anon hätte über `p_viewer_token` ein LIKE-ORAKEL
-- gehabt („hat Token X diesen Artikel geliked?"). Muster 1:1 aus
-- 20260727_live_activities.sql:219-224. Nur das Backend (service_role), das
-- den Bearer vorher gegen auth_users geprüft hat, darf hier ran.
revoke all on function public.ax_news_interaction_counts(text[], text) from public, anon;
grant execute on function public.ax_news_interaction_counts(text[], text) to service_role;

revoke all on function public.ax_news_interaction_counts_grouped(jsonb, text) from public, anon;
grant execute on function public.ax_news_interaction_counts_grouped(jsonb, text) to service_role;
