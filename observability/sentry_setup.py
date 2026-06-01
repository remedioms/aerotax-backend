"""Sentry initialization for the Flask backend.

Integration snippet (add to app.py near top, after env-var load):

    import os
    from observability.sentry_setup import init_sentry, capture_exception

    init_sentry(
        dsn=os.getenv("SENTRY_DSN"),
        environment=os.getenv("CLOUD_RUN_REVISION", "dev"),
    )

    # later, inside an except-block:
    # capture_exception(exc, tags={"endpoint": "/api/job/start", "token_short": tok[:8]})

Behavior:
* SENTRY_DSN env-var missing -> no-op (safe in local dev / tests).
* sentry-sdk import failure -> no-op + stderr warning (no crash).
* Once initialized, capture_exception forwards to Sentry with extra tags
  and never raises -- failures in observability must not break the request.

PII guard:
* Never pass full tokens via tags; callers should always pre-truncate
  (e.g. token[:8]+"...").
* before_send strips Authorization headers and cookies from event payloads.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, Optional


_INITIALIZED: bool = False
_SENTRY: Any = None  # holds the imported sentry_sdk module if loaded


def _strip_sensitive(event: Dict[str, Any], _hint: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Sentry before_send hook: scrubs auth headers and cookie values."""
    try:
        request = event.get("request") or {}
        headers = request.get("headers") or {}
        for key in list(headers.keys()):
            lk = key.lower()
            if lk in ("authorization", "cookie", "x-api-key", "x-auth-token"):
                headers[key] = "[scrubbed]"
        # also scrub from breadcrumbs
        for crumb in event.get("breadcrumbs", {}).get("values", []):
            data = crumb.get("data") or {}
            for k in list(data.keys()):
                if "token" in k.lower() or "auth" in k.lower() or "secret" in k.lower():
                    data[k] = "[scrubbed]"
    except Exception:
        # never let scrubbing break the event
        pass
    return event


def init_sentry(dsn: Optional[str], environment: str = "dev") -> bool:
    """Initialize Sentry SDK if DSN is set.

    Returns True if initialized, False otherwise (missing DSN or import failure).
    Safe to call multiple times -- only the first successful call initializes.
    """
    global _INITIALIZED, _SENTRY

    if _INITIALIZED:
        return True

    if not dsn:
        # no-op path: keep silent, this is the local-dev default
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError as e:
        sys.stderr.write(
            f"[sentry_setup] sentry-sdk not importable ({e!s}); skipping init\n"
        )
        return False

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=environment,
            integrations=[
                FlaskIntegration(),
                LoggingIntegration(level=None, event_level=None),
            ],
            # conservative defaults for a Flask backend serving ~5k users
            traces_sample_rate=0.05,   # 5% perf-trace sampling
            profiles_sample_rate=0.0,  # off until we baseline cost
            send_default_pii=False,
            attach_stacktrace=True,
            before_send=_strip_sensitive,
            release=os.getenv("CLOUD_RUN_REVISION") or os.getenv("GIT_COMMIT_SHA"),
            max_breadcrumbs=50,
        )
        _SENTRY = sentry_sdk
        _INITIALIZED = True
        return True
    except Exception as e:
        sys.stderr.write(f"[sentry_setup] init failed: {e!s}\n")
        return False


def capture_exception(exc: BaseException, tags: Optional[Dict[str, str]] = None) -> None:
    """Forward an exception to Sentry; no-op if not initialized.

    Tags must contain only short, low-cardinality values. Callers MUST
    pre-truncate any token-like value (token[:8]+"...") -- this function
    does not scrub tag values.
    """
    if not _INITIALIZED or _SENTRY is None:
        return
    try:
        with _SENTRY.push_scope() as scope:
            for k, v in (tags or {}).items():
                try:
                    scope.set_tag(k, str(v)[:200])
                except Exception:
                    pass
            _SENTRY.capture_exception(exc)
    except Exception:
        # observability must never crash the caller
        pass


def capture_message(msg: str, level: str = "info", tags: Optional[Dict[str, str]] = None) -> None:
    """Forward a manual message to Sentry; no-op if not initialized."""
    if not _INITIALIZED or _SENTRY is None:
        return
    try:
        with _SENTRY.push_scope() as scope:
            for k, v in (tags or {}).items():
                try:
                    scope.set_tag(k, str(v)[:200])
                except Exception:
                    pass
            _SENTRY.capture_message(msg, level=level)
    except Exception:
        pass


def is_initialized() -> bool:
    return _INITIALIZED
