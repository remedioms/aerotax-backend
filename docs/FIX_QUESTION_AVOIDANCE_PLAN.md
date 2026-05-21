# Fix-Plan — „AeroTAX fragt User, was CAS/SE/KI bereits weiß"

**Datum:** 2026-05-14
**Status:** Reine Analyse + Fix-Plan. **Kein Code geändert. Kein Deploy.**
**Quelle:** Live-Job 3fd8cfe1 (Tibor 2025-Run, 365 Tage)

---

## 0. Source-of-Truth-Hierarchie

| Priorität | Quelle | Wofür | Wo gespeichert |
|---|---|---|---|
| 1 | **SE stfrei_ort + stfrei_total** | Steuerlicher Tatbestand — wenn AG steuerfreie Spesen mit Ausland-Ort ausweist, war Crew nachweislich dort | `se.stfrei_ort`, `se.stfrei_total`, `se.stfrei_inland` |
| 2 | **CAS-Zeit (start_time/end_time/duty_minutes)** | Operative Quelle — exakt wann Crew dienstlich anwesend war | `reader_facts.start_time`, `.end_time`, `.duty_duration_minutes` |
| 3 | **DP/CAS-Marker** | Aktivitäts-Kontext (Frei, ORTSTAG, FRS, SB_S, EM, RES, …) | `reader_facts.marker_raw`, `reader_facts.activity_type` |
| 4 | **KI-Fallback** (NEU) | Code-/Place-/Marker-Auflösung wenn deterministic-Pipeline keine Antwort hat | `_resolve_uncertain_fact_with_ai(kind, context)` |
| 5 | **User-Frage** | Last resort — nur wenn Schritte 1–4 nicht reichen | `_review_items` |

**Eiserne Regel:** **KI darf Fakten (Ort/Land/Code/Marker-Semantik) auflösen — aber NIEMALS Steuerbeträge berechnen.** Beträge kommen ausschließlich aus Python + BMF-Tabelle.

---

## 1. Analyse-Tabelle (20 Tage/Codes)

Konventionen:
- **CAS-Fakt**: was im reader_facts steht
- **SE-Fakt**: was die SE-Abrechnung sagt (aktuell oft fehlend in tage_detail — siehe Sub-Bug 1.x)
- **det. möglich?**: kann Python ohne KI entscheiden?
- **KI-Fallback?**: muss KI helfen?
- **conf. Erwartung**: erwartete confidence wenn KI gefragt wird
- **€-Effekt**: erwartet nach Fix vs. aktuell

| # | Datum | CAS-Fakt | SE-Fakt | aktueller Output | Soll | det.? | KI? | conf. | Review? | Fix-Regel-Cluster | Test | €-Effekt |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | **2025-05-17** | marker `==` FRA, kein layover, frei | stfrei SEA 14€ Ausland | Frei 0€ | Z76 SEA (USA) | **ja** | nein | — | **nein** | C1 SE-Override frei→Z76 | `test_se_foreign_overrides_frei_to_z76` | +40€ |
| 2 | **2025-10-07** | marker `X` ICN, overnight | stfrei SEL 14€ Ausland | Frei 0€ | Z76 SEL (Korea) | ja | nein | — | nein | C1 | s.o. | +32€ |
| 3 | **2025-10-15** | marker `==` FRA | stfrei MRS 14€ Ausland | Frei 0€ | Z76 MRS (Frankreich) | ja | nein | — | nein | C1 | s.o. | +36€ |
| 4 | **2025-10-16** | marker `==` FRA | stfrei AGP 14€ Ausland | Frei 0€ | Z76 AGP (Spanien) | ja | nein | — | nein | C1 | s.o. | +23€ |
| 5 | **2025-10-25** | marker `==` FRA | stfrei LON 14€ Ausland | Frei 0€ | Z76 LON (UK) | ja | **ja**¹ | ≥0.95 | nein | C1 + D Metro-Code | s.o. + `test_metro_code_lon_resolves_no_user_question` | +44€ |
| 6 | **2025-11-18** | marker `==` FRA | stfrei SVG 14€ Ausland | Frei 0€ | Z76 SVG (Norwegen) | ja | nein | — | nein | C1 | s.o. | +50€ |
| 7 | **2025-09-26** | marker, layover=`IST`, routing=`KRK`, duty=355min, overnight | (SE-Daten fehlen im tage_detail, aber Tour war Mid-Day) | **Z74 14€ „Inland MUC"** (FALSCH) | Z76 IST (Türkei voll_24h) | ja | nein | — | nein | C2 SE/Layover-Override Z74→Z76 | `test_se_foreign_overrides_z74_to_z76` | +52€ |
| 8 | **2025-09-27** | layover=`AGP`, routing=`IST`, duty=435min, overnight | (gleicher Tour-Mid-Day) | **Z73 14€ „Inland DUS"** (FALSCH) | Z76 AGP (Spanien an_abreise) | ja | nein | — | nein | C2 | `test_se_foreign_overrides_z73_to_z76` | +9€ |
| 9 | **CHI** (mehrere Tage) | SE-stfrei oder CAS-routing | Ausland | None (BMF-Map fehlt) | „USA – Chicago" | ja (Alias) | **ja**¹ | ≥0.95 | nein | D Metro-Code KI-Fallback | `test_city_code_chi_ai_resolves` | unkl. (+30-60€) |
| 10 | **ROM** | gleich | Ausland | None | „Italien – Rom" | ja (Alias) | **ja**¹ | ≥0.95 | nein | D | `test_city_code_rom_ai_resolves` | unkl. |
| 11 | **STO** | gleich | Ausland | None | „Schweden" | ja (Alias) | **ja**¹ | ≥0.95 | nein | D | `test_city_code_sto_ai_resolves` | unkl. |
| 12 | **2025-05-22** | marker `103703`, routing=`['LAD']`, layover='' (leer!), overnight | (Tour-Mid) | Z76 28€ „Angola fallback" (pauschal) | Z76 LAD voll_24h = 52€ | ja (routing[-1]) | nein | — | nein | F Hotel-Place via Tour-Context | `test_hotel_layover_inferred_from_routing` | +14€ |
| 13 | **2025-12-15** | marker `57783`, routing=`['JFK']`, layover='' | (Tour-Mid) | Z76 28€ **„Irland fallback"** (BMF-Map-Bug!) | Z76 JFK = USA an_abreise 40€ | ja | nein | — | nein | F + sek. BMF-Map-Bug | `test_hotel_layover_jfk_maps_usa_not_ireland` | +12€ |
| 14 | **2025-04-23** | marker `RES` FRA, duty=960min (16h), kein overnight | stfrei FRA 14€ Inland | Standby 0€ | Standby 0€ (KORREKT) | ja | nein | — | nein | E SE-Inland-Audit-Note | `test_inland_stfrei_reimbursement_not_double_counted` | 0€ (Log-Cleanup) |
| 15 | **2025-08-01** | Same-Day routing=FRA→NUE→FRA, total=389min (6:29h) | stfrei NUE 14€ Inland | ZeroDay 0€ | ZeroDay 0€ (KORREKT) | ja | nein | — | nein | E | `test_same_day_under_8h_no_z72_even_with_se_stfrei` | 0€ |
| 16 | **2025-10-20** | marker `RES_SB` FRA, duty=960min | stfrei HAM 14€ Inland | Standby 0€ | Standby 0€ | ja | nein | — | nein | E | s.o. | 0€ |
| 17 | **2025-10-23** | marker `RES` FRA, duty=960min | stfrei LEJ 14€ Inland | Standby 0€ | Standby 0€ | ja | nein | — | nein | E | s.o. | 0€ |
| 18 | **2025-01-07** (ORTSTAG) | marker `ORTSTAG`, FRA, **keine Uhrzeit** | (n/a) | Office 0€ + (vorher: review-item) | Office 0€ (kein Anspruch, kein review) | nein direkt | **ja** (Marker-Semantik) | 1× Gruppe ≥0.95 | **1× Gruppe** wenn KI unsicher | B Marker-only Gruppen-Frage | `test_marker_only_ortstag_creates_one_group_question_not_28_day_questions` | 0€ (Review-Cleanup) |
| 19 | **2025-01-10** (EM) | marker `EM`, FRA, **start=07:30 end=11:00 duty=210min** (3:30h) | (n/a) | Office 0€ | Office 0€ — Zeit da, <8h → kein Z72 | **ja** | nein | — | **nein** | A CAS-Zeit deterministisch | `test_em_with_time_no_review_question` | 0€ |
| 20 | **2025-02-01** (SB_S) | marker `SB_S`, FRA, **start=14:00 end=22:00 duty=480min** (8h glatt) | (n/a) | Standby 0€ „kein VMA" | **prüfen** — 8h Dienst + Anfahrt = >8h → evtl. Z72? | ja + Anfahrt-Logik | nein | — | nein | A + E (Standby-Z72-Schwelle) | `test_standby_with_time_classifies_z72_if_total_over_8h` | +14€ pro Tag wenn betroffen |

¹ KI-Fallback kann durch deterministic Alias-Map ersetzt werden — KI ist nur Fallback wenn Alias fehlt. Beide Cluster-D-Codes können primär deterministisch gelöst werden (Code-Tabelle = Alias-Map); KI greift bei seltenen/unbekannten Codes.

**€-Effekt-Summe (Best-Case, alle Cluster gefixt):** +**~322€** (deployed) + **~135-200€** (geplant) = **+455-520€**

---

## 2. Fix-Plan pro Cluster

### Cluster A: CAS-Zeit deterministisch — keine 8h-Frage wenn Zeit da

**Problem:** Backend wirft review-items für Office/Schulung „ohne Zeitinfo" — auch wenn CAS Zeit liefert.

**Code-Stelle:** `app.py:15452-15485` — Office/Training-review-item-Generator
**Aktueller Check:** `if not duty_known_rev … then create item`
**Fix-Regel:** Wenn `duty_duration_minutes > 0` ODER `start_time + end_time` → KEIN review-item, **deterministische 8h-Klassifikation** (Z72 wenn duty+commute ≥480min, sonst kein VMA).

**Existing Z72-Pfad bei `at='training'` (Z.14377-14390) macht das schon richtig**, nur der review-item-Pfad ist zu aggressiv.

**Tests:**
- `test_em_with_time_no_review_question` (Tag 19 oben)
- `test_standby_with_time_no_review_question` (Tag 20)
- `test_flight_with_briefing_no_office_time_question`
- `test_time_present_classifies_deterministically`
- `test_ai_extracts_time_from_raw_cas_when_parser_misses_it` (Cluster H — KI-Fallback wenn Parser scheitert)
- `test_low_confidence_time_extraction_creates_review_question`

### Cluster B: Marker-only → 1 Gruppen-Frage statt N Tagesfragen

**Problem:** 28× gleiche Frage „warst du an Tag X >8h weg?" für identische ORTSTAG-Marker.

**Bereits gefixt (Rev 00051-cfg):** ORTSTAG ohne Uhrzeit erzeugt **kein** review-item mehr + **kein** Z72.

**Aber per User-Prinzip noch nicht ideal:** sollte eigentlich genau **1 Gruppen-Frage** geben („Was bedeutet ORTSTAG bei dir?") wenn KI-Semantik-Resolver unsicher ist. Aktuell: 0 Fragen (silent skip), aber **Gruppen-Frage wäre besser** wenn nicht klar ob Crew das doch beanspruchen will.

**Code-Stelle:** Z.15452+ (skip-Pfad) — neue Logik: bei ≥3 identischen Markern → 1 Gruppen-Item statt skip, mit `affected_days[]`.

**Tests:**
- `test_marker_only_ortstag_creates_one_group_question_not_28_day_questions`
- `test_marker_answer_passive_resolves_all_ortstag_items`
- `test_marker_answer_office_applies_to_all_or_asks_followup`
- `test_marker_only_no_auto_z72_without_confidence` (✓ schon erfüllt)
- `test_marker_only_no_daily_8h_spam`
- `test_ai_marker_semantics_high_confidence_no_user_question` (Cluster H)
- `test_low_confidence_marker_semantics_grouped_review_question`

### Cluster C1: SE-Auslandsspesen → Override Frei (deployed Rev 00052-h9x)

**Status:** ✓ live für `at='frei'`. **Nicht** für Z73/Z74/Inland-Klassen → siehe C2.

### Cluster C2: SE-Override / Foreign-Layover → Z73/Z74 → Z76

**Problem:** 2025-09-26 layover=IST aber klass=Z74 (Inland). 2025-09-27 layover=AGP aber klass=Z73 (Inland).

**Code-Stelle suchen:** Klassifikator-Pfad für Tour-Mid-Days wo Z73/Z74 vergeben werden, ohne `_is_inland_code(layover_ort)` zu prüfen.
- Vermutlich `_deterministic_classify_v7` Z.14000–14700 (Tour-Tag-Mid-Day-Pfade)
- Plus: Reason-Text „Inland-Mittel-Tag MUC" / „Inland-Layover DUS" ist Tour-Aggregat — gehört nicht zu **diesem** Tag

**Fix-Regel:**
1. **VOR** Z73/Z74-Vergabe: prüfe `layover_ort` gegen `_is_inland_code()`. Wenn `False` → Z76 mit BMF aus layover_ort.
2. **Plus:** Reason-Text muss layover-tag-spezifisch sein (nicht aus Tour-Cluster übernommen).

**Tests:**
- `test_se_foreign_overrides_frei_to_z76` (deployed, regression)
- `test_se_foreign_overrides_z73_to_z76` (Tag 8)
- `test_se_foreign_overrides_z74_to_z76` (Tag 7)
- `test_se_foreign_does_not_double_count`
- `test_se_inland_does_not_force_z76`
- `test_ai_resolves_se_place_high_confidence_no_user_question`
- `test_ai_place_resolution_never_computes_tax_amount`
- `test_bmf_amount_still_from_python_table`

### Cluster D: Metro-City-Codes (CHI/ROM/STO/LON/NYC/PAR/TYO…)

**Problem:** IATA-Metro-Codes nicht in `IATA_TO_BMF`. Aktuell stille Pauschal-Fallback auf 28€.

**Fix-Regel — zweistufig:**
1. **Deterministischer Alias-Layer:** Tabelle `IATA_METRO_TO_BMF` in `bmf_data.py`:
   ```python
   IATA_METRO_TO_BMF = {
     'CHI': 'Vereinigte Staaten von Amerika (USA) – Chicago',
     'ROM': 'Italien – Rom',
     'STO': 'Schweden',
     'LON': 'Vereinigtes Königreich – London',
     'NYC': 'Vereinigte Staaten von Amerika (USA) – New York',
     'PAR': 'Frankreich – Paris',
     'TYO': 'Japan - Tokyo',
     'WAS': 'Vereinigte Staaten von Amerika (USA) – Washington, D.C.',
     'MOW': 'Russland - Moskau' if exists else 'Russland',
   }
   ```
   Lookup-Order: `IATA_TO_BMF[code] or IATA_METRO_TO_BMF[code]`.
   Bei Treffer in Alias-Layer: `_rescues.append({type:'metro_code_alias'})`.
2. **KI-Fallback (Cluster H):** wenn weder primär noch Alias greift → KI-`place_code`-Resolver.

**Tests:**
- `test_bmf_city_code_chi_maps_to_usa_chicago` (deterministic Alias)
- `test_bmf_city_code_rom_maps_to_italy_rome`
- `test_bmf_city_code_sto_maps_to_sweden`
- `test_bmf_city_code_lon_maps_to_uk_london`
- `test_unknown_city_code_remains_unresolved_not_zero`
- `test_metro_code_logs_rescue_entry`
- `test_city_code_chi_ai_resolves_with_high_confidence_no_user_question` (Cluster H Fallback)
- `test_city_code_resolution_uses_python_bmf_amount`

### Cluster E: SE-Inland 14€ — Audit-Note statt unresolved

**Problem:** `_vma_unmapped_se`-Liste enthält 4 Tage mit SE-Inland-stfrei 14€. Backend hat klass=Standby/ZeroDay (korrekt), die SE-Zeile ist AG-Erstattung (per BMF auf Z72 angerechnet → kein zusätzlicher Werbungskosten-Anspruch).

**Fix-Regel:** Wenn `klass in ('Standby', 'ZeroDay')` UND SE-stfrei_inland=True UND stfrei_total ≤ INLAND_TAGESTRIP_8H — das ist **erwarteter Zustand**, NICHT „unmapped". → kein Eintrag in `_vma_unmapped_se`.

**Plus FollowMe-Cross-Check:**
Da Tibor-Golden 133 Arbeitstage hat (gleich wie AeroTAX), sind diese 4 Tage in FollowMe-Soll vermutlich:
- 04-23: Standby (kein Z72) ← bestätigt durch Golden=133 Arbeitstage
- 08-01: ZeroDay <8h
- 10-20: Standby (HAM)
- 10-23: Standby (LEJ)

→ aktuell **korrekt** klassifiziert. Cluster E ist nur Audit-Log-Cleanup.

**Tests:**
- `test_inland_stfrei_reimbursement_not_double_counted`
- `test_followme_z73_inland_arrival_day_if_applicable`
- `test_tibor_specific_2025_04_23`
- `test_tibor_specific_2025_08_01`
- `test_tibor_specific_2025_10_20`
- `test_tibor_specific_2025_10_23`

### Cluster F: Hotel/Layover-Place via Tour-Context

**Problem:** 05-22 + 12-15 overnight=true, layover_ort leer → Fallback auf „Cluster=Ausland → 28€ Pauschal" + bei 12-15 falsch auf „Irland".

**Fix-Regel — Fallback-Kaskade:**
1. `layover_ort` aus reader_facts (primär)
2. `routing[-1]` (Ziel-Ort der letzten Etappe) wenn overnight=true UND routing nicht-leer
3. `routing[0]` (Anfangs-Ort) wenn Tag 1 einer Tour
4. SE-`stfrei_ort` wenn da
5. KI-Fallback: `_resolve_uncertain_fact_with_ai('layover_place', context)` — Context = CAS-raw-Lines diesen Tag + Vortag + Folgetag
6. Wenn KI <0.80 → konkrete Review-Frage „War die Übernachtung am xx.xx in [KI-Vorschlag]?"

**Plus:** BMF-Mapping-Bug für JFK→Irland (12-15) untersuchen — vermutlich Cluster-Tour-BMF-Land-Bug der bei layover_ort='' den BMF-Land des Cluster-„foreign"-Markers nimmt.

**Tests:**
- `test_hotel_layover_ort_inferred_from_neighbor_day`
- `test_hotel_not_counted_when_overnight_without_reliable_place`
- `test_ai_infers_layover_place_from_tour_context`
- `test_low_confidence_hotel_place_creates_specific_review`
- `test_tibor_hotel_count_moves_toward_66_not_away`
- `test_no_hotel_double_count_on_return_day`
- `test_specific_2025_05_22_hotel_decision`
- `test_specific_2025_12_15_hotel_decision`

### Cluster G: Review-Item-Schema (Source-Excerpt + Confidence + Affected-Days)

**Aktuell:** Review-Items haben nur `datum`, `marker`, `reason`, `options`. **Keine** source-evidence, **keine** AI-suggestion, **keine** affected_days-Gruppierung.

**Neues Schema (RFC):**
```python
{
  'id': 'cluster:type:context-hash',   # statt 'office_training_time_missing:DATUM'
  'kind': 'place_code'|'cas_time'|'marker_semantics'|'layover_place'|'tour_context',
  'severity': 'red'|'yellow'|'green',
  'source_type': 'CAS'|'SE'|'LSB'|'KI',
  'source_excerpt': 'CAS-Zeile: "EM /1 A320 FRA 07:30-11:00"',
  'why_not_resolved': 'Parser konnte Uhrzeit nicht extrahieren (KI-conf=0.65)',
  'affected_days': ['2025-01-07','2025-01-12',...] OR ['single'],
  'suggested_answer': {value, confidence, reason},   # wenn KI eine Vermutung hat
  'options': [...],
  'money_impact_estimate': float,
  'status': 'pending'|'answered'|'skipped',
}
```

**Tests:**
- `test_no_review_item_if_cas_time_present`
- `test_no_review_item_if_se_foreign_present`
- `test_marker_review_grouped_once`
- `test_review_item_includes_source_excerpt`
- `test_review_question_asks_cause_not_symptom`
- `test_review_count_not_inflated_by_marker_repetition`
- `test_ai_suggestion_shown_for_medium_confidence`
- `test_high_confidence_ai_resolution_no_review`

### Cluster H: KI-Fallback-Architektur

**Funktion (vorgeschlagen, in app.py):**
```python
def _resolve_uncertain_fact_with_ai(kind, context, job_id=None):
    """Single-purpose AI resolver for facts the deterministic pipeline couldn't
    answer. KIND in {'place_code','cas_time_extraction','marker_semantics',
    'layover_place','tour_context'}.

    KI darf NIEMALS Steuerbeträge berechnen. Output ist faktisch (Ort/Land/Code/
    Marker-Bedeutung), Beträge kommen aus Python+BMF-Tabelle.

    Returns:
      {
        'resolved': bool,
        'value': dict,
        'confidence': float,  # 0.0-1.0
        'reason': str,        # <= 200 chars, keine PII
        'evidence': list[str],# raw-line excerpts max 100 chars each
        'needs_review': bool, # True wenn confidence < 0.95
      }
    """
```

**Wichtig — Architektur-Regeln:**
1. **Cache:** `(job_id, datum, kind, context-hash)` → 24h TTL. Kein doppelter KI-Call pro Tag.
2. **max_tokens klein** (256–512). Strict JSON-only-response (system-prompt enforced).
3. **JSON-Schema-Validation** mit `jsonschema` lib. Bei invalid JSON: `resolved=False, needs_review=True`.
4. **Audit-Log:** strukturiert via `app.logger.info('[ai-resolver] kind=... conf=...')` — KEINE raw PDFs, KEIN Base64, KEINE PII.
5. **Steuerbetrag-Sanitizer:** wenn `value` ein numerisches Feld enthält das wie ein Geldbetrag aussieht (`amount`, `eur`, `rate`, etc.) → reject, force `needs_review=True`.
6. **Confidence-Schwellen:**
   - **≥ 0.95** → `resolved=True, needs_review=False` → automatisch übernehmen
   - **0.80–0.95** → `resolved=True, needs_review=True` → Review mit suggestion
   - **< 0.80** → `resolved=False, needs_review=True` → User-Frage ohne suggestion

**Tests:**
- `test_ai_resolution_json_schema_validated`
- `test_ai_resolution_invalid_json_falls_back_to_review`
- `test_ai_resolution_cached_per_job_day_kind`
- `test_ai_resolution_does_not_include_tax_amount`
- `test_ai_resolution_rejects_value_with_money_field`
- `test_ai_resolution_confidence_thresholds`
- `test_ai_resolution_audit_no_pii`

---

## 3. Reihenfolge der Implementierung

| Phase | Cluster | Abhängigkeit | Aufwand | Risiko |
|---|---|---|---|---|
| **Phase 1** | **A** CAS-Zeit deterministisch | unabhängig | klein | klein — review-item-Skip-Bedingung erweitern |
| **Phase 1** | **C2** SE/Layover-Override Z73/Z74→Z76 | unabhängig | mittel | mittel — Tour-Reason-Sharing-Bug muss verstanden werden |
| **Phase 2** | **D** Metro-Code-Alias deterministisch | unabhängig (KI-Fallback separat) | klein | klein — neue Map in bmf_data.py |
| **Phase 2** | **F** Hotel-Layover-Fallback (deterministisch) | unabhängig | klein | mittel — Hotel-Count Trend zu Golden 66 prüfen |
| **Phase 3** | **G** Review-Item-Schema erweitern | abhängig von Phase 1+2 | mittel | klein |
| **Phase 4** | **H** KI-Fallback-Architektur | abhängig von G | groß | hoch — neue API-Surface, Cache, Validation, Cost |
| **Phase 4** | **B** Marker-only Gruppen-Frage | abhängig von G + H | mittel | mittel — KI-marker-semantics-Resolver |
| **Phase 5** | **E** Audit-Log-Cleanup | unabhängig | klein | minimal |

**Empfehlung:** Phase 1+2 zuerst (alles deterministisch, kein KI-Risk, niedrige Kosten, sofortiger €-Effekt). Dann Phase 3 (Review-Schema). Dann Phase 4 (KI-Fallback, größte Architektur-Änderung). Phase 5 jederzeit nebenher.

---

## 4. Was ich NICHT mache (ohne weitere Freigabe)

- Keine neuen Live-Runs (Kosten)
- Kein Deploy
- Keine `bmf_data.py` Änderung
- Keine neue KI-Aufruf-Stelle ohne komplette H-Architektur
- Keine Test-Files anlegen ohne Cluster-by-Cluster-Freigabe

---

## 5. Pflicht-Entscheidungen vor Code

1. **Reihenfolge OK** (Phase 1+2 → 3 → 4 → 5)?
2. **Bei Cluster B:** soll bei 28 identischen ORTSTAG-Markern eine **Gruppen-Frage** entstehen, oder reicht aktueller silent-skip (Rev 00051-cfg)? Gruppen-Frage = explizite Bestätigung „passive Marker = kein VMA", silent-skip = stille Annahme.
3. **Bei Cluster H KI-Fallback:** Anthropic Sonnet 4.5 nutzen (gleiche Kosten wie aktuelle Reader-Calls) oder kleineres Modell (Haiku) für schnelle Code-/Place-/Marker-Resolves? Confidence-Reporting muss sauber funktionieren.
4. **Bei Cluster D Metro-Codes:** zustimmen zur Alias-Map mit `CHI/ROM/STO/LON/NYC/PAR/TYO/WAS/MOW` als initial Set? Oder breiter (alle IATA Metro Area Codes)?
5. **Bei Cluster F Hotel-Place:** zustimmen zur Fallback-Kaskade `layover_ort → routing[-1] → SE-stfrei_ort → KI`?

Nichts geändert. Warte auf deine Antworten + Freigabe pro Cluster.
