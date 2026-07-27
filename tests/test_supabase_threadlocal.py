"""Regression tests for gunicorn-gthread Supabase transport isolation."""

import gc
import threading
from concurrent.futures import ThreadPoolExecutor

from supabase_threadlocal import (
    ThreadLocalClientProxy,
    _HealthProbeTransport,
    close_supabase_client,
)


class _Transport:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _InnerHttpTransport:
    """Stand-in for the real httpx transport the health probe wraps."""

    def __init__(self):
        self.fail_with = None
        self.calls = 0

    def handle_request(self, request):
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return request

    def close(self):
        pass


class _Session(_Transport):
    def __init__(self):
        super().__init__()
        self._transport = _InnerHttpTransport()


class _Postgrest:
    def __init__(self):
        self.session = _Session()


class _Client:
    def __init__(self, serial):
        self.serial = serial
        self._postgrest = _Postgrest()
        self._storage = None
        self._functions = None
        self.auth = _Transport()

    # The proxy's health probe reaches through the public `postgrest` property,
    # exactly like app._install_sb_retry_transport does.
    @property
    def postgrest(self):
        return self._postgrest

    def table(self, name):
        return self.serial, name


def test_concurrent_threads_never_share_client_or_http_transport():
    lock = threading.Lock()
    serial = 0
    created = []
    initialized = []

    def factory():
        nonlocal serial
        with lock:
            serial += 1
            client = _Client(serial)
            created.append(client)
            return client

    proxy = ThreadLocalClientProxy(
        factory, on_create=lambda client: initialized.append(client.serial)
    )
    barrier = threading.Barrier(8)
    observations = []

    def worker():
        first = proxy.current_client()
        barrier.wait(timeout=5)
        second = proxy.current_client()
        with lock:
            observations.append(
                (id(first), id(first._postgrest.session), id(second))
            )

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert len(created) == 8
    assert sorted(initialized) == list(range(1, 9))
    assert len({client_id for client_id, _, _ in observations}) == 8
    assert len({transport_id for _, transport_id, _ in observations}) == 8
    assert all(first_id == second_id for first_id, _, second_id in observations)


def test_same_thread_reuses_client_and_proxy_keeps_existing_api():
    proxy = ThreadLocalClientProxy(lambda: _Client(41))

    assert proxy.current_client() is proxy.current_client()
    assert proxy.table("jobs") == (41, "jobs")
    assert len(proxy.clients_snapshot()) == 1


def test_close_current_destroys_pool_when_reuse_disabled():
    created = []

    def factory():
        client = _Client(len(created) + 1)
        created.append(client)
        return client

    proxy = ThreadLocalClientProxy(factory, reuse=False)
    first = proxy.current_client()
    proxy.close_current()

    assert first.auth.closed is True
    assert first._postgrest.session.closed is True
    assert proxy.clients_snapshot() == ()
    assert proxy.idle_snapshot() == ()
    second = proxy.current_client()
    assert second is not first
    assert second.serial == 2


def test_close_current_parks_client_for_reuse_instead_of_rebuilding():
    """The owning thread is done, so the next checkout must not pay ~70 ms CPU."""
    created = []

    def factory():
        client = _Client(len(created) + 1)
        created.append(client)
        return client

    proxy = ThreadLocalClientProxy(factory, reuse=True)
    first = proxy.current_client()
    proxy.close_current()

    # Released, therefore no longer owned by any thread…
    assert proxy.clients_snapshot() == ()
    # …but still open and parked, so it can be handed straight back out.
    assert first.auth.closed is False
    assert first._postgrest.session.closed is False
    assert proxy.idle_snapshot() == (first,)

    second = proxy.current_client()
    assert second is first
    assert len(created) == 1
    assert proxy.idle_snapshot() == ()
    stats = proxy.pool_stats()
    assert stats['created'] == 1 and stats['reused'] == 1


def test_idle_pool_is_bounded_and_excess_clients_are_closed():
    created = []

    def factory():
        client = _Client(len(created) + 1)
        created.append(client)
        return client

    proxy = ThreadLocalClientProxy(factory, reuse=True, max_idle=2)
    # Five threads hold a client at the same time, so five must really exist,
    # then all release at once — only max_idle may survive.
    holding = threading.Barrier(6)
    lock = threading.Lock()
    held = []

    def worker():
        client = proxy.current_client()
        with lock:
            held.append(client)
        holding.wait(timeout=5)
        proxy.close_current()

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    holding.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=5)

    assert len(created) == 5
    assert len({id(c) for c in held}) == 5
    assert proxy.clients_snapshot() == ()
    assert len(proxy.idle_snapshot()) == 2
    # Everything that did not fit in the bounded pool was really closed.
    assert sum(1 for c in created if c.auth.closed) == 3
    assert all(not c.auth.closed for c in proxy.idle_snapshot())


def test_close_all_invalidates_generation_and_closes_every_thread_client():
    lock = threading.Lock()
    created = []
    ready = threading.Barrier(3)
    release = threading.Barrier(3)

    def factory():
        with lock:
            client = _Client(len(created) + 1)
            created.append(client)
            return client

    proxy = ThreadLocalClientProxy(factory)

    def worker():
        proxy.current_client()
        ready.wait(timeout=5)
        release.wait(timeout=5)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    # Main thread is the third barrier participant and has its own client.
    main_first = proxy.current_client()
    ready.wait(timeout=5)
    assert len(proxy.clients_snapshot()) == 3
    proxy.close_all()
    assert proxy.clients_snapshot() == ()
    assert all(client.auth.closed for client in created)
    assert all(client._postgrest.session.closed for client in created)
    release.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=5)

    main_second = proxy.current_client()
    assert main_second is not main_first
    assert len(created) == 4


def test_close_helper_does_not_materialize_lazy_postgrest_property():
    class ClientWithLazyProperty:
        auth = _Transport()
        _postgrest = None
        _storage = None
        _functions = None

        @property
        def postgrest(self):  # pragma: no cover - access is the regression
            raise AssertionError("shutdown must not create a new HTTP pool")

    client = ClientWithLazyProperty()
    close_supabase_client(client)
    assert client.auth.closed is True


def test_per_request_executor_workers_can_release_registry_entries():
    """Short-lived request pools must not retain one client per dead thread."""
    lock = threading.Lock()
    created = []

    def factory():
        with lock:
            client = _Client(len(created) + 1)
            created.append(client)
            return client

    proxy = ThreadLocalClientProxy(factory, reuse=False)

    def request_worker():
        try:
            return proxy.current_client().serial
        finally:
            proxy.close_current()

    # Mirrors friends-today/route-history: a new executor per request.
    for _ in range(5):
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda _i: request_worker(), range(8)))
        assert proxy.clients_snapshot() == ()

    assert created
    assert all(client.auth.closed for client in created)
    assert all(client._postgrest.session.closed for client in created)


def test_per_request_executor_reuses_clients_instead_of_rebuilding():
    """The same shape as above, but reuse must collapse the factory calls."""
    lock = threading.Lock()
    created = []

    def factory():
        with lock:
            client = _Client(len(created) + 1)
            created.append(client)
            return client

    proxy = ThreadLocalClientProxy(factory, reuse=True, max_idle=4)

    def request_worker():
        try:
            return proxy.current_client().serial
        finally:
            proxy.close_current()

    for _ in range(5):
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda _i: request_worker(), range(8)))
        assert proxy.clients_snapshot() == ()

    # 5 requests x 8 tasks on 4 threads each: without reuse this path built a
    # brand-new client (measured ~53 ms CPU) for every fresh executor thread.
    assert len(created) <= 4
    assert len(proxy.idle_snapshot()) <= 4
    stats = proxy.pool_stats()
    assert stats['reused'] >= 20


def test_dead_thread_without_explicit_release_does_not_leak_registry_entry():
    """The 23.07. leak: a thread that dies without releasing left its client behind.

    ``lh-warm`` and several ad-hoc ``threading.Thread`` fan-outs never called
    ``close_current()``.  Each of those threads used to add one permanent
    ``_clients`` entry — client, PostgREST session, httpx pool and SSL context
    stayed reachable for the whole life of the gunicorn worker.
    """
    lock = threading.Lock()
    created = []

    def factory():
        with lock:
            client = _Client(len(created) + 1)
            created.append(client)
            return client

    proxy = ThreadLocalClientProxy(factory, reuse=False, max_idle=0)

    for _ in range(6):
        # No try/finally, no close_current() — exactly the leaking pattern.
        thread = threading.Thread(target=lambda: proxy.current_client())
        thread.start()
        thread.join(timeout=5)
        del thread
        gc.collect()

    assert created
    # Before the fix this was len(created); the entries were never reclaimed.
    assert proxy.clients_snapshot() == ()
    assert proxy.idle_snapshot() == ()
    assert all(client.auth.closed for client in created)
    assert proxy.pool_stats()['reaped'] == len(created)


def test_dead_thread_client_is_parked_for_the_next_thread():
    lock = threading.Lock()
    created = []

    def factory():
        with lock:
            client = _Client(len(created) + 1)
            created.append(client)
            return client

    proxy = ThreadLocalClientProxy(factory, reuse=True, max_idle=4)

    for _ in range(6):
        thread = threading.Thread(target=lambda: proxy.current_client())
        thread.start()
        thread.join(timeout=5)
        del thread
        gc.collect()

    assert proxy.clients_snapshot() == ()
    # Dead owners hand their client on instead of it being rebuilt every time.
    assert len(created) == 1
    assert len(proxy.idle_snapshot()) == 1


def test_close_all_drains_the_idle_pool():
    created = []

    def factory():
        client = _Client(len(created) + 1)
        created.append(client)
        return client

    proxy = ThreadLocalClientProxy(factory, reuse=True, max_idle=4)
    first = proxy.current_client()
    proxy.close_current()
    assert proxy.idle_snapshot() == (first,)

    proxy.close_all()
    assert proxy.idle_snapshot() == ()
    assert proxy.clients_snapshot() == ()
    assert first.auth.closed is True
    assert first._postgrest.session.closed is True

    second = proxy.current_client()
    assert second is not first


def test_set_on_create_drops_uninitialized_parked_clients():
    """A parked client that never ran on_create must not be handed out later."""
    created = []

    def factory():
        client = _Client(len(created) + 1)
        created.append(client)
        return client

    proxy = ThreadLocalClientProxy(factory, reuse=True, max_idle=4)
    first = proxy.current_client()
    proxy.close_current()
    assert proxy.idle_snapshot() == (first,)

    initialized = []
    proxy.set_on_create(lambda client: initialized.append(client.serial))
    assert proxy.idle_snapshot() == ()
    assert first.auth.closed is True

    second = proxy.current_client()
    assert second is not first
    assert initialized == [2]


def test_failed_client_is_destroyed_instead_of_parked():
    """A client whose request blew up must never reach another thread.

    Release sites are bare ``finally:`` blocks, so they also fire on the error
    path.  Parking a damaged client would be worse than the old destroy-on-
    release behaviour, because the next thread to check one out may be a
    long-lived daemon that never releases — a silent, permanent failure.
    """
    created = []

    def factory():
        client = _Client(len(created) + 1)
        created.append(client)
        return client

    proxy = ThreadLocalClientProxy(factory, reuse=True, max_idle=4)
    client = proxy.current_client()
    session = client.postgrest.session
    probe = session._transport
    assert isinstance(probe, _HealthProbeTransport)
    probe._inner.fail_with = RuntimeError("h2 went bad")

    try:
        probe.handle_request(object())
    except RuntimeError:
        pass

    proxy.close_current()
    assert proxy.idle_snapshot() == ()
    assert client.auth.closed is True
    assert proxy.pool_stats()['unhealthy'] == 1

    second = proxy.current_client()
    assert second is not client


def test_healthy_client_still_parks_after_successful_requests():
    created = []

    def factory():
        client = _Client(len(created) + 1)
        created.append(client)
        return client

    proxy = ThreadLocalClientProxy(factory, reuse=True, max_idle=4)
    client = proxy.current_client()
    client.postgrest.session._transport.handle_request(object())
    proxy.close_current()

    assert proxy.idle_snapshot() == (client,)
    assert proxy.pool_stats()['unhealthy'] == 0
    assert proxy.current_client() is client


def test_health_probe_wraps_on_create_transport_and_stays_outermost():
    """The probe must sit outside the retry layer, and survive a reuse cycle."""
    order = []

    class _RetryLike:
        def __init__(self, inner):
            self._inner = inner

        def handle_request(self, request):
            order.append("retry")
            return self._inner.handle_request(request)

        def close(self):
            self._inner.close()

    def on_create(client):
        session = client.postgrest.session
        session._transport = _RetryLike(session._transport)

    proxy = ThreadLocalClientProxy(lambda: _Client(1), on_create=on_create,
                                   reuse=True, max_idle=4)
    client = proxy.current_client()
    outer = client.postgrest.session._transport
    assert isinstance(outer, _HealthProbeTransport)
    assert isinstance(outer._inner, _RetryLike)

    outer.handle_request(object())
    assert order == ["retry"]

    proxy.close_current()
    reused = proxy.current_client()
    assert reused is client
    # on_create must not run again, so the probe must not double-wrap.
    assert client.postgrest.session._transport is outer


def test_client_without_observable_health_is_never_parked():
    """Fail closed: if the probe cannot be installed, reuse stays off."""

    class _OpaqueClient(_Client):
        @property
        def postgrest(self):
            return None

    created = []

    def factory():
        client = _OpaqueClient(len(created) + 1)
        created.append(client)
        return client

    proxy = ThreadLocalClientProxy(factory, reuse=True, max_idle=4)
    first = proxy.current_client()
    proxy.close_current()

    assert proxy.idle_snapshot() == ()
    assert proxy.pool_stats()['unprobed'] == 1
    assert first.auth.closed is True
    assert proxy.current_client() is not first
