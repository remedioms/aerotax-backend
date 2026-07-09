#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  poll-tick.sh — VORLAGE für den Hetzner-Minuten-Cron (adaptives Polling)
#
#  Die ECHTE Datei liegt auf dem Hetzner-Host (cron: * * * * * → tick.sh);
#  diese Vorlage zeigt NUR, was der Owner dort ersetzen muss:
#
#    ALT:  (( M % 10 == 0 )) && firep /api/internal/poll-boards
#    NEU:  firep "/api/internal/poll-boards?tier=auto"     # jede Minute, ohne Modulo
#
#  Der Endpoint taktet mit ?tier=auto selbst pro Airport (Event-Fenster ±45 min
#  → 3 min, Roster-Demand ±3h/FRA/MUC → 5 min, Default → 10 min, Nacht lokal
#  0–5 Uhr → 30 min). OHNE ?tier=auto verhält er sich exakt wie bisher — die
#  Umstellung ist also gefahrlos vor/nach dem Backend-Deploy möglich (erst
#  Backend deployen, dann diese Zeile tauschen).
#
#  UNVERÄNDERT lassen: /api/adsb/poll (jede Minute), /api/airport/
#  poll-punctuality (alle 10 min), /api/internal/scrape-boards (alle 15 min,
#  Playwright/eu_scraper — Bot-Wall-Risiko, NICHT beschleunigen). Die
#  firep/fires-Helper der echten tick.sh (curl -X POST mit X-Poll-Secret bzw.
#  Scraper-Ziel) ebenfalls unverändert übernehmen — unten nur als Platzhalter.
# ═══════════════════════════════════════════════════════════════

set -u

# ── Platzhalter-Helper (in der echten tick.sh existieren die schon) ──────────
BACKEND="${BACKEND:-https://hetzner-api.example/api}"   # echte Backend-Basis-URL
SECRET="${ADSB_POLL_SECRET:?ADSB_POLL_SECRET fehlt}"

firep() {  # POST gegen das Backend, mit Poll-Secret (wie in der echten tick.sh)
    curl -fsS -m 110 -X POST -H "X-Poll-Secret: ${SECRET}" \
        -H "User-Agent: aerox-poll-tick" "${BACKEND%/api}${1}" >/dev/null 2>&1
}
fires() {  # POST gegen den eu_scraper (Definition aus der echten tick.sh übernehmen)
    curl -fsS -m 110 -X POST -H "X-Poll-Secret: ${SECRET}" \
        -H "User-Agent: aerox-poll-tick" "${EU_SCRAPER_BASE:?}${1}" >/dev/null 2>&1
}

# ── Tick ─────────────────────────────────────────────────────────────────────
M=$((10#$(date +%M)))

firep /api/adsb/poll                                    # jede Minute (unverändert)
firep "/api/internal/poll-boards?tier=auto"             # NEU: jede Minute, Endpoint taktet selbst
(( M % 10 == 0 )) && firep /api/airport/poll-punctuality        # unverändert
(( (M-5) % 15 == 0 && M>=5 )) && fires /api/internal/scrape-boards  # unverändert

exit 0
