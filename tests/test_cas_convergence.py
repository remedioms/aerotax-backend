"""Konvergenz-Test: verschiedene (verrauschte) Sonnet-Lesungen DERSELBEN Tour
muessen nach CAS-Reconcile zu IDENTISCHEN Steuerzahlen fuehren.

Das ist der Kern-Beweis gegen die dokumentierte "ZeroDay-Stochastik"
(Fahrtage schwankten 41-55 ueber Laeufe). Wenn dieser Test bricht, ist die
Determinismus-Garantie verloren.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

from normalized_tours import (  # noqa: E402
    build_normalized_tours, calculate_allowances_from_normalized_tours,
)
from cas_reconcile import reconcile_days  # noqa: E402

BMF = {'BLR': {'an_abreise': 28.0, 'voll_24h': 42.0, 'country': 'Indien - Bangalore'}}

# Deterministische Grundwahrheit (wie der PDF-Parser sie liefert):
# 4-Tage-Tour FRA->BLR (03.), Layover 04+05, BLR->FRA Heimkehr (06.).
DET = [
    {'datum': '2025-01-03', 'flight_numbers': ['LH756'], 'routing': ['FRA', 'BLR'],
     'dep_time': '13:05', 'arr_time': '01:30'},
    {'datum': '2025-01-04', 'flight_numbers': [], 'routing': ['BLR'],
     'dep_time': None, 'arr_time': None},
    {'datum': '2025-01-05', 'flight_numbers': [], 'routing': ['BLR'],
     'dep_time': None, 'arr_time': None},
    {'datum': '2025-01-06', 'flight_numbers': ['LH755'], 'routing': ['BLR', 'FRA'],
     'dep_time': '03:45', 'arr_time': '09:55'},
]


def _to_cas(days):
    out = []
    for d in days:
        fn = d['flight_numbers'][0] if d['flight_numbers'] else (d.get('marker_raw') or 'X')
        r = d.get('routing_iatas') or d.get('routing') or []
        lay = d.get('layover_iata') or ''
        if not lay:
            if r and r[-1] != 'FRA':
                lay = r[-1]
            elif r and r[0] != 'FRA':
                lay = r[0]
        out.append({
            'datum': d['datum'], 'marker_raw': fn, 'routing': list(r), 'layover_ort': lay,
            'overnight_after_day': bool(d.get('overnight_after_day')),
            'starts_at_homebase': (r[0] == 'FRA' if r else False),
            'ends_at_homebase': (r[-1] == 'FRA' and not d.get('overnight_after_day') if r else False),
            'duty_duration_minutes': 600 if d['flight_numbers'] else 0,
            'has_fl': bool(d['flight_numbers']),
            'tz_hotel_night': d.get('tz_hotel_night'),
        })
    return out


def _run(days):
    tours = build_normalized_tours(_to_cas(days), [], 2025, homebase='FRA')
    r = calculate_allowances_from_normalized_tours(
        tours, BMF, iata_to_bmf={'BLR': 'Indien - Bangalore'}, homebase='FRA')
    return (round(r.z76_eur, 2), r.z76_tage, r.hotel_naechte, r.fahrtage, r.arbeitstage)


def _ideal():
    return [{'datum': x['datum'], 'flight_numbers': list(x['flight_numbers']),
             'routing_iatas': list(x['routing']),
             'overnight_after_day': bool(x['flight_numbers']) or x['datum'] in ('2025-01-04', '2025-01-05')}
            for x in DET]


def _noisy_zeroday():
    d = _ideal()
    for x in d:
        if x['datum'] == '2025-01-04':  # Layover-Tag als frei verloren
            x['routing_iatas'] = []; x['overnight_after_day'] = False
    return d


def _noisy_overnight_route():
    d = _ideal()
    for x in d:
        if x['datum'] == '2025-01-06':
            x['overnight_after_day'] = True   # Heimflug faelschlich auswaerts
        if x['datum'] == '2025-01-03':
            x['routing_iatas'] = ['FRA']      # Ziel BLR verloren
    return d


def _noisy_typo_route():
    d = _ideal()
    for x in d:
        if x['datum'] == '2025-01-03':
            x['flight_numbers'] = ['LH75']    # Flugnummer vertippt
        if x['datum'] == '2025-01-05':
            x['routing_iatas'] = []           # Routing verloren
    return d


def test_noisy_reads_converge_after_reconcile():
    variants = [_noisy_zeroday(), _noisy_overnight_route(), _noisy_typo_route()]
    results = {_run(reconcile_days(DET, v, 'FRA')['days']) for v in variants}
    assert len(results) == 1, f'Nicht konvergent: {results}'
    z76, z76_tage, hotel, fahrt, arb = next(iter(results))
    assert z76 == 140.0, z76          # 2x voll_24h(42) + an_abreise(28) + voll_24h-Heimkehr(28? -> 140)
    assert hotel == 2                 # 2 Layover-Naechte (BLR), nicht im Flug
    assert z76_tage == 4


def test_reconcile_is_idempotent():
    # Reconcile auf bereits korrigierte Tage darf nichts mehr aendern.
    v = _noisy_zeroday()
    once = reconcile_days(DET, v, 'FRA')['days']
    twice = reconcile_days(DET, once, 'FRA')['days']
    assert _run(once) == _run(twice)
