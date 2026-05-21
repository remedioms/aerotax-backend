# BH-CORE-001 Phase 6 — Golden Acceptance Status

Stand: 2026-05-19. **Status: RED** (16 acceptance-Tests fehl).

## §1 Aggregat-KPIs

| KPI | Tour-First Shadow | Golden | Δ | Toleranz | Status |
|---|---:|---:|---:|---:|:-:|
| arbeitstage | 87 | 133 | −46 | ±2 | ✗ |
| reinigungstage | 87 | 133 | −46 | ±2 | ✗ |
| hotel_naechte | 40 | 66 | −26 | ±2 | ✗ |
| fahr_tage | 30 | 58 | −28 | ±2 | ✗ |
| z72_tage | 0 | 5 | −5 | ±1 | ✗ |
| z73_tage | 0 | 11 | −11 | ±1 | ✗ |
| z74_tage | 0 | 1 | −1 | ±1 | ✗ |
| z76_eur | 3771.00 | 4794.00 | −1023.00 | ±150 € | ✗ |
| gesamt | 3771.00 | 6020.72 | −2249.72 | ±150 € | ✗ |

## §2 Klass-Verteilung Shadow vs Golden

| Shadow | Count | Golden | Count |
|---|---:|---|---:|
| Frei | 233 | Frei | ~190 |
| Z76 | 87 | Z76 | ~120 |
| Office | 65 | Office/NO_VMA | ~? |
| Standby | 10 | Standby | ~? |
| Z72 | 0 | Z72 | 5 |
| Z73 | 0 | Z73 | 11 |
| Z74 | 0 | Z74 | 1 |

Tour-pattern-Verteilung in normalized_tours:
- non_tour: 308
- single_dest: 17
- same_day: 7
- multi_stop: 6

## §3 Cluster-Tabelle (69 Mismatches)

| Cluster | Pattern Shadow→Golden | Count | Δ-Beitrag | Ursache | Fix-Kategorie | Risiko |
|---|---|---:|---|---|---|---|
| C1 | Frei → Z76 | 31 | arbeitstage, hotel, z76 | Tour-Erkennung verfehlt — Tour-Tag als non_tour | Tour-boundary | hoch (struktureller Reader/Tour-Boundary-Issue) |
| C2 | Office → Z76 | 13 | arbeitstage, z76 | Day als Office-Hb klassifiziert obwohl foreign-tour | Tour-boundary | hoch |
| C3 | Z76 → Z73 | 7 | z73, z76_eur | Tour-Start abends/late-briefing → Inland-Anreise statt Foreign-An | Counter logic | mittel (BMF-Verteilung der Tour-Start-Tage) |
| C4 | Standby → Z76 | 5 | z76, arbeitstage | RES sollte standby_hotel sein, ist standby_home | KI-resolution | mittel (Phase 5b live calls erforderlich) |
| C5 | Office → Z72 | 4 | z72 | Same-Day-Inland-Trip (>8h duty) wird Office | Counter logic | niedrig |
| C6 | Office → NO_VMA | 3 | — | EM-marker / Schulung — keine Steuer-Auswirkung | nicht kritisch | — |
| C7 | Standby → Z73 | 3 | z73 | RES vor Tour-Anreise → Z73 Inland | Tour-boundary | mittel |
| C8 | Frei → Z73/Z74/Z72 | 3 | z72/z73/z74 | Echter Workday als Frei klassifiziert | Tour-boundary | mittel |

## §4 Counter-Lücken

**Z72/Z73/Z74 = 0** ist das schwerwiegendste Aggregat-Problem:

- **Z72 (Same-Day Inland >8h)**: Tour-Layer erkennt nur Inland-Touren wenn `len(routing) >= 3`. Single-Inland-Trip (FRA→MUC→FRA) qualifiziert NICHT als `same_day`, sondern als `non_tour`. Mock-Heuristik klassifiziert dann Office.
- **Z73 (Tour-Start/End inland ODER Tour-Start late evening)**: Aktuelle Logik triggert Z76 wenn `is_foreign_tour=True` UND BMF-Mapping vorhanden. Z73 nur als Fallback bei fehlendem BMF. Late-evening-briefing (>18:00) sollte Z73 sein, nicht Z76 — das fehlt komplett.
- **Z74 (Tour-Mid inland)**: Nur 1 Day im Golden. Niedrige Priorität.

## §5 Tour-Boundary-Lücken (C1+C2 = 44 Tage)

Diese sind nicht durch Counter-Fixes lösbar — sie erfordern Tour-Erkennung-Verbesserung. Ursachen:

1. **Reader-bugs**: Sonnet misinterpretiert bestimmte Marker als activity_type='frei' obwohl Tour-Routing vorhanden ist.
2. **Sandwich-Pattern unvollständig**: `_normalize_tours_from_raw_facts` greift nur bei prev_overnight + foreign-Layover. Mehrere Tibor-Pattern matchen das nicht.
3. **KI-Resolver-Confidence im Mock-Mode**: Mock-Heuristik gibt 0.60 conf → NEEDS_USER. Real KI mit Crew-Roster-Wissen würde 0.90+ liefern.

Phase 5b (Live-KI-Calls) würde C1/C2/C4/C7 → ca. 49 Mismatches auf ~10 reduzieren — aber `ANTHROPIC_API_KEY` ist nicht im Environment gesetzt (Stop-Rule "Keine Env/Secret-Änderung").

## §6 Phase-6-Akzeptanz: RED. Nächste Schritte

### Innerhalb Phase 6b (ohne KI-Live-Call) lösbar:

- **C3 (7×Z76→Z73)**: Tour-Start mit `start_time >= 18:00` → Z73 Inland-Anreise statt Z76. Counter-Fix in `_classify_days_from_normalized_tours`.
- **C5 (4×Office→Z72)**: Same-Day-Inland mit `duty >= 480` UND single-stop → Z72. Tour-Boundary in `_normalize_tours_from_raw_facts` erweitern.
- **C8 (3×Frei→Inland)**: Same-Day-Inland-Same-day-trips mit duty>8h.

Erwarteter KPI-Effekt:
- z72_tage: +5 (von 4×C5 + 1×C8)
- z73_tage: +7 (C3)
- z74_tage: +1 (C8)
- arbeitstage: +14 (Counter-Fix + Z72/Z73-Klassifikation)

### Nicht in Phase 6b lösbar (Phase 5b ↔ Phase 6c-Kandidat):

- **C1 (31×Frei→Z76)**: braucht Reader-Verbesserung oder Live-KI-Resolver für non_tour-Days mit routing-Evidenz
- **C2 (13×Office→Z76)**: dito
- **C4 (5×Standby→Z76)**: Live-KI-Resolver für standby_hotel-Disambiguation
- **C7 (3×Standby→Z73)**: dito

## §7 Stop-Regeln eingehalten

✓ Kein Deploy
✓ Kein Live-Run
✓ Kein Production-Flag default ON
✓ Keine Env/Secret-Änderung
✓ Keine Migration
✓ Phase-6-Bericht ohne blindes Patchen — Cluster dokumentiert, gezielter Fix-Pfad in Phase 6b
