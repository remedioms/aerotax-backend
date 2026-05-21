# P0 #90 — Upload-Persistenz-Fail nach Zahlung

**Datum:** 2026-05-14
**Status:** Root-Cause + Fix-Plan vor Diff. **Kein Code geändert. Kein Deploy.**

---

## Root-Cause-Beweis (6 Antworten)

### 1. Wo werden `uploaded_files` vor Zahlung gespeichert?

**Pfad A — `/api/upload-files` (app.py:456-486):**
```python
@app.route('/api/upload-files', methods=['POST'])
def upload_files():
    ref = request.form.get('ref', '').strip()
    if not ref or ref not in _store:
        return jsonify({'error': 'ref not found'}), 404
    saved_count = 0
    for key in _ALL_FILE_KEYS:
        ...
        if normalized:
            _store[ref]['files'][key] = normalized   # ← In-Memory
    # Parallel auf Supabase persistieren — überlebt Render-Restart
    if _store[ref].get('files'):
        _save_uploaded_files_supabase(ref, _store[ref]['files'])  # ← return value NICHT geprüft
    print(f"[upload-files] ref={ref[:8]} {saved_count} Dateien gespeichert")
    return jsonify({'status': 'ok', 'count': saved_count})        # ← liegt immer 200
```

→ Files landen **doppelt**: in-Memory in `_store[ref]['files']` UND in Supabase-Table `uploaded_files`.

**Pfad B — `/api/process` cloud_tasks (app.py:2007-2017):**
```python
try:
    _save_uploaded_files_supabase(sb_ref, files_sb_format)
    ...
except Exception as _se:
    _set_job_failed(job_id, 'WORKER_RESTARTED', ...)
    return jsonify({...'status':'failed'...}), 500
```
→ Try/except greift NUR bei `raise`, NICHT bei `return False`. **Bug-Surface.**

### 2. Was passiert wenn `_save_uploaded_files_supabase` fehlschlägt?

**Aktuelle Implementierung (app.py:366-402):**
```python
def _save_uploaded_files_supabase(ref, files_dict, hours=UPLOAD_TTL_HOURS):
    if not SB_AVAILABLE or not ref:
        return False
    ...
    for key, items in files_dict.items():
        for idx, item in enumerate(items):
            ...
            try:
                rows.append({...})
            except Exception as e:
                print(f"[supabase upload] encode fail {key}/{idx}: {e}")  # ← swallow
    if not rows:
        return False
    try:
        sb.table('uploaded_files').delete().eq('ref', ref).execute()
        for i in range(0, len(rows), 5):
            sb.table('uploaded_files').insert(rows[i:i+5]).execute()
        return True
    except Exception as e:
        print(f"[supabase upload] save fail: {e}")  # ← swallow, return False
        return False
```

→ Bei Supabase-503 oder Schema-Mismatch: **`return False`** + print-only. Kein Raise. Kein Caller-Signal.

### 3. Was macht `/api/process` wenn `uploaded_files` leer/fehlt?

**File-Validation (app.py:1849-1853):**
```python
if not files.get('lsb') or not files.get('se') or not files.get('cas'):
    return jsonify({
        'error': 'Für die Auswertung brauchst du Lohnsteuerbescheinigung, '
                 'Streckeneinsatzabrechnung und Dienstplan/CAS/Roster.'
    }), 400
```

**Reihenfolge im Code:**
1. files aus `request.files` (Direct-Upload, Z.1802)
2. Fallback `_store[ref]['files']` (in-Memory, Z.1822)
3. Fallback `_load_uploaded_files_supabase(ref)` (Z.1831)
4. **Z.1849 — Pflicht-Check:** wenn lsb/se/cas leer → return 400 VOR Payment-Gate
5. Z.1855+ Payment-Gate
6. Z.1921 `_consumed_payment_intents[pi_id]=...` — Payment consumed

→ **File-Check IST vor Payment-Gate.** Wenn alle 3 Fallbacks leer → 400 bevor PI consumed wird. **Gut.**

**Aber kritisches Loch:**
Wenn `_store[ref]['files']` in-Memory noch da ist (Container hat nicht restartet zwischen Upload + Process), Supabase aber leer (weil persist gefailt), dann:
- Z.1822 Fallback liefert Files
- Z.1849 Check passt
- Z.1921 Payment consumed
- Z.2007 (cloud_tasks): `_save_uploaded_files_supabase` versucht erneut zu speichern — **`return False` swallow möglich**
- Cloud Task wird dispatched, Worker landet evtl. auf anderem Container
- Worker (`/api/internal/process-job`) holt Files via `_load_uploaded_files_supabase(ref)` → leer
- Z.2166 worker: `_set_job_failed(job_id, 'UPLOAD_EXPIRED', ...)`
- Aber PI ist consumed → User zahlt, sieht „Dokumente abgelaufen", kann **nicht refunden**

### 4. Wird PaymentIntent/Promo trotzdem consumed?

**Antwort: JA.**

Sequenz im Bug-Pfad:
1. User uploadet (Pfad A) — Supabase save fail (silent) → 200 OK
2. User bezahlt (3DS-Redirect, optional)
3. User triggert /api/process — files aus `_store[ref]` (in-Memory) → check passt
4. Z.1921: `_consumed_payment_intents[pi_id] = utcnow()`
5. Z.2007: erneuter Persistenz-Versuch → fail (silent oder raise)
6. Wenn raise: Job failed, Status WORKER_RESTARTED — **aber `_consumed_payment_intents` bleibt gesetzt**
7. Wenn silent (return False): Cloud Task dispatched, Worker hat keine Files

→ **PI ist consumed in beiden Fällen.** Recovery-Token-Pfad existiert (`_recovery_tokens`), aber wird in #90-Bug-Pfad nicht ausgelöst weil PI-consume vor persist-Check.

### 5. Gibt es eine Rückerstattungs-/Retry-Strategie?

**Refund:** Nein. Code hat keinen Stripe-Refund-Aufruf.

**Retry:** Ja, via `_recovery_tokens` (app.py:721) — User kann mit `free_retry_token` einen 2. /api/process aufrufen ohne Zahlung. ABER:
- Token wird nur generiert **wenn Job zu `failed_retryable` State läuft** (`_set_job_failed` mit retryable=True)
- Bei silent-fail (return False ohne raise) wird `_set_job_failed` NICHT aufgerufen
- Bei raise nach Z.2007: `_set_job_failed(job_id, 'WORKER_RESTARTED', ...)` → retryable=True → Recovery-Token sollte generiert werden — **aber: User sieht Error-Banner, keinen klaren „Retry-Code-Generated"-Hinweis**

### 6. Was sieht User aktuell?

**Best-Case (raise nach Z.2007 — Cloud-Tasks-Mode):**
- HTTP 500 + JSON `{'error': 'Auswertung konnte nicht gestartet werden — Datei-Persistenz fehlgeschlagen.'}`
- Frontend zeigt generischen Error
- Session-Token gültig 24h → User kann nochmal versuchen mit `/api/retry/<token>` → Recovery-Pfad

**Worst-Case (silent fail in `_save_uploaded_files_supabase` Pfad A bei Upload):**
- HTTP 200 bei /api/upload-files
- HTTP 200 bei /api/process (Cloud-Task dispatched)
- 30s später Job-State = `failed`, reason `UPLOAD_EXPIRED`
- User sieht „Deine Dokumente sind abgelaufen — bitte neue Auswertung starten" — **falsche Diagnose**
- **PI ist consumed.** User muss erneut zahlen.

→ **Das ist der echte P0-Schaden.**

---

## Fix-Plan

**Scope-Disziplin:** Nur `_save_uploaded_files_supabase` + `/api/upload-files` + Cloud-Tasks-Persistenz-Pfad in `/api/process`. **Keine** Rechenlogik. **Keine** Frontend-Redesigns außer Error-Message-Anzeige.

**Strategie:** Option A+B kombiniert:
- A: `/api/upload-files` schlägt jetzt mit 503 fehl wenn Supabase-Persist nicht garantiert → User sieht Fehler **vor** Stripe-Eingabe → kann nicht zahlen
- B: In `/api/process` cloud_tasks-Pfad: Wenn `_save_uploaded_files_supabase` failt **VOR Payment-Consume** → 503 + kein PI-Consume; **NACH Payment-Consume** → `_set_job_failed('UPLOAD_PERSIST_FAILED')` mit retryable=True + Recovery-Token

### Konkrete Code-Änderungen (geplant — noch nicht umgesetzt)

#### Change 1: `_save_uploaded_files_supabase` returnt Status + raise bei kritischen Errors

```python
class UploadPersistError(Exception):
    """Raised wenn Supabase-Persist der uploaded_files definitiv fehlschlägt."""
    pass

def _save_uploaded_files_supabase(ref, files_dict, hours=UPLOAD_TTL_HOURS):
    """v15 #90 Fix: returnt True/False + raised UploadPersistError bei Supabase-Fehler.
    Kein silent return False mehr."""
    if not SB_AVAILABLE:
        raise UploadPersistError('Supabase not available — cannot persist uploads')
    if not ref:
        raise UploadPersistError('No ref provided')
    expires = (datetime.utcnow() + timedelta(hours=hours)).isoformat() + 'Z'
    rows = []
    for key, items in files_dict.items():
        for idx, item in enumerate(items):
            try:
                if isinstance(item, tuple):
                    data, fname = item[0], (item[1] if len(item) > 1 else f'{key}_{idx}')
                else:
                    data, fname = item, f'{key}_{idx}'
                rows.append({
                    'ref': ref, 'key': key, 'idx': idx,
                    'filename': fname or f'{key}_{idx}',
                    'data_b64': base64.b64encode(data).decode(),
                    'expires_at': expires,
                })
            except Exception as e:
                # encoding-fail einer Datei → log, aber NICHT silent — wird
                # weiter unten via rows-count erkannt
                app.logger.warning(f"[supabase upload] encode fail {key}/{idx}: {e}")
    if not rows:
        raise UploadPersistError('No files to persist (all encode-failed or empty)')
    try:
        sb.table('uploaded_files').delete().eq('ref', ref).execute()
        for i in range(0, len(rows), 5):
            sb.table('uploaded_files').insert(rows[i:i+5]).execute()
        app.logger.info(f"[supabase upload] ref={ref[:8]}: {len(rows)} Dateien persistiert")
        return True
    except Exception as e:
        # Bei Supabase-Error: harter raise — Caller muss reagieren
        app.logger.error(f"[supabase upload] save fail ref={ref[:8]}: {e}")
        raise UploadPersistError(f'Supabase insert failed: {str(e)[:200]}')
```

**Wichtig:**
- `print(...)` → `app.logger.warning/.error/.info` (kein except-print-only mehr)
- Encode-fail einer einzelnen Datei: **log + continue** (andere Dateien überleben). Bei rows=0 dann raise (alle fehlgeschlagen).
- Supabase-Error: **raise** (Caller muss reagieren)
- Erfolgsfall: return True

#### Change 2: `/api/upload-files` reagiert auf raise

```python
@app.route('/api/upload-files', methods=['POST'])
def upload_files():
    ref = request.form.get('ref', '').strip()
    if not ref or ref not in _store:
        return jsonify({'error': 'ref not found'}), 404
    saved_count = 0
    for key in _ALL_FILE_KEYS:
        files = request.files.getlist(key)
        if files:
            normalized = []
            for f in files:
                try:
                    normalized.append(_normalize_upload(f.read(), f.filename))
                    saved_count += 1
                except Exception as e:
                    app.logger.warning(f"[upload-files] {key}/{f.filename} failed: {e}")
            if normalized:
                _store[ref]['files'][key] = normalized

    # v15 #90 Fix: Persist-Fail muss vom Caller sichtbar sein (Frontend zeigt Banner,
    # User zahlt nicht). KEIN silent return 200 mehr.
    if _store[ref].get('files'):
        try:
            _save_uploaded_files_supabase(ref, _store[ref]['files'])
        except UploadPersistError as e:
            ec = AEROTAX_ERROR_CODES['UPLOAD_PERSIST_FAILED']
            app.logger.error(f"[upload-files] persist fail ref={ref[:8]}: {e}")
            return jsonify({
                'error':        ec['user_message'],
                'reason_code':  'UPLOAD_PERSIST_FAILED',
                'retryable':    ec['retryable'],
                'support':      ec['support'],
            }), 503

    app.logger.info(f"[upload-files] ref={ref[:8]} {saved_count} Dateien gespeichert")
    return jsonify({'status': 'ok', 'count': saved_count})
```

#### Change 3: `/api/process` cloud_tasks-Pfad — KEIN PI-Consume bei Persist-Fail

**Aktuelle Reihenfolge:** Z.1849 files-check → Z.1921 PI-consume → Z.2007 cloud_tasks persist + raise.

**Neue Reihenfolge:** Z.1849 files-check → **NEU: pre-persist (cloud_tasks)** → Z.1921 PI-consume → Z.2007 wird nur defensiver Re-Try.

```python
# v15 #90 Fix: VOR Payment-Consume validieren dass Files persistiert sind.
# Wenn pre-persist fehlschlägt, KEIN PI-consume → kein Geldverlust.
if AEROTAX_EXECUTION_MODE == 'cloud_tasks':
    sb_ref = (request.form.get('ref') or '').strip() or f'auto-pre-{uuid.uuid4().hex[:12]}'
    files_sb_format = {}
    for k, items in files.items():
        if not items:
            continue
        files_sb_format[k] = [(it[0] if isinstance(it, tuple) else it,
                               it[1] if isinstance(it, tuple) and len(it) > 1 else f'{k}.pdf')
                              for it in items]
    try:
        _save_uploaded_files_supabase(sb_ref, files_sb_format)
    except UploadPersistError as _e:
        ec = AEROTAX_ERROR_CODES['UPLOAD_PERSIST_FAILED']
        app.logger.error(f"[process] pre-persist fail ref={sb_ref[:8]}: {_e}")
        # Kein PI-consume, kein Job — User behält Zahlungsrecht
        return jsonify({
            'error':       ec['user_message'],
            'reason_code': 'UPLOAD_PERSIST_FAILED',
            'retryable':   ec['retryable'],
            'support':     ec['support'],
        }), 503
    # Wenn pre-persist klappt → ref ans form-dict heften für Worker
    request.form = request.form.copy() if hasattr(request.form, 'copy') else request.form
    # form ist immutable; wir setzen sb_ref direkt im local dict-build später
    _pre_persisted_ref = sb_ref
else:
    _pre_persisted_ref = None
```

→ form-immutability ist tricky in Flask; wir setzen `form['ref'] = sb_ref` an der bestehenden Stelle (Z.1935). Pre-persist-Block muss VOR Z.1849 dabei nicht eingreifen — files-check bleibt unverändert.

#### Change 4: AEROTAX_ERROR_CODES Eintrag

```python
'UPLOAD_PERSIST_FAILED': {
    'user_title':   'Dokumente konnten nicht sicher gespeichert werden',
    'user_message': 'Wir konnten deine Unterlagen gerade nicht zuverlässig zwischenspeichern. '
                    'Bitte lade sie in 1-2 Minuten erneut hoch — es wurde noch keine Zahlung '
                    'belastet.',
    'retryable':    True,
    'support':      True,
},
```

#### Change 5: Cleanup — alte Z.2007-Block-Logik vereinfachen

Da pre-persist nun VOR PI-Consume passiert, ist der alte Block bei Z.2007 **redundant**. Wir können ihn entfernen oder als defensive Re-Persist behalten (idempotent — delete+insert).

**Entscheidung:** Behalten als idempotent defensive Re-Persist. Falls er failt, ist es nicht-mehr User-Money-Loss (PI noch nicht consumed im neuen Flow). Aber: Wenn er failt **nach** PI-consume (mit Race-Verschiebung), brauchen wir noch sauberes `_set_job_failed('UPLOAD_PERSIST_FAILED')` statt `'WORKER_RESTARTED'`.

---

## Tests (geplant — alle vor Deploy grün)

Neue Test-Datei: `tests/test_upload_persist_p0_90.py`

| Test-Name | Verifiziert |
|---|---|
| `test_upload_persist_failure_blocks_process` | `/api/upload-files` mit gemocktem Supabase-Error → 503 + reason_code='UPLOAD_PERSIST_FAILED' |
| `test_process_missing_uploaded_files_returns_structured_error` | `/api/process` ohne files, ohne _store, leeres Supabase → 400 mit user-message |
| `test_paid_process_missing_files_does_not_create_job` | mock Stripe-PI succeeded + leeres Supabase + leeres _store → 400, **kein Job in `_jobs`** |
| `test_paid_process_missing_files_not_silent_success` | gleicher Setup → response.status_code != 200 |
| `test_promo_process_missing_files_not_start_job` | promo_code valid + leere files → 400, no job |
| `test_user_message_upload_persist_failed` | reason_code='UPLOAD_PERSIST_FAILED' in response → user_message enthält „nicht zuverlässig" + „Zahlung" |
| `test_no_except_print_only_in_save_uploaded_files_supabase` | Static check: regex `except\s+\w*:\s*\n\s+print` in `_save_uploaded_files_supabase`-Block muss 0 matches haben |
| `test_pi_not_consumed_when_persist_fails_pre_process` | mock Supabase-Error in pre-persist → `_consumed_payment_intents` bleibt leer für pi_id |
| `test_persist_success_then_pi_consumed` | happy-path → `_consumed_payment_intents[pi_id]` ist gesetzt + job-state 'pending' |
| `test_upload_persist_raises_on_supabase_503` | mock sb.table().insert().execute() raised → `_save_uploaded_files_supabase` raised UploadPersistError |
| `test_upload_persist_returns_true_on_success` | happy-path → returns True |
| `test_upload_persist_raises_on_empty_rows` | files_dict mit allen encode-fail → raises UploadPersistError |

**Bestehende Tests die nicht brechen dürfen:**
- `tests/test_cloud_tasks.py` — Cloud-Tasks-Pfad
- `tests/test_state_machine.py` — failed_retryable mit reason_code
- `tests/test_supabase_timeout_fix.py` — Supabase-Helper

---

## Pre-Deploy-Antworten

### Wird `PaymentIntent` consumed wenn der Fix aktiv ist?

**Nein** (im neuen Flow):
- Pre-persist passiert vor `_consumed_payment_intents[pi_id]=...`
- Wenn pre-persist failt → 503 mit reason_code → kein PI-consume, kein Job
- User kann mit gleichem `pi_id` retry. PI ist multi-use bis explizit consumed.
- Idempotenz: bei retry mit gleichem `pi_id` läuft der Cycle nochmal — wenn diesmal Persist klappt, dann PI-consume + Job-Dispatch. Sauber.

### User-Message

Bei 503 mit reason_code `UPLOAD_PERSIST_FAILED`:
> **Dokumente konnten nicht sicher gespeichert werden**
>
> Wir konnten deine Unterlagen gerade nicht zuverlässig zwischenspeichern. Bitte lade sie in 1-2 Minuten erneut hoch — es wurde noch keine Zahlung belastet.

Im Frontend wird über `reason_code` der Banner gezeigt (Mechanik existiert bereits via `AEROTAX_ERROR_CODES`).

### Welche Race-Conditions bleiben?

- Sub-second race zwischen `_save_uploaded_files_supabase` erfolgreich → `_consumed_payment_intents.update` → Cloud-Run-Container-kill genau in der Mikrosekunde dazwischen. **Sehr selten**, war auch vorher schon Risiko. Mitigation würde transaktionale Supabase-Logik brauchen → out-of-scope für P0 #90.

- Multi-Container-Replay des PI bleibt bestehen — **das ist #96**, separater P0.

---

## Diff-Größe (geschätzt)

| Datei | LoC |
|---|---|
| `app.py` `_save_uploaded_files_supabase` | ~40 (refactor) |
| `app.py` `/api/upload-files` | ~10 (add try/except + return 503) |
| `app.py` `/api/process` pre-persist Block | ~30 (new block) |
| `app.py` AEROTAX_ERROR_CODES | ~7 (new entry) |
| `app.py` `UploadPersistError`-Klasse | ~3 |
| `tests/test_upload_persist_p0_90.py` | ~250 (new file) |
| **Total Backend-Diff** | **~90 prod-LoC + 250 test-LoC** |

**Kein Frontend-Diff nötig.** Frontend kennt `reason_code` bereits aus `AEROTAX_ERROR_CODES`-Pattern.

---

## Open Questions for User

1. **OK mit Strategie A+B kombiniert?** Pre-persist vor PI-consume + Upload-Endpoint blockt schon vor Stripe-Eingabe.
2. **OK mit `app.logger` statt `print()`?** Verändert log-format in Cloud Run (json-structured statt stdout-plain). Sollte sauberer sein, aber ist sichtbare Logging-Änderung.
3. **OK damit dass `Z.2007`-Block redundant bleibt** (defensive idempotent) statt entfernt?
4. **OK mit `UPLOAD_PERSIST_FAILED` als reason_code** (vs. Re-use von `WORKER_RESTARTED`)?

Nach Antwort: Code-Diff zeigen → Tests grün → Freigabe abwarten → Deploy.
