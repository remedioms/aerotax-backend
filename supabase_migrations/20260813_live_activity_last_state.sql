-- Feld-Erhaltung gegen Teil-Absender (Live-Activity, Vorfall Nr. 4 am
-- 13.08.2026: MQTT-Fanout ersetzte per APNs das komplette ContentState und
-- löschte die Dienst-Kette samt Boarding vom Sperrbildschirm).
-- Der Server merkt sich den zuletzt ERFOLGREICH gesendeten (normalisierten)
-- Zustand pro Registry-Zeile; beim nächsten Update ergänzt er Felder, die der
-- neue Absender strukturell nicht kennt (chain, Zonen, Städte, Roster-Notiz).
alter table public.live_activities
    add column if not exists last_content_state jsonb;
