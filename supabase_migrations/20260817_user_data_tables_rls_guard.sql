-- Nachzug zu 6e4c881 (20260816_public_tables_rls_guard.sql): drei weitere
-- backend-eigene Tabellen aus dem Juni-Bestand hatten in ihren Migrationen
-- WEDER `enable row level security` NOCH ein `revoke`. Sie liegen im
-- public-Schema und sind damit ueber die Supabase Data API erreichbar.
--
-- `family_requests` und `feed_statuses` speichern ROHE AT-Tokens (crew_token,
-- family_token) — ein AT ist bei AeroX das Bearer-Credential, nicht nur ein Key.
--
-- PROD-BEFUND 17.08.2026 (pg_class, gelesen ueber die Management-API):
--   family_requests      relrowsecurity = t, policies = 0
--   feed_statuses        relrowsecurity = t, policies = 0
--   flight_observations  relrowsecurity = t, policies = 0
-- RLS ist live also bereits AN (deshalb liefert ein anonymer PostgREST-Read
-- 200 mit 0 Zeilen, waehrend service_role 10 / 11 / 2204 Zeilen sieht) — es
-- fliessen heute KEINE Daten ab. Aber:
--   has_table_privilege('anon', …, 'SELECT'/'INSERT'/'DELETE') = true
-- Die Grants stehen weiterhin. Damit haengt der gesamte Schutz an genau einem
-- Flag: ein spaeteres `disable row level security`, eine versehentlich
-- permissive Policy oder ein Restore ohne RLS oeffnet die Tabellen sofort
-- komplett. Bei den Tabellen aus 6e4c881 ist das nicht so — dort ist der Grant
-- weg und ein anonymer Read scheitert hart mit 42501.
--
-- Diese Migration zieht die drei Tabellen auf denselben Stand: RLS an
-- (idempotent, in Prod ein No-op) UND Grants weg. AeroX-Clients sprechen keine
-- dieser Tabellen direkt an; alle Reads/Writes laufen ueber den service_role-
-- Client des Backends, der RLS ohnehin umgeht.
--
-- NICHT ANGEWENDET. Anwendung gated der Owner mit dem Deploy.

alter table if exists public.family_requests enable row level security;
alter table if exists public.feed_statuses enable row level security;
alter table if exists public.flight_observations enable row level security;

-- `anon` and `authenticated` inherit PUBLIC privileges.  Revoke that role as
-- well so an old broad grant cannot survive this defence-in-depth migration.
revoke all on table public.family_requests from public, anon, authenticated;
revoke all on table public.feed_statuses from public, anon, authenticated;
revoke all on table public.flight_observations from public, anon, authenticated;

notify pgrst, 'reload schema';
