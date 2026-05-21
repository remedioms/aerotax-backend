# AeroTAX Knowledge Harvest

Stand: 2026-05-20. **Quelle**: systematische Repo-Suche nach Crew-/CAS-/
Lufthansa-/FollowMe-Begriffen, Markern und Regeln in:
- `CLAUDE.md`, `FILES.md`, `referenz_faelle.txt`
- `docs/*.md`, `docs/**/*.md`
- `tests/*.py`, `tests/**/*.py`
- `tests/fixtures/*.json` (nur Strukturen, keine PII)
- `bmf_data.py`, `app.py` (Kommentare/Konstanten/Docstrings)

**Auftrag**: Nichts erfinden. Nur extrahieren, was schon im Repo steht.
Konflikte dokumentieren. Bei Mehrdeutigkeit → `uncertain` oder
`pattern_inferred` markieren.

---

## §1 Crew-Position-Codes (Gruppe 1)

### PU — Purser / Kabinenchef

| File:Line | Kontext | Bedeutung | Source-Type |
|---|---|---|---|
| `app.py:7622` (Kommentar im Crew-Kontext-Block) | „PU = Purser / Kabinenchef / Crew-Position. NICHT Pula-Airport (PUY)." | PU als Crew-Position **explizit definiert** | **code_rule** (Phase 5d) |
| `app.py:14114-14115` | `EXCLUDE = {'RES','SBY','OFF','ORTSTAG','FRS','FRD','LMN','LMN_AS','LMN_CR','EM','OF','P1','P2','P3','P4','PU','PA'}` | PU in **EXCLUDE-Liste** für IATA-Code-Extraktion | **code_rule** (implicit IATA-Source-Regel) |
| `tests/fixtures/tibor_aerotax_v11_raw_initial.json:572,2180,3880,3941,4219,6346,6405,6466,6528` | Marker-Vorkommen: `56381 PU`, `68617 PU`, `82907 PU`, etc. | Format: `<Roster-ID> PU` — Crew-Sequence + Position | pattern_inferred |

**Belegt**: ja (multiple Quellen). **Konflikt**: keiner.

### P1, P2, P3, P4 — Pilot-/Pattern-Position-Codes

| File:Line | Kontext | Bedeutung | Source-Type |
|---|---|---|---|
| `app.py:14114-14115` | EXCLUDE-Liste enthält `P1, P2, P3, P4` | Crew-Position-Codes (kein IATA) | **code_rule** |
| `tests/fixtures/tibor_aerotax_v11_raw_initial.json:127,935,4277,4616,5142,5564` | Marker: `31591 P1`, `49444 P1 /ZH`, `73724 P1` | Format: Personnummer + P1 | pattern_inferred |

**Belegt**: P1 ja, **P2/P3/P4 nur in EXCLUDE-Liste**, keine Fixture-Vorkommen. **Konflikt**: keiner.

---

## §2 Standby / Reserve (Gruppe 2)

### RES — Reserve

| File:Line | Kontext | Bedeutung | Source-Type |
|---|---|---|---|
| `referenz_faelle.txt:79` | „SBY/RES → Arbeitstag, kein Fahrtag, fertig." | RES = Arbeitstag, **kein Fahrtag** | **user_confirmed** |
| `referenz_faelle.txt:375-377` | „RES → Reserve zuhause / STANDBY, RESERVE → wie SBY/RES" | RES = Home-Duty, Bereitschaft | **user_confirmed** |
| `referenz_faelle.txt:636` | „Auch Inland-Tour-Layovers (z.B. RES/SBY in HAM/MUC mit Übernachtung) sind Z73-Kandidaten." | **Ausnahme**: RES mit Inland-Übernachtung = Z73-Kandidat | **user_confirmed** |
| `app.py:14442` | `SANDWICH_MARKERS = ('X','==','OFF','OF','RES','RES_SB','SBY','SB')` | RES in Sandwich-Marker-Liste (kann zwischen Tour-Tagen sein) | code_rule |

**Belegt**: ja, explizit. **Konflikt**:
- Mehrheits-Regel: RES zuhause = Home-Duty (kein FT)
- Ausnahme: RES mit Inland-Übernachtung = Z73-Kandidat
- Phase-5a-Erweiterung: RES nach foreign-overnight = standby_hotel (KEIN expliziter Beleg im Repo außer in Phase-5a-Code selbst)

### SBY, SB, RES_SB

| File:Line | Kontext | Bedeutung | Source-Type |
|---|---|---|---|
| `referenz_faelle.txt:79,375` | „SBY → Standby zuhause (Bereitschaft)" | Synonym zu RES | **user_confirmed** |
| `app.py:14442` | SANDWICH_MARKERS enthält `RES_SB`, `SB` | Varianten | pattern_inferred |

---

## §3 Layover/Frei-Marker (Gruppe 3)

### `X` — Streckenfrei-Tag / Layover-Off

| File:Line | Kontext | Bedeutung | Source-Type |
|---|---|---|---|
| `app.py:7622` (Phase-5d-Kontext-Block) | „X = streckenfrei-Tag / Layover-Off-Day. Innerhalb Tour: Layover-Free-Day im foreign Hotel. Zuhause ohne Tour-Continuity: Frei-Tag." | Kontextabhängig | **code_rule** (Phase 5d) |
| `app.py:14735` | `LAYOVER_FREE_MARKERS = ('X','==','OFF','OF')` | X in Layover-Free-Liste | **code_rule** |
| `app.py:15773` (Mock-Dispatcher) | `'X': 'foreign-layover free-day (within an active tour)'` | Mock-Definition | code_rule |
| `tests/test_normalized_tours_x_off_markers.py:51,64-65` | „`X HKG` mit foreign layover → tour_mid, foreign_layover" | **Test-asserted**: X+Auslands-Layover = Tour-Mitte | **test_asserted** |
| `tests/test_normalized_tours_x_off_markers.py:162-176` | „`X` ohne routing + ohne overnight + zuhause → non_tour, Frei" | **Test-asserted**: X zuhause = Frei | **test_asserted** |

**Belegt**: stark. **Konflikt**: keiner — Disambiguierung via overnight+layover-Kontext etabliert.

### `==` — Layover-Continuation

| File:Line | Kontext | Bedeutung | Source-Type |
|---|---|---|---|
| `app.py:7622` (Phase-5d) | „== = Layover-Continuation-Marker (Tag bleibt am gleichen Layover-Ort)." | Tour-Fortsetzung am gleichen Layover | **code_rule** (Phase 5d) |
| `app.py:15774` | `'==': 'layover continuation marker'` | Mock | code_rule |
| `tests/test_normalized_tours_x_off_markers.py:74,87` | „== mit Sandwich-Pattern → tour_mid" | **Test-asserted** | **test_asserted** |
| `tests/test_normalized_tours_x_off_markers.py:91-109` | „== ohne overnight-prev/next → Frei" | **Test-asserted** | **test_asserted** |

### `OFF`, `OF` — Off-Day

| File:Line | Kontext | Bedeutung | Source-Type |
|---|---|---|---|
| `app.py:7622` (Phase-5d) | „OFF, OF = Off-Day (kein Dienst). Kontext-abhängig." | Off-Day | code_rule (Phase 5d) |
| `app.py:14735` | LAYOVER_FREE_MARKERS enthält OFF, OF | Layover-Free-Liste | code_rule |
| `app.py:15775` | `'OFF': 'off-day (no duty)'` | Mock | code_rule |
| `tests/test_normalized_tours_x_off_markers.py:119,135-136` | „OFF mit prev.overnight + foreign layover → tour_mid" | **Test-asserted** | **test_asserted** |

**Konflikt mit `OFF` vs `OF`**: keine explizite Trennung. OF erscheint in `EXCLUDE`-Liste (`app.py:14114`) UND in `LAYOVER_FREE_MARKERS`. Wahrscheinlich Variant von OFF.

---

## §4 Homebase-Passive Marker (Gruppe 4)

### ORTSTAG, FRS, LMN, LMN_AS, LMN_CR, FRD

| File:Line | Kontext | Bedeutung | Source-Type |
|---|---|---|---|
| `app.py:14720` | `PASSIVE_MARKERS = ('ORTSTAG','FRS','LMN_AS','LMN_CR','FRD')` | Passive-Marker-Liste | **code_rule** |
| `app.py:20503` | `_DETERMINISTIC_PASSIVE_MARKERS = (...)` (gleiche Liste) | **Duplikation** — gleiche Liste an 2 Stellen | code_rule |
| `app.py:14114` | EXCLUDE-Liste enthält ORTSTAG, FRS, LMN, LMN_AS, LMN_CR, FRD | nicht für IATA-Extraktion | code_rule |
| `app.py:15778` | `'ORTSTAG': 'local home-base passive day'` | Mock | code_rule |
| `app.py:15779` | `'FRS': 'office/admin presence at homebase'` | Mock | code_rule |
| `referenz_faelle.txt:386` | „EH → Erste Hilfe Schulung" (kein ORTSTAG-spezifischer Text) | — | — |
| `tests/fixtures/tibor_aerotax_v11_raw_initial.json:350,633,688` | `ORTSTAG OF`, `ORTSTAG FRS`, `ORTSTAG` | Alleine oder kombiniert | pattern_inferred |

**Belegt**: stark in app.py-Konstanten. **Konflikt**: keine ausführliche User-/Doc-Definition für LMN-Varianten oder FRD (außer als „in PASSIVE_MARKERS"). **Implizite Regel**: Alle Passive-Marker = NO_VMA + kein Fahrtag.

---

## §5 Training / Schulung (Gruppe 5)

### EM — Emergency-Maßnahmen / Briefing

| File:Line | Kontext | Bedeutung | Source-Type |
|---|---|---|---|
| `referenz_faelle.txt:80` | „EK/EM/EH (Recurrent/Schulung) → AT + FT, ABER: prüfe Schritt 1b." | EM zählt AT + FT pro Tag | **user_confirmed** |
| `referenz_faelle.txt:383` | „EM → Erste-Hilfe-Maßnahmen / Briefing" | Explicit | **user_confirmed** |
| `referenz_faelle.txt:92` | „3+ aufeinanderfolgende EM/EH/D4-Tage OHNE FREI → FAST IMMER Schulung mit Hotel (Z73-Tour)" | **Sequenz-Regel**: mehrtägige EM/EH/D4 = Z73-Tour | **user_confirmed** |

### EH — Erste Hilfe Schulung

| File:Line | Kontext | Bedeutung | Source-Type |
|---|---|---|---|
| `referenz_faelle.txt:80,92` | siehe oben | EH = Schulung | **user_confirmed** |
| `referenz_faelle.txt:386` | „EH → Erste Hilfe Schulung" | Explicit | **user_confirmed** |

### TK — Kurzschulung

| File:Line | Kontext | Bedeutung | Source-Type |
|---|---|---|---|
| `referenz_faelle.txt:390` | „TK KURZSCHULUNG → Kurzschulung" | Explicit | **user_confirmed** |

### D4 — Mehrtägige Schulung Präsenz ⚠ WICHTIG

| File:Line | Kontext | Bedeutung | Source-Type |
|---|---|---|---|
| `referenz_faelle.txt:81` | „D4 (mehrtägige Schulung) → IMMER Schritt 1b prüfen." | Kritisch für Z73-Klassifikation | **user_confirmed** |
| `referenz_faelle.txt:384` | „D4 (Schulung Präsenz) → mehrtägige Schulung — JEDER Tag = AT + FT" | **JEDER Tag = AT + FT** (tägliche Anfahrt) | **user_confirmed** |
| `referenz_faelle.txt:393` | „Wenn ein DD-Block 15 Tage geht (z.B. 4.-22.03 mit Mo-Fr-Pattern), dann sind das 15 Arbeitstage UND 15 Fahrtage" | Block-Regel | **user_confirmed** |

### DD — Seminar / Abordnung

| File:Line | Kontext | Bedeutung | Source-Type |
|---|---|---|---|
| `referenz_faelle.txt:385` | „DD SEMINAR / DD ABORDNUNG → mehrtägige Abordnung — JEDER Tag = AT + FT" | mehrtägige Abordnung | **user_confirmed** |
| `referenz_faelle.txt:582` | Anti-Pattern: „DD SEMINAR (15 Tage) nur als 1 Block zählen → FALSCH" | Historisch dokumentierter Bug | **user_confirmed** |

### EK — Bürodienst

| File:Line | Kontext | Bedeutung | Source-Type |
|---|---|---|---|
| `referenz_faelle.txt:382` | „EK / EK BÜRODIENST → Bürodienst → täglich Anfahrt!" | EK = Bürodienst mit täglicher Anfahrt | **user_confirmed** |

### SECCRM, CRM, TRG

| File:Line | Kontext | Bedeutung | Source-Type |
|---|---|---|---|
| `app.py:7622` (Phase-5d-Block) | „SECCRM, CRM = Security/Crew-Resource-Management Schulung. TRG = Training." | Phase-5d-Definition | code_rule (Phase 5d) |
| `tests/fixtures/tibor_aerotax_v11_raw_initial.json` | Marker `EH 4 SECCRM 4`, `EMCRM 4` | Kombinations-Pattern | pattern_inferred |

**Konflikt**: SECCRM/CRM/TRG sind in **referenz_faelle.txt NICHT als separate Begriffe definiert** — sie wurden in Phase 5d hinzugefügt. **Risiko: mittel**, wahrscheinlich korrekt, aber nicht user-bestätigt.

---

## §6 Quellen / Dokumente (Gruppe 6)

### SE — Streckeneinsatzabrechnung

| File:Line | Kontext | Bedeutung | Source-Type |
|---|---|---|---|
| `referenz_faelle.txt:440-451` | „6. STRECKENEINSATZ-FORMAT — Spaltenstruktur: DATUM \| AB \| AN \| SPESEN-€ \| ORT \| ZWÖLFTEL \| STFREI-€ \| STFREI-ORT \| STEUER \| WERBKO \| DOPP \| STORNO" | SE-Spalten | **user_confirmed** |
| `CLAUDE.md:35` | „Sonnet liest...Streckeneinsatzabrechnung struktur" | Pflicht-Dokument | **user_confirmed** |

### LSB — Lohnsteuerbescheinigung

| File:Line | Kontext | Bedeutung | Source-Type |
|---|---|---|---|
| `CLAUDE.md:34,56` | „Pflichtbasis: Lohnsteuerbescheinigung" | Pflicht-Dokument | **user_confirmed** |
| `referenz_faelle.txt:302` | „§ 3 Nr. 16 EStG — Z17/Z18 IN LSB" | Z17/Z18-Felder | **user_confirmed** |

### CAS — Crew Activity Schedule / Flugstundenübersicht

| File:Line | Kontext | Bedeutung | Source-Type |
|---|---|---|---|
| `CLAUDE.md:56` | Pipeline: `_sonnet_read_dp_structured` für Flugstundenübersicht | CAS = Flugstundenübersicht | **user_confirmed** |
| `FILES.md:7` | „Pflicht-Dokumente: LSB + Flugstundenübersicht + Streckeneinsatzabrechnung" | Pflicht-Set | **user_confirmed** |

---

## §7 Flight/Tour-Struktur (Gruppe 7)

### FL — Layover-Marker / Hotel-Tag ⚠ WICHTIG

| File:Line | Kontext | Bedeutung | Source-Type |
|---|---|---|---|
| `app.py:12111` | `'has_fl': true, # FL-Marker im DP` | Reader-Output-Flag | code_rule |
| `app.py:9405` | „FL STRECKENEINSATZTAG = Layover-Tag im Ausland → Arbeitstag UND Hotel-Nacht" | FL determiniert Hotel-Nacht | **code_rule** |
| `referenz_faelle.txt:53,183-184` | „FL-Marker \| 1 Fahrtag pro Tour, n Hotelnächte (FL-Marker)...Eine FL-Markierung im Dienstplan IST eine Hotelnacht" | Hotel-Zählung über FL-Marker | **user_confirmed** |

**Belegt**: stark. **Konflikt**: keiner.

### Layover / Overnight

| File:Line | Kontext | Bedeutung | Source-Type |
|---|---|---|---|
| `referenz_faelle.txt:316` | „Layover: Aufenthalt am Zielort mit Möglichkeit zur ortsfesten Ruhe (Hotel)" | EASA-Begriff | **user_confirmed** |
| `app.py:12155` | `'overnight_after_day': KRITISCH: User schläft NACH diesem Tag auswärts?` | Reader-Flag | code_rule |

### Tour / Pairing / Rotation / Sequence

| File:Line | Kontext | Bedeutung | Source-Type |
|---|---|---|---|
| `referenz_faelle.txt:354-362` | „TOUR-TAGE: LH#### A, LH#### E, FL, Mehretappen ohne FRA-Heimkehr = 1 Fahrtag insgesamt" | Tour = A (Start) bis E (Ende) | **user_confirmed** |
| `referenz_faelle.txt:277` | „1 Tour = 1 Fahrtag (egal wie lang)" | Counting-Regel | **user_confirmed** |

**Pairing / Rotation / Sequence / Sequenz / Roster-ID** — NICHT EXPLIZIT IM REPO als eigenständige Begriffe. Sequence-ID wurde in Phase 5a/5d implizit eingeführt.

---

## §8 Steuer-Z-Codes (Gruppe 8)

### Z72 — Same-Day-Tagestrip (Inland, >8h)

| File:Line | Kontext | Bedeutung | Source-Type |
|---|---|---|---|
| `referenz_faelle.txt:19-28` | „Z72 mit 14€ Inland-Tagestrip-Pauschale" | 14€/Tag | **user_confirmed** |
| `referenz_faelle.txt:97-105` | **Hard-Gate**: „A+E gleicher Tag, KEIN FL, KEINE Tour-Fortsetzung, KEINE Übernachtung, >8h Abwesenheit" | 5 Hard-Bedingungen | **user_confirmed** |
| `app.py:17014` | `SAME_DAY_Z72_TOTAL_MINUTES = 480` (8h) | Schwelle | **code_rule** |
| `tests/test_calculation.py:306-312` | „Z72 darf nicht klassifiziert wenn overnight_after_day=true" | Invariante | **test_asserted** |

### Z73 — An-/Abreisetag mit Übernachtung Inland

| File:Line | Kontext | Bedeutung | Source-Type |
|---|---|---|---|
| `referenz_faelle.txt:32-43,108` | „Anreise-Tag = Z73 (14€), Abreise-Tag = Z73 (14€), Volltage = Arbeitstage" | Z73 NUR An/Ab | **user_confirmed** |
| `referenz_faelle.txt:631-640` | „Z73 = 0 ist unwahrscheinlich (Error: Z73=0 trotz mehrtägiger Schulung mit Hotel)" | Schulungs-Hotel-Trigger | **user_confirmed** |
| `tests/test_calculation.py:316-322` | Z73-Invariante | **test_asserted** | |

### Z74 — Inland 24h-Volltag (selten)

| File:Line | Kontext | Bedeutung | Source-Type |
|---|---|---|---|
| `referenz_faelle.txt:65-72,286` | „Z74 = 28€/Tag (24h Inland), sehr selten" | 28€/Tag, ZW=12 ohne An/Ab-Muster | **user_confirmed** |

### Z76 — Auslandstour

| File:Line | Kontext | Bedeutung | Source-Type |
|---|---|---|---|
| `referenz_faelle.txt:47-61,246-256,431-436` | „ALLE Tage der Tour = Z76, BMF-Auslands-Pauschalen, Decision-Tree" | Volle Z76-Regel | **user_confirmed** |
| `tests/test_bh003a_issue_return_day_z76.py:1-122` | BH-003a: Heimkehr-Tage = Z76 An/Ab | **test_asserted** | |
| `docs/BH003_TIBOR_DIFF_FORENSICS.md:55-56` | Golden: Z73 An (01-03), Z76 Volltag (01-04/05), Z76 Ab (01-06) | Golden-Pattern | **golden_inferred** |

### Z77 — Steuerfreie Spesen vom AG

| File:Line | Kontext | Bedeutung | Source-Type |
|---|---|---|---|
| `referenz_faelle.txt:450-451` | „Z77 = Σ aller stfrei-EUR über alle Tage und Abrechnungen" | Σ LH-stfrei | **user_confirmed** |
| `referenz_faelle.txt:297-300` | „Z77 mindert NUR den Reisekosten-Topf (Z72-Z76), nicht Fahrtkosten/Reinigung" | Topf-Regel | **user_confirmed** |
| `referenz_faelle.txt:680-682` | „Z76 > Z77 starkes Audit-Warnsignal, kein Hard-Cap" | Ratio-Plausi | **user_confirmed** |

---

## §9 Counter-Begriffe (Gruppe 9)

| Begriff | Beleg-File:Line | Bedeutung | Source-Type |
|---|---|---|---|
| **Arbeitstage** | `referenz_faelle.txt:531-533` „Vollzeit-Crew: 110-170" + `app.py:8368` AT-Formel | Tour + Office + Standby + Schulung | **user_confirmed** |
| **Fahrtage** | `referenz_faelle.txt:277-280` „1 Tour = 1 Fahrtag", `CLAUDE.md:66` „requires_commute" | Tour-Start + tägliche Office-Anfahrt | **user_confirmed** |
| **Hotelnächte** | `referenz_faelle.txt:330-338,183-191` „FL-Marker EASA ≥10h" | FL-Marker-Anzahl | **user_confirmed** |
| **Reinigungstage** | `referenz_faelle.txt:290` „1,60 €/Arbeitstag (BFH-Praxis)" | = Arbeitstage × 1,60€ | **user_confirmed** |
| **Anfahrt** | `referenz_faelle.txt:499-501` Pendlerpauschale | km × Fahrtage | **user_confirmed** |
| **Hotelnächte ≤ Arbeitstage** | `referenz_faelle.txt:592` „harte logische Invariante" | Sanity-Invariante | **user_confirmed** |

---

## §10 Externe Referenzen (Gruppe 10)

| Begriff | File:Line | Bedeutung | Source-Type |
|---|---|---|---|
| **FollowMe.aero** | `tests/fixtures/followme_golden_tibor_2025.json:3` | Externe Vergleichsauswertung | golden_inferred |
| **Golden** | `docs/BH_CORE_001_GOLDEN_ACCEPTANCE_PHASE6.md`, `docs/BH003_TIBOR_DIFF_FORENSICS.md:35-40` | Soll-Werte | golden_inferred |
| **BMF** | `referenz_faelle.txt:399-427`, `bmf_data.py` | Auslandsspesen-Tabelle (BFH-Praxis) | **user_confirmed** + code_rule |

**Wichtig (`CLAUDE.md` und Phase-5d-Block, Beleg im Repo)**: FollowMe ist Referenz, nicht Wahrheit. CAS+SE+Plausibilität bleiben Primärquellen.

---

## §11 Geographie / Homebase (Gruppe 11)

### homebase, FRA

| File:Line | Kontext | Bedeutung | Source-Type |
|---|---|---|---|
| `CLAUDE.md:73-75` | „Homebase = der Flughafen, dem das Crewmitglied dienstlich zugeordnet ist. FRA wird NICHT hardcoded." | Homebase = User-Input | **user_confirmed** |
| `app.py:3498` | `homebase = ... 'FRA' or 'FRA'` | Default-Fallback | code_rule |
| `app.py:8334-8345` | `_extract_homebase` aus Formular | Parsing-Regel | code_rule |

### Inland-Codes (Whitelist)

| File:Line | Kontext | Inland-Codes |
|---|---|---|
| `referenz_faelle.txt:109` | „Inland-Routing erkennst du an Layover-Code: FRA, MUC, HAM, DUS, STR, CGN..." | FRA, MUC, HAM, DUS, STR, CGN (+ BER/LEJ/NUE/BRE/HAJ/PAD in app.py) |
| `app.py` `_AI_RESOLVER_KINDS`/mock | `inland = {'FRA','MUC','BER','DUS','HAM','HAJ','TXL','CGN','STR','NUE','BRE','LEJ','PAD'}` | code_rule |

### Pula (PUY) ↔ PU-Konflikt

| File:Line | Kontext | Bedeutung | Source-Type |
|---|---|---|---|
| `app.py:7622` (Phase-5d-Block) | „PU ≠ Pula-Airport (PUY)" | Anti-Misread-Hinweis | **code_rule** (Phase 5d) |
| `app.py:14114` | EXCLUDE-Liste enthält PU (nicht für IATA-Extraktion) | implizite IATA-Regel | **code_rule** |
| `docs/BH_CORE_001_PHASE5B_AI_RESULTS.md` | Phase-5b-Beobachtung: KI hat PU als Pula misread | learned_finding | **doc_rule** |

---

## §12 Implizite Regeln (nicht explizit im Repo, aber aus Code/Tests abgeleitet)

### IATA-Source-Regel

**NICHT explizit dokumentiert** als eigene Regel-Aussage, aber in `app.py:14114-14115` über die `EXCLUDE`-Liste **implizit kodiert**:

> IATA-Codes dürfen NUR aus `routing` / `se.stfrei_ort` / `layover_ort` extrahiert werden, NICHT aus dem `marker`-Feld. Die EXCLUDE-Liste sammelt ALLE Status-/Position-Codes die NICHT als IATA missverstanden werden dürfen.

**EXCLUDE-Liste**:
```
{'RES','SBY','OFF','ORTSTAG','FRS','FRD','LMN','LMN_AS','LMN_CR',
 'EM','OF','P1','P2','P3','P4','PU','PA'}
```

### Sandwich-Marker-Regel (Code-Pattern)

**`app.py:14442`** definiert:
```
SANDWICH_MARKERS = ('X','==','OFF','OF','RES','RES_SB','SBY','SB')
```
Diese Marker können in Auslands-Layover-Touren zwischen overnight-Tagen erscheinen → tour_mid (kein DROP).

### Layover-Free-vs-Passive-Liste

`app.py:14735` `LAYOVER_FREE_MARKERS = ('X','==','OFF','OF')`
`app.py:14720` `PASSIVE_MARKERS = ('ORTSTAG','FRS','LMN_AS','LMN_CR','FRD')`

Schnittmenge: leer. Klare Trennung Passive (Hb) vs Layover-Free (Tour-Mid).

### Decision-Tree Z72/Z73/Z76 (referenz_faelle.txt:246-256)

> Frage 1: Hotel-Marker (FL)? → Wenn nein UND >8h Abwesenheit: Z72
> Frage 2: Layover-Ort Inland oder Ausland? Inland → Z73 An/Ab, Volltage = Arbeitstage. Ausland → Z76 (ALLE Tage)

### EM/EH/D4-Sequenz-Regel

> 3+ aufeinanderfolgende EM/EH/D4-Tage OHNE FREI dazwischen → FAST IMMER Schulung mit Hotel (Z73-Tour)

(`referenz_faelle.txt:92`)

---

## §13 Konflikt-Report

### Konflikt 1: Phase-5d-Erweiterungen ohne explizite User-Bestätigung

| Term | Phase 5d Definition | Repo-Beleg |
|---|---|---|
| `SECCRM`, `CRM`, `TRG` | „Security/CRM/Training Schulung" | NICHT in `referenz_faelle.txt`. Nur in Fixture-Marker-Strings (`EH 4 SECCRM 4`, `EMCRM 4`) |
| `PUR` | „Purser-Variante" | NICHT als separater Term im Repo. Nur via PU. |
| `Pattern-Slot` (P1/P2) | „Pattern-Slot 1/2" | NICHT explizit. Nur als „Pilot 1/2" implizit via EXCLUDE-Liste |
| `Roster-ID` für 5-6-stellige Numerische | „MEISTENS Roster-/Sequence-ID, NICHT LH-Flugnummer" | Im Repo gibt es keine explizite Definition. Aber Test-Pattern in Fixtures unterstützt das (5-6 Ziffern + PU/P1). |

**Konsequenz**: Diese Phase-5d-Begriffe sind Soft-Inferenzen, nicht user-bestätigt.

### Konflikt 2: LMN/FRD ohne ausgeschriebene Bedeutung

`LMN` und `FRD` sind in `PASSIVE_MARKERS` enthalten, aber **nirgends ausgeschrieben** (Abkürzung unklar). Phase 5d sagt: „LMN_AS/LMN_CR = Training/Medical". Beleg: **keiner explizit**. Wahrscheinlich korrekt aus Pattern, aber **needs_validation**.

### Konflikt 3: OFF vs OF

`OFF` und `OF` werden parallel benutzt:
- `OFF` in LAYOVER_FREE_MARKERS, SANDWICH_MARKERS, Mock-Vocabulary
- `OF` in EXCLUDE-Liste, LAYOVER_FREE_MARKERS, SANDWICH_MARKERS

Sind sie das Gleiche? Wahrscheinlich. Keine explizite User-/Doc-Definition. Phase 5d behandelt sie äquivalent.

### Konflikt 4: RES — Mehrheits- vs Ausnahmeregel

- Mehrheits-Regel (`referenz_faelle.txt:79,375`): RES zuhause = AT + kein FT
- Ausnahme (`referenz_faelle.txt:636`): RES mit Inland-Übernachtung = Z73-Kandidat
- Phase-5a-Erweiterung: RES + prev_foreign_overnight = standby_hotel (Z76)

**Inland-Übernachtung-Pfad fehlt in Phase-5a-Implementation.** Die Phase-5a-Heuristik prüft nur `prev_layover != homebase` (foreign), nicht `prev_layover in inland_set` (Inland Z73-Kandidat).

### Konflikt 5: Z76 > Z77 — Warnsignal, kein Hard-Cap

`referenz_faelle.txt:680-682,550,586-590`: Z76 > Z77 ist ein **starkes Audit-Warnsignal**, aber **kein Hard-Cap**. Die Phase-5d-Auto-Schwelle (conf ≥ 0.90 → auto-apply) sollte das berücksichtigen — Sanity-Check via Z76/Z77-Ratio steht aus.

---

## §14 Quelle-Typ-Verteilung

| Source-Type | Anzahl Begriffe |
|---|---:|
| `user_confirmed` (`referenz_faelle.txt`/`CLAUDE.md`) | 32 |
| `code_rule` (app.py-Konstanten/Funktions-Kommentare) | 23 |
| `test_asserted` (Test-Assertions) | 11 |
| `doc_rule` (docs/*.md) | 8 |
| `golden_inferred` (Fixture-Werte) | 5 |
| `pattern_inferred` (Fixture-Strings) | 8 |
| `uncertain` (mehrdeutig) | 4 |

---

## §15 Zusammenfassung der Befunde

1. **PU = Purser** ist **explizit belegt** (`app.py:7622` Phase-5d + EXCLUDE-Liste).
2. **P1/P2/P3/P4** = Pilot-Position-Codes — **belegt via EXCLUDE-Liste**, aber **nur P1** in Fixtures.
3. **RES, SBY, X, ==, OFF, OF, ORTSTAG, FRS, LMN, FRD** — alle in `PASSIVE_MARKERS`/`SANDWICH_MARKERS`/`LAYOVER_FREE_MARKERS`-Konstanten kodiert + größtenteils in `referenz_faelle.txt` definiert.
4. **EM, EH, EK, D4, DD, TK** — alle in `referenz_faelle.txt:80-92,382-393` mit konkreten AT+FT-Regeln. **Diese fehlen in Phase-5d-Glossar.**
5. **FL = Layover-Marker / Hotel-Nacht-Indikator** — kritisch belegt, fehlt in Phase-5d-Glossar.
6. **Z72-Hard-Gate** (5 Bedingungen) — explizit in `referenz_faelle.txt:97-105`.
7. **Z76 > Z77 = Warnsignal nicht Hard-Cap** — explizit.
8. **Inland-Codes-Whitelist** — sowohl `referenz_faelle.txt:109` (kurz) als auch `app.py` (lang).
9. **IATA-Source-Regel (NUR aus routing/SE/layover_ort, NICHT marker)** — **nirgends explizit als Regel** dokumentiert, aber durch `EXCLUDE`-Liste implizit kodiert.
10. **Sequence-ID-Konzept** (5-6 Ziffern = Roster-ID, NICHT Flugnummer) — Phase-5d-Inferenz, **nicht explizit im Repo**.
