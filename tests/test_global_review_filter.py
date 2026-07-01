"""Globaler Review-Filter (2026-05-21 Produkt-Regel: minimiere User-Talk).

`_should_create_review` ist der Gate vor jeder User-Frage. Diese Tests prüfen:
- Auto-Resolve bei KI-Confidence ≥0.90 + suggestion → kein Review
- Low-Money (<5€) → kein Review
- Strong Counter-Evidence → kein Review
- Money-Relevant + Ambiguous → Review erstellen
- Answered Items behalten Status

Plus: _audit_skipped_reviews wird in cls geschrieben damit PDF-Audit sehen
kann was gefiltert wurde (Transparenz ohne User-Belästigung).
"""

import pytest
import conftest as _cft
import app


# ════════════════════════════════════════════════════════════════════
# Direct unit tests of _should_create_review
# ════════════════════════════════════════════════════════════════════

def test_review_item_only_if_money_relevant():
    """Item mit money_impact < 5€ wird gefiltert."""
    item = {'datum': '2025-01-01', 'money_impact_estimate': 3.0, 'confidence': 0.0}
    keep, reason = app._should_create_review(item)
    assert not keep
    assert 'unter Threshold' in reason


def test_review_item_kept_if_money_above_threshold():
    """Item mit money_impact ≥ threshold wird behalten."""
    item = {'datum': '2025-01-01', 'money_impact_estimate': 14.0,
            'confidence': 0.0}
    keep, _ = app._should_create_review(item)
    assert keep


def test_auto_resolve_high_ki_confidence_skips_review():
    """KI conf ≥0.90 + suggested_answer + ai_safe_to_resolve=True → auto-resolve.

    Safety-Hardening 2026-05-21: ai_safe_to_resolve MUSS explizit True sein.
    Verhindert dass KI-Auto-Resolve einen Tag mit CAS/SE-Konflikt silent
    überschreibt.
    """
    item = {'datum': '2025-01-01', 'money_impact_estimate': 14.0,
            'confidence': 0.95, 'suggested_answer': 'office_passive_at_home',
            'ai_safe_to_resolve': True}
    keep, reason = app._should_create_review(item)
    assert not keep
    assert 'auto-resolved' in reason
    assert '0.95' in reason


def test_ai_auto_resolve_requires_ai_safe_to_resolve_flag():
    """Safety-Hardening: KI conf=0.95 + suggestion ABER ohne
    ai_safe_to_resolve=True → KEIN Auto-Resolve.

    Verhindert dass KI silent eine Steuerentscheidung trifft, wenn der
    Candidate-Builder noch nicht bestätigt hat, dass kein CAS/SE-Konflikt
    vorliegt."""
    # Without ai_safe_to_resolve flag → review bleibt
    item = {'datum': '2025-01-01', 'money_impact_estimate': 14.0,
            'confidence': 0.95, 'suggested_answer': 'foreign_tour'}
    keep, _ = app._should_create_review(item)
    assert keep, 'Ohne ai_safe_to_resolve=True kein Auto-Resolve'

    # With ai_safe_to_resolve=False → review bleibt
    item['ai_safe_to_resolve'] = False
    keep, _ = app._should_create_review(item)
    assert keep, 'ai_safe_to_resolve=False blockiert Auto-Resolve'


def test_high_confidence_without_suggestion_does_not_auto_resolve():
    """conf=0.95 aber suggested_answer=None → kein Auto-Resolve, Review bleibt."""
    item = {'datum': '2025-01-01', 'money_impact_estimate': 14.0,
            'confidence': 0.95, 'suggested_answer': None}
    keep, _ = app._should_create_review(item)
    assert keep, 'Ohne suggestion kein Auto-Resolve'


def test_strong_counter_evidence_skips_review():
    """counter_evidence_score ≥3 UND counter_evidence_sources mit ≥2 echten
    Quellen → audit-only, kein Review.

    Safety-Hardening 2026-05-21: Score allein reicht nicht; named sources nötig.
    """
    item = {'datum': '2025-01-01', 'money_impact_estimate': 50.0,
            'confidence': 0.0, 'counter_evidence_score': 4,
            'counter_evidence_sources': ['cas_clear_off', 'prev_day_frei',
                                         'next_day_frei']}
    keep, reason = app._should_create_review(item)
    assert not keep
    assert 'Gegenquelle' in reason


def test_counter_evidence_score_alone_without_sources_does_not_skip():
    """Safety-Hardening: Score=4 ABER counter_evidence_sources fehlt
    → KEIN Skip. Verhindert Magic-Number-Bypass."""
    item = {'datum': '2025-01-01', 'money_impact_estimate': 50.0,
            'confidence': 0.0, 'counter_evidence_score': 4}
    # Keine counter_evidence_sources
    keep, _ = app._should_create_review(item)
    assert keep, 'Score allein ohne sources reicht nicht'


def test_counter_evidence_with_only_one_source_does_not_skip():
    """Safety-Hardening: counter_evidence_sources mit nur 1 Quelle reicht nicht.
    Need ≥2 named sources."""
    item = {'datum': '2025-01-01', 'money_impact_estimate': 50.0,
            'confidence': 0.0, 'counter_evidence_score': 4,
            'counter_evidence_sources': ['cas_clear_off']}
    keep, _ = app._should_create_review(item)
    assert keep, '1 Quelle reicht nicht; minimum 2'


def test_answered_items_always_kept():
    """Bereits beantwortete Items bleiben (status anzeigen, nicht filtern)."""
    item = {'datum': '2025-01-01', 'money_impact_estimate': 3.0,
            'status': 'answered'}
    keep, _ = app._should_create_review(item)
    assert keep


# ════════════════════════════════════════════════════════════════════
# Integration tests against _build_review_items
# ════════════════════════════════════════════════════════════════════

def test_no_review_when_clear_no_money_case():
    """Klar money-irrelevanter Tag wird NICHT zur Review."""
    cls = {'office_training_time_missing_candidates': [
        {'datum': '2025-04-15', 'marker': 'X', 'activity_type': 'office',
         'money_impact_estimate': 2.0},   # < threshold 5
    ]}
    items = app._build_review_items(cls)
    assert not any(it['datum'] == '2025-04-15' for it in items)
    # Aber im Audit-Trail sichtbar
    skipped = cls.get('_audit_skipped_reviews', [])
    assert any(s['datum'] == '2025-04-15' for s in skipped)


def test_no_review_when_strong_counter_evidence():
    """Counter-Evidence-Score + sources überschreibt money-relevance."""
    test_item = {'money_impact_estimate': 14.0, 'counter_evidence_score': 5,
                 'counter_evidence_sources': ['cas_clear_off', 'prev_frei',
                                              'next_frei']}
    keep, reason = app._should_create_review(test_item)
    assert not keep
    assert 'Gegenquelle' in reason


def test_skipped_reviews_recorded_in_audit_trail():
    """cls['_audit_skipped_reviews'] enthält gefilterte Items mit reason."""
    cls = {'office_training_time_missing_candidates': [
        {'datum': '2025-04-15', 'marker': 'X', 'activity_type': 'office',
         'money_impact_estimate': 2.0},  # filtered: low money
    ]}
    items = app._build_review_items(cls)
    skipped = cls.get('_audit_skipped_reviews', [])
    assert len(skipped) >= 1
    s = next((x for x in skipped if x['datum'] == '2025-04-15'), None)
    assert s is not None
    assert 'skip_reason' in s
    assert 'unter Threshold' in s['skip_reason']


def test_review_items_prioritized_by_money_impact():
    """High-Money zuerst (descending)."""
    cls = {
        'near_8h_review_candidates': [
            {'datum': '2025-04-21', 'total_min_known': 465, 'minutes_to_8h': 15,
             'commute_minutes_input': 30, 'activity_type': 'office', 'marker': '',
             'money_impact_estimate': 14.0, 'time_source': 'cas'},
        ],
        'office_training_time_missing_candidates': [
            {'datum': '2025-04-22', 'marker': 'Schulung', 'activity_type': 'office',
             'money_impact_estimate': 28.0},
        ],
    }
    items = app._build_review_items(cls)
    pending = [it for it in items if it['status'] == 'pending']
    impacts = [float(it.get('money_impact_estimate', 0)) for it in pending]
    assert impacts == sorted(impacts, reverse=True)


# ════════════════════════════════════════════════════════════════════
# Type-specific behavior
# ════════════════════════════════════════════════════════════════════

def test_unknown_marker_has_money_impact_above_threshold():
    """unknown_marker items haben money_impact ≥ 14€ (mind. 1 affected day)
    → kommen durch den Filter."""
    cls = {'unknown_marker_candidates': [
        {'datum': '2025-06-15', 'marker': 'XYZ', 'first_token': 'XYZ'},
    ]}
    items = app._build_review_items(cls)
    um = [it for it in items if it['type'] == 'unknown_marker']
    assert len(um) >= 1
    assert um[0]['money_impact_estimate'] >= 14.0


def test_unknown_marker_money_scales_with_affected_days():
    """unknown_marker an N Tagen (Gruppe via first_token) → money_impact ≥ 14€ × N."""
    cls = {'unknown_marker_candidates': [
        {'datum': '2025-06-15', 'marker': 'XYZ', 'first_token': 'XYZ'},
        {'datum': '2025-06-16', 'marker': 'XYZ', 'first_token': 'XYZ'},
        {'datum': '2025-06-17', 'marker': 'XYZ', 'first_token': 'XYZ'},
    ]}
    items = app._build_review_items(cls)
    um = [it for it in items if it['type'] == 'unknown_marker']
    assert len(um) == 1, 'Same first_token = 1 grouped item'
    assert um[0]['money_impact_estimate'] >= 14.0 * 3, \
        f'3 days expected ≥42€, got {um[0]["money_impact_estimate"]}€'


# ════════════════════════════════════════════════════════════════════
# No generic questions / Source guards
# ════════════════════════════════════════════════════════════════════

def test_no_generic_review_questions():
    """Statik: keine generischen 'Was war an dem Tag'-Fragen im Code."""
    src = open(_cft.backend_path('app.py'),
               encoding='utf-8').read()
    forbidden = ['Was war an diesem Tag', 'Was war an dem Tag',
                 'Bitte erkläre den Tag', 'Bitte beschreibe den Tag']
    for f in forbidden:
        assert f not in src, f'Forbidden generische Frage: „{f}"'


def test_no_missing_document_prompt_when_document_present():
    """Statik: Frontend chat prüft missing_months_cas BEVOR es CAS-Upload sagt."""
    html = open(_cft.site_index_html(),
                encoding='utf-8').read()
    assert '_trulyMissingMonths' in html
    assert 'Dienstplan/CAS bereits vorliegen' in html


def test_unknown_marker_resolved_by_context_no_review():
    """Marker mit KI-resolved-semantic + conf≥0.90 + ai_safe_to_resolve=True
    → auto-skip via filter."""
    item = {
        'type': 'unknown_marker',
        'datum': '2025-06-15',
        'money_impact_estimate': 14.0,
        'confidence': 0.92,
        'suggested_answer': 'office_passive_at_home',
        'ai_safe_to_resolve': True,  # Candidate-Builder bestätigt: kein CAS/SE-Konflikt
    }
    keep, reason = app._should_create_review(item)
    assert not keep
    assert 'auto-resolved' in reason


def test_source_conflict_resolved_by_hierarchy_no_review():
    """Source-Conflict mit klarer Quellenhierarchie → counter_evidence_score
    ≥3 + sources → kein Review."""
    item = {
        'type': 'source_conflict',
        'datum': '2025-06-15',
        'money_impact_estimate': 30.0,
        'counter_evidence_score': 4,
        'counter_evidence_sources': ['cas_clear_off', 'no_se', 'adjacent_frei'],
    }
    keep, _ = app._should_create_review(item)
    assert not keep


def test_source_conflict_equal_strength_creates_review():
    """Source-Conflict mit gleicher Quellenstärke (counter<3) → Review erlaubt."""
    item = {
        'type': 'source_conflict',
        'datum': '2025-06-15',
        'money_impact_estimate': 30.0,
        'counter_evidence_score': 1,   # ambiguous
    }
    keep, _ = app._should_create_review(item)
    assert keep


# ════════════════════════════════════════════════════════════════════
# Audit-trail format integrity
# ════════════════════════════════════════════════════════════════════

def test_skipped_audit_entries_have_required_fields():
    """Jeder Skip-Eintrag hat id, type, datum, money_impact, skip_reason."""
    cls = {'office_training_time_missing_candidates': [
        {'datum': '2025-04-15', 'marker': 'X', 'activity_type': 'office',
         'money_impact_estimate': 2.0},
    ]}
    app._build_review_items(cls)
    skipped = cls.get('_audit_skipped_reviews', [])
    assert len(skipped) >= 1
    for s in skipped:
        assert 'id' in s
        assert 'type' in s
        assert 'datum' in s
        assert 'money_impact' in s
        assert 'skip_reason' in s


# ════════════════════════════════════════════════════════════════════
# Safety-Hardening Tests (2026-05-21 Phase 1)
# ════════════════════════════════════════════════════════════════════

def test_ai_auto_resolve_cannot_override_se_foreign():
    """Safety-Trap: Wenn SE foreign-stfrei vorhanden ist + money ≥ 14€,
    NIE silent skip — auch wenn KI sagt 'office passive'.

    Verhindert dass KI eine Auslandstour als Office umdefiniert."""
    item = {
        'datum': '2025-04-21',
        'money_impact_estimate': 50.0,
        'confidence': 0.95,
        'suggested_answer': 'office_passive_at_home',
        'ai_safe_to_resolve': True,    # KI sagt sie könne resolven
        'se_foreign_evidence': True,    # ABER SE foreign-stfrei vorhanden!
    }
    keep, _ = app._should_create_review(item)
    assert keep, 'Source-conflict-trap: SE foreign + money ≥ 14€ → keep review'


def test_source_conflict_trap_kicks_in_at_14_euro_threshold():
    """Source-conflict-trap aktiviert bei money_impact ≥ 14€."""
    base = {
        'datum': '2025-04-21',
        'confidence': 0.95,
        'suggested_answer': 'office_passive_at_home',
        'ai_safe_to_resolve': True,
        'se_foreign_evidence': True,
    }
    # money = 13€ → KEIN trap, KI darf resolven
    item = {**base, 'money_impact_estimate': 13.0}
    keep, _ = app._should_create_review(item)
    assert not keep, 'money 13€ < 14€ → trap inaktiv → auto-resolve'
    # money = 14€ → trap aktiv → keep
    item = {**base, 'money_impact_estimate': 14.0}
    keep, _ = app._should_create_review(item)
    assert keep, 'money 14€ → trap aktiv → keep review'


def test_no_se_foreign_no_trap():
    """Ohne SE foreign-Beleg: normaler Auto-Resolve-Pfad gilt."""
    item = {
        'datum': '2025-04-21',
        'money_impact_estimate': 50.0,
        'confidence': 0.95,
        'suggested_answer': 'office_passive_at_home',
        'ai_safe_to_resolve': True,
        'se_foreign_evidence': False,  # Kein AG-Beleg
    }
    keep, reason = app._should_create_review(item)
    assert not keep
    assert 'auto-resolved' in reason


def test_high_value_skipped_count_in_cls():
    """cls['_audit_high_value_skipped_count'] zählt Skips mit money≥14€."""
    cls = {'office_training_time_missing_candidates': [
        {'datum': '2025-04-15', 'marker': 'X', 'activity_type': 'office',
         'money_impact_estimate': 2.0},  # low-value
        {'datum': '2025-04-16', 'marker': 'Y', 'activity_type': 'office',
         'money_impact_estimate': 4.0},  # low-value
    ]}
    app._build_review_items(cls)
    # Beide unter 5€ → beide skipped, aber nicht high-value (< 14€)
    assert cls.get('_audit_high_value_skipped_count', None) == 0


def test_skipped_audit_includes_evidence_trail():
    """Safety: Skip-Eintrag enthält evidence_for/against/source_refs."""
    cls = {'office_training_time_missing_candidates': [
        {'datum': '2025-04-15', 'marker': 'X', 'activity_type': 'office',
         'money_impact_estimate': 2.0,
         'evidence_for': ['cas_office_marker'],
         'evidence_against': [],
         'source_refs': ['cas:2025-04-15']},
    ]}
    app._build_review_items(cls)
    skipped = cls.get('_audit_skipped_reviews', [])
    assert len(skipped) >= 1
    s = skipped[0]
    # Schema: evidence_for/against/source_refs vorhanden (default [])
    assert 'evidence_for' in s
    assert 'evidence_against' in s
    assert 'source_refs' in s
    assert isinstance(s['evidence_for'], list)
    assert isinstance(s['evidence_against'], list)
    assert isinstance(s['source_refs'], list)
    # high_value flag
    assert 'high_value' in s
    assert s['high_value'] is False  # 2€ ist nicht high-value


def test_skipped_audit_has_no_pii():
    """Audit-Skipped enthält nur strukturierte Felder, keinen User-Namen
    oder Token."""
    cls = {'office_training_time_missing_candidates': [
        {'datum': '2025-04-15', 'marker': 'X', 'activity_type': 'office',
         'money_impact_estimate': 2.0},
    ]}
    app._build_review_items(cls)
    skipped = cls.get('_audit_skipped_reviews', [])
    serialized = str(skipped)
    # Keine üblichen PII-Felder
    forbidden = ['email', 'AT-', 'Bearer', 'token=', 'X-Session', 'password']
    for f in forbidden:
        assert f not in serialized, f'PII-Token „{f}" in audit'


def test_clear_home_off_silent_skip_no_review():
    """Klarer Home-Off-Tag mit echten Counter-Quellen → silent skip."""
    item = {
        'datum': '2025-05-04',
        'money_impact_estimate': 14.0,
        'counter_evidence_score': 4,
        'counter_evidence_sources': ['cas_clear_off', 'no_se',
                                      'no_layover', 'prev_frei'],
    }
    keep, reason = app._should_create_review(item)
    assert not keep
    assert 'Gegenquelle' in reason


def test_foreign_evidence_adjacent_tour_not_silent_skip():
    """Tag sieht frei aus ABER SE foreign + Vortag/Folgetag Tour
    → Source-conflict-trap → Review keep."""
    item = {
        'datum': '2025-07-23',
        'money_impact_estimate': 44.0,
        'confidence': 0.85,  # KI unsicher
        'suggested_answer': 'free',
        'ai_safe_to_resolve': False,  # Candidate-Builder kann nicht safely resolven
        'se_foreign_evidence': True,
    }
    keep, _ = app._should_create_review(item)
    assert keep, 'Foreign-SE + adjacent tour darf NIE silent geskippt werden'


def test_unknown_marker_alone_not_counter_evidence():
    """Unbekannter Marker allein erzeugt KEIN counter_evidence_score.
    Statik-Check: code setzt counter_evidence_score nicht aus
    unknown_marker_candidates."""
    src = open(_cft.backend_path('app.py'),
               encoding='utf-8').read()
    # In _build_review_items darf bei unknown_marker-Item kein counter_evidence
    # gesetzt sein (default 0). Statik-Audit:
    um_block = src[src.find("items.append({\n            'id': (f'unknown_marker"):]
    um_end = um_block.find("})\n")
    um_block = um_block[:um_end] if um_end > 0 else um_block[:2000]
    assert "counter_evidence_score" not in um_block, \
        'unknown_marker darf KEIN counter_evidence_score haben'


def test_missing_se_alone_not_counter_evidence_if_adjacent_tour():
    """Fehlende SE allein ist KEIN starkes Counter, wenn benachbarte Tour da ist.
    Logisch geprüft: counter_evidence_sources muss explizit 'no_se' + andere haben,
    'no_se' allein wird nicht als ≥2 sources erfasst."""
    item = {
        'datum': '2025-07-23',
        'money_impact_estimate': 44.0,
        'counter_evidence_score': 3,
        'counter_evidence_sources': ['no_se'],  # nur 1 Quelle
    }
    keep, _ = app._should_create_review(item)
    assert keep, '1 Quelle (no_se) reicht nicht als counter-evidence'


def test_followme_mismatch_alone_not_counter_evidence():
    """FollowMe-Diff allein ist nicht Counter-Evidence (FM ist nur Benchmark)."""
    item = {
        'datum': '2025-07-23',
        'money_impact_estimate': 44.0,
        'counter_evidence_score': 2,  # FM disagreement allein
        'counter_evidence_sources': ['followme_disagreement'],
    }
    keep, _ = app._should_create_review(item)
    assert keep, 'FM-Diff allein keine harte Counter-Quelle'


def test_reader_issue_alone_not_counter_evidence():
    """Reader-Issue (z.B. tuple-bug) allein nicht Counter."""
    item = {
        'datum': '2025-07-23',
        'money_impact_estimate': 44.0,
        'counter_evidence_score': 2,
        'counter_evidence_sources': ['reader_parsing_issue'],
    }
    keep, _ = app._should_create_review(item)
    assert keep, 'Reader-Issue allein keine Counter-Quelle'


# ════════════════════════════════════════════════════════════════════
# AI cannot create tax amounts directly
# ════════════════════════════════════════════════════════════════════

def test_ai_auto_resolve_cannot_create_tax_amount():
    """KI darf KEINE Beträge setzen. Statik-Check: KI-Resolver-Output enthält
    keine 'eur' oder 'amount'-Schlüssel die direkt in result_data fließen."""
    src = open(_cft.backend_path('app.py'),
               encoding='utf-8').read()
    # _resolve_uncertain_fact_with_ai darf nur 'value', 'confidence', 'reason'
    # liefern — keine 'eur', 'amount', 'betrag' DIREKT in tage_detail
    resolver_idx = src.find('def _resolve_uncertain_fact_with_ai')
    assert resolver_idx > 0
    block = src[resolver_idx:resolver_idx + 5000]
    # KI-Output darf NICHT direkt klass/eur/betrag setzen
    forbidden_in_resolver = ["return {'klass':", "return {'eur':", "return {'betrag':"]
    for f in forbidden_in_resolver:
        assert f not in block, f'KI-Resolver darf nicht direkt {f} liefern'


def test_ai_auto_resolve_context_only():
    """KI-Resolver-Output muss strukturierte 'value' liefern (Kontext),
    nicht direkten Steuerbetrag."""
    src = open(_cft.backend_path('app.py'),
               encoding='utf-8').read()
    # Suche nach KI-resolved-Pattern
    assert "ai_value.get('semantics')" in src or \
           "ai_value.get('meaning')" in src, \
           'KI-Output wird über semantics/meaning gelesen (Kontext, nicht Betrag)'
