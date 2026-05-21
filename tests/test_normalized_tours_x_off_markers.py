"""BH-CORE-001 Tour-Boundary: X / == / OFF Marker Disambiguation.

Diese Marker sind kontextabhängig:
- innerhalb foreign tour: tour_mid (Hotel-Rest-Day)
- zuhause ohne Tour: non_tour, Frei

Phase-0-Status: RED — `_normalize_tours_from_raw_facts` existiert nicht.
"""
import os
import sys
import pytest
from unittest import mock

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import app as app_module


pytestmark = pytest.mark.skipif(
    not hasattr(app_module, '_normalize_tours_from_raw_facts'),
    reason='BH-CORE-001 Phase 1 noch nicht implementiert'
)


def _make_day(datum, **kwargs):
    dp = {
        'datum': datum,
        'activity_type': 'frei',
        'routing': [], 'layover_ort': '', 'overnight_after_day': False,
        'start_time': '', 'end_time': '', 'duty_duration_minutes': 0,
        'raw_marker': '', 'has_fl': False, 'is_workday': True,
        'requires_commute': False, 'starts_at_homebase': False,
        'ends_at_homebase': False, 'raw_lines': [], 'confidence': 0.9,
    }
    dp.update(kwargs)
    return {'datum': datum, 'dp': dp, 'se': {
        'stfrei_total': 0.0, 'stfrei_ort': '', 'stfrei_inland': None,
        'zwoelftel': 0, 'lines': [], 'count': 0,
    }}


def test_x_marker_with_iata_hint_is_tour_mid():
    """`X HKG` mit foreign layover → tour_mid, foreign_layover."""
    matched = [
        _make_day('2025-01-19', activity_type='tour', routing=['FRA','HKG'],
                  layover_ort='HKG', overnight_after_day=True,
                  starts_at_homebase=True, has_fl=True, raw_marker='42100 P1'),
        _make_day('2025-01-20', raw_marker='X HKG', layover_ort='HKG',
                  overnight_after_day=True),
        _make_day('2025-01-21', activity_type='tour', routing=['HKG','FRA'],
                  overnight_after_day=False, ends_at_homebase=True,
                  has_fl=True),
    ]
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025
    )
    tour = next((t for t in tours
                 if any(d['datum'] == '2025-01-20' for d in t['days'])), None)
    assert tour is not None
    day = next(d for d in tour['days'] if d['datum'] == '2025-01-20')
    assert day['role'] == 'tour_mid'
    assert day.get('location_context') == 'foreign_layover'


def test_double_equals_in_active_tour_is_tour_mid():
    """`==` mit Sandwich-Pattern (overnight-prev + overnight-next) → tour_mid."""
    matched = [
        _make_day('2025-07-22', activity_type='tour', routing=['FRA','GOT'],
                  layover_ort='GOT', overnight_after_day=True,
                  starts_at_homebase=True, has_fl=True),
        _make_day('2025-07-23', raw_marker='==', layover_ort='GOT',
                  overnight_after_day=True),
        _make_day('2025-07-24', activity_type='tour', routing=['GOT','FRA'],
                  overnight_after_day=False, ends_at_homebase=True,
                  has_fl=True),
    ]
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025
    )
    tour = next((t for t in tours
                 if any(d['datum'] == '2025-07-23' for d in t['days'])), None)
    assert tour is not None
    day = next(d for d in tour['days'] if d['datum'] == '2025-07-23')
    assert day['role'] == 'tour_mid'


def test_double_equals_at_home_without_tour_is_non_tour():
    """`==` ohne overnight-prev/next → non_tour, Frei."""
    matched = [
        _make_day('2025-03-25', activity_type='frei', raw_marker='ORTSTAG',
                  starts_at_homebase=True, ends_at_homebase=True),
        _make_day('2025-03-26', raw_marker='==',
                  starts_at_homebase=False, ends_at_homebase=False,
                  overnight_after_day=False),
        _make_day('2025-03-27', activity_type='frei', raw_marker='ORTSTAG',
                  starts_at_homebase=True, ends_at_homebase=True),
    ]
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025
    )
    # 03-26 sollte non_tour sein
    for tour in tours:
        for day in tour['days']:
            if day['datum'] == '2025-03-26':
                assert day['role'] == 'non_tour', (
                    f'== zuhause ohne Tour muss non_tour sein, war {day["role"]}'
                )


def test_off_marker_in_foreign_tour_is_tour_mid():
    """`OFF` mit prev.overnight=True + foreign layover → tour_mid."""
    matched = [
        _make_day('2025-06-16', activity_type='tour', routing=['FRA','LCA'],
                  layover_ort='LCA', overnight_after_day=True,
                  starts_at_homebase=True, has_fl=True),
        _make_day('2025-06-17', raw_marker='OFF', layover_ort='LCA',
                  overnight_after_day=True),
        _make_day('2025-06-18', raw_marker='OFF', layover_ort='LCA',
                  overnight_after_day=True),
        _make_day('2025-06-19', activity_type='tour', routing=['LCA','FRA'],
                  overnight_after_day=False, ends_at_homebase=True,
                  has_fl=True),
    ]
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025
    )
    tour = next((t for t in tours
                 if any(d['datum'] == '2025-06-17' for d in t['days'])), None)
    assert tour is not None
    for datum in ('2025-06-17', '2025-06-18'):
        day = next(d for d in tour['days'] if d['datum'] == datum)
        assert day['role'] == 'tour_mid', (
            f'{datum} OFF in foreign-tour-context muss tour_mid sein, '
            f'war {day["role"]}'
        )


def test_off_marker_at_home_is_non_tour():
    """`OFF` ohne Tour-Kontext → non_tour, Frei."""
    matched = [
        _make_day('2025-09-15', activity_type='frei', raw_marker='ORTSTAG',
                  starts_at_homebase=True, ends_at_homebase=True),
        _make_day('2025-09-16', raw_marker='OFF',
                  starts_at_homebase=False, ends_at_homebase=False,
                  overnight_after_day=False),
        _make_day('2025-09-17', activity_type='frei', raw_marker='==',
                  starts_at_homebase=True, ends_at_homebase=True),
    ]
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025
    )
    for tour in tours:
        for day in tour['days']:
            if day['datum'] == '2025-09-16':
                assert day['role'] == 'non_tour'


def test_x_marker_at_home_without_routing_is_non_tour():
    """`X` ohne routing + ohne overnight + zuhause → non_tour, Frei."""
    matched = [
        _make_day('2025-11-10', activity_type='frei', raw_marker='ORTSTAG',
                  starts_at_homebase=True, ends_at_homebase=True),
        _make_day('2025-11-11', raw_marker='X',
                  starts_at_homebase=False, ends_at_homebase=False,
                  overnight_after_day=False, layover_ort=''),
    ]
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025
    )
    for tour in tours:
        for day in tour['days']:
            if day['datum'] == '2025-11-11':
                assert day['role'] == 'non_tour'


@pytest.mark.skip(reason='BH-CORE-001 Phase 5: KI-Resolver-Integration aktiviert dies')
def test_ki_resolver_called_for_ambiguous_marker():
    """Unklarer Marker im potential-tour-Kontext → KI mit kind='tour_context'.

    Phase 1 ist deterministisch-only (hard-evidence + Sandwich-Pattern).
    KI-Resolver wird in Phase 5 integriert."""
    matched = [
        _make_day('2025-08-04', activity_type='tour', routing=['FRA','JFK'],
                  layover_ort='JFK', overnight_after_day=True,
                  starts_at_homebase=True, has_fl=True),
        _make_day('2025-08-05', raw_marker='UNKNOWN_MARKER',
                  overnight_after_day=True, layover_ort='JFK'),
        _make_day('2025-08-06', activity_type='tour', routing=['JFK','FRA'],
                  overnight_after_day=False, ends_at_homebase=True,
                  has_fl=True),
    ]
    ai_resolver = app_module._resolve_uncertain_fact_with_ai
    with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai',
                            side_effect=ai_resolver) as spy:
        app_module._normalize_tours_from_raw_facts(
            matched, homebase='FRA', year=2025
        )
    # Mindestens ein Call mit kind='tour_context'
    tour_context_calls = [
        c for c in spy.call_args_list
        if c.kwargs.get('kind') == 'tour_context'
    ]
    assert len(tour_context_calls) >= 1, (
        'KI-Resolver muss für unklare Marker im potential-tour-Kontext gerufen werden'
    )


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
