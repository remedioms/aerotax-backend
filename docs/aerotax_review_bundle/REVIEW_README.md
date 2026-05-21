# AeroTAX Review Package — External Audit by ChatGPT

**Bundle generated:** 2026-05-19
**Scope:** Tibor 2025 — Tax-relevant work-day classification of an airline crew roster
**Ask:** External review of calculation logic + identification of root-cause bugs

---

## What AeroTAX does

AeroTAX is a calculator that helps **airline crew members in Germany** prepare their tax declaration. It takes 3 mandatory documents:

1. **LSB** (Lohnsteuerbescheinigung) — annual employer tax statement
2. **SE** (Streckeneinsatzabrechnung) — per-day Steuerfrei reimbursements paid by employer (Auslandspauschalen reduced)
3. **CAS** (Crew-Roster / Dienstplan, also called „Flugstundenübersicht") — daily crew duty log with markers (e.g. `31591 P1`, `ORTSTAG`, `X`, `==`)

The user also enters: tax year, homebase airport (e.g. FRA, MUC), commute distance, optional commute time.

### Output: Tax-relevant work-day totals

| KPI | Meaning |
|---|---|
| `arbeitstage` | Total work days (counted for cleaning/uniform allowance) |
| `reinigungstage` | Same as arbeitstage; tax law treats them identically (1.60 €/day) |
| `fahr_tage` | Commute days (Anfahrten zum Homebase, 28 km × 0.30 €/km × 2) |
| `hotel_naechte` | Hotel nights (3.60 €/night Trinkgeld) |
| `z72` | Inland-Tagestrip ≥ 8h, no overnight (14 €/day) |
| `z73` | Inland-An-/Abreise (14 €/day) |
| `z74` | Inland-Volltag ≥ 24h (28 €/day) |
| `z76_eur` | Foreign per-diem (BMF rates per country, voll_24h vs an_abreise) |
| `z77_total` | Total Steuerfreie Spesen (deducted from arbeitgeber-Erstattung) |
| `gesamt` | Final amount entered in WISO tax software |

### Architecture principle (CLAUDE.md)

> **Sonnet reads facts. Python classifies and calculates. ReportLab renders. No AI-generated tax decision is accepted as final.**

- **AI/Sonnet (Reader)**: extracts structured facts (`activity_type`, `routing`, `overnight_after_day`, `layover_ort`, `start_time`, `end_time`, `stfrei_betrag`, etc.) from PDFs. **No tax decisions.**
- **Python (Classifier `_deterministic_classify_v7`)**: deterministic per-day classification + tour-cluster aggregation. Deterministic — same input facts → same output.
- **ReportLab**: PDF rendering from result dict.

---

## Tibor 2025 — Reference case

Crew member „Tibor" provided his FollowMe.aero output (industry-standard manual calculation) as ground truth. AeroTAX should match within ±2 days / ±150 €.

### Golden (FollowMe) totals

| KPI | Value |
|---|---:|
| arbeitstage | 133 |
| hotelaufenthalte | 66 |
| fahrtage | 58 (53 normal + 5 zusatz) |
| z72 | 5 days, 70 € |
| z73 | 11 days, 154 € |
| z74 | 1 day, 28 € |
| z76 | 113 days, 4794 € |
| **gesamt** | **6020.72 €** |

### AeroTAX IST (Phase-A Forensik, Live-Run 2026-05-15, expired-token)

| KPI | IST | Δ vs Golden |
|---|---:|---:|
| arbeitstage | 140 | +7 |
| hotelnächte | 78 | +12 |
| fahrtage | 55 | −3 |
| z72 | 5 | 0 ✓ |
| z73 | 8 | −3 |
| z74 | 0 | −1 |
| z76_eur | 4437 | −357 |
| **gesamt** | **5621** | **−400** |

### Fixture-simulation (pre-Phase-1-7-fixes)

Replica of `_followme_align_counters` on `tests/fixtures/tibor_aerotax_v11_raw_initial.json`:

| KPI | Fixture | Δ vs Golden |
|---|---:|---:|
| arbeitstage | **115** | **−18** |

**Discrepancy fixture vs live-run = 25 days.** Caused by Phase-1-7 SE-Override / Layover-Inference fixes which **overcorrected** Frei → Z76 in many places.

---

## Recent fixes (deployed)

| Bug | Status | Effect |
|---|---|---|
| **BH-001** Review-Question „>8h weg?" for OF/ORTSTAG → Marker-Semantik-Frage via AI-Resolver | fixed_unverified, deployed Cloud Run 00063-422 | Quality fix; 9 tests green |
| **BH-002** `/api/job/<id>` HTTP 502/HTML → 403 JSON | fixed_unverified | Auth-gate now returns JSON |
| **BH-006** `/api/session/<token>` `canonical_state=null` → full contract | fixed_unverified | All state-fields now in response |
| **BH-003a** Issue-Tag „Heimkehr aus Vortag-Tour" → Z76 An/Ab for 2025-01-06 (7 guards) | fixed_unverified, deployed 00064-x85 | +1 Z76, +28 €, −1 Issue. Guards: prev.layover_ort kein Inland, ends_at_homebase, routing[0]==prev.layover, routing[-1]==homebase, duty≥480, BMF-Mapping |

### Pending Bugs (not yet fixed)

| Bug | Status | Description |
|---|---|---|
| BH-003b | parked (no KPI-Effect) | False Issue-Tage (05-23/06-03/10-28) — audit-cleanup only |
| BH-003c | open | X/==/OFF inside active foreign tour → Frei (should be Z76 Mitte) — **15 days lost** |
| BH-003d-A | open | Z76 An/Ab + Mid days double-counted (7 days) |
| BH-003d-B | open | Z72-Inland >8h with LMN_AS/LMN_CR/EM markers counted as workday (5 days) |
| BH-003e | open | Standby (RES) during active foreign tour → Standby instead of Z76 (9 days) |
| BH-004 | open | Inland-Layover (BER/LEJ/MUC/DUS) classified as foreign Z76 |
| #228/F5 | backlog | BMF day-type rates per country (Volltag vs An/Ab) — residual Z76-€ diff |

---

## Questions for external review

See `REVIEW_OPEN_QUESTIONS.md`. Main themes:

1. Is the `_followme_align_counters` tour-identification algorithm sound, or are tours wrongly split?
2. Why does Tibor's Golden classify days like 04-23-26 (RES marker → Korea Z76) as a Tour, but AeroTAX as Standby? Is RES-during-tour a legitimate pattern we miss?
3. Are the 7 Z76-Tour-days in fixture-extras (05-20–22, 06-01–02, 09-25, 10-26, 12-15) legitimately work-days or wrongly counted?
4. What is the cleanest fix order for the remaining 30+ misclassified days?
5. Is there a normalized-tours pre-aggregation layer that would solve most issues at once (#222 in roadmap)?

---

## Bundle contents

```
REVIEW_README.md              — this file
REVIEW_CURRENT_RUN.json       — current AeroTAX classification (fixture-based, anonymized)
REVIEW_GOLDEN.json            — FollowMe.aero soll-totals + day_classification
REVIEW_DIFF.csv               — day-by-day diff IST vs SOLL
REVIEW_CODE_SNIPPETS.md       — relevant classifier/aggregator/resolver code
REVIEW_TESTS.md               — test inventory + green/red list
REVIEW_OPEN_QUESTIONS.md      — specific questions for ChatGPT
```

## Data-Limitation Disclaimer

- **No live PII**: all crew names/addresses redacted. Tour-IDs (numeric markers like `31591 P1`) are roster-internal codes, kept for classifier-logic-debugging.
- **No raw PDFs**: only extracted facts.
- **No secrets**: no API keys, Supabase keys, Stripe keys, RECOVERY_SECRET, ANTHROPIC_API_KEY.
- **Reference run is fixture-snapshot** (pre-Phase-1-7-deploys). Live-run e132976f data is no longer available (token expired, container restart cleared RAM).
