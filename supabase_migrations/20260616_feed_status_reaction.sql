-- Crew-Reaktion auf eine Family-Nachricht (❤️ zurück). feed_status_blueprint.py.
alter table public.feed_statuses add column if not exists reaction text;
alter table public.feed_statuses add column if not exists reacted_at timestamptz;
