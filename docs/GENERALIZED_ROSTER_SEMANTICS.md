# Generalized Roster Semantics

Stand: 2026-05-20 (MegaR Phase 3).

## §0 Prinzip

> AeroTAX darf nicht auf fixes LH-Kabinen-Glossar angewiesen sein.
> Parser unterscheidet **Feld-Position + Routing + Zeit + Homebase + SE + Tour-Continuity**, nicht „Marker-Wort → Bedeutung".

## §1 Field-Type-Unterscheidung im CAS-Reader

Reader V2 (`_sonnet_read_cas_structured` + Schema) liest pro Tag separat:

| Field-Type | Beispiel | Im Schema | Notizen |
|---|---|---|---|
| **Marker/Role/Pattern** | `RES`, `15688 PU`, `EM` | `raw_marker` | Hint, nicht final |
| **Routing** | `FRA→BLR`, `KRK→FRA→IST` | `routing[]` | Foreign/Inland-Detection via INLAND_IATA-Set |
| **Flugnummer** | `LH828-1`, `38652 P1` | implizit im marker oder routing | Optional |
| **Uhrzeit** | `Briefingzeit 11:20` | `start_time`, `end_time` | Mit `duty_duration_minutes` |
| **Layover/Ort** | `BLR`, `HKG`, `SVG` | `layover_ort` | Mit `_is_inland_code` foreign/inland |
| **Bemerkung/Update** | NTF überschreibt PUB | tour_id, position_in_tour | NTF gewinnt gegen PUB |
| **SE-Ort** | `MUC`, `LON`, `CAI` | `se_context.stfrei_ort` | Top-Priority für bmf_place_code (validiert 92% Golden) |
| **Homebase** | `FRA` / `MUC` / `DUS` etc. | `form['base']` Dynamic | NIE hardcoded |
| **Tour-Continuity** | overnight_after_day-Chain | berechnet aus prev/next | sandwich + day-suffix-detection |

## §2 Marker-Interpretation-Regeln (alle Hint, nicht final)

| Marker | Hint-Bedeutung | Verifikation erforderlich |
|---|---|---|
| `PU` | Position-Identifier (kann Purser oder Sequence-ID sein) | NICHT IATA-Match (anti-Pula) |
| `P1/P2/P3` | Position-im-Tour | NICHT IATA |
| `RES`/`RES_SB`/`SBY`/`SB`/`SB_M` | Standby | + SE-Stempel → activated; sonst homebase_idle |
| `X` | Layover-Off | + prev_overnight + in_tour-context → tour_mid; sonst non_tour |
| `OFF` | Frei oder Layover-Off | gleich wie X (kontext-abhängig) |
| `==` | Frei oder Continuation | gleich, schwächer als OFF |
| `OF` / `ORTSTAG`/`FRS`/`LMN_AS`/`LMN_CR`/`FRD` | Passive Homebase | NICHT als Tour-Tag |
| `EM`/`EH`/`EK`/`D4`/`DD`/`TK`/`EMCRM`/`SECCRM` | Training | + start_time → counted_fahrtag |
| `FL` | Flugdienst | + Routing → Tour-Continuation |
| Day-Suffix `(Day N)`/`Tag N` mit N≥2 | Tour-Continuation | + cas_at=tour OR prev_in_tour → tour_mid |
| Sequence-ID Number `15688`, `38652`, `83003` etc. | Tour-ID | informativ, keine direct-decision |

## §3 Anti-Pattern Beispiele

| Verboten (NICHT machen) | Warum | Stattdessen |
|---|---|---|
| `if marker == 'RES': klass = 'Standby'` | Marker-only-decision | `if marker_first in STANDBY_MARKERS AND not se_activated: standby_homebase` |
| `if 'PU' in marker: bmf_place = 'PUY'` | PU != Pula | `if foreign-route-evidence: bmf = layover_or_se_ort` |
| `if routing[0] == 'FRA': starts_hb = True` | Tibor-Bias | `if routing[0] == form['base']: starts_hb = True` |
| `if duty > 840: drop_tour` | Falscher Tour-Drop | `if duty > FTL + other-evidence-weak: drop` (multi-source-check) |

## §4 Reader-V2-Output Felder

`_cas_reader_v2_validate_schema` prüft:

```json
{
  "datum": "...",
  "source_file": "...",
  "source_page": null,
  "raw_marker": "...",
  "activity_type": "tour|frei|standby|training|office|unknown",
  "routing": [],
  "flight_numbers": [],
  "aircraft": "",
  "start_time": "",
  "end_time": "",
  "duty_duration_minutes": null,
  "has_fl": false,
  "starts_at_homebase": false,
  "ends_at_homebase": false,
  "overnight_after_day": false,
  "layover_ort": "",
  "tour_id_candidate": "",
  "position_in_tour": "",
  "tour_context": "<one of 11 contexts>",
  "standby_context": "homebase_idle|airport_standby_after_return|...|unknown",
  "continuation_from_prev_day": false,
  "continuation_to_next_day": false,
  "reader_confidence": 0.0,
  "raw_evidence_excerpt": "",
  "needs_context_resolution": false,
  "warnings": []
}
```

Forbidden Fields (Anti-Tax-Sanitizer): `amount`, `eur`, `euro`, `tagesatz`, `tax`, `steuer`, `betrag`, `pauschale`, `rate`. Reader gibt NIE Steuerbeträge zurück.

## §5 Verifizierungs-Tests

Bestehende Tests in `tests/test_cas_reader_v2_*.py` + `tests/test_megar_phase2_dynamic_parameterization.py`:

| Test | Verifikation |
|---|---|
| PU != Pula | ✓ |
| P1 != IATA | ✓ |
| Marker-only → kein Z76 | ✓ (6 Markers parametrisiert) |
| Unknown marker + Tour-Evidence → Tour | ✓ |
| MUC/DUS/HAM/BER homebase funktioniert | ✓ (5 Bases parametrisiert) |
| Foreign-Layover-OFF (X mit prev_overnight) → tour_mid | ✓ (Sandwich-Repair-Tests) |
| Day-Suffix Day 2/3 → Continuation | ✓ |
| RES + SE = activated (Z76/Z73) | ✓ |
| RES allein = standby_homebase | ✓ |
| Anti-Phantom: == ohne Evidence → non_tour | ✓ |

## §6 Definition of Done für Phase 3

- [x] Reader-V2-Schema lockt Field-Types separat
- [x] Marker als Hints, nicht final
- [x] Anti-Pula PU/P1/P2-Schutz aktiv
- [x] Dynamic homebase
- [x] Anti-Tax-Sanitizer (keine EUR in Reader-Output)
- [x] Tests für Generalization vorhanden
- [x] Doku-Doppel zu `docs/CAS_READER_PROMPT_V2_SPEC.md` minimiert (dieser Doc ist Konzept-Layer)
