"""R19 — Tests für drei Tuning-Fixes in normalized_tours.calculate_allowances:

1. Z74 natural_tour_boundary erweitert auf Aircraft-Rotation in foreign tour
2. Hotel-Evidence für foreign_layover_iata: cas_overnight als Pflicht
3. Fahrtage: Same-Day-Inland-Trip mit echtem Flight-Token zählt als Fahrtag
"""
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from normalized_tours import (  # noqa: E402
    build_normalized_tours,
    calculate_allowances_from_normalized_tours,
)

HB = 'FRA'

BMF_STUB = {
    'BLR': {'an_abreise': 32.0, 'voll_24h': 47.0, 'country': 'Indien'},
    'HKG': {'an_abreise': 64.0, 'voll_24h': 97.0, 'country': 'Hongkong'},
}
IATA_TO_BMF = {'BLR': 'Indien', 'HKG': 'Hongkong'}


def _day(datum, marker='', routing=None, layover='', overnight=False,
         starts_hb=False, ends_hb=False, duty=0, has_fl=False, activity='free'):
    return {
        'datum': datum, 'date': datum,
        'marker': marker, 'marker_raw': marker,
        'routing': routing or [],
        'layover_ort': layover, 'layover_iata': layover,
        'overnight_after_day': overnight,
        'starts_at_homebase': starts_hb, 'ends_at_homebase': ends_hb,
        'duty_duration_minutes': duty,
        'has_fl': has_fl,
        'activity_type': activity,
        'confidence': 'high',
    }


def _run(days, se_rows=None):
    tours = build_normalized_tours(
        cas_days=days, se_rows=se_rows or [], year=2025, homebase=HB,
    )
    res = calculate_allowances_from_normalized_tours(
        normalized_tours=tours, bmf_table=BMF_STUB,
        iata_to_bmf=IATA_TO_BMF, se_rows=se_rows or [], homebase=HB,
    )
    return tours, res


# ── R19.1: Z74 NICHT bei Aircraft-Rotation in foreign tour ──────────────────

def test_r19_aircraft_rotation_inland_in_foreign_tour_not_z74():
    """Mid-Tour-Tag mit Inland-Layover (z.B. MUC stopover) in einer Tour
    mit foreign target (BLR) darf NICHT als Z74 zählen — bleibt Z76."""
    days = [
        _day('2025-06-01', marker='LH756', routing=[HB, 'BLR', 'LH756'],
             layover='BLR', overnight=True, starts_hb=True, has_fl=True, duty=600),
        # Mid-Tour-Tag mit Aircraft-Stop in MUC (Inland) — bleibt Auslands-Tour
        _day('2025-06-02', marker='X', routing=['MUC'],
             layover='MUC', overnight=True),
        _day('2025-06-03', marker='LH755', routing=['BLR', HB, 'LH755'],
             layover='BLR', overnight=False, ends_hb=True, has_fl=True, duty=400),
    ]
    _, res = _run(days)
    # Mid-Tour-Tag in foreign-Tour mit Aircraft-Stop muss Z76 sein, nicht Z74
    assert res.z74_tage == 0, (
        f'z74_tage erwartet 0 (Aircraft-Rotation in foreign-tour), got {res.z74_tage}'
    )


# ── R19.2: Hotel-Evidence — foreign_layover_iata braucht cas_overnight ──────

def test_r19_foreign_layover_without_overnight_no_hotel():
    """Tag mit layover_iata=foreign aber overnight=False darf KEIN Hotel-Night
    erzeugen (Reader-Phantom-Path)."""
    days = [
        # Echter Dep mit overnight ✓
        _day('2025-07-01', marker='LH756', routing=[HB, 'BLR', 'LH756'],
             layover='BLR', overnight=True, starts_hb=True, has_fl=True, duty=600),
        # Mid-Tour normal ✓
        _day('2025-07-02', marker='X', routing=['BLR'],
             layover='BLR', overnight=True),
        # Reader-Phantom: layover=BLR aber overnight=False → kein Hotel
        _day('2025-07-03', marker='LH755', routing=['BLR', HB, 'LH755'],
             layover='BLR', overnight=False, ends_hb=True, has_fl=True, duty=400),
    ]
    _, res = _run(days)
    # Erwartet: 2 Hotel-Nights (01.07 + 02.07), NICHT 3
    assert res.hotel_naechte == 2, (
        f'hotel_naechte erwartet 2 (nur overnight=True zählt), got {res.hotel_naechte}'
    )


# ── R19.3: Fahrtage — Same-Day-Inland-Trip mit Flight-Token zählt ───────────

def test_r19_same_day_inland_flight_counts_fahrtag():
    """Same-Day-Inland-Trip mit echtem Flugmarker (LH123) und duty>=480 zählt
    als Fahrtag."""
    days = [
        # Same-Day FRA→MUC mit LH-Flight, duty 540min
        _day('2025-08-01', marker='LH123', routing=[HB, 'MUC', 'LH123'],
             layover='', overnight=False,
             starts_hb=True, ends_hb=True, has_fl=True, duty=540),
    ]
    _, res = _run(days)
    assert res.fahrtage == 1, (
        f'fahrtage erwartet 1 (Same-Day-Inland-Flight ist Tour-Start), got {res.fahrtage}'
    )


def test_r19_same_day_office_no_flight_no_fahrtag():
    """Same-Day-Office am HB mit duty>=480 ABER ohne Flight-Marker darf KEIN
    Fahrtag erzeugen (Office-Training-Tag)."""
    days = [
        _day('2025-08-02', marker='EM', routing=[],
             layover='', overnight=False,
             starts_hb=True, ends_hb=True, has_fl=False, duty=540,
             activity='training'),
    ]
    _, res = _run(days)
    assert res.fahrtage == 0, (
        f'fahrtage erwartet 0 (Office ohne Flight), got {res.fahrtage}'
    )


def test_r19_same_day_inland_duty_under_480_no_fahrtag():
    """Same-Day-Inland mit duty<480 (z.B. 6h Trip): kein Fahrtag (kein Z72-trigger)."""
    days = [
        _day('2025-08-03', marker='LH123', routing=[HB, 'MUC', 'LH123'],
             layover='', overnight=False,
             starts_hb=True, ends_hb=True, has_fl=True, duty=360),
    ]
    _, res = _run(days)
    assert res.fahrtage == 0


# ── R19.4: Regression — Foreign-Tour Fahrtage funktionieren weiter ──────────

def test_r19_regression_foreign_tour_still_counts_fahrtag():
    days = [
        _day('2025-09-01', marker='LH796', routing=[HB, 'HKG', 'LH796'],
             layover='HKG', overnight=True, starts_hb=True, has_fl=True, duty=720),
        _day('2025-09-02', marker='X', routing=['HKG'],
             layover='HKG', overnight=True),
        _day('2025-09-03', marker='LH797', routing=['HKG', HB, 'LH797'],
             layover='HKG', overnight=False, ends_hb=True, has_fl=True, duty=400),
    ]
    _, res = _run(days)
    assert res.fahrtage == 1
    assert res.z76_tage >= 1
    assert res.hotel_naechte == 2  # Dep + Mid (nicht Ret)
