"""BH-CORE-001 Phase 5a.1 — Known Cross-Source-Conflict → ai_required.

Auch KEEP_TOUR-Tage müssen Resolver triggern, wenn echte Konflikte vorhanden:
- cas_followme_place_conflict (CAS-Place widerspricht FollowMe-Place)
- routing_inconsistent
- transit_via_homebase_ends_foreign
- routing_ends_foreign_at_claimed_return
- day_suffix_claims_completed_prev
- day_already_in_other_tour

Mock-Resolver-Modus ausschließlich — kein Anthropic-Call.
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
    reason='Phase 5a.1 erfordert normalize_tours + evidence',
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
# 1. 2025-12-14 JFK vs FollowMe-Ireland-Shannon → ai_required
# ════════════════════════════════════════════════════════════════════════════

def test_1214_jfk_vs_ireland_conflict_requires_ai():
    """Bekannter Tibor-Fall: CAS-Reader sieht FRA→JFK, FollowMe-Golden hat
    Tour 51 nach Irland/Shannon. Place-Conflict → ai_required=True selbst
    wenn evidence_decision KEEP_TOUR bleibt. Resolver-Kind=place_code."""
    matched = [
        _matched('2025-12-14', _day(
            '2025-12-14', routing=['FRA','JFK'], layover_ort='JFK',
            overnight_after_day=True, starts_at_homebase=True,
            start_time='14:00', duty_duration_minutes=600,
            raw_marker='57783 P1', has_fl=True),
            se=_se()),
    ]
    fm_ctx = {
        'anfahrten_dates': {'2025-12-14'},
        'tour_spans': [
            ('2025-12-14', '2025-12-15', {'2025-12-14', '2025-12-15'}),
        ],
        # FollowMe-Golden: Tour 51 = Ireland/Shannon
        'tour_destinations': {
            '2025-12-14': {'SNN'},   # Shannon (IRL)
            '2025-12-15': {'SNN'},
        },
    }
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025, followme_context=fm_ctx,
    )
    day = _all_days(tours)[0]
    # Evidence-Decision kann KEEP_TOUR oder NEEDS_AI sein (FollowMe-place
    # disagree count gegen FOR-Score). Pflicht: ai_required=True.
    assert day['ai_required'] is True, (
        f'12-14 JFK vs Shannon: ai_required muss True sein, war False '
        f'(decision={day["evidence_decision"]})'
    )
    # cas_followme_place_conflict muss in evidence_against sein
    ag_names = [n for n, _, _ in day['evidence_against']]
    assert 'cas_followme_place_conflict' in ag_names, (
        f'place_conflict-Evidence fehlt: {ag_names}'
    )
    # Resolver wurde getriggert mit Kind=place_code
    assert day['ai_resolution_kind'] == 'place_code', (
        f'Resolver-Kind muss place_code sein, war {day["ai_resolution_kind"]}'
    )


# ════════════════════════════════════════════════════════════════════════════
# 2. Generelle Regel: KEEP_TOUR mit Place-Conflict → AI-Resolution
# ════════════════════════════════════════════════════════════════════════════

def test_keep_tour_with_place_conflict_gets_ai_resolution():
    """Beliebige KEEP_TOUR-Konstellation + cas_followme_place_conflict
    → ai_required=True + Resolver-Aufruf mit kind=place_code."""
    matched = [
        _matched('2025-06-10', _day(
            '2025-06-10', routing=['FRA','SIN'], layover_ort='SIN',
            overnight_after_day=True, starts_at_homebase=True,
            start_time='11:00', duty_duration_minutes=720,
            raw_marker='LH778', has_fl=True),
            se=_se(stfrei_ort='SIN', stfrei_total=44.0,
                   stfrei_inland=False, count=1)),
    ]
    # FollowMe sagt: dieser Tag gehört zu Tour Bangkok (BKK), nicht SIN
    fm_ctx = {
        'anfahrten_dates': {'2025-06-10'},
        'tour_spans': [
            ('2025-06-10', '2025-06-13', {'2025-06-10','2025-06-11',
                                           '2025-06-12','2025-06-13'}),
        ],
        'tour_destinations': {
            '2025-06-10': {'BKK'},
            '2025-06-11': {'BKK'},
            '2025-06-12': {'BKK'},
            '2025-06-13': {'BKK'},
        },
    }
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025, followme_context=fm_ctx,
    )
    day = _all_days(tours)[0]
    assert day['ai_required'] is True
    assert day['ai_resolution_kind'] == 'place_code'


# ════════════════════════════════════════════════════════════════════════════
# 3. Saubere KEEP_TOUR ohne Konflikt → kein AI-Call
# ════════════════════════════════════════════════════════════════════════════

def test_clear_keep_tour_without_conflict_no_ai():
    """KEEP_TOUR mit SE-foreign UND FollowMe-place stimmt überein
    → ai_required=False, kein Resolver-Aufruf."""
    matched = [
        _matched('2025-06-10', _day(
            '2025-06-10', routing=['FRA','SIN'], layover_ort='SIN',
            overnight_after_day=True, starts_at_homebase=True,
            start_time='11:00', duty_duration_minutes=720,
            raw_marker='LH778', has_fl=True),
            se=_se(stfrei_ort='SIN', stfrei_total=44.0,
                   stfrei_inland=False, count=1)),
    ]
    fm_ctx = {
        'anfahrten_dates': {'2025-06-10'},
        'tour_spans': [
            ('2025-06-10', '2025-06-13', {'2025-06-10','2025-06-11',
                                           '2025-06-12','2025-06-13'}),
        ],
        'tour_destinations': {
            '2025-06-10': {'SIN'},   # FollowMe und CAS einig
        },
    }
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025, followme_context=fm_ctx,
    )
    day = _all_days(tours)[0]
    assert day['evidence_decision'] == 'KEEP_TOUR'
    assert day['ai_required'] is False, (
        f'Saubere KEEP_TOUR ohne Konflikt: ai_required muss False sein, '
        f'evidence_against={[n for n,_,_ in day["evidence_against"]]}'
    )
    assert day['ai_resolution_kind'] == ''


# ════════════════════════════════════════════════════════════════════════════
# 4. Bangalore — alle 4 Tage KEEP ohne ai_required
# ════════════════════════════════════════════════════════════════════════════

def test_bangalore_keep_no_ai_required():
    """Bangalore-Tour mit SE-Foreign-Stempeln + FollowMe-place=BLR übereinstimmt
    → alle 4 Tage KEEP_TOUR und ai_required=False."""
    se_blr = _se(stfrei_ort='BLR', stfrei_total=42.0,
                 stfrei_inland=False, count=1)
    matched = [
        _matched('2025-01-03', _day('2025-01-03', routing=['FRA','BLR'],
            layover_ort='BLR', overnight_after_day=True,
            starts_at_homebase=True, start_time='10:55',
            duty_duration_minutes=785, raw_marker='31591 P1', has_fl=True),
            se=se_blr),
        _matched('2025-01-04', _day('2025-01-04', routing=[],
            layover_ort='BLR', overnight_after_day=True, raw_marker='X'),
            se=se_blr),
        _matched('2025-01-05', _day('2025-01-05', routing=['BLR','FRA'],
            layover_ort='BLR', overnight_after_day=True,
            start_time='23:28', duty_duration_minutes=31,
            raw_marker='755 LH755-1', has_fl=True),
            se=se_blr),
        _matched('2025-01-06', _day('2025-01-06', routing=['BLR','FRA'],
            starts_at_homebase=True, ends_at_homebase=True,
            start_time='00:00', duty_duration_minutes=561,
            raw_marker='755 LH755-1')),
    ]
    fm_ctx = {
        'anfahrten_dates': {'2025-01-03'},
        'tour_spans': [
            ('2025-01-03', '2025-01-06', {'2025-01-03','2025-01-04',
                                          '2025-01-05','2025-01-06'}),
        ],
        'tour_destinations': {
            '2025-01-03': {'BLR'},
            '2025-01-04': {'BLR'},
            '2025-01-05': {'BLR'},
            '2025-01-06': {'BLR'},
        },
    }
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025, followme_context=fm_ctx,
    )
    days = _all_days(tours)
    for d in days:
        if d['datum'] in ('2025-01-03','2025-01-04','2025-01-05','2025-01-06'):
            assert d['evidence_decision'] == 'KEEP_TOUR', (
                f'BLR {d["datum"]}: KEEP erwartet, war {d["evidence_decision"]}'
            )
            assert d['ai_required'] is False, (
                f'BLR {d["datum"]}: ai_required muss False sein, '
                f'evidence_against={[n for n,_,_ in d["evidence_against"]]}'
            )


# ════════════════════════════════════════════════════════════════════════════
# 5. RES Korea KEEP — Standby-Context-Resolver KANN aktiv sein, muss aber
#     nicht via ai_required (Continuation-Evidence gibt KEEP)
# ════════════════════════════════════════════════════════════════════════════

def test_res_korea_keep_can_still_request_standby_context_if_marker_res():
    """RES nach foreign-overnight: evidence_decision=KEEP_TOUR durch
    continuation_from_prev_tour. Wenn FollowMe-context Place stimmt,
    kein cas_followme_place_conflict, ai_required=False. Phase 5b kann
    bei Bedarf standby_context via expliziter Kandidaten-Liste anfragen."""
    matched = [
        _matched('2025-04-22', _day('2025-04-22', routing=['FRA','ICN'],
            layover_ort='ICN', overnight_after_day=True,
            starts_at_homebase=True, start_time='13:00',
            duty_duration_minutes=720, raw_marker='30099 P1', has_fl=True),
            se=_se(stfrei_ort='ICN', stfrei_total=42.0,
                   stfrei_inland=False, count=1)),
        _matched('2025-04-23', _day('2025-04-23', raw_marker='RES')),
    ]
    fm_ctx = {
        'anfahrten_dates': {'2025-04-22'},
        'tour_spans': [
            ('2025-04-22', '2025-04-26', {'2025-04-22','2025-04-23',
                                          '2025-04-24','2025-04-25',
                                          '2025-04-26'}),
        ],
        'tour_destinations': {
            '2025-04-22': {'ICN'},
            '2025-04-23': {'ICN'},
        },
    }
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025, followme_context=fm_ctx,
    )
    days = _all_days(tours)
    by = {d['datum']: d for d in days}
    res_day = by['2025-04-23']
    # FollowMe place stimmt mit prev_layover → kein Place-Conflict
    ag_names = [n for n, _, _ in res_day['evidence_against']]
    assert 'cas_followme_place_conflict' not in ag_names, (
        f'RES mit FollowMe-konsistentem prev_layover: kein place_conflict; '
        f'against={ag_names}'
    )
    # Wenn evidence_decision KEEP_TOUR ist und kein harter Konflikt vorliegt,
    # ist ai_required=False
    if res_day['evidence_decision'] == 'KEEP_TOUR':
        assert res_day['ai_required'] is False or \
               res_day['ai_resolution_kind'] in ('standby_context', '')


# ════════════════════════════════════════════════════════════════════════════
# 6. Klare DROP_TOUR-Tage → kein AI-Call
# ════════════════════════════════════════════════════════════════════════════

def test_no_ai_for_clean_drop_tour():
    """ORTSTAG/FRS/LMN zuhause: FOR=0, AGAINST hoch → DROP_TOUR + ai_required=False."""
    matched = [
        _matched('2025-03-10', _day('2025-03-10', raw_marker='ORTSTAG',
            starts_at_homebase=True, ends_at_homebase=True)),
        _matched('2025-08-19', _day('2025-08-19', raw_marker='FRS',
            starts_at_homebase=True, ends_at_homebase=True)),
        _matched('2025-04-02', _day('2025-04-02', raw_marker='LMN_AS',
            starts_at_homebase=True, ends_at_homebase=True)),
    ]
    fm_ctx = {
        'anfahrten_dates': set(),
        'tour_spans': [
            ('2025-09-01', '2025-09-04', {'2025-09-01','2025-09-02',
                                          '2025-09-03','2025-09-04'}),
        ],
    }
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025, followme_context=fm_ctx,
    )
    for d in _all_days(tours):
        if d['datum'] in ('2025-03-10', '2025-08-19', '2025-04-02'):
            assert d['evidence_decision'] in ('DROP_TOUR', 'NEEDS_AI'), (
                f'{d["datum"]}: erwartet DROP/NEEDS_AI, war {d["evidence_decision"]}'
            )
            if d['evidence_decision'] == 'DROP_TOUR':
                assert d['ai_required'] is False, (
                    f'Clean DROP {d["datum"]}: ai_required muss False sein'
                )


# ════════════════════════════════════════════════════════════════════════════
# 7. KEINE finale KPI-Änderung durch Phase 5a.1
# ════════════════════════════════════════════════════════════════════════════

def test_no_final_kpi_change_phase5a1():
    """Tour-Membership (tour_id, tour_size, tour_pattern, roles) bleiben
    bit-identisch — Phase 5a.1 nur Audit-Layer."""
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
    fm_ctx = {
        'anfahrten_dates': {'2025-01-03'},
        'tour_spans': [
            ('2025-01-03', '2025-01-06', {'2025-01-03','2025-01-04',
                                          '2025-01-05','2025-01-06'}),
        ],
        'tour_destinations': {
            '2025-01-03': {'BLR'}, '2025-01-04': {'BLR'},
            '2025-01-05': {'BLR'}, '2025-01-06': {'BLR'},
        },
    }
    tours1 = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025, followme_context=fm_ctx,
    )
    tours2 = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025, followme_context=fm_ctx,
    )
    membership1 = [(t['tour_id'], t['tour_size'], t['tour_pattern'])
                   for t in tours1]
    membership2 = [(t['tour_id'], t['tour_size'], t['tour_pattern'])
                   for t in tours2]
    roles1 = [[d['role'] for d in t['days']] for t in tours1]
    roles2 = [[d['role'] for d in t['days']] for t in tours2]
    assert membership1 == membership2
    assert roles1 == roles2
    # Bangalore-Tour muss 4-day-Membership behalten
    blr = next(t for t in tours1
               if any(d['datum'] == '2025-01-04' for d in t['days']))
    assert blr['tour_size'] == 4
    blr_roles = [d['role'] for d in blr['days']]
    assert blr_roles == ['tour_start', 'tour_mid', 'tour_mid', 'tour_end']
