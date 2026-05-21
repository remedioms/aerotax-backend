# BH-CORE-001 Phase 5d — Prompt-Hardening + Structured Decision-Mapping

Stand: 2026-05-20. **Status: COMPLETE.** Keine KI-Live-Calls.
Mock-only. Keine finale KPI-Änderung.

## §0 Lessons Learned aus Phase 5b

1. **PU-Misread**: KI interpretierte „PU" als Pula (PUY-Airport) statt als
   Purser/Crew-Position. Phase 5d fixt das mit explizitem Crew-Code-Vokabular.
2. **LAD/TLV-Naivität**: KI bestätigte Phantom-Touren weil sie keinen
   Zugang zu Cross-Source-Konflikten (SE/Anfahrt/FollowMe) hatte. Phase 5d
   liefert die WHY-evidence-uncertain-Erklärung mit.
3. **Dict-vs-String-Mapping**: KI lieferte Dicts wie
   `{resolved_place: 'LAD'}`, Mapping in `_build_normalized_day` erwartete
   Strings — Phase 5d fügt ein neues strukturiertes `decision`-Feld
   („KEEP_TOUR" / „DROP_TOUR" / „NEEDS_REVIEW") und `context_type` hinzu,
   das direkt mappbar ist.

## §1 Diff-Übersicht

| Datei | Änderung |
|---|---|
| `app.py` `_ai_resolver_airline_crew_context_block` | Erweitert um Crew-Code-Vokabular (16 Codes) + numerische-Marker-Regel + FollowMe-Referenz-Hinweis |
| `app.py` `_ai_resolver_safe_context` | Neue Felder: `why_evidence_uncertain` (rekonstruiert aus evidence_against), `se_has_foreign_allowance`, `has_anfahrt_evidence` |
| `app.py` `_ai_resolver_build_prompt` | Cross-Source-Konflikt-Block sichtbar; Structured-Output-Schema mit `decision` + `context_type`; explizite Regeln für confidence-Schwellen |
| `app.py` `_resolve_uncertain_fact_with_ai` (live + mock-bypass) | Whitelist + Sanitisierung von `decision` (KEEP_TOUR/DROP_TOUR/NEEDS_REVIEW) + `context_type` (8 enum-Werte) |
| `app.py` `_ai_resolver_mock_dispatch` | Phase-5d-Updates: PU=Purser-Awareness, Phantom-Detection, Place-Conflict, Transit-Pattern, jeder Branch liefert decision+context_type |
| `app.py` `_build_normalized_day` Phase-5c-Mapping | Nutzt jetzt strukturiertes `decision`-Feld als Primärquelle; Legacy-Mapping bleibt als Fallback |
| `tests/test_phase5d_ai_prompt_and_mapping.py` | **NEU** — 16 Tests |
| `docs/BH_CORE_001_PHASE5D_PROMPT_AND_MAPPING.md` | **NEU** — dieses Dokument |

## §2 Crew-Code-Vokabular im Prompt

Pflicht-Block im Crew-Kontext-Header:

| Code | Bedeutung | Anti-Verwechslung |
|---|---|---|
| PU, PUR | Purser / Kabinenchef | **NICHT Pula-Airport (PUY)** |
| P1, P2 | Pilot 1/2 oder Pattern-Slot | NICHT Flughafen-Codes |
| CR | Captain | NICHT IATA-Code |
| FO | First Officer | — |
| RES, SB, SBY | Reserve / Bereitschaft | Kontext-abhängig (home vs hotel) |
| X | streckenfrei-Tag / Layover-Off | Innerhalb Tour ≠ Frei zuhause |
| == | Layover-Continuation | — |
| OFF, OF | Off-Day | Kontext-abhängig |
| ORTSTAG, FRS, LMN_AS/CR | Office/Training Hb | passive |
| EM, EH | Emergency-Training, Eintraining | passive |
| SECCRM, CRM, TRG | Training | passive |

**Numerische Marker-Regel:** 5-6-stellige Ziffer (z.B. 103703, 32935, 57783) = Roster-/Sequence-ID, NICHT LH-Flugnummer. LH-Flugnummern sind 3-4-stellig (LH404, LH755).

**FollowMe-Hinweis:** Referenz, nicht Wahrheit. CAS+SE+Plausibilität sind Primärquellen.

## §3 Structured Output Schema

KI MUSS liefern:

```json
{
  "resolved": true|false,
  "decision": "KEEP_TOUR" | "DROP_TOUR" | "NEEDS_REVIEW",
  "context_type": "tour_day" | "homebase_free" | "homebase_standby"
                  | "hotel_standby" | "reader_misread" | "routing_conflict"
                  | "positioning" | "unknown",
  "value": {"resolved_place": "...", "country": "...", ...} oder beschreibend,
  "confidence": 0.0-1.0,
  "reason": "kurze Begründung",
  "evidence": ["Beleg 1", "Beleg 2"],
  "needs_review": true|false
}
```

Decision-Regeln:
- **KEEP_TOUR** nur wenn CAS+SE+Plausibilität konsistent. Bei FollowMe-Place-Konflikt → NICHT blind KEEP.
- **DROP_TOUR** bei Phantom-Verdacht (no SE, no Anfahrt, Frei-Lücke).
- **NEEDS_REVIEW** bei Ambig (RES nach foreign-overnight, Phantom-Tag mit Routing aber ohne SE).

Confidence-Regeln (verschärft):
- ≥ 0.90 nur wenn alle drei Quellen konsistent. Cross-Source-Konflikt → max 0.85.
- 0.70-0.89 wenn defensible mit Konflikt-Hinweisen.
- < 0.70 wenn unklar.

## §4 Decision-Mapping (Hard-Blocker bleiben)

In `_build_normalized_day`:

```python
if hard_blockers:
    # duty_over_ftl, day_already_in_other_tour, cas_followme_place_conflict
    proposed = 'NEEDS_REVIEW'
elif ai_decision in ('KEEP_TOUR', 'DROP_TOUR', 'NEEDS_REVIEW') and ai_resolved:
    if ai_conf >= 0.90:
        proposed = ai_decision  # auto-apply
    elif 0.70 <= ai_conf < 0.90:
        proposed = 'NEEDS_REVIEW'
    else:
        proposed = 'NEEDS_USER'
```

## §5 Beispiel-Prompt: OTP→FRA→LHR (Transit)

```
... [Crew-Code-Vokabular Block] ...

Resolver-Aufgabe: routing_consistency

Plan-Kontext (anonymisiert, PII-gefiltert):
  day: {"datum": "2025-07-03", "routing": ["OTP","FRA","LHR"],
        "duty_duration_minutes": 720, "raw_marker": "129023 PU / Tag 3",
        "has_fl": true, ...}
  se: {"se_has_allowance": false}
  homebase: "FRA"
  evidence_against: [["transit_via_homebase_ends_foreign", 4, "..."]]
  why_evidence_uncertain: ["Routing hat Homebase mittig als Transit,
                           endet aber im Ausland."]
  se_has_foreign_allowance: false

WARUM EVIDENCE-ENGINE UNSICHER IST (Cross-Source-Konflikte):
  - Routing hat Homebase mittig als Transit, endet aber im Ausland.
    KEIN normaler Same-Day-Homebase-Return.

Unsicherer Fakt: 129023 PU / Tag 3

[Strict-JSON-Schema mit decision + context_type ...]
```

### Mock-Resultat OTP→FRA→LHR

```json
{
  "resolved": true,
  "decision": "NEEDS_REVIEW",
  "context_type": "routing_conflict",
  "value": "inconsistent",
  "confidence": 0.85,
  "reason": "routing transits via homebase (FRA) und endet foreign (LHR) — KEIN clean Same-Day-Homebase-Return. Eher positioning oder reader_misread.",
  "evidence": ["routing=['OTP', 'FRA', 'LHR']"],
  "needs_review": true
}
```

→ Phase-5d-Mapping: medium-conf (0.85) → `proposed=NEEDS_REVIEW`. **NICHT blind KEEP.**

## §6 Beispiel-Prompt: LAD-Phantom

```
... [Crew-Code-Vokabular] ...

Resolver-Aufgabe: tour_boundary

Plan-Kontext:
  day: {"datum": "2025-05-20", "routing": ["FRA","LAD"], "layover_ort": "LAD",
        "overnight_after_day": true, "raw_marker": "103703 P1",
        "duty_duration_minutes": 234, ...}
  se: {"se_has_allowance": false}
  followme: {"in_any_tour_span": false}
  evidence_against: [
    ["no_se_allowance", 2, "no SE-stamp"],
    ["followme_explicit_other_span", 3, "not in any span"],
    ["no_homebase_commute_evidence", 2, "not in anfahrten"]
  ]
  why_evidence_uncertain: [
    "Keine SE-Auslandsspesen-Zeile UND nicht in FollowMe-Tour-Spans —
     Phantom-Tag-Verdacht oder echte Tour ohne Beleg.",
    "Tag claimt Hb-Departure, aber keine Anfahrt in der Anfahrten-Liste."
  ]
  se_has_foreign_allowance: false

WARUM EVIDENCE-ENGINE UNSICHER IST (Cross-Source-Konflikte):
  - Keine SE-Auslandsspesen-Zeile UND nicht in FollowMe-Tour-Spans —
    Phantom-Tag-Verdacht oder echte Tour ohne Beleg.
  - Tag claimt Hb-Departure, aber keine Anfahrt in der Anfahrten-Liste.

Unsicherer Fakt: 103703 P1

[Strict-JSON-Schema ...]
```

### Mock-Resultat LAD-Phantom

```json
{
  "resolved": false,
  "decision": "NEEDS_REVIEW",
  "context_type": "reader_misread",
  "value": {
    "phantom_candidate": true,
    "cas_routing_unverified": ["FRA", "LAD"]
  },
  "confidence": 0.65,
  "reason": "Phantom-Verdacht: CAS-routing zeigt foreign, aber kein SE-Stempel, nicht in FollowMe-Tour-Spans, keine Anfahrt-Evidence. CAS-Reader könnte falsch gelesen haben oder Sequence-ID statt Flight-Nr.",
  "evidence": [
    "routing=['FRA', 'LAD']",
    "se_has_foreign_allowance=False",
    "fm.in_any_tour_span=False",
    "no_anfahrt_for_this_date"
  ],
  "needs_review": true
}
```

→ Phase-5d-Mapping: low-conf (0.65) → `proposed=NEEDS_USER`. **NICHT blind Z76.** ✓

## §7 Beispiel-Resultat: PU-only-Marker (vorher Pula-Misread)

Aufgabe: `marker_semantics`, marker=`'PU'`, kein layover_ort, kein foreign-Routing.

```json
{
  "resolved": true,
  "decision": "NEEDS_REVIEW",
  "context_type": "unknown",
  "value": {
    "meaning": "crew_position: Purser/Kabinenchef (NICHT Pula-Airport)",
    "is_crew_position_code": true
  },
  "confidence": 0.92,
  "reason": "marker PU → crew_position-Code (kein Tour-Indikator)",
  "evidence": ["marker=PU"],
  "needs_review": true
}
```

→ Mock erkennt PU explizit als Crew-Position, **nicht Airport**. `is_crew_position_code=True` macht das maschinell weiterverwertbar.

## §8 Akzeptanz-Tests (16/16 grün)

| Test | Status |
|---|---|
| `test_prompt_contains_crew_code_vocabulary_pu_not_pula` | ✓ |
| `test_prompt_contains_cross_source_context` | ✓ |
| `test_prompt_explains_followme_is_reference_not_truth` | ✓ |
| `test_structured_output_requires_decision` | ✓ |
| `test_ai_decision_keep_maps_only_with_high_confidence` | ✓ |
| `test_ai_decision_drop_maps_only_with_high_confidence` | ✓ |
| `test_medium_confidence_maps_to_review` | ✓ |
| `test_low_confidence_maps_to_user_question` | ✓ |
| `test_hard_blocker_duty_over_ftl_blocks_keep` | ✓ |
| `test_lad_phantom_prompt_mentions_no_se_no_anfahrt_free_gap` | ✓ |
| `test_tlv_phantom_prompt_mentions_no_se_no_anfahrt_free_gap` | ✓ |
| `test_jfk_shannon_prompt_mentions_place_conflict` | ✓ |
| `test_otp_fra_lhr_prompt_mentions_transit_not_homebase_return` | ✓ |
| `test_pu_marker_not_interpreted_as_airport_by_mock` | ✓ |
| `test_marker_semantics_pu_is_crew_position` | ✓ |
| `test_no_final_kpi_change_phase5d` | ✓ |

## §9 KPI-Effekt: **0**

Phase 5d ändert nur Prompt-Text, Mock-Heuristik und Decision-Mapping.
Keine finale Berechnung verändert.

| KPI | Phase 5b | Phase 5d | Δ |
|---|---:|---:|---:|
| arbeitstage | 90 | 90 | 0 |
| z76_eur | 3648 | 3648 | 0 |
| gesamt | 3732 | 3732 | 0 |

## §10 Volle Regression

**1510 grün** (+16 vs Phase 5b mit 1494), 7 skipped, 16 acceptance
(Phase 5e ggf. Live-Re-Run für Counter-Convergence — separate Freigabe).

## §11 Stop-Status

✓ Kein Deploy
✓ Kein Live-Run
✓ Kein Production-Flag
✓ Keine finale Berechnung
✓ Keine neuen Live-KI-Calls (Mock-only)
✓ Anti-Tax-Sanitizer aktiv (unverändert)
✓ PII-Hardening aktiv (unverändert)
✓ Cache aktiv (unverändert)

**STOP. Erst nach User-Entscheidung Phase 5e (zweiter Live-KI-Mini-Batch
mit dem neuen Prompt + Mapping zum Vergleich vs Phase-5b-Ergebnis).**
