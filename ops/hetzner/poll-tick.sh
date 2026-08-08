#!/bin/bash
# AeroX Polls auf Hetzner (DC-Netz, immer an). Laeuft jede Minute via cron.
cd /opt/aerox || exit 0
PS=$(grep "^ADSB_POLL_SECRET=" env.list | cut -d= -f2-)
SS=$(grep "^AEROX_SCRAPE_SECRET=" env.list | cut -d= -f2-)
M=$((10#$(date +%M)))
firep(){ curl -s -o /dev/null -m 280 -X POST -H "X-Poll-Secret: $PS" "http://127.0.0.1:8081$1" & }
fires(){ curl -s -o /dev/null -m 280 -X POST -H "X-Scrape-Secret: $SS" "http://127.0.0.1:8081$1" & }
firep /api/adsb/poll                                            # JEDE Minute (Watch-Positionen frisch, ~14s)
(( M % 10 == 0 ))             && firep /api/airport/poll-punctuality
firep "/api/internal/poll-boards?tier=auto"
(( (M-5) % 15 == 0 && M>=5 )) && fires /api/internal/scrape-boards
wait
# Detail-Warmer (Owner 2026-07-25): alle User-Fluege heute+morgen vorwaermen —
# auf dem BACKEND-Port (8080), dessen Disk-Cache teilen sich die 3 Worker.
(( M % 30 == 7 )) && curl -s -o /dev/null -m 10 -X POST -H "X-Poll-Secret: $PS" "http://127.0.0.1:8080/api/internal/warm-flight-details" &
