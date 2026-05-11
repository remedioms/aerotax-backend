# Cloud Run Migration — Schritt-für-Schritt-Anleitung

> **Stand:** v12 Phase B vorbereitet. Backend-Code Cloud-Run-tauglich (Dockerfile, gunicorn, $PORT). Frontend hat konfigurierbares `window._API`. Default zeigt noch auf Render — Migration läuft additiv, ohne Service-Bruch.

Was du brauchst:
- Google Cloud Account + Billing aktiv
- `gcloud` CLI installiert (`brew install --cask google-cloud-sdk`)
- Eine GCP Project-ID (z.B. `aerotax-prod` — wird gleich erzeugt)
- Cloudflare-Zugang für DNS auf `aerosteuer.de`

Ich (Claude) kann diese Schritte nicht selbst ausführen — sie brauchen `gcloud auth login` mit deinem Account. Folge der Reihe nach. Bei jedem Schritt zeige ich dir was rauskommen soll.

---

## 0. Vorab — Docker lokal testen (optional)

Du hast aktuell kein lokales Docker installiert. Für Cloud Build brauchst du es auch nicht — der Build läuft in GCP. Wenn du trotzdem lokal testen willst:

```bash
brew install --cask docker  # Docker Desktop
open -a Docker              # Starten + Login
cd ~/Desktop/aerotax-backend
docker build -t aerotax-test .
docker run --rm -p 8080:8080 \
  -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  -e SUPABASE_URL="$SUPABASE_URL" \
  -e SUPABASE_SERVICE_KEY="$SUPABASE_SERVICE_KEY" \
  aerotax-test
# In zweitem Terminal:
curl http://localhost:8080/api/health
# erwartete Antwort: {"ok": true, "service": "aerotax-backend", "version": "v8.40"}
```

Wenn du das überspringen willst — Cloud Build baut + testet automatisch.

---

## 1. gcloud CLI Setup

```bash
# Falls noch nicht installiert:
brew install --cask google-cloud-sdk

# Login (öffnet Browser)
! gcloud auth login

# Project erzeugen + setzen
gcloud projects create aerotax-prod --name="AeroTAX Production"
gcloud config set project aerotax-prod

# Billing-Account verknüpfen (musst du im Browser machen):
# → https://console.cloud.google.com/billing/linkedaccount?project=aerotax-prod
# Ohne Billing → Cloud Run lehnt Deploy ab.

# Application Default Credentials (für andere Tools)
! gcloud auth application-default login
```

**Sanity-Check:**
```bash
gcloud config get-value project
# erwartete Ausgabe: aerotax-prod
```

---

## 2. Erforderliche APIs aktivieren

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com
```

Dauert ~30s. Output: `Operation "..." finished successfully.`

---

## 3. Artifact Registry erzeugen (für Container-Image)

```bash
gcloud artifacts repositories create aerotax \
  --repository-format=docker \
  --location=europe-west3 \
  --description="AeroTAX Backend Container Images"
```

> **Region:** `europe-west3` (Frankfurt) — niedrigste Latenz für deutsche User + DSGVO-Konform.

---

## 4. Secrets in Secret Manager (keine Plain-ENV-Vars!)

Sensible Werte werden NICHT direkt als Cloud-Run-ENV gesetzt — sondern als Secret-Reference. Sonst stehen sie als Plaintext im Cloud-Run-UI.

```bash
# Pro Secret:
# 1. erzeugen
# 2. Versionen reinkippen (über stdin)
# 3. Cloud-Run-Service-Account Read-Access geben

create_secret() {
  local name=$1
  local value=$2
  echo -n "$value" | gcloud secrets create "$name" --data-file=- 2>/dev/null \
    || echo -n "$value" | gcloud secrets versions add "$name" --data-file=-
}

# WERTE HIER EINTRAGEN (oder aus deinem Render-Dashboard ziehen via API)
create_secret ANTHROPIC_API_KEY           'sk-ant-...'
create_secret SUPABASE_URL                'https://xxx.supabase.co'
create_secret SUPABASE_SERVICE_KEY        'eyJhbGc...'
create_secret STRIPE_SECRET_KEY           'sk_live_...'
create_secret STRIPE_WEBHOOK_SECRET       'whsec_...'
create_secret SESSION_SECRET              'random-base64-32bytes'
create_secret RENDER_API_KEY              ''   # nur falls Cloud Run noch Render-Logs zieht

# Service-Account von Cloud Run (wird automatisch erzeugt beim ersten Deploy)
# bekommt Read-Zugriff auf alle Secrets:
PROJECT_NUMBER=$(gcloud projects describe aerotax-prod --format='value(projectNumber)')
SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

for s in ANTHROPIC_API_KEY SUPABASE_URL SUPABASE_SERVICE_KEY \
         STRIPE_SECRET_KEY STRIPE_WEBHOOK_SECRET SESSION_SECRET RENDER_API_KEY; do
  gcloud secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:${SA}" \
    --role='roles/secretmanager.secretAccessor'
done
```

> **Render-API-Key** (falls im Repo aktiv): in Cloud Run nicht mehr nötig. Nur falls ein Job-Script die Render-Logs zieht — sonst weglassen.

**Sanity-Check:**
```bash
gcloud secrets list
# Erwartete Spalten: NAME, CREATED, REPLICATION_POLICY, LOCATIONS
```

---

## 5. Cloud Run Deploy (erster Push)

`gcloud run deploy` ist „source-deploy": baut das Image automatisch via Cloud Build, pusht in Artifact Registry, deployt den Service. **Kein lokales Docker nötig.**

```bash
cd ~/Desktop/aerotax-backend

gcloud run deploy aerotax-backend \
  --source . \
  --region europe-west3 \
  --memory 2Gi \
  --cpu 1 \
  --concurrency 1 \
  --timeout 1800 \
  --min-instances 0 \
  --max-instances 3 \
  --allow-unauthenticated \
  --set-env-vars "AEROTAX_PIPELINE_VERSION=v11_cas_primary" \
  --set-env-vars "AEROTAX_FOLLOWME_ALIGN=1" \
  --set-env-vars "AEROTAX_CAPTURE_SNAPSHOTS=0" \
  --set-env-vars "AEROTAX_USE_CHUNK_PERSISTENCE=0" \
  --set-env-vars "AEROTAX_CAS_MAX_PARALLEL=2" \
  --set-env-vars "AEROTAX_LSB_FAST_READER_MODE=gated" \
  --set-env-vars "AEROTAX_CAS_MERGE=1" \
  --set-secrets "ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest" \
  --set-secrets "SUPABASE_URL=SUPABASE_URL:latest" \
  --set-secrets "SUPABASE_SERVICE_KEY=SUPABASE_SERVICE_KEY:latest" \
  --set-secrets "STRIPE_SECRET_KEY=STRIPE_SECRET_KEY:latest" \
  --set-secrets "STRIPE_WEBHOOK_SECRET=STRIPE_WEBHOOK_SECRET:latest" \
  --set-secrets "SESSION_SECRET=SESSION_SECRET:latest"
```

> **Build dauert** ~3-5 Min beim ersten Mal (kein Cache). Folge-Deploys 1-2 Min.

**Erfolgreicher Output:**
```
✓ Building and deploying new service... Done.
Service URL: https://aerotax-backend-XXXXXX-ew.a.run.app
```

> ⚠️ `--allow-unauthenticated`: Cloud Run akzeptiert Anfragen ohne IAM-Token. Wir brauchen das, weil Frontend direkt vom Browser kommt. Authentifizierung läuft über `X-Session-Token`-Header.

---

## 6. Smoke-Tests am frisch deployten Service

```bash
SERVICE_URL=$(gcloud run services describe aerotax-backend \
              --region europe-west3 --format='value(status.url)')
echo "Service: $SERVICE_URL"

# 1. Quick-Health
curl -s "$SERVICE_URL/api/health"
# Erwartet: {"ok": true, "service": "aerotax-backend", "version": "v8.40"}

# 2. Full-Health (testet Anthropic + Disk)
curl -s "$SERVICE_URL/api/health/full" | python3 -m json.tool

# 3. CORS-Preflight (so wie Frontend käme)
curl -s -X OPTIONS "$SERVICE_URL/api/health" \
  -H "Origin: https://aerosteuer.de" \
  -H "Access-Control-Request-Method: GET" -i | head -20

# 4. Session-Endpoint mit unbekanntem Token (Phase A state-machine response)
curl -s "$SERVICE_URL/api/session/AT-NONEXISTENT" | python3 -m json.tool
# Erwartet: 404 mit canonical_state=expired, reason_code=ACCESS_CODE_EXPIRED

# 5. Logs
gcloud run services logs read aerotax-backend --region europe-west3 --limit 50
```

**Soll-Zustand:**
- `/api/health` → 200 OK
- `/api/health/full` → `anthropic: ok`, `file_system: ok`, `supabase: ok`
- CORS-Preflight: `Access-Control-Allow-Origin: https://aerosteuer.de`
- Logs: keine Boot-Errors, kein missing ENV, gunicorn worker auf 0.0.0.0:8080

Wenn EIN Test rot ist: nicht weitermachen. Logs lesen, Secret-Binding prüfen.

---

## 7. Custom Domain: `api.aerosteuer.de` → Cloud Run

```bash
# 1. Domain im Cloud Run Service mappen
gcloud beta run domain-mappings create \
  --service aerotax-backend \
  --domain api.aerosteuer.de \
  --region europe-west3

# Output zeigt DNS-Records die du in Cloudflare setzen musst:
# z.B. CNAME api → ghs.googlehosted.com.
```

In Cloudflare DNS-Settings:
- Type: `CNAME`
- Name: `api`
- Target: aus Cloud Run Output (typisch `ghs.googlehosted.com`)
- Proxy: **DNS-only** (graue Wolke) — sonst doppeltes TLS
- TTL: Auto

Nach 1-5 Min:
```bash
curl -s https://api.aerosteuer.de/api/health
# Erwartet: dasselbe wie Cloud-Run-URL
```

---

## 8. Frontend umschalten auf Cloud Run

In `~/Desktop/site/index.html` Z. 1538 — eine einzelne Konstante umstellen:

```javascript
var DEFAULT_PRIMARY = CLOUD_RUN_PROD;  // war: RENDER_FALLBACK
```

Deploy:
```bash
wrangler pages deploy ~/Desktop/site --project-name aerosteuer --commit-dirty=true
```

Frontend zeigt jetzt auf `https://api.aerosteuer.de`. Render bleibt parallel, kann jederzeit per Code-Push wieder primary werden.

**Sanity-Check via Browser:**
```javascript
// In DevTools:
console.log(window._API_CONFIG)
// Erwartet: { active: 'https://api.aerosteuer.de', is_cloud_run: true, ... }
```

---

## 9. CORS-Whitelist auf Cloud Run

Cloud Run ist hinter Google Frontend — Standard ist `*`. Wir wollen explizit `aerosteuer.de` + Cloudflare Pages. Im `app.py` ist `CORS(app)` aktuell offen (siehe Z. ~18). Wenn du das tightenen willst:

```python
CORS(app, origins=[
    "https://aerosteuer.de",
    "https://www.aerosteuer.de",
    "https://aerosteuer.pages.dev",
    "https://*.aerosteuer.pages.dev",   # Cloudflare preview-deploys
])
```

Aktuell offen lassen ist OK für jetzt — Cloud Run akzeptiert ohnehin nur unsere Endpoints, und Session-Token wird per Request validiert.

---

## 10. Render parallel laufen lassen (Rollback)

**Keine Aktion nötig.** Render-Service `srv-d7o6qbe8bjmc7398acdg` bleibt aktiv. Konsequenz:
- Frontend zeigt auf Cloud Run via `DEFAULT_PRIMARY = CLOUD_RUN_PROD`
- Render-URL ist als `RENDER_FALLBACK` definiert, Override möglich:
  - Query: `?api=https://aerotax-backend.onrender.com`
  - LocalStorage: `localStorage.setItem('aerotax_api', 'https://aerotax-backend.onrender.com')`
- Code-Rollback in 1 Zeile: `DEFAULT_PRIMARY = RENDER_FALLBACK` + redeploy.
- Render kann nach 2-4 Wochen stabilem Cloud-Run-Betrieb auf Free-Plan zurück (kostet $0) oder ganz weg.

---

## 11. Kostenschätzung

Cloud Run preisbild (europe-west3, Stand 2025):
- CPU-Sekunden: $0.000024/sec aktiv = $1.44/Std (1 vCPU)
- Memory-Sekunden: $0.0000025/sec/GiB = $0.018/Std (2 GiB)
- Requests: $0.40/Mio (vernachlässigbar)
- Build (Cloud Build): 120 Build-Min/Tag free, danach $0.003/min

**Realistic für AeroTAX (10-50 Auswertungen/Monat, ~6 Min pro Job):**
- CPU + Memory aktiv: 50 × 6 min = 300 min × ~$0.018+0.024/min ≈ **$13/mo aktiv**
- Plus idle: min-instances=0 → $0 bei keinen Anfragen
- Plus Storage (Artifact Registry): ~$0.10/mo
- **Gesamt: ~$13-20/mo**

Vergleich:
- Render Free: $0 (aber: OOM + Cold-Boot + 25-Request-Cap)
- Render Starter: $7/mo (2 GB RAM, kein OOM, aber 24/7-Idle-Kosten)
- Cloud Run: $13-20/mo bei aktuellem Volumen, **skaliert linear** mit Job-Count

Bei Beta-Launch + Volumensteigerung: Cloud Run zahlt sich aus (pay-per-use vs. Render fixed).

---

## 12. Was nicht passieren darf

- ❌ Secrets direkt als `--set-env-vars` setzen (Plain-Text in Cloud-Console sichtbar)
- ❌ `--allow-unauthenticated` weglassen (Frontend kann dann nicht zugreifen)
- ❌ Region in `europe-west1` (Belgien) deployen (Latenz höher für deutsche User; weniger DSGVO-konform)
- ❌ `max-instances > 5` (Sonnet-API-Costs könnten bei DDoS explodieren)
- ❌ Render löschen bevor Cloud Run 2+ Wochen stabil ist

---

## 13. Rollback in 5 Minuten

Wenn Cloud Run nach Migration Probleme macht:

1. `~/Desktop/site/index.html` Z. 1538: `DEFAULT_PRIMARY = RENDER_FALLBACK`
2. `wrangler pages deploy ~/Desktop/site --project-name aerosteuer --commit-dirty=true`
3. Frontend zeigt sofort auf Render (5 Min cache-TTL Cloudflare)
4. Cloud Run Service bleibt deployed, kostet nichts bei `min-instances=0`
5. Issue debuggen, fixen, re-deployen, dann erneut umschalten

Optional härter: Cloud Run Service pausieren:
```bash
gcloud run services update aerotax-backend --region europe-west3 --max-instances=0
```
→ Cloud Run akzeptiert weiterhin Requests, gibt aber 503 (kein Container wird gestartet).

---

## Status nach Phase B (heute)

✅ Backend Code Cloud-Run-tauglich (Dockerfile, gunicorn, `$PORT`, libheif)  
✅ Frontend `window._API` konfigurierbar (Hostname-Routing + Query/LocalStorage-Override)  
✅ Default zeigt noch auf Render — kein Service-Bruch  
✅ Rollback-Plan dokumentiert  
✅ 58 Tests grün (State-Machine + Dockerfile-Sanity + Frontend-Config)  

🔲 **Pending (manuell von dir):** Schritte 1-9 oben durchgehen → Cloud Run deployen + `api.aerosteuer.de` mappen + Frontend `DEFAULT_PRIMARY` umstellen  
🔲 Capture-Live-Run nach Smoke-Tests grün
