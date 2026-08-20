"""AeroDataBox allowance is exclusive to the user-opened live departure board."""
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as A  # noqa: E402
import blueprints.aerox_data_blueprint as D  # noqa: E402
from blueprints import paid_cost_control as PCC  # noqa: E402


def _flight_row():
    return {
        'number': 'LH 123', 'status': 'Scheduled',
        'airline': {'iata': 'LH', 'name': 'Lufthansa'},
        'aircraft': {'reg': 'D-AIXA', 'model': 'Airbus A350'},
        'departure': {
            'airport': {'iata': 'FRA', 'name': 'Frankfurt'},
            'scheduledTime': {'local': '2026-08-20 18:00+02:00'},
            'revisedTime': {'local': '2026-08-20 18:05+02:00'},
            'gate': 'Z52', 'terminal': '1',
        },
        'arrival': {'airport': {'iata': 'LAX', 'name': 'Los Angeles'}},
    }


def test_upstream_uses_one_12h_call_and_opposite_airport(monkeypatch):
    response = MagicMock(status_code=200)
    response.json.return_value = {'departures': [_flight_row()]}
    get = MagicMock(return_value=response)
    monkeypatch.setenv('AERODATABOX_KEY', 'test-key')
    monkeypatch.setattr('requests.get', get)
    monkeypatch.setattr(A, '_airport_local_now',
                        lambda _iata: datetime(2026, 8, 20, 10, tzinfo=timezone.utc))

    payload = A._aerodatabox_board_upstream('FRA', 'departure')

    assert get.call_count == 1
    assert payload['data'][0]['dest_iata'] == 'LAX'
    assert payload['data'][0]['sched'] == '2026-08-20T18:00+02:00'
    assert payload['data'][0]['gate'] == 'Z52'


def test_upstream_classifies_rate_limit(monkeypatch):
    response = MagicMock(status_code=429)
    monkeypatch.setenv('AERODATABOX_KEY', 'test-key')
    monkeypatch.setattr('requests.get', MagicMock(return_value=response))
    assert A._aerodatabox_board_upstream('LAX') == {
        '_ax_error': 'rate_limited'}


def test_paid_board_is_shared_and_budgeted_at_two_units(monkeypatch):
    PCC.reset_local_state()
    D._MEM_BUDGET.clear()
    calls = MagicMock(return_value={'data': [_flight_row()]})
    monkeypatch.setenv('AERODATABOX_KEY', 'test-key')
    monkeypatch.setenv('AX_PAID_CONTROL_ALLOW_LOCAL', '1')
    monkeypatch.setattr(D, '_sb', lambda: None)
    monkeypatch.setattr(A, '_aerodatabox_board_upstream', calls)

    first = A._aerodatabox_board('LAX', 'departure')
    second = A._aerodatabox_board('LAX', 'departure')

    assert first[0] == second[0] and first[1] is None
    assert calls.call_count == 1
    day_key, month_key = A._aerodatabox_board_budget_keys()
    assert D._MEM_BUDGET[day_key] == 2
    assert D._MEM_BUDGET[month_key] == 2


def test_background_poller_never_opts_into_paid_board(monkeypatch):
    seen = []

    def board(ap, ft, allow_paid=False):
        seen.append((ap, ft, allow_paid))
        return (None, None)

    monkeypatch.setattr(A, '_native_board_cached', board)
    assert A._poll_boards_once(['CDG'])['CDG'] == 'no_flights'
    assert seen == [('CDG', 'departure', False)]


def test_user_departure_board_uses_paid_fallback_after_free_misses(monkeypatch):
    board_row = {
        'airline': 'LH', 'flight': 'LH 123', 'dest_iata': 'LAX',
        'sched': '2099-08-20T18:00:00+02:00', 'esti': None,
        'gate': 'Z52', 'terminal': '1', 'status': 'Scheduled',
    }
    monkeypatch.setattr(A, '_native_board_cached',
                        lambda *a, **k: (None, None))
    monkeypatch.setattr(A, '_board_rows_from_obs_for_date', lambda *a, **k: [])
    monkeypatch.setattr(A, '_fetch_opensky_board', lambda *a, **k: None)
    monkeypatch.setattr(A, '_aerodatabox_board',
                        lambda *a, **k: ([board_row], None))
    monkeypatch.setattr(A, '_queue_board_delay_merge', lambda *a, **k: None)
    A._AIRPORT_BOARD_RESPONSE_MEMO.clear()
    A._BOARD_LAST_GOOD.clear()

    with A.app.test_request_context(
            '/api/airport/AT-TEST/board?airport=LAX&type=departure'):
        response = A.airport_board('AT-TEST')

    body = response.get_json()
    assert body['ok'] is True
    assert body['source'] == 'aerodatabox_live_board'
    assert body['flights'][0]['flight'] == 'LH 123'


def test_non_board_aerodatabox_is_off_by_default(monkeypatch):
    monkeypatch.setenv('AERODATABOX_KEY', 'test-key')
    monkeypatch.delenv('ADB_ALLOW_NON_BOARD', raising=False)
    assert A._aerodatabox_non_board_enabled() is False
    assert D._aerodatabox_route('DLH123') is None
