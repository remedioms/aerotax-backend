#!/bin/bash
# AeroX Kalender-Invarianten-Wächter (Host-Wrapper).
# Cron: 45 4,16 * * * /opt/aerox/sweep_wrapper.sh
# Kopiert den Sweep-Code in den Container, fuehrt ihn aus, mailt bei R1-R4-Treffern.
# Exit-Code immer 0 (kein Cron-Spam).
set -uo pipefail

LOG=/tmp/aerox-sweep-out.txt
SCRIPT_IN_CONTAINER=/tmp/sweep_kalender.py
HOST_SCRIPT=/opt/aerox/sweep_kalender.py
CONTAINER=aerotax-backend

# ── Resend-Zugangsdaten aus env.list laden ───────────────────────────────────
cd /opt/aerox || { echo "FATAL: /opt/aerox fehlt" >> "$LOG"; exit 0; }
RK=$(grep  "^RESEND_API_KEY="      env.list | cut -d= -f2-)
TO=$(grep  "^SUPPORT_NOTIFY_EMAIL=" env.list | cut -d= -f2-)

# ── Testmail-Modus (SWEEP_TEST_MAIL=1 aus der Umgebung) ─────────────────────
if [ "${SWEEP_TEST_MAIL:-0}" = "1" ]; then
    python3 - "$RK" "$TO" <<'PY'
import sys, json, urllib.request
rk, to = sys.argv[1], sys.argv[2]
data = json.dumps({
    "from":    "AeroX Sweep <support@aerosteuer.de>",
    "to":      [to],
    "subject": "[AeroX-Sweep] Testalarm — Einrichtung ok",
    "text":    ("Dies ist eine Testmail des AeroX Kalender-Invarianten-Waechters.\n"
                "Der Wächter ist korrekt eingerichtet und betriebsbereit.\n\n"
                "Cron: 45 4,16 * * * /opt/aerox/sweep_wrapper.sh")
}).encode()
req = urllib.request.Request(
    "https://api.resend.com/emails", data=data,
    headers={"Authorization": "Bearer " + rk,
             "Content-Type": "application/json",
             "User-Agent": "AeroX-Sweep/2.0"})
resp = urllib.request.urlopen(req, timeout=15)
print(f"Resend HTTP {resp.status}: {resp.read().decode()[:200]}")
PY
    exit 0
fi

# ── Sweep im Container ausführen ─────────────────────────────────────────────
{
    docker cp "$HOST_SCRIPT" "${CONTAINER}:${SCRIPT_IN_CONTAINER}" 2>&1
    docker exec "$CONTAINER" python3 "$SCRIPT_IN_CONTAINER" 2>&1
} > "$LOG" || true   # Fehler nicht abbrechen lassen

# ── Verstoss-Zaehler auslesen ────────────────────────────────────────────────
N=$(grep -oP 'SWEEP_VIOLATIONS_R1R4=\K[0-9]+' "$LOG" 2>/dev/null || echo "")

# Falls Zeile fehlt (crash vor print), Verstoss-Sentinel setzen
if [ -z "$N" ]; then
    N="ERR"
fi

# ── Mail nur bei echten Verstaessen (R1-R4 > 0 oder Crash) ──────────────────
if [ "$N" != "0" ] && [ "$N" != "" ]; then
    BODY=$(cat "$LOG")
    SUBJECT="[AeroX-Sweep] ${N} Verstöße $(date -u '+%Y-%m-%d %H:%M') UTC"
    python3 - "$RK" "$TO" "$SUBJECT" "$BODY" <<'PY'
import sys, json, urllib.request
rk, to, subject, body = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
data = json.dumps({
    "from":    "AeroX Sweep <support@aerosteuer.de>",
    "to":      [to],
    "subject": subject,
    "text":    body
}).encode()
req = urllib.request.Request(
    "https://api.resend.com/emails", data=data,
    headers={"Authorization": "Bearer " + rk,
             "Content-Type": "application/json",
             "User-Agent": "AeroX-Sweep/2.0"})
try:
    resp = urllib.request.urlopen(req, timeout=15)
    print(f"[sweep-mail] Resend HTTP {resp.status}: {resp.read().decode()[:200]}")
except Exception as e:
    print(f"[sweep-mail] Resend-Fehler: {e}", file=sys.stderr)
PY
fi

exit 0
