# CAS-Reconcile — Status: VERDRAHTET ✓ (2026-05-30)

Der deterministische CAS-Abgleich ist jetzt vollständig in die Live-Pipeline
eingehängt. Standardmäßig AUS (Flag), d.h. ohne Aktivierung ändert sich nichts.

## Was eingebaut wurde (alles in aerotax-backend)

Neue Module (standalone, getestet, gegen 12 echte CAS-PDFs validiert):
- `cas_table_parser.py`  — deterministischer PDF-Parser (Datum, Flüge, Routing, UTC-Zeiten)
- `tz_midnight.py`       — „Ort um 24:00 Ortszeit" via Flughafen-Zeitzone
- `cas_reconcile.py`     — Abgleich + 2 getrennte Steuer-Flags (VMA vs Hotel)
- `cas_integration.py`   — eine flag-gated Brücke `reconcile_cas_days()`
- `airport_tz.py`        — 11.029 Flughäfen Land+Zeitzone

Verdrahtung in `app.py` (Funktion hybrid_analyze):
- **Schritt A** (~Z. 25700): CAS-Bytes-Kopie `cas_bytes_for_reconcile` gesichert,
  BEVOR der Speicher freigegeben wird. (Eine vorherige, abgebrochene Bearbeitung
  hatte hier kaputten Code hinterlassen — bereinigt.)
- **Schritt B** (~Z. 25936): direkt vor `_adapt_cas_reader_to_builder` ruft die
  Pipeline `reconcile_cas_days(cas_bytes_for_reconcile, _cas_days_raw, homebase)`.
  Korrigiert Flugnummern/Routing/overnight aus den harten PDF-Fakten. Defensiv:
  Flag aus / fremdes Layout / jeder Fehler → Roh-Tage unverändert.
- **Schritt C** (`normalized_tours.py`, Hotel-Logik): respektiert `tz_hotel_night`.
  Ist es zeitbasiert False (im Flug / Nachtflug-Heimkehr), wird KEINE Hotelnacht
  gezählt — verhindert Phantom-Hotelnächte. Konservativ: nur unterdrücken, nie erfinden.

Status: alle Module parsen, **Test-Suite 2662 passed (kein Regress)**, Flag default off.

## WAS DU JETZT TUN MUSST (ich kann das nicht — braucht echten Upload + Keys)

### 1. Lokaler Smoke-Test (Flag an), gleicher CAS 3×
```bash
cd ~/Desktop/aerotax-backend
export AEROTAX_USE_NORMALIZED_TOURS=1      # normalized_tours muss aktiv sein
export AEROTAX_USE_CAS_RECONCILE=1         # NEU: deterministischen Abgleich an
export ANTHROPIC_API_KEY=...               # dein Key
# Server starten (wie du es normal machst), z.B.:
python3 app.py        # oder gunicorn ...
```
Dann über die normale Weboberfläche (oder dein Test-Skript) **denselben CAS
dreimal** auswerten.

**Erwartung (das ist der eigentliche Beweis):**
- Im Log erscheint `[cas-reconcile] N Korrekturen, det_only=[...]`
- Die Kennzahlen (Z76 €, Fahrtage, Hotelnächte) sind über die **3 Läufe IDENTISCH**.
  Vorher schwankten Fahrtage 41–55 — das war der „ZeroDay"-Bug.

### 2. Gegenprobe (Flag aus)
```bash
unset AEROTAX_USE_CAS_RECONCILE
```
Gleicher CAS → Verhalten exakt wie heute (Sicherheits-Check, dass nichts kaputt ist).

### 3. Wenn die Zahlen plausibel + stabil sind: deployen
- Die neuen `.py`-Dateien + `app.py`/`normalized_tours.py` committen & pushen.
- Auf Cloud Run die Env-Variable `AEROTAX_USE_CAS_RECONCILE=1` setzen.
- Empfehlung: erst eine Weile mitlaufen lassen und die `[cas-reconcile]`-Logs +
  `audit_notes` beobachten (welche Korrekturen, wie oft), bevor es „die Wahrheit" wird.

## Falls etwas nicht stimmt
- Flag sofort wieder aus (`unset` / Env entfernen) → alter Zustand, kein Risiko.
- `reconcile_cas_days` schluckt jeden Fehler und gibt die Eingabe unverändert
  zurück — ein Bug im Abgleich kann die Auswertung nicht crashen.
- Prüf, ob `cas_pre_read` im V2-Format ist (sonst sieht reconcile keine `flight_numbers`).
