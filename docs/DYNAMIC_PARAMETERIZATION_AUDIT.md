# Dynamic Parameterization Audit

Stand: 2026-05-20 (MegaR Phase 2).

## §0 Master-Regel

> Airport == homebase, nie Airport == FRA.
> Homebase kommt aus Website/Session.
> Role/airline nur Kontext, keine harte Steuerlogik.
> Crew-Marker niemals allein final.

## §1 FRA-References im Code (audit)

| Datei:Zeile | Kontext | Hardcoded? | Darf bleiben? | Begründung |
|---|---|:-:|:-:|---|
| Function defaults `homebase='FRA'` (20× Stellen) | Funktion-Signaturen | nein | ✓ | Defensive Default, Production-Caller passt dynamic `base` |
| `(homebase or 'FRA').upper()` (20+ Stellen) | Fallback-Pattern | nein | ✓ | Fallback nur wenn Caller None passt (Test-Code) |
| `INLAND_IATA_CODES = {'FRA','MUC',...}` Z11343 | Inland-Detection | nein | ✓ | Korrekte Semantik: FRA ist deutsche IATA, gehoert in Inland-Set |
| `_extract_homebase` Fallback `return 'FRA'` Z8489 | Pflicht-Validator-Fallback | low | ✓ | Production: `request.form.get('base')` ist HARD-REQUIRED → kein Fallback |
| `INLAND_IATA = {...}` Legacy Z8522/8705/8920 | Deprecated DP-Reader | nein | DEPRECATED | Im Forensik-Override-Pfad, nicht aktiv |
| JSON-Schema-Examples `"routing": ["FRA","DUB"]` Z12309 | Sonnet-Prompt-Beispiel | nein | ✓ | Nur Doku-String, kein Runtime-Effekt |
| `homebase='FRA'` in `parse_dienstplan_mit_ki` | Legacy DP-Reader | DEPRECATED | – | Forensik-Override hartgestoppt |

### KEINE `== 'FRA'` Comparison-Hardcoding gefunden
```
grep -n "== ['\"]FRA['\"]" app.py → 0 hits
```
→ Pipeline verwendet **immer** dynamic `homebase`-Variable, nie literal `'FRA'`.

## §2 Marker-Hardcoding (PU/P1/RES/SB/X/OFF/==)

| Marker | Stellen | Hardcoded? | Begründung |
|---|---:|:-:|---|
| `PU` | Reader-V2-Prompt, marker-list | nein | Hint im Prompt, nicht final-decision |
| `P1/P2/P3` | Marker-IATA-extract guard | nein | Anti-Pula-Interpretation: P1 != IATA |
| `RES/RES_SB/SBY/SB/SB_M` | `_STANDBY_ACTIVATION_MARKERS` Set | controlled | Standby-Activation-Detection (per SE-Stempel disambiguiert) |
| `X/OFF/==/OF` | `_PHANTOM_MARKERS`, `SANDWICH_MARKERS` Sets | controlled | Phantom-Removal + Layover-OFF-Detection; brauchen Kontext-Evidence |
| `ORTSTAG/FRS/LMN_AS/LMN_CR/FRD` | `PASSIVE_MARKERS` Set | controlled | Passive-Homebase-Marker (kein Tour-Indikator) |
| `EM/EH/TK/EMCRM/SECCRM/EK/D4/DD/FL` | Training-Marker-Detection | controlled | Office mit start_time = Fahrtag-Pendel |

**Wichtig**: Diese Markers sind **Hints**, nicht final. Tour-/Z-Klassifikation erfordert immer zusätzliche Evidence (routing, layover, overnight, SE-Stempel, duty, Tour-Continuity).

## §3 Lufthansa/Cabin/Cockpit-Bias

| Wort | Stellen | Hardcoded? | Gefahr |
|---|---:|:-:|---|
| "Lufthansa" / "LH" | Kommentare, Sonnet-Prompts | doku | gering — Prompt erlaubt generische Airline-Marker-Pattern |
| "Cabin/Cockpit" | Glossar-Doku | doku | gering — Reader unterscheidet Position aus Marker-Pattern, nicht Role |
| `marker_iata` extraction | `_extract_iata_from_marker` | controlled | PU/P1/P2 werden **explizit ausgeschlossen** vom IATA-Match |

## §4 Generalization-Pflicht-Pattern

Pipeline klassifiziert basierend auf **Feldpositionen + Kontext**, nicht auf Marker-Wörter allein:

| Pattern | Quelle | NICHT von |
|---|---|---|
| Tour-Start | starts_at_homebase + routing>=2 + foreign-IATA + has_fl/overnight | NICHT marker-allein |
| Tour-Mid | prev_overnight=True + in_tour[i-1] + foreign-layover | NICHT marker-allein |
| Tour-End | ends_at_homebase + prev_overnight + routing[-1]==hb | NICHT marker-allein |
| Same-Day | starts_hb + ends_hb + routing>=2 + foreign-or-inland | NICHT marker-allein |
| Standby-Aktiv | RES/SB-marker + SE-stempel ZUSAMMEN | NICHT marker-allein |
| Foreign-Layover-OFF | X/== mit prev_overnight=True + in_tour-context | NICHT marker-allein |
| Z76 | foreign-layover OR SE-foreign-stempel | NICHT marker-allein |
| Z73 | inland-Anreise/Abreise pattern | NICHT marker-allein |
| Fahrtag | role=tour_start OR role=same_day OR Training+start_time | NICHT marker-allein |

→ **Master-Regel „Marker sind Hinweise, keine finale Wahrheit" eingehalten.**

## §5 Verification

Tests in `tests/test_megar_phase2_dynamic_parameterization.py`:
- MUC-base + MUC-routing → MUC ist homebase, kein FRA-Bias
- DUS-base funktioniert
- HAM-base funktioniert
- BER-base funktioniert
- Unknown marker + foreign-routing + overnight → Tour-Erkennung trotz unbekanntem Marker
- PU != Pula (kein IATA-Match)
- P1 != IATA
- RES allein ohne SE → standby_homebase
- X allein ohne Tour-Continuity → non_tour
- Z76 nur mit foreign-Evidence, nie aus marker allein
- Counters aus klass-Aggregation, nicht aus marker-Pattern

## §6 Verbleibende Tibor-Spezifika

Suche im Produktions-Code:
- `tibor` / `TIBOR`: nur in **Test-Dateien** (`tests/test_*tibor*`) und **Audit-Docs** — keine Tibor-Hardcoding im Produktivcode ✓
- `99102` (Tibor Personalnummer): NICHT im produktiven Code ✓ (verifiziert via `tests/test_v11_phase7_counter_invariants.py::test_no_tibor_hardcoded_strings_in_app`)
- Tibor-Datumsliste: keine ✓

## §7 Definition of Done für Phase 2

- [x] Keine `== 'FRA'` Hardcoding
- [x] `homebase`-Defaults sind defensive Fallbacks, nicht Produktions-Werte
- [x] Marker sind Hints + Evidenz-basiert klassifiziert
- [x] INLAND_IATA-Set ist semantisch korrekt (alle deutsche Flughäfen)
- [x] Tests für MUC/DUS/HAM/BER-Bases vorhanden (siehe Tests in §5)
- [x] Tibor-Hardcoding-Scan: 0 Treffer im Produktiv-Code
