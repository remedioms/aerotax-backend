# RECHENWEG — wie AeroTax die Werbungskosten berechnet

Dieses Dokument ist die **kompakte Referenz für Steuerberater oder Steuerberater-KIs**, die den Berechnungsweg fachlich prüfen wollen. Es zeigt jede Formel, jede Pauschale und jede gesetzliche Grundlage auf 2 Seiten — ohne im 7.700-Zeilen `app.py` wühlen zu müssen.

---

## Die Hauptformel (1 Blick)

```
                             ┌─ Fahrtkosten      = km-Pauschale × Fahrtage
                             ├─ Reinigung        = 1,60€ × Arbeitstage
BRUTTO-WERBUNGSKOSTEN  =     ├─ Trinkgeld        = 3,60€ × Hotelnächte
                             ├─ VMA Inland       = Z72 + Z73 + Z74
                             ├─ VMA Ausland      = Z76 (BMF-Auslandspauschalen)
                             └─ Optional-Belege  = Σ aller WK-Belege

NETTO-WERBUNGSKOSTEN   = (Fahrt − Z17)        ← Fahrtkosten-Topf
                       + Reinigung + Trinkgeld + Optional-Belege
                       + max(0, (VMA-Brutto − Z77))   ← Reisekosten-Topf
```

**Code-Stelle:** `app.py` Zeilen 5862-5867 (`_berechne_via_hybrid`).

**Topf-Trennung erklärt:** Z17 (AG-Fahrkostenzuschuss) wird NUR von Fahrtkosten abgezogen, Z77 (LH-stfrei-Spesen) NUR von VMA. So wird vermieden, dass eine Über-Erstattung in einem Topf den anderen Topf negativ macht (das wäre unzulässig).

---

## Die einzelnen Posten

### 1. Fahrtkosten (Wohnung ↔ Homebase)

**Rechtsgrundlage:** § 9 Abs. 1 Nr. 4 EStG (Entfernungspauschale)

**Formel:**
```
Fahrtkosten = min(km, 20) × Fahrtage × 0,30 €
            + max(0, km − 20) × Fahrtage × 0,38 €
```

**Beispiel:** 27 km × 71 Fahrtage = 20×71×0,30 + 7×71×0,38 = 426 + 188,86 = **614,86 €**

**Code-Stelle:** `app.py:5571-5572` und `PENDLER_BY_YEAR` Z. 2747.

**Wichtig:** Pauschale gilt unabhängig vom Verkehrsmittel (Auto, Fahrrad, ÖV — aber Bahn/Flug haben Sonderregeln, hier nicht implementiert weil bei Crew untypisch).

---

### 2. Reinigungskosten Berufskleidung

**Rechtsgrundlage:** Verwaltungspraxis nach BFH VI R 56/91

**Formel:**
```
Reinigung = Arbeitstage × 1,60 €
```

**Code-Stelle:** `app.py:5835` und `REINIGUNG_PRO_TAG_BY_YEAR` Z. 2762.

**Hintergrund:** Pauschale ohne Beleg, weil Crew-Uniform (Hemd/Bluse mit Logo) zwingend zu reinigen ist. Bei höheren Beleg-Kosten ist optional auch der Einzelnachweis möglich (in WISO separat).

---

### 3. Trinkgelder / Reisenebenkosten

**Rechtsgrundlage:** § 9 Abs. 1 Nr. 5a EStG

**Formel:**
```
Trinkgeld = Hotelnächte × 3,60 €
```

**Code-Stelle:** `app.py:5836` und `TRINKGELD_PRO_NACHT_BY_YEAR` Z. 2767.

**Definition Hotelnacht:** Nur **echte** Übernachtungen außerhalb Homebase (FL-Marker im Dienstplan, ≥10h Bodenzeit nach EASA-FTL). Tagestrips zählen nicht.

---

### 4. VMA Inland — Z72 / Z73 / Z74

**Rechtsgrundlage:** § 9 Abs. 4a EStG + jährliches BMF-Schreiben "Reisekosten"

**Pauschalen 2025/2026 (Inland):**
```
Z72  Tagestrip > 8h ohne Übernachtung    14,00 €/Tag
Z73  An- oder Abreisetag mit Übernachtung 14,00 €/Tag
Z74  Inland 24h ohne Tagestrip-Pattern   28,00 €/Tag
```

**Formel:**
```
VMA Inland = Z72_Tage × 14 + Z73_Tage × 14 + Z74_Tage × 28
```

**Code-Stelle:** `app.py:5773-5776` und `BMF_INLAND_BY_YEAR` Z. 2742.

**Wann Z72/Z73/Z74?** Siehe Decision-Tree in `referenz_faelle.txt` Section 1 + 2. Kurz:
- **Z72:** User fliegt morgens raus, kommt abends zurück (Same-Day, egal ob Inland-Ziel oder EU-Ausland-Ziel — die "FollowMe-Konvention").
- **Z73:** User fliegt zu Inland-Ziel (FRA/MUC/HAM/...), übernachtet, kommt zurück. Nur Anreise- + Abreisetag = Z73; Volltage dazwischen sind nur Arbeitstage ohne extra VMA.
- **Z74:** Selten — Inland-Tag mit ≥24h Anwesenheit ohne Tagestrip-Charakter.

---

### 5. VMA Ausland — Z76 (BMF-Auslandspauschalen pro Land)

**Rechtsgrundlage:** § 9 Abs. 4a EStG + BMF-Schreiben "Auslandstagegelder" (jährlich)

**Formel (vereinfacht):**
```
Z76 = Σ (über alle Auslandstouren):
        BMF-Anreisesatz × (Anreise- + Abreisetage der Tour)
      + BMF-24h-Satz × Volltage zwischen An- und Abreise
```

**Beispiel:** 4-Tages-Tour BLR (Indien):
- Anreise-Satz Indien 2025 = 30 €, 24h-Satz = 39 €
- Tag 1 (Anreise): 30 €
- Tag 2-3 (Volltage): 2 × 39 = 78 €
- Tag 4 (Abreise): 30 €
- **Z76 für diese Tour: 138 €**

**Code-Stelle:** Berechnung in `_opus_classify_days_v2()` (`app.py:5144 ff.`); Land-Tabelle in `bmf_data.py` (`BMF_AUSLAND_BY_YEAR`).

**Wann Z76?** Tour mit Übernachtung (FL-Marker am Folgetag) UND Layover-Ort liegt außerhalb Inland-IATA-Liste (FRA/MUC/HAM/DUS/STR/CGN/HAJ/BER/LEJ/NUE/BRE/FMO/PAD).

---

### 6. Abzüge: Z17 und Z77

**Z17 — AG-Fahrkostenzuschuss (Lohnsteuerbescheinigung Zeile 17):**
```
Fahrt-netto = max(0, Fahrtkosten − Z17)
```
Wird NUR von Fahrtkosten abgezogen, nicht von VMA.

**Z77 — LH-steuerfrei-Spesen (Streckeneinsatz-Abrechnung):**
```
VMA-netto = max(0, (Z72+Z73+Z74+Z76) − Z77)
```
Wird NUR von VMA abgezogen, nicht von Fahrtkosten.

**Mathematische Invariante:** Z76 ≤ Z77 (BMF-Pauschale immer ≤ AG-Auszahlung). Der Backend-Code prüft das und triggert ein Self-Reflection-Pass wenn die Klassifikation diese Regel verletzt — siehe `_detect_classification_issues()` `app.py:5410`.

---

### 7. Optionale Werbungskosten-Belege

User kann zusätzlich Belege hochladen (Telefon, Gewerkschaft, Steuerberatung etc.). Diese werden via Vision-API gelesen und einzeln in WISO eingetragen — separate Zeilen, nicht mit VMA verrechnet.

| Beleg-Typ | Absetzbar | Default-Anteil |
|---|---|---|
| Telefon & Internet | 20% Jahreskosten (BFH-Praxis) | 20% |
| Gewerkschaft (UFO, ver.di) | 100% | 100% |
| Berufsbedingt-Fortbildung | 100% | 100% |
| Steuerberatung | 100% | 100% |
| Kontoführung | 100% (max 16€ Pauschale) | min(real, 16) |
| Fachliteratur | 100% | 100% |

**Code-Stelle:** `app.py:5840-5860`. Logik dynamisch je nach Beleg-Typ.

---

## Komplette Beispiel-Rechnung

**Annahme:** Vollzeit-Crew FRA, Steuerjahr 2025
- Brutto: 52.884,81 € (aus LSB)
- Z17 (AG-Fahrkosten): 330 €
- Z77 (LH-stfrei): 4.655 € (davon 3.896 € Auslandsspesen, 759 € Inlandsspesen)
- 27 km Anfahrt zur Homebase
- 71 Fahrtage (Tour-Starts + Office-Days)
- 170 Arbeitstage
- 53 Hotelnächte (alle FL-Marker = Auslands-Layovers)
- 25 × Z72-Tage (Same-Day-Tagestrips)
- 0 × Z73-Tage
- 0 × Z74-Tage
- Z76 = 5.980 € (Σ über alle Auslandstouren mit BMF-Tabelle)

**Berechnung:**
```
Fahrtkosten = 20×71×0,30 + 7×71×0,38             = 614,86 €
Reinigung   = 170 × 1,60                          = 272,00 €
Trinkgeld   = 53 × 3,60                           = 190,80 €
Z72         = 25 × 14                             = 350,00 €
Z73         = 0                                   = 0,00 €
Z74         = 0                                   = 0,00 €
Z76         = (Σ BMF-Auslandstouren)              = 5.980,00 €
                                                ──────────
BRUTTO-WERBUNGSKOSTEN                            = 7.407,66 €

Fahrt-netto    = max(0, 614,86 − 330)            = 284,86 €
VMA-Brutto     = 350 + 0 + 0 + 5.980             = 6.330,00 €
VMA-netto      = max(0, 6.330 − 4.655)           = 1.675,00 €
                                                ──────────
NETTO-WERBUNGSKOSTEN für WISO                    = 2.422,66 €
                                                  (= 284,86 + 272 + 190,80 + 1.675)
```

---

## Eintragung in die Steuererklärung (Anlage N)

| Anlage-N-Zeile | Posten | Wert | Kommentar |
|---|---|---|---|
| Z. 27-30 | Fahrten Wohnung ↔ erste Tätigkeitsstätte | 614,86 € (brutto) | WISO zieht Z17 automatisch ab |
| Z. 62 | Reinigung Arbeitskleidung | 272,00 € | — |
| Z. 68 | Reisenebenkosten | 190,80 € | — |
| Z. 72 | VMA Inland >8h | 350,00 € (25 Tage) | — |
| Z. 73 | VMA An-/Abreisetage Inland | 0,00 € (0 Tage) | — |
| Z. 74 | VMA Inland 24h | 0,00 € (0 Tage) | — |
| Z. 76 | VMA Ausland | 5.980,00 € | — |
| Z. 77 | LH-stfrei-Erstattung Spesen | 4.655,00 € | wird von Z76 abgezogen |

**WISO-Vereinfachung:** Manche User tragen direkt `Netto-Werbungskosten = 2.422,66 €` unter "Reisenebenkosten" ein. Das ist mathematisch äquivalent (Finanzamt rechnet eh auf den Netto), aber der konkrete Tax-Compliance-Pfad ist die saubere Eintragung Zeile-für-Zeile.

---

## Edge-Cases die der Code abfängt

1. **Z76 > Z77 (mathematisch unmöglich)** → Self-Reflection-Loop ruft Opus erneut auf mit Korrektur-Auftrag.
2. **Hotel > Arbeitstage (logisch unmöglich)** → wie 1.
3. **Fahr-Tage > Arbeitstage (logisch unmöglich)** → wie 1.
4. **Z76 vs Auslandsspesen-Summe ±40% Diff** → wie 1.
5. **Multi-LSB (Arbeitgeber-Wechsel)** → Brutto/Lohnsteuer/SozVers ADDIEREN, Personalien von 1. LSB. Code: `_sonnet_read_lsb_v2` Z. 4929-4942.
6. **Storno-Zeilen in SE (X-Marker)** → ignoriert. Wissens-Buch-Anweisung im Sonnet-Prompt.
7. **Z77 < 500 € bei Vollzeit** → Hinweis-Note für User (möglicherweise nicht alle SE-Files hochgeladen).
8. **Teilzeit / Mutterschutz / Krankheit** → Werte skalieren proportional, keine Plausi-Bandbreiten-Hochrechnung.

---

## Was AeroTax NICHT abdeckt (User-Verantwortung)

- Doppelte Haushaltsführung (Anlage N Zeile 81+)
- Kinderbetreuungskosten (Anlage Kind)
- Außergewöhnliche Belastungen (Anlage AVOR)
- Sonderausgaben außerhalb Werbungskosten (Vorsorge etc.)
- Kapitaleinkünfte (Anlage KAP)

Dafür empfehlen wir Steuerberater oder VLH (Lohnsteuerhilfeverein).

---

## Wo ist welche Logik im Code?

| Was | Datei | Zeilen |
|---|---|---|
| Sonnet liest LSB | `app.py` | 4801-4954 |
| Sonnet liest SE-Summen | `app.py` | 4955-5143 |
| Opus klassifiziert Tage | `app.py` | 5144-5409 |
| Self-Reflection-Loop (Math-Check) | `app.py` | 5410-5462 |
| Hauptformel (Brutto+Netto) | `app.py` | 5862-5867 |
| Topf-Trennung Z17/Z77 | `app.py` | 5865-5867 |
| BMF Inland-Pauschalen | `app.py` | 2742-2747 (`BMF_INLAND_BY_YEAR`) |
| BMF Ausland-Pauschalen | `bmf_data.py` | komplett (`BMF_AUSLAND_BY_YEAR`) |
| Pendlerpauschale | `app.py` | 2747-2754 (`PENDLER_BY_YEAR`) |
| Reinigung + Trinkgeld | `app.py` | 2762-2772 |
| Klassifikations-Wissensbuch | `referenz_faelle.txt` | gesamt (573 Zeilen) |
| Tag-für-Tag-Audit-Output | `app.py` | 5193-5210 (`tage_detail` schema) |
| PDF-Aufbau | `app.py` | 6800-7700 |

---

## Wenn ihr das Tool prüft, achtet auf:

1. **Stimmt die Hauptformel?** (siehe oben Block 1) — § 9 EStG-konform?
2. **Topf-Trennung Z17/Z77** — wird das richtig gemacht?
3. **BMF-Pauschalen aktuell** — sind die 14€/28€-Sätze für 2025 korrekt? (Ja, unverändert seit 2014.)
4. **Z76-Klassifikation** — wird ein 4-Tages-BLR-Tour wirklich mit BMF-Indien-Pauschalen × 4 Tage gerechnet?
5. **Z73 vs Z76** — Inland vs. Ausland-Übernachtung sauber unterschieden?
6. **Self-Check Math-Invariants** — funktioniert das Math-Konsistenz-System?
7. **Edge-Cases Multi-LSB / Teilzeit** — sind die abgedeckt?

Bei Fragen: schaut in `referenz_faelle.txt` Section 12 ("Self-Check vor Tool-Aufruf") — das ist die Liste die Opus selbst durchgeht bevor er klassifiziert.
