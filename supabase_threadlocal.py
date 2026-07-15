"""Thread-isolated synchronous Supabase clients.

``supabase-py``'s synchronous PostgREST client owns one HTTP/2 ``httpx.Client``.
Sharing that object between gunicorn ``gthread`` workers lets independent
requests mutate the same h2 connection state concurrently.  The resulting
failures are transport-level (``StreamIDTooLowError``,
``LocalProtocolError(ConnectionInputs.*)``), so retrying on the same shared
client cannot make the design safe.

This small proxy preserves the existing ``sb.table(...)`` API while creating
at most one real client per long-lived worker thread.  A generation counter
makes explicit cleanup deterministic and avoids reusing inherited clients
after a fork.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Callable, Dict, Optional


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
    """Attribute proxy backed by one lazily-created client per thread."""

    def __init__(
        self,
        factory: Callable[[], Any],
        *,
        on_create: Optional[Callable[[Any], None]] = None,
        closer: Callable[[Any], None] = close_supabase_client,
    ) -> None:
        self._factory = factory
        self._on_create = on_create
        self._closer = closer
        self._local = threading.local()
        self._lock = threading.RLock()
        self._clients: Dict[int, Any] = {}
        self._generation = 0
        self._pid = os.getpid()

    def set_on_create(self, callback: Optional[Callable[[Any], None]]) -> None:
        """Configure initialization applied to every subsequently made client."""

        with self._lock:
            self._on_create = callback

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
            inherited = list(self._clients.values())
            self._clients.clear()
            self._generation += 1
            self._pid = pid
        for client in inherited:
            self._closer(client)

    def current_client(self) -> Any:
        """Return this thread's client, creating and initializing it once."""

        self._reset_after_fork_if_needed()
        generation = self._generation
        entry = getattr(self._local, "entry", None)
        if entry is not None and entry[0] == generation:
            return entry[1]

        client = self._factory()
        callback = self._on_create
        try:
            if callback is not None:
                callback(client)
        except Exception:
            self._closer(client)
            raise

        with self._lock:
            # ``close_all`` may have advanced the generation while the factory
            # was running.  Do not publish a client for the stale generation.
            if generation != self._generation:
                self._closer(client)
                return self.current_client()
            self._clients[id(client)] = client
            self._local.entry = (generation, client)
        return client

    def clients_snapshot(self):
        """Testing/diagnostics snapshot; never includes credentials."""

        with self._lock:
            return tuple(self._clients.values())

    def close_current(self) -> None:
        """Close and forget the calling thread's client, if initialized."""

        entry = getattr(self._local, "entry", None)
        if entry is None:
            return
        client = entry[1]
        try:
            del self._local.entry
        except AttributeError:
            pass
        with self._lock:
            self._clients.pop(id(client), None)
        self._closer(client)

    def close_all(self) -> None:
        """Close all known pools and force lazy reinitialization on next use."""

        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
            self._generation += 1
        try:
            del self._local.entry
        except AttributeError:
            pass
        for client in clients:
            self._closer(client)

    def __getattr__(self, name: str) -> Any:
        # Called only when the attribute is not a proxy implementation detail.
        return getattr(self.current_client(), name)
