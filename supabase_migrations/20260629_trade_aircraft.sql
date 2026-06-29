-- Trip-Trade: Aircraft-Filter (2026-06-29).
--
-- Neues optionales Feld `aircraft` (Flugzeugmuster der Tour, z.B. "A350"),
-- damit das Board nach Muster gefiltert werden kann. Additiv + idempotent —
-- bestehende Posts bleiben unberührt (aircraft = NULL).
--
-- Das Backend (trip_trade_blueprint._sb_insert_post) ist gegen eine noch
-- NICHT eingespielte Migration abgesichert: schlägt der Insert wegen der
-- unbekannten Spalte fehl, wird einmal ohne `aircraft` erneut versucht, damit
-- der Post trotzdem durabel in Supabase landet. Nach Anwenden dieser Migration
-- wird das Muster mitgespeichert.

alter table public.trade_posts
    add column if not exists aircraft text;

-- Optionaler Filter-Index (Board filtert open+not-deleted nach airline/aircraft).
create index if not exists idx_trade_posts_aircraft
    on public.trade_posts(airline, aircraft)
    where deleted = false and status = 'open';
