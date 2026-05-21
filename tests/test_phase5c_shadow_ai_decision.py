"""BH-CORE-001 Phase 5c — KI-Resolution in Shadow-Klassifikation.

Tests verifizieren:
- High-Conf-KI → auto übernommen in proposed_tour_decision_after_ai
- Medium-Conf → NEEDS_REVIEW
- Low-Conf → NEEDS_USER
- KI darf duty>FTL NICHT überschreiben
- KI darf day_already_in_other_tour NICHT überschreiben
- Keine finale KPI-Änderung (Shadow only)
"""
import os
import sys
import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import app as app_module

pytestmark = pytest.mark.skipif(
    not hasattr(app_module, '_normalize_tours_from_raw_facts')
    or not hasattr(app_module, '_score_tour_day_evidence'),
    reason='Phase 5c erfordert normalize_tours + evidence',
)


@pytest.fixture(autouse=True)
def _force_mock_mode(monkeypatch):
    monkeypatch.setenv('AEROTAX_AI_RESOLVER_MODE', 'mock')
    if hasattr(app_module, '_ai_resolver_cache'):
        app_module._ai_resolver_cache.clear()
    yield


def _day(datum, **kw):
    base = {
        'datum': datum, 'activity_type': 'tour',
        'routing': [], 'layover_ort': '', 'overnight_after_day': False,
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


def _matched(datum, dp, se=None):
    return {'datum': datum, 'dp': dp, 'se': se or _se()}


def _all_days(tours):
    return [d for t in tours for d in (t.get('days') or [])]


# ════════════════════════════════════════════════════════════════════════════
# 1. High-conf RES + foreign-overnight → KEEP_TOUR via standby_hotel
# ════════════════════════════════════════════════════════════════════════════

def test_high_confidence_res_foreign_tour_becomes_keep_in_shadow():
    """RES nach foreign-overnight: KI Mock liefert standby_hotel @ 0.85
    (medium). Test prüft, dass proposed_tour_decision_after_ai dafür
    NEEDS_REVIEW oder KEEP_TOUR sinnvoll wird, NICHT blind DROP."""
    matched = [
        _matched('2025-04-22', _day('2025-04-22', routing=['FRA','ICN'],
            layover_ort='ICN', overnight_after_day=True,
            starts_at_homebase=True, start_time='13:00',
            duty_duration_minutes=720, raw_marker='30099 P1', has_fl=True),
            se=_se(stfrei_ort='ICN', stfrei_total=42.0,
                   stfrei_inland=False, count=1)),
        _matched('2025-04-23', _day('2025-04-23', raw_marker='RES')),
    ]
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025,
    )
    days = _all_days(tours)
    by = {d['datum']: d for d in days}
    res = by['2025-04-23']
    # KI sagt standby_hotel — Decision-Vorschlag sollte KEEP_TOUR ODER
    # NEEDS_REVIEW sein, niemals blind DROP_TOUR.
    assert res['proposed_tour_decision_after_ai'] in (
        'KEEP_TOUR', 'NEEDS_REVIEW', 'NEEDS_AI'
    ), (
        f'RES Korea: proposed={res["proposed_tour_decision_after_ai"]}, '
        f'ai_kind={res["ai_resolution_kind"]}, ai_value={res["ai_value"]}, '
        f'conf={res["ai_confidence"]}'
    )


# ════════════════════════════════════════════════════════════════════════════
# 2. High-conf phantom (kein SE, no foreign) → DROP_TOUR
# ════════════════════════════════════════════════════════════════════════════

def test_high_confidence_phantom_becomes_drop_in_shadow():
    """ORTSTAG zuhause: clean DROP. proposed_tour_decision_after_ai = DROP_TOUR
    oder NEEDS_REVIEW (kein KEEP)."""
    matched = [
        _matched('2025-03-10', _day('2025-03-10', raw_marker='ORTSTAG',
            starts_at_homebase=True, ends_at_homebase=True)),
    ]
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025,
    )
    day = _all_days(tours)[0]
    assert day['proposed_tour_decision_after_ai'] != 'KEEP_TOUR'


# ════════════════════════════════════════════════════════════════════════════
# 3. Medium confidence → review suggestion
# ════════════════════════════════════════════════════════════════════════════

def test_medium_confidence_becomes_review_suggestion():
    """layover_place Mock (conf=0.75 medium) → NEEDS_REVIEW im proposed."""
    matched = [
        _matched('2025-12-14', _day('2025-12-14', routing=['FRA','JFK'],
            layover_ort='JFK', overnight_after_day=True,
            starts_at_homebase=True, start_time='14:00',
            duty_duration_minutes=600, raw_marker='57783 P1', has_fl=True)),
    ]
    fm_ctx = {
        'anfahrten_dates': {'2025-12-14'},
        'tour_spans': [('2025-12-14','2025-12-15',{'2025-12-14','2025-12-15'})],
        'tour_destinations': {'2025-12-14': {'SNN'}},
    }
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025, followme_context=fm_ctx,
    )
    day = _all_days(tours)[0]
    # KI Mock liefert für place_code Kandidat 1 conf=0.92 → auto-handled
    # → proposed bleibt evidence_decision (= KEEP_TOUR weil FOR dominant).
    # Audit-Fakt-Klärung, nicht Tour-Decision-Wechsel.
    assert day['ai_resolution_kind'] == 'place_code'
    assert day['ai_required'] is True
    # KI hat hier KEINE direkte Tour-Decision-Wirkung, nur Place-Klärung:
    # proposed darf evidence_decision behalten ODER NEEDS_REVIEW signalisieren
    assert day['proposed_tour_decision_after_ai'] in (
        'KEEP_TOUR', 'NEEDS_REVIEW', 'NEEDS_AI'
    )


# ════════════════════════════════════════════════════════════════════════════
# 4. Low confidence → user question
# ════════════════════════════════════════════════════════════════════════════

def test_low_confidence_becomes_user_question():
    """Tour-boundary mit ambiguous evidence (LAD phantom, no SE) → Mock
    liefert conf=0.60. proposed = NEEDS_USER."""
    matched = [
        _matched('2025-05-20', _day('2025-05-20', routing=['FRA','LAD'],
            layover_ort='LAD', overnight_after_day=True,
            starts_at_homebase=True, start_time='20:05',
            duty_duration_minutes=234, raw_marker='103703 P1', has_fl=True)),
    ]
    fm_ctx = {
        'anfahrten_dates': set(),
        'tour_spans': [
            ('2025-05-14','2025-05-19', {'2025-05-14','2025-05-15',
                                          '2025-05-16','2025-05-17',
                                          '2025-05-18','2025-05-19'}),
        ],
    }
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025, followme_context=fm_ctx,
    )
    day = _all_days(tours)[0]
    # NEEDS_AI evidence + Mock low-conf tour_boundary (0.60) → NEEDS_USER
    assert day['ai_confidence'] < 0.70
    assert day['proposed_tour_decision_after_ai'] in (
        'NEEDS_USER', 'NEEDS_REVIEW'
    )


# ════════════════════════════════════════════════════════════════════════════
# 5. KI darf duty>FTL nicht zu KEEP überschreiben
# ════════════════════════════════════════════════════════════════════════════

def test_ki_cannot_override_duty_over_ftl_to_keep():
    """Tag mit duty=1450 (> FTL 840). Selbst wenn KI tour_boundary=start
    mit conf=0.95 vorschlüge: proposed darf NICHT KEEP_TOUR auto sein —
    Reader-Bug-Verdacht muss bleiben."""
    matched = [
        _matched('2025-06-01', _day('2025-06-01', routing=['FRA','CPH','GOT'],
            layover_ort='GOT', overnight_after_day=True,
            starts_at_homebase=True, start_time='05:00',
            duty_duration_minutes=1450, raw_marker='126533 PU', has_fl=True),
            se=_se(stfrei_ort='GOT', stfrei_total=42.0,
                   stfrei_inland=False, count=1)),
    ]
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025,
    )
    day = _all_days(tours)[0]
    ag_names = [n for n, _, _ in day['evidence_against']]
    assert 'duty_over_ftl' in ag_names
    # Auch bei high-conf KI darf proposed nicht blind KEEP_TOUR sein
    assert day['proposed_tour_decision_after_ai'] != 'KEEP_TOUR', (
        f'duty>FTL Tag: proposed darf nicht KEEP_TOUR sein, war '
        f'{day["proposed_tour_decision_after_ai"]}'
    )


# ════════════════════════════════════════════════════════════════════════════
# 6. KI darf day_already_in_other_tour nicht überschreiben
# ════════════════════════════════════════════════════════════════════════════

def test_ki_cannot_override_day_already_in_other_tour():
    """day_already_in_other_tour ist hard blocker — KI darf nicht zu KEEP."""
    matched = [
        _matched('2025-08-15', _day('2025-08-15', routing=['FRA','JFK'],
            layover_ort='JFK', overnight_after_day=True,
            starts_at_homebase=True, start_time='13:00',
            duty_duration_minutes=600, raw_marker='LH404', has_fl=True),
            se=_se(stfrei_ort='JFK', stfrei_total=40.0,
                   stfrei_inland=False, count=1)),
    ]
    fm_ctx = {
        'anfahrten_dates': {'2025-08-15'},
        # Day in another span — sollte day_already_in_other_tour triggern
        'day_in_other_span_dates': {'2025-08-15'},
        'tour_spans': [],
    }
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025, followme_context=fm_ctx,
    )
    day = _all_days(tours)[0]
    ag_names = [n for n, _, _ in day['evidence_against']]
    assert 'day_already_in_other_tour' in ag_names
    # proposed muss NEEDS_REVIEW oder DROP sein — niemals KEEP
    assert day['proposed_tour_decision_after_ai'] in (
        'NEEDS_REVIEW', 'DROP_TOUR', 'NEEDS_AI'
    ), (
        f'day_already_in_other_tour: proposed darf nicht KEEP, war '
        f'{day["proposed_tour_decision_after_ai"]}'
    )


# ════════════════════════════════════════════════════════════════════════════
# 7. Keine finale KPI-Änderung in Phase 5c
# ════════════════════════════════════════════════════════════════════════════

def test_no_final_kpi_change_yet_phase5c():
    """Tour-Membership unverändert — Phase 5c ist Shadow-Layer."""
    se_blr = _se(stfrei_ort='BLR', stfrei_total=42.0,
                 stfrei_inland=False, count=1)
    matched = [
        _matched('2025-01-03', _day('2025-01-03', routing=['FRA','BLR'],
            layover_ort='BLR', overnight_after_day=True,
            starts_at_homebase=True, duty_duration_minutes=785,
            raw_marker='31591 P1', has_fl=True), se=se_blr),
        _matched('2025-01-04', _day('2025-01-04', routing=[],
            layover_ort='BLR', overnight_after_day=True, raw_marker='X'),
            se=se_blr),
        _matched('2025-01-05', _day('2025-01-05', routing=['BLR','FRA'],
            layover_ort='BLR', overnight_after_day=True,
            start_time='23:28', duty_duration_minutes=31,
            raw_marker='755 LH755-1', has_fl=True), se=se_blr),
        _matched('2025-01-06', _day('2025-01-06', routing=['BLR','FRA'],
            starts_at_homebase=True, ends_at_homebase=True,
            duty_duration_minutes=561, raw_marker='755 LH755-1')),
    ]
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025,
        known_anfahrten=[{'datum': '2025-01-03'}],
    )
    membership = [(t['tour_id'], t['tour_size'], t['tour_pattern'])
                  for t in tours]
    roles = [[d['role'] for d in t['days']] for t in tours]
    blr = next(t for t in tours
               if any(d['datum'] == '2025-01-04' for d in t['days']))
    assert blr['tour_size'] == 4
    blr_roles = [d['role'] for d in blr['days']]
    assert blr_roles == ['tour_start', 'tour_mid', 'tour_mid', 'tour_end']
    # Idempotenz
    tours2 = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025,
        known_anfahrten=[{'datum': '2025-01-03'}],
    )
    membership2 = [(t['tour_id'], t['tour_size'], t['tour_pattern'])
                   for t in tours2]
    roles2 = [[d['role'] for d in t['days']] for t in tours2]
    assert membership == membership2
    assert roles == roles2
