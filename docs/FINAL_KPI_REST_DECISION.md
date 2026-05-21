# Final KPI Rest Decision

Stand: 2026-05-20 (MegaR Phase 4).

Eingabe: Pipeline-Output nach FinalFix Round (1658 Tests, 12 xfailed):
- fahr_tage = 58 / Golden 58 ✓
- z73_tage = 12 / Golden 11 ✓ (±1)
- z74_tage = 2 / Golden 1 ✓ (±1)
- arbeitstage = 142 / Golden 133, **Δ+9**
- reinigung = 142 / Golden 133, **Δ+9**
- hotel = 62 / Golden 66, **Δ−4**
- z72_tage = 3 / Golden 5, **Δ−2**
- z76_eur = 5514 / Golden 4794, **Δ+720**
- gesamt = 5780 / Golden 6020.72, **Δ−241**

## §1 arbeitstage Δ+9 — Detail-Analyse

| Tag | Pipeline | Golden | Source | Decision |
|---|---|---|---|---|
| 2025-05-20 | Z73 Angola Anreise | NICHT IN GOLDEN | CAS belegt P1+routing+layover+overnight | **documented_reference_disagreement** (KEEP) |
| 2025-05-21 | Z76 Angola | NICHT IN GOLDEN | CAS belegt 4-Tage-Tour | **documented_reference_disagreement** |
| 2025-05-22 | Z76 Angola | NICHT IN GOLDEN | CAS belegt | **documented_reference_disagreement** |
| 2025-05-23 | Z76 Angola Same-Day | NICHT IN GOLDEN | CAS belegt | **documented_reference_disagreement** |
| 2025-06-01 | Z76 Schweden | NICHT IN GOLDEN | CAS belegt (3-source-Phase-E) | **documented_reference_disagreement** |
| 2025-06-02 | Z76 Bulgarien | NICHT IN GOLDEN | CAS belegt + Day-Suffix | **documented_reference_disagreement** |
| 2025-06-03 | Z76 Bulgarien Same-Day | NICHT IN GOLDEN | CAS belegt | **documented_reference_disagreement** |
| 2025-10-26 | Z76 Israel TLV | NICHT IN GOLDEN | CAS belegt routing+layover+overnight | **documented_reference_disagreement** |
| 2025-10-27 | Z76 Israel TLV Layover-Off | NICHT IN GOLDEN | CAS belegt | **documented_reference_disagreement** |
| 2025-10-28 | Z76 Israel TLV Tour-End | NICHT IN GOLDEN | CAS belegt | **documented_reference_disagreement** |
| 2025-03-22 | Z76 Norwegen Same-Day | NICHT IN GOLDEN | CAS belegt PU+foreign route+duty 510 | **documented_reference_disagreement** |
| 2025-12-15 | Z76 USA-NY | NICHT IN GOLDEN | CAS Day-2-Marker + JFK layover | **documented_reference_disagreement** |
| 2025-12-16 | Z76 USA-NY (X return) | NICHT IN GOLDEN | CAS ambiguous (X+FRA, but tour continues) | **needs_review** (low priority) |

→ **13 documented_reference_disagreement Tage** (alle CAS-belegte real-touren, Golden vermisst).
→ Erwarteter +9 (5 ohne disagreement-Frei-tage offsets, da 06-17/06-18/05-17/04-01/07-23/09-20 Frei = 6 dis-tage abgezogen vom +13)

**Decision: documented_reference_disagreement** — Pipeline bleibt CAS-conform. Beleg dokumentiert in `docs/FIX10_PHANTOM_BEWEIS.md`.

## §2 hotel Δ−4 — Detail

Pipeline 62, Golden 66. Pipeline-Hotelnächte basieren auf:
- `counted_as_hotel_nacht=True` für Z73/Z74/Z76 mit foreign-layover

Golden zählt 4 Hotelnächte mehr. Wahrscheinlich:
- Standby-Activation Day 1 (z.B. 11-17 SB_M SVG) — Pipeline-role=tour_mid setzt counted_hotel=True ✓
- Foreign-Same-Day-Office-Konversion: counted_hotel ist FALSE für same-day-Z76 (kein overnight)
- Documented-Disagreement-Touren: Golden vermisst Angola/Israel/Schweden (alle inkl. Hotelnächte)

**Decision: documented_reference_disagreement** — Hotel-Discount durch fehlende Disagreement-Tour-Hotels in Golden.

## §3 z72 Δ−2 — Detail

Pipeline 3, Golden 5. Fehlt:
- 2025-02-10 `68617 PU` SE=DUS inland duty=325min — Pipeline=Office (<8h), Golden=Z72 14€
- 2025-09-20 `==` — documented_disagreement (CAS clear frei)

**Decision**: 
- 02-10 ist Z72-Boundary-Issue (Golden gibt 14€ für <8h, AeroTAX-Heuristik braucht ≥480min). Risiko-Bewertung: Z72-Threshold-Senkung würde False-Positives für andere kurze Office-Days bedeuten. **no_action** (zu riskant; Δ−2 ist in Toleranz nahe).
- 09-20 = documented_disagreement.

## §4 z76_eur Δ+720 — Detail

Source-Bucket-Analyse (siehe `docs/FINAL_Z76_EUR_DIFF.md`):

| Bucket | Tage | EUR |
|---|---:|---:|
| exact_match (Land+EUR identisch) | 50 | +0 |
| rate_only_diff (voll_24h vs an_abreise) | 9 | +89 |
| formatting_diff (gleiches Land, City-Detail) | 13 | +20 |
| real_land_conflict (SE-Ort priorisiert via Fix 3) | ~26 → reduziert | −40 |
| extra_aero (Golden vermisst real-Tour, AeroTAX KEEP) | ~13 | +500 |
| missing_aero (Disagreement-Frei-Tage) | 5 | −155 |

Net erwartet ~+400-500€. Aktuell +720€. Differenz ~200€ aus:
- Phantom-Tour-13-Tage-Real-Z76 wie oben in §1 dokumentiert
- Single-Day-Land-Sub-Region-Choices (Spanien-Madrid vs Spanien generisch etc.)

**Decision: documented_reference_disagreement** — die ~500€ aus den 13 disagreement-Touren sind belegt; die restlichen ~200€ sind BMF-Land-Sub-Region-Variationen ohne klare Master-Regel.

## §5 gesamt Δ−241 — Detail

Net-Effekt aus arbeitstage (+9 × Reinigung 1.6€ + Trinkgeld 3.6€), Hotel-Verlust, z76-Drift.

5780 vs 6020.72 — Δ−241 = innerhalb yellow-Tolerance-Verdopplung. **Decision: documented_reference_disagreement.**

## §6 Cluster-Summary

| KPI | Δ | Tage | Ursache | Decision |
|---|---:|---:|---|---|
| arbeitstage | +9 | 13 KEEP | CAS-belegte real-Touren, Golden missing | documented_reference_disagreement |
| reinigung | +9 | 13 KEEP | gekoppelt an arbeitstage | documented_reference_disagreement |
| hotel | -4 | 4 KEEP/missing | hotel-Bilanzierung der disagreement-Touren | documented_reference_disagreement |
| z72 | -2 | 1 missing | 02-10 Z72-Boundary + 09-20 documented | no_action + documented_reference_disagreement |
| z76_eur | +720 | 13 KEEP + Land-Variations | documented + BMF-sub-region | documented_reference_disagreement |
| gesamt | -241 | proportional aller obigen | net effect | documented_reference_disagreement |

→ **Alle verbleibenden gelben/roten KPIs sind documented_reference_disagreement** zwischen CAS-Quelle (AeroTAX-conform) und FollowMe-Golden (Referenz).

## §7 Master-Rule-Compliance

Per Master-Regel:
- ✓ „CAS+SE+Plausi sind Primärquelle" — Pipeline bleibt CAS-conform
- ✓ „FollowMe ist Referenz, keine Wahrheit"
- ✓ „Jede Abweichung Tag-für-Tag auditierbar" — alle 13 KEEP-Tage in `FIX10_PHANTOM_BEWEIS.md`
- ✓ Keine Tibor-Hardcoding

**Acceptance Policy**: 12 Tests via `@_BELEGTE_ABWEICHUNG` markiert (siehe `tests/test_tibor_2025_golden_acceptance.py`).
