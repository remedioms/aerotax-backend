#!/usr/bin/env bash
# Ein-Kommando-Setup des FR24-Harvesters auf einer frischen Ubuntu-VM.
# Aufruf (auf der VM, nach SSH):
#
#   SUPABASE_URL='https://<ref>.supabase.co' \
#   SUPABASE_KEY='<service-role-key>' \
#   TILES='0,1' \
#   bash bootstrap.sh
#
# TILES pro VM verschieden setzen: VM1='0,1' VM2='2,3' VM3='4,5' VM4='6,7'
# (oder eine VM mit TILES='all' für den Einstieg).
set -euo pipefail

: "${SUPABASE_URL:?SUPABASE_URL fehlt}"
: "${SUPABASE_KEY:?SUPABASE_KEY fehlt}"
TILES="${TILES:-all}"
POLL_SECONDS="${POLL_SECONDS:-20}"

echo "[bootstrap] installiere python3-pip + requests ..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3-pip curl
pip3 install --quiet --user requests || sudo pip3 install --quiet requests

sudo mkdir -p /opt/fr24
# harvester.py muss neben diesem Skript liegen (scp/git) ODER wird hier geholt:
if [ -f "$(dirname "$0")/harvester.py" ]; then
  sudo cp "$(dirname "$0")/harvester.py" /opt/fr24/harvester.py
elif [ ! -f /opt/fr24/harvester.py ]; then
  echo "[bootstrap] harvester.py nicht gefunden — bitte neben bootstrap.sh legen." >&2
  exit 1
fi

echo "[bootstrap] schreibe systemd-Service (TILES=$TILES) ..."
sudo tee /etc/systemd/system/fr24-harvester.service >/dev/null <<UNIT
[Unit]
Description=AeroX FR24 harvester
After=network-online.target
Wants=network-online.target

[Service]
Environment=SUPABASE_URL=${SUPABASE_URL}
Environment=SUPABASE_KEY=${SUPABASE_KEY}
Environment=TILES=${TILES}
Environment=POLL_SECONDS=${POLL_SECONDS}
ExecStart=/usr/bin/python3 /opt/fr24/harvester.py
Restart=always
RestartSec=15
User=root

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now fr24-harvester
sleep 3
echo "[bootstrap] fertig. Live-Logs:"
sudo journalctl -u fr24-harvester -n 15 --no-pager || true
echo
echo "[bootstrap] Dauer-Logs:  sudo journalctl -u fr24-harvester -f"
