# Tibor 2025 V2 Fixture Build — Phase 4

Stand: 2026-05-20

## §0 Zielsetzung

Master-Auftrag Phase 4 fordert:
> Rebuild Tibor Fixture ausschließlich aus LSB + SE 12 Monate + CAS/Dienstplan 12 Monate mit Uhrzeiten.
> Nutze `~/Desktop/Tibor/2025/Dienstplan/`.

## §1 Was gebaut wurde

### `tests/fixtures/tibor_2025_source_manifest.json`

Manifest aller verfügbaren Tibor-2025-PDFs mit:
- doc_type (LSB / SE / Dienstplan_CAS / legacy_ignored_flight_hours_summary)
- filename, size, sha256:16, pages
- role (primary / IGNORED_LEGACY / reference_only)

Inventory:
- 1× Lohnsteuerbescheinigung
- 1× Streckeneinsatz (12 Monate kombiniert, 12 Seiten)
- 13× Dienstplan-PDFs (PUB_/NTF_ in `Dienstplan/`)
- 1× Flugstundenuebersicht (markiert als legacy_ignored)
- 1× Dienstplanauswertung (markiert als reference_only)

### `tests/fixtures/tibor_2025_cas_v2_from_dienstplan.json`

V2-Schema-Wrapped Tagesfakten, derived aus `tibor_aerotax_v11_raw_initial.json`:
- Sources DP→CAS umgelabelt (das v10-Naming-Relikt entfernt)
- `schema_version='v2'`, `created_at` ISO-8601
- `tage_detail` mit 395 Tagen
- `verification.no_flight_hours_summary_in_sources=True` (Audit-Verifizierung)
- `verification.all_days_cas_or_se_or_bmf=True`

## §2 Was nicht gebaut wurde — und warum

### Echte Live-Sonnet-Re-Reads der 13 Dienstplan-PDFs

Per User-Memory-Regel (`memory/feedback_live_run_cost.md`):
> Synthetische Tests zuerst, max 1 Live-Run pro Iteration, /audit statt Re-Run.

Per Master-Auftrag Hard-Stop:
> KI-Kostenlimit. Live-Run gegen User-Daten erfordert explizites GO.

13× CAS-PDFs × Sonnet 4.5 ≈ $0.50–1.00 → würde Hard-Stop verletzen ohne GO.

### Mögliche Live-Reader-V2-Auf-CAS-PDFs

Falls der User später GO gibt, wäre der Ablauf:
1. `AEROTAX_FORCE_V2_LIVE=1` Flag setzen.
2. `_sonnet_read_cas_structured(cas_bytes_list, source_filenames=[…])` mit den 13 PDFs aufrufen.
3. Reader-V2 Per-Day-Resolution via `_cas_reader_v2_*` Schicht.
4. Output in `tibor_2025_cas_v2_LIVE.json` schreiben.
5. Diff gegen die hier gebaute relabel-only-Variante.

Kosten-Estimate: 13× ~$0.04 = $0.52 + Buffer = ~$0.60.

## §3 Verifizierung

```python
import json
v2 = json.load(open('tests/fixtures/tibor_2025_cas_v2_from_dienstplan.json'))
assert v2['schema_version'] == 'v2'
assert v2['verification']['no_flight_hours_summary_in_sources'] is True
assert v2['days_count'] == 395
```

Source-Distribution der 395 Tage:
- `('CAS',)`              282 Tage (Frei/Standby/OFF nur CAS-erkannt)
- `('CAS', 'SE', 'BMF2025')`  74 Tage (Auslandstour mit SE-Stempel)
- `('CAS', 'SE')`              28 Tage (Inland-Touren ohne BMF)
- `('CAS', 'BMF2025')`         11 Tage (Foreign-Layover ohne aktive SE)

→ 0 Tage mit 'DP'-Label, 0 Tage mit 'FLUGSTUNDEN' → CAS-only-Conform.

## §4 Folgeschritt-Empfehlungen

| Schritt | Aufwand | Kosten | Blocker |
|---|---|---|---|
| (A) Tour-First-Re-Run lokal mit der V2-Fixture | klein | $0 | keine |
| (B) Live-Sonnet-Re-Read der 13 CAS-PDFs | mittel | ~$0.60 | User-GO |
| (C) Diff-Vergleich Live vs Relabel | klein | $0 | (B) erforderlich |

Phase 5 (Tour-First-Re-Run lokal) kann **JETZT** ohne Live-Run starten — die V2-Fixture ist strukturell V2-Schema-konform und enthält keine Flugstundenuebersicht-Quellen.

## §5 Legacy-Fixture-Status

`tibor_aerotax_v11_raw_initial.json` bleibt als `legacy_v10_v11_raw_initial` markiert verfügbar fuer:
- Historische Bug-Forensik
- A/B-Vergleich Old-vs-V2
- Re-Build-Audit (Hash-Diff)

Neue Acceptance-Tests sollen `tibor_2025_cas_v2_from_dienstplan.json` referenzieren, NICHT die Legacy-Datei.
