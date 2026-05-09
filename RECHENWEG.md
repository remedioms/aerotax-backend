# RECHENWEG — AeroTax Werbungskosten-Berechnung

Dieses Dokument ist die **fachliche Kurzreferenz** für Entwickler, Steuerberater und prüfende KI-Systeme. Es beschreibt den Berechnungsweg, die Topf-Trennung, die Plausibilitätschecks und die Grenzen des Tools, ohne in `app.py` suchen zu müssen.

**Wichtig:** AeroTax ist ein Berechnungs- und Dokumentationswerkzeug. Es ersetzt keine Steuerberatung und keine verbindliche Auskunft. Nutzer müssen die Werte, Belege und Eintragungen selbst bzw. mit Steuerberater prüfen.

---

## 0. Grundprinzip

AeroTax trennt konsequent zwischen Brutto-Werbungskosten, steuerfreien Arbeitgeber-Erstattungen und Netto-Werbungskosten. Erstattungen mindern nur den jeweils passenden Topf.

---

## 1. Hauptformel

```text
BRUTTO-WERBUNGSKOSTEN =
    Fahrtkosten Wohnung ↔ Homebase
  + Reinigung Berufskleidung
  + Trinkgeld / Reisenebenkosten
  + VMA Inland  (Z72 + Z73 + Z74)
  + VMA Ausland (Z76)
  + optionale Werbungskosten-Belege
```

```text
NETTO-WERBUNGSKOSTEN =
    max(0, Fahrtkosten − Z17)
  + Reinigung Berufskleidung
  + Trinkgeld / Reisenebenkosten
  + optionale Werbungskosten-Belege
  + max(0, (Z72 + Z73 + Z74 + Z76) − Z77)
```

### Topf-Trennung

| Kürzung | Quelle | Kürzt nur | Kürzt nicht |
|---|---|---|---|
| Z17 | Lohnsteuerbescheinigung | Fahrtkosten | VMA, Reinigung, Trinkgeld, Belege |
| Z77 | Streckeneinsatz / steuerfreie Spesen | VMA / Reisekosten-Topf | Fahrtkosten, Reinigung, Trinkgeld, Belege |

Eine Übererstattung in einem Topf darf andere Töpfe nicht negativ machen. Deshalb nutzt AeroTax `max(0, …)` pro Topf.

---

## 2. Fahrtkosten Wohnung ↔ Homebase

**Grundlage:** Entfernungspauschale nach § 9 Abs. 1 Nr. 4 EStG.

```text
Fahrtkosten =
    min(km, 20) × Fahrtage × 0,30 €
  + max(0, km − 20) × Fahrtage × 0,38 €
```

**Beispiel:** 27 km × 71 Fahrtage

```text
20 × 71 × 0,30 € = 426,00 €
 7 × 71 × 0,38 € = 188,86 €
Summe             = 614,86 €
```

**Wichtig:**

- Es zählt grundsätzlich die einfache Entfernung, nicht Hin- und Rückweg.
- Ein mehrtägiger Umlauf erzeugt in AeroTax grundsätzlich **einen Fahrtag am Tour-Start**, nicht einen Fahrtag pro Layover.
- Office-/Schulungstage an der Homebase können tägliche Fahrtage sein, wenn der Nutzer tatsächlich zur Homebase fährt.
- Z17 mindert nur diesen Topf.

---

## 3. Reinigung Berufskleidung

```text
Reinigung = Arbeitstage × 1,60 €
```

Die 1,60 € sind eine **AeroTax-Arbeitshilfe / Pauschalannahme** für typische Crew-Uniformreinigung. Sie ist nicht als gesetzlich garantierter Fixbetrag zu formulieren. Höhere nachgewiesene Kosten können separat als Beleg angesetzt werden, wenn sie beruflich veranlasst und plausibel nachweisbar sind.

---

## 4. Trinkgeld / Reisenebenkosten

```text
Trinkgeld = Hotelnächte × 3,60 €
```

Die 3,60 € sind eine **AeroTax-Pauschalannahme für typische Reisenebenkosten**. Sie sollte im PDF und in der Doku als prüfbarer Ansatz dargestellt werden, nicht als garantiert akzeptierter Betrag.

**Hotelnacht zählt nur bei echter Übernachtung außerhalb der Homebase**, typischerweise durch FL-Marker bzw. erkennbaren Layover. Tagestrips, Nachtflug-Heimkehr und Turnarounds zählen nicht.

---

## 5. VMA Inland — Z72 / Z73 / Z74

**Grundlage:** § 9 Abs. 4a EStG und die Lohnsteuer-/BMF-Regeln zu Verpflegungsmehraufwand.

```text
Z72 = Tagestrip > 8h ohne Übernachtung        = 14 € pro Tag
Z73 = An-/Abreisetag mit Inland-Übernachtung = 14 € pro Tag
Z74 = voller Inland-Zwischentag / 24h        = 28 € pro Tag
```

```text
VMA Inland =
    Z72_Tage × 14 €
  + Z73_Tage × 14 €
  + Z74_Tage × 28 €
```

**AeroTax-/FollowMe-Klassifikation:**

- **Z72:** Same-Day-Tagestrip mit Rückkehr am selben Tag. In der Projektlogik wird auch ein EU-Auslandsziel als Z72 behandelt, wenn keine Auslandsübernachtung vorliegt.
- **Z73:** Inland-Tour mit Übernachtung, z. B. Schulung oder Layover in MUC/HAM/BER. Nur An- und Abreisetag bekommen Z73.
- **Z74:** Seltener voller Inland-Zwischentag mit 24h-Abwesenheit.

**Jahresprüfung:** Die Inlandssätze 14 €/28 € sind für 2025/2026 unverändert. Nicht schreiben: „seit 2014 unverändert“, weil die heutige 14/28-Systematik seit 2020 gilt.

---

## 6. VMA Ausland — Z76

**Grundlage:** § 9 Abs. 4a EStG und jährliches BMF-Schreiben zu Auslandsreisekosten.

```text
Z76 =
  Summe über alle Auslandstouren:
    BMF-Anreisesatz für Anreise- und Abreisetage
  + BMF-24h-Satz für volle Zwischentage
```

**Beispiel BLR / Indien, 4-Tages-Tour:**

```text
Tag 1 Anreise:   30 €
Tag 2 Volltag:   39 €
Tag 3 Volltag:   39 €
Tag 4 Abreise:   30 €
Summe Z76:      138 €
```

**Wann Z76?** Eine Tour bekommt Z76, wenn eine Übernachtung außerhalb Deutschlands vorliegt. Entscheidend ist die Tour-/Layover-Logik, nicht nur ein einzelner FRA- oder Inland-Stempel in der Streckeneinsatz-Datei.

---

## 7. Z76, Z77 und Audit-Regel

### Fachlich korrekte Trennung

- **Z76** = berechneter Werbungskostenanspruch aus BMF-Auslandspauschalen.
- **Z77** = tatsächlich steuerfrei vom Arbeitgeber erstattete Spesen laut SE/LSB-Logik.
- Beides sind verwandte, aber nicht identische Größen.

### Projektregel

Nicht formulieren: „Z76 muss immer kleiner oder gleich Z77 sein.“

Stattdessen:

```text
Z76 > Z77 ist ein starkes Audit-Warnsignal und löst einen Recheck aus,
aber es ist nicht automatisch ein rechtlicher oder mathematischer Beweisfehler.
```

**AeroTax-Verhalten:**

- Z76 > Z77 → Self-Reflection/Recheck auslösen.
- Z76 deutlich abweichend von Auslandsspesen-SE → Recheck auslösen.
- Z76 nicht automatisch auf Z77 deckeln.
- Z77 mindert den gesamten VMA-Topf über `max(0, VMA-Brutto − Z77)`.

**Typische Ursachen für Z76 > Z77:**

- SE-Dateien fehlen oder wurden unvollständig gelesen.
- Tage wurden fälschlich als Auslandstour statt Inland/Same-Day klassifiziert.
- Arbeitgeber hat anteilig/gekürzt/anders gezahlt.
- Mahlzeiten wurden gestellt oder es gibt Zwölftel-/Kürzungslogik.
- BMF-Satz und Arbeitgeber-Erstattung sind nicht identisch.

---

## 8. Mahlzeitenkürzung / gestellte Mahlzeiten

Wenn Mahlzeiten vom Arbeitgeber gestellt oder übernommen wurden, können VMA-Pauschalen zu kürzen sein. AeroTax sollte diese Fälle im PDF als **Prüfhinweis** ausweisen, wenn die Datenlage nicht eindeutig ist.

Standardformulierung:

```text
Bitte prüfen, ob auf einzelnen Reisen Mahlzeiten gestellt wurden.
Falls ja, können die Verpflegungspauschalen steuerlich zu kürzen sein.
```

---

## 9. Optionale Werbungskosten-Belege

Optionale Belege werden separat behandelt und nicht mit VMA vermischt.

| Beleg-Typ | Ansatz in AeroTax | Bemerkung |
|---|---:|---|
| Telefon & Internet | typischer beruflicher Anteil, z. B. 20 % | bei Nachweis ggf. höher |
| Gewerkschaft | 100 % | z. B. UFO/ver.di |
| Fortbildung | 100 % | beruflich veranlasst |
| Steuerberatung | Werbungskosten-Anteil | nicht immer komplett Werbungskosten |
| Kontoführung | Pauschale bis 16 € | typische Vereinfachung |
| Fachliteratur | 100 % | beruflich veranlasst |

---

## 10. Beispielrechnung — konsistenter Standardfall

**Annahme:** Vollzeit-Crew FRA, Steuerjahr 2025

| Wert | Betrag / Anzahl |
|---|---:|
| Brutto | 52.884,81 € |
| Z17 | 330,00 € |
| Z77 | 4.655,00 € |
| davon Auslandsspesen | 3.896,00 € |
| davon Inlandsspesen | 759,00 € |
| Entfernung | 27 km |
| Fahrtage | 71 |
| Arbeitstage | 170 |
| Hotelnächte | 53 |
| Z72 | 25 Tage |
| Z73 | 0 Tage |
| Z74 | 0 Tage |
| Z76 | 3.896,00 € |

```text
Fahrtkosten = 20×71×0,30 + 7×71×0,38    =   614,86 €
Reinigung   = 170 × 1,60                 =   272,00 €
Trinkgeld   = 53 × 3,60                  =   190,80 €
Z72         = 25 × 14                    =   350,00 €
Z73         = 0 × 14                     =     0,00 €
Z74         = 0 × 28                     =     0,00 €
Z76         = Auslandstouren/BMF          = 3.896,00 €
                                             ──────────
BRUTTO-WERBUNGSKOSTEN                     = 5.323,66 €

Fahrt-netto = max(0, 614,86 − 330)        =   284,86 €
VMA-Brutto  = 350 + 0 + 0 + 3.896         = 4.246,00 €
VMA-netto   = max(0, 4.246 − 4.655)       =     0,00 €
                                             ──────────
NETTO-WERBUNGSKOSTEN                      =   747,66 €
```

---

## 11. Beispielrechnung — Warnfall Z76 > Z77

Wenn die Tag-für-Tag-Klassifikation **Z76 = 5.980 €** ergibt, aber **Z77 = 4.655 €** beträgt, dann ist das kein automatischer Rechenabbruch, sondern ein Audit-Fall.

```text
VMA-Brutto = Z72 + Z73 + Z74 + Z76
           = 350 + 0 + 0 + 5.980
           = 6.330,00 €

VMA-netto  = max(0, 6.330 − 4.655)
           = 1.675,00 €

Netto-WK   = 284,86 + 272,00 + 190,80 + 1.675,00
           = 2.422,66 €
```

**Pflicht-Prüfung:** SE-Vollständigkeit, Ausland/Inland-Klassifikation, Storno-Zeilen, FL-Marker, Mahlzeiten/Kürzungen und BMF-Jahrestabelle prüfen.

---

## 12. Eintragung in Anlage N / WISO

Die saubere Variante ist die **zeilenweise Eintragung**.

| Bereich | Posten | Wert aus Standardbeispiel | Hinweis |
|---|---:|---:|---|
| Fahrten Wohnung ↔ erste Tätigkeitsstätte | Entfernungspauschale brutto | 614,86 € | Z17 separat berücksichtigen bzw. von Software abziehen lassen |
| Arbeitsmittel / Berufskleidung | Reinigung Uniform | 272,00 € | als Werbungskosten-Posten |
| Reisekosten / Reisenebenkosten | Trinkgeld | 190,80 € | Hotelnächte × 3,60 € |
| VMA Inland >8h | Z72 | 350,00 € | 25 Tage |
| VMA An-/Abreise Inland | Z73 | 0,00 € | 0 Tage |
| VMA Inland 24h | Z74 | 0,00 € | 0 Tage |
| VMA Ausland | Z76 | 3.896,00 € | nach BMF-/Tour-Auswertung |
| steuerfrei ersetzt | Z77 | 4.655,00 € | mindert nur den VMA-Topf |

Eine direkte Netto-Summe unter einem Sammelposten ist weniger transparent und sollte im Tool nicht als Standardempfehlung stehen.

---

## 13. Harte Invarianten vs. Audit-Plausibilität

### Harte Invarianten

Diese Checks dürfen nicht verletzt werden:

```text
Hotelnächte ≤ Arbeitstage
Fahrtage ≤ Arbeitstage
Arbeitstage ≤ 365
Fahrtage ≤ 365
Hotelnächte ≤ 365
```

### Audit-Plausibilitätschecks

Diese Checks sind Warnsignale, keine automatischen Rechtsfehler:

```text
Z76 > Z77
Z76 stark abweichend von Auslandsspesen-SE
Z73 = 0 bei Vollzeit-Crew
Z77 ungewöhnlich niedrig bei Vollzeit
Fahrtage oder Hotelnächte stark außerhalb typischer Bandbreite
```

---

## 14. Was AeroTax nicht abdeckt

- Doppelte Haushaltsführung
- Kinderbetreuungskosten
- Außergewöhnliche Belastungen
- Sonderausgaben außerhalb Werbungskosten
- Kapitaleinkünfte
- individuelle Steuerberatung
- verbindliche Rechtsauskunft
- automatische Mahlzeitenkürzung, sofern nicht eindeutig aus Dokumenten erkennbar

---

## 15. Prüfpunkte vor Veröffentlichung / Steuerberater-Review

1. Stimmen Z17-/Z77-Topf-Trennung und Netto-Formel?
2. Stimmen die Inlandspauschalen 14 €/28 € für das Steuerjahr?
3. Ist `BMF_AUSLAND_BY_YEAR` für das Steuerjahr vollständig und aktuell?
4. Wird Z76 aus echten Auslandstouren mit Übernachtung abgeleitet?
5. Werden Same-Day-Trips nicht fälschlich als Z76 behandelt?
6. Werden Inland-Schulungen mit Hotel als Z73 behandelt?
7. Werden Storno-Zeilen ignoriert?
8. Werden gestellte Mahlzeiten als Prüfhinweis berücksichtigt?
9. Sind PII, Uploads, Audit-Logs und Belege DSGVO-konform verarbeitet?
10. Enthalten Frontend/PDF keine Garantien wie „steuerlich garantiert“, „100 % akzeptiert“ oder „ersetzt Steuerberater“?
