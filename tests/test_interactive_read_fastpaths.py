"""Regressionen fuer die interaktiven AeroX-Lese-Schnellpfade."""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import app as A
from blueprints import lh_flightops as fo


TOKEN = 'AT-1111111111111111'


def test_briefing_response_memo_reuses_merged_payload():
    calls = {'manual': 0, 'ical': 0}

    def manual(_token):
        calls['manual'] += 1
        return {'2026-08-07': {'remarks': 'ready'}}

    def ical(_token):
        calls['ical'] += 1
        return {}

    A._briefing_response_memo_invalidate(TOKEN)
    with (
        patch.object(A, '_maybe_refresh_calendar_feed'),
        patch.object(A, '_maybe_refresh_flightops'),
        patch.object(A, '_manual_briefings_load', side_effect=manual),
        patch.object(A, '_ical_briefings_load', side_effect=ical),
    ):
        with A.app.test_request_context(method='GET'):
            first = A.get_briefings(TOKEN).get_json()
        with A.app.test_request_context(method='GET'):
            second = A.get_briefings(TOKEN).get_json()

    assert first == second
    assert first['briefings']['2026-08-07']['remarks'] == 'ready'
    assert calls == {'manual': 1, 'ical': 1}
    assert A._BRIEFING_ENRICH_BUDGET_S < 1.0
    A._briefing_response_memo_invalidate(TOKEN)


def test_briefing_memo_hit_exposes_newly_warmed_boarding_marks():
    """The 20-s response memo must not conceal a completed marks warmer."""
    today = date.today().isoformat()
    dep = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime(
        '%Y-%m-%dT%H:%M:%SZ')
    data = {today: {'ical_sectors': [{
        'flight': 'LH123', 'from': 'FRA', 'to': 'MUC', 'dep_iso': dep,
    }]}}
    calls = []

    def cache_only_enrich(_token, sectors, now_ts=None):
        calls.append(len(calls) + 1)
        if len(calls) >= 2:
            sectors[0]['boarding_iso'] = dep
            return True
        return False

    A._briefing_response_memo_invalidate(TOKEN)
    with (
        patch.object(A, '_maybe_refresh_calendar_feed'),
        patch.object(A, '_maybe_refresh_flightops'),
        patch.object(A, '_manual_briefings_load', return_value=data),
        patch.object(A, '_ical_briefings_load', return_value={}),
        patch.object(A, '_enrich_leg_delays'),
        patch.object(A, '_profile_homebase_cached', return_value='FRA'),
        patch.object(fo, 'enrich_sectors_boarding',
                     side_effect=cache_only_enrich),
    ):
        with A.app.test_request_context(method='GET'):
            first = A.get_briefings(TOKEN).get_json()
        with A.app.test_request_context(method='GET'):
            second = A.get_briefings(TOKEN).get_json()

    assert 'boarding_iso' not in first['briefings'][today]['ical_sectors'][0]
    assert second['briefings'][today]['ical_sectors'][0]['boarding_iso'] == dep
    assert calls == [1, 2]
    A._briefing_response_memo_invalidate(TOKEN)


def test_wall_feed_memo_and_bulk_avatar_path_avoid_profile_n_plus_one():
    calls = {'posts': 0, 'avatars': 0}

    def posts(*_args, **_kwargs):
        calls['posts'] += 1
        return [{
            'id': 'p1', 'author_token': 'AT-2222222222222222',
            'author_name': 'Crew', 'text': 'Hello', 'ts': 10,
        }]

    def avatars(tokens):
        calls['avatars'] += 1
        assert tokens == ['AT-2222222222222222']
        return {'AT-2222222222222222': '/avatar.jpg'}

    with A._WALL_FEED_MEMO_LOCK:
        A._WALL_FEED_MEMO.clear()
    with (
        patch.object(A, '_friends_load', return_value={'friends': []}),
        patch.object(A, '_blocked_by', return_value=set()),
        patch.object(A, '_muted_by', return_value=set()),
        patch.object(A, '_wall_likes_load', return_value=set()),
        patch.object(A, '_wall_dislikes_load', return_value=set()),
        patch.object(A, '_wall_posts_load_recent', side_effect=posts),
        patch.object(A, '_wall_load_posts_from_disk', return_value=[]),
        patch.object(A, '_author_avatar_urls', side_effect=avatars),
        patch.object(A, '_profile_load', side_effect=AssertionError(
            'Wall must use the bulk avatar resolver')),
    ):
        with A.app.test_request_context('/api/wall/feed?limit=30'):
            first = A.get_wall_feed(TOKEN).get_json()
        with A.app.test_request_context('/api/wall/feed?limit=30'):
            second = A.get_wall_feed(TOKEN).get_json()

    assert first == second
    assert first['posts'][0]['author_avatar'] == '/avatar.jpg'
    assert 'author_token' not in first['posts'][0]
    assert calls == {'posts': 1, 'avatars': 1}


def test_fra_board_warm_snapshot_seeds_cold_worker(tmp_path, monkeypatch):
    rows = [{'flight': 'LH123', 'sched': '2026-08-07T12:00:00+02:00'}]
    monkeypatch.setattr(A, '_USER_HISTORY_DIR', str(tmp_path))
    monkeypatch.setattr(A, '_AIRPORT_BOARD_CACHE', {})
    A._fra_board_disk_save('departure', rows)

    def serve_seed(cache, key, _ttl, _fetch):
        assert cache[key][0] == 0.0
        return cache[key][1]

    monkeypatch.setattr(A, '_board_swr', serve_seed)
    assert A._fra_board_cached('departure') == rows


def test_airport_board_response_memo_reuses_finished_query():
    rows = [{
        'flight': 'LH123', 'airline': 'LH',
        'sched': '2099-08-07T12:00:00+02:00',
    }]
    with A._AIRPORT_BOARD_RESPONSE_MEMO_LOCK:
        A._AIRPORT_BOARD_RESPONSE_MEMO.clear()
    with (
        patch.object(A, '_fra_board_cached', return_value=rows) as fetch,
        patch.object(A, '_fra_local_now',
                     return_value=datetime(2026, 8, 7, 12, 0)),
        patch.object(A, '_queue_board_delay_merge', return_value=True) as queue,
        patch.object(A, '_departed_rows_from_store', return_value=[]),
        patch.object(A, '_board_cross_side_enrich',
                     side_effect=lambda values, _ftype: values),
    ):
        path = f'/api/airport/{TOKEN}/board?airport=FRA&type=departure&limit=80'
        with A.app.test_request_context(path):
            first = A.airport_board(TOKEN).get_json()
        with A.app.test_request_context(path):
            second = A.airport_board(TOKEN).get_json()

    assert first == second
    assert first['flights'][0]['flight'] == 'LH123'
    fetch.assert_called_once_with('departure')
    queue.assert_called_once()
    with A._AIRPORT_BOARD_RESPONSE_MEMO_LOCK:
        A._AIRPORT_BOARD_RESPONSE_MEMO.clear()


def test_board_delay_merge_queue_is_bounded_and_deduplicated(monkeypatch):
    class Pending:
        def done(self):
            return False

    class Executor:
        def __init__(self):
            self.calls = []

        def submit(self, fn, *args):
            self.calls.append((fn, args))
            return Pending()

    executor = Executor()
    monkeypatch.setattr(A, '_BOARD_DELAY_MERGE_EXECUTOR', executor)
    with A._BOARD_DELAY_MERGE_LOCK:
        A._BOARD_DELAY_MERGE_PENDING.clear()
    rows = [{'flight': 'LH123', 'sched': '2026-08-07T12:00:00+02:00'}]

    assert A._queue_board_delay_merge(rows, '2026-08-07', 'FRA') is True
    assert A._queue_board_delay_merge(rows, '2026-08-07', 'FRA') is False
    assert len(executor.calls) == 1
    # Der Worker bekommt einen Snapshot, nicht die spaeter weiterverarbeitete
    # Response-Dict-Referenz des Requests.
    rows[0]['flight'] = 'MUTATED'
    assert executor.calls[0][1][0][0]['flight'] == 'LH123'
    with A._BOARD_DELAY_MERGE_LOCK:
        A._BOARD_DELAY_MERGE_PENDING.clear()
