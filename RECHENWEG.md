# RECHENWEG — wie AeroTax die Werbungskosten berechnet

Dieses Dokument ist die **kompakte Referenz für Steuerberater oder Steuerberater-KIs**, die den Berechnungsweg fachlich prüfen wollen. Es zeigt Formel, Pauschalen und fachliche Prüfregeln, ohne im 7.700-Zeilen-Backend suchen zu müssen.

**Wichtig:** AeroTax ist ein Berechnungs- und Dokumentationswerkzeug. Es ersetzt keine Steuerberatung. Alle Ergebnisse müssen vom User bzw. Steuerberater geprüft werden.

---

## Die Hauptformel

```text
BRUTTO-WERBUNGSKOSTEN = Fahrtkosten
                      + Reinigung Berufskleidung
                      + Trinkgeld / Reisenebenkosten
                      + VMA Inland (Z72 + Z73 + Z74)
                      + VMA Ausland (Z76)
                      + optionale Werbungskosten-Belege

NETTO-WERBUNGSKOSTEN = max(0, Fahrtkosten − Z17)
                     + Reinigung Berufskleidung
                     + Trinkgeld / Reisenebenkosten
                     + optionale Werbungskosten-Belege
                     + max(0, (Z72 + Z73 + Z74 + Z76) − Z77)
```

**Topf-Trennung:**

- **Z17** aus der Lohnsteuerbescheinigung mindert nur den Fahrtkosten-Topf.
- **Z77** aus dem Streckeneinsatz mindert nur den VMA-/Reisekosten-Topf.
- Eine Übererstattung in einem Topf darf den anderen Topf nicht negativ machen.

---

## 1. Fahrtkosten Wohnung ↔ Homebase

**Grundlage:** Entfernungspauschale nach § 9 Abs. 1 Nr. 4 EStG.

```text
Fahrtkosten = min(km, 20) × Fahrtage × 0,30 €
            + max(0, km − 20) × Fahrtage × 0,38 €
```

**Beispiel:** 27 km × 71 Fahrtage

```text
20 × 71 × 0,30 € = 426,00 €
 7 × 71 × 0,38 € = 188,86 €
Summe             = 614,86 €
```

**Wichtig:** Es zählt grundsätzlich die einfache Entfernung, nicht Hin- und Rückweg. Ein mehrtägiger Umlauf erzeugt in der AeroTax-Logik nur einen Fahrtag am Tour-Start, nicht jeden Layover-Tag erneut.

---

## 2. Reinigungskosten Berufskleidung

```text
Reinigung = Arbeitstage × 1,60 €
```

Die Pauschale ist eine interne AeroTax-Arbeitshilfe für typische Crew-Uniformreinigung. Höhere tatsächlich nachgewiesene Kosten können separat über Belege angesetzt werden.

---

## 3. Trinkgeld / Reisenebenkosten

```text
Trinkgeld = Hotelnächte × 3,60 €
```

**Hotelnacht zählt nur bei echter Übernachtung außerhalb der Homebase**, typischerweise durch FL-Marker bzw. erkennbaren Layover. Tagestrips, Nachtflug-Heimkehr und reine Turnarounds zählen nicht.

---

## 4. VMA Inland — Z72 / Z73 / Z74

**Grundlage:** § 9 Abs. 4a EStG und BMF-/Lohnsteuer-Regeln zu Verpflegungsmehraufwand.

```text
Z72 = Tagestrip > 8h ohne Übernachtung       = 14 € pro Tag
Z73 = An-/Abreisetag mit Übernachtung Inland = 14 € pro Tag
Z74 = voller Inland-Zwischentag / 24h        = 28 € pro Tag
```

```text
VMA Inland = Z72_Tage × 14 €
           + Z73_Tage × 14 €
           + Z74_Tage × 28 €
```

**AeroTax-/FollowMe-Klassifikation:**

- **Z72:** Same-Day-Tagestrip mit Rückkehr am selben Tag. In der Projektlogik wird auch ein EU-Auslandsziel als Z72 behandelt, wenn keine Auslandsübernachtung vorliegt.
- **Z73:** Inland-Tour mit Übernachtung, z. B. Schulung oder Layover in MUC/HAM/BER. Nur An- und Abreisetag bekommen Z73.
- **Z74:** Seltener voller Inland-Zwischentag mit 24h-Abwesenheit.

**Hinweis:** Die Inlandspauschalen 14 €/28 € gelten auch 2026 weiter; die Aussage „unverändert seit 2014“ sollte nicht verwendet werden, weil die heutige 14/28-Systematik erst seit der Reform 2020 gilt.

---

## 5. VMA Ausland — Z76

**Grundlage:** § 9 Abs. 4a EStG und jährliches BMF-Schreiben zu Auslandsreisekosten.

```text
Z76 = Summe über alle Auslandstouren:
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

**Wann Z76?** Eine Tour bekommt Z76, wenn eine Übernachtung außerhalb Deutschlands vorliegt. Entscheidend ist die Tour-/Layover-Logik, nicht nur der einzelne Streckeneinsatz-Stempel.

**Prüfregel statt harter Wahrheit:** Z76 sollte zur Summe der steuerfrei gezahlten Auslandsspesen plausibel passen. Eine starke Abweichung ist ein Audit-Signal und soll eine erneute Klassifikation auslösen.

---

## 6. Abzüge: Z17 und Z77

### Z17 — AG-Fahrkostenzuschuss

```text
Fahrt-netto = max(0, Fahrtkosten − Z17)
```

Z17 mindert nur die Fahrten Wohnung ↔ erste Tätigkeitsstätte.

### Z77 — steuerfrei erstattete Spesen

```text
VMA-netto = max(0, (Z72 + Z73 + Z74 + Z76) − Z77)
```

Z77 mindert nur den Reisekosten-/VMA-Topf.

### Wichtige Korrektur zu Z76 ≤ Z77

Nicht sauber ist die Formulierung: **„Z76 ≤ Z77 ist mathematisch immer zwingend.“**

Besser:

```text
Z76 > Z77 ist ein starkes Warnsignal, aber nicht automatisch ein mathematischer Beweisfehler.
```

Warum: Z76 ist der berechnete Werbungskostenanspruch nach BMF-Pauschalen. Z77 ist die tatsächlich steuerfrei erstattete Summe des Arbeitgebers. In der Praxis sollten beide bei korrekt gelesenen Lufthansa-SE-Daten oft nah beieinander liegen, aber sie sind nicht dieselbe Größe. Für AeroTax ist deshalb sinnvoll:

- **Z76 deutlich über Z77** → Audit-Warnung / Self-Reflection auslösen.
- **Z76 grob im Bereich der Auslandsspesen** → plausibel.
- **Z77 > VMA-Brutto** → Netto-VMA wird 0 €, aber andere Töpfe bleiben erhalten.

---

## 7. Optionale Werbungskosten-Belege

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

## Korrigierte Beispiel-Rechnung

**Annahme:** Vollzeit-Crew FRA, Steuerjahr 2025

- Brutto: 52.884,81 €
- Z17: 330,00 €
- Z77: 4.655,00 €
- davon Auslandsspesen: 3.896,00 €
- davon Inlandsspesen: 759,00 €
- Entfernung: 27 km
- Fahrtage: 71
- Arbeitstage: 170
- Hotelnächte: 53
- Z72: 25 Tage
- Z73: 0 Tage
- Z74: 0 Tage
- **Z76 korrigiert: 3.896,00 €**

Die alte Beispielzahl **Z76 = 5.980 €** war im Dokument widersprüchlich, weil im selben Beispiel Z77 nur 4.655 € betrug und der Text gleichzeitig Z76 ≤ Z77 als harte Invariante formulierte.

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
                                             (= 284,86 + 272,00 + 190,80)
```

**Alternative, falls Z76 = 5.980 € fachlich wirklich aus den Touren kommt:** Dann darf das Beispiel nicht gleichzeitig behaupten, Z76 ≤ Z77 sei zwingend. In diesem Fall wäre die Rechnung mathematisch:

```text
VMA-Brutto = 350 + 5.980 = 6.330,00 €
VMA-netto  = max(0, 6.330 − 4.655) = 1.675,00 €
Netto-WK   = 284,86 + 272,00 + 190,80 + 1.675,00 = 2.422,66 €
```

Dann müsste aber der Audit-Text lauten: „Z76 > Z77 → Warnung prüfen“, nicht „mathematisch unmöglich“.

---

## Eintragung in Anlage N / WISO

| Bereich | Posten | Wert aus Beispiel | Hinweis |
|---|---:|---:|---|
| Fahrten Wohnung ↔ erste Tätigkeitsstätte | Entfernungspauschale brutto | 614,86 € | Z17 separat berücksichtigen bzw. durch Software abziehen lassen |
| Arbeitsmittel / Berufskleidung | Reinigung Uniform | 272,00 € | als Werbungskosten-Posten |
| Reisekosten / Reisenebenkosten | Trinkgeld | 190,80 € | Hotelnächte × 3,60 € |
| VMA Inland >8h | Z72 | 350,00 € | 25 Tage |
| VMA An-/Abreise Inland | Z73 | 0,00 € | 0 Tage |
| VMA Inland 24h | Z74 | 0,00 € | 0 Tage |
| VMA Ausland | Z76 | 3.896,00 € | nach BMF-/Tour-Auswertung |
| steuerfrei ersetzt | Z77 | 4.655,00 € | mindert nur den VMA-Topf |

**WISO-Hinweis:** Die saubere Variante ist die zeilenweise Eintragung. Eine direkte Netto-Summe unter einem Sammelposten kann rechnerisch funktionieren, ist aber weniger transparent und sollte nicht als Standardempfehlung im Tool stehen.

---

## Edge-Cases / Audit-Regeln

1. **Z76 deutlich > Z77 oder deutlich abweichend von Auslandsspesen** → Audit-Warnung, erneute Klassifikation.
2. **Hotelnächte > Arbeitstage** → logisch falsch, erneute Klassifikation.
3. **Fahrtage > Arbeitstage** → logisch falsch, erneute Klassifikation.
4. **Z76 vs. Auslandsspesen-Summe stark abweichend** → vermutlich falsch klassifizierte Ausland-/Inland-Touren oder fehlende SE-Dateien.
5. **Multi-LSB** → numerische Werte addieren; Personalien aus erster LSB übernehmen.
6. **Storno-Zeilen in SE** → ignorieren.
7. **Z77 ungewöhnlich niedrig bei Vollzeit** → Hinweis: möglicherweise fehlen Streckeneinsatz-Dateien.
8. **Teilzeit / Mutterschutz / Krankheit** → keine Hochrechnung; nur tatsächlich vorhandene Dienst-/SE-Daten zählen.

---

## Was AeroTax nicht abdeckt

- Doppelte Haushaltsführung
- Kinderbetreuungskosten
- Außergewöhnliche Belastungen
- Sonderausgaben außerhalb Werbungskosten
- Kapitaleinkünfte
- individuelle Steuerberatung / verbindliche Rechtsauskunft

---

## Prüfpunkte für Steuerberater / Review

1. Ist die Topf-Trennung Z17/Z77 korrekt?
2. Stimmen die Inlandspauschalen 14 €/28 € für das geprüfte Steuerjahr?
3. Stimmen die Auslandspauschalen im jeweiligen Jahr gemäß BMF-Tabelle?
4. Ist Z76 aus echten Auslandstouren mit Übernachtung abgeleitet?
5. Wird Z72 nicht fälschlich für mehrtägige Touren genutzt?
6. Werden Inland-Schulungen mit Hotel als Z73 behandelt?
7. Sind Storno-Zeilen und Nicht-Dienst-Tage sauber ausgeschlossen?
8. Werden PII und Belege DSGVO-konform verarbeitet?

