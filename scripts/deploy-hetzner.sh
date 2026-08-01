#!/bin/bash
# AeroX Hetzner Deploy: neues Backend-Image ausrollen mit Health-Check + Auto-Rollback.
#
# GCLOUD-PYTHON-FIX (2026-08-01): gcloud griff sich Xcodes Python 3.9.6 und
# crashte beim Laden von gcloud.builds („'_MessageClass' | '_MessageClass'")
# — damit schlug JEDER --from-git-Build fehl. Homebrew-python3.11 ist die
# von gcloud unterstützte Version auf dieser Maschine; nur setzen, wenn der
# Aufrufer nichts anderes vorgibt und der Interpreter existiert.
if [ -z "${CLOUDSDK_PYTHON:-}" ] && [ -x /opt/homebrew/bin/python3.11 ]; then
    export CLOUDSDK_PYTHON=/opt/homebrew/bin/python3.11
fi
#
# EMPFOHLEN (Deploy-Gates 2026-07-27 — „Fixes bleiben gefixt"):
#   ./deploy-hetzner.sh --from-git [<git-ref>]
#     baut aus einem SAUBEREN Worktree des Commits das Image main-<shortsha>
#     und deployt es. Kein dirty Tree kann mehr unbemerkt in Prod landen.
# ALT-PFAD (fertiges Image):
#   ./deploy-hetzner.sh <ARTIFACT-REGISTRY-IMAGE-REF>
#     Gates gelten trotzdem; der Commit des Images wird als HEAD des Repos
#     angenommen (oder explizit via DEPLOY_SHA=<sha> übergeben).
#
# GATES (Vorfall 27.07.: HEAD-Deploy rollte den live laufenden, erst später
# committeten Ein-Refresher zurück; parallel deployende Sessions überschrieben
# sich gegenseitig):
#   1. LEASE      — nur EIN Deploy gleichzeitig (mkdir-Lock, bash-3.2-portabel).
#   2. CLEAN-TREE — Alt-Pfad verweigert dirty Trees (FORCE_DIRTY=1 übersteuert).
#   3. ANCESTOR   — der Host merkt sich den deployten Commit; ein neues Image
#                   muss ihn ENTHALTEN (merge-base), sonst würde der Deploy
#                   Live-Code zurückrollen (FORCE_ROLLBACK=1 übersteuert).
# Weitere Overrides: FORCE_DEPLOY=1 (Refresh-Cron-Fenster), DRY_RUN=1 (alle
# Gates + Build-Auflösung durchlaufen, aber NICHT deployen).
set -euo pipefail

REPO="$HOME/Developer/Backend/aerotax-backend"
AR_BASE="europe-west3-docker.pkg.dev/aerotax-prod/cloud-run-source-deploy/aerotax-backend"
DEPLOYED_SHA_FILE="/var/lib/aerox-deployed-sha"

# ── Argumente auflösen ───────────────────────────────────────────────────────
MODE="image"; IMG=""; GITREF=""
case "${1:?Nutzung: --from-git [<ref>] | <image-ref>}" in
  --from-git) MODE="git"; GITREF="${2:-HEAD}" ;;
  *)          IMG="$1" ;;
esac

# ── GATE 1: Deploy-Lease (ein Deploy zur Zeit, über alle Sessions) ──────────
LEASE="$HOME/aerox-oracle-prep/.deploy.lease.d"
_lease_acquired=0
_release_lease(){ [ "$_lease_acquired" = "1" ] && rm -rf "$LEASE" 2>/dev/null || true; }
trap _release_lease EXIT
_i=0
while ! mkdir "$LEASE" 2>/dev/null; do
  # Stale-Lease (>45 min alt = abgestürzter Deploy) übernehmen.
  if [ -n "$(find "$LEASE" -maxdepth 0 -mmin +45 2>/dev/null)" ]; then
    echo "⚠️  Stale Deploy-Lease (>45 min) — übernehme."
    rm -rf "$LEASE"; continue
  fi
  if [ "$_i" -eq 0 ]; then
    echo "⏳ Andere Session deployt gerade ($(cat "$LEASE/info" 2>/dev/null || echo '?')) — warte (max 30 min)…"
  fi
  _i=$((_i+1)); [ "$_i" -gt 120 ] && { echo "⛔ Lease nach 30 min nicht frei — Abbruch."; exit 1; }
  sleep 15
done
_lease_acquired=1
echo "pid=$$ start=$(date -u +%H:%M:%SZ) mode=$MODE" > "$LEASE/info"

# ── Commit des Deploys bestimmen (+ GATE 2: Clean-Tree im Alt-Pfad) ─────────
if [ "$MODE" = "git" ]; then
  DEPLOY_SHA=$(git -C "$REPO" rev-parse "$GITREF")
  if [ -n "$(git -C "$REPO" status --porcelain)" ]; then
    echo "ℹ️  Hinweis: Working Tree hat uncommittete Änderungen — sie fahren"
    echo "   NICHT mit (gebaut wird sauber aus Commit ${DEPLOY_SHA:0:7})."
  fi
else
  DEPLOY_SHA="${DEPLOY_SHA:-$(git -C "$REPO" rev-parse HEAD)}"
  if [ -n "$(git -C "$REPO" status --porcelain)" ] && [ "${FORCE_DIRTY:-0}" != "1" ]; then
    echo "⛔ Working Tree ist DIRTY und der Alt-Pfad kann nicht wissen, was im"
    echo "   Image steckt (Vorfall-Klasse: Prod lief mit uncommittetem Code,"
    echo "   der nächste saubere Deploy rollte ihn zurück)."
    echo "   → Empfohlen: erst committen, dann './deploy-hetzner.sh --from-git'."
    echo "   → Bewusster Ausnahme-Pfad: FORCE_DIRTY=1 (und DEPLOY_SHA=<sha> setzen)."
    exit 1
  fi
fi
SHORT="${DEPLOY_SHA:0:7}"

source ~/aerox-oracle-prep/hetzner.env
KEY="$HOME/.ssh/hetzner_aerox"; KH="$HOME/.ssh/known_hosts_hetzner"
rsh(){ ssh -i "$KEY" -o UserKnownHostsFile="$KH" -o StrictHostKeyChecking=accept-new root@"$server_ip" "$@"; }

# ── GATE 3: Ancestor-Check gegen den live deployten Commit ──────────────────
DEPLOYED=$(rsh "cat $DEPLOYED_SHA_FILE 2>/dev/null" || true)
if [ -n "$DEPLOYED" ]; then
  if ! git -C "$REPO" cat-file -e "$DEPLOYED" 2>/dev/null; then
    echo "⛔ Live deployter Commit $DEPLOYED ist diesem Repo UNBEKANNT — erst"
    echo "   klären, was auf Prod läuft (FORCE_ROLLBACK=1 übersteuert bewusst)."
    [ "${FORCE_ROLLBACK:-0}" = "1" ] || exit 1
  elif ! git -C "$REPO" merge-base --is-ancestor "$DEPLOYED" "$DEPLOY_SHA" 2>/dev/null; then
    echo "⛔ ROLLBACK-GEFAHR: das neue Image (${SHORT}) enthält den live"
    echo "   laufenden Stand (${DEPLOYED:0:7}) NICHT — dieser Deploy würde"
    echo "   bereits ausgelieferten Code zurückrollen (Vorfall 27.07.:"
    echo "   Ein-Refresher 4 h weg). Erst rebasen/mergen, dann deployen."
    echo "   Bewusster Rollback: FORCE_ROLLBACK=1."
    [ "${FORCE_ROLLBACK:-0}" = "1" ] || exit 1
    echo "⚠️  FORCE_ROLLBACK=1 gesetzt — fahre bewusst fort."
  else
    echo "✓ Ancestor-Gate: ${SHORT} enthält Live-Stand ${DEPLOYED:0:7}."
  fi
else
  echo "ℹ️  Kein deployter Commit auf dem Host vermerkt (Bootstrap) — Gate übersprungen."
fi

# ── REFRESH-CRON-FENSTER-GUARD (25.07.2026: 33 LH-FlightOps-Grants verbrannt) ──
# Der refresh-all-Cron laeuft `23 */2 * * *` UTC (gerade Stunden) und braucht
# fuer 200+ User bis ~25 min. Ein Container-Restart MITTEN im Lauf killt
# Refreshes zwischen LH-Token-Rotation und Persist bzw. drueckt die Claim-RPC
# in den Fallback → LH-Reuse-Detection toetet ganze Grant-Familien. Deploys
# in geraden UTC-Stunden zwischen :21 und :50 daher blocken. FORCE_DEPLOY=1
# uebersteuert bewusst.
if [ "${FORCE_DEPLOY:-0}" != "1" ] && [ "${DRY_RUN:-0}" != "1" ]; then
  _H=$(date -u +%H); _M=$(date -u +%M)
  if [ $((10#$_H % 2)) -eq 0 ] && [ $((10#$_M)) -ge 21 ] && [ $((10#$_M)) -le 50 ]; then
    echo "⛔ Refresh-all-Cron-Fenster (gerade UTC-Stunde, :21–:50) — Deploy wuerde"
    echo "   laufende LH-Grant-Rotationen killen. Spaeter erneut ausfuehren oder"
    echo "   bewusst mit FORCE_DEPLOY=1 erzwingen."
    exit 1
  fi
fi

# ── --from-git: Image aus sauberem Worktree bauen (main-<shortsha>) ─────────
if [ "$MODE" = "git" ]; then
  IMG="$AR_BASE:main-$SHORT"
  if [ "${DRY_RUN:-0}" = "1" ]; then
    echo "🧪 DRY_RUN: Build übersprungen (würde $IMG bauen)."
  elif gcloud artifacts docker images describe "$IMG" >/dev/null 2>&1; then
    echo "✓ Image $IMG existiert schon — Build übersprungen."
  else
    echo "[0/5] Baue $IMG aus sauberem Worktree (${SHORT})…"
    WT=$(mktemp -d /tmp/aerox-deploy-wt.XXXXXX)
    git -C "$REPO" worktree add --detach "$WT" "$DEPLOY_SHA" >/dev/null
    ( cd "$WT" && gcloud builds submit --tag "$IMG" . >/dev/null ) \
      || { git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1; echo "⛔ Build fehlgeschlagen."; exit 1; }
    git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1 || true
    echo "✓ Build fertig."
  fi
fi

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "🧪 DRY_RUN: alle Gates grün — würde jetzt $IMG (Commit ${SHORT}) deployen. Ende."
  exit 0
fi

TOKEN=$(gcloud auth print-access-token)

echo "[1/5] AR-Login + Pull auf Hetzner…"
rsh "echo '$TOKEN' | docker login -u oauth2accesstoken --password-stdin https://europe-west3-docker.pkg.dev >/dev/null && docker pull '$IMG' >/dev/null && echo OK"
echo "[2/5] aktuelles Image sichern + Compose umschreiben…"
# Deploy-Flag fuer den Monitor (Alarm-Stille waehrend des Deploy-Fensters,
# monitor.sh prueft das Alter; Audit 19.07: Deploy-Schnappschuss mailte
# "removing/created/HTTP 000" obwohl alles planmaessig war).
rsh "date +%s > /var/lib/aerox-deploy-flag"
# Vorherigen SHA fuer den Rollback-Pfad sichern (Ancestor-Wahrheit bleibt
# auch nach einem automatischen Rollback korrekt).
PREV_SHA="$DEPLOYED"
# Nur Zeilen umschreiben, die schon auf ein aerotax-backend-Image zeigen.
# Der Poll-Split ist Absicht: `aerotax-backend` (:8080) und `aerotax-poll`
# (:8081) laufen aus DEMSELBEN Image, beide Zeilen MUESSEN mitwandern. Das
# alte `s#image: .*#…#` traf aber jede image-Zeile — ein spaeter ergaenzter
# Dienst (Redis, Postgres, Exporter) waere beim naechsten Deploy stillschweigend
# auf das Backend-Image umgebogen worden und haette als "gesund" gestartet.
# Danach wird geprueft, dass ueberhaupt etwas ersetzt wurde.
REWROTE=$(rsh "cd /opt/aerox && cp compose.yaml compose.yaml.prev && sed -i -E 's#^([[:space:]]*image:[[:space:]]*).*aerotax-backend.*\$#\\1$IMG#' compose.yaml && grep -c '$IMG' compose.yaml")
if [ "${REWROTE:-0}" -lt 1 ]; then
  echo "❌ Compose-Umschreibung traf KEINE aerotax-backend-image-Zeile — Abbruch vor dem Neustart."
  rsh "cd /opt/aerox && cp compose.yaml.prev compose.yaml"
  exit 1
fi
echo "     $REWROTE image-Zeile(n) auf $IMG gesetzt."
echo "[2b/5] FlightOps-Refresh drainen (Grant-Burn-Schutz)…"
# Praeziser Schutz zusaetzlich zum Cron-Fenster-Guard oben: laufende
# refresh-all-Laeufe sauber auslaufen lassen (drain-Flag + poll bis
# running=false, max 60s). Ein Kill zwischen LH-Token-Rotation und Persist
# verbrennt sonst die Grant-Familie (Reuse-Detection).
rsh 'SEC=$(grep "^ADSB_POLL_SECRET=" /opt/aerox/env.list | cut -d= -f2-)
for port in 8080 8081; do
  curl -s -m5 -X POST -H "X-Poll-Secret: $SEC" http://127.0.0.1:$port/api/internal/flightops/refresh-drain >/dev/null 2>&1 || true
done
for i in $(seq 1 20); do
  R1=$(curl -s -m5 -X POST -H "X-Poll-Secret: $SEC" http://127.0.0.1:8080/api/internal/flightops/refresh-drain 2>/dev/null)
  R2=$(curl -s -m5 -X POST -H "X-Poll-Secret: $SEC" http://127.0.0.1:8081/api/internal/flightops/refresh-drain 2>/dev/null)
  case "$R1$R2" in *"\"running\": true"*|*"\"running\":true"*) sleep 3 ;; *) break ;; esac
done
echo drained'
echo "[3/5] Container neu erstellen…"
rsh "cd /opt/aerox && docker compose up -d >/dev/null 2>&1 && echo OK"
echo "[4/5] Health-Check (bis 60s)…"
# Geprueft werden BEIDE Container und der Blueprint-Zustand:
#  · :8080 = Public-Backend (AeroX-API)
#  · :8081 = Poll-/Refresher-Container aus DEMSELBEN Image. Er trug bisher
#    keinerlei Gate — ein Image, das nur dort kaputtgeht (Poll, FlightOps-
#    Refresher, Cron-Ziele), wurde als "gesund" durchgewunken, waehrend der
#    Refresher still tot war.
#  · blueprints_failed: app.py faengt Blueprint-Importfehler ab, damit ein
#    einzelner Fehler nicht das Backend killt. Ohne diese Pruefung deployt ein
#    Backend mit toten Feature-Familien (Flugbuch, Trip-Trade, Crew-Graph) mit
#    gruenem Haken.
OK=$(rsh 'R=FAIL
for i in $(seq 1 20); do
  sleep 3
  c=$(curl -s -o /dev/null -w "%{http_code}" -m5 http://127.0.0.1:8080/api/health)
  [ "$c" = 200 ] || continue
  c2=$(curl -s -o /dev/null -w "%{http_code}" -m5 http://127.0.0.1:8081/api/health)
  [ "$c2" = 200 ] || { R=FAIL_POLL; continue; }
  body=$(curl -s -m5 http://127.0.0.1:8080/api/health)
  # KEIN case-Muster hier: `[]` im Muster liest bash als Zeichenklasse und
  # bricht mit einem Syntaxfehler ab — der riss beim ersten Einsatz (01.08.)
  # unter `set -e` das ganze Skript nach dem Container-Neustart weg, also
  # OHNE Rollback und OHNE die SHA-Buchhaltung.
  #
  # NICHT unbedingt abbrechen (zweiter Anlauf 01.08.): ein `break` hinter dem
  # if/else beendete die Schleife auch dann, wenn dieser EINE Body-Abruf leer
  # zurueckkam (Worker gerade im Neustart). Ergebnis war ein Rollback mit der
  # Meldung "Blueprint kaputt", obwohl blueprints_failed nachweislich []
  # war — ein Fehlalarm, der einen gesunden Deploy zurueckdrehte. Nur ERFOLG
  # bricht ab; ein echter Blueprint-Fehler faellt nach 20 Versuchen trotzdem
  # durch, weil R dann FAIL_BLUEPRINTS bleibt.
  if [ -n "$body" ] && printf %s "$body" | grep -q "blueprints_failed[\"]*:[ ]*\[\]"; then
    R=OK
    break
  fi
  R=FAIL_BLUEPRINTS
done
echo $R')
case "$OK" in
  FAIL_POLL)       echo "     ⚠️  :8080 gesund, aber der Poll-/Refresher-Container (:8081) antwortet nicht." ;;
  FAIL_BLUEPRINTS) echo "     ⚠️  Backend laeuft, aber mindestens ein Blueprint hat den Import NICHT ueberlebt:"
                   rsh "curl -s -m5 http://127.0.0.1:8080/api/health | tr ',' '\n' | grep -A2 blueprints_failed" || true ;;
esac
if [ "$OK" = "OK" ]; then
  echo "[5/5] ✅ Deploy erfolgreich & gesund."
  # Deployten Commit auf dem Host vermerken (Quelle des Ancestor-Gates).
  rsh "echo '$DEPLOY_SHA' > $DEPLOYED_SHA_FILE"
  # Alte Backend-Images aufräumen — nur aktuelles + Rollback (compose.yaml.prev)
  # behalten. Ohne das lief die Platte bei jedem Deploy voller (2026-07-12:
  # 12 Images à 767 MB → 89 % → Monitor-Alerts).
  echo "[6/6] Image-Cleanup (behalte aktuelles + Rollback)…"
  rsh "PREV=\$(grep 'image:' /opt/aerox/compose.yaml.prev 2>/dev/null | head -1 | awk '{print \$2}'); docker images --format '{{.Repository}}:{{.Tag}}' | grep aerotax-backend | grep -v -e '$IMG' -e \"\$PREV\" | xargs -r docker rmi >/dev/null 2>&1; docker image prune -f >/dev/null; echo OK"
else
  echo "[5/5] ❌ ungesund → automatischer Rollback aufs vorige Image…"
  rsh "cd /opt/aerox && cp compose.yaml.prev compose.yaml && docker compose up -d"
  # SHA-Vermerk auf den zurueckgerollten Stand zuruecksetzen.
  if [ -n "$PREV_SHA" ]; then rsh "echo '$PREV_SHA' > $DEPLOYED_SHA_FILE"; fi
  echo "Rollback erledigt. Deploy ABGEBROCHEN."; exit 1
fi
