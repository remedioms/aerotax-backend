"""Cost-control tests are offline; no provider request or Supabase write occurs."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

from blueprints import paid_cost_control as C


def _budget():
    state = {}
    lock = threading.Lock()

    def used(key):
        with lock:
            return state.get(key, 0)

    def adjust(key, delta):
        with lock:
            state[key] = max(0, state.get(key, 0) + delta)

    return state, used, adjust


def setup_function():
    C.reset_local_state()


def _run(fetch, state, used, adjust, **kw):
    return C.paid_fetch(
        sb=None, call_key=kw.pop("call_key", "fr24:FN:LH400:2026-07-14"),
        provider="fr24", day_key="fr24:20260714", month_key="fr24m:202607",
        reserve_units=20, day_cap=100, month_cap=1000, fetch=fetch,
        actual_units=lambda payload: max(2, len((payload or {}).get("data") or [])),
        budget_used=used, budget_adjust=adjust, wait_seconds=1.0,
        positive_ttl=3600, allow_local=True, **kw)


def test_reserves_max_before_call_then_refunds_to_actual():
    state, used, adjust = _budget()

    def fetch():
        # The maximum reservation is visible before any provider work starts.
        assert state["fr24:20260714"] == 20
        assert state["fr24m:202607"] == 20
        return {"data": [{"flight": "LH400"}]}

    out = _run(fetch, state, used, adjust)
    assert out.source == "upstream" and out.actual_units == 2
    assert state == {"fr24:20260714": 2, "fr24m:202607": 2}


def test_positive_payload_shared_without_second_spend():
    state, used, adjust = _budget()
    calls = 0

    def fetch():
        nonlocal calls
        calls += 1
        return {"data": [{"flight": "LH400"}]}

    first = _run(fetch, state, used, adjust)
    second = _run(fetch, state, used, adjust)
    assert first.source == "upstream"
    assert second.source == "shared_cache" and second.payload == first.payload
    assert calls == 1
    assert state["fr24:20260714"] == 2


def test_parallel_callers_are_singleflight():
    state, used, adjust = _budget()
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def fetch():
        nonlocal calls
        calls += 1
        entered.set()
        release.wait(timeout=2)
        return {"data": [{"flight": "LH400"}]}

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_run, fetch, state, used, adjust)
        assert entered.wait(timeout=1)
        second = pool.submit(_run, fetch, state, used, adjust)
        time.sleep(0.05)
        release.set()
        results = [first.result(timeout=2), second.result(timeout=2)]
    assert calls == 1
    assert sorted(r.source for r in results) == ["shared_cache", "upstream"]
    assert state["fr24:20260714"] == 2


def test_reason_specific_negative_ttl_allows_early_transient_retry():
    state, used, adjust = _budget()
    now = [1000.0]
    calls = 0

    def fetch():
        nonlocal calls
        calls += 1
        return None

    args = dict(clock=lambda: now[0], sleeper=lambda _n: None,
                negative_ttls={"upstream_error": 5})
    assert _run(fetch, state, used, adjust, **args).negative_reason == "upstream_error"
    assert _run(fetch, state, used, adjust, **args).source == "negative"
    now[0] += 6
    assert _run(fetch, state, used, adjust, **args).source == "negative"
    assert calls == 2
    # Each attempted provider request costs the 2-credit minimum.
    assert state["fr24:20260714"] == 4


def test_not_found_uses_long_negative_cache():
    state, used, adjust = _budget()
    now = [1000.0]
    calls = 0

    def fetch():
        nonlocal calls
        calls += 1
        return {"data": []}

    args = dict(clock=lambda: now[0], sleeper=lambda _n: None,
                negative_ttls={"not_found": 100})
    assert _run(fetch, state, used, adjust, **args).negative_reason == "not_found"
    now[0] += 99
    assert _run(fetch, state, used, adjust, **args).source == "negative"
    assert calls == 1


def test_budget_denial_happens_before_fetch():
    state, used, adjust = _budget()
    state["fr24:20260714"] = 90
    called = False

    def fetch():
        nonlocal called
        called = True
        return {"data": [{}]}

    out = _run(fetch, state, used, adjust)
    assert out.source == "denied" and out.negative_reason == "budget_denied"
    assert called is False and state["fr24:20260714"] == 90


def test_charge_over_reservation_is_critical_negative_and_blocks_reuse():
    state, used, adjust = _budget()
    calls = 0

    def fetch():
        nonlocal calls
        calls += 1
        # Defensive simulation of a provider violating an explicit result limit.
        return {"data": [{} for _ in range(21)]}

    first = _run(fetch, state, used, adjust)
    second = _run(fetch, state, used, adjust)
    assert first.source == "negative"
    assert first.negative_reason == "reservation_overrun"
    assert second.source == "negative" and calls == 1
    # Truthful accounting is retained (never silently clipped to the reserve),
    # so every later reservation sees the debt and the cap stays closed.
    assert state["fr24:20260714"] == 21


class _BrokenRPC:
    def rpc(self, _name, _params):
        raise RuntimeError("PGRST202 Could not find the function")


def test_database_present_but_migration_missing_fails_closed():
    state, used, adjust = _budget()
    called = False

    def fetch():
        nonlocal called
        called = True

    out = C.paid_fetch(
        sb=_BrokenRPC(), call_key="fr24:FN:LH400", provider="fr24",
        day_key="d", month_key="m", reserve_units=20,
        day_cap=100, month_cap=1000, fetch=fetch,
        actual_units=lambda _p: 2, budget_used=used, budget_adjust=adjust,
        wait_seconds=0)
    assert out.source == "denied"
    assert out.negative_reason == "control_unavailable"
    assert called is False and not state


def test_no_distributed_store_fails_closed_unless_local_explicitly_allowed():
    state, used, adjust = _budget()
    called = False

    def fetch():
        nonlocal called
        called = True

    out = C.paid_fetch(
        sb=None, call_key="fr24:FN:LH400", provider="fr24",
        day_key="d", month_key="m", reserve_units=20,
        day_cap=100, month_cap=1000, fetch=fetch,
        actual_units=lambda _p: 2, budget_used=used, budget_adjust=adjust)
    assert out.negative_reason == "control_unavailable"
    assert called is False


class _Response:
    def __init__(self, data):
        self.data = data


class _ScriptedRPC:
    def __init__(self):
        self.calls = []

    def rpc(self, name, params):
        self.calls.append((name, params))
        values = {
            "ax_paid_call_acquire": {"status": "acquired"},
            "ax_paid_reserve_budget": {"status": "granted", "reserved_units": 20},
            "ax_paid_reconcile_budget": True,
            "ax_paid_call_complete": True,
        }
        response = _Response(values[name])

        class _Request:
            def execute(self):
                return response

        return _Request()


def test_distributed_path_reserves_before_fetch_and_reconciles_after():
    state, used, adjust = _budget()
    sb = _ScriptedRPC()

    def fetch():
        assert [name for name, _params in sb.calls] == [
            "ax_paid_call_acquire", "ax_paid_reserve_budget"]
        return {"data": [{"flight": "LH400"}]}

    out = C.paid_fetch(
        sb=sb, call_key="fr24:FN:LH400", provider="fr24",
        day_key="d", month_key="m", reserve_units=20,
        day_cap=100, month_cap=1000, fetch=fetch,
        actual_units=lambda _p: 2, budget_used=used, budget_adjust=adjust)
    assert out.source == "upstream" and out.actual_units == 2
    assert [name for name, _params in sb.calls] == [
        "ax_paid_call_acquire", "ax_paid_reserve_budget",
        "ax_paid_reconcile_budget", "ax_paid_call_complete"]
    reserve_params = sb.calls[1][1]
    assert reserve_params["p_units"] == 20
    assert reserve_params["p_day_cap"] == 100
    assert reserve_params["p_month_cap"] == 1000
    reconcile_params = sb.calls[2][1]
    assert reconcile_params["p_actual_units"] == 2
