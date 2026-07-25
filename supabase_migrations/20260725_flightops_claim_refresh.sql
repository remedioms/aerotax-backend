-- FlightOps Cross-Container-Refresh-Claim (Grant-Burn #2, 2026-07-25 02:54Z):
-- LH rotiert den Refresh-Token bei jedem Refresh und revoziert bei REUSE die
-- ganze Token-Familie (Reuse-Detection, undokumentiert — live beobachtet).
-- Der Python-Soft-Guard (foguard-bf86c33) verkleinert das Race-Fenster nur
-- auf die SB-Write-Latenz (~100ms). Dieses RPC macht das Claim ATOMAR: genau
-- EIN Prozess bekommt pro Refresh-Token den Zuschlag (Row-Lock im UPDATE),
-- alle anderen sehen false und übernehmen später die rotierten Tokens des
-- Gewinners, statt den RT doppelt bei LH zu verheizen.
-- Returns true ⇔ Claim gewonnen (Guard {rt8, ts} server-seitig geschrieben).
create or replace function public.flightops_claim_refresh(
    p_token text,
    p_refresh text,
    p_rt8 text,
    p_ttl numeric default 15
) returns boolean
language plpgsql
as $$
declare
    n int;
begin
    update public.user_profiles
    set metadata = jsonb_set(
            coalesce(metadata, '{}'::jsonb),
            '{flightops_tokens,refresh_guard}',
            jsonb_build_object('rt8', p_rt8,
                               'ts', extract(epoch from now())),
            true),
        updated_at = now()
    where token = p_token
      and metadata->'flightops_tokens'->>'refresh' = p_refresh
      and not (
            coalesce(metadata->'flightops_tokens'->'refresh_guard'->>'rt8', '')
                = p_rt8
        and coalesce((metadata->'flightops_tokens'
                      ->'refresh_guard'->>'ts')::numeric, 0)
                > extract(epoch from now()) - p_ttl
      );
    get diagnostics n = row_count;
    return n > 0;
end;
$$;
