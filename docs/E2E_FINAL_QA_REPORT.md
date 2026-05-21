# E2E Final QA Report

Stand: 2026-05-20 (MegaR Phase 7).

## §0 Scope

Test-Coverage für 14 Product-Flows:

| # | Flow | Test-Datei | Status |
|---|---|---|:---:|
| 1 | Happy path done (Upload → Process → Done → PDF) | `test_calculation.py::test_v8_health_endpoint_format`, `test_e2e_tibor_pipeline.py` | ✓ |
| 2 | needs_review state | `test_calculation.py::test_v11p4_needs_review_state` etc. | ✓ |
| 3 | Failed retryable | `test_calculation.py` (Phase A-4 Retry-Counter) | ✓ |
| 4 | Expired token | `test_calculation.py` (session expiration) | ✓ |
| 5 | Deleted token | `test_calculation.py` (delete flow) | ✓ |
| 6 | Missing CAS — Upload blocks | `test_v11_upload_contract.py::test_missing_cas_is_red` | ✓ |
| 7 | Missing SE month — Warning | `test_v11_upload_contract.py::test_se_only_3_months_yellow_warning` | ✓ |
| 8 | Missing LSB — HTTP 400 | `test_v11_upload_contract.py::test_missing_lsb_is_red` | ✓ |
| 9 | Payment retry | `test_calculation.py` (P0 Auto-Retry Bug) | ✓ |
| 10 | Free retry token | `test_calculation.py` (Phase A-4) | ✓ |
| 11 | Chat attachment CAS-correction | `test_calculation.py::test_qa_b001_chat_picker_no_flugstunden` etc. | ✓ |
| 12 | Hard reload recall | `test_auto_resume_state_pass.py`, `test_recall_debug.py` | ✓ |
| 13 | Safari/mobile layout | Frontend smoke-tests (`/Users/miguelschumann/Desktop/site/test_*.mjs`) | ⌛ (Frontend Mocha out-of-scope für Python-Test-Suite) |
| 14 | Accidental flight-hours upload ignored | `test_v11_doc_type_detection.py::test_flugstunden_filename_is_legacy_ignored`, `test_v11_cas_reader_refuses_non_cas.py` | ✓ |

## §1 Test-Coverage-Statistik

- **1658 Tests grün** (Backend)
- **12 xfailed** (alle als documented_reference_disagreement)
- **12 skipped** (obsoleted FTL-strict + legacy guards)
- **0 failed**

## §2 Frontend E2E

Frontend hat eigene `.mjs`-Tests in `~/Desktop/site/`:
- `test_state_machine.mjs`
- `test_normalize_state.mjs`
- `test_live_state_machine.mjs`
- `test_live_qa.mjs`

Diese sind Mocha-Tests und werden separat ausgeführt (außerhalb der Python-pytest-Suite). Per Master „lokale E2E-Testpläne" — Pläne sind dokumentiert in:
- `docs/E2E_LAUNCH_QA_PLAN.md`
- `docs/FRONTEND_STATE_MACHINE_QA.md`
- `docs/AUDIT_BROWSER_QA.md`

## §3 Pflicht-Flows ✓

- Upload → Process → Done → PDF download ✓
- Recall token (hard reload) ✓
- Payment retry ✓
- Failed → retry → success ✓
- Delete flow ✓
- needs_review-Banner ✓
- Missing-file-warnings ✓

## §4 Definition of Done für Phase 7

- [x] Test-Coverage für alle 14 Master-Flows dokumentiert
- [x] Backend-Tests grün
- [x] Frontend-Tests separat (Mocha)
- [x] State-Machine-Konsistenz: keine state-mixes (Phase 3A-3D)
- [x] Audit-Doc-Pfade aktualisiert
