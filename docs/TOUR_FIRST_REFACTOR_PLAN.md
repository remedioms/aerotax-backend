# Tour-First Refactor Plan

**Erstellt:** 2026-05-27 (R37 Follow-up)
**Status:** Plan-Phase, NOCH KEINE Klassifikations-Änderungen

## Warum

Heute haben wir 80+ Rescue-/Patch-Marker in `app.py` (`BH-003a/b/c`, `R0/R28/R29/R32/R34`, `v15 Fix`, `HD-A/B`, …). Jede neue Schicht erzeugt potentiell den nächsten Edge-Case. User-Feedback Tibor-Job zeigt das Resultat:

- **14.02:** Heimflug aus Miami → System sagt „Mischfall, weiß ich nicht" statt **Z76 An/Ab USA-Miami**
- **25.03:** FRS-Tag ohne Uhrzeit, SE-Buchhaltungs-Lag mit AMM-Zeile → R32 hätte fast fälschlich Z76 erzwungen
- **26.03:** Heimflug-Tag aus BOS-Tour → ZeroDay 0 € statt Z76 An/Ab USA

Alle drei sind Symptome **eines** Problems: **Tag-für-Tag-Klassifikation ohne robusten Tour-Kontext.**

## Ziel-Architektur

**Tour-First, Tag-Rolle deterministisch:**

```
Phase 1: CAS-Reader liefert raw_days (deterministisch, plus Sonnet-Healing)
Phase 2: Tour-Builder klammert Tour-Cluster (= Sequenz aktiver Tage)
Phase 3: Tour-Land-Resolver bestimmt Hauptland pro Tour (CAS-Routing + SE)
Phase 4: Tag-Rolle-Klassifikator:
         - 1. Tag mit Briefing-Zeit + Routing zur HB → Anreise
         - Mittelere Tage mit overnight=True → Volltag
         - Letzter Tag (kein overnight, ends_at_homebase) → Heimkehr-An/Ab
         - Tag außerhalb Tour-Klammer → Frei
Phase 5: Pauschalen aus BMF-Tabelle (Land + Rolle → Tagessatz)
```

**Was das fixt:**
- 14.02 wird in Tour-Klammer der MIA-Tour aufgenommen → Last-Day → Z76 An/Ab USA-Miami
- 25.03 ist außerhalb jeder Tour-Klammer (vorheriger Tag war nicht overnight in AMM) → Frei
- 26.03 ist Last-Day BOS-Tour → Z76 An/Ab USA

**Was wir abbauen können:**
- `BH-003a/b/c` Heimkehr-Rescues (Tour-Klammer macht das automatisch)
- `R32`/`R34` SE-Override (Tour-Klammer berücksichtigt SE primär als Validierung)
- Die meisten v15-Fixes (Mid-Tour-X-Klassifikation läuft automatisch via Klammer)

## Implementierungsplan — kleinste sinnvolle Schritte

### Schritt 1 (jetzt): Baseline-Test
**Ziel:** Tibors aktuelle Werte als „darf-nicht-schlechter-werden"-Anker.
- Snapshot-File `tests/fixtures/tibor_baseline_2026_05_27.json` mit allen Top-Counter
- Test `tests/test_tour_first_refactor_baseline.py` der gegen den Snapshot prüft
- Läuft mit lokalen Fixtures, kein API-Call
- **Akzeptanz:** Test grün gegen aktuellen Code, läuft <5s

### Schritt 2: Tour-Klammer-Erkennung erweitern
**Ziel:** Heimkehr-Tag (prev_overnight=True + today no_overnight + ends_at_homebase=True) wird automatisch zur Tour gerechnet.
- Edit in `normalized_tours.py:build_normalized_tours` Pass-1-Loop
- Wenn `is_tour_continuation=True` (gestern in Tour) UND today.ends_at_homebase=True UND kein overnight → flush_tour erst NACH today (today gehört zur Tour als Last-Day)
- **Akzeptanz:** Baseline-Test bleibt grün ODER hat klar belegte Verbesserung (z.B. mehr Z76, weniger Issue)

### Schritt 3: Last-Day-Klassifikation in Calculator
**Ziel:** Last-Day-Tour-Day mit kein overnight + ends_at_homebase → Z76 An/Ab des Tour-Lands.
- Edit in `normalized_tours.py:calculate_allowances_from_normalized_tours`
- Pro Tour: identifiziere Last-Day (höchstes Datum, ends_at_homebase=True)
- Rolle = `'an_ab_return'`, BMF-Satz = an_abreise
- **Akzeptanz:** 14.02 Tibor wird zu Z76 An/Ab USA-Miami (44 €)

### Schritt 4: Audit alte Rescues
**Ziel:** Welche der existierenden BH-003a/b/c Pfade sind durch Schritt 2+3 obsolet?
- Diff-Audit: pro Tour-Tag prüfen, ob alte Rescue noch greift
- Wenn alle 0 Tage betroffen → Rescue als deaktiviert markieren (`if False:` mit Audit-Hinweis)

### Schritt 5: Tests + Deploy
- Volle Test-Suite muss grün
- Live-Test gegen Tibor-Job: Werte vor/nach
- Wenn Brutto stabil oder höher (BMF-konform): Deploy

## Risiken & Gegenmaßnahmen

| Risiko | Gegenmaßnahme |
|---|---|
| Tibors Werte verschlechtern sich | Baseline-Test als Gatekeeper |
| Neue Edge-Cases bei anderen Crew | Hard-Fails (`hotel > arbeitstage` etc) bleiben |
| Reader-Stochastik-Anfälligkeit | Tour-Cluster ist robust gegen einzelne Tag-Lücken |
| Cockpit/andere-Airline-Daten | Activity-First-Logik trägt sich (Marker irrelevant) |

## Was wir NICHT umbauen

- Reader (CAS Reader V2 bleibt — funktioniert)
- BMF-Daten (`bmf_data.py` bleibt)
- LSB-Klassifikator (Z17/Z77-Reading bleibt)
- PDF-Renderer (nur die Audit-Sektion ist raus, Rest bleibt)
- Frontend-State-Machine (nur Audit-Box ist raus)

## Iterations-Modus

Nach jedem Schritt:
1. Tests laufen lassen
2. Diff zum Baseline prüfen
3. User-Check
4. Erst dann nächster Schritt

Kein „big bang". Reine evolutionäre Verbesserung gegen messbaren Baseline.
