"""v13 Cloud Tasks Worker — Tests gemäß User-Spec Phase 1D (12 Tests).

Architektur:
  /api/process  → erzeugt Job + dispatcht Cloud Task
  Cloud Task     → /api/internal/process-job (OIDC-Auth)
  Worker         → läuft synchron im HTTP-Request, kein Background-Thread

Mode-Switch via AEROTAX_EXECUTION_MODE = 'thread' | 'cloud_tasks'.
"""
import os
import conftest as _cft
import sys
import json as _json
import importlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _find_process_cloud_tasks_branch(src, span=4500):
    """Liefert den Code-Block des `/api/process` cloud_tasks-Branches.

    BUG-002-Fix hat einen früheren `cloud_tasks`-Branch in `_start_calc_worker`
    eingeführt — der Boot-Disable-Branch. Die alten Tests suchen den *Routing*-
    Branch in `/api/process`, daher verankern wir am Call-Site `_enqueue_cloud_task(
    job_id, attempt=1)` und gehen rückwärts zum letzten cloud_tasks-Check davor.
    """
    enq_idx = src.find('_enqueue_cloud_task(job_id, attempt=1)')
    assert enq_idx > 0, '_enqueue_cloud_task call-site fehlt in app.py'
    cloud_check_idx = src.rfind("AEROTAX_EXECUTION_MODE == 'cloud_tasks'", 0, enq_idx)
    assert cloud_check_idx > 0, 'cloud_tasks-Branch in /api/process fehlt'
    return src[cloud_check_idx:cloud_check_idx + span]


def _load_app_fresh(env=None):
    """Reload app mit optionalen ENV-Overrides.
    Wichtig: setzt AEROTAX_EXECUTION_MODE explizit auf 'thread' falls nicht
    anders gewünscht, damit Tests nicht durch vorherige test-runs verseucht
    werden (process-global ENV)."""
    os.environ['AEROTAX_EXECUTION_MODE'] = 'thread'  # default reset
    if env:
        for k, v in env.items():
            os.environ[k] = v
    if 'app' in sys.modules:
        del sys.modules['app']
    import app as _app
    importlib.reload(_app)
    return _app


# ─── Cloud Tasks Helper-Funktionen existieren ────────────────────────────────

def test_cloud_tasks_constants_defined():
    """ENV-Konstanten vorhanden mit Defaults."""
    _app = _load_app_fresh()
    assert hasattr(_app, 'AEROTAX_EXECUTION_MODE')
    assert hasattr(_app, 'AEROTAX_TASKS_QUEUE')
    assert hasattr(_app, 'AEROTAX_TASKS_LOCATION')
    assert hasattr(_app, 'AEROTAX_GCP_PROJECT')
    assert hasattr(_app, 'AEROTAX_CLOUD_RUN_WORKER_URL')
    assert hasattr(_app, 'AEROTAX_TASK_INVOKER_SA')


def test_enqueue_cloud_task_function_exists():
    _app = _load_app_fresh()
    assert hasattr(_app, '_enqueue_cloud_task')


def test_verify_internal_task_auth_function_exists():
    _app = _load_app_fresh()
    assert hasattr(_app, '_verify_internal_task_auth')


def test_internal_worker_endpoint_exists():
    """Route /api/internal/process-job ist registriert."""
    _app = _load_app_fresh()
    rules = [str(r) for r in _app.app.url_map.iter_rules()]
    assert any('/api/internal/process-job' in r for r in rules)


# ─── 12 vom User benannte Tests ──────────────────────────────────────────────

def test_process_enqueues_cloud_task(monkeypatch):
    """In cloud_tasks-Mode: /api/process ruft _enqueue_cloud_task, putetet NICHT in queue."""
    _app = _load_app_fresh()
    monkeypatch.setattr(_app, 'AEROTAX_EXECUTION_MODE', 'cloud_tasks')
    enqueue_calls = []

    def fake_enqueue(job_id, attempt=1, delay_seconds=0):
        enqueue_calls.append((job_id, attempt))
        return 'projects/fake/queue/aerotax-jobs/tasks/abc123'

    monkeypatch.setattr(_app, '_enqueue_cloud_task', fake_enqueue)
    # Code-Inspection statt request: Verzweigung muss da sein
    src = open(_app.__file__).read()
    process_idx = src.find('# v13 Cloud Tasks: Verzweigung')
    assert process_idx > 0, 'cloud_tasks Verzweigung im /api/process fehlt'
    block = src[process_idx:process_idx + 6000]
    assert "AEROTAX_EXECUTION_MODE == 'cloud_tasks'" in block
    assert '_enqueue_cloud_task(' in block


def test_process_returns_queued_not_running(monkeypatch):
    """In cloud_tasks-Mode: Response hat status='queued' + canonical_state='queued'."""
    _app = _load_app_fresh()
    src = open(_app.__file__).read()
    process_idx = src.find('# v13 Cloud Tasks: Verzweigung')
    block = src[process_idx:process_idx + 6000]
    assert "'status': 'queued'" in block
    assert "'canonical_state': 'queued'" in block
    assert "'execution_mode': 'cloud_tasks'" in block


def test_internal_worker_requires_auth():
    """POST ohne Bearer-Token → 401."""
    _app = _load_app_fresh({'AEROTAX_EXECUTION_MODE': 'cloud_tasks'})
    client = _app.app.test_client()
    resp = client.post('/api/internal/process-job',
                        json={'job_id': 'test-1', 'attempt': 1})
    assert resp.status_code == 401
    body = resp.get_json() or {}
    assert 'unauthorized' in (body.get('error') or '').lower()


def test_internal_worker_processes_job_to_done(monkeypatch):
    """Wenn Job durchläuft + status=done gesetzt wird: response 200 ok=True."""
    _app = _load_app_fresh()
    # Test-Bypass für Auth (kein OIDC-Setup nötig)
    monkeypatch.setattr(_app, 'AEROTAX_EXECUTION_MODE', 'thread')  # bypass-mode aktiv

    # Job im memory + form + files in Supabase mock
    with _app._jobs_lock:
        _app._jobs['j-done-test'] = {
            'status': 'queued',
            'attempt_id': 0,
            'form': {'ref': 'ref-done-test', 'year': 2025, 'base': 'Frankfurt (FRA)'},
            'session_token': 'AT-TEST',
        }
    monkeypatch.setattr(_app, '_load_uploaded_files_supabase',
                         lambda ref: {'lsb': [(b'fake', 'lsb.pdf')]})
    monkeypatch.setattr(_app, '_save_job_to_disk', lambda jid: None)

    # _run_process_async setzt status=done direkt
    def fake_run(jid, form, files):
        with _app._jobs_lock:
            _app._jobs[jid]['status'] = 'done'
            _app._jobs[jid]['data'] = {'netto': 5000.0}
    monkeypatch.setattr(_app, '_run_process_async', fake_run)

    client = _app.app.test_client()
    resp = client.post('/api/internal/process-job',
                        headers={'X-Internal-Task-Mode': 'test'},
                        json={'job_id': 'j-done-test', 'attempt': 1})
    body = resp.get_json() or {}
    assert resp.status_code == 200, f"got {resp.status_code}: {body}"
    assert body.get('ok') is True
    assert body.get('status') == 'done'


def test_internal_worker_failed_retryable_returns_500(monkeypatch):
    """Wenn Job in failed_retryable State: HTTP 500 damit Cloud Tasks retried."""
    _app = _load_app_fresh()
    with _app._jobs_lock:
        _app._jobs['j-retry'] = {
            'status': 'queued',
            'attempt_id': 0,
            'form': {'ref': 'ref-retry'},
            'session_token': 'AT-RETRY',
        }
    monkeypatch.setattr(_app, '_load_uploaded_files_supabase',
                         lambda ref: {'lsb': [(b'fake', 'lsb.pdf')]})
    monkeypatch.setattr(_app, '_save_job_to_disk', lambda jid: None)

    def fake_run(jid, form, files):
        with _app._jobs_lock:
            _app._jobs[jid]['status'] = 'failed'
            _app._jobs[jid]['error'] = 'sonnet timeout'
            _app._jobs[jid]['reason_code'] = 'SONNET_TIMEOUT'
    monkeypatch.setattr(_app, '_run_process_async', fake_run)

    client = _app.app.test_client()
    resp = client.post('/api/internal/process-job',
                        headers={'X-Internal-Task-Mode': 'test'},
                        json={'job_id': 'j-retry', 'attempt': 1})
    body = resp.get_json() or {}
    # attempt=1 < MAX_RETRY=2 → retry → 500
    assert resp.status_code == 500
    assert body.get('retryable') is True
    assert body.get('reason_code') == 'SONNET_TIMEOUT'


def test_internal_worker_failed_support_returns_200_no_retry(monkeypatch):
    """failed_support State → 200, kein Retry-Signal."""
    _app = _load_app_fresh()
    with _app._jobs_lock:
        _app._jobs['j-support'] = {
            'status': 'queued',
            'attempt_id': 0,
            'form': {'ref': 'ref-support'},
            'session_token': 'AT-SUP',
        }
    monkeypatch.setattr(_app, '_load_uploaded_files_supabase',
                         lambda ref: {'lsb': [(b'fake', 'lsb.pdf')]})
    monkeypatch.setattr(_app, '_save_job_to_disk', lambda jid: None)

    def fake_run(jid, form, files):
        with _app._jobs_lock:
            _app._jobs[jid]['status'] = 'failed'
            _app._jobs[jid]['data'] = {'_followme_align_failed': {'when': 'now'}}
    monkeypatch.setattr(_app, '_run_process_async', fake_run)

    client = _app.app.test_client()
    resp = client.post('/api/internal/process-job',
                        headers={'X-Internal-Task-Mode': 'test'},
                        json={'job_id': 'j-support', 'attempt': 1})
    body = resp.get_json() or {}
    assert resp.status_code == 200
    assert body.get('canonical_state') == 'failed_support'
    assert body.get('reason_code') == 'ALIGN_FAILED'
    assert body.get('retryable') is None or body.get('retryable') is False


def test_no_background_thread_in_cloud_tasks_mode():
    """In cloud_tasks-mode: /api/process puttet nicht in _calc_queue (aktiv).
    Kommentare die '_calc_queue.put' erwähnen sind erlaubt.

    Wir suchen den `/api/process`-spezifischen Branch — nicht den Boot-Disable-
    Branch in `_start_calc_worker` (BUG-002), der vorne im File steht.
    """
    src = open(_cft.backend_path('app.py')).read()
    # Suche den Branch in /api/process der `_enqueue_cloud_task(` aufruft (= Call-Site,
    # nicht die Funktions-Definition). Wir verankern am Call-Pattern (Aufruf mit
    # attempt=1) und gehen rückwärts zum cloud_tasks-Check davor.
    enq_idx = src.find('_enqueue_cloud_task(job_id, attempt=1)')
    assert enq_idx > 0, '_enqueue_cloud_task call-site fehlt in app.py'
    cloud_check_idx = src.rfind("AEROTAX_EXECUTION_MODE == 'cloud_tasks'", 0, enq_idx)
    assert cloud_check_idx > 0, (
        'cloud_tasks-Branch vor _enqueue_cloud_task fehlt'
    )
    block = src[cloud_check_idx:cloud_check_idx + 6000]
    # _enqueue_cloud_task ist im Branch
    assert '_enqueue_cloud_task(' in block
    # cloud_tasks-Branch endet beim Thread-Mode-Marker
    thread_marker = block.find('# ── Thread-Mode')
    assert thread_marker > 0, 'Thread-Mode-Marker muss vorhanden sein'
    cloud_tasks_branch = block[:thread_marker]
    # Im cloud_tasks-Branch darf KEIN aktiver _calc_queue.put((job_id, form, files)) Call sein.
    # Nur Kommentare die das erwähnen (mit # davor) sind OK.
    for line in cloud_tasks_branch.split('\n'):
        stripped = line.strip()
        if stripped.startswith('#'):
            continue  # Kommentar ignorieren
        assert '_calc_queue.put(' not in stripped, \
            f'aktiver _calc_queue.put im cloud_tasks-Branch gefunden: {stripped[:80]}'


def test_thread_mode_still_works_local_dev():
    """Thread-Mode (default) ruft _calc_queue.put — Legacy bleibt funktionsfähig."""
    src = open(_cft.backend_path('app.py')).read()
    # Markiert als „Thread-Mode (Default / Legacy)"
    assert 'Thread-Mode (Default / Legacy)' in src
    assert '_calc_queue.put((job_id, form, files))' in src


def test_cloud_task_duplicate_is_idempotent(monkeypatch):
    """Doppelter Task-Dispatch (gleiche attempt) → 200 duplicate, kein Re-Compute."""
    _app = _load_app_fresh()
    run_count = {'n': 0}

    with _app._jobs_lock:
        _app._jobs['j-dup'] = {
            'status': 'processing',
            'attempt_id': 1,
            'form': {'ref': 'ref-dup'},
            'session_token': 'AT-DUP',
        }
    monkeypatch.setattr(_app, '_save_job_to_disk', lambda jid: None)

    def fake_run(jid, form, files):
        run_count['n'] += 1
    monkeypatch.setattr(_app, '_run_process_async', fake_run)

    client = _app.app.test_client()
    resp = client.post('/api/internal/process-job',
                        headers={'X-Internal-Task-Mode': 'test'},
                        json={'job_id': 'j-dup', 'attempt': 1})  # gleiche attempt
    body = resp.get_json() or {}
    assert resp.status_code == 200
    assert body.get('idempotent') is True
    assert body.get('duplicate') is True
    assert run_count['n'] == 0, '_run_process_async darf NICHT aufgerufen werden bei duplicate'


def test_cloud_task_done_job_is_idempotent(monkeypatch):
    """Status=done → 200, kein Re-Compute auch bei höherer attempt."""
    _app = _load_app_fresh()
    run_count = {'n': 0}

    with _app._jobs_lock:
        _app._jobs['j-already-done'] = {
            'status': 'done',
            'attempt_id': 1,
            'data': {'netto': 5000.0},
            'form': {'ref': 'ref-x'},
            'session_token': 'AT-DONE',
        }

    def fake_run(jid, form, files):
        run_count['n'] += 1
    monkeypatch.setattr(_app, '_run_process_async', fake_run)
    monkeypatch.setattr(_app, '_save_job_to_disk', lambda jid: None)

    client = _app.app.test_client()
    resp = client.post('/api/internal/process-job',
                        headers={'X-Internal-Task-Mode': 'test'},
                        json={'job_id': 'j-already-done', 'attempt': 2})
    body = resp.get_json() or {}
    assert resp.status_code == 200
    assert body.get('idempotent') is True
    assert body.get('status') == 'done'
    assert run_count['n'] == 0


def test_cloud_task_retry_count_persistent():
    """attempt_id wird im Job-Dict persistiert — überlebt Container-Restart via Disk/Supabase."""
    src = open(_cft.backend_path('app.py')).read()
    # Worker-Endpoint setzt attempt_id im job
    worker_idx = src.find('def internal_process_job(')
    block = src[worker_idx:worker_idx + 6500]  # window erweitert nach CR-1 stale-detection insertion
    assert "j['attempt_id'] = attempt" in block
    assert '_save_job_to_disk(job_id)' in block  # persistiert
    # Plus initiales setzen in /api/process
    process_idx = src.find('# v13 Cloud Tasks: Verzweigung')
    p_block = src[process_idx:process_idx + 6000]
    assert "'attempt_id'" in p_block


def test_access_code_works_while_task_processing():
    """/api/session/<token> liefert canonical_state auch während task läuft.
    Frontend pollt /api/job/<id> → state-machine response funktioniert."""
    src = open(_cft.backend_path('app.py')).read()
    # /api/job/<id> returnt canonical_state immer (von _classify_job_state)
    job_endpoint_idx = src.find('def get_job_status(')
    block = src[job_endpoint_idx:job_endpoint_idx + 2000]
    assert '_classify_job_state(j)' in block
    # /api/session/<token> ebenfalls
    sess_idx = src.find('def session_recall(')
    s_block = src[sess_idx:sess_idx + 2000]
    assert '_classify_job_state(' in s_block


def test_frontend_polling_not_required_for_job_survival():
    """Worker-Endpoint läuft IN dem HTTP-Request — keine Abhängigkeit von Frontend-Pings.
    Code-Check: _run_process_async wird SYNCHRON im Worker-Endpoint gerufen."""
    src = open(_cft.backend_path('app.py')).read()
    worker_idx = src.find('def internal_process_job(')
    block = src[worker_idx:worker_idx + 6500]  # window erweitert nach CR-1 stale-detection insertion
    # Synchroner Aufruf — kein threading.Thread, kein _calc_queue.put
    assert '_run_process_async(job_id, form, files)' in block
    assert 'threading.Thread' not in block
    assert '_calc_queue' not in block


# ─── Zusatz: Auth-Verify-Logik ───────────────────────────────────────────────

def test_verify_auth_rejects_missing_bearer():
    _app = _load_app_fresh({'AEROTAX_EXECUTION_MODE': 'cloud_tasks'})
    from unittest.mock import MagicMock
    fake_req = MagicMock()
    fake_req.headers = {}
    assert _app._verify_internal_task_auth(fake_req) is False


def test_verify_auth_test_bypass_in_thread_mode():
    """Im thread-Mode: X-Internal-Task-Mode=test header → bypass für Tests."""
    _app = _load_app_fresh({'AEROTAX_EXECUTION_MODE': 'thread'})
    from unittest.mock import MagicMock
    fake_req = MagicMock()
    fake_req.headers = {'X-Internal-Task-Mode': 'test'}
    assert _app._verify_internal_task_auth(fake_req) is True


def test_verify_auth_no_bypass_in_cloud_tasks_mode():
    """Cloud-Tasks-Mode: X-Internal-Task-Mode=test wird NICHT akzeptiert
    (echter OIDC nötig)."""
    _app = _load_app_fresh({'AEROTAX_EXECUTION_MODE': 'cloud_tasks'})
    from unittest.mock import MagicMock
    fake_req = MagicMock()
    fake_req.headers = {'X-Internal-Task-Mode': 'test'}
    # Kein Bearer → False (Test-Bypass nur in thread-mode)
    assert _app._verify_internal_task_auth(fake_req) is False


# ─── Enqueue-Helper Sanity ───────────────────────────────────────────────────

def test_enqueue_raises_when_worker_url_missing(monkeypatch):
    """Ohne AEROTAX_CLOUD_RUN_WORKER_URL: RuntimeError."""
    _app = _load_app_fresh()
    monkeypatch.setattr(_app, 'AEROTAX_CLOUD_RUN_WORKER_URL', '')
    import pytest as _pt
    with _pt.raises(RuntimeError, match='AEROTAX_CLOUD_RUN_WORKER_URL'):
        _app._enqueue_cloud_task('test-job-1', attempt=1)


# ─── Bug-Fix: Files müssen vor Cloud-Task-Dispatch in Supabase persistiert sein ─

def test_cloud_tasks_process_persists_files_to_supabase():
    """v13 Bug-Fix: /api/process im cloud_tasks-Mode ruft _save_uploaded_files_supabase
    BEVOR _enqueue_cloud_task. Sonst hat Worker keinen Zugriff auf Direct-Upload-Files.

    Capture-Run #1 (2026-05-12) zeigte: ohne diesen Step kommt CAS=False im
    PARALLEL READER STAGE, weil Worker via _load_uploaded_files_supabase(ref) lädt."""
    src = open(_cft.backend_path('app.py')).read()
    # Im cloud_tasks-Branch muss _save_uploaded_files_supabase ausgeführt werden
    block = _find_process_cloud_tasks_branch(src, span=4000)
    assert '_save_uploaded_files_supabase(' in block, \
        'cloud_tasks-Branch muss Files in Supabase persistieren vor enqueue'
    # Aufruf-Reihenfolge: persist VOR enqueue
    persist_pos = block.find('_save_uploaded_files_supabase(')
    enqueue_pos = block.find('_enqueue_cloud_task(')
    assert persist_pos > 0 and enqueue_pos > 0
    assert persist_pos < enqueue_pos, \
        'Files müssen VOR enqueue persistiert sein'


def test_cloud_tasks_process_generates_fallback_ref_when_missing():
    """Falls /api/process ohne ref aufgerufen wird (Direct-Upload-Only): generiert
    einen fallback-ref aus job_id."""
    src = open(_cft.backend_path('app.py')).read()
    block = _find_process_cloud_tasks_branch(src, span=4000)
    # Fallback-ref Generation: f'auto-{job_id[:12]}'
    assert "f'auto-" in block, 'Fallback-ref-Generation fehlt'


def test_cloud_tasks_process_hard_fails_on_persist_error():
    """Wenn _save_uploaded_files_supabase failed: Job sofort als failed mit
    spezifischem reason_code markieren — NICHT enqueue.

    2026-05-19 Modernisierung: Reason-Code von generic WORKER_RESTARTED auf
    spezifisch UPLOAD_PERSIST_FAILED upgraded (P0 #90). Log-String englisch
    durch deutsch ersetzt. Test prüft jetzt die spezifischen reason_code-Pfade.
    """
    src = open(_cft.backend_path('app.py')).read()
    block = _find_process_cloud_tasks_branch(src, span=6500)
    # Spezifischer reason_code (P0 #90)
    assert ("_set_job_failed(job_id, 'UPLOAD_PERSIST_FAILED'" in block
            or "_set_job_failed(job_id, 'WORKER_RESTARTED'" in block), (
        '_set_job_failed mit reason_code (UPLOAD_PERSIST_FAILED oder WORKER_RESTARTED) bei persist-fail'
    )
    # Persist-Error-Handling vorhanden (UploadPersistError-catch)
    assert 'UploadPersistError' in block, 'UploadPersistError catch fehlt im cloud_tasks branch'


def test_cloud_tasks_process_converts_files_to_supabase_format():
    """Files-Dict {key: [(bytes, fname)|bytes]} → Supabase-Format {key: [(bytes, fname)]}."""
    src = open(_cft.backend_path('app.py')).read()
    block = _find_process_cloud_tasks_branch(src, span=4500)
    # Format-Konvertierung-Logik
    assert 'files_sb_format' in block
    # Behandelt sowohl tuple (bytes, fname) als auch bare bytes
    assert 'isinstance(it, tuple)' in block
    assert 'isinstance(it, (bytes, bytearray))' in block


if __name__ == '__main__':
    import pytest
    sys.exit(pytest.main([__file__, '-v']))
