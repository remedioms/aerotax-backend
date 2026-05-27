"""Tour-First-Heimkehr-Tests (R38 Schritt 2, 2026-05-27).

Synthetische Mini-Szenarien die belegen dass das normalized_tours-Modell
Heimkehr-Tage KORREKT als Tour-Last-Day erkennt und mit Z76 An/Ab
klassifiziert. Wenn diese Tests grün sind aber die Live-Auswertung den
Heimkehr-Tag falsch klassifiziert, liegt der Bug:
  (a) im CAS-Reader-Output (Sonnet liest die Felder anders)
  (b) im Legacy-Klassifikator-Pfad der den Tour-First-Override umgeht
NICHT in der Tour-First-Logik selbst.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _bmf_table_2025():
    """Baut die bmf_table wie in production (app.py:22989ff): IATA-keyed."""
    from bmf_data import BMF_AUSLAND_BY_YEAR, IATA_TO_BMF
    bmf_year = BMF_AUSLAND_BY_YEAR.get(2025, {})
    out = {}
    for iata, country in (IATA_TO_BMF or {}).items():
        entry = bmf_year.get(country)
        if isinstance(entry, (tuple, list)) and len(entry) >= 2:
            out[iata] = {
                'voll_24h':   float(entry[0]),
                'an_abreise': float(entry[1]),
                'country':    country,
            }
    return out


def _miami_tour_2_days():
    """Tibors realer Fall: 13./14.02.2025 Miami-Tour mit Heimflug am 14."""
    return [
        {'datum': '2025-02-12', 'marker_raw': 'OFF', 'activity_type': 'free'},
        {'datum': '2025-02-13', 'marker_raw': 'LH462', 'activity_type': 'tour',
         'routing': ['FRA', 'MIA'], 'overnight_after_day': True,
         'layover_ort': 'MIA', 'has_fl': True,
         'start_time': '10:15', 'end_time': '23:30',
         'starts_at_homebase': True, 'ends_at_homebase': False,
         'duty_duration_minutes': 660},
        {'datum': '2025-02-14', 'marker_raw': 'LH463', 'activity_type': 'tour',
         'routing': ['MIA', 'FRA'], 'overnight_after_day': False,
         'layover_ort': '', 'has_fl': True,
         'start_time': '18:00', 'end_time': '23:55',
         'starts_at_homebase': False, 'ends_at_homebase': True,
         'duty_duration_minutes': 600},
        {'datum': '2025-02-15', 'marker_raw': 'OFF', 'activity_type': 'free'},
    ]


def test_tour_builder_keeps_heimkehr_in_tour():
    """13.02 + 14.02 müssen als 1 Tour mit 2 Tagen gebaut werden."""
    from normalized_tours import build_normalized_tours
    tours = build_normalized_tours(
        _miami_tour_2_days(), se_rows=[], year=2025, homebase='FRA',
    )
    assert len(tours) == 1, f'Erwartet 1 Tour, bekam {len(tours)}'
    tour = tours[0]
    assert len(tour.days) == 2
    dates = [d.date.isoformat() for d in tour.days]
    assert dates == ['2025-02-13', '2025-02-14']
    # 13.02 = Anreise, 14.02 = Heimkehr
    assert tour.days[0].is_departure_day
    assert tour.days[1].is_return_day
    assert tour.days[0].layover_iata == 'MIA'
    assert tour.days[0].has_real_fl_layover  # overnight in MIA


def test_calculator_heimkehr_z76_an_ab_usa_miami():
    """Heimkehr-Tag (14.02) muss Z76 An/Ab mit USA-Miami-Pauschale (44€) sein."""
    from normalized_tours import (build_normalized_tours,
                                   calculate_allowances_from_normalized_tours)
    from bmf_data import IATA_TO_BMF
    tours = build_normalized_tours(
        _miami_tour_2_days(), se_rows=[], year=2025, homebase='FRA',
    )
    result = calculate_allowances_from_normalized_tours(
        tours, _bmf_table_2025(), iata_to_bmf=IATA_TO_BMF, homebase='FRA',
    )
    # 14.02 muss Z76 sein, nicht Issue oder Frei
    day_14 = result.by_date.get('2025-02-14')
    assert day_14 is not None
    assert day_14.get('klass') == 'Z76', (
        f'14.02 sollte Z76 sein, bekam {day_14.get("klass")}'
    )
    assert day_14.get('amount') == 44.0, (
        f'14.02 An/Ab USA-Miami sollte 44€ sein, bekam {day_14.get("amount")}'
    )
    assert 'Miami' in (day_14.get('country') or ''), (
        f'14.02 country sollte Miami enthalten, bekam {day_14.get("country")}'
    )
    # 13.02 = Anreise auch An/Ab
    day_13 = result.by_date.get('2025-02-13')
    assert day_13.get('klass') == 'Z76'
    assert day_13.get('amount') == 44.0
    # 1 Hotelnacht
    assert result.hotel_naechte == 1
    # Σ Z76 = 88€
    assert result.z76_eur == 88.0


def test_calculator_outside_tour_stays_free():
    """Tage außerhalb der Tour (12.02, 15.02) bleiben unverändert."""
    from normalized_tours import (build_normalized_tours,
                                   calculate_allowances_from_normalized_tours)
    from bmf_data import IATA_TO_BMF
    tours = build_normalized_tours(
        _miami_tour_2_days(), se_rows=[], year=2025, homebase='FRA',
    )
    result = calculate_allowances_from_normalized_tours(
        tours, _bmf_table_2025(), iata_to_bmf=IATA_TO_BMF, homebase='FRA',
    )
    # 12.02 und 15.02 sind nicht in by_date weil keine Tour-Tage
    assert '2025-02-12' not in result.by_date
    assert '2025-02-15' not in result.by_date


def test_easa_ruhezeit_blockt_neue_tour_implizit():
    """Tibors Argument: nach US-Longhaul keine Tour möglich am gleichen Tag.
    Unser Modell sieht 14.02 nicht als „same_day + neue Tour", sondern als
    Last-Day der Vortag-Tour. Damit ist die EASA-Logik implizit erfüllt."""
    from normalized_tours import build_normalized_tours
    tours = build_normalized_tours(
        _miami_tour_2_days(), se_rows=[], year=2025, homebase='FRA',
    )
    # Genau eine Tour. Wenn das Modell 14.02 fälschlich als „neue Tour"
    # interpretiert hätte, wären's zwei.
    assert len(tours) == 1


def test_bos_heimkehr_analog_miami():
    """26.03 BOS-Heimkehr analog Miami — andere Foreign-Destination,
    selbe Pattern. Test sichert Generalität des Modells."""
    from normalized_tours import (build_normalized_tours,
                                   calculate_allowances_from_normalized_tours)
    from bmf_data import IATA_TO_BMF
    cas = [
        {'datum': '2025-03-25', 'marker_raw': 'OFF', 'activity_type': 'free'},
        {'datum': '2025-03-26', 'marker_raw': 'LH752', 'activity_type': 'tour',
         'routing': ['FRA', 'BOS'], 'overnight_after_day': True,
         'layover_ort': 'BOS', 'has_fl': True,
         'start_time': '11:55', 'end_time': '21:00',
         'starts_at_homebase': True, 'ends_at_homebase': False,
         'duty_duration_minutes': 545},
        {'datum': '2025-03-27', 'marker_raw': 'LH753', 'activity_type': 'tour',
         'routing': ['BOS', 'FRA'], 'overnight_after_day': False,
         'layover_ort': '', 'has_fl': True,
         'start_time': '16:00', 'end_time': '23:55',
         'starts_at_homebase': False, 'ends_at_homebase': True,
         'duty_duration_minutes': 480},
        {'datum': '2025-03-28', 'marker_raw': 'OFF', 'activity_type': 'free'},
    ]
    tours = build_normalized_tours(cas, se_rows=[], year=2025, homebase='FRA')
    result = calculate_allowances_from_normalized_tours(
        tours, _bmf_table_2025(), iata_to_bmf=IATA_TO_BMF, homebase='FRA',
    )
    day_27 = result.by_date.get('2025-03-27')
    assert day_27 is not None
    assert day_27.get('klass') == 'Z76'
    # BOS Boston Pauschale An/Ab = 42€
    assert day_27.get('amount') == 42.0
    assert 'Boston' in (day_27.get('country') or '')
