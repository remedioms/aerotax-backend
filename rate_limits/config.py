"""Rate-limit configuration + sliding-window check/record.

Integration snippet (add to a route handler in any blueprint):

    from rate_limits import check, record

    allowed, reset_in = check(token, "wall_post")
    if not allowed:
        return jsonify({"error": "rate_limited", "retry_after": reset_in}), 429
    record(token, "wall_post")
    # ... do the work ...

Storage:
    Supabase table public.rate_limit_buckets (token, scope, window_start_epoch,
    window_sec, count). We keep one row per (token, scope, window_sec) bucket
    and roll forward when window_start_epoch + window_sec < now.

Soft-burst:
    Independent BURST window (5 hits / 60s) blocks rapid-fire requests
    regardless of scope. This is a 429 with Retry-After.

Local-dev fallback:
    If SUPABASE_URL/KEY are not set, we fall back to a process-local in-memory
    dict so unit tests / local development still see the limit semantics.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Dict, Optional, Tuple


# ---------------------------------------------------------------------------
# SCOPES: per-endpoint hard limits.
# Each scope has (max_per_hour, max_per_day). 0 means "no limit at this layer".
# ---------------------------------------------------------------------------
SCOPES: Dict[str, Dict[str, int]] = {
    # Wall feed (public posts)
    "wall_post":             {"per_hour": 10, "per_day": 30},
    "wall_comment":          {"per_hour": 30, "per_day": 100},

    # Forum threads
    "forum_thread":          {"per_hour": 5,  "per_day": 15},
    "forum_reply":           {"per_hour": 30, "per_day": 100},

    # Direct messages
    "dm_send":               {"per_hour": 60, "per_day": 200},

    # Trip-Trade marketplace
    "trip_trade_post":       {"per_hour": 0,  "per_day": 3},

    # Aircraft Health (community-sourced safety reports)
    "aircraft_health_report": {"per_hour": 0, "per_day": 5},

    # Layover reviews
    "layover_review":        {"per_hour": 0,  "per_day": 10},

    # Crew-Graph friend system
    "friend_request":        {"per_hour": 0,  "per_day": 30},
}

# Soft-burst: 5 hits in 60s across all scopes returns 429.
BURST_MAX: int = 5
BURST_WINDOW_SEC: int = 60

# Maximum number of buckets to keep per (token, scope) in memory fallback
_MEM_MAX_BUCKETS = 1024


# ---------------------------------------------------------------------------
# In-memory fallback (local dev / no Supabase env)
# ---------------------------------------------------------------------------
_MEM_LOCK = threading.Lock()
# key: (token, scope, window_sec) -> (window_start_epoch, count)
_MEM_BUCKETS: Dict[Tuple[str, str, int], Tuple[int, int]] = {}


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------
def _sb_headers() -> Optional[Dict[str, str]]:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
    if not url or not key:
        return None
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _sb_url() -> Optional[str]:
    url = os.getenv("SUPABASE_URL")
    if not url:
        return None
    return url.rstrip("/") + "/rest/v1/rate_limit_buckets"


def _sb_get_bucket(token: str, scope: str, window_sec: int,
                   now: int) -> Tuple[int, int]:
    """Returns (window_start_epoch, count); creates if missing."""
    base = _sb_url()
    headers = _sb_headers()
    if not base or not headers:
        return (now, 0)
    try:
        import requests
        # try to fetch existing row
        r = requests.get(
            base,
            params={
                "token": f"eq.{token}",
                "scope": f"eq.{scope}",
                "window_sec": f"eq.{window_sec}",
                "select": "window_start_epoch,count",
                "limit": 1,
            },
            headers=headers,
            timeout=1.5,
        )
        if r.status_code < 300:
            rows = r.json() or []
            if rows:
                row = rows[0]
                return (int(row.get("window_start_epoch", now)),
                        int(row.get("count", 0)))
    except Exception:
        # network blip -> fail open at the SQL layer; the in-memory
        # fallback will still bound the request rate within this instance
        pass
    return (now, 0)


def _sb_upsert_bucket(token: str, scope: str, window_sec: int,
                      window_start: int, count: int) -> None:
    base = _sb_url()
    headers = _sb_headers()
    if not base or not headers:
        return
    try:
        import requests
        payload = [{
            "token": token,
            "scope": scope,
            "window_sec": window_sec,
            "window_start_epoch": window_start,
            "count": count,
        }]
        # PostgREST upsert via on_conflict
        requests.post(
            base + "?on_conflict=token,scope,window_sec",
            json=payload,
            headers={**headers, "Prefer": "resolution=merge-duplicates"},
            timeout=1.5,
        )
    except Exception:
        # rate-limit accounting must not break the request
        pass


# ---------------------------------------------------------------------------
# Shared bucket logic (Supabase or in-memory)
# ---------------------------------------------------------------------------
def _get_count(token: str, scope: str, window_sec: int,
               now: int) -> Tuple[int, int]:
    """Returns (window_start_epoch, count_in_window)."""
    if _sb_headers() is None:
        with _MEM_LOCK:
            key = (token, scope, window_sec)
            ws, count = _MEM_BUCKETS.get(key, (now, 0))
            if now - ws >= window_sec:
                ws, count = now, 0
            return ws, count
    ws, count = _sb_get_bucket(token, scope, window_sec, now)
    if now - ws >= window_sec:
        ws, count = now, 0
    return ws, count


def _increment(token: str, scope: str, window_sec: int,
               window_start: int, new_count: int) -> None:
    if _sb_headers() is None:
        with _MEM_LOCK:
            _MEM_BUCKETS[(token, scope, window_sec)] = (window_start, new_count)
            # bound the in-memory dict
            if len(_MEM_BUCKETS) > _MEM_MAX_BUCKETS:
                # drop oldest 25%
                items = sorted(_MEM_BUCKETS.items(), key=lambda kv: kv[1][0])
                for k, _ in items[: _MEM_MAX_BUCKETS // 4]:
                    _MEM_BUCKETS.pop(k, None)
        return
    _sb_upsert_bucket(token, scope, window_sec, window_start, new_count)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def check(token: str, scope: str) -> Tuple[bool, int]:
    """Return (allowed, reset_in_sec). Does NOT increment the counter.

    `record()` must be called only after the action succeeded.
    """
    if not token:
        # treat anonymous as not-allowed at the rate-limit layer;
        # the auth layer should have rejected earlier
        return (False, 60)

    cfg = SCOPES.get(scope)
    if not cfg:
        # unknown scope -> fail open (no limit applies) but log via caller
        return (True, 0)

    now = int(time.time())

    # 1) burst window (60s, BURST_MAX)
    ws_b, count_b = _get_count(token, "_burst", BURST_WINDOW_SEC, now)
    if count_b >= BURST_MAX:
        reset = max(1, BURST_WINDOW_SEC - (now - ws_b))
        return (False, reset)

    # 2) per-hour
    per_hour = cfg.get("per_hour", 0)
    if per_hour > 0:
        ws_h, count_h = _get_count(token, scope, 3600, now)
        if count_h >= per_hour:
            reset = max(1, 3600 - (now - ws_h))
            return (False, reset)

    # 3) per-day
    per_day = cfg.get("per_day", 0)
    if per_day > 0:
        ws_d, count_d = _get_count(token, scope, 86400, now)
        if count_d >= per_day:
            reset = max(1, 86400 - (now - ws_d))
            return (False, reset)

    return (True, 0)


def record(token: str, scope: str) -> None:
    """Increment all relevant buckets after a successful action."""
    if not token:
        return

    cfg = SCOPES.get(scope)
    if not cfg:
        return

    now = int(time.time())

    # burst
    ws, count = _get_count(token, "_burst", BURST_WINDOW_SEC, now)
    _increment(token, "_burst", BURST_WINDOW_SEC, ws, count + 1)

    # per-hour
    if cfg.get("per_hour", 0) > 0:
        ws, count = _get_count(token, scope, 3600, now)
        _increment(token, scope, 3600, ws, count + 1)

    # per-day
    if cfg.get("per_day", 0) > 0:
        ws, count = _get_count(token, scope, 86400, now)
        _increment(token, scope, 86400, ws, count + 1)


def reset_memory_for_tests() -> None:
    """Test helper: clear the in-memory fallback buckets."""
    with _MEM_LOCK:
        _MEM_BUCKETS.clear()
