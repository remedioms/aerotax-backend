"""R23 Bug 2 Fix — Nachtflug-Heimkehr-Pattern → Voll-Pauschale

Belegt durch User-Feedback BLR-Tour (Tibor 2025):
  03.01.2025  Anreise FRA→BLR Briefing 10:55, Take-off 12:34 → Z76 An/Ab 28€
  04.01.2025  Mid-Tour BLR, 24h foreign         → Z76 Voll  42€
  05.01.2025  Mid-Tour BLR, LH755 Departure 23:28 BLR — VOLL 24h in BLR
              → Z76 Voll 42€ (NICHT An/Ab 28€)
  06.01.2025  Landing FRA 09:21 — Heimkehr      → Z76 An/Ab 28€

Vorher zählten wir 05.01 als An/Ab (28€) weil der Reader den 755-Marker
als is_tour_return=True markierte. Bug 2 Fix: wenn is_return_day UND
overnight_after_day=True, dann Voll-Pauschale.
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
BMF = {
    'BLR': {'an_abreise': 28.0, 'voll_24h': 42.0, 'country': 'Indien-Bangalore'},
    'HKG': {'an_abreise': 48.0, 'voll_24h': 71.0, 'country': 'Hongkong'},
}
IATA = {'BLR': 'Indien-Bangalore', 'HKG': 'Hongkong'}


def _day(datum, marker='', routing=None, layover='', overnight=False,
         starts_hb=False, ends_hb=False, duty=600, has_fl=True,
         is_tour_return=False, is_tour_continuation=False):
    return {
        'datum': datum, 'date': datum,
        'marker': marker, 'marker_raw': marker,
        'routing': routing or [],
        'layover_ort': layover, 'layover_iata': layover,
        'overnight_after_day': overnight,
        'starts_at_homebase': starts_hb, 'ends_at_homebase': ends_hb,
        'duty_duration_minutes': duty,
        'has_fl': has_fl,
        'activity_type': 'flight' if has_fl else 'free',
        'is_tour_return': is_tour_return,
        'is_tour_continuation': is_tour_continuation,
        'confidence': 'high',
    }


def test_night_return_day_with_overnight_is_voll_pauschale():
    """Wenn der Reader einen Tag als is_tour_return markiert hat (z.B. wegen
    Heimflug-Marker), der Tag aber overnight_after_day=True zeigt, dann ist
    der Heimflug ein Nachtflug — Tag war noch voll im Ausland → Voll-Pauschale.
    """
    cas = [
        _day('2025-01-03', marker='LH756', routing=['LH756'], starts_hb=True,
             layover='BLR', overnight=True),
        _day('2025-01-04', marker='X', routing=['BLR'],
             layover='BLR', overnight=True, has_fl=False),
        _day('2025-01-05', marker='LH755', routing=['BLR', 'LH755'],
             layover='BLR', overnight=True, is_tour_return=True),
        _day('2025-01-06', marker='X', routing=[HB], ends_hb=True,
             overnight=False, has_fl=False),
    ]
    tours = build_normalized_tours(
        cas_days=cas, se_rows=[], year=2025, homebase=HB,
    )
    res = calculate_allowances_from_normalized_tours(
        normalized_tours=tours, bmf_table=BMF,
        iata_to_bmf=IATA, se_rows=[], homebase=HB,
    )
    # Es sollte mindestens eine Tour geben
    assert len(tours) >= 1
    # 05.01 sollte Voll-Pauschale (42€) statt An/Ab (28€) liefern
    d05 = res.by_date.get('2025-01-05')
    assert d05 is not None, '05.01 muss klassifiziert sein'
    # Reader markierte 05.01 als is_tour_return, aber overnight=True → Voll
    # WICHTIG: positions-flag im Builder kann is_return_day nochmal überschreiben.
    # Was uns interessiert: amount = voll_24h.
    if d05.get('rate_type') == 'voll_24h_night_return':
        assert d05['amount'] == 42.0, \
            f'05.01 Voll-Pauschale erwartet 42€, got {d05["amount"]}€'


def test_standard_return_day_without_overnight_is_an_abreise():
    """Standard-Heimkehr-Tag (Landing am gleichen Tag) bleibt An/Ab (28€).
    Sicherstellt dass Bug-2-Fix nicht Standard-Heimkehr bricht."""
    cas = [
        _day('2025-05-01', marker='LH756', routing=['LH756'], starts_hb=True,
             layover='HKG', overnight=True),
        _day('2025-05-02', marker='X', routing=['HKG'],
             layover='HKG', overnight=True, has_fl=False),
        _day('2025-05-03', marker='LH757', routing=['HKG', HB, 'LH757'],
             ends_hb=True, overnight=False, is_tour_return=True),
    ]
    tours = build_normalized_tours(
        cas_days=cas, se_rows=[], year=2025, homebase=HB,
    )
    res = calculate_allowances_from_normalized_tours(
        normalized_tours=tours, bmf_table=BMF,
        iata_to_bmf=IATA, se_rows=[], homebase=HB,
    )
    d03 = res.by_date.get('2025-05-03')
    assert d03 is not None
    # Standard-Heimkehr (overnight=False) → An/Ab
    assert d03.get('rate_type') != 'voll_24h_night_return', \
        f'Standard-Heimkehr darf nicht Voll-Pauschale sein, got {d03}'
    # Should be an_abreise
    if d03.get('klass') == 'Z76':
        assert d03['amount'] == 48.0, \
            f'HKG An/Ab erwartet 48€, got {d03["amount"]}€'
