#!/bin/bash
# AeroX LH-Quota-Tagesreport (Host-Wrapper). Muster wie signup_digest_wrapper.sh.
# Cron: 40 5 * * * /opt/aerox/lh_quota_wrapper.sh    (07:40 DE-Sommerzeit)
# Report-Zeile 1 = Subject, Rest = Body. Mailt via Resend.
# Exit-Code immer 0 (kein Cron-Spam).
set -uo pipefail

LOG=/tmp/aerox-lh-quota-out.txt
cd /opt/aerox || { echo "FATAL: /opt/aerox fehlt" >> "$LOG"; exit 0; }
RK=$(grep "^RESEND_API_KEY="      env.list | cut -d= -f2-)
TO=$(grep "^SIGNUP_NOTIFY_EMAIL=" env.list | cut -d= -f2-)
TO=${TO:-de.miguel.schumann@sapo.pt}

REPORT=$(docker exec -i aerotax-backend python3 - < /opt/aerox/lh_quota_report.py 2>>"$LOG")
if [ -z "$REPORT" ]; then
    echo "$(date -u +%FT%TZ) leerer LH-Quota-Report — siehe Fehler oben" >> "$LOG"
    exit 0
fi

export AEROX_REPORT="$REPORT"
python3 - "$RK" "$TO" <<PY >> "$LOG" 2>&1
import sys, json, urllib.request, os
rk, to = sys.argv[1], sys.argv[2]
lines = os.environ["AEROX_REPORT"].split("\n")
subject, body = lines[0], "\n".join(lines[1:])
data = json.dumps({
    "from":    "AeroX <noreply@aerosteuer.de>",
    "to":      [to],
    "subject": subject,
    "text":    body,
}).encode()
req = urllib.request.Request("https://api.resend.com/emails", data=data,
    headers={"Authorization": "Bearer " + rk, "Content-Type": "application/json",
             # Cloudflare vor Resend blockt Pythons Default-UA (Error 1010).
             "User-Agent": "curl/8.0"})
print(urllib.request.urlopen(req, timeout=30).status)
PY
exit 0
