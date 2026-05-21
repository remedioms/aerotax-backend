# BH-CORE-001 — Tour-First-Classifier Specification

Stand: 2026-05-19. **Spec-Doc. Kein Code in app.py. Tests siehe TEST_MATRIX.**

External-Review (ChatGPT) hat festgestellt: Die Reihenfolge **„Tag klassifizieren → Tour bauen"** zerreißt aktive Auslandstouren. Korrekte Reihenfolge: **„Tour aus raw facts bauen → Tag im Tourkontext klassifizieren"**.

---

## 1. Architektur-Übersicht

```
┌─────────────────────────────────────────────────────────────────┐
│ Layer 0 — Raw Facts (Reader/Sonnet liest Fakten, KEINE Klasse) │
│  Output pro Datum: routing, overnight_after_day, layover_ort,   │
│                    se_stfrei_ort, start_time, has_fl, ...       │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ Layer 1 — Normalized Tours (NEU, BH-CORE-001)                  │
│  _normalize_tours_from_raw_facts(matched_days, homebase, year)  │
│                                                                  │
│  - Tour-Boundaries AUS raw facts (NICHT aus klass)              │
│  - KI-Resolver für unklare Marker im Tour-Kontext               │
│  - Output: normalized_tours mit role/location_context pro Tag   │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ Layer 2 — Tour-Aware Day Classification                         │
│  _classify_days_from_normalized_tours(tours, bmf_table, homebase)│
│                                                                  │
│  - klass folgt aus Tour-Rolle + Location-Context                │
│  - SE-Override braucht zusätzliche Evidenz (nicht blind)        │
│  - BMF-Lookup deterministisch                                    │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ Layer 3 — KPI-Aggregation aus normalized_tours direkt           │
│  arbeitstage/reinigung/fahrtage/hotel aus Tour-Rollen,          │
│  NICHT aus tage_detail.klass.                                    │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ Layer 4 — Review Items / User Questions (Last Resort)           │
│  Nur bei normalized_day.needs_review=True UND keine             │
│  KI-confidence ≥ 0.70.                                           │
└─────────────────────────────────────────────────────────────────┘
```

---

## 2. Datenstrukturen

### 2.1 `RawFact` (Layer 0, unverändert)

Pro Datum:
```
{
  datum: str,
  raw_marker: str,
  activity_type_reader: str,      # was Reader sagt (NICHT Steuer-Decision)
  routing: list[str],
  has_fl: bool,
  starts_at_homebase: bool,
  ends_at_homebase: bool,
  overnight_after_day: bool,
  layover_ort: str,
  start_time: str,
  end_time: str,
  duty_duration_minutes: int,
  raw_lines: list[str],
  se_stfrei_ort: str,
  se_stfrei_total: float,
  se_stfrei_inland: bool|None,
  se_count: int,
  confidence: float,
  source: str,   # 'cas' | 'se' | 'lsb'
}
```

### 2.2 `NormalizedTour` (Layer 1, NEU)

```
{
  tour_id: str,                   # "T01_2025-01-03_FRA-BLR" o.ä.
  start_date: str,
  end_date: str,
  homebase: str,
  primary_destination: str,       # IATA-Code z.B. "BLR"
  destination_country: str,       # BMF-Land z.B. "Indien - Bangalore"
  tour_size: int,
  tour_pattern: str,              # "single_dest" | "multi_stop" | "same_day" | "deadhead_in"
  is_foreign: bool,
  is_inland: bool,
  is_mixed: bool,                 # Inland-Stopp innerhalb Auslands-Tour
  confidence: float,
  evidence: list[str],            # Begründungen warum diese Tage = eine Tour
  days: list[NormalizedDay],
}
```

### 2.3 `NormalizedDay`

```
{
  datum: str,
  raw_marker: str,
  routing: list[str],
  role: str,                      # "tour_start" | "tour_mid" | "tour_end" |
                                  #  "same_day" | "non_tour"
  location_context: str,          # "homebase" | "foreign_layover" |
                                  #  "inland_layover" | "in_flight" |
                                  #  "in_transit" | "unknown"
  layover_ort: str,               # roher Wert aus raw_facts
  inferred_layover_ort: str,      # nach Phase-4-Kaskade
  bmf_place_code: str,            # für BMF-Lookup
  se_context: {
    has_se_stamp: bool,
    stfrei_ort: str,
    stfrei_inland: bool|None,
    stfrei_betrag: float,
  },
  has_real_duty: bool,            # duty_duration_minutes > 0 oder start/end_time
  is_passive_homebase_marker: bool,   # ORTSTAG/FRS/LMN_AS/LMN_CR/FRD
  is_standby_homebase: bool,      # RES/SB ohne overnight + ohne foreign-context
  is_standby_hotel: bool,         # RES + prev_overnight=True + foreign layover
  is_layover_free_day: bool,      # X/==/OFF + prev_overnight=True + foreign layover
  confidence: float,              # 0.0-1.0
  evidence: list[str],            # menschenlesbare Begründungen
  needs_review: bool,             # True nur wenn KI conf < 0.70
}
```

---

## 3. Tour-Membership-Regeln

### 3.1 Tour-START (mindestens 1 erfüllt)

- `starts_at_homebase=True` UND `routing[0]==homebase` UND `routing[-1]!=homebase` UND `has_fl=True`
- `activity_type_reader='tour'` UND `requires_commute=True` UND Vortag NICHT overnight
- `has_fl=True` UND `routing` enthält foreign-Code UND Vortag-overnight=False
- KI-Resolver `tour_context` conf≥0.85 mit role='tour_start'

### 3.2 Tour-MID / CONTINUATION (mindestens 1 erfüllt)

- Vortag `overnight_after_day=True`
- `layover_ort != homebase` UND `layover_ort` ist gesetzt
- Marker enthält Airport/City-Code (`X HKG`, `X BLR`, `X HND`, ...)
- `routing` ohne Homebase-Element (z.B. nur `['BLR']`)
- SE-stamp foreign mit Datum innerhalb Tour-Range
- KI-Resolver `tour_context` conf≥0.85 mit role='tour_mid'
- Sandwich-Pattern: Vortag UND Folgetag sind tour_mid/tour_end, dazwischenliegender Tag wird tour_mid (KI darf entscheiden ob ja/nein)

### 3.3 Tour-END (alle erfüllt)

- `ends_at_homebase=True`
- `routing[-1]==homebase`
- Vortag `overnight_after_day=True` (Vortag war Tour-mid/start)

### 3.4 SAME-DAY (kein overnight, kein prev-overnight, kein next-overnight)

- `routing` zeigt Roundtrip (FRA → X → FRA) im gleichen Tag
- ODER `starts_at_homebase=True` UND `ends_at_homebase=True` UND `routing` nicht leer
- ist eine eigene „Tour" mit tour_size=1

### 3.5 NON-TOUR (default fallback)

- Keiner der obigen Tests greift
- `activity_type_reader in {'frei','urlaub','krank'}`
- Marker passive zuhause (`ORTSTAG`, `FRS`, `FRD`, `LMN_AS`, `LMN_CR`) ohne Tour-Indikatoren
- Plus: KI-Resolver bei unklarem Marker mit Standby-Pattern, conf≥0.85 für role='non_tour'

### 3.6 Verboten als Tour-Kriterium

- `klass != 'Frei'` ← NIEMALS
- `klass != 'Issue'` ← NIEMALS
- Tagesklasse als Grundlage für Tour-Grenzen ← NIEMALS

---

## 4. Marker-Semantik im Tour-Kontext

| Marker | Im aktiven foreign tour-Kontext | Zuhause ohne Tour |
|---|---|---|
| `X` | tour_mid, location=foreign_layover | non_tour, Frei |
| `X <IATA>` (z.B. `X HKG`) | tour_mid, location=foreign_layover, destination_hint=IATA | (gibt's nicht) |
| `==` | tour_mid (oder layover_free_day, KI entscheidet) | non_tour, Frei |
| `OFF` | tour_mid (Layover-OFF) | non_tour, Frei |
| `RES` | is_standby_hotel=True, role=tour_mid | is_standby_homebase=True, role=non_tour |
| `RES_SB`, `SBY` | wie RES | is_standby_homebase=True, role=non_tour |
| `ORTSTAG` | (selten in tour) | is_passive_homebase_marker, role=non_tour |
| `FRS`, `FRD` | (sehr selten in tour) | is_passive_homebase_marker, role=non_tour |
| `LMN_AS`, `LMN_CR` | (gehört nicht in tour) | is_passive_homebase_marker, role=non_tour (Medical) |
| `EM` | (kann tour_start für office-meeting sein) | role=non_tour (Office) |
| `OF` | im foreign tour-context: layover_free_day | non_tour, Office passive |
| Number-Code (`31591`, `755 LH...`) | Flight-Number → tour_start/end | (gibt's nicht außerhalb Tour) |

---

## 5. KI-Resolver-Spezifikation

### 5.1 Aktive Kaskade (NICHT Last-Resort)

```
Step 1: Deterministische Tour-Membership-Regeln (§3.1–3.5)
Step 2: KI-Resolver (siehe 5.2)
Step 3: User-Frage (nur wenn KI conf<0.70 UND Geld-Impact ≥ 14€)
Step 4: Python+BMF berechnet Betrag (deterministisch)
```

### 5.2 Erlaubte KI-Kinds

| kind | Eingabe | Erwartete `value` |
|---|---|---|
| `tour_context` | datum + marker + prev/next-day + se | `{role, location, destination, confidence_reason}` |
| `tour_boundary` | day-range + overnights + layovers | `{start_date, end_date, primary_destination, member_dates[]}` |
| `marker_semantics` | marker + activity_type + sample_dates | `{semantics, meaning}` |
| `layover_place` | datum + routing-fragment + prev/next | `{resolved_place, country, iata}` |
| `place_code` | iata + crew-context | `{country, region, bmf_key}` |
| `cas_time_extraction` | raw_lines + marker | `{start_time, end_time, duty_minutes}` |
| `standby_context` | RES-marker + prev/next + se | `{is_standby_hotel, location}` |

### 5.3 Pflicht-Prompt-Header (immer enthalten)

```
"Dieser Plan gehört zu Flugpersonal, Cockpit/Kabine, Airline-Crew-Roster,
Lufthansa-ähnlicher Dienstplan.
Beurteile nur die Bedeutung des Markers im Tour-Kontext.
Keine Steuerbeträge, keine EUR-Werte, keine Tagesätze, keine Steuerentscheidung.

Bekannte LH-Marker-Konventionen:
- 'X' = OFF-Day. Im Tour-Kontext oft Hotel-Rest-Day mid-tour.
- '==' = Frei-Marker. Im Tour-Kontext potentiell Layover-Day.
- 'OFF' = Off-Day. Im Tour-Kontext = Hotel/Layover.
- 'RES' = Standby on-call. Wenn nicht zuhause: Hotel-Standby.
- 'ORTSTAG'/'FRS'/'LMN_AS'/'LMN_CR' = Office-/Medical-passive zuhause.
- Number-Codes wie '31591 P1', '755 LH755-1' = Flight-Numbers.
"
```

### 5.4 Anti-Tax-Sanitizer

Verbotene Keys in KI-`value`:
`amount`, `eur`, `euro`, `rate`, `tagesatz`, `pauschale`, `tax`, `steuer`,
`an_abreise`, `voll_24h`, `tagestrip_8h`, `price`, `preis`, `satz`, `deduction`,
`steuerbetrag`

Bei Verstoß: `resolved=False`, `needs_review=True`, audit-log entry.

### 5.5 Confidence-Schwellen

| conf | Aktion |
|---|---|
| ≥ 0.90 | auto-übernehmen, Audit-Evidence speichern |
| 0.70–0.90 | normalized_day.confidence gesetzt, `needs_review=True`, Vorschlag im Review-Item |
| < 0.70 | User-Frage ohne Suggestion (last resort) |

---

## 6. Tour-Aware Day Classification (Layer 2)

### 6.1 Klass-Decision-Matrix

| Tour | Role | Location | klass | Hint |
|---|---|---|---|---|
| foreign | tour_start | in_flight (early briefing) | Z76 An/Ab | BMF an_abreise-Satz |
| foreign | tour_start | in_flight (Abendbriefing) | Z73 (Inland-An-Hilfe) | 14 € (Übernachtung in DE) |
| foreign | tour_mid | foreign_layover | Z76 Volltag | BMF voll_24h |
| foreign | tour_mid | inland_layover (Mixed-Tour) | Z74 | 28 € (Inland-Volltag) |
| foreign | tour_mid | standby_hotel | Z76 An/Ab oder Volltag | depending on duty |
| foreign | tour_end | in_flight | Z76 An/Ab | BMF an_abreise |
| inland | tour_start | in_flight | Z73 | 14 € |
| inland | tour_mid | inland_layover | Z74 | 28 € |
| inland | tour_end | in_flight | Z73 | 14 € |
| same_day | same_day | foreign | Z76 An/Ab >8h | BMF an_abreise |
| same_day | same_day | inland | Z72 >8h | 14 € |
| same_day | same_day | (< 8h) | ZeroDay | 0 € |
| non_tour | non_tour | homebase passive | Office | 0 € |
| non_tour | non_tour | homebase + duty>=480 (Schulung) | Z72 | 14 € |
| non_tour | non_tour | standby_homebase | Standby | 0 € |
| non_tour | non_tour | Frei/Urlaub/Krank | Frei | 0 € |

### 6.2 SE-Override (chirurgisch, NICHT mehr blind)

SE-Override darf NUR `Frei → Z76` setzen wenn ALLE erfüllt:
- `se.count > 0` UND `se.stfrei_inland=False` UND `se.stfrei_ort` gesetzt
- ZUSÄTZLICH mindestens eins:
  - `prev.overnight_after_day=True` UND `prev.layover_ort=foreign`
  - `routing` enthält Auslands-IATA UND `has_fl=True`
  - KI-Resolver `tour_context` conf≥0.85 bestätigt `role=tour_*`

→ Schützt vor SE-Stamp-Drift (Stamp am Tag N+1 für Tour-Tag N).

### 6.3 09-27 AGP/DUS-Fall (kritisch)

Aktuelles Verhalten: SE-Inland-Stamp DUS wird durch CAS-Foreign-Layover AGP überstimmt → Z76 AGP.
Golden sagt: Z74 Deutschland 28€ (Inland-Volltag).

Neue Regel: Bei SE-Inland-Stamp UND CAS-Foreign-Layover gleichzeitig → Disambiguation:
- Wenn `prev.layover_ort` Foreign UND `next.routing[0]` ist Foreign → tour_mid foreign (CAS wins)
- Wenn `prev.routing[-1]` ist `homebase` UND `next.routing[0]` ist `homebase` → tour_mid inland (SE wins, Z74)
- Sonst KI-Resolver `tour_context` mit beiden Stamps als evidence

---

## 7. Hotelnacht-Logik (eigene, nicht `len(Z76)-1`)

Bestehende Formel `hotel_naechte = Σ tour_z76_tage − tour_count` produziert 60 (Golden 66).

Neue Regel: Hotelnacht zählt für day N wenn:
- N ist tour_mid (foreign oder inland)
- ODER N ist tour_start UND overnight_after_day=True
- NIEMALS für tour_end (Crew kommt nach Hause)

Pseudo:
```
hotel_count = 0
for tour in normalized_tours:
    for day in tour.days:
        if day.role == 'tour_end':       continue
        if day.role == 'non_tour':       continue
        if day.role == 'same_day':       continue
        if not day.overnight_after_day:  continue
        hotel_count += 1
```

Inland-Hotel-Nächte zählen mit (Z74 + Z73-Inland-Hotel-Pattern). Siehe `HOTEL_SEMANTICS_AUDIT.md`.

---

## 8. KPI-Aggregation (Layer 3)

Nicht aus `tage_detail.klass` zählen, sondern aus `normalized_tours`:

| KPI | Berechnung |
|---|---|
| `arbeitstage` | Σ days mit role ∈ {tour_start, tour_mid, tour_end, same_day, non_tour-mit-Z72/Office-aktiv} MINUS passive_homebase_marker |
| `reinigungstage` | identisch arbeitstage (gleicher Definition wie FollowMe-Seite 3) |
| `fahr_tage` | Σ tour_starts mit homebase-departure + Σ non_tour-tage mit Office/Z72 + Anreise |
| `hotel_naechte` | siehe §7 |
| `z72_tage` | Σ days mit klass='Z72' |
| `z73_tage` | Σ days mit klass='Z73' |
| `z74_tage` | Σ days mit klass='Z74' |
| `z76_eur` | Σ days mit klass='Z76' × BMF-Tagessatz(land, tagtyp) |

---

## 9. Abgrenzung Reader vs Classifier

| Layer | Verantwortung | Was NICHT |
|---|---|---|
| Reader (Layer 0) | Strukturierte Fakten extrahieren | KEINE klass-Entscheidung, KEINE Z-Codes |
| Tour-Normalizer (Layer 1) | Tour-Rolle/Location aus Fakten | KEIN Betrag, KEIN BMF-Lookup |
| KI-Resolver | Marker/Tour-Kontext aufklären | KEINE EUR, KEINE Steuersätze (Anti-Tax-Sanitizer) |
| Classifier (Layer 2) | klass aus Tour-Rolle + BMF | KEINE Tour-Membership-Änderung |
| Counter (Layer 3) | KPI aus normalized_tours | KEIN Tag-für-Tag-Re-Counting |

---

## 10. Acceptance-Criteria (BH-CORE-001 Done-Definition)

| # | Kriterium | Gating |
|---|---|---|
| 1 | `_normalize_tours_from_raw_facts` existiert + 14+ Unit-Tests grün | hard |
| 2 | KI-Resolver-Kinds `tour_context`, `tour_boundary`, `standby_context` aktiv | hard |
| 3 | `_classify_days_from_normalized_tours` ersetzt per-Tag-Heuristik | hard |
| 4 | Feature-Flag `AEROTAX_TOUR_FIRST_CLASSIFIER` ON/OFF Switch | hard |
| 5 | `test_tibor_2025_golden_acceptance.py` Toleranz-grün (siehe TEST_MATRIX §4) | **hard, primary gate** |
| 6 | Bestehende 1358 Backend-Tests bleiben grün | hard |
| 7 | Live-Run Tibor: gesamt 6020.72 ±150€ | hard (nach Phase 5) |
| 8 | Backward-Compat: Flag-off läuft alte Pipeline unverändert | hard |
| 9 | `HOTEL_SEMANTICS_AUDIT.md` aktualisiert mit finaler Logik | soft |
| 10 | Audit-Trail mit Tour-IDs für jeden Tag persistiert | soft |

---

## 11. Stop-Regeln (Hard, projektweit)

- **Keine** weiteren Einzel-Guards in `_deterministic_classify_v7` ohne Tour-First-Plan
- **Kein** neuer SE-Override ohne zusätzliche Tour-Evidenz (§6.2)
- **Kein** `Frei → Z76` nur wegen SE-Ort
- **Kein** `RES → Z76` ohne Tour-/Hotel-Kontext
- **Kein** `X → Frei` wenn X innerhalb aktiver Auslandstour
- **Kein** `X → Z76` wenn X zuhause ohne Tour
- **Kein** Hotel-Fix ohne `HOTEL_SEMANTICS_AUDIT.md`
- **Kein** `verified_closed` ohne Golden-Acceptance oder Live-Run-Beweis
- **Keine** Codeänderung in app.py vor Spec + Testmatrix
