# API Contract Matrix

Phase A-Inventory. Stand 2026-05-15.

Quelle: `app.py` (42 Routes via `grep '@app.route'`). Live-Verifikation via `curl --max-time 25`.

---

## Pflicht-Felder pro Job-State-Endpoint

Definiert in `_classify_job_state` (app.py: Phase A-1, 2026 P0-Fixes). Jeder Endpoint der Job-State liefert MUSS folgende Felder konsistent setzen:

```json
{
  "canonical_state":  "done|needs_review|failed_retryable|failed_support|expired|deleted|processing|queued|pending",
  "status":           "<rohstatus aus job>",
  "reason_code":      "<NULL oder code>",
  "user_title":       "<lokalisiert>",
  "user_message":     "<lokalisiert>",
  "pdf_allowed":      true|false,
  "result_stale":     true|false,
  "document_health":  null|{status,color,details},
  "fetch_error":      null|true,
  "next_actions":     [{type,label}],
  "review_items":     [...],
  "result_data":      {...netto, brutto, arbeitstage, ...},
  "download_url":     "<url|null>"
}
```

> ⚠️ Wenn `canonical_state` null/missing: Frontend `_normalizeBackendState` baut den Vertrag clientseitig nach (BH-006).

---

## Endpoint × Felder

| Endpoint | Method | Zeile | canonical_state | user_title | user_message | pdf_allowed | next_actions | review_items | result_data | Status |
|---|---|---|---|---|---|---|---|---|---|---|
| `/api/process` | POST | 1953 | ? | ? | ? | ? | ? | ? | ✓ | TBD |
| `/api/internal/process-job` | POST | 2393 | — | — | — | — | — | — | — | Worker, kein User-Vertrag |
| `/api/job/<job_id>` | GET | 3327 | **502 ❌** | — | — | — | — | — | — | **BH-002** |
| `/api/job/<job_id>/audit` | GET | 3361 | **timeout ❌** | — | — | — | — | — | — | **BH-002** |
| `/api/job/<job_id>/review-answer` | POST | 3583 | ? | ? | ? | ? | ? | ? | ? | TBD |
| `/api/job/<job_id>/review-bulk-answer` | POST | 3679 | ? | ? | ? | ? | ? | ? | ? | TBD |
| `/api/job/<job_id>/review-groups` | GET | 3777 | — | — | — | — | — | ? | — | Read-only, eigener Vertrag |
| `/api/job/<job_id>/ai-chat` | POST | 3933 | — | — | — | — | — | — | — | Chat-Stream, eigener Vertrag |
| `/api/job/<job_id>/review-interpret` | POST | 4119 | — | — | — | — | — | — | — | KI-Resolver |
| `/api/job/<job_id>/review-answer-bulk` | POST | 4186 | ? | ? | ? | ? | ? | ? | ? | duplicate to 3679? |
| `/api/job/<job_id>/marker-answer` | POST | 4289 | ? | ? | ? | ? | ? | ? | ? | TBD |
| `/api/job/<job_id>/upload-replacement` | POST | 4355 | ? | ? | ? | ? | ? | ? | ? | TBD |
| `/api/job/<job_id>/upload-roster-screenshot` | POST | 4506 | ? | ? | ? | ? | ? | ? | ? | TBD |
| `/api/job/<job_id>/finalize-pdf` | POST | 4814 | (Spez-Vertrag Phase A-3) | — | — | ✓ | — | — | — | OK |
| `/api/session/<token>` | GET | 5845 | **null ❌ (BH-006)** | null | null | null | [] | [] | ✓ | **BH-006** |
| `/api/session-by-code/<code>` | GET | 5798 | ? | ? | ? | ? | ? | ? | ? | TBD |
| `/api/session/<token>/delete` | POST | 6225 | — | — | — | — | — | — | — | OK |
| `/api/recover` | POST | 5011 | ? | ? | ? | ? | ? | ? | ? | TBD |
| `/api/restore-session/<token>` | POST | 886 | ? | ? | ? | ? | ? | ? | ? | TBD (Stripe-3DS-Return) |
| `/api/chat` | POST | 5956 | — | — | — | — | — | — | — | Standalone Chat |
| `/api/chat/history` | POST | 6245 | — | — | — | — | — | — | — | OK |
| `/api/chat/clear` | POST | 6256 | — | — | — | — | — | — | — | OK |
| `/api/health` | GET | 5093 | — | — | — | — | — | — | — | OK |
| `/api/health/full` | GET | 5104 | — | — | — | — | — | — | — | OK |

### Non-Job-State-Endpoints (kein Job-Vertrag relevant)

`/api/create-checkout` (289), `/api/payment-status/<ref>` (318), `/api/create-payment-intent` (327), `/api/init-upload-session` (522), `/api/upload-files` (537), `/api/stripe-webhook` (709), `/api/status/<ref>` (747), `/api/download/<token>` (828), `/api/demo` (928), `/api/qa*` (6546+), `/api/support-message` (6718), `/api/admin/*` (6830, 6970), `/api/progress` (7145), `/` (7132).

---

## Live-Beobachtungen

| Endpoint | Probe | Befund |
|---|---|---|
| `GET /api/session/AT-C33E…` | curl 25s | 200, 411 KB, canonical_state=null, top-level state-fields fehlen, `result_data._review_items=2`. **→ BH-006** |
| `GET /api/session/AT-46C9…` | curl 25s | 200, 411 KB, canonical_state=null. Gleicher Befund. |
| `GET /api/job/e132976f-…` | curl 25s | **502, 223 KB HTML-Errorseite**. **→ BH-002** |
| `GET /api/job/e132976f-…/audit` | curl 25s | **timeout (HTTP 000)**. **→ BH-002** |
| `GET /api/session/AT-FAKE…` | curl 5s | 4xx, JSON-Error. OK |

---

## API-Error-Page-Probleme (HTML statt JSON)

**Befund:** `/api/job/<id>` liefert bei 502 eine **HTML-Errorseite** (223 KB) statt JSON. Frontend kann das nicht parsen → fetch.json() crashed.

**Frontend-Mitigation (aktuell):**
- `_recallSubmit` Z.8012: `.catch(function(err){ return {} })` — defensiver Catch
- `_autoResume`: `try { … } catch(_arErr) { silent return }` — fängt fetch-Fehler

**Backend-Pflicht:** Alle API-Endpoints müssen JSON-Errors liefern, nie HTML. Cloud-Run-Edge-Errors (502/503) sind außerhalb der App-Kontrolle aber müssen via Cloud-Run-Custom-Error oder via Gateway abgefangen werden.

---

## next_actions Pflicht-Set pro State

Aktuell uneinheitlich. Empfehlung:

| State | next_actions |
|---|---|
| `done` (mit download_url) | `[{type:'download_pdf', label:'⬇ PDF herunterladen'}]` |
| `done` (ohne download_url) | `[{type:'create_pdf', label:'PDF erstellen'}]` |
| `needs_review` | `[{type:'open_review_chat', label:'Im Chat klären'}, {type:'support', label:'Support'}]` |
| `failed_retryable` | `[{type:'retry', label:'Erneut versuchen'}, {type:'support', label:'Support'}]` |
| `failed_support` | `[{type:'support', label:'Support kontaktieren'}]` |
| `expired` | `[{type:'start_new', label:'Neue Auswertung starten'}]` |
| `deleted` | `[{type:'start_new', label:'Neue Auswertung'}]` |
| `processing` | `[{type:'refresh', label:'Status aktualisieren'}, {type:'come_back_later', label:'Später wieder'}]` |
| `fetch_error` | `[{type:'refresh', label:'Erneut prüfen'}, {type:'support', label:'Support'}]` |

Frontend `_normalizeBackendState` füllt diese defaults wenn Backend leer lässt.

---

## Test-Anforderungen (BH-006 Fix)

```
test_session_contract_done                    # canonical_state='done', pdf_allowed=true, next_actions has download_pdf
test_session_contract_needs_review            # cs='needs_review', pdf_allowed=false, review_items not empty
test_session_contract_failed                  # cs='failed_*', no result, retry action
test_session_never_null_state_with_result     # wenn result_data → cs nicht null
test_session_contract_expired
test_session_contract_deleted
test_session_contract_returns_json_not_html
test_job_contract_done                        # /api/job/<id>
test_job_contract_needs_review
test_job_502_regression                       # nach BH-002 Fix
test_api_errors_return_json_not_html
test_pdf_allowed_false_when_review_pending
test_next_actions_present_for_state
```
