# Flugstundenübersicht Legacy Purge — Inventory & Action Plan

Stand: 2026-05-20 · Master-Auftrag „AeroTAX Clean Release".

## §0 Beschluss

Per User-Master-Order vom 2026-05-20:
**Flugstundenübersicht ist KEIN Pflicht-, Ersatz-, Summary-, Plausi- oder
Reader-Dokument mehr.** Die drei finalen Upload-Familien sind:

1. **Lohnsteuerbescheinigung** (LSB) — Brutto/Jahres-/AG-Erstattung
2. **Streckeneinsatz-Abrechnungen** (SE) — 12 Monate, Spesen + AG-gezahlt
3. **Dienstplan / CAS** (PUB/NTF) — 12 Monate mit Uhrzeiten, Touren

Keine Flugstundenübersicht in UI, Backend, Tests, Fixtures, Prompts, Docs.

## §1 Inventory — alle Fundstellen

### App-Code (`app.py`)

| Zeile | Vorkommen | Aktiver Pfad heute? | Action |
|---|---|---|---|
| 369 | Kommentar v11-Aufgabe (`anstelle der Flugstundenübersicht (dp)`) | nein, historisch | DEPRECATE-Kommentar lassen, klarstellen |
| 385–386 | `AEROTAX_PIPELINE_VERSION` Default `v11_cas_primary` | aktiv (Default); v10 nur per ENV | KEEP (Default v11), v10-Pfad als legacy markieren |
| 2026–2031 | Upload-Audit-Log: `'dp': 'flugstunden'` | aktiv | UMBENENNEN zu `legacy_ignored_flight_hours_summary` |
| 2058–2066 | Reject wenn nur DP, kein CAS hochgeladen | aktiv | KEEP — funktional korrekt |
| 4309 | Doc-Type-Default `'flugstundenuebersicht'` in `/api/legacy/...` | dead code | DEPRECATE |
| 4370–4372 | v11-CAS-Primary-Block: `doc_type == 'dp'` rejected | aktiv | KEEP |
| 5307 | Doc-Type-Lookup-Default | dead code | DEPRECATE |
| 5936 | Word-Stem-Liste enthält `'flugstunden'` für Doc-Typ-Detection | aktiv | KEEP — markiert Flugstundenübersicht als Doc-Typ (legacy_ignored) |
| 8382 | `_parse_flugstunden_deterministic` Funktion (~150 LOC) | nur via v10_legacy | RAISE-Deprecate-Guard |
| 9220 | Counter „SE-direkte Counts (überlebt jetzt ohne Flugstundenübersicht)" | aktiv | Kommentar aktualisieren |
| 9227 | `parse_dienstplan_mit_ki` Funktion (DP-KI-Reader) | nur via v10_legacy | RAISE-Deprecate-Guard |
| 9302 | `_parse_flugstunden_deterministic` Aufruf in DP-KI-Reader | nur via v10_legacy | mit Deprecate-Guard tot |
| 11228 | Kommentar `Ersetzt in v11 die Flugstundenübersicht` | aktiv | KEEP |
| 12122 | `_sonnet_read_dp_structured` Funktion (Sonnet-DP-Reader) | nur via v10_legacy | RAISE-Deprecate-Guard |
| 12229 | DP-Prompt-Text (Sonnet) | nur via v10_legacy | mit Deprecate-Guard tot |
| 12644 | Heartbeat-Label `'Flugstundenübersicht wird gelesen…'` | nur via v10_legacy | mit Deprecate-Guard tot |
| 12667 | Heartbeat-Label `'Flugstundenübersicht wird ausgewertet (Abschnitt …)'` | nur via v10_legacy | mit Deprecate-Guard tot |
| 12771 | Fehlertext `Abschnitten der Flugstundenübersicht` | nur via v10_legacy | mit Deprecate-Guard tot |
| 17442 | Document-Health-Issue `Flugstundenübersicht konnte nicht gelesen werden` | nur via v10_legacy | mit Deprecate-Guard tot |
| 20451 | Kommentar `v10_legacy: DP-Reader (Flugstundenübersicht)` | aktiv (Branch-Switch) | KEEP-Kommentar, RAISE im else-Pfad |
| 20537 | Heartbeat `Flugstundenübersicht wird in Abschnitten ausgewertet…` | nur via v10_legacy | mit Deprecate-Guard tot |

**Aktiv-Status pro Branch**:
- `AEROTAX_PIPELINE_VERSION=v11_cas_primary` (Default in Prod): **Flugstundenübersicht-Code wird NICHT betreten** → Block-Reject im Upload, CAS-Pipeline läuft. ✓
- `AEROTAX_PIPELINE_VERSION=v10_legacy` (nur per ENV-Override): DP-Pfad würde aktiviert → wir **schließen das jetzt mit RuntimeError-Guard**.
- Auch wenn `v11_cas_primary` aber `cas_bytes` leer + `dp_bytes` da: v11-Branch wird übersprungen, fällt in `elif dp_bytes:` → das **muss** geguarded werden.

### Tests (`tests/`)

| Datei | Hits | Aktion |
|---|---:|---|
| `tests/test_calculation.py` | 47 | Großteils Anti-Flugstunden-Tests (Phase 2+ v11-Asserts). KEEP — sie verifizieren genau das, was wir wollen. Nur Helfer-Aufrufe mit `airline='LH', doc_type='flugstundenuebersicht'` (5x) UMBENENNEN auf `legacy_ignored_flight_hours_summary` oder Test-Helper anpassen, wo nötig |
| `tests/test_e2e_tibor_pipeline.py` | nutzt `tibor_aerotax_v11_raw_initial.json` | MIGRIEREN auf neue V2 Fixture |
| `tests/test_tibor_2025_golden_acceptance.py` | nutzt `tibor_aerotax_v11_raw_initial.json` | MIGRIEREN auf neue V2 Fixture |
| `tests/test_bh003a_issue_return_day_z76.py` | nutzt `tibor_aerotax_v11_raw_initial.json` | MIGRIEREN |
| `tests/test_normalized_tours_bangalore.py` | nutzt `tibor_aerotax_v11_raw_initial.json` | MIGRIEREN |
| `tests/test_reader_gap_11_days.py` | KEIN flugstunden-Hit | KEEP |
| `tests/fixtures/tibor_aerotax_v11_raw_initial.json` | 395 Tage, **alle mit `sources=['DP', …]`** → DP-derived | DEPRECATE-Marker setzen, Acceptance-Tests auf neue V2 Fixture umstellen |
| `tests/fixtures/tibor_cas_reader_v2_gap_days.json` | CAS-Reader-V2-Output | KEEP — basis für V2 Tour-First |
| `tests/fixtures/followme_golden_tibor_2025.json` | FollowMe-Referenz | KEEP — Referenz, nicht Wahrheit |

### Frontend (`/Users/miguelschumann/Desktop/site/index.html`)

| Zeile | Vorkommen | Aktion |
|---|---|---|
| 2154 | Kommentar `v11 PFLICHT: 3 Karten — LSB + SE + CAS (Flugstundenübersicht entfernt)` | KEEP — historischer Kontext |
| 2179 | Kommentar `neue 3. Pflicht-Karte (ersetzt Flugstundenübersicht)` | KEEP |
| 2770 | Kommentar `ups.cas statt ups.dp — Flugstundenübersicht ist nicht mehr Pflicht` | KEEP |
| 3362 | Kommentar `dp (Flugstundenübersicht) wird nicht mehr aktiv genutzt` | KEEP |

UI selbst zeigt keine Flugstundenübersicht-Karte mehr. ✓

### Docs (`docs/`)

| Datei | Hits | Aktion |
|---|---:|---|
| `CLAUDE.md` | 2 | UPDATE: 3-Doc-Modell finalisieren (LSB + SE + CAS), Flugstundenübersicht-Erwähnung deprecaten |
| `FILES.md` | 2 | UPDATE |
| `referenz_easa.txt` | 1 | KEEP (read-only Referenz) |
| `docs/AEROTAX_KNOWLEDGE_HARVEST.md` | 3 | KEEP (historische Harvest-Doku) |
| `docs/CAS_READER_PROMPT_V2_SPEC.md` | 1 | UPDATE: klare Trennung CAS vs Flugstunden |
| `docs/AEROTAX_CREW_CODE_GLOSSARY.md` | 2 | KEEP (historisch) |
| `docs/CAS_FOLLOWME_DISAGREEMENT_AUDIT.md` | 15 | KEEP (Audit-Trail) |
| `docs/BH_CORE_001_LIVE_RUN_PLAN.md` | 1 | UPDATE |
| `docs/aerotax_review_bundle/REVIEW_README.md` | 1 | KEEP |

## §2 Action Plan (Phase 0b)

### Pflicht-Änderungen (Code)

1. **`_parse_flugstunden_deterministic`** → `RuntimeError` mit Hint, dass v11 CAS primary läuft.
2. **`parse_dienstplan_mit_ki`** → `RuntimeError` Deprecate-Guard.
3. **`_sonnet_read_dp_structured`** und **`_sonnet_read_dp_structured_chunked_v104`** → `RuntimeError` Deprecate-Guard mit Klartext-Reason.
4. **`hybrid_analyze`** elif-Branch `elif dp_bytes:` → ersetzen durch:
   - Wenn `v11_cas_primary` aktiv UND `cas_bytes` leer → **harter Stop mit Document-Health-Issue** (kein silent Fallback auf DP).
   - Wenn `v10_legacy` ENV gesetzt → ebenfalls **harter Stop** mit Hint: „Pipeline v10_legacy ist nicht mehr unterstützt".
5. **Audit-Label** `'dp': 'flugstunden'` → `'dp': 'legacy_ignored_flight_hours_summary'`.

### Pflicht-Änderungen (Tests)

1. Test-Helper mit `doc_type='flugstundenuebersicht'` (5 Stellen in `test_calculation.py`) — umbenennen auf `legacy_ignored_flight_hours_summary`, sicherstellen dass Doc-Type-Detection diesen Wert ausspuckt.
2. Neue Tests:
   - `test_v11_no_legacy_dp_pipeline_runs_in_v11_cas_primary` — Guard wirft.
   - `test_v11_no_silent_fallback_to_dp_when_cas_missing` — Health=red statt DP.
   - `test_doc_detection_returns_legacy_ignored_for_flight_hours_pdf` — Doc-Type-Detection klassifiziert Flugstundenübersicht-Text als `legacy_ignored_flight_hours_summary`.

### Pflicht-Änderungen (Docs)

1. `CLAUDE.md` — Architektur-Grundsatz aktualisieren auf LSB + SE + CAS.
2. `FILES.md` — Pflicht-Doc-Liste.
3. `docs/CAS_READER_PROMPT_V2_SPEC.md` — refuse-on-non-CAS-Note.
4. `docs/BH_CORE_001_LIVE_RUN_PLAN.md` — Pflicht-Quellen.

### Nicht zu löschen (historischer Audit-Trail)

- `docs/AEROTAX_KNOWLEDGE_HARVEST.md`
- `docs/AEROTAX_CREW_CODE_GLOSSARY.md`
- `docs/CAS_FOLLOWME_DISAGREEMENT_AUDIT.md`
- `docs/BH_CORE_001_*` Closeout-Docs
- `tests/fixtures/tibor_aerotax_v11_raw_initial.json` — bleibt für `legacy=true`-Tests, NEUE V2-Fixture ersetzt sie für Launch-Acceptance.

## §3 Verifizierung

Nach Phase 0b müssen folgende Greps **0 Treffer in produktivem Code** liefern:

- `rg -n "Flugstundenübersicht.*lesen|Flugstundenübersicht.*ausgewertet" app.py` → 0 (Heartbeat-Texte tot)
- `rg -n "parse_dienstplan_mit_ki\(.*\)" app.py | grep -v "raise\|deprecated"` → 0 aktive Aufrufe
- `rg -n "_sonnet_read_dp_structured" app.py | grep -v "raise\|deprecated\|def "` → 0 Aufrufe

Tests müssen weiterhin **alle bestehenden Anti-Flugstunden-Asserts** grün halten:
- `test_v11_no_flugstunden_in_upload_psub` ✓
- `test_v11p5_frontend_error_message_no_flugstunden` ✓
- `test_v11p5_progress_animation_no_flugstunden` ✓
- `test_v11p6_no_user_facing_flugstunden_anywhere_critical` ✓
- `test_qa_b001_chat_picker_no_flugstunden` ✓
- `test_qa_b007_chat_intent_regex_no_flugstunden` ✓

## §4 Risiken

- **R1 Fixture-Migration-Risk**: 4 Test-Dateien hängen an `tibor_aerotax_v11_raw_initial.json`. Wenn neue V2-Fixture nicht 1:1 dieselben `tage_detail`-Shape liefert, brechen die Tests. Mitigation: V2-Fixture-Builder produziert dieselbe Shape, nur `sources=['CAS','SE',…]` statt `['DP',…]`.
- **R2 Test-Helper-Rename**: `airline='LH', doc_type='flugstundenuebersicht'` in 5 Test-Aufrufen → wenn doc_type-Detection den neuen Wert nicht erkennt, schlagen Tests fehl. Mitigation: doc_type-Detection erweitern um `legacy_ignored_flight_hours_summary` Branch.
- **R3 Roll-Back-Path**: Wenn nach Deploy ein Bug in v11_cas auftaucht → kein Rollback auf v10_legacy mehr möglich. Mitigation: Im Master-Plan steht „Kein Deploy", entsprechend kein Risiko bis Live-GO.

## §5 Definition of Done für Phase 0/0b

- [ ] FLUGSTUNDEN_LEGACY_PURGE.md geschrieben ✓ (dieses Dokument)
- [ ] `_parse_flugstunden_deterministic` deprecated mit RuntimeError
- [ ] `parse_dienstplan_mit_ki` deprecated mit RuntimeError
- [ ] `_sonnet_read_dp_structured*` deprecated mit RuntimeError
- [ ] `hybrid_analyze` elif-DP-Branch → harter Stop
- [ ] Audit-Label `legacy_ignored_flight_hours_summary`
- [ ] Test-Helper-Aufrufe umbenannt
- [ ] 3 neue Guard-Tests grün
- [ ] CLAUDE.md + FILES.md aktualisiert
- [ ] py_compile grün
- [ ] Full Regression grün (kein Regress an v11-CAS-Tests)
