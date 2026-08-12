-- News-Kommentare tragen ein Medium (GIF-Kette, Owner-Go 2026-08-12).
--
-- WARUM: Chat, Forum und Crew-Feed haben längst ein Bild-Feld (`image_url`),
-- News-Kommentare hatten NUR `{body}`. Damit ein GIF auch unter einer Story
-- landen kann, braucht die Zeile einen Verweis.
--
-- WAS GESPEICHERT WIRD: NIE Bytes, nur der backend-eigene Serve-Pfad aus
-- `POST /api/wall/<token>/upload-image`, also `/api/wall/image/<key>/<datei>`.
-- Die Bytes liegen damit in DERSELBEN Ablage wie jedes andere Foto (R2
-- `wall/…`, Disk-Fallback) — kein zweiter Speicherort, kein externer Dienst.
-- Die App-Schicht (`news_blueprint._news_media_url`) verwirft alles mit Schema
-- oder Host, damit kein fremder Tracking-Beacon in die Kommentare kommt
-- (gleiche Regel wie `app._own_image_url_only`, Full-Review 01.08.).
--
-- REIHENFOLGE (PFLICHT): erst diese Migration, dann der Backend-Deploy. Das
-- Backend schickt `media_url` NUR mit, wenn wirklich ein Medium dranhängt —
-- ein Text-Kommentar funktioniert also auch auf einem Origin, wo die Spalte
-- noch fehlt. Ein GIF-Kommentar würde dort mit 503 `store_failed` ehrlich
-- scheitern, statt still ohne Bild gespeichert zu werden.
alter table public.ax_news_comments
    add column if not exists media_url text;

-- Länge deckeln: der Pfad ist `/api/wall/image/<32-hex>/<12-hex>.<ext>`, also
-- deutlich unter 512. Der Check ist die Notbremse gegen einen aufgeblähten
-- Wert, nicht die Validierung (die macht die App-Schicht).
do $$
begin
    if not exists (
        select 1 from pg_constraint
         where conname = 'ax_news_comments_media_url_len'
           and conrelid = 'public.ax_news_comments'::regclass
    ) then
        alter table public.ax_news_comments
            add constraint ax_news_comments_media_url_len
            check (media_url is null or char_length(media_url) between 1 and 512);
    end if;
end $$;

-- ── Anonyme Kommentare (Owner 12.08. „plus für anonym bild und so") ──────
-- Gleiches Muster wie Wall-Posts und Layover-Stories: `is_anonymous` schaltet
-- die Anzeige um, `anon_handle` ist der pseudonyme Name (HMAC mit Server-
-- Secret + Per-Kommentar-Salt, siehe app._anon_handle_for — NICHT aus dem
-- Token rekonstruierbar und zwischen zwei Kommentaren desselben Users nicht
-- verkettbar).
--
-- WICHTIG: `author_token` bleibt gefüllt. Anonym ist der Kommentar gegenüber
-- anderen NUTZERN, nicht gegenüber der Moderation — Melden (kind=
-- 'news_comment') und Blockieren müssen den Autor weiter auflösen können.
-- Die App-Schicht liefert bei `is_anonymous` weder AXU-Ref noch Klarnamen aus.
alter table public.ax_news_comments
    add column if not exists is_anonymous boolean not null default false;
alter table public.ax_news_comments
    add column if not exists anon_handle text;

do $$
begin
    if not exists (
        select 1 from pg_constraint
         where conname = 'ax_news_comments_anon_handle_len'
           and conrelid = 'public.ax_news_comments'::regclass
    ) then
        alter table public.ax_news_comments
            add constraint ax_news_comments_anon_handle_len
            check (anon_handle is null or char_length(anon_handle) between 1 and 64);
    end if;
end $$;

-- BODY DARF JETZT LEER SEIN — aber nur mit Medium.
--
-- Ein reines GIF ist ein vollwertiger Kommentar; der bisherige Check
-- (`char_length(body) between 1 and 12000`) hätte ihn abgelehnt. Statt den
-- Schutz zu streichen wird er präzisiert: leerer Text ist NUR erlaubt, wenn
-- ein Medium dranhängt. Eine komplett leere Zeile bleibt unmöglich.
alter table public.ax_news_comments
    drop constraint if exists ax_news_comments_body_len;
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
            check (char_length(body) between 0 and 12000);
    end if;
    if not exists (
        select 1 from pg_constraint
         where conname = 'ax_news_comments_not_empty'
           and conrelid = 'public.ax_news_comments'::regclass
    ) then
        alter table public.ax_news_comments
            add constraint ax_news_comments_not_empty
            check (char_length(body) > 0 or media_url is not null);
    end if;
end $$;
