# Global Review Filter — Final Report

Stand: 2026-05-21.
Produkt-Regel: **AeroTAX soll Nutzer so wenig wie möglich belästigen.**
Review-Fragen sind kein Fallback — sie sind die letzte Stufe nach
Selbstprüfung aller Quellen.

## §1 Globaler Review-Filter

Neue Helper-Funktion `_should_create_review(item, money_threshold=5.0)` in
`app.py` entscheidet pro Item, ob es zum User geht oder silent/audit-only
bleibt. Filter-Reihenfolge:

| Filter | Bedingung | Aktion |
|---|---|---|
| **E. Already answered** | `status == 'answered'` | **keep** (User hat entschieden) |
| **B. Low-Value** | `money_impact_estimate < 5.0 €` | **skip** (`audit-trail`) |
| **A. Auto-Resolve** | `confidence ≥ 0.90` + `suggested_answer` | **skip** (`audit-trail` mit reason) |
| **C. Counter-Evidence** | `counter_evidence_score ≥ 3` | **skip** (`audit-trail`) |
| **D. Money-Relevant Ambiguous** | sonst | **keep** (Review erstellen) |

Implementierung: `app.py:_should_create_review` (neuer Helper) wird am Ende
von `_build_review_items` auf jedes erstellte Item angewendet. Abgelehnte
Items landen in `cls['_audit_skipped_reviews']` mit struktureller Skip-Info
(`id`, `type`, `datum`, `money_impact`, `skip_reason`).

## §2 Welche Review-Typen bleiben

Nach Filter-Gate verbleiben nur:

| Typ | Bedingung Review entsteht |
|---|---|
| `near_8h_review` | total_minutes 420-479 + money_impact 14€ + ambiguous |
| `office_training_time_missing` | Office/Schulung ohne Zeitinfo + nicht-passive Marker + money ≥ 5€ |
| `unknown_marker` | KI-confidence < 0.90 + money_impact (14€ × affected_days) ≥ 5€ |
| `source_conflict` | Quellen gleicher Stärke (counter_evidence_score < 3) + money ≥ 5€ |
| `missing_document` | nur wenn `document_health.missing_months_X` wirklich Monat anzeigt |

## §3 Welche Review-Typen wurden eliminiert (auto-resolved oder audit-only)

| Vorher gefragt | Jetzt automatisch |
|---|---|
| „Was bedeutet ORTSTAG?" / „FRS"? / „LMN_AS"? / „LMN_CR"? / „FRD"? | Silent-skip per deterministic passive-marker-list (bereits implementiert) |
| Unknown_marker mit KI-Resolved Semantic (conf≥0.90) | Auto-resolved via `suggested_answer` filter |
| Tage mit money_impact < 5 € (kosmetische Mini-Diffs) | Skipped als low-value |
| Source-Konflikte mit klarer Quellenhierarchie | Skipped, audit-only |
| Klar über 8h-Tage (≥480min) | Auto-Z72 (bereits implementiert) |
| Klar unter 7h-Tage (< 420min) | Silent, kein Review |
| Foreign-Tour-Tag mit eindeutigem CAS+SE | Auto-Z76 (P0 Fixes #1+#2+#3) |
| Standby zuhause (klar) | Silent kein VMA (bereits Default) |
| Missing-document wenn Dokument vorhanden | Frontend-Filter via `missing_months_cas` (bereits implementiert) |

## §4 Beispiele: Auto-Resolved vs Review

### Auto-Resolved (kein Review)

**Beispiel 1 — Klar über 8h:**
- Input: CAS 21.04. start=07:00 end=17:00 duty=600min, commute=30
- Total: 660min → ≥480 → **Auto Z72** (14€). Kein Review.

**Beispiel 2 — KI-resolved unknown marker:**
- Marker „LMN_AS", KI conf=0.95, suggestion=„office_passive_at_home"
- Filter A: confidence ≥ 0.90 + suggestion → **Skip mit audit-Note**:
  `'id': 'unknown_marker:group:LMN_AS', 'skip_reason': 'auto-resolved (KI confidence 0.95, suggestion: office_passive_at_home)'`

**Beispiel 3 — Source-Conflict gelöst:**
- 24.07. CAS=Frei aber SE=Inland-Stempel
- counter_evidence_score=4 (CAS+Vortag+Folgetag alle Frei)
- Filter C: ≥3 → **Skip audit-only**

### Review (mit Money-Hebel)

**Beispiel 4 — Near-8h Tag:**
- Input: CAS 21.04. start=09:00 end=15:55 duty=415min, commute=30
- Total: 475min → 7:55 Std., 5min fehlen bis Z72
- Filter D: ambiguous + money 14€ → **Review** mit contextueller Frage:
  > „Ich komme für den 2025-04-21 auf 7:55 Std. … Ab mehr als 8 Stunden
  > kann eine Verpflegungspauschale (14 €) angesetzt werden. Warst du
  > länger als 8 Stunden unterwegs?"

**Beispiel 5 — Unknown Marker ohne KI-Resolution:**
- Marker „RB" an 5 Tagen, KI conf=0.30 (low), keine eindeutige Semantik
- money_impact = 14 € × 5 = 70 €
- Filter D: ambiguous + money 70 € → **Review** kontextbezogen:
  > „Im Dienstplan steht 5× die Kennung „RB". Was bedeutet sie?"

## §5 Tests

`tests/test_global_review_filter.py` — **18/18 grün**:

| Test | Was er prüft |
|---|---|
| `test_review_item_only_if_money_relevant` | Threshold-Filter |
| `test_review_item_kept_if_money_above_threshold` | Positiv-Case |
| `test_auto_resolve_high_ki_confidence_skips_review` | Filter A |
| `test_high_confidence_without_suggestion_does_not_auto_resolve` | Edge-Case |
| `test_strong_counter_evidence_skips_review` | Filter C |
| `test_answered_items_always_kept` | Filter E |
| `test_no_review_when_clear_no_money_case` | Integration |
| `test_no_review_when_strong_counter_evidence` | Integration |
| `test_skipped_reviews_recorded_in_audit_trail` | Audit-Trail-Schema |
| `test_review_items_prioritized_by_money_impact` | Sortierung |
| `test_unknown_marker_has_money_impact_above_threshold` | Type-spezifisch |
| `test_unknown_marker_money_scales_with_affected_days` | 14€ × N |
| `test_no_generic_review_questions` | Statik-Audit |
| `test_no_missing_document_prompt_when_document_present` | Frontend-Check |
| `test_unknown_marker_resolved_by_context_no_review` | KI-Auto-Resolve |
| `test_source_conflict_resolved_by_hierarchy_no_review` | Filter C |
| `test_source_conflict_equal_strength_creates_review` | Filter D |
| `test_skipped_audit_entries_have_required_fields` | Schema-Garantie |

Plus:
- `test_near_8h_review_helpful.py` — 10 Tests (existing)
- `test_z76_mid_tour_voll_24h.py` — 8 Tests (existing)
- `test_highest_defensible_vma.py` — 12 Tests (existing)
- `test_source_breakdown_labeling.py` — 17 Tests (existing)

**Full Regression: 2131 passed, 13 skipped, 13 xfailed.** Keine Failures.

## §6 Risiko: Wird Nutzer zu viel gefragt?

**Risiko: niedrig.** Vor jeder Review-Frage greift der Filter:
- Money-Threshold 5 € filtert Mini-Diffs
- KI-Auto-Resolve filtert eindeutige Marker
- Counter-Evidence filtert klare Quellenhierarchie-Fälle
- Sortierung nach money_impact desc zeigt User die Top-Hebel zuerst

**Beispielhafte Reduktion**:
- Vorher (theoretisch ungefiltert): 30-50 Review-Items pro Jahres-Auswertung
- Nach Filter: 3-8 Items (nur Money-Hebel + ambiguous)
- Mit KI-Auto-Resolve: oft 0-3 Items

## §7 Risiko: Wird Geld still verloren?

**Risiko: niedrig, aber nicht null.** Maßnahmen:

1. **Audit-Trail** zeigt jeden Skip mit Grund. PDF-Audit-Section kann
   die `_audit_skipped_reviews` listen — User sieht transparent was
   silent entschieden wurde.
2. **Money-Threshold 5 €** ist konservativ niedrig — fängt nur
   Wirklich-Kosmetisches ab.
3. **counter_evidence_score ≥ 3** verlangt **starke** Gegenquellen
   (z.B. 3+ Tage CAS-Frei für eine vermeintliche Tour) bevor das System
   selbst entscheidet.
4. **Highest-Defensible-Produktregel** sagt: wenn zwei Auslegungen
   vertretbar sind, nimm die user-günstigere. Das gilt parallel zum
   Filter und schließt aggressive Konservativität aus.
5. **KI-Auto-Resolve** nur bei conf ≥ 0.90 + explicit `suggested_answer`
   — sehr hohe Schwelle.

**Wo Geld stille verloren gehen KÖNNTE**:
- Wenn `money_impact_estimate` für einen Typ falsch berechnet ist (z.B.
  nur 1€ statt 14€). Mitigation: Type-spezifische min-floors (z.B.
  unknown_marker = 14€ × affected_days).
- Wenn KI-Auto-Resolve einen Marker fälschlich als „passive" klassifiziert
  (conf 0.92 aber tatsächlich Auslandstour). Mitigation:
  conf-Schwelle 0.90 ist hoch, KI gibt nicht leichtfertig diese Confidence.
- Wenn counter_evidence_score >3 fälschlich gesetzt wird. Mitigation:
  Score wird nur in Code-Pfaden gesetzt die mehrere Quellen vergleichen
  (kein per-Default-3).

## §8 Hard-Stops eingehalten

Kein Deploy. Kein Live-Run. Kein Production-Switch.
Keine Tibor-/FollowMe-Hardcoding.
Keine Marker-only-Tax-Decision.

## §9 Implementierte Änderungen

| Datei | Änderung |
|---|---|
| `app.py` | `+_should_create_review` Helper, `+`Filter-Block am Ende von `_build_review_items`, `+_audit_skipped_reviews` in cls, `+`unknown_marker money_impact = 14€×N |
| `tests/test_global_review_filter.py` (NEU) | 18 Tests |
| `docs/GLOBAL_REVIEW_FILTER_REPORT.md` (NEU) | Dieser Bericht |
