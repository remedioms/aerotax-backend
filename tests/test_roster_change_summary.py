"""`_roster_change_summary` — menschlicher „WAS hat sich geändert"-Text pro
Roster-Änderung (Julia Sievert 2026-07-17: „ich sehe 1 bei Kalender, aber nicht,
WAS die Änderung ist"). Aus dem im Change gespeicherten old/new abgeleitet.
"""
import os
import sys

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app as A


def _day(flight, frm, to, dep, arr, reg=None):
    s = {'flight': flight, 'from': frm, 'to': to, 'dep_iso': dep, 'arr_iso': arr}
    if reg:
        s['reg'] = reg
    return {'ical_sectors': [s], 'routing': f'{frm}-{to}'}


def test_aircraft_change_is_summarized():
    old = _day('LH 754', 'FRA', 'BLR', '2026-07-16T10:50:00Z', '2026-07-16T19:50:00Z', reg='D-AIXY')
    new = _day('LH 754', 'FRA', 'BLR', '2026-07-16T10:50:00Z', '2026-07-16T19:50:00Z', reg='D-AIMA')
    s = A._roster_change_summary({'kind': 'modified', 'old': old, 'new': new})
    assert 'D-AIXY' in s and 'D-AIMA' in s and 'Flugzeug' in s


def test_departure_time_change_is_summarized():
    old = _day('LH 123', 'FRA', 'JFK', '2026-07-16T12:40:00Z', '2026-07-16T21:00:00Z')
    new = _day('LH 123', 'FRA', 'JFK', '2026-07-16T13:10:00Z', '2026-07-16T21:30:00Z')
    s = A._roster_change_summary({'kind': 'modified', 'old': old, 'new': new})
    assert 'Abflug' in s and '→' in s


def test_added_and_removed():
    new = _day('LH 400', 'FRA', 'JFK', '2026-07-20T08:00:00Z', '2026-07-20T16:30:00Z')
    assert 'Neuer Dienst' in A._roster_change_summary({'kind': 'added', 'new': new})
    assert 'entfernt' in A._roster_change_summary({'kind': 'removed', 'old': new})


def test_never_throws_on_garbage():
    assert isinstance(A._roster_change_summary({'kind': 'modified', 'old': None, 'new': None}), str)
    assert isinstance(A._roster_change_summary({}), str)
