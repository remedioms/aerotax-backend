-- Airport-Delay-Beobachtungen: VOLLE Flug-Felder persistieren (2026-06-14).
--
-- PROBLEM: Die persistierten `airport_delay_obs`-Rows waren SPARSE — nur
-- date/airport/flight/sched/max_delay_min/cancelled/status. Damit konnte die
-- „Früher heute"-/Vergangene-Tage-Tafel zwar die Flugnummer + Verspätung zeigen,
-- aber NICHT von→nach (Ziel), Gate oder Terminal. Ein abgeflogener Flug wie
-- LH848 (FRA→Helsinki) war später nur als nackte „LH848 10:00 +30" auffindbar,
-- ohne Ziel/Gate. Diese Migration rüstet die fehlenden Spalten idempotent nach,
-- sodass der Write-Through die reichen Felder aus dem Live-Board mitschreibt und
-- die Historie von→nach + Gate/Terminal zurückgeben kann.
--
-- Alle Spalten sind NULLABLE (`add column if not exists`) → abwärtskompatibel:
-- der manuelle UPDATE→INSERT-Upsert im Backend hängt NICHT von ihnen ab, alte
-- Rows ohne diese Felder bleiben gültig (Reconstruction fällt auf '' zurück).

alter table public.airport_delay_obs
    add column if not exists dest_iata    text;
alter table public.airport_delay_obs
    add column if not exists dest_name    text;
alter table public.airport_delay_obs
    add column if not exists gate         text;
alter table public.airport_delay_obs
    add column if not exists terminal     text;
alter table public.airport_delay_obs
    add column if not exists airline      text;
-- `esti` = geschätzte (tatsächliche) Zeit als ISO-String, für „echte" Abflugzeit.
alter table public.airport_delay_obs
    add column if not exists esti         text;
