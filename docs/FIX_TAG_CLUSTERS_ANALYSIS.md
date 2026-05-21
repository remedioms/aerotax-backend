# Tag-Cluster-Analyse — Tibor-Run 3fd8cfe1

**Datum:** 2026-05-14
**Quelle:** `_tage_detail` + `_missing_z76_candidates` + `_hotel_candidate_issues` + `_vma_unmapped_se` + `_iata_unknown` aus Live-Job 3fd8cfe1.
**Status:** Reine Analyse. **Kein Code geändert. Kein Deploy.**

---

## Tabelle — alle 14 betroffenen Tage

| Datum | aktuell | Soll | Quelle (CAS/SE) | Root-Cause | Fix-Regel | Testname | €-Effekt |
|---|---|---|---|---|---|---|---|
| 2025-05-17 | Frei (0€) | Z76 | CAS:`==` FRA; SE:stfrei SEA (USA) | SE-Override nur für `at='frei'` mit foreign-SE — bereits in Rev 00052-h9x gefixt | (deployed) | `test_se_foreign_overrides_frei_to_z76` | +**40 €** (USA an_abreise) |
| 2025-10-07 | Frei (0€) | Z76 | CAS:`X` ICN (overnight=true); SE:stfrei SEL | gleich wie 05-17 | (deployed) | s.o. | +**32 €** |
| 2025-10-15 | Frei (0€) | Z76 | CAS:`==` FRA; SE:stfrei MRS | gleich | (deployed) | s.o. | +**36 €** |
| 2025-10-16 | Frei (0€) | Z76 | CAS:`==` FRA; SE:stfrei AGP | gleich | (deployed) | s.o. | +**23 €** |
| 2025-10-25 | Frei (0€) | Z76 | CAS:`==` FRA; SE:stfrei LON | gleich | (deployed) | s.o. | +**44 €** |
| 2025-11-18 | Frei (0€) | Z76 | CAS:`==` FRA; SE:stfrei SVG | gleich | (deployed) | s.o. | +**50 €** |
| **Cluster 1 (deployed)** | | | | | | | **Σ ~225 €** |
| 2025-09-26 | **Z74 14€ MUC (FALSCH)** | Z76 IST | CAS: layover='IST' (Türkei), routing=KRK | Klassifikator-Bug: layover_ort='IST' wird ignoriert, Tour-Reason übernimmt anderen Tag („MUC") — vermutlich Cluster-Tour-Tag-Reason-Sharing | **Cluster 1.2:** Wenn layover_ort gesetzt UND `_is_inland_code(layover)==False` → Z76 statt Z74, egal welche Tour-Reason | `test_se_foreign_overrides_z74_to_z76` | +**66 € − 14 €** = **+52 €** (Türkei voll_24h) |
| 2025-09-27 | **Z73 14€ DUS (FALSCH)** | Z76 AGP | CAS: layover='AGP' (Spanien), routing=IST | wie 09-26 | gleich | `test_se_foreign_overrides_z73_to_z76` | +**23 € − 14 €** = **+9 €** (Spanien an_abreise) |
| **Cluster 1.2** | | | | | | | **Σ ~+61 €** |
| 2025-05-22 | Z76 28€ „Angola fallback" (layover leer) | Z76 LAD (Angola voll_24h) | CAS: routing=`['LAD']`, overnight=true, layover_ort='' (leer!) | layover_ort wurde nicht aus routing-Endort abgeleitet → fallback `Tour-Übernachtung ohne Ort → Cluster=Ausland → 28€ pauschal` | **Cluster 3:** wenn overnight=true UND layover_ort leer UND routing[-1] vorhanden → layover_ort = routing[-1] | `test_hotel_layover_ort_inferred_from_routing_endpoint` | +**14 €** (Angola voll_24h=52€ statt 28€ Pauschal) |
| 2025-12-15 | Z76 28€ „Irland fallback" (layover leer) | Z76 JFK?? (USA) — bmf=Irland ist falsch | CAS: routing=`['JFK']`, overnight=true, layover_ort='' | wie 05-22 + BMF-Fallback-Mapping = Irland??? (JFK ist New York, NICHT Irland) | wie 05-22 + Bug im BMF-Fallback prüfen | `test_hotel_layover_jfk_maps_to_usa_not_ireland` | +**11 € − 28 €** (USA an_abreise=40€) — eigentlich +12 € |
| **Cluster 3** | | | | | | | **Σ ~+26 €** |
| 2025-04-23 | Standby (0€) | **Standby (0€) — KORREKT** | CAS: RES FRA, duty=960min; SE: stfrei FRA 14€ | SE-Zeile = AG-Erstattung (AG zahlt 14€ steuerfrei für >8h Standby Anwesenheit). Per BMF: AG-Erstattung wird auf Z72 angerechnet → **kein zusätzlicher VMA-Werbungskosten-Anspruch** | **Cluster 4 = false-positive Audit-Lärm.** SE-Zeile dokumentiert AG-Pauschale, nicht zusätzlichen Anspruch. **Audit-Note** statt unresolved | `test_inland_stfrei_reimbursement_not_double_counted` | 0 € |
| 2025-08-01 | ZeroDay (0€) | ZeroDay (0€) | CAS: Same-Day 389min total, routing=FRA→NUE→FRA; SE: stfrei NUE 14€ | <8h → kein Z72-Anspruch per BMF. SE-Zeile = AG-Erstattung (14€ Pauschale für Inland-Tagestrip <8h). | wie 04-23 | `test_same_day_under_8h_no_z72_even_if_se_stfrei` | 0 € |
| 2025-10-20 | Standby (0€) | Standby (0€) | RES_SB HAM, duty=960min; SE: stfrei HAM 14€ | wie 04-23 | wie 04-23 | gleicher Test | 0 € |
| 2025-10-23 | Standby (0€) | Standby (0€) | RES LEJ, duty=960min; SE: stfrei LEJ 14€ | wie 04-23 | wie 04-23 | gleicher Test | 0 € |
| **Cluster 4** | | | | | | | **0 € (false-positive audit-noise — nur log-Bereinigung)** |

---

## Cluster 2 — IATA City-Codes CHI/ROM/STO

**Wo werden sie genutzt?** Nicht in tage_detail.routing/layover (kein Match). Vermutlich in SE-Reader-Output (SE-Zeile mit stfrei_ort='CHI' o.ä.) ODER in CAS-Marker-Field der nicht direkt im `reader_facts.routing` landet.

**`bmf_data.IATA_TO_BMF`:**
- `CHI` → **None** ❌
- `ROM` → **None** ❌
- `STO` → **None** ❌
- aber:
- `ORD` → „USA – Chicago" ✓
- `FCO` → „Italien – Rom" ✓
- `ARN` → „Schweden" ✓
- `BMA` → „Schweden" ✓

**Root-Cause:** IATA-Standard kennt **Metro Area Codes** (CHI, ROM, STO, LON, NYC, MOW, …) als „Mehrere Flughäfen einer Stadt"-Code. AeroTAX-Map enthält nur einzelne Airport-Codes, keine Metro-Codes.

**Fix-Regel:**
1. Erweitere `bmf_data.IATA_TO_BMF` um Metro-Code-Aliases:
   ```python
   'CHI': 'Vereinigte Staaten von Amerika (USA) – Chicago',
   'ROM': 'Italien – Rom',
   'STO': 'Schweden',
   'NYC': 'Vereinigte Staaten von Amerika (USA) – New York',
   'LON': 'Vereinigtes Königreich – London',
   'MOW': 'Russland – Moskau',
   'TYO': 'Japan - Tokyo',
   'WAS': 'Vereinigte Staaten von Amerika (USA) – Washington, D.C.',
   ```
2. Wenn Alias getroffen: in `_rescues`-Liste einen Eintrag schreiben (`rescue_type='metro_code_alias'`)
3. Wenn unknown bleibt: NICHT silent auf 28€-Fallback, sondern in `_iata_unknown` lassen

**Tests:**
- `test_bmf_city_code_chi_maps_to_usa_chicago`
- `test_bmf_city_code_rom_maps_to_italy_rome`
- `test_bmf_city_code_sto_maps_to_sweden`
- `test_bmf_city_code_lon_maps_to_uk_london`
- `test_unknown_city_code_remains_unresolved_not_zero`
- `test_metro_code_logs_rescue_entry`

**€-Effekt unklar** ohne Tag-für-Tag-Bestätigung (kommt CHI/ROM/STO 1× oder 10× pro Jahr im Tibor-Run vor?). Schätzung: 3 Codes × 2-5 Tage × ~30-40€/Tag = **+90-200€** wenn relevant.

---

## Cluster 1.2 — IST/AGP-Bug detailliert

**Tag 2025-09-26** (so wie er im backend abgespeichert ist):
```
klass = Z74 (Inland-Volltag 28€)
reason = "Inland-Mittel-Tag MUC (Z74 24h)"
reader_facts.routing = ['KRK']        # Krakau
reader_facts.layover_ort = 'IST'      # Istanbul (= Türkei = AUSLAND!)
duty_duration = 355min
overnight = true
```

**Bug-Doppel:**
1. **Reason-Text falsch:** „MUC" steht im Reason, hat aber NICHTS mit diesem Tag zu tun. Vermutlich übernommen aus Tour-Aggregation eines anderen Tages (Tour-Cluster-Reason-Sharing-Bug)
2. **Klassifikation falsch:** layover_ort='IST' ist offensichtlich Ausland (Türkei). Backend hat trotzdem Z74 (Inland-Volltag) klassifiziert — Klassifikator hat layover_ort nicht gegen IATA_TO_BMF / Inland-Liste geprüft.

**Tag 2025-09-27**:
```
klass = Z73 (Inland An-/Ab 14€)
reason = "Inland-Layover DUS (Z73 An/Ab)"
reader_facts.routing = ['IST']
reader_facts.layover_ort = 'AGP'      # Málaga = Spanien = AUSLAND
```

Gleicher Bug: layover='AGP' (Spanien), aber klass=Z73 (Inland) mit Reason 'DUS'.

**Fix-Stelle suchen:** Code-Pfad wo Z74/Z73 für Tour-Mid-Days vergeben wird. Vermutlich in `_deterministic_classify_v7` rund um Tour-Cluster-Handling (Z.14000+). Muss noch genauer lokalisiert werden.

**Fix-Regel:**
- **Bevor** klass=Z73/Z74 wegen „Inland-Cluster" gesetzt wird: prüfe `_is_inland_code(layover_ort)`. Wenn `False` → klass=Z76, BMF-Land aus layover_ort.
- Plus: Reason-Text muss layover-spezifisch sein, nicht aggregiert aus Tour-Mittelpunkt.

---

## €-Effekt zusammenfasst

| Cluster | Status | Erwarteter Δ |
|---|---|---|
| 1 (Frei→Z76 SE-Override) | deployed Rev 00052-h9x | **+225 €** |
| 1.2 (Z73/Z74→Z76 für IST/AGP) | **noch nicht gefixt** | **+61 €** |
| 2 (Metro-Code-Aliases CHI/ROM/STO) | **noch nicht gefixt** | **+90-200 €** geschätzt |
| 3 (Hotel-layover_ort-Fallback) | **noch nicht gefixt** | **+26 €** |
| 4 (Standby/Inland 14€-Audit-Noise) | **kein €-Effekt — nur Audit-Aufräumen** | 0 € |
| **Total Soll-Diff zu Tibor-Golden** | | **+402–512 €** |

Vorher: Diff zu Golden = **-791.68 €**. Nach allen Fixes: Restdiff = **~280-390 €** — die liegen wahrscheinlich in #228 (F5 VMA-Tagesatz-Logic, separate Track).

---

## Vorgeschlagene Reihenfolge (nach Freigabe)

1. **Cluster 1.2** zuerst (Z73/Z74→Z76 Override): kleinster Diff, klare Logik, gut testbar
2. **Cluster 2** (Metro-Codes): Daten-Änderung in `bmf_data.py`, isoliert, klein
3. **Cluster 3** (Hotel-layover-Fallback): wirkt nur auf 2 Tage, vorsichtig wegen Hotel-Count-Trend (66 → nicht über 70)
4. **Cluster 4** (Audit-Noise): low-prio, kein €-Effekt, nur Log-Cleanup

Pro Cluster vor Code-Diff: konkrete Code-Stelle + Test + Diff → deine Freigabe.

---

## Offen für deine Entscheidung

1. **Reihenfolge OK** (1.2 → 2 → 3 → 4)?
2. **Cluster 4 wirklich als false-positive abhaken** oder gegen Tibor-Golden nochmal prüfen ob die 4 Tage dort als Z73 zählen?
3. **Wenn ja zu Cluster 1.2:** soll ich erst die Tour-Cluster-Reason-Sharing-Logik in app.py orten + Code-Diff vorschlagen (vor Freigabe)?

Nichts geändert. Warte auf Antwort.
