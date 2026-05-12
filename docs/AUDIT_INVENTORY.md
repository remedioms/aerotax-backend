# AUDIT_INVENTORY — AeroTAX vollständige Oberflächen-Karte

> Stand: 2026-05-12. Ziel: jede UI-Stelle, jeden Endpunkt, jeden State
> erfassen, damit kein Bug-Pfad unbemerkt bleibt.

## A) Frontend-Seiten / Panels

| Panel-ID | Zweck | Trigger | Sichtbar bei State |
|---|---|---|---|
| (Landing) | Hero-Seite, Produkt-Pitch, Pricing | initial | always |
| `tool` | Tool-Section: Formular + Upload (Step 1-3) | Klick „Tool starten" | upload_empty/partial |
| `p-proc` | Progress-Panel: Live-Texte während Auswertung | nach `/api/process` | queued/processing |
| `p-result` | Result-Panel: Banner + Detail + Chat + PDF | nach Worker done OR recall | done/needs_review/failed_* |
| (Forum) | Q&A Community | Nav-Link „Forum" | always |
| (Recall-Modal) | Code prüfen + Auswertung wieder öffnen | Nav-Link „Token" | always |
| (Support-Modal) | Support-Nachricht senden | Nav-Link „Support" | always |
| (Footer) | Datenschutz, Impressum, AGB | Footer-Links | always |

**Hinweis:** Es gibt **nur 2 `class="panel"`-Elemente** im HTML (`p-proc`, `p-result`).
Alle anderen Views sind Sections im Hauptdokument oder Modals.

---

## B) Frontend-Buttons (alle interaktiven Controls)

| Button-ID / Selector | Handler | Erlaubte States | Risiko-Check |
|---|---|---|---|
| `p0-weiter-btn` | `goStep(1)` | upload_empty | — |
| `p1-weiter-btn` | `goStep(2)` | upload_partial → upload | Pflicht-Files-Check |
| `p2-weiter-btn` | `goStep(3)` | upload_complete | km/Jahr-Check |
| `pay-btn` | Stripe Confirm | payment_pending | — |
| `dl-btn-main` | `dlPDF()` | done + pdf_allowed | **v14 Hard-Gate `canShowPdfDownload`** |
| `dl-btn-hint` | (Hint-Text) | done | — |
| `header-pdf-btn` | `dlPDF()` | done + pdf_allowed | **v14 Hard-Gate** |
| `dl-btn-row` | Container | done | — |
| `recall-open-btn` | `window._recallSubmit()` | always | **BUG-001: hängt** |
| `recall-edit-btn` | `window._recallEdit()` | recall + done | — |
| `hero-primary-btn` | varies (Review/PDF) | needs_review/done | gated |
| `hero-secondary-btn` | varies | needs_review/done | gated |
| `qa-ask-btn` | Forum: Frage stellen | always | — |
| `chat-close-btn` | Chat schließen | result | — |
| `chat-reset-btn` | Chat-History löschen | result | — |
| `chat-upload-btn` | Replacement-File senden | needs_review | — |
| `opt-plus-btn` | Optionale Belege | upload_complete | — |
| `cas-btns`, `se-btns` | Datei-Aktionen | upload | — |

**`class="dlb"`** (multiple Buttons mit dieser Klasse) — werden in `dlPDF()` zentral disabled.

---

## C) Backend-Endpunkte (alle Routes aus `app.py`)

### Payment / Upload
| Route | Method | Zweck |
|---|---|---|
| `/api/create-checkout` | POST | Stripe Checkout-Session |
| `/api/payment-status/<ref>` | GET | Zahlungsstatus pollen |
| `/api/create-payment-intent` | POST | Stripe Payment Intent + Promo |
| `/api/init-upload-session` | POST | Upload-Session anlegen |
| `/api/upload-files` | POST | Files in `uploaded_files`-Tabelle |
| `/api/stripe-webhook` | POST | Stripe → Backend Webhook |
| `/api/status/<ref>` | GET | Ref-Status |
| `/api/download/<token>` | GET | PDF-Token-Download (final) |
| `/api/restore-session/<token>` | POST | Session wiederherstellen |
| `/api/demo` | POST | Demo-PDF-Generator |

### Job lifecycle
| Route | Method | Zweck | Phase |
|---|---|---|---|
| `/api/process` | POST | Job erstellen + Worker enqueuen | thread/cloud_tasks |
| `/api/internal/process-job` | POST | Worker-Endpoint (OIDC) | v13 cloud_tasks |
| `/api/job/<id>` | GET | Job-Status (mit canonical_state) | always |
| `/api/job/<id>/audit` | GET | Audit-Log | done |
| `/api/job/<id>/review-answer` | POST | Single Review-Antwort | needs_review |
| `/api/job/<id>/review-bulk-answer` | POST | Bulk Review-Antwort | needs_review |
| `/api/job/<id>/review-groups` | GET | Review-Gruppen | needs_review |
| `/api/job/<id>/ai-chat` | POST | Review-Chat | needs_review |
| `/api/job/<id>/review-interpret` | POST | Sonnet interpretiert freie Antwort | needs_review |
| `/api/job/<id>/review-answer-bulk` | POST | (legacy) | needs_review |
| `/api/job/<id>/marker-answer` | POST | Marker-Frage beantworten | needs_review |
| `/api/job/<id>/upload-replacement` | POST | Datei ersetzen | needs_review/failed_* |
| `/api/job/<id>/upload-roster-screenshot` | POST | CAS-Screenshot nachreichen | needs_review |
| `/api/job/<id>/finalize-pdf` | POST | PDF erstellen | done/needs_review+skip |
| `/api/recover` | POST | Retry mit Token | failed_retryable |

### Session / Chat / Support
| Route | Method | Zweck |
|---|---|---|
| `/api/session/<token>` | GET | **Recall: canonical_state liefern** |
| `/api/session-by-code/<code>` | GET | (Alternative Recall) |
| `/api/session/<token>/delete` | POST | Session löschen |
| `/api/chat` | POST | Standalone-Chat |
| `/api/chat/history` | POST | Chat-History |
| `/api/chat/clear` | POST | Chat löschen |
| `/api/support-message` | POST | Support-Nachricht |
| `/api/admin/support-list` | GET | Admin |

### Q&A Forum
| Route | Method | Zweck |
|---|---|---|
| `/api/qa` | GET | Fragen-Liste |
| `/api/qa/ask` | POST | Frage stellen |
| `/api/qa/<qid>/answer` | POST | Antworten |
| `/api/qa/<qid>/upvote` | POST | Upvote |

### Health
| Route | Method | Zweck |
|---|---|---|
| `/api/health` | GET | Liveness |
| `/api/health/full` | GET | Full Health (Anthropic, Supabase, Stripe) |
| `/api/progress` | GET | SSE Live-Updates |
| `/` | GET | Root (302 redirect) |

---

## D) Canonical States (Phase A)

| State | Phase A reason_code | UI-Banner | PDF | Chat | Retry | Support |
|---|---|---|---|---|---|---|
| `created` | — | Auswertung vorbereitet | ❌ | — | — | optional |
| `uploaded` | — | Dokumente empfangen | ❌ | — | — | optional |
| `queued` | — | Auswertung wartet | ❌ | gated | — | optional |
| `processing` | — | Auswertung läuft | ❌ | gated | — | optional |
| `needs_review` | `OPEN_REVIEW` | Klärung nötig | ❌ | review-mode | ❌ | optional |
| `done` | — | Auswertung fertig | ✅ | full | — | optional |
| `failed_retryable` | `WORKER_RESTARTED` / `SONNET_TIMEOUT` / `JOB_TIMEOUT` etc. | Unterbrochen | ❌ | minimal | ✅ | ✅ |
| `failed_support` | `ALIGN_FAILED` / `CLASSIFICATION_SCHEMA_FAILED` / `DOCUMENT_HEALTH_RED` / `RETRY_LIMIT_REACHED` / `PDF_RENDER_FAILED` | Nicht sicher | ❌ | minimal | ❌ | ✅ prominent |
| `expired` | `ACCESS_CODE_EXPIRED` | Code abgelaufen | ❌ | — | — | — |
| `deleted` | `SESSION_DELETED` | Gelöscht | ❌ | — | — | — |
| `fetch_error` | (UI-only) | Verbindung kurz unterbrochen | ❌ | — | — | ✅ |

---

## E) LocalStorage Keys (Frontend-Persistenz)

| Key | Zweck | Lifecycle |
|---|---|---|
| `aerotax_session` | `{token, saved}` für Auto-Resume | 24h TTL (server) |
| `aerotax_upload_ref` | Upload-Reference vor Payment | session |
| `aerotax_uploads` | (legacy?) | session |
| `aerotax_codename` | Forum-Pseudonym | persistent |
| `aerotax_liked` | Forum-Like-IDs | persistent |
| `aerotax_api` | Override `window._API` (Dev) | persistent |
| `aerodebug` | Debug-Modus-Flag | session |

---

## F) PDF-Render-Stellen (jede ist Sicherheitsrisiko)

| Stelle | File:Line | v14 Status |
|---|---|---|
| `dl-btn-main` HTML | index.html:~2382 | **display:none, gated** |
| `header-pdf-btn` HTML | index.html:~2297 | **display:none, gated** |
| `dlPDF()` Funktion | index.html:~5063 | **Hard-Gate `canShowPdfDownload` am Anfang** |
| `_refreshPdfBubble` | index.html:~4684 | **Check `canShowPdfDownload` |
| `showDemoResult` | index.html:~2683 | `_isDemo:true` only |
| Demo-Path in `dlPDF` | index.html:~5143 | Nur wenn `_isDemo===true && !job_id` |
| `_applyPdfVisibility` | index.html:~1624 | **Zentraler Toggle für beide Buttons** |
| `pdf-locked-indicator` | index.html:~2384 | **Lock-Hinweis-Element** |

---

## G) Forum-Flows (Q&A)

| Flow | UI | API | Status |
|---|---|---|---|
| Load Fragen-Liste | Forum-Page render | `GET /api/qa?sort=hot` | **BUG-002 lädt langsam** |
| Frage stellen | `qa-ask-btn` | `POST /api/qa/ask` | — |
| Antworten | Comment-Form | `POST /api/qa/<qid>/answer` | — |
| Upvote | Like-Button | `POST /api/qa/<qid>/upvote` | localStorage tracked |
| Codename | localStorage | — | — |

---

## H) Chat-Flows

| Flow | API | Trigger | State |
|---|---|---|---|
| Free-Chat (done) | `/api/chat` | Hauptchat im Result | done |
| Review-Chat | `/api/job/<id>/ai-chat` | needs_review | needs_review |
| Review-Interpret | `/api/job/<id>/review-interpret` | freie Antwort | needs_review |
| Marker-Frage | `/api/job/<id>/marker-answer` | Marker unbekannt | needs_review |
| Chat-Gate (Phase A-5) | im `/api/chat` | wenn canonical != done | nicht-done |
| History | `/api/chat/history` | Recall | done |
| Clear | `/api/chat/clear` | reset-btn | done |

---

## I) Support-Flows

| Flow | UI | API |
|---|---|---|
| Support-Form öffnen | Nav „Support" → `openSupport()` | — |
| Support-Nachricht senden | Submit | `POST /api/support-message` |
| Admin List (intern) | — | `GET /api/admin/support-list` |

---

## J) Payment-Flows

| Flow | UI | API | State |
|---|---|---|---|
| Stripe Payment Intent | `pay-btn` | `POST /api/create-payment-intent` | payment_pending |
| Stripe 3DS-Return | `?paid=1&ref=<>` | (auto) | post-payment |
| Promo-Code | Promo-Input | im `create-payment-intent` | payment-bypass |
| `SMOKETEST` Promo | speziell | — | gratis |
| Stripe Webhook | (server-only) | `POST /api/stripe-webhook` | payment_confirmed |

---

## K) Mobile-kritische Screens

| Screen | Issue zu prüfen |
|---|---|
| Landing | Hero-Buttons tap-able |
| Upload (`tool`) | Dateiauswahl, Camera-Picker für iOS-Belege |
| Progress | Live-Texte lesbar, kein Layout-Bruch |
| Result | Hero-Betrag groß genug, PDF-Button reachable |
| Chat | Tastatur drängt nicht weg, scroll funktioniert |
| Recall-Modal | Token-Input fokussiert, virtuelle Tastatur ok |
| Forum | Scroll smooth, Like-Button tap-area |

---

## L) Worker-Pipeline (Backend nicht UI)

| Phase | Endpoint | Worker-Step | Snapshot-Punkt |
|---|---|---|---|
| Upload | `/api/process` | Files in Supabase persistieren | — |
| Enqueue | (Cloud Tasks) | Task in Queue | — |
| Worker start | `/api/internal/process-job` | Idempotency-Check + Lock | — |
| Parallel Reader | (intern) | LSB+SE+CAS parallel | `after_lsb`, `after_se`, `after_cas_file_NN` |
| Merge | (intern) | CAS-Dedup über Files | `after_cas_merge` |
| Match | (intern) | DP+SE per Datum | `after_match_cas_se` |
| Validate | (intern) | `_validate_pipeline_shape(matched, _SCHEMA_MATCHED_DAYS)` | `matched_schema_invalid` (bei Fehler) |
| Classify | (intern) | `_deterministic_classify_v7` | `pre_classify_v7`, `post_classify_v7` |
| Align | (intern) | `_followme_align_counters` | `pre_followme_align`, `followme_align_success/crash` |
| Save | (intern) | jobs.data + session.result_data | — |
| Final | (intern) | status=done/failed | — |
