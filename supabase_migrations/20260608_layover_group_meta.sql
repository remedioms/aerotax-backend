-- Layover-Group-Meta: geteilter Layover-Plan + Meetup-Umfragen + Pin pro Gruppe.
-- Bisher hielt die iOS-App das nur lokal pro Gerat (LayoverGroupMetaStore).
-- Jetzt server-seitig, damit alle Mitglieder denselben Plan + dieselben Umfragen
-- sehen. Muster wie dm_messages / wall_posts: text-PK, jsonb-Blobs, numeric ts,
-- Service-Role-Key umgeht RLS (Anon-Client bleibt geblockt).
--
--   plan        = {place, hotelName, meetSpot, dateText, notes, updatedAt}
--   polls       = [{id, question, options:[{id,label,voter_tokens:[token]}],
--                   createdAt, closed}]
--   pinned_note = angepinnter Hinweis oben im Chat-Header
--   updated_at  = epoch seconds (Last-Write-Wins fur plan/pin)

create table if not exists public.layover_group_meta (
    group_id    text        primary key,
    plan        jsonb       not null default '{}',
    polls       jsonb       not null default '[]',
    pinned_note text        not null default '',
    updated_at  numeric     not null default 0,
    metadata    jsonb       not null default '{}'
);

alter table public.layover_group_meta enable row level security;
