# FIX10 Phantom-Tour-Removal — Beweisphase

Stand: 2026-05-20. Eingabe: per-Tag CAS+SE+Golden-Analyse, Context −3/+3 Tage.

## §0 Master-Regel

Pro Tag muss bewiesen sein:
- keine CAS-Dienstplan-Evidenz für echte Tour
- keine SE-Auslandsspesen, die den Tag bestätigen
- oder starke Reader-/FTL-/Tour-Boundary-Warnung
- oder Tag liegt außerhalb echter Tour-Span

→ Nur dann DROP.

Wenn SE oder CAS die Tour bestätigt → NICHT droppen.
Wenn unklar → needs_review.

## §1 Phantom-Day-Tabelle

| Datum | CAS marker | CAS evidence (route/layover/overnight/duty) | SE | Golden | Pipeline aktuell | Pro DROP | Gegen DROP | Entscheidung | Risiko | KPI-Impact |
|---|---|---|---|---|---|---|---|---|---|---|
| **2025-05-20** | `103703 P1` | FRA→LAD, LAD, overnight=T, duty=234, start=20:05 | – | – | Z73 14€ tour_start | Golden missing | **CAS belegt klar**: P1-Marker + foreign routing + layover LAD + overnight + duty | **KEEP** | – | – |
| **2025-05-21** | `103703 P1` | LAD, LAD, overnight=T, duty=270 | – | – | Z76 40€ tour_mid Angola | Golden missing | CAS-Tour-Continuation (P1) | **KEEP** | – | – |
| **2025-05-22** | `103703 P1` | LAD→FRA, LAD, overnight=T, duty=179 | – | – | Z76 40€ tour_mid Angola | Golden missing | CAS-Tour-Continuation | **KEEP** | – | – |
| **2025-05-23** | `103703 P1` | LAD, –, overnight=F, duty=330 (same_day return) | – | – | Z76 40€ tour_mid Angola | Golden missing | CAS-Tour-End-Same-Day | **KEEP** | – | – |
| **2025-06-01** | `126533 PU` | FRA→CPH→GOT, GOT, overnight=T, **duty=1084 > FTL** | – | – | Z76 44€ tour_start Schweden | Golden missing + FTL-Warning | **CAS belegt klar**: PU+route+layover+overnight (Phase-E 3-source override) | **KEEP** | – | – |
| **2025-06-02** | `126533 PU` | GOT→FRA→SOF, SOF, overnight=T, **duty=1189 > FTL** | – | – | Z76 22€ tour_mid Bulgarien | Golden missing + FTL-Warning | CAS-Continuation (Day-Suffix Day-2) | **KEEP** | – | – |
| **2025-06-03** | `126533 PU` | SOF→FRA→LHR, –, overnight=F, duty=465 | – | – | Z76 22€ tour_mid Bulgarien | Golden missing | CAS-Tour-End-Same-Day | **KEEP** | – | – |
| **2025-10-26** | `32935 PU` | FRA→TLV, TLV, overnight=T, duty=449 | – | – | Z76 44€ tour_start Israel | Golden missing | **CAS belegt klar**: PU+route+layover+overnight+duty<FTL | **KEEP** | – | – |
| **2025-10-27** | `X` | TLV, TLV, overnight=T, duty=0 (layover-OFF-Day mid-tour) | – | – | Z76 66€ tour_mid Israel | – | Layover-OFF während echter Tour (Sandwich-Repair korrekt) | **KEEP** | – | – |
| **2025-10-28** | `32935 PU` | TLV→FRA, –, overnight=F, duty=280 (return) | – | – | Z76 44€ tour_end Israel | – | CAS-Tour-End | **KEEP** | – | – |
| **2025-11-18** | `==` | – | – | Z76 Norwegen | Z73 tour_start (phantom anreise) | **NO CAS evidence** | (none) | **DROP** | gering | −Z73 −1 fahrtag |
| **2025-11-19** | `==` | – | – | – | Z76 75€ tour_mid Norwegen | **NO CAS evidence + Golden missing** | (none) | **DROP** | gering | −Z76 75€ −1 arbeitstag |
| **2025-12-15** | `57783 P1 Tag 2` | JFK→FRA, JFK, overnight=T, duty=184 | – | – | Z76 66€ tour_mid USA-NY | Golden missing 12-15 | **CAS belegt klar**: P1 + Day-Suffix + JFK layover + overnight | **KEEP** | – | – |
| **2025-12-16** | `X` | FRA, –, overnight=F, **duty=455 (8h activity am Hb)** | – | – | Z76 66€ tour_mid USA-NY | Pipeline klass falsch | CAS belegt FRA-Day (return-day office?) – kein TLV/JFK mehr | **REVIEW** (currently DROP-empfohlen — klass-Falsch, sollte Z72 inland-Same-Day oder Office) | mittel | −Z76 66€ +Z72 14€ |
| **2025-07-24** | `== OFF` | – | – | – | Z76 66€ tour_mid Schweden | **NO CAS evidence + Golden missing** | (none) | **DROP** | gering | −Z76 66€ −1 arbeitstag |
| **2025-03-22** | `83343 PU` | FRA→TOS, –, overnight=F, duty=510 (8.5h Same-Day) | – | – | Z76 50€ same_day Norwegen | Golden missing | **CAS belegt klar**: PU + foreign route + duty≥480 | **KEEP** | – | – |
| **2025-07-01** | `129023 PU / Tag 1` | FRA→STR→NAP→OTP, OTP, overnight=T, duty=1099 | **SE=BUH** | **Z76 pos 2/3** | Z76 21€ tour_start Rumänien | – | **CAS+SE+Golden alle ja** | **KEEP** | – | – |

## §2 Cluster-Summary

| Cluster | Tage | Decision | Begründung |
|---|---:|---|---|
| **Angola-Tour (05-20 bis 05-23)** | 4 | KEEP | CAS hat starke 4-Tage-Evidence; Golden vermisst sie. AeroTAX bleibt CAS-conform. **documented_reference_disagreement Golden-Gap**. |
| **Skandi-Bulg-Tour (06-01/02/03)** | 3 | KEEP | CAS belegt klar trotz duty>FTL (Phase-E 3-source-override greift); Golden vermisst. **documented_reference_disagreement**. |
| **Israel-Tour (10-26/27/28)** | 3 | KEEP | CAS belegt klar (TLV+layover+overnight+duty<FTL); Golden vermisst. **documented_reference_disagreement**. |
| **USA-NY-Tour (12-14 Anreise → 12-15 Day 2)** | 1 KEEP, 1 REVIEW | 12-15 KEEP, 12-16 REVIEW | 12-15 hat Day-2-Marker + JFK-layover (echt). 12-16 X+FRA+duty=455 (return-day, kein Z76). **12-16 needs_review oder klass→Office/Z72**. |
| **TOS-Same-Day (03-22)** | 1 | KEEP | CAS-Same-Day-Foreign-Tour belegt durch P1+routing+duty. **documented Golden-Gap**. |
| **Bukarest (07-01)** | 1 | KEEP | Golden bestätigt sogar diesen Tag. War nur als „Extra-Fahrtag" gelistet weil pos in Golden Tour anders. |
| **Phantom-Extensions (11-18, 11-19, 07-24)** | 3 | **DROP** | KEIN CAS-Marker mit Tour-Bedeutung. KEIN routing. KEIN layover. KEIN overnight. KEIN duty. KEIN SE. Pipeline-Phantom durch Standby-Activation-Chain oder Tour-Extension. |

## §3 Erwarteter KPI-Effekt aus echtem Phantom-DROP (3 Tage)

| Tag | Phantom-Klass | Phantom-EUR | Wird zu | Effekt |
|---|---|---:|---|---|
| 2025-11-18 | Z73 tour_start | 14 | Frei (CAS-conform) | −1 fahrtag, −14€, −1 arbeitstag (aber 11-20 echte Tour wird dann counted_fahrtag=True!) |
| 2025-11-19 | Z76 tour_mid Norwegen | 75 | Frei | −1 arbeitstag, −1 hotel, −75€ z76 |
| 2025-07-24 | Z76 tour_mid Schweden | 66 | Frei | −1 arbeitstag, −1 hotel, −66€ z76 |

**Total DROP-Effekt**: −3 arbeitstage, −2 hotel, −1 fahrtag, −155€ z76, +1 fahrtag (11-20 freigegeben) = **netto: −3 arbeitstage, −2 hotel, 0 fahrtag, −155€ z76**.

Plus **12-16 REVIEW**: −66€ z76 (wenn Z76→Z72 14€) = +14€ z72 → netto −52€ z76, +1 z72.

**Gesamt-Phantom-Reduktion**: −207€ z76.

## §4 Was BLEIBT als documented_reference_disagreement

Die echten CAS-Tour-Tage die Golden vermisst (Pipeline KEEP, aber Golden-Acceptance würde rote KPIs liefern):

| Tour | Tage | EUR | Begründung |
|---|---:|---:|---|
| Angola | 4 | 134 | CAS hat klare P1+routing+layover+overnight+duty |
| Skandi+Bulg | 3 | 88 | CAS+Day-Suffix+routing |
| Israel TLV | 3 | 154 | CAS+routing+layover+overnight |
| USA-NY (12-15) | 1 | 66 | CAS+Day-Suffix+JFK-layover |
| TOS Same-Day | 1 | 50 | CAS-Same-Day-Foreign |

**Total Golden-Missing-Real-Tours**: 12 Tage, **492€** z76 die KEEP bleiben.

Diese müssen als **documented_reference_disagreement** akzeptiert werden — KEIN Pipeline-Bug.

## §5 Pipeline-aktueller-Z76 → Erwarteter Z76 nach Fix-10

```
Pipeline aktuell:  5484€
− Phantom-DROP:    −155€ (11-18 14€, 11-19 75€, 07-24 66€)
− 12-16 REVIEW:    −52€  (Z76 66€ → Z72 14€)
= Erwartet:        5277€
Golden:            4794€
Δ erwartet:        +483€
Tol:               ±150€
Status nach Fix 10 alleine: RED (immer noch +483€ über)
```

→ Fix 10 alleine reicht NICHT für z76 grün. **Plus Low-Risk-Fixes 1-9 sind nötig.**

## §6 Phantom-Removal Regel-Entwurf (defensive Evidence-Regel)

```
PHANTOM-DROP-Regel: 
  Tag T wird zu non_tour (klass=Frei) demoted wenn:
    1. role[T] ∈ {'tour_mid', 'tour_start'}
    AND
    2. Marker (uppercase, stripped) ∈ {'', '==', 'OFF', '== OFF', '=', '/-', 'OF'}
    AND
    3. routing-Liste ist leer ODER == ['<homebase>']
    AND
    4. layover_ort ist leer
    AND
    5. overnight_after_day (ORIGINAL CAS-Wert, nicht synthetic) ist False
    AND
    6. duty_duration_minutes < 60  (kein realer Dienst)
    AND
    7. SE-Stempel-count == 0 ODER SE.stfrei_ort leer
    AND
    8. NICHT zwischen zwei echten Tour-Tagen mit prev.layover_ort UND next.layover_ort != homebase
       (= verhindert echte Layover-OFF-Days zu droppen)
```

Generalisierbar: keine Datumsliste, keine Tibor-Hardcoding. Pattern allein aus CAS-Markern + Quellen-Evidenz.

## §7 Tests vor Implementierung

| Test | Erwartung |
|---|---|
| `test_phantom_11_18_drop` | 11-18 wird Frei (war Z73 phantom) |
| `test_phantom_11_19_drop` | 11-19 wird Frei (war Z76 Norwegen phantom) |
| `test_phantom_07_24_drop` | 07-24 wird Frei (war Z76 Schweden phantom) |
| `test_angola_05_20_to_23_kept` | Alle 4 Tage bleiben Z76/Z73 Angola |
| `test_skandi_06_01_to_03_kept` | Alle 3 Tage bleiben Z76 trotz duty>FTL |
| `test_tlv_10_26_to_28_kept` | Alle 3 Tage bleiben Z76 Israel |
| `test_jfk_12_15_kept` | 12-15 bleibt Z76 (Day-2-Marker mit JFK-layover) |
| `test_jfk_12_16_review_or_office` | 12-16 wird Office/Z72 (kein Z76 mehr) |
| `test_bangalore_01_03_to_06_kept` | Bangalore-Tour unangetastet (real with SE+CAS) |
| `test_x_layover_off_kept` | 01-20 X HKG layover-off-day bleibt tour_mid Z76 |
| `test_res_hotel_tour_kept` | 04-23/24/25/26 RES Korea Tour bleibt |
| `test_no_double_count` | Counter stimmt mit tage_detail.klass-Sum überein |
| `test_no_date_hardcoding` | Code enthält keine Tibor-Spezifischen Daten |

## §8 Entscheidung für Phase 2 (Code)

Implementiere defensive Evidence-Regel aus §6.
Tests aus §7.
Erwarteter Effekt: **3 echte Phantoms gedropped, alle echten Touren bleiben.**
KPIs verbessern sich, aber Phase 2 (Fix 10) alleine reicht nicht → Fix 1-9 müssen folgen.
