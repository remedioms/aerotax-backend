"""FR24-Korridor-Treffer → OpenSky-Row: Ehrlichkeit der on_ground-Ableitung.

Owner 19.08.: der FlightDeck-Tap „soll immer einmal beim Klick suchen" — ein
Treffer ohne Höhe/Speed darf deshalb NICHT als on_ground=True geliefert werden
(der Client verwirft Boden-Treffer), sondern ehrlich als None wie bei den
übrigen Quellen (ADB/Watch).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: F401 — registriert das Blueprint genau einmal (Muster tier3)
from blueprints.adsb_blueprint import _fr24_corridor_position_row


BASE_HIT = {'lat': 50.05, 'lon': 8.57, 'callsign': 'DLH123',
            'reg': 'D-AIXA', 'hex': '3c4b26', 'obs_ts': 1_766_000_000}


def test_airborne_hit_maps_to_full_row():
    row = _fr24_corridor_position_row({**BASE_HIT, 'alt': 36000, 'speed': 470,
                                       'track': 92.0})
    assert row[0] == '3c4b26'
    assert row[1] == 'DLH123'
    assert row[2] == 'D-AIXA'
    assert row[8] is False                       # klar in der Luft
    assert abs(row[7] - 36000 * 0.3048) < 1      # Meter
    assert abs(row[9] - 470 * 0.514444) < 0.1    # m/s


def test_missing_alt_and_speed_is_not_claimed_on_ground():
    row = _fr24_corridor_position_row(dict(BASE_HIT))
    assert row is not None
    assert row[8] is None                        # kein Beleg → kein Boden


def test_low_alt_and_speed_is_on_ground():
    row = _fr24_corridor_position_row({**BASE_HIT, 'alt': 30, 'speed': 8})
    assert row[8] is True


def test_missing_hex_falls_back_to_flight_id_key():
    hit = {k: v for k, v in BASE_HIT.items() if k != 'hex'}
    row = _fr24_corridor_position_row({**hit, 'flight_id': '3A2B1C',
                                       'alt': 36000, 'speed': 470})
    assert row[0] == 'fr24-3a2b1c'


def test_millisecond_timestamps_are_normalized():
    row = _fr24_corridor_position_row({**BASE_HIT, 'obs_ts': 1_766_000_000_000,
                                       'alt': 36000, 'speed': 470})
    assert row[3] == 1_766_000_000
