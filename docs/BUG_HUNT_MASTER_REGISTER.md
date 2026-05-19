# Bug-Hunt Master Register

Phase A — Inventory + Static Scan. Stand: 2026-05-15.

Schema pro Eintrag:
- **ID** · Titel · Ebene · Severity · Status · Owner/Phase
- **User Impact** · **Repro** · **Beweis** · **Root Cause (Hypothese)** · **Fix Plan** · **Tests Required** · **Deploy Required** · **Verification** · **Regression Risk**

Status-Werte: `open` / `investigating` / `fixed_unverified` / `verified_closed` / `false_positive`

> ⚠️ Kein Bug darf `verified_closed` werden ohne Repro + Root-Cause + Fix + Test + Live-Beweis.

---

## P0 — Sofort

### BH-001 · Review-Question fragt Symptom statt Ursache (ORTSTAG/OF >8h)
- **Ebene:** 7 (KI-Resolver / Review-Quality)
- **Severity:** P0 (User-vertrauen, falsche Antworten möglich)
- **Status:** open
- **Owner/Phase:** Phase E — Review Quality
- **User Impact:** User AT-C33E6274D260FC78 sieht „⚠️ einen Punkt offen — 19.12. — Einzeltage. Frage: warst du an diesem Tag inkl. Hin-/Rückweg länger als 8h weg?". Tag ist Marker `OF` (Office/Schulung an Homebase). User-Aussage: „ein ortstag.. unadmissable!". User wird gefragt was die KI aus dem Marker hätte ableiten können.
- **Repro:** `https://aerosteuer.de` mit AT-C33E… → Chat-Frage zeigt 8h-Symptomatik
- **Beweis:**
  - `result_data._office_training_time_missing_candidates`: `[{'activity_type':'office','datum':'2025-12-19','marker':'OF','money_impact_estimate':14.0,'reason':'Office/Schulung an Homebase ohne Zeitinfo'}]`
  - `_review_items[0].question` enthält „Warst du inklusive Hin-/Rückweg länger als 8h"
  - app.py `_build_review_items` baut Fragen-Template für office_training_time_missing als 8h-Symptom-Frage statt Marker-Semantik-Frage
- **Root Cause (Hypothese):** Review-Builder hat einen generischen Template für `office_training_time_missing`, der unabhängig vom Marker-Typ die 8h-Frage stellt. KI-Resolver `kind='cas_time_extraction'` wird nicht **vor** der Fragebildung versucht (oder nicht für OF-Marker). ORTSTAG/OF sollten passive-Marker sein → silent-skip, nicht Review.
- **Fix Plan:** (a) Marker-Semantik-Resolver `marker_semantics` für `OF` vor `office_training_time_missing` rufen — wenn `office_passive` → skip Review; (b) Wenn aktiv, Frage umformulieren auf „Was bedeutet OF bei dir — Bürodienst zuhause (passiv) oder Schulung mit Anreise?"; (c) Passive-Marker-Liste erweitern: `OF` in passive-skip-set wenn `office_passive_at_home` Semantics.
- **Tests:** `test_of_marker_skips_review_if_passive`, `test_review_question_asks_marker_meaning_not_8h`
- **Deploy:** Backend.
- **Verification:** Tibor-Token öffnen → Frage zeigt Marker-Semantik, nicht 8h-Frage.
- **Regression Risk:** mittel — Marker-Klassifikation könnte andere Tage betreffen.

### BH-002 · `/api/job/<id>` HTTP 502 / HTML-Errorseite
- **Ebene:** 4 (Backend API Contract)
- **Severity:** P0 (Frontend nur fallback auf /api/session, single-point-of-failure)
- **Status:** investigating
- **Owner/Phase:** Phase D — Backend API Contract
- **User Impact:** Frontend kann Jobs nicht direkt abfragen; alle Recall-Pfade müssen über `/api/session` laufen. Falls Session-Token unbekannt → User stuck.
- **Repro:** `curl --max-time 25 https://aerotax-backend.onrender.com/api/job/e132976f-d0dd-4627-9d80-9782721602ba` → HTTP 502, 223 KB HTML.
- **Beweis:** Background-Task-Output `/tmp/.../b7f5dii1b.output`: `HTTP=502 bytes=223038`. Cloud-Run-Edge oder Worker-Crash.
- **Root Cause (Hypothese):** Route existiert (app.py:3327), aber 30s Cloud-Run-timeout oder Worker-OOM. Kein anderer Endpoint ist 502, also kein globaler Issue. Möglicherweise lädt der Job aus Supabase volle `result_data` (90+ Felder, _tage_detail 365 Einträge → ~400KB) und das verstösst gegen response-size-limit oder gegen Worker-Memory.
- **Fix Plan:** (a) Memory-Trace pro Job-Load, (b) Response-Size-Limit prüfen, (c) Slim-Response statt vollem _tage_detail (paginated/optional).
- **Tests:** `test_job_endpoint_returns_json_under_30s`, `test_job_502_regression`
- **Deploy:** Backend.
- **Verification:** 10× `/api/job/<id>` Probes → alle 200 + JSON in <10s.
- **Regression Risk:** niedrig.

### BH-003 · Arbeitstage +7 / Reinigung +7 (8 Issue-Tage als „Heimkehr")
- **Ebene:** 6 (Calculation / Classifier)
- **Severity:** P0 (User sieht falsche Zahlen)
- **Status:** investigating
- **Owner/Phase:** Phase F — Calculation Forensics
- **User Impact:** Tibor sieht +7 Arbeitstage, +7 Reinigung. Beeinflusst Pauschbetrag-Schwelle und damit Gesamtbetrag.
- **Repro:** `e132976f` → `_klass_summary.arbeitstage=140` (Golden=133, Δ+7).
- **Beweis (TIBOR_DIFF_FORENSICS.md §3):** 8 Tage haben `klass='Issue'` mit Reason „Heimkehr aus Vortag-Tour — separater Tour-Abschluss":
  - 2025-01-04 X BLR, 2025-01-06 X FRA, 2025-03-26 ==, 2025-04-02 ==, 2025-05-23 LAD, 2025-06-03 SOF, 2025-10-28 TLV, 2025-12-16 X JFK
- **Root Cause (Hypothese):** Tour-Cluster-Logik markiert den Heimkehr-Tag als separater Issue-Tag statt ihn dem Vortag-Cluster anzuhängen. Issue-Tage zählen aktuell als Arbeitstag (vermutlich) — sollte aber als Z73/Z74/Z76-Heimkehr klassifiziert werden im selben Tour-Cluster.
- **Fix Plan:** Klassifikator-Logik `_build_tour_clusters` muss Heimkehr-Tag (routing zeigt Rückflug zum Homebase, Vortag war Tour-Layover) als Tour-Abschluss innerhalb des Vortag-Clusters klassifizieren — Z73/Z74/Z76 An/Ab statt Issue.
- **Tests:** `test_tour_heimkehr_day_classified_as_z76_an_ab_not_issue`, `test_tibor_workday_diff_list_exact`
- **Deploy:** Backend.
- **Verification:** Tibor-Re-Run zeigt arbeitstage=133, Issue=0 für Heimkehr-Tage.
- **Regression Risk:** hoch — Tour-Cluster-Logik berührt mehrere Klassifikations-Pfade.

### BH-004 · Hotelnächte +12 (Layover-Inferenz zu großzügig)
- **Ebene:** 6 (Calculation / Classifier)
- **Severity:** P0 (Zahlen falsch)
- **Status:** investigating
- **Owner/Phase:** Phase F — Calculation Forensics
- **User Impact:** +12 Hotelnächte → mehr Z76-Volltag-Tage → eigentlich höherer Z76, aber durch Issue-Tage Verlust.
- **Repro:** `e132976f` → `_klass_summary.hotel_naechte=78` (Golden=66, Δ+12).
- **Beweis (TIBOR_DIFF_FORENSICS.md §4):** Mehrere Inland-Layover-Tage sind als Z76 mit Hotel klassifiziert: 06-23 LIN, 06-24 BER, 11-01 LEJ. Plus Phase-4 `routing_endpoint`-Rescues könnten zu großzügig laufen.
- **Root Cause (Hypothese):** Phase-4 Layover-Place-Inferenz (routing[-1]) inkludiert auch Tage wo routing nur Inland-Codes hat. Plus Inland-Layover-Detection (DUS/MUC/LEJ/BER/LIN) wird teilweise als Z76 statt Z73 klassifiziert wegen CAS-Foreign-Override.
- **Fix Plan:** Layover-Inferenz darf Inland-Layover nicht zu Z76 hochstufen. Cluster-C2 (CAS-Foreign-Override) muss strikter: nur wenn CAS klares Auslands-Layover zeigt, nicht jedes routing-Endpoint.
- **Tests:** `test_inland_layover_stays_z73_not_z76`, `test_hotel_inference_not_too_aggressive`, `test_tibor_hotel_diff_list_exact`
- **Deploy:** Backend.
- **Verification:** Tibor-Re-Run zeigt hotel=66.
- **Regression Risk:** mittel — könnte echte Auslands-Layover als Inland fehl-klassifizieren.

### BH-005 · z76 −357 € / z73 −3 / z74 −1 (BMF-Tagesätze + falsche Tour-Klassifikation)
- **Ebene:** 6 (Calculation / Classifier)
- **Severity:** P0 (Gesamtbetrag −400€)
- **Status:** open
- **Owner/Phase:** Phase F — folgt nach BH-003 + BH-004
- **User Impact:** Tibor sieht −400€ vs Golden.
- **Repro:** e132976f → z76_eur=4437 (Soll 4794, Δ−357); z73=8 (Soll 11, −3); z74=0 (Soll 1, −1).
- **Beweis (TIBOR_DIFF_FORENSICS.md §5):** _missing_z73_candidates zeigt 09-26 MUC + 09-27 DUS (durch CAS-Override zu Z76 → Z73 verloren). Vermutete weitere Verluste durch BH-003 (Issue-Tage statt Z73/Z74 für Heimkehr-Inland).
- **Root Cause (Hypothese):** Zwei Effekte: (a) BMF-Tagesätze für An/Ab-Tage vs Volltag pro Land könnten anders gegenüber Golden sein (Tibor-spezifische F5/#228). (b) Inland-Heimkehr-Tage werden als Issue statt Z73/Z74 klassifiziert (Folge von BH-003).
- **Fix Plan:** Erst BH-003 + BH-004 fixen, dann z76-Restdiff diagnostizieren. Wenn Restdiff bleibt → F5/#228 (BMF-Tagesätze nach FollowMe-Logik).
- **Tests:** `test_z73_z74_not_overridden_by_wrong_foreign_context`, `test_z76_day_type_matches_golden_for_known_days`
- **Deploy:** Backend.
- **Verification:** Tibor-Re-Run gesamt=6021 ±2€.
- **Regression Risk:** mittel.

---

## P1 — Strukturell

### BH-006 · `/api/session/<token>` liefert canonical_state=null
- **Ebene:** 4 (API Contract)
- **Severity:** P1 (Workaround via Normalizer existiert, aber Backend sollte source-of-truth sein)
- **Status:** investigating
- **Owner/Phase:** Phase D — Backend API Contract
- **User Impact:** Frontend muss aus result_data raten. Wenn Normalizer-Logik buggy → falsche UI.
- **Repro:** `curl /api/session/AT-…` → response hat `canonical_state: null` (auch bei needs_review/done).
- **Beweis:** AT-C33E…: keys=`['download_url','expires','job_id','notes','result_data','token']` — keine state-Felder.
- **Root Cause (Hypothese):** Endpoint app.py:5845 baut Response ohne `canonical_state`/`user_title`/`user_message`/`next_actions`/`pdf_allowed`/`review_items` top-level. Funktion `_classify_job_state` existiert aber wird nicht in `/api/session` integriert.
- **Fix Plan:** `/api/session` muss `build_api_state_response(job)` rufen (gleicher Helper wie `/api/job`). Felder einbauen: canonical_state, status, reason_code, user_title, user_message, pdf_allowed, result_stale, document_health, fetch_error, next_actions, review_items (top-level).
- **Tests:** `test_session_contract_done`, `test_session_contract_needs_review`, `test_session_contract_failed`, `test_session_never_null_state_with_result`
- **Deploy:** Backend.
- **Verification:** `curl /api/session/AT-…` zeigt vollständigen Vertrag.
- **Regression Risk:** niedrig — Frontend tolerant (Normalizer fängt).

### BH-007 · Multi-Endpoint-Inkonsistenz im API-Contract
- **Ebene:** 4 (API Contract)
- **Severity:** P1
- **Status:** open
- **Owner/Phase:** Phase D
- **User Impact:** Polling vs Recall vs AutoResume können verschiedene Antworten kriegen.
- **Beweis:** 42 Routes (app.py), davon ~10 job-spezifisch. Frontend pollt via `/api/job` (siehe BH-002), recovered via `/api/session`. Wenn `/api/job` 502 → Normalizer-Fallback. Aber `/api/job/<id>/review-answer`, `/upload-replacement` etc. brauchen auch konsistente Response.
- **Fix Plan:** API_CONTRACT_MATRIX.md durchgehen, jeden Endpoint auf Pflicht-Felder prüfen.
- **Tests:** `test_api_contract_consistency` per Endpoint.
- **Deploy:** Backend.
- **Verification:** alle Endpoints liefern gleichen state-Vertrag.
- **Regression Risk:** niedrig.

### BH-008 · `arbeitstage` doppelte Aggregation (counted_as_workday=193 vs _klass_summary.arbeitstage=140)
- **Ebene:** 6 (Calculation)
- **Severity:** P1
- **Status:** investigating
- **Owner/Phase:** Phase F
- **User Impact:** Indirekt — wenn beide Counter verwendet werden, kann Inkonsistenz auftreten.
- **Beweis:** Tibor e132976f: `sum(t.classifier_result.counted_as_workday)=193`, `_klass_summary.arbeitstage=140`. Δ=53.
- **Root Cause (Hypothese):** `counted_as_workday` im Day-Level klassifiziert „dienstlich-aktive" Tage breit (alle Klassen außer Frei). `arbeitstage` im Summary nimmt eine spezifischere Subset (vermutlich nur VMA-relevante). Nicht „falsch" pro se, aber zwei Counter sind verwirrend.
- **Fix Plan:** Klar dokumentieren oder vereinheitlichen.
- **Tests:** `test_workday_counter_definition_documented`.
- **Regression Risk:** niedrig.

### BH-009 · Frontend Cache-Bust fehlt
- **Ebene:** 3 (Network / Cache)
- **Severity:** P1 (User-Reports „Status wird geprüft" trotz Deploy)
- **Status:** open
- **Owner/Phase:** Phase C — Network
- **User Impact:** Browser cached alte index.html → state-machine-fixes wirken nicht.
- **Beweis:** Cloudflare `cache-control: max-age=0, must-revalidate` ist gesetzt, aber Service-Worker oder Browser-Cache könnten weiterhin alte HTML zeigen. Aktuell kein `window.AEROTAX_FRONTEND_VERSION` Marker → kein User-sichtbarer Version-Check.
- **Fix Plan:** Version-Marker einbauen (z.B. Build-Date + Git-SHA als JS-Const). Optionaler Debug-Panel zeigt Version.
- **Tests:** `test_frontend_version_present`, `test_cache_bust_version_changes_on_deploy`
- **Deploy:** Frontend.
- **Regression Risk:** niedrig.

### BH-010 · Stripe-Webhook ohne idempotency
- **Ebene:** 8 (Security / Payment)
- **Severity:** P1
- **Status:** open
- **Beweis:** Bisher kein expliziter Test. (Phase H-Scan ausstehend)
- **Fix Plan:** Phase H Audit.

---

## P2 — Hardening

### BH-011 · Unknown-Marker „==" 6× — Reader-Bug
- **Ebene:** 7 (Reader)
- **Severity:** P2
- **Status:** open
- **Beweis:** Tibor: `==` als unknown_marker 6× (Frei-Marker). Reader sollte das als „Frei" erkennen, nicht als Unknown.
- **Fix Plan:** Reader-Marker-Lexikon `==` → activity_type='frei'.
- **Tests:** `test_double_equals_marker_recognized_as_frei`.

### BH-012 · 737 except/pass/print sites in app.py
- **Ebene:** 8 (Hardening)
- **Severity:** P2
- **Status:** open
- **Beweis:** `grep -cE '^\s*except|^\s*pass\s*$|print\(' app.py` = 737. Davon ein Teil legit (Defensiv-Logging), aber Audit ausstehend.
- **Fix Plan:** Phase H: Audit aller `except: pass` + `print()` ohne Logger.

---

## P3 — Backlog

Reserviert für Cleanup-Tasks die erst nach P0/P1-Stabilisierung sinnvoll sind.

---

## Verified-Closed (referenz)

- **BH-PRIOR-001** State-Mix Failed+Done (Frontend) — fixed in v15 state-machine-deploy 2026-05-15 (test_state_machine.mjs 19/19 + headless live QA 17/17). **Status: fixed_unverified** — User-Browser-QA mit AT-46C9... noch ausstehend.
- **BH-PRIOR-002** „Status wird geprüft" Dauerzustand — fixed via `_normalizeBackendState` 2026-05-15. **Status: fixed_unverified** — Browser-QA ausstehend.
