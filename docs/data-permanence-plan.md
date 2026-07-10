# Daten-Permanenz-Plan — eigene Supabase als einzige dauerhafte Wahrheit

**Stand: 2026-07-10 · Task 15 (Owner-Zielbild)**

> Owner-Ziel: **JEDER Datenpunkt, den wir je gesehen haben, landet dauerhaft in der
> eigenen Supabase.** Externe APIs (FR24 paid, adsbdb, planespotters, AviationStack,
> Boards) sind nur **Erstbefüllung** — der zweite Blick auf denselben Fakt ist immer
> gratis aus der eigenen DB. Doktrin: free-first, zero-double-spend, kein
> Geister-Flieger, confirmed-or-hidden.

Dieses Dokument ist **nur Plan + DDL-Skizze** — keine Migration wurde angewendet.

---

## (a) Ist-Inventar der Tabellen

| Tabelle | Key | Schreiber | Wachstum / Retention | Bewertung |
|---|---|---|---|---|
| `flights` | (flight, service_date)-artig, Spalten u.a. `op_flight_no, origin, destination, gate, terminal, status, tail, hex, sched_dep, est_dep, service_date, updated_at` (Read: `aerox_data_blueprint.py:264-277`) | flight-warehouse Node-Harvester (NAS, out-of-repo) | wächst pro getafeltem Abflug; keine Retention bekannt | DEP-zentrisch — **Ankunfts-Fakten landen hier nie** (siehe d.1) |
| `airport_delay_obs` | (airport, flight, date), ARR-Rows als `<Ziel>#ARR` (`aerox_data_blueprint.py:2974, 3104-3130`; `eu_scraper/supabase_writer.py`) | poll-boards + eu_scraper (Hetzner-Cron), `_crowdsource_flight_obs` (app.py:30779 — bezahlte Treffer werden zurückgeschrieben), adsb-IST-Zeit-Rows (`adsb_blueprint.py:3132`) | wächst pro Board-Row/Tag; keine Retention | **wichtigste Roh-Wahrheit**, aber nur Rohzeilen — kein kanonisches Leg |
| `aircraft_live` | `reg` (PK, 1 Row je Airframe, upsert) — `nas_harvester/schema.sql` | NAS FR24-gRPC-Harvester (`nas_harvester/ingest.py`) | **konstant** (Snapshot, kein Wachstum) | ok — Last-Known-Position by design flüchtig, Historie liegt in `aircraft_track` |
| `aircraft_track` | PK `(reg, seen_ts)`; Spalten `flight, origin, dest, lat, lon, alt_ft, gs_kt, track_deg, on_ground, source` (Write-back `aerox_data_blueprint.py:3756-3783`) | NAS-Harvester-Breadcrumbs (`ingest.py:115-129`, Gates 1 nm + 120 s, alle Carrier) + FR24-Trail-Rückschreibung (Tier 2) | **~1 Mio Rows/Tag** (Kommentar `ingest.py:124`); Retention **10 Tage** via `/api/internal/track-prune` (`aerox_data_blueprint.py:3953-4004`, `TRACK_RETENTION_DAYS=10`, Hetzner-Cron) | **einzige Stelle, an der wir Daten ENDGÜLTIG LÖSCHEN** → verletzt das Permanenz-Ziel; siehe (c) Verdichtung |
| `ax_route_cache` | `flight` = `CS@YYYYMMDD` / nackter CS / `REG:<reg>@YYYYMMDD` (`_record_resolved_route`, `aerox_data_blueprint.py:936-959`) | jeder erfolgreiche Route-Resolve (obs/warehouse/gRPC/FR24/ADB, `warehouse_reader.py:459-539`) + Hex-State-Machine-Harvest (gratis Legs, `aerox_data_blueprint.py:1695`) | wächst pro aufgelöster (flight,date); keine Retention | gut (permanent), ABER: `_cache_get` (`:96-108`) liest **nie `updated_at`** → nackter CS-Key gilt „für immer" (Sweep-Klasse B) |
| `ax_aircraft_cache` | `hex` (`aerox_data_blueprint.py:2499/2512`) | erster adsbdb/hexdb-Treffer je Hex | wächst pro neuem Airframe (~konstant, Flotte endlich) | permanent ✓; keine Staleness (Umregistrierung) + kein Negativ-Cache |
| dazu: `ax_photo_cache` (`:2628-2637`), `ax_schedule_cache` (mit eigener `_fetched`-Alterslogik, `:4527-4537`), `ax_api_budget` (KV, Budget + Prewarm-Watermarks) | | | | Foto-Links permanent ✓ (nur URL, kein Bild) |

**Größen messen statt schätzen** (einmalig im Supabase-SQL-Editor, Zahlen unten in (e) eintragen):

```sql
select relname, pg_size_pretty(pg_total_relation_size(c.oid)) as total,
       coalesce(s.n_live_tup, 0) as rows
from pg_class c
join pg_namespace n on n.oid = c.relnamespace and n.nspname = 'public'
left join pg_stat_user_tables s on s.relid = c.oid
where relname in ('flights','airport_delay_obs','aircraft_live','aircraft_track',
                  'ax_route_cache','ax_aircraft_cache','ax_photo_cache','ax_schedule_cache')
order by pg_total_relation_size(c.oid) desc;
```

---

## (b) Ziel: kanonische `legs`-Sicht pro (flight, date)

Heute wird das „best-known Leg" bei **jedem Request neu zusammengerechnet**
(`_flight_facts_from_obs` merged DEP+`#ARR` on-the-fly, `aerox_data_blueprint.py:3104`;
`resolve_unified_flight` kaskadiert free→paid). Ziel: das Ergebnis dieser Merges wird
**materialisiert** — EIN Datensatz je (flight, date) mit best-known Fakten + Quelle +
Konfidenz. Externe APIs füllen ihn nur beim ersten Mal.

```sql
-- DDL-SKIZZE (nicht angewendet)
create table if not exists public.legs (
    flight        text not null,            -- IATA/OP-Flugnr normalisiert (LH1558)
    service_date  date not null,            -- Betriebstag station-lokal (Abflug)
    origin        text, dest text,
    sched_dep     timestamptz, est_dep timestamptz, act_dep timestamptz,
    sched_arr     timestamptz, est_arr timestamptz, act_arr timestamptz,
    gate_dep      text, terminal_dep text, gate_arr text,
    status        text, cancelled boolean default false,
    tail          text, hex text, ac_type text,
    delay_dep_min integer, delay_arr_min integer,
    -- Herkunft/Vertrauen PRO FAKT-GRUPPE (nicht nur pro Row):
    src_times     text,   -- 'board_obs' | 'warehouse' | 'fr24' | 'adsb_selfcomputed' | 'schedule'
    src_route     text,
    src_tail      text,
    confidence    smallint default 0,       -- 3=Board-IST, 2=Board-Soll/FR24, 1=inferiert
    updated_at    timestamptz default now(),
    primary key (flight, service_date)
);
create index if not exists idx_legs_tail_date on public.legs (tail, service_date);
create index if not exists idx_legs_date      on public.legs (service_date);
```

Quellen-Rangfolge (höher gewinnt, gleiche Quelle: frischer gewinnt):
`board_obs`-IST (airport_delay_obs, beobachtet) → `warehouse` (`flights`) →
`fr24` (bezahlt, gespiegelt) → `ax_route_cache`@date → Inferenz (nie „confirmed").

Migrationspfad (jeweils klein, einzeln shipbar):

1. **Phase 0 — Read-only-View:** SQL-View `legs_v` über `airport_delay_obs`
   (DEP + `#ARR`-Merge in SQL) + `flights`. Kein Code-Change, nur Validierung
   gegen `_flight_facts_from_obs`-Output.
2. **Phase 1 — Write-Through:** `_flight_facts_from_obs` und
   `_crowdsource_flight_obs` upserten ihr gemergtes Ergebnis zusätzlich nach
   `legs` (best-effort, nie werfen — Muster `_record_resolved_route`).
3. **Phase 2 — legs-first Reads:** resolve-flight/uflight/flight-detail/route-history
   lesen zuerst `legs`; nur bei Miss die heutige Kaskade (die dann Phase-1-Write-Through
   triggert). Ab hier gilt: 1 externer Call je (flight,date), alle weiteren gratis.

---

## (c) Track-VERDICHTUNG statt Prune-Löschung

Heute **löscht** `/api/internal/track-prune` Breadcrumbs > 10 Tage endgültig
(`aerox_data_blueprint.py:3953-4004`). Ziel: vor dem Prune wird jeder Flug zu einer
komprimierten Polyline verdichtet (Douglas-Peucker, ~50–100 Punkte) und dauerhaft
archiviert — die geflogene Route geht nie mehr verloren, das Volumen sinkt ~10–20×.

```sql
-- DDL-SKIZZE (nicht angewendet)
create table if not exists public.flight_tracks_archive (
    reg        text not null,               -- normalisiert wie aircraft_track
    first_ts   timestamptz not null,        -- erster Roh-Punkt des Legs
    last_ts    timestamptz not null,
    leg_date   date not null,               -- UTC-Tag von first_ts
    flight     text, origin text, dest text,
    n_raw      integer,                     -- Roh-Breadcrumbs vor Verdichtung
    n_points   integer,                     -- Punkte nach Douglas-Peucker
    points     jsonb not null,              -- [[epoch,lat,lon,alt_ft,gs_kt], …] — kompaktes Array,
                                            -- lat/lon auf 4 Dezimalen (~11 m) gerundet
    source     text default 'aircraft_track',
    created_at timestamptz default now(),
    primary key (reg, first_ts)             -- idempotent: Re-Run überschreibt, dupliziert nie
);
create index if not exists idx_fta_flight_date on public.flight_tracks_archive (flight, leg_date);
create index if not exists idx_fta_leg_date    on public.flight_tracks_archive (leg_date);
```

**Verdichtungs-Algorithmus** (neuer interner Endpoint, gleiche Schutz-/Batch-Muster
wie `ax_track_prune`):

1. Fenster: Rows mit `seen_ts` im Tag `heute − ARCHIVE_AFTER_DAYS` (Default 8) —
   also **vor** dem Prune-Cutoff (10 d), damit nie ungesichert gelöscht wird.
2. Gruppieren nach `reg`, **Leg-Split** bei Lücke > 45 min oder `on_ground=true`
   (gleiches Muster wie die Hex-State-Machine für Leg-Erkennung).
3. Pro Leg Douglas-Peucker auf (lat, lon): Epsilon adaptiv verdoppeln bis
   `n_points <= 100`; Start bei ~0.005° (~500 m). Höhe/Speed nur an den
   behaltenen Punkten mitnehmen (Profil bleibt erkennbar).
4. Upsert nach `flight_tracks_archive` (PK `(reg, first_ts)`, `on_conflict` →
   idempotent, Re-Runs safe).
5. Erst wenn der Archiv-Lauf für einen Tag `ok` war, darf `track-prune` diesen
   Tag löschen (einfachste Kopplung: `ARCHIVE_AFTER_DAYS < TRACK_RETENTION_DAYS`
   und Cron-Reihenfolge archive → prune; robuster: Watermark-Key
   `track_archived_until` im `ax_api_budget`-KV, den prune als Obergrenze liest —
   gleiches Muster wie `_fr24_prewarm_mark_get`, `aerox_data_blueprint.py:4007-4011`).

**Migrations-/Cron-Plan:**

| Schritt | Was | Wo |
|---|---|---|
| M1 | DDL oben anwenden (Supabase SQL-Editor oder DATABASE_URL wie bei `aircraft_live`-DDL) | Owner/autonom |
| M2 | Endpoint `POST /api/internal/track-archive` (X-Poll-Secret == `ADSB_POLL_SECRET`, 403 ohne Secret — exakt das `ax_track_prune`-Muster `:3960-3965`; Batch-Limit + Zeit-Scheiben wie dort) | `blueprints/aerox_data_blueprint.py` |
| M3 | Hetzner `poll-tick.sh`: täglich `track-archive` VOR dem bestehenden `track-prune`-Aufruf einhängen | `/opt/aerox/poll-tick.sh` |
| M4 | Backfill: einmalig `track-archive` über die aktuell vorhandenen 10 Tage laufen lassen (Tages-Loop), erst danach M3 scharf | einmalig |
| M5 | `/api/ax/flown-track` Tier 1b: bei Miss in `aircraft_track` (Leg älter als Retention) aus `flight_tracks_archive` lesen — Tier 2 (FR24 paid) rückt eine Stufe nach hinten | `aerox_data_blueprint.py` (ax_flown_track, `:3786ff`) |

**Volumen-Wirkung:** 1 M Roh-Rows/Tag ≈ 25–50 k Legs/Tag (20–40 Punkte/Leg beim
2-min-Raster). Archiv-Row mit ≤100-Punkte-JSONB ≈ 1.5–4 KB → **~50–150 MB/Tag → nach
Verdichtung ~1.5–4.5 GB/Jahr**, statt unbegrenzt wachsender Roh-Punkte (die Roh-Tabelle
bleibt bei ~10 M Rows Steady-State). Wird das zu viel: `points` als
polyline-encoded `text` (~0.5–1 KB/Leg) oder Zweitstufen-Retention im Archiv
(Nicht-LH-Group nach 1 Jahr auf 30 Punkte eindampfen).

---

## (d) Pfade, die heute NICHT zurückschreiben (Code-belegt)

1. **Gemergte Leg-Fakten werden nie materialisiert.** `_flight_facts_from_obs`
   (`aerox_data_blueprint.py:3104-3130`) merged DEP+`<arr>#ARR` bei JEDEM Request neu;
   das Ergebnis (inkl. Ankunft/Gate) landet weder in `flights` noch in einer
   legs-Tabelle. Genau deshalb „landete die Ankunft nie in flights" (Unified-Flight-
   Befund) → Abschnitt (b).
2. **FR24-Paid-Hard-Cache ist nur In-Memory.** `_FR24_REG_CACHE`
   (`aerox_data_blueprint.py:1240-1257`, TTL 6 h) lebt **pro Gunicorn-Prozess** (×3
   Worker auf Hetzner) und stirbt bei jedem Deploy/Restart. Die Helper
   `_fr24_flights_by_reg`/`_fr24_flight_by_number` persistieren selbst NICHTS — nur
   manche Caller spiegeln via `_crowdsource_flight_obs` (tail-history own=1 `:3658-3676`,
   resolve-flight `:4183-4190` ✓). Wer den Helper ohne Spiegelung ruft, verbrennt
   Credits ohne dauerhaften Gegenwert. → Regel: **Spiegelung in den Helper ziehen**,
   nicht dem Caller überlassen.
3. **METAR wird verworfen.** `_METAR_CACHE` 10 min in-process
   (`aerox_data_blueprint.py:4446-4456`); keine Wetter-Historie in Supabase (wäre für
   Pünktlichkeits-/Delay-Korrelation wertvoll). Negativ-Ergebnis wird gar nicht gecacht.
4. **NOTAM-Endpoint persistiert nichts** — nicht mal den Fehler (app.py:12638-12654,
   jeder Request = neuer 10-s-Upstream-Call; Sweep J2).
5. **Negativ-Ergebnisse fehlen systematisch:** `ax_photo` 404 (`:2633`), `ax_photo_reg`,
   unbekannter Hex in `ax_aircraft` (`:2505ff`) — jeder Miss trifft die externe Quelle
   erneut (Sweep J2). Fix-Muster existiert im selben File (`_fr24_flights_by_reg`
   Negativ-Cache `:1295-1297`) — nur eben in Supabase (`{'neg': true}`-Payload, kurzer
   TTL via `updated_at`) statt in-memory.
6. **ADS-B-Mirror-Positionen (adsb.lol/adsb.fi) sind flüchtig.** In `aircraft_track`
   schreiben nur der NAS-FR24-Harvester (`ingest.py:288ff`) und die FR24-Trail-
   Rückschreibung (`:3756`) — Positionen, die Radar/Watch über die adsb-Mirrors sehen,
   werden nicht angehängt. (Bewusste Abwägung: Volumen; aber es ist eine Lücke im
   „jeder Datenpunkt"-Ziel — Kandidat: nur eigene/Watch-Regs anhängen.)
7. **Staleness-blinder Cache-Layer:** `_cache_get` (`:96-108`) selektiert nur `payload`,
   nie `updated_at` → permanent gespeicherte Daten können nicht von veralteten
   unterschieden werden (nackter `ax_route_cache`-Key „für immer", `:523`;
   `ax_aircraft_cache` Umregistrierung). Permanenz braucht das Gegenstück:
   **Alter sichtbar machen**, nicht löschen.

---

## (e) Größen-Budget (Supabase Pro)

Supabase Pro: **8 GB Datenbank inklusive**, danach ~0.125 $/GB/Monat (Disk wächst
automatisch; IO/Compute unabhängig davon). Budget-Rechnung (Schätzwerte — mit der
SQL-Messung aus (a) verifizieren und hier eintragen):

| Posten | Steady-State / Wachstum | Budget |
|---|---|---|
| `aircraft_track` (Roh, 10 d Retention) | ~10 M Rows × ~200 B (Row+PK-Index) | **~2–2.5 GB konstant** |
| `flight_tracks_archive` (neu, permanent) | ~50–150 MB/Tag Roh-JSONB → nach DP-Verdichtung | **~1.5–4.5 GB/Jahr** (mit encoded-polyline: ~0.5–1.5 GB/Jahr) |
| `airport_delay_obs` (permanent) | Board-Rows aller geharvesteten Airports | messen; grob 100–300 MB/Jahr — unkritisch |
| `flights` (permanent) | 1 Row/getafeltem Flug | messen; gleiche Größenordnung |
| `legs` (neu, permanent) | 1 Row je (flight,date), schmal | < 100 MB/Jahr |
| `aircraft_live` | 1 Row/Airframe | vernachlässigbar (konstant) |
| `ax_*_cache` | Flotte/Routen endlich | vernachlässigbar |

**Fazit:** Mit Track-Verdichtung passt das Gesamtziel für **> 1 Jahr in die 8-GB-
Inklusivgrenze**; danach kostet jedes weitere Jahr Archiv grob 2–6 $/Jahr zusätzlichen
Disk-Preis — kein Grund, je wieder Daten zu löschen. Wachstums-Wache: die Mess-Query
aus (a) monatlich laufen lassen (monitor.sh-Anhang oder manuell); Alarmschwelle 6 GB.

---

## Offene Entscheidungen (Owner)

1. `points`-Format im Archiv: JSONB-Array (einfach, direkt querybar) vs.
   polyline-encoded Text (~4× kleiner) — Vorschlag: JSONB starten, bei > 4 GB umcodieren.
2. ADS-B-Mirror-Positionen für Watch-Regs zusätzlich in `aircraft_track` anhängen (d.6)?
3. METAR-Historie (d.3) als eigene kleine Tabelle (`icao, obs_ts, raw, flight_category`)
   — winzig, aber neuer Datenstrom: nur wenn ein Feature ihn braucht.
