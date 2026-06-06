-- Home-Address-Persistence (Commute-Distance / Smart-Pickup, 2026-06-06).
--
-- Die Home-Adresse der Crew lebte bisher nur on-device. Für Commute-Distance
-- (Entfernung Wohnort→Homebase) und Smart-Pickup muss sie serverseitig
-- persistiert werden — bei Re-Install/Device-Wechsel ging sie sonst verloren.
--
-- Top-Level-Spalten statt metadata-jsonb, weil _PROFILE_KNOWN_COLS sie als
-- echte Felder spiegelt (siehe app.py _PROFILE_KNOWN_COLS). `home_geocoded`
-- markiert ob lat/lon vom Client bereits aufgelöst wurden.

alter table public.user_profiles
    add column if not exists home_address        text,
    add column if not exists home_latitude       double precision,
    add column if not exists home_longitude      double precision,
    add column if not exists home_transport_mode text,
    add column if not exists home_geocoded       boolean default false;
