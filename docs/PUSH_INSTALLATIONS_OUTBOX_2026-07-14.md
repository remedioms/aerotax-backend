# APNs installations and durable outbox — production runbook

## What changes

`push_installations` is the delivery source of truth for native APNs devices.
The unique identity is `(apns_token, bundle_id, environment)`. Registering that
identity under a new account atomically rebinds it and clears any legacy row
that could otherwise resurrect the previous account (both APNs and Expo
compatibility tokens). When a stable `device_id` is supplied, older endpoints
for that physical app installation are tombstoned on token/build rotation. A
user can own multiple active installations; delivery fans out once to each
unique endpoint.

Logout writes a durable tombstone. New clients can tombstone one installation
by `installation_id` (and may additionally send APNs token/topic/environment).
The old `{token}` payload remains accepted and intentionally tombstones all
installations for that account, matching its former one-row semantics.

`push_outbox` stores notification intent before APNs network I/O. Enqueue is an
atomic idempotent RPC. Workers atomically claim rows with `FOR UPDATE SKIP
LOCKED`, reclaim abandoned processing locks, retry with bounded exponential
backoff, and dead-letter after eight attempts. A process-wide HTTP/2 client
reuses APNs connections. Delivered payload/user fields are erased immediately;
delivered rows expire after 24 hours and dead rows after seven days.

The enqueue and a domain mutation are not yet one PostgreSQL transaction when
the domain write is performed by a separate legacy API call. Every current
producer persists notification intent after its successful mutation and before
its HTTP response; chat persists a parent fanout job first, whose worker creates
idempotent per-recipient child rows. A store outage uses the metered legacy
direct-send fallback. Future high-value producers should move mutation +
enqueue into one domain RPC.

## Production migration order

1. Record counts from `user_push_tokens` and take the normal Supabase backup.
2. Apply `supabase_migrations/20260714_push_installations_outbox.sql` while the
   old backend is still running. It is additive and backfills APNs rows.
3. Verify privileges before deploying code:

   ```sql
   select
     has_function_privilege('anon',
       'public.register_push_installation(text,text,text,text,text,text,jsonb,text)',
       'execute') as anon_must_be_false,
     has_function_privilege('authenticated',
       'public.enqueue_push_outbox(text,text,jsonb)',
       'execute') as authenticated_must_be_false,
     has_function_privilege('service_role',
       'public.claim_push_outbox(text,integer,integer)',
       'execute') as service_role_must_be_true,
     has_table_privilege('anon', 'public.push_outbox_metrics', 'select')
       as anon_metrics_must_be_false;
   ```

4. Confirm Hetzner does not set the emergency opt-out
   `AEROX_REQUIRE_TOKEN_BINDING=0`. Missing/`1` means enforced. If production
   currently contains `0`, remove it only as an explicit compose/env change,
   then exercise login/profile/friends/push smoke tests before continuing.
5. Deploy the exact verified backend image. Do not retire `user_push_tokens` or
   either legacy registration route in this release.
6. Confirm `/api/health` reports `token_binding_enforced: true`; watch the
   in-process `push_outbox` counters and the durable view:

   ```sql
   select * from public.push_outbox_metrics order by status;
   select count(*) from public.push_installations where active;
   ```

7. Schedule `select public.cleanup_push_outbox();` daily with the existing
   trusted scheduler/service role.

## Implemented iOS client contract (pending TestFlight verification)

The matching iOS changes are now integrated in the shared worktree:

1. Generate a random installation ID once and retain it in Keychain across
   account logout. Send it as `device_id` during registration. Do not derive it
   from a user credential.
2. Persist the server-returned `installation_id` and installation-scoped
   `unregister_token` together with APNs token, bundle ID and environment. The
   capability is rotated on registration and is not an account credential.
3. On logout, first persist a device-global pending-unregister record, then send:

   ```json
   {
     "installation_id": "<server UUID>",
     "unregister_token": "<installation-scoped capability>",
     "apns_token": "<current device token>",
     "bundle_id": "aerotax.AeroTax",
     "apns_env": "prod"
   }
   ```

4. UI logout may complete immediately, but the pending tombstone must survive
   the account credential wipe and offline app termination. Retry it using the
   installation capability—do not retain the old account Bearer—until HTTP 200.
   The capability RPC is idempotent, so a lost first 200 can be retried safely.
   A 503 plus `Retry-After` is retryable and must never be interpreted as an
   invalid capability or account.
5. On any subsequent login, register the current APNs token before considering
   push bootstrap complete. This authoritative account rebind also heals a
   previous offline logout whose request never reached the server.
6. Re-register on APNs token rotation, bundle/environment change and reinstall.

Older installed builds that still use `{token}` unregister remain compatible:
they safely prevent cross-account PII but disable all devices for that account.
The integrated client uses the installation capability flow; it still requires
the planned TestFlight device matrix below before release.

An empty durable installation result is authoritative. The backend never
"heals" it from the one-row legacy token because that could resurrect a
logged-out or account-rebound shared device. A durable-registry outage also
fails closed and leaves the outbox retryable instead of sending to disk cache.

## External/device verification

- Two iPhones on one account both receive one DM push.
- Logging out iPhone A with installation identity leaves iPhone B active.
- Logging into account B on iPhone A rebinds the installation; account A can no
  longer deliver to A even if its offline logout never arrived.
- Debug/sandbox and TestFlight/prod installations coexist without environment
  probing or duplicate delivery.
- APNs `410 Unregistered` tombstones only the rejected installation.
- Kill/restart the backend after enqueue and before send; the new process claims
  and delivers the persisted row once.
- Force APNs 429/transport failure and observe retry/backoff, then dead-letter at
  attempt eight. Confirm cleanup retention and no raw credentials in logs.
