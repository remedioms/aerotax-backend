# AeroTAX — Security / Privacy Launch Audit

Stand: 2026-05-20. Pflicht-Audit für Launch-Readiness.

## §1 Secret-Scan

| Scan | Befehl | Status |
|---|---|:-:|
| Hardcoded `sk-ant-api03-...` | `grep -rn 'sk-ant-api' --include='*.py' --include='*.md' --include='*.json'` | ✓ keine Treffer (außer `_job_chunks_state/` Test-Fixture-Dir) |
| Hardcoded `STRIPE_SECRET_KEY = '...'` | `grep -rn STRIPE_SECRET --include='*.py'` | ✓ nur Kommentare + `os.environ`-Lookups |
| Hardcoded `SUPABASE_KEY = '...'` | `grep -rn SUPABASE_KEY --include='*.py'` | ✓ nur `os.environ`-Lookups, kein Literal |
| Hardcoded `RECOVERY_SECRET = '...'` | `grep -rn 'RECOVERY_SECRET = '` | ✓ keine Treffer außer `os.environ` |
| `.env`-Files committed? | `git ls-files \| grep -i env` | ✓ `.env` in `.gitignore` |

**Pflicht-Quelle für Secrets**: Render Env-Vars (Backend) / Cloudflare Pages (Frontend hat keine Secrets).

**Bekannte Test-Hardcodes (akzeptabel)**:
- `tests/fixtures/*.json` enthält **keine Echt-Secrets** (PII-anonymisiert, BMF-Werte nur)
- `_job_chunks_state/*.json` enthält Test-Token (`test-job-001` etc.)

**Stop-Status**: ✓ Keine Echt-Secrets im Repo.

## §2 PII-Scan im Code/Tests

| Scan | Befehl | Status |
|---|---|:-:|
| Echte Namen | `grep -rn 'Tibor\|Mustermann\|Schumann'` außerhalb `CLAUDE.md`/Test-Fixtures | ✓ nur in CLAUDE.md (User-Email-Reference) und docs/, nicht in Production-Code |
| Echte Telefonnummern | `grep -E '\+?49[1-9][0-9]{8,}'` | ✓ nur in PII-Hardening-Tests als negative samples |
| Echte IBAN | `grep -E 'DE[0-9]{20}'` | ✓ nur in PII-Hardening-Tests als negative samples |
| `schumannmiguel2@gmail.com` | echte User-Email | ⚠ erscheint in CLAUDE.md / memory (User-Account-Reference, nicht in Production-Code) |

**Stop-Status**: ✓ Keine PII in Production-Code-Pfad.

## §3 KI-Prompt-Sanity

Tests in `tests/test_phase5b_pii_hardening.py` decken ab:
- ✓ Whitelist-Filter `_ai_resolver_safe_context` aktiv
- ✓ Forbidden-Fields-Liste (name/mitarbeiter/pnr/email/iban/tax_id/birthdate/...) wird gestrippt
- ✓ Session/Recovery/Payment-Tokens entfernt (`session_token`, `payment_intent`, `stripe_*`)
- ✓ Raw-PDF-Text / pdf_bytes / file_content / filename entfernt
- ✓ Cache-Keys PII-frei (nur job_id-prefix + hash)
- ✓ Audit-Logs PII-frei (`_ai_resolver_log`-Funktion)

Tests in `tests/test_phase5_ai_resolver.py::test_ai_rejects_tax_amount_fields`:
- ✓ 11 Forbidden-Keys (amount/eur/euro/tagesatz/tax/steuerbetrag/deduction/rate/betrag/vma/pauschale) → `AI_TAX_VALUE_REJECTED`
- ✓ Nested-Detection rekursiv

## §4 Payment-Replay-Schutz

`tests/test_payment_intent_lock_p0_96.py`:
- ✓ Gleicher PaymentIntent zweimal → zweiter Request blockiert
- ✓ Supabase-Lock via `_try_consume_payment_intent_supabase`
- ✓ `_update_payment_intent_lock_status` für Statusübergänge

## §5 Upload-vor-Payment-Schutz

`tests/test_upload_persist_p0_90.py`:
- ✓ Wenn Supabase-Upload scheitert: KEIN Stripe-Checkout
- ✓ User bekommt Fehlermeldung VOR Zahlung

## §6 Data Retention

| Pfad | TTL | Cleanup |
|---|---|---|
| Original-PDFs in Supabase | `UPLOAD_TTL_HOURS` (Default 24h) | `_delete_uploaded_files_supabase` nach Job-Abschluss |
| Result-PDF | `PDF_TTL_HOURS` (Default 720h = 30 Tage) | TTL-basiert |
| Session-Tokens | recovery_pepper-Hash, 30 Tage | TTL-basiert |
| `_ai_resolver_cache` (in-memory) | 24h | auto-expire pro Eintrag |
| `_job_chunks_state/*.json` | bis Job-Abschluss | manuell oder via Worker-Restart-Cleanup |

**TODO vor Launch**: `_job_chunks_state/`-Cleanup-Job (CRON) prüfen.

## §7 Logs

| Log-Pattern | PII-Status | Pflicht-Check |
|---|:-:|:-:|
| `[v8-classify]` Phase-Übergang | PII-frei | ✓ |
| `[ai-resolver-mock] kind=... conf=...` | PII-frei (nur Kind/Conf/Resolved) | ✓ |
| `[ai-resolver] api_fail kind=... attempt=...` | PII-frei | ✓ |
| `[finalize-pdf] Counter ...` | PII-frei (nur Aggregate) | ✓ |
| `[queue] ...` Cloud-Tasks | PII-frei (job_id-Prefix) | ✓ |
| `print(...)` direkte Aufrufe | gemischt — Code-Review nötig | ⚠ Stichprobe |

**Stichprobe app.py print()-Aufrufe (Z2000-3000)**: Logging-Kontext meist `[v8-...]`-prefixed, PII-frei.

## §8 KI-Kostenkontrolle

| Schutz | Implementiert? | Status |
|---|:-:|---|
| `_AI_RESOLVER_MAX_TOKENS = 512` | ✓ | hart |
| `_AI_RESOLVER_TIMEOUT_S = 30` | ✓ | hart |
| `_AI_RESOLVER_CACHE_TTL_HOURS = 24` | ✓ | verhindert Re-Calls |
| Anti-Retry-Loop bei Auth-Errors | ✓ | hart (Phase 5b-Lesson) |
| `AEROTAX_AI_RESOLVER_PHASE5B_APPROVED` Env-Gate für Live | ✓ | hart |
| Per-Job Call-Cap (z.B. ≤20/job) | ⚠ nicht hart implementiert | TODO |
| Tägliches Cost-Limit | ⚠ nicht hart implementiert | TODO |
| Monatliches Cost-Limit | ⚠ Monitoring-Sache | dokumentiert in Production-Switch-Plan |

**Empfehlung Phase O**: Per-Job-Cap = 20 KI-Calls, Auto-Cap auf NEEDS_USER bei Überschreitung.

## §9 Browser-Side Privacy

| Item | Status |
|---|:-:|
| `localStorage` enthält nur `session_token` (hashed reference) | ✓ |
| Kein PDF-Bytes im Frontend-Cache | ✓ |
| Cache-Buster für Hard-Reload | ✓ (`_v=20251019_3`) |
| HTTPS-only (Render + Cloudflare Pages) | ✓ |

## §10 Compliance — DSGVO/EStG

| Anforderung | Status |
|---|:-:|
| Originaldokumente werden nach Job-Abschluss gelöscht | ✓ (UPLOAD_TTL) |
| User-Delete-Endpoint funktioniert | ⚠ Coverage prüfen |
| Recovery-Token-Mechanismus mit hashed-reference | ✓ |
| Kein Profile-Tracking | ✓ |
| AGB/Impressum/Datenschutz auf Frontend | ⚠ Frontend-Check nötig |
| Steuerliche Aussagen ehrlich (siehe v8.23-Stubs) | ✓ in CLAUDE.md dokumentiert |

## §11 Akzeptanz-Status

| Kategorie | Status | Pflicht vor Launch? |
|---|:-:|:-:|
| §1 Secret-Scan | ✓ PASS | ja |
| §2 PII-Scan Code | ✓ PASS | ja |
| §3 KI-Prompt-Hardening | ✓ PASS | ja |
| §4 Payment-Replay | ✓ PASS | ja |
| §5 Upload-vor-Payment | ✓ PASS | ja |
| §6 Data Retention | ⚠ Cleanup-Job-Check | ja |
| §7 Logs PII-Status | ✓ PASS (Stichprobe) | ja |
| §8 KI-Kosten Per-Job-Cap | ⚠ NEEDS_DECISION | empfohlen, nicht blocker |
| §9 Browser-Privacy | ✓ PASS | ja |
| §10 DSGVO User-Delete-Coverage | ⚠ Test prüfen | ja |
| §10 AGB/Impressum Frontend | ⚠ Manuelle Verifikation | ja |

**Pflicht-Closeouts vor Launch**: §6 Cleanup, §10 User-Delete-Test, §10 Frontend-Legal-Pages.
