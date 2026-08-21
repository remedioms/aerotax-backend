"""Pure helpers for roster markers shared by parser and live-state consumers."""

import re


_CANCELLED_STANDBY_RE = re.compile(
    r'^(?:(?:STANDBY|OFF\s+DAY)\s*)?(?:\(\s*)?SCU(?:\s*\))?$',
    re.IGNORECASE,
)


def is_cancelled_standby_marker(value):
    """True only for LH's cancelled-standby marker SCU.

    The whole-summary match is intentional: ``SCU`` is also an airport code,
    so a real route such as ``FRA-SCU`` must never become a free day.
    """
    text = ' '.join(str(value or '').split())
    return bool(text and _CANCELLED_STANDBY_RE.fullmatch(text))
