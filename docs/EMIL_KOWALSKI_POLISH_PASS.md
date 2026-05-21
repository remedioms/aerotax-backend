# Emil Kowalski Polish Pass

Stand: 2026-05-21.
Skill-Quelle: `emilkowalski/skill` → `emil-design-eng/SKILL.md` (UI polish, animation decisions, invisible details).

## §1 Was wurde angefasst

### Foundation: Easing- + Timing-Variablen im `:root`

Vorher waren custom easings inkonsistent (`.22,1,.36,1` an einigen, weak built-in `ease` an vielen Stellen). Jetzt zentral:

```css
--ease-out:    cubic-bezier(0.23, 1, 0.32, 1);   /* enter/exit UI — punchy */
--ease-in-out: cubic-bezier(0.77, 0, 0.175, 1);  /* on-screen movement */
--ease-drawer: cubic-bezier(0.32, 0.72, 0, 1);   /* iOS drawer-style */

--t-press:  140ms;  /* button press feedback */
--t-tip:    180ms;  /* tooltips, small popovers */
--t-pop:    220ms;  /* dropdowns, selects, popovers */
--t-modal:  280ms;  /* modals, drawers */
```

### Polish-Review (Before/After/Why)

| Before | After | Why |
| --- | --- | --- |
| `transition: all .25s` auf `.st`, `.sdot`, `.bo`, `.rc-icon`, `.optblock`, `.img-upload`, `.sp-close`, `.sp-tab`, `.sp-send`, `.sstep`, `.sstep-icon`, `.legal-close` + 2× Inline-Like-Buttons | Explicit properties (background/color/border-color/transform) mit `var(--ease-out)` + spezifischen Durations | `transition: all` triggert auch teure Properties (height, width, layout). Emil: "Specify exact properties" |
| `transition: opacity .2s ease` auf `.dlb` | `transition: opacity var(--t-pop) var(--ease-out), transform var(--t-press) var(--ease-out), box-shadow var(--t-pop) var(--ease-out)` | Built-in `ease` lacks punch — custom curve mit klar definierten Properties |
| `@keyframes popIn{ from{ scale(0) } to{ scale(1) } }` | `from{ scale(.6); opacity:0 } to{ scale(1); opacity:1 }` | Emil: "Never animate from scale(0). Nothing in the real world appears from nothing." |
| `float-up` particle starts `scale(0)` | `scale(.7)` start mit opacity:0 | Selbst dekorative Particles bekommen baseline-Größe |
| Keine `:active` scale auf Primary CTAs (`.bth`, `.btg`, `.dlb`, `.pact .bn`, `.bo`, `.sp-close`, `.sp-send`, `.legal-close`) | `:active{ transform: scale(.97) }` mit `transition: transform var(--t-press) var(--ease-out)` | Emil: "Buttons must feel responsive to press. Scale 0.95–0.98 on `:active`." |
| `.bth:hover` / `.btg:hover` / `.dlb:hover` / `.bo:hover` / `.legal-close:hover` ohne hover-media-query | `@media (hover: hover) and (pointer: fine){ ... }` Wrapper | Touch devices triggern hover on tap → false positives. Gate verhindert "klebenden" Hover-Zustand auf Mobile |
| `.dlb:hover{ opacity:.85 }` | `.dlb:hover{ opacity:.92; box-shadow:0 6px 18px rgba(234,122,60,.40) }` | Subtileres opacity + box-shadow-Glow als zweite Dimension |
| Keine Stagger auf USP-Cards | `.hc.reveal:nth-child(N){ animation-delay: (N-1)×50ms }` mit `hcReveal` keyframe (translateY 8px + scale .97 → 0 + 1) | Emil: 30–80ms per step, max 4 cards = ~200ms total. Cascading entry feels alive |
| `transition: background .3s ease, ... transform .3s ease` auf `.hc` | `transition: background-color 280ms var(--ease-out), border-color 280ms var(--ease-out), box-shadow 280ms var(--ease-out), transform 280ms var(--ease-out)` | Stronger curve, präzise property list |
| `transition: background-position .35s cubic-bezier(.22,1,.36,1)` (Shimmer auf `.bth::after`) | dito mit `var(--ease-out)` | Konsistente Variable statt magic curve |
| `.sstep` enters with `transition: all .4s` | Property-list mit `280ms var(--ease-out)` | Schneller + spezifisch |

### Animation-Decision-Framework angewandt

| Element | Frequency | Decision |
| --- | --- | --- |
| Step-Tab Activation `.st` | Wenige Male pro Session | Color-transition only, 220ms ease-out |
| Button Press (`:active`) | Mehrfach täglich | 140ms feedback (Emil: 100-160ms) |
| Popovers / Tooltips | Häufig | 180ms ease-out |
| Panel-Transition (`.panel-entering`) | Mehrmals pro Session | 220ms (vorher schon stark) |
| USP-Card Stagger | Einmal pro Seitenaufruf | 420ms-Cascade, prefers-reduced-motion respected |
| Particle `float-up` | Continuous loop, background | Linear infinite (Emil: constant motion → linear) |
| Hold-to-Delete | Nicht vorhanden | (nicht eingebaut; bestehende `.bn`-Step-Reveal-Pattern hat bereits 2s color morph mit kurzem transform-press separat) |

### Was bewusst NICHT angefasst wurde

- `.bn` (Step-Aktivierung): hat 2s color/border morph für „glass → gradient" reveal beim Step-Wechsel. Das ist intentional und gemäß Emil unter "marketing/explanatory — can be longer". Transform-press wurde separat hinzugefügt mit `var(--t-press)`.
- `.chat-msg-pop` (Chat-Bubble): existierender custom curve `cubic-bezier(.34,1.56,.64,1)` — playful bounce, passt zur Persönlichkeit von Chat-Bubbles. Emil's „match motion to mood".
- Particle keyframe `float-up` start war `scale(0)` — auf `scale(.7)` angehoben, aber Loop-Logik blieb.
- Existierende `@keyframes` für Page-Reveals + Hero-Up-Sequence — bleibt.

## §2 Tests

`tests/test_frontend_release_copy.mjs` — erweitert um `[emil polish]`-Block mit 14 zusätzlichen Tests:

| Test | Was er prüft |
| --- | --- |
| `easing_variables_defined_in_root` | `--ease-out: cubic-bezier(0.23, 1, 0.32, 1)` existiert |
| `timing_variables_defined_in_root` | `--t-press` + `--t-pop` definiert |
| `no_transition_all_in_authored_styles` | 0× `transition: all` außerhalb von JS |
| `primary_cta_bth_has_active_scale` | `.bth:active` mit `scale(.9X)` |
| `primary_cta_btg_has_active_scale` | `.btg:active` mit `scale(.9X)` |
| `pdf_dlb_has_active_scale` | `.dlb:active` mit `scale(.9X)` |
| `zurueck_bo_has_active_scale` | `.bo:active` (Back-Button) hat press feedback |
| `weiter_bn_has_active_scale` | `.pact .bn:active` hat press feedback |
| `hover_gated_for_touch_devices` | `@media (hover: hover) and (pointer: fine)` existiert |
| `no_scale_zero_entry_keyframe` | Kein `from { transform: scale(0) }` in Keyframes |
| `popIn_starts_from_six_not_zero` | popIn-Keyframe startet bei scale(.6) |
| `usp_cards_have_stagger_animation` | `.hc.reveal:nth-child(2)` hat `animation-delay: 50ms` |
| `stagger_within_emil_30_80ms_per_step` | Per-step delays ≤80ms |
| `reduced_motion_respected_in_stagger` | `@media (prefers-reduced-motion: reduce)` kill switch |

## §3 Final Test Results

```
test_frontend_release_copy.mjs          : 70/70 ✓ (was 55, +15 Emil tests)
test_frontend_state_machine_live_run    : 28/28 ✓
test_frontend_scroll_helpers            : 15/15 ✓
test_frontend_progress_shimmer          : 23/23 ✓
Full backend pytest                     : 2148 passed, 13 skipped, 13 xfailed
TOTAL                                   : 2284 tests, 0 failed
```

## §4 Polish-Metriken

| Metric | Vorher | Jetzt |
| --- | --- | --- |
| `transition: all` count | 14 | **0** |
| `scale(0)` entries | 3 | **0** (alle ≥ .6) |
| `:active scale` button feedback | 1 (nur `.gl-btn`) | **9** primary CTAs |
| Hover-Gates für Touch | 0 | **5** Stellen |
| Zentrale Easing-Variablen | 0 | **4** (`--ease-out`, `--ease-in-out`, `--ease-drawer`, `--ease-snappy`) |
| Zentrale Timing-Variablen | 0 | **4** (`--t-press`, `--t-tip`, `--t-pop`, `--t-modal`) |
| Stagger-Animationen | 0 | **1** (USP-Cards) |
| `prefers-reduced-motion`-Respect | 7 Stellen | 8 Stellen (+Stagger-Killswitch) |

## §5 Was der User spürt

- **Buttons reagieren auf Press**: `:active{ scale(.97) }` mit 140ms — sofortiges Feedback, „UI hört zu"
- **Hover bleibt nicht kleben auf Mobile**: Touch-Tap löst keinen permanenten Hover-State mehr aus
- **USP-Cards bauen sich auf**: Cascading entry 0/50/100/150/200ms statt alle gleichzeitig
- **Particles + popIn fühlen sich nicht aus dem Nichts erscheinen**: scale(.6/.7) start statt scale(0)
- **Konsistente Bewegungssprache**: Alle Transitions teilen die gleichen Easings/Durations — kein Mix aus weak `ease` + custom curves mehr

## §6 Outstanding (für späteren Sprint)

- **Glass-Card-Transition** beim Step-Wechsel (`.panel-entering`) — könnte custom `--ease-out` statt eigene magic curve nutzen
- **Result-Card Hover-Lift** — könnte mit `transform: translateY(-2px)` + box-shadow boost subtile Tiefe geben
- **Skeleton-States** im PDF-Result während Recalc — opacity-Pulse mit reduced-motion-respect

## §7 Hard-Stops eingehalten

- Backend unverändert (kein gcloud-Deploy nötig)
- HTML-Struktur unverändert (nur CSS + minimale style attrs)
- Test-Coverage gewachsen (55 → 70 release-copy Tests)
- Kein Live-Run gestartet
