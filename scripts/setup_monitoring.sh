#!/usr/bin/env bash
# setup_monitoring.sh — creates/refreshes AeroX Cloud Monitoring resources.
#
# Idempotent-ish: log metrics are created only if missing; alert policies are
# created only if no policy with the same displayName exists yet.
#
# Prereqs: gcloud authed against project aerotax-prod.
# Policies reference the email notification channel created 2026-07-02:
#   projects/aerotax-prod/notificationChannels/18344722479875947413
#   (email: miguel.schumann@icloud.com)
#
# Usage: ./scripts/setup_monitoring.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")/monitoring" && pwd)"
SERVICE_FILTER='resource.type="cloud_run_revision" AND resource.labels.service_name="aerotax-backend"'

create_metric() {
  local name="$1" desc="$2" filter="$3"
  if gcloud logging metrics describe "$name" >/dev/null 2>&1; then
    echo "log metric $name: exists"
  else
    gcloud logging metrics create "$name" --description="$desc" --log-filter="$filter"
  fi
}

create_metric apns_send_failed \
  "AeroX: APNs push send failed (non-200 from api.push.apple.com)" \
  "$SERVICE_FILTER AND textPayload=~\"\\[APNS\\] send failed\""

create_metric auth_store_unavailable \
  "AeroX: auth store load failures (sb_load_fail / auth store unavailable)" \
  "$SERVICE_FILTER AND textPayload=~\"sb_load_fail|auth store unavailable\""

create_metric auth_gate_error \
  "AeroX: unexpected auth gate errors" \
  "$SERVICE_FILTER AND textPayload=~\"auth_gate_error|UNEXPECTED gate error\""

create_metric server_5xx \
  "AeroX: HTTP 5xx responses from aerotax-backend" \
  "$SERVICE_FILTER AND httpRequest.status>=500"

existing_policies="$(gcloud alpha monitoring policies list --format='value(displayName)' 2>/dev/null || true)"

for f in "$DIR"/policy_*.json; do
  display_name="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['displayName'])" "$f")"
  if grep -Fqx "$display_name" <<<"$existing_policies"; then
    echo "alert policy '$display_name': exists"
  else
    gcloud alpha monitoring policies create --policy-from-file="$f"
  fi
done

echo "--- verification ---"
gcloud logging metrics list --format='value(name)'
gcloud alpha monitoring policies list --format='table(displayName,enabled,name)'
