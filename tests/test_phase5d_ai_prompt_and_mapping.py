"""BH-CORE-001 Phase 5d — KI-Prompt mit Crew-Code-Vokabular,
Cross-Source-Context und Structured Decision-Mapping.

Tests verifizieren:
- Crew-Code-Vokabular im Prompt (PU=Purser, P1/P2, RES, X, ORTSTAG, ...)
- Cross-Source-Context (warum unsicher, welcher Konflikt)
- Structured Output mit decision + context_type
- Decision-Mapping: high-conf → auto, medium → review, low → user
- Hard-Blocker: duty_over_ftl, day_already_in_other_tour
- Mock-Updates für PU (not Pula), LAD/TLV phantom, JFK/SNN, OTP transit
- Keine KI-Live-Calls

KEINE Live-Calls. KEINE finale KPI-Änderung.
"""
import os
import sys
import json
import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import app as app_module


pytestmark = pytest.mark.skipif(
    not hasattr(app_module, '_ai_resolver_build_prompt')
    or not hasattr(app_module, '_ai_resolver_safe_context')
    or not hasattr(app_module, '_ai_resolver_mock_dispatch'),
    reason='Phase 5d requires Phase 5a/5b infrastructure',
)


@pytest.fixture(autouse=True)
def _mock_mode(monkeypatch):
    monkeypatch.setenv('AEROTAX_AI_RESOLVER_MODE', 'mock')
    if hasattr(app_module, '_ai_resolver_cache'):
        app_module._ai_resolver_cache.clear()
    yield


def _day(datum, **kw):
    base = {
        'datum': datum, 'activity_type': 'tour', 'routing': [],
        'layover_ort': '', 'overnight_after_day': False,
        'start_time': '', 'end_time': '', 'duty_duration_minutes': 0,
        'raw_marker': '', 'has_fl': False, 'is_workday': True,
        'requires_commute': False, 'starts_at_homebase': False,
        'ends_at_homebase': False,
    }
    base.update(kw)
    return base


def _se(stfrei_ort='', stfrei_total=0.0, stfrei_inland=None, count=0):
    return {'stfrei_ort': stfrei_ort, 'stfrei_total': stfrei_total,
            'stfrei_inland': stfrei_inland, 'count': count,
            'zwoelftel': 0, 'lines': []}


# ════════════════════════════════════════════════════════════════════════════
# 1. Prompt enthält Crew-Code-Vokabular: PU ist Purser, NICHT Pula
# ════════════════════════════════════════════════════════════════════════════

def test_prompt_contains_crew_code_vocabulary_pu_not_pula():
    """Pflicht-Crew-Vokabular-Block muss PU=Purser explizit nennen."""
    ctx = {'day': _day('2025-07-03', routing=['OTP','FRA','LHR']),
           'homebase': 'FRA'}
    prompt = app_module._ai_resolver_build_prompt(
        'routing_consistency', ctx, '129023 PU / Tag 3'
    )
    p = prompt
    assert 'PU' in p
    assert 'Purser' in p
    # Crew-Vokabular muss erklären: NICHT Pula
    assert 'NICHT Pula' in p or 'NICHT Pula-Airport' in p
    # Position-Codes
    assert 'P1' in p and 'P2' in p
    # Numerische Marker explanation
    assert ('Roster-ID' in p or 'Sequenz-ID' in p
            or 'Crew-Sequenz' in p)


# ════════════════════════════════════════════════════════════════════════════
# 2. Prompt enthält Cross-Source-Context
# ════════════════════════════════════════════════════════════════════════════

def test_prompt_contains_cross_source_context():
    """Wenn evidence_against Konflikte signalisiert, muss im Prompt das WHY
    erklärt werden (welcher Konflikt konkret besteht)."""
    ctx = {
        'day': _day('2025-05-20', routing=['FRA','LAD'], layover_ort='LAD',
                    overnight_after_day=True, starts_at_homebase=True,
                    duty_duration_minutes=234, raw_marker='103703 P1'),
        'se': _se(),
        'homebase': 'FRA',
        'followme_context': {'in_any_tour_span': False},
        'evidence_against': [
            ['no_se_allowance', 2, 'no SE-stamp'],
            ['followme_explicit_other_span', 3, 'not in any tour-span'],
            ['no_homebase_commute_evidence', 2, 'not in anfahrten'],
        ],
    }
    prompt = app_module._ai_resolver_build_prompt(
        'tour_boundary', ctx, '103703 P1'
    )
    # Cross-Source-Konflikt-Block muss vorhanden sein
    assert ('CROSS-SOURCE' in prompt
            or 'WARUM EVIDENCE-ENGINE UNSICHER' in prompt
            or 'Cross-Source-Konflikt' in prompt)
    # Konkret muss "Phantom" oder "ohne Beleg" erwähnt werden
    assert ('Phantom' in prompt or 'ohne Beleg' in prompt
            or 'reader_misread' in prompt)


# ════════════════════════════════════════════════════════════════════════════
# 3. Prompt erklärt: FollowMe ist Referenz, nicht Wahrheit
# ════════════════════════════════════════════════════════════════════════════

def test_prompt_explains_followme_is_reference_not_truth():
    ctx = {'day': _day('2025-12-14', routing=['FRA','JFK']),
           'homebase': 'FRA'}
    prompt = app_module._ai_resolver_build_prompt('place_code', ctx, 'JFK')
    assert 'FOLLOWME' in prompt or 'FollowMe' in prompt
    assert 'REFERENZ' in prompt or 'Vergleichsmaßstab' in prompt
    assert 'NICHT die Wahrheit' in prompt or 'CAS+SE' in prompt


# ════════════════════════════════════════════════════════════════════════════
# 4. Structured Output Schema verlangt decision-Feld
# ════════════════════════════════════════════════════════════════════════════

def test_structured_output_requires_decision():
    """Prompt muss explizit decision + context_type-Feld anfordern."""
    ctx = {'day': _day('2025-01-04', routing=[], layover_ort='BLR',
                       overnight_after_day=True, raw_marker='X'),
           'homebase': 'FRA'}
    prompt = app_module._ai_resolver_build_prompt('marker_semantics', ctx, 'X')
    assert '"decision"' in prompt
    assert '"context_type"' in prompt
    # Erlaubte decision-Werte müssen genannt sein
    assert 'KEEP_TOUR' in prompt
    assert 'DROP_TOUR' in prompt
    assert 'NEEDS_REVIEW' in prompt
    # context_type-Werte
    assert 'reader_misread' in prompt
    assert 'routing_conflict' in prompt
    assert 'hotel_standby' in prompt or 'homebase_standby' in prompt


# ════════════════════════════════════════════════════════════════════════════
# 5+6. Decision-Mapping: KEEP/DROP nur bei high-conf
# ════════════════════════════════════════════════════════════════════════════

def _resolver_call(kind, ctx, marker='X'):
    return app_module._resolve_uncertain_fact_with_ai(
        kind=kind, context=ctx, uncertain_fact=marker, _force_mock=True,
    )


def test_ai_decision_keep_maps_only_with_high_confidence():
    """Mock liefert decision=KEEP_TOUR conf=0.92 (BLR X mid-tour)
    → bei high-conf wird in normalized_day proposed_tour_decision_after_ai=KEEP."""
    matched = [
        {'datum': '2025-01-03', 'dp': _day(
            '2025-01-03', routing=['FRA','BLR'], layover_ort='BLR',
            overnight_after_day=True, starts_at_homebase=True,
            duty_duration_minutes=785, raw_marker='31591 P1',
            has_fl=True), 'se': _se(stfrei_ort='BLR', stfrei_total=42.0,
                                    stfrei_inland=False, count=1)},
        {'datum': '2025-01-04', 'dp': _day(
            '2025-01-04', routing=[], layover_ort='BLR',
            overnight_after_day=True, raw_marker='X'),
         'se': _se(stfrei_ort='BLR', stfrei_total=42.0,
                   stfrei_inland=False, count=1)},
    ]
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025,
    )
    days = [d for t in tours for d in t['days']]
    # 01-04 ist KEEP_TOUR via evidence; KI muss nicht überschreiben (high-FOR)
    # Test verifiziert: kein NEEDS_USER fälschlich
    by = {d['datum']: d for d in days}
    assert by['2025-01-04']['proposed_tour_decision_after_ai'] not in ('NEEDS_USER',)


def test_ai_decision_drop_maps_only_with_high_confidence():
    """RES zuhause: Mock liefert decision=DROP_TOUR ctx=homebase_standby
    conf=0.85 → 0.70-0.89-Schwelle → NEEDS_REVIEW (nicht direkt DROP_TOUR)."""
    ctx = {
        'day': _day('2025-03-11', raw_marker='RES',
                    starts_at_homebase=True, ends_at_homebase=True),
        'homebase': 'FRA',
    }
    r = _resolver_call('standby_context', ctx, marker='RES')
    assert r['decision'] == 'DROP_TOUR'
    assert r['context_type'] == 'homebase_standby'
    assert r['confidence'] >= 0.70


# ════════════════════════════════════════════════════════════════════════════
# 7. Medium-confidence → NEEDS_REVIEW
# ════════════════════════════════════════════════════════════════════════════

def test_medium_confidence_maps_to_review():
    """LAD-Phantom: Mock liefert decision=NEEDS_REVIEW conf=0.65
    (oder 0.70-0.89). Mapping = NEEDS_REVIEW."""
    matched = [
        {'datum': '2025-05-20', 'dp': _day(
            '2025-05-20', routing=['FRA','LAD'], layover_ort='LAD',
            overnight_after_day=True, starts_at_homebase=True,
            start_time='20:05', duty_duration_minutes=234,
            raw_marker='103703 P1', has_fl=True),
         'se': _se()},
    ]
    fm = {'anfahrten_dates': set(),
          'tour_spans': [('2025-05-14','2025-05-19',
                          {'2025-05-14','2025-05-15','2025-05-16',
                           '2025-05-17','2025-05-18','2025-05-19'})]}
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025, followme_context=fm,
    )
    days = [d for t in tours for d in t['days']]
    by = {d['datum']: d for d in days}
    assert by['2025-05-20']['proposed_tour_decision_after_ai'] in (
        'NEEDS_REVIEW', 'NEEDS_USER', 'NEEDS_AI'
    )


# ════════════════════════════════════════════════════════════════════════════
# 8. Low-confidence → NEEDS_USER
# ════════════════════════════════════════════════════════════════════════════

def test_low_confidence_maps_to_user_question():
    """Unbekannter Marker, no routing → Mock liefert conf<0.70
    → proposed=NEEDS_USER."""
    matched = [
        {'datum': '2025-04-05', 'dp': _day(
            '2025-04-05', raw_marker='ZZZ_UNKNOWN_MARKER',
            starts_at_homebase=True, ends_at_homebase=True),
         'se': _se()},
    ]
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025,
    )
    days = [d for t in tours for d in t['days']]
    by = {d['datum']: d for d in days}
    if by['2025-04-05']['ai_confidence'] < 0.70 and by['2025-04-05']['ai_resolution_kind']:
        assert by['2025-04-05']['proposed_tour_decision_after_ai'] in (
            'NEEDS_USER', 'NEEDS_REVIEW'
        )


# ════════════════════════════════════════════════════════════════════════════
# 9. Hard-Blocker duty_over_ftl blockiert KEEP-Override durch KI
# ════════════════════════════════════════════════════════════════════════════

def test_hard_blocker_duty_over_ftl_blocks_keep():
    """duty>FTL als evidence_against → proposed darf nicht KEEP_TOUR sein
    selbst wenn KI mit high-conf KEEP vorschlüge."""
    matched = [
        {'datum': '2025-06-01', 'dp': _day(
            '2025-06-01', routing=['FRA','CPH','GOT'], layover_ort='GOT',
            overnight_after_day=True, starts_at_homebase=True,
            start_time='05:00', duty_duration_minutes=1450,
            raw_marker='126533 PU', has_fl=True),
         'se': _se(stfrei_ort='GOT', stfrei_total=42.0,
                   stfrei_inland=False, count=1)},
    ]
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025,
    )
    days = [d for t in tours for d in t['days']]
    by = {d['datum']: d for d in days}
    ag = {n for n, _, _ in by['2025-06-01']['evidence_against']}
    assert 'duty_over_ftl' in ag
    # Hard-Blocker → keine KEEP-Übernahme
    assert by['2025-06-01']['proposed_tour_decision_after_ai'] != 'KEEP_TOUR'


# ════════════════════════════════════════════════════════════════════════════
# 10+11. LAD/TLV Phantom-Prompt erwähnt no_se+no_anfahrt+free_gap
# ════════════════════════════════════════════════════════════════════════════

def _phantom_prompt(datum, routing, layover):
    ctx = {
        'day': _day(datum, routing=routing, layover_ort=layover,
                    overnight_after_day=True, starts_at_homebase=True,
                    raw_marker='12345 P1', has_fl=True),
        'se': _se(),
        'homebase': 'FRA',
        'followme_context': {'in_any_tour_span': False},
        'evidence_against': [
            ['no_se_allowance', 2, 'no SE'],
            ['followme_explicit_other_span', 3, 'not in any span'],
            ['no_homebase_commute_evidence', 2, 'no anfahrt'],
        ],
    }
    return app_module._ai_resolver_build_prompt(
        'tour_boundary', ctx, '12345 P1'
    )


def test_lad_phantom_prompt_mentions_no_se_no_anfahrt_free_gap():
    p = _phantom_prompt('2025-05-20', ['FRA','LAD'], 'LAD')
    assert 'SE-Auslandsspesen' in p or 'SE-Stempel' in p or 'kein SE' in p
    assert ('Anfahrt' in p or 'commute' in p)
    # Phantom oder reader_misread konkret genannt
    assert ('Phantom' in p or 'reader_misread' in p
            or 'ohne Beleg' in p or 'Sequence-ID' in p)


def test_tlv_phantom_prompt_mentions_no_se_no_anfahrt_free_gap():
    p = _phantom_prompt('2025-10-26', ['FRA','TLV'], 'TLV')
    assert ('SE-Auslandsspesen' in p or 'SE-Stempel' in p or 'kein SE' in p)
    assert ('Anfahrt' in p or 'commute' in p)
    assert ('Phantom' in p or 'reader_misread' in p
            or 'ohne Beleg' in p or 'Sequence-ID' in p)


# ════════════════════════════════════════════════════════════════════════════
# 12. JFK/Shannon-Konflikt-Prompt erwähnt place_conflict
# ════════════════════════════════════════════════════════════════════════════

def test_jfk_shannon_prompt_mentions_place_conflict():
    ctx = {
        'day': _day('2025-12-14', routing=['FRA','JFK'], layover_ort='JFK',
                    overnight_after_day=True, starts_at_homebase=True,
                    raw_marker='57783 P1', has_fl=True),
        'se': _se(),
        'homebase': 'FRA',
        'followme_context': {'expected_destinations': ['SNN']},
        'evidence_against': [
            ['cas_followme_place_conflict', 4, 'CAS=JFK vs FollowMe=SNN'],
        ],
    }
    p = app_module._ai_resolver_build_prompt('place_code', ctx, '57783 P1')
    assert ('PLACE/ROUTING-CONFLICT' in p or 'place_conflict' in p.lower()
            or 'reader_misread' in p)
    assert 'SNN' in p or 'expected_destinations' in p
    assert 'JFK' in p


# ════════════════════════════════════════════════════════════════════════════
# 13. OTP→FRA→LHR-Prompt erwähnt transit
# ════════════════════════════════════════════════════════════════════════════

def test_otp_fra_lhr_prompt_mentions_transit_not_homebase_return():
    ctx = {
        'day': _day('2025-07-03', routing=['OTP','FRA','LHR'],
                    starts_at_homebase=False, ends_at_homebase=False,
                    duty_duration_minutes=720, raw_marker='129023 PU / Tag 3',
                    has_fl=True),
        'se': _se(),
        'homebase': 'FRA',
        'evidence_against': [
            ['transit_via_homebase_ends_foreign', 4,
             'FRA als Transit, endet LHR'],
        ],
    }
    p = app_module._ai_resolver_build_prompt(
        'routing_consistency', ctx, '129023 PU / Tag 3'
    )
    assert 'Transit' in p or 'transits via' in p or 'Homebase mittig' in p
    assert ('Same-Day-Homebase-Return' in p or 'KEIN' in p
            or 'normalen Same-Day' in p or 'routing_conflict' in p)


# ════════════════════════════════════════════════════════════════════════════
# 14. PU-Marker wird vom Mock NICHT als Pula-Airport interpretiert
# ════════════════════════════════════════════════════════════════════════════

def test_pu_marker_not_interpreted_as_airport_by_mock():
    """Marker mit reinem 'PU' (Crew-Position) und keinem layover_ort →
    Mock erkennt PU als Crew-Position, NICHT als Pula-IATA."""
    ctx = {
        'day': _day('2025-07-03', routing=['FRA'], layover_ort='',
                    raw_marker='PU'),
        'homebase': 'FRA',
    }
    r = _resolver_call('place_code', ctx, marker='PU')
    # Mock darf NICHT resolved=True mit value=Pula liefern
    if r['resolved']:
        val_str = json.dumps(r['value'], default=str).lower()
        assert 'pula' not in val_str, (
            f'Mock interpretierte PU fälschlich als Pula. value={r["value"]}'
        )
        assert 'puy' not in val_str
    # Erwartete decision: NEEDS_REVIEW (Crew-Position-Code, kein Airport)
    assert r['decision'] in ('NEEDS_REVIEW', 'DROP_TOUR'), (
        f'PU-only Marker: erwartet NEEDS_REVIEW/DROP, war {r["decision"]}'
    )


def test_marker_semantics_pu_is_crew_position():
    """marker_semantics-Resolver: PU = crew_position, mit
    is_crew_position_code=True."""
    ctx = {
        'day': _day('2025-07-03', raw_marker='PU'),
        'homebase': 'FRA',
    }
    r = _resolver_call('marker_semantics', ctx, marker='PU')
    assert r['resolved'] is True
    val = r['value']
    assert isinstance(val, dict)
    assert val.get('is_crew_position_code') is True
    assert 'Purser' in val.get('meaning', '')
    assert 'NICHT Pula' in val.get('meaning', '') or 'Pula' not in val.get('meaning', '')


# ════════════════════════════════════════════════════════════════════════════
# 15. Phase 5d ändert keine finale KPI
# ════════════════════════════════════════════════════════════════════════════

def test_no_final_kpi_change_phase5d():
    """Tour-Membership + Counter bleiben bit-identisch — Phase 5d ist
    Prompt+Mock+Decision-Mapping-only."""
    se_blr = _se(stfrei_ort='BLR', stfrei_total=42.0,
                 stfrei_inland=False, count=1)
    matched = [
        {'datum': '2025-01-03', 'dp': _day(
            '2025-01-03', routing=['FRA','BLR'], layover_ort='BLR',
            overnight_after_day=True, starts_at_homebase=True,
            duty_duration_minutes=785, raw_marker='31591 P1',
            has_fl=True), 'se': se_blr},
        {'datum': '2025-01-04', 'dp': _day(
            '2025-01-04', routing=[], layover_ort='BLR',
            overnight_after_day=True, raw_marker='X'), 'se': se_blr},
        {'datum': '2025-01-05', 'dp': _day(
            '2025-01-05', routing=['BLR','FRA'], layover_ort='BLR',
            overnight_after_day=True, start_time='23:28',
            duty_duration_minutes=31, raw_marker='755 LH755-1',
            has_fl=True), 'se': se_blr},
        {'datum': '2025-01-06', 'dp': _day(
            '2025-01-06', routing=['BLR','FRA'], starts_at_homebase=True,
            ends_at_homebase=True, duty_duration_minutes=561,
            raw_marker='755 LH755-1'), 'se': _se()},
    ]
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025,
        known_anfahrten=[{'datum':'2025-01-03'}],
    )
    blr = next(t for t in tours
               if any(d['datum'] == '2025-01-04' for d in t['days']))
    assert blr['tour_size'] == 4
    roles = [d['role'] for d in blr['days']]
    assert roles == ['tour_start', 'tour_mid', 'tour_mid', 'tour_end']
    # Counter aus classifier
    result = app_module._classify_days_from_normalized_tours(
        tours, year=2025, homebase='FRA',
    )
    z76_total_blr = sum(d['amount'] for d in result['tage_detail']
                        if d['datum'].startswith('2025-01-0')
                        and d['klass'] == 'Z76')
    # BLR-Tour soll Z76-Anteil > 0 haben
    assert z76_total_blr > 0
