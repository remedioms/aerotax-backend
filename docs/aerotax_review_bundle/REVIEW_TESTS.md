# AeroTAX — Test Inventory

**Total backend tests:** 1358 (post BH-003a)
**Status:** All green (1358/1358)
**Frontend tests:** 31/31 green (Node.js + jsdom)

---

## Recent BH-Bugs (with tests)

### BH-001 — Review-Question Marker-Semantik
File: `tests/test_bh001_review_question_marker_semantics.py` (9 tests, all green)

| Test | Asserts |
|---|---|
| `test_of_marker_with_passive_ai_skips_review` | KI conf≥0.90 + semantics=passive → no review item |
| `test_of_marker_with_active_ai_creates_marker_semantics_question` | conf≥0.70 → item with suggested_answer + new question form |
| `test_of_marker_low_confidence_falls_back_to_semantics_question` | conf<0.70 → marker-semantics question (not 8h-symptom) |
| `test_ki_call_includes_crew_context` | Phase-7 invariant: prompt contains „Crew/Airline/Cockpit/Lufthansa" |
| `test_ortstag_still_skipped_silent` | regression: ORTSTAG never reaches AI path |
| `test_frs_lmn_also_skipped_silent` | regression: FRS/LMN_AS/LMN_CR silent skip |
| `test_unknown_marker_still_works_with_ai_call` | regression: Phase-6 unknown_marker path intact |
| `test_multiple_office_candidates_each_get_ai_call` | each candidate gets AI call |
| `test_ai_returns_amount_field_is_sanitized` | Anti-Tax-Sanitizer: forbidden value-keys → review-fallback |

### BH-003a — Issue-Heimkehr → Z76 An/Ab
File: `tests/test_bh003a_issue_return_day_z76.py` (12 tests, all green)

| Test | Asserts |
|---|---|
| `test_bh003a_2025_01_06_issue_return_day_becomes_z76_abreise` | Bangalore-Tour 01-06: Issue → Z76 |
| `test_bh003a_uses_prev_layover_ort_blr_for_bmf_india` | BMF-Land = Indien (from prev BLR) |
| `test_bh003a_requires_duty_480` | duty<480 → no Z76 |
| `test_bh003a_requires_routing_from_layover_to_homebase` | routing[0]≠prev.layover → no Z76 |
| `test_bh003a_requires_ends_at_homebase` | ends_hb=False → no Z76 |
| `test_bh003a_does_not_apply_to_2025_05_23_duty_330` | duty 330<480 → bleibt Issue |
| `test_bh003a_does_not_apply_to_2025_06_03_duty_465_and_not_homebase_end` | routing endet LHR + duty<480 → bleibt Issue |
| `test_bh003a_does_not_apply_to_2025_10_28_duty_280` | duty 280<480 → bleibt Issue |
| `test_bh003a_does_not_apply_to_x_marker_without_routing_2025_01_04` | X-marker → Frei-branch, BH-003a-guard nie erreicht |
| `test_bh003a_does_not_change_hotel_count` | ends_hb=True → kein Hotel |
| `test_bh003a_no_double_count_tage_detail` | 01-06 nur 1× im output |
| `test_bh003a_issue_count_reduced_by_one` | Issue-list für Bangalore-Tour leer |

---

## Phase 1-7 Tests (Audit-Cycle-Refactor, alle grün)

| Phase | File | Tests | Coverage |
|---|---|---:|---|
| 4 — Layover-Place-Inferenz | `tests/test_phase4_layover_place.py` | 11 | routing-endpoint cascade, SE-stfrei fallback, prev/next-day, AI-resolver layover_place |
| 5 — Review-Item-Schema | `tests/test_phase5_review_schema.py` | 7 | source_type, source_excerpt, why_not_resolved, suggested_answer, confidence, affected_days |
| 6 — Marker-Gruppierung | `tests/test_phase6_marker_grouping.py` | 9 | same-marker N-days → 1 grouped item, AI marker_semantics call, conf-threshold for suggested |
| 7 — SE-Inland-Audit | `tests/test_phase7_inland_stfrei_audit.py` | 4 | Standby/ZeroDay with SE-Inland-14€ → audit-note (AG-Erstattung), not vma_unmapped |

---

## State-Machine / API-Contract Tests

| File | Tests | Coverage |
|---|---:|---|
| `tests/test_auto_resume_state_pass.py` | 11 | `_autoResume` passes canonical_state, pdf_allowed, reason_code, review_items, user_message, next_actions, document_health to render() |
| `tests/test_failed_state_ui_gate.py` | 8 | render()-non-done-gate hides amount/details/PDF on failed/fetch_error/expired/deleted |
| `tests/test_recall_debug.py` | 31 | recall-flow debug-stepper, hard-reset-timer, finally-block button-reset, AbortError friendly |
| `tests/test_cas_silent_fail_p0_75.py` | 5 | CAS-reader failure isn't silent — produces UNRESOLVED issue |
| `tests/test_recovery_secret_p0_10.py` | 16 | RECOVERY_SECRET required, HMAC tokens, no plaintext in logs |
| `tests/test_payment_intent_lock_p0_96.py` | 20 | Supabase atomic-claim, multi-container-safe, no double-spend |
| `tests/test_upload_persist_p0_90.py` | 20 | Pre-PI-consume persist; structured 503 with reason_code=UPLOAD_PERSIST_FAILED |
| `tests/test_pii_persist_p0_95.py` | 6 | session-cleanup deletes uploaded_files but keeps result_data 24h |

---

## Frontend Tests (Node.js + jsdom)

Location: `~/Desktop/site/test_*.mjs`

| File | Tests | Coverage |
|---|---:|---|
| `test_normalize_state.mjs` | 12 | `_normalizeBackendState` — canonical_state derivation from result_data |
| `test_state_machine.mjs` | 19 | `_hardHideResultSections` + `_failedStateLocked` + render-lock |
| `test_live_qa.mjs` | 18 | Live-deployed `aerosteuer.de` + live `/api/session/` integration |
| `test_live_state_machine.mjs` | 17 | Live state-machine against deployed frontend + Cloud Run backend |

---

## Reference-Contract Tests

| File | Test | Coverage |
|---|---|---|
| `tests/test_calculation.py::test_v89_reference_contract_constants_present` | Asserts REFERENCE_CONTRACT_2025_MIGUEL constants (User Miguel's golden, NOT Tibor's) |
| `tests/test_e2e_tibor_pipeline.py::test_align_failure_sets_red_health` | If FollowMe-Align crashes → document_health=red, `_followme_align_failed` in classification |
| `tests/test_e2e_tibor_pipeline.py::test_pdf_blocked_if_align_failed` | `/finalize-pdf` blocks on failed_support via state-machine |

---

## Not (yet) tested

| Gap | Impact | Priority |
|---|---|---|
| Tibor-Golden-Acceptance integration test (full pipeline → Golden compare) | High — would catch all 30+ misclassified days | P0 |
| `_followme_identify_tours` tour-boundary detection (Standby-during-Tour) | Medium — would catch BH-003e (9 days lost) | P1 |
| X-marker + active foreign tour-cluster detection | High — BH-003c (15 days lost) | P0 |
| Multi-stop foreign tours with inland-layover-Stops | Medium — BH-004 hotel +12 cases | P1 |
| BMF day-type rate matrix (Volltag vs An/Ab per country) | Medium — #228 F5 z76-€ residual | P2 |
| Live-PDF-generation round-trip | Low — already smoke-tested manually | P3 |
| Frontend visual-regression (Playwright/Cypress) | Low | P3 |
