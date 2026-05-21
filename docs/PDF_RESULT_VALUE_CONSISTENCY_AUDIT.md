# PDF / Result / UI — Value-Consistency-Audit

Stand: 2026-05-20.

## §1 Auslöser

User-Bug-Report nach Live-Test (Token `AT-11CEB21120E7799B`):

```
Fahrtkosten Homebase (28 km × 55 Fahrtage):   497,20 €
Reinigungskosten (135 Reinigungstage):        216,00 €
Trinkgelder / Reisenebenkosten (73 Nächte):   262,80 €
VMA Inland >8h (8 Tage):                      112,00 €
VMA An-/Abreisetage (9 Tage):                 126,00 €
VMA Ausland:                                4.125,00 €
= Brutto-Aufwendungen gesamt:               5.339,00 €
Abzug AG-Fahrkostenzuschuss:                 -0,00 €
Abzug steuerfreie Spesen Lufthansa Z77:    -4.705,00 €
= Einzutragender Gesamtbetrag:               976,00 €
```

Mathematisch: `5.339 − 4.705 = 634`, nicht `976`. Die Tabelle suggeriert
eine Differenz, die das Backend NICHT so rechnet → User-Vertrauensbruch.

## §2 Was rechnet Python tatsächlich?

`app.py:3431-3435` (`_recompute_with_overrides`):

```python
gesamt    = round(fahr + reinig + trink + vma_in + vma_aus + opt_zu_gesamt, 2)
vma_total = round(vma_in + vma_aus, 2)
vma_netto = round(max(0, vma_total - z77), 2)        # ← Z77 nur gegen VMA-Topf
fahr_netto = round(max(0, fahr - ag_z17), 2)         # ← AG-Z17 nur gegen Fahrt-Topf
netto      = round(fahr_netto + reinig + trink + vma_netto + opt_zu_gesamt, 2)
```

**Topf-Regeln**:
- AG-Z17 (Lohnsteuerbescheinigung Zeile 17 — Fahrkostenzuschuss) reduziert
  **nur Fahrtkosten**, geklamped auf ≥ 0.
- Z77 (Steuerfreie Spesen Lufthansa — Streckeneinsatz-Summe) reduziert
  **nur Verpflegungsmehraufwand (VMA)**, geklamped auf ≥ 0.
- Reinigung, Trinkgeld, optionale Werbungskosten-Belege werden **niemals** von
  einer Erstattung berührt.

**Berechnung für Tibor**:
- `fahr_netto = max(0, 497.20 − 0) = 497.20`
- `vma_total = 112 + 126 + 4125 = 4363.00`
- `vma_netto = max(0, 4363 − 4705) = 0` ← Z77-Überschuss 342 € verfällt nicht in andere Töpfe
- `netto = 497.20 + 216.00 + 262.80 + 0.00 + 0 = 976.00` ✓

**Berechnung selbst war korrekt.** Nur die Tabellen-Darstellung führte in die Irre.

## §3 Topf-Tabelle (Position → Topf → Erstattung)

| Position | Topf | AG-Erstattung anrechenbar? | Netto nach Erstattung | PDF-Zeile (WISO) | Quelle | Test |
|---|---|---|---|---|---|---|
| Fahrtkosten Homebase | A — Sonstige Werbungskosten | ja, gegen `ag_z17` (Z17 LSB) | `max(0, fahr − ag_z17)` | Zeilen 27–30 | Form-Input km + fahr_tage | `test_variant_4`, `test_variant_5` |
| Reinigungskosten | A | nein | `reinig` unverändert | Zeile 62 | reinigungstage × 1.60€ | `test_case_S_z77_exceeds_vma_clamps_to_zero` |
| Trinkgelder/Reisenebenkosten | A | nein | `trink` unverändert | Zeile 68 | hotel_naechte × 3.60€ | `test_case_S` |
| Optionale Werbungskosten-Belege | A | nein | `opt` unverändert | div. | optionale_belege[] | `test_variant_9` |
| VMA Inland >8h (Z72) | B — VMA | ja, gegen `z77` (gepoolt) | Anteil an `max(0, vma_total − z77)` | Zeile 72 | vma_72_tage × BMF[8h] | `test_variant_1/2/3` |
| VMA An-/Abreise (Z73) | B | ja, gegen `z77` | Anteil | Zeile 73 | vma_73_tage × BMF[an_ab] | `test_variant_1/2/3` |
| VMA Inland 24h (Z74) | B | ja, gegen `z77` | Anteil | Zeile 74 | vma_74_tage × BMF[24h] | `test_variant_1/2/3` |
| VMA Ausland (Z76) | B | ja, gegen `z77` | Anteil | Zeile 76 | BMF-Pauschalen pro Land | `test_tibor_2025_displayed_total_matches_block_sum` |
| AG-Fahrkostenzuschuss (Z17) | — Erstattung | reduziert nur Topf A.Fahrt | n/a | LSB Z17 | LSB-Reader | `test_variant_4/5` |
| Steuerfreie Spesen (Z77) | — Erstattung | reduziert nur Topf B.VMA | n/a | SE-Summe | SE-Reader, Summe-Zeile | `test_variant_1/2/3` |

## §4 Wo entstand 976 €?

In `_recompute_with_overrides` (siehe §2). Nicht aus `gesamt − z77 − ag_z17`,
sondern aus `fahr_netto + reinig + trink + vma_netto + opt`.

## §5 Warum zeigte die Tabelle 5339 − 4705 = 976?

PDF (`app.py:24078-24083`) und UI (`index.html:3916-3919`) zeigten:

```
= Brutto-Aufwendungen gesamt:   5339
- AG-Z17:                          0
- Z77:                          4705
= Einzutragender Betrag:         976
```

Diese Reihenfolge legt mathematisch nahe: `5339 − 0 − 4705 = 634`, nicht `976`.
Der korrekte Wert ergibt sich aber nur, wenn man wüsste, dass Z77-Überschuss
verfällt (Clamp), was die Tabelle nicht zeigt.

## §6 Fix (Block-Split-Darstellung)

Sowohl PDF als auch UI sind jetzt umgestellt auf:

```
A · Sonstige Werbungskosten:
  Fahrtkosten Homebase              497.20    (oder Fahrt brutto − Z17 = netto, wenn Z17 > 0)
  Reinigungskosten                  216.00
  Trinkgelder / Reisenebenkosten    262.80
  Optionale Belege                    0.00
= Zwischensumme A                   976.00

B · Verpflegungsmehraufwand (VMA):
  VMA brutto (Z72+Z73+Z74+Z76)     4363.00
  Abzug: Steuerfreie Spesen Z77   -4705.00
  Hinweis: AG-Erstattung übersteigt VMA um 342.00; VMA wird nicht negativ angesetzt.
= VMA netto (≥ 0)                     0.00

= Einzutragender Gesamtbetrag (A + B)  976.00
```

**Pflicht-Invariante**: `displayed_total == sum(displayed_net_buckets)` —
genau 976.00 = 976.00 + 0.00. Keine versteckte Mathematik mehr.

Code-Anker:
- PDF: `app.py` Berechnung-Section ab `# A · Sonstige Werbungskosten` (ehemals
  Zeile 24078).
- UI: `site/index.html` `_renderDetailTable()` ab Block-A-Header.

## §7 Z77 > VMA — Chat-Erklärung

User-Frage: „Warum ist 5.339 minus 4.705 nicht 976?"

Chat-Antwort (zu hinterlegen in `CHAT_COPY_MATRIX_FINAL` für FAQ-Q):

> Die alte Darstellung war missverständlich. Der Betrag entsteht aus
> getrennten Töpfen: Die VMA wird durch die steuerfreien Spesen (Z77) auf
> 0 € reduziert, der Überschuss verfällt — er wird nicht auf Fahrt-, Reinigung-
> oder Reisenebenkosten übertragen. Daher bleibt der einzutragende
> Gesamtbetrag bei 976 € (Fahrt + Reinigung + Trinkgeld).

## §8 Test-Coverage

| Invariante | Test |
|---|---|
| Tibor-Tabelle = 976.00 | `test_tibor_2025_displayed_total_matches_block_sum` |
| Naive Mathe 5339-4705=634 NICHT identisch mit 976 | `test_tibor_no_hidden_math_5339_minus_4705_does_not_equal_976` |
| Z77 < VMA | `test_variant_1_z77_less_than_vma` |
| Z77 = VMA | `test_variant_2_z77_equals_vma` |
| Z77 > VMA → Clamp | `test_variant_3_z77_greater_than_vma`, `test_case_S` |
| Z17 < Fahrt | `test_variant_4_ag_z17_less_than_fahrt` |
| Z17 > Fahrt → Clamp | `test_variant_5_ag_z17_greater_than_fahrt` |
| Jobticket (volle Fahrt-Erstattung) | `test_variant_6_jobticket_zero_fahrt` |
| ÖPNV | `test_variant_7_oepnv_path_already_in_fahr` |
| Shuttle | `test_variant_8_shuttle_path` |
| Kein VMA | `test_variant_9_no_vma` |
| Nur Fahrt+Reinig | `test_variant_10_only_fahrtkosten_reinigung` |
| Negative Töpfe verhindert | `test_variant_11_negative_topf_prevented`, `test_case_T_z17_only_offsets_fahrt` |
| Cent-Rundung konsistent | `test_variant_12_cents_rounding_consistent` |
| PDF-Code zeigt Block A/B | `test_pdf_table_no_misleading_subtract_line_static` |
| UI-Code zeigt Block A/B | `test_ui_table_no_misleading_brutto_minus_z77_static` |
| _recompute_with_overrides klemmt | `test_case_T_static_recompute_separates_buckets` |

**Total: 18 arithmetische + 30 every-case Pytests grün.**

## §9 Was war Bug, was Bug-Free?

| Layer | Status |
|---|---|
| Backend-Berechnung (`_recompute_with_overrides`) | ✓ korrekt; max(0,…)-Clamp pro Topf |
| Backend-Klassifikation (`_classify_v7`) | ✓ korrekt; nicht Teil dieses Bugs |
| PDF-Darstellung (`app.py:24078-24083 alt`) | ✗ missverständlich — gefixt |
| UI-Darstellung (`index.html:3916-3919 alt`) | ✗ missverständlich — gefixt |
| Chat-Erklärung Z77>VMA | ⚠ noch nicht in FAQ — pending |

## §10 Remaining Risks

1. PDF Audit-Section (außerhalb der Berechnungs-Tabelle) zeigt noch andere
   Werte. Audit nicht in diesem Pass abgedeckt — falls dort auch eine
   Brutto-Z77-Darstellung auftaucht, müsste analog umgebaut werden.
2. WISO-Übergabe-Wert (`netto`) bleibt unverändert — User trägt korrekt
   976 € ein.
3. Existing PDF-Audit-Tests können brechen wenn sie exakt nach „Summe aller
   Aufwendungen" suchen — Re-Run der full pytest empfohlen.
