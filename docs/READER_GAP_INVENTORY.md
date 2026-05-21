# Reader Gap Inventory — 11 Frei→Z76 Days

Stand: 2026-05-20. Per-Tag-Analyse: was hat der CAS-Reader heute extrahiert,
was fehlt, ist Fix aus Fixture möglich.

## §1 Pro-Tag-Tabelle

| Datum | Golden klass / land / pos / size | dauer_h | Reader marker / activity / routing / layover / overnight / start-end / duty | raw_lines | sources | alt v7 classifier | Missing facts | Likely reader failure | Required extraction | Fix-Source |
|---|---|---:|---|:-:|---|---|---|---|---|:-:|
| 2025-05-17 | Z76 / USA / 4/4 (Abreise) | 10.5h | `OFF` / frei / [] / "" / False / "" / 0min | 0 | DP | klass=Frei, kein SE | tour_id, position_in_tour, route-end-segments, abreise-time | OFF marker **innerhalb 4-day-USA-Tour-Schluss** als Frei gelesen statt tour_end | tour_id_candidate aus prev_day routing TLV→FRA; position=4/4 | **needs_pdf_reread** |
| 2025-06-17 | Z76 / Kroatien / 1/2 (Anreise) | 8.3h | `OFF` / frei / ['FRA'] / "" / False / "" / 0min | 0 | DP | klass=Frei, kein SE | routing=FRA→[ZAG/PUY/...], layover, overnight, start/end | OFF-Marker statt Tour-Start gelesen | tour_start, foreign-routing-extraction, overnight=True | **needs_pdf_reread** |
| 2025-06-18 | Z76 / Kroatien / 2/2 (Abreise) | 18.2h | `OFF` / frei / ['FRA'] / "" / False / "" / 0min | 0 | DP | klass=Frei, kein SE | routing=Kroatien→FRA, layover-prev=Kroatien | OFF-Marker statt Tour-End gelesen | continuation_from_prev_day=True | **needs_pdf_reread** |
| 2025-07-23 | Z76 / Schweden / 1/1 (Anreise+Abreise) | 8.1h | `==` / frei / [] / "" / False / "" / 0min | 0 | DP | klass=Frei, kein SE | routing=FRA→[ARN/GOT/...]→FRA, same-day-foreign | == als Frei gelesen statt same_day foreign roundtrip | same_day-Tour mit foreign-IATA | **needs_pdf_reread** |
| 2025-08-22 | Z76 / Zypern / 3/3 (Abreise) | 6.6h | `X` / frei / ['FRA'] / "" / False / "" / 0min | 0 | DP | klass=Frei, kein SE | continuation_from_prev_day, routing-back-to-FRA | X als Frei statt Tour-End | tour_end, prev_layover=Zypern (LCA) | **needs_pdf_reread** |
| **2025-09-26** | Z76 / Bulgarien / 1/3 (Anreise) | 18.0h | `15688 PU (Day 2)` / tour / ['KRK','FRA','IST'] / IST / True / 04:10-10:05 / 355min | 0 | **DP, SE** | klass=**Z74**, se_ort=MUC, dp_layover=IST | bulgarien-IATA-Korrelation, Day-Suffix-Interpretation | Reader hat tour-Tag erkannt aber dp_layover=IST nicht als Bulgarien (SOF/BOJ) → alt v7 Z74 fälschlich | SE→MUC suggest Inland — Konflikt zu Golden Bulgarien. **Audit-Frage** | **needs_se_crosscheck + audit** |
| **2025-10-15** | Z76 / Frankreich / 1/2 (Anreise) | 16.7h | `""` / unknown / ['FRA'] / "" / False / "" / 0min | 0 | **DP, SE, BMF2025** | klass=**Z76**, land=Frankreich, **se_ort=MRS** | tour_start, layover=MRS (Marseille) | Reader marker leer, aber SE hat MRS → tour bestätigt | SE-foreign-Stempel + foreign-iata aus se_effective_ort | **fixable from fixture** (alt v7 hat schon Z76 erkannt) |
| **2025-10-16** | Z76 / Spanien / 2/2 (Abreise) | 22.1h | `""` / unknown / ['FRA'] / "" / False / "" / 0min | 0 | **DP, SE, BMF2025** | klass=**Z76**, land=Spanien, **se_ort=AGP** | continuation_from_prev_day (MRS→AGP-Tour), layover=AGP (Málaga) | Reader leer, aber SE hat AGP | SE-foreign-Stempel | **fixable from fixture** |
| **2025-10-25** | Z76 / UK-London / 3/3 (Abreise) | 23.5h | `""` / unknown / ['FRA'] / "" / False / "" / 0min | 0 | **DP, SE, BMF2025** | klass=**Z76**, land=UK-London, **se_ort=LON** | continuation_from_prev_day (LON), tour_end | Reader leer, aber SE hat LON | SE-foreign-Stempel + LON-IATA | **fixable from fixture** |
| **2025-11-17** | Z76 / Norwegen / 1/2 (Anreise) | 4.9h | `SB_M` / standby / ['FRA'] / "" / False / 08:00-15:30 / 450min | 0 | **DP, SE** | klass=**Standby**, **se_ort=SVG** | tour_start vs standby? Norwegen=Stavanger (SVG) | SB_M (Standby Morning) → Tour-Standby-Trigger? oder echt? | **fixable from fixture** (SE-SVG bestätigt Norwegen-Tour) |
| 2025-11-18 | Z76 / Norwegen / 2/2 (Abreise) | 16.1h | `==` / frei / [] / "" / False / "" / 0min | 0 | DP | klass=Frei, kein SE | continuation_from_prev_day (SVG), tour_end | Layover-Continuation-Marker als Frei | continuation_from_prev_day=True | **needs_pdf_reread** (oder Inferenz aus 11-17 SE-Stempel) |

## §2 Klassifizierung

### `fixable_from_existing_fixture` (5 Tage)

Diese haben in `classifier_result.se_effective_ort` einen foreign-IATA. Tour-First-Layer hat sie übersehen, weil **mein `_build_matched_from_raw` die SE-Rekonstruktion nicht ausreichend nutzt** — aktuell nur wenn `sources` enthält 'SE'.

| Datum | alt-v7-erkannt | Was zu tun |
|---|---|---|
| 2025-10-15 | Z76 Frankreich/MRS | SE-Rekonstruktion müsste foreign-Stempel = MRS setzen — passiert schon, aber routing leer → Tour-First erkennt nicht. Brauche zusätzlichen Override-Pfad: `se_foreign UND prev/next_in_tour` → akzeptiere als Tour-Tag |
| 2025-10-16 | Z76 Spanien/AGP | dito |
| 2025-10-25 | Z76 UK-London/LON | dito |
| 2025-11-17 | SE-SVG (Norwegen) → alt v7 fälschlich Standby | SB_M mit SE-foreign-Stempel → Tour-Standby-Pfad |
| 2025-09-26 | alt v7 Z74 (MUC inland) | **Konflikt**: Golden sagt Bulgarien, alt v7 sagt MUC-Inland. Audit nötig. |

### `needs_pdf_reread` (6 Tage)

Diese haben nur `sources=['DP']` ohne SE-Daten und keine raw_lines. Tour-First kann **nichts erfinden** ohne CAS-Re-Read. Diese Tage sind reine Reader-Bugs.

| Datum | Golden | Reader-Fail | Was im PDF stehen müsste |
|---|---|---|---|
| 2025-05-17 | Z76 USA Abreise | OFF statt Tour-End | LH-Flight-Number `XXX A`/Tour-End-Suffix |
| 2025-06-17 | Z76 Kroatien Anreise | OFF statt Tour-Start | LH-Flight FRA→[ZAG/PUY/...] |
| 2025-06-18 | Z76 Kroatien Abreise | OFF statt Tour-End | LH-Flight [Kroatien]→FRA |
| 2025-07-23 | Z76 Schweden same-day | == statt same_day | LH-Flight FRA→[ARN]→FRA |
| 2025-08-22 | Z76 Zypern Abreise | X statt Tour-End | LH-Flight [LCA]→FRA |
| 2025-11-18 | Z76 Norwegen Abreise | == statt Tour-End | LH-Flight [SVG]→FRA |

### `needs_se_crosscheck + audit` (1 Tag)

| Datum | Konflikt |
|---|---|
| 2025-09-26 | Reader: dp_layover=IST, alt v7: se_ort=MUC (Inland), Golden: Bulgarien. SE-Ort widerspricht sowohl dem Reader-DP-Layover als auch Golden. **Vermutung**: Marker `15688 PU (Day 2)` ist Tour-Continuation, IST war Tag-1-Layover, Bulgarien (SOF?) war Tag-2-Layover. Audit nötig. |

### `need_user_only_if_no_data` (0 Tage)

Keine — alle 11 Tage haben mindestens einen Datenpunkt (entweder SE-Stempel oder Tour-Position in Golden).

## §3 Aufwand-Schätzung

| Pfad | Tage | Fix-Aufwand | KPI-Effekt |
|---|---:|---|---|
| fixable_from_existing_fixture | 5 | Logik-Erweiterung in `_normalize_tours_from_raw_facts`: `se_foreign + prev_or_next_in_tour` → tour_mid/tour_start | +5 arbeitstage, +5 hotel, +Z76-Pauschalen |
| needs_pdf_reread | 6 | CAS-Reader-V2 mit verbessertem Prompt + Re-Read der 6 Tage. Erfordert lokales CAS-PDF + Live-KI-Calls | +6 arbeitstage, +5 hotel, +Z76-Pauschalen |
| needs_se_crosscheck + audit | 1 | Audit-Entscheidung: ist 09-26 SE-MUC ein Reader-Fehler, oder echte Inland-Komponente einer Multi-Stop-Tour? Disambig durch raw-line-Excerpt | ggf. +1 arbeitstag, +Z76 |

**Bei nur §3 Path 1 (fixable_from_existing_fixture)** + bestehende Logik:
- arbeitstage 119 → ~124 (Δ-9 vs Golden 133, noch außerhalb ±2)
- hotel 53 → ~57 (Δ-9)
- z76_eur 4864 → ~5050 (innerhalb Toleranz)
- gesamt 4948 → ~5150 (Δ-870, noch außerhalb ±150)

**Bei §3 Path 1 + Path 2 (alle 11 Tage gefixed)**:
- arbeitstage 119 → ~130 (innerhalb ±2 wenn Path 1+2)
- hotel 53 → ~63 (innerhalb ±2)
- z76_eur → ~5400+ (eventuell überschießend, je nach Land/Pauschale)
- gesamt → ~5800+ (innerhalb ±150?)

## §4 Risk-Tabelle

| Fix-Pfad | Risk wenn falsch |
|---|---|
| Path 1 (fixable_from_existing_fixture) | niedrig: alt v7 hat dieselbe Logik schon validiert für 4/5 Tage |
| Path 2 (needs_pdf_reread) | mittel: Re-Read könnte neue Inkonsistenzen schaffen; Reader-V2-Prompt muss generalisierbar sein |
| Path 3 (09-26 audit) | mittel: SE vs Golden vs Reader-DP-Konflikt; user-Entscheidung sinnvoll |

## §5 Empfehlung

**Phase R2/R3**: Reader-V2-Prompt-Spec + Tests bauen (generalisierbar, nicht Tibor-hardcoded).

**Phase R4-Status**: 
- 5 Tage **NICHT** PDF-Re-Read-bedürftig (fixable via Logik-Erweiterung in Tour-First-Layer).
- 6 Tage **brauchen** CAS-PDF-Re-Read. STOP-Bedingung wenn CAS-PDF nicht lokal verfügbar.
- 1 Tag braucht raw_line-Audit.

**Pragmatisch**: 5 fixable_from_existing → Logic-Erweiterung. 6 needs_pdf_reread → R4 STOP-Punkt (PDF benötigt).
