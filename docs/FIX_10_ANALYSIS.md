# P0 #10 — RECOVERY_SECRET default empty (Analyse)

**Datum:** 2026-05-14
**Status:** Reine Analyse. **Kein Code geändert. Kein Deploy.**

---

## 1. Env-Check (Cloud Run)

```
RECOVERY_SECRET           present=false
AEROTAX_QA_SEED_TOKEN     present=false
ADMIN_TOKEN               present=false
SUPPORT_TOKEN             present=false
INTERNAL_TOKEN            present=false
AEROTAX_ALLOW_BOOT_WITHOUT_KEY  present=false
```

**Bestätigt: `RECOVERY_SECRET` ist NICHT in Cloud Run env gesetzt.** Default `''` ist live aktiv.

(Wert nicht angefragt, nicht ausgegeben.)

---

## 2. Code-Stellen — RECOVERY_SECRET wird an 3 Stellen genutzt

### 2.1 `/api/support` (app.py:6587-6590) — IP-Hash-Pepper für Support-Anfragen

```python
'ip_hash': _hashlib.sha256(
    (request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
     + os.environ.get('RECOVERY_SECRET','')).encode()
).hexdigest()[:12],
```

**Bei default `''`:**
- Hash kollabiert zu `sha256(ip)[:12]`
- IPv4 hat ~4.3 Mrd. Werte → in Sekunden rainbow-tablebar
- Wer Supabase `support_requests` liest, kann jeden Hash-Wert auf eine konkrete IP zurückrechnen

**Auswirkung:** DSGVO-Schwäche (IP-Pseudonymisierung gebrochen). Nicht direkt Auth-Bypass.

### 2.2 `/api/admin/support-list` (app.py:6678-6687) — Admin-Auth-Token

```python
auth = request.headers.get('X-Admin-Token', '')
expected = os.environ.get('RECOVERY_SECRET', '')
if not expected or not auth or not hmac.compare_digest(auth, expected):
    return jsonify({'error': 'Unauthorized'}), 401
```

**Bei default `''`:**
- `not expected` ist truthy → Auth schlägt fehl → **401 immer** → fail-closed ✓
- Aber: wenn jemand das Secret später auf `''` lässt und denkt er hat es gesetzt, dann ist der Admin-Endpoint silent unbenutzbar (Verfügbarkeits-Problem, nicht Security)
- Wenn jemand es auf `'a'` setzt → trivial brute-forcable

**Auswirkung:** Aktuell sicher (fail-closed), aber kein Schutz gegen versehentlich-leere/triviale Secrets.

### 2.3 `/api/qa/<qid>/upvote` (app.py:6910) — IP-Hash-Pepper für Upvote-Dedup

```python
ip_hash = _hashlib.sha256((ip + os.environ.get('RECOVERY_SECRET','')).encode()).hexdigest()[:8]
```

**Bei default `''`:** Wie #2.1 — Hash kollabiert zu `sha256(ip)[:8]`. Plus: 8 Zeichen = 32 Bit → noch billiger als #2.1.

**Auswirkung:** Upvote-Dedup kann durch IP-Rotation umgangen werden (kein Auth-Schaden, Spam-Schutz erschwert). DSGVO-Schwäche wie #2.1.

### 2.4 NICHT betroffen: User-facing Recovery-Tokens

`_recovery_tokens` (app.py:5074) und `_is_valid_recovery_token` (app.py:5077) sind **UUID-basiert**, nicht `RECOVERY_SECRET`-basiert. Diese Tokens sind nicht durch das fehlende Secret kompromittiert.

Der Audit-Eintrag #10 in `BUG_AUDIT_100.md` schrieb fälschlich „Recovery-Tokens via sha256(ip+'')". Korrekt: **IP-Hashes für Audit/Spam + Admin-Auth-Token**, nicht User-Recovery-Tokens.

---

## 3. Default production-reachable?

| Verwendung | Default `''` reachable? | Echte Auswirkung |
|---|---|---|
| `/api/support` ip_hash | **Ja**, jeder Support-Submit | DSGVO: IP deanonymisierbar aus Supabase-Read |
| `/api/admin/support-list` | **Ja** als auth-check | Fail-closed: 401 — kein Auth-Bypass |
| `/api/qa/upvote` ip_hash | **Ja**, jeder Upvote | DSGVO + Spam-Schutz schwächer |

**Severity-Reklassifizierung:**
- Original-Audit: **P0** mit Begründung „Auth-Bypass möglich"
- **Korrekte Severity nach Analyse: P1** — kein Auth-Bypass (Admin ist fail-closed), aber DSGVO-Schwäche aktiv + Versehens-Risiko wenn Secret triviall gesetzt.

Du entscheidest: Fix als P1 jetzt direkt anpacken oder erst nach #95/#75?

---

## 4. Richtiger Fix

### 4.1 Code-Änderungen

**a) Boot-Time-Check (Top des Files, nach env-load):**

```python
def _validate_recovery_secret_on_boot():
    """P0 #10 Fix: RECOVERY_SECRET MUSS gesetzt sein in Production.
    Wird als IP-Hash-Pepper + Admin-Auth-Token verwendet — leerer Default
    bricht IP-Pseudonymisierung und macht Admin-Endpoint unbenutzbar.

    Test/Local-Boot: AEROTAX_ALLOW_BOOT_WITHOUT_KEY=1 erlaubt expliziten
    Test-Boot ohne Secret (Tests setzen kein env).
    """
    sec = os.environ.get('RECOVERY_SECRET', '')
    if sec and len(sec) >= 32:
        return  # OK
    allow_boot = os.environ.get('AEROTAX_ALLOW_BOOT_WITHOUT_KEY') == '1'
    if not sec:
        msg = 'RECOVERY_SECRET ist nicht gesetzt'
    else:
        msg = f'RECOVERY_SECRET zu kurz (min 32 chars, got {len(sec)})'
    if not allow_boot:
        # Fail-closed in Production
        raise RuntimeError(
            f'[boot] {msg}. Set RECOVERY_SECRET to a strong secret '
            f'(min 32 chars) or AEROTAX_ALLOW_BOOT_WITHOUT_KEY=1 for local tests.'
        )
    # Test/Local: warnen aber zulassen
    app.logger.warning(f'[boot] {msg} — running in test-mode')

_validate_recovery_secret_on_boot()
```

**b) Helper `_recovery_pepper()` statt direkten env-Zugriff:**

```python
def _recovery_pepper():
    """Liefert RECOVERY_SECRET; bei leerem Secret in non-test-mode raisen
    (sollte bereits von Boot-Check abgefangen sein). Damit nie ein leerer
    Pepper an einen Hash-Call gegeben wird."""
    sec = os.environ.get('RECOVERY_SECRET', '')
    if not sec and os.environ.get('AEROTAX_ALLOW_BOOT_WITHOUT_KEY') != '1':
        raise RuntimeError('RECOVERY_SECRET not configured')
    return sec
```

**c) 3 Stellen umstellen:**
- Z.6589: `+ os.environ.get('RECOVERY_SECRET','')` → `+ _recovery_pepper()`
- Z.6685: `expected = os.environ.get('RECOVERY_SECRET', '')` → `expected = _recovery_pepper()`
- Z.6910: gleiches Pattern

**d) Logging-Safety:** schon ok — Secret wird NUR an `.encode()` für SHA256 gegeben und an `hmac.compare_digest`. Wird nie in `print`/`logger` ausgegeben. **Keine Änderung nötig.**

### 4.2 Env-Setup

**Vor Code-Deploy:**

```bash
# Erzeuge starkes Secret (64 hex chars = 256 bits Entropy)
NEW_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")

# Setze in Cloud Run via update (NICHT --set-env-vars — wisst noch BUG-005)
gcloud run services update aerotax-backend \
    --region=europe-west3 \
    --update-env-vars "RECOVERY_SECRET=$NEW_SECRET"

# Verify (Wert maskiert)
gcloud run services describe aerotax-backend --region=europe-west3 \
    --format='value(spec.template.spec.containers[0].env[?name==\"RECOVERY_SECRET\"].name)'
```

**Wert wird NIE in Chat oder Logs ausgegeben.** Erzeugen + setzen in einer Pipe.

### 4.3 Migration-Strategy

**Achtung:** wenn wir Secret setzen UND Boot-Check deployen, gibt es 2 Schritte:

| Reihenfolge | Risiko |
|---|---|
| 1. ENV setzen → 2. Code deploy | sicher: ENV ist beim Deploy schon da, Boot-Check passt |
| 1. Code deploy → 2. ENV setzen | unsicher: Code crasht beim Boot bevor ENV gesetzt → 503 für alle |

**Empfehlung:** zuerst ENV setzen, dann Code-Deploy. Identisch zum #96-Migration-Workflow.

---

## 5. Tests (vor Deploy)

Neue Datei: `tests/test_recovery_secret_p0_10.py`

| Test | Verifiziert |
|---|---|
| `test_recovery_secret_no_empty_default` | Source enthält keinen `os.environ.get('RECOVERY_SECRET','')` Pattern mehr (alle 3 Stellen umgestellt auf `_recovery_pepper()`) |
| `test_recovery_secret_required_in_production` | `_validate_recovery_secret_on_boot` raised wenn ENV leer + ALLOW_BOOT_WITHOUT_KEY≠'1' |
| `test_recovery_secret_allows_test_boot_only_with_flag` | ALLOW_BOOT_WITHOUT_KEY='1' → kein raise, nur warning |
| `test_recovery_secret_min_length_32` | Secret mit 10 chars → raise auch in production |
| `test_recovery_endpoint_rejects_missing_secret` | `/api/admin/support-list` ohne X-Admin-Token → 401 |
| `test_recovery_endpoint_accepts_valid_secret` | `/api/admin/support-list` mit korrektem Token → 200 |
| `test_secret_not_logged` | Mock logger, call all 3 use-sites, assert keine log-line enthält das Secret |
| `test_pepper_helper_returns_value` | `_recovery_pepper()` returnt ENV-Wert wenn gesetzt |
| `test_pepper_helper_raises_when_missing_in_prod` | RaiseRuntimeError wenn ENV leer + flag nicht gesetzt |

Plus Update bestehender Tests: alle Tests, die `RECOVERY_SECRET` benutzen, müssen `AEROTAX_ALLOW_BOOT_WITHOUT_KEY=1` setzen (in `setUp` oder via test-env). Sonst brechen sie beim Import-Time-Boot-Check.

---

## 6. Vorgeschlagener Stop-Gate-Workflow

| Schritt | Stop-Gate |
|---|---|
| 1. Du gibst Severity-Decision (P0 weiter oder P1-Re-Triage) | jetzt |
| 2. Ich erzeuge neuen Secret + setze via `--update-env-vars` | nach deinem Go |
| 3. Ich verifiziere ENV-Presence (Wert maskiert) | automatisch |
| 4. Ich baue Code-Diff + Tests | nach deinem Go für Schritt 2 |
| 5. Du gibst Code-Deploy-Freigabe | nach Tests grün |
| 6. Deploy + Smoke + Live-Proof | automatisch |
| 7. `fixed_unverified` markieren | abschließend |

---

## 7. Frage an dich

1. **Severity-Reklassifizierung akzeptieren?** Original war P0 mit irreführender Begründung. Echte Surface ist DSGVO + Versehens-Risiko (Admin ist fail-closed) → **P1**. Du entscheidest:
   - **A:** Trotzdem als P0 sofort fixen (DSGVO ernst, plus Versehens-Risiko vermeiden)
   - **B:** Re-Klassifizierung auf P1, später nach #95/#75 angehen

2. **Falls A oder B mit Fix:** soll ich Secret-Generation + ENV-Set zuerst machen (so dass beim Code-Deploy die ENV schon da ist), oder erst Tests/Diff zeigen?

3. **Logging-Policy bestätigen:** Secret wird NIE ausgegeben — auch nicht hash-prefix oder length. Korrekt?

Nichts geändert. Warte auf deine Antwort.
