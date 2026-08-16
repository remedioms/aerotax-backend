-- Public API proxy views must evaluate permissions as the caller. The existing
-- anon/authenticated roles already have the matching read access on the three
-- api-schema source views, so this removes the definer escalation without
-- changing their current read contract.
alter view public.v_aircraft_latest set (security_invoker = true);
alter view public.v_flight_feed set (security_invoker = true);
alter view public.v_radar_pins set (security_invoker = true);

-- Keep backend access explicit as well. It currently reads the source tables,
-- but these grants make the proxy views safe to use in future server code.
grant usage on schema api to service_role;
grant select on api.v_aircraft_latest to service_role;
grant select on api.v_flight_feed to service_role;
grant select on api.v_radar_pins to service_role;

notify pgrst, 'reload schema';
