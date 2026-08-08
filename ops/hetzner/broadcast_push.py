import os, json, time, urllib.request
import app as A
TITLE = "AeroX-Update"
BODY = ("Wir haben einige Fehler behoben und neue Funktionen ergänzt. "
        "Bitte aktualisiert AeroX auf die neueste Version. "
        "Viel Spaß mit dem Update – und vielen Dank für euer Feedback! \U0001f499")
U = os.environ["SUPABASE_URL"]; K = os.environ["SUPABASE_SERVICE_KEY"]
def page(off):
    req = urllib.request.Request(
        U + f"/rest/v1/user_push_tokens?select=user_token,apns_token,metadata&limit=1000&offset={off}",
        headers={"apikey": K, "Authorization": "Bearer " + K})
    return json.load(urllib.request.urlopen(req, timeout=60))
rows = []; off = 0
while True:
    b = page(off)
    if not b: break
    rows += b; off += len(b)
    if len(b) < 1000: break
def env_of(md):
    if not isinstance(md, dict): return "unknown"
    v = md.get("apns_env") or md.get("environment")
    return str(v).lower() if v else "unknown"
seen = {}
for r in rows:
    if not (r.get("apns_token") or "").strip(): continue
    if env_of(r.get("metadata")) == "sandbox": continue
    seen[r["user_token"]] = True
targets = list(seen)
print(f"[BROADCAST] rows={len(rows)} production_targets={len(targets)}", flush=True)
try: A.app.app_context().push()
except Exception: pass
ok = deliv = supp = fail = 0
for i, ut in enumerate(targets):
    try:
        d = A._send_push_notification(ut, TITLE, BODY, data={"type": "app_update"}, _return_detail=True)
        if isinstance(d, dict):
            if d.get("ok"): ok += 1; deliv += int(d.get("delivered") or 0)
            elif "pref" in str(d.get("reason") or "") or "suppress" in str(d.get("reason") or ""): supp += 1
            else: fail += 1
        elif d: ok += 1
        else: fail += 1
    except Exception:
        fail += 1
    if (i + 1) % 40 == 0: time.sleep(0.8)
print(f"[BROADCAST] DONE 07:00 CEST targets={len(targets)} ok={ok} delivered={deliv} suppressed={supp} fail={fail}", flush=True)
