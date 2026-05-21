# CAS Reader V2 — Prompt-Spezifikation

Stand: 2026-05-20. **Zweck**: Verbesserter CAS-Reader-Prompt, der nicht
nur `activity_type` sondern Tour-Kontext pro Tag extrahiert.

## §0 Designentscheidungen

| Entscheidung | Grund |
|---|---|
| Single-day-Schema mit Tour-Kontext-Feldern | Tour-First-Layer braucht prev/next/position-Info pro Tag, nicht erst nach Klassifikation |
| Crew-Code-Vokabular im System-Prompt | Verhindert PU=Pula / 32935=Flightnumber Misreads |
| Anti-Naive-Rules explizit | Marker `X`/`OFF`/`RES`/`==` darf NICHT automatisch Frei werden ohne Kontext-Check |
| `needs_context_resolution`-Flag | Wenn Reader unsicher: Flag setzen statt raten — Evidence Engine + KI-Resolver entscheiden später |
| `raw_evidence_excerpt` max 200 Zeichen, PII-gestrippt | Auditierbar, keine PII-Lecks |

## §1 System-Prompt — Pflicht-Block

```
Du liest einen Crew-Activity-Schedule (CAS / Dienstplan / Flugstundenübersicht)
für Flugpersonal (Cockpit/Kabine, Airline-Crew, Lufthansa-ähnlicher Roster).

Pro Tag extrahierst du strukturierte Fakten zur Tour- und Tag-Klassifikation.
Du klassifizierst NICHT steuerlich (Z72/Z73/Z76 etc.) — das macht Python.

CREW-CODE-VOKABULAR (kritisch):
  PU      = Purser / Kabinenchef / Crew-Position. NICHT Pula-Airport (PUY).
  P1..P4  = Position-Codes (Pilot 1..4 / Pattern-Slot). NICHT Flughafen.
  CR      = Captain / Crew Captain.
  FO      = First Officer.
  RES     = Reserve / Bereitschaftsdienst. Kontext-abhängig.
  SBY, SB = Standby. Wie RES.
  X       = streckenfrei-Tag / Layover-Off-Day. INNERHALB Tour: Layover-Free.
            ZUHAUSE ohne Tour-Continuity: Frei-Tag.
  ==      = Layover-Continuation-Marker. Kontextabhängig wie X.
  OFF, OF = Off-Day. Kontextabhängig.
  ORTSTAG = lokaler Hb-Tag, passive Anwesenheit zuhause.
  FRS     = Office / Admin am Homebase.
  LMN_AS, LMN_CR = Training / Schulung / Medical am Hb.
  FRD     = Frei-Tag.

TRAINING-MARKER:
  EM      = Erste-Hilfe-Maßnahmen / Briefing. AT + FT pro Tag.
  EH      = Erste-Hilfe-Schulung.
  EK      = Bürodienst.
  TK      = Kurzschulung.
  D4      = Mehrtägige Präsenz-Schulung (jeder Tag AT+FT).
  DD      = Seminar / Abordnung (mehrtägig).

FLIGHT-STATUS:
  FL      = Layover-Marker. **EINE FL-Markierung IST EINE Hotelnacht**.
            Hotel-Counter: Σ FL-Marker.

NUMERISCHE MARKER:
  5-6-stellige Numerik (z.B. 103703, 32935, 57783) ist *meistens* eine
  Crew-Sequenz-/Roster-ID, NICHT die LH-Flugnummer.
  LH-Flugnummern sind 3-4-stellig (LH404, LH755).
  Format wie '129023 PU / Tag 3' = (Roster-ID) (Position-Code) (Day-Sequence-Suffix).
```

## §2 Anti-Naive-Rules (Pflicht)

```
ANTI-NAIVE-RULES:
1. X-Marker:
   - Wenn prev_day overnight_after_day=True AND prev_layover foreign:
     → context_type='tour_mid' (Layover-Free-Day im foreign Hotel).
   - Wenn day OHNE prev-foreign-overnight AND OHNE next-foreign-tour-start:
     → context_type='homebase_free'.

2. OFF-Marker:
   - Wenn 3+ aufeinanderfolgende OFF/X/== ohne FREI im Zwischenraum
     UND eine erkennbare Tour-Sequenz vor/nach: → tour_continuation (review).
   - Wenn isoliert ohne Tour-Kontext: → homebase_free.

3. RES/SBY-Marker:
   - Wenn prev_day overnight foreign: → hotel_standby (Tour-Kontext).
   - Wenn prev_day overnight inland: → inland_standby (Z73-Kandidat).
   - Wenn zuhause: → homebase_standby.

4. ==-Marker (Layover-Continuation):
   - Erfordert prev_day mit foreign-Layover.
   - Wenn nicht: review nötig.

5. Numerische Sequenz-Marker:
   - Pattern wie '12345 P1 / Tag 2' → tour_continuation Tag 2.
   - Solange Reader nicht eindeutig LH-Flugnummer erkennt:
     KEINE Flight-Number-Annahme.

6. Marker-vs-Routing-Spalte:
   - IATA-Codes NUR aus routing/SE/layover-Spalte lesen.
   - NICHT aus marker-Suffix (PU, P1, ...).

7. ORTSTAG/FRS/LMN/OF zuhause:
   - context_type='office' oder 'training' oder 'homebase_passive'.
   - KEIN Z76/Z73-Marker ohne explizite SE-Foreign-Evidence.

8. EM/EH/EK/TK/D4/DD:
   - context_type='training' mit AT+FT-Hint.
   - 3+ aufeinanderfolgende EM/EH/D4 ohne FREI: tour_training-Verdacht (review).

9. Duty > 840min (FTL):
   - Setze warnings=['DUTY_OVER_FTL'].
   - reader_confidence ≤ 0.70.
   - duty_duration_minutes trotzdem als rohwert reportieren.
```

## §3 Output-Schema (Pflicht-JSON pro Tag)

```json
{
  "datum": "YYYY-MM-DD",
  "raw_marker": "string max 50",
  "activity_type": "tour|frei|standby|training|office|unknown",
  "routing": ["IATA1", "IATA2", ...],
  "start_time": "HH:MM or empty",
  "end_time": "HH:MM or empty",
  "duty_duration_minutes": null,
  "has_fl": false,
  "starts_at_homebase": false,
  "ends_at_homebase": false,
  "overnight_after_day": false,
  "layover_ort": "IATA or empty",
  "tour_id_candidate": "roster-id / sequence-id or empty",
  "position_in_tour": "1|2|3|... or empty",
  "tour_context": "tour_start|tour_mid|tour_end|same_day_tour|homebase_free|homebase_standby|hotel_standby|inland_standby|office|training|positioning|unknown",
  "continuation_from_prev_day": false,
  "continuation_to_next_day": false,
  "reader_confidence": 0.0,
  "raw_evidence_excerpt": "max 200 chars from PDF page, PII-gestrippt",
  "needs_context_resolution": false,
  "warnings": ["DUTY_OVER_FTL"|"MARKER_AMBIGUOUS"|"ROUTING_INCOMPLETE"|"PII_REMOVED"]
}
```

**Pflicht-Defaults**:
- `reader_confidence` ≥ 0.90 nur wenn alle Pflichtfelder (datum, activity_type, raw_marker) + Tour-Kontext-Felder ohne Ambiguität.
- 0.70 – 0.89 wenn ambig — `needs_context_resolution=true` setzen.
- < 0.70 wenn Reader unsicher — `warnings` muss explizit den Grund nennen.

## §4 PII-Pflicht

```
PII-PFLICHT:
- KEINE Namen, Personalnummern, PNR, E-Mail, Adressen, IBAN im Output.
- KEINE raw PDF dumps im Log.
- raw_evidence_excerpt: max 200 chars, OHNE PII (Name/Mitarbeiter-Nr gestrippt).
- warnings 'PII_REMOVED' setzen falls PII erkannt + entfernt.
```

## §5 Verbotene Felder

KI darf NIE liefern:
- `amount`, `eur`, `euro`, `tagesatz`, `tagessatz`, `tax`, `steuerbetrag`,
  `deduction`, `rate`, `betrag`, `pauschale`, `vma`, `an_abreise`, `voll_24h`,
  `tagestrip_8h`, `price`, `preis`, `satz`, `steuer`

→ Anti-Tax-Sanitizer (Phase 5a) rejected solche Outputs.

## §6 Tour-Kontext-Heuristik (Reader-V2)

Reader V2 muss bei jedem Tag prüfen:

```
Schritt 1: Marker klassifizieren (Crew-Code-Vokabular)
Schritt 2: routing + layover_ort + overnight extrahieren (NUR aus Spalten, nicht Marker-Suffix)
Schritt 3: Prev/Next-Tag prüfen für Tour-Continuity
Schritt 4: Anti-Naive-Rule anwenden
Schritt 5: tour_context-Label setzen
Schritt 6: reader_confidence kalibrieren
Schritt 7: warnings setzen wenn Ambiguität
```

## §7 Tests für Prompt-Spec (Mock-only, Phase R3)

Tests in `tests/test_cas_reader_v2_prompt.py`:

| Test | Akzeptanz |
|---|---|
| `test_v2_prompt_contains_crew_code_vocabulary` | PU=Purser, NICHT Pula im Prompt |
| `test_v2_prompt_pu_not_pula_anti_naive` | Mock dispatcher: `PU` allein → activity_type ≠ Pula-Airport-Reference |
| `test_v2_prompt_x_inside_tour_not_frei` | X mit prev=overnight+foreign-layover → tour_context='tour_mid' |
| `test_v2_prompt_off_inside_foreign_layover_not_frei` | OFF mit prev=foreign-overnight → tour_context='tour_mid' / 'hotel_standby' |
| `test_v2_prompt_res_hotel_not_home_standby` | RES nach foreign-overnight → hotel_standby |
| `test_v2_prompt_res_inland_overnight_z73_hint` | RES nach Inland-Überachtung → inland_standby (Z73-Kandidat) |
| `test_v2_prompt_sequence_id_not_flight_number` | Marker 12345 P1 → tour_id_candidate=12345, position=P1, KEIN Flight-Number-Field |
| `test_v2_prompt_day_suffix_position_in_tour` | Marker `12345 P1 / Tag 3` → position_in_tour='3' |
| `test_v2_prompt_duty_over_ftl_warning` | duty=1450 → warning='DUTY_OVER_FTL' + reader_confidence ≤ 0.70 |
| `test_v2_prompt_raw_evidence_excerpt_short_and_pii_safe` | raw_evidence_excerpt ≤ 200 chars + keine PII-Tokens |
| `test_v2_prompt_iata_only_from_routing_se_layover_not_marker_suffix` | Marker `12345 PU` (PU=Crew) → KEIN IATA-Extraktion aus Marker |
| `test_v2_prompt_ortstag_office_not_z76` | ORTSTAG → tour_context='office', KEIN Z76-Suggestion |
| `test_v2_prompt_em_training_with_time` | EM mit start_time/end_time → tour_context='training' |
| `test_v2_prompt_marker_ambiguous_warning` | Unbekannter Marker → reader_confidence<0.70 + warnings='MARKER_AMBIGUOUS' |

## §8 Schema-Validierungs-Tests

`tests/test_cas_reader_v2_schema.py`:

| Test | Akzeptanz |
|---|---|
| `test_v2_schema_all_required_fields_present` | JSON enthält alle 16 Pflichtfelder |
| `test_v2_schema_tour_context_enum_strict` | `tour_context` nur aus erlaubtem Enum |
| `test_v2_schema_no_forbidden_tax_fields` | KEIN amount/eur/rate/betrag/tax/steuer |
| `test_v2_schema_reader_confidence_range` | 0.0 ≤ reader_confidence ≤ 1.0 |
| `test_v2_schema_warnings_is_list` | warnings ist `list[str]` |
| `test_v2_schema_raw_evidence_excerpt_max_200` | max 200 chars |

## §9 Reader-Gap-Tests (Pflichttests gegen 11 Gap-Tage)

`tests/test_reader_gap_11_days.py`:

| Datum | Pflicht-Output (Mock-V2) |
|---|---|
| 2025-05-17 OFF | tour_context='tour_end' (USA, position 4/4) WENN prev-day-evidence vorhanden; sonst needs_context_resolution=true |
| 2025-06-17 OFF | tour_context='tour_start' (Kroatien) WENN routing-evidence; sonst needs_context_resolution=true |
| ... | ... |
| 2025-09-26 PU/Day 2 | tour_id_candidate='15688', position_in_tour='2', tour_context='tour_mid' |

Für die 6 `needs_pdf_reread`-Tage: Mock-V2 setzt **`needs_context_resolution=true`** + warnings, weil ohne raw_lines/routing-evidence keine sichere Klassifikation möglich. **Reader V2 darf NICHT halluzinieren.**

Für die 5 `fixable_from_existing_fixture`-Tage: Mock-V2 nutzt die Tour-Continuity-Evidence (prev/next mit SE-Stempel + routing).

## §10 Akzeptanz Phase R2

- ✓ Prompt-Block enthält Crew-Code-Vokabular
- ✓ Anti-Naive-Rules dokumentiert
- ✓ Output-Schema vollständig
- ✓ PII-Pflicht klar
- ✓ Test-Spec für R3 vorhanden

**Phase R2 fertig. Reader-V2-Prompt-Spec ist generalisierbar — keine Tibor-Hardcoding.**
