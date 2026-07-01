"""BUG-005 P0-Fix Tests: Supabase-Timeout + _jobs_lock-Disziplin.

Verhindert dass eine hängende `sb.table('jobs').execute()` 30s lang den
Recall-/Job-Endpoint blockiert.

Verifiziert:
1. _supabase_execute_with_timeout: returns (None, True) on timeout
2. _load_job_from_persistence: returns (None, True) when supabase hangs
3. _get_or_load_job: does NOT hold _jobs_lock during supabase I/O
4. /api/session/<token>: returns 503 fetch_error when supabase hangs
5. /api/session/<token>: returns 503 fetch_error when job-load hangs
6. requires_session_token decorator: returns 503 fetch_error when job-load hangs
7. Static: no network/supabase call inside `with _jobs_lock:` blocks (sample)
8. /api/session/<valid-token-from-supabase>: loads from Supabase when memory empty
"""
import os
import conftest as _cft
import sys
import time
import threading
import importlib
import pytest


@pytest.fixture
def fresh_app(monkeypatch):
    """Reload app.py freshly per test so we can monkeypatch its globals."""
    monkeypatch.setenv('AEROTAX_EXECUTION_MODE', 'thread')
    sys.modules.pop('app', None)
    import app
    app.app.config['TESTING'] = True
    return app


@pytest.fixture
def client(fresh_app):
    return fresh_app.app.test_client()


# ─── 1. _supabase_execute_with_timeout helper ──────────────────────────────

def test_supabase_timeout_helper_returns_none_on_hang(fresh_app):
    """fn() blocks 10s → helper returns (None, True) after 1s timeout."""
    def _hang():
        time.sleep(10)
        return 'should-never-reach'
    start = time.time()
    result, timed_out = fresh_app._supabase_execute_with_timeout(
        'test-hang', _hang, timeout_s=1
    )
    duration = time.time() - start
    assert result is None
    assert timed_out is True
    assert duration < 2.0, f'timeout overhead too high: {duration:.2f}s'


def test_supabase_timeout_helper_returns_result_when_fast(fresh_app):
    """fn() returns quickly → helper returns (result, False)."""
    def _fast():
        return 'fast-result'
    result, timed_out = fresh_app._supabase_execute_with_timeout(
        'test-fast', _fast, timeout_s=5
    )
    assert result == 'fast-result'
    assert timed_out is False


def test_supabase_timeout_helper_returns_none_on_exception(fresh_app):
    """fn() raises → helper catches and returns (None, True)."""
    def _crash():
        raise RuntimeError('boom')
    result, timed_out = fresh_app._supabase_execute_with_timeout(
        'test-crash', _crash, timeout_s=2
    )
    assert result is None
    assert timed_out is True


# ─── 2. _load_job_from_persistence with timeout ─────────────────────────────

def test_load_job_from_persistence_returns_timeout_flag(fresh_app, monkeypatch):
    """Wenn supabase.execute() hängt → (None, True) statt endlos hängen."""
    fresh_app.SB_AVAILABLE = True

    class _SlowQuery:
        def select(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def limit(self, *a, **k): return self
        def execute(self):
            time.sleep(10)
            return None

    class _SlowSB:
        def table(self, *a, **k):
            return _SlowQuery()

    monkeypatch.setattr(fresh_app, 'sb', _SlowSB())
    # Override _supabase_execute_with_timeout's timeout to 1s for this test
    orig = fresh_app._supabase_execute_with_timeout
    monkeypatch.setattr(
        fresh_app, '_supabase_execute_with_timeout',
        lambda label, fn, timeout_s=5: orig(label, fn, timeout_s=1)
    )

    start = time.time()
    result, timed_out = fresh_app._load_job_from_persistence('test-job-id')
    duration = time.time() - start
    assert result is None
    assert timed_out is True
    assert duration < 2.0, f'load took {duration:.2f}s, should be <2s'


def test_load_job_from_persistence_not_found_no_timeout(fresh_app, monkeypatch):
    """Wenn supabase fast eine leere Liste returnt: (None, False) — kein timeout."""
    fresh_app.SB_AVAILABLE = True

    class _EmptyResult:
        data = []

    class _EmptyQuery:
        def select(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def limit(self, *a, **k): return self
        def execute(self): return _EmptyResult()

    class _EmptySB:
        def table(self, *a, **k): return _EmptyQuery()

    monkeypatch.setattr(fresh_app, 'sb', _EmptySB())
    result, timed_out = fresh_app._load_job_from_persistence('non-existent')
    assert result is None
    assert timed_out is False


# ─── 3. _get_or_load_job releases _jobs_lock during I/O ────────────────────

def test_get_or_load_job_does_not_hold_lock_during_supabase(fresh_app, monkeypatch):
    """KRITISCH: während supabase-Query darf _jobs_lock NICHT gehalten werden.
    Sonst blockieren alle anderen Lock-User für die ganze Query-Duration."""
    fresh_app._jobs.clear()
    fresh_app.SB_AVAILABLE = True

    lock_acquired_during_query = []
    query_started = threading.Event()
    query_finished = threading.Event()

    class _BlockingQuery:
        def select(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def limit(self, *a, **k): return self
        def execute(self):
            query_started.set()
            # Während dieser Sekunde sollte ein anderer Thread den Lock bekommen
            time.sleep(0.5)
            query_finished.set()

            class _Result: data = []
            return _Result()

    class _BlockingSB:
        def table(self, *a, **k): return _BlockingQuery()

    monkeypatch.setattr(fresh_app, 'sb', _BlockingSB())

    def _try_acquire_lock_during_query():
        query_started.wait(timeout=5)
        if not query_finished.is_set():
            acquired = fresh_app._jobs_lock.acquire(blocking=False)
            lock_acquired_during_query.append(acquired)
            if acquired:
                fresh_app._jobs_lock.release()

    t = threading.Thread(target=_try_acquire_lock_during_query)
    t.start()
    fresh_app._get_or_load_job('some-job-id')
    t.join(timeout=5)

    assert lock_acquired_during_query == [True], (
        f'Lock war während supabase-Query GEHALTEN — anderer Thread konnte ihn nicht greifen. '
        f'lock_acquired_during_query={lock_acquired_during_query}'
    )


def test_get_or_load_job_returns_memory_status_when_in_jobs(fresh_app):
    fresh_app._jobs['test-id-1'] = {'job_id': 'test-id-1', 'status': 'done'}
    job, status = fresh_app._get_or_load_job('test-id-1')
    assert job == {'job_id': 'test-id-1', 'status': 'done'}
    assert status == 'memory'


def test_get_or_load_job_returns_timeout_status_when_supabase_hangs(fresh_app, monkeypatch):
    fresh_app._jobs.clear()
    fresh_app.SB_AVAILABLE = True

    class _HangQuery:
        def select(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def limit(self, *a, **k): return self
        def execute(self):
            time.sleep(10)

    class _HangSB:
        def table(self, *a, **k): return _HangQuery()

    monkeypatch.setattr(fresh_app, 'sb', _HangSB())
    orig = fresh_app._supabase_execute_with_timeout
    monkeypatch.setattr(
        fresh_app, '_supabase_execute_with_timeout',
        lambda label, fn, timeout_s=5: orig(label, fn, timeout_s=1)
    )

    job, status = fresh_app._get_or_load_job('test-hang-id')
    assert job is None
    assert status == 'timeout'


def test_get_or_load_job_returns_not_found_for_empty_result(fresh_app, monkeypatch):
    fresh_app._jobs.clear()
    fresh_app.SB_AVAILABLE = True

    class _EmptyResult: data = []
    class _EmptyQuery:
        def select(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def limit(self, *a, **k): return self
        def execute(self): return _EmptyResult()
    class _EmptySB:
        def table(self, *a, **k): return _EmptyQuery()

    monkeypatch.setattr(fresh_app, 'sb', _EmptySB())
    job, status = fresh_app._get_or_load_job('non-existent-id')
    assert job is None
    assert status == 'not_found'


# ─── 4. /api/session/<token> fetch_error response on supabase hang ──────────

def test_session_endpoint_returns_503_when_session_load_times_out(client, fresh_app, monkeypatch):
    """Wenn _load_session_safe einen timeout-flag returnt → HTTP 503 mit fetch_error."""
    def _hang_session(token):
        # _load_session_safe expects (None, True) on timeout
        return (None, True)
    monkeypatch.setattr(fresh_app, '_load_session_safe', _hang_session)

    start = time.time()
    r = client.get('/api/session/AT-TEST-TOKEN-123')
    duration = time.time() - start
    assert r.status_code == 503
    body = r.get_json()
    assert body['canonical_state'] == 'fetch_error'
    assert body['reason_code'] == 'SUPABASE_TIMEOUT'
    assert body['pdf_allowed'] is False
    assert body['retry_allowed'] is True
    assert duration < 3.0, f'endpoint took {duration:.2f}s'


def test_session_endpoint_returns_503_when_job_load_times_out(client, fresh_app, monkeypatch):
    """Wenn _get_or_load_job returnt status='timeout' → HTTP 503 mit fetch_error."""
    def _fake_session(token):
        return ({
            'token': token, 'job_id': 'some-job-id',
            'result_data': {'netto': 100}, 'notes': [], 'download_url': None,
            'chat_history': [], 'expires': '2030-01-01T00:00:00Z',
        }, False)
    monkeypatch.setattr(fresh_app, '_load_session_safe', _fake_session)
    monkeypatch.setattr(fresh_app, '_get_or_load_job', lambda jid: (None, 'timeout'))

    r = client.get('/api/session/AT-VALID-TOKEN-456')
    assert r.status_code == 503
    body = r.get_json()
    assert body['canonical_state'] == 'fetch_error'
    assert body['reason_code'] == 'SUPABASE_TIMEOUT'


def test_session_endpoint_normal_path_still_works(client, fresh_app, monkeypatch):
    """Wenn alles fast geht: HTTP 200 mit canonical_state vom Job."""
    fake_session = {
        'token': 'AT-OK', 'job_id': 'memory-job',
        'result_data': {'netto': 1430.60}, 'notes': [],
        'download_url': '/api/download/foo', 'chat_history': [],
        'expires': '2030-01-01T00:00:00Z',
    }
    fake_job = {
        'job_id': 'memory-job', 'status': 'done',
        'data': {'_review_items': []},
    }
    monkeypatch.setattr(fresh_app, '_load_session_safe', lambda t: (fake_session, False))
    monkeypatch.setattr(fresh_app, '_get_or_load_job', lambda jid: (fake_job, 'memory'))

    r = client.get('/api/session/AT-OK')
    assert r.status_code == 200
    body = r.get_json()
    assert body['canonical_state'] in ('done', 'done_clean')
    assert body['pdf_allowed'] is True
    assert body['result_data']['netto'] == 1430.60


# ─── 5. requires_session_token: fetch_error on timeout ──────────────────────

def test_requires_session_token_decorator_returns_503_on_timeout(client, fresh_app, monkeypatch):
    """Decorator vor /api/job/<id>/audit: bei load-timeout → 503 statt 30s-Hang."""
    monkeypatch.setattr(fresh_app, '_get_or_load_job', lambda jid: (None, 'timeout'))
    start = time.time()
    r = client.get(
        '/api/job/some-job-id/audit',
        headers={'X-Session-Token': 'AT-FAKE'},
    )
    duration = time.time() - start
    assert r.status_code == 503
    body = r.get_json()
    assert body['canonical_state'] == 'fetch_error'
    assert duration < 3.0


def test_requires_session_token_decorator_returns_404_on_not_found(client, fresh_app, monkeypatch):
    monkeypatch.setattr(fresh_app, '_get_or_load_job', lambda jid: (None, 'not_found'))
    r = client.get(
        '/api/job/nonexistent-id/audit',
        headers={'X-Session-Token': 'AT-FAKE'},
    )
    assert r.status_code == 404


# ─── 6. Valid session after memory empty must load from persistence ────────

def test_valid_session_after_memory_empty_loads_from_supabase(client, fresh_app, monkeypatch):
    """User-Spec: nach Restart muss /api/session/<valid-token> aus Supabase laden <2s."""
    fresh_app._jobs.clear()
    fresh_app.SB_AVAILABLE = True

    # Fake fast Supabase that returns a job for jobs query, session for sessions query
    fake_job_data = {
        'job_id': 'persisted-job-789',
        'status': 'done',
        'data': {'_review_items': [], 'netto': 1430.60},
    }
    fake_session_row = {
        'token': 'AT-RESTART-TOKEN',
        'job_id': 'persisted-job-789',
        'result_data': {'netto': 1430.60},
        'notes': [], 'download_url': None, 'chat_history': [],
        'expires_at': '2030-01-01T00:00:00Z',
    }

    class _JobsResult:
        data = [{'data': fake_job_data}]
    class _SessionsResult:
        data = [fake_session_row]
    class _Query:
        def __init__(self, table_name):
            self.table_name = table_name
        def select(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def limit(self, *a, **k): return self
        def execute(self):
            return _JobsResult() if self.table_name == 'jobs' else _SessionsResult()
    class _FastSB:
        def table(self, name):
            return _Query(name)

    monkeypatch.setattr(fresh_app, 'sb', _FastSB())

    start = time.time()
    r = client.get('/api/session/AT-RESTART-TOKEN')
    duration = time.time() - start

    assert r.status_code == 200, f'got {r.status_code}: {r.data[:200]}'
    body = r.get_json()
    assert body['canonical_state'] in ('done', 'done_clean')
    assert duration < 2.0, f'load took {duration:.2f}s — should be <2s'


# ─── 7. Static check: no supabase call inside critical lock blocks ─────────

def test_static_session_recall_does_not_use_old_lock_pattern():
    """Statisch: /api/session/<token>-Handler verwendet NICHT mehr
    `with _jobs_lock: ... _load_job_from_disk(...)`."""
    src = open(_cft.backend_path('app.py')).read()
    # Find the session_recall function
    idx = src.find("def session_recall(token):")
    assert idx > 0
    block = src[idx:idx + 2000]
    # Critical: no `_jobs_lock` acquisition that wraps `_load_job_from_disk`
    # (the OLD pattern that caused 30s hangs)
    assert 'with _jobs_lock' not in block, (
        '/api/session/<token>-Handler hält _jobs_lock noch — Lock-during-I/O fixme nicht durchgeführt'
    )
    # New pattern: uses _get_or_load_job
    assert '_get_or_load_job' in block


def test_static_decorator_uses_get_or_load_job():
    src = open(_cft.backend_path('app.py')).read()
    idx = src.find("def requires_session_token(fn):")
    assert idx > 0
    block = src[idx:idx + 2500]
    assert '_get_or_load_job' in block
    # Old pattern weg
    assert 'with _jobs_lock' not in block or block.find('_get_or_load_job') < block.find('with _jobs_lock')


def test_static_load_job_from_disk_uses_timeout_helper():
    src = open(_cft.backend_path('app.py')).read()
    idx = src.find("def _load_job_from_disk(job_id):")
    assert idx > 0
    block = src[idx:idx + 2500]
    assert '_supabase_execute_with_timeout' in block


def test_static_load_session_uses_timeout_helper():
    src = open(_cft.backend_path('app.py')).read()
    idx = src.find("def _load_session_safe(token):")
    assert idx > 0
    block = src[idx:idx + 2500]
    assert '_supabase_execute_with_timeout' in block


def test_static_supabase_timeout_error_code_exists():
    src = open(_cft.backend_path('app.py')).read()
    assert "'SUPABASE_TIMEOUT':" in src
    assert "'fetch_error'" in src


# ─── 8. Backward-compat: _load_job_from_disk still works ──────────────────

def test_load_job_from_disk_returns_dict_when_supabase_returns_data(fresh_app, monkeypatch):
    fresh_app.SB_AVAILABLE = True

    class _R:
        data = [{'data': {'job_id': 'x', 'status': 'done'}}]
    class _Q:
        def select(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def limit(self, *a, **k): return self
        def execute(self): return _R()
    class _SB:
        def table(self, *a, **k): return _Q()

    monkeypatch.setattr(fresh_app, 'sb', _SB())
    result = fresh_app._load_job_from_disk('x')
    assert result == {'job_id': 'x', 'status': 'done'}


def test_load_job_from_disk_returns_none_when_supabase_times_out(fresh_app, monkeypatch):
    """Backward-compat: _load_job_from_disk gibt bei timeout None zurück
    (gleiche Signatur wie vorher)."""
    fresh_app.SB_AVAILABLE = True

    class _HangQuery:
        def select(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def limit(self, *a, **k): return self
        def execute(self): time.sleep(10)
    class _HangSB:
        def table(self, *a, **k): return _HangQuery()

    monkeypatch.setattr(fresh_app, 'sb', _HangSB())
    orig = fresh_app._supabase_execute_with_timeout
    monkeypatch.setattr(
        fresh_app, '_supabase_execute_with_timeout',
        lambda label, fn, timeout_s=5: orig(label, fn, timeout_s=1)
    )

    start = time.time()
    result = fresh_app._load_job_from_disk('hang-id')
    duration = time.time() - start
    assert result is None
    assert duration < 2.0


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
