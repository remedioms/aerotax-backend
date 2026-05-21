# AeroTAX — Open Questions for External Review

> External reviewer (ChatGPT): please answer these from `REVIEW_DIFF.csv` + `REVIEW_CURRENT_RUN.json` + `REVIEW_GOLDEN.json` + `REVIEW_CODE_SNIPPETS.md`.

---

## Q1 — Tour-Identification Algorithm Soundness

The current `_followme_identify_tours` defines a tour as **max-contiguous sequence of service-days**, where a service-day is anything except `frei | urlaub | krank | zeroday | issue` (plus `standby` with `eur > 0`).

**Problem:** Looking at Tibor's Golden `day_classification`, single tours have `tour_size` of 1 to 6 days, and Golden marks `is_anreise` / `is_abreise` flags per day. Some examples:

- **Tour 1 (Bangalore)**: 4 days (Jan 3-6). Golden: Z73 An → Z76 × 2 → Z76 Ab.
- **Tour 2 (single same-day)**: Jan 10. Golden: NO_VMA, tour_size=1.
- **Tour 53 (Israel)**: Dec 27-29. Golden: Z76 An → Z76 24h → Z76 Ab.

In AeroTAX fixture, the same `_followme_identify_tours` is run on `tage_detail` AFTER classification. But **classifier may have wrongly marked Jan 4 (X marker, BLR overnight) as `Frei`** — which **splits the tour** into Jan 3 alone + Jan 5-6.

**Question:** Should `_followme_identify_tours` look at **`reader_facts.overnight_after_day`** (carries over even if klass=Frei) instead of `klass != frei`? Or should tour-identification be done **before** classification, from raw routing-flow + overnight-flags?

---

## Q2 — Standby (RES) Marker during active Foreign Tour

In Tibor's Golden:

| Date | Golden klass | Land | tour_pos |
|---|---|---|---|
| 2025-04-23 | **Z73** | Deutschland | An |
| 2025-04-24 | **Z76** | Republik Korea | mid |
| 2025-04-25 | **Z76** | Republik Korea | mid |
| 2025-04-26 | **Z76** | Republik Korea | Ab |

In AeroTAX fixture: **All 4 days classified as `Standby`** (marker `RES`).

**Background:** „RES" in Lufthansa CAS = on-call standby. But during an active tour, a crew member can be **on standby IN the hotel abroad** (Hotel-Standby). The crew is physically in Korea, on duty for next flight, eligible for foreign per-diem.

**Questions:**
1. Should the classifier treat `RES`-marker as `Z76` when:
   - `prev_overnight=True`
   - `prev.layover_ort` is foreign
   - Crew is at a foreign hotel?
2. How to distinguish from "RES at homebase" (Standby zuhause, no per-diem)?
3. Is there a CAS-marker variant (`RES_SB`, `SBY`, etc.) that disambiguates?

**Lost days from this:** 9 (04-23-26 Korea, 10-20-24 London/Spanien)

---

## Q3 — Z76-Tour-Tage in fixture-extras: legitimately or wrongly counted?

AeroTAX fixture classifies 7 days as `Z76` that Golden does NOT have at all:

| Date | Marker | Routing | Duty | AeroTAX klass | Golden status |
|---|---|---|---:|---|---|
| 2025-05-20 | 103703 P1 | FRA→LAD | 234m | Z73 (Abend-Anreise) | NOT in Golden |
| 2025-05-21 | 103703 P1 | LAD | 270m | Z76 (Auslands-Layover) | NOT in Golden |
| 2025-05-22 | 103703 P1 | LAD→FRA | 179m | Z76 (Z76 An/Ab) | NOT in Golden |
| 2025-06-01 | 126533 PU | FRA→CPH→GOT | 1084m | Z76 (Auslands-Layover GOT) | NOT in Golden |
| 2025-06-02 | 126533 PU | GOT→FRA→SOF | 1189m | Z76 (Auslands-Layover SOF) | NOT in Golden |
| 2025-09-25 | 15688 PU | FRA→BER→KRK | 1059m | Z76 (Auslands-Layover KRK) | NOT in Golden |
| 2025-10-26 | 32935 PU | FRA→TLV | 449m | Z76 (Auslands-Layover TLV) | NOT in Golden |
| 2025-12-15 | 57783 P1 Tag 2 | JFK→FRA | 184m | Z76 (Z76 An/Ab) | NOT in Golden |

**Question:** Are these days **legitimately work-days that Golden missed** (Golden = manual FollowMe.aero, prone to omission), or **wrongly counted by AeroTAX**?

Note: LAD = Luanda Angola, GOT = Göteborg Sweden, SOF = Sofia Bulgaria, KRK = Krakow, TLV = Tel Aviv, JFK = New York. All real foreign destinations with confirmed CAS overnight-flags in the fixture.

The 1189-minute duty on 06-02 (FRA→CPH→GOT→SOF in one day) is unusually long — possibly a multi-leg-tour where Golden misaggregated.

---

## Q4 — Z72-Inland >8h Office-Tage

AeroTAX fixture counts 5 days as Z72 (Inland 14 €) that Golden does NOT have:

| Date | Marker | Routing | Duty | AeroTAX reason |
|---|---|---|---:|---|
| 2025-03-22 | 83343 PU | FRA→TOS | 510m | Same-Day Z72 (total 570min ≥ 480) |
| 2025-04-07 | **ORTSTAG FRS** | FRA | 1439m | Office Inland >8h |
| 2025-04-28 | **LMN_AS LMN_CR1** | FRA | 600m | Office Inland >8h |
| 2025-05-19 | **LMN_AS / LMN_CR1** | FRA | 600m | Office Inland >8h |
| 2025-07-03 | 129023 PU / Tag 3 | OTP→FRA→LHR | 485m | Same-Day Z72 (total 545min) |

**Question:**
- `ORTSTAG FRS` is normally a **passive marker** (Crew at home, no duty). But this day has `duty=1439 min` (24h). Is this a **24h-Standby** or **Bürodienst** day that legitimately earns Z72?
- `LMN_AS LMN_CR1` are **medical-license-renewal-markers** (Loss of License Medical). Should these EVER count as Z72?
- 07-03 routing `OTP→FRA→LHR` ends at LHR (London, Auslands). Why is it classified as Inland-Z72?

---

## Q5 — X-Marker during active foreign tour (BH-003c, 15 days lost)

Tibor's roster uses `X` as a marker for **mid-tour-day** when crew is at foreign hotel (no flight on that day, just resting before next leg). Examples:

| Date | Marker | overnight_after_day | layover_ort | AeroTAX klass | Golden klass |
|---|---|---|---|---|---|
| 2025-01-04 | X | True | BLR | **Frei** | Z76 Indien-Bangalore |
| 2025-01-20 | X HKG | True | HKG | **Frei** | Z76 China-HongKong |
| 2025-02-14 | X HND | True | HND | **Frei** | Z76 Japan-Tokyo |
| 2025-03-30 | X BOM | True | BOM | **Frei** | Z76 Indien-Mumbai |
| 2025-04-10 | X | True | ICN | **Frei** | Z76 Republik Korea |
| 2025-05-15 | X TLV | True | LAX | **Frei** | Z76 USA |
| 2025-05-27 | X TLV | True | ORD | **Frei** | Z76 USA-Chicago |
| 2025-06-09 | X | True | SIN | **Frei** | Z76 Singapur |
| ... 7 more | | | | | |

**Reader-heuristic** maps `X` → `activity_type='frei'`. But `overnight_after_day=True` + `layover_ort=foreign` should override this.

**Question:** What is the cleanest detection rule?
- Option A: Reader-heuristic checks `X + overnight_after_day=True + layover_foreign` → `activity_type='tour'`?
- Option B: Classifier post-processing: `klass='Frei'` but in middle of tour-cluster → upgrade to `Z76`?
- Option C: Tour-cluster pre-aggregation (#222 normalized_tours-layer) before classification?

**Hint:** The marker text often includes destination code (`X HKG`, `X HND`, `X BOM`) — this is strong evidence of foreign mid-tour.

---

## Q6 — Fix-Order Safety

After fixing BH-001 + BH-003a, the remaining bugs are:

| Bug | Days affected | Pattern |
|---|---:|---|
| BH-003c X-marker in tour | 15 | reader-mistake or classifier-override |
| BH-003e Standby in tour | 9 | tour-aware standby detection |
| BH-003f == in tour | 4 | similar to X-marker |
| BH-003g OFF-marker in tour | 3 | similar to X-marker |
| BH-003d-A Z76-Anreise/Heimkehr-double-count | 7 | tour-aggregation off-by-one |
| BH-003d-B Z72 Inland >8h false positive | 5 | marker-semantik for LMN_AS |
| BH-004 Inland-layover (BER/LEJ) in foreign tour | 3-5 | layover-code resolver too narrow |
| #228 BMF day-type rate matrix | ~10 € res | per-country fine-tuning |

**Risk concerns:**
1. Fixing BH-003c (X→Z76 in tour) may regress legitimate „X = Frei zuhause" days.
2. Fixing BH-003e (RES→Z76 in tour) may regress legitimate „RES = Standby zuhause" days.
3. Fixing BH-003d-A may regress BH-003a (01-06 should still be Z76 Heimkehr).

**Question:** What is the **safest fix order** that minimizes regression risk?

Proposed:
1. BH-003c (X+overnight_foreign → Z76 Mitte) — guard: `marker contains foreign-IATA AND overnight=True`
2. BH-003f (== same pattern)
3. BH-003g (OFF same pattern)
4. BH-003e (RES+overnight_foreign → Z76 Mitte)
5. BH-003d-A (audit tour-aggregation, ensure no double-counting)
6. BH-004 (Inland-layover-code in tour-context)
7. #228 (BMF rate matrix fine-tuning)

Is this order sound, or is there a higher-leverage refactor (normalized_tours-layer) that solves multiple at once?

---

## Q7 — Phase-1-7 SE-Override: overcorrecting?

The current Phase-1 SE-Override rescue (in `_deterministic_classify_v7`) converts `Frei → Z76` when SE-line shows active foreign reimbursement:

```python
if klass == 'Frei' and se.get('count', 0) > 0 \
        and se.get('stfrei_inland') is False \
        and se.get('stfrei_ort'):
    klass = 'Z76'
    eur_added = bmf.get('an_abreise', 28.0)
```

This was added because the reader sometimes mis-reads tour-mid-days as `Frei` when SE clearly shows abroad. **But** the fixture (pre-Phase-1) has these 15 days as `Frei`, and the live-run (post-Phase-1) had +25 days as `Z76`. **The rescue may have overshot.**

**Question:**
- Are there days where Phase-1 SE-Override **should NOT** have rescued?
- Specifically: if SE has stamp for day N but actually-foreign-overnight was day N-1 (SE-stamps can be daily-aggregate), does the rescue mistakenly fire?
- Should the SE-Override require **additional evidence** like `overnight_after_day=True` or `prev_layover_ort=foreign`?

---

## Q8 — Reader Quality (Pre-classifier)

The reader (Anthropic Sonnet 4.5 with structured prompts) sometimes:
- Misses `overnight_after_day` flag → tour appears as isolated days
- Sets `activity_type='frei'` for ambiguous markers like X, ==, OFF
- Misses `routing` for non-flight days
- Misses `layover_ort` when ambiguous

**Question:** Should some of these reader-mistakes be **fixed at reader level** (better prompt with more crew-context) or always **post-processed** by classifier? Where to draw the line?

Hint: Current architecture says „Sonnet reads facts, Python classifies." But if reader-quality is the root, fixing classifier patches symptoms.

---

## Q9 — Hotelnächte semantics

Golden has `hotelaufenthalte = 66`. AeroTAX fixture computes hotel = `sum(z76-tage in tour) - 1 per tour`.

But Golden's `day_classification` doesn't expose `hotel`-flag per day. It only marks `is_abreise=True` for the last day. So **hotel = total_z76 - tour_count**?

Counting Golden:
- Z76 count = 113
- Tours = 53 (matches `meta.touren`)
- 113 - 53 = 60 — close to 66 but off by 6.

**Question:**
1. Is `hotel = Σ Z76-tage per tour − 1` the correct FollowMe definition? Or should also count Z73-overnight-tage (Inland-Hotel)?
2. Where do the missing 6 come from? Maybe Z73-mid-tour-tage or RES-with-Hotel?

---

## Q10 — Test Coverage Gaps

The current test suite has 1358 backend tests + 31 frontend. Critical gaps:

1. **No full-pipeline-Tibor-Golden-acceptance test**: Should run classify_v11_cas_pipeline on tibor_aerotax_v11_raw_initial.json fixture and compare ALL totals to followme_golden_tibor_2025.json with strict tolerances.

2. **No tour-boundary-detection tests**: `_followme_identify_tours` is tested implicitly but not for edge cases (single-day-tour, multi-stop-tour, tour-with-Standby-mid).

3. **No reader-quality tests** with anonymized Tibor-CAS-pages.

**Question:** Which of these gaps is most important to fix first?

---

## Summary of Asks

| Q | Topic | Type |
|---|---|---|
| 1 | Tour-Identification soundness | Architecture |
| 2 | Standby-during-tour | Domain |
| 3 | Z76-Anreise/Heimkehr legit? | Data-validation |
| 4 | Z72-Inland-Office >8h | Domain |
| 5 | X-marker in tour | Detection-Rule |
| 6 | Fix-order safety | Risk-mgmt |
| 7 | Phase-1 SE-Override overshoot | Bug-hypothesis |
| 8 | Reader vs Classifier responsibility | Architecture |
| 9 | Hotelnächte semantics | Domain |
| 10 | Test coverage gaps | Test-strategy |
