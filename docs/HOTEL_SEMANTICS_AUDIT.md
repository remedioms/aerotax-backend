# Hotel-Semantics Audit

Stand: 2026-05-19. Aktuelle Hotelnacht-Logik überzählt um Δ+12 in Tibor's Live-Run (78 vs Golden 66).

---

## §1 Aktueller Stand

### 1.1 Implementation (`_followme_align_counters`, app.py:13751-13759)

```python
hotel_naechte_followme = 0
for tour in tours:
    z76_idxs = [idx for idx, td in enumerate(tour['days'])
                 if (td.get('klass') or '').lower() == 'z76']
    if not z76_idxs:
        continue   # reine Inland-Tour → 0 Hotel
    # Alle Z76-Tage außer dem letzten zählen als Hotel
    hotel_naechte_followme += len(z76_idxs) - 1
```

**Übersetzung:** Hotel = `Σ (Z76-Tage in Tour) − 1` pro foreign tour. Inland-Touren zählen 0.

### 1.2 Probleme dieser Formel

1. **Z74-Inland-Volltage zählen nicht** (z.B. Madrid-Tour mit BER-Layover-Mitte → BER-Übernachtung sollte als Hotel zählen)
2. **Z73-Anreise-Tage mit overnight zählen nicht** (z.B. wenn 01-03 = Z73 + overnight → Hotel-Nacht in DE nach Late-Briefing)
3. **Same-Day-Trips mit Z76 (mehrtägige Tour aus Sicht des Klassifikators?) zählen ggf. falsch**
4. **Tour-mit-Z76-am-Letzttag** (Heimkehr) — diese Z76 wird auch als Hotel gezählt minus −1, was logisch ist, ABER nur wenn die Tour KEIN nicht-z76-Mid-Day enthält. Bei Mixed-Touren: Inland-Mid-Day überspringt die Subtraktion.
5. **Z76-Mid-Tour-Tage die fälschlich als Frei klassifiziert sind** (BH-003c, 15 Tage) zählen nicht in Z76-idxs → Hotelnächte fehlen
6. **Cluster-C2-Override** (z.B. 09-27 fälschlich Z76 AGP statt Z74 DE) → zählt eine zusätzliche Hotelnacht die's gar nicht gab

### 1.3 IST-Werte (Tibor Live-Run)

| Source | hotel_naechte |
|---|---:|
| AeroTAX Live (e132976f) | **78** |
| AeroTAX fixture-Simulation | 46 (weniger weil weniger Z76-Tage) |
| Golden | **66** |

---

## §2 Golden-Definition (aus FollowMe.aero)

Golden's `soll_summary.hotelaufenthalte = 66` ist eine **Single-Number**. Die `day_classification` hat **kein hotel-Flag pro Tag**.

### 2.1 Rekonstruktion aus Golden-Daten

| Quelle | Wert |
|---|---:|
| `soll_summary.hotelaufenthalte` | 66 |
| Golden-Tours mit `tour_size` ≥ 2 | 36 Touren |
| Golden-Tours mit `tour_size` ≥ 2 → `Σ (tour_size − 1)` | (zu rechnen) |
| Golden Z76-count | 113 |
| Golden Z76-count minus tour_count (53) | 60 |

**Hypothese 1 (`Z76 − tour_count`):** 113 − 53 = **60** ≠ 66.

**Hypothese 2 (`Σ overnight per active day`):** Jede Tour-Mitte/Start mit overnight=True zählt 1 Nacht.

Beispiel Bangalore-Tour (4 Tage):
- 01-03 (Z73): overnight=True → 1 Nacht
- 01-04 (Z76): overnight=True → 1 Nacht
- 01-05 (Z76): overnight=True → 1 Nacht
- 01-06 (Z76): overnight=False (Heimkehr) → 0 Nächte
- **Σ Bangalore = 3 Nächte**

Mit aktueller Formel (Z76−1): 3 Z76-Tage − 1 = 2 Nächte.
Mit overnight-based: 3 Nächte ✓

→ **Hypothese 2 ist konsistenter.**

### 2.2 Inland-Hotel-Anteile

Golden hat 11 Z73-Tage. Wenn einige davon overnight in DE haben (Hotel nahe Homebase nach späten Briefings), zählen sie als Hotelnacht.

Z74-Tage (1) sind Inland-Volltag → mindestens 1 Inland-Hotel-Nacht.

Plus: Anreise-Tage mit Hotel (z.B. wenn Tour 01-03 mit Abend-Briefing in Hotel BLR übernachtet) zählen mit.

---

## §3 Tibor-Stichproben

### 3.1 Z76-Tage aus fixture mit overnight=True

(Lokal berechnen — Phase 1 Aufgabe, hier nur als Methodik)

### 3.2 Inland-Z73 mit overnight=True

Schätzung aus Golden's day_classification: Z73-Tage mit `is_abreise=False` UND `position_in_tour > 1` → potentielle Hotel-Tage in DE.

---

## §4 Vorschläge für neue Hotel-Logik

### 4.1 Vorschlag A — overnight-basiert (Empfehlung)

```python
hotel_count = 0
for tour in normalized_tours:
    for day in tour.days:
        if day.role == 'tour_end':       continue
        if day.role == 'non_tour':       continue
        if day.role == 'same_day':       continue
        if not day.raw_facts.overnight_after_day: continue
        hotel_count += 1
```

**Pro:**
- Verlässt sich auf raw fact (overnight_after_day) statt klass
- Tour-Mitte-Tage (auch wenn fälschlich als Frei klassifiziert) zählen trotzdem mit, wenn Reader overnight=True lieferte
- Funktioniert sowohl für Z76- als auch für Z74-Touren

**Contra:**
- Wenn Reader overnight_after_day falsch las (zu viel) → Hotel zu hoch
- Wenn Reader es nicht las (zu wenig) → Hotel zu niedrig

### 4.2 Vorschlag B — Tour-Position-basiert

```python
hotel_count = 0
for tour in normalized_tours:
    if tour.tour_size <= 1: continue   # same-day = 0 Hotelnächte
    # tour_size - 1 Nächte (Übernachtungen zwischen day N und N+1)
    hotel_count += tour.tour_size - 1
```

**Pro:** Einfach, deterministisch
**Contra:** Zählt Inland-Tour-Mitte-Tage gleich wie foreign — eventuell zu großzügig

### 4.3 Vorschlag C — Hybrid (Empfehlung für Master-Implementation)

```python
hotel_count = 0
for tour in normalized_tours:
    for i, day in enumerate(tour.days[:-1]):   # bis vorletzter Tag
        # Nächte nur wenn:
        # - overnight_after_day = True UND
        # - role in {tour_start, tour_mid} UND
        # - location_context != 'homebase'
        if not day.raw_facts.overnight_after_day:
            continue
        if day.role not in ('tour_start', 'tour_mid'):
            continue
        if day.location_context == 'homebase':
            continue   # Crew schläft zuhause
        hotel_count += 1
```

**Pro:** Tour-aware, robust gegen klass-Misklassifikation, respektiert location_context.

---

## §5 Verbliebene offene Fragen

### Q1: Zählt Tour-Anreise-Tag mit Abend-Briefing als Hotel?

Beispiel: 2025-01-03 Bangalore-Anreise, Briefing 10:55 in DE → Abend-Flug → Übernachtung in BLR.
- Z73 (Golden) → Frage: zählt diese Hotelnacht?
- Vorschlag A/C: ja (overnight=True UND nicht-homebase nach Briefing)

### Q2: Inland-Layover-Tag mit Hotel-Übernachtung?

Beispiel: 2025-06-23 Madrid-Tour mit BER (Inland-Layover) Stopp → Übernachtung in Berlin.
- Golden klass = Z76 Spanien-Madrid (Tour-Ziel-Land überwiegt)
- Hotel zählt? Vorschlag C: ja (overnight + tour_mid + non-homebase)

### Q3: Standby im Hotel = Hotel-Nacht?

Beispiel: 2025-04-24 RES in Korea — Crew schläft in Hotel auf Standby.
- Vorschlag A/C: ja (overnight=True + role=tour_mid + foreign)

### Q4: Trinkgeld vs Hotel — gleiche Definition?

`trinkgelder.naechte = 66, satz = 3.6 €/nacht = 237.60 €`
`hotelaufenthalte = 66`

Sollte gleich sein. Golden-Definition: Übernachtung außerhalb Homebase → Trinkgeld + Hotel-Nacht beide +1.

---

## §6 Audit-Procedure

Nach Implementation von Vorschlag C in Phase 1:

1. Iteriere alle 53 Golden-Touren, berechne hotel_count nach Vorschlag C.
2. Sollte 66 ergeben oder ±2.
3. Falls nicht: Listen aufmachen welche Touren overcount/undercount.
4. Disambiguate per KI-Resolver oder fixture-correction.

---

## §7 Test-Coverage

| Test | Datei | Erwartung |
|---|---|---|
| `test_hotel_bangalore_3_nights` | new | 4-day-tour mit 3 overnights = 3 Hotelnächte |
| `test_hotel_inland_z74_volltag_counts` | new | Inland-Tour 24h → 1 Hotelnacht |
| `test_hotel_same_day_zero` | new | Same-day-Trip → 0 Hotelnächte |
| `test_hotel_standby_at_foreign_counts` | new | RES Korea-Tour → Hotelnächte zählen |
| `test_hotel_at_homebase_does_not_count` | new | Z72 Inland-Office am Hb → 0 Hotelnächte |
| `test_hotel_total_tibor_within_tolerance` | new | Gesamt 66 ±2 für Tibor |

---

## §8 Status & Next Step

**Status:** Audit-Doc, kein Code-Change.

**Phase-1-Anforderung:**
1. Vorschlag C in `_followme_align_counters` einbauen hinter Feature-Flag
2. 6 Hotel-Tests grün bringen
3. Tibor-Acceptance hotel-Toleranz 66 ±2 erreichen

**Solange dieses Doc nicht abgesegnet ist:** Kein Hotel-Fix.
