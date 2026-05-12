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

## Bug-Hygiene-Regeln (verbindlich)

1. **Status nur 4 Werte**: `open` / `in_progress` / `fixed_unverified` / `verified_closed`
2. **`verified_closed` nur mit Browser-Beweis** (Screenshot, Trace, oder explizite User-Bestätigung)
3. **Bei Bug-Closing: PR-Link + Beweis-Link im Eintrag**
4. **Wenn ein „fixed_unverified" 7+ Tage offen ist → P1 escalation**
5. **Neue Bugs immer hier eintragen, nicht in Chat erwähnen + vergessen**
