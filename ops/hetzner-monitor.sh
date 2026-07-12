#!/bin/bash
# AeroX Hetzner Health-Monitor. Cron alle 5 Min. Alert-Mail via Resend, entprellt 1h.
# Versionierte Kopie: liegt live auf dem Hetzner-Server unter /opt/aerox/monitor.sh.
cd /opt/aerox || exit 0
RK=$(grep "^RESEND_API_KEY=" env.list | cut -d= -f2-)
TO=$(grep "^SUPPORT_NOTIFY_EMAIL=" env.list | cut -d= -f2-)
STATE=/var/lib/aerox-mon.state
issues=""
DISK=$(df / | awk 'NR==2{gsub("%","",$5);print $5}')
[ "${DISK:-0}" -gt 85 ] && issues="${issues}
- Disk ${DISK}% (>85)"
MEM=$(free | awk '/Mem:/{printf "%d",($2-$7)/$2*100}')
[ "${MEM:-0}" -gt 90 ] && issues="${issues}
- RAM ${MEM}% (>90)"
CORES=$(nproc); LIM=$((CORES*2)); LOAD=$(awk '{print $1}' /proc/loadavg)
awk -v l="$LOAD" -v m="$LIM" 'BEGIN{exit !(l>m)}' && issues="${issues}
- Load ${LOAD} (>${LIM})"
for c in aerotax-backend aerotax-poll cloudflared; do
  st=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null)
  [ "$st" = "running" ] || issues="${issues}
- Container $c: ${st:-FEHLT}"
done
code=$(curl -s -o /dev/null -w '%{http_code}' -m 8 http://127.0.0.1:8080/api/health)
[ "$code" = "200" ] || issues="${issues}
- /api/health HTTP ${code}"
now=$(date +%s)
if [ -n "$issues" ]; then
  sig=$(printf '%s' "$issues" | md5sum | cut -d' ' -f1)
  last_sig=$(sed -n 1p "$STATE" 2>/dev/null); last_ts=$(sed -n 2p "$STATE" 2>/dev/null)
  if [ "$sig" != "$last_sig" ] || [ $((now - ${last_ts:-0})) -gt 3600 ]; then
    python3 - "$RK" "$TO" "$issues" "$DISK" "$MEM" "$LOAD" <<'PY'
import sys,json,urllib.request,socket
rk,to,issues,disk,mem,load=sys.argv[1:7]
body=f"AeroX Hetzner ({socket.gethostname()}) Alert:\n{issues}\n\nDisk {disk}%  RAM {mem}%  Load {load}"
data=json.dumps({"from":"AeroX Monitor <support@aerosteuer.de>","to":[to],"subject":"⚠️ AeroX Hetzner Alert","text":body}).encode()
req=urllib.request.Request("https://api.resend.com/emails",data=data,headers={"Authorization":"Bearer "+rk,"Content-Type":"application/json","User-Agent":"AeroX-Monitor/1.0"})
try: urllib.request.urlopen(req,timeout=15)
except Exception: pass
PY
    printf '%s\n%s\n' "$sig" "$now" > "$STATE"
  fi
else
  rm -f "$STATE"
fi

# --- NAS-Harvester-Frische (2026-07-12) -------------------------------------
# Der fr24-harvester (NAS-Container) schreibt aircraft_live nach Supabase.
# Stirbt er, versiegen die Boden-/Positionsdaten STILL — deshalb: juengstes
# updated_at pruefen; aelter als NAS_MAX_MIN Minuten (oder nicht abfragbar)
# => Alert. Eigenes State-File, Anti-Spam 6h (der Harvester braucht ggf. laenger
# zum Heilen als die 1h-Entprellung oben).
NAS_MAX_MIN="${NAS_MAX_MIN:-15}"
NAS_STATE=/var/lib/aerox-mon-nas.state
SU=$(grep "^SUPABASE_URL=" env.list | cut -d= -f2-)
SK=$(grep "^SUPABASE_SERVICE_KEY=" env.list | cut -d= -f2-)
latest=$(curl -s -m 10 -H "apikey: $SK" -H "Authorization: Bearer $SK" \
  -H "User-Agent: AeroX-Monitor/1.0" \
  "$SU/rest/v1/aircraft_live?select=updated_at&order=updated_at.desc&limit=1")
age_min=$(python3 - "$latest" <<'PY'
import sys,json,datetime
try:
    ts=json.loads(sys.argv[1])[0]["updated_at"]
    dt=datetime.datetime.fromisoformat(ts.replace("Z","+00:00"))
    print(int((datetime.datetime.now(datetime.timezone.utc)-dt).total_seconds()//60))
except Exception:
    print(-1)  # nicht abfragbar (Supabase down / leere Tabelle) => auch Alert
PY
)
if [ "${age_min:--1}" -lt 0 ] || [ "$age_min" -gt "$NAS_MAX_MIN" ]; then
  last_nas=$(sed -n 1p "$NAS_STATE" 2>/dev/null)
  if [ $((now - ${last_nas:-0})) -gt 21600 ]; then
    python3 - "$RK" "$TO" "$age_min" <<'PY'
import sys,json,urllib.request,socket
rk,to,age=sys.argv[1:4]
detail=f"aircraft_live {age} min alt" if int(age)>=0 else "aircraft_live nicht abfragbar"
body=(f"AeroX Hetzner ({socket.gethostname()}) Alert:\n"
      f"- NAS-Harvester liefert nicht ({detail})\n\n"
      "Check: Container fr24-harvester auf dem Synology-NAS "
      "(docker logs fr24-harvester; :8787/health).")
data=json.dumps({"from":"AeroX Monitor <support@aerosteuer.de>","to":[to],"subject":"⚠️ AeroX Hetzner Alert","text":body}).encode()
req=urllib.request.Request("https://api.resend.com/emails",data=data,headers={"Authorization":"Bearer "+rk,"Content-Type":"application/json","User-Agent":"AeroX-Monitor/1.0"})
try: print("resend:",urllib.request.urlopen(req,timeout=15).read().decode())
except Exception as e: print("resend-fail:",e,file=sys.stderr)
PY
    printf '%s\n' "$now" > "$NAS_STATE"
  fi
else
  rm -f "$NAS_STATE"
fi
