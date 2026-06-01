"""Structured JSON-line logger for Google Cloud Run Log Explorer.

Integration snippet (add to app.py near top):

    from observability.json_logger import StructuredLogger
    log = StructuredLogger("aerotax")

    # in a request handler:
    log.info("job_started", job_id=job_id, token_short=token[:8] + "...")
    log.warning("rate_limited", scope="wall_post", token_short=token[:8] + "...")
    log.error("supabase_down", op="select", table="wall_posts")

Output format (one JSON object per line on stdout):
    {"ts": "2026-06-01T10:21:33.412Z", "level": "INFO", "logger": "aerotax",
     "event": "job_started", "request_id": "...", "token_short": "abc12345...",
     "job_id": "..."}

Cloud Run automatically parses stdout JSON into structured log entries that
are filterable in Log Explorer (resource.labels.service_name=aerotax-backend,
jsonPayload.event="job_started", jsonPayload.level="ERROR").

PII guard:
* Tokens MUST be passed already-truncated (token[:8]+"..."). This logger
  does best-effort scrubbing on common key names but the caller is the
  source of truth.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional


# keys that look like full tokens or secrets -> auto-truncate / scrub
_SENSITIVE_KEY_FRAGMENTS = ("authorization", "secret", "api_key", "apikey",
                            "password", "passwd", "bearer", "cookie")
_TOKEN_KEY_FRAGMENTS = ("token", "session_id", "device_id")


def _scrub_value(key: str, value: Any) -> Any:
    """Best-effort PII guard for value entering a log line."""
    if not isinstance(value, str):
        return value
    lk = key.lower()
    if any(f in lk for f in _SENSITIVE_KEY_FRAGMENTS):
        return "[scrubbed]"
    # tokens: keep first 8 chars + suffix marker
    if any(f in lk for f in _TOKEN_KEY_FRAGMENTS) and not lk.endswith("_short"):
        if len(value) > 12:
            return value[:8] + "..."
    return value


def _iso_now() -> str:
    # Cloud Logging tolerates ISO8601 with milliseconds + 'Z'
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{int((time.time() % 1) * 1000):03d}Z"


class StructuredLogger:
    """JSON-line logger writing to stdout for Cloud Run / Render Log Explorer."""

    # Cloud Run Severity mapping
    _LEVEL_MAP = {
        "DEBUG": "DEBUG",
        "INFO": "INFO",
        "NOTICE": "NOTICE",
        "WARNING": "WARNING",
        "ERROR": "ERROR",
        "CRITICAL": "CRITICAL",
    }

    def __init__(self, logger_name: str = "aerotax",
                 default_request_id: Optional[str] = None):
        self.logger_name = logger_name
        self.default_request_id = default_request_id
        # honour ENV LOG_LEVEL filter (default INFO)
        env_level = (os.getenv("LOG_LEVEL", "INFO") or "INFO").upper()
        self._min_severity_rank = self._rank(env_level)

    @staticmethod
    def _rank(level: str) -> int:
        order = {"DEBUG": 10, "INFO": 20, "NOTICE": 25,
                 "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
        return order.get(level.upper(), 20)

    def _emit(self, level: str, event: str, kv: Dict[str, Any]) -> None:
        if self._rank(level) < self._min_severity_rank:
            return

        record: Dict[str, Any] = {
            "ts": _iso_now(),
            "severity": self._LEVEL_MAP.get(level, level),  # Cloud Run convention
            "level": level,                                  # human convention
            "logger": self.logger_name,
            "event": event,
        }

        # request_id: explicit > Flask g.request_id (best-effort) > default > new
        rid = kv.pop("request_id", None)
        if rid is None:
            try:
                from flask import g, has_request_context
                if has_request_context():
                    rid = getattr(g, "request_id", None)
            except Exception:
                rid = None
        if rid is None:
            rid = self.default_request_id or str(uuid.uuid4())[:8]
        record["request_id"] = rid

        # merge user-supplied k/v, with scrubbing
        for k, v in kv.items():
            try:
                record[k] = _scrub_value(k, v)
            except Exception:
                record[k] = "[unserializable]"

        try:
            line = json.dumps(record, default=str, ensure_ascii=False)
        except Exception:
            # last-resort: drop unserializable bits
            safe = {k: (str(v)[:500] if not isinstance(v, (int, float, bool))
                        else v) for k, v in record.items()}
            line = json.dumps(safe, ensure_ascii=False)

        # stdout for Cloud Run; flush so the line is visible immediately
        sys.stdout.write(line + "\n")
        try:
            sys.stdout.flush()
        except Exception:
            pass

    # public API
    def debug(self, event: str, **kv: Any) -> None:
        self._emit("DEBUG", event, kv)

    def info(self, event: str, **kv: Any) -> None:
        self._emit("INFO", event, kv)

    def notice(self, event: str, **kv: Any) -> None:
        self._emit("NOTICE", event, kv)

    def warning(self, event: str, **kv: Any) -> None:
        self._emit("WARNING", event, kv)

    def warn(self, event: str, **kv: Any) -> None:
        self.warning(event, **kv)

    def error(self, event: str, **kv: Any) -> None:
        self._emit("ERROR", event, kv)

    def critical(self, event: str, **kv: Any) -> None:
        self._emit("CRITICAL", event, kv)
