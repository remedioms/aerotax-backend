-- FlightOps Rotations-Save als Compare-and-Swap (Refresher-Umbau 2026-07-27,
-- nach Grant-Burn #4: 254/571 Grants tot, weil ein unbestätigter Save den
-- konsumierten RT stehen ließ und ein blinder Guard-Fallback trotzdem
-- refreshte).
--
-- Semantik: der frisch rotierte Token-Stand wird NUR geschrieben, wenn der
-- durable Stand noch am KONSUMIERTEN Refresh-Token hängt (oder idempotent
-- schon am neuen). Hängt er an einem FREMDEN RT (Re-Login während der
-- Rotation / Alt-Code-Container im Deploy-Übergang), kommt 'superseded'
-- zurück und NICHTS wird überschrieben — der neuere Stand gewinnt immer.
-- Row-Lock (FOR UPDATE) macht Check+Write atomar.
--
-- Returns: 'saved' | 'superseded' | 'no_row'.
-- Python-Fallback ohne dieses RPC: bestätigter Merge + Supersede-Readback
-- (_tokens_save_rotated in blueprints/lh_flightops.py) — funktional gleich,
-- nur ohne DB-Atomarität. Migration daher unabhängig vom Deploy applizierbar.
create or replace function public.flightops_save_rotated(
    p_token text,
    p_consumed text,
    p_tokens jsonb
) returns text
language plpgsql
as $$
declare
    cur text;
begin
    select metadata->'flightops_tokens'->>'refresh' into cur
      from public.user_profiles
     where token = p_token
       for update;
    if not found then
        return 'no_row';
    end if;
    if cur is not null and cur <> coalesce(p_consumed, '')
                       and cur <> (p_tokens->>'refresh') then
        return 'superseded';
    end if;
    update public.user_profiles
       set metadata = jsonb_set(coalesce(metadata, '{}'::jsonb),
                                '{flightops_tokens}', p_tokens, true),
           updated_at = now()
     where token = p_token;
    return 'saved';
end;
$$;
