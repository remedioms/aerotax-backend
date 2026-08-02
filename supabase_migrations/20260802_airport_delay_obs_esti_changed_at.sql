-- airport_delay_obs.esti_changed_at — WANN hat sich die Ankunfts-/Abflug-
-- SCHÄTZUNG dieser Board-Zeile zuletzt GEÄNDERT?
--
-- WARUM (Owner-Befund 02.08.2026, Kalender FRA→CAI 26.07. „Ist 10:40–15:43",
-- Ankunft = exakter Planwert): Die Physik-Hürde in `_enrich_leg_delays`
-- („eine Landung kann nicht VOR der Landung aufgezeichnet worden sein")
-- verglich bisher `updated_at` gegen die behauptete Ankunftszeit. `updated_at`
-- rückt aber bei JEDEM Repoll vor — auch wenn die Tafel exakt dasselbe sagt
-- wie vor zehn Minuten. Eine eingefrorene PROGNOSE sah damit dauerhaft
-- „frisch geschrieben" aus und galt als Messung.
--
-- Der Write-on-Change-Schutz im Poller (`poll_scheduler._OBS_HASH_MEMO`) hätte
-- das theoretisch verhindert — er ist aber IN-PROCESS: auf Hetzner laufen drei
-- Gunicorn-Worker mit je eigenem Memo, die Worker werden recycelt, und der
-- Playwright-Scraper auf der NAS schreibt als völlig separater Client über
-- PostgREST. Jeder von ihnen kennt nur seine eigene Historie.
--
-- DESHALB TRIGGER STATT CLIENT-LOGIK: Der Stempel entsteht IN der Datenbank,
-- an der einzigen Stelle, die alle Schreiber gemeinsam passieren — die drei
-- Backend-Worker, der NAS-euscraper, der FR24-Ankunfts-Backfill und jeder
-- künftige Schreiber. Kein Client kann ihn vergessen oder fälschen.
--
-- BEDEUTUNG DES WERTS: `esti_changed_at` ist der Zeitpunkt, zu dem DIESE
-- Uhrzeit zum ersten Mal so in der Zeile stand. Liegt er NACH der behaupteten
-- Ankunft, kann jemand die Landung gesehen haben → Messung. Liegt er davor,
-- ist der Wert eine Vorhersage — dann wird eskaliert (FR24), statt eine nie
-- stattgefundene Uhrzeit als „Ist" auszuweisen (Owner-Regel „lieber keine
-- Zeile als ein synthetisierter Wert").
--
-- ALTBESTAND: Zeilen von vor dieser Migration bekommen KEINEN Stempel
-- (bewusst NULL statt now() — ein rückdatierter Stempel wäre ein erfundener
-- Wert). Der Lesepfad fällt für sie auf das bisherige `updated_at`-Verhalten
-- zurück; mit wachsendem Trigger-Bestand läuft dieser Fallback von selbst aus.
--
-- Rein additiv: kein bestehender Schreiber muss etwas ändern, keine Spalte
-- verschwindet, kein Default verändert bestehende Zeilen.

alter table public.airport_delay_obs
  add column if not exists esti_changed_at timestamptz;

create or replace function public.airport_delay_obs_stamp_esti_changed()
returns trigger
language plpgsql
as $$
begin
    if tg_op = 'INSERT' then
        -- Neue Zeile MIT Schätzung: der Wert steht ab jetzt da.
        -- Ohne Schätzung: nichts zu stempeln (bleibt NULL).
        if new.esti is not null then
            new.esti_changed_at := now();
        end if;
    else
        -- `is distinct from` behandelt NULL korrekt (NULL→Wert und Wert→NULL
        -- sind beides Änderungen); ein reiner Repoll mit identischer Schätzung
        -- erbt den alten Stempel und altert damit ehrlich weiter.
        if new.esti is distinct from old.esti then
            new.esti_changed_at := now();
        else
            new.esti_changed_at := old.esti_changed_at;
        end if;
    end if;
    return new;
end;
$$;

drop trigger if exists trg_airport_delay_obs_esti_changed on public.airport_delay_obs;

create trigger trg_airport_delay_obs_esti_changed
    before insert or update on public.airport_delay_obs
    for each row
    execute function public.airport_delay_obs_stamp_esti_changed();

-- PostgREST cached das Schema. Ohne dieses NOTIFY liefert die REST-API die
-- neue Spalte nicht aus und ein `select(...esti_changed_at)` scheitert mit
-- „column does not exist" (teuer gelernt: fcm_token-Spalte, 01.08.2026).
notify pgrst, 'reload schema';
