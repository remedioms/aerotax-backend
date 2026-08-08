#!/usr/bin/env python3
# AeroX Wochen-Analytics (läuft IM aerotax-backend-Container, druckt Report auf stdout).
# Aufruf: docker exec -i aerotax-backend python3 - < analytics_report.py
# Datenquellen + Interpretation: siehe Memory aerox-usage-metrics-method
#  - ax_app_opens = ehrliche Foreground-Opens (iOS-Ping ab Build >221)
#  - roster_snapshots.updated_at = Gerät synct (FG oder BGAppRefresh)
#  - user_profiles.updated_at NICHT als DAU nutzen (Freunde-View-Bump)
import os, json, urllib.request, time, datetime
from collections import Counter

U = os.environ["SUPABASE_URL"]; K = os.environ["SUPABASE_SERVICE_KEY"]

def _req(path, extra=None):
    h = {"apikey": K, "Authorization": "Bearer " + K}
    h.update(extra or {})
    return urllib.request.Request(U + path, headers=h)

def count(path):
    r = _req(path, {"Prefer": "count=exact", "Range": "0-0"})
    with urllib.request.urlopen(r, timeout=30) as resp:
        return int(resp.headers.get("Content-Range", "0/0").split("/")[-1])

def rows(path, rng="0-9999"):
    with urllib.request.urlopen(_req(path, {"Range": rng}), timeout=30) as resp:
        return json.load(resp)

now = datetime.datetime.now(datetime.timezone.utc)
ep = time.time()
def iso(d): return (now - datetime.timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")
def day(d): return (now - datetime.timedelta(days=d)).strftime("%Y-%m-%d")

L = []
p = L.append
p(f"AeroX Wochen-Analytics — Stand {now.strftime('%d.%m.%Y %H:%M')} UTC")
p("=" * 60)

total = count("/rest/v1/user_profiles?select=token")
neu7 = count(f"/rest/v1/user_profiles?select=token&created_at=gte.{iso(7)}")
p(f"\nREGISTRIERT: {total}  (+{neu7} in 7 Tagen)")
p("Signups pro Tag:")
for d in range(6, -1, -1):
    n = count(f"/rest/v1/user_profiles?select=token&created_at=gte.{iso(d+1)}&created_at=lt.{iso(d)}")
    p(f"  {(now - datetime.timedelta(days=d+1)).strftime('%a %d.%m')}: {n}")

# Echte Opens (ax_app_opens) — leer bis der Build mit dem Ping verteilt ist.
p("\nECHTE APP-OPENS (ax_app_opens, Foreground-Ping):")
try:
    dau = count(f"/rest/v1/ax_app_opens?select=token&day=eq.{day(1)}")
    wau_rows = rows(f"/rest/v1/ax_app_opens?select=token&day=gte.{day(7)}")
    wau = len(set(x["token"] for x in wau_rows))
    if wau == 0:
        p("  noch keine Daten — Ping shippt mit dem nächsten iOS-Build (>221)")
    else:
        p(f"  DAU gestern: {dau}  |  WAU: {wau}  ({100*wau//max(total,1)}% der Registrierten)")
        for d in range(6, -1, -1):
            n = count(f"/rest/v1/ax_app_opens?select=token&day=eq.{day(d)}")
            p(f"  {(now - datetime.timedelta(days=d)).strftime('%a %d.%m')}: {n} User")
except Exception as e:
    p(f"  Abfrage-Fehler: {e}")

p("\nPROXY-TIERS (bis ax_app_opens greift):")
p(f"  Gerät synct (roster_snapshots, FG+BG) 24h/7d: "
  f"{count(f'/rest/v1/roster_snapshots?select=token&updated_at=gte.{iso(1)}')}"
  f" / {count(f'/rest/v1/roster_snapshots?select=token&updated_at=gte.{iso(7)}')}")
p(f"  Push-Installationen aktiv: {count('/rest/v1/push_installations?select=id&active=eq.true')}"
  f"  (tombstoned gesamt: {count('/rest/v1/push_installations?select=id&tombstoned_at=not.is.null')})")

d7 = ep - 7 * 86400
human = set()
for path, f in [
        (f"/rest/v1/dm_lastseen?select=user_token&last_seen_ts=gte.{d7}", "user_token"),
        (f"/rest/v1/dm_messages?select=author_token&ts=gte.{d7}", "author_token"),
        (f"/rest/v1/forum_threads?select=author_token&ts=gte.{d7}", "author_token"),
        (f"/rest/v1/forum_replies?select=author_token&ts=gte.{d7}", "author_token"),
        (f"/rest/v1/wall_posts?select=author_token&ts=gte.{d7}", "author_token"),
        (f"/rest/v1/forum_likes?select=user_token&created_at=gte.{iso(7)}", "user_token"),
        (f"/rest/v1/user_flight_ops?select=token&updated_at=gte.{iso(7)}", "token"),
        (f"/rest/v1/user_friends?select=owner_token&created_at=gte.{iso(7)}", "owner_token")]:
    try:
        human |= set(x[f] for x in rows(path) if x.get(f))
    except Exception:
        pass
p(f"  Sicher-menschliche Aktionen (Chat/Posts/Likes/Logbuch/Anfragen) 7d: {len(human)}")

p("\nCOMMUNITY (7 Tage):")
p(f"  Forum: {count(f'/rest/v1/forum_threads?select=id&ts=gte.{d7}')} Threads, "
  f"{count(f'/rest/v1/forum_replies?select=id&ts=gte.{d7}')} Replies")
p(f"  Wall-Posts: {count(f'/rest/v1/wall_posts?select=id&ts=gte.{d7}')}  |  "
  f"DMs: {count(f'/rest/v1/dm_messages?select=id&ts=gte.{d7}')}")
p(f"  Freundschaften gesamt (accepted): {count('/rest/v1/user_friends?select=owner_token&status=eq.accepted')}")

ohne_airline = count("/rest/v1/user_profiles?select=token&airline=is.null")
p(f"\nONBOARDING-LÜCKE: {ohne_airline} Profile ohne Airline")

print("\n".join(L))
