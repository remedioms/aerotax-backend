"""Regressionen fuer den Friends-Listen-Request-Sturm aus AeroX 2.2.2."""
import concurrent.futures
import threading
import time

import app as A


def _clear():
    with A._USER_FRIENDS_PAYLOAD_LOCK:
        A._USER_FRIENDS_PAYLOAD_MEMO.clear()
        A._USER_FRIENDS_PAYLOAD_KEY_LOCKS.clear()
        A._USER_FRIENDS_PAYLOAD_GENERATION.clear()


def test_repeated_friends_payload_builds_only_once(monkeypatch):
    _clear()
    calls = []

    def build(token):
        calls.append(token)
        return {'token': token, 'friends': [], 'count': 0}

    monkeypatch.setattr(A, '_build_user_friends_payload', build)
    first = A._user_friends_payload_cached('viewer-a')
    second = A._user_friends_payload_cached('viewer-a')

    assert first == second
    assert calls == ['viewer-a']


def test_concurrent_friends_requests_share_one_build(monkeypatch):
    """Neun parallele Mounts duerfen nur EINEN teuren Backend-Build ausloesen."""
    _clear()
    calls = 0
    calls_lock = threading.Lock()

    def build(token):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.05)
        return {'token': token, 'friends': [{'token': 'friend'}], 'count': 1}

    monkeypatch.setattr(A, '_build_user_friends_payload', build)
    with concurrent.futures.ThreadPoolExecutor(max_workers=9) as pool:
        results = list(pool.map(
            lambda _: A._user_friends_payload_cached('viewer-a'), range(9)))

    assert calls == 1
    assert all(result == results[0] for result in results)


def test_friends_cache_is_user_scoped_and_invalidates(monkeypatch):
    _clear()
    calls = []

    def build(token):
        calls.append(token)
        return {'token': token, 'friends': [], 'count': 0}

    monkeypatch.setattr(A, '_build_user_friends_payload', build)
    A._user_friends_payload_cached('viewer-a')
    A._user_friends_payload_cached('viewer-b')
    A._user_friends_payload_invalidate('viewer-a')
    A._user_friends_payload_cached('viewer-a')
    A._user_friends_payload_cached('viewer-b')

    assert calls == ['viewer-a', 'viewer-b', 'viewer-a']


def test_failed_friends_build_is_never_cached(monkeypatch):
    _clear()
    attempts = 0

    def build(token):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError('temporary backend failure')
        return {'token': token, 'friends': [], 'count': 0}

    monkeypatch.setattr(A, '_build_user_friends_payload', build)
    try:
        A._user_friends_payload_cached('viewer-a')
    except RuntimeError:
        pass
    result = A._user_friends_payload_cached('viewer-a')

    assert result['count'] == 0
    assert attempts == 2


def test_invalidation_during_build_cannot_recache_stale_payload(monkeypatch):
    _clear()
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def build(token):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            assert release.wait(timeout=1)
        return {'token': token, 'friends': [], 'count': calls}

    monkeypatch.setattr(A, '_build_user_friends_payload', build)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(A._user_friends_payload_cached, 'viewer-a')
        assert started.wait(timeout=1)
        A._user_friends_payload_invalidate('viewer-a')
        release.set()
        assert first.result(timeout=1)['count'] == 1

    # Der waehrend der Mutation gebaute alte Snapshot wurde nicht gespeichert.
    assert A._user_friends_payload_cached('viewer-a')['count'] == 2
