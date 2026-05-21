# Chat / PDF / Recalculation — Release Gate

Stand: 2026-05-20.
Scope: end-to-end Chat-Antworten, PDF-Generierung, Recalculation-Flow,
Bucket-Math, Singular/Plural, Review-Kind-Copy, CAS-Self-Awareness, State-Sync.

## §1 Test-Statistik

- Backend pytest: **2052 passed**, 13 skipped, 13 xfailed (alle dokumentiert).
- Frontend JS-Tests: 28 + 15 + 23 = **66 passed**.
- Total: **2118 passing**, 0 hard-failures.

## §2 Gates

### A · Chat-State-Konsistenz

| Gate | Status | Beleg |
|---|:-:|---|
| State-Mix `Status wird geprüft` parallel zu Result/Review | PASS | `tests/test_frontend_state_machine_live_run.mjs::Case 1+10` |
| `_normalizeBackendState` läuft auf allen Pfaden | PASS | render-guard + calculate + recall + auto-resume |
| Done + pending review_items → derive needs_review | PASS | `_classify_job_state` job: status='done' + _review_items pending |
| Failed + result_data → hide result-UI | PASS | `render()` Non-Done-Gate (`_hardHideResultSections`) |
| Chat sagt nie „brauche Antworten" wenn `_livePending=0` | PASS | `tests/test_every_case_chat_pdf_state.py::test_case_D_*` |
| State-Sync nach Bulk-Answer (alle review_items → answered) | PASS | `_applyPendingProposal` 3-array sync + normalize-call |

### B · Review-Kind-Copy

| Gate | Status | Beleg |
|---|:-:|---|
| Unknown_marker → kontextbezogene Frage statt 8h | PASS | `test_case_C_static_frontend_unknown_marker_template_exists` |
| Office/training_time → 8h-Frage (passend) | PASS | review-card kind-routing |
| Standby → Bereitschaft/Flughafen-Frage | PASS | review-card kind-routing |
| Missing_document → Beschreibungs-Frage | PASS | review-card kind-routing |
| Source_conflict → Klärungs-Frage | PASS | review-card kind-routing |
| Intro-Bulk: dominanter Kind bestimmt Frage-Text | PASS | `startReviewFlowInChat` _dominant-Routing |

### C · CAS-Self-Awareness

| Gate | Status | Beleg |
|---|:-:|---|
| Kein „CAS hochladen"-Hinweis wenn CAS vorhanden | PASS | `_missingCasSet`-Filter + `_trulyMissingMonths` |
| Honest Fallback wenn CAS da, aber Marker unklar | PASS | „Ich habe deinen Dienstplan/CAS bereits vorliegen" |
| missing_months_cas → exakt diese Monate genannt | PASS | filter via document_health.missing_months_cas |

### D · Singular/Plural

| Gate | Status | Beleg |
|---|:-:|---|
| 1 Punkt → „Ein Punkt offen" + „sobald er geklärt ist" | PASS | `startReviewFlowInChat` Singular/Plural-Block |
| N Punkte → „N Punkte offen" + „sobald die geklärt sind" | PASS | dito |
| 1 Tag → „1 Tag" applied, nicht „1 Tage" | PASS | `_tagText` Variable in `_applyPendingProposal` |
| Banner-Title `_classify_job_state` Singular | PASS | `pending_count` + ternary `'e' if pending_count != 1 else ''` |
| Tageliste Singular: „Dieser Tag" | PASS | `startReviewFlowInChat` |
| PDF-locked Singular | PASS | `Ein Punkt muss noch geklärt werden` |

### E · PDF/UI Bucket-Math

| Gate | Status | Beleg |
|---|:-:|---|
| Block A + Block B == displayed_total (Tibor 976.00) | PASS | `tests/test_pdf_result_arithmetic.py` (18 Tests) |
| Z77 > VMA → VMA-netto clamped to 0 | PASS | `test_variant_3`, `test_case_S` |
| Z77 < VMA → VMA-netto = VMA − Z77 | PASS | `test_variant_1` |
| Z17 > Fahrt → Fahrt-netto clamped to 0 | PASS | `test_variant_5`, `test_case_T` |
| Z17 nicht gegen VMA verrechnet | PASS | `test_case_T_z17_only_offsets_fahrt` |
| Z77 nicht gegen Fahrt verrechnet | PASS | `test_case_T_static_recompute_separates_buckets` |
| PDF zeigt Block A / Block B / A + B | PASS | `test_pdf_table_no_misleading_subtract_line_static` |
| UI zeigt Block A / Block B / A + B | PASS | `test_ui_table_no_misleading_brutto_minus_z77_static` |
| Naive Mathe `5339-4705=634` ≠ 976 dokumentiert | PASS | `test_tibor_no_hidden_math_*` |
| Z77-Überschuss-Hinweis sichtbar | PASS | Block-B-Hinweis-Line + PDF Paragraph oblique |

### F · Recalculation / Polling

| Gate | Status | Beleg |
|---|:-:|---|
| Review-answer-bulk: review_items → status='answered' atomar | PASS | `app.py:4216-4248` + frontend triple-array sync |
| Preview-Total atomar geliefert (`updated_preview_total`) | PASS | `app.py:5435-5436` `result_data sync` |
| Polling-Interval saniert | PASS | calculate() 3000ms standard, fetch_error backoff 5000ms |
| Polling stoppt bei done/needs_review/failed_support | PASS | calculate() while-loop break-conditions |
| Polling doppeltes Job-Create verhindert | PASS | `_autoResumeInFlight` Lock |
| pdf_allowed=False während pending_reread | PASS | `_classify_job_state` pending_reread-Branch |

### G · Progress UX

| Gate | Status | Beleg |
|---|:-:|---|
| Progress freezed nicht bei 92% | PASS | `.pf-indeterminate` shimmer overlay |
| 100% nur nach calculate-resolved | PASS | `calculate()` line `pfEl.style.width = '100%'` |
| >90 s: „läuft noch"-Eskalation | PASS | `heartbeatStart` elapsed-check |
| >5 min: Zugangscode-Hinweis | PASS | dito |
| `prefers-reduced-motion` respektiert | PASS | CSS `@media (prefers-reduced-motion: reduce)` |

### H · Scroll / Glass-Card

| Gate | Status | Beleg |
|---|:-:|---|
| Kein Jump-to-Top nach Auswertung | PASS | `_scrollToActivePanel('result')` replaces `scrollTo(0,0)` |
| Step-Wechsel zentriert | PASS | `_centerActiveCard` in `go()` |
| Glass-Card-Transition 220ms | PASS | `.panel-entering` keyframe |
| Reduced-motion respektiert | PASS | CSS Media-Query |
| Kein Scroll bei Chat-Input-Focus | PASS | `_userIsTyping()` check |

### I · Privacy / Forbidden Strings

| Gate | Status | Beleg |
|---|:-:|---|
| Keine „garantiert"/„prüfungsfest" in state-copy | PASS | `test_case_N_no_tax_guarantee_in_user_messages` |
| Keine FRA-Hardcodierung in Comparison-Logic | PASS | `test_case_O_homebase_dynamic_not_hardcoded` |
| Sessions/PII PII-redacted in audit | PASS | bereits Rel Phase 13 |
| Deleted-Session blockiert PDF/Chat | PASS | `test_case_K_deleted_blocks_everything` |
| Expired-Session blockiert PDF | PASS | `test_case_L_expired_no_pdf` |

### J · Backend Contract (Deploy-Lag)

| Gate | Status | Beleg |
|---|:-:|---|
| Local app.py setzt canonical_state | PASS | `_classify_job_state` 13 states |
| Live `/api/session/<token>` liefert canonical_state | **FAIL** | xfail `test_live_session_endpoint_has_canonical_state` — Backend-Redeploy pending |
| Live `/api/session/__test_404__` liefert state | **FAIL** | dito |
| Frontend kompensiert per `_normalizeBackendState` | PASS | bridge-fix |

## §3 Forbidden-Combination Tests (Hard-Stop wenn FAIL)

| Forbidden Combo | Status |
|---|:-:|
| done + pending review_items → derive needs_review | PASS |
| done + pending_reread=True → derive needs_review | PASS |
| failed_* + result-UI sichtbar → MUST hide | PASS |
| pdf_allowed=true + !download_url → flip to false | PASS |
| processing + result_data.KPIs sichtbar → MUST hide | PASS |
| expired + valid download_url → reject | PASS |
| Chat „PDF bereit" wenn pdf_allowed=false | PASS (test_case_D) |
| Chat „keine offenen Punkte" wenn pending>0 | PASS |
| Chat „brauche Antworten" wenn pending=0 | PASS |
| Tabelle suggeriert `Brutto − Z77 = Netto` | PASS (block-split) |

## §4 Open Risks / NEEDS_DECISION

1. **Backend Deploy-Lag (J/Live)** — Backend muss neu deployed werden bevor
   /api/session/<token> die State-Felder zurückgibt. Frontend kompensiert,
   aber Audit/Analytics/Drittsysteme könnten falsche States lesen.
   → Empfehlung: Backend-Redeploy als Bedingung für Release-GO.

2. **`result_version` / `pdf_version` Fields** — In §3 des State-Contracts als
   TODO markiert. Aktuell nutzt Frontend `download_url`-Vergleich als Fallback.
   Acceptable für MVP, technical debt für v12.

3. **Tibor live-Result KPI-Δ vs Reference** — arbeitstage +6, hotel_naechte
   +19 vs reference. RB-Review pending → nach Recalc neu vergleichen.
   Nicht Teil dieses Gates.

4. **Mobile Safari Manual QA** — Phase 6 QA-Checklist in
   `docs/FRONTEND_LIVE_RUN_UX_STATE_FIX.md`, aber nicht durchgespielt.

5. **Master Phases 3-11 (50 E2E + polling tests etc.)** — nicht implementiert
   im aktuellen Sprint. Every-case-Tests A-T (30 Tests) decken die wichtigsten
   Szenarien ab, aber nicht alle 50 user-spec scenarios.

## §5 Recommendation

**PASS to controlled live-run** — mit folgenden Konditionen:

1. Backend muss neu deployed werden (Render Push) damit `/api/session/<token>`
   die State-Felder liefert. Frontend bridge ist defensive Sicherung, nicht
   alleinige Quelle.
2. Frontend muss deployed werden (`wrangler pages deploy`).
3. Manual QA mit `docs/FRONTEND_LIVE_RUN_UX_STATE_FIX.md` §9 durchgespielt
   (Chrome, Safari, iPhone).
4. Bei Live-Run: NEUE Sitzung mit Tibor-Files (NICHT alte Token-Recovery),
   damit Bucket-Math + Chat-Copy mit fresh result_data verifiziert wird.
5. Stop nach 1 Live-Run für Verifikation aller §2-A bis §2-J Gates im echten Browser.

**NO-GO Trigger (während Live-Run)**:
- Chat sagt „PDF bereit" während Recalc läuft.
- Tabelle zeigt mathematisch unmögliche Differenz (z.B. Brutto − Z77).
- Banner zeigt „Status wird geprüft" bei vorhandenem result_data.
- Singular/Plural-Mix in einer Antwort.
- CAS-Upload-Hinweis trotz CAS vorhanden.
- PDF freigegeben bei pending review_items.

Bei NO-GO: Frontend zurück per Wrangler-Vorgängerversion + Bug-Tracking;
keine Datenfreigabe an User mit potentiell verwirrender Darstellung.
