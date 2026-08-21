"""Deterministic Austrian sick-leave certificate calendar parser.

The Austrian ``Arbeitsunfähigkeitsmeldung`` is not an airline roster, but crew
members legitimately upload it as evidence for roster days marked sick.  Only
the explicitly printed inclusive absence period becomes calendar data. Names,
addresses, insurance numbers, doctors and diagnoses are deliberately ignored.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta


_HEADER_RE = re.compile(r"ARBEITSUNF(?:Ä|AE)HIGKEITSMELDUNG", re.IGNORECASE)
_FROM_RE = re.compile(
    r"Arbeitsunf(?:ä|ae)hig\s+von:\s*(\d{2}\.\d{2}\.\d{4})",
    re.IGNORECASE,
)
_TO_RE = re.compile(
    r"Letzter\s+Tag\s+der\s+Arbeitsunf(?:ä|ae)higkeit:\s*"
    r"(\d{2}\.\d{2}\.\d{4})",
    re.IGNORECASE,
)
_COLUMN_PERIOD_RE = re.compile(
    r"Arbeitsunf(?:ä|ae)hig\s+von:[^\n]*"
    r"Letzter\s+Tag\s+der\s+Arbeitsunf(?:ä|ae)higkeit:[^\n]*\n\s*"
    r"(\d{2}\.\d{2}\.\d{4})\s+(\d{2}\.\d{2}\.\d{4})(?:\s|$)",
    re.IGNORECASE,
)


def _printed_date(value):
    try:
        return datetime.strptime(value, "%d.%m.%Y").date()
    except (TypeError, ValueError):
        return None


def parse_at_sick_leave_calendar(extracted_text):
    """Return ``(events, year, month, report, error)`` for a verified notice.

    Each calendar day gets its own all-day ``Krank`` event.  This matches the
    roster model's day-level semantics and avoids retaining any medical detail
    beyond the fact and dates the user explicitly asked AeroX to import.
    """
    source = str(extracted_text or "")
    if not _HEADER_RE.search(source[:1000]):
        return None, None, None, None, "unsupported_pdf_format"
    # Require independent document markers so a random sentence containing
    # the title cannot become a sickness entry.
    lowered = source.casefold()
    if ("versicherungsträger" not in lowered
            or "ausstellungsdatum" not in lowered
            or "grund der arbeitsunfähigkeit" not in lowered):
        return None, None, None, None, "at_sick_leave_contract_missing"

    starts = _FROM_RE.findall(source)
    ends = _TO_RE.findall(source)
    # pdfplumber preserves the certificate's three-column reading order:
    # both labels share one extracted line and both values the next.  Keep a
    # strict two-label anchor so unrelated dates (for example the issue date)
    # can never be mistaken for the sick-leave period.
    if len(starts) != 1 or len(ends) != 1:
        pair = _COLUMN_PERIOD_RE.search(source)
        if pair:
            starts, ends = [pair.group(1)], [pair.group(2)]
    if len(starts) != 1 or len(ends) != 1:
        return None, None, None, None, "at_sick_leave_dates_missing"
    start = _printed_date(starts[0])
    end = _printed_date(ends[0])
    if start is None or end is None or end < start:
        return None, None, None, None, "at_sick_leave_invalid_period"
    if (end - start).days > 730:
        return None, None, None, None, "at_sick_leave_period_too_long"

    events = []
    current = start
    while current <= end:
        events.append((
            f"sick-{current:%Y%m%d}", current, current + timedelta(days=1),
            "Krank", True,
        ))
        current += timedelta(days=1)
    report = {
        "format": "at_sick_leave_certificate",
        "period": f"{start.isoformat()}..{end.isoformat()}",
        "event_count": len(events),
        "privacy": "dates_only",
    }
    return events, start.year, start.month, report, None
