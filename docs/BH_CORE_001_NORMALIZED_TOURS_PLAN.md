# BH-CORE-001 — Normalized-Tours-Layer (Tour-First-Classifier)

Stand: 2026-05-19. **Plan-Doc. Kein Code. Kein Deploy. Kein Re-Run.**

External-Review-Ergebnis: Die aktuelle Pipeline klassifiziert **Tage** und versucht **danach** Tour-Aggregation. Das zerreißt CAS-Touren bei `X/==/OFF/RES`-Markern in der Tour-Mitte. Lösung: **Tour-Erkennung VOR Tag-Klassifikation**.

---

## 1. Aktuelle Pipeline (Status Quo) — Problem

```
Reader (Sonnet)
    ↓ raw reader_facts pro Tag (activity_type, routing, overnight, layover_ort, ...)
_match_dp_se_per_day
    ↓ matched_days [{datum, dp, se}]
_deterministic_classify_v7                  ◀── klassifiziert TAGE
    ↓ tage_detail [{klass, eur, ...}]       ◀── klass ∈ {Z76, Z73, ..., Frei, Issue}
_followme_align_counters                    ◀── baut Touren DANACH aus klass
    ↓ Tour-Sequenzen, KPI-Aggregation
final result_data
```

**Defekt-Pattern:** Ein Tag mit Marker `X` mitten in Bangalore-Tour wird vom Reader als `activity_type='frei'` gelesen → Classifier setzt `klass='Frei'` → `_followme_is_service_day` returnt False → **Tour wird gesplittet** → Tag fehlt in Z76-Aggregation.

Konkret: 15 X-Tage, 9 RES-Tage, 4 ==-Tage, 3 OFF-Tage → **31 Tour-Mitte-Tage verloren**.

---

## 2. Neue Pipeline (BH-CORE-001) — Soll

```
Reader (Sonnet)
    ↓ raw reader_facts pro Tag (UNVERÄNDERT)
_match_dp_se_per_day                        ◀── UNVERÄNDERT
    ↓ matched_days [{datum, dp, se}]
┌─────────────────────────────────────────────────────────────────┐
│ NEU: _normalize_tours_from_raw_facts(matched_days)              │
│ - Erkennt Tour-Membership AUS raw facts (NICHT aus klass)       │
│ - KI-Resolver für unklare Marker (X/==/OFF/RES) im Kontext      │
│ - Output: normalized_tours mit role/location_context pro Tag    │
└─────────────────────────────────────────────────────────────────┘
    ↓ normalized_tours
_deterministic_classify_v7(matched_days, normalized_tours=...)
    ↓ tage_detail mit klass abgeleitet AUS Tour-Rolle + Location
    (deterministisch: gleicher Tour-Kontext → gleicher klass)
_followme_align_counters                    ◀── nimmt Tours direkt aus normalize-Layer
    ↓ KPI-Aggregation
final result_data
```

**Invariante:** Tour-Membership darf **nicht** von `klass != 'Frei'` abhängen. Sie hängt nur von **raw evidence** ab:
- `overnight_after_day`
- `layover_ort` (auch wenn Reader nur 1× gesetzt hat)
- `routing` continuity (Auslands-Code in routing)
- SE `stfrei_ort` (auch Inland-Stamp ist evidence)
- Vortags-/Folgetags-Pattern
- Marker mit Airport/City-Code (`X HKG`, `X BLR`, `X TLV`, `X HND`, `X BOM`, ...)
- Marker mit Schichtkontext (`RES`, `EM`, `ORTSTAG`, `FRS`, `LMN_AS`, `LMN_CR`)
- KI-Resolver bei unklarer Bedeutung

---

## 3. Datenmodell `normalized_tours`

```
normalized_tours = [
  {
    tour_id:        str  # z.B. "T01_2025-01-03_FRA-BLR"
    start_date:     str  # ISO-date
    end_date:       str  # ISO-date
    homebase:       str  # z.B. "FRA"
    primary_destination: str  # erkannt aus routing/layover, z.B. "BLR" (Bangalore)
    destination_country: str  # BMF-Land, z.B. "Indien - Bangalore"
    tour_size:      int  # days_count
    tour_pattern:   str  # "single_dest" | "multi_stop" | "same_day" | "deadhead"
    is_foreign:     bool
    is_inland:      bool
    is_mixed:       bool  # Inland-Stopp innerhalb Auslands-Tour (z.B. BER mid-Madrid)
    days: [
      {
        datum:               str
        role:                str  # "tour_start" | "tour_mid" | "tour_end" | "same_day" | "non_tour"
        raw_marker:          str
        activity_type_raw:   str  # was Reader sagte
        routing:             list[str]
        layover_ort:         str  # roher Wert
        inferred_layover_ort: str  # nach Kaskade aufgelöst (BH-Phase-4-Helper)
        layover_country:     str  # BMF-Land oder ""
        location_context:    dict {
          'at_homebase':        bool
          'at_foreign_hotel':   bool
          'in_flight':          bool
          'in_transit':         bool
          'standby_at_home':    bool
          'standby_at_foreign': bool
        }
        se_context: dict {
          'has_se_stamp':       bool
          'stfrei_ort':         str
          'stfrei_inland':      bool
          'stfrei_betrag':      float
        }
        marker_semantics:    str   # via KI: "office_passive_at_home" | "office_active_with_commute" |
                                   #          "tour_mid_at_hotel" | "standby_at_hotel" | ...
        confidence:          float # 0.0-1.0
        evidence:            list[str]  # menschenlesbare Begründungen
        needs_review:        bool
      }
    ]
  }
]
```

---

## 4. Tour-Detection-Algorithmus

### 4.1 Stufe 1 — Hard-Evidence-Tour-Boundaries

Sortiere `matched_days` nach Datum. Iteriere durch und sammle Tour-Tage:

**Tour-START:** Tag erfüllt MIND. EINS:
- `starts_at_homebase=True` UND `routing[0]==homebase` UND `routing[-1]!=homebase`
- `activity_type='tour'` UND `requires_commute=True` UND Vortag war NICHT overnight
- `has_fl=True` + routing zeigt Auslands-Ziel UND Vortag-overnight=False

**Tour-Mitte (Continuation):** Tag erfüllt MIND. EINS:
- Vortag hatte `overnight_after_day=True`
- `layover_ort != homebase` (auch wenn klass=Frei)
- Marker enthält Airport/City-Code (`X HKG`, `X BLR`, ...)
- `routing` ohne Homebase-Element (= Crew ist nicht zuhause)
- SE-stamp foreign auf dieser Tour-Datum-Range
- KI-Resolver bestätigt mit `conf≥0.85` dass Marker = Tour-Mitte

**Tour-Ende:** Tag erfüllt:
- `ends_at_homebase=True` UND Vortag overnight=True UND routing[-1]==homebase

**Non-Tour:** keiner der Tour-Indikatoren.

### 4.2 Stufe 2 — KI-Resolver für unklare Marker

Wenn Stufe 1 kein eindeutiges Tour-Mid liefert ABER Indizien existieren (Vortag overnight=True, layover_ort gesetzt etc.), rufe KI:

```
kind: 'tour_context'
context: {
  'datum':           '2025-01-04',
  'marker':          'X',
  'activity_type_raw': 'frei',
  'routing':         [],
  'layover_ort':     'BLR',
  'prev_day':        {'datum':'2025-01-03', 'marker':'31591 P1', 'routing':['FRA','BLR'], 'overnight':True},
  'next_day':        {'datum':'2025-01-05', 'marker':'755 LH755-1', 'routing':['BLR','FRA'], 'overnight':True},
  'se_stamp':        {'has': False},
  'crew_context':    'Airline-Crew-Roster, Cockpit/Kabine, Lufthansa-ähnlich.
                      Marker X in einer Tour-Sequenz bedeutet typischerweise:
                      - bei foreign overnight + layover: Hotel-Rest-Day (Tour-Mitte)
                      - bei homebase + kein overnight: Frei/OFF (kein VMA)'
}
return: {
  'resolved': true,
  'value': {'role': 'tour_mid', 'location': 'at_foreign_hotel', 'destination': 'Bangalore (BLR)'},
  'confidence': 0.92,
  'reason': 'Marker X eingebettet zwischen 2 overnight-Tagen mit BLR-Layover',
  'evidence': ['prev.overnight=True', 'prev.layover=BLR', 'next.routing=BLR→FRA'],
  'needs_review': false
}
```

KI darf **NIEMALS**:
- EUR-Beträge
- BMF-Tagessätze
- Z72/Z73/Z74/Z76 als Output (sind Klassifikator-Decision)

### 4.3 Stufe 3 — Tour-Boundary-Repair

Wenn Stufe 1 einen Tour-Gap-Tag findet (z.B. X-Tag mit klass=Frei zwischen 2 Z76-Tagen), erweitere Tour-Range über den Gap. Nutze KI conf≥0.70 für unsichere Erweiterungen, sonst hartcoded Mid-Tour wenn Vortag-overnight=True UND Folgetag-overnight=True (Sandwich-Pattern).

### 4.4 Spezielle Marker-Patterns

| Marker | im foreign tour-context | zuhause ohne Tour |
|---|---|---|
| `X` | Tour-Mitte (Hotel-Rest) | Frei/OFF |
| `X BLR/HKG/...` | Tour-Mitte mit Destination-Hinweis | (gibt's nicht) |
| `==` | Tour-Mitte | Frei |
| `OFF` | Tour-Mitte (Layover-OFF) | Frei |
| `RES` | Standby-im-Hotel (Z76 mit reduzierten Sätzen?) | Standby zuhause (kein VMA) |
| `RES_SB`, `SBY` | wie RES | Standby zuhause |
| `ORTSTAG` | (sollte nicht in tour-mitte erscheinen) | Office-passive zuhause |
| `FRS`, `FRD` | (selten in tour) | Free-Standby-zuhause |
| `LMN_AS`, `LMN_CR` | (sollte nicht in tour) | Medical-License |
| `EM` | Office-Meeting | Office-Meeting |
| Number-Code (`31591`, `755 LH755-1`) | Flight-Number → tour_start/end | nicht in non-tour |

---

## 5. Klassifikator-Adapter (`_deterministic_classify_v7` Anpassung)

Statt aktuell **Tag-für-Tag-Entscheidung** auf `activity_type` + `prev_overnight`, jetzt:

```
für jeden Tag d in matched_days:
  tour_ctx = lookup(d.datum, normalized_tours)
  if tour_ctx.role == 'non_tour':
    # alte Logik für non-tour-Tage (Office, Same-Day-without-tour, Frei)
    klass = classify_non_tour(d)
  elif tour_ctx.role == 'tour_start':
    if tour_ctx.is_foreign:
      klass = 'Z76' if briefing_morning else 'Z73'  # An/Ab-Sätze
    else:
      klass = 'Z73'  # Inland-Anreise
  elif tour_ctx.role == 'tour_mid':
    if tour_ctx.location_context.at_foreign_hotel:
      klass = 'Z76'  # 24h-Volltag
    elif tour_ctx.location_context.at_inland_hotel:
      klass = 'Z74'  # Inland-Volltag
    elif tour_ctx.location_context.standby_at_foreign:
      klass = 'Z76'  # An/Ab-Satz (RES-im-Hotel)
    ...
  elif tour_ctx.role == 'tour_end':
    klass = 'Z76' if tour_ctx.is_foreign else 'Z73'  # An/Ab
  elif tour_ctx.role == 'same_day':
    # echter same-day-trip (kein overnight)
    if foreign-routing UND duty≥480: klass = 'Z76'-an_abreise
    elif inland-routing UND duty≥480: klass = 'Z72'
    else: klass = 'ZeroDay'
```

**Vorteil:** klass-Entscheidung folgt aus Tour-Rolle, nicht aus per-Tag-Heuristik. Konsistent über Tour-Mitte-Tage hinweg (alle Z76 wenn Tour foreign + at_hotel).

---

## 6. Migration-Strategie (kein Big-Bang)

### 6.1 Phase 1 — Add Layer Behind Feature-Flag

- Neuer ENV: `AEROTAX_NORMALIZED_TOURS=0` (default off)
- `_normalize_tours_from_raw_facts` als separate Funktion in app.py
- Wenn Flag=0: alte Pipeline unverändert
- Wenn Flag=1: Layer läuft, Output landet in `_normalized_tours_audit` (read-only)
- Vergleicht in Audit-Log mit `_followme_identify_tours`-Output, **trifft aber keine klass-Entscheidung**

### 6.2 Phase 2 — Wire-Up Classifier-Adapter

- Klassifikator akzeptiert optional `normalized_tours` Parameter
- Bei Flag=1: Klassifikator nutzt Tour-Rolle-Adapter
- Bei Flag=0: alte Tag-Logik
- Beide Pipelines laufen parallel auf gleichen Eingaben, Diff im Audit

### 6.3 Phase 3 — Acceptance-Test vs Golden

- `tests/test_tibor_2025_golden_acceptance.py` läuft mit `AEROTAX_NORMALIZED_TOURS=1`
- Toleranzen siehe §8
- Solange dieser Test rot ist, Flag bleibt default off in prod

### 6.4 Phase 4 — Flip Default

- ENV-Default 0→1
- Old Code unter Flag bleibt 1 Release-Cycle als Rollback
- Nach 1 Cycle: alten Code löschen

---

## 7. KI-Resolver-Erweiterung

Bestehender `_resolve_uncertain_fact_with_ai(kind, context, ...)` braucht 2 neue kinds:

| neuer kind | Input-Context | Erwarteter Output (value) |
|---|---|---|
| `tour_context` | datum + marker + prev/next-day-fakten + se_stamp + crew-kontext | `{role: tour_start/mid/end/non_tour, location: at_homebase/at_foreign_hotel/in_flight/..., destination: IATA_or_country, confidence_reason}` |
| `tour_boundary` | day-range + alle overnights + alle layovers + ki-conf-Threshold | `{tour_start_date, tour_end_date, primary_destination, member_dates}` |

Plus bestehende kinds bleiben:
- `marker_semantics` (BH-001)
- `place_code` (Phase 3 BMF-Lookup)
- `layover_place` (Phase 4)
- `cas_time_extraction`

**Anti-Tax-Sanitizer bleibt:** verbietet `amount/eur/rate/betrag/euro/tagesatz/pauschale/tax/steuer/an_abreise/voll_24h/tagestrip_8h/price/preis/satz` als Schlüssel in `value`.

---

## 8. Testmatrix

### 8.1 Tour-Detection-Tests (Unit)

| Test | Eingabe | Erwartung |
|---|---|---|
| `test_tour_simple_foreign_3_days` | FRA→BLR overnight → X BLR overnight → BLR→FRA | 1 Tour, 3 days, foreign |
| `test_tour_with_x_marker_mid` | FRA→BLR overnight → `X` overnight (gemeint: Hotel-Rest) → BLR→FRA | 1 Tour, X = tour_mid |
| `test_tour_with_double_equals_mid` | FRA→KRK overnight → `==` overnight → KRK→FRA | 1 Tour, == = tour_mid |
| `test_tour_with_off_marker_mid` | FRA→USA overnight → `OFF` overnight → USA→FRA | 1 Tour, OFF = tour_mid |
| `test_tour_with_res_at_foreign_hotel` | FRA→ICN overnight → `RES` overnight ICN → ICN→FRA | 1 Tour, RES = tour_mid (standby_at_foreign) |
| `test_res_at_home_not_tour` | Office → `RES` no-overnight no-routing → Office | non_tour, RES=standby_at_home |
| `test_ortstag_at_home_not_tour` | Frei → `ORTSTAG` → Frei | non_tour, Office-passive |
| `test_multi_stop_inland_foreign_mixed` | FRA→BER→KRK overnight → KRK→BER→FRA | 1 Tour with mid_stop in Inland, is_mixed=True |
| `test_same_day_foreign_trip` | FRA→TLV→FRA same-day | 1 Tour, role=same_day, is_foreign |
| `test_same_day_inland_trip` | FRA→HAM→FRA same-day | 1 Tour, role=same_day, is_inland |
| `test_deadhead_to_tour_start` | DH-flight FRA→BCN → tour | DH-day = tour_start (Anreise zur Tour-Crew) |
| `test_tour_boundary_split_by_freier_tag` | Tour ends, 2 Tage Frei, neue Tour | 2 separate Touren |
| `test_ki_resolver_called_for_unknown_marker_in_tour_context` | unknown marker mid-tour | KI gerufen mit kind=tour_context |
| `test_ki_low_conf_marker_in_tour_remains_uncertain` | KI conf=0.5 → `needs_review=True` in normalized_day |

### 8.2 Klassifikator-Tests (Integration mit Tour-Layer)

| Test | Eingabe | Erwartung |
|---|---|---|
| `test_bangalore_tour_jan_03_to_06` | 4 Tage Bangalore | 01-03 Z73 An, 01-04 Z76 mid, 01-05 Z76 mid, 01-06 Z76 Ab |
| `test_korea_res_tour_apr_23_to_26` | 4 Tage Korea mit RES | 04-23 Z73 An, 04-24/25 Z76 mid, 04-26 Z76 Ab |
| `test_x_hkg_marker_becomes_z76` | X HKG mit foreign overnight context | Z76 |
| `test_x_at_home_stays_frei` | X mit no-routing, no-overnight, home-context | Frei |
| `test_multi_stop_madrid_inland_layover` | FRA→KRK→FRA→MAD-Tour mit BER-Layover-mid | BER nicht als Z76 Land=Deutschland, sondern Tour-mid Inland → Z74 |
| `test_inland_z74_volltag` | Inland-Tour mit 24h-Layover | Z74 28 € |
| `test_lmn_as_at_home_z72_only_if_briefing_time` | LMN_AS ORTSTAG ohne start_time | Office-passive, kein Z72 |
| `test_em_office_meeting_z72_with_briefing_time` | EM mit start_time + duty≥480 | Z72 14 € |
| `test_07_03_otp_fra_lhr_not_inland_z72` | routing OTP→FRA→LHR same-day | Tour-Tag (endet LHR foreign), Z76 An/Ab, NICHT Z72 |

### 8.3 Tibor-Golden-Acceptance (Master-Test)

`tests/test_tibor_2025_golden_acceptance.py`:

```
def test_tibor_2025_golden_acceptance():
    """Full pipeline mit normalized_tours layer aktiv → Golden-Match."""
    raw = json.load(open('tests/fixtures/tibor_aerotax_v11_raw_initial.json'))
    matched = build_matched_from_raw(raw)  # baut matched_days struktur

    normalized = _normalize_tours_from_raw_facts(matched, year=2025, homebase='FRA')
    result = _deterministic_classify_v7(matched, year=2025, homebase='FRA',
                                          normalized_tours=normalized)
    aligned = _followme_align_counters(result, matched, year=2025, homebase='FRA',
                                         normalized_tours=normalized)

    golden = json.load(open('tests/fixtures/followme_golden_tibor_2025.json'))
    soll = golden['soll_summary']

    # Toleranzen
    assert abs(aligned['arbeitstage'] - 133) <= 2,    f"arbeitstage {aligned['arbeitstage']}"
    assert abs(aligned['hotel_naechte'] - 66) <= 2,   f"hotel {aligned['hotel_naechte']}"
    assert abs(aligned['fahr_tage'] - 58) <= 2,       f"fahr_tage {aligned['fahr_tage']}"
    assert abs(aligned['z72_tage'] - 5) <= 1,         f"z72 {aligned['z72_tage']}"
    assert abs(aligned['z73_tage'] - 11) <= 1,        f"z73 {aligned['z73_tage']}"
    assert abs(aligned['z74_tage'] - 1) <= 1,         f"z74 {aligned['z74_tage']}"
    assert abs(aligned['z76_eur'] - 4794.0) <= 150,   f"z76_eur {aligned['z76_eur']}"
    # Plus pro Tag-Vergleich aller 133 Golden-Tage
    gd = golden['day_classification']
    mismatches = []
    for datum, exp in gd.items():
        actual = next((t for t in aligned['tage_detail'] if t['datum'] == datum), None)
        if not actual:
            mismatches.append(f'{datum}: Golden hat, AeroTAX fehlt')
            continue
        if (actual.get('klass') or '').lower() != (exp.get('klass') or '').lower():
            mismatches.append(f'{datum}: Soll={exp["klass"]} Ist={actual.get("klass")}')
    assert len(mismatches) <= 8, f'{len(mismatches)} Tag-Mismatches: {mismatches[:10]}'
```

### 8.4 Tibor-Day-Specific Acceptance-Tests

Pro betroffenem Tag (aus REVIEW_DIFF.csv) ein gezielter Test:

```
test_tibor_2025_01_04_x_blr_becomes_z76
test_tibor_2025_01_06_z76_abreise (BH-003a, schon da)
test_tibor_2025_01_20_x_hkg_becomes_z76
test_tibor_2025_02_14_x_hnd_becomes_z76
test_tibor_2025_03_30_x_bom_becomes_z76
test_tibor_2025_04_10_x_korea_becomes_z76
test_tibor_2025_04_23_to_26_res_korea_becomes_tour
test_tibor_2025_05_15_x_tlv_becomes_z76
test_tibor_2025_05_17_off_becomes_z76_usa
test_tibor_2025_06_17_18_off_becomes_z76_kroatien
test_tibor_2025_09_27_dus_inland_not_overridden_by_agp
test_tibor_2025_07_03_otp_fra_lhr_not_inland
test_tibor_2025_04_07_ortstag_frs_not_z72
test_tibor_2025_04_28_lmn_as_not_z72
test_tibor_2025_05_19_lmn_as_not_z72
test_tibor_2025_06_01_02_skandinavien_not_double_count
test_tibor_2025_chi_rom_sto_metro_codes_mapped
```

---

## 9. Risiken + Mitigation

| Risiko | Wahrscheinlichkeit | Mitigation |
|---|---|---|
| KI-Resolver-Latenz steigt (mehr Calls pro Job) | hoch | Cache pro (job_id, datum, kind, context-hash) bereits implementiert. Batch-Resolution für Tour-Boundary. |
| KI-Cost steigt (mehr Tokens) | mittel | Slim-Prompts, Antwort-Schema strikt validieren. Plus deterministische Pre-Filter (hard-evidence cases) ohne KI. |
| Regression bei legitimen Frei-Tagen | mittel | Feature-Flag, parallele Pipeline, Diff-Audit-Log, Acceptance-Test als Gate. |
| Tour-Boundary-Detection zu aggressiv → falsche „Mid"-Tage | mittel | KI conf-Schwellenwerte (≥0.85 für auto, sonst review). Plus regression-Tests für negative-cases (X-zuhause bleibt Frei). |
| Klassifikator-Refactor bricht andere Bugs | hoch | Migration in 4 Phasen, kein Big-Bang. Bestehende 1358 Tests müssen grün bleiben. |
| Golden-Acceptance-Test ist subjektiv (Golden hat eigene Annahmen) | niedrig | Toleranzen ±2 Tage / ±150€. Plus pro-Tag-Mismatch-Limit (≤8). |

---

## 10. Erwarteter KPI-Effekt (geplant)

Nach kompletter BH-CORE-001-Implementation (alle 4 Phasen):

| KPI | Vor BH-CORE-001 (fixture-Sim) | Soll (Golden) | Ziel-Toleranz |
|---|---:|---:|---|
| arbeitstage | 115 (Phase-A live: 140) | 133 | ±2 |
| hotelnächte | 46 (Phase-A live: 78) | 66 | ±2 |
| fahrtage | (?) (Phase-A live: 55) | 58 | ±2 |
| z72 | 5 | 5 | ±1 |
| z73 | (?) | 11 | ±1 |
| z74 | (?) | 1 | ±1 |
| z76_eur | (?) | 4794 | ±150 € |
| **gesamt** | (?) | **6020.72 €** | **±150 €** |

---

## 11. Reihenfolge / Roadmap

| # | Schritt | Aufwand | Risiko | Dauer |
|---:|---|---|---|---|
| 1 | **Plan-Review** (dieses Doc) | gering | n/a | jetzt |
| 2 | Acceptance-Test schreiben (`test_tibor_2025_golden_acceptance.py`) — wird **erst rot** | mittel | niedrig | 1 Iter |
| 3 | `_normalize_tours_from_raw_facts` Stage-1 (hard-evidence-only, ohne KI) | hoch | mittel | 2 Iter |
| 4 | Unit-Tests Tour-Detection (Tabelle §8.1) | mittel | niedrig | 1 Iter |
| 5 | Feature-Flag-Wire-Up + Audit-Log-Diff | gering | niedrig | 1 Iter |
| 6 | KI-Resolver `tour_context` + `tour_boundary` | mittel | mittel | 1 Iter |
| 7 | Klassifikator-Adapter (Tour-Rolle → klass) | hoch | hoch | 2 Iter |
| 8 | Integration-Tests Tibor-Days (§8.4) | mittel | mittel | 1 Iter |
| 9 | Live-Run Tibor + Diff-Audit | gering | mittel | 1 Live-Run |
| 10 | Flag default flip → Cleanup | gering | mittel | 1 Iter |

**Schätzaufwand:** ~7-10 Iterationen + 1 Live-Run am Ende.

---

## 12. Acceptance-Kriterien für BH-CORE-001

| # | Kriterium | Status (vor Start) |
|---|---|---|
| 1 | `_normalize_tours_from_raw_facts` existiert + ist getestet | offen |
| 2 | KI-Resolver `tour_context` + `tour_boundary` aktiv | offen |
| 3 | Klassifikator nutzt Tour-Rolle, nicht per-Tag-Heuristik | offen |
| 4 | Feature-Flag `AEROTAX_NORMALIZED_TOURS` schalbar | offen |
| 5 | Tibor-Golden-Acceptance-Test exists | offen |
| 6 | Tibor-Golden-Acceptance-Test grün mit ±2/±150€ Toleranz | offen |
| 7 | 17 Tibor-Day-Specific-Tests grün | offen |
| 8 | Bestehende 1358 Backend-Tests bleiben grün | sichtbar |
| 9 | Live-Run-Verifikation: gesamt ≈ 6021€ ±150€ | offen |
| 10 | Backward-Compat: Flag-off läuft alte Pipeline unverändert | offen |

**Solange nicht alle 10 grün, BH-CORE-001 = `in_progress`.**

---

## 13. Was BH-CORE-001 NICHT macht

| Ausgeschlossen | Begründung |
|---|---|
| Frontend-Änderungen | nichts UI-spezifisches. State-Machine bleibt. |
| Payment/Recovery-Token-Logik | nicht relevant. |
| BMF-Tagessatz-Tabelle (#228) | separate cleanup nach BH-CORE-001 funktioniert. Restdiff <100€ akzeptabel. |
| Reader-Refactor | Reader bleibt unverändert. Wenn Reader systematisch falsch liest → separates BH-READER-001. |
| Audit-Trail-Cleanup BH-003b | parked als P2. Macht arbeitstage nicht falsch. |

---

## 14. Verbindung zu existierenden Bug-IDs

| Bestehender Bug | Wird durch BH-CORE-001 gelöst? |
|---|---|
| BH-001 Review-Question | nein (orthogonal, schon gefixt) |
| BH-002 /api/job 502 | nein (orthogonal, schon gefixt) |
| BH-003a Heimkehr→Z76 | nein, **aber kompatibel** (BH-003a-Guard greift für 01-06; BH-CORE-001 erkennt 01-06 als tour_end UND klassifiziert via Tour-Rolle → Z76. Beide Pfade konvergieren.) |
| BH-003b Audit-Cleanup | parked, nach BH-CORE-001 obsolete (Issue verschwindet als Klasse weitgehend) |
| **BH-003c X-marker in tour** | **ja, primärer Use-Case** |
| **BH-003e RES in tour** | **ja, primärer Use-Case** |
| BH-003f == in tour | ja |
| BH-003g OFF in tour | ja |
| BH-003d-A Z76-double-count | ja, sauberere Tour-Aggregation |
| BH-003d-B Z72 Inland >8h | teilweise (Tour-Layer markiert non-tour-tage als Office-passive) |
| BH-004 Inland-Layover in foreign tour | ja (Tour-mit-Inland-Stopp wird `is_mixed=True`, Stopp wird tour_mid-Inland) |
| #228 BMF day-type rate matrix | nein, separat |
| #222 normalized_tours-Schicht | **= BH-CORE-001 — gleicher Task** |

---

## 15. Offene Fragen vor Start

1. **KI-Cost-Approval:** ungefähr 365 Tage × evtl. 0–3 KI-Calls pro Tag (je nach Cache-Hit-Rate). Bei voller Iteration ~$0.50-$2 pro Test-Lauf. OK?
2. **Acceptance-Toleranzen sinnvoll:** ±2 Tage / ±150€ — oder strenger?
3. **Phase-Split:** Soll Acceptance-Test ZUERST geschrieben werden (TDD) oder NACH normalize-Funktion?
4. **Reader-Bug-Eskalation:** Wenn Tour-Boundary-KI für viele Tage `needs_review=True` zurückgibt → eskalieren zu BH-READER-001 (Reader-Prompt-Refactor)?
5. **Rollback-Strategie:** Wenn Phase 4 (Flag-default-flip) regressiert in Live: sofort 0-Flag zurück oder schnellster-Fix-forward?

---

**Plan-Status:** ready_for_review. Kein Code geschrieben. Wartet auf User-Freigabe für Start mit Phase 2 (Acceptance-Test schreiben → erwartet rot → dann incremental aufbauen).
