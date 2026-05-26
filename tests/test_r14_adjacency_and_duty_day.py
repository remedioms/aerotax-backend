"""R14 — Tests fuer Date-Adjacency-Guard und Mid-Tour-Continuation-Arbeitstage.

Deckt die beiden Pipeline-Fixes ab:
  - cas_postprocessor: R1/R2/R3/R5 verketten nur noch bei Datums-Adjazenz.
  - normalized_tours: is_real_duty_day akzeptiert is_full_away_day innerhalb
    echter Tour (foreign-signal/overnight).

Hard constraints:
  - Kein Live-Run, kein Deploy.
  - Keine Tibor-/FollowMe-Beträge.
  - Synthetische Datentage.
"""
import os
import sys
import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from cas_postprocessor import (  # noqa: E402
    _dates_are_adjacent,
    normalize_cas_days_v2,
)
from normalized_tours import (  # noqa: E402
    build_normalized_tours,
    calculate_allowances_from_normalized_tours,
)


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

HB = 'FRA'

BMF_STUB = {
    'BLR': {'an_abreise': 32.0, 'voll_24h': 47.0, 'country': 'Indien'},
    'HKG': {'an_abreise': 64.0, 'voll_24h': 97.0, 'country': 'Hongkong'},
    'NRT': {'an_abreise': 56.0, 'voll_24h': 84.0, 'country': 'Japan'},
}
IATA_TO_BMF = {'BLR': 'Indien', 'HKG': 'Hongkong', 'NRT': 'Japan'}


def _dep(datum, iata, duty=600):
    return {
        'datum': datum, 'marker_raw': 'LH756', 'normalized_marker': 'LH756',
        'activity_type': 'tour_departure',
        'starts_at_homebase': True, 'ends_at_homebase': False,
        'overnight_after_day': True,
        'routing': [HB, iata, 'LH756'], 'routing_iatas': [HB, iata],
        'flight_numbers': ['LH756'],
        'layover_ort': iata, 'layover_iata': iata,
        'has_fl': True, 'duty_duration_minutes': duty,
        'tour_context_hint': 'departure', 'tour_context_confidence': 'high',
        'is_tour_departure': True, 'is_tour_continuation': False,
        'is_tour_return': False, 'return_from_layover': False,
        'has_flight_segment': True, 'confidence': 'high',
    }


def _mid(datum, iata):
    return {
        'datum': datum, 'marker_raw': 'X', 'normalized_marker': 'X',
        'activity_type': 'tour_continuation',
        'starts_at_homebase': False, 'ends_at_homebase': False,
        'overnight_after_day': True,
        'routing': [iata], 'routing_iatas': [iata], 'flight_numbers': [],
        'layover_ort': iata, 'layover_iata': iata,
        'has_fl': False, 'duty_duration_minutes': 0,
        'tour_context_hint': 'mid_tour', 'tour_context_confidence': 'high',
        'is_tour_departure': False, 'is_tour_continuation': True,
        'is_tour_return': False, 'return_from_layover': False,
        'has_flight_segment': False, 'confidence': 'high',
    }


def _ret(datum, iata, duty=330):
    return {
        'datum': datum, 'marker_raw': 'LH755', 'normalized_marker': 'LH755',
        'activity_type': 'tour_return',
        'starts_at_homebase': False, 'ends_at_homebase': True,
        'overnight_after_day': False,
        'routing': [iata, HB, 'LH755'], 'routing_iatas': [iata, HB],
        'flight_numbers': ['LH755'],
        'layover_ort': iata, 'layover_iata': iata,
        'origin_iata': iata, 'destination_iata': HB,
        'has_fl': True, 'duty_duration_minutes': duty,
        'tour_context_hint': 'return', 'tour_context_confidence': 'high',
        'is_tour_departure': False, 'is_tour_continuation': False,
        'is_tour_return': True, 'return_from_layover': True,
        'has_flight_segment': True, 'confidence': 'high',
    }


def _x_isolated(datum):
    """Isolierter X-Tag ohne Tour-Kontext — must NOT be counted as arbeitstag."""
    return {
        'datum': datum, 'marker_raw': 'X', 'normalized_marker': 'X',
        'activity_type': 'free',
        'starts_at_homebase': True, 'ends_at_homebase': True,
        'overnight_after_day': False,
        'routing': [], 'routing_iatas': [], 'flight_numbers': [],
        'layover_ort': '', 'layover_iata': None,
        'has_fl': False, 'duty_duration_minutes': 0,
        'tour_context_hint': 'home', 'tour_context_confidence': 'high',
        'is_tour_departure': False, 'is_tour_continuation': False,
        'is_tour_return': False, 'has_flight_segment': False,
        'confidence': 'high',
    }


def _standby(datum):
    return {
        'datum': datum, 'marker_raw': 'SB_S', 'normalized_marker': 'SB_S',
        'activity_type': 'home_standby',
        'starts_at_homebase': True, 'ends_at_homebase': True,
        'overnight_after_day': False,
        'routing': [HB], 'routing_iatas': [HB], 'flight_numbers': [],
        'layover_ort': '', 'layover_iata': None,
        'has_fl': False, 'duty_duration_minutes': 480,
        'tour_context_hint': 'standby', 'tour_context_confidence': 'high',
        'is_tour_departure': False, 'is_tour_continuation': False,
        'is_tour_return': False, 'has_flight_segment': False,
        'confidence': 'high',
    }


# ────────────────────────────────────────────────────────────────────────────
# R14.A — _dates_are_adjacent Helper-Tests
# ────────────────────────────────────────────────────────────────────────────

def test_adjacent_consecutive_days_true():
    assert _dates_are_adjacent('2025-01-06', '2025-01-07') is True


def test_adjacent_same_day_false():
    assert _dates_are_adjacent('2025-01-06', '2025-01-06') is False


def test_adjacent_two_days_gap_false_with_default():
    assert _dates_are_adjacent('2025-01-06', '2025-01-08') is False


def test_adjacent_large_gap_false():
    assert _dates_are_adjacent('2025-01-06', '2025-02-10') is False


def test_adjacent_with_explicit_gap_window():
    # Wenn max_gap_days=3 erlaubt, ist 3-Tage-Lücke OK
    assert _dates_are_adjacent('2025-01-06', '2025-01-09', max_gap_days=3) is True


def test_adjacent_reverse_order_false():
    assert _dates_are_adjacent('2025-01-07', '2025-01-06') is False


def test_adjacent_invalid_input_false():
    assert _dates_are_adjacent(None, '2025-01-07') is False
    assert _dates_are_adjacent('garbage', '2025-01-07') is False


# ────────────────────────────────────────────────────────────────────────────
# R14.B — Postprocessor R3 verkettet nicht ueber grosse Datumsluecke
# ────────────────────────────────────────────────────────────────────────────

def test_r3_does_not_chain_non_adjacent_tour_return_into_next_departure():
    """Tour-Ende 2025-01-06 + naechster Departure 2025-02-10: R3 darf
    ends_at_homebase NICHT auf False kippen.
    """
    days = [
        _dep('2025-01-03', 'BLR'),
        _mid('2025-01-04', 'BLR'),
        _mid('2025-01-05', 'BLR'),
        _ret('2025-01-06', 'BLR'),
        # 35-Tage-Luecke — R3 darf hier nichts heilen
        _dep('2025-02-10', 'HKG'),
        _mid('2025-02-11', 'HKG'),
        _ret('2025-02-12', 'HKG'),
    ]
    normalized = normalize_cas_days_v2(days, homebase=HB)
    by_date = {d['datum']: d for d in normalized}
    blr_ret = by_date['2025-01-06']
    # ends_at_homebase muss True bleiben (Tour BLR endet)
    assert blr_ret.get('ends_at_homebase') is True, (
        f'R3 hat ends_at_homebase falsch auf False gekippt: '
        f'healed_by={blr_ret.get("healed_by")}, warnings={blr_ret.get("warnings")}'
    )
    # Audit-Spur muss vorhanden sein
    assert any(
        'non-adjacent' in w for w in blr_ret.get('warnings') or []
    ), f'erwartete non-adjacent warning, got: {blr_ret.get("warnings")}'


def test_r3_still_chains_adjacent_continuation_correctly():
    """Wenn naechster Tag direkt benachbart und Tour foreign weitergeht,
    darf R3 weiterhin korrigieren (positive Path).
    """
    # Hypothetisch: Sonnet liest Anreise als ends_at_homebase=True falsch
    # und naechster Tag ist klare foreign-Continuation.
    days = [
        {
            **_dep('2025-03-01', 'NRT'),
            'ends_at_homebase': True,  # bewusst falsch gelesen
        },
        _mid('2025-03-02', 'NRT'),
        _ret('2025-03-03', 'NRT'),
    ]
    normalized = normalize_cas_days_v2(days, homebase=HB)
    by_date = {d['datum']: d for d in normalized}
    dep = by_date['2025-03-01']
    # ends_at_homebase muss durch R3 auf False korrigiert sein
    assert dep.get('ends_at_homebase') is False
    assert 'rule3_ends_hb_correction' in (dep.get('healed_by') or [])


def test_r1_does_not_heal_x_return_across_date_gap():
    """X-Return-Healing darf nicht greifen, wenn prev und current nicht
    benachbart sind.
    """
    days = [
        _dep('2025-01-03', 'BLR'),
        _ret('2025-01-04', 'BLR'),
        # 30-Tage-Luecke
        {
            'datum': '2025-02-05', 'marker_raw': 'X', 'normalized_marker': 'X',
            'activity_type': 'free',
            'starts_at_homebase': True, 'ends_at_homebase': True,
            'overnight_after_day': False,
            'routing': [HB], 'routing_iatas': [HB], 'flight_numbers': [],
            'layover_ort': '', 'layover_iata': None,
            'has_fl': False, 'duty_duration_minutes': 0,
            'tour_context_hint': 'home', 'tour_context_confidence': 'high',
            'is_tour_departure': False, 'is_tour_continuation': False,
            'is_tour_return': False, 'has_flight_segment': False,
            'confidence': 'high',
        },
    ]
    # Note: prev = ret day (overnight=False, layover BLR aber overnight already
    # false). R1 sollte sowieso nicht greifen. Aber Test gegen non-adjacent ist
    # Sicherheitsnetz.
    normalized = normalize_cas_days_v2(days, homebase=HB)
    feb = next(d for d in normalized if d['datum'] == '2025-02-05')
    # Soll Frei bleiben — nicht zu tour_return gemacht
    assert feb.get('is_tour_return') is not True


# ────────────────────────────────────────────────────────────────────────────
# R14.C — is_real_duty_day fuer Mid-Tour-Continuation
# ────────────────────────────────────────────────────────────────────────────

def _run(days):
    tours = build_normalized_tours(
        cas_days=days, se_rows=[], year=2025, homebase=HB,
    )
    res = calculate_allowances_from_normalized_tours(
        normalized_tours=tours, bmf_table=BMF_STUB,
        iata_to_bmf=IATA_TO_BMF, se_rows=[], homebase=HB,
    )
    return tours, res


def test_mid_tour_x_within_real_tour_counts_as_arbeitstag():
    """4-Tage-BLR-Tour (dep + 2x mid + ret): erwartete 4 arbeitstage."""
    days = [
        _dep('2025-04-01', 'BLR'),
        _mid('2025-04-02', 'BLR'),
        _mid('2025-04-03', 'BLR'),
        _ret('2025-04-04', 'BLR'),
    ]
    tours, res = _run(days)
    assert len(tours) == 1
    assert len(tours[0].days) == 4
    assert res.arbeitstage == 4, f'erwartet 4, got {res.arbeitstage}'


def test_isolated_x_outside_tour_does_not_count():
    """Einzelner X-Tag ohne Tour-Klammer darf NICHT als arbeitstag zaehlen."""
    days = [_x_isolated('2025-05-15')]
    tours, res = _run(days)
    assert len(tours) == 0
    assert res.arbeitstage == 0


def test_home_standby_does_not_count_as_arbeitstag():
    """Home-Standby alleine erzeugt keine Tour und keinen Arbeitstag."""
    days = [_standby('2025-06-01'), _standby('2025-06-02')]
    tours, res = _run(days)
    assert len(tours) == 0
    assert res.arbeitstage == 0
    assert res.reinigungstage == 0


def test_se_only_does_not_create_tour_or_hotel():
    """SE-Zeile alleine darf weder Tour noch Hotelnacht noch Z76 erzeugen."""
    se_rows = [{
        'datum': '2025-07-10', 'stfrei_ort': 'BLR', 'stfrei_betrag': 20.0,
        'storno': False,
    }]
    # Keine CAS-Tage am Tag
    tours = build_normalized_tours(
        cas_days=[], se_rows=se_rows, year=2025, homebase=HB,
    )
    res = calculate_allowances_from_normalized_tours(
        normalized_tours=tours, bmf_table=BMF_STUB,
        iata_to_bmf=IATA_TO_BMF, se_rows=se_rows, homebase=HB,
    )
    assert len(tours) == 0
    assert res.hotel_naechte == 0
    assert res.z76_eur == 0.0
    assert res.arbeitstage == 0


def test_mid_tour_continuation_does_not_increase_reinigung_for_layover_rest():
    """Layover-Free-Day bekommt arbeitstag aber KEIN Reinigung (im Hotel)."""
    days = [
        _dep('2025-04-01', 'BLR'),
        _mid('2025-04-02', 'BLR'),  # Layover-Rest
        _mid('2025-04-03', 'BLR'),  # Layover-Rest
        _ret('2025-04-04', 'BLR'),
    ]
    _, res = _run(days)
    # 4 arbeitstage, aber reinigung nur fuer dep+ret (2)
    assert res.arbeitstage == 4
    assert res.reinigungstage == 2, (
        f'reinigung sollte 2 sein (nur dep+ret), got {res.reinigungstage}'
    )


def test_two_separate_tours_no_chain_across_gap():
    """Mit R3-Date-Adjacency-Fix: zwei Touren bleiben getrennt, ohne dass
    ein Free-Day-Buffer dazwischen liegt.
    """
    days = [
        _dep('2025-01-03', 'BLR'),
        _mid('2025-01-04', 'BLR'),
        _ret('2025-01-05', 'BLR'),
        # 5-Wochen-Luecke (keine Tage dazwischen)
        _dep('2025-02-10', 'HKG'),
        _ret('2025-02-11', 'HKG'),
    ]
    tours, res = _run(days)
    assert len(tours) == 2, (
        f'erwartet 2 Touren, got {len(tours)}: '
        f'{[(t.tour_id, t.start_date.isoformat(), t.end_date.isoformat()) for t in tours]}'
    )
    blr = tours[0]
    hkg = tours[1]
    assert blr.start_date.isoformat() == '2025-01-03'
    assert blr.end_date.isoformat() == '2025-01-05'
    assert hkg.start_date.isoformat() == '2025-02-10'
    assert hkg.end_date.isoformat() == '2025-02-11'
