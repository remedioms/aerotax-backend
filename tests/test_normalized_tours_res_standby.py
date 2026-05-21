"""BH-CORE-001 Tour-Boundary: RES-Marker Disambiguation.

RES innerhalb foreign tour (Hotel-Standby) vs RES zuhause (Standby-zuhause).

Phase-0-Status: RED — `_normalize_tours_from_raw_facts` existiert nicht.
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
    not hasattr(app_module, '_normalize_tours_from_raw_facts'),
    reason='BH-CORE-001 Phase 1 noch nicht implementiert'
)


def _make_day(datum, **kwargs):
    dp = {
        'datum': datum,
        'activity_type': 'standby',
        'routing': [], 'layover_ort': '', 'overnight_after_day': False,
        'start_time': '', 'end_time': '', 'duty_duration_minutes': 0,
        'raw_marker': 'RES', 'has_fl': False, 'is_workday': True,
        'requires_commute': False, 'starts_at_homebase': False,
        'ends_at_homebase': False, 'raw_lines': [], 'confidence': 0.9,
    }
    dp.update(kwargs)
    return {'datum': datum, 'dp': dp, 'se': {
        'stfrei_total': 0.0, 'stfrei_ort': '', 'stfrei_inland': None,
        'zwoelftel': 0, 'lines': [], 'count': 0,
    }}


def test_res_in_foreign_tour_is_standby_hotel():
    """RES + prev.overnight=True + foreign layover → is_standby_hotel=True."""
    matched = [
        _make_day('2025-04-22', activity_type='tour', routing=['FRA','ICN'],
                  layover_ort='ICN', overnight_after_day=True,
                  starts_at_homebase=True, raw_marker='42000 P1', has_fl=True),
        _make_day('2025-04-23', raw_marker='RES', layover_ort='ICN',
                  overnight_after_day=True),
        _make_day('2025-04-24', raw_marker='RES', layover_ort='ICN',
                  overnight_after_day=True),
        _make_day('2025-04-25', activity_type='tour', routing=['ICN','FRA'],
                  layover_ort='', overnight_after_day=False,
                  ends_at_homebase=True, has_fl=True),
    ]
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025
    )
    tour = next((t for t in tours
                 if any(d['datum'] == '2025-04-23' for d in t['days'])), None)
    assert tour is not None, 'RES-Tour nicht erkannt'
    day_23 = next(d for d in tour['days'] if d['datum'] == '2025-04-23')
    assert day_23.get('is_standby_hotel') is True, (
        f'04-23 RES + prev.overnight=ICN-foreign muss is_standby_hotel=True sein, '
        f'is_standby_hotel={day_23.get("is_standby_hotel")}'
    )


def test_res_at_homebase_is_standby_homebase():
    """RES + kein overnight + homebase context → is_standby_homebase=True, non_tour."""
    matched = [
        _make_day('2025-02-04', activity_type='frei', raw_marker='ORTSTAG',
                  starts_at_homebase=True, ends_at_homebase=True),
        _make_day('2025-02-05', raw_marker='RES',
                  starts_at_homebase=True, ends_at_homebase=True,
                  overnight_after_day=False),
        _make_day('2025-02-06', activity_type='frei', raw_marker='==',
                  starts_at_homebase=True, ends_at_homebase=True),
    ]
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025
    )
    # Tag 02-05 sollte als non_tour markiert sein
    for tour in tours:
        for day in tour['days']:
            if day['datum'] == '2025-02-05':
                assert day['role'] == 'non_tour', (
                    f'RES zuhause muss role=non_tour sein, war {day["role"]}'
                )
                assert day.get('is_standby_homebase') is True


def test_res_korea_4_days_single_tour():
    """04-23 bis 04-26: Korea-Tour, alle 4 Tage in 1 Tour."""
    matched = [
        _make_day('2025-04-22', activity_type='tour', routing=['FRA','ICN'],
                  layover_ort='ICN', overnight_after_day=True,
                  starts_at_homebase=True, raw_marker='42000 P1', has_fl=True),
        _make_day('2025-04-23', raw_marker='RES', layover_ort='ICN',
                  overnight_after_day=True),
        _make_day('2025-04-24', raw_marker='RES', layover_ort='ICN',
                  overnight_after_day=True),
        _make_day('2025-04-25', raw_marker='RES', layover_ort='ICN',
                  overnight_after_day=True),
        _make_day('2025-04-26', activity_type='tour', routing=['ICN','FRA'],
                  overnight_after_day=False, ends_at_homebase=True,
                  raw_marker='42001 P1', has_fl=True),
    ]
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025
    )
    tour = next((t for t in tours
                 if any(d['datum'] == '2025-04-24' for d in t['days'])), None)
    assert tour is not None
    assert tour['tour_size'] == 5, (
        f'Korea-Tour mit RES-Mitte muss 5 Tage haben, hat {tour["tour_size"]}'
    )


def test_res_korea_destination_resolved():
    """primary_destination muss ICN/Republik Korea sein."""
    matched = [
        _make_day('2025-04-22', activity_type='tour', routing=['FRA','ICN'],
                  layover_ort='ICN', overnight_after_day=True,
                  starts_at_homebase=True, has_fl=True),
        _make_day('2025-04-23', raw_marker='RES', layover_ort='ICN',
                  overnight_after_day=True),
    ]
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025
    )
    tour = next((t for t in tours
                 if any(d['datum'] == '2025-04-22' for d in t['days'])), None)
    assert tour and tour.get('primary_destination') == 'ICN'


def test_res_alone_at_homebase_not_tour():
    """Standalone RES ohne foreign context → role=non_tour."""
    matched = [
        _make_day('2025-03-15', raw_marker='RES',
                  starts_at_homebase=True, ends_at_homebase=True),
    ]
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025
    )
    # entweder es entsteht keine Tour, oder eine 1-Day-Tour mit role=non_tour
    for tour in tours:
        for day in tour['days']:
            if day['datum'] == '2025-03-15':
                assert day['role'] == 'non_tour'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
