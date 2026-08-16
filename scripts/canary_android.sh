#!/usr/bin/env bash
# Android production canary. GET-only: it never signs up, changes state, or
# includes credentials in URLs or output.
#
# Optional environment:
#   AEROX_BACKEND_URL=https://api.aerosteuer.de
#   AEROX_ANDROID_CANARY_TOKEN=<purpose-made canary account bearer token>
#   AEROX_CANARY_CONNECT_TIMEOUT=5
#   AEROX_CANARY_MAX_TIME=15
set -u
set -o pipefail

BASE="${AEROX_BACKEND_URL:-https://api.aerosteuer.de}"
BASE="${BASE%/}"
CONNECT_TIMEOUT="${AEROX_CANARY_CONNECT_TIMEOUT:-5}"
MAX_TIME="${AEROX_CANARY_MAX_TIME:-15}"
TOKEN="${AEROX_ANDROID_CANARY_TOKEN:-}"
FAILURES=0
TMPDIR_CANARY=""

note() { printf '%s\n' "$*"; }
fail() { note "FAIL  $*"; FAILURES=$((FAILURES + 1)); }
cleanup() { [ -n "$TMPDIR_CANARY" ] && rm -rf "$TMPDIR_CANARY"; }
trap cleanup EXIT

case "$BASE" in
  https://*) ;;
  *) note "FAIL  AEROX_BACKEND_URL must be an HTTPS origin"; exit 2 ;;
esac
case "$CONNECT_TIMEOUT:$MAX_TIME" in
  *[!0-9:]*|:*) note "FAIL  canary timeouts must be positive integer seconds"; exit 2 ;;
esac
if [ "$CONNECT_TIMEOUT" -le 0 ] || [ "$MAX_TIME" -le 0 ]; then
  note 'FAIL  canary timeouts must be positive integer seconds'
  exit 2
fi

TMPDIR_CANARY="$(mktemp -d "${TMPDIR:-/tmp}/aerox-android-canary.XXXXXX")" || {
  note "FAIL  could not create temporary directory"; exit 2;
}

request_status() {
  # Args: path, output filename, optional bearer flag. The token is supplied
  # only as an HTTP header and is never printed, interpolated into a URL, or
  # written to the result body.
  local path="$1" output="$2" use_token="${3:-0}" status
  local -a args=(--silent --show-error --output "$output" --write-out '%{http_code}'
                 --connect-timeout "$CONNECT_TIMEOUT" --max-time "$MAX_TIME"
                 --header 'Accept: application/json' --request GET)
  if [ "$use_token" = "1" ]; then
    args+=(--header "Authorization: Bearer $TOKEN")
  fi
  status="$(curl "${args[@]}" "$BASE$path" 2>/dev/null)" || status="000"
  printf '%s' "$status"
}

is_installed_status() {
  # Unauthenticated /api/me routes normally return 401. A 405 is allowed for
  # method-specific aliases, and 2xx is allowed for intentionally public reads.
  case "$1" in 2??|401|405) return 0 ;; *) return 1 ;; esac
}

validate_json_shape() {
  # Never print response bodies: parse locally and emit only a generic reason.
  local kind="$1" body="$2"
  python3 - "$kind" "$body" <<'PY'
import json
import sys

kind, path = sys.argv[1:3]
try:
    with open(path, encoding='utf-8') as f:
        value = json.load(f)
except Exception:
    raise SystemExit(1)

if not isinstance(value, dict):
    raise SystemExit(1)
if kind == 'profile':
    # The Android owner response must carry the profile envelope; no values,
    # identifiers, or user attributes are asserted or emitted here.
    valid = isinstance(value.get('profile'), dict)
elif kind == 'entitlement':
    valid = isinstance(value.get('ok'), bool) and isinstance(value.get('pro_required'), bool)
else:
    valid = False
raise SystemExit(0 if valid else 1)
PY
}

note "Android production canary: $BASE"

health_body="$TMPDIR_CANARY/health.json"
health_status="$(request_status '/api/health' "$health_body")"
if [ "$health_status" = "200" ]; then
  note 'OK    GET /api/health -> 200'
else
  fail "GET /api/health -> $health_status"
fi

# Representative Android /api/me families. This detects a missing deployment
# without requiring an account or exposing account credentials.
ROUTES=(
  '/api/me/profile'
  '/api/me/entitlement'
  '/api/me/friends'
  '/api/me/roster'
  '/api/me/crew-chat/inbox'
  '/api/me/forum/threads'
  '/api/me/push/prefs'
  '/api/me/ax/daily-briefing'
)
for route in "${ROUTES[@]}"; do
  body="$TMPDIR_CANARY/route-${RANDOM}.json"
  status="$(request_status "$route" "$body")"
  if is_installed_status "$status"; then
    note "OK    GET $route -> $status"
  else
    # 404 and every 5xx deliberately fail, as do redirects/unexpected statuses.
    fail "GET $route -> $status (expected 2xx, 401, or 405)"
  fi
done

if [ -n "$TOKEN" ]; then
  note 'Authenticated Android contract checks enabled.'
  for kind in profile entitlement; do
    route="/api/me/$kind"
    body="$TMPDIR_CANARY/auth-${kind}.json"
    status="$(request_status "$route" "$body" 1)"
    if [ "$status" != "${status#2}" ] && validate_json_shape "$kind" "$body"; then
      note "OK    authenticated GET $route -> $status (minimal JSON contract)"
    else
      fail "authenticated GET $route -> $status (expected 2xx + minimal JSON contract)"
    fi
  done
else
  note 'SKIP  authenticated profile/entitlement contract (AEROX_ANDROID_CANARY_TOKEN not set)'
fi

if [ "$FAILURES" -eq 0 ]; then
  note 'ANDROID CANARY OK'
  exit 0
fi
note "ANDROID CANARY FAILED ($FAILURES check(s))"
exit 1
