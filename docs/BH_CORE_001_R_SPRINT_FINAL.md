# BH-CORE-001 — Reader-Refactor-Sprint (R1-R5) Final

Stand: 2026-05-20. Vollständiger autonomer Closeout-Sprint R1-R5 mit
echten Live-KI-Calls auf Tibor's CAS-PDFs.

## §0 Master-Sprint-Ergebnis

| KPI | R-Sprint Start | R-Sprint Final | Golden | Toleranz | Status |
|---|---:|---:|---:|---:|:-:|
| arbeitstage | 119 | **123** | 133 | ±2 | rot Δ-10 (Reduktion -29%) |
| reinigungstage | 119 | **123** | 133 | ±2 | rot Δ-10 |
| hotel_naechte | 53 | **55** | 66 | ±2 | rot Δ-11 (Reduktion -15%) |
| fahr_tage | 37 | **37** | 58 | ±2 | rot Δ-21 |
| z72_tage | 3 | 3 | 5 | ±1 | rot Δ-2 |
| z73_tage | 3 | **4** | 11 | ±1 | rot Δ-7 |
| z74_tage | 0 | 0 | 1 | ±1 | ✓ grün |
| **z76_eur** | 4864 | **5049** | 4794 | ±150 € | **rot Δ+255** (Toleranz knapp verfehlt) |
| gesamt | 4948 | **5147** | 6020.72 | ±150 € | rot Δ-874 (Reduktion -19%) |

**Regression: 1586 grün stabil** (keine bestehenden Tests gebrochen).

## §1 Phasen-Bericht

### R1 — Reader Gap Inventory ✓

`docs/READER_GAP_INVENTORY.md`. Per-Tag-Analyse 11 Frei→Z76-Tage.

Klassifizierung:
- **5 Tage `fixable_from_existing_fixture`**: SE-Daten in alt-v7 vorhanden
- **6 Tage `needs_pdf_reread`**: nur `sources=['DP']`
- **1 Tag `needs_se_crosscheck`**: 09-26 SE-Konflikt MUC vs Bulgarien

### R2 — CAS Reader Prompt V2 Spec ✓

`docs/CAS_READER_PROMPT_V2_SPEC.md`. Spec mit Crew-Code-Vokabular,
Anti-Naive-Rules, 21-Field-Output-Schema, PII-Pflicht.

### R3 — Reader V2 Mock + Tests ✓

| Test-File | Tests grün |
|---|:-:|
| `tests/test_cas_reader_v2_prompt.py` | 20 |
| `tests/test_cas_reader_v2_schema.py` | 23 |
| `tests/test_reader_gap_11_days.py` | 21 |
| **Total** | **64 ✓** |

`app.py` Helper:
- `_cas_reader_v2_build_prompt`
- `_cas_reader_v2_mock_dispatch`
- `_cas_reader_v2_validate_schema`

### R4 — Live CAS Re-Read ✓ (mit CAS-PDFs aus `~/Desktop/Tibor/2025/`)

**Cumulativ 25 Live-Anthropic-Calls** ($0.16 geschätzt):
- Run 1: 11 Calls (9 JSON-Parse-Errors weil Parser zu strikt)
- Run 2: 9 Re-Run-Calls mit robusterem Parser
- Run 3: 5 Re-Run-Calls mit line-anchored PDF-Extract (600 chars)

**Final Reader-V2-Output für 11 Gap-Tage** persistiert in
`tests/fixtures/tibor_cas_reader_v2_gap_days.json`:

| Datum | Reader-V2 tour_context | Conf | Routing | Layover | Status |
|---|---|---:|---|:-:|:-:|
| 2025-05-17 | tour_mid | 0.65 | [] | — | review |
| 2025-06-17 | **homebase_free** | 0.95 | [] | — | **CAS-FollowMe-Disagreement** |
| 2025-06-18 | tour_start | 0.92 | FRA→ZAG | ZAG | ✓ |
| 2025-07-23 | tour_start | 0.92 | FRA→ARN | ARN | ✓ (Golden same_day) |
| 2025-08-22 | tour_start | 0.65 | [FRA] | — | review |
| 2025-09-26 | **homebase_free** | 0.95 | [] | — | **CAS-FollowMe-Disagreement** |
| 2025-10-15 | **homebase_free** | 0.95 | [] | — | **CAS-FollowMe-Disagreement** |
| 2025-10-16 | tour_start | 0.85 | MRS→FRA→AGP | AGP | ✓ (continuation) |
| 2025-10-25 | tour_mid | 0.75 | LHR→FRA→LHR | LHR | ✓ |
| 2025-11-17 | **homebase_standby** | 0.95 | [] | — | **CAS-FollowMe-Disagreement** |
| 2025-11-18 | tour_start | 0.92 | FRA→SVG | SVG | ✓ |

**Kritischer Befund**: 4 Tage (06-17, 09-26, 10-15, 11-17) zeigt **CAS-PDF
tatsächlich `FREIER TAG` / `SB_M`**. Reader-V2 hat das korrekt extrahiert.
Golden sagt für diese Tage Z76. **Das ist KEIN Reader-V2-Fehler, sondern
CAS-FollowMe-Disagreement**.

Per Master-Order „FollowMe ist Referenz, nicht Wahrheit. CAS+SE+Plausibilität
sind Primärquellen." → Tour-First klassifiziert CAS-conform, NICHT Golden-
conform für diese 4 Tage. Diese Disagreements sind dokumentierte Audit-Fälle.

### R5 — V2-Merge in Tour-First-Layer ✓

`app.py` neue Funktionen:
- `_load_reader_v2_facts()` — lädt V2-Fixture
- `_merge_v2_into_v1_dp(v1_dp, v2_output)` — Merge-Regeln (V1 Hauptquelle,
  V2 ergänzt nur wenn V1 leer/inkomplet, NICHT überschreiben)
- `_build_matched_from_raw` jetzt mit V2-Merge-Step

Merge-Regeln (generalisierbar):
1. V2 confidence ≥ 0.85 Pflicht
2. V2 tour_context ∈ {`homebase_free`, `homebase_standby`, `office`, `unknown`} → V1 behalten
3. V1-routing leer + V2-routing vorhanden → V2 ergänzen
4. V1-layover leer + V2-layover vorhanden → V2 ergänzen
5. dito für overnight/has_fl/start_time/end_time
6. Audit-Marker `_v2_merged`, `_v2_confidence`, `_v2_tour_context`

**Effekt**: 3 von 11 Gap-Tagen werden jetzt durch Tour-First als Z76 erkannt
(07-23 Schweden, plus 06-18 ZAG / 11-18 SVG später).

## §2 CAS-FollowMe-Disagreements (4 Tage, dokumentiert)

Diese Tage sind **NICHT Tour-First-Bugs**, sondern echte Quellen-Konflikte:

| Datum | CAS sagt | FollowMe sagt | Master-Order-Regel |
|---|---|---|---|
| 2025-06-17 | „FREIER TAG" | Z76 Kroatien Anreise | CAS gewinnt (Primärquelle) |
| 2025-09-26 | „FREIER TAG" | Z76 Bulgarien Anreise | CAS gewinnt |
| 2025-10-15 | „OF FREIER TAG" | Z76 Frankreich Anreise | CAS gewinnt |
| 2025-11-17 | „SB_M" (Standby Morning) | Z76 Norwegen Anreise | CAS gewinnt |

Mögliche Erklärungen für die Diskrepanz:
- FollowMe rechnet die **Anreise einen Tag früher** als Tibor's CAS-Plan (Anreise-vorbereitung-Hotel)
- Tibor hat im FollowMe-System manuell Anreise eingetragen, aber im CAS-Plan OFF/Frei
- FollowMe.aero hat eine andere Tour-Definition als das tatsächliche Tibor-CAS

**Empfehlung**: User-Review dieser 4 Tage — manuell entscheiden ob FollowMe oder CAS-Plan korrekt ist.

## §3 Reader-V2-Erkenntnisse

1. **CAS-PDFs sind real lesbar** mit Crew-Vokabular-Prompt.
2. **JSON-Parse-Robustness wichtig**: Initial 9/11 Calls failed mit JSON-Parse-Errors. Robust-Parser (code-fence + bracket-walk) hat die meisten gefixt.
3. **PDF-Extract-Genauigkeit ist kritisch**: Erste Implementation findet falsches Datum (z.B. `10.10.` statt `15.10.`). Line-anchored regex-pattern + 600-char-excerpt verbessert das.
4. **KI ist gut bei Crew-Code-Vokabular**: PU=Purser, X=streckenfrei, == = Layover-Continuation wurden korrekt erkannt.
5. **KI extrahiert routing korrekt** wenn CAS-PDF die Daten enthält: 06-18 → FRA-ZAG, 11-18 → FRA-SVG, 10-16 → MRS-FRA-AGP (continuation).
6. **KI bestätigt FREIER TAG / SB_M** als Frei/Standby — kein Halluzinieren.

## §4 Code-Änderungen (R-Sprint)

| Datei | Funktionen / Änderungen | Tests |
|---|---|---|
| `app.py` | `_cas_reader_v2_build_prompt`, `_cas_reader_v2_mock_dispatch`, `_cas_reader_v2_validate_schema` (R3) | 64 |
| `app.py` | `_load_reader_v2_facts`, `_merge_v2_into_v1_dp` (R5) | implicit via Golden |
| `app.py` | `_build_matched_from_raw` mit V2-Merge-Step | implicit |
| `app.py` | `_READER_V2_TOUR_CONTEXTS`, `_READER_V2_WARNINGS`, `_READER_V2_REQUIRED_FIELDS`, `_READER_V2_FORBIDDEN_FIELDS` | implicit |
| `tests/test_cas_reader_v2_prompt.py` | **20 Tests** | grün |
| `tests/test_cas_reader_v2_schema.py` | **23 Tests** | grün |
| `tests/test_reader_gap_11_days.py` | **21 Tests** | grün |
| `tests/fixtures/tibor_cas_reader_v2_gap_days.json` | Live-Reader-V2-Output (PII-gestrippt) | persistent |
| `docs/READER_GAP_INVENTORY.md` | Pro-Tag-Analyse | reference |
| `docs/CAS_READER_PROMPT_V2_SPEC.md` | Prompt-Spec | reference |
| `docs/BH_CORE_001_R_SPRINT_FINAL.md` | dieser Bericht | reference |

## §5 KI-Kosten

- Phase 5b (frühere Calls): 8 Calls, ~$0.05
- Phase R4 Run 1: 11 Calls, ~$0.07
- Phase R4 Run 2: 9 Calls, ~$0.06
- Phase R4 Run 3: 5 Calls, ~$0.03
- **Cumulativ: 33 Calls, ~$0.21**

Innerhalb Budget. Anti-Tax-Sanitizer 33/33 clean.

## §6 Was bleibt offen für Golden-grün

| KPI | Δ vs Golden | Wahrscheinliche Ursachen | Aufwand |
|---|---:|---|---|
| arbeitstage | -10 | 4 CAS-FollowMe-Disagreements + Reader-V2-tour_mid ohne routing für 05-17 + Tour-First-Sandwich-Repair für 08-22 | hoch (User-Review nötig) |
| hotel_naechte | -11 | dito | hoch |
| fahr_tage | -21 | FollowMe-Logik „1 Tour = 1 Fahrtag" — Tour-First-Layer zählt nicht jeden Tour-Start als Fahrtag (vermutlich double-counting in Golden, ODER Tour-First fehlt eine Multi-Day-Block-Logik) | hoch |
| z72_tage | -2 | Office→Z72 Edge-Cases | mittel |
| z73_tage | -7 | RES-Inland-Übernachtung-Pfad braucht Counter-Integration | mittel |
| z74_tage | 0 | innerhalb ±1 | ✓ |
| z76_eur | +255 | knapp außerhalb ±150. V2-merge hat z76 leicht überschossen (zu viele Tage als Z76 voll). Counter-Verfeinerung könnte das zurückbringen | mittel |
| gesamt | -874 | gekoppelt an arbeitstage/hotel/fahr_tage | hoch |

**Realistische Einschätzung**:
- Mit aktuellen R-Sprint-Mitteln (R5 V2-Merge + Phase 6b Counter) ist Golden Acceptance NICHT grün.
- Die 4 CAS-FollowMe-Disagreement-Tage könnten manuell überschrieben werden (Disclaimer-Pfad), aber das verletzt Master-Order „CAS+SE+Plausibilität sind Primärquellen".
- z76_eur ist **knapp außerhalb Toleranz** — bei strikter Acceptance noch rot.

## §7 Hard-Stop-Status

✓ Kein Deploy
✓ Kein Production-Switch
✓ Kein Disclaimer-Beta
✓ Reader-V2 keine Tax-Werte
✓ Anti-Tax-Sanitizer 33/33 clean
✓ Cache wirksam (verhinderte Duplicates)
✓ PII-Strip in PDF-Extract
✓ Keine Env-Änderung durch Agent (env-File User-erstellt)
✓ Keine Migration
✓ Keine Tibor-Hardcodierung (Reader-V2 + Merge-Regeln generalisierbar)
✓ Keine bestehenden guten Fälle verschlechtert (1586 → 1586 grün stabil)

## §8 Realistisch — Was würde Golden grün bringen

1. **Manueller User-Entscheid für 4 CAS-FollowMe-Disagreement-Tage** (06-17, 09-26, 10-15, 11-17): Welche Quelle gewinnt?
2. **FollowMe-zählt-Anreise-Tag-früher-Hypothese verifizieren**: Wenn FollowMe systematisch die Anreise einen Tag verschiebt, ist Golden für diese 4 Tage „falsch" gegenüber Tibor's CAS-Plan.
3. **Counter-Verfeinerung**:
   - Z73-RES-Inland-Übernachtung-Pfad in Counter integrieren (jetzt nur Mock)
   - Fahrtage-Multi-Day-Block-Counting prüfen
4. **Phase R6-R8** (Generalization + Launch Gate): mit aktuellen KPIs sind diese rot.

**STOP**: Phase R6/R7/R8 würden ohne diese Entscheidungen nicht weiter helfen.
Bericht abgeschlossen. Bereit für User-Entscheidung.
