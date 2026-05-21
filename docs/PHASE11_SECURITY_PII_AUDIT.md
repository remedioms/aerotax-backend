# Phase 11 — Security / PII / Prompt Audit

Stand: 2026-05-20.

## §0 Scan-Ergebnis

| Scan | Ergebnis |
|---|---|
| `rg "AKIA[A-Z0-9]{16}"` (AWS-Keys in app.py/tests/docs) | **0 hits** |
| `rg "sk-ant-api[…]"` (Anthropic-Keys hardcoded) | **0 hits** |
| `rg "sk_test_[…]" / "sk_live_[…]"` (Stripe-Keys hardcoded) | **0 hits** |
| `tests/test_phase5b_pii_hardening.py` (7 PII-Hardening-Tests) | **alle gruen** |
| `tests/test_phase3_metro_codes.py` (KI-Prompt Crew-Context) | **alle gruen** |
| `tests/test_phase5d_ai_prompt_and_mapping.py` (Structured-Output) | **alle gruen** |

→ 44/44 Security/PII-Tests grün.

## §1 Pflicht-Audit (Master-Spec)

| Pflicht | Status | Stelle |
|---|:---:|---|
| Keine Raw PDFs in Logs | ✓ | nur Filename + Pages, keine bytes |
| Keine PDF bytes in Logs | ✓ | `_log_redact` strippt bytes |
| Keine Personaldaten in Prompts | ✓ | Phase 5b PII-Hardening + Whitelist-Filter |
| Keine Namen/Adressen/PNR/Personalnummern in Prompts | ✓ | Reader-V2 `safe_excerpt[:200]` |
| Keine session/recovery/payment tokens in Logs | ✓ | `_redact_token`, `_safe_log_session` |
| Keine API Keys in Files/Logs | ✓ | scan green |
| KI keine Steuerbeträge | ✓ | Anti-Tax-Sanitizer `_READER_V2_FORBIDDEN_FIELDS` |
| Anti-Tax-Sanitizer grün | ✓ | `_cas_reader_v2_validate_schema` forbidden-check |
| Prompt-Hardening grün | ✓ | crew-vocabulary in `_ai_resolver_airline_crew_context_block` |
| Cache TTL sauber | ✓ | `_ai_resolver_cache` 24h TTL |
| env-file nach KI-Läufen löschen/rotieren | ✓ | User-eigene Verantwortung, /tmp/phase5b_env.sh deleted |

## §2 Forensik-Override Risiko

Die Forensik-Variable `AEROTAX_LEGACY_FLUGSTUNDEN_FORENSIK=1` aktiviert deaktivierte Legacy-Reader. Sicherheit:

- Standard-Wert: nicht gesetzt (Funktionen werfen RuntimeError).
- Aktivierung: bewusste manuelle Env-Setzung erforderlich.
- Tests verifizieren: ohne Override → RuntimeError; mit Override → Funktion läuft.
- Render Produktions-Env hat KEIN `AEROTAX_LEGACY_FLUGSTUNDEN_FORENSIK=1` gesetzt.

## §3 Anti-Tax-Sanitizer

`tests/test_v11_cas_reader_v2_*.py` + `tests/test_phase5d_ai_prompt_and_mapping.py` verifizieren dass:
- KI-Output keine Felder mit `amount/eur/euro/tagesatz/tax/steuer/betrag/pauschale/rate` enthält
- Reader-V2 Tool-Schema diese Felder nicht erlaubt
- `_cas_reader_v2_validate_schema` returnt `valid=False` wenn Forbidden-Field auftritt

## §4 PII-Hardening (Phase 5b)

`tests/test_phase5b_pii_hardening.py` (7 Tests):
- Names redaction
- Personal-IDs strip
- PNR-pattern strip
- Address-pattern strip
- Session-token strip
- IBAN-pattern strip
- IATA-codes pass-through (nicht PII)

## §5 Definition of Done für Phase 11

- [x] Secret-Greps in app.py, tests/, docs/ → 0 hits
- [x] PII-Hardening Tests grün (7/7)
- [x] Anti-Tax-Sanitizer Tests grün
- [x] KI-Prompt Crew-Context Tests grün
- [x] Forensik-Override-Risiko dokumentiert
- [x] Pflicht-Tabelle alle ✓
