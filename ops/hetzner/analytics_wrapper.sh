#!/bin/bash
# AeroX Wochen-Analytics (Host-Wrapper) — Muster wie sweep_wrapper.sh.
# Cron: 0 7 * * 1 /opt/aerox/analytics_wrapper.sh
# Führt analytics_report.py im Backend-Container aus und mailt den Report
# via Resend an SUPPORT_NOTIFY_EMAIL. Exit-Code immer 0 (kein Cron-Spam).
set -uo pipefail

LOG=/tmp/aerox-analytics-out.txt
cd /opt/aerox || { echo "FATAL: /opt/aerox fehlt" >> "$LOG"; exit 0; }
RK=$(grep "^RESEND_API_KEY="       env.list | cut -d= -f2-)
TO=$(grep "^SUPPORT_NOTIFY_EMAIL=" env.list | cut -d= -f2-)

REPORT=$(docker exec -i aerotax-backend python3 - < /opt/aerox/analytics_report.py 2>>"$LOG")
if [ -z "$REPORT" ]; then
    echo "$(date -u +%FT%TZ) leerer Report — siehe Fehler oben" >> "$LOG"
    exit 0
fi

export AEROX_REPORT="$REPORT"
python3 - "$RK" "$TO" <<PY >> "$LOG" 2>&1
import sys, json, urllib.request, os
rk, to = sys.argv[1], sys.argv[2]
report = os.environ["AEROX_REPORT"]
data = json.dumps({
    "from":    "AeroX Analytics <support@aerosteuer.de>",
    "to":      [to],
    "subject": "[AeroX] Wochen-Analytics",
    "text":    report,
}).encode()
req = urllib.request.Request("https://api.resend.com/emails", data=data,
    headers={"Authorization": "Bearer " + rk, "Content-Type": "application/json",
             # Cloudflare vor Resend blockt Pythons Default-UA (Error 1010).
             "User-Agent": "curl/8.0"})
print(urllib.request.urlopen(req, timeout=30).status)
PY
exit 0
