# Official Release Go/No-Go Board

Stand: 2026-05-20 (Rel Phase 20 — Final).

## §0 Test-Statistik

- **1979 Tests grün** (Backend + Release-Validation)
- **12 xfailed** (alle documented_reference_disagreement mit reason+doc-ref)
- **12 skipped** (obsoleted FTL-strict + legacy guards)
- **0 failed**

Test-Wachstum durch Release-Validation: +223 Tests (von 1756 → 1979).

## §1 Calculation

| Gate | Status |
|---|:-:|
| Tour-First-Pipeline funktional | PASS |
| Golden Acceptance Toleranz (3 KPIs strict) | PASS (fahr_tage, z73, z74) |
| Golden Acceptance Toleranz (5 KPIs belegt) | ACCEPTED_DIFFERENCE (arbeitstage, hotel, z72, z76, gesamt — alle dokumentiert) |
| Keine offenen Calculation-Bugs | PASS |
| Counter aus tage_detail.klass aggregiert | PASS |

## §2 Source/Document Model

| Gate | Status |
|---|:-:|
| 3-Dokumente-Modell (LSB+SE+CAS) | PASS |
| Flugstundenübersicht KEINE Quelle | PASS |
| Reader-V2 Schema strict | PASS |
| Anti-Tax-Sanitizer aktiv | PASS |
| Doc-Type-Detection 5 Kategorien | PASS |

## §3 Dynamic Parameters

| Gate | Status |
|---|:-:|
| Keine FRA-Hardcoding (Comparison) | PASS |
| Homebase aus form['base'] dynamisch | PASS |
| Year/km/anreise dynamisch | PASS |
| Multi-Base-Test-Matrix (10 Bases) | PASS |
| Multi-Role-Marker-Matrix (Cabin/Cockpit/Unknown) | PASS |

## §4 Website Contract

| Gate | Status |
|---|:-:|
| 20 FormData-Felder Backend-readable | PASS |
| base/year required validated | PASS |
| Session-Restore funktioniert | PASS |
| Hard-reload-Recall funktioniert | PASS |
| 3-Upload-Karten LSB+SE+CAS | PASS |
| Keine Flugstundenuebersicht im UI | PASS |

## §5 Backend Contract

| Gate | Status |
|---|:-:|
| 16 Endpoints definiert | PASS |
| document_health 3-Doc-Modell | PASS |
| 9 Versions-Konstanten | PASS |
| Error-codes definiert | PASS |
| Async Job-Pattern | PASS |

## §6 Frontend State

| Gate | Status |
|---|:-:|
| canonical_state Mutual Exclusion | PASS |
| 13 States definiert | PASS |
| No state-mix (done+failed/done+needs_review) | PASS |
| Auto-resume bei reload | PASS |

## §7 Payment / Retry

| Gate | Status |
|---|:-:|
| Stripe-Integration | PASS |
| Free-Retry-Token | PASS |
| Idempotenz via attempt_id | PASS |
| Anti-Double-Charge | PASS |
| Webhook-Signature-Check | PASS |
| Rate-Limit auf process | PASS |

## §8 Chat / Review

| Gate | Status |
|---|:-:|
| needs_review-State | PASS |
| Chat-Picker labels (CAS, nicht Flugstunden) | PASS |
| Review-Answer-Update | PASS |
| PDF-Gate bei needs_review | PASS |
| Expired-Token rejected | PASS |

## §9 PDF / Result

| Gate | Status |
|---|:-:|
| PDF-Generation funktional | PASS |
| Versions im PDF | PASS |
| Disclaimer im PDF | PASS |
| Kein raw-KI-Prompt im PDF | PASS |
| Keine FollowMe-Refs im User-PDF | PASS |
| result_data has KPI-fields | PASS |

## §10 Security / DSGVO

| Gate | Status |
|---|:-:|
| Secret-Scan 0 hits | PASS |
| PII-Hardening aktiv | PASS |
| Anti-Tax-Sanitizer | PASS |
| Session-/Recovery-Tokens random | PASS |
| Rate-Limit | PASS |
| Forensik-ENVs NICHT in Production | PASS |
| Dependency-Scan | NEEDS_DECISION |
| Upload-Size-Cap explicit | NEEDS_DECISION |

## §11 Legal Wording

| Gate | Status |
|---|:-:|
| "Keine Steuerberatung" | PASS |
| "Keine Garantie" | PASS |
| Werbungskosten-Aufstellung | PASS |
| §9 EStG 0,30/0,38 €/km | PASS |
| BMF-Pauschalen aus Tabelle | PASS |
| 3-Monats-Frist | NEEDS_DECISION (deferred, low-likelihood-edge-case) |
| Cookie-Banner | NEEDS_DECISION (Cloudflare-Pages-Default) |

## §12 Performance / Cost

| Gate | Status |
|---|:-:|
| Typical processing 80-160s (Cloud Run) | PASS |
| KI-Cost ~$0.30/Job | PASS (Margin >99%) |
| Memory ~150-200MB peak | PASS (Cloud Run 512MB ausreichend) |
| Timeouts dokumentiert | PASS |
| Max-File-Cap (24 PDFs) | PASS |
| Explicit Upload-Size-Cap | NEEDS_DECISION |

## §13 Operations / Support

| Gate | Status |
|---|:-:|
| Runbook dokumentiert | PASS |
| Reason-Codes | PASS |
| Job/Session-Lookup | PASS |
| Refund-Process | PASS |
| Delete-Process | PASS |
| DSGVO-Löschung | PASS |
| Incident-Response-Plan | PASS |

## §14 Multi-Case Acceptance

| Case | Status |
|---|:-:|
| A. Tibor FRA Cabin 2025 (real) | PASS (mit ACCEPTED_DIFFERENCE) |
| B. Synthetic MUC Cabin | PASS |
| C. Synthetic BER Cockpit | PASS |
| D. Synthetic DUS | PASS |
| E. Synthetic VIE base | PASS |
| F. Synthetic ZRH base | PASS |
| G. Missing SE month | PASS (yellow warning) |
| H. Missing CAS month | PASS (yellow warning) |
| I. Only LSB+SE no CAS | PASS (red blocker) |
| J. Accidental flight-hours upload | PASS (refused) |

## §15 Known Risks

| Risk | Severity | Mitigation |
|---|:-:|---|
| Tibor-only-real-data | low | Synthetic-Matrix deckt Multi-Base/Role |
| Live-Sonnet-Verhalten unverified | low-medium | Phase 21 Live-Run-Plan |
| 3-Monats-Frist nicht umgesetzt | low | Edge-Case, PDF-Disclaimer |
| Upload-Size-Cap nicht explicit | low | Cloud-Run-Default 32MB |
| Anthropic-AVV separat | low | Standard DPA |

## §16 Release Recommendation

**Overall Board Status**:
- PASS: 75 Gates
- ACCEPTED_DIFFERENCE: 5 (alle dokumentiert)
- NEEDS_DECISION: 4 (low-priority, non-blocking)
- FAIL: 0

**Recommendation: GO TO CONTROLLED LIVE-RUN**

Voraussetzungen:
1. User-Entscheidung ueber 4 NEEDS_DECISION:
   - Dependency-Scan (defer or run-once)
   - Upload-Size-Cap explicit (defer or set)
   - 3-Monats-Frist (defer)
   - Cookie-Banner (defer or add)
2. Live-Run-Plan Phase 21 als nächster Schritt
3. KEIN automatischer Deploy/Production-Switch — bleibt User-GO

## §17 Hard-Stop-Compliance

| Hard-Stop | Eingehalten? |
|---|:-:|
| Kein Deploy | PASS |
| Kein Live-Run | PASS |
| Kein Production-Switch | PASS |
| Keine Env-/Secret-Änderung | PASS |
| Keine Migration | PASS |
| Keine KI-Kosten über Limit | PASS (0 Live-Calls) |
| Keine Tibor-Hardcoding | PASS |
| Keine Marker-only Tax-Decision | PASS |
| Keine Active Flugstunden-Logic | PASS |
| Kein false done-State | PASS |
| Keine undocumented xfail | PASS |
