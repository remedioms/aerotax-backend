-- Event-trigger functions never need to be callable through PostgREST. Keep
-- execution limited to database owners/internal roles.
revoke all on function public.aerox_rls_auto_enable() from public;
revoke all on function public.aerox_rls_auto_enable() from anon, authenticated;

notify pgrst, 'reload schema';
