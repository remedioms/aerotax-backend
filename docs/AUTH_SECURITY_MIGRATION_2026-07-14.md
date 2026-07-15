# AeroX Auth security migration — 2026-07-14

This document separates the safe, backward-compatible boundary now in place
from the credential migration that still requires a coordinated iOS rollout.

## Implemented now

### Auth-store tri-state

Token validation returns exactly one of:

- `VALID`: the credential is known. A previously verified credential remains
  valid from the stale in-process cache during a transient store outage.
- `INVALID`: the auth store was reachable and the credential is not present.
- `UNAVAILABLE`: the store cannot currently prove an unknown credential either
  valid or invalid.

`UNAVAILABLE` never enters an owner route. The global auth gate and
`_token_auth_required` return `503 auth_store_unavailable` with `Retry-After: 5`.
This avoids both the old forged-token fail-open and an incorrect session-invalid
`401` that could log a legitimate newly-created user out.

### Method/route-exact owner binding

Cross-user exemptions are deny-by-default. Only these exact reads are exempt:

- `GET|HEAD /api/user/profile/<AT-token>` (public profile projection)
- `GET|HEAD /api/user/friends/<AT-token>` (legacy discovery contract)

Writes and subroutes such as profile `PUT`, friends `add`/`remove`, and friends
`overlap` are owner-scoped. Missing or mismatched Bearers are rejected by
default. Only the explicit emergency value `AEROX_REQUIRE_TOKEN_BINDING=0`
temporarily restores legacy missing-header behavior; mismatches still reject.

### Observability redaction

`observability/redaction.py` is the single redaction boundary for request logs,
Werkzeug/Gunicorn/Python log records, structured JSON logs, and Sentry error and
transaction payloads. It removes Bearer values, sensitive query parameters,
secret-like fields, and legacy `AT-...` path credentials. Application logs must
never manually reproduce `request.path` or `request.url` without this helper.

## Still required: coordinated credential migration

The following is intentionally not changed behind old clients:

1. Add a server-side `auth_sessions` table with a random 256-bit session ID,
   SHA-256/HMAC-peppered token hash, user ID, installation ID, creation/expiry,
   last-used, revoked-at, and rotation-family fields. Never store the raw token.
2. Issue short-lived access tokens and rotating refresh tokens. Reuse of a
   rotated refresh token must revoke its entire family.
3. Accept `Authorization: Bearer` only for all owner-scoped v2 routes. The user
   identity must come from the verified principal, never a body/path token.
4. Ship an additive iOS version that uses v2 header-only routes, safely refreshes
   once under concurrency, and treats only explicit invalid/revoked results as
   logout. `503` and timeouts remain retryable.
5. Measure adoption, then enable strict binding for v1 owner routes. Maintain a
   dated compatibility window; after it ends, retire URL-token routes and revoke
   long-lived legacy credentials.
6. Configure edge/proxy access logs to omit raw query strings and redact legacy
   path segments during the compatibility window. App-level redaction cannot
   rewrite logs produced before the request reaches Flask.

This migration needs a schema change, staged client adoption, revocation and
rollback runbooks, and production telemetry. It must not be silently bundled
into an unrelated backend deploy.
