# P0 #96 — PaymentIntent Replay-Schutz Multi-Container

**Datum:** 2026-05-14
**Status:** Root-Cause + Plan vor Diff. **Kein Code geändert. Keine Migration ausgeführt. Kein Deploy.**

---

## Root-Cause-Beweis

### 1. Wo wird aktuell geprüft / gesetzt / cleanup?

**Storage-Init (app.py:545):**
```python
_consumed_payment_intents = {}  # pi_id → consumed_at; verhindert Replay
```

**Check (app.py:1917-1921):**
```python
# Replay-Schutz: PI darf nur 1x für /api/process genutzt werden
if pi_id and pi_id in _consumed_payment_intents:
    return jsonify({
        'error': 'Diese Zahlung wurde bereits für eine Auswertung verwendet. Pro Bezahlung gibt es eine Auswertung.'
    }), 402
```

**Set (app.py:2019-2025):**
```python
if pi_id:
    _consumed_payment_intents[pi_id] = datetime.utcnow()
    # Alte Einträge >25h aufräumen
    cutoff_pi = datetime.utcnow() - timedelta(hours=25)
    for k, ts in list(_consumed_payment_intents.items()):
        if ts < cutoff_pi:
            _consumed_payment_intents.pop(k, None)
```

**Cleanup:** Inline beim Set (line 2022-2025), 25h Cutoff.

### 2. Weitere Schutzschichten

| Layer | Code | Multi-Container-tauglich? |
|---|---|---|
| `_store[ref]['paid']` Webhook-State | line 545+, 609, 1916 | **Nein** — `_store` ist in-memory pro Container |
| Stripe PI direct-verify | line 1923-1937 (`stripe.PaymentIntent.retrieve`) | Ja, aber idempotent → mehrere succeeded-Lookups bestehen alle |
| `ref` ↔ `pi_ref` Match | line 1930 | Container-übergreifend, weil PI-Metadata serverseitig |
| `_consumed_payment_intents` | line 1918, 2019 | **Nein** — dict pro Container |
| Job-ID Uniqueness (UUID) | line 1992 | Ja, aber rein-statistisch — keine Garantie gegen "1 PI → 2 Jobs" |
| Webhook-Idempotenz `_processed_stripe_events` | line 537-547 | **Nein** — dict pro Container |

### 3. Warum reichen sie in Multi-Container nicht?

Cloud Run = mehrere Container parallel. `_consumed_payment_intents` ist Python-Dict im Process-Memory. Container A weiß nicht, was Container B im Dict hat.

Stripe PI direct-verify ist **nicht atomar**: `stripe.PaymentIntent.retrieve(pi_id)` liefert beim 2. Aufruf immer noch `status='succeeded'`, weil PI eben succeeded ist. Stripe selbst verhindert NICHT, dass derselbe PI zweimal "verbraucht" wird — das ist Aufgabe der Anwendung.

### 4. Was passiert bei 2 parallelen /api/process mit gleichem pi_id?

Setup: Cloud Run mit ≥2 Containern (`min-instances=1`, max=5). User-Doppelklick auf „Auswertung starten" + Loadbalancer routet 2 Requests auf verschiedene Container.

| Step | Container A | Container B |
|---|---|---|
| 1 | `pi_id in _consumed_payment_intents` → False | `pi_id in _consumed_payment_intents` → False |
| 2 | Stripe-verify → succeeded ✓ | Stripe-verify → succeeded ✓ |
| 3 | Pre-Persist Supabase → OK | Pre-Persist Supabase → OK (idempotenter delete+insert) |
| 4 | `_consumed_payment_intents[pi_id] = now()` in **A** | `_consumed_payment_intents[pi_id] = now()` in **B** |
| 5 | Job 1 erstellt + Cloud-Task dispatched | Job 2 erstellt + Cloud-Task dispatched |
| 6 | Worker startet Auswertung 1 | Worker startet Auswertung 2 |

**User sieht:** 2 verschiedene `session_token`s in 2 Tabs → 2 parallele Auswertungen → 2 PDFs für 1 Zahlung. Schaden:
- Double-Spending bei Anthropic-API-Costs (≈ 0.50€/Job)
- Double-Storage in Supabase
- Inkonsistente DB-Counts/Stats
- User-Verwirrung welcher Session-Token der "richtige" ist

---

## Fix-Plan

### Schema (Supabase-Migration)

**Datei:** `supabase_migrations/20260514_payment_intent_consumptions.sql`

```sql
create table if not exists public.payment_intent_consumptions (
  payment_intent_id text primary key,
  ref               text,
  job_id            uuid,
  consumed_at       timestamptz not null default now(),
  status            text not null default 'claimed',
  metadata          jsonb not null default '{}'::jsonb
);

create index if not exists idx_pi_consumptions_ref     on public.payment_intent_consumptions(ref);
create index if not exists idx_pi_consumptions_status  on public.payment_intent_consumptions(status);

alter table public.payment_intent_consumptions enable row level security;
-- Service-Role-Key bypasses RLS; explizit kein public-policy.
```

- Primary Key auf `payment_intent_id` → atomic claim via `INSERT ... ON CONFLICT` raise.
- `status` tracked: `'claimed'` (initial) → `'done'` / `'failed_retryable'` / `'failed_support'` (updated wenn Job state set).
- `job_id` lets User über Lock-Conflict-Response wissen: "Dein PI gehört zu Job XY".

### Code-Änderungen

**1. Helper `_try_consume_payment_intent_supabase(pi_id, ref, job_id)`** (new function):

Atomic claim via Supabase. Returns einen Tupel `(outcome, existing_record)`:
- `('claimed', None)`: Erste Erfolg — Caller darf processen
- `('already_used', record_dict)`: Conflict — Lock existiert schon, Record geliefert
- `('lock_unavailable', None)`: Supabase down/timeout — Caller muss fail-closed reagieren

Detection: `INSERT ... execute()` → bei conflict raised supabase-py PostgrestException mit `'23505'` oder `'duplicate key'` im Error-Body. Catch + lookup existing record.

**2. `/api/process` Integration** (line ~1948):

Reihenfolge:
```
files-check → payment-gate-check → pre-persist (#90) → PI-Lock (#96) → PI-Consume → Job-Creation
```

PI-Lock kommt **nach** pre-persist (#90 schon validiert dass Files da sind) und **vor** Job-Creation. Bei `'already_used'` → 409 + reason_code `PAYMENT_ALREADY_USED` + existing job_id wenn vorhanden.

**3. Lock-Status-Update nach Job-Completion:**

In `_set_job_failed` UND in der Done-Path-Stelle (line ~2304+): wenn Job finalisiert → Lock-Status updaten. Damit kann der Cleanup-Cron später unterscheiden: `claimed` ohne `job_id` für >24h ist orphan, `done`/`failed_*` mit `job_id` ist Audit-Record.

**4. In-Memory-Cache als Optimisation behalten (NICHT als Source-of-Truth):**

`_consumed_payment_intents` bleibt als L1-Cache pro Container. Wenn dort drin → schneller short-circuit. Wenn nicht → Supabase-Lock-Insert. Cache wird bei lock-claim gefüllt.

**5. `_release_payment_intent_lock` bleibt absichtlich NO-OP:**

User-Anweisung: "kein Deadlock". Lösung: Lock bleibt aber Status wird upgedatet zu `'failed_retryable'`. Frontend nutzt **Recovery-Token-Pfad** (existiert bereits) für Re-Try — PI-Lock muss nicht released werden.

### AEROTAX_ERROR_CODES neu

```python
'PAYMENT_ALREADY_USED': {
    'user_title':   'Diese Zahlung ist bereits einer Auswertung zugeordnet',
    'user_message': 'Diese Zahlung wurde bereits für eine Auswertung verwendet. '
                    'Öffne deine bestehende Auswertung mit deinem Zugangscode '
                    'oder kontaktiere den Support.',
    'retryable':    False,
    'support':      True,
    'next_actions': [
        {'type': 'open_existing', 'label': 'Bestehende Auswertung öffnen'},
        {'type': 'contact_support', 'label': 'Support kontaktieren'},
    ],
},
'PAYMENT_LOCK_FAILED': {
    'user_title':   'Zahlung konnte gerade nicht verarbeitet werden',
    'user_message': 'Der Replay-Schutz für Zahlungen ist gerade nicht erreichbar. '
                    'Bitte in 1-2 Minuten erneut versuchen — keine Doppelbelastung.',
    'retryable':    True,
    'support':      True,
    'next_actions': [
        {'type': 'retry', 'label': 'Erneut versuchen'},
        {'type': 'contact_support', 'label': 'Support kontaktieren'},
    ],
},
```

### Graceful Degradation

**Wenn Tabelle fehlt / Supabase down / Lock unavailable:**

| Pfad | Verhalten |
|---|---|
| `is_paid` (echte Zahlung) | **Fail-closed:** 503 + `PAYMENT_LOCK_FAILED`. Lieber temporär nicht bedienen als Double-Spending zulassen |
| `is_promo` (Promo-Code) | **Pass:** kein pi_id involved → kein Lock nötig → Job geht durch |
| `is_free_retry` (Recovery-Token) | **Pass:** kein pi_id involved → kein Lock nötig |
| `allow_unpaid` (Dev-Flag) | **Pass:** Dev-Pfad nicht produktionsrelevant |

Aktivierung: nur wenn Migration ausgeführt → ohne Migration läuft Code in den Lock-Pfad und kriegt `'lock_unavailable'` → fail-closed für paid PIs. Daher **erst Migration ausführen, dann deployen**.

---

## Tests (10 + Static-Checks)

Neue Test-Datei: `tests/test_payment_intent_lock_p0_96.py`

1. `test_payment_intent_consume_atomic_first_wins` — mock insert success → outcome='claimed'
2. `test_payment_intent_consume_atomic_second_rejected` — mock 2nd insert raises 23505 → outcome='already_used', existing record returned
3. `test_parallel_process_same_pi_creates_one_job` — 2 sequential calls (sim parallel) → 1 job created, 2nd returns 409
4. `test_parallel_process_same_pi_enqueues_one_task` — mock enqueue counter → 1× called
5. `test_process_lock_survives_memory_restart` — clear `_consumed_payment_intents` between calls (sim container restart), but Supabase keeps row → 2. call gets 409
6. `test_payment_already_used_returns_structured_error` — response has reason_code, user_message, next_actions
7. `test_promo_flow_not_blocked_by_pi_lock` — promo_code path → kein Lock-Aufruf, Job geht durch
8. `test_no_in_memory_consumed_payment_intents_required_for_cloud_tasks` — Static-check: source enthält Supabase-Lock-call vor Job-Creation
9. `test_migration_has_primary_key_on_payment_intent_id` — Parse SQL file, verify PRIMARY KEY constraint
10. `test_lock_insert_failure_returns_payment_lock_failed` — mock Supabase 503 → outcome='lock_unavailable' → 503 + reason_code

Bonus-Tests:
- `test_free_retry_not_blocked_by_pi_lock` — Recovery-Token-Pfad unblockiert
- `test_lock_status_updated_on_job_done` — wenn Job finalisiert, Lock-Status='done'
- `test_in_memory_cache_short_circuits_lock` — wenn pi_id im L1-Cache, kein Supabase-Call

---

## Vor-Deploy-Antworten (5 Pflichtfragen)

### 1. Wann wird Lock gesetzt?

In `/api/process`, nach **payment-gate** und nach **pre-persist (#90)**, **vor** `_consumed_payment_intents` + Job-Creation. Genau eine atomic-INSERT pro PI.

### 2. Was passiert bei Parallel-Request?

- Container A: `INSERT` succeeded → `'claimed'` → Job 1 erstellt
- Container B: `INSERT` raised 23505 → SELECT existing → `'already_used'` + record(job_id=Job1) → **409** + `reason_code='PAYMENT_ALREADY_USED'` + `existing_job_id=Job1`
- User auf Container B sieht: „Bestehende Auswertung öffnen" (mit Code), kein neuer Job, kein neuer Cloud-Task.

### 3. Was passiert wenn Lock gesetzt, aber Job-Creation fehlschlägt?

Lock bleibt mit `status='claimed'` und `job_id=neuer_uuid`. Wenn Job dann später failed:
- `_set_job_failed` updated Lock-Row zu `status='failed_retryable'`
- User bekommt Recovery-Token wie üblich → kann via `/api/process` mit `free_retry_token` einen NEUEN Job starten (kein pi_id, kein Lock-Check)
- PI-Lock-Row bleibt als Audit; lockt PI für ewig (das ist OK — eine Zahlung = ein Auswertungs-Slot, der Recovery-Token nimmt die Wiederholung)

**Kein Deadlock:** User hat immer Recovery-Token-Pfad.

### 4. Was sieht User?

**Erster Request (gewinnt):** Normaler Flow — Job startet, Session-Token, etc.

**Zweiter Request (verliert):**
- HTTP 409
- JSON:
```json
{
  "ok": false,
  "reason_code": "PAYMENT_ALREADY_USED",
  "user_title": "Diese Zahlung ist bereits einer Auswertung zugeordnet",
  "user_message": "Diese Zahlung wurde bereits für eine Auswertung verwendet. Öffne deine bestehende Auswertung mit deinem Zugangscode oder kontaktiere den Support.",
  "retryable": false,
  "support": true,
  "existing_job_id": "abc-...",
  "next_actions": [
    {"type": "open_existing", "label": "Bestehende Auswertung öffnen"},
    {"type": "contact_support", "label": "Support kontaktieren"}
  ]
}
```

**Lock-unavailable (Supabase down):**
- HTTP 503
- reason_code=`PAYMENT_LOCK_FAILED` → User soll in 1-2 Min nochmal probieren

### 5. Cleanup alter Locks

**Aktive Cleanup-Logik (existiert bereits in `_run_cleanup_loop` ~line 5173+):**
- Erweitern: alle 30min löschen wo `status IN ('done', 'failed_support')` UND `consumed_at < now() - 30 Tage`
- `status='claimed'` Locks **niemals** löschen (potentielle orphan races — Support-Case)
- `status='failed_retryable'` Locks **niemals** löschen (Recovery-Token könnte noch genutzt werden)

Storage-Aufwand: 1 Row ≈ 100 Bytes. Bei 1000 Auswertungen/Monat = 100KB/Monat. Vernachlässigbar.

---

## Diff-Größe (Schätzung)

| Datei | LoC | Neu/Refactor |
|---|---|---|
| `app.py` neue Helper `_try_consume_payment_intent_supabase` + `_update_payment_intent_lock_status` | ~70 | neu |
| `app.py` `/api/process` Integration | ~30 | Insert |
| `app.py` AEROTAX_ERROR_CODES (2 Einträge) | ~25 | neu |
| `app.py` `_set_job_failed` Lock-Update | ~5 | minimal-invasiv |
| `app.py` Cleanup-Loop-Erweiterung | ~5 | minimal-invasiv |
| `supabase_migrations/20260514_payment_intent_consumptions.sql` | ~15 | **neu, NICHT auto-execute** |
| `tests/test_payment_intent_lock_p0_96.py` | ~350 | neu |
| **Total Backend-Diff** | **~485 LoC** | |

**Kein Frontend-Diff.** Frontend kennt reason_code-Pattern bereits.

## Migration-Workflow

1. Code-Diff zeigen
2. Migration-File zeigen
3. Tests lokal grün
4. **Du gibst Migration-Freigabe** → ich führe migration via Supabase aus
5. Du gibst Code-Deploy-Freigabe → `gcloud run deploy`
6. Post-Deploy-Smoke gegen aktualisierte Cloud Run Revision

Wichtig: Migration vor Deploy ausführen. Sonst läuft Code in `lock_unavailable` für paid PIs → 503. Promo + Recovery-Token funktionieren weiter (kein PI involved).
