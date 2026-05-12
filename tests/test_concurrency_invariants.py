"""BUG-005 — Gunicorn/Cloud-Run-Concurrency-Invarianten + Instrumentation.

Verhindert dass das System nochmal mit zu wenig Worker-Threads gegen zu hoher
Cloud-Run-Concurrency deployed wird.

Plus: Verifiziert dass Request-Instrumentation existiert (before_request,
after_request, teardown_request mit pid + thread_id + duration_ms).
"""
import os
import re


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKERFILE = os.path.join(ROOT, 'Dockerfile')
PROCFILE = os.path.join(ROOT, 'Procfile')
APP = os.path.join(ROOT, 'app.py')


def _read(path):
    return open(path).read()


# ─── Dockerfile: gthread + threads=8 ─────────────────────────────────────────

def test_dockerfile_uses_gthread():
    """Dockerfile muss --worker-class gthread setzen.
    Default sync-worker blockiert bei hängendem Supabase-Call alle weiteren
    Threads, gthread macht echte Per-Request-Threads."""
    src = _read(DOCKERFILE)
    assert '--worker-class gthread' in src or '--worker-class=gthread' in src, (
        'Dockerfile gunicorn command muss --worker-class gthread setzen'
    )


def test_dockerfile_threads_8():
    """Dockerfile muss --threads 8 setzen (matched Cloud Run concurrency)."""
    src = _read(DOCKERFILE)
    # --threads 8 oder --threads=8
    assert re.search(r'--threads[ =]+8\b', src), (
        'Dockerfile gunicorn command muss --threads 8 setzen'
    )


def test_dockerfile_workers_still_1():
    """workers=1 bleibt — gthread mit threads>1 ersetzt das."""
    src = _read(DOCKERFILE)
    assert re.search(r'--workers[ =]+1\b', src)


def test_dockerfile_timeout_1800():
    """Worker-Endpoint braucht 30 Min für lange Auswertungen."""
    src = _read(DOCKERFILE)
    assert re.search(r'--timeout[ =]+1800\b', src)


def test_dockerfile_max_requests_200():
    """Memory-Leak-Guard: graceful restart alle 200 requests."""
    src = _read(DOCKERFILE)
    assert re.search(r'--max-requests[ =]+200\b', src)


def test_dockerfile_max_requests_jitter_20():
    """jitter=20 (User-Spec)."""
    src = _read(DOCKERFILE)
    assert re.search(r'--max-requests-jitter[ =]+20\b', src)


# ─── Procfile-Konsistenz ─────────────────────────────────────────────────────

def test_procfile_matches_dockerfile_gthread():
    """Procfile (Render fallback) muss gthread+threads=8 matchen, sonst
    bei Rollback auf Render würde altes blocking-Pattern zurückkommen."""
    src = _read(PROCFILE)
    assert '--worker-class gthread' in src or '--worker-class=gthread' in src
    assert re.search(r'--threads[ =]+8\b', src)
    assert re.search(r'--timeout[ =]+1800\b', src)


# ─── Cloud-Run-Concurrency-Invariante ────────────────────────────────────────

def test_cloud_run_concurrency_not_above_gunicorn_threads():
    """Falls Cloud-Run-Concurrency > gunicorn-threads → Queue-Stau.
    Dieser Test prüft die DOKUMENTATION (Dockerfile-Kommentar) erwähnt die
    Invariante — und das CLOUD_RUN_MIGRATION-Doc, falls vorhanden, hat
    auch die richtige Zahl.

    Live-Cloud-Run-Config wird nicht hier geprüft (kein gcloud im Test) —
    das ist Aufgabe vom Deploy-Skript / Manual-Check."""
    docker = _read(DOCKERFILE)
    # Kommentar muss containerConcurrency=8 erwähnen
    assert 'concurrency=8' in docker.lower() or 'containerConcurrency=8'.lower() in docker.lower(), (
        'Dockerfile-Kommentar muss containerConcurrency=8 erwähnen — sonst '
        'kann jemand Cloud Run wieder auf 10 setzen und der Stau ist zurück'
    )


# ─── ENV-Vars dürfen nicht durch --set-env-vars überschrieben werden ─────────

def test_cloud_tasks_env_not_removed_by_deploy_docs():
    """CLOUD_RUN_MIGRATION.md oder gleichwertig muss warnen:
    `--set-env-vars` ersetzt ALLE env vars, `--update-env-vars` merged.

    Dieser Test schützt vor dem heutigen self-inflicted bug (revision 00022)
    wo AEROTAX_EXECUTION_MODE gelöscht wurde."""
    docs_dir = os.path.join(ROOT, 'docs')
    candidates = []
    if os.path.isdir(docs_dir):
        for f in os.listdir(docs_dir):
            if f.endswith('.md'):
                candidates.append(os.path.join(docs_dir, f))
    # Plus CLAUDE.md im Root
    if os.path.exists(os.path.join(ROOT, 'CLAUDE.md')):
        candidates.append(os.path.join(ROOT, 'CLAUDE.md'))
    found = False
    for path in candidates:
        try:
            txt = open(path).read().lower()
        except Exception:
            continue
        if 'update-env-vars' in txt and 'set-env-vars' in txt:
            found = True
            break
    assert found, (
        'Mind. eine Doc-Datei muss --update-env-vars vs --set-env-vars '
        'erklären (Warnung gegen self-inflicted env-overwrite)'
    )


# ─── Request-Instrumentation (BUG-005-Diagnostik) ────────────────────────────

def test_request_instrumentation_exists():
    """app.py muss before_request + after_request + teardown_request haben,
    die jeweils Request-Path, PID, Thread-ID, Duration loggen."""
    src = _read(APP)
    assert '@app.before_request' in src
    assert '@app.after_request' in src
    assert '@app.teardown_request' in src


def test_instrumentation_logs_path_and_pid_and_thread():
    """Logs müssen mindestens path, pid, tid (thread id) enthalten."""
    src = _read(APP)
    # Im before_request-Handler — alle 3 Felder
    idx = src.find('_bug005_before_request')
    assert idx > 0
    block = src[idx:idx + 1500]
    assert 'path=' in block
    assert 'pid=' in block
    assert 'tid=' in block


def test_instrumentation_logs_duration_in_after():
    """after_request muss duration_ms loggen."""
    src = _read(APP)
    idx = src.find('_bug005_after_request')
    assert idx > 0
    block = src[idx:idx + 1500]
    assert 'duration_ms=' in block


def test_instrumentation_logs_status_in_after():
    """after_request muss status (HTTP status code) loggen."""
    src = _read(APP)
    idx = src.find('_bug005_after_request')
    block = src[idx:idx + 1500]
    assert 'status=' in block


def test_instrumentation_teardown_logs_exception_type():
    """teardown_request loggt bei exception den exception-type."""
    src = _read(APP)
    idx = src.find('_bug005_teardown')
    assert idx > 0
    block = src[idx:idx + 1500]
    assert 'exc=' in block
    assert 'type(exc).__name__' in block


def test_instrumentation_uses_request_id():
    """Jeder Request bekommt eine ID (uuid4-prefix) zur Korrelation
    before→after→teardown."""
    src = _read(APP)
    idx = src.find('_bug005_before_request')
    block = src[idx:idx + 1500]
    assert '_req_id' in block
    assert 'uuid' in block.lower()


def test_instrumentation_does_not_raise():
    """Alle 3 Hooks haben try/except — Instrumentation darf NIE die App
    kaputt machen."""
    src = _read(APP)
    for fn_name in ('_bug005_before_request', '_bug005_after_request', '_bug005_teardown'):
        idx = src.find(fn_name)
        assert idx > 0, f'{fn_name} fehlt'
        block = src[idx:idx + 1500]
        assert 'try:' in block, f'{fn_name} braucht try-block'
        assert 'except' in block, f'{fn_name} braucht except-block'


def test_instrumentation_skips_sse_endpoint():
    """SSE-Endpoint /api/progress soll NICHT geloggt werden (zu noisy,
    plus log würde während long-open-Connection nicht feuern)."""
    src = _read(APP)
    assert '/api/progress' in src
    # Im Skip-Set
    idx = src.find('_REQ_LOG_SKIP')
    assert idx > 0
    block = src[idx:idx + 200]
    assert '/api/progress' in block


def test_instrumentation_prefix_is_grepable():
    """Logs nutzen einen festen prefix '[req]' — so kann man grep'en."""
    src = _read(APP)
    assert "'[req]'" in src or '"[req]"' in src


# ─── Regression: BUG-002 + BUG-009 noch grün ─────────────────────────────────

def test_cloud_tasks_mode_disables_bg_threads_still_green():
    """Phase 1 BUG-002-Fix darf nicht durch unsere Dockerfile/Instrumentation-
    Änderungen kaputt gehen."""
    src = _read(APP)
    assert "AEROTAX_EXECUTION_MODE == 'cloud_tasks'" in src
    # _start_calc_worker hat early-return im cloud_tasks-mode
    idx = src.find('def _start_calc_worker(')
    block = src[idx:idx + 1500]
    assert "AEROTAX_EXECUTION_MODE == 'cloud_tasks'" in block
    assert 'legacy background worker disabled' in block


if __name__ == '__main__':
    import sys, pytest
    sys.exit(pytest.main([__file__, '-v']))
