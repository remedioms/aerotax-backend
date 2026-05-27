# Classifier V2 — Cleane Architektur-Spec

**Status:** Phase 0/1 Implementation, parallel zum Legacy-Pfad
**Ziel:** ~80 Rescue-Patches in `app.py` durch 5 klare Regel-Klassen ersetzen

## Das Problem

Die heutige `_classify_v11_cas_pipeline` enthält:
- 80+ `# BH-003a`, `# v15 Fix`, `# R26-R39`, `# v8.x Fix`, `# HD-A/B`-Marker
- Interaktionen zwischen Patches unvorhersehbar
- Mental-Modell nicht mehr darstellbar
- Jeder neue Edge-Case droht den nächsten Patch zu erzeugen

## Die Lösung: 5 reine Funktionen

Jede Funktion deterministisch, testbar, ohne Seiteneffekte.

```
classify_marker(marker_raw, cas_fields) → MarkerKind
build_tours(sorted_days)                → list[Tour]
day_role_in_tour(day, tour)             → DayRole
resolve_country(day, tour, se_rows)     → (country, iata, source)
is_hotel_night(day, tour, country)      → (bool, reason)
```

## Regel 1: classify_marker

**Input:** `marker_raw: str`, `cas_fields: dict`

**Output:** `MarkerKind` ∈ {
- `STRICT_PASSIVE` — immer Frei (LMN_HT*, LMN_AD*, LMN_DS, LMN_FT, ORTSTAG, OFF, OF, URLAUB)
- `FLEXIBLE_PASSIVE` — Frei nur wenn CAS-Felder leer (FRS, FRD, LMN_AS, LMN_CR)
- `STANDBY_HOME` — Standby zuhause (SB_S, SB_M, RB, RES_SB)
- `STANDBY_AIRPORT` — Standby am Flughafen (SB_F, SBA, SBY, RES)
- `TRAINING` — Schulungs-Marker (EM, EH, EK, D4, DD, TK, SM, SIM, EMCRM, SECCRM)
- `FLIGHT` — Flugnummer (LH123, 4-stellige Zahl, IATA-Codes im routing)
- `UNKNOWN` — alles andere (Cockpit-Marker, andere Airlines)
}

**Architektur-Prinzip:** Marker-Klassifikation entscheidet **nicht** Z72/Z73/etc — sie liefert nur eine Kategorie. Die finale Klasse kommt aus Tour-Kontext + CAS-Feldern.

## Regel 2: build_tours

**Input:** `sorted_days: list[Day]`

**Output:** `list[Tour]` — jede Tour hat:
```python
@dataclass
class Tour:
    days: list[Day]
    start_date: date
    end_date: date
    foreign_country: str | None  # primäres Land (oder None bei Inland)
    has_overnight: bool
```

**Regeln:**
1. Eine Tour ist ein **zusammenhängender Block** aktiver Tage zwischen 2 Frei/Standby_Home-Tagen.
2. Eine Tour MUSS mindestens **ein** dieser Signale haben:
   - Foreign-IATA in routing
   - layover_iata mit Foreign-Code
   - overnight_after_day=True + Foreign-Signal
   - Same-Day-Inland-Flight (starts+ends_homebase + duty>=480 + Flight-Token)
3. Marker_kind ∈ {STRICT_PASSIVE, FLEXIBLE_PASSIVE_ohne_Felder, STANDBY_HOME, free-activity_type} → **außerhalb** jeder Tour.
4. Heimkehr-Tag (ends_at_homebase + kein overnight + prev_overnight=True) → **Last-Day** der Vortag-Tour (auch wenn eigenes Routing dünn).

## Regel 3: day_role_in_tour

**Input:** `day: Day`, `tour: Tour`

**Output:** `DayRole` ∈ {
- `DEPARTURE` — erster Tag der Tour, mit Briefing-Zeit
- `MID_FULL_AWAY` — Mid-Tour-Tag mit overnight=True (User schläft im Ausland)
- `RETURN` — letzter Tag der Tour, ends_at_homebase=True, kein overnight
- `SAME_DAY_INLAND` — 1-Tages-Tour mit Same-Day-Inland-Flight
- `STANDBY_AIRPORT` — Tour-Tag mit Standby-Marker am Flughafen
- `OFFICE_AT_HB` — Office-Tag innerhalb Tour-Klammer (selten)
}

## Regel 4: resolve_country

**Input:** `day: Day`, `tour: Tour`, `se_rows: list[SE]`

**Output:** `(country: str|None, iata: str|None, source: str)`

**Source-Hierarchie** (oben gewinnt):
1. `SE.stfrei_ort` am gleichen Datum, Foreign
2. `day.layover_iata`, Foreign
3. `day.target_iata`, Foreign
4. `day.origin_iata` bei RETURN-Day
5. `tour.foreign_country` (Tour-Kontext)
6. `routing[0]` oder `routing[-1]`, wenn Foreign-IATA
7. **Nichts** → return (None, None, 'missing_bmf_country')

## Regel 5: is_hotel_night

**Input:** `day: Day`, `tour: Tour`, `country: str|None`

**Output:** `(counts: bool, reason: str)`

**Counts = True wenn ALLE erfüllt:**
1. `day.overnight_after_day = True`
2. Tag-Rolle ∈ {DEPARTURE, MID_FULL_AWAY} (NICHT RETURN, NICHT SAME_DAY_INLAND)
3. country aus Regel 4 ist Foreign (nicht None, nicht Inland)
4. NICHT Standby-Home

## Tag-Klassifikation aus den 5 Regeln

Pseudo-Code für die finale Tag-Klasse:

```python
def classify_day(day, tour, country, hotel_night):
    if not tour:
        # Außerhalb jeder Tour
        if classify_marker(day.marker, day.cas) == STRICT_PASSIVE:
            return 'Frei'
        if classify_marker == STANDBY_HOME:
            return 'Standby'
        if day.has_briefing and day.at_homebase:
            return 'Office'  # Reinigung läuft
        if day.has_briefing and day.is_truly_away_inland:
            return 'Z72'     # Same-Day Inland Auswärts
        return 'Frei'  # Default

    # In Tour
    role = day_role_in_tour(day, tour)
    if country is None or country == 'Inland':
        # Inland-Tour
        if role == DEPARTURE or role == RETURN:
            return 'Z73'  # An/Ab
        if role == MID_FULL_AWAY:
            return 'Z74'  # Voll-Inland 24h
        if role == SAME_DAY_INLAND:
            return 'Z72'
    else:
        # Auslandstour
        if role == DEPARTURE or role == RETURN:
            return 'Z76', rate='an_abreise'
        if role == MID_FULL_AWAY:
            return 'Z76', rate='voll_24h'
```

## Zähler aus den Tag-Klassen

```python
arbeitstage      = Σ Tour-Tage + Σ Office + Σ Z72 + Σ Standby-Airport
reinigungstage   = Σ Arbeitstage außer Standby-Home
fahrtage         = Σ Tour-Starts (1 pro Tour) + Σ Solo-Office mit Briefing + Σ Standby-Airport ohne Tour-Anschluss
hotel_naechte    = Σ Tage mit is_hotel_night=True
z72/z73/z74/z76  = aus Tag-Klasse + BMF-Pauschale
```

## Was diese Architektur löst

**80 Patches → 5 Funktionen.** Jede Funktion testbar, Edge-Cases werden zu **Test-Inputs** statt Code-Patches.

| Heutiger Patch | In V2 |
|---|---|
| R26 LMN_HT1 auto-heal | classify_marker(LMN_HT1) = STRICT_PASSIVE → Frei |
| R29 strict/flexible | classify_marker gibt direkt die Klasse |
| R32 SE-Cross-Check | resolve_country Source #1 ist SE |
| R34 SE darf nicht überstimmen | classify_marker entscheidet zuerst — wenn STRICT, ignoriert SE |
| R39 Z72 Auswärtstätigkeit | classify_day prüft is_truly_away_inland — kein Z72 am HB |
| BH-003a/b/c Heimkehr-Rescue | build_tours nimmt Heimkehr-Tag in Tour, day_role_in_tour gibt RETURN |
| HD-A/B Mid-Tour-Foreign | day_role_in_tour gibt MID_FULL_AWAY |

## Migration ohne Risiko

1. **`classifier_v2.py`** als neues Modul (kein Eingriff in `app.py`)
2. Parallel-Hook in `app.py`: nach Legacy-Klassifikator läuft V2 zusätzlich
3. **Diff-Logger** schreibt pro Tag wo Legacy ≠ V2
4. Sobald Diff für Tibor + User 95775 + N weitere Real-User = 0 → Switch

## Tests-Strategie

Pro Regel ein Test-File:
- `test_classifier_v2_markers.py` — alle Marker-Klassen
- `test_classifier_v2_tours.py` — Tour-Klammer (Mid-Tour-X, Heimkehr, Standby-Aktivierung)
- `test_classifier_v2_roles.py` — Day-Roles
- `test_classifier_v2_country.py` — Country-Resolver-Hierarchie
- `test_classifier_v2_hotel.py` — Hotel-Erkennung

Plus Integration:
- `test_classifier_v2_tibor_snapshot.py` — Tibor 2025 muss Brutto ~6.073 € geben
- `test_classifier_v2_user_95775_snapshot.py` — User 95775 muss Z72=13 geben (R39-konsistent)
