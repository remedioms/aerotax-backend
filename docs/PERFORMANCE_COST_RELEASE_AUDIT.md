# Performance/Cost Release Audit

Stand: 2026-05-20 (Rel Phase 18).

## §1 Processing Times

| Phase | Typical (Render Free) | Cloud Run |
|---|---|---|
| LSB-Reader (Sonnet) | 15-25s | 8-15s |
| SE-Reader (Sonnet hybrid) | 20-35s | 10-20s |
| CAS-Reader (Sonnet, 12 PDFs) | 100-180s parallel | 60-120s |
| Tour-First-Classify | <1s | <1s |
| PDF-Render | 2-5s | 2-5s |
| Total | 150-250s | 80-160s |

Frontend zeigt Progress-Bar, kein Timeout-Crash bei 100s (asynchron via Job-Pattern).

## §2 KI-Cost-Estimate per Job

| Modell | Calls/Job | Tokens (est) | Cost/Job |
|---|---:|---:|---:|
| Sonnet 4.5 LSB | 1 | 5k in, 1k out | $0.02 |
| Sonnet 4.5 SE | 12 (per month) | 30k in, 5k out | $0.10 |
| Sonnet 4.5 CAS | 12 (per PDF) | 60k in, 10k out | $0.18 |
| Optional KI-Resolver (mock-first, max 5 live) | 0-5 | 2k in, 0.5k out | $0.02 |
| **Total per Job** | – | – | **~$0.30** |

Buffer für seltene Retries: ~$0.50/Job.

User-Preis pro Auswertung: 19,99€ → Margin ~99% nach KI-Kosten.

## §3 Concurrency

- Cloud Tasks Queue: max 10 concurrent jobs
- Cloud Run Instance: 1 worker per request (no shared state)
- Sonnet API: Rate-Limit-tolerant via retry-with-backoff

## §4 File Limits

| Limit | Value | Source |
|---|---|---|
| Max files per upload | 24 PDFs (Sonnet-Cap) | hartstop in `_sonnet_read_cas_structured` |
| Max file size | 32 MB (Cloud Run default) | NEEDS_DECISION (explicit cap empfohlen) |
| Max PDF pages | unbegrenzt | OK |
| Total payload | 32 MB request body | NEEDS_DECISION |

## §5 Memory

- Single CAS-PDF Sonnet-Call: ~50 MB
- 12 PDFs parallel (max 2) + LSB + SE: ~150-200 MB peak
- Cloud Run Recommended: 512 MB
- Render Free: 512 MB (knapp, manchmal OOM-Restart)

## §6 Cache

- KI-Resolver-Cache TTL: 24h
- File-Hash-Cache (CAS): pro-File-SHA256
- Job-State-Cache: persistent in Supabase

## §7 Timeouts

| Layer | Timeout |
|---|---|
| Sonnet single call | 180s |
| Job total | 600s (Cloud Tasks) |
| Frontend polling | infinite, mit progressive messages |
| Stripe webhook | 30s |

## §8 Risiken

| Risk | Likelihood | Impact | Mitigation |
|---|:-:|:-:|---|
| KI-Cost-Spike (mehrere Concurrent-Live-Calls) | low | medium | Anthropic-Rate-Limit + per-Job-Cost-Cap |
| Memory-OOM auf Render Free | medium | medium | CAS-Reader Stream-Mode, optional |
| Sonnet-Provider-Outage | low | high | Retry + needs_review-Fallback |
| Stripe-Outage | low | medium | Free-Retry-Token-Fallback nicht abhängig |

## §9 Status

**Overall: PASS** mit:
- 2 NEEDS_DECISION (max-file-size explicit cap, max-payload explicit cap)
- Operations Runbook §5 dokumentiert Performance-Erwartungen
