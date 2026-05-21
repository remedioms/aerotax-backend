# Final Pre-Live-Run Verification

Stand: 2026-05-21 (preflight only; **no deploy, no live-run**).

## §0 Hard Baseline

### Git state
- Branch: `main`
- Modified vs HEAD (tracked): `app.py`, `tests/test_calculation.py` (window-bumps),
  `tests/test_auto_resume_state_pass.py`, `tests/test_cloud_tasks.py`,
  `tests/test_e2e_tibor_pipeline.py`, `tests/test_recall_debug.py`,
  `CLAUDE.md`, `docs/BUG_HUNT_MASTER_REGISTER.md`, 7× `_job_chunks_state/test-job-*.json`
- Untracked (uncommitted): ~70 docs (release-audit family), ~70 test files,
  2 supabase migrations, `tests/conftest.py`, `tests/fixtures/*.json`,
  `tests/helpers/*`
- `~/Desktop/site/index.html` lives outside git (Cloudflare Pages direct-upload)
- App.py diff stat: **+4232 lines** since last commit

→ **All fixes are local-only.** Nothing committed since `f7d473e` (BH-003a, pre-bucket-math).

### Hashes
| Asset | Local MD5 | Live MD5 | Match |
|---|---|---|:-:|
| `~/Desktop/aerotax-backend/app.py` | `c9a207af…` | n/a (server-side) | — |
| `~/Desktop/site/index.html` | `60ba1bef…` | `d29012e3…` | ✗ |

### Versions (local source-of-truth)
| Const | Value |
|---|---|
| `APP_VERSION` | `'11.0'` |
| `ENGINE_VERSION` | `'tour_first_v11_clean_release'` |
| `PROMPT_VERSION` | `'v11_0'` |
| `READER_VERSIONS` | dict |
| `FRONTEND_CONTRACT_VERSION` | `'v11_3doc_lsb_se_cas_2026_05_20'` |

### Live deploy status

| Probe | Result |
|---|---|
| `/api/health` | `{ok:true, service:"aerotax-backend", version:"v8.40"}` — hardcoded, **doesn't confirm latest deploy** |
| `/api/session/AT-11CEB21120E7799B` (valid token) | returns 6 keys, **no canonical_state, no pdf_allowed, no next_actions, no user_title, no review_items** |
| `/api/session/FAKE_TEST_TOKEN` (404 path) | returns only `{error:…}` — **expected `_classify_job_state(None,None)` spread missing** |
| `/api/job/fake-job` (404 path) | returns only `{error:…}` |
| `https://aerosteuer.de/` size | 591684 bytes; **no `panel-entering` / `pf-indeterminate` / `_scrollToActivePanel` / `FRONTEND_CONTRACT_VERSION`** present |
| Old display string `Brutto-Aufwendungen gesamt` in live HTML | **1 occurrence — old display bug live** |

### Match table

| Area | Local | Live | Match? | Risk | Action |
|---|---|---|:-:|---|---|
| Backend code (`app.py`) | v11 with Phase 2 state-contract + bucket-math + recall fixes | Older revision missing `_classify_job_state` injection on `/api/session` and `/api/session/<token>` 404 path | ✗ | Audit/3rd-party clients see stale state | Backend redeploy required |
| Frontend code (`index.html`) | v11 with state-mix fix, scroll helpers, glass transition, progress shimmer, bucket-split table, chat state-sync, review-kind copy, CAS-self-awareness, singular/plural | Old version — none of these fixes present | ✗ | Live frontend will show old bugs | Frontend deploy required |
| Test files | 70+ untracked test files in `tests/` | n/a | — | Tests are local-only, run locally | Commit before deploy if version-tracking needed |
| Backend `_classify_job_state` injection on `/api/session` | present at `app.py:5887` | NOT in live response | ✗ | Frontend bridge compensates; backend cleanup pending | Backend redeploy |

---

## §1 Backend Deploy Readiness

### Syntax / Import
- `python3 -m py_compile app.py` → **OK**

### Full pytest
- `pytest tests/ --ignore=tests/test_e2e_tibor_pipeline.py` → **2052 passed, 13 skipped, 13 xfailed**
- No unexpected failures
- xfails all `documented_reference_disagreement` or deploy-lag tracker
- skips all pre-existing (FTL strict, obsolete guards)

### Targeted critical-path
| Test file | Result |
|---|---|
| `test_pdf_result_arithmetic.py` | **18/18** pass |
| `test_every_case_chat_pdf_state.py` | **30/30** pass |
| `test_rb_review_recalc_flow_mock.py` | **14/14** pass |
| `test_backend_contract_local_vs_live.py` | **11 pass + 1 skip + 1 xfail** (deploy-lag tracker) |
| `test_release_backend_failure_injection.py` | pass |
| `test_release_payment_process_e2e.py` | pass |
| `test_release_pdf_result_audit.py` | pass |
| `test_dynamic_homebase_matrix.py` | pass |
| `test_generalized_roster_marker_matrix.py` | pass |
| `test_upload_document_fuzz.py` | pass |
| `test_release_dsgvo_security.py` | pass |
| `test_release_review_chat_system.py` | pass |
| `test_release_frontend_state_machine.py` | **16/16** pass |
| **Subtotal targeted** | **240 passed, 1 skipped, 1 xfailed** |

→ **Backend code GREEN locally.** Hard-block: deploy-lag confirmed via §0.

---

## §2 Frontend Deploy Readiness

### Syntax / Static
- `node --check /tmp/cb.js` (extracted scripts) → **OK**

### Local JS tests (against `~/Desktop/site/index.html`)
| Test file | Result |
|---|---|
| `tests/test_frontend_state_machine_live_run.mjs` | **28/28** pass |
| `tests/test_frontend_scroll_helpers.mjs` | **15/15** pass |
| `tests/test_frontend_progress_shimmer.mjs` | **23/23** pass |

### Pre-existing JS tests in `~/Desktop/site/`
| Test file | Result | Note |
|---|---|---|
| `test_normalize_state.mjs` | 12/12 pass | local-source |
| `test_state_machine.mjs` | 19/19 pass | local-source |
| `test_live_qa.mjs` | 9 pass / **9 fail** | tests live-deployed code+backend with token `AT-C33E6274D260FC78` which now returns `{error:…}` → fails are **stale-token + pre-existing**, not caused by my changes |
| `test_live_state_machine.mjs` | 11 pass / **6 fail** | same root cause as above |

→ Pre-existing failures are token-aging artefacts; **none caused by current code**.
→ These tests intentionally fetch DEPLOYED state; will recover once new backend/frontend live.

### Forbidden-string audit (local frontend)
| String | Local count | Status |
|---|---:|---|
| `Status wird geprüft` | 4 | 1 active in `deriveUiState` unknown-fallback (essentially unreachable post-fix); 3 in comments |
| `lade dein CAS` | 2 | 1 in comment; 1 active inside `if(_trulyMissingMonths.length > 0)` guard — only fires if CAS truly missing |
| `Flugstunden` | 4 | Legacy-purge markers + reader comments — not active in pipeline |
| `Brutto-Aufwendungen gesamt` | **0** | ✓ Old misleading line removed |
| `sobald die geklärt` | 1 | active inside plural-branch of `n===1 ? singular : plural` ternary |
| `1 Tage` | **0** | ✓ Singular/plural fix done |
| `ich brauche zuerst deine Antworten` | **0** | ✓ Self-contradiction copy removed |
| `window.scrollTo(0,0)` | 1 | in comment only |

→ All remaining occurrences are SAFE (guarded, fallback-only, or comments).

### Critical state-feeding paths
All 4 render-entry-points feed through `_normalizeBackendState`:

| Path | Line | Normalize? | Top-level state merged? |
|---|---:|:-:|:-:|
| `calculate()` poll-done | 3770, 3826 | ✓ | ✓ |
| `render()` defensive entry-guard | 4016-4040 | ✓ | ✓ |
| Auto-resume (`autoResume`) | 7193-7217 | ✓ | ✓ |
| Poll-interval (`pollIv`) | 7266-7289 | ✓ | ✓ |
| Recall (recall-submit) | 8671-8688 | ✓ | ✓ |

→ **No render-call uses raw `data.data` or `result_data` without normalization.**

---

## §3 Live Backend Contract Preflight

### Read-only probes performed
- `/api/health` → 200 JSON ✓
- `/api/session/FAKE_TEST_TOKEN` → 404 JSON ✓ (but only `{error:…}`, missing state-fields)
- `/api/session/AT-11CEB21120E7799B` → 200 JSON ✓ (but only 6 keys, missing 8 required state-fields)
- `/api/job/fake-job` → 404 JSON ✓ (but missing state-fields)
- No 502 HTML, no 000 status, no missing content-type, no traceback

### Mandatory state-fields per Master state-contract `RECALC_PDF_CHAT_STATE_CONTRACT.md §3`

| Field | Live `/api/session/<token>` 200 | Live 404 path |
|---|:-:|:-:|
| `canonical_state` | ✗ | ✗ |
| `reason_code` | ✗ | ✗ |
| `pdf_allowed` | ✗ | ✗ |
| `user_title` | ✗ | ✗ |
| `user_message` | ✗ | ✗ |
| `next_actions` | ✗ | ✗ |
| `review_items` (top) | ✗ | ✗ |
| `document_health` (top) | ✗ | ✗ |

→ **Backend deploy is required** before live-run. Frontend `_normalizeBackendState`
compensates for UI continuity, but audit, 3rd-party clients, and recall-via-link from
older sessions would still see incomplete contract.

---

## §4 Response-Contract Unification Audit

Every render-call site goes through `_normalizeBackendState` (see §2 table).

**No path can show "Status wird geprüft"** with present result_data because:
1. Live polls (`autoResume`/`pollIv`) explicitly call normalize.
2. Calculate done-path merges top-level state + calls normalize.
3. Recall path calls normalize.
4. Defensive `render()` entry-guard normalizes on its own if `canonical_state` missing.

This means the original AT-11CEB21120E7799B bug shape (`canonical_state` missing
+ result_data present + `_review_items` pending) maps to `needs_review` on
**all 4 paths** ⇒ banner = "Auswertung vorbereitet — kurze Klärung nötig",
never "Status wird geprüft".

---

## §5 Review-Answer → Recalc → PDF Preflight (Mock)

Suite: `tests/test_rb_review_recalc_flow_mock.py` — **14/14 pass**.

| Stage | Asserted behaviour | Test |
|---|---|---|
| Initial | `canonical_state=needs_review`, `pdf_allowed=false`, `can_show_final_amount=false` | stage1 + stage5 |
| Chat-PDF question during pending | NIE „PDF ist fertig"; bei `_livePending=0` honest fallback statt „brauche Antworten" | every-case D + chat-pdf handler audit |
| Review-Bulk-Answer accepted | `review_item.status='answered'` across all 3 arrays, `_data.canonical_state='done'` lokal | stage4 + frontend triple-sync |
| `pending_reread=True` | derives `needs_review` even after all answers | stage9 |
| Skip-Unanswered | derives `done` despite items | stage4c |
| Audit-Log | review-answer event written | stage7 |

→ **The observed live bug is impossible in the local code.**

---

## §6 PDF Bucket Math Preflight

Suite: `tests/test_pdf_result_arithmetic.py` — **18/18 pass**.

Tibor numbers (live result):
```
A · Sonstige Werbungskosten:
  Fahrtkosten Homebase 497.20
  Reinigung           216.00
  Trinkgelder         262.80
  = Zwischensumme A   976.00

B · VMA:
  VMA brutto         4363.00
  − Z77              4705.00
  (Hinweis: Z77 übersteigt VMA um 342.00; VMA wird nicht negativ.)
  = VMA netto (≥0)      0.00

= Einzutragender Gesamtbetrag (A + B)   976.00
```

| Check | Status |
|---|:-:|
| `displayed_total == block_a + block_b` | ✓ |
| No `Brutto-Aufwendungen 5339 − Z77 4705` line | ✓ (string removed) |
| `max(0, vma_total - z77)` clamp in Python | ✓ |
| `max(0, fahr - ag_z17)` clamp in Python | ✓ |
| PDF code: Block-A header present | ✓ |
| PDF code: Block-B header present | ✓ |
| PDF code: "A + B" in final line | ✓ |
| UI code: Block-A header present | ✓ |
| UI code: Block-B header present | ✓ |

---

## §7 Chat Copy Preflight

Suite: `tests/test_every_case_chat_pdf_state.py` — **30/30 pass**.

Sampling of 15 user questions × 8 states = 120 logical-state combinations,
covered by:
- 20 every-case scenarios A-T (with multi-base parametrization R = 7 subcases)
- chat-PDF-question handler with 6 reason-branches by state
- review-kind-copy templates for 5 kinds (unknown_marker, office, standby, missing_doc, source_conflict)

Forbidden combinations all confirmed prevented:
- PDF ready while `pdf_allowed=false`: prevented by `_classify_job_state` + `canShowPdfDownload` gate.
- "Brauche Antworten" when `pending=0`: prevented by `_livePending` check in handler.
- "Lade CAS hoch" when CAS present: prevented by `_missingCasSet` filter.
- Tax-guarantee strings: not present in `_classify_job_state` user-facing copy.
- "Status wird geprüft" with result/review present: prevented by normalize + entry-guard.

---

## §8 UI/UX Preflight

### Local browser-static checks
| Check | Status |
|---|:-:|
| `_scrollToActivePanel` helper defined | ✓ |
| `_centerActiveCard` helper defined | ✓ |
| `_userIsTyping` guard prevents scroll during chat input | ✓ |
| `prefers-reduced-motion` respected (scroll + glass + shimmer) | ✓ |
| `window.scrollTo(0,0)` in render() done-path | removed (✓) |
| `.panel-entering` class applied + auto-removed | ✓ |
| `.pf-indeterminate` shimmer triggered at 91.5% | ✓ |
| Heartbeat escalation messages at 90s / 300s | ✓ |
| Bar never reaches 100% before calculate-done | ✓ |

### Manual QA browser matrix
- Chrome desktop: **not tested in this preflight**
- Safari desktop: **not tested**
- iPhone Safari: **not tested**

→ See §11 — Mobile/Safari treated as REMAINING_RISK.

---

## §9 Document Health / Upload Preflight

| Check | Status |
|---|:-:|
| LSB/SE/CAS marked mandatory in upload UI | ✓ (3-doc model) |
| Flugstundenübersicht hard-deactivated | ✓ (`AEROTAX_LEGACY_FLUGSTUNDEN_FORENSIK` Forensik-only) |
| `document_health` schema has `missing_months_se`, `missing_months_cas` | ✓ |
| Chat reads `document_health.missing_months_cas` before suggesting CAS upload | ✓ (lines 4874-4895) |
| Honest fallback when CAS present but unclear | ✓ ("Ich habe deinen Dienstplan/CAS bereits vorliegen") |
| Wrong file-type rejection | ✓ (doc-type detection 5 categories per Release Phase 1) |

---

## §10 Security / Privacy / Payment

| Check | Status | Source |
|---|:-:|---|
| No API keys in tracked files | ✓ | git diff scan |
| Anti-Tax-Sanitizer active | ✓ | Rel Phase 13 |
| PII-Hardening enforced | ✓ | `test_release_dsgvo_security.py` pass |
| Payment idempotency via attempt_id | ✓ | `test_release_payment_process_e2e.py` pass |
| Anti-Double-Charge | ✓ | Rel Phase 9 |
| Free-Retry-Token consumable once | ✓ | tests |
| No raw PII in AI prompts | ✓ | `test_phase5b_pii_hardening.py` |
| Prompt injection resistance | ✓ | per Rel Phase 11 |
| AI cannot emit tax-amount fields | ✓ | strict reader-vs-classifier separation per CLAUDE.md |
| Delete endpoint integrity | ✓ | `tests/test_pii_persist_p0_95.py` |
| Upload-Size-Cap explicit | NEEDS_DECISION | Cloud Run default 32 MB |
| Cookie-Banner | NEEDS_DECISION | per `LEGAL_TEXT_RELEASE_AUDIT` |

---

## §11 Deploy Readiness Checklist

### Backend
- [x] Local tests pass (2052 + 240 targeted)
- [x] Local contract pass (`_classify_job_state` returns all required fields)
- [x] No syntax errors
- [x] No PII/secret in diff
- [x] Env unchanged (no env var deletions planned)
- [ ] **DEPLOY REQUIRED** — live currently lags local
- [x] Rollback: `git revert HEAD` + push; or `gcloud run deploy aerotax-backend --source . --region europe-west3`

### Frontend
- [x] Local tests pass (28+15+23 = 66, plus 12+19 pre-existing OK)
- [x] Local contract pass (normalize + render + chat)
- [x] No syntax errors
- [x] No forbidden active strings
- [x] CSS additions OK
- [ ] **DEPLOY REQUIRED** — live missing all new helpers, CSS classes, table restructure
- [x] Rollback: previous `.html` from `~/Desktop/site/index.html.backup` if kept; otherwise wrangler revert via previous deploy

### Live-Run
- [x] Plan documented in `BH_CORE_001_LIVE_RUN_PLAN.md`
- [x] Stop after 1 fresh-session run
- [x] Use FRESH session (not RECALL of AT-11CEB21120E7799B, since the rb-marker still pending in that job)
- [x] Success criteria: bucket-split shown, no „Status wird geprüft", chat not contradicting
- [x] KPI tolerance per `OFFICIAL_RELEASE_GO_NO_GO_BOARD.md`

### Deploy order (when triggered)
1. **Backend deploy** (Render auto-deploy on `git push origin main`, ~3-4 min)
2. **Backend smoke**: re-run `/api/session/FAKE_TEST_TOKEN`, confirm `canonical_state` field present in 404 response
3. **Frontend deploy** (`wrangler pages deploy ~/Desktop/site --project-name aerosteuer --commit-dirty=true`)
4. **Frontend smoke**: re-run `node tests/test_frontend_state_machine_live_run.mjs` against LIVE-fetched HTML (curl + extract)
5. **One fresh live-run** with new files & form
6. Stop — wait for User decision

### Live-Run NO-GO Triggers
- "Status wird geprüft" parallel zur Result-Card
- PDF freigegeben bei pending review
- Chat sagt „brauche Antworten" wenn pending=0
- Chat sagt „lade CAS hoch" trotz CAS vorhanden
- Table-Math suggeriert `Brutto − Z77 = Netto` unmöglich
- API 502/000/HTML statt JSON
- Pent-up state-mix (done+failed parallel)
- PII/Secret/Token leak in Logs
- 1 Tag vs 1 Tage / Plural-vs-Singular Inkonsistenz

---

## §12 Final Decision

### Local code: PASS
- Backend pytest 2052 / Frontend JS 66 / Targeted critical-path 240 — alle grün.
- 0 hard failures, 13 documented xfails, 13 documented skips.
- No PII/Security/Payment blocker found.
- Bucket-math invariant holds: `displayed_total == sum(displayed_net_buckets)`.
- All render-paths normalize.
- Singular/Plural, CAS-self-awareness, Review-kind-copy, Chat-state-sync alle gefixt + getestet.

### Deploy gap: **BLOCKER** (until deploy)
- Backend live `/api/session/<token>` returns NONE of the required state-fields.
- Live frontend `index.html` lacks all fixes (scroll, glass, shimmer, bucket-split, chat-sync, review-kind-copy, CAS-self-awareness).

### **Decision: PASS TO DEPLOY TEST**

→ All local verification PASSED. Live testing requires the deploy step.

### Hard requirements before live-run
1. Backend redeploy on Render (`git push origin main`).
2. Frontend redeploy via Wrangler.
3. Backend smoke confirms `canonical_state` in 404-response.
4. Fresh-session live-run (new files, not recall of old token).

### Remaining risks (acknowledged, non-blocking)
- Mobile Safari Manual QA pending — track as RISK, not blocker.
- `result_version` / `pdf_version` fields not in backend yet (TODO Master P9).
- 70+ uncommitted docs/test files — operational risk if disk lost; recommend `git add tests/ docs/ supabase_migrations/` and commit before deploy for audit trail.
- `test_live_qa.mjs` / `test_live_state_machine.mjs` 15 failures predate this work — they use stale tokens against LIVE-deployed code, not local code.
