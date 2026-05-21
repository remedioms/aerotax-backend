# Security / PII / Tax Safety — Final Audit

Stand: 2026-05-20 (MegaR Phase 8).

## §0 Scan-Ergebnisse

| Scan | Ergebnis | Tool |
|---|:-:|---|
| API-Key-Leak (AKIA / sk-ant / sk-proj / sk_test / sk_live) | **0 hits** | `rg` über app.py + tests/ + docs/ + site/ |
| Session-Token in stdout | ✓ redacted | `_redact_token`, `_safe_log_session` |
| Recovery-Token in stdout | ✓ redacted | gleich |
| Payment-Token in stdout | ✓ redacted | gleich |
| PII in KI-Prompts | ✓ stripped | Reader-V2-PII-Hardening (Phase 5b) |
| Raw PDF in Logs | ✓ verhindert | nur Filename + Pages |
| PDF-Bytes in Logs | ✓ verhindert | `_log_redact` strippt bytes |

## §1 Anti-Tax-Sanitizer (KI darf keine Steuerbeträge zurückgeben)

`_cas_reader_v2_validate_schema` Z14444 prüft rekursiv auf:

```
_READER_V2_FORBIDDEN_FIELDS = {
    'amount', 'eur', 'euro', 'tagesatz', 'tax', 'steuer',
    'betrag', 'pauschale', 'rate'
}
```

Wenn irgendein Tool-Output diese Felder enthält → `valid=False`, Tag wird `needs_context_resolution=True` markiert.

Tests:
- `tests/test_cas_reader_v2_schema.py::test_*_no_tax_fields` ✓
- `tests/test_phase5d_ai_prompt_and_mapping.py` ✓
- `tests/test_v11_phase9_generalization.py::test_no_specific_tibor_values_hardcoded_in_logic` ✓ (prüft auf hardcoded EUR-Werte)

## §2 PII-Hardening (Phase 5b)

`tests/test_phase5b_pii_hardening.py` (7 Tests grün):
- Names: `redacted`
- Personal-IDs: stripped
- PNR-Patterns: stripped
- Address-Patterns: stripped
- Session-tokens: stripped
- IBAN-Patterns: stripped
- IATA-codes: pass-through (nicht PII)

## §3 Cache-TTL

KI-Resolver-Cache (`_ai_resolver_cache`):
- TTL: 24h
- Storage: in-memory + Supabase (PII-safe)

## §4 Forensik-Overrides (defensive, nicht aktiv)

Per ENV-Override zugängliche Legacy-Pfade — sicher:

| ENV | Aktiviert | Risk |
|---|---|---|
| `AEROTAX_LEGACY_FLUGSTUNDEN_FORENSIK=1` | Legacy DP-Reader-Funktionen | nur in Forensik, NICHT in Production |
| `AEROTAX_LEGACY_R5_V2_MERGE=1` | V2-Merge aus Flugstunden-Gap-Fixture | nur in Forensik, NICHT in Production |
| `AEROTAX_PIPELINE_VERSION=v10_legacy` | ENV-Variable existiert, KEINE funktionale Auswirkung mehr | – |

In Render-Produktion sind alle 3 ENVs NICHT gesetzt → Production läuft v11-Clean-Release.

## §5 Test-Suite-Verifizierung

```
$ pytest tests/test_phase5b_pii_hardening.py        ← 7 grün
$ pytest tests/test_phase5d_ai_prompt_and_mapping.py ← grün
$ pytest tests/test_v11_clean_release_flugstunden_purge.py ← 8 grün
$ pytest tests/test_phase3_metro_codes.py            ← 21 grün
$ pytest tests/test_v11_cas_reader_v2_schema.py      ← 23 grün
$ pytest tests/test_v11_cas_reader_refuses_non_cas.py ← 5 grün
```

Gesamt 64+ Security/PII/Schema-Tests grün.

## §6 Pflicht-Tabelle

| Pflicht | Status |
|---|:---:|
| Keine API Keys in repo/docs/logs | ✓ |
| Keine session/recovery/payment tokens in stdout | ✓ |
| Keine PII in KI prompts | ✓ |
| Keine raw PDF dumps in logs | ✓ |
| No PDF bytes in logs | ✓ |
| No names in fixtures (best-effort) | ✓ Tibor anonymisiert |
| Anti-Tax-Sanitizer active | ✓ |
| KI cannot return amount/eur/tagesatz/tax fields | ✓ |
| Cache TTL safe | ✓ 24h |
| env file cleanup warning | ✓ (User-Verantwortung post-Forensik) |

## §7 Definition of Done für Phase 8

- [x] Secret-Scan 0 hits
- [x] PII-Hardening Tests grün
- [x] Anti-Tax-Sanitizer Tests grün
- [x] Forensik-Overrides dokumentiert, Production-Default sicher
- [x] PDF-Audit kein raw-prompt-leak
- [x] Logs redacted
