-- Forward-only repair for projects that applied earlier AeroX migrations before
-- the server-only hardening was added.  This file deliberately does not create
-- public client policies: every object below is accessed by the Flask backend's
-- service-role connection, never directly from iOS/Flutter.
--
-- It is safe to apply after any partial historical schema because each object
-- is looked up first.  Do not replace this with a dashboard paste: retain the
-- migration history and validate the resulting catalog before a release.

DO $$
DECLARE
  table_name text;
  backend_only_tables constant text[] := ARRAY[
    -- Legacy dashboard-bootstrap tables; all calls are backend service role.
    'questions', 'answers', 'upvotes', 'sessions', 'audit_logs', 'pdfs',
    'uploaded_files',
    -- Family and operational records contain account-token-like identifiers.
    'family_shares', 'family_requests', 'feed_statuses',
    'flight_observations',
    -- Public landing-page guest records are backend-mediated only.
    'layover_guest_invites', 'layover_guest_sessions'
  ];
BEGIN
  FOREACH table_name IN ARRAY backend_only_tables LOOP
    IF to_regclass(format('public.%I', table_name)) IS NOT NULL THEN
      EXECUTE format(
        'ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', table_name
      );

      -- PUBLIC is always present; the Supabase Data API roles are guarded so
      -- this remains idempotent even in a stripped-down local PostgreSQL role set.
      EXECUTE format('REVOKE ALL ON TABLE public.%I FROM PUBLIC', table_name);
      IF to_regrole('anon') IS NOT NULL THEN
        EXECUTE format('REVOKE ALL ON TABLE public.%I FROM anon', table_name);
      END IF;
      IF to_regrole('authenticated') IS NOT NULL THEN
        EXECUTE format(
          'REVOKE ALL ON TABLE public.%I FROM authenticated', table_name
        );
      END IF;
    END IF;
  END LOOP;
END;
$$;

-- SECURITY DEFINER functions receive EXECUTE via PUBLIC when created unless
-- it is revoked.  Keep the budget RPC available only to the backend role.
DO $$
BEGIN
  IF to_regprocedure('public.ax_budget_increment(text,integer)') IS NOT NULL THEN
    REVOKE ALL ON FUNCTION public.ax_budget_increment(text, integer) FROM PUBLIC;
    IF to_regrole('anon') IS NOT NULL THEN
      REVOKE ALL ON FUNCTION public.ax_budget_increment(text, integer) FROM anon;
    END IF;
    IF to_regrole('authenticated') IS NOT NULL THEN
      REVOKE ALL ON FUNCTION public.ax_budget_increment(text, integer)
        FROM authenticated;
    END IF;
    IF to_regrole('service_role') IS NOT NULL THEN
      GRANT EXECUTE ON FUNCTION public.ax_budget_increment(text, integer)
        TO service_role;
    END IF;
  END IF;

  -- This is a trigger helper, not an API.  The trigger executes it as its
  -- owner, so no caller grant is needed after the public revoke.
  IF to_regprocedure('public.enforce_layover_guest_limit()') IS NOT NULL THEN
    REVOKE ALL ON FUNCTION public.enforce_layover_guest_limit() FROM PUBLIC;
    IF to_regrole('anon') IS NOT NULL THEN
      REVOKE ALL ON FUNCTION public.enforce_layover_guest_limit() FROM anon;
    END IF;
    IF to_regrole('authenticated') IS NOT NULL THEN
      REVOKE ALL ON FUNCTION public.enforce_layover_guest_limit()
        FROM authenticated;
    END IF;
  END IF;
END;
$$;
