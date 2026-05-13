# AUDIT_BUG_REGISTER — AeroTAX

> Verbindlich: ein Bug ist nur `verified_closed` mit Browser-/Trace-Beweis.
> Sonst `fixed_unverified` oder `open`.

---

## BUG-001 — „Code prüfen" hängt im Loading-State

| Feld | Inhalt |
|---|---|
| **ID** | BUG-001 |
| **Titel** | „Code prüfen"-Button bleibt auf „⏳ Code wird geprüft…", öffnet weder Result-Panel noch Fehler |
| **Area** | Frontend Recall-Flow (`window._recallSubmit` in index.html ~Z. 7490) |
| **Severity** | P0 |
| **User impact** | Funktionaler Lock-out: User mit gültigem Zugangscode kommt nicht zur Auswertung zurück. |
| **Reporter** | User (2026-05-12 17:30 UTC) |
| **Status** | `open` |

### Repro Steps
1. `https://aerosteuer.de/` öffnen
2. Nav-Link „Token" anklicken → Modal öffnet
3. Code eingeben: `AT-89080734B3FDC191` (Mini-Run-Token von 2026-05-12)
4. „Code prüfen" anklicken
5. Beobachten: Button-Text wechselt sofort auf „⏳ Code wird geprüft…"
6. **Erwartet:** Modal schließt + Result-Panel öffnet ODER Fehler-Banner erscheint
7. **Beobachtet:** Button bleibt permanent in Loading. Kein Modal-Close, keine Page-Navigation, kein Fehler

### Evidence (Beweise, die wir haben)

| Quelle | Beobachtung |
|---|---|
| Cloud Run Logs | Multiple `GET 200 /api/session/AT-89080734B3FDC191` Aufrufe vom User-Browser am 17:23 + 17:39 + 17:43 |
| Cloud Run Logs | KEIN Aufruf von `/api/job/c7135ecf-...` nach User-Click |
| Supabase direkt | Session existiert, `job_id=c7135ecf-42d5-4f0e-883c-dbd228d2ff17`, `result_data` komplett (netto=159.78€, brutto=52884.81, 15 review_items pending) |
| Job-Tabelle direkt | `status=done`, `attempt_id=1`, `reason_code=None` (= needs_review wegen pending review items) |

### Root Cause Status: **UNGEKLÄRT**

Hypothesen (noch unbewiesen):

| Hypothese | Status |
|---|---|
| (A) `await r.json()` hängt obwohl Backend 200 sendet | unbewiesen |
| (B) `_recallSubmit` JS-Error nach Response, silent crash | unbewiesen |
| (C) Zweiter fetch `/api/job/<id>` hängt (CORS/Cloud Run) | unbewiesen — User-Logs zeigen, dass `/api/job` GAR NICHT aufgerufen wird |
| (D) Modal-Close-Logik feuert nicht | unbewiesen |
| (E) Network failure silent (z.B. iOS Safari connection drop) | unbewiesen |

### Fix Plan
1. **`?debug=1` Modus** in `_recallSubmit` mit 20 sichtbaren Steps (siehe Schritt 2 oben)
2. **AbortController-Timeout** auf jeden fetch (15s session, 10s job)
3. **`finally`-Block** der Button immer reaktiviert
4. **Klar getrenntes UI-Feedback** pro Pfad (success/error/timeout)
5. **Console-Logs** `[recall-debug] step=...` parallel

### Tests Required
- [x] `test_recall_debug_steps_exist` (statisch: Code enthält 20 Steps)
- [ ] `test_recall_valid_done_opens_result` (DOM)
- [ ] `test_recall_valid_needs_review_opens_result` (DOM)
- [ ] `test_recall_valid_processing_opens_progress` (DOM)
- [ ] `test_recall_failed_support_shows_support_state` (DOM)
- [ ] `test_recall_expired_shows_expired` (DOM)
- [ ] `test_recall_wrong_code_shows_error` (DOM)
- [ ] `test_recall_session_timeout_shows_connection_message` (DOM)
- [ ] `test_recall_job_timeout_uses_session_fallback` (DOM)
- [ ] `test_recall_button_never_stuck_disabled` (DOM)
- [ ] `test_recall_no_silent_fallthrough` (DOM)

### Browser Proof Required
- [ ] User klickt mit `?debug=1` → Step-Trace vollständig
- [ ] User-Screenshot/Trace zeigt **welcher Step** crasht
- [ ] Fix verifiziert: User klickt erneut → Modal schließt + Result öffnet

### Workaround (User-side)
- localStorage-clear für `aerotax_session` löschen + Page reload (Auto-Resume umgehen)

---

## BUG-002 — Cloud Run antwortet nicht zuverlässig (HTTP 000 + Container-Restart-Loop)

| Feld | Inhalt |
|---|---|
| **ID** | BUG-002 |
| **Titel** | `/api/health`, `/api/qa`, `/api/session`, `/api/health/full` antworten mit HTTP 000 (starttransfer=0) nach 30s timeout. Container im Restart-Loop. |
| **Area** | Cloud Run Service-Konfiguration / Architektur / Container-Stabilität |
| **Severity** | P0 — Backend praktisch nicht erreichbar; **blockiert BUG-001 verified_closed** |
| **Reporter** | User (2026-05-12 18:00 UTC „forum lädt 40s nicht"), reproduziert via curl 18:08 UTC |
| **Status** | `open` — Workaround (`concurrency=10`, `min=1`) **wirkt nicht**, Problem persistiert |

### Repro Steps
1. Worker-Job starten (z.B. via `/api/process`)
2. Während Worker rechnet: parallel `/api/health` aufrufen
3. **Erwartet:** 200 in <2s
4. **Beobachtet:** 429 für mehrere Minuten ODER >15s response time

### Evidence
**Frische curl-Daten 2026-05-12 18:08 UTC** (4 Endpoints, alle HTTP 000 nach 30s):
```
/api/health        HTTP 000 | time=30.006s | dns=2ms | connect=102ms | starttransfer=0.000000s
/api/qa?sort=hot   HTTP 000 | time=30.006s | starttransfer=0.000000s
/api/session/...   HTTP 000 | time=30.006s | starttransfer=0.000000s
/api/health/full   HTTP 000 | time=30.006s | starttransfer=0.000000s
```
**Interpretation:** TCP-Connection zu Cloud Run Edge klappt (102ms), aber Container schickt 30s lang keinen einzigen HTTP-Response-Byte.

**Container-Restart-Loop (gcloud logs read, 17:00-18:10 UTC):**
```
17:07:20 term  → 17:09:09 supabase connect    (2 Min Gap, Container neu)
17:14:07 supabase connect                      (noch ein Container)
17:23:00 term  → 17:23:12 starting gunicorn   (12s Gap)
17:36:41 starting gunicorn  ×3 parallel!       (3 Container gleichzeitig hoch)
17:39:14 term               ×3 parallel!       (3 Container gleichzeitig tot)
17:51:42 term, 17:51:48 term, 17:51:49 connect, 17:51:50 shutdown  (Chaos)
18:02:55 starting gunicorn                     (noch ein Restart)
18:04:49 supabase connect
```

**3× ERROR-Events** (gcloud logging read severity>=ERROR, 2h Fenster):
```
17:23:14 ERROR  (kein textPayload — vermutlich crash/OOM)
17:37:09 ERROR
17:40:35 ERROR
```

**Cloud Run Revision Health:**
```
Ready=True; ContainerHealthy=True; MinInstancesProvisioned=Unknown
```
`MinInstancesProvisioned=Unknown` = Cloud Run kann den `min-instances=1`-Vertrag nicht halten.

### Root Cause Status: **HYPOTHESE NEU**

| Hypothese | Status | Beweis |
|---|---|---|
| Worker-Thread (`_calc_worker`) läuft trotz `AEROTAX_EXECUTION_MODE=cloud_tasks` weiter und blockiert Gunicorn-Mainloop | **plausibel — zu prüfen** | `[queue] Worker-Thread gestartet` in jedem Container-Boot-Log |
| Cloud Run Health-Probe timeoutet → kill → Restart-Loop | **plausibel — zu prüfen** | Repeated `Handling signal: term` ohne User-Action |
| Memory-Pressure → OOMKilled | **plausibel — zu prüfen** | 3× ERROR ohne payload (typisch für OOM-Kill) |
| Per-IP-Rate-Limit von Cloud Run | unwahrscheinlich | Curl von 2 verschiedenen IPs identisches Verhalten |
| `concurrency=1` blockiert | **widerlegt** | Workaround=10 schon aktiv, Problem persistiert |

### Workaround Status
- `concurrency=10`, `min-instances=1`, `max-instances=5` **deployed (revision 00019-xwv) und unwirksam**.
- Container restartet weiter, `starttransfer=0` persistiert.

### Phase 1 Option A — Deploy 2026-05-12 18:25 UTC (revision 00020-2c8)
**Status:** `fixed_unverified` (Restart-Loop-Symptom weg, aber neuer BUG-005 sichtbar)

Boot-Logs zeigen alle 3 Disable-Messages:
```
[boot] cloud_tasks mode: legacy background worker disabled
[boot] cloud_tasks mode: restart-recovery background thread disabled
[boot] cloud_tasks mode: cleanup-loop background thread disabled
```
KEIN `Worker-Thread + Restart-Recovery gestartet (async)` mehr. KEINE `Handling signal: term`-Schleife in den letzten 20 min. KEINE ERROR-Events.

**Phase 1 Fix wirkt** — der Restart-Loop ist gestoppt. Aber **BUG-005 (cpu-throttling)** ist die zweite Ursache der HTTP 000 Symptome. Beide brauchen Fix.

### Echter Fix (geplant, nicht gemacht) — drei Optionen

**Option A — Worker-Thread in `app.py` deaktivieren wenn `AEROTAX_EXECUTION_MODE=cloud_tasks`**
- *Minimal-invasiv:* legacy `_calc_worker` darf nicht starten wenn Cloud Tasks aktiv.
- 1 File geändert (`app.py`), 1 If-Statement, ~10 Zeilen.
- Beweist Hypothese 1. Wenn das Container-Restart-Loop stoppt → Root Cause bewiesen.

**Option B — Service-Trennung `aerotax-api` + `aerotax-worker`**
- *Echte Architektur-Fix:* Worker-Traffic kann User-Traffic nicht mehr blockieren.
- Neues Cloud Run Service `aerotax-worker` mit eigener URL.
- `aerotax-api` kriegt env `AEROTAX_WORKER_URL=<worker-url>` für Cloud Tasks Target.
- CORS muss bei beiden für `aerosteuer.de` offen sein.
- ~2-3h Setup, deploy, env-mapping.

**Option C — Rollback auf Render**
- *Fallback:* Render hatte das Problem nie. Cloud Run ist neu (v13 Phase B).
- Cloud Tasks würde dann gegen Render-URL feuern.
- Verworfener Plan rückgängig.

**Empfehlung:** Erst A diagnostisch (10 Zeilen, klare Hypothese-Probe). Wenn A nicht reicht → B als echter Architektur-Fix. C nur als Notfall.

### Tests Required (Latenz-Messung)
- [ ] `test_api_health_under_2s_without_worker` (10×)
- [ ] `test_api_session_under_2s_without_worker` (10×)
- [ ] `test_api_qa_under_2s_without_worker` (10×)
- [ ] `test_api_health_under_2s_during_worker` (synthetisch)
- [ ] `test_api_session_under_2s_during_worker` (synthetisch)
- [ ] `test_no_429_during_worker_run` (synthetisch)

### Browser Proof Required
- [ ] Live-Test: Worker läuft + Forum lädt + Recall lädt — alle <2s

---

## BUG-003 — PDF-Button bei `pdf_allowed=false` sichtbar (Mini-Run-Reopen)

| Feld | Inhalt |
|---|---|
| **ID** | BUG-003 |
| **Titel** | „PDF herunterladen"-Button sichtbar trotz `canonical_state=needs_review` + `pdf_allowed=false` |
| **Area** | Frontend Result-Panel rendering |
| **Severity** | P0 (Sicherheit: User könnte falschen Betrag als final übernehmen) |
| **Reporter** | User-Report nach Mini-Run-Reopen (2026-05-12) |
| **Status** | `fixed_unverified` |

### Repro Steps
1. Erfolgreichen Mini-Run mit `needs_review`-Outcome haben
2. Auswertung erneut öffnen (Recall mit Token)
3. **Erwartet:** „PDF noch nicht verfügbar — Offene Punkte im Chat klären"
4. **Beobachtet:** Vor v14: „⬇ PDF herunterladen"-Button sichtbar + Detail-Tabelle als final

### Evidence
- Mini-Run #1 (Token `AT-89080734B3FDC191`): Backend lieferte `canonical_state=needs_review` + 15 pending review_items, aber Frontend zeigte PDF-Button
- Root Cause: Frontend las `download_url` direkt statt `canonical_state`

### Root Cause Status: **BEWIESEN**
Frontend ignorierte Phase-A canonical_state. PDF-Sichtbarkeit hing nur an `data.download_url`.

### Fix Implementiert (v14, lokal + production)
- `window.canShowPdfDownload(apiState)` zentrale Gate-Funktion
- `window.deriveUiState(apiState)` UI-State-Ableitung
- `window._applyPdfVisibility(uiState)` einziger Toggle-Pfad
- HTML-Buttons initial `display:none`
- `dlPDF()` Hard-Gate

### Tests Required
- [x] `test_canShowPdfDownload_blocks_non_done` (statisch)
- [x] `test_canShowPdfDownload_blocks_pdf_not_allowed` (statisch)
- [x] `test_canShowPdfDownload_blocks_pending_review_items` (statisch)
- [x] `test_pdf_button_initial_display_none` (statisch HTML)
- [x] `test_dlPDF_has_hard_gate_at_start` (statisch)
- [ ] **`test_dom_renders_no_pdf_when_needs_review`** (echter DOM-Test) — NICHT vorhanden
- [ ] **Browser-Beweis**: Recall mit needs_review-Token zeigt KEINEN PDF-Button — NICHT erbracht

### Browser Proof Required
Sobald BUG-001 gelöst: User soll mit `AT-89080734B3FDC191` recallen und screenshotten dass kein PDF-Button erscheint.

---

## BUG-005 — Cloud Run cpu-throttling=true + Gunicorn sync-worker → Container hängt unter Last

| Feld | Inhalt |
|---|---|
| **ID** | BUG-005 |
| **Titel** | Backend HTTP 000 / 15-30s Latenz. Doppelursache: (a) cpu-throttling=true → Container hat idle keine CPU; (b) Gunicorn workers=1 sync-worker → bei Cloud Run concurrency=10 staut sich die Request-Queue, ein hängender Supabase-Call blockiert alle anderen Threads. |
| **Area** | Cloud Run Infrastruktur + Gunicorn Worker-Klasse |
| **Severity** | P0 — Backend faktisch unerreichbar aus User-Perspektive (Frontend-Timeouts) |
| **Reporter** | Diagnostik nach Phase 1 BUG-002-Fix-Deploy (2026-05-12 18:30-18:45 UTC) |
| **Status** | `fixed_unverified` — alle Latenz-Tests A-E grün, Browser-Proof noch ausstehend (2026-05-13 19:13 lokal) |

### Repro Steps
1. Cloud Run Service `aerotax-backend` 2+ min idle lassen
2. `curl --max-time 15 https://aerotax-backend-...run.app/api/health`
3. **Beobachtet:** HTTP 000 nach 15s, starttransfer=0
4. **Cloud Run Logs:** `httpRequest.latency = 14.97s, status = 200`
5. Bedeutet: Container hat 200 nach 14.97s zurückgegeben, curl-Timeout knapp vorher.

### Evidence
**Phase 1 Deploy (revision 00020-2c8) bestätigt:**
- Boot-Logs zeigen alle 3 Disable-Messages ✓
- KEIN Container-Restart-Loop mehr ✓
- KEINE ERROR-Events mehr ✓
- ABER: Latenz pro Request bleibt 15-30s (Cloud Run Frontend log)

**Cloud Run Config (aktuell, revision 00020-2c8):**
```
cpu-throttling:      true       ← URSACHE
startup-cpu-boost:   true
min-instances:       1          ← greift faktisch nicht
max-instances:       5
concurrency:         10
timeout:             1800s
```

**Latenz-Pattern (httpRequest.latency aus Cloud Run logs):**
```
18:43:32  /api/session/...  → 200 (29.948s)
18:43:02  /api/session/...  → 200 (29.917s)
18:36:51  /api/job/...      → 200 (14.913s)  ← warm
18:36:36  /api/session/...  → 200 (14.995s)
18:36:05  /api/health/full  → 200 (14.922s)
```

### Root Cause Status: **BEWIESEN (Hypothese mit hohem Konfidenz)**

Mit `cpu-throttling=true` weist Cloud Run dem Container nur CPU zu wenn aktiv Requests bearbeitet werden. `min-instances=1` hält den Container "warm" im Sinne von "nicht beendet", aber bei Idle hat er fast keine CPU. Bei eingehender Request muss er:
1. Wakeup (~5-10s)
2. Python-GIL aktivieren, Import-Caches warm
3. Erste DB-Connection (supabase) re-establishen
→ 15-30s bis erste Bytes.

Subsequent Requests in der Warm-Phase sind schnell (<200ms), aber nach ~1-2 min Idle wieder Cold-Path.

### Fix Plan
```bash
gcloud run services update aerotax-backend \
  --region=europe-west3 \
  --no-cpu-throttling
```

- Effekt: Container kriegt durchgehend CPU
- Erwartete Latenz: p95 <200ms für Health/Forum, <500ms für Session
- Cost: +$15-30/Monat (CPU 24/7 statt request-only)
- Reversibel: `--cpu-throttling`

### Fix Implementiert (deployed in revision 00028-kgd, 2026-05-13 19:04 lokal)

**1. Dockerfile** (Gunicorn-Konfiguration):
```dockerfile
CMD exec gunicorn app:app \
    --workers 1 \
    --worker-class gthread \    ← NEU (vorher: default sync)
    --threads 8 \               ← NEU (vorher: 2)
    --timeout 1800 \
    --max-requests 200 \
    --max-requests-jitter 20
```

**2. Cloud Run Service Config:**
```
containerConcurrency:  8        ← matched gunicorn threads
cpu-throttling:        false    ← Container kriegt durchgehend CPU
min-instances:         1
max-instances:         5
```

**3. Request-Instrumentation in app.py** (`@app.before_request` / `@app.after_request` / `@app.teardown_request`):
- Jeder Request kriegt request_id (uuid-prefix)
- Loggt path, method, pid, tid, duration_ms, status, exc
- Skip-Liste für `/api/progress` (SSE)
- Prefix `[req]` macht grepbar
- Try/except in allen Hooks (Instrumentation darf nie App brechen)

### Tests Required (Latenz-Messung NACH Fix) — ALLE GRÜN
- [x] `/api/health` 20× → 20/20 200, p95=205ms ✓
- [x] `/api/qa?sort=hot` 20× → 20/20 200, p95=443ms ✓
- [x] `/api/session/<token>` 20× → 20/20 responsiv (404 in 161-407ms) ✓
- [x] 8 parallel `/api/qa` → 8/8 200 in 510-513ms (echte Concurrency, 8 distinkte `tid`s in Logs) ✓
- [x] 5min Idle Retest → Health 5/5 (83-160ms), Forum 5/5 (308-344ms) — kein Cold-Start ✓

### Test-Coverage Statisch
- `tests/test_concurrency_invariants.py` (19 Tests) — alle grün:
  - Dockerfile-Invarianten (gthread, threads=8, timeout=1800, max-requests=200/jitter=20)
  - Procfile-Konsistenz
  - Instrumentation-Hooks vollständig (path, pid, tid, duration_ms, status, exc, request_id)

### Browser Proof Required (offen, nächster Schritt)
- [ ] User klickt „Code prüfen" → Modal schließt + Result öffnet <3s
- [ ] User lädt Seite mit aktivem Token neu → Auto-Resume zeigt korrekten State

### Self-inflicted Incident (Lessons Learned, 2026-05-12)
Während Diagnostik nutzte ich `gcloud run services update --set-env-vars=...` als Force-Restart-Trick → das **überschrieb alle env vars** inkl. `AEROTAX_EXECUTION_MODE=cloud_tasks`. Backend fiel in thread-mode zurück, Worker-Thread lief wieder, Container-Restart-Loop kehrte zurück. Recovery: env vars aus alter revision wiederhergestellt mit `--update-env-vars`. CLAUDE.md aktualisiert mit Warnung.

---

## BUG-004 — Doppelte Recall-Hinweise (Button-Text + Status-Banner identisch)

| Feld | Inhalt |
|---|---|
| **ID** | BUG-004 |
| **Titel** | Während Code-Check zwei gleiche Hinweise: Button „⏳ Code wird geprüft…" + Banner „⏳ Code wird geprüft — einen Moment bitte…" |
| **Area** | Frontend Recall-Modal Loading-State |
| **Severity** | P3 (Cosmetic) |
| **Reporter** | User (2026-05-12 17:55) |
| **Status** | `fixed_unverified` |

### Repro Steps
1. Token-Modal öffnen
2. Code eingeben
3. „Code prüfen" klicken
4. **Erwartet:** EIN Loading-Indikator
5. **Beobachtet (vor Fix):** ZWEI gleiche Texte

### Evidence
User-Screenshot mit beiden Texten gleichzeitig.

### Root Cause Status: **BEWIESEN**
Mein v14-Code setzte sowohl Button-Text als auch Status-Banner mit Loading-Text.

### Fix Implementiert (lokal, deployed)
Status-Banner-Loading-Aufruf entfernt. Nur noch Button-Text während Check. Banner kommt nur bei Antwort (success/error).

### Tests Required
- [ ] `test_recall_loading_no_duplicate_hint` (DOM)
- [ ] Browser-Beweis: Klick → nur Button-Text, kein Banner

### Browser Proof Required
- [ ] User klickt → nur Button-Loader, kein doppelter Hinweis

---

## BUG-006 — Race Condition: gleichzeitige `/api/process` mit gleichem PaymentIntent

| Feld | Inhalt |
|---|---|
| **ID** | BUG-006 |
| **Titel** | `_consumed_payment_intents` ist In-Memory dict + nicht thread-locked. Bei 2 parallelen Requests mit selbem PI gleichzeitig: beide passieren den Check, 2 Jobs entstehen. |
| **Area** | Backend `/api/process` Payment-Idempotency |
| **Severity** | P1 — niedrige Wahrscheinlichkeit (Race-Window <100ms), aber Doppelzahlung möglich |
| **Reporter** | Code-Inspection im Rahmen Phase 2 Bug-Hunt (2026-05-12) |
| **Status** | `open` |

### Repro Steps (theoretisch)
1. User auf Stripe-Form: Klick „Bezahlen"
2. Stripe `confirmPayment` returnt `succeeded` → Frontend triggert `process()` (Z. 2663 in index.html)
3. Parallel: Browser reload macht `?paid=1&ref=X` redirect-handling → triggert auch `process()` (Z. 6383)
4. Beide `/api/process`-Requests senden gleichen `payment_intent_id`
5. Beide passieren `if pi_id and pi_id in _consumed_payment_intents` (false bei beiden, weil noch nicht eingetragen)
6. Beide setzen `_consumed_payment_intents[pi_id] = now()` → 2 Jobs erstellt

### Evidence
**Code-Inspektion app.py:1641-1644:**
```python
if pi_id and pi_id in _consumed_payment_intents:
    return jsonify({'error': '...'}), 402
# ... (nicht thread-locked)
if pi_id:
    _consumed_payment_intents[pi_id] = datetime.utcnow()
```

Plus: bei Multi-Container-Cloud-Run hat jeder Container sein eigenes `_consumed_payment_intents` dict → 2 Container können beide PI akzeptieren ohne Konflikt.

### Mitigations (existent)
- Stripe webhook setzt `_store[ref]['paid'] = True` — bei `is_paid` check ist das primärer Layer
- `_store[ref]['paid'] = False` nach Verbrauch (Z. 1697)
- Aber: bei race condition könnten beide den `is_paid` check passieren wenn `_store` race ebenfalls

### Fix Plan (NICHT JETZT — nur dokumentiert)
- Supabase atomic check-and-set für PI-Verbrauch (statt in-memory)
- Plus: Frontend deduplizieren — `window._processStarted=true` flag mit timestamp

### Tests Required
- [ ] `test_concurrent_process_same_pi_only_one_succeeds` (synth)
- [ ] `test_process_idempotent_per_pi_across_containers` (Supabase-Lock-Test)

---

## BUG-007 — Frontend `process()` kein Idempotenz-Lock

| Feld | Inhalt |
|---|---|
| **ID** | BUG-007 |
| **Titel** | `process()` (Z. 2976 in index.html) hat keinen Doppelklick-Schutz wie `pay()` (Z. 2619 `_payInFlight`). 2× klicken bei mode=paid → 2× /api/process. |
| **Area** | Frontend `process()` |
| **Severity** | P1 — User könnte versehentlich 2 Jobs starten |
| **Reporter** | Code-Inspection 2026-05-12 |
| **Status** | `open` |

### Repro Steps
1. Bezahlung abgeschlossen (Stripe success)
2. `process()` wird aufgerufen + dauert mehrere Sekunden bis nächster Screen
3. User klickt nochmal (Frust, langsame Verbindung)
4. **Beobachtet:** zweite `process()` startet parallel → zwei `/api/process`-Requests

### Evidence
**Code-Inspection:**
- `pay()` hat `if(window._payInFlight){ ...return; }` (Z. 2619)
- `process()` hat KEINEN solchen Check

### Mitigation (existent)
- BUG-006 backend `_consumed_payment_intents` fängt das ab — wenn nicht race-condition
- Plus: `process()` ruft `go('proc')` was zur Result-Page navigiert → User sieht nicht mehr den Button

### Fix Plan
```js
async function process(){
  if(window._processInFlight){ return; }
  window._processInFlight = true;
  // ... existing code ...
  // finally: setTimeout(()=>{ window._processInFlight=false }, 5000);
}
```

### Tests Required
- [ ] `test_process_double_call_only_one_runs` (DOM)
- [ ] `test_process_inflight_flag_resets_after_timeout` (DOM)

---

## BUG-008 — `_qa_async_aerotax` (per-request thread) bleibt unangetastet trotz cloud_tasks

| Feld | Inhalt |
|---|---|
| **ID** | BUG-008 |
| **Titel** | `app.py:5728` spawnt einen Background-Thread für Q&A-Antworten. Im cloud_tasks-mode läuft das immer noch im API-Container und kann lange Sonnet-Calls (10-30s) im Hintergrund halten. |
| **Area** | Backend `/api/qa/ask` |
| **Severity** | P2 — kein Restart-Loop (Threads sind per-request, kurzlebig), aber Cloud Run Container terminiert evtl. mit pending threads bei scale-down |
| **Reporter** | Code-Inspection Phase 2 Bug-Hunt 2026-05-12 |
| **Status** | `open` |

### Evidence
**Code-Inspection app.py:5728:**
```python
_qa_thread.Thread(target=_qa_async_aerotax, args=(qid, title, text), daemon=True).start()
```
Wird in `/api/qa/ask` per-Request gestartet. Cloud Run kann den Container beenden bevor der Thread Sonnet-Call fertig hat → Q&A-Antwort verloren.

### Fix Plan (Option später)
- Im cloud_tasks-mode: `_qa_async_aerotax` per Cloud Task statt Thread enqueueren
- Oder: synchron im request, mit kürzerem Sonnet-Timeout

### Tests Required
- [ ] `test_qa_ask_returns_without_blocking_main_response`
- [ ] `test_qa_async_completes_under_container_lifetime`

---

## BUG-009 — Auto-Resume bypasst v14 PDF/State-Gate

| Feld | Inhalt |
|---|---|
| **ID** | BUG-009 |
| **Titel** | `_autoResume` in index.html:6307 ruft `window.render({...rd, download_url, notes})` — gibt **canonical_state nicht weiter**. `deriveUiState` sieht `cs='unknown'`, kein State-Match, kein Banner. User landet auf Result-Panel ohne State-Kontext. |
| **Area** | Frontend Auto-Resume bei Reload |
| **Severity** | P0 — User mit needs_review-Token reloadet → keine „kurze Klärung nötig"-Banner-Info |
| **Reporter** | Phase 2 Bug-Hunt Code-Inspection 2026-05-12 |
| **Status** | `open` |

### Repro Steps
1. User mit `canonical_state=needs_review`-Token öffnet die Seite
2. localStorage hat `aerotax_session.token` gespeichert (vorheriger Besuch)
3. `_autoResume` fired (Z. 6277)
4. `fetch /api/session/<token>` → response hat `canonical_state='needs_review'`, `result_data`, `download_url`
5. `_autoResume` ruft `window.render({...rd, download_url, notes})`
6. `deriveUiState(d)` sieht: `cs = d.canonical_state || 'unknown'` → `cs='unknown'` (canonical_state war nicht in `d`)
7. **Beobachtet:** Kein „Auswertung vorbereitet — kurze Klärung nötig"-Banner, kein Review-Hint
8. PDF-Buttons sind korrekt versteckt (canShowPdfDownload prüft canonical_state !== 'done' → false → KEIN PDF) — **dieser Teil ist safe**
9. ABER: Detail-Tabelle mit netto-Betrag wird trotzdem gerendert (kein `show_final_amount`-Check im render-Pfad)

### Evidence
**Code-Inspection index.html:6307:**
```js
window.render({...rd, download_url: j.download_url, notes: j.notes || []});
// MISSING: canonical_state, pdf_allowed, reason_code, _review_items
```

**deriveUiState index.html:1668:**
```js
var cs = s.canonical_state || 'unknown';
// 'unknown' matched keinen Branch → default state, kein Banner
```

**show_final_amount wird nie gelesen** (nur gesetzt) — auch ein eigener Bug, aber bei korrektem state-Pass wäre Banner mindestens „Klärung nötig".

### Root Cause Status: **BEWIESEN**

### Fix Plan
Edit `_autoResume` Z. 6307:
```js
window.render({
  ...rd,
  download_url: j.download_url,
  notes: j.notes || [],
  canonical_state: j.canonical_state,
  reason_code: j.reason_code,
  pdf_allowed: j.pdf_allowed,
  result_stale: j.result_stale,
  document_health: j.document_health,
  _review_items: rd._review_items || j.review_items,
  user_title: j.user_title,
  user_message: j.user_message,
  next_actions: j.next_actions,
});
```

Plus: `hasResult`-Check verfeinern. Bei `canonical_state='needs_review'` mit `rd.netto>0` → NICHT als "fertig" behandeln, sondern erst route nach state.

### Tests Required
- [ ] `test_auto_resume_passes_canonical_state_to_render` (statisch+DOM)
- [ ] `test_auto_resume_needs_review_shows_review_banner` (DOM)
- [ ] `test_auto_resume_failed_support_shows_support_state` (DOM)

### Browser Proof Required
- [ ] Token `AT-89080734B3FDC191` (needs_review) → Reload → Banner „kurze Klärung nötig" sichtbar

---

## BUG-010 — `show_final_amount` wird gesetzt aber nirgendwo geprüft

| Feld | Inhalt |
|---|---|
| **ID** | BUG-010 |
| **Titel** | `deriveUiState.show_final_amount` ist die Single-Source-of-Truth ob ein finaler Betrag dem User gezeigt werden darf. Aber der render()-Pfad liest es NICHT — netto/brutto werden bedingungslos in DOM geschrieben. |
| **Area** | Frontend Result-Render |
| **Severity** | P1 — bei processing/needs_review/failed_* könnte ein Betrag groß sichtbar sein der nicht final ist |
| **Reporter** | Phase 2 Bug-Hunt 2026-05-12 |
| **Status** | `open` |

### Evidence
```bash
grep -nE "show_final_amount|_uiState\.show_final" index.html
1683:    show_final_amount:  false,
1718:    out.show_final_amount = false; // NIE final bei needs_review
1730:    out.show_final_amount = true;
3581:    ... show_final_amount:false ...
```

4 Vorkommen — alle in Setter-Position. **Kein Reader.**

### Fix Plan
Im `render()` Pfad (index.html:3566+):
```js
var amountEl = document.getElementById('result-netto-big');
if(amountEl){
  if(_uiState.show_final_amount){
    amountEl.textContent = formatEUR(d.netto);
    amountEl.style.display = '';
  } else {
    // bei needs_review etc.: dimmen oder als „vorläufig" markieren
    amountEl.textContent = formatEUR(d.netto) + ' (vorläufig)';
    amountEl.style.opacity = '0.6';
  }
}
```

Plus: Detail-Tabelle nur bei `show_detail_table=true`.

### Tests Required
- [ ] `test_render_hides_final_amount_when_show_final_amount_false`
- [ ] `test_render_shows_final_amount_when_done`
- [ ] `test_render_marks_amount_as_provisional_in_needs_review`

---

## Bug-Hygiene-Regeln (verbindlich)

1. **Status nur 4 Werte**: `open` / `in_progress` / `fixed_unverified` / `verified_closed`
2. **`verified_closed` nur mit Browser-Beweis** (Screenshot, Trace, oder explizite User-Bestätigung)
3. **Bei Bug-Closing: PR-Link + Beweis-Link im Eintrag**
4. **Wenn ein „fixed_unverified" 7+ Tage offen ist → P1 escalation**
5. **Neue Bugs immer hier eintragen, nicht in Chat erwähnen + vergessen**
