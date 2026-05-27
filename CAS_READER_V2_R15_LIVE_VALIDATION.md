# CAS Reader V2 — R15 Live-Validation Report

**Stand:** 2026-05-26
**Status: NEEDS_FIX** (siehe Abschnitt 5).

---

## 1. Setup

| Punkt | Wert |
| --- | --- |
| Branch | `main` (commit-stand wie geschrieben in `AEROTAX_FIX_STATE.md`) |
| Tibor-Dir | `/Users/miguelschumann/Desktop/Tibor/2025/` |
| Input | LSB 1× · SE 1× · CAS 13× (NTF/PUB, Flugstundenübersicht ignoriert) |
| Homebase | FRA |
| ANTHROPIC_API_KEY | in-process aus `gcloud secrets versions access` (nicht ins Repo geschrieben, nicht ins Log geechot) |
| AEROTAX_ALLOW_BOOT_WITHOUT_KEY | `1` (Hinweis aus dem Boot-Check selbst) |
| AEROTAX_CAS_READER_V2 | V1-Run: ungesetzt · V2-Run: `1` |
| AEROTAX_USE_NORMALIZED_TOURS | **nicht gesetzt** ⇒ normalized_tours-Audit ist None — beide Runs laufen ausschließlich im Legacy-Pfad |
| Production-Aktivierung | nein |
| Deploy | nein |
| Default-Switch | nein |
| Harness | `scripts/r15_live_validation.py` |
| Roh-Output | `R15_VALIDATION_OUTPUT.json` |

### Wallclock
- V1: 337.6 s
- V2: 526.6 s (langsamer wegen längerem CAS-Prompt — V2_PROMPT_INSTRUCTIONS angehängt)

---

## 2. KPI Vergleich V1 vs V2

KPIs aggregiert aus `tage_detail.klass` und `eur` pro Tag (Quelle: `R15_VALIDATION_OUTPUT.json`).

| KPI | V1 | V2 | Diff V2−V1 | Tibor-Range (R13/R14) | V2 im Range? |
| --- | --- | --- | --- | --- | --- |
| Z72 Tage    |   5 |   7 | +2  | 4–7    | ✓ |
| Z72 €       |  70 |  98 | +28 | —      | — |
| Z73 Tage    |   6 |   4 | −2  | 9–13   | ✗ unter Range |
| Z73 €       |  84 |  56 | −28 | —      | — |
| Z74 Tage    |   0 |   0 | 0   | 0–2    | ✓ |
| Z74 €       |   0 |   0 | 0   | —      | — |
| Z76 Tage    | 125 | 125 | 0   | —      | — |
| Z76 €       | 5196 | 5310 | +114 | 4600–5100 | ✗ leicht über |
| Fahrtage    | 101 | 107 | +6  | 52–54  | ✗ ~doppelt |
| Arbeitstage | 174 | 179 | +5  | 128–138 | ✗ über |
| Reinigungstage | 141 | 152 | +11 | —    | — |
| Hotelnächte | 53 | 65 | +12 | 64–67  | ✓ V2 im Range |
| Trinkgeld € | — | — | — | —     | nicht im Legacy-`classification`-Top-Level durchgereicht |
| Gesamtbetrag € | nicht aus `classification` extrahiert (Wert wird erst nach WISO-Aggregation gesetzt) | | | | |

Difference-to-FollowMe ist die R13/R14-Acceptance-Range. Konkrete FollowMe-Tagessätze wurden nicht in den Test geladen (No-Hardcoding-Regel) — wir vergleichen nur gegen die Range.

---

## 3. Bekannte Problemfälle (Tag-Audit)

### 3.1 Bangalore 03.–08.01.2025

Aus `tage_detail` beider Runs:

| Datum | Marker | Routing | V1 klass / € | V2 klass / € | Bewertung |
| --- | --- | --- | --- | --- | --- |
| 2025-01-01 | U | — | Frei / 0 | Frei / 0 | Urlaub — OK |
| 2025-01-02 | U | — | Frei / 0 | Frei / 0 | Urlaub — OK |
| 2025-01-03 | 31591 | FRA | Z76 / 28 | Z76 / 28 | BLR-Anreise — OK |
| 2025-01-04 | X | BLR | Z76 / 28 | Z76 / **42** | V2 = Z76-Volltag (Indien voll_24h-Variante), V1 nur An/Ab |
| 2025-01-05 | 755 | BLR | Z76 / 28 | Z76 / 28 | Reader liest LH755 hier (Nachtflug-Span 05./06.) |
| **2025-01-06** | **X** | **FRA** | **Issue / —** | **Issue / —** | **✗ V2 hat das NICHT gefixt** — Heimkehrtag bleibt Issue |
| 2025-01-07 | ORTSTAG | FRA | Frei / 0 | Frei / 0 | Office am Homebase — OK |
| 2025-01-08 | == | FRA | Frei / 0 | ZeroDay / 0 | minimaler Klassifikator-Unterschied |

**Kernbefund:** Der R14-Fix für „X als tour_return" greift **nicht** im Legacy-Pfad, weil:
- V2-Prompt ist nur an Sonnet **angehängt** (V1-Tool-Schema bleibt aktiv → Sonnet kann `is_tour_return` nicht setzen).
- Der R14-Postprocessor (`cas_postprocessor.normalize_cas_days_v2`) läuft nur in `build_normalized_tours`, das wiederum nur aktiv ist, wenn `AEROTAX_USE_NORMALIZED_TOURS=1`. Diese Flag war im Live-Run **nicht** gesetzt.
- Die Legacy-Klassifikation (`_classify_v11_cas_pipeline`) ruft den Postprocessor nicht und klassifiziert direkt aus reader_facts → 06.01 bleibt `Issue`.

### 3.2 BH-003c Phantomtage (SE-only-Z76)

`grep "SE-Override"` im aktuellen V2-Lauf: **0 Treffer**. Im allerersten Lauf (Run 1) gab es noch:
- `2025-01-19/20 (HKG)` und `2025-02-13/14 (HND)` mit `reason_counted='SE-Override: aktive Auslands-SE → Z76 (statt Frei)'`.

Im aktuellen Lauf (Run 2) tauchen diese Strings nicht mehr auf — stattdessen werden dieselben Tage **unter `cluster_foreign=True` als Z76-Volltag verbucht**. Das ist **keine Verbesserung**, sondern eine reason-Verschiebung: die Tage zählen weiter als Z76, nur unter anderem reason-Code. Phantom-Identifikation eindeutig = offen, weil `reason_counted` nicht im result-JSON durchgereicht wurde (offenes Audit-Ticket — siehe Abschnitt 6 #7).

### 3.3 Pattern-A-Residual (04-08, 10-05)

Aus V2-Log-Diag-Items:

| Datum | Marker | Routing | layover_iata | klass | € | reason (V2-Log) |
| --- | --- | --- | --- | --- | --- | --- |
| 2025-04-08 | 90064 | FRA | ICN | Z76 | 32 | „Auslandstour-An/Ab (Homebase FRA, Ziel SEL) Z76" — via Cluster-Foreign |
| 2025-10-05 | 18776 | FRA | — | Z76 | 32 | analog: Cluster-Foreign-Anreise, Folgetag-Layover als Beweis |

Beide klassifizieren als Z76 wegen Cluster-Foreign-Logik (Folgetag hat Auslands-Layover). Logik ist **strukturell**, nicht zufällig. Aber Folgetag-Beweis allein ist riskant, wenn Briefing-Zeit am Anreise-Tag in Deutschland war (dann wäre Z73 korrekter).

### 3.4 Home-Standby

In `tage_detail` ist `SB_S/SB_F/RB/RES_SB` mit `klass='Standby'` oder `'Frei'` gelistet. Beispiel V2: `2025-10-20 ort=HAM betrag=14€ klass=Standby → Audit-Note` (Inland-AG-Erstattung mit Standby, kein Tour-Tag). **Keine** Home-Standby-Tage erscheinen mit `klass='Z76'` oder `'Z73'`. ✓ erfüllt.

### 3.5 Hotelnächte

V2 = 65 (im Tibor-Range 64–67). V1 = 53. V2 erkennt mehr echte Layover-Nächte; im V2-Log `counted_as_hotel_nacht=True` durchgängig für Foreign-Layover-Tage. Keine Phantom-Hotelnächte aus Inland-Tagen oder Standby beobachtbar. ✓ größte messbare V2-Verbesserung.

### 3.6 Fahrtage

V2 = 107, Erwartung 52–54 (~doppelt). Vermutung: in der Legacy-Pipeline wird jeder dienstliche Tag mit `requires_commute=True` als Fahrtag gezählt — Mid-Tour-Tage inkl. Layover-Tage. Der R14-`B9`-Fahrtag-Filter (1 pro Tour-Start) wirkt nur in `normalized_tours.calculate_allowances`, im Live-Run nicht aktiv.

---

## 4. Audit-Auszug (V2-Run, kritische Tage)

Aus `tage_detail` + Diag-Items im V2-Log:

| Datum | Marker | Routing | Layover | klass | € | reason / Begründung |
| --- | --- | --- | --- | --- | --- | --- |
| 2025-01-03 | 31591 | FRA | BLR | Z76 | 28 | Anreise FRA→BLR, BMF an/ab |
| 2025-01-04 | X | BLR | BLR | Z76 | 42 | Auslands-Layover BLR (Z76 Volltag) |
| 2025-01-05 | 755 | BLR | BLR | Z76 | 28 | LH755-Eintrag, overnight=True |
| **2025-01-06** | X | FRA | — | **Issue** | — | Reader-Ambiguität — V1 + V2 identisch |
| 2025-01-19 | X | HKG | HKG | Z76 | — | Auslands-Layover, in V2 unter Cluster-Foreign |
| 2025-02-13 | X | HND | HND | Z76 | — | Tokyo-Layover, Cluster-Foreign |
| 2025-03-29 | 74016 | FRA | BOM | Z76 | — | Mumbai-Anreise, „Auslandstour-An/Ab Z76" |
| 2025-04-08 | 90064 | FRA | ICN | Z76 | 32 | Seoul-Anreise via Cluster |
| 2025-05-02 | 111174 | KRK | HAM | Z76 | — | Krakau-Tour, Layover HAM (Aircraft-Rotation) |
| 2025-09-26 | — | IST | MUC | Z76 | 24 | Audit-Note: CAS-Auslands-Layover IST überstimmt SE-Inland-Stempel MUC |
| 2025-10-20 | — | HAM | — | Standby | 14 | Inland-AG-Erstattung mit Standby → kein Tour-Tag (korrekt) |

Volle Roh-Daten in `R15_VALIDATION_OUTPUT.json`.

---

## 5. Entscheidung

**`NEEDS_FIX`**

### Warum nicht `PASS_FOR_STAGING`

V2 verbessert messbar gegen V1, aber **die zwei kritischen Akzeptanzkriterien aus R13/R14 sind nicht erfüllt**:

1. **2025-01-06 Bangalore-Heimkehr bleibt `Issue`** in V2 — exakt der Anchor-Use-Case, für den R14 gebaut wurde. V2-Prompt allein reicht nicht, weil:
   - Tool-Schema bleibt V1 (Sonnet kann `is_tour_return` nicht im Tool setzen).
   - Postprocessor läuft nicht, weil `AEROTAX_USE_NORMALIZED_TOURS=0`.
   - Legacy-Klassifikator entscheidet aus reader_facts ohne V2-Heilung.
2. **Fahrtage 107 / Arbeitstage 179 / Z73 4** sind weit außerhalb der Tibor-Acceptance-Range (52–54 / 128–138 / 9–13). Diese Counter werden im Legacy-Pfad anders berechnet als in `normalized_tours.calculate_allowances_from_normalized_tours` (B9-Filter, R14-Continuation, B17). Im Live-Run war ausschließlich der Legacy-Pfad aktiv — R14 hatte keine Wirkfläche.

### Was V2 messbar bringt
- **Hotelnächte 53 → 65** und damit erstmals im Tibor-Range.
- **Reinigungstage +11 / Arbeitstage +5** — Mid-Tour-X wird etwas öfter als dienstlich erkannt.
- **Z76 ≈ stabil** (kein Auslands-Verlust durch V2).

---

## 6. Konkrete Abweichungen + minimale Fix-Vorschläge

| # | Datum / Bereich | Erwartet | Tatsächlich (V2) | Vermutete Ursache | Betroffene Funktion | Minimaler Fix-Vorschlag |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 2025-01-06 | tour_return / Z76 oder Z73 | `Issue` | Postprocessor läuft nur in normalized_tours; Legacy ignoriert V2-Heilung | `_classify_v11_cas_pipeline` ruft `cas_postprocessor.normalize_cas_days_v2` nicht | **2. Live-Iteration mit `AEROTAX_USE_NORMALIZED_TOURS=1` UND `AEROTAX_CAS_READER_V2=1` gleichzeitig.** Falls weiterhin Issue: V2-Tool-Schema separat einbauen (statt nur Prompt-Append). |
| 2 | Fahrtage | 52–54 | 107 | Legacy zählt jeden dienstlichen Tag mit Commute, nicht nur Tour-Starts | `_classify_v11_cas_pipeline` Fahrtag-Loop | normalized_tours-Pfad via Flag aktivieren — B9-Filter dort enthalten |
| 3 | Arbeitstage | 128–138 | 179 | Legacy zählt liberal jeden non-Free-Tag | wie #2 | wie #2 (B17 strikter) |
| 4 | Z73 | 9–13 | 4 | Tour-Anreise-Tage mit SE-Inland werden vom Cluster-Logik als Z76 überstimmt | `_v15-cluster-c2`-Pfad in `app.py` (Beispiel 2025-09-26 IST-vs-MUC) | Inland-stfrei-Stempel an einem Anreise-Tag muss vorrangig Z73 sein, wenn CAS-Marker eine Inland-Briefing-Zeit zeigt. Heute überstimmt der CAS-Auslands-Layover. → separates Ticket |
| 5 | BLR 04.01 (28→42 €) | konsistent BMF Indien | V1: 28, V2: 42 | V2 wählt Volltag wo V1 An/Ab — beides defensibel | `_classify_v11_cas_pipeline` Pre-/Post-Departure-Logik | sichtbar machen, keine Korrektur erforderlich |
| 6 | 04-08 / 10-05 (90064/18776) | Z76 oder Z73 je nach Briefing | Z76 (32 €) via Cluster | Cluster-Foreign-Logik nutzt Folgetag-Layover als Beweis | analog #4 | Cluster-Logik mit Briefing-Zeit gegenchecken |
| 7 | BH-003c-Identifikation | klar im result-Dict sichtbar | `reason_counted` nicht durchgereicht → Phantom-Filter blind | Harness/Result-Schema | `_extract_phantom_z76` muss in `result` ein explizites Audit-Feld lesen; ggf. `audit_notes` in app.py erweitern, dass Phantom-Z76-Trigger explizit getagged sind |

### Was als nächstes minimal zu tun ist

1. **Zweite Live-Iteration mit beiden Flags an:**
   ```bash
   AEROTAX_USE_NORMALIZED_TOURS=1 \
   AEROTAX_CAS_READER_V2=1 \
   AEROTAX_ALLOW_BOOT_WITHOUT_KEY=1 \
   ANTHROPIC_API_KEY="$(gcloud secrets versions access latest --secret=ANTHROPIC_API_KEY --project=aerotax-prod)" \
   python3 scripts/r15_live_validation.py --skip-v1
   ```
   Erwartung: 2025-01-06 zeigt nicht mehr `Issue`. Fahrtage/Arbeitstage rücken Richtung Tibor-Range, weil B9 + R14-Continuation wirken.

2. **Falls auch dann nicht in Range:** V2-Tool-Schema einbauen (Sonnet bekommt direkt `is_tour_return`/`tour_context_hint`-Felder im Schema, nicht nur Prompt-Append). Das war im R14-Scope bewusst draußen — wäre dann der nächste Architektur-Schritt.

3. **Phantom-Audit fixen:** `reason_counted` aus `tage_detail` ins JSON-Result durchreichen, dann `_extract_phantom_z76` auf eindeutige SE-only-Phantome (Marker leer/X UND `cluster_foreign=False` UND SE-Override-reason) filtern.

**STOP nach Bericht** — kein Deploy, kein Default-Switch, kein zweiter Live-Run von mir, bevor du grünes Licht gibst.
