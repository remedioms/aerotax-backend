#!/usr/bin/env python3
# AeroX Signup-Tages-Digest (läuft IM aerotax-backend-Container).
# Ersetzt seit 2026-07-25 die Echtzeit-Mail pro Signup (_notify_owner_new_signup,
# aus app.py entfernt — Inbox lief bei 60-400 Signups/Tag voll).
# Ausgabe: Zeile 1 = Mail-Subject, Rest = Body. Zählt den VOLLEN Vortag (UTC).
import os, json, urllib.request, datetime

U = os.environ["SUPABASE_URL"]; K = os.environ["SUPABASE_SERVICE_KEY"]

def _req(path, extra=None):
    h = {"apikey": K, "Authorization": "Bearer " + K}
    h.update(extra or {})
    return urllib.request.Request(U + path, headers=h)

def count(path):
    r = _req(path, {"Prefer": "count=exact", "Range": "0-0"})
    with urllib.request.urlopen(r, timeout=30) as resp:
        return int(resp.headers.get("Content-Range", "0/0").split("/")[-1])

def rows(path, rng="0-4999"):
    with urllib.request.urlopen(_req(path, {"Range": rng}), timeout=30) as resp:
        return json.load(resp)

heute = datetime.datetime.now(datetime.timezone.utc).date()
tag = heute - datetime.timedelta(days=1)
a, b = f"{tag}T00:00:00Z", f"{heute}T00:00:00Z"

neue = rows(f"/rest/v1/auth_users?select=token,apple_sub&created_at=gte.{a}&created_at=lt.{b}")
total = count("/rest/v1/auth_users?select=token")
apple = sum(1 for x in neue if x.get("apple_sub"))

print(f"AeroX: {len(neue)} neue Accounts am {tag.strftime('%d.%m.')} (gesamt {total})")
print(f"Neue Accounts am {tag.strftime('%d.%m.%Y')}: {len(neue)}")
print(f"  per Apple: {apple}  |  per E-Mail: {len(neue) - apple}")
print(f"Gesamt: {total} Accounts")

toks = [x["token"] for x in neue if x.get("token")]
if toks:
    airlines = {}
    for i in range(0, len(toks), 100):
        q = ",".join(f'"{t}"' for t in toks[i:i+100])
        for p in rows(f"/rest/v1/user_profiles?select=airline&token=in.({q})"):
            al = p.get("airline") or "(noch keine Airline)"
            airlines[al] = airlines.get(al, 0) + 1
    print("\nAirlines der Neuen:")
    for al, n in sorted(airlines.items(), key=lambda x: -x[1]):
        print(f"  {al}: {n}")
