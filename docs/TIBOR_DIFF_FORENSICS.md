# Tibor-Diff Forensics — e132976f vs Golden 2025

Phase A-Inventory. Stand 2026-05-15. Datenquelle: `result_data._tage_detail` (365 Einträge) + `_klass_summary` aus `/api/session/AT-C33E6274D260FC78`.

> ⚠️ Diagnose-Doc. Kein Fix-Plan, nur Beweise pro Diff.

---

## §1 Aggregat-Diff

| Wert | IST (e132976f) | Golden | Δ | Bewertung |
|---|---:|---:|---:|---|
| arbeitstage | **140** | 133 | **+7** | zu viel (→ §3) |
| reinigungstage | 140 | 133 | +7 | folgt aus arbeitstage |
| fahr_tage | **55** | 58 | **−3** | zu wenig (→ §6) |
| hotel_naechte | **78** | 66 | **+12** | zu viel (→ §4) |
| z72_tage | 5 | 5 | **+0** | ✓ exakt |
| z73_tage | **8** | 11 | **−3** | zu wenig (→ §5) |
| z74_tage | **0** | 1 | **−1** | komplett verloren (→ §5) |
| z76_eur | **4437.0** | 4794 | **−357** | zu wenig (→ §7) |
| z77_total | 4705.0 | 4705 | +0 | ✓ exakt |
| **gesamt** | **5621** | 6021 | **−400** | (siehe §7) |

---

## §2 Klass-Counter (alle 365 Tage)

| Klasse | n | Bemerkung |
|---|---:|---|
| Frei | 158 | OK |
| Z76 | 119 | siehe §4 (12 davon Hotel zu viel?) |
| Office | 35 | OK (Z72 candidates auch hier) |
| Standby | 18 | OK |
| ZeroDay | 14 | 8 dienstlich, 6 passiv (siehe §3.2) |
| **Issue** | **8** | **alle „Heimkehr aus Vortag-Tour" → §3.1** |
| Z73 | 8 | siehe §5 |
| Z72 | 5 | ✓ |

Σ = 365 ✓

**Counter-Diskrepanz**:
- `sum(t.classifier_result.counted_as_workday)` = **193**
- `_klass_summary.arbeitstage` = **140**
- Δ = 53. Zwei verschiedene Counter — siehe **BH-008**.

---

## §3 Arbeitstage +7 — Beweis

### §3.1 Issue-Tage (8 Heimkehr-Tage als „Issue" statt Z73/Z74/Z76)

| Datum | Marker | Routing | Reason |
|---|---|---|---|
| 2025-01-04 | `X` | BLR | Heimkehr aus Vortag-Tour |
| 2025-01-06 | `X` | FRA | Heimkehr aus Vortag-Tour |
| 2025-03-26 | `==` | (leer) | Heimkehr aus Vortag-Tour |
| 2025-04-02 | `==` | FRA | Heimkehr aus Vortag-Tour |
| 2025-05-23 | `103703` | LAD | Heimkehr aus Vortag-Tour |
| 2025-06-03 | `126533` | SOF | Heimkehr aus Vortag-Tour |
| 2025-10-28 | `32935` | TLV | Heimkehr aus Vortag-Tour |
| 2025-12-16 | `X` | JFK | Heimkehr aus Vortag-Tour |

**Beobachtung:** Alle 8 sind Tour-Heimkehr-Tage. Vortag war Auslands-Layover (z.B. LAD, SOF, TLV, JFK). Aktuell klassifiziert als `Issue`.

**Verdacht — Root-Cause:**
- Im Klassifikator `_deterministic_classify_v7` werden Heimkehr-Tage nicht dem Vortag-Tour-Cluster zugeordnet sondern als separater Issue-Tag behandelt.
- 7 davon (Auslands-Heimkehr) sollten **Z76 An/Ab** sein → würden Arbeitstage auf 140-7=133 reduzieren ✓ matched Golden.
- 1 davon (2025-04-02 `==` FRA) ist Inland-Routing → wäre Z73 An/Ab.

**Klass-Counter-Effekt nach Fix:**
- Z76: 119 + 7 = 126 (?? — Golden nicht direkt vergleichbar, da Golden anders aggregiert. Cross-check via z76_eur §7)
- Z73: 8 + 1 = 9 (Δ noch −2 zu Golden 11)
- Issue: 0 ✓
- arbeitstage: 140 − 7 = 133 ✓ matched Golden

### §3.2 ZeroDay-Workdays (8 dienstlich, 6 passiv)

Aus `counted_as_workday=True` und `klass=ZeroDay`:

| Datum | Marker | Routing | Reason |
|---|---|---|---|
| 2025-01-08 | `==` | FRA | Same-Day ohne duty-Info, kein VMA |
| 2025-01-09 | `==` | FRA | Same-Day ohne duty-Info |
| 2025-02-10 | `68617` | FRA | Same-Day < 8h (445 min) |
| 2025-04-03 | `==` | FRA | Same-Day ohne duty-Info |
| 2025-04-04 | `==` | FRA | Same-Day ohne duty-Info |
| 2025-04-30 | `99761` | FRA | Same-Day < 8h (370 min) |
| 2025-08-01 | `144349` | HAM | Same-Day < 8h (389 min) |
| 2025-09-21 | `15370` | FRA | Same-Day < 8h (350 min) |

**Beobachtung:** Same-Day-Touren < 8h zählen als ZeroDay-Arbeitstag aber ohne VMA. Das ist by-design korrekt (counted_as_workday=True, amount=0). Kein offensichtlicher Bug, aber Marker `==` 4× sollte vermutlich „Frei" sein (siehe BH-011 / §8).

---

## §4 Hotelnächte +12 — Beweis

`_klass_summary.hotel_naechte = 78`, Golden = 66.

### §4.1 Inland-Layover-Tage fälschlich als Z76+Hotel

Auszug aus den 78 Hotel-Tagen (alle Klass=Z76):

| Datum | Layover_ort | Routing | Reason | Inland/Ausland? |
|---|---|---|---|---|
| 2025-06-23 | **LIN** | WAW | Auslands-Layover MAD (Z76 Volltag) | **LIN=Milano, OK Ausland** |
| 2025-06-24 | **BER** | LIN | Auslands-Layover MAD (Z76 An/Ab) | **BER=Berlin Inland! → Z73** |
| 2025-09-26 | IST | KRK | Z76 (überstimmt SE-Inland MUC) | **IST=Istanbul Ausland OK** |
| 2025-09-27 | AGP | IST | Z76 (überstimmt SE-Inland DUS) | **AGP=Malaga Ausland OK** |
| 2025-11-01 | **LEJ** | FRA | Auslands-Layover STO (Z76 An/Ab) | **LEJ=Leipzig Inland! → Z73** |

**Verdacht:** Einige Tage haben Inland-Layover-Codes (BER, LEJ), werden aber als Z76 klassifiziert wegen Tour-Cluster-Foreign-Override (Cluster C2 / Phase-1-Fix). Das ist **zu aggressiv** für mixed-Inland/Ausland-Touren.

### §4.2 _hotel_candidate_issues

Aktuell nur 1 Eintrag:
```
{'datum': '2025-05-22', 'klass': 'Z76', 'reason': 'overnight=true aber layover_ort fehlt — Hotel nicht eindeutig'}
```
→ Phase-4-Inferenz (LAD aus routing[-1]) löste das, aber Hotel-Counter zählt trotzdem. OK.

### §4.3 Hypothese — Δ+12 = +8 von §3.1 Issue→Z76-Heimkehr + 4 falsche Inland-Z76

Wenn 8 Issue-Tage zu Z76 An/Ab werden (mit overnight=true), zählen sie als +8 Hotelnächte. Dann 78 − 8 = 70. Plus die 4-5 Inland-Layover-Tage die nicht Hotel sein sollten → 70 − 4 = 66 ✓ Golden.

→ **BH-003 + BH-004 zusammen erklären die +12.**

---

## §5 z73 −3 / z74 −1 — Beweis

### §5.1 _missing_z73_candidates (2 Tage)

```
{'datum': '2025-09-26', 'klass': 'Z76', 'layover_ort': 'MUC', 'reason': 'Inland-Layover MUC (≠ Homebase) ohne Z73/Z74'}
{'datum': '2025-09-27', 'klass': 'Z76', 'layover_ort': 'DUS', 'reason': 'Inland-Layover DUS (≠ Homebase) ohne Z73/Z74'}
```

**Beobachtung:** Klassifikator hat erkannt dass SE-Stempel MUC/DUS sind (Inland), aber CAS-Override (Cluster C2, IST/AGP-Foreign) hat den Tag zu Z76 hochgestuft. Audit-Liste registriert das als „missing Z73/Z74" — aber im Counter wird das als Z76 gezählt. → **2 Tage falsch in Z73-Bucket.**

### §5.2 Verlorene Z73/Z74 durch Issue (§3.1)

1 Issue-Tag mit Inland-Routing (2025-04-02 FRA): wäre Z73 An/Ab. → +1 Z73 nach Fix.

### §5.3 Z74 (Inland-Volltag 28€) komplett 0

Golden hat 1 Z74-Tag. Suchen müssen wir den Tibor-Tag — vermutlich ein Inland-Tour-Mitteltag (z.B. zwischen 2 Inland-Layovern). Aktuell vermutlich als Z73 oder Z76 fehlklassifiziert.

**Hypothese:** Nach BH-003/BH-004-Fix:
- z73: 8 + 1 (Issue→Z73) + 2 (Inland-Layover) = 11 ✓ Golden
- z74: 0 + 1 = 1 ✓ Golden (durch saubere Inland-Tour-Logik)

---

## §6 Fahrtage −3 — Beweis

`fahr_tage = 55`, Golden = 58.

Aus `counted_as_fahrtag`: n=129 (aber `_klass_summary.fahr_tage`=55 — wieder Counter-Diskrepanz wie BH-008).

**Wahrscheinliche Ursache:** Fahrtage = Tour-Starts. Wenn 8 Heimkehr-Tage als Issue (statt Tour-Ende im Cluster) klassifiziert sind, fehlen ggf. die zugehörigen Tour-Start-Markierungen für 3 Touren. Diagnose ausstehend.

---

## §7 z76 −357 € — Beweis

`z76_eur = 4437`, Golden = 4794.

**Δ-Quellen (Hypothese):**

1. **+8 An/Ab-Tage durch Issue→Z76-Fix (§3.1)**: Wenn 7 Auslands-Heimkehr → Z76 An/Ab. Bei ~30€ avg → ~+210€. Plus möglicherweise overnight=true → +Volltag-Sätze.

2. **MUC/DUS Inland-Override-Anomalie (§5.1)**: Aktuell werden 09-26 (IST/MUC) und 09-27 (AGP/DUS) als Z76 mit Türkei/Spanien-Tagessätzen (24€+23€=47€) gezählt. Wären sie Z73 (14€ jeweils) → 47€ vs 28€ = +19€ z76 (zu viel).

3. **BMF-Tagesätze-Differenzen** (F5 / #228, Task-Backlog): einzelne Land-Sätze pro Tag-Typ könnten leicht abweichen.

**Verifikation:** Lass §3+§5 fixen, dann Restdiff messen.

---

## §8 Marker-Reader Bugs

### §8.1 „==" als unknown_marker 6×

```
2025-01-28, 01-29, 01-31, 02-03, 02-06, 02-07 — alle marker='==', activity_type='unknown'
```

Plus weitere `==` als ZeroDay-Heimkehr (01-08, 01-09, 04-03, 04-04, 03-26, 04-02).

**Reader-Bug:** `==` ist Standard-Frei-Marker im Lufthansa-CAS. Reader sollte ihn als `frei` aktivieren statt als unknown. → **BH-011**.

### §8.2 „X" als Heimkehr-Marker

`X` erscheint 5× als Issue (01-04, 01-06, 04-09 — als Z76, nicht Issue, OK, 04-10, 10-06, 10-07, 10-27, 12-16). Aber 3 davon als Issue-Heimkehr. → **Marker-Lexikon-Pflege** parallel zu BH-003.

---

## §9 Review-Items aktuell

```
1. type=office_training_time_missing  datum=2025-12-19  marker=OF  status=pending
   q="Am 2025-12-19 war ein Office-/Schulungstag (OF) eingetragen — wir konnten keine Uhrzeit erkennen.
      Warst du inklusive Hin-/Rückweg länger als 8h weg?"
   → BH-001 Symptom-Frage statt Marker-Semantik

2. type=unknown_marker  datum=2025-01-28  marker===  status=pending
   affected_days=['2025-01-28', '2025-01-29', '2025-01-31', '2025-02-03', '2025-02-06', '2025-02-07']
   q="In deinem Crew-Dienstplan steht 6× die unbekannte Kennung „=="..."
   → BH-011 Reader sollte `==` als frei erkennen, Review unnötig
```

---

## §10 Rescues Audit

12 Rescues geloggt (Auszug, alle korrekt):

| Datum | Type | Effect |
|---|---|---|
| 2025-01-05 | layover_place_inferred | routing[-1]=BLR |
| 2025-05-17 | frei_to_z76_active_foreign_se | Frei→Z76 USA-SEA |
| 2025-05-22 | layover_place_inferred | routing[-1]=LAD |
| 2025-06-17 | frei_to_z76_active_foreign_se | Frei→Z76 Kroatien-ZAG |
| 2025-06-18 | frei_to_z76_active_foreign_se | Kroatien-ZAG |
| 2025-08-22 | frei_to_z76_active_foreign_se | Zypern-LCA |
| 2025-09-26 | cas_foreign_layover_over_se_inland_stamp | SE-MUC/CAS-IST → Z76 |
| 2025-09-27 | cas_foreign_layover_over_se_inland_stamp | SE-DUS/CAS-AGP → Z76 |
| 2025-10-15 | frei_to_z76_active_foreign_se | Frankreich-MRS |
| 2025-10-16 | frei_to_z76_active_foreign_se | Spanien-AGP |
| 2025-10-25 | frei_to_z76_active_foreign_se | London |
| 2025-11-18 | frei_to_z76_active_foreign_se | Norwegen-SVG |

**Beobachtung:** Phase-1 SE-Override wirkt korrekt für 9 Tage. Phase-4 layover_place_inferred für 2 Tage. Cluster-C2-Override für 2 Tage — letztere sind aber laut §5.1 streitbar (MUC/DUS waren laut SE Inland).

---

## §11 Fix-Priorität nach Forensik

| Priorität | Bug | Erwarteter Gewinn |
|---|---|---|
| #1 | **BH-003** Issue→Z76/Z73/Z74-Heimkehr-Cluster | arbeitstage 140→133 ✓, hotel 78→70, z73/z74 +1 |
| #2 | **BH-004** Inland-Layover-Z76-Override stricter | hotel 70→66 ✓, z73 8→10 |
| #3 | **§5.1** MUC/DUS-Override review (Tibor-spezifisch) | z73 10→11 oder 12, je nach Reality |
| #4 | **BH-011** `==` als frei erkennen | Klass: ZeroDay/Issue → Frei für 4-6 Tage |
| #5 | **BH-001** Review-Question Quality | UX |
| #6 | F5 / #228 BMF-Tagesätze | Restdiff z76 < ±50€ |
