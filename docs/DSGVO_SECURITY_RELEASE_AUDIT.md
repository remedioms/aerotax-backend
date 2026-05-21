# DSGVO/Security Release Audit

Stand: 2026-05-20 (Rel Phase 13). **Status: PASS** (mit Annahmen).

## §1 Data Minimization

| Pflicht | Status |
|---|:---:|
| Nur erforderliche Daten gespeichert | ✓ Form-Felder + 3 Doc-Familien |
| Optional fields nur wenn nötig | ✓ Belege optional |
| Keine raw PDFs in KI-Prompts | ✓ pdfplumber-Text-Extract |
| Keine PDF-bytes in Logs | ✓ `_log_redact` |
| Keine unnötige PII in result_data/PDF | ✓ Whitelist-Filter |

## §2 Legal Basis / User-Information

| Pflicht | Status |
|---|:---:|
| Datenschutzerklärung existiert | ✓ (HTML-Footer) |
| Löschung/Retention erklärt | ✓ (Operations Runbook) |
| Keine "Steuerberatung"-Behauptung | ✓ |
| Keine Garantie-Sprache | ✓ |
| Klar "keine Steuerberatung" | ✓ |

## §3 Storage

| Pflicht | Status |
|---|:---:|
| Uploaded PDFs nach Processing gelöscht | ✓ TTL-Cleanup |
| Result-TTL dokumentiert | ✓ ~30d |
| Delete-Endpoint funktioniert | ✓ |
| Recovery-Token limited (random+TTL) | ✓ |
| Access-Token random/unpredictable | ✓ (sha256-based) |

## §4 AI Processing

| Pflicht | Status |
|---|:---:|
| Keine PII über Notwendigkeit hinaus | ✓ Reader-V2-PII-Hardening |
| Prompt-Whitelist | ✓ |
| Keine Secrets/Tokens in Prompts | ✓ |
| AI-Output sanitized | ✓ Anti-Tax-Sanitizer |
| AI nie Tax-Amounts | ✓ |
| Cache TTL 24h | ✓ |

## §5 Security

| Pflicht | Status | Beleg |
|---|:---:|---|
| Secret-Scan | ✓ | 0 hits in app.py + tests + docs + site |
| Dependency-Scan | NEEDS_DECISION | `requirements.txt` review-empfohlen |
| XSS-Risk in Chat | low | input sanitized vor Display |
| CSRF-Risk | low | API-only, kein Cookie-Auth |
| CORS-Review | ✓ | CORS-Headers per Flask-CORS |
| Rate-Limit | ✓ | `_ip_rate_limited` aktiv |
| Upload-Type-Limit | ✓ | accept=pdf/image |
| Upload-Size-Limit | NEEDS_DECISION | Cloud-Run hat default 32MB limit |
| Path-Traversal | low | Files in-memory, kein FS-direct-access |
| Prompt-Injection via PDF | mitigation | PDF-Text wird vor KI gestrippt + Sanitizer |
| Malicious Chat-Input | low | escape/strip |
| Payment-Replay | ✓ | attempt_id + Stripe-Status-Check |
| Token-Brute-Force | ✓ | Rate-Limit + 32-char-random |

## §6 Pflicht-Docs

| Doc | Status |
|---|:---:|
| Privacy-Checklist | ✓ (dieser Doc) |
| Incident-Plan | ✓ (Operations Runbook) |
| Deletion-Request-Process | ✓ (Runbook §7) |
| Support-Process | ✓ (Runbook §8) |

## §7 Status pro Kategorie

| Kategorie | Status |
|---|:---:|
| Data-Minimization | PASS |
| Legal-Basis | PASS |
| Storage | PASS |
| AI-Processing | PASS |
| Security | PASS (2 NEEDS_DECISION: dependency-scan, upload-size-cap) |
| Docs | PASS |

**Overall: PASS** mit 2 NEEDS_DECISION (low priority, non-blocking).
