"""BH-CORE-001 Tour-Boundary: Bangalore-Tour 01-03 bis 01-06.

Erwartung (nach BH-CORE-001):
- 4 Tage = 1 Tour (nicht gesplittet durch X-Marker)
- 01-04 X = tour_mid, location=foreign_layover, NICHT non_tour
- destination = BLR (Indien-Bangalore)

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
    """Bangalore-Tour-Fixture-Tage im matched-day schema."""
    dp = {
        'datum': datum,
        'activity_type': 'tour',
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


def _bangalore_tour():
    """Repliziert Tibor's Bangalore-Tour aus tibor_aerotax_v11_raw_initial fixture."""
    return [
        _make_day('2025-01-03',
                  activity_type='tour', routing=['FRA', 'BLR'],
                  layover_ort='BLR', overnight_after_day=True,
                  starts_at_homebase=True, ends_at_homebase=False,
                  start_time='10:55', end_time='23:59',
                  duty_duration_minutes=785, raw_marker='31591 P1',
                  has_fl=True, requires_commute=True),
        _make_day('2025-01-04',
                  activity_type='frei',  # Reader heuristic — wird durch Tour-Layer korrigiert
                  routing=[], layover_ort='BLR', overnight_after_day=True,
                  starts_at_homebase=False, ends_at_homebase=False,
                  raw_marker='X'),
        _make_day('2025-01-05',
                  activity_type='tour', routing=['BLR', 'FRA'],
                  layover_ort='BLR', overnight_after_day=True,
                  starts_at_homebase=False, ends_at_homebase=False,
                  start_time='23:28', end_time='23:59',
                  duty_duration_minutes=31, raw_marker='755 LH755-1',
                  has_fl=True, requires_commute=False),
        _make_day('2025-01-06',
                  activity_type='same_day', routing=['BLR', 'FRA'],
                  layover_ort='', overnight_after_day=False,
                  starts_at_homebase=True, ends_at_homebase=True,
                  start_time='00:00', end_time='09:21',
                  duty_duration_minutes=561, raw_marker='755 LH755-1',
                  has_fl=False, requires_commute=True),
    ]


def test_bangalore_4_days_single_tour():
    matched = _bangalore_tour()
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025
    )
    # Es darf genau 1 Tour aus den 4 Tagen entstehen
    bangalore_tour = next(
        (t for t in tours
         if any(d['datum'] == '2025-01-04' for d in t['days'])),
        None
    )
    assert bangalore_tour is not None, 'Bangalore-Tour nicht gefunden'
    assert len(bangalore_tour['days']) == 4, (
        f'Bangalore-Tour muss 4 Tage haben, hat {len(bangalore_tour["days"])}'
    )


def test_bangalore_01_04_x_marker_is_tour_mid():
    matched = _bangalore_tour()
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025
    )
    # Finde Tour mit 01-04
    tour = next(t for t in tours
                if any(d['datum'] == '2025-01-04' for d in t['days']))
    day_01_04 = next(d for d in tour['days'] if d['datum'] == '2025-01-04')
    assert day_01_04['role'] == 'tour_mid', (
        f'01-04 X-Marker muss tour_mid sein, war {day_01_04["role"]}'
    )


def test_bangalore_destination_is_blr_india():
    matched = _bangalore_tour()
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025
    )
    tour = next(t for t in tours
                if any(d['datum'] == '2025-01-04' for d in t['days']))
    assert tour['primary_destination'] == 'BLR', (
        f'primary_destination muss BLR sein, war {tour["primary_destination"]}'
    )
    # destination_country sollte "Indien" enthalten
    assert 'Indien' in (tour.get('destination_country') or ''), (
        f'destination_country muss Indien enthalten, war '
        f'{tour.get("destination_country")}'
    )


def test_bangalore_tour_size_4():
    matched = _bangalore_tour()
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025
    )
    tour = next(t for t in tours
                if any(d['datum'] == '2025-01-04' for d in t['days']))
    assert tour['tour_size'] == 4


def test_bangalore_01_03_is_tour_start():
    matched = _bangalore_tour()
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025
    )
    tour = next(t for t in tours
                if any(d['datum'] == '2025-01-04' for d in t['days']))
    day = next(d for d in tour['days'] if d['datum'] == '2025-01-03')
    assert day['role'] == 'tour_start', (
        f'01-03 muss tour_start sein, war {day["role"]}'
    )


def test_bangalore_01_06_is_tour_end():
    matched = _bangalore_tour()
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025
    )
    tour = next(t for t in tours
                if any(d['datum'] == '2025-01-04' for d in t['days']))
    day = next(d for d in tour['days'] if d['datum'] == '2025-01-06')
    assert day['role'] == 'tour_end', (
        f'01-06 muss tour_end sein, war {day["role"]}'
    )


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
