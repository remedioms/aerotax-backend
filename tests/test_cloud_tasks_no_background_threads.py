"""BUG-002 — In cloud_tasks-mode startet KEIN Legacy-Background-Thread.

Hintergrund: Cloud Run Container im Restart-Loop, weil _calc_worker und
_restart_recovery_async parallel zum Gunicorn-Mainloop liefen, Health-Probe
timeoutete, Cloud Run killte Container.

Diese Tests verifizieren:
- Im cloud_tasks-Mode startet NUR der HTTP-Server (keine Background-Threads).
- Im thread-Mode (legacy / lokal / Render) bleibt das alte Verhalten.
- /api/internal/process-job läuft trotzdem sync und macht die Arbeit.
"""
import os
import subprocess
import sys
import threading


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _run_app_import(mode):
    """Importiert app.py in einem frischen Python-Subprozess mit gesetztem
    AEROTAX_EXECUTION_MODE und gibt (stdout, stderr, threadnames) zurück.

    Wir nutzen einen Subprozess, damit der Import-Side-Effect (Thread-Spawn)
    sauber pro Test isoliert ist — Threads aus früheren Test-Imports würden
    sonst durchscheinen.
    """
    code = (
        "import os, sys, threading, time\n"
        "sys.path.insert(0, %r)\n"
        # Tests setzen Disable normalerweise — hier explizit AKTIVIEREN,
        # damit das produktive Spawning-Verhalten gemessen wird.
        "os.environ.pop('AEROTAX_DISABLE_BG_THREADS', None)\n"
        "os.environ['AEROTAX_EXECUTION_MODE'] = %r\n"
        "os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')\n"
        "import app\n"
        # 200ms warten — falls Threads asynchron erst gleich starten.
        "time.sleep(0.2)\n"
        "names = sorted(t.name for t in threading.enumerate())\n"
        "print('THREAD_NAMES=' + ','.join(names))\n"
    ) % (ROOT, mode)
    res = subprocess.run(
        [sys.executable, '-c', code],
        capture_output=True, text=True, cwd=ROOT, timeout=30,
    )
    out = res.stdout
    err = res.stderr
    thread_names = []
    for line in out.splitlines():
        if line.startswith('THREAD_NAMES='):
            thread_names = [n for n in line.split('=', 1)[1].split(',') if n]
    return out, err, thread_names


# ─── 1. cloud_tasks-Mode startet KEINEN calc-worker ──────────────────────────

def test_cloud_tasks_mode_does_not_start_calc_worker():
    """Im cloud_tasks-Mode darf kein Thread mit Namen 'calc-worker' laufen."""
    out, err, names = _run_app_import('cloud_tasks')
    assert 'calc-worker' not in names, (
        f"calc-worker thread still spawned in cloud_tasks-mode. "
        f"threads={names}\nstderr={err[-500:]}"
    )


# ─── 2. cloud_tasks-Mode startet KEINEN restart-recovery thread ──────────────

def test_cloud_tasks_mode_does_not_start_restart_recovery_thread():
    """Im cloud_tasks-Mode darf kein Thread mit Namen 'restart-recovery' laufen."""
    out, err, names = _run_app_import('cloud_tasks')
    assert 'restart-recovery' not in names, (
        f"restart-recovery thread still spawned in cloud_tasks-mode. "
        f"threads={names}"
    )


# ─── 3. thread-Mode (legacy) startet calc-worker WEITERHIN ───────────────────

def test_thread_mode_starts_calc_worker():
    """In thread-mode (legacy / Render) MUSS calc-worker laufen — sonst werden
    Jobs aus der lokalen Queue nicht abgearbeitet."""
    out, err, names = _run_app_import('thread')
    assert 'calc-worker' in names, (
        f"calc-worker thread NOT spawned in thread-mode (regression!). "
        f"threads={names}\nstderr={err[-500:]}"
    )


# ─── 4. /api/process im cloud_tasks-Mode enqueued OHNE local worker ──────────

def test_process_cloud_tasks_enqueues_without_local_worker():
    """Bei AEROTAX_EXECUTION_MODE=cloud_tasks ruft /api/process Cloud Tasks an,
    NICHT den lokalen _calc_queue. Wir prüfen das, indem wir den enqueue-Pfad
    static im Code verifizieren — der lokale Queue-put darf nur im thread-mode
    erreicht werden."""
    src = open(os.path.join(ROOT, 'app.py')).read()
    # Es muss eine Verzweigung geben, die cloud_tasks separat behandelt.
    assert "AEROTAX_EXECUTION_MODE == 'cloud_tasks'" in src
    # Plus: der Worker-Spawn ist in cloud_tasks-mode early-return.
    # Suche die Funktion _start_calc_worker und prüfe dass sie früh returnt.
    idx = src.find('def _start_calc_worker(')
    assert idx > 0
    func_body = src[idx:idx + 1500]
    # Erst kommt cloud_tasks-check, dann ein return
    ct_idx = func_body.find("AEROTAX_EXECUTION_MODE == 'cloud_tasks'")
    assert ct_idx > 0, '_start_calc_worker hat keinen cloud_tasks-Branch'
    after_check = func_body[ct_idx:ct_idx + 500]
    assert 'return' in after_check, 'cloud_tasks-Branch returnt nicht früh'
    # Plus: Boot-Log muss da sein
    assert 'legacy background worker disabled' in after_check


# ─── 5. /api/internal/process-job läuft synchron — KEIN Thread nötig ─────────

def test_internal_process_job_still_runs_sync():
    """Cloud Tasks ruft /api/internal/process-job direkt. Der Endpoint MUSS
    synchron arbeiten (in der Request-Handler-Funktion selber), nicht in einer
    Background-Queue."""
    src = open(os.path.join(ROOT, 'app.py')).read()
    # Endpoint existiert
    assert "/api/internal/process-job" in src
    # Er ruft die echte Pipeline-Funktion synchron auf (process_pipeline / _run_full_pipeline)
    # Statisch: er darf nicht _calc_queue.put() machen.
    # Wir extrahieren die Funktion und prüfen
    ep_idx = src.find("/api/internal/process-job")
    # Suche darum die @app.route-Definition
    route_idx = src.rfind('@app.route', 0, ep_idx)
    assert route_idx > 0
    # Bis zur nächsten @app.route oder Funktionsende
    next_route = src.find('@app.route', ep_idx + 10)
    if next_route < 0:
        next_route = len(src)
    handler = src[route_idx:next_route]
    # Der Handler darf _calc_queue nicht benutzen
    assert '_calc_queue.put' not in handler, (
        '/api/internal/process-job legt Job in _calc_queue ab — '
        'das wäre wieder Background-Worker-Abhängigkeit'
    )


# ─── 6. Kein Background-Calc-Loop in cloud_tasks-mode (cleanup-loop auch) ────

def test_no_background_calc_loop_in_cloud_tasks():
    """Auch der cleanup-loop (alle 2 Min Stale-Detection + alle 30 Min Supabase-
    Sweep) darf in cloud_tasks-mode nicht starten."""
    out, err, names = _run_app_import('cloud_tasks')
    forbidden = {'calc-worker', 'restart-recovery', 'cleanup-loop'}
    leaked = forbidden & set(names)
    assert not leaked, (
        f"Diese Background-Threads dürfen in cloud_tasks-mode nicht laufen: "
        f"{leaked}\nalle threads={names}"
    )


# ─── 7. Boot-Logs: explizite Disable-Meldungen ───────────────────────────────

def test_cloud_tasks_boot_logs_show_disabled_messages():
    """User-Spec: bei cloud_tasks-mode müssen explizite Boot-Logs erscheinen,
    damit der Operator sieht dass die Disable-Logik gegriffen hat."""
    out, err, names = _run_app_import('cloud_tasks')
    combined = out + err
    assert 'cloud_tasks mode: legacy background worker disabled' in combined, (
        f"Erwartete Boot-Log fehlt.\nstdout={out[-1000:]}\nstderr={err[-500:]}"
    )
    assert 'cloud_tasks mode: restart-recovery background thread disabled' in combined
    assert 'cloud_tasks mode: cleanup-loop background thread disabled' in combined


def test_cloud_tasks_boot_logs_do_not_show_thread_started():
    """Der alte „Worker-Thread gestartet" Log darf in cloud_tasks-mode nicht
    mehr erscheinen — der User soll sofort sehen dass das alte Verhalten weg
    ist."""
    out, err, names = _run_app_import('cloud_tasks')
    combined = out + err
    # Der alte Log wäre `[queue] Worker-Thread + Restart-Recovery gestartet (async)`
    assert 'Worker-Thread' not in combined, (
        f"Alter Worker-Thread-Log noch da:\nstdout={out[-1500:]}"
    )


if __name__ == '__main__':
    import pytest
    sys.exit(pytest.main([__file__, '-v']))
