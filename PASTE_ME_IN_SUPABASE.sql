-- ────────────────────────────────────────────────────────────────────────
-- AEROTAX/AERIS — Konsolidierte Migrations für Supabase Dashboard
-- Stand: 2026-06-01 (Update: + Aircraft-Health + Rate-Limit-Buckets)
-- 10 Tabellen-Blöcke. Idempotent (CREATE TABLE IF NOT EXISTS).
-- 
-- https://app.supabase.com/project/jyrbijvmwacuivssbxlg → SQL Editor → New query → Run
-- ────────────────────────────────────────────────────────────────────────

-- ╔════ 20260531_user_data ════╗
-- User-Daten in Supabase persistieren (P0-Fix Phase 2): Profile + Friends +
-- Push-Tokens. Vorher lebten profile_<token>.json, friends_<token>.json,
-- push_<token>.json auf Container-ephemeral-disk und verschwanden bei jedem
-- Cloud-Run-Redeploy. Reihenfolge: 20260531_auth_users.sql zuerst.

-- ── Profile ──────────────────────────────────────────────────────────
-- Pro Token genau eine Zeile mit den User-Profilfeldern.
-- `employers` als jsonb (Multi-Employer aus A4). `metadata` für alle
-- Felder die nicht im Whitelist sind aber persistiert werden sollen.
create table if not exists public.user_profiles (
    token        text         primary key,
    name         text,
    homebase     text,
    airline      text,
    "position"   text,
    hometown     text,
    share_roster boolean      not null default false,
    employers    jsonb        not null default '[]'::jsonb,
    metadata     jsonb        not null default '{}'::jsonb,
    created_at   timestamptz  not null default now(),
    updated_at   timestamptz  not null default now()
);
create index if not exists idx_user_profiles_homebase
    on public.user_profiles(homebase) where homebase is not null;
create index if not exists idx_user_profiles_airline
    on public.user_profiles(airline) where airline is not null;
alter table public.user_profiles enable row level security;


-- ── Friends ──────────────────────────────────────────────────────────
-- Eine Zeile = eine gerichtete Buddy-Beziehung (owner sieht friend).
-- Reziproke Freundschaft = 2 Zeilen (a→b und b→a) — vereinfacht Queries.
create table if not exists public.user_friends (
    owner_token   text         not null,
    friend_token  text         not null,
    status        text         not null default 'accepted',
    created_at    timestamptz  not null default now(),
    metadata      jsonb        not null default '{}'::jsonb,
    primary key (owner_token, friend_token),
    check (status in ('pending', 'accepted', 'blocked'))
);
create index if not exists idx_user_friends_friend_token
    on public.user_friends(friend_token);
create index if not exists idx_user_friends_owner_status
    on public.user_friends(owner_token, status);
alter table public.user_friends enable row level security;


-- ── Push-Tokens ──────────────────────────────────────────────────────
-- Routing von app-Token → APNs/Expo-Token. Pro User-Token ein Eintrag.
-- Bei Re-Install ein neuer device_token, alter wird via updated_at sichtbar.
create table if not exists public.user_push_tokens (
    user_token   text         primary key,
    expo_token   text,
    apns_token   text,
    device_id    text,
    platform     text,
    metadata     jsonb        not null default '{}'::jsonb,
    updated_at   timestamptz  not null default now()
);
create index if not exists idx_push_expo on public.user_push_tokens(expo_token) where expo_token is not null;
create index if not exists idx_push_apns on public.user_push_tokens(apns_token) where apns_token is not null;
alter table public.user_push_tokens enable row level security;

-- ╔════ 20260601_social ════╗
-- Social-Persistenz in Supabase: Wall-Posts, Forum-Threads/Replies, DM-Messages.
-- Vorher lebten alle drei in ephemeral disk-Files unter _USER_HISTORY_DIR (wall/
-- posts.json, forum/threads.json + replies_<id>.json, chat/<channel>.json).
-- Cloud-Run-Redeploy wipte den disk → ALLE Posts/Threads/DMs weg pro Release.
-- Bei 5000 User unbenutzbar.
--
-- Pattern wie auth_users + user_profiles (siehe 20260531_*.sql):
--   * eine Tabelle pro Domain
--   * häufig gefilterte Felder als eigene columns
--   * jsonb metadata für unknown/extension keys
--   * Service-Role-Key umgeht RLS (Anon-Client bleibt geblockt)

-- ─────────── WALL-POSTS ───────────
create table if not exists public.wall_posts (
    id text primary key,
    author_token text not null,
    ts numeric not null,
    body text,
    layover_iata text,
    image_url text,
    hashtags jsonb not null default '[]',
    like_count int not null default 0,
    comment_count int not null default 0,
    deleted boolean not null default false,
    metadata jsonb not null default '{}'
);
create index if not exists idx_wall_posts_ts
    on public.wall_posts(ts desc) where deleted = false;
create index if not exists idx_wall_posts_author
    on public.wall_posts(author_token);
alter table public.wall_posts enable row level security;

-- ─────────── WALL-LIKES ───────────
create table if not exists public.wall_likes (
    post_id text not null,
    user_token text not null,
    created_at timestamptz not null default now(),
    primary key (post_id, user_token)
);
alter table public.wall_likes enable row level security;

-- ─────────── WALL-COMMENTS ───────────
create table if not exists public.wall_comments (
    id text primary key,
    post_id text not null,
    author_token text not null,
    body text,
    ts numeric not null,
    parent_id text,
    image_url text,
    metadata jsonb not null default '{}'
);
create index if not exists idx_wall_comments_post
    on public.wall_comments(post_id, ts);
alter table public.wall_comments enable row level security;

-- ─────────── FORUM-THREADS ───────────
create table if not exists public.forum_threads (
    id text primary key,
    category_id text not null,
    author_token text not null,
    title text,
    body text,
    ts numeric not null,
    hashtags jsonb not null default '[]',
    like_count int not null default 0,
    reply_count int not null default 0,
    deleted boolean not null default false,
    metadata jsonb not null default '{}'
);
create index if not exists idx_forum_threads_cat_ts
    on public.forum_threads(category_id, ts desc);
alter table public.forum_threads enable row level security;

-- ─────────── FORUM-REPLIES ───────────
create table if not exists public.forum_replies (
    id text primary key,
    thread_id text not null,
    author_token text not null,
    body text,
    ts numeric not null,
    parent_reply_id text,
    mentioned_token text,
    image_url text,
    like_count int not null default 0,
    metadata jsonb not null default '{}'
);
create index if not exists idx_forum_replies_thread
    on public.forum_replies(thread_id, ts);
alter table public.forum_replies enable row level security;

-- ─────────── DM-MESSAGES ───────────
create table if not exists public.dm_messages (
    id text primary key,
    channel_id text not null,
    author_token text not null,
    body text,
    ts numeric not null,
    image_url text,
    metadata jsonb not null default '{}'
);
create index if not exists idx_dm_messages_channel
    on public.dm_messages(channel_id, ts);
alter table public.dm_messages enable row level security;

-- ─────────── FORUM-LIKES (per user) ───────────
-- Pro Liker/Target gibt es eine Row. target_type ∈ {'thread','reply'}.
-- Composite-PK verhindert Doppel-Likes deterministisch.
create table if not exists public.forum_likes (
    user_token text not null,
    target_type text not null,
    target_id text not null,
    created_at timestamptz not null default now(),
    primary key (user_token, target_type, target_id)
);
create index if not exists idx_forum_likes_user
    on public.forum_likes(user_token);
alter table public.forum_likes enable row level security;

-- ─────────── DM-LASTSEEN (per user/channel) ───────────
-- Pro User+Channel-Kombi der letzte gelesen-Timestamp für Unread-Counter.
create table if not exists public.dm_lastseen (
    user_token text not null,
    channel_id text not null,
    last_seen_ts numeric not null default 0,
    primary key (user_token, channel_id)
);
create index if not exists idx_dm_lastseen_user
    on public.dm_lastseen(user_token);
alter table public.dm_lastseen enable row level security;

-- ╔════ 20260601_layover_reviews ════╗
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

-- ╔════ 20260601_friend_groups ════╗
-- Friend-Groups in Supabase persistieren (Worker-H Polish, 2026-06-01).
-- Vorher lebten groups[] nur im disk-File friends_<token>.json — die SB-
-- Migration der Friends in 20260531_user_data.sql hat groups bewusst nicht
-- mitmigriert (W18-Note: "Friend-`groups`: bleiben disk-only"). Cloud-Run-
-- Redeploy wischte die ephemeral disk → alle User-Gruppen weg.
--
-- Schema: pro owner_token mehrere Gruppen, jeweils mit name + members-jsonb.
-- members ist jsonb (Liste von friend_tokens) statt 1:N-Tabelle weil:
--  · Groups haben typisch <20 Members
--  · keine Cross-Group-Queries nötig (alle Reads sind owner-scoped)
--  · CRUD wird ein einzelnes upsert/delete statt N-Inserts
--  · UI lädt komplette Groups-Liste sowieso atomar
create table if not exists public.user_friend_groups (
    id            text         primary key,
    owner_token   text         not null,
    name          text         not null,
    members       jsonb        not null default '[]'::jsonb,
    created_at    timestamptz  not null default now(),
    updated_at    timestamptz  not null default now()
);
create index if not exists idx_user_friend_groups_owner
    on public.user_friend_groups(owner_token);
alter table public.user_friend_groups enable row level security;

-- ╔════ 20260601_license_wallet ════╗
-- License-Wallet cross-device-sync (Worker P4, 2026-06-01).
-- Persistiert die iOS LicenseItem-SwiftData-Models in Supabase damit der
-- gleiche User auf einem zweiten Gerät (oder nach Reinstall) seine Wallet
-- wieder bekommt. Disk-Fallback (licenses_<token>.json) übernimmt wenn SB
-- down ist — Schema spiegelt deshalb 1:1 die iOS-Item-Felder.
--
-- Schema-Entscheidungen:
--  · PK auf item-id (UUID-Text) — iOS generiert die UUID lokal, beim Sync
--    landet sie unverändert in der Tabelle. Bei Mehrgerät-Konflikt gewinnt
--    Last-Writer (updated_at).
--  · user_token getrennt indiziert, weil die häufigste Query
--    "list all items for token, deleted=false" ist.
--  · category als CHECK statt enum-Tabelle — die Liste ändert sich selten,
--    iOS-LicenseCategory.rawValue ist das single-source-of-truth.
--  · item_type als freier text — iOS LicenseItemType wächst mit neuen Lizenz-
--    Klassen (z.B. neue Type-Ratings); ein DB-Constraint hier wäre ein
--    Deploy-Blocker bei jeder neuen Konstante.
--  · photo_blob_id als text (Referenz auf separates Storage, falls je
--    server-side Photo-Upload kommt). Aktuell bleibt das Foto AES-GCM
--    verschlüsselt nur auf dem Device — der Server sieht den Cipher-Blob
--    bewusst NICHT (Privacy-by-default).
--  · custom_notes als text — der User kann hier Klartext eintippen; das ist
--    persönlich, RLS verhindert Cross-User-Access.
--  · alert_window_days als jsonb mit Default [90,60,30,7] passend zum
--    iOS-Default in LicenseItem.swift.
--  · deleted als boolean für Soft-Delete — App-Side filtert deleted=false,
--    Sync-Kollisionen können so noch erkannt werden ohne Hard-Delete.
--  · metadata jsonb als Catch-all für Felder die zukünftig vom Client
--    geschickt werden ohne dass die Tabelle migriert werden muss
--    (issuing_authority-Codes, Revalidation-Daten, etc.).
create table if not exists public.user_licenses (
    id                  text         primary key,
    user_token          text         not null,
    category            text         not null,
    item_type           text         not null,
    label               text,
    issue_date          date,
    expiry_date         date,
    issuing_authority   text,
    document_number     text,
    photo_blob_id       text,
    custom_notes        text,
    alert_window_days   jsonb        not null default '[90,60,30,7]'::jsonb,
    deleted             boolean      not null default false,
    metadata            jsonb        not null default '{}'::jsonb,
    created_at          timestamptz  not null default now(),
    updated_at          timestamptz  not null default now(),
    check (category in ('cockpit', 'cabin', 'general'))
);

-- Häufigste Query: "alle nicht-gelöschten Items für diesen User-Token".
create index if not exists idx_user_licenses_token
    on public.user_licenses(user_token) where deleted = false;

-- Sekundär-Query (Notification-Scheduler, Aggregat-Statistik): items die
-- in den nächsten N Tagen ablaufen.
create index if not exists idx_user_licenses_expiry
    on public.user_licenses(expiry_date) where deleted = false;

alter table public.user_licenses enable row level security;

-- ╔════ 20260601_crew_graph ════╗
-- Worker-P6a Crew-Graph Edges — Server-side aggregation of "who-flew-with-whom".
--
-- Pendant zur iOS-CrewGraphEdge (SwiftData @Model). Lokal auf dem Device gilt
-- das Privacy-by-Default-Modell: Klarnamen werden NICHT gespeichert, nur
-- otherShortName ("Schumann M.") + opaker otherToken. Server-side spiegeln wir
-- exakt dasselbe Modell, plus eine `other_id` Spalte die als stabiler Composite-
-- Key-Part dient (otherToken wenn App-User, sonst sha256(self_token+shortname)
-- truncated — siehe blueprint).
--
-- Composite-PK statt einzelne id: pro (self_token, other_id) gibt es genau eine
-- Edge — verhindert Race-induzierte Duplikate. Counter-Increment läuft
-- entweder via RPC (atomic update) oder SELECT-then-UPSERT-fallback in der App.
--
-- shared_layovers/shared_routes sind jsonb-Arrays (max 20 Einträge, capped im
-- Blueprint), nicht 1:N Tabellen — Cross-Edge-Queries auf Layover gibt es nicht,
-- und ein Edge-Read fasst beide atomar an.

create table if not exists public.crew_edges (
    self_token          text         not null,
    other_id            text         not null,
    other_token         text,
    other_display_name  text,
    other_position      text,
    tour_count          int          not null default 1,
    last_flown_date     date,
    shared_layovers     jsonb        not null default '[]'::jsonb,
    shared_routes       jsonb        not null default '[]'::jsonb,
    created_at          timestamptz  not null default now(),
    updated_at          timestamptz  not null default now(),
    primary key (self_token, other_id)
);

-- Hot-path: "Top-N strongest connections for me" sortiert nach tour_count desc.
create index if not exists idx_crew_edges_self
    on public.crew_edges(self_token, tour_count desc);

-- Reverse-Lookup: "ist <other_token> bekannt im Graph?" Bei NULL otherToken
-- (= nicht-App-User, nur shortname-hash) kein Sinn — partial index.
create index if not exists idx_crew_edges_other_token
    on public.crew_edges(other_token) where other_token is not null;

-- Service-Role-Key umgeht RLS. Anon-Client bleibt geblockt (kein Read/Write).
alter table public.crew_edges enable row level security;

-- Atomic-Counter-RPC (optional, blueprint nutzt SELECT-then-UPSERT als Fallback
-- wenn die RPC nicht existiert). Inkrementiert tour_count und merged die jsonb-
-- Arrays atomar in einer Transaction. Bei Race kein Doppel-Insert dank
-- ON CONFLICT.
create or replace function public.crew_edges_upsert_increment(
    p_self_token          text,
    p_other_id            text,
    p_other_token         text,
    p_other_display_name  text,
    p_other_position      text,
    p_tour_date           date,
    p_new_layovers        jsonb,
    p_new_routes          jsonb
) returns void
language plpgsql
as $$
begin
    insert into public.crew_edges (
        self_token, other_id, other_token, other_display_name, other_position,
        tour_count, last_flown_date, shared_layovers, shared_routes
    ) values (
        p_self_token, p_other_id, p_other_token, p_other_display_name,
        coalesce(p_other_position, ''),
        1, p_tour_date,
        coalesce(p_new_layovers, '[]'::jsonb),
        coalesce(p_new_routes,   '[]'::jsonb)
    )
    on conflict (self_token, other_id) do update
    set tour_count          = public.crew_edges.tour_count + 1,
        other_token         = coalesce(excluded.other_token, public.crew_edges.other_token),
        other_display_name  = coalesce(excluded.other_display_name, public.crew_edges.other_display_name),
        other_position      = coalesce(nullif(excluded.other_position, ''), public.crew_edges.other_position),
        last_flown_date     = greatest(
                                  coalesce(excluded.last_flown_date, public.crew_edges.last_flown_date),
                                  coalesce(public.crew_edges.last_flown_date, excluded.last_flown_date)
                              ),
        shared_layovers     = (
            select coalesce(jsonb_agg(distinct val), '[]'::jsonb)
            from (
                select val from jsonb_array_elements_text(public.crew_edges.shared_layovers) as val
                union
                select val from jsonb_array_elements_text(coalesce(excluded.shared_layovers, '[]'::jsonb)) as val
            ) merged
        ),
        shared_routes       = (
            select coalesce(jsonb_agg(distinct val), '[]'::jsonb)
            from (
                select val from jsonb_array_elements_text(public.crew_edges.shared_routes) as val
                union
                select val from jsonb_array_elements_text(coalesce(excluded.shared_routes, '[]'::jsonb)) as val
            ) merged
        ),
        updated_at          = now();
end;
$$;

-- ╔════ 20260601_trip_trade ════╗
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

-- ╔════ 20260601_aircraft_health ════╗
-- Aircraft-Health Crowd-Reports (Worker W-USP, 2026-06-01).
--
-- Crews reichen tail-spezifische Reports ein (IFE row 24-30 broken, Galley
-- freezer warm, Toilet vac intermittent). Die naechste Crew die auf derselben
-- Tail-Reg fliegt sieht beim Boarding "3 Berichte letzter 7 Tage · Tap fuer
-- Details".
--
-- Schema-Entscheidungen:
--  · PK report_id (uuid) statt composite — Listing-Queries gehen
--    immer ueber `tail_reg + created_at`, lookup by PK ist selten.
--  · `reported_by_token` ist gespeichert (Spam-/Abuse-Tracking serverseitig)
--    aber NIE im Listing-Output gerendert (siehe blueprint).
--  · `system` + `severity` als CHECK statt FK auf Enum-Tabellen — Werte sind
--    stabil im App-Code (siehe AircraftHealthClient.SystemCategory/Severity).
--  · `status` default 'open' — spaeter koennte ein Maintenance-Mod einen
--    Report als 'resolved' markieren (Listing kappt das dann optional).
--  · Description-Cap 280 chars wie iOS/Server-Validation.

create table if not exists public.aircraft_health_reports (
    report_id           uuid          primary key default gen_random_uuid(),
    tail_reg            text          not null,
    system              text          not null,
    severity            text          not null,
    description         text          not null,
    reported_by_token   text          not null,
    status              text          not null default 'open',
    created_at          timestamptz   not null default now(),
    updated_at          timestamptz   not null default now(),
    check (system in ('ife', 'galley', 'cabin', 'lavatory', 'avionics', 'other')),
    check (severity in ('info', 'minor', 'major')),
    check (status   in ('open', 'resolved')),
    check (char_length(description) between 6 and 280),
    check (char_length(tail_reg) between 3 and 12)
);

-- Hot-path: Listing pro Tail in einem Zeitfenster.
create index if not exists idx_aircraft_health_tail_created
    on public.aircraft_health_reports(tail_reg, created_at desc);

-- Defensive: Wenn ein Token mehrere Reports am gleichen Tag fuer den gleichen
-- Tail einreicht (Abuse-Pattern), koennen wir das spaeter im Blueprint
-- detecten via diesem Index.
create index if not exists idx_aircraft_health_token_created
    on public.aircraft_health_reports(reported_by_token, created_at desc);

-- Service-Role-Key umgeht RLS. Anon-Client bleibt geblockt (Reports kommen
-- nur via Blueprint-Endpoint mit Token, nie direkt vom Client).
alter table public.aircraft_health_reports enable row level security;

-- ╔════ 20260601_rate_limit_buckets ════╗
-- Rate-limit sliding-window buckets per (token, scope, window_sec).
-- Used by rate_limits/config.py for hard per-endpoint limits + 5/60s burst.
--
-- One row per (token, scope, window_sec). When the current epoch crosses
-- window_start_epoch + window_sec we treat the row as expired and start a
-- new window in-place (upsert on conflict).
--
-- We deliberately do not delete expired rows in the hot path -- a nightly
-- cleanup keeps the table small.

create table if not exists public.rate_limit_buckets (
    token              text   not null,
    scope              text   not null,
    window_sec         int    not null,
    window_start_epoch bigint not null,
    count              int    not null default 0,
    updated_at         timestamptz not null default now(),
    primary key (token, scope, window_sec)
);

create index if not exists idx_rl_buckets_updated
    on public.rate_limit_buckets(updated_at);

-- service-role key bypasses RLS; anon clients must never read/write this table
alter table public.rate_limit_buckets enable row level security;

-- Cleanup helper: deletes rows whose window has been expired for >2x window.
-- Schedule via pg_cron (Supabase extension) or run manually:
--   select public.rate_limit_buckets_cleanup();
create or replace function public.rate_limit_buckets_cleanup()
returns int
language plpgsql
as $$
declare
    deleted_count int;
begin
    delete from public.rate_limit_buckets
    where window_start_epoch + (window_sec * 2) < extract(epoch from now())::bigint;
    get diagnostics deleted_count = row_count;
    return deleted_count;
end;
$$;

-- pg_cron schedule (uncomment if pg_cron is enabled on the project):
--   select cron.schedule('rate_limit_buckets_cleanup', '17 3 * * *',
--                        'select public.rate_limit_buckets_cleanup();');

