-- Hotel-Sub-Ratings — Hotel-Level-Aggregation (Overall/Zimmer/Frühstück/Fitness), 2026-06-07.
--
-- PROBLEM: hotel_room_reports trägt bisher nur ROOM-LEVEL-Tipps (Zimmernummer,
-- Seite, Lärm/View/Comfort). Es fehlt eine HOTEL-WEITE Sub-Rating-Aggregation
-- der Art "LH FRA 4.7/5 · Zimmer 4.3 · Frühstück 3.8 · Fitness 4.1".
--
-- Wir erweitern die BESTEHENDE Tabelle (kein paralleles System):
--  · overall_rating   — Gesamteindruck Hotel (1-5)
--  · breakfast_rating — Frühstück (1-5)
--  · fitness_rating   — Fitness/Gym (1-5)
--  · "Zimmer" als Sub-Rating reused das bestehende comfort_rating — KEINE
--    Duplikat-Spalte. Die /summary-Aggregation mappt avg_room = avg(comfort_rating).
--
-- Alle drei Spalten nullable (alte Reports ohne diese Felder bleiben gültig).
-- Die /summary-Aggregation liefert ehrliche NULLs wenn für eine Sub-Dimension
-- keine Ratings existieren — nie ein erfundener 0-Wert.
--
-- Idempotent: add column if not exists. Die CHECK-Constraints werden via
-- DO-Block guarded angelegt (Postgres kennt kein "add constraint if not exists").

alter table public.hotel_room_reports
    add column if not exists overall_rating   int,
    add column if not exists breakfast_rating int,
    add column if not exists fitness_rating   int;

-- CHECK-Constraints idempotent nachziehen (1-5 oder null).
do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'hotel_room_reports_overall_rating_check'
    ) then
        alter table public.hotel_room_reports
            add constraint hotel_room_reports_overall_rating_check
            check (overall_rating is null or overall_rating between 1 and 5);
    end if;

    if not exists (
        select 1 from pg_constraint
        where conname = 'hotel_room_reports_breakfast_rating_check'
    ) then
        alter table public.hotel_room_reports
            add constraint hotel_room_reports_breakfast_rating_check
            check (breakfast_rating is null or breakfast_rating between 1 and 5);
    end if;

    if not exists (
        select 1 from pg_constraint
        where conname = 'hotel_room_reports_fitness_rating_check'
    ) then
        alter table public.hotel_room_reports
            add constraint hotel_room_reports_fitness_rating_check
            check (fitness_rating is null or fitness_rating between 1 and 5);
    end if;
end $$;

-- RLS bleibt wie gehabt aktiviert (siehe 20260601_hotel_rooms.sql) — Service-Role
-- (Backend) schreibt/liest, Anon-Client bleibt geblockt. Kein erneutes enable nötig.
