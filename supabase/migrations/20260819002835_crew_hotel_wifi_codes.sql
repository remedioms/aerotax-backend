-- Crowdsourced WLAN code attached to the existing airline-scoped crew-hotel
-- directory row. Clients never access this table directly: AeroX's backend
-- validates the app token, airline and active hotel before writing/serving it.
alter table public.crew_hotel_directory
    add column if not exists wifi_code text,
    add column if not exists wifi_updated_at timestamptz,
    add column if not exists wifi_updated_by text;

do $$
begin
    if not exists (
        select 1
        from pg_constraint
        where conrelid = 'public.crew_hotel_directory'::regclass
          and conname = 'crew_hotel_directory_wifi_code_length'
    ) then
        alter table public.crew_hotel_directory
            add constraint crew_hotel_directory_wifi_code_length
            check (wifi_code is null or char_length(wifi_code) between 1 and 128);
    end if;
end
$$;

comment on column public.crew_hotel_directory.wifi_code is
    'Current crew-shared hotel WLAN code, visible only through AeroX airline-scoped API.';
comment on column public.crew_hotel_directory.wifi_updated_by is
    'Non-reversible AeroX token hash of the last contributing crew member.';

-- Keep the current defense-in-depth contract explicit. The Flask backend uses
-- the service role; mobile clients receive neither anon nor authenticated table
-- grants and therefore cannot bypass the airline gate in the API.
alter table public.crew_hotel_directory enable row level security;
revoke all on table public.crew_hotel_directory from anon, authenticated;
grant select, insert, update, delete on table public.crew_hotel_directory to service_role;
