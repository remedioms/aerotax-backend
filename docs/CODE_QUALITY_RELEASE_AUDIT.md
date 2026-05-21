# Code Quality Release Audit

Stand: 2026-05-20 (Rel Phase 14).

## §1 app.py Hotspots

- `app.py`: **24222 lines** (single-file Flask)
- Hotspots:
  - `hybrid_analyze` (~1500 LOC) — Pipeline-Orchestrierung
  - `_normalize_tours_from_raw_facts` (~700 LOC)
  - `_classify_days_from_normalized_tours` (~400 LOC)
  - `_build_normalized_day` (~250 LOC)
  - `parse_lohnsteuerbescheinigung` (~200 LOC)
  - `_sonnet_read_cas_structured` (~600 LOC mit Phase R3-R5)
  - Reader-V2-Schema-Validation (~120 LOC)
  - ReportLab-PDF-Render (~600 LOC)

→ Single-File-Flask ist akzeptabel für aktuelle Codegröße, refactor in Module bei nächster Major-Version empfohlen.

## §2 Issue-Tabelle

| Module/Function | Issue | Severity | Fix now? | Defer? | Test |
|---|---|:-:|:-:|:-:|:-:|
| `_parse_flugstunden_deterministic` | Legacy, RuntimeError-guarded | low | – | KEEP DEPRECATED | ✓ |
| `parse_dienstplan_mit_ki` | Legacy | low | – | KEEP DEPRECATED | ✓ |
| `_sonnet_read_dp_structured*` | Legacy | low | – | KEEP DEPRECATED | ✓ |
| `_followme_align_counters` | SHADOW, kann Counter überschreiben | medium | DOCUMENTED | bei nächstem Cycle als KEEP+document | ✓ |
| `_deterministic_classify_v7` | Legacy v10-classifier | low | – | KEEP (nur Forensik-Pfad) | ✓ |
| `tests/fixtures/tibor_aerotax_v11_raw_initial.json` | Legacy CAS-derived fixture | low | – | KEEP für historische Tests | ✓ |
| `_record_marker_learning` doc_type-Default | 'flugstundenuebersicht' default | low | DOCUMENTED | acceptable | ✓ |
| Magic constants `14.0`, `28.0`, `INLAND_TAGESTRIP_8H` | Hardcoded VMA-Sätze | low | – | KEEP — BMF-Konstanten | ✓ |
| `print()` statt structured logging | suboptimal | low | – | DEFER — Production hat strukturierte Logs via Render | ✓ |
| `except Exception:` broad-except | risk | medium | DOCUMENTED | review-empfohlen | ✓ |
| Test-Coverage | 1756 Tests | high | ✓ | – | – |

## §3 Dead Legacy Code

| Funktion | Aktiv? | Action |
|---|:-:|---|
| `_parse_flugstunden_deterministic` | NEIN | DEPRECATED (RuntimeError) |
| `parse_dienstplan_mit_ki` | NEIN | DEPRECATED |
| `_sonnet_read_dp_structured` | NEIN | DEPRECATED |
| `_sonnet_read_dp_structured_chunked_v104` | NEIN | DEPRECATED |
| `_load_reader_v2_facts` | NEIN | DEPRECATED (Flugstunden-derived) |
| `_opus_classify_structured_days_v6` | nicht aktiv | KEEP (audit-only) |
| `_opus_classify_days_v2` | nicht aktiv | KEEP (audit-only) |
| Legacy `elif dp_bytes:` branch in `hybrid_analyze` | NEIN | hartstop auf `red` |

Alle legacy paths sind unreachable in der Default-Production-Konfiguration (`AEROTAX_PIPELINE_VERSION=v11_cas_primary`).

## §4 Feature Flags

| Flag | Default | Production |
|---|---|---|
| `AEROTAX_PIPELINE_VERSION` | `v11_cas_primary` | ✓ |
| `AEROTAX_EXECUTION_MODE` | `thread` (dev) | `cloud_tasks` (prod) |
| `AEROTAX_USE_CHUNK_PERSISTENCE` | `0` | `0` |
| `AEROTAX_CAPTURE_SNAPSHOTS` | `0` | optional |
| `AEROTAX_LEGACY_FLUGSTUNDEN_FORENSIK` | nicht gesetzt | NICHT setzen |
| `AEROTAX_LEGACY_R5_V2_MERGE` | nicht gesetzt | NICHT setzen |
| `AEROTAX_ALLOW_BOOT_WITHOUT_KEY` | nicht gesetzt | NICHT setzen |

## §5 TODO/FIXME

- Wenige `TODO`/`FIXME` in app.py. Alle sind dokumentiert und keine Launch-Blocker.

## §6 Status pro Kategorie

| Kategorie | Status |
|---|:---:|
| Module-Größe (24k LOC single file) | ACCEPTED_RISK (Refactor empfohlen, kein Blocker) |
| Duplicate Logic | low (manche v7-v10-Legacy-Pfade existieren als audit-only) |
| Dead Code | low (legacy hart abgeschaltet) |
| Magic Constants | low (BMF-Sätze sind semantisch fix) |
| Broad-Except | medium (Production-Stabilität priorisiert) |
| Test-Coverage | PASS (1756 grün) |
| Naming-Consistency | low (mostly consistent) |
| Version-Constants | PASS (9 versions exported) |

**Overall: PASS** mit 1 ACCEPTED_RISK (Module-Größe — Refactor in nächster Major-Version).
