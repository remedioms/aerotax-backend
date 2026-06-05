# AeroX Test-Pyramid (iOS app + Cloud-Run backend)

This directory holds the **automated** layers of the AeroX test pyramid.
Manual QA (Layer 12) lives in
`/Users/miguelschumann/Desktop/aeris-ios/AeroTax/tests/MANUAL_QA_CHECKLIST.md`.

## One-command runner

```bash
bash /Users/miguelschumann/Desktop/aerotax-backend/run_all_tests.sh
# options:
#   --keep-going  → continue after a layer fails (default: fail-fast)
#   --quick       → skip live-network layers (Layer 2)
```

Exit-code `0` = all automated layers pass.

## Layers

| Layer | File / Script                                                                                       | Expected result                                  |
| ----- | --------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| 1     | `tests/aerox/test_contract_ios_backend.py`                                                          | 49 tests pass (JSON contracts iOS ↔ backend)     |
| 2     | `tests/aerox/test_e2e_smoke_live.py`                                                                | 7 live journeys pass (auth, post, chat, roster)  |
| 3-5   | Wave-2 audits (discovered dynamically under `aeris-ios/AeroTax/tests/`): `permission_audit.py`, network/keychain/offline audits | each script exits `0` and writes a `*_REPORT.md` |
| 6     | `tests/aerox/test_ical_corpus.py`                                                                   | 20 iCal fixtures parse without warnings          |
| 7-8   | Wave-2 perf + snapshot audits (when delivered)                                                      | each exits `0`                                   |
| 9     | `aeris-ios/AeroTax/tests/security_audit.sh`                                                         | 0 FAIL in `SECURITY_AUDIT_REPORT.md`             |
| 10    | (reserved — Wave 2)                                                                                 |                                                  |
| 11    | `aeris-ios/AeroTax/tests/appstore_readiness.sh`                                                     | 25 checks, 0 FAIL in `APPSTORE_READINESS_REPORT.md` |
| 12    | `aeris-ios/AeroTax/tests/MANUAL_QA_CHECKLIST.md`                                                    | human-driven, ~30 min, all sections green        |

## Reading the output

- Pytest layers print one line per test plus a final `=== N passed ===` summary.
- Audit scripts print a banner per check and write a markdown report:
  - `SECURITY_AUDIT_REPORT.md`
  - `APPSTORE_READIness_REPORT.md`
  - `PERMISSION_AUDIT_REPORT.md`
  - (more as Wave 2 lands)
- The runner concludes with a summary block: `Passed:` / `Failed:` / `Skipped:`.
  Any non-empty `Failed:` list means the run is red.

## Cadence

| Layer        | Cadence                                                                |
| ------------ | ---------------------------------------------------------------------- |
| 1, 6         | every push to `main` (cheap, fully offline)                            |
| 2            | daily and before each release (touches live cloud-run)                 |
| 3-5, 7-8     | per commit affecting iOS source                                        |
| 9, 11        | before every TestFlight upload                                         |
| 12 (Manual)  | before every App-Store submission (release-blocker for sections 1-6+9) |

## Adding a new layer

1. Drop the script under `tests/aerox/` (Python) or `aeris-ios/AeroTax/tests/` (shell / Python audits).
2. The runner picks up `*.sh` and `*audit*.py` / `*test*.py` files automatically.
3. Update the table above + the cadence row.
