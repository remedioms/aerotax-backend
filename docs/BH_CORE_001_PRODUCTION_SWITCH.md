# BH-CORE-001 Phase 8 — Production Switch Plan

Stand: 2026-05-19. **STATUS: PLAN. NICHT AUSGEFÜHRT. Wartet auf User-Freigabe.**

## §0 Vorbedingungen

Vor Production-Switch MÜSSEN erfüllt sein:

| Vorbedingung | Status |
|---|:-:|
| Phase 7 Live-Run erfolgreich gegen Tibor's echte Files | ⌛ pending |
| Live-Run-Result ≥ alte Pipeline | ⌛ pending |
| Cluster-Mismatches dokumentiert + akzeptiert oder gefixt | ⌛ pending |
| KI-Kosten-Budget definiert | ⌛ pending |
| Monitoring/Alert-Routes konfiguriert | ⌛ pending |
| Rollback-Drill erfolgreich getestet | ⌛ pending |
| Browser-QA grün (Chrome, Safari, Firefox; Desktop+Mobile) | ⌛ pending |

## §1 Feature-Flag-Switch

### Aktuelle Default-State

```
AEROTAX_TOUR_FIRST_CLASSIFIER=0  (default OFF)
AEROTAX_AI_RESOLVER_MODE=mock     (default mock)
AEROTAX_AI_RESOLVER_PHASE5B_APPROVED=  (default unset)
```

### Production-Switch (ON-Befehl)

```
gcloud run services update aerotax-backend \
  --region europe-west3 \
  --update-env-vars AEROTAX_TOUR_FIRST_CLASSIFIER=1
```

**WICHTIG**: `--update-env-vars` (merge), NICHT `--set-env-vars` (überschreibt alle!).
Lesson learned aus BUG-005 (2026-05-12).

### Rollback (OFF-Befehl)

```
gcloud run services update aerotax-backend \
  --region europe-west3 \
  --update-env-vars AEROTAX_TOUR_FIRST_CLASSIFIER=0
```

Rollback-Latency: ~30s (Cloud-Run-revision-deploy).

## §2 Smoke-Tests (vor Switch)

Reihenfolge muss eingehalten werden. Bei jedem rot → STOP.

1. **py_compile**:
   ```
   python3 -m py_compile app.py
   ```
   Erwartet: keine Fehler.

2. **Full Regression**:
   ```
   python3 -m pytest --tb=no -q
   ```
   Erwartet: 1487+ grün, 7 skipped, max 16 acceptance rot (Tour-Boundary-pending).

3. **Tour-First-Tests isoliert**:
   ```
   python3 -m pytest tests/test_phase48_*.py tests/test_phase5*.py \
                     tests/test_phase6b_*.py tests/test_normalized_tours_*.py \
                     tests/test_evidence_engine.py -v
   ```
   Erwartet: 100% grün.

4. **Bangalore-Reference-Test**:
   ```
   python3 -m pytest tests/test_normalized_tours_bangalore.py -v
   ```
   Erwartet: 6 grün, KEINE Regression.

5. **Anti-Tibor-Hardcoding-Check**:
   - `grep -ri "tibor\|mustermann\|schumannmiguel" app.py` → leer
   - `grep -rE "^[[:space:]]*'2025-0[1-9]'" app.py` → keine date-hardcoded constants

## §3 API-Contract (vor + nach Switch identisch)

| Endpoint | Method | Response-Schema |
|---|---|---|
| `/api/job/<job_id>` | GET | `{status, klass_summary, tage_detail, ...}` |
| `/api/session/<token>` | GET | siehe v8.23 |
| `/finalize-pdf/<job_id>` | POST | `{pdf_url, state, can_show_pdf, ...}` |

Tour-First darf KEINEN Response-Schema-Bruch verursachen. Test:
```
python3 tools/api_contract_check.py --before <baseline.json> --after <run.json>
```

## §4 Browser-QA (Manual Check)

Nach Switch:

| Browser | Plattform | Test |
|---|---|---|
| Chrome 132+ | macOS Desktop | Voll-Job mit Tibor's 3 PDFs |
| Safari 18+ | iOS | Mobile-Upload + Chat-Drawer |
| Firefox 134+ | Linux Desktop | API-Contract-Antworten |

Akzeptanz:
- Result-Card zeigt korrekte KPIs
- Audit-Trail in `tage_detail` sichtbar
- Chat zeigt KEINE "EUR-from-AI"-Werte
- Polling reagiert auf neue Felder (`ai_required`, `proposed_tour_decision_after_ai`)

## §5 Monitoring & Alerts

### Logs

Filter-Patterns für Render-Log-Stream:
- `[v8-classify] Done` — Erfolg
- `[ai-resolver-mock]` — Mock-Resolver-Calls
- `[bh-core-001] tour_first_active=True` — Feature-Flag-Bestätigung
- `AI_TAX_VALUE_REJECTED` — Anti-Tax-Sanitizer-Trigger
- `AI_LIVE_NOT_APPROVED_PHASE5B` — Live-Call-Versuch ohne Freigabe

### Metrics

| Metric | Threshold | Alert |
|---|---|---|
| Jobs/min | < 10 normal | > 50 → email |
| Crash-Rate | < 0.1% | > 1% → page |
| KI-Calls/Job | < 5 normal | > 20 → email (Kosten-Warning) |
| `failed_timeout`-Rate | < 5% | > 15% → email |
| `pending_reread`-Backlog | < 10 | > 50 → email |

### KI-Kostenkontrolle

| Schwelle | Aktion |
|---|---|
| Pro Job > 20 KI-Calls | Auto-Cap, Rest auf NEEDS_USER |
| Pro Tag > 1000 KI-Calls (alle Jobs) | Auto-Disable `AEROTAX_AI_RESOLVER_MODE=live`, fallback Mock |
| Pro Monat > 50€ KI-Budget | Page + Diskussion |

## §6 Datenschutz

| Check | Status |
|---|:-:|
| PII-Filter in Resolver-Logs (Name/PNR/Email/Birthdate) | ✓ implementiert (Phase 5a) |
| Snapshots vor Persist sanitized | ⌛ implementieren falls live-run |
| KI-Prompt enthält keine PII | ✓ implementiert |
| Cache-Keys enthalten keine PII (nur job_id-prefix + hash) | ✓ implementiert |
| Forbidden-Tax-Keys in KI-Output → reject | ✓ implementiert |

## §7 Cache-Management

Resolver-Cache (`_ai_resolver_cache`):
- TTL 24h pro Eintrag
- Key: `(job_id, datum, kind, ctx_hash)`
- Wird beim Worker-Restart geleert (in-memory)
- Production-Wunsch: persistent-Cache mit Supabase → **NICHT für initial Production-Switch**, kann Phase 9 sein

## §8 Support-Fallback

Wenn User Result anzweifelt:
1. Snapshots aus `_job_chunks_state/<job_id>/` zeigen Phase-für-Phase-Output
2. `ai_resolution_kind` + `ai_value` + `ai_confidence` + `ai_reason` zeigen KI-Begründung
3. `evidence_for` + `evidence_against` + `score_for` + `score_against` zeigen Entscheidungs-Logik
4. `proposed_tour_decision_after_ai` zeigt vorgeschlagene Klassifikation
5. Bei klar falscher Auto-Klassifikation → `force_klass`-Override via Admin-Endpoint (Phase 9)

## §9 Error-Thresholds (Auto-Rollback)

Auto-Rollback `AEROTAX_TOUR_FIRST_CLASSIFIER=1 → 0` bei:

| Trigger | Window | Action |
|---|---|---|
| Crash-Rate > 5% | 1h | Auto-Disable + email |
| Tour-Boundary-Phantome > 10/Job | per-job | Per-Job-Disable |
| Counter-Sanity-Fail (arbeitstage > 230 oder < 50) | per-job | Per-Job-Disable + log |
| `AI_TAX_VALUE_REJECTED` > 3/Job | per-job | Log + escalate |

## §10 Stop-Regeln eingehalten

- ✓ Plan-Doc, keine Ausführung
- ✓ Kein Deploy jetzt
- ✓ Keine Env/Secret-Änderung jetzt
- ✓ Kein Live-Run jetzt
- ✓ Kein Production-Flag-Switch jetzt
- ✓ Rollback dokumentiert
- ✓ Browser-QA spezifiziert
- ✓ KI-Kostenkontrolle definiert
- ✓ Datenschutz dokumentiert
- ✓ Support-Fallback definiert

**Status: Wartet auf User-Freigabe nach Phase 7 Live-Run.**
