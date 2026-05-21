# AeroTAX — E2E Launch QA Plan

Stand: 2026-05-20. **10 Pflicht-E2E-Flows** für Launch-Readiness.

## §1 Test-Flows

### Flow 1 — Happy Path (Done)

```
Upload (3 PDFs) → Payment/Promo OK → Cloud Task Processing
  → state=done → PDF sichtbar → Download OK → Recall mit Token OK
```

**Test**: `tests/test_e2e_tibor_pipeline.py` (vorhanden)
**Akzeptanz**: PDF generiert, Counter > 0, kein needs_review-Banner sichtbar.
**Aktueller Status**: ✓ läuft grün (siehe Regression 1521).

### Flow 2 — Needs Review

```
Upload → Processing → state=needs_review → Chat sichtbar
  → PDF gesperrt → Review-Items beantworten → state=done → PDF unlocked
```

**Test**: `tests/test_state_machine_*.py` (vorhanden)
**Akzeptanz**:
- `pdf_allowed=false` während `needs_review`
- Chat-Drawer öffnet (Glassmorphism Desktop / Modal Mobile)
- Chat-Header zeigt live-Betrag während Drawer offen (v8.23-Workaround)
- Nach Antworten: `state=done`, `pdf_allowed=true`

**Aktueller Status**: ✓ Tests grün.

### Flow 3 — Failed Retryable

```
Fehler in Processing → state=failed_retryable
  → Klare Fehlermeldung → Kein Done-UI-Mix
  → Retry/Support sichtbar
```

**Test**: `tests/test_auto_resume_state_pass.py`
**Akzeptanz**:
- Kein Betrag, kein PDF
- `canShowPdfDownload(state) == false`
- Reason-Code im API-Response

**Aktueller Status**: ✓ Tests grün.

### Flow 4 — Expired Token

```
Token > 30 Tage alt → Recall-Versuch → state=expired
  → „Token abgelaufen" → Kein Done/Review-Mix
```

**Akzeptanz**: API gibt `{state: 'expired'}` zurück, Frontend rendert Expired-State.
**Aktueller Status**: ⚠ Test-Coverage prüfen.

### Flow 5 — Deleted

```
User löscht Job → DELETE-Endpoint → Folge-Requests:
  → state=deleted → keine PDF/Result mehr abrufbar
```

**Akzeptanz**: PDF-Bytes aus Supabase entfernt, Frontend zeigt klare Meldung.
**Aktueller Status**: ⚠ Test-Coverage prüfen.

### Flow 6 — Payment Replay

```
Gleicher PaymentIntent zweimal → zweiter Request → blockiert (idempotent)
```

**Test**: `tests/test_payment_intent_lock_p0_96.py`
**Akzeptanz**: `_try_consume_payment_intent_supabase` blockt Replay.
**Aktueller Status**: ✓ Tests grün.

### Flow 7 — Upload Persist Fail

```
Upload-Speicherung scheitert (Supabase down) → Payment wird NICHT ausgelöst
```

**Test**: `tests/test_upload_persist_p0_90.py`
**Akzeptanz**: Fehlermeldung vor Stripe-Checkout, kein PaymentIntent erstellt.
**Aktueller Status**: ✓ Tests grün.

### Flow 8 — Worker Missing Files (Recovery)

```
Worker startet Job, Files fehlen → Restart-Recovery markiert als
  failed_recoverable → Support-Fallback sichtbar
```

**Test**: `tests/test_cloud_tasks.py` (Restart-Recovery-Pfad)
**Akzeptanz**: orphan-Job-Markierung, klare Support-Hinweise.
**Aktueller Status**: ✓ Tests grün.

### Flow 9 — KI-Fail

```
Anthropic-API timeout / 429 / 5xx → _claude_with_retry kümmert sich
  → Resolver-Fallback → keine Pipeline-Hänger
```

**Test**: `tests/test_phase2_ai_resolver.py` (`test_ai_resolution_timeout_falls_back_to_review`)
**Akzeptanz**: `_ai_resolver_review_fallback`-Pfad aktiv, kein Crash.
**Aktueller Status**: ✓ Tests grün.

### Flow 10 — PDF Generation Fail

```
ReportLab-Fehler → kein falscher done-State
  → klare Meldung „PDF wird vorbereitet" oder Fehler-State
```

**Akzeptanz**: `canonical_state` bleibt korrekt (`pdf_pending` oder `failed_pdf`).
**Aktueller Status**: ⚠ Test-Coverage prüfen.

## §2 Browser-QA-Plan

| Browser | Plattform | Test-Flows |
|---|---|---|
| Chrome 132+ | macOS Desktop | 1, 2, 3, 4, 9, 10 |
| Safari 18+ | iOS Mobile | 1, 2 (Drawer als Modal), 4 |
| Safari 18+ | macOS Desktop | 1 (hard reload), 4 (recall) |
| Firefox 134+ | Linux Desktop | 1, 2, API-Contract |

**Hard-Reload-Test (Safari + iOS bekannt-anfällig)**:
- F5 / Cmd+R während `processing` → Auto-Resume-Banner mit gespeichertem Token
- Cache-Buster wirksam (`_v=20251019_3`)
- Result-Card rendert konsistent nach Reload

## §3 API-Contract-Tests

`tests/test_state_machine_contract_*.py` deckt ab:
- `/api/job/<id>` Response-Schema pro State
- `/api/session/<token>` Schema-Konsistenz
- `/finalize-pdf/<id>` `pdf_allowed`-Gate

**Aktueller Status**: ✓ Tests grün.

## §4 Akzeptanz-Status (Gesamt)

| Flow | Status | Test-Coverage |
|---|:-:|:-:|
| 1 Happy Path | ✓ | hoch |
| 2 Needs Review | ✓ | hoch |
| 3 Failed Retryable | ✓ | hoch |
| 4 Expired Token | ⚠ | mittel — prüfen |
| 5 Deleted | ⚠ | mittel — prüfen |
| 6 Payment Replay | ✓ | hoch |
| 7 Upload Persist Fail | ✓ | hoch |
| 8 Worker Missing Files | ✓ | mittel |
| 9 KI-Fail | ✓ | mittel |
| 10 PDF Generation Fail | ⚠ | mittel — prüfen |

**Pflicht vor Launch**: Flows 4, 5, 10 zusätzlich abdecken.
