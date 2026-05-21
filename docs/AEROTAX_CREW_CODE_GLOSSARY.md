# AeroTAX Crew-Code Glossary

**Stand: 2026-05-20.** **Quelle: nur Repo-Belege** aus
[AEROTAX_KNOWLEDGE_HARVEST.md](AEROTAX_KNOWLEDGE_HARVEST.md). Nichts erfunden.

**Schema pro Eintrag:**
```
code:               Der Marker / Code
field_type:         crew_position | standby_marker | layover_free_marker
                    | homebase_passive_marker | training_marker
                    | flight_status | tour_role | tax_code | document_type
                    | counter | external_ref | geography
meaning:            Bedeutung
not_this:           Was es NICHT ist (Anti-Verwechslung)
context_rules:      Wann/wie es entscheidungsrelevant ist
examples:           Beispiele aus Fixtures
source_refs:        File:Line-Belege
confidence:         high | medium | low
needs_validation:   true | false (User-Bestätigung nötig?)
```

---

## §1 Crew-Position-Codes

### PU

```
code:               PU
field_type:         crew_position
meaning:            Purser / Kabinenchef. Crew-Position im Roster.
not_this:           - NICHT Pula-Airport (IATA PUY)
                    - NICHT IATA-Code
context_rules:      - Erscheint als Suffix in Markern wie '56381 PU', '68617 PU'
                    - In EXCLUDE-Liste app.py:14114 → wird nicht für IATA-Extraktion verwendet
                    - place_code-Resolver MUSS PU als crew_position behandeln
examples:           '56381 PU', '68617 PU', '82907 PU' (Tibor-Fixture)
source_refs:        app.py:7622, app.py:14114-14115, fixtures/tibor_aerotax_v11_raw_initial.json:572+
confidence:         high
needs_validation:   false
```

### P1, P2, P3, P4

```
code:               P1, P2, P3, P4
field_type:         crew_position
meaning:            Pilot-/Position-Codes (Pilot 1, Pilot 2, ...). Cockpit-Position
                    oder Pattern-Slot.
not_this:           - NICHT Flughafen-Codes
                    - NICHT Flight-Number-Suffixe
context_rules:      - Erscheinen als Suffix nach Roster-ID: '31591 P1'
                    - In EXCLUDE-Liste app.py:14114 → kein IATA
examples:           '31591 P1', '49444 P1 /ZH', '73724 P1' (P1 in Fixtures)
                    P2/P3/P4 nur in EXCLUDE-Liste, keine Fixture-Vorkommen
source_refs:        app.py:14114-14115, fixtures/tibor_aerotax_v11_raw_initial.json:127+
confidence:         high (P1), medium (P2/P3/P4 — nicht in Fixtures belegt)
needs_validation:   false für P1, true für P2/P3/P4 (Bedeutungs-Klärung)
```

### PA

```
code:               PA
field_type:         crew_position
meaning:            (vermutlich) weitere Crew-Position. UNCLEAR ohne weitere Doku.
not_this:           NICHT IATA-Code
context_rules:      Nur in EXCLUDE-Liste app.py:14114 erwähnt
examples:           keine Fixture-Vorkommen gefunden
source_refs:        app.py:14114-14115
confidence:         low
needs_validation:   true
```

---

## §2 Standby / Reserve

### RES

```
code:               RES
field_type:         standby_marker
meaning:            Reserve / Bereitschaftsdienst. Crew steht für Einsatz bereit.
not_this:           - NICHT Resort
                    - NICHT IATA
context_rules:      - Default zuhause: Arbeitstag + KEIN Fahrtag (referenz_faelle.txt:79)
                    - Ausnahme: RES mit Inland-Übernachtung (HAM/MUC) → Z73-Kandidat
                      (referenz_faelle.txt:636)
                    - Phase 5a-Erweiterung: RES + prev_foreign_overnight → standby_hotel
                      (Belegquelle: nur Phase-5a-Code, KEIN explizites referenz_faelle-Statement)
                    - In SANDWICH_MARKERS app.py:14442
examples:           'RES', 'RES /1'
source_refs:        referenz_faelle.txt:79,375-377,636
                    app.py:14442
confidence:         high (für home-duty); medium (für standby_hotel-Erweiterung)
needs_validation:   true (standby_hotel-Pfad nicht explizit in referenz_faelle)
```

### SBY, SB, RES_SB

```
code:               SBY, SB, RES_SB
field_type:         standby_marker
meaning:            Standby / Bereitschaft (Synonyme zu RES).
not_this:           NICHT IATA
context_rules:      Gleich wie RES (siehe oben).
examples:           in SANDWICH_MARKERS
source_refs:        referenz_faelle.txt:79,375
                    app.py:14442
confidence:         high (SBY); medium (SB, RES_SB — pattern_inferred)
needs_validation:   false (SBY), true (SB, RES_SB — Bedeutungs-Klärung)
```

---

## §3 Layover / Frei-Marker

### `X`

```
code:               X
field_type:         layover_free_marker
meaning:            Streckenfrei-Tag / Layover-Off-Day.
not_this:           - NICHT Multiplikations-Zeichen
                    - NICHT Storno-Marker
context_rules:      - Innerhalb Tour (prev_overnight + foreign_layover) → tour_mid
                      (Layover-Free-Day im foreign Hotel)
                    - Zuhause ohne Tour-Continuity → Frei-Tag (non_tour)
                    - In LAYOVER_FREE_MARKERS app.py:14735
                    - In SANDWICH_MARKERS app.py:14442
examples:           'X', 'X HKG', 'X BLR', 'X TLV', 'X HND', 'X BOM'
source_refs:        app.py:7622,14735,14442,15773
                    tests/test_normalized_tours_x_off_markers.py:51,64-65,162-176
confidence:         high
needs_validation:   false
```

### `==`

```
code:               ==
field_type:         layover_free_marker
meaning:            Layover-Continuation-Marker. Tag bleibt am gleichen Layover-Ort.
not_this:           NICHT Equality-Operator
context_rules:      - In LAYOVER_FREE_MARKERS app.py:14735
                    - Mit Sandwich-Pattern (overnight-prev + overnight-next) → tour_mid
                    - Ohne overnight-Kontext → Frei
examples:           '=='
source_refs:        app.py:7622,14735,15774
                    tests/test_normalized_tours_x_off_markers.py:74,87,91-109
confidence:         high
needs_validation:   false
```

### OFF, OF

```
code:               OFF, OF
field_type:         layover_free_marker
meaning:            Off-Day (kein Dienst).
not_this:           NICHT IATA
context_rules:      - Kontextabhängig (wie X)
                    - In LAYOVER_FREE_MARKERS und SANDWICH_MARKERS
                    - OFF und OF wahrscheinlich Varianten — keine ausdrückliche
                      Trennung im Repo
examples:           'OFF', 'OF', 'ORTSTAG OF'
source_refs:        app.py:7622,14735,14442,15775
                    tests/test_normalized_tours_x_off_markers.py:119,135-136
confidence:         high (OFF), medium (OF — nicht eigenständig definiert)
needs_validation:   true (OF-vs-OFF-Trennung klären)
```

---

## §4 Homebase-Passive Marker

### ORTSTAG

```
code:               ORTSTAG
field_type:         homebase_passive_marker
meaning:            Lokaler Hb-Tag — passive Anwesenheit zuhause.
not_this:           NICHT Fahrtag-relevant
context_rules:      - In PASSIVE_MARKERS app.py:14720
                    - In _DETERMINISTIC_PASSIVE_MARKERS app.py:20503
                    - In EXCLUDE-Liste app.py:14114
                    - NO_VMA, kein Fahrtag, kein Hotel
examples:           'ORTSTAG', 'ORTSTAG OF', 'ORTSTAG FRS'
source_refs:        app.py:14114,14720,20503,15778
                    fixtures/tibor_aerotax_v11_raw_initial.json:350,633,688
confidence:         high
needs_validation:   false
```

### FRS

```
code:               FRS
field_type:         homebase_passive_marker
meaning:            Office / Admin-Präsenz am Homebase.
not_this:           NICHT IATA
context_rules:      Wie ORTSTAG.
examples:           'FRS', 'ORTSTAG FRS'
source_refs:        app.py:14720,20503,15779
confidence:         high
needs_validation:   false
```

### LMN, LMN_AS, LMN_CR

```
code:               LMN, LMN_AS, LMN_CR
field_type:         homebase_passive_marker
meaning:            Lokale Maßnahmen / Training / Medical am Homebase.
                    (Abkürzungs-Expansion ist nicht explizit im Repo dokumentiert.)
not_this:           NICHT IATA
context_rules:      - In PASSIVE_MARKERS (LMN_AS, LMN_CR)
                    - LMN (ohne Suffix) in EXCLUDE-Liste, aber nicht in PASSIVE_MARKERS
                      → vermutlich Sammelbegriff
examples:           keine direkten Fixture-Vorkommen gesichtet
source_refs:        app.py:14114,14720,20503
confidence:         medium (Existenz belegt, Bedeutung der Abkürzungs-Expansion uncertain)
needs_validation:   true
```

### FRD

```
code:               FRD
field_type:         homebase_passive_marker
meaning:            (vermutlich) Frei-Tag — keine explizite Doku.
not_this:           NICHT IATA
context_rules:      In PASSIVE_MARKERS app.py:14720
examples:           keine direkten Vorkommen
source_refs:        app.py:14720,20503
confidence:         low
needs_validation:   true
```

---

## §5 Training / Schulung

### EM

```
code:               EM
field_type:         training_marker
meaning:            Erste-Hilfe-Maßnahmen / Briefing-Schulung.
not_this:           NICHT IATA
context_rules:      - Recurrent/Schulung → AT + FT pro Tag (täglich neue Anfahrt)
                    - 3+ aufeinanderfolgende EM/EH/D4-Tage OHNE FREI → Z73-Tour-Verdacht
                      (Hotel-Block)
                    - In EXCLUDE-Liste app.py:14114
examples:           'EM', 'EM /1'
source_refs:        referenz_faelle.txt:80,92,383
                    app.py:14114
confidence:         high
needs_validation:   false
```

### EH

```
code:               EH
field_type:         training_marker
meaning:            Erste-Hilfe-Schulung.
not_this:           NICHT IATA
context_rules:      Wie EM (AT+FT pro Tag, sequenz-bezogen).
examples:           'EH 4 SECCRM 4', 'EH /1'
source_refs:        referenz_faelle.txt:80,92,386
confidence:         high
needs_validation:   false
```

### TK

```
code:               TK
field_type:         training_marker
meaning:            Kurzschulung.
context_rules:      AT + FT.
source_refs:        referenz_faelle.txt:390
confidence:         high
needs_validation:   false
```

### D4 ⚠ WICHTIG

```
code:               D4
field_type:         training_marker
meaning:            Mehrtägige Schulung (Präsenz).
not_this:           NICHT IATA, NICHT Tag-Suffix
context_rules:      - JEDER Tag = AT + FT (tägliche Anfahrt)
                    - Multi-Tag-Block ohne FREI dazwischen → Z73-Tour mit Hotel
                      (referenz_faelle.txt:92)
                    - Critical für Schritt 1b der Klassifikation
examples:           keine direkten Fixture-Vorkommen gesichtet
                    aber: referenz_faelle:393 dokumentiert „DD-Block 15 Tage = 15 AT, 15 FT"
source_refs:        referenz_faelle.txt:81,92,384,393
confidence:         high
needs_validation:   false
```

### DD

```
code:               DD
field_type:         training_marker
meaning:            Seminar / Abordnung (mehrtägig).
context_rules:      JEDER Tag = AT + FT. Multi-Tag-Block.
                    Anti-Pattern dokumentiert: „DD SEMINAR (15 Tage) als 1 Block zählen"
                    ist FALSCH.
source_refs:        referenz_faelle.txt:385,582
confidence:         high
needs_validation:   false
```

### EK

```
code:               EK
field_type:         training_marker (alternativ office)
meaning:            Bürodienst (täglich Anfahrt).
context_rules:      AT + FT (tägliche Anfahrt).
examples:           'EK BÜRODIENST'
source_refs:        referenz_faelle.txt:80,382
confidence:         high
needs_validation:   false
```

### SECCRM, CRM, TRG ⚠ uncertain

```
code:               SECCRM, CRM, TRG
field_type:         training_marker
meaning:            (Phase 5d annahme) Security/CRM/Training-Schulung.
not_this:           NICHT IATA
context_rules:      Erscheinen in Fixture-Markern wie 'EH 4 SECCRM 4', 'EMCRM 4'.
                    Phase 5d-Mock interpretiert sie als Schulungs-Marker.
                    KEIN explizites Statement in referenz_faelle.txt.
examples:           'EH 4 SECCRM 4', 'EMCRM 4' (Tibor-Fixture)
source_refs:        app.py:7622 (Phase-5d-Block — nicht user-bestätigt)
confidence:         medium
needs_validation:   **true** — User muss bestätigen
```

---

## §6 Quellen / Dokumente

### SE (Streckeneinsatzabrechnung)

```
code:               SE
field_type:         document_type
meaning:            Streckeneinsatz-Abrechnung von Lufthansa.
                    Spalten: DATUM | AB | AN | SPESEN-€ | ORT | ZWÖLFTEL |
                    STFREI-€ | STFREI-ORT | STEUER | WERBKO | DOPP | STORNO.
context_rules:      Pflicht-Dokument 3 von 3. Source für stfrei_ort/inland-Flag.
source_refs:        referenz_faelle.txt:440-451
                    CLAUDE.md:35,56
confidence:         high
needs_validation:   false
```

### LSB (Lohnsteuerbescheinigung)

```
code:               LSB
field_type:         document_type
meaning:            Lohnsteuerbescheinigung mit Z17/Z18-Feldern (§3 Nr.16 EStG).
context_rules:      Pflicht-Dokument 1 von 3.
source_refs:        CLAUDE.md:34,56; referenz_faelle.txt:302
confidence:         high
needs_validation:   false
```

### CAS (Flugstundenübersicht / Dienstplan)

```
code:               CAS
field_type:         document_type
meaning:            Crew Activity Schedule / Flugstundenübersicht.
context_rules:      Pflicht-Dokument 2 von 3.
                    Sonnet-Reader: _sonnet_read_dp_structured.
source_refs:        CLAUDE.md:56; FILES.md:7
confidence:         high
needs_validation:   false
```

---

## §7 Flight / Tour-Struktur

### FL ⚠ WICHTIG

```
code:               FL
field_type:         flight_status
meaning:            Layover-Marker im Dienstplan — bedeutet Hotel-Nacht im Ausland.
not_this:           NICHT IATA, NICHT Flight-Suffix
context_rules:      - EINE FL-Markierung IST EINE Hotelnacht
                      (referenz_faelle.txt:53,183-184)
                    - hotel_naechte = Σ aller FL-Marker im Dienstplan, EGAL Inland/Ausland
                    - Layover ≥10h Bodenzeit (EASA-Definition referenz_faelle.txt:316)
                    - Reader-Flag: has_fl=True
examples:           'FL' im Dienstplan
source_refs:        referenz_faelle.txt:53,183-191,316
                    app.py:9405,12111,12155
confidence:         high
needs_validation:   false
```

### Layover, Overnight

```
code:               (Konzepte, nicht Marker)
field_type:         flight_status
meaning:            Layover = Aufenthalt am Zielort mit ortsfester Ruhe (Hotel)
                    Overnight = Übernachtung NACH dem Tag auswärts
context_rules:      Layover ≥10h Bodenzeit. Reader-Flag overnight_after_day.
source_refs:        referenz_faelle.txt:316; app.py:12155
confidence:         high
needs_validation:   false
```

### Tour

```
code:               Tour
field_type:         tour_role (Konzept)
meaning:            Crew-Tour von Start (A-Marker) bis Ende (E-Marker) mit
                    Mehretappen ohne Hb-Heimkehr.
context_rules:      - 1 Tour = 1 Fahrtag (egal wie lang)
                    - Counter: Fahrtage = Σ Tour-Starts
source_refs:        referenz_faelle.txt:277,354-362
confidence:         high
needs_validation:   false
```

### Pairing, Rotation, Sequence, Sequenz, Roster-ID ⚠ uncertain

```
code:               Pairing, Rotation, Sequence, Sequenz, Roster-ID
field_type:         tour_role (Konzepte)
meaning:            (Phase 5d-Annahme) Branchen-übliche Begriffe für Tour-Pattern.
                    5-6-stellige Numerische Marker (z.B. 103703) = Roster-/Sequence-ID,
                    NICHT LH-Flugnummer (LH-Flugnummern sind 3-4-stellig).
context_rules:      Phase-5d-Mock interpretiert numerische Marker als Sequence-ID.
                    KEIN explizites Statement in referenz_faelle.txt.
examples:           '31591 P1', '103703 P1', '32935 PU'
source_refs:        app.py:7622 (Phase-5d-Block — nicht user-bestätigt)
                    Pattern-Beleg via Fixtures
confidence:         medium
needs_validation:   **true**
```

---

## §8 Steuer-Z-Codes

### Z72 — Same-Day-Tagestrip Inland

```
code:               Z72
field_type:         tax_code
meaning:            Same-Day-Tagestrip ohne Übernachtung, >8h Abwesenheit, Inland/EU
context_rules:      Hard-Gate (referenz_faelle.txt:97-105):
                    1. A+E gleicher Tag (kein overnight)
                    2. KEIN FL-Marker
                    3. KEINE Tour-Fortsetzung
                    4. KEINE Übernachtung
                    5. >8h Abwesenheit (480 Minuten)
                    Pauschale: 14€/Tag
source_refs:        referenz_faelle.txt:19-28,97-105
                    app.py:17014 (SAME_DAY_Z72_TOTAL_MINUTES = 480)
                    tests/test_calculation.py:306-312
confidence:         high
needs_validation:   false
```

### Z73 — An-/Abreise Inland mit Hotel

```
code:               Z73
field_type:         tax_code
meaning:            Inland-Tour-Anreise- oder Abreise-Tag mit Hotel.
context_rules:      - Anreise = Z73 (14€), Abreise = Z73 (14€)
                    - Volltage dazwischen = Arbeitstage OHNE VMA
                    - Inland-Schulung mit Hotel (HAM/MUC) → Z73-Kandidaten
source_refs:        referenz_faelle.txt:32-43,108,631-640
                    tests/test_calculation.py:316-322
confidence:         high
needs_validation:   false
```

### Z74 — Inland 24h-Volltag

```
code:               Z74
field_type:         tax_code
meaning:            Inland-Volltag 24h (sehr selten).
context_rules:      - ZW=12 ohne An/Ab-Muster
                    - 28€/Tag
source_refs:        referenz_faelle.txt:65-72,286
confidence:         high
needs_validation:   false
```

### Z76 — Auslandstour

```
code:               Z76
field_type:         tax_code
meaning:            Auslandstour: ALLE Tage der Tour mit BMF-Pauschalen.
context_rules:      - An-/Abreise mit BMF-Anreise-Pauschale
                    - Volltage mit BMF-24h-Pauschale
                    - Decision-Tree: FL ja + Layover Ausland → Z76
source_refs:        referenz_faelle.txt:47-61,246-256,431-436
                    tests/test_bh003a_issue_return_day_z76.py
                    docs/BH003_TIBOR_DIFF_FORENSICS.md:55-56 (Golden-Pattern)
confidence:         high
needs_validation:   false
```

### Z77 — Steuerfreie Spesen vom AG

```
code:               Z77
field_type:         tax_code
meaning:            Σ aller stfrei-€-Beträge die LH steuerfrei gezahlt hat.
context_rules:      - Mindert NUR den Reisekosten-Topf (Z72-Z76)
                    - NICHT Fahrtkosten/Reinigung
                    - Z76 > Z77 = starkes Audit-Warnsignal, KEIN Hard-Cap
source_refs:        referenz_faelle.txt:297-300,450-451,680-682
                    app.py:6054
confidence:         high
needs_validation:   false
```

---

## §9 Counter-Begriffe

| Begriff | Definition | Source |
|---|---|---|
| **Arbeitstage** | Tour + Office + Standby + Schulung (Vollzeit: 110-170) | referenz_faelle.txt:531-533, app.py:8368 |
| **Fahrtage** | 1 Tour = 1 Fahrtag (einfache Strecke) + tägliche Office-Anfahrt | referenz_faelle.txt:277-280, app.py:8365 |
| **Hotelnächte** | Σ FL-Marker, EASA-Layover ≥10h | referenz_faelle.txt:183-191,330-338 |
| **Reinigungstage** | = Arbeitstage. Pauschale 1,60 €/AT (BFH-Praxis) | referenz_faelle.txt:290, app.py:6056 |
| **Anfahrt** | Pendlerpauschale × km × Fahrtage | referenz_faelle.txt:499-501 |

**Invariante (`referenz_faelle.txt:592`)**: Hotelnächte ≤ Arbeitstage.

---

## §10 Externe Referenzen

### FollowMe.aero

```
code:               FollowMe
field_type:         external_ref
meaning:            Externe Dienstplan-Auswertung als Vergleichs-Referenz.
                    Quelle: FollowMe.aero Dienstplanauswertung_99102_2025.pdf
context_rules:      - REFERENZ, NICHT WAHRHEIT
                    - CAS+SE+Plausibilität bleiben Primärquellen
                    - Wenn CAS/FollowMe widersprechen: PLACE/ROUTING-CONFLICT,
                      User-Review oder reader_misread-Verdacht
source_refs:        tests/fixtures/followme_golden_tibor_2025.json:3
                    app.py:7622 (Phase-5d-Block, doc_rule)
confidence:         high
needs_validation:   false
```

### Golden

```
code:               Golden
field_type:         external_ref
meaning:            Soll-Werte / Referenz-Klassifikation aus FollowMe-Auswertung.
context_rules:      Vergleichs-Basis für Acceptance-Tests.
source_refs:        docs/BH_CORE_001_GOLDEN_ACCEPTANCE_PHASE6.md
                    docs/BH003_TIBOR_DIFF_FORENSICS.md
confidence:         high
needs_validation:   false
```

### BMF

```
code:               BMF
field_type:         external_ref
meaning:            Bundesministerium-der-Finanzen-Auslandsspesen-Tabelle.
                    Jährlich aktualisiert (Reviewed: 2026-05-09).
context_rules:      Quelle für Z76-Pauschalen.
source_refs:        referenz_faelle.txt:399-427
                    bmf_data.py
                    FILES.md:82
confidence:         high
needs_validation:   false
```

---

## §11 Geographie

### homebase, FRA

```
code:               homebase
field_type:         geography
meaning:            Heimatflughafen des Crew-Mitglieds (User-Input aus Formular).
not_this:           NICHT hardcoded FRA — FRA bei MUC/BER-Base = normaler Routing-IATA
context_rules:      - User-Input via /process-Endpoint
                    - Default-Fallback FRA wenn nicht gesetzt
                    - Inland-Whitelist: FRA, MUC, HAM, DUS, STR, CGN, BER, LEJ,
                      NUE, BRE, HAJ, TXL, PAD
source_refs:        CLAUDE.md:73-75
                    app.py:3498,8334-8345
                    referenz_faelle.txt:109
confidence:         high
needs_validation:   false
```

---

## §12 Implizite Regeln (im Repo nur kodiert, nicht textlich)

### IATA-Source-Regel (impliziter Standard)

```
rule:               IATA-Codes NUR aus routing / se.stfrei_ort / layover_ort.
                    NICHT aus marker-Feld.
implementation:     EXCLUDE-Liste in app.py:14114-14115:
                    {'RES','SBY','OFF','ORTSTAG','FRS','FRD','LMN','LMN_AS',
                     'LMN_CR','EM','OF','P1','P2','P3','P4','PU','PA'}
                    → Alle Status-/Position-Codes die NICHT als IATA gelten
source_refs:        app.py:14114-14115 (kodiert, nicht textlich dokumentiert)
confidence:         high (implementiert), medium (dokumentiert)
needs_validation:   sollte als explizite Doku-Regel formuliert werden
```

### Z76 vs Z77 — Warnsignal nicht Hard-Cap

```
rule:               Z76 > Z77 ist ein starkes Audit-Warnsignal, aber KEIN Hard-Cap.
context:            Z76 = BMF-Anspruch; Z77 = LH-tatsächlich-gezahlt. Eine Diff
                    >30% kann legitim sein.
source_refs:        referenz_faelle.txt:680-682
confidence:         high
needs_validation:   false
```

### EM/EH/D4-Sequenz → Z73-Tour

```
rule:               3+ aufeinanderfolgende EM/EH/D4-Tage OHNE FREI dazwischen
                    → FAST IMMER Z73-Schulungs-Tour mit Hotel.
source_refs:        referenz_faelle.txt:92
confidence:         high
needs_validation:   false
```

---

## §13 Phase-5d-Glossar Validierung (Vergleich gegen dieses Glossar)

| Phase-5d-Eintrag | Im Repo belegt? | Status |
|---|:-:|---|
| PU = Purser, NICHT Pula | ✓ | belegt (Phase-5d ↔ EXCLUDE-Liste) |
| PUR | ✗ | NICHT separat im Repo. Soft-Variante. |
| P1, P2 = Position-Codes | ✓ (P1), ⚠ (P2) | EXCLUDE-Liste belegt, P2-Bedeutung uncertain |
| CR, FO | ⚠ | NICHT in EXCLUDE/PASSIVE. Phase-5d-Inferenz. |
| RES, SB, SBY | ✓ | belegt |
| X, ==, OFF, OF | ✓ | belegt |
| ORTSTAG, FRS, LMN_AS, LMN_CR, FRD | ✓ | belegt in PASSIVE_MARKERS |
| EM, EH, EK, D4, DD, TK | ✓ | belegt in referenz_faelle.txt (Phase 5d hatte EK/D4/DD/TK NICHT) |
| SECCRM, CRM, TRG | ⚠ | NUR in Fixture-Markern, NICHT als Begriff dokumentiert |
| FL = Layover/Hotel-Marker | ✓ | belegt (Phase 5d hatte das NICHT) |
| 5-6-stellige Numerische = Roster-ID | ⚠ | Phase-5d-Inferenz, KEIN Repo-Beleg |
| Z72/Z73/Z74/Z76 Hard-Gate | ✓ | belegt |
| FollowMe = Referenz, nicht Wahrheit | ✓ | belegt |

---

## §14 Empfehlungen — was muss in den KI-Prompt?

**Belegt + sicher (sollten in Prompt):**
- PU/P1/P2/P3/P4/PA = Crew-Position (NICHT IATA)
- RES/SBY/SB/RES_SB = Standby
- X/==/OFF/OF = Layover-Free / Off-Day (kontextabhängig)
- ORTSTAG/FRS/LMN/LMN_AS/LMN_CR/FRD = Hb-Passive
- EM/EH/EK/D4/DD/TK = Training-Marker mit AT+FT-Regel
- FL = Hotel-Nacht-Indikator
- SE/LSB/CAS = Pflicht-Dokumente
- Z72/Z73/Z74/Z76/Z77 = Tax-Codes
- FollowMe = Referenz nicht Wahrheit
- IATA-Source-Regel (NUR routing/SE/layover_ort)

**Phase-5d-Inferenzen → NICHT in Prompt ohne User-Bestätigung:**
- PUR (als separater Term)
- CR/FO (im Repo nicht in EXCLUDE-Liste oder Doku)
- SECCRM/CRM/TRG (nur Fixture-Pattern, kein Term-Doku)
- 5-6-stellige Numerische = Roster-ID (nicht im Repo)

## §15 Empfehlungen — was muss deterministisch in Python?

**Bereits in Python kodiert (Code-Belege):**
- PASSIVE_MARKERS-Liste → Office-Klassifikation
- SANDWICH_MARKERS-Liste → Tour-Mid-Sandwich-Repair
- LAYOVER_FREE_MARKERS-Liste → Layover-Free-Tag
- EXCLUDE-Liste → IATA-Source-Filter
- Z72-Hard-Gate (480-min-Schwelle)
- Inland-IATA-Whitelist

**Sollte Python-deterministisch sein (nicht KI):**
- EM/EH/EK/D4/DD/TK-Sequenz-Detection (3+ aufeinander ohne FREI → Z73)
- FL-Marker-Counting → Hotelnächte
- Tour=A-bis-E-Erkennung
- Z76>Z77-Warnsignal-Check

**Sinnvoll für KI (Ambiguitäten lösen):**
- X/==/OFF-Disambiguierung wenn evidence ambig
- RES-Hotel vs RES-Home wenn prev-overnight foreign
- Place-Code-Konflikt (CAS vs FollowMe)
- Reader-Misread-Verdacht

---

## §16 Konflikt-Report

### Konflikt 1: Phase-5d-Erweiterungen ohne explizite User-Bestätigung
Siehe §13 oben. Risiko: **mittel**. Affected: SECCRM/CRM/TRG, PUR, CR, FO, Roster-ID-Konzept.

### Konflikt 2: LMN/FRD ohne ausgeschriebene Bedeutung
Existenz belegt, semantische Expansion uncertain. Risiko: **niedrig** (Klassifikation funktioniert).

### Konflikt 3: OFF vs OF
Wahrscheinlich Varianten. Phase 5d behandelt sie äquivalent — wahrscheinlich korrekt.

### Konflikt 4: RES Inland-Übernachtung Z73 vs RES standby_hotel Z76
- referenz_faelle.txt:636: RES mit Inland-Übernachtung = Z73-Kandidat
- Phase 5a: RES + foreign-overnight = standby_hotel Z76
- **Inland-Pfad ist in Phase-5a-Mock NICHT implementiert**

### Konflikt 5: Z76 > Z77 Warnsignal-Implementation
Im Code: kein automatischer Sanity-Check implementiert. Sollte als Audit-Note erscheinen.
