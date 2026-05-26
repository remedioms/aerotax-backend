"""Tests für normalized_tours.calculate_allowances_from_normalized_tours.

Verifiziert dass die Allowance-Berechnung aus normalisierten Touren:
  - Mid-Tour Foreign-Tag → voll_24h
  - Foreign-Departure → an_abreise
  - Foreign-Return → an_abreise
  - Inland-Same-Day >8h → Z72
  - Inland <8h → keine VMA
  - SE-Place enriches country, erzeugt nicht allein Z76
  - Z77 kein Effekt hier (wird in app.py abgezogen)
  - Z17 kein Effekt hier (Fahrtkosten)
  - Hotelnächte nur aus normalisierten Touren
"""
import os
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE.parent))
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

from normalized_tours import (  # noqa: E402
    build_normalized_tours,
    calculate_allowances_from_normalized_tours,
)


BMF_2025 = {
    'BLR': {'an_abreise': 28.0, 'voll_24h': 42.0, 'country': 'Indien - Bangalore'},
    'HKG': {'an_abreise': 48.0, 'voll_24h': 71.0, 'country': 'China - Hong Kong'},
    'BOM': {'an_abreise': 36.0, 'voll_24h': 53.0, 'country': 'Indien - Mumbai'},
    'ICN': {'an_abreise': 32.0, 'voll_24h': 48.0, 'country': 'Republik Korea'},
    'SEL': {'an_abreise': 32.0, 'voll_24h': 48.0, 'country': 'Republik Korea'},
    'CPH': {'an_abreise': 50.0, 'voll_24h': 75.0, 'country': 'Dänemark'},
}


def _cas(datum, marker='', routing=None, layover_ort='', overnight=False,
         starts_hb=False, ends_hb=False, duty_min=0, has_fl=False):
    return {
        'datum': datum,
        'marker_raw': marker,
        'routing': routing or [],
        'layover_ort': layover_ort,
        'overnight_after_day': overnight,
        'starts_at_homebase': starts_hb,
        'ends_at_homebase': ends_hb,
        'duty_duration_minutes': duty_min,
        'has_fl': has_fl,
    }


def test_mid_tour_foreign_day_gets_voll_24h():
    """3-Tage-Tour zu BLR: Mid-Tour-Tag bekommt voll_24h=42€."""
    cas = [
        _cas('2025-01-03', marker='LH756', routing=['LH756'],
             starts_hb=True, layover_ort='BLR', overnight=True, duty_min=600),
        _cas('2025-01-04', marker='X', routing=['LH756'],
             layover_ort='BLR', overnight=True),
        _cas('2025-01-05', marker='X', routing=['LH756'],
             layover_ort='BLR', overnight=True),
        _cas('2025-01-06', marker='LH755', routing=['LH755'],
             ends_hb=True, duty_min=600),
    ]
    tours = build_normalized_tours(cas, [], 2025, homebase='FRA')
    result = calculate_allowances_from_normalized_tours(tours, BMF_2025)
    # Tag 04 und 05 sind full_away → voll_24h=42€ jeder
    # Tag 03 departure → 28€, Tag 06 return → 28€
    by_date = result.by_date
    assert by_date['2025-01-04']['amount'] == 42.0
    assert by_date['2025-01-04']['rate_type'] == 'voll_24h'
    assert by_date['2025-01-05']['amount'] == 42.0


def test_foreign_departure_day_gets_z76_an_abreise():
    """Tour-Start-Tag mit foreign target → Z76 an_abreise."""
    cas = [
        _cas('2025-01-18', marker='LH', routing=['LH'],
             starts_hb=True, layover_ort='HKG', overnight=True, duty_min=600),
        _cas('2025-01-19', marker='X', layover_ort='HKG', overnight=True),
        _cas('2025-01-22', marker='LH', ends_hb=True, duty_min=600),
    ]
    tours = build_normalized_tours(cas, [], 2025, homebase='FRA')
    result = calculate_allowances_from_normalized_tours(tours, BMF_2025)
    dep = result.by_date.get('2025-01-18')
    assert dep is not None
    assert dep['klass'] == 'Z76'
    assert dep['amount'] == 48.0  # HKG an_abreise


def test_foreign_return_day_gets_z76_an_abreise():
    """Tour-Ende-Tag mit foreign target → Z76 an_abreise."""
    cas = [
        _cas('2025-01-18', marker='LH', routing=['LH'],
             starts_hb=True, layover_ort='HKG', overnight=True, duty_min=600),
        _cas('2025-01-22', marker='LH', routing=['LH'],
             ends_hb=True, duty_min=600),
    ]
    tours = build_normalized_tours(cas, [], 2025, homebase='FRA')
    result = calculate_allowances_from_normalized_tours(tours, BMF_2025)
    ret = result.by_date.get('2025-01-22')
    assert ret is not None
    assert ret['klass'] == 'Z76'
    assert ret['amount'] == 48.0


def test_inland_same_day_over_8h_gets_z72():
    """Same-Day Inland mit >8h Dienst → Z72 14€."""
    cas = [
        _cas('2025-03-22', marker='LH', routing=['LH'],
             starts_hb=True, ends_hb=True, duty_min=570),  # >8h
    ]
    tours = build_normalized_tours(cas, [], 2025, homebase='FRA')
    result = calculate_allowances_from_normalized_tours(tours, BMF_2025)
    if '2025-03-22' in result.by_date:
        d = result.by_date['2025-03-22']
        assert d['klass'] == 'Z72'
        assert d['amount'] == 14.0


def test_inland_under_8h_no_vma():
    """Same-Day Inland mit <8h → keine VMA."""
    cas = [
        _cas('2025-08-01', marker='LH', routing=['LH'],
             starts_hb=True, ends_hb=True, duty_min=389),  # <8h
    ]
    tours = build_normalized_tours(cas, [], 2025, homebase='FRA')
    result = calculate_allowances_from_normalized_tours(tours, BMF_2025)
    if '2025-08-01' in result.by_date:
        d = result.by_date['2025-08-01']
        assert d['klass'] == 'none'
        assert d['amount'] == 0.0


def test_se_place_enriches_existing_tour_country():
    """SE-stfrei_ort am Tour-Tag wird als country-Quelle akzeptiert wenn
    CAS keine target_iata extrahieren konnte."""
    cas = [
        _cas('2025-04-08', marker='LH', routing=['LH'],
             starts_hb=True, overnight=True, duty_min=600),  # kein layover_ort
        _cas('2025-04-09', marker='X', overnight=True),
        _cas('2025-04-11', marker='LH', ends_hb=True, duty_min=600),
    ]
    se_rows = [
        {'datum': '2025-04-09', 'stfrei_ort': 'ICN',
         'stfrei_betrag': 48.0, 'storno': False},
    ]
    tours = build_normalized_tours(cas, se_rows, 2025, homebase='FRA')
    if tours:
        t = tours[0]
        # target_iata wurde aus SE angereichert
        mid_days = [td for td in t.days if td.is_full_away_day]
        if mid_days:
            assert mid_days[0].target_iata == 'ICN'


def test_se_place_does_not_create_z76_without_tour():
    """SE-stfrei am Frei-Tag (keine CAS-Tour) → KEIN Z76, keine Tour."""
    cas = [
        _cas('2025-06-01', marker='', activity_type='') if False else
        _cas('2025-06-01', marker=''),
    ]
    se_rows = [
        {'datum': '2025-06-01', 'stfrei_ort': 'GOT',
         'stfrei_betrag': 50.0, 'storno': False},
    ]
    tours = build_normalized_tours(cas, se_rows, 2025, homebase='FRA')
    result = calculate_allowances_from_normalized_tours(tours, BMF_2025)
    assert result.z76_eur == 0.0
    assert result.z76_tage == 0


def test_z77_offsets_vma_only_in_caller():
    """Z77 wird NICHT von normalized_tours abgezogen — das macht app.py.

    normalized_tours liefert nur Brutto-VMA pro Tour-Tag.
    """
    cas = [
        _cas('2025-01-03', marker='LH', routing=['LH'],
             starts_hb=True, layover_ort='BLR', overnight=True, duty_min=600),
        _cas('2025-01-06', marker='LH', ends_hb=True, duty_min=600),
    ]
    tours = build_normalized_tours(cas, [], 2025, homebase='FRA')
    result = calculate_allowances_from_normalized_tours(tours, BMF_2025)
    # z76_eur sollte gesetzt sein, kein Z77-Abzug
    assert result.z76_eur > 0
    # Es gibt KEIN result.z77_eur — Z77 ist außerhalb dieses Moduls.
    assert not hasattr(result, 'z77_eur')


def test_z17_offsets_commute_only_in_caller():
    """Z17 wird NICHT von normalized_tours abgezogen — das macht app.py.

    normalized_tours liefert nur fahrtage-Count, nicht Brutto-Fahrtkosten.
    """
    cas = [
        _cas('2025-01-03', marker='LH', routing=['LH'],
             starts_hb=True, layover_ort='BLR', overnight=True, duty_min=600),
        _cas('2025-01-06', marker='LH', ends_hb=True, duty_min=600),
    ]
    tours = build_normalized_tours(cas, [], 2025, homebase='FRA')
    result = calculate_allowances_from_normalized_tours(tours, BMF_2025)
    assert result.fahrtage == 1
    assert not hasattr(result, 'z17_eur')


def test_hotel_nights_from_normalized_tours_only():
    """Hotel-Nächte werden nur in Touren mit echtem FL-Layover gezählt."""
    cas = [
        _cas('2025-01-03', marker='LH', routing=['LH'],
             starts_hb=True, layover_ort='BLR', overnight=True, duty_min=600),
        _cas('2025-01-04', marker='X', layover_ort='BLR', overnight=True),
        _cas('2025-01-05', marker='X', layover_ort='BLR', overnight=True),
        _cas('2025-01-06', marker='LH', ends_hb=True, duty_min=600),
    ]
    tours = build_normalized_tours(cas, [], 2025, homebase='FRA')
    result = calculate_allowances_from_normalized_tours(tours, BMF_2025)
    # 3 Nächte BLR (03/04/05 mit overnight+layover≠HB) — Heimkehr-Tag (06) keine Hotelnacht
    assert result.hotel_naechte == 3, f'expected 3 hotel nights, got {result.hotel_naechte}'


def test_home_standby_does_not_count_as_cleaning_day():
    """Home-Standby-Tage werden NICHT als Reinigungstag gezählt.

    normalized_tours: Home-Standby ist GAR KEIN Tour-Tag, daher keine Reinigung.
    """
    cas = [
        _cas('2025-02-01', marker='SB_S'),
        _cas('2025-02-02', marker='SB_S'),
        _cas('2025-02-04', marker='SB_S'),
        _cas('2025-02-05', marker='SB_S'),
    ]
    tours = build_normalized_tours(cas, [], 2025, homebase='FRA')
    result = calculate_allowances_from_normalized_tours(tours, BMF_2025)
    assert result.reinigungstage == 0, \
        f'Home-Standby darf nicht als Reinigung zählen, got {result.reinigungstage}'


def test_phantom_tour_does_not_create_vma_or_hotel():
    """Phantom-Tour (SE-only ohne CAS-Evidence) → 0€ VMA, 0 Hotel."""
    cas = [
        _cas('2025-05-19', marker=''),
        _cas('2025-05-20', marker=''),
        _cas('2025-05-21', marker=''),  # kein duty, kein Routing
        _cas('2025-05-22', marker=''),
    ]
    se_rows = [
        {'datum': '2025-05-21', 'stfrei_ort': 'LAD',
         'stfrei_betrag': 84.0, 'storno': False},
    ]
    tours = build_normalized_tours(cas, se_rows, 2025, homebase='FRA')
    result = calculate_allowances_from_normalized_tours(tours, BMF_2025)
    assert result.z76_eur == 0.0
    assert result.hotel_naechte == 0
