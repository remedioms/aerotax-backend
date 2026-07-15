"""Distributed cost control for paid, cacheable upstream lookups.

The database path provides three guarantees before an upstream request starts:

* one worker owns a short lease for a logical lookup (singleflight),
* the maximum possible cost is atomically reserved against day/month caps, and
* retries of the same reservation id are idempotent.

Successful payloads and reason-specific negative results are shared between
workers.  When Supabase is deliberately unavailable (unit tests/development), a
thread-safe process-local implementation keeps the same semantics.  If a
Supabase client exists but the required RPC migration is missing, paid calls
fail closed; silently falling back to per-process accounting would make the
production cap ineffective again.
"""
from __future__ import annotations

import copy
import hashlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional


DEFAULT_NEGATIVE_TTLS = {
    # A syntactically valid lookup with no rows is unlikely to change quickly.
    "not_found": 12 * 3600,
    # Provider throttles are normally short rolling windows.
    "rate_limited": 90,
    # Avoid hammering with a revoked/misconfigured credential while allowing a
    # corrected secret to recover without a deployment.
    "auth": 10 * 60,
    # Transient network/5xx failures must retry much sooner than real misses.
    "upstream_error": 5 * 60,
    "invalid_payload": 10 * 60,
    # A cap can be raised operationally; do not pin a denial until midnight.
    "budget_denied": 60,
    "control_unavailable": 60,
    # Provider violated the request's hard result/cost bound. Keep this logical
    # lookup closed while operators inspect the critical alert.
    "reservation_overrun": 3600,
    "singleflight_busy": 5,
}


@dataclass(frozen=True)
class PaidFetchResult:
    payload: Any = None
    source: str = "none"       # upstream | shared_cache | negative | denied | busy
    negative_reason: Optional[str] = None
    actual_units: int = 0


_LOCK = threading.RLock()
_LOCAL_CALLS: dict[str, dict[str, Any]] = {}
_LOCAL_RESERVATIONS: dict[str, dict[str, Any]] = {}


def reset_local_state() -> None:
    """Test helper; production never needs to clear cost-control state."""
    with _LOCK:
        _LOCAL_CALLS.clear()
        _LOCAL_RESERVATIONS.clear()


def _rpc_data(response: Any) -> Any:
    data = getattr(response, "data", None)
    if isinstance(data, list) and len(data) == 1:
        return data[0]
    return data


def _rpc(sb: Any, name: str, params: dict[str, Any]) -> Any:
    return _rpc_data(sb.rpc(name, params).execute())


def _is_missing_rpc(exc: Exception) -> bool:
    msg = str(exc).lower()
    return ("pgrst202" in msg or "could not find the function" in msg
            or "does not exist" in msg or "schema cache" in msg)


def _local_acquire(call_key: str, owner: str, now: float,
                   lease_seconds: int) -> dict[str, Any]:
    with _LOCK:
        row = _LOCAL_CALLS.get(call_key)
        if row:
            if row.get("result_until", 0) > now:
                return {"status": "hit", "result": copy.deepcopy(row.get("result"))}
            if row.get("negative_until", 0) > now:
                return {"status": "negative",
                        "negative_reason": row.get("negative_reason")}
            if row.get("lease_until", 0) > now and row.get("owner") != owner:
                return {"status": "busy"}
        _LOCAL_CALLS[call_key] = {
            "owner": owner, "lease_until": now + max(1, lease_seconds),
            "result": None, "result_until": 0,
            "negative_reason": None, "negative_until": 0,
            "updated_at": now,
        }
        if len(_LOCAL_CALLS) > 2000:
            expired = sorted(
                ((k, v.get("updated_at", 0)) for k, v in _LOCAL_CALLS.items()
                 if v.get("lease_until", 0) <= now
                 and v.get("result_until", 0) <= now
                 and v.get("negative_until", 0) <= now),
                key=lambda item: item[1])
            for key, _ts in expired[:500]:
                _LOCAL_CALLS.pop(key, None)
        return {"status": "acquired"}


def _acquire(sb: Any, call_key: str, owner: str, now: float,
             lease_seconds: int) -> dict[str, Any]:
    if sb is None:
        return _local_acquire(call_key, owner, now, lease_seconds)
    try:
        data = _rpc(sb, "ax_paid_call_acquire", {
            "p_call_key": call_key, "p_owner": owner,
            "p_lease_seconds": max(1, int(lease_seconds)),
        })
        return data if isinstance(data, dict) else {"status": "unavailable"}
    except Exception as exc:
        # Any database uncertainty is fail-closed.  Missing-RPC detection is
        # retained for precise operational diagnostics without logging secrets.
        return {"status": "unavailable",
                "reason": "migration_missing" if _is_missing_rpc(exc) else "db_error"}


def _local_complete(call_key: str, owner: str, now: float, payload: Any,
                    result_ttl: int, negative_reason: Optional[str],
                    negative_ttl: int) -> bool:
    with _LOCK:
        row = _LOCAL_CALLS.get(call_key)
        if not row or row.get("owner") != owner:
            return False
        row["lease_until"] = 0
        if negative_reason:
            row["negative_reason"] = negative_reason
            row["negative_until"] = now + max(1, negative_ttl)
            row["result"] = None
            row["result_until"] = 0
        else:
            row["negative_reason"] = None
            row["negative_until"] = 0
            row["result"] = copy.deepcopy(payload)
            row["result_until"] = now + max(1, result_ttl)
        row["updated_at"] = now
        return True


def _complete(sb: Any, call_key: str, owner: str, now: float, payload: Any,
              result_ttl: int, negative_reason: Optional[str],
              negative_ttl: int) -> bool:
    if sb is None:
        return _local_complete(call_key, owner, now, payload, result_ttl,
                               negative_reason, negative_ttl)
    try:
        data = _rpc(sb, "ax_paid_call_complete", {
            "p_call_key": call_key, "p_owner": owner,
            "p_result": payload if not negative_reason else None,
            "p_result_ttl_seconds": max(1, int(result_ttl)),
            "p_negative_reason": negative_reason,
            "p_negative_ttl_seconds": max(1, int(negative_ttl)),
        })
        return bool(data)
    except Exception:
        return False


def _local_reserve(idempotency_key: str, day_key: str, month_key: str,
                   units: int, day_cap: int, month_cap: int,
                   budget_used: Callable[[str], int],
                   budget_adjust: Callable[[str, int], None]) -> dict[str, Any]:
    with _LOCK:
        old = _LOCAL_RESERVATIONS.get(idempotency_key)
        if old:
            return {"status": "granted", **old}
        day_used = int(budget_used(day_key) or 0)
        month_used = int(budget_used(month_key) or 0)
        if day_used + units > day_cap or month_used + units > month_cap:
            return {"status": "denied", "day_used": day_used,
                    "month_used": month_used}
        budget_adjust(day_key, units)
        budget_adjust(month_key, units)
        row = {"reserved_units": units, "actual_units": None,
               "day_key": day_key, "month_key": month_key,
               "created_at": time.time()}
        _LOCAL_RESERVATIONS[idempotency_key] = row
        if len(_LOCAL_RESERVATIONS) > 4000:
            done = sorted(
                ((k, v.get("created_at", 0)) for k, v in _LOCAL_RESERVATIONS.items()
                 if v.get("actual_units") is not None), key=lambda item: item[1])
            for key, _ts in done[:1000]:
                _LOCAL_RESERVATIONS.pop(key, None)
        return {"status": "granted", **row,
                "day_used": day_used + units, "month_used": month_used + units}


def _reserve(sb: Any, idempotency_key: str, provider: str, day_key: str,
             month_key: str, units: int, day_cap: int, month_cap: int,
             budget_used: Callable[[str], int],
             budget_adjust: Callable[[str, int], None]) -> dict[str, Any]:
    if sb is None:
        return _local_reserve(idempotency_key, day_key, month_key, units,
                              day_cap, month_cap, budget_used, budget_adjust)
    try:
        data = _rpc(sb, "ax_paid_reserve_budget", {
            "p_idempotency_key": idempotency_key, "p_provider": provider,
            "p_day_key": day_key, "p_month_key": month_key,
            "p_units": units, "p_day_cap": day_cap, "p_month_cap": month_cap,
        })
        return data if isinstance(data, dict) else {"status": "unavailable"}
    except Exception as exc:
        return {"status": "unavailable",
                "reason": "migration_missing" if _is_missing_rpc(exc) else "db_error"}


def _local_reconcile(idempotency_key: str, actual_units: int,
                     budget_adjust: Callable[[str, int], None]) -> bool:
    with _LOCK:
        row = _LOCAL_RESERVATIONS.get(idempotency_key)
        if not row:
            return False
        if row.get("actual_units") is not None:
            return True
        delta = actual_units - int(row["reserved_units"])
        if delta:
            budget_adjust(row["day_key"], delta)
            budget_adjust(row["month_key"], delta)
        row["actual_units"] = actual_units
        return True


def _reconcile(sb: Any, idempotency_key: str, actual_units: int,
               state: str, budget_adjust: Callable[[str, int], None]) -> bool:
    if sb is None:
        return _local_reconcile(idempotency_key, actual_units, budget_adjust)
    try:
        return bool(_rpc(sb, "ax_paid_reconcile_budget", {
            "p_idempotency_key": idempotency_key,
            "p_actual_units": max(0, int(actual_units)), "p_state": state,
        }))
    except Exception:
        # The reservation remains conservatively charged at its maximum.  That
        # can reduce availability but can never permit an overspend.
        return False


def classify_payload(payload: Any) -> Optional[str]:
    """Return a stable negative reason, or ``None`` for a usable response."""
    if payload is None:
        return "upstream_error"
    if not isinstance(payload, Mapping):
        return "invalid_payload"
    marker = str(payload.get("_ax_error") or "").lower()
    if marker in DEFAULT_NEGATIVE_TTLS:
        return marker
    data = payload.get("data")
    if data is not None and not isinstance(data, list):
        return "invalid_payload"
    if isinstance(data, list) and not data:
        return "not_found"
    return None


def paid_fetch(*, sb: Any, call_key: str, provider: str,
               day_key: str, month_key: str, reserve_units: int,
               day_cap: int, month_cap: int, fetch: Callable[[], Any],
               actual_units: Callable[[Any], int],
               budget_used: Callable[[str], int],
               budget_adjust: Callable[[str, int], None],
               positive_ttl: int = 6 * 3600,
               negative_ttls: Optional[Mapping[str, int]] = None,
               lease_seconds: int = 20, wait_seconds: float = 2.5,
               allow_local: bool = False,
               clock: Callable[[], float] = time.time,
               sleeper: Callable[[float], None] = time.sleep,
               logger: Optional[logging.Logger] = None) -> PaidFetchResult:
    """Run one paid lookup with distributed singleflight and atomic budgeting.

    ``reserve_units`` must be the maximum provider charge for this request
    shape.  The actual charge is reconciled afterwards, atomically refunding the
    difference.  A busy waiter never performs a second paid request.
    """
    log = logger or logging.getLogger(__name__)
    neg_ttls = dict(DEFAULT_NEGATIVE_TTLS)
    if negative_ttls:
        neg_ttls.update({str(k): max(1, int(v)) for k, v in negative_ttls.items()})
    call_key = str(call_key)[:240]
    if sb is None and not allow_local:
        # Database loss must not turn a multi-worker production deployment into
        # several independent spend counters.  Tests/dev can opt into the
        # semantically equivalent process-local implementation explicitly.
        log.error("paid cost-control has no distributed store provider=%s", provider)
        return PaidFetchResult(source="denied",
                               negative_reason="control_unavailable")
    owner = uuid.uuid4().hex
    deadline = clock() + max(0.0, float(wait_seconds))

    while True:
        lease = _acquire(sb, call_key, owner, clock(), lease_seconds)
        status = lease.get("status")
        if status == "hit":
            return PaidFetchResult(payload=lease.get("result"), source="shared_cache")
        if status == "negative":
            return PaidFetchResult(source="negative",
                                   negative_reason=lease.get("negative_reason"))
        if status == "acquired":
            break
        if status == "busy" and clock() < deadline:
            sleeper(min(0.1, max(0.0, deadline - clock())))
            continue
        if status == "unavailable":
            log.error("paid cost-control unavailable provider=%s reason=%s",
                      provider, lease.get("reason") or "unknown")
            return PaidFetchResult(source="denied",
                                   negative_reason="control_unavailable")
        return PaidFetchResult(source="busy", negative_reason="singleflight_busy")

    # One reservation per acquired lease attempt.  A retry of the RPC itself is
    # idempotent, while a later legitimate retry receives a fresh key.
    idem_raw = "%s|%s|%s" % (provider, call_key, owner)
    idem = hashlib.sha256(idem_raw.encode("utf-8")).hexdigest()
    reservation = _reserve(
        sb, idem, provider, day_key, month_key, max(1, int(reserve_units)),
        max(0, int(day_cap)), max(0, int(month_cap)), budget_used, budget_adjust)
    if reservation.get("status") != "granted":
        reason = ("budget_denied" if reservation.get("status") == "denied"
                  else "control_unavailable")
        _complete(sb, call_key, owner, clock(), None, positive_ttl, reason,
                  neg_ttls[reason])
        if reason == "control_unavailable":
            log.error("paid budget reservation unavailable provider=%s", provider)
        return PaidFetchResult(source="denied", negative_reason=reason)

    payload = None
    reason = "upstream_error"
    charged = 0
    try:
        payload = fetch()
        reason = classify_payload(payload)
        # A provider request was attempted even for a miss/error.  The callback
        # supplies the contract-specific minimum charge.
        charged = max(0, int(actual_units(payload)))
        if charged > max(1, int(reserve_units)):
            log.critical(
                "paid provider charge exceeded reservation provider=%s "
                "reserved=%s actual=%s", provider, reserve_units, charged)
            reason = "reservation_overrun"
    except Exception:
        log.exception("paid upstream call failed provider=%s", provider)
        reason = "upstream_error"
        charged = max(0, int(actual_units(None)))
    finally:
        _reconcile(sb, idem, charged,
                   "completed" if reason is None else reason, budget_adjust)

    if reason:
        _complete(sb, call_key, owner, clock(), None, positive_ttl, reason,
                  neg_ttls.get(reason, neg_ttls["upstream_error"]))
        return PaidFetchResult(source="negative", negative_reason=reason,
                               actual_units=charged)
    _complete(sb, call_key, owner, clock(), payload, positive_ttl, None, 1)
    return PaidFetchResult(payload=payload, source="upstream", actual_units=charged)
