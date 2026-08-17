#!/bin/bash
# Flugbuch-Upload-Wächter — Host-Cron-Wrapper.
#
# Der Parser im deployten Container-Image ist die kanonische Quelle. Eine
# frühere Wrapper-Version kopierte vor jedem Lauf eine Host-Kopie in den
# Container und konnte dadurch einen gerade deployten Fix wieder mit altem
# Code überschreiben. flock garantiert weiterhin höchstens einen Lauf.
set -u
LOCK=/var/lock/logbook-watchdog.lock
LOG=/var/log/logbook-watchdog.log
exec 9>"$LOCK"
flock -n 9 || exit 0

# Log-Deckel: über 5 MB die ältere Hälfte verwerfen (kein logrotate nötig).
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG")" -gt 5242880 ]; then
  tail -c 2621440 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

docker exec -w /app/tools/logbook-parsers aerotax-backend \
  python3 logbook_watchdog.py >> "$LOG" 2>&1

# Freie Airlines aus dem Onboarding: derselbe Cron-Lock, damit auch die
# KI-/Feed-Retries höchstens einmal parallel laufen. Ein Fehler hier darf den
# nächsten regulären Flugbuch-Lauf nicht verhindern.
docker exec -w /app/tools/logbook-parsers aerotax-backend \
  python3 airline_support_watchdog.py >> "$LOG" 2>&1
