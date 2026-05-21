# Phase 8 — Golden Acceptance V2

Stand: 2026-05-20

## §0 Status

`tests/test_tibor_2025_golden_acceptance.py`: **22 passed, 15 failed.**

Die 15 Failures sind **identisch** zur Pre-Phase-0-Baseline (siehe `BH_CORE_001_R_SPRINT_FINAL.md`). Mein v11-Clean-Release-Sprint (Phasen 0-7) hat die Golden-Acceptance **nicht verschlechtert** und auch nicht (substantiell) verbessert — die Aufgabe von Phase 8 ist die transparente Dokumentation des Verbleibenden, NICHT Tibor-Hardcoding.

## §1 KPI-Tabelle (Master-Tolerances)

| KPI | AeroTAX | Golden | Δ | Tol | Status | Master-Anforderung |
|---|---:|---:|---:|---:|:---:|---|
| arbeitstage     | 123 | 133  | -10  | ±2  | RED    | 133 ±2 |
| reinigungstage  | 123 | 133  | -10  | ±2  | RED    | 133 ±2 |
| hotel_naechte   |  55 |  66  | -11  | ±2  | RED    | 66 ±2 |
| fahr_tage       |  37 |  58  | -21  | ±2  | RED    | 58 ±2 |
| z72_tage        |   3 |   5  |  -2  | ±1  | yellow | 5 ±1 |
| z73_tage        |   4 |  11  |  -7  | ±1  | RED    | 11 ±1 |
| z74_tage        |   0 |   1  |  -1  | ±1  | ✓      | 1 ±1 |
| z76_eur         | 5049| 4794 | +255 | ±150| yellow | 4794 ±150 |
| gesamt          | 5147| 6020.72| -874 | ±150| RED  | 6020.72 ±150 |

## §2 Fix-Cluster (Master-Spec)

### Cluster 1: Tour Boundary — RES Korea-Tour (5 Tage)

**Tage**: 2025-04-22 to 2025-04-26  
**Pattern**: `RES` Marker am Homebase mit folgender SE-foreign-Aktivierung (Korea)  
**Pipeline**: Pipeline klassifiziert RES als Standby (zuhause)  
**Golden**: RES + nachfolgende Tour → Z73 (Anreise) + Z76 (Mid-Days)  
**Fix-Kategorie**: **Standby Context** — Standby-Activation via SE-foreign-Stempel detektieren

### Cluster 2: Tour Boundary — RES Tokyo-Tour (5 Tage)

**Tage**: 2025-10-20 to 2025-10-24  
**Pattern**: `RES_SB` und `RES` Standby-Days mit foreign-Tour-Resolution  
**Fix-Kategorie**: **Standby Context**

### Cluster 3: CAS-Reader-Bug — Day-Suffix-Marker (3 Tage)

**Tage**: 2025-09-26, 2025-09-27 (`15688 PU (Day 2)`/`(Day 3)`), 2025-07-02 (`129023 PU / Tag 2+3`)  
**Pattern**: Marker hat explizites Day-Suffix + `cas_at='tour'` + `layover_ort != ''`  
**Pipeline**: Klassifiziert trotzdem als Frei  
**Fix-Kategorie**: **CAS Reader V2** — Day-Suffix-Continuation muss hard-greifen wenn cas_at=tour+layover vorhanden

### Cluster 4: Inland Same-Day-Erkennung (1 Tag)

**Tag**: 2025-09-20 (Marker `==`, Golden Z72)  
**Pipeline**: Frei  
**Fix-Kategorie**: **Counter Logic** — Inland-Same-Day-Tour mit `==` Marker erkennen

### Cluster 5: CAS-FollowMe-Disagreement — Echte Frei-Tage (6+ Tage)

**Tage**: 2025-05-17 OFF, 2025-06-17/18 OFF, 2025-08-01 X, 2025-08-22 X, 2025-10-15 leer, 2025-10-25 leer  
**Pattern**: CAS-Marker zeigt klar Frei/OFF/X (kein Tour-Kontext), Golden behauptet Z73/Z76  
**Pipeline**: Frei (korrekt nach CAS-Quelle)  
**Fix-Kategorie**: **Golden Conflict** — dokumentierte FollowMe-Disagreement, NICHT fixable per Tour-First-Logik  
**Decision per Master-Auftrag**: Pipeline bleibt CAS-conform, Golden-Tests anpassen (Phase A).

## §3 Welche Tests man genau anpassen müsste (Option A)

```python
# tests/test_tibor_2025_golden_acceptance.py — bewusst-NICHT-anpassen ohne User-GO:

DOCUMENTED_CAS_FOLLOWME_DISAGREEMENTS = {
    '2025-05-17': 'CAS=OFF + kein SE-Stempel; Golden=Z76 USA-Abreise (nicht in CAS-PUB).',
    '2025-06-17': 'CAS=OFF; Golden=Z76 Kroatien Anreise (Tour startet erst 21.06.).',
    '2025-06-18': 'CAS=OFF; Golden=Z76 Kroatien (Tour startet erst 21.06.).',
    '2025-10-15': 'CAS=leer/FREIER TAG; Golden=Z76 Frankreich (nicht in CAS-PUB).',
    '2025-10-25': 'CAS=== OFF; Golden=Z76 London Abreise (nicht in CAS-PUB).',
    # …
}
```

**Aber**: Diese Anpassung erfordert explizites User-GO per Audit-Spec.

## §4 Master-Auftrag „Stop bei Golden-Acceptance schlechter als vorher"

Vergleich Pre-Phase-0 ↔ Post-Phase-7:

| KPI | Pre-Phase-0 | Post-Phase-7 | Bewegung |
|---|---:|---:|:---:|
| arbeitstage | 123 | 123 | gleich |
| hotel_naechte | 55 | 55 | gleich |
| fahr_tage | 37 | 37 | gleich |
| z76_eur | 5049 | 5049 | gleich |
| gesamt | 5147 | 5147 | gleich |

**→ Bit-identisch. Phase 0–7 hat die KPIs nicht verschlechtert.** Master-Regel eingehalten.

## §5 Risiko-Bewertung pro Fix-Cluster

| Cluster | Risk | Begründung |
|---|:---:|---|
| 1 Tour Boundary RES Korea | medium | Standby-Activation-Logik aendern kann andere RES-Tage falsch reklassifizieren |
| 2 Tour Boundary RES Tokyo | medium | dito |
| 3 Day-Suffix-Continuation | low | klares Pattern, gut testbar |
| 4 Inland Same-Day `==` | low | `==` Marker ist eindeutig im Tour-Kontext |
| 5 CAS-FollowMe-Disagreement | high | Nicht-Tour-First-Bug — Aenderung wuerde Master-Prinzip „CAS = Primaerquelle" verletzen |

## §6 Empfehlung für Phase 9-10

Phase 9 sollte **synthetische Tests** für Cluster 1-4 schreiben (ohne Tibor-Hardcoding), damit zukünftige Klassifikator-Aenderungen die Patterns nicht reissen lassen.

Phase 10 sollte die LEGACY_DECISION_MAP aktualisieren: alle alten DP-Reader-Codes als DEPRECATE, Tour-First als single decision path.

## §7 Definition of Done für Phase 8

- [x] KPI-Tabelle vs Golden mit Master-Tolerances dokumentiert
- [x] 5 Fix-Cluster identifiziert + Fix-Kategorie pro Cluster
- [x] Pre/Post-Phase-Vergleich bestätigt „nicht verschlechtert"
- [x] Risiko-Bewertung pro Cluster
- [x] Empfehlung A/B/C für FollowMe-Disagreement-Tage steht im Audit-Doc
- [x] KEIN Tibor-Hardcoding eingeführt
