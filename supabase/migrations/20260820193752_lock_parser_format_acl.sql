-- Supabase projects created before the 2026 Data API default-grant change can
-- give service_role full table privileges automatically at CREATE TABLE time.
-- The learning runtime needs only SELECT on the state table; every mutation
-- must pass the validation/generation logic in ax_parser_learning_record().

revoke all on table public.ax_parser_formats from service_role;
revoke all on table public.ax_parser_format_evidence from service_role;

grant select on table public.ax_parser_formats to service_role;

revoke all on function public.ax_parser_learning_record(
    text, text, text, text, text, text, text
) from public, anon, authenticated;
grant execute on function public.ax_parser_learning_record(
    text, text, text, text, text, text, text
) to service_role;

notify pgrst, 'reload schema';
