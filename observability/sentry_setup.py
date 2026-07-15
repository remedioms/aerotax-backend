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
* before_send and before_send_transaction deep-scrub headers, query secrets,
  long-lived path credentials, breadcrumbs and exception/message text.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, Optional

from observability.redaction import redact_mapping, redact_text, redact_value


_INITIALIZED: bool = False
_SENTRY: Any = None  # holds the imported sentry_sdk module if loaded


def _strip_sensitive(event: Dict[str, Any], _hint: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Sentry before_send hook: deep-scrub secrets, URLs and path tokens."""
    try:
        return redact_mapping(event)
    except Exception:
        # Fail CLOSED: a broken scrubber must drop observability, never send the
        # original potentially credential-bearing payload.
        return None


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
            before_send_transaction=_strip_sensitive,
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

    Tags must contain only short, low-cardinality values. Secret-like tag keys
    and credential shapes are centrally scrubbed even if a caller forgets.
    """
    if not _INITIALIZED or _SENTRY is None:
        return
    try:
        with _SENTRY.push_scope() as scope:
            for k, v in (tags or {}).items():
                try:
                    scope.set_tag(k, str(redact_value(k, v))[:200])
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
                    scope.set_tag(k, str(redact_value(k, v))[:200])
                except Exception:
                    pass
            _SENTRY.capture_message(redact_text(msg), level=level)
    except Exception:
        pass


def is_initialized() -> bool:
    return _INITIALIZED
