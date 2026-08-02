-- Einmalige Nachrichtenanfrage vor einer Freundschaft.
--
-- Jeder erreichbare AeroX-Account darf genau eine Vorschau senden. Die
-- UNIQUE-Kante bleibt auch nach Ablehnung
-- bestehen: das Paar kann sich nicht durch einen Request in Gegenrichtung eine
-- zweite Vorschau-Nachricht schicken.
-- Erst status='accepted' autorisiert der Server den normalen DM-Kanal; es wird
-- dabei bewusst KEINE Freundschaft erzeugt.

create table if not exists public.crew_dm_requests (
    id              uuid        primary key,
    sender_token    text        not null,
    recipient_token text        not null,
    message         text        not null check (char_length(message) between 1 and 500),
    status          text        not null default 'pending'
                                check (status in ('pending', 'accepted', 'declined')),
    -- Legacy-/Auditfelder: bei allgemeinen Anfragen steht hier AEROX +
    -- Erstelldatum; die App blendet diesen internen Marker aus.
    flight_number   text        not null,
    flight_date     date        not null,
    created_at      timestamptz not null default now(),
    created_ts      numeric     not null,
    decided_at      timestamptz,
    unique (sender_token, recipient_token),
    check (sender_token <> recipient_token)
);

create index if not exists idx_crew_dm_requests_recipient_pending
    on public.crew_dm_requests(recipient_token, created_at desc)
    where status = 'pending';

create index if not exists idx_crew_dm_requests_sender
    on public.crew_dm_requests(sender_token, created_at desc);

create index if not exists idx_crew_dm_requests_accepted_pair
    on public.crew_dm_requests(sender_token, recipient_token)
    where status = 'accepted';

create unique index if not exists uq_crew_dm_requests_unordered_pair
    on public.crew_dm_requests (
        least(sender_token, recipient_token),
        greatest(sender_token, recipient_token)
    );

alter table public.crew_dm_requests enable row level security;

-- Ausschliesslich der Backend-Service-Role-Key liest/schreibt diese Tabelle.
-- Insbesondere darf der Web/PWA-Gastclient niemals rohe Account-Tokens sehen.
