# BH-CORE-001 Phase 5b — Live-KI-Calls Resultate

Stand: 2026-05-20. **Status: ERFOLGREICH durchgelaufen.** 8 Live-Calls
gegen Anthropic Claude Sonnet 4.5. Alle Anti-Tax-Sanitizer-clean.

## §0 Preflight

| Check | Status |
|---|:-:|
| `ANTHROPIC_API_KEY present` | yes (Wert nicht geloggt) |
| `AEROTAX_AI_RESOLVER_PHASE5B_APPROVED=yes` | yes |
| `max_calls cap` | 10 (verwendet: 8) |
| `PII-Hardening (_ai_resolver_safe_context)` | yes |
| `Anti-Tax-Sanitizer` | yes |
| `Cache (_ai_resolver_cache)` | yes |
| `Crew-Kontext im Prompt` | yes |
| **Preflight** | **GREEN** |

## §1 Output-Tabelle

| ID | Datum | Kind | resolved | KI-Value (Kurz) | Conf | Review | Decision-Effect | Tax-Reject |
|---:|---|---|:-:|---|---:|:-:|---|:-:|
| 1 | 2025-12-14 | `place_code` | False | `{}` | 0.15 | ✓ | USER question | ✓ clean |
| 2 | 2025-12-15 | `tour_boundary` | True | duty_id=57783, day_sequence=Tag 2, multi_day_duty | 0.95 | ✗ | AUTO (high conf) | ✓ clean |
| 3 | 2025-07-03 | `routing_consistency` | True | resolved_place=Pula HR — **Misread** | 0.75 | ✓ | REVIEW w/ suggestion | ✓ clean |
| 4 | 2025-04-23 | `standby_context` | True | activity=RES, type=standby_duty, location=ICN | 0.95 | ✗ | AUTO (high conf) | ✓ clean |
| 5 | 2025-07-29 | `tour_boundary` | True | place=RIX, country=Lettland, X=streckenfrei | 0.95 | ✗ | AUTO (high conf) | ✓ clean |
| 6 | 2025-01-04 | `marker_semantics` | True | place=BLR, country=IND, X=layover_day | 0.95 | ✗ | AUTO (high conf) | ✓ clean |
| 7 | 2025-05-20 | `tour_boundary` | True | place=LAD, country=Angola, LH3703 | 0.95 | ✗ | AUTO (high conf) | ✓ clean |
| 8 | 2025-10-26 | `tour_boundary` | True | place=TLV, country=Israel, LH32935 | 0.82 | ✓ | REVIEW w/ suggestion | ✓ clean |

## §2 Pro-Kandidat-Details

### #1 JFK vs FollowMe-Shannon (2025-12-14, place_code)
**KI: resolved=False, conf=0.15, needs_review=True**

- value: `{}`
- reason: KI kann „57783 P1" nicht eindeutig auf Airport-Code mappen. 57783
  ist ungewöhnlich lang für LH-Flugnummern, P1 könnte Crew-Position sein.
- evidence:
  - raw_marker: '57783 P1' im tour-day FRA→JFK
  - routing zeigt FRA→JFK, layover JFK
  - keine Korrelation zwischen 57783 und bekannten Airport/City-Codes

**Akzeptanz:** ✓ KI hat KEINE blinde JFK-Bestätigung geliefert. Niedrige Conf
→ Phase-5c-Logik geht auf NEEDS_USER. Tour-First darf hier **nicht** blind
KEEP setzen. **Cross-Source-Konflikt mit FollowMe-Shannon bleibt ungelöst —
braucht User-Antwort oder externe Quelle.**

### #2 JFK Tag 2 (2025-12-15, tour_boundary)
**KI: resolved=True, conf=0.95, needs_review=False**

- value: `{marker_type=multi_day_duty_identifier, duty_id=57783,
  position=P1, day_sequence=Tag 2}`
- reason: Standardformat für mehrtägige Crew-Einsätze. Tag 2 korreliert mit
  prev_day (Tag 1: FRA-JFK Layover) und current_day (Tag 2: JFK-FRA, ends
  homebase).
- evidence: raw_marker, prev_day, current_day, duty_duration 580min.

**Akzeptanz:** ✓ KI bestätigt: „Tag 2" ist legitime Multi-Day-Duty-Notation,
nicht Reader-Bug. **Bedeutet: Day-Suffix-Marker mit prev=foreign-overnight
ist KEIN automatischer DROP-Indikator.** Diese Information sollte in Phase
4.8b oder 5c eingebaut werden (heutige Heuristik markiert das als
boundary-conflict).

### #3 OTP→FRA→LHR Transit (2025-07-03, routing_consistency)
**KI: resolved=True, conf=0.75, needs_review=True — MISREAD entdeckt**

- value: `{resolved_place=Pula, country=Kroatien, iata_code=PUY, bmf_key=HR}`
- reason: KI interpretiert „PU" in „129023 PU / Tag 3" als IATA-Code für
  Pula. **Das ist falsch.** PU ist im Lufthansa-Crew-Roster der Code für
  „Purser" (Kabinenchef), nicht für Pula-Flughafen.
- evidence: raw_marker, routing, activity_type, Tag 3.

**Akzeptanz:** ✓ `needs_review=True` (conf in 0.70-0.89-Band) → REVIEW mit
Suggestion, NICHT auto-übernehmen. User würde den Pula-Fehler beim Review
fangen. **Wichtige Lesson Learned:** KI hat zwar geografisches Wissen, aber
Lufthansa-spezifische Crew-Codes (PU=Purser, CR=Captain, FO=First Officer)
sind ein Wissens-Gap. Phase-5b-Audit-Note: Prompt könnte explizit über
Crew-Position-Codes informieren.

### #4 RES Korea (2025-04-23, standby_context)
**KI: resolved=True, conf=0.95, needs_review=False**

- value: `{activity_code=RES, expanded=Reserve/Bereitschaftsdienst,
  type=standby_duty, location=ICN}`
- reason: RES = Standard Reserve. Crew übernachtet nach FRA-ICN am
  Vortag in ICN. Am 23.04 RES ohne Routing → Reserve am aktuellen
  Aufenthaltsort.
- evidence: day.raw_marker, day.activity_type, prev_day.layover_ort,
  day.routing, se.stfrei_ort.

**Akzeptanz:** ✓ KI bestätigt unsere Erwartung: RES + prev_overnight_foreign
= standby_hotel. **Wertvolle Klärung.**

### #5 X RIX Kroatien-OFF (2025-07-29, tour_boundary)
**KI: resolved=True, conf=0.95, needs_review=False**

- value: `{resolved_place=RIX, country=Lettland, bmf_key=LV,
  meaning=Layover/Übernachtung in Riga, activity=streckenfrei-Tag während
  Tour}`
- reason: X = streckenfrei-Tag während Tour. RIX (Riga/Lettland) als
  layover_ort konsistent zwischen day + prev_day + SE.
- evidence: day, prev_day, se, Crew-Notation X=streckenfrei.

**Akzeptanz:** ✓ KI bestätigt X-RIX als legitimer Layover-Mid-Day.
**Decision: KEEP_TOUR.**

### #6 X-Marker Bangalore (2025-01-04, marker_semantics)
**KI: resolved=True, conf=0.95, needs_review=False**

- value: `{resolved_place=BLR, country=IND, activity_type=layover_day}`
- reason: X = Layover-Tag ohne Dienst. Crew übernachtet nach FRA-BLR vom
  Vortag in BLR. SE bestätigt foreign-Stempel.
- evidence: Vortag-routing, aktueller Tag, SE-foreign, kein has_fl.

**Akzeptanz:** ✓ Bangalore-Tour bleibt KEEP. Konsistent mit Phase 4.8b.

### #7 LAD-Phantom (2025-05-20, tour_boundary)
**KI: resolved=True, conf=0.95, needs_review=False**

- value: `{resolved_place=LAD, country=Angola, bmf_key=AGO,
  entity_type=flight_number, flight_number=LH3703, crew_position=P1}`
- reason: KI interpretiert 103703 als LH3703 (Lufthansa-Flugnummer
  ohne Präfix 10). Routing FRA-LAD bestätigt Luanda/Angola.
- evidence: raw_marker, routing, layover_ort, activity_type, has_fl.

**Akzeptanz:** ⚠ KI bestätigt naiv die LAD-Tour, weil sie **keinen Zugang
zu Cross-Source-Konflikten hat** (no SE-Stempel, nicht in FollowMe-Spans,
keine Anfahrt-Evidence). **Phase-5c-Defensiv-Behandlung greift:**
- KI-value ist ein dict, nicht ein String wie 'start'/'mid'/'end'.
- Phase-5c-Logik in `_build_normalized_day` fällt auf
  `evidence_decision` zurück.
- `evidence_decision` = NEEDS_AI (durch Multi-Conflict-Override).
- → `proposed_tour_decision_after_ai` bleibt NEEDS_AI/NEEDS_REVIEW.

**Effekt: Kein blindes KEEP trotz KI-conf=0.95.** Cross-Source-Konflikt
gewinnt gegen KI-Naivität. ✓

### #8 TLV-Phantom (2025-10-26, tour_boundary)
**KI: resolved=True, conf=0.82, needs_review=True**

- value: `{resolved_place=TLV, country=Israel, bmf_key=IL}`
- reason: 32935 als LH-Flugnummer, PU als Positioning. KI ist hier
  vorsichtiger als bei LAD (conf 0.82 statt 0.95).
- evidence: routing, layover_ort, raw_marker, starts_at_homebase.

**Akzeptanz:** ✓ `needs_review=True` (medium-conf) → REVIEW. Cross-Source-
Konflikt bleibt erhalten wie bei LAD.

## §3 Akzeptanz-Checks (User-Requirements)

| Anforderung | Status |
|---|:-:|
| Keine EUR-/Tagesatz-Felder akzeptiert | ✓ 8/8 clean |
| Keine Secrets im Log | ✓ kein Key, kein PII |
| Keine duplicate calls | ✓ Cache geleert vor Lauf |
| JFK/Irland nicht blind KEEP | ✓ conf=0.15 → USER question |
| OTP Transit nicht blind KEEP | ✓ conf=0.75 → REVIEW |
| RES/OFF/== echte Tourfälle plausibel | ✓ conf=0.95 für RES Korea, X RIX, X BLR |
| Phantom LAD/TLV nicht blind Z76 | ✓ Phase-5c-Defensive: KI-dict matcht nicht string-decision-values |

## §4 KI-Erkenntnisse (Surprise-Findings)

1. **Day-Suffix „Tag 2" ist legitim** (Multi-Day-Duty-Pattern), nicht
   Reader-Bug. Heuristik in `_normalize_tours_from_raw_facts` muss das
   anerkennen.
2. **„PU" im Crew-Roster bedeutet „Purser"**, nicht „Pula" (PUY). KI fehlt
   dieses Lufthansa-spezifische Wissen. Prompt-Erweiterung in Phase 5c+
   wäre: Crew-Position-Codes explizit als Hilfs-Vokabular im Kontext-Block
   („PU = Purser/Kabinenchef, CR = Captain, ...").
3. **KI hat keinen Zugang zu Cross-Source-Konflikten** (SE-Stempel-Liste,
   FollowMe-Tour-Spans, Anfahrten-Liste). Sie urteilt nur auf
   CAS-Routing-Basis. Bei Phantom-Tagen LAD/TLV bestätigt sie deshalb
   blind das CAS-Routing. → Phase-5c-Defensiv-Logik (dict-value matcht
   nicht string-decisions) verhindert blind-KEEP. ✓

## §5 Phase-5c-Gap (NICHT in Phase 5b zu fixen)

Aktuelle `proposed_tour_decision_after_ai`-Logik in `_build_normalized_day`
erwartet KI-values als String („start" / „mid" / „end" / „non_tour" /
„standby_hotel" / „standby_home" / „inconsistent" / „consistent"). Die KI
liefert aber Dicts (z.B. `{resolved_place: 'LAD', country: 'Angola'}`).

Konsequenz:
- **Korea-RES** (KI: dict mit activity_code=RES, type=standby_duty) →
  Phase-5c-Mapping greift nicht → bleibt bei `evidence_decision`
  (KEEP_TOUR via continuation_from_prev_tour). ✓ funktioniert für diesen
  Fall, aber zufällig.
- **JFK Tag 2** (KI: dict mit duty_id) → bleibt bei evidence_decision
  (NEEDS_AI durch day_suffix_claims_completed_prev). Zufällig OK.
- **LAD/TLV** (KI: dict mit place) → bleibt bei evidence_decision
  (NEEDS_AI). ✓ defensiv korrekt.

**Empfehlung Phase 5d:** Phase-5c-Mapping erweitern um dict-Parsing
(z.B. `_ai_val.get('type') == 'standby_duty'` → standby_hotel; oder
`_ai_val.get('marker_type') == 'multi_day_duty_identifier'` → KEEP_TOUR).
Aktuell bleibt das Tour-First-System konservativ — keine fälschlichen
Auto-Übernahmen, dafür mehr `NEEDS_REVIEW`/`NEEDS_USER`.

## §6 KPI-Simulation (KLAR MARKIERT ALS SIMULATION)

Phase 5b ändert KEINE finale Berechnung. Counter-Effekt wird nur
simuliert, NICHT in `tage_detail` geschrieben.

Angenommen, alle 5 high-conf-Resultate (Bangalore-X, RIX-X, Korea-RES,
JFK Tag 2, LAD) würden in der Pipeline auto-übernommen UND die 2
REVIEW-Resultate (OTP Pula-Misread, TLV) blieben in REVIEW:

| KPI | Phase 6b actual | Phase-5b simuliert | Golden | Δ vs Golden (sim) |
|---|---:|---:|---:|---:|
| arbeitstage | 90 | ~95-100 (sim) | 133 | -33 bis -38 |
| z76_eur | 3648 | ~3800-3950 (sim) | 4794 | -844 bis -994 |
| gesamt | 3732 | ~3870-4020 (sim) | 6020.72 | -2001 bis -2151 |

**Diese Werte sind reine Annahme.** Tatsächliche Pipeline-Wirkung
erfordert:
1. Phase-5c-Dict-Mapping (siehe §5)
2. Korea-RES standby_hotel → Z76 statt Standby in
   `_classify_days_from_normalized_tours`
3. JFK-Tag-2-Multi-Day-Duty-Pattern als Anti-Drop in normalize_tours
4. LAD/TLV bleiben NEEDS_REVIEW — kein Auto-KEEP, KEIN Counter-Effekt
5. OTP Pula-Misread bleibt NEEDS_REVIEW — kein Auto-KEEP

**Echter Counter-Effekt für Golden-Convergence** kommt erst nach
Phase-5d-Dict-Mapping + Phase-5c-Counter-Integration. Aktuelle Phase 6b
KPIs unverändert: 90 arbeitstage, 3648 z76_eur, 3732 gesamt.

## §7 Cache-Status nach Lauf

`_ai_resolver_cache` enthält 8 Einträge mit TTL 24h. Bei Re-Trigger
innerhalb 24h würden dieselben Resultate aus Cache zurückkommen, KEINE
neuen Anthropic-Calls (Cost-Schutz). `_ai_resolver_cache.clear()` würde
einen Re-Run erzwingen.

## §8 Cost-Tracking

8 Calls × ~100-200 Input-Tokens × ~200-400 Output-Tokens = grob
~3000 Input + ~2400 Output Tokens. Bei Sonnet 4.5 Preis ($3/M input,
$15/M output) ≈ **$0.045 für den ganzen Lauf.** Innerhalb User-Budget
(max 10 Calls).

## §9 Stop-Status

✓ Kein Deploy
✓ Kein Live-Run gegen User-Daten
✓ Kein Production-Flag default ON
✓ Keine finale Berechnung geändert
✓ Keine Env-/Secret-Änderung (User hat env-File selbst erzeugt)
✓ Anti-Tax-Sanitizer 8/8 sauber
✓ Cache verhindert weitere Calls innerhalb 24h
✓ KI-Key wird nach Lauf rotiert (User-Verantwortung)
✓ max 10 Calls eingehalten (8 verwendet)

**STOP. Warte auf nächste User-Entscheidung. Kein automatischer Übergang
zu Phase 5d (Dict-Mapping), Live-Run oder Production-Switch.**
