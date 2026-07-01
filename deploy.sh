#!/usr/bin/env bash
# AeroTax Backend — versionierter Deploy mit Skalierungs-/Kosten-Guardrails.
#
# WARUM: Bisher lag die Cloud-Run-Skalierungs-Config NUR in der Console
# (unversioniert, driftet). Der Pre-Release-Audit fand die Hauptkosten-/Risiko-
# Treiber: (1) KEIN max-instances-Cap -> ein Retry-Sturm kann Instanzen
# unbegrenzt auffaechern (4-stelliger Bill-Spike moeglich); (2) kein
# reproduzierbarer Deploy. Dieses Script setzt die Guardrails als Code.
#
# SICHER: --source . baut eine neue Revision OHNE env-Vars anzufassen
# (siehe CLAUDE.md / BUG-005). Die Skalierungs-Flags (--max-instances,
# --concurrency, --memory, --cpu) sind Service-Config, KEINE env-Vars — sie
# loeschen nichts. NIEMALS --set-env-vars hier reinnehmen.
#
# HINWEIS Timeout: Der gunicorn-Timeout (1800s) im Dockerfile bleibt, weil der
# lange Worker-Job (process-job via Cloud Tasks) ihn braucht. Den Cloud-Run-
# Request-Timeout hier NICHT auf 60s setzen, solange API + Worker EIN Service
# sind — das wuerde die langen Jobs killen. Sauberer Folgeschritt: API- und
# Worker-Service trennen (API --timeout=60, Worker --timeout=1800).
set -euo pipefail

REGION="europe-west3"
SERVICE="aerotax-backend"

# Codifiziert die bereits LIVE laufende (gute) Config als reproduzierbare IaC:
# max-instances=10 (Runaway-Bremse — schon gesetzt), scale-to-zero, cpu-throttling
# (request-billed), cpu=2/4Gi (die Tax-/PDF-/SQLite-Jobs brauchen den Speicher —
# NICHT verkleinern). concurrency=8 == gunicorn threads=8.
# Kapazitaet: 10 * 8 = 80 gleichzeitige Requests. Fuer den 5k-Launch i. d. R.
# ausreichend (Traffic ist bursty, schwere Tax-Jobs laufen async via Cloud Tasks).
# Falls bei Peak 429/throttling auftaucht: --max-instances=20 (verdoppelt die
# Kosten-Obergrenze, deckt groessere Peaks). Spend-Alert haengt separat als
# Billing-Budget (100 EUR/Monat, 50/90/100%) am Projekt aerotax-prod.
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --max-instances=10 \
  --min-instances=0 \
  --concurrency=8 \
  --memory=4Gi \
  --cpu=2 \
  --cpu-throttling

echo ""
echo "Deployed $SERVICE. Verify scaling config:"
echo "  gcloud run services describe $SERVICE --region $REGION --format='value(spec.template.spec.containerConcurrency, spec.template.metadata.annotations)'"
