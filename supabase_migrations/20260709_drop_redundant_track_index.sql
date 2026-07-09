-- idx_aircraft_track_reg_ts ist identisch zum Primary Key (reg, seen_ts) → reine
-- Write-Amplification bei jedem Breadcrumb-Insert, null Read-Nutzen (Kosten-Review 2026-07-09).
drop index if exists idx_aircraft_track_reg_ts;
