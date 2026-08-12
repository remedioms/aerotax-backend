-- Der Flugbuch-Wächter kennt einen vierten Ausgang: `review` (unklarer Fall
-- wartet auf einen Menschen, KEIN Nutzer-„failed"). Die CHECK-Bedingung aus
-- 20260810_logbook_upload_status.sql erlaubt ihn nicht — PostgREST antwortete
-- mit 400, der Wächter setzte die Zeilen zurück auf `pending` und probierte
-- es alle 10 Minuten erneut (Absturz + Owner-Mail im Kreis).
do $$
begin
    alter table public.ax_logbook_upload
        drop constraint if exists ax_logbook_upload_status_check;
    alter table public.ax_logbook_upload
        add constraint ax_logbook_upload_status_check
        check (status in ('pending', 'processing', 'completed', 'failed',
                          'review'));
exception
    when duplicate_object then null;
end $$;
