# Runbook — Supabase Auth + User-Data Migration

P0-Fix der ephemeral-disk-Datenverlust-Bug auf Cloud Run. Vor diesem Deploy:
Cloud-Run wipte Accounts/Profile/Friends/Push-Tokens bei jedem Redeploy + bei
Auto-Scale auf zweite Instance verlor User seinen Login zwischen Requests.

## Reihenfolge

1. SQL-Migration in Supabase ausführen (vor Deploy)
2. Code Deploy
3. Verify

## 1. Supabase SQL ausführen

In Supabase Dashboard → SQL Editor → "New query":

```bash
# auth_users
cat /Users/miguelschumann/Desktop/aerotax-backend/supabase_migrations/20260531_auth_users.sql

# user_profiles + user_friends + user_push_tokens
cat /Users/miguelschumann/Desktop/aerotax-backend/supabase_migrations/20260531_user_data.sql
```

Copy/Paste beide Files nacheinander, "Run" drücken. Erwartung: "Success. No rows returned." pro Statement.

Verifikation:

```sql
select table_name from information_schema.tables
where table_schema = 'public'
  and table_name in ('auth_users', 'user_profiles', 'user_friends', 'user_push_tokens')
order by table_name;
```

Sollte 4 Zeilen liefern.

## 2. Deploy

```bash
git push origin main
gcloud run deploy aerotax-backend --source /Users/miguelschumann/Desktop/aerotax-backend --region=europe-west3
```

(Erwartung: ~4-5 Min Build + Deploy)

## 3. Verify nach Deploy

```bash
URL="https://aerotax-backend-kkyrr7h7xa-ey.a.run.app"

# Signup, dann gleich nochmal (sollte schon existieren = email_already_exists)
EMAIL="migration-test-$(date +%s)@aerosteuer.de"
curl -s -X POST "$URL/api/auth/signup" -H "Content-Type: application/json" \
  -d "{\"email\":\"$EMAIL\",\"password\":\"TestPass2026!\"}"
echo ""

# Manuell eine neue Cloud-Run-Revision triggern um zu beweisen dass der Account überlebt
gcloud run deploy aerotax-backend --source . --region=europe-west3

# Nach Deploy: Login sollte funktionieren (vorher: invalid_credentials weil disk wiped)
curl -s -X POST "$URL/api/auth/login" -H "Content-Type: application/json" \
  -d "{\"email\":\"$EMAIL\",\"password\":\"TestPass2026!\"}"
# Erwartung: {"ok":true,"token":"AT-..."} — vorher 401
```

Auch verifizieren in Supabase Dashboard → Table Editor → `auth_users`:
sollte mind. 1 Zeile mit der Test-Email zeigen.

## Rollback (falls etwas schiefgeht)

Code zurück auf vorherigen Commit:

```bash
git log --oneline -5  # vorigen Commit-Hash finden
gcloud run deploy aerotax-backend --source . --region=europe-west3 --revision-suffix=rollback
```

Die SQL-Tabellen müssen NICHT gelöscht werden — sie tun nichts solange Code
sie nicht beschreibt. Wenn aber Code auf alte Disk-Version zurückgeht, bleibt
SB-Daten unverändert für späteren Re-Migrations-Versuch.
