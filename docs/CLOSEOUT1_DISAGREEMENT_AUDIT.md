# Closeout 1 — CAS-FollowMe Disagreement Final Audit

Stand: 2026-05-20. Eingabe: 19 echte Pipeline-vs-Golden-Konflikte (Phase 6).

Rohdaten: `docs/CLOSEOUT1_DISAGREEMENT_AUDIT.json`.

## §0 Decision Rules

- **A**: CAS klar Frei/Standby + KEINE SE-Spesen + Golden behauptet Tour → CAS+SE win, **documented_reference_disagreement** (kein KPI-Effekt).
- **B**: CAS Frei/Standby/unknown ABER SE hat Spesen → **SE wins**, Tour rekonstruieren.
- **X**: Day-Suffix/Continuation-Bug → **Tour-Continuation forcen** wenn cas_at=tour+layover+overnight.

## §1 Decision A — documented_reference_disagreement (5 Tage)

CAS und SE haben KEINE Tour-Evidenz für diese Tage. Golden behauptet Tour, ist aber nicht durch unsere Pflicht-Quellen belegt. AeroTAX bleibt CAS-conform.

| Datum | CAS marker | CAS at | SE | Golden | Decision |
|---|---|---|---|---|---|
| 2025-05-17 | `OFF` | frei | KEINE | Z76 USA pos 4/4 | A — keine SE-Spesen für USA-Abreise |
| 2025-06-17 | `OFF` | frei | KEINE | Z76 Kroatien pos 1/2 | A — keine SE-Spesen für Kroatien-Anreise |
| 2025-08-01 | `X` | frei | KEINE | Z73 Deutschland pos 5/5 | A — keine SE-Spesen für Inland-Tour |
| 2025-08-22 | `X` | frei | KEINE | Z76 Zypern pos 3/3 | A — keine SE-Spesen für Zypern |
| 2025-09-20 | `==` | frei | KEINE | Z72 Deutschland pos 1/1 | A — keine SE-Spesen für Inland-Same-Day |

**KPI-Effekt**: 0 — AeroTAX bleibt CAS-conform, Golden-Tests werden weiterhin als rot dokumentiert.

## §2 Decision B — SE wins → Tour rekonstruieren (10 Tage)

CAS-Marker zeigt RES/SB/Standby, ABER SE hat klare Spesen (foreign oder inland) für diesen Tag. Das ist klassische Standby-Activation: der Crew-Member war auf RES-Bereitschaft, wurde aktiviert und flog. SE belegt es.

| Datum | CAS marker | SE ort | SE inland | Golden | Decision | Effekt |
|---|---|---|---|---|---|---|
| 2025-04-23 | `RES` | FRA | true | Z73 Deutschland pos 1/4 | B inland | +1 Z73, +1 arbeitstag, +1 fahrtag |
| 2025-04-24 | `RES` | SEL | false | Z76 Republik Korea pos 2/4 | B foreign | +1 Z76, +1 arbeitstag, +1 hotel |
| 2025-04-25 | `RES` | SEL | false | Z76 Republik Korea pos 3/4 | B foreign | +1 Z76, +1 arbeitstag, +1 hotel |
| 2025-04-26 | `RES` | SEL | false | Z76 Republik Korea pos 4/4 | B foreign | +1 Z76, +1 arbeitstag, +1 hotel |
| 2025-10-20 | `RES_SB` | HAM | true | Z73 Deutschland pos 1/2 | B inland | +1 Z73, +1 arbeitstag, +1 fahrtag |
| 2025-10-21 | `RES` | AGP | false | Z76 Spanien pos 2/2 | B foreign | +1 Z76, +1 arbeitstag, +1 hotel |
| 2025-10-23 | `RES` | LEJ | true | Z73 Deutschland pos 1/3 | B inland | +1 Z73, +1 arbeitstag, +1 fahrtag |
| 2025-10-24 | `RES` | LON | false | Z76 London pos 2/3 | B foreign | +1 Z76, +1 arbeitstag, +1 hotel |
| 2025-10-25 | (leer) | LON | false | Z76 London pos 3/3 | B foreign (cas_at=unknown +SE foreign) | +1 Z76, +1 arbeitstag, +1 hotel |
| 2025-11-17 | `SB_M` | SVG | false | Z76 Norwegen pos 1/2 | B foreign | +1 Z76, +1 arbeitstag, +1 hotel |

**KPI-Effekt geschätzt**: +10 arbeitstage, +3 fahrtage (Anreise-Tage 04-23, 10-20, 10-23), +7 hotel_naechte (foreign), +3 Z73 (inland), +7 Z76 (foreign).

## §3 Decision X — Day-Suffix/Continuation-Bug (3 Tage)

CAS hat klare Day-Suffix-Tour-Evidenz (Day 2/3 + cas_at=tour + layover + overnight), aber Pipeline droppt den Day-1 wegen FTL-Plausi-Warning und damit fällt die Day 2/3-Continuation auch durch.

| Datum | CAS marker | cas_at | Layover | duty | Golden | Bug |
|---|---|---|---|---|---|---|
| 2025-07-02 | `129023 PU / Tag 2+3` | same_day | (none) | n/a | Z76 London pos 3/3 | Tag 2+3 Continuation nach Day 1 |
| 2025-09-26 | `15688 PU (Day 2)` | tour | IST | 355 | Z76 Bulgarien pos 1/3 | Day 1 (09-25) droppt FTL duty=1059, dadurch Day 2 fail |
| 2025-09-27 | `15688 PU (Day 3)` | tour | AGP | 435 | Z74 Deutschland pos 2/3 | dito |

**Root Cause für 09-26/27**: Day 1 (09-25) hat duty=1059min > 840 FTL-Limit. Pipeline droppt 09-25. Day-Suffix-Continuation an 09-26 checkt `in_tour[i-1]=in_tour[09-25]=False` → continuation fails.

**Fix**: Day-Suffix-mit-Tour-Evidence (cas_at=tour + layover + overnight) muss `in_tour[i]=True` setzen **unabhängig** von Prev-In-Tour.

**KPI-Effekt geschätzt**: +3 arbeitstage, +2 hotel_naechte (09-26 IST, 09-27 AGP), +1 Z74 (09-27 inland), +1 Z76 (09-26 foreign), +1 Z76 (07-02 LON).

## §4 Decision C — needs_review (1 Tag, partial)

| Datum | CAS marker | cas_at | SE | Golden | Decision |
|---|---|---|---|---|---|
| 2025-10-15 | (leer) | unknown | MRS foreign | Z76 Frankreich pos 1/2 | B foreign (cas_at=unknown + SE-foreign-ort → SE wins) |

(10-25 LON ist bereits in §2 als B aufgeführt; 10-15 MRS ist analog → wandert nach §2 Decision B.)

## §5 Erwarteter KPI-Effekt nach Fix-Anwendung

| KPI | Aktuell | Δ aus B-Fix | Δ aus X-Fix | Geschätzt neu | Golden | Status nach Fix |
|---|---:|---:|---:|---:|---:|:---:|
| arbeitstage | 123 | +10 | +3 | 136 | 133 | yellow (+3) |
| hotel_naechte | 55 | +7 | +2 | 64 | 66 | ✓ (-2) |
| fahr_tage | 37 | +3 | 0 | 40 | 58 | RED (-18) |
| z72_tage | 3 | 0 | 0 | 3 | 5 | yellow (-2) |
| z73_tage | 4 | +3 | 0 | 7 | 11 | RED (-4) |
| z74_tage | 0 | 0 | +1 | 1 | 1 | ✓ |
| z76_eur | 5049 | +(7×~50€)≈350 | +(2×~50€)≈100 | ~5499 | 4794 | RED (+705, drift) |
| gesamt | 5147 | +450 | +150 | ~5747 | 6020.72 | RED (-273) |

**Fahrtage-Gap** (-18) bleibt unerklärt — möglicherweise weitere Tour-Anreise-Erkennung nötig. Z76_eur drifts weiter — die SE-Land-Pauschalen sind unterschiedlich.

## §6 Empfehlung

1. **Implementiere Fix-1** (Standby-Activation, §2) — generalisierbar.
2. **Implementiere Fix-2** (Day-Suffix-Continuation, §3) — generalisierbar.
3. **Akzeptiere §1 als documented_reference_disagreement** (5 Tage) — kein Code-Fix.
4. **Re-Run Acceptance** und evaluiere ob Restl. fahrtage-Gap weiteren Fix braucht.

## §7 Nicht in Scope

- 2025-10-26 ist Z76 TLV (Tel Aviv), nicht Tokyo wie Golden vermutet → Aber: TLV ist ja Israel/foreign → Pipeline klassifiziert korrekt, kein Bug.
- 2025-11-18, 11-19 phantom-Z73/Z76 (Marker `==`, kein Tour-Kontext) — Pipeline halluziniert eine Tour. Das ist ein **separater Bug** (Anti-Phantom-Tour), nicht im Disagreement-Scope.
