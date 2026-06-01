"""Rate-limit package: sliding-window per-token-per-scope counter on Supabase.

Integration snippet (add to a route handler in any blueprint):

    from rate_limits import check, record

    allowed, reset_in = check(token, "wall_post")
    if not allowed:
        return jsonify({"error": "rate_limited", "retry_after": reset_in}), 429
    # ... do the work ...
    record(token, "wall_post")

Scopes are defined in rate_limits/config.py. To add a new scope add an entry
there; do not hard-code limits in the route handler.

Table: public.rate_limit_buckets (see supabase_migrations/20260601_rate_limit_buckets.sql)
"""

from rate_limits.config import (
    SCOPES,
    BURST_WINDOW_SEC,
    BURST_MAX,
    check,
    record,
)

__all__ = ["SCOPES", "BURST_WINDOW_SEC", "BURST_MAX", "check", "record"]
