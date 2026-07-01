-- Data-Layer-Refactor (2026-07-01): atomare Counter-RPCs + Metadata-Merge + Indexe.
-- Vorher liefen Social-Mutationen als load-ALL → mutate-in-Python → save-ALL
-- (bis zu 5000 Rows Bulk-Upsert für EINEN Like). Zwei Cloud-Run-Instanzen
-- verloren sich gegenseitig Updates (read-modify-write über den ganzen
-- Datensatz). app.py schreibt jetzt per-Row; Counter-Bumps laufen über die
-- RPCs hier — atomar server-seitig, kein Lost-Update mehr.
--
-- WICHTIG: app.py funktioniert auch OHNE diese Migration (degradierter
-- read-modify-write-Fallback pro Row, einmalige Warnung im Log). Für die
-- rennsicheren Counter die Datei einmal im Supabase SQL-Editor ausführen.
--
-- Pattern wie crew_edges_upsert_increment (20260601_crew_graph.sql):
--   * RPC optional — Python-Fallback wenn sie fehlt
--   * Service-Role-Key umgeht RLS; Anon-Client bleibt geblockt

-- ─────────── WALL-POSTS: Like/Dislike/Comment-Counter atomar ───────────
-- dislike_count ist KEINE echte Spalte (lebt in metadata-jsonb, siehe
-- _SB_SCHEMA_WALL_POSTS in app.py) → jsonb_set im selben UPDATE.
create or replace function public.wall_post_counters_apply(
    p_post_id  text,
    p_like     int default 0,
    p_dislike  int default 0,
    p_comment  int default 0
) returns table(like_count int, dislike_count int, comment_count int)
language plpgsql
as $$
begin
    return query
    update public.wall_posts wp
    set like_count    = greatest(0, coalesce(wp.like_count, 0) + p_like),
        comment_count = greatest(0, coalesce(wp.comment_count, 0) + p_comment),
        metadata = case
            when p_dislike = 0 then coalesce(wp.metadata, '{}'::jsonb)
            else jsonb_set(
                coalesce(wp.metadata, '{}'::jsonb),
                '{dislike_count}',
                to_jsonb(greatest(0,
                    coalesce((wp.metadata ->> 'dislike_count')::int, 0) + p_dislike))
            )
        end
    where wp.id = p_post_id
    returning wp.like_count,
              coalesce((wp.metadata ->> 'dislike_count')::int, 0),
              wp.comment_count;
end;
$$;

-- ─────────── FORUM-THREADS: Like/Reply-Counter + last_reply_ts atomar ──────
-- last_reply_ts ist keine echte Spalte (metadata-jsonb); NULL ⇒ nicht anfassen.
create or replace function public.forum_thread_counters_apply(
    p_thread_id     text,
    p_like          int default 0,
    p_reply         int default 0,
    p_last_reply_ts numeric default null
) returns table(like_count int, reply_count int)
language plpgsql
as $$
begin
    return query
    update public.forum_threads ft
    set like_count  = greatest(0, coalesce(ft.like_count, 0) + p_like),
        reply_count = greatest(0, coalesce(ft.reply_count, 0) + p_reply),
        metadata = case
            when p_last_reply_ts is null then coalesce(ft.metadata, '{}'::jsonb)
            else jsonb_set(coalesce(ft.metadata, '{}'::jsonb),
                           '{last_reply_ts}', to_jsonb(p_last_reply_ts))
        end
    where ft.id = p_thread_id
    returning ft.like_count, ft.reply_count;
end;
$$;

-- ─────────── FORUM-REPLIES: Like-Counter atomar ───────────
create or replace function public.forum_reply_like_apply(
    p_reply_id text,
    p_delta    int default 0
) returns table(like_count int)
language plpgsql
as $$
begin
    return query
    update public.forum_replies fr
    set like_count = greatest(0, coalesce(fr.like_count, 0) + p_delta)
    where fr.id = p_reply_id
    returning fr.like_count;
end;
$$;

-- ─────────── USER-PROFILES: atomarer metadata-Merge ───────────
-- Ersetzt den read-merge-upsert in _profile_save_to_supabase (der historische
-- Avatar-Clobber-Bug-Klasse: zwei Instanzen lasen dieselbe metadata, mergten
-- lokal, letzter Upsert gewann → avatar_url/current_city weg). `||` merged
-- server-seitig in EINEM Statement.
-- Returns die Zahl der upgedateten Rows: 0 ⇒ Row existiert noch nicht, der
-- Python-Caller legt sie dann über den Fallback-Pfad inkl. metadata an.
create or replace function public.profile_metadata_merge(
    p_token text,
    p_patch jsonb
) returns int
language plpgsql
as $$
declare
    n int;
begin
    update public.user_profiles
    set metadata   = coalesce(metadata, '{}'::jsonb) || coalesce(p_patch, '{}'::jsonb),
        updated_at = now()
    where token = p_token;
    get diagnostics n = row_count;
    return n;
end;
$$;

-- ─────────── Indexe ───────────
-- wall_likes wird per user_token gefiltert (_wall_likes_load_from_supabase),
-- der PK ist (post_id, user_token) → ohne Index Seq-Scan pro Feed-Read.
create index if not exists idx_wall_likes_user on public.wall_likes(user_token);
-- Avatar-Serve löst den R2-Key über metadata->>'avatar_dir_key' auf.
create index if not exists idx_profiles_avatar_dir
    on public.user_profiles ((metadata->>'avatar_dir_key'));
