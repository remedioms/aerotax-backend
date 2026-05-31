# Runbook: Migration `AEROTAX_CRYPTO_KEY` + `RESEND_API_KEY` in Secret Manager

**Status:** PLANUNG — noch nicht ausgeführt.

**Wer fuhrt das aus:** USER (Miguel) lokal in einem Terminal mit gcloud-Auth.
Worker (Claude) darf KEINE gcloud-Writes ausführen — per CLAUDE.md (BUG-005
self-inflicted lessons-learned).

**Risiko:** Falsche Reihenfolge oder falsches Flag (`--set-env-vars` statt
`--update-env-vars`) loescht alle env vars. Recovery nur über vorherige
Revision (~10 Min Downtime).

---

## 1. Vorbedingungen / Read-only Check

Aktuellen Stand verifizieren — welche env-Vars sind plain, welche secretKeyRef:

```bash
gcloud run services describe aerotax-backend --region=europe-west3 \
  --format=json \
  | jq '.spec.template.spec.containers[0].env
        | map({name, valueFrom_secret: (.valueFrom.secretKeyRef.name // null), plain_value_present: (.value != null)})'
```

Erwartet (Stand: vor Migration):
- `ANTHROPIC_API_KEY`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`,
  `SUPABASE_SERVICE_KEY`, `RECOVERY_SECRET`, `SESSION_SECRET`,
  `RENDER_API_KEY` → `secretKeyRef` (bereits sauber)
- `AEROTAX_CRYPTO_KEY`, `RESEND_API_KEY` → plain (zu migrieren)

(Worker konnte den Live-Stand nicht auslesen — `gcloud run services describe`
war im Sandbox blockiert. User soll Output gegen die Erwartung abgleichen.)

Service-Account-Name fuer Cloud Run notieren (fuer IAM-Binding):

```bash
gcloud run services describe aerotax-backend --region=europe-west3 \
  --format='value(spec.template.spec.serviceAccountName)'
```

Falls leer → Default-Compute-SA wird benutzt:
`PROJECT_NUMBER-compute@developer.gserviceaccount.com` (PROJECT_NUMBER via
`gcloud projects describe PROJECT_ID --format='value(projectNumber)'`).

---

## 2. AEROTAX_CRYPTO_KEY migrieren

### 2a. Aktuellen plain-Wert sichern (LOKAL, nicht git)

```bash
gcloud run services describe aerotax-backend --region=europe-west3 \
  --format='value(spec.template.spec.containers[0].env)' \
  | grep -A1 AEROTAX_CRYPTO_KEY
```

Den Wert in `/tmp/aerotax_crypto_key.txt` (oder eine andere kurzlebige Datei
ausserhalb des Repos) zwischenspeichern. **NICHT** in den git-tree
commiten.

### 2b. Secret in Secret Manager anlegen

```bash
gcloud secrets create AEROTAX_CRYPTO_KEY \
  --replication-policy=automatic

# Initial-Version aus zwischen-gespeicherter Datei
gcloud secrets versions add AEROTAX_CRYPTO_KEY \
  --data-file=/tmp/aerotax_crypto_key.txt
```

Verifizieren:

```bash
gcloud secrets versions access latest --secret=AEROTAX_CRYPTO_KEY | wc -c
# Sollte == Laenge der gesicherten Datei sein (z.B. 44 fuer base64-32B).
```

### 2c. IAM-Permission fuer Cloud-Run-SA

```bash
SA="PROJECT_NUMBER-compute@developer.gserviceaccount.com"  # anpassen!

gcloud secrets add-iam-policy-binding AEROTAX_CRYPTO_KEY \
  --member="serviceAccount:${SA}" \
  --role="roles/secretmanager.secretAccessor"
```

### 2d. Cloud Run umstellen — plain entfernen, secret hinzufuegen

**Wichtig:** Reihenfolge wie unten. `--update-secrets` UND `--remove-env-vars`
koennen kombiniert werden — das ist atomar (eine neue Revision).

```bash
gcloud run services update aerotax-backend --region=europe-west3 \
  --remove-env-vars=AEROTAX_CRYPTO_KEY \
  --update-secrets=AEROTAX_CRYPTO_KEY=AEROTAX_CRYPTO_KEY:latest
```

NIEMALS `--set-env-vars` oder `--set-secrets` benutzen — die loeschen alle
nicht erwaehnten Variables/Secrets (BUG-005).

### 2e. Verifikation

```bash
# Neuer Revision sollte AEROTAX_CRYPTO_KEY als secretKeyRef haben
gcloud run services describe aerotax-backend --region=europe-west3 \
  --format=json \
  | jq '.spec.template.spec.containers[0].env[] | select(.name=="AEROTAX_CRYPTO_KEY")'

# Erwartet: { "name": "AEROTAX_CRYPTO_KEY", "valueFrom": { "secretKeyRef": { ... } } }

# Service-Health pruefen
curl -sS https://aerotax-backend.onrender.com/api/health | jq .

# Live-Logs auf Crypto-Fehler scannen (ein Fenster):
gcloud run services logs read aerotax-backend --region=europe-west3 \
  --limit=50 --format='value(textPayload)' \
  | grep -i 'crypto\|decrypt\|AEROTAX_CRYPTO'
```

### 2f. Cleanup

Sobald Service stabil laeuft (>15 Min):

```bash
shred -u /tmp/aerotax_crypto_key.txt   # oder rm + sicherheitshalber neu booten
```

---

## 3. RESEND_API_KEY migrieren

Gleiches Pattern wie 2a-2f, nur mit `RESEND_API_KEY` als Name:

```bash
# Wert sichern
gcloud run services describe aerotax-backend --region=europe-west3 \
  --format=json | jq -r '.spec.template.spec.containers[0].env[]
                          | select(.name=="RESEND_API_KEY") | .value' \
  > /tmp/resend_api_key.txt

# Secret anlegen
gcloud secrets create RESEND_API_KEY --replication-policy=automatic
gcloud secrets versions add RESEND_API_KEY --data-file=/tmp/resend_api_key.txt

# IAM
gcloud secrets add-iam-policy-binding RESEND_API_KEY \
  --member="serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

# Service umstellen
gcloud run services update aerotax-backend --region=europe-west3 \
  --remove-env-vars=RESEND_API_KEY \
  --update-secrets=RESEND_API_KEY=RESEND_API_KEY:latest

# Verifikation
gcloud run services describe aerotax-backend --region=europe-west3 \
  --format=json \
  | jq '.spec.template.spec.containers[0].env[] | select(.name=="RESEND_API_KEY")'

# Optional: Test-Email-Endpoint pingen (wenn vorhanden) oder Logs scannen
shred -u /tmp/resend_api_key.txt
```

---

## 4. Rollback (wenn der neue Revision tot ist)

Cloud Run haelt alte Revisions automatisch. Schnellster Weg zurueck:

```bash
# Letzte funktionierende Revision finden
gcloud run revisions list --service=aerotax-backend --region=europe-west3 \
  --limit=10 --format='table(metadata.name,status.conditions[0].status,metadata.creationTimestamp)'

# Traffic 100% auf die alte Revision zurueck
gcloud run services update-traffic aerotax-backend --region=europe-west3 \
  --to-revisions=<ALTE_REVISION_NAME>=100
```

Danach Root-Cause analysieren (Logs, IAM, Secret-Wert pruefen), dann erneut.

Falls Secret kaputt: Wert via `gcloud secrets versions add` korrigieren
(neue Version), dann `--update-secrets=NAME=NAME:latest` erneut, ODER die
spezifische Versionsnummer pinnen mit `NAME=NAME:3`.

---

## 5. Was Worker NICHT macht

- KEINE `gcloud secrets create`
- KEINE `gcloud run services update`
- KEINE Modifikation von IAM-Policies
- KEINE `--set-env-vars` (loescht alles — BUG-005)
- KEINE Aenderung des Cloud-Run-Service-Accounts

**Ausfuehrung dieses Runbooks ist Aufgabe des Users.** Worker hat in dieser
Welle nur Read-only-Checks (die hier in der Sandbox auch blockiert waren — der
User muss den `describe`-Output selbst gegen die Erwartung in Abschnitt 1
abgleichen).
