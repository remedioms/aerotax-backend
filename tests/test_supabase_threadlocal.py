"""Regression tests for gunicorn-gthread Supabase transport isolation."""

import threading
from concurrent.futures import ThreadPoolExecutor

from supabase_threadlocal import ThreadLocalClientProxy, close_supabase_client


class _Transport:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _Postgrest:
    def __init__(self):
        self.session = _Transport()


class _Client:
    def __init__(self, serial):
        self.serial = serial
        self._postgrest = _Postgrest()
        self._storage = None
        self._functions = None
        self.auth = _Transport()

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


def test_close_current_closes_pool_and_reinitializes_lazily():
    created = []

    def factory():
        client = _Client(len(created) + 1)
        created.append(client)
        return client

    proxy = ThreadLocalClientProxy(factory)
    first = proxy.current_client()
    proxy.close_current()

    assert first.auth.closed is True
    assert first._postgrest.session.closed is True
    assert proxy.clients_snapshot() == ()
    second = proxy.current_client()
    assert second is not first
    assert second.serial == 2


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

    proxy = ThreadLocalClientProxy(factory)

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
