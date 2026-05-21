# Highest-Defensible Optimization Audit

Stand: 2026-05-21.
Produkt-Regel: maximaler vertretbarer, belegbarer Werbungskostenansatz.
Jede optimierende Annahme oder User-Angabe muss source-getaggt sein.

## §1 Source-Type-Schema

Jeder Wert in `result_data` bekommt einen `source_type`:

| `source_type` | Bedeutung | Beispiel |
|---|---|---|
| `document` | Direkt aus LSB/SE/CAS-PDF gelesen | Z77 aus SE-Summenzeile |
| `user` | User-Angabe aus Formular oder Review-Chat | km-Entfernung, anfahrt_min |
| `bmf` | Pauschalsatz aus BMF-Tabelle | Z76-Pauschalen, 0,30€/km |
| `calculated` | Pauschal-Erfahrungswert / Berechnungs-Default | Reinigung 1,60€/Arbeitstag, Trinkgeld 3,60€/Hotelnacht |
| `mixed` | Mehrere Quellen kombiniert | Fahrtkosten = User-km × CAS-Fahrtage × BMF-Satz |

UI/PDF Display-Konvention:
- `document` → unverändert
- `user` → mit `*` markiert, Legende „* Nutzerangabe"
- `bmf` → mit Hinweis „BMF 2025"
- `calculated` → mit Hinweis „Pauschal-Ansatz" oder „Erfahrungswert"
- `mixed` → primärer Source-Type des dominanten Inputs + Audit-Hinweis im PDF-Audit-Block

## §2 Stellschrauben-Inventory (Was kann den Betrag erhöhen?)

### Block A — Sonstige Werbungskosten

| Position | Quelle | Source-Type | Optimierbar? | Risiko | Implementiert? |
|---|---|---|---|---|:-:|
| Fahrtkosten Homebase | km (User), fahr_tage (CAS), 0.30/0.38€ (BMF) | mixed | km muss realistisch sein; weitere km verlangen Beleg | Falschangabe = Steuerhinterziehung-Risiko | ✓ |
| AG-Fahrkostenzuschuss (Z17) | LSB Zeile 17 | document | Nicht optimierbar — exakter LSB-Wert | n/a | ✓ |
| ÖPNV / Jobticket | User-Formular / LSB | user | User-input, optional | falsche Doppel-Erfassung mit Entfernungspauschale | ⚠ teilweise |
| Shuttle-Kosten | User-Formular | user | User-input mit Beleg | nur wenn nicht durch Pauschale gedeckt | ⚠ teilweise |
| Parkkosten | nicht implementiert | user | optional, mit Beleg | nur wenn nicht durch Pauschale | ✗ NOT_IMPLEMENTED |
| Reinigungskosten | reinigungstage (CAS) × 1,60€ (Erfahrungswert) | calculated | nicht ohne Belege erhöhbar; Pauschale ist Konvention | Pauschale ist Crew-typisch, Steuerberater akzeptieren meist | ✓ |
| Trinkgelder / Reisenebenkosten | hotel_naechte (CAS) × 3,60€ (Erfahrungswert) | calculated | nicht ohne Belege erhöhbar | Crew-Konvention, akzeptabel | ✓ |
| Optionale Werbungskosten-Belege (Telefon, Gewerkschaft, Fachliteratur etc.) | User-Upload (Belege) | user | belegt → 1:1; bei Telefon 20% pauschal | Beleg-Echtheit | ✓ |

### Block B — VMA (Verpflegungsmehraufwand)

| Position | Quelle | Source-Type | Optimierbar? | Risiko | Implementiert? |
|---|---|---|---|---|:-:|
| Z72 Inland >8h | CAS-Zeiten + commute_minutes | mixed | bei nahe-8h-Tagen via User-Bestätigung | „>8h" muss plausibel sein | ✓ (mit Reviewen) |
| Z73 Inland An/Ab | CAS-Tour-Cluster | mixed | nicht direkt optimierbar | n/a | ✓ |
| Z74 Inland 24h | CAS overnight in DE | document | n/a | n/a | ✓ |
| Z76 Ausland An/Ab | CAS layover_ort + SE foreign-stfrei + BMF Land | mixed | Highest-defensible via P0-Fix #2 | ohne AG-Beleg konservativ Inland | ✓ (post P0 Fix #2) |
| Z76 Ausland Volltag | wie oben + Übernachtungs-Signal | mixed | Highest-defensible via P0-Fix #1/#3 | ohne SE-Evidence nur an_abreise | ✓ (post P0 Fix #1+#3) |
| Steuerfreie Spesen Z77 | SE-Summenzeile pro Monat | document | nicht optimierbar — AG-Beleg | n/a | ✓ |

### Section: AG-Erstattungen (mindern Werbungskosten)

| Position | Quelle | Source-Type | Verrechnung | Risiko |
|---|---|---|---|---|
| Z17 (Fahrkostenzuschuss) | LSB Zeile 17 | document | nur gegen Fahrtkosten (`max(0, fahr − ag_z17)`) | ✓ implementiert |
| Z77 (steuerfreie Spesen) | SE-Summe | document | nur gegen VMA-Topf (`max(0, vma − z77)`) | ✓ implementiert |
| Jobticket Steuerwert | LSB | document | gegen ÖPNV-Bucket, nicht doppelt | ⚠ teilweise |
| Sonstige steuerfreie LH-Zuschüsse | LSB Z18-21 | document | bucket-spezifisch | NEEDS_REVIEW |

## §3 Was schon implementiert ist

### Bereits live (Cloud Run rev 00066-pkk)
- Bucket-Math: Block A + Block B, max(0, ...) Clamps
- P0 Fix #1: Mid-Tour same_day rescue → voll_24h
- P0 Fix #2: Evening-Anreise + foreign-SE → Z76 An/Ab
- P0 Fix #3: Mid-Tour by SE-Evidence (prev+today+next foreign-SE) → voll_24h
- Z77 reduziert nur VMA-Topf, Z17 nur Fahrt-Topf
- Foreign-Anreise / Mid-Tour / Abreise korrekt klassifiziert bei AG-Beleg

### Was noch fehlt — P1 (nicht launch-blocking aber für Public-Launch nötig)

| Stellschraube | Was fehlt | Priorität |
|---|---|---|
| Source-Labels in result_data | `source_breakdown` Block pro Bucket | P1 — diese Iteration |
| UI-Legende für `*` | Sternchen + Tooltip im Detail-Table | P1 |
| PDF-Legende | „* Nutzerangabe"-Footnote unter der Berechnungs-Section | P1 |
| Lost-tour-days (BLR Issue, Schweden Frei) | Review-Item statt stille Issue | P1 (separater Sprint) |
| Near-8h Days Review | Review-Item wenn 7-9h, User-Bestätigung mit `*` | P1 |
| Jobticket Doppel-Counting Check | wenn LSB-Jobticket > 0 UND user.km > 0 → Hinweis | P1 |
| Parkkosten | Neuer Beleg-Typ in optionale_belege | P2 |
| Standby-Konflikt Review-Items | aktuell stille Issue | P1 |

## §4 Optimierungs-Hebel — was kann theoretisch zusätzlich kommen?

Konkret für Tibor-typisches Profil (Crew, FRA, Z77 hoch):

| Hebel | Mechanik | Erwarteter Δ Brutto | Voraussetzung | Risiko |
|---|---|---:|---|---|
| **Near-8h Days → User-Review** | Days mit total_minutes 480±60 → User bestätigt → Z72 statt Office | bis +100 € | User-Bestätigung mit `*` | none, source-tagged |
| **Lost-tour-days Review** | Issue/Frei mit benachbarter Tour-Evidence → Review-Item → Z76 wenn User bestätigt | bis +160 € | User-Antwort | sauber wenn `*` markiert |
| **Standby Review** | RES/SBY-Tage mit foreign-Vortag → "Bereitschaft am Flughafen?" → ggf. Z73/Z76 | bis +60 € | User-Antwort | per-Day Source-Tag |
| **Anfahrt-Minuten** (`anfahrt_min` form-Feld) | Wenn User Fahrzeit angibt, wird sie in Z72-8h-Berechnung berücksichtigt | bis +50 € | User-input mit `*` | OK |
| **3-Monats-Frist** (Auswärtstätigkeit am selben Ort) | Wenn Crew >3 Mon am selben Ort → keine VMA mehr ab Mon 4 | typischerweise 0 € für Crew (Touren wechseln) | n/a | nicht launch-blocking |
| **Doppelter Haushalt** (Familien-Wohnsitz vs Tätigkeitsstätte) | komplexer Fall | individual | User-input | NICHT_IMPLEMENTIERT, deferred |

## §5 Was darf NICHT optimiert werden

Diese Hebel sind klar nicht-defensibel und werden NICHT angesetzt:

| Hebel | Warum nicht |
|---|---|
| Z76-Tage ohne CAS/SE-Beleg | Keine Auswärtstätigkeit nachweisbar |
| Mehr Fahrtage als CAS zeigt | Marker-only ist BMF-feindlich |
| km > realistische Pendel-Distanz | Steuerhinterziehung-Risiko |
| Fahrtzeit ohne User-Bestätigung | „Frei erfundene" Fahrtzeit ist nicht-defensibel |
| Hotelnächte > CAS-Layover-Anzahl | Pauschal-Annahme ohne Beleg |
| KI-extrapolierte Steuer-Beträge | strict Reader/Calculator separation per CLAUDE.md |

## §6 UI/PDF Source-Labeling Plan

### Minimum-Viable Source-Labels (this iteration)

```json
result_data['source_breakdown'] = {
    'block_a': {
        'fahr':   {'type': 'mixed', 'label': 'CAS + Nutzerangabe (km) + BMF',
                    'user_inputs': ['km'], 'star_required': true},
        'reinig': {'type': 'calculated', 'label': 'Pauschal-Ansatz pro Arbeitstag (Crew-Erfahrungswert)',
                    'user_inputs': [], 'star_required': false},
        'trink':  {'type': 'calculated', 'label': 'Pauschal-Ansatz pro Hotelnacht (Crew-Erfahrungswert)',
                    'user_inputs': [], 'star_required': false},
        'opt_zu': {'type': 'user', 'label': 'Belege (Telefon, Gewerkschaft, etc.)',
                    'user_inputs': ['optionale_belege'], 'star_required': true},
    },
    'block_b': {
        'vma_72': {'type': 'mixed', 'label': 'CAS-Zeiten + commute_min + BMF', 'star_required': false},
        'vma_73': {'type': 'mixed', 'label': 'CAS-Tour-Cluster + BMF', 'star_required': false},
        'vma_74': {'type': 'document', 'label': 'CAS overnight Inland + BMF', 'star_required': false},
        'vma_76': {'type': 'mixed', 'label': 'CAS layover + SE foreign-stfrei + BMF', 'star_required': false},
    },
    'erstattung': {
        'ag_z17': {'type': 'document', 'label': 'LSB Zeile 17', 'star_required': false},
        'z77':    {'type': 'document', 'label': 'SE Summenzeilen pro Monat', 'star_required': false},
    },
    'review_user_inputs': [
        # Filled per review-answer
        # {'datum': '2025-04-21', 'review_kind': 'unknown_marker',
        #  'user_answer': 'RES at Flughafen', 'type': 'user', 'star': '*'}
    ],
}
```

### UI/PDF Legende (Text-Block am unteren Tabellen-Rand)

```
* Nutzerangabe — bitte Plausibilität selbst prüfen.
Quellen: CAS = Dienstplan, SE = Streckeneinsatz, LSB = Lohnsteuerbescheinigung,
BMF = §9 Abs. 4a EStG Pauschalen 2025, Pauschal-Ansatz = Crew-Erfahrungswert.
```

## §7 Implementierungs-Roadmap

### Diese Iteration (P0-Foundation)
1. **`source_breakdown` Struktur** in `result_data` einbauen → ja, diese Iteration
2. **PDF-Legende** — kurzer Text-Block unter Berechnungs-Tabelle → ja
3. **UI-Legende** — `*`-Hinweis unter Detail-Tabelle → ja
4. **Tests** → ja, source-label assertions

### Nächste Iteration (P1 — vor Public-Launch)
1. Near-8h Review-Items
2. Lost-tour-days Review-Items
3. Standby Review-Items
4. Jobticket-Doppel-Counting-Detection

### Später (P2)
1. Parkkosten als optionaler Beleg-Typ
2. Doppelter Haushalt
3. 3-Monats-Frist-Audit

## §8 Tests-Plan

### Source-Label-Tests (foundational, diese Iteration)

```
test_result_data_has_source_breakdown_block
test_block_a_fahr_marked_as_mixed_with_user_km
test_block_a_reinig_marked_as_calculated
test_block_a_trink_marked_as_calculated
test_block_b_vma_72_marked_as_mixed
test_block_b_vma_76_marked_as_mixed_with_se_evidence
test_erstattung_z17_marked_as_document
test_erstattung_z77_marked_as_document
test_user_km_change_marks_fahr_as_star_required
test_pdf_contains_source_legend_text
test_ui_contains_source_legend_text
test_no_unlabeled_user_influenced_amount
```

### Behavioral guards (P1 — to follow)

- Near-8h day creates review-item
- Lost-tour-day creates review-item
- Standby ambiguity creates review-item

## §9 Final Output Summary

### 1. Welche Schrauben erhöhen den Betrag legal?
- Fahrtkosten (km × Fahrtage × BMF 0.30/0.38)
- VMA Z76 voll_24h für Mid-Tour-Tage (per P0 Fix #1+#3)
- Z76 An/Ab für Auslands-Anreise mit foreign-SE (per P0 Fix #2)
- Reinigung 1.60€/Arbeitstag (calculated)
- Trinkgeld 3.60€/Hotelnacht (calculated)
- Optionale Belege (User-Upload)
- Near-8h Days → User-Bestätigung (P1)
- Lost-tour-days Review (P1)

### 2. Welche sind schon implementiert?
- Alle Z72/Z73/Z74/Z76-Berechnungen (mit P0 Fixes #1+#2+#3)
- Block A Fahrt+Reinig+Trink
- Z17/Z77 Topf-Verrechnung mit Clamp
- Optionale Belege
- 3-Doc-Pipeline (LSB+SE+CAS)

### 3. Welche brauchen User-Angabe?
- km-Entfernung (form)
- anfahrt_min (form, optional)
- Fahrzeug, oepnv_kosten, jobticket, shuttle (form, optional)
- Optionale Belege (upload, optional)
- Review-Antworten (chat, post-eval)
- Near-8h-Bestätigung (P1)
- Standby-Kontext (P1)

### 4. Welche dürfen nicht genutzt werden?
- Z76 ohne CAS+SE-Beleg
- Mehr Fahrtage als CAS zeigt
- km > realistische Distanz
- Fahrtzeit ohne User-Bestätigung
- Hotelnächte > Layover-Anzahl
- KI-extrapolierte Steuer-Beträge

### 5. Welche UI/PDF Source-Labels werden ergänzt?
- `result_data.source_breakdown` per Bucket
- PDF-Legende: „* Nutzerangabe" unter Berechnungs-Tabelle
- UI-Legende: gleicher Hinweis unter Detail-Tabelle
- Audit-Block in PDF mit „Quelle: …"-Spalten per Bucket

### 6. Tests + Ergebnis
- 12 neue source-label-Tests (foundational)
- Full regression unchanged

### 7. Ob noch zusätzliche Beträge möglich sind
- **Ja**, in P1 via Review-Items:
  - Near-8h Days → bis +100€
  - Lost-tour-days Review → bis +160€
  - Standby Review → bis +60€
- Erwarteter zusätzlicher Δ pro Crew-User: 50-300€ je nach Profil
- Erwarteter Steuer-Effekt @42% Grenzsteuer: 20-125€

### Hard-Stops eingehalten

Kein Deploy. Kein Live-Run. Kein Production-Switch.
Kein Tibor-/FollowMe-Hardcoding.
