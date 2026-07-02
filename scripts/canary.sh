#!/usr/bin/env bash
# canary.sh — post-deploy canary for the AeroX backend (Cloud Run: aerotax-backend).
#
# Run this after every deploy. It verifies, against PRODUCTION:
#   1. Health routes respond 200 (/api/health).
#   2. The iOS<->backend contract suite passes
#      (tests/aerox/test_contract_ios_backend.py, ~50 live HTTP calls,
#      creates + wipes one throwaway account).
#   3. APNs key auth is healthy: builds an ES256 JWT from a LOCAL .p8 key and
#      POSTs an alert with a deliberately fake device token to
#      api.push.apple.com. Expected: HTTP 400 {"reason":"BadDeviceToken"}
#      (= Apple accepted our JWT, key auth works). HTTP 403 = key/team/env
#      broken -> FAIL. Skipped with a warning if APNS_P8 is not set.
#
# Environment variables:
#   APNS_P8            (step 3) path to local AuthKey_<KEYID>.p8 file.
#                      NOT committed anywhere; typically ~/Downloads/AuthKey_5275MV6S9S.p8
#   APNS_KEY_ID        (step 3) APNs auth key ID (e.g. 5275MV6S9S).
#   APNS_TEAM_ID       (step 3) Apple team ID. Default: 8CLSCYBJ2M
#   APNS_TOPIC         (step 3) bundle id used as apns-topic. Default: aerotax.AeroTax
#   AEROX_BACKEND_URL  base URL for steps 1+2. Default: prod Cloud Run URL.
#
# Exit code: 0 = all green, 1 = at least one step failed.
#
# Example:
#   APNS_P8=~/Downloads/AuthKey_5275MV6S9S.p8 APNS_KEY_ID=5275MV6S9S ./scripts/canary.sh
set -u

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BASE="${AEROX_BACKEND_URL:-https://aerotax-backend-443401186607.europe-west3.run.app}"
APNS_TEAM_ID="${APNS_TEAM_ID:-8CLSCYBJ2M}"
APNS_TOPIC="${APNS_TOPIC:-aerotax.AeroTax}"

FAILURES=()
note() { printf '%s\n' "$*"; }

# ---------- step 1: health routes -------------------------------------------
note "== [1/3] health check: $BASE/api/health"
HTTP_CODE=$(curl -s -o /tmp/canary_health.json -w '%{http_code}' --max-time 20 "$BASE/api/health")
if [ "$HTTP_CODE" = "200" ] && grep -q '"ok"[[:space:]]*:[[:space:]]*true' /tmp/canary_health.json; then
  note "   OK  /api/health -> 200 $(cat /tmp/canary_health.json)"
else
  note "   FAIL /api/health -> $HTTP_CODE $(cat /tmp/canary_health.json 2>/dev/null)"
  FAILURES+=("health")
fi

# ---------- step 2: iOS contract suite (live, against $BASE) ----------------
note "== [2/3] contract suite: tests/aerox/test_contract_ios_backend.py (live, ~50 calls)"
if (cd "$REPO_DIR" && AEROX_LIVE_TESTS=1 AEROX_BACKEND_URL="$BASE" \
      python3 -m pytest tests/aerox/test_contract_ios_backend.py -q); then
  note "   OK  contract suite passed"
else
  note "   FAIL contract suite (see pytest output above)"
  FAILURES+=("contract")
fi

# ---------- step 3: APNs key-auth probe --------------------------------------
note "== [3/3] APNs key-auth probe (expect 400 BadDeviceToken)"
if [ -z "${APNS_P8:-}" ] || [ -z "${APNS_KEY_ID:-}" ]; then
  note "   SKIP APNS_P8 / APNS_KEY_ID not set — APNs probe not run"
else
  APNS_P8_EXPANDED="${APNS_P8/#\~/$HOME}"
  if [ ! -f "$APNS_P8_EXPANDED" ]; then
    note "   FAIL APNs key file not found: $APNS_P8_EXPANDED"
    FAILURES+=("apns")
  else
    APNS_JWT=$(APNS_P8="$APNS_P8_EXPANDED" APNS_KEY_ID="$APNS_KEY_ID" APNS_TEAM_ID="$APNS_TEAM_ID" python3 - <<'PYEOF'
import os, time, sys
try:
    import jwt  # PyJWT + cryptography
except ImportError:
    sys.exit("PyJWT not installed: pip3 install pyjwt cryptography")
with open(os.environ["APNS_P8"]) as f:
    key = f.read()
print(jwt.encode(
    {"iss": os.environ["APNS_TEAM_ID"], "iat": int(time.time())},
    key, algorithm="ES256",
    headers={"kid": os.environ["APNS_KEY_ID"]},
))
PYEOF
)
    if [ -z "$APNS_JWT" ]; then
      note "   FAIL could not build APNs JWT (PyJWT/cryptography missing or bad key file)"
      FAILURES+=("apns")
    else
      FAKE_TOKEN="0000000000000000000000000000000000000000000000000000000000000000"
      APNS_BODY=/tmp/canary_apns.json
      APNS_CODE=$(curl -s -o "$APNS_BODY" -w '%{http_code}' --http2 --max-time 20 \
        -H "authorization: bearer $APNS_JWT" \
        -H "apns-topic: $APNS_TOPIC" \
        -H "apns-push-type: alert" \
        -d '{"aps":{"alert":"canary"}}' \
        "https://api.push.apple.com/3/device/$FAKE_TOKEN")
      case "$APNS_CODE" in
        400)
          if grep -q 'BadDeviceToken' "$APNS_BODY"; then
            note "   OK  APNs -> 400 BadDeviceToken (key auth healthy)"
          else
            note "   FAIL APNs -> 400 but unexpected reason: $(cat "$APNS_BODY")"
            FAILURES+=("apns")
          fi
          ;;
        403)
          note "   FAIL APNs -> 403 $(cat "$APNS_BODY") — key/team/topic auth BROKEN"
          FAILURES+=("apns")
          ;;
        *)
          note "   FAIL APNs -> $APNS_CODE $(cat "$APNS_BODY" 2>/dev/null)"
          FAILURES+=("apns")
          ;;
      esac
    fi
  fi
fi

# ---------- summary -----------------------------------------------------------
if [ ${#FAILURES[@]} -eq 0 ]; then
  note "CANARY OK — health + contract + apns all green ($BASE)"
  exit 0
else
  note "CANARY FAILED — failing steps: ${FAILURES[*]} ($BASE)"
  exit 1
fi
