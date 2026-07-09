# AeroX Unified FlightState Engine — Exec Summary

**One-liner:** Replace 8 private phase/position heuristics scattered across 4 surfaces with **one pure reducer** that every surface projects from — so my-flight, friends, family and radar can never disagree, and the whole class of "ghost aircraft" bugs dies at the source.

## What changes

- **Collectors → Observations → pure reducer → thin projections.** All I/O, caching, budget and `targeted/allow_paid` stay in collectors. `resolve_flight_state()` is a zero-I/O function reduced from a normalized `Observation` list — 100 % fixture-testable.
- **One phase enum** (10 tokens): `SCHEDULED · BOARDING · TAXI_OUT · AIRBORNE · APPROACH · LANDED · ARRIVED · CANCELLED · DIVERTED · UNKNOWN`.
- **One airborne gate** (`alt>1000 OR gs>=80`, near-origin guarded). The raw `on_ground` bit is ignored everywhere. **Position is rendered only when the phase is airborne** — a fix that fails the gate ⇒ `live=null`, no ETA extrapolation.
- **Per-output precedence** (position winner ≠ delay winner ≠ phase winner), freshness-gated, with a 3-value honesty ladder `observed | estimated | simulated` mapping 1:1 to the UI's `~` / `(geschätzt)`.
- `ax_flight_live`, friends `flights_live[]`, family `CrewStatus`, `ax_my_flight_status` become ~15-line **projections** of the one record; legacy JSON keys preserved (additive).

## Why it kills the whole bug class

The ghost bugs (taxiing plane shown cruising near Mannheim; lost long-haul over Russia; stale board un-landing a flying plane; wrong-leg / wrong-tail) all came from **each surface trusting a different raw signal (`on_ground`, `alt<50`, stale board string) on its own.** By construction:
- **"Abgeflogen" on the departure board = off-block, not airborne** → cannot trigger AIRBORNE → taxi plane is `TAXI_OUT` with no dot and no bogus ETA.
- **ADS-B `absent` over Siberia falls through to the FR24 snapshot** on the real southern route, honestly flagged, bounded (never a straight line over forbidden airspace) → the aircraft is never lost, never faked.
- **Monotonicity + sticky-airborne** stop stale/low samples from un-flying a plane.
- Codeshare **marketing→operating** resolution (missing from all three source designs) matches the real metal.

## Rollout (safe, reversible)

1. Land `flight_state.py` (pure) + fixture suite + the load-bearing property test; `make verify` green.
2. Wrap existing collectors to emit `Observation`s — no behavior change.
3. **48 h shadow-mode:** compute FlightState alongside legacy endpoints, log disagreements (no user impact).
4. Flip endpoints one at a time (`ax_flight_live → friends → family → my-status`), each behind a kill-switch env var with legacy fallback.

## Risks (with mitigations)

- **Single-resolver blast radius** → per-endpoint kill-switch + shadow-mode + property tests; precedence is data, not code.
- **Simulated dot masks a real diversion** → hard cap (>30 min past ETA / >sched+45 min → `UNKNOWN`); any hard signal overrides sim instantly.
- **Cloud Run multi-instance hysteresis flap** → state store is Supabase-backed, not in-process; TTL 6 h.
- **Cache cross-contamination / paid leak** → memo key includes `targeted`+`allow_paid`; paid tiers live only in collectors.
- **Collector timeout misread as "on ground"** → `unavailable ≠ absent`; a timed-out high-tier source holds the prior phase.

## Files

- `DESIGN.md` — full design (state machine, precedence tables, schema, migration, iOS phase line, D-AINV walk-through).
- `flight_state.py` — runnable reference prototype (`python3 docs/flightstate/flight_state.py`). **6/6 scenarios green:** D-AINV taxi, over-Russia blind, codeshare+swap, clean cruise, cancelled, stale-landed monotonicity.
