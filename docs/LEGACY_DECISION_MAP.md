# AeroTAX — Legacy Decision Map

Stand: 2026-05-20 (v11 Clean-Release Phase 10 Update).
**Zweck**: Vollständiges Inventar aller Funktionen, die
eine steuer-/tour-/status-relevante Entscheidung treffen können. Pflicht
nach Master-Auftrag Phase K: bei `AEROTAX_TOUR_FIRST_CLASSIFIER=1` darf
NUR die Tour-First-Pipeline final entscheiden.

## §-1 Phase 10 — Flugstundenuebersicht Removal (2026-05-20)

Per `docs/FLUGSTUNDEN_LEGACY_PURGE.md` und v11 Clean-Release sind die Flugstundenuebersicht-Reader **vollständig deaktiviert**:

- `_parse_flugstunden_deterministic` — DEPRECATED, raises RuntimeError ohne Forensik-Override.
- `parse_dienstplan_mit_ki` (legacy DP-KI-Reader) — DEPRECATED, raises RuntimeError.
- `_sonnet_read_dp_structured` — DEPRECATED, raises RuntimeError.
- `_sonnet_read_dp_structured_chunked_v104` — DEPRECATED, raises RuntimeError.
- `hybrid_analyze` elif-DP-Branch — hart auf `document_health.status='red'` umgestellt, KEIN Legacy-Reader-Call mehr.

Forensik-Override: `AEROTAX_LEGACY_FLUGSTUNDEN_FORENSIK=1` macht die Funktionen wieder benutzbar — NICHT für Produktion.

`AEROTAX_PIPELINE_VERSION` Default-Werte bleiben:
- `v11_cas_primary` (Default) → Tour-First-Layer aktiv.
- `v10_legacy` (ENV-Override) → ENV-Variable existiert noch, aber DP-Pipeline ist trotzdem deaktiviert. ENV-Override hat KEINE funktionale Auswirkung mehr.

## §0 Architektur-Regel

**Wenn Tour-First aktiv**:
```
Raw Facts → Normalized Tours → Evidence Engine + KI-Resolver
         → Tour-aware Classification → BMF/Counter
```

Alte Rescue-/Override-Blöcke dürfen NUR:
- Shadow vergleichen
- Audit liefern
- **NIEMALS** final überstimmen

## §1 Entscheidungs-Funktionen — Inventar

### A — KEEP (bleiben immer aktiv)

| Datei:Zeile | Funktion | Rolle | Trifft finale Entscheidung? | Status |
|---|---|---|:-:|:-:|
| `app.py` ~Z2000-3500 | Reader (Sonnet `_sonnet_read_lsb_v2`, `_sonnet_read_se_structured`, `_sonnet_read_dp_structured`) | Strukturierte Lese-Fakten extrahieren | Nein | KEEP |
| `bmf_data.py` | `BMF_AUSLAND_BY_YEAR`, IATA-Mapping | BMF-Pauschalen-Tabelle (jährlich reviewed) | Nein | KEEP |
| `app.py` Z5800-6300 | PDF-Renderer (ReportLab) | Rendert Result-Dict zu PDF | Nein | KEEP |
| `app.py` Z290-880 | Payment/Stripe/Session-Mgmt | Token, Promo, Recall | Nein | KEEP |
| `app.py` Z700-720 | `_ip_rate_limited`, `_client_ip` | Safety | Nein | KEEP |

### B — SHADOW (laufen, dürfen NICHT final entscheiden bei Tour-First=1)

| Datei:Zeile | Funktion | Rolle | Status |
|---|---|---|:-:|
| `app.py:14076` | `_followme_identify_tours(tage_detail, homebase)` | FollowMe-Tour-Pattern-Identifikation (alte v9-Heuristik) | SHADOW |
| `app.py:16353` | `_followme_align_counters(classification, matched_days)` | Counter-Align v11 F3/F4 | SHADOW (durfte Counter überschreiben — gefährlich) |
| `app.py:17122` | `_deterministic_classify_v7(matched_days, year, homebase)` | v7 Tag-Klassifikation (vor Tour-First) | SHADOW |
| `app.py` Rescue-Blöcke (zerstreut) | Issue-Heimkehr-Z76-Rescue (BH-003a), SE-Override-Cluster-C2 | Rescue-Patches | SHADOW |

### C — FINAL (Tour-First-Pfad — finale Quelle wenn Flag aktiv)

| Datei:Zeile | Funktion | Rolle | Status |
|---|---|---|:-:|
| `app.py:14233` | `_normalize_tours_from_raw_facts(matched_days, ...)` | Layer 1: Touren aus Roh-Fakten | FINAL |
| `app.py:14711` | `_build_normalized_day(sig, role, ...)` | NormalizedDay-Builder + Evidence + KI-Resolution + proposed_tour_decision_after_ai | FINAL |
| `app.py:15042` | `_score_tour_day_evidence(day, prev, next, ...)` | Evidence Engine (Phase 4.7/4.8/4.8b) | FINAL |
| `app.py:8101` | `_resolve_uncertain_fact_with_ai(kind, ctx, ...)` | KI-Resolver (Mock/Live, Phase 5a-5d) | FINAL (für NEEDS_AI-Fälle) |
| `app.py:15995` | `_classify_days_from_normalized_tours(normalized_tours, ...)` | Layer 2: Tour-aware Klassifikation + Counter | FINAL |

### D — DEPRECATE (sollte abgeschaltet werden)

| Datei:Zeile | Funktion | Risiko | Ersetzen durch |
|---|---|---|---|
| `app.py` v7-SE-Override-Blöcke (Cluster-C2 in `_deterministic_classify_v7`) | Stille Z76→Z73-Reklassifikation ohne Evidence | mittel | Evidence-Engine `cas_followme_place_conflict` + `_score_tour_day_evidence` |
| `app.py` BH-003a Issue→Z76 Rescue | Hardcoded-Heimkehr-Rescue | niedrig | Tour-First `tour_end` Rolle (Layer 1) |
| `app.py` `_followme_align_counters` Force-Override | Direktes Counter-Überschreiben | hoch | Counter aus `_classify_days_from_normalized_tours._klass_summary` |
| Marker-Heuristiken ohne Glossar (zerstreute Re-Reads) | Markersemantik durch Re-Read statt durch Crew-Code-Glossar | mittel | Crew-Glossary in Prompt + Mock-Dispatcher |
| Review-Builder ohne KI-Kontext | Userfragen ohne Cross-Source-Erklärung | niedrig | KI-Resolver + `proposed_tour_decision_after_ai` |

## §2 Pflicht-Akzeptanz-Tests für Legacy-Kontrolle

**Status: TODO — noch zu implementieren in Phase K-2**

| Test | Akzeptanz | Status |
|---|---|:-:|
| `test_tour_first_flag_off_unchanged_behaviour` | Bei `AEROTAX_TOUR_FIRST_CLASSIFIER=0` läuft alte Pipeline identisch wie vor BH-CORE-001 | ⌛ |
| `test_tour_first_flag_on_uses_only_tour_first_path` | Bei `=1` wird KEINE alte Rescue-Logik final angewendet | ⌛ |
| `test_no_double_counting_old_plus_new_pipeline` | Counter aus Tour-First überschreiben NICHT Counter aus `_followme_align_counters` | ⌛ |
| `test_shadow_legacy_diff_audit_only` | Alte Pipeline läuft, aber Output landet in `_audit_notes`, nicht im Final-Result | ⌛ |

## §3 Feature-Flag-Pfade

Aktuell:
```python
# Implizit: Tour-First-Layer existiert, wird aber NICHT von der Main-Pipeline
# als Final-Quelle genutzt. _deterministic_classify_v7 ist die Default-Quelle.
# AEROTAX_TOUR_FIRST_CLASSIFIER ist Schema, aber Schalter fehlt im Code.
```

**TODO Phase K-3**: Schalter explizit einbauen:
```python
if os.getenv('AEROTAX_TOUR_FIRST_CLASSIFIER') == '1':
    # Tour-First-Pfad als FINAL
    result = _classify_days_from_normalized_tours(...)
    # Legacy läuft im Shadow für audit_notes
    legacy = _deterministic_classify_v7(...)
    result['_legacy_shadow'] = legacy
else:
    # Legacy als FINAL (heutiger Default)
    result = _deterministic_classify_v7(...)
```

## §4 Open Concerns

1. **`_followme_align_counters`** überschreibt aktuell Counter unabhängig vom Flag. Risiko: Tour-First-Counter werden durch Legacy-Align überschrieben → Doppel-Logik.
2. **Rescue-Blöcke** in `_deterministic_classify_v7` sind nicht Flag-gated.
3. **Reader-Misread-Erkennung** läuft heute nur im Tour-First-Pfad. Legacy hat keinen Mechanismus dafür.
4. **PDF-Render** liest direkt aus dem Result-Dict — bei Flag-Wechsel muss `_klass_summary` schema-konform sein.

## §5 Empfehlung Phase K-Closure

| Aktion | Priorität | Status |
|---|:-:|:-:|
| Feature-Flag-Schalter explizit einbauen | high | TODO |
| Schema-Konformitäts-Test Tour-First vs Legacy | high | TODO |
| Acceptance-Tests §2 schreiben | high | TODO |
| `_followme_align_counters` Flag-gaten | medium | TODO |
| Legacy-Audit-Notes in `_audit_notes` | low | TODO |
