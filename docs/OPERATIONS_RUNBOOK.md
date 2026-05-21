# AeroTAX — Operations Runbook

Stand: 2026-05-20. Pflicht-Runbook für Support + Launch.

## §1 Reason-Codes (aus `_classify_job_state`)

| reason_code | User-sichtbar | Support-Aktion |
|---|---|---|
| `OK` | „Auswertung läuft" | Polling fortsetzen |
| `PROCESSING_TIMEOUT` | „Auswertung dauert länger als üblich" | Cloud-Tasks-Queue prüfen, Worker-Status |
| `READER_FAIL_PDF` | „PDF konnte nicht gelesen werden" | Datei-Format prüfen, ggf. neu hochladen lassen |
| `READER_FAIL_API` | „API-Fehler beim Lesen" | Anthropic-API-Status prüfen (`/api/health`) |
| `CLASSIFICATION_SCHEMA_FAILED` | „Daten unvollständig" | Snapshots `_job_chunks_state/<job_id>/` prüfen |
| `MAX_TOKENS_EXCEEDED` | „Datei zu groß für KI-Lesung" | PDF in kleinere Teile splitten, neu hochladen |
| `PENDING_REREAD` | „Datei erhalten. Erneute Auswertung läuft." | Job nochmal triggern wenn nicht erfolgt |
| `NEEDS_USER_REVIEW` | „Klärung nötig" mit Review-Items | User soll Review-Items beantworten |
| `PAYMENT_REQUIRED` | „Zahlung ausstehend" | Stripe-Session prüfen |
| `PAYMENT_LOCK_CONFLICT` | „Diese Zahlung wurde bereits verarbeitet" | PaymentIntent-Lock in Supabase prüfen |
| `UPLOAD_PERSIST_FAIL` | „Hochladen fehlgeschlagen" | Supabase-Storage-Status prüfen |
| `SESSION_EXPIRED` | „Token abgelaufen" | User neuen Job starten |
| `JOB_DELETED` | „Daten gelöscht" | Final, kein Recovery möglich |
| `FAILED_RETRYABLE` | „Auswertung fehlgeschlagen — bitte erneut versuchen" | Retry-Button anbieten, max 2 Retries (P0 #75 fix) |
| `FAILED_TERMAL` | „Auswertung fehlgeschlagen — Support kontaktieren" | Logs ziehen, User refund evaluieren |

## §2 Wo Logs liegen

| System | Zugang | Filter-Pattern |
|---|---|---|
| **Render Backend** | Render Dashboard / Render API mit `RENDER_API_KEY` | `[v8-classify]`, `[ai-resolver]`, `[finalize-pdf]`, `[queue]` |
| **Cloud Run** (falls aktiv) | `gcloud logging read` | `severity>=WARNING resource.type=cloud_run_revision` |
| **Supabase** | Supabase Dashboard | `payment_intent_lock`-Tabelle, `uploaded_files`-Tabelle |
| **Anthropic** | Anthropic Dashboard / Usage-API | Calls per day, costs |
| **Stripe** | Stripe Dashboard | PaymentIntents, Refunds |
| **Cloudflare Pages** | Cloudflare Dashboard | Build-Logs, kein Runtime-Log (statisches Hosting) |

Render-Log-API:
```
GET https://api.render.com/v1/logs?ownerId=tea-d7np5om8bjmc73909ea0&resource=srv-d7o6qbe8bjmc7398acdg&type=app
```

## §3 Wie Support einen Job/Token findet

1. **User gibt token** → `GET /api/session/<token>` → liefert `job_id` + `canonical_state` + `reason_code`
2. **User gibt nur Email** → keine Lookup (DSGVO — kein Email-Index)
3. **Job-ID direkt** → `GET /api/job/<job_id>`
4. **Snapshots prüfen**:
   ```
   ls _job_chunks_state/<job_id>/
   # Erwartete Files: 01_upload.json, 02_lsb_facts.json, ..., 09_final.json
   ```

## §4 Recovery-Token-Prozess

Recovery-Token werden **gehashed** in Supabase gespeichert (recovery_pepper, P0 #10).

Support-Schritt:
1. User gibt Token → `_load_session_from_supabase(token_hash)` versucht Match
2. Wenn 30+ Tage alt → `SESSION_EXPIRED`
3. Wenn `session.consumed=true` → konsumed, kein Re-Use
4. Wenn `session.consumed=false` und `state=done` → Recovery-Banner zeigt, Re-Download möglich

## §5 Refund/Payment Support

| Situation | Aktion |
|---|---|
| User hat gezahlt, Job ist `failed_terminal` | Stripe-Refund manuell ausführen, User informieren |
| PaymentIntent Replay-Versuch | Lock-Status in Supabase prüfen, ggf. unlock + retry |
| User reklamiert Doppel-Zahlung | Stripe-Dashboard prüfen, Refund einer Charge |

## §6 KI-Fail-Prozess

| KI-Status | Aktion |
|---|---|
| Anthropic 429 (Rate Limit) | Backoff via `_claude_with_retry` (3 retries) — meist automatisch resolved |
| Anthropic 5xx (Server Error) | gleich wie 429 |
| Anthropic 401 (Auth) | KEIN retry, Key prüfen — `gcloud run services update --update-env-vars ANTHROPIC_API_KEY=...` |
| Anthropic 404 / model deprecated | Modell-Konfiguration prüfen (`_AI_RESOLVER_MODEL`) |
| Cost-Spike (>50€/Monat) | Optional `AEROTAX_AI_RESOLVER_MODE=mock` als Notbremse |

## §7 Upload-Fail-Prozess

| Symptom | Diagnose | Aktion |
|---|---|---|
| `UPLOAD_PERSIST_FAIL` | Supabase Storage down? | Supabase-Status prüfen, Health-Check `/api/health` |
| `Upload size limit` | PDF > Limit | User-Hinweis, kleinere Datei |
| Timeout während Upload | Slow connection | Retry-Hinweis |

## §8 PDF-Fail-Prozess

| Symptom | Diagnose |
|---|---|
| `canonical_state=done` aber `pdf_url=null` | ReportLab-Error in `_render_pdf`; Logs prüfen |
| PDF-Bytes korrupt | Re-Trigger via `/finalize-pdf/<job_id>` manuell |

## §9 Rollback-Prozess (Production)

**Wenn `AEROTAX_TOUR_FIRST_CLASSIFIER=1` Production-Switch fehlerhaft:**

```bash
# Cloud Run — niemals --set-env-vars (BUG-005-Lesson)
gcloud run services update aerotax-backend \
  --region europe-west3 \
  --update-env-vars AEROTAX_TOUR_FIRST_CLASSIFIER=0
```

**Wenn Render-Backend rollback nötig:**
- Render Dashboard → Services → aerotax-backend → Manual Deploy → vorherige Commit-SHA wählen
- ODER: `git revert <bad-commit> && git push origin main` → Auto-Deploy

**Wenn Cloudflare-Pages-Frontend rollback:**
- Cloudflare Dashboard → Pages → aerosteuer → Deployments → Rollback to previous

## §10 Feature-Flag OFF Prozess

```bash
# Standard-OFF (Default-Verhalten)
gcloud run services update aerotax-backend \
  --region europe-west3 \
  --update-env-vars AEROTAX_TOUR_FIRST_CLASSIFIER=0
```

Per-Job-OFF (testweise):
```
# Stand 2026-05-20: nicht implementiert. Job-spezifischer Override fehlt.
# Empfehlung: vor Production-Switch Code-Review für Per-Job-Override.
```

## §11 Monitoring (Alert-Schwellen)

| Metric | Normal | Warning | Critical |
|---|---:|---:|---:|
| Jobs/min | < 10 | > 30 | > 50 (Skalierungs-Bedarf) |
| Crash-Rate | < 0.1% | > 0.5% | > 1% (Auto-Rollback) |
| KI-Calls/Job (avg) | < 5 | > 10 | > 20 (Per-Job-Cap) |
| KI-Calls/Tag (gesamt) | < 200 | > 500 | > 1000 (Mock-Fallback) |
| `failed_timeout`-Rate | < 5% | > 10% | > 15% |
| Stripe-Payment-Replay-Attempts | < 1/Tag | > 5/Tag | > 20/Tag (Investigation) |
| `pending_reread`-Backlog | < 5 | > 20 | > 50 |
| Anthropic Cost/Monat | < 30€ | > 50€ | > 100€ |

## §12 Support-Eskalation

**Stufe 1 (Standard)**: Snapshots checken, reason_code identifizieren, User-Antwort mit Boilerplate.

**Stufe 2 (Recovery)**: Recovery-Token-Mechanismus nutzen, ggf. manueller PDF-Re-Render via `/finalize-pdf/<job_id>`.

**Stufe 3 (Engineering)**: Logs + Snapshots + Job-ID an Engineering. KEINE PII in Tickets — Token/job_id reichen.

## §13 Akzeptanz: Support kann ohne Engineering-Wissen sehen

| Frage | Wie? |
|---|---|
| Was ist passiert? | `GET /api/session/<token>` → `reason_code` + Snapshots |
| Muss User nochmal zahlen? | `payment_intent_lock`-Status in Supabase prüfen |
| Existiert PDF? | `state=done` + `pdf_allowed=true` + `pdf_url` |
| Wurden Dokumente gelöscht? | `UPLOAD_TTL_HOURS`-Check, `_delete_uploaded_files_supabase`-Aufruf in Logs |
| Ist Recovery möglich? | `session.consumed=false` + `state in (done, needs_review)` |
