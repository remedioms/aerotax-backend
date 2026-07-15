# Legal-consent ledger rollout

## What is canonical

The current legal wording remains the user-visible content in the iOS file
`AeroTax/Mehr/LegalViews.swift` (`Stand: Juni 2026`). No legal wording was
created or changed by this migration.

`data/legal_consent_manifest.json` is the versioned release manifest for those
bundled documents. The two document hashes were derived from the existing
`PrivacyPolicyView` and `TermsView` source blocks. To change either document:

1. legal review changes the bundled view;
2. bump its document version and the manifest version;
3. recompute the affected source-block SHA-256 and the canonical manifest hash;
4. update the same constants in `CurrentLegalConsentManifest` on iOS;
5. ship the client before (or together with) the matching backend manifest.

Recompute the current source-block hashes from the iOS repository:

```sh
sed -n '/struct PrivacyPolicyView/,/MARK: - AGB/p' AeroTax/Mehr/LegalViews.swift | shasum -a 256
sed -n '/struct TermsView/,/MARK: - Impressum/p' AeroTax/Mehr/LegalViews.swift | shasum -a 256
```

The manifest hash is SHA-256 of `documents` encoded as compact JSON with keys
sorted and array order preserved. The backend verifies it during module import;
the iOS unit test verifies its identical canonical representation.

## Deployment order

1. Apply `supabase_migrations/20260714_legal_consent_ledger.sql`.
2. Add this tuple to the existing blueprint-registration list in `app.py`:

   ```python
   ('blueprints.legal_consent_blueprint', 'legal_consent_bp'),
   ```

3. Run `make verify`, then deploy the backend.
4. Upload the matching iOS build to TestFlight.

Do not register the blueprint before the migration: reads and writes deliberately
fail with retryable 503 when the durable ledger is unavailable.

## Security and compatibility properties

- The HTTP API is header-only (`Authorization: Bearer`); credentials are not in
  these endpoint URLs.
- The ledger stores a stable UUID account ID, never the reusable Bearer token.
- The SECURITY DEFINER function is executable only by `service_role`; RLS and
  explicit grants deny `public`, `anon`, and `authenticated` direct access.
- Acceptance rows are immutable/idempotent per account and exact document
  version/hash. Database time is the audit timestamp.
- The app's local record is keyed by an opaque SHA-256 account reference. Offline
  acceptance is marked pending and retried only with that same account token.
- The old unversioned Boolean may migrate exactly once when no versioned record
  exists. Once a record exists, a different version or hash always re-arms the
  explicit consent screen.
- Against an old backend (404) or during an outage, the UI remains usable after
  an explicit local acceptance and synchronizes later.
