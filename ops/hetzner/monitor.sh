#!/bin/bash
# DEPLOY-FENSTER-GATE (19.07): deploy-hetzner.sh setzt /var/lib/aerox-deploy-flag.
# Ist das Flag <10 min alt, laeuft gerade ein geplanter Deploy (Container
# removing/created, Health kurz weg) — dann KEINE Alerts, still beenden.
if [ -f /var/lib/aerox-deploy-flag ]; then
  _df=$(cat /var/lib/aerox-deploy-flag 2>/dev/null || echo 0)
  if [ $(( $(date +%s) - _df )) -lt 600 ]; then exit 0; fi
fi

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
  # SIG OHNE ZAHLEN (2026-08-02): das Alter/Prozente im Text aendern sich pro
  # Lauf — mit Zahlen in der Signatur war die Entprellung wirkungslos und der
  # Monitor mailte alle 5 Minuten (55 Mails in einer Nacht).
  sig=$(printf '%s' "$issues" | sed -E 's/[0-9]+//g' | md5sum | cut -d' ' -f1)
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

# --- Board-Scraper-Frische (2026-07-17, ZRH-ARR-Luecke 10:31-13:19Z) ---------
# euscraper-Gruppen schreiben stuendlich, der FRA-Poller minuetlich; versiegt
# eine Gruppe still (Playwright-Haenger), fehlen Ankunfts-Ist-Zeiten und Crew
# bleibt im Feed "IM FLUG" (Lane/LX1719). Juengstes updated_at je Schluessel-
# Airport pruefen; aelter als Limit => Alert, 6h-Entprellung, eigenes State.
BOARD_STATE=/var/lib/aerox-mon-board.state
board_issues=""
check_board() {
  local ap="$1"; local lim="$2"; local qs="$3"; local qe="$4"
  # qs/qe (optional, HHMM UTC, Fenster darf ueber Mitternacht gehen): RUHE-
  # Fenster = Nachtflugsperre des Airports. Innerhalb wird NICHT bewertet
  # (leere Tafel = keine Writes = kein Ausfall; ZRH stand am 02.08. 7h still
  # und war kerngesund). NACH dem Fenster zaehlt die Wartezeit ab Fenster-
  # ende, nicht ab letztem Write — sonst feuert beim Fenster-Austritt sofort
  # ein Fehlalarm, solange der erste Morgen-Write noch aussteht.
  if [ -n "$qs" ]; then
    local hh; hh=$(date -u +%H%M)
    if [ "$qs" -gt "$qe" ]; then
      { [ "$hh" -ge "$qs" ] || [ "$hh" -le "$qe" ]; } && return
    else
      { [ "$hh" -ge "$qs" ] && [ "$hh" -le "$qe" ]; } && return
    fi
  fi
  local enc; enc=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=''))" "$ap")
  # Datums-Fenster (heute+gestern) haelt die Query auf dem PK-Index — ohne
  # Filter lief die FRA-Abfrage in einen Seq-Scan-Timeout und der Sentinel
  # 99999 feuerte einen Fehlalarm (2026-07-17 16:25). Plus Retry.
  local since; since=$(date -u -d '1 day ago' +%F)
  local url="$SU/rest/v1/airport_delay_obs?select=updated_at&airport=eq.$enc&date=gte.$since&order=updated_at.desc&limit=1"
  local latest; latest=$(curl -s -m 20 -H "apikey: $SK" -H "Authorization: Bearer $SK" \
    -H "User-Agent: AeroX-Monitor/1.0" "$url")
  case "$latest" in "["*) ;; *) sleep 3; latest=$(curl -s -m 20 -H "apikey: $SK" -H "Authorization: Bearer $SK" \
    -H "User-Agent: AeroX-Monitor/1.0" "$url");; esac
  local age; age=$(python3 - "$latest" <<'PY2'
import sys,json,datetime,re
try:
    rows=json.loads(sys.argv[1]); ts=rows[0]["updated_at"].replace("Z","+00:00")
    # Python 3.10 fromisoformat kann NUR 3- oder 6-stellige Subsekunden —
    # PostgREST liefert getrimmte (.2016) -> auf 6 Stellen padden (Audit 19.07:
    # intermittierende "Frische nicht abfragbar"-Mails).
    ts=re.sub(r'\.(\d{1,6})', lambda m: '.'+m.group(1)[:6].ljust(6,'0'), ts, count=1)
    dt=datetime.datetime.fromisoformat(ts)
    if dt.tzinfo is None: dt=dt.replace(tzinfo=datetime.timezone.utc)
    print(int((datetime.datetime.now(datetime.timezone.utc)-dt).total_seconds()//60))
except Exception:
    print(99999)
PY2
)
  if [ "${age:-99999}" -eq 99999 ]; then
    # Fetch/Parse-Fehler = UNBEKANNT, nicht automatisch still: erst nach 2
    # Laeufen in Folge alarmieren (2026-07-17 20:10: transienter Supabase-
    # Hickup meldete 99999, obwohl der juengste Write 2 min alt war).
    local sf="/var/lib/aerox-mon-board-unknown-$(printf %s "$ap" | tr -c "A-Za-z0-9" _)"
    if [ -f "$sf" ]; then
      board_issues="${board_issues}
- Board ${ap}: Frische nicht abfragbar (2 Laeufe in Folge)"
    else
      touch "$sf"
    fi
    return
  fi
  rm -f "/var/lib/aerox-mon-board-unknown-$(printf %s "$ap" | tr -c "A-Za-z0-9" _)"
  # Alter am Ruhefenster-Ende kappen: die Nacht-Stille zaehlt nicht mit.
  if [ -n "$qs" ]; then
    local qe_s now_s since_qe
    now_s=$(date -u +%s)
    qe_s=$(date -u -d "today $(printf '%02d:%02d' $((10#$qe / 100)) $((10#$qe % 100)))" +%s)
    if [ "$now_s" -ge "$qe_s" ]; then
      since_qe=$(( (now_s - qe_s) / 60 ))
      [ "$since_qe" -lt "$age" ] && age=$since_qe
    fi
  fi
  [ "$age" -gt "$lim" ] && board_issues="${board_issues}
- Board ${ap}: letzter Write vor ${age} min (Limit ${lim})"
}
# NACHTFENSTER (Scraping-Audit 19.07, verallgemeinert 02.08.): Nachtflug-
# verbote = leere Boards = keine Writes = KEIN Ausfall. FRA 23-05 lokal;
# ZRH/VIE-Sperre ~23:30-06:00 lokal, gemessene Write-Luecke 02.08.:
# ZRH#ARR 21:18Z-04:19Z (7h, kerngesund). Fenster in UTC mit Marge fuer
# Sommer/Winter; ZRH/VIE-Gruppen laufen nur stuendlich (:05, NAS).
check_board "FRA" 240 2030 430
check_board "FRA#ARR" 240 2030 430
check_board "ZRH#ARR" 150 2030 600
check_board "VIE" 150 2030 600
if [ -n "$board_issues" ]; then
  # SIG OHNE ZAHLEN — siehe oben: "vor 174 min" -> "vor 179 min" erzeugte
  # sonst pro Lauf eine neue Signatur und hebelte die 6h-Entprellung aus.
  bsig=$(printf '%s' "$board_issues" | sed -E 's/[0-9]+//g' | md5sum | cut -d' ' -f1)
  bl_sig=$(sed -n 1p "$BOARD_STATE" 2>/dev/null); bl_ts=$(sed -n 2p "$BOARD_STATE" 2>/dev/null)
  if [ "$bsig" != "$bl_sig" ] || [ $((now - ${bl_ts:-0})) -gt 21600 ]; then
    python3 - "$RK" "$TO" "$board_issues" <<'PY3'
import sys,json,urllib.request,socket
rk,to,issues=sys.argv[1:4]
body=f"AeroX Board-Scraper-Frische ({socket.gethostname()}):\n{issues}\n\nQuelle: airport_delay_obs updated_at. Stunden-Gruppen (ZRH/VIE) laufen auf dem NAS (eu-scraper, cron :05/:20/:35/:50)."
data=json.dumps({"from":"AeroX Monitor <support@aerosteuer.de>","to":[to],"subject":"⚠️ AeroX Board-Scraper still","text":body}).encode()
req=urllib.request.Request("https://api.resend.com/emails",data=data,headers={"Authorization":"Bearer "+rk,"Content-Type":"application/json","User-Agent":"AeroX-Monitor/1.0"})
try: urllib.request.urlopen(req,timeout=15)
except Exception: pass
PY3
    printf '%s\n%s\n' "$bsig" "$now" > "$BOARD_STATE"
  fi
else
  rm -f "$BOARD_STATE"
fi
