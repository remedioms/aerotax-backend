# Operations Runbook Final

Stand: 2026-05-20 (Rel Phase 19).

## §1 Reason Codes & Support Actions

| reason_code | User-Message | Support-Action |
|---|---|---|
| `UPLOAD_MISSING_REQUIRED` | „Pflichtfeld {field} fehlt" | User auf UI-Felder hinweisen |
| `UPLOAD_PERSIST_FAILED` | „Upload-Persist fehlgeschlagen, bitte erneut" | Render-Logs prüfen + Supabase |
| `PAYMENT_ALREADY_USED` | „Zahlung wurde bereits verwendet" | Stripe Dashboard prüfen, ggf. neuen PI |
| `PAYMENT_LOCK_FAILED` | „Zahlungssperre fehlgeschlagen" | DB-Lock-Stale-Detection |
| `CLASSIFICATION_SCHEMA_FAILED` | „Auswertung schlägt fehl — Support" | failed_support, kein Retry, manuell prüfen |
| `JOB_NOT_FOUND` | „Job nicht gefunden" | Token/Job-ID prüfen, ggf. Recovery-Token |

## §2 Wie finde ich einen Job/Session?

```bash
# Per Job-ID
curl https://aerotax-backend.onrender.com/api/job/<job_id>

# Per Recovery-Token (User hat Link)
curl https://aerotax-backend.onrender.com/api/session/<recovery_token>

# Supabase: jobs-Tabelle
# SELECT * FROM jobs WHERE id = '<job_id>';
```

## §3 Wie verifiziere ich Payment?

```bash
# Stripe Dashboard → Payments → suchen via Email/Amount
# OR
# Backend-Check: /api/payment-status/<ref>
curl https://aerotax-backend.onrender.com/api/payment-status/<ref>
```

## §4 Wie prüfe ich document_health?

```python
# Aus Job-Response: result['document_health']
{
  'pipeline': 'v11_cas_primary',
  'lsb_present': True,
  'se_months_count': 12,
  'cas_months_count': 12,
  'detailed_cas_present': True,
  'missing_months_se': [],
  'missing_months_cas': [],
  'ignored_legacy_files': [],
  'warnings': [],
  'status': 'green'
}
```

## §5 Wie regeneriere ich PDF?

PDF kann erst nach `canonical_state=done` und `pdf_allowed=True` neu generiert werden.

```bash
# Per Session-Token
curl -X POST https://aerotax-backend.onrender.com/api/finalize-pdf \
  -d "session_token=<token>"
```

## §6 Wie lösche ich Daten?

```bash
# User-Recall-Token nötig:
curl -X DELETE https://aerotax-backend.onrender.com/api/delete-session/<token>
```

Files werden physisch gelöscht; result_data in DB als deleted markiert.

## §7 DSGVO-Löschungsanfrage

Per Email (Support): User-Email + Recovery-Token → 
- Find session in Supabase
- Set `deleted=True`, `data_deleted_at=now()`
- Delete uploaded PDF blobs (in-memory + Supabase Storage)
- Confirm to User

## §8 Refund

Stripe Dashboard → Payment → Refund. Backend setzt `payment_refunded=True`, blockiert weitere Aktionen für die Session.

## §9 needs_review Handling

User-Action:
- UI zeigt Chat mit Review-Items
- User kann antworten oder „Ohne Klärung fortfahren"
- Nach Antworten: Pipeline rerun mit `review_decisions={...}`

Support-Action: 
- Wenn User stuck: review_decisions manuell injecten via DB-Update
- Letzter Resort: `canonical_state='done'` setzen, User informieren

## §10 failed_retryable Handling

User-Action: Click Retry → free_retry_token consumed → 1 weiterer Versuch ohne Payment

Support-Action: Job-Logs prüfen, ggf. Code-Bug → Update + redeploy + neuer Retry

## §11 Rollback / Feature-Flags

| ENV | Action |
|---|---|
| `AEROTAX_PIPELINE_VERSION=v10_legacy` | NICHT mehr wirksam (alle DP-Reader hart deaktiviert) |
| `AEROTAX_LEGACY_FLUGSTUNDEN_FORENSIK=1` | Forensik-only, NICHT in Production setzen |
| Source-Re-Deploy ohne env-Risk | `gcloud run deploy aerotax-backend --source . --region europe-west3` |

## §12 Incident Response

1. **Render-Outage**: Cloud Run als Fallback (siehe `BH_CORE_001_PRODUCTION_SWITCH.md`)
2. **Sonnet-Outage**: Backend retried 3× mit Backoff; bei 3 Failures → `needs_review` State
3. **Supabase-Outage**: Backend hat Disk-Fallback für Session-State
4. **Stripe-Outage**: Backend blockiert neue Payments, User informieren

## §13 AI-Cost-Spike Response

- Monitor Anthropic-Dashboard für hohe Token-Verbrauch
- Per `AEROTAX_CAS_MAX_PARALLEL=1` (env) Concurrent-Limit reduzieren
- Per `AEROTAX_AI_RESOLVER_MAX_CALLS=0` (env) Live-KI komplett deaktivieren

## §14 Common Support Q&A

| Frage | Antwort |
|---|---|
| Muss User nochmal zahlen? | Nein, free_retry_token deckt 1 Re-Try |
| Sind Daten gelöscht? | Per Delete-Endpoint ja, sonst nach 30d TTL |
| Warum fehlt PDF? | canonical_state ist nicht 'done' — prüfe document_health |
| Warum braucht es Review? | Source-Conflict-Erkennung — User-Bestätigung nötig |
| Welche Dokumente fehlen? | document_health.missing_months_* + warnings |
| Welche Version hat gerechnet? | result['engine_version'] + result['reader_versions'] |
| Was tun bei falscher Homebase? | Session neu starten mit korrektem `base` |

## §15 Privacy Deletion Request

User → support@aerosteuer.de → 
1. Recovery-Token bestätigen
2. Session-Daten löschen
3. Stripe-Refund prüfen
4. Confirmation-Email an User
