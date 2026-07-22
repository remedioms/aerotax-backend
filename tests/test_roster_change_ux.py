"""Roster-Änderungs-UX (Forum-Feedback 2026-07-22).

Ralf Spannagel: Ist-Zeiten-Nachträge NACH der Landung (gleicher Tag) lösten
"Dienstplan-Änderung"-Pushes aus → Vergangenheits-Gate deckt jetzt auch
beendete Dienste des HEUTIGEN Tages ab.
Birgit Schepler: 4 geänderte Tage (Reserve → StandBy/HYD-Tour) renderten alle
nur "Dienst geändert" → marker-basiertes Vorher→Nachher.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as backend  # noqa: E402


def _mod(datum, old_marker, new_marker, old_rf=None, new_rf=None,
         old_extra=None, new_extra=None):
    old = {'datum': datum, 'marker': old_marker, 'reader_facts': old_rf or {}}
    new = {'datum': datum, 'marker': new_marker, 'reader_facts': new_rf or {}}
    old.update(old_extra or {})
    new.update(new_extra or {})
    return {'datum': datum, 'kind': 'modified', 'old': old, 'new': new}


# ── Birgit: aussagekräftige Summaries ────────────────────────────────────────

def test_summary_reserve_to_standby():
    e = _mod('2026-07-22', 'Reserve', 'StandBy')
    assert backend._roster_change_summary(e) == 'Reserve → StandBy'


def test_summary_reserve_to_flight_day():
    e = _mod('2026-07-23', 'Reserve',
             '11:10 LT Briefing FRA · LH 752: FRA-HYD · Layover [HYD] (Tag 1/3)')
    s = backend._roster_change_summary(e)
    assert 'Reserve →' in s and 'FRA-HYD' in s, s


def test_summary_reserve_to_layover_day():
    e = _mod('2026-07-24', 'Reserve', 'Layover [HYD] (Tag 2/3)')
    s = backend._roster_change_summary(e)
    assert s == 'Reserve → Layover [HYD]', s


def test_summary_generic_when_identical_markers():
    e = _mod('2026-07-24', 'Office Day', 'Office Day')
    assert backend._roster_change_summary(e) == 'Dienst geändert'


# ── Ralf: Vergangenheits-Gate für beendete Tages-Dienste ─────────────────────

def test_past_gate_same_day_ended_duty_suppressed():
    # Flug heute, laut neuer Endzeit um 14:05 gelandet — jetzt ist 18:30.
    e = _mod('2026-07-22', 'LH 400: FRA-JFK', 'LH 400: FRA-JFK',
             old_rf={'start_time': '08:15', 'end_time': '13:50'},
             new_rf={'start_time': '08:15', 'end_time': '14:05'})
    assert backend._roster_change_is_past(e, '2026-07-22', '18:30') is True
    # Vor dem Dienst-Ende bleibt der Push aktiv.
    assert backend._roster_change_is_past(e, '2026-07-22', '11:00') is False


def test_past_gate_redeye_never_suppressed_same_day():
    # Red-Eye: Ende 05:40 liegt am FOLGETAG (end < start) → nie unterdrücken.
    e = _mod('2026-07-22', 'LH 756: FRA-BLR', 'LH 756: FRA-BLR',
             new_rf={'start_time': '21:30', 'end_time': '05:40'})
    assert backend._roster_change_is_past(e, '2026-07-22', '23:00') is False


def test_past_gate_added_future_duty_today_pushes():
    e = {'datum': '2026-07-22', 'kind': 'added',
         'new': {'datum': '2026-07-22', 'marker': 'StandBy',
                 'reader_facts': {'start_time': '16:00', 'end_time': '23:00'}}}
    assert backend._roster_change_is_past(e, '2026-07-22', '12:00') is False


def test_past_gate_yesterday_still_suppressed():
    e = _mod('2026-07-21', 'LH 400: FRA-JFK', 'LH 400: FRA-JFK')
    assert backend._roster_change_is_past(e, '2026-07-22', '09:00') is True
