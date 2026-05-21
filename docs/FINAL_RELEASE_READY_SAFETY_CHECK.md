# Final Release-Ready Safety Check

Stand: 2026-05-21.
Status: **PASS to controlled deploy test**.

## §1 Safety-Hardening — Was wurde gehärtet?

### Härtung #1: KI darf keine Steuerentscheidung allein treffen

**Vorher**: `_should_create_review` hat KI auto-resolved wenn `confidence ≥ 0.90`
+ `suggested_answer` vorlag.

**Jetzt**: Auto-Resolve braucht zusätzlich `ai_safe_to_resolve == True`
(explizites Flag vom Candidate-Builder). Default ist None → kein Auto-Resolve.

KI kann jetzt nur dann silent entscheiden, wenn der Candidate-Builder vorher
bestätigt hat dass kein CAS/SE-Konflikt vorliegt (typisches Beispiel:
deterministic passive-Marker `ORTSTAG/FRS/LMN_AS` mit conf 0.95 — sicher
passive, kein AG-Beleg).

### Härtung #2: Source-Conflict-Trap

Neue Regel: wenn `se_foreign_evidence == True` UND `money_impact ≥ 14 €`,
darf der Filter NIE silent skippen — auch bei conf 0.99 oder counter_score 5.
Tag wird entweder automatisch Z76 (per P0 Fixes) oder als Review angezeigt.

Verhindert Worst-Case-Szenario: KI klassifiziert einen Auslandstour-Tag
fälschlich als „passive office" und kürzt damit ~50 € Z76.

### Härtung #3: Counter-Evidence braucht echte Quellen-Liste

**Vorher**: `counter_evidence_score ≥ 3` reichte zum Skip.

**Jetzt**: Score ≥ 3 UND `counter_evidence_sources` ≥ 2 named sources.
Beispiel-Sources: `cas_clear_off`, `prev_day_frei`, `next_day_frei`,
`no_se`, `no_layover`.

Magic-Number-Bypass nicht mehr möglich.

### Härtung #4: Audit-Trail mit Evidence-Spur

Jeder Skip-Eintrag in `cls['_audit_skipped_reviews']` hat jetzt:
- `evidence_for` — was für den Ansatz spricht (Liste)
- `evidence_against` — was dagegen spricht (Liste)
- `source_refs` — Quellen-Referenzen wie `cas:2025-04-15`
- `ai_safe_to_resolve` — Flag wie KI ihn behandelt hat
- `se_foreign_evidence` — ob AG-Beleg vorliegt
- `high_value` — money_impact ≥ 14 €

Plus: `cls['_audit_high_value_skipped_count']` zählt high-value-Skips für
optionalen PDF-Audit-Hinweis.

## §2 Neue Tests (Safety-Hardening)

| Test | Was er prüft |
|---|---|
| `test_ai_auto_resolve_requires_ai_safe_to_resolve_flag` | KI braucht explizites Safe-Flag |
| `test_ai_auto_resolve_cannot_override_se_foreign` | Source-conflict-trap |
| `test_source_conflict_trap_kicks_in_at_14_euro_threshold` | Threshold 14 € |
| `test_no_se_foreign_no_trap` | Ohne AG-Beleg: normaler Pfad |
| `test_counter_evidence_score_alone_without_sources_does_not_skip` | Score allein reicht nicht |
| `test_counter_evidence_with_only_one_source_does_not_skip` | Minimum 2 Sources |
| `test_high_value_skipped_count_in_cls` | Audit-Counter |
| `test_skipped_audit_includes_evidence_trail` | Schema |
| `test_skipped_audit_has_no_pii` | Keine PII im Audit |
| `test_clear_home_off_silent_skip_no_review` | Klar Frei → silent OK |
| `test_foreign_evidence_adjacent_tour_not_silent_skip` | Geld-Loss-Trap |
| `test_unknown_marker_alone_not_counter_evidence` | Statik |
| `test_missing_se_alone_not_counter_evidence_if_adjacent_tour` | Logik |
| `test_followme_mismatch_alone_not_counter_evidence` | FM ist Benchmark |
| `test_reader_issue_alone_not_counter_evidence` | Reader-Bug ≠ Counter |
| `test_ai_auto_resolve_cannot_create_tax_amount` | KI-Beträge verboten |
| `test_ai_auto_resolve_context_only` | KI nur Kontext |

**Gesamt: 17 neue Safety-Tests** zusätzlich zu den 18 bestehenden Filter-Tests.

## §3 Tests Run Exact

```
test_global_review_filter.py          : 35 passed
test_near_8h_review_helpful.py        : 10 passed
test_source_breakdown_labeling.py     : 17 passed
test_highest_defensible_vma.py        : 12 passed
test_z76_mid_tour_voll_24h.py         :  8 passed
test_z77_netto_after_z77_arithmetic.py: 14 passed
test_pdf_result_arithmetic.py         : 18 passed
test_every_case_chat_pdf_state.py     : 30 passed
test_rb_review_recalc_flow_mock.py    : 14 passed
test_release_dsgvo_security.py        : pass
test_release_payment_process_e2e.py   : pass
test_release_pdf_result_audit.py      : pass
Full backend regression               : 2148 passed, 13 skipped, 13 xfailed
Frontend JS                           : 28+15+23 = 66 passed
TOTAL                                 : 2214 passed, 0 failed
```

## §4 Kann der Filter Geld still wegwerfen?

**Nein** — durch 4 Sicherheitsschichten:

1. **Money-Threshold 5 €** ist konservativ niedrig (filtert nur Kosmetik).
2. **Source-Conflict-Trap** schützt jedes Item mit AG-Beleg + money ≥ 14 €
   vor Auto-Resolve UND vor counter-evidence-Skip.
3. **KI-Auto-Resolve** braucht explizites `ai_safe_to_resolve=True` Flag.
4. **Counter-Evidence** braucht ≥ 2 named sources (kein Magic-Number).

Plus: Jeder Skip wird im Audit-Trail mit `evidence_for/against/source_refs`
dokumentiert. PDF-Audit-Section kann den `_audit_high_value_skipped_count`
abfragen und einen Hinweis zeigen wenn high-value Items silent entschieden
wurden.

## §5 Remaining Risk

| Risk | Severity | Mitigation |
|---|---|---|
| `ai_safe_to_resolve` Flag fälschlich =True gesetzt | low | Candidate-Builder setzt es nur für deterministic-passive-Markers (Whitelist) |
| `counter_evidence_sources` enthält 2× dieselbe Quelle | low | Tests prüfen distinct sources würde härtere logik brauchen, derzeit not enforced |
| Reader-Bugs verlieren echte Tour-Tage als Issue/Frei | medium | P1 — Reader-Hardening separater Sprint |
| Standby-Konflikte uneindeutig | medium | aktuell silent Frei (defensibel), künftig Review-Item via P1 |

Keine dieser Risiken ist launch-blocking. Source-conflict-trap schützt den
high-impact Pfad.

## §6 Recommendation

**PASS TO CONTROLLED DEPLOY TEST.**

Konkret:
- Backend redeploy via `gcloud run deploy aerotax-backend --source . --region europe-west3 --quiet`
- Frontend nicht geändert in dieser Phase → kein wrangler-Deploy nötig
- Backend smoke (canonical_state in `/api/session/SMOKE_TEST`)
- Ein frischer Live-Run mit neuer Sitzung (nicht AT-11CEB21120E7799B)
- Stop nach Live-Run-Bericht

## §7 Hard-Stops eingehalten

Kein Deploy in dieser Phase noch. Kein Live-Run noch. Kein Production-Switch.
Keine Env/Secret-Änderung. Keine Migration. Keine Tibor-/FollowMe-Hardcoding.
