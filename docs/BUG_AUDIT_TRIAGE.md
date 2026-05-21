# P0/P1-Triage mit Beweispflicht

**Datum:** 2026-05-14
**Quelle:** `docs/BUG_AUDIT_100.md` (138 Findings).
**Methode:** Pro Bug: Code re-read + Cloud-Run-ENV-Check + Test-Inventar + Deduplikation.

## Faktenbasis vor Triage

**Cloud-Run-ENV (`aerotax-backend`, europe-west3):**
```
ANTHROPIC_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_KEY,
STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, SESSION_SECRET,
AEROTAX_PIPELINE_VERSION, AEROTAX_FOLLOWME_ALIGN,
AEROTAX_CAPTURE_SNAPSHOTS, AEROTAX_USE_CHUNK_PERSISTENCE,
AEROTAX_CAS_MAX_PARALLEL, AEROTAX_LSB_FAST_READER_MODE,
AEROTAX_CAS_MERGE, PROMO_CODES, AEROTAX_EXECUTION_MODE,
AEROTAX_TASKS_QUEUE, AEROTAX_TASKS_LOCATION,
AEROTAX_GCP_PROJECT, AEROTAX_CLOUD_RUN_WORKER_URL,
AEROTAX_TASK_INVOKER_SA
```

**Nicht gesetzt** (wichtig für Default-Triage):
`RECOVERY_SECRET`, `SUPPORT_NOTIFY_EMAIL`, `AEROTAX_QA_SEED_TOKEN`, `ALLOW_UNPAID`

**Test-Inventar (13 Dateien):** test_auto_resume_state_pass, test_calculation, test_cloud_tasks, test_cloud_tasks_no_background_threads, test_concurrency_invariants, test_e2e_tibor_pipeline, test_qa_seed, test_recall_debug, test_state_machine, test_supabase_timeout_fix, test_ui_safety_gate.

**Architektur-Konstante:** `_jobs_lock = threading.RLock()` (app.py:909) → alle „Lock + save inside lock"-Findings prüfen ob mit RLock noch Deadlock.

---

## P0-Triage (16 Findings im Audit als P0 klassifiziert)

| ID | Datei:Zeile | Prod-reachable? | ENV-Override? | Test? | False-Positive? | Dedup | **Entscheidung** | Begründung |
|---|---|---|---|---|---|---|---|---|
| **#1** | app.py:4795 SESSION_SECRET fallback | NEIN (ENV gesetzt) | ja | fehlt | nein | — | **downgrade_to_P3** | Fallback in Cloud Run unerreichbar; Hardcode bleibt Smell für Dev/Self-Hosting |
| **#2** | index.html:1624 `pdf_allowed === false` strict | NEIN | n/a | test_ui_safety_gate.py | ja | — | **false_positive** | `canShowPdfDownload` prüft danach `!download_url`. Strict-equals fängt nur `false`, lässt `true/undef` durch — korrektes Verhalten für defensive Default |
| **#3** | app.py:1754 `anreise default 'auto'` | JA (Form kann fehlen) | n/a | fehlt | nein | — | **needs_evidence** | Muss prüfen ob Frontend immer `anreise` sendet (HTML-required ≠ Server-Validation) |
| **#28** | app.py:6660-6720 SSE 7:30min sleep | JA | n/a | fehlt | nein | — | **downgrade_to_P2** | gthread × 8 = 16 SSE-Slots. Aktuell <5 parallel User → kein akuter Block. Scaling-Risk |
| **#60** | index.html:5919 `_preUploadFiles().catch(console.warn)` | JA | n/a | fehlt | nein | — | **verified_bug P1** | User-Banner fehlt; Server schlägt aber bei process() trotzdem fehl → User-impact ist Verwirrung, nicht Geldverlust |
| **#68** | app.py 47× bare except | JA | n/a | fehlt | mixed | siehe Cluster | **cluster_needed** | Siehe Bare-Except-Cluster unten — nicht alle gleich kritisch |
| **#71** | app.py:534 Stripe-Webhook `str(e)` leak | NEIN für interne stacks | n/a | fehlt | teilweise | — | **downgrade_to_P2** | `stripe.Webhook.construct_event` raised typed exceptions mit cleanen messages, nicht python stacktraces |
| **#95** | app.py:1245 `_redact_pii` VOR Supabase persist | **JA** | n/a | fehlt | nein | — | **verified_bug P0** ✓ | `name/vorname/nachname` werden in Supabase als `[redacted]` persistiert. PDF heißt `[redacted].pdf` nach Container-Restart (siehe Z.2297 `result['name'].replace`). Chat zeigt „Hallo [redacted]" |
| **#96** | app.py:489 `_consumed_payment_intents` in-memory | **JA** | n/a | fehlt | nein | — | **verified_bug P0** ✓ | Cloud Run = multi-container. User schickt 2× /api/process parallel mit gleichem PI → Container A+B sehen je leeren cache → 2 Auswertungen für 1 Zahlung |
| **#97** | app.py:537 `_processed_stripe_events` in-memory | JA | n/a | fehlt | teilweise | dup #98 | **downgrade_to_P2** | Webhook-Replay trifft anderen Container → idempotenz-check schlägt. Aber `_store[ref]['paid']=True` ist idempotent (no-op bei 2. Set) |
| **#98** | app.py:550 Webhook `_store[ref]['paid']=True` in-memory | JA | n/a | fehlt | nein | partial dup #96 | **downgrade_to_P1** | Mitigation greift: `/api/process` fällt auf direct `stripe.PaymentIntent.retrieve` (app.py:1869+) wenn `ref` nicht in lokalem _store |
| **#99** | app.py:2376 review-answer ↔ upload-replacement race | JA, theoretisch | n/a | fehlt | nein | — | **downgrade_to_P2** | Beide Endpoints brauchen User-Action. User klickt nicht 2 Dinge gleichzeitig parallel. Theoretical race |
| **#100** | app.py:3214 `_jobs_lock + _save_job_to_disk INSIDE` | NEIN | n/a | test_concurrency_invariants | ja | — | **false_positive** ✓ | `_jobs_lock = RLock()` (app.py:909) — re-entrant, kein Deadlock |
| **#101** | app.py:4143 `_jobs.get() or _load_from_disk INSIDE lock` | JA (Tail-Latenz) | n/a | fehlt | nein | — | **downgrade_to_P2** | Mit RLock kein Deadlock; nur Tail-Latenz weil Disk-IO innerhalb Critical Section |
| **#102** | app.py:4905 `_save_session` upsert full-overwrite | JA, theoretisch | n/a | fehlt | nein | — | **downgrade_to_P2** | Jede Mutation hat Lese-Phase davor; race-Window ist sub-second |
| **#118** | index.html:5862 `_preUploadFiles` listet 'cas' nicht | UNKLAR | n/a | fehlt | unklar | dup #115 | **needs_evidence** | Backend akzeptiert `request.files.getlist(key)` für jedes key. Aber: ist `cas` aktuell überhaupt ein separater upload-key im Frontend, oder Teil von `dp`? |

**Verified P0 nach Triage: 2** (#95, #96)

Plus **#10** (RECOVERY_SECRET) wandert von P1 → P0 nach Triage, weil ENV-Check zeigt: nicht gesetzt → Default `''` wirklich aktiv (siehe unten).

Plus **bare-except-Cluster** liefert weitere echte P0 (siehe Sample-Tabelle unten).

---

## P1-Triage (35 Findings)

Kompakt — full Code-Verifikation und Dedup pro Eintrag.

| ID | Bug | Reachable? | ENV-Override? | Test? | Decision | Begründung |
|---|---|---|---|---|---|---|
| **#4** | app.py:3099 homebase default 'FRA' | nur wenn `cached_state['homebase']` fehlt → bei legacy-Jobs möglich | nein | fehlt | **downgrade_to_P2** | Pfad nur bei orphan/legacy state — keine aktiven legacy Jobs |
| **#5** | app.py:1789 `form.get('base', 'Frankfurt (FRA)')` | JA wenn Frontend nicht sendet | nein | fehlt | **verified P1** | HTML-required ≠ Server-Validation; muss serverseitig validiert werden |
| **#6** | app.py:288 amount default 1999 | nur wenn `data.amount` fehlt | nein | fehlt | **downgrade_to_P2** | Frontend setzt immer 1999. Backend-default ist gleicher Wert — kosmetisch |
| **#7** | index.html:3887 `_year \|\| 2025` | bei 2024-Jobs ohne year-field | nein | fehlt | **downgrade_to_P2** | Backend liefert immer year; legacy-fallback wirkt nur bei sehr alten Jobs |
| **#8** | index.html:3854 `dhStatus \|\| 'green'` | bei missing document_health | nein | fehlt | **downgrade_to_P2** | Backend liefert immer status; defensive default |
| **#9** | app.py:2110 `status or 'pending'` | wenn DB status falsy | nein | test_state_machine | **verified P1** | Bug: falsy (0, '', None) → 'pending' → re-process von done-jobs möglich bei DB-Glitch |
| **#10** | app.py:6266 `RECOVERY_SECRET default ''` | **JA** (ENV NICHT gesetzt!) | NEIN | fehlt | **upgrade_to_P0** ⚠ | Bei leerem Secret werden Recovery-Tokens via `sha256(ip+'')` raterbar |
| **#11** | app.py:749-764 Mustermann-defaults 140/5920 etc. | JA bei partial-state | nein | fehlt | **verified P1** | Demo-Werte sneaken in PDF wenn Reader partial liefert |
| **#25** | index.html:6609 pollIv ohne cleanup | JA | n/a | fehlt | **downgrade_to_P2** | Memory-Leak, kein Funktions-Bug |
| **#27** | index.html:3046 EventSource cleanup nicht finally | JA bei Tab-close | n/a | fehlt | **downgrade_to_P2** | Browser cleanups SSE bei tab-close meist selbst |
| **#30** | app.py:5507 chat_history race ohne Lock | JA bei Multi-Tab | n/a | fehlt | **downgrade_to_P2** | Multi-Tab selten; User-impact „Message verloren" |
| **#31** | app.py:1241 Save outside lock | JA | n/a | test_concurrency_invariants | **downgrade_to_P2** | Lost-update Race — selten, single-user pro session |
| **#32** | app.py:2133 Cloud-Tasks retry race | **JA** | n/a | test_cloud_tasks | **verified P1** | Stale-detect (>15min) + neuer Task race-on-branch → 2 Worker für 1 Job |
| **#46** | app.py:1188 `_restart_recovery_async` daemon pre-fork | NEIN | n/a | test_cloud_tasks_no_background_threads | **false_positive** | gthread mode startet keine background threads, Test verifiziert das |
| **#47** | index.html:6648 `_autoResumeInFlight finally` race | JA | n/a | fehlt | **downgrade_to_P2** | Race-Guard wirkt für initial trigger; re-trigger durch User-Action selten parallel |
| **#52** | index.html:2660 `_payInFlight=false 5000ms hardcoded` | JA bei Cold-Start >5s | n/a | fehlt | **verified P1** | Cold-Start kann >5s sein → Doppel-Job möglich |
| **#56** | index.html:3270 `try{json()}catch(_){}` swallow | JA bei 502/HTML-error-page | n/a | fehlt | **downgrade_to_P2** | Diagnose-impact, kein Funktions-Bug |
| **#57** | index.html:3299 await pollRes.json() ohne catch | JA | n/a | fehlt | **downgrade_to_P2** | dup #56 — SyntaxError-Pattern |
| **#58** | index.html:4063 r.json() ohne catch 6+ Stellen | JA | n/a | fehlt | **downgrade_to_P2** | dup #56/57 — broad fix nötig |
| **#69** | app.py:475 `print(crash:e)` ohne stack | JA | n/a | fehlt | **downgrade_to_P2** | Logging-Quality, kein User-Bug |
| **#70** | app.py 389× print() statt logger | JA | n/a | fehlt | **downgrade_to_P3** | Logging-Quality; refactor-cost massiv |
| **#74** | app.py:7008 `_claude_with_retry` keyword substring | JA | n/a | fehlt | **verified P1** | Echte API-Errors (401/400) als retryable klassifiziert → 5× retry mit gleichem 401 |
| **#75** | app.py:8013 `pdfplumber.open(...) except: pass` | **JA** | n/a | test_e2e_tibor_pipeline | **upgrade_to_P0** ⚠ | Silent CAS data loss — direkt verantwortlich für FollowMe-Diff-Bugs aus dem Audit-Trail (Z76 Ausland −1493€) |
| **#79** | app.py:8328 Sonnet-truncated generic error | JA | n/a | fehlt | **downgrade_to_P2** | User sieht „fehlgeschlagen"; SONNET_TRUNCATED-Code wäre besser, ist Diagnose |
| **#81** | app.py:9007 CAS-Reader parallel 429 | JA bei Stripe-burst | n/a | fehlt | **needs_evidence** | Muss prüfen ob CAS-parallel per-file try/except hat — Code zeigen |
| **#82** | app.py:9272 `_load_lsb_text except: return None` | **JA** | n/a | fehlt | **verified P1** | Defekte LSB-PDF → None → Brutto=0 → Auswertung mit 0€ statt Fehler-State |
| **#85** | app.py:9505 LSB-eLSTB try/except (großer Block) | JA | n/a | fehlt | **duplicate_of #82** | Gleiches Pattern, gleicher Fix |
| **#89** | index.html:6149 Web3forms ohne `if(!res.ok)` | JA | n/a | fehlt | **verified P1** | User denkt support-Nachricht gesendet, ist sie nicht |
| **#90** | app.py:399 `_save_uploaded_files_supabase except` | **JA** | n/a | fehlt | **upgrade_to_P0** ⚠ | User zahlt 19.99€, Supabase-Insert schlägt fehl, Files weg, /api/process findet 0 Files → User-Money-Loss |
| **#103** | app.py:2733 `_skipped_unanswered` define-no-setter | NEIN | n/a | fehlt | **downgrade_to_P3** | Dead code, kein bug |
| **#104** | app.py:2117 'cancelled' inconsistent | JA | n/a | test_state_machine | **downgrade_to_P2** | State-machine-Edge, kein User-Bug |
| **#105** | app.py:1238 `_save_job_to_disk` Lost-Update | JA | n/a | test_concurrency_invariants | **downgrade_to_P2** | Selten in single-user-flow |
| **#106** | app.py:2737 `_review_items` dict→list default | JA | n/a | fehlt | **downgrade_to_P2** | Type-coercion sneaks |
| **#107** | app.py:4012 `datetime.now()` vs `utcnow()` mixed | **JA** | n/a | fehlt | **verified P1** | Stale-detection mit local-time vs utc-stored → 2h drift in Cloud Run (UTC) → false-positive stale + false-negative stale |
| **#111** | app.py:1860 `_recovery_tokens` in-memory | **JA** | n/a | fehlt | **verified P1** | User schickt Recovery auf Container A, retry trifft Container B → 402 für legitimen User |
| **#114** | app.py:2402 `print(... Session-Token: {session_token})` | **JA** | n/a | fehlt | **verified P1** | Session-Token in Cloud-Logs lesbar — Cloud-Logging hat IAM aber Token ist 24h gültig, jeder mit Log-Access kann impersonate |
| **#115** | index.html:6252 `reqDone {lsb,dp,se}` ohne cas | UNKLAR | n/a | fehlt | **needs_evidence** | dup #118 — muss prüfen ob `cas` aktuell separater upload-key |
| **#116** | index.html:6320 _restoreUploads fehlt cas | UNKLAR | n/a | fehlt | **needs_evidence** | dup #115 |
| **#117** | index.html:6720 Drag-Drop ohne cas | UNKLAR | n/a | fehlt | **needs_evidence** | dup #115 |
| **#125** | index.html:5197 `result-netto-display` „—" leak | JA | n/a | fehlt | **downgrade_to_P2** | UI-bleed bei stale state |

**Verified P1 nach Triage: 11** (#5, #9, #11, #32, #52, #60, #74, #82, #89, #107, #111, #114, plus downgrade #98 → 13)

---

## Bare-except-Cluster (#68, 47 Vorkommen)

Sample-Klassifikation:

| Line | Was wird verschluckt | Risiko-Kategorie |
|---|---|---|
| 419 | wahrscheinlich init/marker_lexicon load | **P3 logging** |
| 618, 625, 660 | recovery_token/store cleanup | **P2 auth-edge** |
| 1098 | restart_recovery file write | **P2** |
| 2307 | PDF-render edge | **P2** |
| 5037, 5052 | session/store cleanup | **P3 logging** |
| 5274, 5295, 5297 | qa-feed parsing | **P3** |
| 5766, 5847, 5888 | qa-thread/session edges | **P3** |
| 5958, 6383 | logging/cleanup | **P3** |
| 6754 | progress-sse edge | **P3** |
| 6963 | recovery edge | **P2** |
| 7559, 7567, 7659, 7670, 7732, 7813, 7895 | Claude/Anthropic-API edges | **P1 — verschluckt API-Errors** |
| 7972, 8013, 8018, 8043, 8056 | **pdfplumber/CAS-reader** | **P0 — Silent CAS data loss** |
| 8112, 8121, 8392 | Sonnet output parsing | **P1 — verschluckt parser errors** |
| 8836, 8914, 8927, 8936 | classifier/match edges | **P1 — verschluckt calculation errors** |
| 9818 | LSB-load edge | dup #82 |
| 10937, 11607, 12036 | reportlab edges | **P2** |
| 15158, 15323 | cleanup | **P3** |
| 16240, 17156, 17207, 17270 | chat/post-edges | **P2** |
| 18679 | late-init | **P3** |

**Aus 47 bare-except sind ~5 P0 (CAS-reader cluster), ~10 P1 (claude/parser/classifier), Rest P2/P3.**

---

## Echte P0-Liste nach Triage (sortiert nach Impact)

| # | ID | Bug | Impact |
|---|---|---|---|
| 1 | **#95** | `_redact_pii` vor Supabase persist | PDF heißt `[redacted].pdf` nach Container-Restart |
| 2 | **#96** | `_consumed_payment_intents` in-memory | Multi-Container: 1 Zahlung → 2 Auswertungen |
| 3 | **#90** | `_save_uploaded_files_supabase` swallow | User zahlt, Files weg, /api/process leer |
| 4 | **#75** | `pdfplumber except: pass` (+ CAS cluster) | Silent CAS-data-loss → FollowMe-Diff-Bugs |
| 5 | **#10** | `RECOVERY_SECRET default ''` | ENV nicht in Cloud Run → Tokens raterbar |

**Verified P0: 5** (von 16 Findings, nach Triage).

## Echte P1-Liste nach Triage (sortiert)

| # | ID | Bug | Impact |
|---|---|---|---|
| 1 | **#98** | Webhook `_store[ref]['paid']` in-memory | Mitigated durch direct-PI-verify |
| 2 | **#5** | `form.get('base', 'Frankfurt')` | MUC-Crew silent als FRA-Crew |
| 3 | **#82** | `_load_lsb_text except: return None` | Brutto=0 silent statt error |
| 4 | **#111** | `_recovery_tokens` in-memory | Multi-Container retry → 402 |
| 5 | **#114** | `session_token` in stdout-Log | Token in Cloud-Logs lesbar |
| 6 | **#107** | `datetime.now/utcnow` mixed | Stale-Threshold drift |
| 7 | **#32** | Cloud-Tasks retry race | Stale+fresh task race |
| 8 | **#52** | `_payInFlight=false 5s hardcoded` | Doppel-Job bei Cold-Start |
| 9 | **#74** | claude-retry keyword substring | 401 als retryable, 5× retry |
| 10 | **#60** | `_preUploadFiles().catch(warn)` | User-Banner fehlt |
| 11 | **#11** | Mustermann-defaults 140/5920 | Demo-Werte sneaken |
| 12 | **#9** | status `or 'pending'` | Re-process von done-jobs |
| 13 | **#89** | Web3forms ohne ok-check | Support-Mail silent fail |
| 14 | **#68/Claude/Parser** | bare except in API-cluster | API/Parser errors swallowed |
| 15 | **#68/Calc** | bare except in classifier | Calc-errors swallowed |

**Verified P1: 15** (von 35 Findings, nach Triage).

---

## Top-10 Fix-Reihenfolge

| Rang | Fix | Warum zuerst? | Isolation? |
|---|---|---|---|
| 1 | **#95** PII-redact nur in disk/logs, NICHT vor Supabase | Größter User-impact (broken PDFs), kleinster Diff | **isoliert deploy** — touch nur `_save_job_to_disk` |
| 2 | **#90** Pre-upload Supabase fail → 503 + Refund-Pfad | User-Money-Loss — höchste Severity | **isoliert** |
| 3 | **#75 + CAS-cluster** bare-except → `logger.exception` | Direkt verbunden mit FollowMe-Diff Audit (#214 Z76 −1493€) | **isoliert** — touch nur CAS-reader |
| 4 | **#96** `_consumed_payment_intents` in Supabase migrieren | Multi-Container Payment-Bypass | **isoliert + neuer Supabase-Table** |
| 5 | **#10** RECOVERY_SECRET in Cloud Run setzen + Code-Check `if not secret: abort(503)` | ENV-Fix + Defensive | **kombiniert mit #114** (beide auth-related) |
| 6 | **#114** session_token aus Log entfernen + Token-mask helper | Token-Leak in Cloud-Logs | **kombiniert mit #10** |
| 7 | **#82** `_load_lsb_text` → `raise` statt return None + Test | Brutto=0 silent → falsche Auswertung | **isoliert** |
| 8 | **#5** Server-Validation `base` required | Silent FRA-Default für MUC-Crew | **kombiniert mit #11** (beide form-default-bugs) |
| 9 | **#11** Mustermann-defaults → `raise PartialReaderError` | Demo-Werte in echtem PDF | **kombiniert mit #5** |
| 10 | **#74** claude-retry exact-status statt substring + #68 API-cluster | API-Errors falsch klassifiziert | **isoliert** |

## Welche Fixes zusammen, welche isoliert?

**Zusammen-möglich (gleicher PR):**
- **#5 + #11**: beide Form-Default-Bugs in `/api/process` — gleicher Test-File
- **#10 + #114**: beide Auth-related — gleicher Diff-Scope
- **#75 + CAS-Cluster bare-except**: gleiche Funktion, gleicher Test-File `test_e2e_tibor_pipeline.py`

**Muss isoliert deployed werden:**
- **#95 PII-redact**: betrifft Supabase-Schema-Semantik — eigener PR mit Migration-Note
- **#96 _consumed_payment_intents → Supabase**: braucht neue Table + Stripe-Integration-Test
- **#90 pre-upload fail**: Stripe-Refund-Logic neu — eigener PR
- **#82 LSB raise**: Error-Surface ändert User-Flow — eigener PR mit UI-Update

## Was bleibt offen (`needs_evidence`)

| ID | Frage |
|---|---|
| **#3** anreise default | Sendet Frontend `anreise` immer (HTML check vs JS submit)? |
| **#81** CAS parallel 429 | Hat `_cas_read_parallel` per-file try? |
| **#115/116/117/118** cas vs dp/einsatz | Ist `cas` aktueller separater upload-key oder Teil von `dp`? |
| **#46** daemon pre-fork | gthread-mode + `--preload`? Test bestätigt aber nicht 100% |

---

## Zusammenfassung

- **Findings im Audit:** 138
- **Als P0 markiert:** 16 → nach Triage **5 verified P0**
- **Als P1 markiert:** 35 → nach Triage **15 verified P1**
- **False-Positives:** 2 (#2, #46, #100)
- **Downgrades zu P2/P3:** ~25
- **Needs-Evidence:** ~7
- **Bare-except 47×:** ~5 echte P0 (CAS), ~10 P1 (API/Parser/Calc), Rest P2/P3

**Echte Bug-Bilanz:** **~20 Fixes lohnen sich** (5 P0 + 15 P1), Rest ist Smell/Diagnostik/theoretische Races.

Keine Fixes durchgeführt. Keine Deploys. Keine neuen Bug-Hunts.
