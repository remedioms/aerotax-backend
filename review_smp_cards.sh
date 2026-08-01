#!/usr/bin/env bash
# SMP-Flashcard Review — listet pending User-Karten und lässt den Owner sie
# per ID freigeben/ablehnen. Nutzt den BESTEHENDEN Admin-Mechanismus
# (X-Admin-Token == RECOVERY_SECRET, gleiches Muster wie admin_support_list /
# ax_crew_hotels-Admin / ax_lh_quota in app.py) — es gibt bewusst KEIN
# zweites Admin-Secret nur für dieses Feature.
#
# Usage:
#   export RECOVERY_SECRET=...          # gleicher Wert wie auf dem Server
#   ./review_smp_cards.sh                # zeigt alle pending Karten
#   ./review_smp_cards.sh approve <id>   # gibt eine Karte frei
#   ./review_smp_cards.sh reject <id>    # lehnt eine Karte ab
#
# API_BASE kann überschrieben werden (Default: Produktion).
set -euo pipefail

API_BASE="${API_BASE:-https://api.aerosteuer.de}"

if [ -z "${RECOVERY_SECRET:-}" ]; then
  echo "RECOVERY_SECRET ist nicht gesetzt. export RECOVERY_SECRET=... (gleicher Wert wie auf dem Server)." >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "jq wird benötigt (brew install jq)." >&2
  exit 1
fi

cmd="${1:-list}"

list_pending() {
  local resp
  resp="$(curl -sS -H "X-Admin-Token: ${RECOVERY_SECRET}" \
    "${API_BASE}/api/ax/smp/review/pending")"

  if ! echo "$resp" | jq -e '.ok' >/dev/null 2>&1; then
    echo "Fehler beim Laden der Review-Queue:" >&2
    echo "$resp" >&2
    exit 1
  fi

  local count
  count="$(echo "$resp" | jq -r '.count')"
  echo "── ${count} Karte(n) warten auf Review ──"
  echo ""
  echo "$resp" | jq -r '
    .cards[]
    | "ID:      \(.id)\nModul:   \(.module)\nThema:   \(.topic // "—")\nErstellt: \(.created_at)\n\nFRAGE:\n\(.front)\n\nANTWORT:\n\(.back)\n" + ("─" * 60)
  '
  echo ""
  echo "Freigeben:  ./review_smp_cards.sh approve <id>"
  echo "Ablehnen:   ./review_smp_cards.sh reject <id>"
}

decide() {
  local decision="$1"
  local id="${2:-}"
  if [ -z "$id" ]; then
    echo "Usage: ./review_smp_cards.sh ${decision} <id>" >&2
    exit 1
  fi
  local resp
  resp="$(curl -sS -X POST \
    -H "X-Admin-Token: ${RECOVERY_SECRET}" \
    -H "Content-Type: application/json" \
    -d "{\"decision\":\"${decision}\"}" \
    "${API_BASE}/api/ax/smp/review/${id}")"

  if echo "$resp" | jq -e '.ok' >/dev/null 2>&1; then
    echo "OK — Karte ${id}: ${decision}"
  else
    echo "Fehler:" >&2
    echo "$resp" >&2
    exit 1
  fi
}

case "$cmd" in
  list)
    list_pending
    ;;
  approve)
    decide approved "${2:-}"
    ;;
  reject)
    decide rejected "${2:-}"
    ;;
  *)
    echo "Usage: ./review_smp_cards.sh [list|approve <id>|reject <id>]" >&2
    exit 1
    ;;
esac
