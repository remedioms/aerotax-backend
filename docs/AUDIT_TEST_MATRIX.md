# AUDIT_TEST_MATRIX — Tests pro State + Schicht

> Pro canonical_state müssen alle 6 Schichten Tests haben.
> `static` = Code-Regex (schwache Evidence)
> `unit` = Funktions-Test mit Mock
> `api` = Endpoint-Test via Flask test_client
> `dom` = DOM-rendering-Test (jsdom o.ä.)
> `chat` = Chat-Endpoint-Verhalten
> `pdf` = PDF-Sichtbarkeit-Test
> `browser` = manueller Browser-Beweis ODER Playwright

Status-Legende: ✓ vorhanden grün · ⚠️ vorhanden aber schwach · ❌ fehlt

---

## State: `created`
| Schicht | Test | Status |
|---|---|---|
| unit | `_classify_job_state` returnt `created` für leeres Job | ❌ (Job mit status='pending' wird zu `processing` gemappt) |
| api | `/api/job/<id>` für nicht-existenten Job → `expired` | ✓ `test_no_raw_job_not_found_user_facing` |
| dom | UI zeigt „Bitte lade Dokumente hoch" | ❌ |
| browser | Visit landing → tool → upload-empty | ❌ |

## State: `processing`
| Schicht | Test | Status |
|---|---|---|
| unit | `_classify_job_state({status:'running'})` → `processing` | ✓ |
| api | `/api/job/<id>` mit status=running liefert canonical_state | ✓ |
| dom | render() mit canonical_state=processing → kein PDF-Button | ❌ |
| chat | Chat-Endpoint blockt Final-Betrag | ✓ `test_chat_processing_blocks_final_amount` |
| pdf | `canShowPdfDownload({canonical:'processing'})` = false | ⚠️ statisch |
| browser | Mini-Run live → Progress-Page → keine final amount | ❌ |

## State: `needs_review`
| Schicht | Test | Status |
|---|---|---|
| unit | `_classify_job_state` mit pending review_items → `needs_review` | ✓ |
| api | `/finalize-pdf` blockt 409 | ✓ |
| dom | render() zeigt „kurze Klärung nötig" + kein PDF | ❌ |
| chat | Chat-Mode = review | ⚠️ statisch |
| pdf | `canShowPdfDownload({canonical:'needs_review'})` = false | ⚠️ statisch |
| browser | **BUG-003**: Recall mit needs_review-Token zeigt KEIN PDF | ❌ NICHT BEWIESEN |

## State: `done`
| Schicht | Test | Status |
|---|---|---|
| unit | `_classify_job_state({status:'done'})` → `done` | ✓ |
| api | `/finalize-pdf` allowed | ✓ |
| dom | render() zeigt PDF-Button | ❌ |
| chat | normaler Sonnet-Chat | ✓ (manuell) |
| pdf | `canShowPdfDownload({canonical:'done',pdf_allowed:true,download_url:'/x'})` = true | ⚠️ statisch |
| browser | Done-Token → PDF-Button erscheint | ❌ |

## State: `failed_retryable`
| Schicht | Test | Status |
|---|---|---|
| unit | Sonnet-Timeout → reason_code | ✓ |
| api | Worker returnt 500 für Cloud Tasks Retry | ✓ |
| dom | „Auswertung unterbrochen" + Retry-Button | ❌ |
| chat | Retry-Aktion angeboten | ✓ statisch |
| pdf | PDF locked | ⚠️ statisch |
| browser | failed_retryable-Token → Retry-Button sichtbar | ❌ |

## State: `failed_support`
| Schicht | Test | Status |
|---|---|---|
| unit | ALIGN_FAILED → failed_support | ✓ |
| api | `/finalize-pdf` blockt 409 | ✓ |
| dom | „Nicht sicher abgeschlossen" + Support-Button | ❌ |
| chat | Chat verweist auf Support | ⚠️ statisch |
| pdf | PDF locked | ⚠️ statisch |
| browser | failed_support-Token → Support-Banner | ❌ |

## State: `expired`
| Schicht | Test | Status |
|---|---|---|
| unit | `_classify_job_state(None)` → `expired` | ✓ |
| api | `/api/session/AT-NONEXISTENT` → 404 mit `canonical_state=expired` | ✓ |
| dom | „Code abgelaufen" sichtbar | ❌ |
| chat | n/a | — |
| pdf | n/a | — |
| browser | Recall mit invalid Token → friendly error | ❌ |

## State: `deleted`
| Schicht | Test | Status |
|---|---|---|
| unit | Session.deleted=true | ✓ |
| api | (n/a — Session-Delete-Endpoint) | — |
| dom | „Auswertung gelöscht" | ❌ |
| browser | — | ❌ |

## State: `fetch_error`
| Schicht | Test | Status |
|---|---|---|
| unit | `deriveUiState({fetch_error:true})` → fetch_error branch | ✓ statisch |
| api | (n/a) | — |
| dom | „Verbindung kurz unterbrochen" + nicht „Auswertung fehlgeschlagen" | ⚠️ statisch |
| browser | Network-Block → friendly Text | ❌ |

---

## Test-Schichten-Summary

| Schicht | Coverage | Beweis-Stärke |
|---|---|---|
| **Static** (Regex/Substring) | hoch | ⚠️ schwach — sagt nur „Code existiert", nicht „funktioniert" |
| **Unit** (Python pytest) | hoch für State-Machine | ✓ mittel |
| **API** (Flask test_client) | mittel | ✓ stark für Backend-Contract |
| **DOM** (jsdom) | **NULL** | ❌ kritisch fehlt |
| **Chat-State-Gate** | hoch | ✓ mittel |
| **PDF-Visibility** | hoch statisch | ⚠️ kein DOM-Beweis |
| **Browser** | **NULL** | ❌ kritisch fehlt |

## Was als nächstes gebraucht wird

1. **jsdom-Setup oder Playwright** für DOM-Tests
2. **Mock API Server** für DOM-Tests (kann Backend mocken)
3. **Browser-Test-Skript** (Playwright/Puppeteer) für 9 States × 4 Browser

## Status-Honesty-Check

> Vor v14 hatten wir 1046/1046 Tests grün und der Mini-Run-Reopen-Bug war
> trotzdem live. Das beweist: **statische Tests sind nicht hinreichend**.
> Wir brauchen DOM-/Browser-Tests, sonst geht jeder zweite Frontend-Bug
> durch.
