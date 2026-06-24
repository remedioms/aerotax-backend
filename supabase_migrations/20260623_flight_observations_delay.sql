-- Verspätungs-Trend pro Flug in der selbst-bauenden Flug-DB
-- (flight_profile_blueprint.py /observe). Der Radar meldet jetzt ZUSÄTZLICH zu
-- reg/type/dep/arr optional die geplante Abflugzeit + Verspätung/Status, damit
-- beim späteren Öffnen einer ALTEN Tour eine Delay-Historie existiert — auch
-- wenn an dem Tag niemand das Tafel-Board geöffnet hat.
--
-- Alle Spalten nullable + additiv (back-compat: alte Clients senden sie nicht).
-- Die eigentliche Airport-Tages-Stichprobe schreibt /observe parallel nach
-- airport_delay_obs (kanonischer Board-Pfad _delay_obs_write_through).

alter table public.flight_observations
    add column if not exists sched     text;
alter table public.flight_observations
    add column if not exists delay_min  integer;
alter table public.flight_observations
    add column if not exists status     text;
alter table public.flight_observations
    add column if not exists cancelled  boolean;
