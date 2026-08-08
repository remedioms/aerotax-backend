#!/bin/bash
# AeroX täglicher FR24-Credits-Report → Resend-Mail. Cron 1x/Tag (06:00 UTC).
# Liest ax_api_budget (fr24:YYYYMMDD = Credits/Tag) + aircraft_live (Free-Volumen).
cd /opt/aerox || exit 0
RK=$(grep "^RESEND_API_KEY=" env.list | cut -d= -f2-)
TO=$(grep "^SUPPORT_NOTIFY_EMAIL=" env.list | cut -d= -f2-)
SB=$(grep "^SUPABASE_URL=" env.list | cut -d= -f2-)
SK=$(grep "^SUPABASE_SERVICE_KEY=" env.list | cut -d= -f2-)
CAP=$(grep "^FR24_DAILY_CREDIT_CAP=" env.list | cut -d= -f2-)
[ -z "$RK" ] && exit 0
python3 - "$RK" "$TO" "$SB" "$SK" "${CAP:-8000}" <<'PY'
import sys, json, time, urllib.request
rk, to, sb, sk, cap = sys.argv[1:6]
cap = int(cap or 8000)
sb = sb.rstrip('/')
H = {'apikey': sk, 'Authorization': 'Bearer ' + sk, 'User-Agent': 'AeroX-Credits/1.0'}

def get_json(path):
    try:
        req = urllib.request.Request(sb + '/rest/v1/' + path, headers=H)
        return json.loads(urllib.request.urlopen(req, timeout=15).read())
    except Exception:
        return []

def count(table):
    try:
        h = dict(H); h['Prefer'] = 'count=exact'; h['Range'] = '0-0'
        req = urllib.request.Request(sb + '/rest/v1/' + table + '?select=flight', headers=h)
        cr = urllib.request.urlopen(req, timeout=15).headers.get('Content-Range', '')
        return int(cr.split('/')[-1]) if '/' in cr else None
    except Exception:
        return None

rows = get_json('ax_api_budget?month=like.fr24:*&select=month,n&order=month.desc&limit=8')
def key(off): return 'fr24:' + time.strftime('%Y%m%d', time.gmtime(time.time() - off * 86400))
by = {r['month']: r['n'] for r in rows}
yday = by.get(key(1), 0)          # gestern = vollständiger Tag = Schlagzeile
today = by.get(key(0), 0)         # heute bisher
live = count('aircraft_live')

lines = []
lines.append(f"FR24 Paid GESTERN: {yday} Credits  (~{yday//2} Lookups)  =  {round(100*yday/cap,1)}% vom Tages-Cap ({cap})")
lines.append(f"Heute bisher:      {today} Credits  (~{today//2} Lookups)")
if live is not None:
    lines.append(f"Gratis live jetzt: ~{live} Fluege im aircraft_live (FR24-gRPC-Scrape, 0 Credits)")
lines.append("")
lines.append("Letzte Tage (FR24-Paid-Credits):")
for r in rows:
    m = r['month'].replace('fr24:', '')
    lines.append(f"  {m[:4]}-{m[4:6]}-{m[6:]}: {r['n']:>5}  (~{r['n']//2} Lookups)")
lines.append("")
lines.append("Free-first laeuft: Paid ist Notnagel fuer Luecken; jeder Credit wird gecached.")
body = "\n".join(lines)
subj = f"AeroX FR24-Credits: gestern {yday} ({round(100*yday/cap)}% Cap)"

data = json.dumps({"from": "AeroX Credits <support@aerosteuer.de>", "to": [to],
                   "subject": subj, "text": body}).encode()
try:
    req = urllib.request.Request("https://api.resend.com/emails", data=data,
        headers={"Authorization": "Bearer " + rk, "Content-Type": "application/json",
                 "User-Agent": "AeroX-Credits/1.0"})
    urllib.request.urlopen(req, timeout=15).read()
    print("sent:", subj)
except Exception as e:
    print("send-fail:", str(e)[:120])
PY
