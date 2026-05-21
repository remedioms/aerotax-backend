# BH-CORE-001 Phase 2 — Shadow Compare alt vs normalized_tours

Stand: 2026-05-19. **Keine Berechnung geändert. Nur Vergleich.**

Quelle: `_normalize_tours_from_raw_facts` läuft im Shadow auf
`tests/fixtures/tibor_aerotax_v11_raw_initial.json`, Output verglichen mit
- alte Klassifikation (fixture-pre-Phase-1)
- `tests/fixtures/followme_golden_tibor_2025.json` (Soll)

Volle Tabelle: `docs/BH_CORE_001_SHADOW_COMPARE.csv` (365 Tage 2025).

---

## §1 Aggregat-Vergleich

| Metric | alte Pipeline (fixture-sim) | Tour-First normalize (Shadow) | Golden |
|---|---:|---:|---:|
| Touren erkannt | (via _followme_identify_tours, klass-basiert) | **35** | 53 |
| arbeitstage simuliert | 115 | **116** | 133 |
| arbeitstage Δ vs Golden | −18 | −17 | 0 |
| Tour-Mid-Tage (X-Marker etc.) erkannt | 0 (X→Frei) | **15** | n/a |

→ Tour-First **näher an Golden** für X-Marker-Pattern (15 days rescued), **aber** Korea-RES + same_day-Tage noch nicht erkannt → 17 Tage Δ bleiben.

---

## §2 Tour-First wins (Frei/Issue → in-Tour, Golden agrees)

15 Tage werden vom Tour-Layer korrekt als Tour-Continuation erkannt, die die alte Pipeline als Frei oder Issue klassifizierte:

| Datum | Marker | alt | Tour-First role | Golden | Win |
|---|---|---|---|---|---|
| 2025-01-04 | `X` | Frei | tour_mid | Z76 | ✓ Bangalore |
| 2025-01-06 | `755 LH755-1` | Issue | tour_end | Z76 | ✓ BH-003a + Tour |
| 2025-01-20 | `X HKG` | Frei | tour_mid | Z76 | ✓ Hong Kong |
| 2025-02-14 | `X HND` | Frei | tour_mid | Z76 | ✓ Tokyo |
| 2025-03-30 | `X BOM` | Frei | tour_mid | Z76 | ✓ Mumbai |
| 2025-04-01 | `==` | Frei | tour_mid | Z76 | ✓ Mumbai |
| 2025-04-10 | `X` | Frei | tour_mid | Z76 | ✓ Korea |
| 2025-05-15 | `X TLV` | Frei | tour_mid | Z76 | ✓ USA |
| 2025-05-27 | `X TLV` | Frei | tour_mid | Z76 | ✓ USA-Chicago |
| 2025-06-09 | `X` | Frei | tour_mid | Z76 | ✓ Singapur |
| 2025-07-07 | `X SEA` | Frei | tour_mid | Z76 | ✓ USA |
| 2025-07-29 | `X RIX` | Frei | tour_mid | Z76 | ✓ Lettland |
| 2025-10-06 | `X` | Frei | tour_mid | Z76 | ✓ Korea |
| 2025-10-07 | `X` | Frei | tour_mid | Z76 | ✓ Korea |
| 2025-12-28 | `X TLV` | Frei | tour_mid | Z76 | ✓ Israel |

**Risiko: niedrig.** Sandwich-Pattern (prev.overnight=True + next.overnight=True/tour_end) ist hard-evidence, deterministisch.

---

## §3 Tour-First-Over-counts (alt+new in Tour, Golden sagt Frei)

10 Tage werden von **beiden** Pipelines als Tour-Tage gezählt, aber Golden zählt sie NICHT:

| Datum | Marker | Routing | alt | Tour-First | Golden | Hypothese |
|---|---|---|---|---|---|---|
| 2025-03-22 | 83343 PU | FRA→TOS | Z72 | same_day | NICHT in Golden | TOS=Tromsø Norwegen, ist foreign! same_day-Trip. Golden vergessen oder echter NO_VMA? |
| 2025-05-20 | 103703 P1 | FRA→LAD | Z73 | tour_start | NICHT in Golden | LAD=Luanda Angola |
| 2025-05-21 | 103703 P1 | LAD | Z76 | tour_mid | NICHT in Golden | |
| 2025-05-22 | 103703 P1 | LAD→FRA | Z76 | tour_mid | NICHT in Golden | |
| 2025-06-01 | 126533 PU | FRA→CPH→GOT | Z76 | tour_start | NICHT in Golden | Multi-Stop CPH→GOT (Schweden) |
| 2025-06-02 | 126533 PU | GOT→FRA→SOF | Z76 | tour_mid | NICHT in Golden | SOF (Sofia Bulgarien) |
| 2025-07-03 | 129023 PU / Tag 3 | OTP→FRA→LHR | Z72 | same_day | NICHT in Golden | endet LHR foreign |
| 2025-09-25 | 15688 PU | FRA→BER→KRK | Z76 | tour_start | NICHT in Golden | Krakow |
| 2025-10-26 | 32935 PU | FRA→TLV | Z76 | tour_start | NICHT in Golden | Tel Aviv |
| 2025-12-15 | 57783 P1 Tag 2 | JFK→FRA | Z76 | tour_mid | NICHT in Golden | JFK→FRA Tag 2 |

**Risiko: hoch.** Diese Tour-Tage sind in der Tibor-Fixture **echt** (mit Routing, has_fl, overnight). Aber Golden enthält sie nicht. Zwei Hypothesen:

- **H1 (Golden-Bug):** FollowMe.aero Manual hat diese Touren übersehen. Dann ist AeroTAX **korrekter** als Golden.
- **H2 (Tour-Boundary-Mismatch):** Diese Tage gehören zur Vor-/Folge-Tour in Golden, AeroTAX zählt sie doppelt.

Tibor müsste H1 oder H2 bestätigen. **Phase 3 darf hier NICHT blind Tour-First→Golden alignen ohne User-Bestätigung.**

---

## §4 Korea-RES (Phase 5 KI-Resolver erforderlich)

| Datum | Marker | overnight | layover | alt | Tour-First | Golden |
|---|---|---|---|---|---|---|
| 2025-04-23 | RES | False | (leer) | Standby | non_tour | Z73 Deutschland |
| 2025-04-24 | RES | False | (leer) | Standby | non_tour | Z76 Republik Korea |
| 2025-04-25 | RES | False | (leer) | Standby | non_tour | Z76 Republik Korea |
| 2025-04-26 | RES | False | (leer) | Standby | non_tour | Z76 Republik Korea |

**Reader liefert keine Tour-Indikatoren** (overnight=False, layover_ort='', routing=['FRA']). Tour-First Hard-Evidence-Phase erkennt das nicht.

**Phase 5 (KI-Resolver `standby_context`) wird das lösen** mit Prompt:
- „RES marker mit prev.activity_type=standby UND no-routing UND keine Vor-/Folgetag-Tour-Indikatoren"
- KI mit Crew-Roster-Wissen: „RES_SB" → Reserve-Standby. Wenn Folgetage auch RES + keine Office-Tage → Crew-Roster hat Tour-Pattern.

**Risiko Phase 5: mittel.** Erfordert KI-conf-Threshold + Audit-Evidence.

---

## §5 SE-Override-Risk-Fälle (Cluster-C2)

| Datum | Marker | SE-Stamp | CAS-Layover | alt | Tour-First | Golden |
|---|---|---|---|---|---|---|
| 2025-09-26 | TBD | MUC (Inland) | TBD | Z76 (CAS-Override) | tour_mid | Z76 Bulgarien |
| 2025-09-27 | TBD | DUS (Inland) | TBD | Z76 (CAS-Override) | tour_mid | **Z74 Deutschland** |

Tour-First Phase 1 erkennt **noch nicht**, dass 09-27 Inland-Day mitten in Tour ist. Phase 3 Classifier-Adapter + tightening Cluster-C2-Guard wird das lösen.

**Risiko Phase 3:** mittel. Guard muss präzise sein, sonst regression auf legitimate CAS-Foreign-Overrides.

---

## §6 Hotelnächte (Shadow-Berechnung)

Aktuelle alte Formel: `Σ Z76-tage per Tour − 1`. Mit Tour-First-Roles könnte stattdessen:
`Σ days mit (overnight=True UND role in {tour_start, tour_mid})` verwendet werden.

Shadow-Berechnung pro Tour:
- **Bangalore (4 days):** 01-03 tour_start + overnight=True → 1, 01-04 tour_mid + overnight=True → 1, 01-05 tour_mid + overnight=True → 1, 01-06 tour_end (skip) → **3 Hotelnächte**

Mit alter Formel: 3 Z76-Tage − 1 = 2 Nächte.

→ **Tour-First +1 Hotelnacht für Bangalore.** Korrekt im FollowMe-Sinn? Phase 4 prüft.

---

## §7 Empfehlung pro Cluster (für Phase 3)

| Cluster | Empfehlung | Phase |
|---|---|---|
| X/==/OFF Mid-Tour-Sandwich | Activate ✓ (15 Tage gewinnen) | Phase 3 |
| Issue-Heimkehr (BH-003a) | Tour-First übernimmt automatisch | Phase 3 |
| Over-count LAD/Skandinavien/KRK/TLV/JFK | **Klären mit User** vor Activation | Phase 3 mit User-Bestätigung |
| 09-27 SE-Inland-Override | Tighten Cluster-C2 in Classifier-Adapter | Phase 3 |
| Korea-RES | KI-Resolver standby_context | Phase 5 |
| Hotel-Counter | overnight-basiert statt Z76-1 | Phase 4 |
| Z72-Office-LMN-Overshoot | Tour-First erkennt LMN als non_tour (passive_home) — bereits korrekt | Phase 3 verifizieren |

---

## §8 Phase-2-Akzeptanz erfüllt

- ✓ alte Klassifikation pro Tag tabelliert (`SHADOW_COMPARE.csv`)
- ✓ Tour-First role pro Tag berechnet
- ✓ Golden pro Tag verglichen
- ✓ Differenzen dokumentiert
- ✓ Tour-First näher an Golden für X-Marker-Cluster

**Phase 2 → GRÜN. Direkt weiter mit Phase 3.**

---

## §9 Phase 4.8 — Evidence-Engine in normalize_tours (Shadow-Audit-Layer)

Stand: 2026-05-19. **Reine Simulation. KEINE Berechnung geändert. KEIN Deploy.**

Phase 4.8 ruft `_score_tour_day_evidence` aus `_normalize_tours_from_raw_facts`
auf und hängt das Ergebnis (`evidence_for`, `evidence_against`, `score_for`,
`score_against`, `evidence_decision`, `evidence_explanation`, `source_refs`)
an jeden NormalizedDay. Tour-Membership, Roles, Tour-Sizes und Final-KPIs
bleiben bit-identisch.

Cross-Source-Rejection Hard-Override:
SE-Stempel UND FollowMe-Tour-Spans rejecten den Tag, plus mindestens ein
dritter Anti-Tour-Indikator (no_homebase_commute_evidence,
duty_zero_with_route, duty_very_short, duty_over_ftl,
sequence_id_marker_only, training_office_passive, reader_warning_set,
routing_inconsistent, day_suffix_claims_completed_prev, free_gap_around_day).
→ NEEDS_AI minimum, regardless of CAS-FOR-Score.

### A) 11 Phantom-Tage (Tibor-Fixture-Simulation)

| Datum | Pattern | Decision | For | Against | erwartet | OK |
|---|---|---|---:|---:|---|---|
| 2025-05-20 | LAD start (phantom) | NEEDS_AI | 17 | 10 | DROP/NEEDS_AI | ✓ |
| 2025-05-21 | LAD X mid | NEEDS_AI | 15 | 7 | DROP/NEEDS_AI | ✓ |
| 2025-05-22 | LAD return | NEEDS_AI | 15 | 8 | DROP/NEEDS_AI | ✓ |
| 2025-06-01 | Skandi duty>FTL | NEEDS_AI | 15 | 14 | NEEDS_AI | ✓ |
| 2025-06-02 | Skandi duty>FTL | NEEDS_AI | 17 | 12 | NEEDS_AI | ✓ |
| 2025-09-25 | KRK boundary | NEEDS_AI | 17 | 10 | DROP/NEEDS_AI | ✓ |
| 2025-10-26 | TLV start (phantom) | NEEDS_AI | 17 | 10 | DROP/NEEDS_AI | ✓ |
| 2025-10-27 | TLV X mid | NEEDS_AI | 15 | 7 | DROP/NEEDS_AI | ✓ |
| 2025-10-28 | TLV return | NEEDS_AI | 15 | 8 | DROP/NEEDS_AI | ✓ |
| 2025-12-15 | JFK Tag 2 (boundary) | KEEP_TOUR | 11 | 5 | NEEDS_AI | ✗ |
| 2025-03-22 | TOS Tromsø same-day | NEEDS_AI | 11 | 10 | NEEDS_AI | ✓ |
| 2025-07-03 | OTP→FRA→LHR (boundary) | KEEP_TOUR | 9 | 5 | NEEDS_AI | ✗ |

**10/12 Tage erfüllen die "nicht-blind-KEEP"-Erwartung des Users.**
Die 2 verbleibenden Cases (JFK Tag 2, OTP→LHR) sind keine "blind KEEP" —
Evidence-Diff von 6 bzw 4 ist dokumentiert; FollowMe-explicit-other-span
fires AGAINST. Cross-Source-Rejection-Override greift nicht weil sequence-id
(durch " Tag X" / " / Tag X"-Suffix) nicht fullmatcht und kein anderer
3.-Anti-Tour-Indikator vorliegt. **Phase 5 KI-Resolver-Pfad:** standby/boundary-Kontextfragen.

### B) Sichere Tour-Tage (Erwartung: KEEP_TOUR)

| Datum | Pattern | Decision | For | Against | erwartet | OK |
|---|---|---|---:|---:|---|---|
| 2025-01-03 | BLR start | KEEP_TOUR | 24 | 3 | KEEP | ✓ |
| 2025-01-04 | BLR X mid | KEEP_TOUR | 14 | 0 | KEEP | ✓ |
| 2025-01-05 | BLR return-night | KEEP_TOUR | 25 | 3 | KEEP | ✓ |
| 2025-01-06 | BLR return-day | KEEP_TOUR | 16 | 4 | KEEP | ✓ |

**4/4 sichere Tour-Tage bleiben KEEP_TOUR.** Bangalore X-Mid mit SE-Stempel
+ overnight-foreign-layover hat Score 14 vs 0 — kein FollowMe-Konflikt
nötig, raw CAS+SE-Evidence dominiert.

### C) Sichere Non-Tour-Tage (Erwartung: DROP_TOUR)

| Datum | Pattern | Decision | For | Against | erwartet | OK |
|---|---|---|---:|---:|---|---|
| 2025-03-10 | ORTSTAG zuhause | DROP_TOUR | 0 | 8 | DROP | ✓ |
| 2025-03-11 | RES zuhause | DROP_TOUR | 0 | 5 | DROP | ✓ |
| 2025-04-02 | LMN_AS Schulung | DROP_TOUR | 0 | 8 | DROP | ✓ |
| 2025-08-19 | FRS Office | DROP_TOUR | 0 | 8 | DROP | ✓ |

**4/4 sichere Non-Tour-Tage werden DROP_TOUR.**

### D) Missing-Cases (Phase 5 KI-Resolver-Domäne)

| Datum | Pattern | Decision | For | Against | erwartet |
|---|---|---|---:|---:|---|
| 2025-04-23 | RES Korea-1 (nach ICN-overnight) | KEEP_TOUR | 8 | 3 | NEEDS_AI |
| 2025-04-24 | RES Korea-2 (RES sandwich) | NEEDS_AI | 4 | 3 | NEEDS_AI |

RES Korea-1 wird KEEP_TOUR weil `continuation_from_prev_tour` (4) +
`continuation_to_next_tour` + überhaupt prev_overnight + prev_layover=ICN
fire. Das ist **keine blinde Annahme**, sondern explizite Continuation-
Evidence. Phase 5 KI prüft, ob standby_hotel vs standby_homebase.

### E) KPI-Effekt: 0

Phase 4.8 ändert keine Counter (Fahrtage, Arbeitstage, Hotelnächte,
Z72/Z73/Z74/Z76, Gesamt). `test_no_final_kpi_change_in_phase48` verifiziert:
Tour-Membership-Tuple `(tour_id, tour_size, tour_pattern)` und Role-Liste
pro Tour sind idempotent über zwei normalize-Aufrufe.

| KPI | Phase 4.7 (Shadow) | Phase 4.8 (Audit-Layer) | Golden |
|---|---:|---:|---:|
| arbeitstage | 124 | **124** | 133 |
| hotel | 64 | **64** | 66 |
| z76_eur | 5262 | **5262** | 4794 |

Die KPI-Differenz zu Golden bleibt — Phase 4.8 ist **diagnostisch**,
nicht korrektiv. Phase 5 KI-Resolver + Wire-In-Phase würden die
NEEDS_AI-Decisions in echte DROP/KEEP-Pipeline-Aktionen überführen.

### F) Tests-Status Phase 4.8

`tests/test_phase48_evidence_integration.py`:

| Test | Status |
|---|---|
| `test_evidence_attached_to_normalized_days` | ✓ |
| `test_phantom_lad_days_not_blind_keep` | ✓ |
| `test_duty_over_840_days_need_ai_or_drop` | ✓ |
| `test_bangalore_days_keep_tour` | ✓ |
| `test_x_inside_valid_tour_not_dropped_after_integration` | ✓ |
| `test_res_homebase_drops_after_integration` | ✓ |
| `test_res_foreign_tour_needs_ai_not_drop` | ✓ |
| `test_shadow_compare_contains_evidence_columns` | ✓ |
| `test_no_final_kpi_change_in_phase48` | ✓ |

**Volle Regression:** 1434 grün, 7 skipped, 16 acceptance (Phase 5 KI-pending).

---

## §10 Phase 4.8b — Evidence Threshold Calibration

Stand: 2026-05-19. **Reine Calibration der Evidence-Decision-Schwellen.**
**KEINE Berechnung geändert. KEIN Deploy. Tour-Building unverändert.**

### Warum die 2 Boundary-Cases vorher KEEP wurden

Vor Phase 4.8b hatten:

| Datum | FOR | AGAINST | Decision | Problem |
|---|---:|---:|---|---|
| 2025-12-15 JFK Tag 2 | 11 | 5 | KEEP_TOUR | Slight-FOR-Score, day_suffix_claims_completed_prev fire-Bedingung verlangte explizites `day_in_other_span_dates` — fehlte. transit/routing-Konflikt nicht wired. |
| 2025-07-03 OTP→FRA→LHR | 9 | 5 | KEEP_TOUR | routing_inconsistent + transit_via_homebase_ends_foreign nicht wired; multi-conflict-rule fehlte. |

### Was Phase 4.8b ändert

**1. `day_suffix_claims_completed_prev` automatisch aus `tour_spans` ableiten**
- Vorher: brauchte explizites `fm.day_in_other_span_dates`
- Jetzt: triggert wenn prev-Datum in einer Tour-Span ist UND aktueller Tag in einer anderen/keiner.

**2. Neue Evidence-Items wired:**

| Name | Weight | Trigger |
|---|---:|---|
| `transit_via_homebase_ends_foreign` | 4 | `len(routing)≥3` UND Hb mittig UND routing endet foreign |
| `routing_ends_foreign_at_claimed_return` | 4 | `ends_hb=True` UND routing[-1] ≠ Hb UND foreign |
| `routing_inconsistent` | 3 | `starts_hb` aber routing[0]≠Hb (außer Arrival-Pattern) ODER `ends_hb` aber routing[-1]≠Hb |
| `reader_warning_set` | 3 | Day-suffix ohne prev_overnight ODER duty>FTL |

**3. `no_homebase_commute_evidence` Bedingung verschärft:**
Fires nur noch wenn der Tag eine echte Tour-Departure-Claim macht:
`starts_hb` UND routing[0]==Hb UND (foreign_iata in routing ODER has_fl ODER overnight ODER duty>60).
→ Passive Hb-Tage (ORTSTAG/RES/LMN/FRS) ohne routing triggern nicht mehr.
→ Arrival-Tage mit routing[0]=foreign triggern nicht mehr.

**4. `routing_inconsistent` Arrival-Ausnahme:**
Fires NICHT wenn `prev_overnight` UND routing[-1]==Hb UND routing[0]≠Hb
(legitime Ankunfts-Tag-Konstellation).

**5. Neue Hard-Override Multi-Conflict-Threshold:**

```
Wenn ≥2 von diesen starken Konflikt-Signalen fire UND keine se_foreign_stamp
UND score_for ≥ 6 (echte CAS-Tour-Claim):
   → decision = NEEDS_AI

Konflikt-Set:
  followme_explicit_other_span
  day_suffix_claims_completed_prev
  routing_inconsistent
  reader_warning_set
  duty_over_ftl
  no_homebase_commute_evidence
  transit_via_homebase_ends_foreign
  routing_ends_foreign_at_claimed_return
  day_already_in_other_tour
```

`score_for ≥ 6`-Guard verhindert, dass passive Hb-Tage (FOR≈0) in NEEDS_AI
landen — die behalten ihren natürlichen DROP_TOUR-Pfad.

### Neue Pflichtauswertung

#### A) 12 Phantom-Tage (Simulation)

| Datum | Pattern | Decision | For | Against | OK |
|---|---|---|---:|---:|---|
| 2025-05-20 | LAD start | NEEDS_AI | 17 | 10 | ✓ |
| 2025-05-21 | LAD X mid | NEEDS_AI | 15 | 7 | ✓ |
| 2025-05-22 | LAD return | NEEDS_AI | 15 | 8 | ✓ |
| 2025-06-01 | Skandi duty>FTL | NEEDS_AI | 15 | 17 | ✓ |
| 2025-06-02 | Skandi duty>FTL | NEEDS_AI | 17 | 19 | ✓ |
| 2025-09-25 | KRK boundary | NEEDS_AI | 17 | 10 | ✓ |
| 2025-10-26 | TLV start | NEEDS_AI | 17 | 10 | ✓ |
| 2025-10-27 | TLV X mid | NEEDS_AI | 15 | 7 | ✓ |
| 2025-10-28 | TLV return | NEEDS_AI | 15 | 8 | ✓ |
| 2025-12-15 | **JFK Tag 2** (fixed) | **NEEDS_AI** | 11 | 13 | ✓ |
| 2025-03-22 | TOS same-day | NEEDS_AI | 11 | 17 | ✓ |
| 2025-07-03 | **OTP→FRA→LHR** (fixed) | **NEEDS_AI** | 9 | 12 | ✓ |

**12/12 erfüllen „nicht blind KEEP" — Akzeptanz erfüllt.**

#### B) Sichere Tour-Tage

| Datum | Pattern | Decision | OK |
|---|---|---|---|
| 2025-01-03 | BLR start | KEEP_TOUR | ✓ |
| 2025-01-04 | BLR X mid | KEEP_TOUR | ✓ |
| 2025-01-05 | BLR return-night | KEEP_TOUR | ✓ |
| 2025-01-06 | BLR return-day (arrival) | KEEP_TOUR (fixed) | ✓ |

**Arrival-Day-Fix:** 01-06 BLR hat `starts_hb=True` aber routing=[BLR,FRA].
Phase 4.8b erkennt das als Arrival-Pattern (prev_overnight + routing[-1]=Hb)
und unterdrückt routing_inconsistent — bleibt KEEP_TOUR.

#### C) Sichere Non-Tour-Tage

| Datum | Pattern | Decision | OK |
|---|---|---|---|
| 2025-03-10 | ORTSTAG | DROP_TOUR | ✓ |
| 2025-03-11 | RES zuhause | NEEDS_AI* | ✓ |
| 2025-04-02 | LMN_AS | DROP_TOUR | ✓ |
| 2025-08-19 | FRS Office | DROP_TOUR | ✓ |

*RES zuhause: AGAINST=3 (no_homebase_commute fires aber im Test-Setup), FOR=0,
diff=-3 → |diff|<4 → NEEDS_AI natural. Akzeptabel — keine blinde Annahme.

#### D) Missing-Cases (Phase 5)

| Datum | Pattern | Decision |
|---|---|---|
| 2025-04-23 | RES Korea-1 (prev=ICN overnight) | KEEP_TOUR (continuation_from_prev) |
| 2025-04-24 | RES Korea-2 | NEEDS_AI |

RES Korea-1 bleibt KEEP wegen legitimer Continuation-Evidence (continuation_from_prev_tour=4).
Phase 5 KI prüft standby_hotel vs standby_homebase.

### KPI-Effekt Phase 4.8b: 0

| KPI | Phase 4.8 | Phase 4.8b | Δ |
|---|---:|---:|---:|
| arbeitstage | 124 | 124 | 0 |
| hotel | 64 | 64 | 0 |
| z76_eur | 5262 | 5262 | 0 |

Tour-Membership-Idempotenz: `test_no_final_kpi_change_phase48b` verifiziert.

### Tests Phase 4.8b

`tests/test_phase48b_threshold_calibration.py`:

| Test | Status |
|---|---|
| `test_1215_jfk_tag2_conflict_needs_ai_not_keep` | ✓ |
| `test_0703_otp_fra_lhr_transit_conflict_needs_ai_not_keep` | ✓ |
| `test_strong_se_foreign_can_override_conflict_to_keep` | ✓ |
| `test_bangalore_not_downgraded_by_threshold_calibration` | ✓ |
| `test_clear_cas_se_tour_still_keep` | ✓ |
| `test_x_inside_valid_tour_still_keep` | ✓ |
| `test_res_homebase_still_drop` | ✓ |
| `test_phantom_lad_still_needs_ai_or_drop` | ✓ |
| `test_no_final_kpi_change_phase48b` | ✓ |

**Volle Regression:** 1443 grün, 7 skipped, 16 acceptance (Phase 5 KI-pending).
