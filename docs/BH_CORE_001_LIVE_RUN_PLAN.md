# BH-CORE-001 Phase 7 — Controlled Live-Run Plan

Stand: 2026-05-20 (Rel Phase 21 Final-Update). **STATUS: PLAN. NICHT AUSGEFÜHRT. Wartet auf User-Freigabe.**

## §-1 v11 Clean-Release Plan-Update

Per Master „CAS ist Pflicht-Doc, Flugstundenübersicht raus" und MegaR + Release-Validation:

**Files für Live-Run**:
- 1× Lohnsteuerbescheinigung (LSB)
- 1× Streckeneinsatz-Abrechnung (SE — 12 Monate kombiniert OR Einzelfiles)
- N× Dienstplan/CAS (PUB_/NTF_) — alle verfügbaren Monate
- KEINE Flugstundenübersicht-PDF im Upload-Set

**Website-Eingaben**:
- Vorname/Nachname
- Homebase (z.B. „Frankfurt (FRA)" oder „München (MUC)")
- Steuerjahr (2024/2025/2026)
- km-Entfernung Wohnung→Homebase
- Anreise-Modi (auto/oepnv/shuttle/fahrrad)
- Fahrzeug
- oepnv_kosten / jobticket / shuttle_kosten / anfahrt_min

**Expected document_health**:
- status='green' wenn LSB+SE+CAS alle 12 Monate
- status='yellow' wenn <6 Monate
- status='red' wenn LSB oder SE oder CAS komplett fehlt

**Expected KPIs (Toleranz)**:
- arbeitstage 100-220
- fahrtage 30-90
- hotel_naechte 30-80
- z76_eur 2000-8000€
- gesamt (variiert nach Brutto + Werbungskosten)

**Logs to inspect**:
```
[v11-cas-pipeline] start
[phantom-removal] N dropped
[CAS-Reader] FERTIG: N/N Files, X Tage
arbeitstage=N fahr_tage=N z76_eur=X
```

**Payment-Verhalten**:
- Frontend setzt Stripe-PI auf
- Backend prüft via attempt_id Idempotenz
- free_retry_token für Re-Try ohne neue Zahlung

**Expected PDF**:
- Versions-Block: APP_VERSION=11.0, ENGINE_VERSION=tour_first_v11_clean_release
- KPI-Summary mit Z72/Z73/Z74/Z76
- AG-Erstattung-Anrechnung
- Werbungskosten-Aufstellung
- Disclaimer „Keine Steuerberatung"

**Success Criteria**:
- canonical_state='done' (oder 'needs_review' mit User-Action)
- pdf_allowed=True nach Done
- KPIs plausibel (Hard-Constraints OK: hotel<=arbeitstage, arbeitstage<=230)
- document_health.status != 'red' (außer Block-Reason klar erkennbar)

**Rollback**:
- Bei Fail: free_retry_token bereitstellen
- Bei Bug: gcloud Source-Re-Deploy (kein env-Risk)
- Bei Daten-Issue: Delete-Endpoint + Refund

**Stop after**: 1 Run. Kein Re-Run ohne explizites User-GO.

**KI-Kosten Budget**: ~$1 (Worst-Case 12 CAS-PDFs + LSB + SE + 5 Live-Resolver-Calls).

---



## §0 Vorbedingungen

| Item | Status |
|---|:-:|
| Phase 4.7 Evidence Engine | ✓ |
| Phase 4.8 Evidence in normalize_tours | ✓ |
| Phase 4.8b Threshold Calibration | ✓ |
| Phase 5a Mock-Resolver Infrastructure | ✓ |
| Phase 5a.1 Known-Conflict ai_required | ✓ |
| Phase 5b Live-KI-Call-Vorbereitung | ✓ (Dry-Run only, no API-Key) |
| Phase 5c KI-Resolution Shadow-Decision | ✓ |
| Phase 6 Golden Acceptance Cluster-Doc | ✓ |
| Phase 6b Counter-Finalisierung C3/C5/C8 | ✓ |
| Regression 1487 grün | ✓ |
| Acceptance 16 rot (Tour-Boundary-pending) | ⚠ erwartet |

## §1 Live-Run-Scope

### Was getan wird

Genau **EIN** Tibor-Live-Run gegen Production-Backend mit:
- Tibor's 3 echte PDF-Files (LSB, SE, Flugstundenübersicht)
- Standard-Formularangaben (Steuerjahr 2025, Homebase FRA, km-Entfernung)
- Feature-Flag `AEROTAX_TOUR_FIRST_CLASSIFIER=1` (per-job, NICHT global default)
- `AEROTAX_AI_RESOLVER_MODE=mock` (KEINE Live-KI-Calls für Counter)

### Was NICHT getan wird

- KEIN Production-Default-Switch (default bleibt OFF)
- KEIN Mass-Run gegen mehrere User
- KEIN Live-KI-Resolver für nicht-explizit-genehmigte Kandidaten

## §2 Files

| Datei | Lokation | Größe (geschätzt) | Hash |
|---|---|---:|---|
| `tibor_2025_LSB.pdf` | Tibor's lokales test-set | ~80 KB | (vor Run zu erfassen) |
| `tibor_2025_SE.pdf` | dito | ~400 KB | (dito) |
| `tibor_2025_CAS.pdf` | dito | ~1.2 MB | (dito) |

Diese Files **dürfen NICHT in Git oder Docs committed werden** — PII.
Hashes werden im Run-Audit erfasst und mit fixture `tibor_aerotax_v11_raw_initial.json` verglichen.

## §3 Authentifizierung / Token

- Test-Token aus Promo-Code-System (NICHT Stripe-payment)
- Token-Format: `aero_promo_<job_id>`
- Job-ID wird in Run-Audit aufgezeichnet
- Token wird **nach Run** consumed/sealed (kein Re-Use)

## §4 Snapshots — Was zu kapturen ist

Pro Phase-Übergang wird Snapshot in `_job_chunks_state/<job_id>/` geschrieben:

| Phase | Snapshot-File | Inhalt |
|---|---|---|
| Upload | `01_upload.json` | File-hashes, Größen, normalized-Filename |
| LSB-Reader | `02_lsb_facts.json` | `_sonnet_read_lsb_v2`-Output (Z17, etc.) |
| SE-Reader | `03_se_facts.json` | `_sonnet_read_se_structured`-Output (lines, totals) |
| CAS-Reader | `04_cas_facts.json` | `_sonnet_read_dp_structured`-Output (raw_days) |
| Health-Check | `05_doc_health.json` | `_document_health_check`-Output |
| Match | `06_matched.json` | `_match_dp_se_per_day`-Output |
| Tour-First | `07_normalized_tours.json` | `_normalize_tours_from_raw_facts`-Output mit evidence_*, ai_*, proposed_* |
| Klassifikation | `08_classification.json` | `_classify_days_from_normalized_tours`-Output mit tage_detail |
| Final | `09_final.json` | KPIs + audit_notes |

PII-Filter: Snapshots werden vor Persist durch `_pii_sanitize_snapshot` gefiltert (entfernt: Name, PNR, Email, Mitarbeiter-Nr).

## §5 Logs

Standard-Render-Log-Capture mit Filter:
- `[v8-classify] Done` — Phase-Übergang sichtbar
- `[ai-resolver-mock] kind=...` — Mock-Resolver-Calls (keine PII)
- `[bh-core-001] tour_first_active=True` — Feature-Flag aktiv bestätigt
- `[finalize-pdf] Counter ...` — Counter-Output

Logs werden via Render-API gezogen (`GET /v1/logs?ownerId=...&resource=srv-...&type=app`), 60 Sekunden vor + 5 Minuten nach Run.

## §6 API-Endpoints

| Endpoint | Method | Zweck |
|---|---|---|
| `/init-upload-session` | POST | Token+upload-session erstellen |
| `/upload-files` | POST | 3 PDFs hochladen |
| `/process` | POST | Job-Start mit `tour_first=true`-Flag |
| `/api/job/<job_id>` | GET | Poll-Status |
| `/api/session/<token>` | GET | Result-Polling |
| `/finalize-pdf/<job_id>` | POST | PDF-Generation |

## §7 KPIs zu erfassen

Vergleich mit Phase 6b Shadow-Werten (sind die Soll-Werte für Live-Run):

| KPI | Erwartet (Shadow) | Golden | Toleranz |
|---|---:|---:|---:|
| arbeitstage | 90 | 133 | ±2 vs Shadow |
| reinigungstage | 90 | 133 | ±2 vs Shadow |
| fahr_tage | 30 | 58 | ±2 vs Shadow |
| hotel_naechte | 40 | 66 | ±2 vs Shadow |
| z72_tage | 3 | 5 | ±0 vs Shadow |
| z73_tage | 3 | 11 | ±0 vs Shadow |
| z74_tage | 0 | 1 | ±0 vs Shadow |
| z76_eur | 3648 | 4794 | ±50€ vs Shadow |
| gesamt | 3732 | 6020.72 | ±50€ vs Shadow |

Akzeptanz-Definition: Live-Run muss **gleich oder besser als Shadow-Phase-6b** sein (max ±2 Abweichung pro Counter), und gleich oder besser als alte Pipeline (1473 grün-Stand).

## §8 Vergleich zu Golden

Nach Run, drei Vergleichs-Tabellen:

1. **Live-Run vs Shadow**: identisch innerhalb Toleranz?
2. **Live-Run vs Golden**: Cluster-Tabelle wie Phase 6 (welche Mismatches bleiben)
3. **Live-Run vs alte Pipeline-Result**: nicht schlechter?

Output-Dokument: `docs/BH_CORE_001_LIVE_RUN_RESULT.md` (wird nach Live-Run erzeugt).

## §9 Rollback

| Trigger | Action |
|---|---|
| Live-Run-Result < alte Pipeline | Rollback `AEROTAX_TOUR_FIRST_CLASSIFIER` → 0 für betroffenen Job |
| Crash / Timeout / Unhandled Exception | Auto-Rollback durch `_classify_job_state` → `failed_recoverable` |
| Counter-Output gefährlich falsch (z.B. arbeitstage=0) | Manual Rollback durch Admin |
| Live-Run successful aber UI-Bug | UI-Fix vor Phase 8 Production-Switch |

Rollback-Befehl (Render-Admin via Dashboard):
```
gcloud run services update aerotax-backend \
  --update-env-vars AEROTAX_TOUR_FIRST_CLASSIFIER=0 \
  --region europe-west3
```

## §10 Max 1 Live-Run

Stop-Regel: bei Erfolg → STOP für User-Review. Bei Mismatch → STOP für Debug.
Erst nach User-Freigabe weitere Live-Runs.

## §11 Stop-Regeln eingehalten

- ✓ Plan-Doc, keine Ausführung
- ✓ Kein Deploy
- ✓ Kein Live-Run jetzt
- ✓ Keine Env-/Secret-Änderung jetzt
- ✓ Keine Migration
- ✓ Kostenkontrolle KI dokumentiert (Mock-Mode für Live-Run)

**Status: Wartet auf User-Freigabe vor Ausführung.**
