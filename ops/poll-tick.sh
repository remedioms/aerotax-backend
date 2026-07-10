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

# ── NEU (Permanenz-Plan (c)): Track-Verdichtung VOR dem Prune ────────────────
#  In der ECHTEN Hetzner-crontab (NICHT in tick.sh — eigene Tages-Cron-Zeilen):
#
#    # 03:40 UTC: Breadcrumbs älter als RETENTION−2 Tage per Douglas-Peucker
#    #            (≤80 Punkte/Leg) dauerhaft nach flight_tracks_archive
#    #            verdichten. Idempotent; hebt die Watermark 'trackarch:until'.
#    40 3 * * *  curl -fsS -m 300 -X POST -H "X-Poll-Secret: ${SECRET}" -H "User-Agent: aerox-poll-tick" "${BACKEND%/api}/api/internal/track-compact" >/dev/null 2>&1
#
#    # 04:17 UTC: bestehender track-prune (Zeile UNVERÄNDERT lassen) — löscht
#    #            seit dem Compact-Deploy nur noch, was archiviert ist ODER
#    #            älter als Retention+2 Tage (Sicherheitsnetz). Reihenfolge
#    #            wichtig: compact (03:40) VOR prune (04:17).
#    17 4 * * *  curl -fsS -m 300 -X POST -H "X-Poll-Secret: ${SECRET}" -H "User-Agent: aerox-poll-tick" "${BACKEND%/api}/api/internal/track-prune" >/dev/null 2>&1
#
#  Einmaliger Backfill (M4, vor dem Scharfschalten): den compact-Aufruf mit
#  ?max_legs=5000 wiederholt ausführen, bis 'days_done' 0 bleibt — dann sind
#  alle vorhandenen ~10 Retention-Tage archiviert.

exit 0
