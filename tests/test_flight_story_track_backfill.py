"""Flight-story contract: a dense but truncated track is not a full flight.

The regression was visible as LH780 FRA-SIN with 3,357 km and an artificial
straight tail to Singapore.  FR24 Playback must replace that partial history,
and the endpoint must publish provider-ready stats to the app.
"""
import datetime as dt
import os
import time
from unittest.mock import patch

import pytest

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import app as A
import blueprints.adsb_blueprint as ADSB
import blueprints.aerox_data_blueprint as BP
import blueprints.fr24_grpc as FR24


AIRPORTS = {
    'FRA': (50.0267, 8.5583),
    'SIN': (1.3644, 103.9915),
}


@pytest.fixture(autouse=True)
def _pin_app_module():
    import sys
    previous = sys.modules.get('app')
    sys.modules['app'] = A
    yield
    if previous is not None:
        sys.modules['app'] = previous


def _partial_track(base_ts):
    return [
        {'lat': AIRPORTS['FRA'][0], 'lon': AIRPORTS['FRA'][1],
         'alt': 0, 'gs': 12, 'trk': 70, 'ts': base_ts},
        {'lat': 48.0, 'lon': 20.0, 'alt': 33_000,
         'gs': 510, 'trk': 110, 'ts': base_ts + 7_200},
        {'lat': 40.18, 'lon': 48.47, 'alt': 33_000,
         'gs': 537, 'trk': 101, 'ts': base_ts + 13_800},
    ]


def _fr24_trail(base_ts):
    return {
        'flightid': 0x39ABCDEF,
        'reg': 'DABVM',
        'flight': 'LH780',
        'origin': 'FRA',
        'dest': 'SIN',
        'duration_min': 735,
        'points': [
            {'lat': AIRPORTS['FRA'][0], 'lon': AIRPORTS['FRA'][1],
             'alt_ft': 0, 'gs_kt': 10, 'track_deg': 70,
             'ts': str(base_ts)},
            {'lat': 48.0, 'lon': 30.0, 'alt_ft': 35_000,
             'gs_kt': 510, 'track_deg': 105,
             'ts': str(base_ts + 14_000)},
            {'lat': 28.0, 'lon': 70.0, 'alt_ft': 37_000,
             'gs_kt': 500, 'track_deg': 120,
             'ts': str(base_ts + 28_000)},
            {'lat': AIRPORTS['SIN'][0], 'lon': AIRPORTS['SIN'][1],
             'alt_ft': 0, 'gs_kt': 20, 'track_deg': 190,
             'ts': str(base_ts + 44_100)},
        ],
    }


def _call(trail, live_fid=0x39ABCDEF, *, story=True, day_offset=-1):
    service_date = (dt.datetime.now(dt.timezone.utc).date()
                    + dt.timedelta(days=day_offset))
    date = service_date.isoformat()
    base_ts = int(dt.datetime.combine(
        service_date, dt.time(10, 0), tzinfo=dt.timezone.utc).timestamp())
    partial = _partial_track(base_ts)
    db_result = (partial, 'DABVM', 'FRA', 'SIN', False)

    with patch.object(BP, '_memo_get', return_value=None), \
            patch.object(BP, '_flown_track_db', return_value=db_result) as db_fetch, \
            patch.object(BP, '_aircraft_live_pos', return_value=(None, None, None, None)), \
            patch.object(BP, '_aircraft_live_flightid', return_value=live_fid), \
            patch.object(BP, '_fr24_flight_by_number', return_value={
                'fr24_id': '39abcdef', 'dep_iata': 'FRA', 'arr_iata': 'SIN',
                'sched_dep': dt.datetime.fromtimestamp(
                    base_ts, tz=dt.timezone.utc).isoformat(), 'reg': 'DABVM',
            }) as summary, \
            patch.object(BP, '_flown_track_writeback'), \
            patch.object(BP, '_iata_latlon', side_effect=lambda code: AIRPORTS.get(code)), \
            patch.object(FR24, 'flown_trail_by_flightid', return_value=trail) as fetch, \
            patch.object(ADSB, '_rate_limited', return_value=False):
        with A.app.test_request_context(
                f'/api/ax/flown-track?reg=DABVM&flight_no=LH780&date={date}'
                '&dep=FRA&arr=SIN' + ('&story=1' if story else '')):
            response = BP.ax_flown_track()
    if isinstance(response, tuple):
        response = response[0]
    return response.get_json(), fetch, summary, base_ts, response.headers, db_fetch


def test_recent_partial_track_is_replaced_by_complete_fr24_playback():
    service_date = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    base_ts = int(dt.datetime.combine(
        service_date, dt.time(10, 0), tzinfo=dt.timezone.utc).timestamp())
    body, fetch, summary, _, _, db_fetch = _call(_fr24_trail(base_ts))

    assert body['source'] == 'fr24_trail'
    assert body['track_complete'] is True
    assert body['count'] >= 4
    assert body['distance_km'] > 9_000
    assert body['duration_min'] == 735
    assert body['max_altitude_ft'] == 37_000
    assert body['points'][-1]['lat'] == pytest.approx(AIRPORTS['SIN'][0])
    assert fetch.call_args.kwargs['timestamp'] is not None
    summary.assert_not_called()
    assert db_fetch.call_args.args[5] == (
        dt.datetime.combine(service_date, dt.time(), tzinfo=dt.timezone.utc)
        + dt.timedelta(hours=36)).strftime('%Y-%m-%dT%H:%M:%SZ')


def test_story_never_buys_a_summary_when_free_flight_id_was_pruned():
    service_date = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    base_ts = int(dt.datetime.combine(
        service_date, dt.time(10, 0), tzinfo=dt.timezone.utc).timestamp())
    body, fetch, summary, _, _, _ = _call(
        _fr24_trail(base_ts), live_fid=None)

    summary.assert_not_called()
    fetch.assert_not_called()
    assert body['source'] == 'aircraft_track'
    assert body['track_complete'] is False
    assert body['distance_km'] is None


def test_normal_historical_route_does_not_activate_story_playback():
    service_date = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    base_ts = int(dt.datetime.combine(
        service_date, dt.time(10, 0), tzinfo=dt.timezone.utc).timestamp())
    body, fetch, summary, _, headers, db_fetch = _call(
        _fr24_trail(base_ts), story=False)

    fetch.assert_not_called()
    summary.assert_not_called()
    assert body['source'] == 'aircraft_track'
    assert body['track_complete'] is False
    assert headers['Cache-Control'] == 'public, max-age=86400'
    assert db_fetch.call_args.args[5] == (
        dt.datetime.combine(service_date + dt.timedelta(days=1), dt.time(),
                            tzinfo=dt.timezone.utc)
        .strftime('%Y-%m-%dT%H:%M:%SZ'))


def test_live_route_keeps_its_free_fr24_gap_filler():
    service_date = dt.datetime.now(dt.timezone.utc).date()
    base_ts = int(dt.datetime.combine(
        service_date, dt.time(0, 5), tzinfo=dt.timezone.utc).timestamp())
    body, fetch, summary, _, _, _ = _call(
        _fr24_trail(base_ts), story=False, day_offset=0)

    assert body['source'] == 'fr24_trail'
    assert body['track_complete'] is True
    assert fetch.call_args.kwargs['timestamp'] is None
    summary.assert_not_called()


def test_partial_track_never_publishes_a_misleading_distance_or_duration():
    body, _, _, _, _, _ = _call(None)

    assert body['source'] == 'aircraft_track'
    assert body['track_complete'] is False
    assert body['distance_km'] is None
    assert body['duration_min'] is None
    # A measured cruise altitude is still a valid fact, even when the route is
    # incomplete; the app simply waits for the complete story before sharing.
    assert body['max_altitude_ft'] == 33_000


def test_playback_shape_reads_nested_fr24_actuals_and_identity():
    shaped = FR24._shape_trail({'detail': {
        'aircraft_info': {'reg': 'D-ABVM'},
        'schedule_info': {
            'flight_number': 'LH780',
            'actual_departure': 1_786_095_000,
            'actual_arrival': 1_786_139_100,
        },
        'flight_info': {'flightid': 0x39ABCDEF},
        'flight_trail_list': [
            {'lat': 50.02, 'lon': 8.56, 'heading': 70,
             'snapshot_id': '1786095000'},
            {'lat': 1.36, 'lon': 103.99, 'altitude': 37_000,
             'spd': 20, 'heading': 190, 'snapshot_id': '1786139100'},
        ],
    }})

    assert shaped['flightid'] == 0x39ABCDEF
    assert shaped['flight'] == 'LH780'
    assert shaped['reg'] == 'D-ABVM'
    assert shaped['duration_min'] == 735
    assert shaped['points'][-1]['alt_ft'] == 37_000
    assert shaped['points'][-1]['gs_kt'] == 20
    assert shaped['points'][-1]['track_deg'] == 190
    assert shaped['points'][-1]['ts'] == 1_786_139_100


def test_incomplete_story_remains_short_cached_without_paid_fallback():
    trail = _fr24_trail(int(time.time()) - 6 * 3600)
    trail['duration_min'] = None
    trail['points'][-1].update({
        'lat': 28.82, 'lon': 71.90, 'alt_ft': 35_000,
        'gs_kt': 495, 'track_deg': 138, 'ts': int(time.time()),
    })
    body, _, _, _, headers, _ = _call(trail)

    assert body['source'] == 'fr24_trail'
    assert body['track_complete'] is False
    assert body['in_flight'] is False
    assert body['distance_km'] is None
    assert body['duration_min'] is None
    assert headers['Cache-Control'] == 'public, max-age=45'


def test_official_summary_keeps_the_playback_flight_id():
    raw = {
        'fr24_id': '39abcdef', 'flight': 'LH780', 'callsign': 'DLH780',
        'orig_icao': 'EDDF', 'dest_icao': 'WSSS',
        'datetime_takeoff': '2026-08-07T10:00:00Z',
        'datetime_landed': '2026-08-07T22:15:00Z',
        'flight_ended': True, 'reg': 'D-ABVM', 'type': 'B744',
    }
    with patch.object(BP, '_icao_to_iata', side_effect={
            'EDDF': 'FRA', 'WSSS': 'SIN'}.get):
        leg = BP._fr24_summary_to_leg(raw)

    assert leg['fr24_id'] == '39abcdef'
    assert leg['src'] == 'FRA' and leg['dst'] == 'SIN'
