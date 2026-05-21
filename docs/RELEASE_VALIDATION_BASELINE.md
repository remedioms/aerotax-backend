# Release Validation Baseline Snapshot

Stand: 2026-05-20 (Rel Phase 0).

## §1 Git State

- Branch: `main`
- Last commits: `f7d473e BH-003a`, `c0078aa BH-001`, `3fa0aec E2E-Simulator-Bug`
- Uncommitted changes: 16 modified files + ~50 new docs/tests (Megaround + FinalFix + Closeout)
- NICHT gepusht (Hard-Stop „kein Deploy")

## §2 Version Constants (app.py Z246-262)

| Constant | Value |
|---|---|
| APP_VERSION | `11.0` |
| APP_BUILD | `v11-clean-release-flugstunden-removed-2026-05-20` |
| ENGINE_VERSION | `tour_first_v11_clean_release` |
| PROMPT_VERSION | `v11_0` |
| RULESET_VERSION | `v11_clean_release_2026_05_20` |
| AI_RESOLVER_VERSION | `phase5d_crew_vocab_v1` |
| CAS_READER_VERSION | `v2_with_refuse_non_cas_2026_05_20` |
| SE_READER_VERSION | `sonnet_se_structured_v8_0` |
| LSB_READER_VERSION | `sonnet_lsb_v8_0` |
| FRONTEND_CONTRACT_VERSION | `v11_3doc_lsb_se_cas_2026_05_20` |
| Default Pipeline | `AEROTAX_PIPELINE_VERSION=v11_cas_primary` |

## §3 Test-Suite Baseline

- **1756 grün**
- **12 skipped** (obsoleted FTL-strict + legacy guards)
- **12 xfailed** (alle als documented_reference_disagreement mit reason+doc-ref)
- **0 failed**

Test-Datei-Count: ~80 .py-Files unter `tests/`.

## §4 Aktuelle KPI-Snapshot (Tibor 2025 reference)

| KPI | AeroTAX | Golden | Δ | Tol | Status |
|---|---:|---:|---:|---:|:---:|
| fahr_tage | 58 | 58 | +0 | ±2 | PASS |
| z73_tage | 12 | 11 | +1 | ±1 | PASS |
| z74_tage | 2 | 1 | +1 | ±1 | PASS |
| arbeitstage | 142 | 133 | +9 | ±2 | ACCEPTED_DIFFERENCE |
| hotel_naechte | 62 | 66 | -4 | ±2 | ACCEPTED_DIFFERENCE |
| z72_tage | 3 | 5 | -2 | ±1 | ACCEPTED_DIFFERENCE |
| z76_eur | 5514 | 4794 | +720 | ±150 | ACCEPTED_DIFFERENCE |
| gesamt | 5780 | 6020.72 | -241 | ±150 | ACCEPTED_DIFFERENCE |

## §5 Upload Contract (3 Pflicht-Doks)

- LSB (Lohnsteuerbescheinigung) — 1 PDF
- SE (Streckeneinsatz-Abrechnungen) — 12 PDFs ideal
- CAS (Dienstplan/CAS PUB/NTF) — 12 PDFs ideal
- Flugstundenübersicht — KEINE Pflicht/Reader/Plausi-Quelle mehr

## §6 Backend Endpoints (Routes)

| Route | Method | Purpose |
|---|---|---|
| `/api/upload-files` | POST | Pre-Process Upload |
| `/api/process` | POST | Async Job Start |
| `/api/job/<id>` | GET | Job Status Poll |
| `/api/session/<token>` | GET | Recall via Token |
| `/api/restore-session/<token>` | POST | Hard-Reload-Resume |
| `/api/payment-status/<ref>` | GET | Stripe Status |
| `/api/create-checkout` | POST | Stripe Setup |
| `/api/create-payment-intent` | POST | Stripe Intent |
| `/api/stripe-webhook` | POST | Webhook |
| `/api/status/<ref>` | GET | Pre-Process Status |
| `/api/download/<token>` | GET | PDF Download |
| `/api/health` | GET | Health |
| `/api/progress` | GET (SSE) | Progress-Stream |
| `/api/finalize-pdf` | POST | Trigger PDF Build |
| `/api/demo` | POST | Demo-Mode |
| `/` | GET | Root + Versions |

## §7 Environment Assumptions

- `RECOVERY_SECRET` — Pflicht in Production (min 32 chars)
- `AEROTAX_EXECUTION_MODE=cloud_tasks` — Production
- `ANTHROPIC_API_KEY` — Sonnet 4.5 + 4.6
- `STRIPE_SECRET_KEY` — Payment
- `RENDER_API_KEY` — Logs/Deploy
- `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` — Frontend Deploy
- Forensik-Overrides (NICHT in Production):
  - `AEROTAX_LEGACY_FLUGSTUNDEN_FORENSIK=1`
  - `AEROTAX_LEGACY_R5_V2_MERGE=1`
  - `AEROTAX_ALLOW_BOOT_WITHOUT_KEY=1` (Test-Mode)

## §8 Known Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Tibor-Daten als einzige real-data-Quelle | Single-User-Validation | Phase 3+4 synthetic CAS für Multi-Base/Role |
| Live-Sonnet-Verhalten nicht verified | Production-Unknown | Phase 21 Live-Run-Plan |
| Render auto-deploy bei git push main | Deployment-Risk | Kein git push ohne User-GO |
| Cloud Run env-Vars Set-vs-Update | Operations-Risk | CLAUDE.md dokumentiert + Runbook |
| Pre-Phase-0 working tree uncommitted | Loss-Risk | Lokal vorhanden, kein remote-Verlust |

## §9 Definition of Done für Phase 0

- [x] Git-State dokumentiert
- [x] Versions-Constants exportiert
- [x] Test-Statistik Baseline
- [x] KPI-Snapshot
- [x] Upload-Contract Pflicht-Liste
- [x] Endpoints
- [x] Environment-Assumptions
- [x] Known Risks
