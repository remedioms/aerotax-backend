# Monitoring & Canary

## What exists (created 2026-07-02, project `aerotax-prod`)

**Log-based metrics** (`gcloud logging metrics list`), all scoped to
`resource.labels.service_name="aerotax-backend"`:

| Metric | Filter (textPayload / httpRequest) |
|---|---|
| `apns_send_failed` | `textPayload=~"\[APNS\] send failed"` |
| `auth_store_unavailable` | `textPayload=~"sb_load_fail\|auth store unavailable"` |
| `auth_gate_error` | `textPayload=~"auth_gate_error\|UNEXPECTED gate error"` |
| `server_5xx` | `httpRequest.status>=500` |

**Alert policies** (`gcloud alpha monitoring policies list`), each JSON in this
directory is the source of truth:

| Policy displayName | Threshold | File |
|---|---|---|
| AeroX: APNs send failed (>0 in 5min) | >0 / 5min | `policy_apns_send_failed.json` |
| AeroX: auth gate error (>0 in 5min) | >0 / 5min | `policy_auth_gate_error.json` |
| AeroX: auth store unavailable wave (>5 in 5min) | >5 / 5min | `policy_auth_store_unavailable.json` |
| AeroX: HTTP 5xx wave (>10 in 5min) | >10 / 5min | `policy_server_5xx.json` |

**Notification channel**: email to miguel.schumann@icloud.com —
`projects/aerotax-prod/notificationChannels/18344722479875947413`
(referenced by all four policies).

## Re-applying / adding policies

```bash
./scripts/setup_monitoring.sh
```

Creates any missing log metric + any `policy_*.json` in this directory whose
`displayName` doesn't exist yet, then lists everything for verification.
To CHANGE an existing policy, edit its JSON and run:

```bash
gcloud alpha monitoring policies update <policy-name> --policy-from-file=scripts/monitoring/policy_X.json
```

## Post-deploy canary

```bash
APNS_P8=~/Downloads/AuthKey_5275MV6S9S.p8 APNS_KEY_ID=5275MV6S9S ./scripts/canary.sh
```

Checks (against prod, override with `AEROX_BACKEND_URL`):
1. `/api/health` → 200
2. iOS contract suite `tests/aerox/test_contract_ios_backend.py` (live, ~50 calls)
3. APNs key auth: ES256 JWT from the local .p8 + fake device token →
   expects `400 BadDeviceToken` (healthy); `403` = key broken → exit 1

Exit 0 = green, 1 = at least one step failed. Env vars documented at the top
of `scripts/canary.sh`. The .p8 key stays local — never committed.
