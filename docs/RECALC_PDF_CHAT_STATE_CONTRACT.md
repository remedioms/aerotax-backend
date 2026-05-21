# Recalc / PDF / Chat — End-to-End State Contract

Stand: 2026-05-20.
Status: **Authoritative**. Alle Endpunkte (`/api/process`, `/api/job/<id>`,
`/api/session/<token>`, Chat-/Review-Routen, `/api/download/<pdf_id>`,
`/api/finalize-pdf/<job_id>`, Retry-Endpunkte) MÜSSEN diesen Contract erfüllen.

---

## §0 Deploy-Lag-Hinweis (Production-Risk)

Lokaler `app.py` setzt seit v12 Phase A `canonical_state` per
`_classify_job_state(...)` an alle Job/Session-Responses. Der Render-Deploy
zum Zeitpunkt 2026-05-20 reflektiert das aber **nicht**:

| Endpoint | Local code | Live response |
|---|---|---|
| `/api/session/<token>` valid | sollte 14 state-Felder mergen | nur `download_url`, `expires`, `job_id`, `notes`, `result_data`, `token` |
| `/api/session/__test_404__` | sollte `{error, …state}` | nur `{error: …}` |
| `/api/job/<id>` valid | sollte state mergen | (nicht zuverlässig getestet — kalte Render-Instance, Timeout) |

→ **Backend-Redeploy ist Bedingung für Release-GO.**
→ Frontend kompensiert via `_normalizeBackendState` für UX-Continuity, aber das
darf nicht das einzige Sicherheitsnetz bleiben (Audit, Analytics, Recall via
Drittsysteme könnten falsche States lesen).

---

## §1 20 States

| # | State | Source | reason_code | pdf_allowed | retry_allowed |
|---:|---|---|---|:-:|:-:|
| 1 | `needs_review` | job pending review_items | `OPEN_REVIEW` | false | false |
| 2 | `user_answer_submitted` | review-answer endpoint accepted | — (transient) | false | false |
| 3 | `recalculation_queued` | overrides saved, async recalc not started yet | — | false | false |
| 4 | `recalculation_running` | `_recompute_with_overrides` in progress | — | false | false |
| 5 | `recalculation_long_running` | recalc >5min (currently sync, edge-case) | — | false | false |
| 6 | `recalculation_done` | preview_breakdown updated | — | true (if no pending review) | false |
| 7 | `recalculation_failed_retryable` | recompute exception, recoverable | `RECOMPUTE_FAILED` | false | true |
| 8 | `recalculation_failed_final` | recompute fundamental error | `RECOMPUTE_FATAL` | false | false |
| 9 | `pdf_generating` | finalize-pdf in flight | — | false | false |
| 10 | `pdf_ready` | finalize-pdf done, download_url valid | — | true | false |
| 11 | `pdf_locked` | review pending OR pending_reread | `OPEN_REVIEW` / `PENDING_REREAD` | false | false |
| 12 | `pdf_stale` | result_data changed since last PDF gen | `RESULT_NEWER_THAN_PDF` | false | false |
| 13 | `pdf_downloaded` | analytics-only (no state-change) | — | true | false |
| 14 | `review_still_pending` | answer accepted but more items open | `OPEN_REVIEW` | false | false |
| 15 | `review_complete` | all items answered OR skipped | — | true | false |
| 16 | `user_continues_without_clarification` | `_skipped_unanswered=True` | `USER_SKIPPED_REVIEW` | true | false |
| 17 | `user_uploads_correction_file` | upload-replacement accepted | — | false | false |
| 18 | `correction_file_processing` | sonnet re-read running on replaced doc | — | false | false |
| 19 | `correction_file_failed` | re-read invalid doc-type | `WRONG_DOCUMENT_TYPE` | false | true |
| 20 | `correction_file_accepted` | re-read success → triggers recalc | — | false (until recalc done) | false |

### Mapping to canonical_state strings

Currently `app._classify_job_state` returns only the 13 canonical strings:

`created`, `uploaded`, `queued`, `processing`, `needs_review`, `done`,
`failed_retryable`, `failed_support`, `expired`, `deleted`, `fetch_error`,
`pending` (input-only), `running` (input-only).

The 20 logical states above are **sub-states** that get derived from
`canonical_state` + flag fields (`pending_reread`, `_skipped_unanswered`,
`_pdf_version`, `_result_version`).

**Recommendation (post-release)**: Promote `pdf_generating`, `pdf_stale`,
`correction_file_processing` to first-class `canonical_state` values.
Currently the frontend infers them from side-channel signals
(`download_url`/`result_version` mismatch). Acceptable for MVP, technical
debt for v12.

---

## §2 Per-State Contract Table

| State | banner_title | banner_text | allowed buttons | forbidden buttons | poll active? | poll interval | next_actions | test |
|---|---|---|---|---|:-:|---:|---|---|
| needs_review | „Auswertung vorbereitet — kurze Klärung nötig" | „Ich habe deine Dokumente ausgewertet. 1 Punkt brauche ich noch zur Bestätigung." | open_review_chat, replace_file, skip_noncritical, support | download_pdf, create_pdf | no | — | open_review_chat / support | `test_rb_stage1_needs_review_state` |
| user_answer_submitted | „Antwort gespeichert" | „Danke, ich habe deine Antwort übernommen. Ich aktualisiere die Auswertung." | — | download_pdf | yes | 1500 ms | refresh | `test_rb_stage4_answer_marks_item_answered` |
| recalculation_running | „Auswertung wird neu berechnet" | „Das kann ein paar Minuten dauern. Bleib gerne auf der Seite." | come_back_later | download_pdf | yes | 3000 ms | refresh, come_back_later | (Master P3) |
| recalculation_done | „Auswertung aktualisiert" | „Dein Betrag ist neu berechnet." | download_pdf (if pdf_ready) | — | no | — | download_pdf, open_chat | (Master P3) |
| pdf_generating | „PDF wird erstellt" | „Der Rechenstand ist fertig. Ich erstelle gerade dein PDF." | — | download_pdf | yes | 1500 ms | refresh | (Master P5) |
| pdf_ready | „PDF bereit" | „Dein PDF ist bereit. Du kannst es jetzt herunterladen." | download_pdf, open_chat, start_new | retry | no | — | download_pdf | `test_classify_job_state_done_contract` |
| pdf_locked | „PDF gesperrt" | „Das PDF ist noch gesperrt, weil offene Punkte fehlen." | open_review_chat | download_pdf | no | — | open_review_chat | `test_rb_stage2_pdf_locked_during_review` |
| pdf_stale | „PDF veraltet" | „Die Antwort hat den Rechenstand geändert. PDF wird neu erstellt." | — | download_pdf (alter URL) | yes | 1500 ms | refresh | (Master P5) |
| failed_retryable | „Auswertung unterbrochen" | „Die Auswertung konnte nicht abgeschlossen werden. Deine Dokumente sind noch vorhanden — du kannst sie jetzt erneut starten." | retry, support | download_pdf | no | — | retry, support | `test_classify_job_state_failed_retryable_contract` |
| failed_support | „Auswertung konnte nicht sicher abgeschlossen werden" | „Damit kein unsicherer Betrag entsteht, wurde die Berechnung gestoppt. Bitte kontaktiere den Support." | support | retry, download_pdf | no | — | support | `test_classify_job_state` (failed_support branch) |
| expired | „Code abgelaufen" | „Dein Zugangscode ist abgelaufen. Bitte starte eine neue Auswertung." | start_new | download_pdf | no | — | start_new | `test_classify_job_state_expired_contract` |
| deleted | „Auswertung gelöscht" | „Diese Auswertung wurde gelöscht." | start_new | download_pdf | no | — | start_new | `test_classify_job_state_deleted_contract` |
| fetch_error | „Verbindung kurz unterbrochen" | „Deine Auswertung läuft möglicherweise weiter. Prüfe den Status erneut oder komme später mit deinem Zugangscode zurück." | refresh, support | download_pdf | yes | 5000 ms (backoff) | refresh | (Master P4) |
| processing | „Auswertung läuft" | „Deine Dokumente werden ausgewertet. Du kannst hier bleiben oder später mit deinem Zugangscode zurückkommen." | come_back_later, refresh | download_pdf | yes | 3000 ms | refresh, come_back_later | `test_classify_job_state_processing_contract` |

---

## §3 Endpoint-Contract — Pflichtfelder

Alle State-liefernden Endpunkte MÜSSEN bei 200 / 503 (fetch_error) folgende
Felder zurückgeben (nullable wenn nicht anwendbar, aber Key MUSS existieren):

```json
{
  "canonical_state":          "<one of 13 strings>",
  "reason_code":              "<error code or null>",
  "user_title":               "<i18n DE string>",
  "user_message":             "<i18n DE string, no English fallback>",
  "next_actions":             [{"type": "<action>", "label": "<DE label>"}],
  "pdf_allowed":              true | false,
  "retry_allowed":            true | false,
  "support_recommended":      true | false,
  "can_chat_explain_calculation": true | false,
  "can_show_final_amount":    true | false,

  "result_data":              { /* KPIs, audit_notes, _review_items, … */ } | null,
  "download_url":             "<path /api/download/<pdf_id>>" | null,
  "review_items":             [{"id", "type", "datum", "status", "question"}],
  "document_health":          { /* see §4 */ } | null,
  "engine_version":           "<ENGINE_VERSION>",
  "reader_versions":          { "lsb":, "se":, "cas": } | null,
  "result_version":           "<sha1-of-result_data>" | null,   ← TODO Master P9
  "pdf_version":              "<sha1-of-pdf-bytes>" | null      ← TODO Master P9
}
```

**Hinweis**: `result_version` und `pdf_version` existieren in der Backend-Source
heute **nicht** — sie sind ein Master-Phase-9 Add. Frontend nutzt aktuell
`download_url`-Vergleich als Fallback-Diff.

---

## §4 document_health Schema

```json
{
  "pipeline":          "v11_cas_primary",
  "status":            "green" | "yellow" | "red",
  "lsb_present":       true | false,
  "se_months_count":   0..12,
  "cas_months_count":  0..12,
  "detailed_cas_present": true | false,
  "missing_months_se":  ["2025-03", ...],
  "missing_months_cas": ["2025-03", ...],
  "ignored_legacy_files": ["flight_hours_summary_*.pdf"],
  "warnings":          ["<i18n DE string>"]
}
```

red → kein PDF, kein recompute. yellow → PDF mit Warning-Section.

---

## §5 Polling-Contract

- Polling-Endpoint: `/api/job/<id>` während aktiver Eval / Recalc.
- Polling-Endpoint: `/api/session/<token>` für Recall + Reload-Resume.
- Standard-Interval: 3000 ms. Backoff bei `fetch_error`: 5000 ms.
- Polling stop conditions:
  - `canonical_state ∈ {done, needs_review, failed_support, expired, deleted}` → STOP
  - `pdf_allowed=true && download_url` valid → STOP (no need to keep polling)
  - max iterations (12 min @ 3s = 240) → STOP + show "later" message
  - User leaves page / closes tab → STOP
- Polling MUST NOT create duplicate jobs. `/api/process` is invoked at most once
  per session unless explicit retry via `_manualRetry()`.
- Polling MUST NOT duplicate chat messages. Chat is rendered from `_data._review_items`,
  not appended from poll responses.

---

## §6 Forbidden State Combinations

- `done` + `pending review_items` → DERIVE `needs_review`, never show both.
- `done` + `pending_reread=True` → DERIVE `needs_review`.
- `failed_*` + result_data with `gesamt>0` → renderer MUST hide done sections
  (see `_hardHideResultSections` in `site/index.html`).
- `pdf_allowed=true` + `!download_url` → backend MUST flip to
  `pdf_allowed=false` + `user_title='Auswertung fertig — PDF erstellen'`
  (already enforced at `app.py:5895-5908`).
- `canonical_state=processing` + `result_data with KPIs > 0` → renderer must
  prefer state over data (no preview while processing).
- `canonical_state=expired` + valid download_url → reject download
  (download endpoint enforces session-token check).

---

## §7 Test-Coverage-Map

| Contract | Test |
|---|---|
| _classify_job_state done | `tests/test_backend_contract_local_vs_live.py::test_classify_job_state_done_contract` |
| _classify_job_state needs_review | `…test_classify_job_state_needs_review_contract` |
| _classify_job_state failed_retryable | `…test_classify_job_state_failed_retryable_contract` |
| _classify_job_state expired | `…test_classify_job_state_expired_contract` |
| _classify_job_state deleted | `…test_classify_job_state_deleted_contract` |
| _classify_job_state processing | `…test_classify_job_state_processing_contract` |
| session_recall safe.update wiring | `…test_session_recall_safe_dict_includes_state_fields` |
| /api/job state injection | `…test_get_job_status_includes_state_fields` |
| 404 path state response | `…test_no_response_returns_only_error_for_404_path` |
| Live contract pre-deploy gate | `…test_live_contract_pre_deploy` (skipped unless `AEROTAX_LIVE_CONTRACT=1`) |
| Deploy-lag xfail tracker | `…test_live_session_endpoint_has_canonical_state` (xfail) |
| Frontend normalize bug-shape | `tests/test_frontend_state_machine_live_run.mjs` (28 cases) |
| RB review needs_review flow | `tests/test_rb_review_recalc_flow_mock.py` (14 cases) |

---

## §8 Releasable When

GO criteria:
- All 11 contract pytests green (currently: 11 passed + 1 skipped + 1 xfailed deploy-lag).
- Live `/api/session/<token>` returns `canonical_state` (currently: FAILS → blocker via xfail).
- Live `/api/session/__test_404__` returns full state response (currently: FAILS).
- Frontend normalize tests 28/28 green.
- RB recalc mock tests 14/14 green.
- Manual QA checklist `docs/FRONTEND_LIVE_RUN_UX_STATE_FIX.md` §9 completed.

NO-GO trigger:
- Any of the above fails.
- Live `/api/finalize-pdf/<id>` returns `pdf_allowed=true` while review pending.
- Live `/api/download/<pdf_id>` allows expired/deleted sessions.
