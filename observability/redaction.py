"""Central secret redaction for request paths, URLs and observability payloads.

Legacy AeroX clients still place their long-lived ``AT-...`` credential in
some URL paths.  Until those routes have migrated to header-only credentials,
*no* logging or Sentry integration may copy the raw path.  Keep that rule in
one small dependency-free module so request instrumentation, structured logs
and Sentry cannot drift apart.
"""

from __future__ import annotations

import re
import logging
from typing import Any


REDACTED = "[redacted]"
REDACTED_TOKEN = "[redacted-token]"

# AT credentials currently contain letters, digits, '_' and '-'. Four payload
# characters also catches the historical `token[:8]` log prefix ("AT-" plus
# five characters), while still leaving documentation literals such as
# "AT-..." alone.
_AT_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])AT-[A-Za-z0-9_-]{4,}")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_SENSITIVE_QUERY_RE = re.compile(
    r"(?i)([?&](?:access_token|auth_token|authorization|bearer|token|"
    r"session(?:_token|_id)?|refresh_token|reset_token|secret|api[_-]?key)"
    r"=)[^&#\s]*"
)

_SECRET_KEY_FRAGMENTS = (
    "authorization", "bearer", "cookie", "secret", "password", "passwd",
    "api_key", "apikey", "access_token", "refresh_token", "reset_token",
    "session_token", "auth_token",
)
_URL_KEY_FRAGMENTS = ("url", "uri", "path", "query_string")
_TOKEN_KEY_FRAGMENTS = ("token", "session_id", "device_id")


def redact_text(value: Any) -> Any:
    """Return ``value`` with credential shapes removed if it is a string."""
    if not isinstance(value, str):
        return value
    value = _BEARER_RE.sub("Bearer " + REDACTED_TOKEN, value)
    value = _SENSITIVE_QUERY_RE.sub(lambda m: m.group(1) + REDACTED, value)
    return _AT_TOKEN_RE.sub(REDACTED_TOKEN, value)


def redact_url(value: Any) -> Any:
    """Alias documenting that a path/URL crosses an observability boundary."""
    return redact_text(value)


def redact_value(key: Any, value: Any) -> Any:
    """Redact a key/value pair recursively without mutating the input."""
    key_lower = str(key or "").lower()
    if any(fragment in key_lower for fragment in _SECRET_KEY_FRAGMENTS):
        return REDACTED
    if any(fragment in key_lower for fragment in _TOKEN_KEY_FRAGMENTS):
        return REDACTED_TOKEN if value not in (None, "") else value
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, list):
        return [redact_value("", item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value("", item) for item in value)
    # All strings get pattern redaction.  URL/path keys are called out here for
    # readability and so future URL-specific rules have one obvious home.
    if any(fragment in key_lower for fragment in _URL_KEY_FRAGMENTS):
        return redact_url(value)
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    # Unknown objects may expose credentials through __str__/__repr__. Never
    # hand the original object to a downstream serializer or logger.
    return "[redacted-object]"


def redact_mapping(value: Any) -> Any:
    """Deep-copy and redact a dict/list observability payload."""
    if isinstance(value, dict):
        return {key: redact_value(key, item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value("", item) for item in value]
    return redact_value("", value)


class RedactingLogFilter(logging.Filter):
    """Render then redact a LogRecord before any handler emits it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
            record.msg = redact_text(rendered)
            record.args = ()
        except Exception:
            # Fail CLOSED: if rendering or redaction breaks, emit a neutral
            # record rather than the original potentially secret-bearing one.
            record.msg = "[redacted-log-record]"
            record.args = ()
        return True


def install_logging_redaction(*loggers: logging.Logger) -> RedactingLogFilter:
    """Attach one redactor to loggers and all handlers they currently own."""
    redactor = RedactingLogFilter()
    for logger in loggers:
        if logger is None:
            continue
        logger.addFilter(redactor)
        for handler in logger.handlers:
            handler.addFilter(redactor)
    return redactor
