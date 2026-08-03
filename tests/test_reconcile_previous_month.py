"""Regression: entfernte Umläufe aus dem Vormonat dürfen nicht weiter pushen."""
import os
import sys
from datetime import datetime, timedelta

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app as backend


def _event(day, summary):
    return {
        'summary': summary,
        'location': 'FRA',
        'start': day,
        'start_iso': day + 'T08:00:00',
        '_multiday_dates': [day],
    }


def _flight_day(day, flight):
    return {
        'ical_summary': f'{flight} FRA-JFK',
        'ical_start_iso': day + 'T08:00:00',
        'ical_sectors': [{
            'flight': flight,
            'from': 'FRA',
            'to': 'JFK',
            'dep_iso': day + 'T08:00:00Z',
            'arr_iso': day + 'T16:00:00Z',
        }],
        'legs': [{'flight': flight, 'from': 'FRA', 'to': 'JFK'}],
    }


def test_removed_previous_month_rotation_is_pruned_but_older_history_stays():
    this_month = datetime.now().replace(day=1)
    previous_end = this_month - timedelta(days=1)
    previous_start = previous_end.replace(day=1)
    previous_keep = previous_start.strftime('%Y-%m-%d')
    previous_removed = previous_end.strftime('%Y-%m-%d')
    current_keep = this_month.strftime('%Y-%m-%d')
    older_history = (previous_start - timedelta(days=1)).strftime('%Y-%m-%d')

    briefings = {
        older_history: _flight_day(older_history, 'LH400'),
        previous_keep: _flight_day(previous_keep, 'LH401'),
        previous_removed: _flight_day(previous_removed, 'LH441'),
        current_keep: _flight_day(current_keep, 'LH500'),
    }
    # Der neue Feed deckt Vormonat bis laufenden Monat ab, enthält LH441 aber
    # nicht mehr (z. B. Reserve-Flug statt des ursprünglichen Umlaufs).
    feed = [
        _event(previous_keep, 'LH401 FRA-JFK'),
        _event(current_keep, 'LH500 FRA-JFK'),
    ]

    result = backend._reconcile_month_briefings(
        'TESTTOKEN_PREVIOUS_MONTH', briefings, feed)

    assert previous_removed not in briefings
    assert previous_keep in briefings and current_keep in briefings
    assert older_history in briefings, 'ältere Historie bleibt eingefroren'
    assert result['window'].startswith(previous_keep)
