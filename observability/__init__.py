"""Observability package: Sentry hook + structured JSON logging.

Integration snippet (add to app.py near top, after env-var load):

    import os
    from observability.sentry_setup import init_sentry
    from observability.json_logger import StructuredLogger

    init_sentry(
        dsn=os.getenv("SENTRY_DSN"),
        environment=os.getenv("CLOUD_RUN_REVISION", "dev"),
    )
    log = StructuredLogger("aerotax")

All functions are safe no-ops when env-vars are missing (local dev).
"""

from observability.sentry_setup import init_sentry, capture_exception
from observability.json_logger import StructuredLogger

__all__ = ["init_sentry", "capture_exception", "StructuredLogger"]
