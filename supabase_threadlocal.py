"""Thread-isolated synchronous Supabase clients with bounded client reuse.

``supabase-py``'s synchronous PostgREST client owns one HTTP/2 ``httpx.Client``.
Sharing that object between gunicorn ``gthread`` workers lets independent
requests mutate the same h2 connection state concurrently.  The resulting
failures are transport-level (``StreamIDTooLowError``,
``LocalProtocolError(ConnectionInputs.*)``), so retrying on the same shared
client cannot make the design safe.

This small proxy preserves the existing ``sb.table(...)`` API while giving every
thread its own real client.  A generation counter makes explicit cleanup
deterministic and avoids reusing inherited clients after a fork.

Why the idle pool exists (2026-07-27)
-------------------------------------
"One client per thread" was cheap while every thread was long-lived.  It stopped
being cheap once the hot request paths began fanning out into *short-lived*
threads (per-request ``ThreadPoolExecutor``s in the flight-detail aggregate,
``lh-warm``, ``detail-swr``, the warm-run).  Two failure modes came out of that:

1.  **Registry leak.**  ``_clients`` kept a strong reference keyed by
    ``id(client)``.  A thread that created a client and then died left its entry
    behind forever — nothing ever removed it.  The client, its PostgREST
    session, its httpx connection pool and its SSL context stayed reachable for
    the life of the worker process, and every additional short-lived thread made
    it worse.  Only the call sites that explicitly ran
    ``_close_current_thread_supabase_client()`` escaped it.

2.  **Construction cost.**  Measured inside the production container
    (2026-07-27): ``create_client()`` costs **52.6 ms of CPU**, of which
    **48 ms is ``ssl.create_default_context()``** parsing the CA bundle; the
    first query on a fresh client costs another ~20 ms of CPU for the TCP+TLS
    handshake.  A query on an already-warm client costs **4 ms**.  So every
    short-lived thread that touched Supabase burned ~18x the CPU of the query it
    actually wanted to run.  The fix for (1) that was in place — closing the
    client at the end of each task — removed the leak but paid that 70 ms on
    every single task.

Both are the same design problem, so they get one fix: when a client's owner is
finished with it, the client is **parked in a bounded idle pool** instead of
being destroyed, and the next thread that needs one checks it out.

The h2 isolation invariant is preserved exactly
-----------------------------------------------
A client is, at every instant, in exactly one of two states:

*   bound to exactly one live thread (present in ``_clients``, reachable through
    that thread's ``_local.entry``), or
*   parked in ``_idle``, owned by no thread.

Checkout pops under the lock, so two threads can never hold the same client.
Parking happens only when the previous owner is provably finished:

*   ``close_current()`` — the calling thread declares itself done.
*   the thread-death finalizer — the owning ``Thread`` object has been garbage
    collected, which cannot happen before the thread has exited.

The invariant that keeps a client from being parked mid-h2-stream is narrow and
worth stating exactly, because it is easy to break by accident:

    **every release site is a ``finally:`` that runs after the synchronous
    supabase call has returned or raised — never at the moment a caller gives
    up on it.**

Abandoning a call does *not* release its client.  ``_supabase_execute_with_timeout``
(app.py) abandons the future but the work keeps running on a long-lived
``sb-timeout`` pool thread that never releases; the per-request detail executor
(``aerox_data_blueprint._res`` / ``ex.shutdown(wait=False)``) abandons results
and lets hung workers run on, and those workers only reach their ``finally``
once the call itself has come back.  **Never add a ``close_current()`` to a
timeout, cancel or abandon path** — that would hand a mid-stream client to
another thread, and ``_SB_TRANSIENT_ERRORS`` in app.py does not list the h2
protocol errors, so the corruption would not be retried.

A client that *failed* is never parked
--------------------------------------
A bare ``finally:`` also runs when the task raised, so "the call came back"
includes "the call blew up".  Handing such a client on would be worse than the
old destroy-on-release behaviour: the next thread to check one out may be a
long-lived daemon (cleanup-loop, push-outbox-daemon, calc-worker,
lh-budget-flush), and those never release, so a damaged client would be married
to that daemon for the life of the process — a silent, permanent failure.

So each client gets an outermost ``_HealthProbeTransport``.  It sits *outside*
the app's retry transport and therefore only sees failures that already
survived the retries.  Any such failure marks the client unreusable and it is
destroyed on release instead of parked.  If the probe cannot be installed the
client is marked unreusable too — reuse is only ever enabled where health can
actually be observed (fail closed).

``AEROTAX_SB_CLIENT_REUSE=0`` restores the previous destroy-on-release behaviour
without a code change, as an operational kill switch.
"""

from __future__ import annotations

import os
import threading
import time
import weakref
from typing import Any, Callable, Dict, List, Optional

# Parking more than a handful of idle clients would just trade CPU for RSS: each
# one holds an SSL context and a connection pool.  The pool only has to cover the
# concurrency of the fan-out bursts (gthread threads + a per-request executor),
# not the total number of threads ever created.  NB this bound is per proxy and
# the process has two of them (app.sb and layover_group._SB), so the worst case
# per gunicorn worker is twice this number.
_DEFAULT_MAX_IDLE = 8
_STATS_LOG_INTERVAL_S = 300.0

try:  # pragma: no cover - httpx is a hard supabase-py dependency in production
    import httpx as _httpx
    _TransportBase = _httpx.BaseTransport
except Exception:  # pragma: no cover - keeps the module importable bare
    _TransportBase = object


class _HealthProbeTransport(_TransportBase):
    """Outermost PostgREST transport: remembers that a client went bad.

    Installed *after* the proxy's ``on_create`` callback, so in the app it wraps
    ``_SBRetryTransport`` and only observes failures that already exhausted the
    retries.  A client that raised may have a mid-stream or otherwise unusable
    HTTP/2 connection, so it must be destroyed on release rather than parked for
    the next thread — which could be a daemon that keeps it forever.
    """

    def __init__(self, inner: Any, on_failure: Callable[[], None]) -> None:
        self._inner = inner
        self._on_failure = on_failure

    def handle_request(self, request):
        try:
            return self._inner.handle_request(request)
        except BaseException:
            try:
                self._on_failure()
            except Exception:
                pass
            raise

    def close(self):
        close = getattr(self._inner, 'close', None)
        if callable(close):
            close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in ('0', 'false', 'no', 'off', '')


def _env_int(name: str, default: int) -> int:
    try:
        value = int(str(os.environ.get(name, '')).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def close_supabase_client(client: Any) -> None:
    """Best-effort close of lazily-created supabase-py HTTP clients.

    Supabase's top-level sync ``Client`` currently has no public ``close``.
    Avoid touching lazy properties (which would create new pools during
    shutdown) and close only components that already exist.
    """

    candidates = [
        getattr(client, "auth", None),
        getattr(client, "_postgrest", None),
        getattr(client, "_storage", None),
        getattr(client, "_functions", None),
    ]
    seen = set()
    for component in candidates:
        if component is None:
            continue
        # Prefer a component's public close().  Otherwise close the known
        # internal httpx holder used by PostgREST/Storage/Functions.  Selecting
        # only one path avoids double-closing auth's private HTTP client.
        component_close = getattr(component, "close", None)
        candidates_to_close = (
            (component,)
            if callable(component_close)
            else (
                getattr(component, "session", None),
                getattr(component, "_client", None),
                getattr(component, "_http_client", None),
            )
        )
        for candidate in candidates_to_close:
            if candidate is None or id(candidate) in seen:
                continue
            seen.add(id(candidate))
            close = getattr(candidate, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    # Cleanup must never turn graceful worker shutdown into a
                    # crash; the process is exiting and sockets will be reaped.
                    pass


class ThreadLocalClientProxy:
    """Attribute proxy backed by one exclusively-owned client per thread."""

    def __init__(
        self,
        factory: Callable[[], Any],
        *,
        on_create: Optional[Callable[[Any], None]] = None,
        closer: Callable[[Any], None] = close_supabase_client,
        reuse: Optional[bool] = None,
        max_idle: Optional[int] = None,
        stats_label: str = 'sb-pool',
    ) -> None:
        self._factory = factory
        self._on_create = on_create
        self._closer = closer
        self._local = threading.local()
        # RLock, not Lock, is load-bearing: a garbage collection triggered by an
        # allocation inside a locked block runs the thread-death finalizer on
        # *this* thread, which re-enters the lock.  A plain Lock would deadlock.
        self._lock = threading.RLock()
        self._clients: Dict[int, Any] = {}
        self._finalizers: Dict[int, Any] = {}
        self._idle: List[Any] = []
        # ids of clients that must never be parked: a request on them failed, or
        # the health probe could not be installed so we cannot tell.
        self._unreusable: set = set()
        self._generation = 0
        self._pid = os.getpid()
        self._reuse = (
            _env_flag('AEROTAX_SB_CLIENT_REUSE', True) if reuse is None else bool(reuse)
        )
        self._max_idle = (
            _env_int('AEROTAX_SB_MAX_IDLE_CLIENTS', _DEFAULT_MAX_IDLE)
            if max_idle is None
            else int(max_idle)
        )
        self._stats_label = stats_label
        self._stats = {
            'created': 0,      # real factory calls (the expensive path)
            'reused': 0,       # checkouts served from the idle pool
            'released': 0,     # explicit close_current() by the owning thread
            'reaped': 0,       # owner thread died without releasing
            'parked': 0,       # handed to the idle pool
            'destroyed': 0,    # actually closed
            'unhealthy': 0,    # refused reuse because a request had failed
            'unprobed': 0,     # refused reuse because health is unobservable
        }
        self._stats_logged_at = time.monotonic()

    # ── configuration ────────────────────────────────────────────────────
    def set_on_create(self, callback: Optional[Callable[[Any], None]]) -> None:
        """Configure initialization applied to every subsequently made client."""

        with self._lock:
            self._on_create = callback
            # Any client made before the callback existed never ran it (for the
            # app that means: no retry transport installed).  Retire all of them
            # rather than reason about half-initialized reuse.  Draining `_idle`
            # alone would not be enough: a client already popped by a concurrent
            # checkout is in neither collection at that instant, and only the
            # generation bump inside close_all() still catches it before
            # current_client() publishes it.
            stale = bool(self._clients or self._idle)
        if stale:
            self.close_all()

    # ── fork safety ──────────────────────────────────────────────────────
    def _reset_after_fork_if_needed(self) -> None:
        pid = os.getpid()
        if pid == self._pid:
            return
        # A fork copies sockets and locks.  Never use those inherited pools in
        # the child; advancing the generation makes every thread initialize a
        # fresh client on its next access.
        with self._lock:
            if pid == self._pid:
                return
            inherited = list(self._clients.values()) + list(self._idle)
            self._clients.clear()
            self._idle.clear()
            self._unreusable.clear()
            self._detach_all_finalizers_locked()
            self._generation += 1
            self._pid = pid
        for client in inherited:
            self._closer(client)

    # ── registry bookkeeping (all callers hold self._lock) ───────────────
    def _detach_all_finalizers_locked(self) -> None:
        finalizers, self._finalizers = self._finalizers, {}
        for finalizer in finalizers.values():
            try:
                finalizer.detach()
            except Exception:
                pass

    def _bind_locked(self, client: Any, generation: int) -> None:
        """Register ``client`` as owned by the calling thread."""

        key = id(client)
        self._clients[key] = client
        previous = self._finalizers.pop(key, None)
        if previous is not None:
            try:
                previous.detach()
            except Exception:
                pass
        # The finalizer fires when the owning Thread object is collected, which
        # cannot happen while the thread is still running.  It captures only
        # ints, so it does not extend the client's lifetime by itself.
        finalizer = weakref.finalize(
            threading.current_thread(), self._reap_dead_owner, generation, key
        )
        # Interpreter shutdown is handled by the atexit-registered close_all();
        # letting finalizers also fire there only risks double-close noise.
        finalizer.atexit = False
        self._finalizers[key] = finalizer

    def _unbind_locked(self, key: int) -> Any:
        finalizer = self._finalizers.pop(key, None)
        if finalizer is not None:
            try:
                finalizer.detach()
            except Exception:
                pass
        return self._clients.pop(key, None)

    def _mark_unreusable(self, key: int, reason: str) -> None:
        with self._lock:
            if key not in self._unreusable:
                self._unreusable.add(key)
                self._stats[reason] += 1

    def _install_health_probe(self, client: Any) -> None:
        """Make failures on ``client`` observable, or mark it unreusable.

        Runs after ``on_create`` so the probe ends up outside the app's retry
        transport and only sees failures that survived the retries.
        """

        key = id(client)
        try:
            session = getattr(getattr(client, 'postgrest', None), 'session', None)
            transport = getattr(session, '_transport', None)
            if transport is None:
                self._mark_unreusable(key, 'unprobed')
                return
            if isinstance(transport, _HealthProbeTransport):
                return
            session._transport = _HealthProbeTransport(
                transport, lambda: self._mark_unreusable(key, 'unhealthy')
            )
        except Exception:
            # Never let diagnostics break client creation; just do not reuse it.
            self._mark_unreusable(key, 'unprobed')

    def _park_or_close_locked(self, client: Any, generation: int) -> bool:
        """Return True when the client was parked for reuse (still open)."""

        key = id(client)
        if (
            self._reuse
            and generation == self._generation
            and key not in self._unreusable
            and len(self._idle) < self._max_idle
        ):
            self._idle.append(client)
            self._stats['parked'] += 1
            return True
        self._unreusable.discard(key)
        self._stats['destroyed'] += 1
        return False

    def _reap_dead_owner(self, generation: int, key: int) -> None:
        """Called once the thread that owned ``key`` has been collected.

        Runs on whichever thread happened to trigger the collection.  The owner
        is gone, so the client has no user and can be parked for the next
        thread instead of being rebuilt at ~70 ms of CPU.
        """

        if os.getpid() != self._pid:
            # We are in a freshly forked child: ``threading._after_fork()`` drops
            # every non-surviving Thread object, which fires these finalizers
            # before any application code runs.  Taking the lock there could
            # deadlock on an RLock that a parent thread held at fork time, and
            # there is nothing to do anyway — the inherited state is discarded
            # wholesale by _reset_after_fork_if_needed().
            return

        with self._lock:
            self._finalizers.pop(key, None)
            client = self._clients.pop(key, None)
            if client is None:
                return
            self._stats['reaped'] += 1
            parked = self._park_or_close_locked(client, generation)
        if not parked:
            self._closer(client)

    # ── checkout ─────────────────────────────────────────────────────────
    def current_client(self) -> Any:
        """Return this thread's client, creating or checking one out once."""

        self._reset_after_fork_if_needed()
        generation = self._generation
        entry = getattr(self._local, "entry", None)
        if entry is not None and entry[0] == generation:
            return entry[1]

        client = None
        if self._reuse:
            with self._lock:
                if generation == self._generation and self._idle:
                    client = self._idle.pop()
                    self._stats['reused'] += 1

        if client is None:
            client = self._factory()
            callback = self._on_create
            try:
                if callback is not None:
                    callback(client)
            except Exception:
                self._closer(client)
                raise
            # After on_create, so the probe wraps the app's retry transport.
            self._install_health_probe(client)
            with self._lock:
                self._stats['created'] += 1

        with self._lock:
            # ``close_all`` may have advanced the generation while the factory
            # was running.  Do not publish a client for the stale generation.
            if generation != self._generation:
                self._unreusable.discard(id(client))
                self._closer(client)
                return self.current_client()
            self._bind_locked(client, generation)
            self._local.entry = (generation, client)
        self._maybe_log_stats()
        return client

    # ── release ──────────────────────────────────────────────────────────
    def close_current(self) -> None:
        """Declare the calling thread finished with its client.

        Historically this destroyed the client.  With reuse enabled it hands it
        to the idle pool instead — the calling thread is done, so the client has
        no user, and the next thread avoids the ~70 ms CPU rebuild.  The proxy
        forgets the binding either way, so a later ``sb.…`` access on this
        thread checks out afresh.
        """

        entry = getattr(self._local, "entry", None)
        if entry is None:
            return
        generation, client = entry
        try:
            del self._local.entry
        except AttributeError:
            pass
        with self._lock:
            if self._unbind_locked(id(client)) is None:
                # Already reaped or invalidated by close_all — nothing to do.
                return
            self._stats['released'] += 1
            parked = self._park_or_close_locked(client, generation)
        if not parked:
            self._closer(client)

    def close_all(self) -> None:
        """Close all known pools and force lazy reinitialization on next use."""

        with self._lock:
            clients = list(self._clients.values()) + list(self._idle)
            self._clients.clear()
            self._idle.clear()
            self._unreusable.clear()
            self._detach_all_finalizers_locked()
            self._generation += 1
            self._stats['destroyed'] += len(clients)
        try:
            del self._local.entry
        except AttributeError:
            pass
        for client in clients:
            self._closer(client)

    # ── diagnostics ──────────────────────────────────────────────────────
    def clients_snapshot(self):
        """Testing/diagnostics snapshot of thread-owned clients; no credentials."""

        with self._lock:
            return tuple(self._clients.values())

    def idle_snapshot(self):
        """Testing/diagnostics snapshot of parked, unowned clients."""

        with self._lock:
            return tuple(self._idle)

    def pool_stats(self) -> Dict[str, int]:
        """Counters for the reuse pool; safe to log (no credentials)."""

        with self._lock:
            stats = dict(self._stats)
            stats['live'] = len(self._clients)
            stats['idle'] = len(self._idle)
            stats['reuse'] = 1 if self._reuse else 0
            stats['max_idle'] = self._max_idle
            stats['unreusable_now'] = len(self._unreusable)
            return stats

    def _maybe_log_stats(self) -> None:
        """Emit one throttled line per process so production can be measured."""

        now = time.monotonic()
        with self._lock:
            if now - self._stats_logged_at < _STATS_LOG_INTERVAL_S:
                return
            self._stats_logged_at = now
            stats = self.pool_stats()
        try:
            served = stats['created'] + stats['reused']
            hit = (100.0 * stats['reused'] / served) if served else 0.0
            print(
                f"[{self._stats_label}] pid={os.getpid()} created={stats['created']} "
                f"reused={stats['reused']} hit={hit:.0f}% live={stats['live']} "
                f"idle={stats['idle']} released={stats['released']} "
                f"reaped={stats['reaped']} destroyed={stats['destroyed']} "
                f"unhealthy={stats['unhealthy']} unprobed={stats['unprobed']}",
                flush=True,
            )
        except Exception:
            pass

    def __getattr__(self, name: str) -> Any:
        # Called only when the attribute is not a proxy implementation detail.
        return getattr(self.current_client(), name)
