"""Konflikt C4 (aus AEROTAX_KNOWLEDGE_HARVEST.md §13):
RES + Inland-Übernachtung → Z73 (NICHT standby_hotel Z76).

Beleg: referenz_faelle.txt:636 — „RES/SBY in HAM/MUC mit Übernachtung
sind Z73-Kandidaten" (Inland-Schulung-Hotel).

KEIN Live-Call, nur Mock-Pfad.
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
    not hasattr(app_module, '_resolve_uncertain_fact_with_ai'),
    reason='Resolver missing',
)


@pytest.fixture(autouse=True)
def _force_mock(monkeypatch):
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


def _call(ctx, marker='RES'):
    return app_module._resolve_uncertain_fact_with_ai(
        kind='standby_context', context=ctx,
        uncertain_fact=marker, _force_mock=True,
    )


# ════════════════════════════════════════════════════════════════════════════
# C4: RES + Inland-Übernachtung → Z73-Kandidat, NICHT standby_hotel
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('inland_iata', ['MUC', 'HAM', 'DUS', 'STR',
                                          'CGN', 'BER', 'LEJ', 'NUE'])
def test_res_with_inland_prev_overnight_is_z73_candidate(inland_iata):
    """RES nach Inland-Übernachtung (HAM/MUC/etc.) → standby_inland_hotel.
    Decision NEEDS_REVIEW (kein automatischer Z76-Auto-Apply)."""
    ctx = {
        'day': _day('2025-04-15', raw_marker='RES'),
        'prev_day': _day('2025-04-14', routing=['FRA', inland_iata],
                         layover_ort=inland_iata, overnight_after_day=True,
                         starts_at_homebase=True,
                         duty_duration_minutes=420, has_fl=True),
        'homebase': 'FRA',
    }
    r = _call(ctx)
    assert r['resolved'] is True
    assert r['decision'] == 'NEEDS_REVIEW', (
        f'RES Inland {inland_iata}: erwartet NEEDS_REVIEW, war {r["decision"]}'
    )
    assert r['context_type'] == 'hotel_standby'
    # value muss inland_hotel signalisieren, NICHT standby_hotel
    val = r['value']
    if isinstance(val, dict):
        assert 'inland' in str(val.get('meaning', '')).lower() or \
               'Inland' in str(val.get('tax_hint', ''))
    else:
        assert 'inland' in str(val).lower()


def test_res_with_foreign_prev_overnight_remains_standby_hotel():
    """Foreign-prev-overnight bleibt standby_hotel KEEP_TOUR (Phase 5a-Pfad)."""
    ctx = {
        'day': _day('2025-04-23', raw_marker='RES'),
        'prev_day': _day('2025-04-22', routing=['FRA','ICN'],
                         layover_ort='ICN', overnight_after_day=True,
                         starts_at_homebase=True,
                         duty_duration_minutes=720, has_fl=True),
        'homebase': 'FRA',
    }
    r = _call(ctx)
    assert r['resolved'] is True
    assert r['decision'] == 'KEEP_TOUR'
    assert r['context_type'] == 'hotel_standby'
    assert r['value'] == 'standby_hotel'


def test_res_homebase_remains_standby_home():
    """RES ohne prev-overnight bleibt standby_home DROP_TOUR."""
    ctx = {
        'day': _day('2025-03-11', raw_marker='RES',
                    starts_at_homebase=True, ends_at_homebase=True),
        'homebase': 'FRA',
    }
    r = _call(ctx)
    assert r['decision'] == 'DROP_TOUR'
    assert r['context_type'] == 'homebase_standby'


def test_res_inland_z73_evidence_cites_referenz_faelle():
    """Audit-Trail muss die referenz_faelle-Quelle nennen."""
    ctx = {
        'day': _day('2025-09-26', raw_marker='RES'),
        'prev_day': _day('2025-09-25', routing=['FRA','MUC'],
                         layover_ort='MUC', overnight_after_day=True),
        'homebase': 'FRA',
    }
    r = _call(ctx)
    ev_joined = ' '.join(str(e) for e in r['evidence'])
    assert 'referenz_faelle' in ev_joined or 'rule:' in ev_joined.lower()
