-- Secure backend-owned tables that were created through raw SQL before RLS
-- was enabled. AeroX clients do not access these tables directly; all reads and
-- writes go through the backend's service_role client, which bypasses RLS.

alter table if exists public.aircraft_track enable row level security;
alter table if exists public.crew_hotel_directory enable row level security;
alter table if exists public.crew_circles enable row level security;
alter table if exists public.crew_circle_members enable row level security;

revoke all on table public.aircraft_track from anon, authenticated;
revoke all on table public.crew_hotel_directory from anon, authenticated;
revoke all on table public.crew_circles from anon, authenticated;
revoke all on table public.crew_circle_members from anon, authenticated;

-- Daily ADS-B contact tables are created outside the normal migration path.
-- Lock every existing daily table now; the event trigger below covers future
-- tables as soon as they are created.
do $do$
declare
  table_record record;
begin
  for table_record in
    select c.oid::regclass as table_name
      from pg_class c
      join pg_namespace n on n.oid = c.relnamespace
     where n.nspname = 'public'
       and c.relkind in ('r', 'p')
       and c.relname like 'adsb_contacts_y%'
  loop
    execute format('alter table %s enable row level security', table_record.table_name);
    execute format(
      'revoke all on table %s from anon, authenticated',
      table_record.table_name
    );
  end loop;
end
$do$;

-- Supabase exposes the public schema through its Data API. Make RLS the safe
-- default for every future public table, including tables added by background
-- collectors. A table that intentionally needs client access must still add an
-- explicit policy and grant in its own migration.
create or replace function public.aerox_rls_auto_enable()
returns event_trigger
language plpgsql
security definer
set search_path = pg_catalog
as $function$
declare
  command_record record;
begin
  for command_record in
    select *
      from pg_event_trigger_ddl_commands()
     where command_tag in ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO')
       and object_type in ('table', 'partitioned table')
  loop
    if command_record.schema_name = 'public' then
      begin
        execute format(
          'alter table if exists %s enable row level security',
          command_record.object_identity
        );
      exception
        when others then
          raise warning 'aerox_rls_auto_enable failed for %: %',
            command_record.object_identity,
            sqlerrm;
      end;
    end if;
  end loop;
end
$function$;

drop event trigger if exists aerox_ensure_rls;
create event trigger aerox_ensure_rls
  on ddl_command_end
  when tag in ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO')
  execute function public.aerox_rls_auto_enable();

notify pgrst, 'reload schema';
