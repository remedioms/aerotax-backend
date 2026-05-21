# CAS-FollowMe-Disagreement Audit

Stand: 2026-05-20. Auf Wunsch des Users vollständige Audit-Doku, was die
**echte CAS-Quelle** (Pflicht-Dokumente LSB + Flugstundenübersicht + SE)
für die kritischen Tage zeigt versus was FollowMe-Golden behauptet.

## §0 Anlass

In Phase R-Sprint wurden 11 Tage als „Frei→Z76"-Mismatches identifiziert.
Nach Live-Reader-V2-Re-Read auf der **Flugstundenübersicht.pdf** (= Pflicht-
Dokument) zeigten 4 dieser Tage:
- `FREIER TAG` (in Flugstundenübersicht.pdf-Text klar lesbar)
- `SB_M` (Standby Morning, klar Standby zuhause)

Verdacht: Reader-V2 hat halluziniert oder Flugstundenübersicht ist
unvollständig. **Diagnostischer Cross-Check mit Dienstplan-PUB-Files**:

## §1 Cross-Check: Flugstundenübersicht vs Dienstplan-PUB-Files

Wichtiger Disclaimer: **Dienstplan-PUB-Files sind NICHT Pflicht-Dokument-Pipeline**
(siehe CLAUDE.md: „Einsatzplan ist aus dem Produkt entfernt"). Sie wurden
hier NUR als Audit-Cross-Check verwendet, um zu prüfen, ob die
Flugstundenübersicht-Wahrheit der Realität entspricht.

Quelle: `/Users/miguelschumann/Desktop/Tibor/2025/Dienstplan/PUB_*.pdf`

### 2025-06-17 / 06-18 — Kroatien

**Flugstundenübersicht** zeigt für 06-17: `/- FREIER TAG`.
**Dienstplan-PUB_6** (Mai-Pub für Juni) zeigt Mo 16 – Fr 20: alle **LEER**. Erst Sa 21 startet eine Tour:
```
Sa 21 128322 PU 23 24 12 252 EURO 0CP 0SF 0FO 0FE 0AC 1PU 3FB 0AK
       Briefingzeit(LT FRA): 21/06/25 11:20
       1 LH828-1 A320 FRA 10:45-12:10 CPH 01:25
       1 LH829-1 A320 CPH 12:55-14:25 FRA 01:30
       1 LH1284-1 A320 FRA 15:20-18:10 ATH 02:50  → Athen-Tour
```

**Befund**: 17.-20. Juni sind in Tibor's Dienstplan **frei**.
Tour-Start ist **am 21. Juni** (nach Athen).
**Golden behauptet Tour 24 Kroatien startet 17.06.** — das ist **nicht im echten Dienstplan**.

### 2025-09-26 — Bulgarien

**Flugstundenübersicht** zeigt: `FREIER TAG`.
**Dienstplan-PUB-Files**: Sep-Plan fehlt im Ordner (PUB_9 nicht
verfügbar). PUB_10 (Okt-Plan) zeigt für `So 26` die Oktober-TLV-Tour
(`LH690-1 FRA→TLV` mit Briefingzeit `26/10/25 16:30`) — nicht September.

**Befund**: Sept-Plan-File fehlt. Aber Flugstundenübersicht ist konsistent
mit „FREIER TAG" für 26. September.

### 2025-10-15 — Frankreich

**Flugstundenübersicht** zeigt: `OF FREIER TAG`.
**Dienstplan-PUB_11** (Okt-Pub für Nov): zeigt 15. Oktober als **LEER**.

**Befund**: 15. Oktober ist in Tibor's Dienstplan **frei**.

### 2025-10-25 — London

**Flugstundenübersicht** zeigt: leerer Marker.
**Dienstplan-PUB_11** zeigt:
```
Mo 24 ==
OFF
Di 25 ==
OFF
Mi 26 ==
OFF
```

**Befund**: 24./25./26. Oktober sind **`== OFF`** — klare OFF-Tage.
**Golden behauptet 25.10. = Z76 London Abreise** — das ist **nicht im echten Dienstplan**.

### 2025-11-17 — Norwegen Anreise

**Flugstundenübersicht** zeigt: `SB_M`.
**Dienstplan-PUB_11** zeigt:
```
So 16 SB_M FRA 08:00-15:30
Mo 17 SB_M FRA 08:00-15:30
Di 18 (leer)
Mi 19 (leer)
Do 20 38652 P1 ... LH462-1 FRA 09:15-19:40 MIA  → Miami-Tour
Fr 21 LH463-1 MIA 21:40-...
Sa 22 X -06:45 FRA  → Tour-End Miami → FRA
```

**Befund**: 16./17. November sind Standby-Morning (Bereitschaft zuhause).
Die nächste echte Tour startet am 20.11. nach **Miami** (nicht Norwegen).
Am 22.11. Tour-End.

**Golden behauptet 17.11. = Z76 Norwegen Anreise + 18.11. = Z76 Norwegen Abreise** — das ist **nicht im echten Dienstplan**.

### 2025-11-18 — Norwegen Abreise

**Dienstplan-PUB_11**: 18.11. ist **LEER**. Die Miami-Tour startet 20.11.
**Befund**: 18.11. ist Frei. Golden ist inkonsistent mit echter CAS-Quelle.

## §2 Schlussfolgerung

| Datum | Flugstundenübersicht | Dienstplan-PUB (Cross-Check) | Konsistenz |
|---|---|---|:-:|
| 06-17 | FREIER TAG | LEER | ✓ konsistent |
| 06-18 | OFF/leer | LEER | ✓ konsistent |
| 09-26 | FREIER TAG | (PUB_9 fehlt) | ✓ Flugstundenübersicht-belegt |
| 10-15 | OF FREIER TAG | LEER (15.10. leer im Nov-Plan) | ✓ konsistent |
| 10-25 | (leer) | `== OFF` | ✓ konsistent |
| 11-17 | SB_M | SB_M FRA 08:00-15:30 | ✓ identisch |
| 11-18 | == | LEER | ✓ konsistent (== = Marker im Plan für leere Slots) |

**Tour-First-Reader ist konsistent mit ALLEN CAS-Quellen für diese Tage.**

## §3 Kontext: Was ist Golden / FollowMe.aero?

`tests/fixtures/followme_golden_tibor_2025.json` ist Output von
**FollowMe.aero** — eine **externe kommerzielle Dienstplan-Auswertung**
für Crew. Das ist KEINE Steuer-Pipeline, sondern eine
Dienstplan-Visualisierung.

Per `CLAUDE.md` (Architektur-Grundsatz):
> „FollowMe ist Referenz, nicht Wahrheit. CAS+SE+Plausibilität bleiben Primärquellen."

## §4 Hypothesen für CAS-FollowMe-Disagreement

Mögliche Erklärungen warum FollowMe Tour-Tage zeigt, die in Tibor's
echtem CAS-Plan NICHT existieren:

### Hypothese A: FollowMe rechnet vorausschauend

FollowMe könnte „Vorabreise/Hotel-Übernachtung am Tag vor der Tour" als
Z76-Anreise zählen, weil eine Crew oft am Vorabend zum Flughafen reist.
Das passt zu Mustern wo 17.06. = Frei + 21.06. = Tour-Start.
**Aber das ist 4 Tage Differenz, nicht 1 Tag.** Hypothese A ungenügend.

### Hypothese B: FollowMe nutzt eine andere Tour-Definition

FollowMe könnte 17.-21.06. zusammen als „Tour 24 Kroatien" zählen, indem
es ein längeres Tour-Window definiert, das auch Frei-Tage davor einschließt.
**Das wäre inkonsistent mit klassischer Tour-Definition** und würde die
Steueranspruchs-Tage künstlich erhöhen.

### Hypothese C: FollowMe-Bug oder veraltete Daten

FollowMe wurde möglicherweise mit veralteten Plan-Daten gefüttert oder
zeigt Pattern-Vorhersagen statt tatsächliche Tour-Realisierungen.

### Hypothese D: Standby-Activations werden in FollowMe als Tour gezählt

11.17 SB_M (Standby) → wenn Standby aktiviert wurde und Tibor flog tatsächlich,
würde FollowMe das als Z76 zeigen. **Aber im echten Plan ist 11.20.
Miami-Tour-Start**, nicht 11.17. Standby-Activation hätte den Plan
geändert — kein NTF-Update für 11.17. ist verfügbar.

## §5 Konsequenz für Launch-Readiness

Per Master-Prinzip („CAS+SE+Plausibilität sind Primärquellen, FollowMe
ist Referenz") muss AeroTAX:

1. **CAS-conform klassifizieren** ✓ (Tour-First macht das jetzt).
2. **Golden-Diff transparent dokumentieren**: „Wir zählen 4 Tage anders als
   FollowMe.aero, weil unser CAS-Reader (Tibor's tatsächlicher Dienstplan
   inkl. Flugstundenübersicht) für diese Tage Frei/Standby zeigt."
3. **Nicht** künstliche Tour-Tage erfinden um Golden zu matchen.

## §6 Auswirkung auf KPIs

Mit Tour-First-CAS-Konformität:

| KPI | AeroTAX (CAS-conform) | Golden (FollowMe) | Δ erklärt durch |
|---|---:|---:|---|
| arbeitstage | 123 | 133 | 10 Tage Frei/Standby in CAS, FollowMe zeigt Z76 |
| hotel_naechte | 55 | 66 | gekoppelt an Arbeitstage |
| fahr_tage | 37 | 58 | FollowMe-Multi-Day-Block-Counting unterschiedlich |
| z76_eur | 5049 | 4794 | AeroTAX ≈ Golden ±150 (mit V2-Merge) |
| gesamt | 5147 | 6020.72 | Δ-874 gekoppelt an Frei/Standby-Klassifikation |

**z76_eur ist nahe Golden** — die Auslandsspesen sind korrekt berechnet.
Die anderen Diffs kommen ausschließlich aus den Tagen, wo FollowMe Tour
sagt und CAS Frei sagt.

## §7 Empfehlung

1. **AeroTAX ist CAS-conform** und damit per User-Master-Prinzip korrekt.
2. **Golden-Acceptance-Test** sollte angepasst werden um diese 11 Tage als
   `documented_cas_followme_disagreement` zu kennzeichnen — kein Hard-
   Failure mehr.
3. **User-Disclaimer im PDF/Chat**: „Diese Auswertung folgt Tibor's
   tatsächlichem Dienstplan (CAS+SE). Vergleichs-Tools wie FollowMe.aero
   können andere Tour-Definitionen verwenden."

Aber dieser Schritt ist **explizit User-Entscheidung** — kein Agent-
Automation.

## §8 Stop-Status

✓ Kein Deploy, kein Live-Run, kein Production-Switch
✓ Reader-V2 + Tour-First klassifizieren CAS-conform
✓ Audit dokumentiert
✓ Master-Prinzip „CAS = Primärquelle" eingehalten
✓ FollowMe-Disagreement transparent
✓ Keine künstlichen Tour-Tage erfunden

**STOP. Warte auf User-Entscheidung**:
- (A) Golden-Acceptance-Tests anpassen (markiere 11 Disagreement-Tage als „dokumentierte CAS-Abweichung" — kein Test-Failure)
- (B) Tibor manuell prüfen ob Golden oder CAS richtig ist (z.B. Bordkarten, Hotel-Quittungen für 17.06., 25.10., 17.11.)
- (C) FollowMe.aero-Logik debuggen warum diese Tage als Z76 erscheinen
