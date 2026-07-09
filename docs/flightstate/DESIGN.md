# AeroX Unified FlightState Engine — Definitive Design

**Status:** design locked, reference prototype green (`flight_state.py`, 6/6 scenarios).
**Author:** lead architect synthesis of the three candidate designs + 3-judge jury.
**Problem it kills:** 8 feeds × 4 surfaces × 8 private heuristics = 8 different answers for the same flight, and a class of "ghost" bugs (a taxiing plane rendered cruising near Mannheim; a lost long-haul over Russia; a stale board un-landing a flying plane).

---

## 0. The one idea

> **Collectors do all I/O. A single pure function reduces their output. Surfaces only project.**

```
 SIGNAL COLLECTORS  (all I/O, caching, budget, targeted/allow_paid, codeshare lookup)
   roster · boards · warehouse(flights+aircraft_events) · aircraft_live(NAS FR24) ·
   fr24_grpc(bulk+details) · adsb.lol/fi/live · opensky · paid(ADB/AviationStack) · reference
        │  each emits 0..n  Observation(kind, value, source, obs_ts, conf, status)
        ▼
 ┌──────────────────────────────────────────────┐
 │  resolve_flight_state(keys, observations)     │   PURE. zero I/O. fixture-testable.
 │   • codeshare: marketing → operating keys      │
 │   • per-output freshness-gated precedence      │
 │   • trigger-priority phase machine             │
 │   • airborne gate + position render gate       │
 │   • 3-value confidence ladder                  │
 └───────────────────────┬──────────────────────┘
                         ▼   ONE canonical FlightState
        project_flight_live · project_friend_leg · project_crew_status · project_my_flight_status
```

Why this skeleton (all three judges ranked it #1): the four surfaces **physically cannot diverge** — they reduce the same observation list; the engine is **100 % unit-testable** with no network; and **cost/privacy is structurally safe** because `allow_paid`/`targeted` live only in collectors — the pure function is identical for a paying own-flight caller and a free family caller, it just receives fewer observations.

### The Observation (the only input type)

```python
Observation(
  kind   = "position|phase_hard|phase_soft|dep_time|arr_time|eta|delay|route|reg|event",
  value  = {...},              # kind-specific payload
  source = "roster|board|warehouse_event|warehouse_flight|aircraft_live|fr24_bulk|"
           "fr24_details|adsb|opensky|paid_adb|paid_avstack|reference",
  obs_ts = 1783583910,         # UNIX secs the OBSERVATION was made (not fetch time)
  conf   = 0.0..1.0,           # collector self-trust
  status = "ok | absent | unavailable",   # <-- load-bearing (see §2.4)
  meta   = {...},              # side='arr', proven_airborne, codeshare, verify, flightno_matched…
)
```

`status` is the distinction the jury flagged as essential:
- **ok** — a real signal.
- **absent** — the collector ran and there is genuinely nothing (ADS-B over Siberia). A legitimate miss.
- **unavailable** — the collector errored / timed out. This is **NOT evidence the plane is on the ground**; a timed-out high-tier source must let the engine hold the prior phase, never downgrade it.

---

## 1. Phase state machine

### 1.1 Canonical enum (10 tokens — minimal, exhaustive, mutually exclusive)

| Phase | Meaning | Renders a map dot? |
|---|---|---|
| `SCHEDULED` | plan exists, no departure signal | no (seed at dep) |
| `BOARDING` | at gate: boarding / gate-closed / final call | no |
| `TAXI_OUT` | **off-block, on the ground, not yet airborne** | **no — suppressed** |
| `AIRBORNE` | proven in the air (climb/cruise) | **yes** |
| `APPROACH` | airborne + descent envelope of dest (wording only) | yes |
| `LANDED` | touchdown → not yet on-block | no (taxiing in) |
| `ARRIVED` | on-block / at-gate → layover begins | no |
| `CANCELLED` | board/paid cancel token | no |
| `DIVERTED` | landed at airport ≠ scheduled dest | last known |
| `UNKNOWN` | keys valid, no usable signal (honest blank) | no |

`TAXI_OUT` exists **solely** to give "off-block but not flying" a truthful home so its position is never rendered as cruise. `APPROACH` is a wording refinement of AIRBORNE (from progress/vertical-rate), not a separate authority. Family's 4-token `flight_phase` and the legacy status strings are derived by projection (§4).

### 1.2 The airborne gate — the single load-bearing invariant

```python
def is_airborne_kinematic(pos, near_origin=False):
    alt, gs = pos.alt_ft, pos.gs_kt
    if alt is not None and alt > 1000:  return True
    if gs  is not None and gs >= 80:
        if alt is None and near_origin: return False   # high-speed taxi / rejected T/O guard
        return True
    return False
```

Rules baked around it (each has a property test):
- **The raw `on_ground` bit is ignored entirely.** FR24/adsb.lol lie during pushback (`on_ground=false, alt=None`).
- **Position is rendered ONLY when `phase ∈ {AIRBORNE, APPROACH, DIVERTED}`.** Any fix failing the gate ⇒ `live=None` AND no ETA extrapolation.
- The **near-origin guard** on the gs-only branch (added per jury flaw #6) rejects a fast, alt-less fix sitting on the departure field.

### 1.3 Transition table — trigger-priority reducer

This is a **reducer, not an edge-FSM**: from *any* state, the highest-priority satisfied trigger wins (cannot get "stuck"). Priority high → low:

| # | Trigger (fused signal) | → Phase |
|---|---|---|
| T0 | `phase_hard` cancelled (board/paid) | `CANCELLED` |
| T1 | `event: landed` at airport ≠ dest | `DIVERTED` |
| T2 | `event: landed`@dest **or** board arr-side hard landed/on-block **or** paid actual-arr | `ARRIVED` if on-block/actual-arr else `LANDED` |
| T3 | `event: takeoff` **or** board dep-side **proven** `airborne/en-route` **or** fr24 `flight_stage=AIRBORNE` **or** a position passing the airborne gate | `AIRBORNE` (→`APPROACH` if progress≥0.80 or vrate<−500) |
| — | **sticky-airborne**: prior sample this leg passed the gate → hold AIRBORNE through one slow/low dip | (hold) |
| T4 | live position exists but **fails** the gate **and** a dep off-block signal exists | `TAXI_OUT` |
| T5 | board soft boarding/gate-closed/final-call (dep side) | `BOARDING` |
| T6 | roster leg exists, plan clock | `SCHEDULED` |
| T7 | keys valid, nothing else | `UNKNOWN` |

**The crux of the ghost fix (T3 requires PROOF):** board dep-side **"Abgeflogen" = off-block, NOT airborne.** It is encoded side-aware so it can only satisfy T4 (`TAXI_OUT`), never T3. A bulk-FR24 `on_ground=false` is likewise *not* a T3 trigger — it becomes a position observation, and if that position fails the gate it falls to T4. (This resolves the contradiction the jury caught in the SIMPLICITY draft, adopting AUTHORITY's side-aware encoding — because the real `_status_phase_of` returns `'airborne'` for dep-side "Abgeflogen" today.)

### 1.4 The two guards (the only memory — persisted, see §6)

1. **Monotonicity:** no `LANDED/ARRIVED → AIRBORNE` regression **except** on a HARD signal newer than the one that set the terminal phase (honors a real return-to-gate/go-around, blocks a stale-scheduled throwback). `DIVERTED` (T1) is exempt.
2. **Sticky-airborne:** once a fix this leg passed the gate, a single subsequent slow/low sample (gs 60–79, prop/hold/go-around, coverage dropout) does **not** un-fly the aircraft. Only a hard landed/on-block signal leaves AIRBORNE.

---

## 2. Signal fusion / precedence

One table per output, read top-down; first **fresh, valid** observation wins; ties broken by `obs_ts` (newer) then authority rank. Precedence is **per-output** — the POSITION winner differs from the DELAY winner differs from the PHASE winner (AUTHORITY's key insight).

### 2.1 Freshness gates (`MAX_AGE`, seconds)

adsb 120 · opensky 300 · aircraft_live / fr24_bulk 2100 (35 min) · fr24_details 600 · warehouse_flight 900 · board 720 (12 min) · paid_adb 600 · roster 6 h. A signal older than its gate is **demoted** (a hard phase → soft; a position → simulated or dropped).

### 2.2 Per-output winner order

| Output | Winner order (defers downward) | Conflict rule |
|---|---|---|
| **PHASE** | warehouse `aircraft_events` → board hard (arr-side landed beats dep-side) → fr24 `flight_stage` → airborne-gate over best position → board soft → roster clock | Hard beats soft regardless of recency for landed/departed; soft never overrides a fresh airborne gate. |
| **POSITION** | adsb (targeted, `position_source=0`) → aircraft_live NAS snapshot → warehouse `v_aircraft_latest` → fr24 bulk corridor → paid ADB | rendered only if phase renders position; fresher targeted fix beats older snapshot; over Russia/ocean the snapshot wins (its reason to exist). |
| **DEP time** | paid ADB actual/runway → warehouse takeoff → board esti/sched(+delay) → roster plan | actual > revised > est > sched. |
| **ARR / ETA** | paid ADB actual/revised → board est_arr → fr24 details eta → warehouse est → roster arr → gs-extrapolation | gs-extrapolation is last-resort, flagged `simulated`/`estimated`, **yields NO delay**. |
| **DELAY** | board arr-side (`delay_known`) → board dep-side → paid ADB | `delay_known=False ⇒ delay=None, on_time=None`. Never from extrapolation. |
| **ROUTE** | `ax_route_cache` → board `dest_iata` (arr row → ORIGIN!) → warehouse flights → fr24 bulk `extra_info.route` → aircraft_live → roster → paid | live-confirmed beats plan only if it passes the consistency gate (`dep == roster dep`); else keep plan label (Miami "stimmt nicht" fix). Static callsign→route (adsbdb/hexdb) stays disabled. |
| **REG/tail** | board BOARD_TAIL (0.98)/warehouse verified → aircraft_live `reg_display` (flight-no matched) → fr24 → paid → reference | `verify=false` ⇒ discard; flight-number match overrides stale roster tail (aircraft swap); route-consistency required before adopting a snapshot's reg. |

### 2.3 Worked precedence cases

**Taxi/pushback trap (resolved by construction):** collector emits `position{on_ground_raw=false, alt=None, gs=15}` near FRA → engine ignores `on_ground_raw` → gate fails → no T3 → board dep-side "Abgeflogen" is off-block → **T4 → TAXI_OUT** → position dropped (`live=None`) → gs-extrapolation never runs (only fires for AIRBORNE) so the bogus 13:05 ETA is impossible. **No FRA special-case; it is the general rule.**

**Over-Russia / ocean (LH716 FRA→HND, resolved by precedence):** adsb `absent` (structural hole, not error) → aircraft_live southern-route snapshot (real cruise fix, passes gate) → AIRBORNE, dot on the correct southern arc. If the snapshot is stale (>35 min) but < 45 min, position is emitted `simulated` bounded to the stored route (never a naive great-circle over forbidden airspace); if it runs >30 min past ETA or >sched+45 min with no fresh signal → forced `UNKNOWN` (honest "Position offline"), never an indefinite phantom.

### 2.4 unavailable vs absent

A high-tier source returning **unavailable** (timeout/error) must **hold the last phase** via hysteresis — never be read as evidence of ground. Only **absent** lets precedence fall through to the next tier. The engine must return a valid `UNKNOWN` even when handed an *empty* observation list.

---

## 3. Confidence model — the 3-value honesty ladder

Kept to exactly 3 (jury rejected COVERAGE's fuzzy `[0,1]` scalar as untestable and a tuning treadmill). Every scalar output carries one tag:

| Tag | Meaning | Emitted when | UI marker |
|---|---|---|---|
| `observed` | real, fresh measurement | fix within gate; board hard; aircraft_events; paid actual time | plain text |
| `estimated` | real data but predictive/plan | board sched/est, roster plan, paid predicted, fr24 details ETA | `~` |
| `simulated` | engine/iOS extrapolating past a stale fix | snapshot older than gate but still airborne; gs-based ETA | `~ … (geschätzt)` |

Laws:
- **Never upgrade `estimated → observed`** (a scheduled time that "came true" stays estimated until an actual/event confirms it).
- **Delay is only ever `observed` or absent** — there is no "estimated delay". `unknown ≠ punctual`.
- A `simulated` position **must** carry `stale_since` so iOS can hedge.
- `phase_conf` = confidence of the trigger that set the phase (roster clock → estimated; aircraft_events/board-hard → observed).

---

## 4. Canonical FlightState schema (superset of all four legacy payloads)

Additive: existing iOS keys keep working; new keys are ignored by old clients. `live.{lat,lon,track,gs,alt,on_ground}` is byte-identical to today's `AXLifecycleLive` decoder (`gs`, not `speed`).

```jsonc
{
  "ok": true, "schema": "flightstate/1",
  "keys": { "flight": "LX1071", "mkt_flight": "LH2557", "date": "2026-07-09",
            "dep_iata": "ZRH", "arr_iata": "FRA", "leg_index": 0 },

  "phase": "AIRBORNE",                 // canonical enum §1.1
  "phase_conf": "observed",            // observed|estimated|simulated
  "phase_source": "warehouse_event|board|fr24_details|kinematic|roster|sticky",
  "in_flight": true,                   // == phase in {AIRBORNE,APPROACH}  (legacy)
  "on_time": true,                     // null when delay unknown
  "cancelled": false, "diverted_to": null,

  "route": { "dep": "ZRH", "dst": "FRA", "conf": "observed",
             "source": "board", "label_locked_to_plan": false },

  "reg": "HB-JCA", "reg_conf": "observed", "reg_swap": true,
  "hex": "4B1234", "ac_type": "BCS3", "callsign": "SWR1071",

  "times": {
     "sched_dep_iso": "...Z", "est_dep_iso": "...Z", "act_dep_iso": "...Z", "dep_conf": "observed",
     "sched_arr_iso": "...Z", "est_arr_iso": "...Z", "act_arr_iso": null,   "arr_conf": "estimated",
     "eta_iso": "...Z", "eta_conf": "estimated"
  },
  "delay": { "known": true, "min": 8, "side": "arr",
             "dep_delay_min": 6, "arr_delay_min": 8, "conf": "observed" },

  "live": {                            // null unless phase renders position
     "lat": 48.5, "lon": 8.4, "track": 20, "gs": 420, "alt": 33000,
     "on_ground": false,               // engine-decided (gate), never raw feed
     "conf": "observed", "source": "adsb",
     "obs_ts": "...Z", "stale_since": null, "position_source": 0
  },
  "progress": 0.34,                    // 0..1 great-circle, null if not in_flight
  "sticky_airborne": true,
  "freshness": { "as_of": "...Z", "degraded": false },
  "sources": ["adsb","board"], "unavailable": []
}
```

---

## 5. Migration — 8 heuristics → 1 engine

| Today (divergent) | Fate |
|---|---|
| `app.py:_flight_status_bucket` | **delete** → phase machine T-table |
| `warehouse_reader:_status_phase_of / _status_is_hard / status_for_flight` | **keep as a board COLLECTOR** emitting `phase_hard`/`phase_soft` (side-aware); no longer a phase authority on its own |
| `family_watch:_flight_window_state / _canonical_flight_phase / _load_crew_status_for_family` | phase logic **deleted** → `_load_crew_status_for_family` becomes `project_crew_status()` (privacy grants unchanged) |
| `aerox_data:_nas_live_pos / _aircraft_live_pos / _machine_live / _progress_along_route / ax_flight_live` | position COLLECTORS + engine; `_aircraft_live_pos` taxi-gate becomes the single `is_airborne_kinematic`; endpoint → `project_flight_live()` |
| `app.py:_flight_obs_merged / _board_local_to_utc_iso` | **keep as the board collector** (times/delay/route-endpoint) — already the central merge; engine calls it, doesn't duplicate |
| `aerox_data:_build_inbound_chain / _inbound_arr_row_by_reg / _rotation_positioning_row / ax_turnaround` | collector for arr-time/ETA + reg; inbound = engine run on the inbound leg; turnaround = engine(prev-leg).ARRIVED + engine(next-leg) |
| `get_friends_today` multi-leg cascade / `_sb_day_reg` | leg-selection stays in the friends builder (roster owns "which leg"); it then calls engine per leg instead of hand-rolling live/delay/status |
| adsb `_obs_is_grounded` + scattered kinematic gates | centralized into the two `is_*_kinematic` funcs |

**~11 functions delete their decision logic; their data-fetch halves survive as collectors.**

### 5.1 Endpoint rewiring (thin projections, guaranteed consistent)

```python
st = resolve_flight_state(keys, collect_observations(keys, targeted=is_own_or_watch,
                                                      allow_paid=is_own_or_watch))
return project_flight_live(st)      # or project_friend_leg / project_crew_status / project_my_flight_status
```

- **`ax_flight_live`** → `project_flight_live` (`live, in_flight, progress, sched_arr, est_arr, arr_delay_min, dest_gate, source` + new `phase, phase_conf, eta_iso, eta_conf`).
- **`ax_my_flight_status`** → `project_my_flight_status` (`reg, aircraft, status, delay_known, delay_min, delay_side, on_time, est_dep_iso, est_arr_iso` + `phase, phase_conf`).
- **friends `flights_live[]`** → `project_friend_leg` (exact flat keys preserved; `live` now guaranteed null unless the gate passed → kills bulk taxi ghosts).
- **family `CrewStatus`** → `project_crew_status` (`flying_now, flight_phase∈{airborne,landed,grounded,cancelled}, live_lat/lon` only under the existing grant + `phase_conf∈{observed,estimated}`).

### 5.2 Codeshare resolution (closes the gap ALL THREE designs missed)

Rosters import the **marketing** flight number (LH2557); boards and ADS-B callsigns carry the **operating** one (LX1071 / SWR1071). A codeshare collector emits an observation whose `meta.codeshare = {oper_flight, oper_callsign, oper_carrier}`; `resolve_operating_key()` folds it into the query keys **before** the reg/route/position/board cascades run, so lookups match the metal. Without this, codeshared legs silently fall through to plan/UNKNOWN. (Scenario 3 verifies it: roster `LH2557` → engine keys `LX1071`, tail corrected `HB-OLD → HB-JCA`.)

### 5.3 Rollout (safe, reversible)

1. Land `flight_state.py` (pure, no Flask/Supabase imports) + fixture suite (ghost, Russia, taxi, divert, cancel, swap, stale-landed) + the load-bearing property test. `make verify` green.
2. Wrap existing collectors to emit `Observation`s — **no behavior change yet**.
3. **Shadow-mode 48 h:** compute FlightState alongside each legacy endpoint, log disagreements (no user impact). This surfaces real divergences (incl. any pre-existing `_status_phase_of` mis-tokenization) *before* the flip.
4. Flip endpoints to projections one at a time (`ax_flight_live → friends → family → my-status`), each behind a per-endpoint kill-switch env var with legacy fallback.

---

## 6. State store (Cloud Run correctness — jury blocker)

Hysteresis / last-emitted-state (monotonicity + sticky-airborne) is the only stateful piece. Cloud Run is **multi-instance + ephemeral**, so an in-process store would flap between requests. It lives in **Supabase** (`flightstate_hysteresis`, key `(flight,date,reg)`, TTL 6 h): `{phase, obs_ts, sticky_airborne, updated_at}`, read at the top of the reducer, written after. Any **hard** event always overrides it; DIVERTED is exempt. The **memo cache key includes `targeted` and `allow_paid`** (`(flight,date,dep,arr,reg,targeted,allow_paid)`, ~90 s) so a free/untargeted family record can never be served to a paid own-flight view or vice-versa (fixes AUTHORITY's leak).

---

## 7. iOS phase-line display concept

The engine hands iOS `phase` + `phase_conf` + `eta_conf`. iOS builds `(verb, connector, time, honesty-marker)` — **never a bare absolute unless conf=observed.** Map dot rule (one line, kills ghosts): **render `live` iff `fs.live != null`** — iOS never re-decides airborne.

| Phase / conf | German phase line |
|---|---|
| SCHEDULED | `Geplant · Abflug 12:50` |
| BOARDING | `Boarding · Abflug ~12:52` |
| TAXI_OUT | `Rollt zur Startbahn · FRA` (no dot, no ETA absolute) |
| AIRBORNE observed (early) | `Steigflug` |
| AIRBORNE observed (cruise) | `Reiseflug · an ~14:20` |
| AIRBORNE simulated (Russia/ocean) | `über Sibirien · Ankunft ~14:20 (geschätzt)` + hollow pulsing dot |
| APPROACH | `Sinkflug · an ~14:18` |
| LANDED | `Gelandet · rollt zum Gate` |
| ARRIVED | `Angekommen · Gate B23` |
| DIVERTED | `Umgeleitet nach MUC` |
| CANCELLED | `Annulliert` (rot) |
| UNKNOWN | `Status wird ermittelt` (never "pünktlich", never a fake time/dot) |

Marker mapping: `observed` → no marker · `estimated` → `~` · `simulated` → `~ … (geschätzt)`. Solid dot = observed position; hollow/pulsing = simulated. Delay chip only when `delay_known`.

---

## 8. D-AINV FRA→GVA worked example (the ghost bug)

**Inputs (collectors emit):** roster leg FRA→GVA (STD 12:50Z); board FRA dep-side "Abgeflogen" → `phase_hard=TAXI_OUT (side=dep)`; aircraft_live snapshot `{lat≈Mannheim, gs=15, alt=None, on_ground_raw=false}`; adsb near FRA → grounded/absent.

**Reducer trace:**
1. Position candidate = the snapshot. `is_airborne_kinematic({alt:None, gs:15})` → **False**.
2. Phase scan: no cancel, no landed, **no T3** (no takeoff event, no fr24 stage, board "Abgeflogen" is dep-side off-block ≠ proven airborne, gate fails). A dep off-block signal exists + fix fails gate → **T4 → `TAXI_OUT`**.
3. Position render gate: `TAXI_OUT ∉ {AIRBORNE,APPROACH,DIVERTED}` → **`live=None`**.
4. ETA: gs-extrapolation only fires for AIRBORNE → **does not run** → ETA = board/sched `13:55Z (estimated)`. **The 13:05 ghost is structurally impossible.**

**Before → After:**
- **Before:** `alt<50`-style rule + raw `on_ground=false` → plane rendered cruising near Mannheim, ETA "an ~13:05" gs-extrapolated from a 15 kt fix.
- **After:** `TAXI_OUT`, `live=null`, no ETA absolute → iOS renders **"Rollt zur Startbahn · FRA"**. When D-AINV actually rotates (next snapshot `alt=6000, gs=280` → passes gate, or a warehouse `takeoff` event) → automatic flip to `AIRBORNE`, dot on the real climb-out. No special code.

Prototype output (Scenario 1):
```
phase: TAXI_OUT (observed, via board) · in_flight: False · live: null
eta:   2026-07-09T13:55:00Z (estimated) · delay: known=False on_time=None  => OK
```

---

## 9. Risks & guardrails

| Risk | Guardrail |
|---|---|
| Single-resolver blast radius (one bug → all 4 surfaces) | per-endpoint kill-switch + 48 h shadow-mode + fixture/property tests; precedence is **data, not code** (a systematic mislabel is a one-line global fix) |
| Airborne gate false-negative on low/slow (prop, hold, go-around) | sticky-airborne holds AIRBORNE through one dip; board hard `airborne` overrides the gate |
| Simulated dot masks a real diversion / unobserved landing | hard cap: >30 min past ETA or >sched+45 min with no fresh signal → `UNKNOWN`; any hard signal overrides sim immediately; conf=`simulated` + hollow dot in UI |
| Stale hard signal outranking reality | per-output `max_age` demotes stale hard → soft; monotonicity allows `LANDED→AIRBORNE` only on a *fresher* hard signal |
| Codeshare mis-match | marketing→operating resolver runs before all cascades (§5.2) |
| Cost/privacy leak via cache | memo key includes `targeted`+`allow_paid`; paid tiers stay in collectors; engine callable with empty observation list |
| Hysteresis flapping on Cloud Run | Supabase-backed store, not in-process (§6) |
| Collector timeout read as "on ground" | `unavailable` ≠ `absent`: timeout holds prior phase, never downgrades |
| High-speed taxi / rejected T/O rendered as cruise | near-origin guard on the gs-only gate branch |

**The one guardrail that matters most** (dedicated property test): *for all* position observations failing `is_airborne_kinematic`, output `live == null` AND no ETA is extrapolated. That single invariant is what makes the engine both simple and honest — and it is exactly what the pre-unification code got wrong by trusting the raw `on_ground` bit.
