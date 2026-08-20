"""Deterministic Virgin Atlantic flight-based roster calendar parser."""

from __future__ import annotations

import io
import re
from datetime import date, datetime, timedelta

import pdfplumber


_FORMAT_MARKER = "Roster Report (flight based)"
_PERIOD_RE = re.compile(
    r"Planning Period\s+(\d{2})\s*-\s*(\d{2})([A-Za-z]{3})(\d{4})"
)
_FLIGHT_RE = re.compile(
    r"^\s*(?P<day>\d{1,2})\s+VS\s+(?P<number>\d{1,4})\s+"
    r"(?P<dep>[A-Z]{3})\s+(?P<arr>[A-Z]{3})\s+"
    r"\((?P<dep_local>\d{2}:\d{2})\)(?P<dep_utc>\d{2}:\d{2})\s+"
    r"\((?P<arr_local>\d{2}:\d{2})\)(?P<arr_utc>\d{2}:\d{2})\s+"
    r"(?P<block>\d{2}:\d{2})\b"
)
_BRIEFING_RE = re.compile(
    r"\bBriefing\s+\((?P<local>\d{2}:\d{2})\)(?P<utc>\d{2}:\d{2})\b"
)
_TOTAL_RE = re.compile(r"\bFlight duty this month\s+(\d{1,3}):(\d{2})\b")
_OFF_TOTAL_RE = re.compile(r"\bDays off this month\s+(\d+)\b")
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _clock(day, value):
    return datetime(day.year, day.month, day.day,
                    int(value[:2]), int(value[3:]))


def _duration(value):
    return int(value[:2]) * 60 + int(value[3:])


def _period(source):
    match = _PERIOD_RE.search(source or "")
    if not match:
        return None, None
    first, last, month_name, year = match.groups()
    try:
        month = _MONTHS[month_name.upper()]
        return (date(int(year), month, int(first)),
                date(int(year), month, int(last)))
    except (KeyError, ValueError):
        return None, None


def _center(word):
    return (float(word.get("x0") or 0) + float(word.get("x1") or 0)) / 2


def _overview_markers(pdf_bytes, start, end):
    """Read only the day-number row and LVE/RDO tokens from page one."""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            if not pdf.pages:
                return None, "pdf_extract_failed"
            words = pdf.pages[0].extract_words(
                x_tolerance=2, y_tolerance=2, keep_blank_chars=False,
                use_text_flow=False,
            ) or []
    except Exception:
        return None, "pdf_extract_failed"

    numeric = [word for word in words
               if re.fullmatch(r"(?:[1-9]|[12]\d|3[01])",
                               str(word.get("text") or ""))]
    rows = []
    for word in sorted(numeric, key=lambda item: float(item.get("top") or 0)):
        top = float(word.get("top") or 0)
        if not rows or abs(top - rows[-1]["top"]) > 2.0:
            rows.append({"top": top, "words": [word]})
        else:
            rows[-1]["words"].append(word)
    day_rows = [row for row in rows if len(row["words"]) >= (end - start).days + 1]
    if not day_rows:
        return None, "virgin_overview_date_mismatch"
    row = max(day_rows, key=lambda item: len(item["words"]))
    anchors = sorted(row["words"], key=lambda word: float(word["x0"]))

    dates = []
    current = start
    for anchor in anchors:
        if current > end:
            break
        if int(anchor["text"]) != current.day:
            return None, "virgin_overview_date_mismatch"
        dates.append((current, anchor))
        current += timedelta(days=1)
    if current <= end:
        return None, "virgin_overview_date_mismatch"

    markers = []
    for day, anchor in dates:
        matches = [
            word for word in words
            if 6 < float(word.get("top") or 0) - row["top"] < 24
            and abs(_center(word) - _center(anchor)) <= 8
            and str(word.get("text") or "").upper() in ("LVE", "RDO")
        ]
        if len(matches) > 1:
            return None, "virgin_ambiguous_day_status"
        if matches:
            markers.append((day, str(matches[0]["text"]).upper()))
    return markers, None


def parse_virgin_atlantic_calendar(pdf_bytes, extracted_text=""):
    """Return ``(events, year, month, report, error)`` for a VS roster."""
    source = str(extracted_text or "")
    if (_FORMAT_MARKER not in source[:1000]
            or "Rostering_Cabin" not in source[:1000]
            or "FLIGHT DETAILS" not in source):
        return None, None, None, None, "unsupported_pdf_format"
    start, end = _period(source)
    if start is None or end is None or end < start:
        return None, None, None, None, "virgin_invalid_period"

    markers, error = _overview_markers(pdf_bytes, start, end)
    if error:
        return None, None, None, None, error
    off_total = _OFF_TOTAL_RE.search(source)
    if not off_total:
        return None, None, None, None, "virgin_missing_ground_checksum"
    if len(markers) != int(off_total.group(1)):
        return None, None, None, None, "virgin_ground_checksum_mismatch"

    legs = []
    pending_briefing = None
    for raw in source.splitlines():
        line = " ".join(raw.split())
        briefing = _BRIEFING_RE.search(line)
        if briefing:
            pending_briefing = briefing.groupdict()
            continue
        flight = _FLIGHT_RE.match(line)
        if not flight:
            continue
        if pending_briefing is None:
            return None, None, None, None, "virgin_missing_briefing"
        values = flight.groupdict()
        try:
            day = date(start.year, start.month, int(values["day"]))
        except ValueError:
            return None, None, None, None, "virgin_invalid_flight_date"
        if not start <= day <= end:
            return None, None, None, None, "virgin_invalid_flight_date"
        departure = _clock(day, values["dep_utc"])
        arrival = _clock(day, values["arr_utc"])
        if arrival <= departure:
            arrival += timedelta(days=1)
        parsed_block = int((arrival - departure).total_seconds() // 60)
        if parsed_block != _duration(values["block"]):
            return None, None, None, None, "virgin_leg_block_mismatch"
        values.update({
            "date": day, "departure": departure, "arrival": arrival,
            "block_minutes": parsed_block,
            "briefing_local": pending_briefing["local"],
        })
        legs.append(values)
        pending_briefing = None
    if not legs:
        return None, None, None, None, "no_roster_days"

    total = _TOTAL_RE.search(source)
    if not total:
        return None, None, None, None, "virgin_missing_flight_checksum"
    expected_block = int(total.group(1)) * 60 + int(total.group(2))
    parsed_block = sum(int(leg["block_minutes"]) for leg in legs)
    if parsed_block != expected_block:
        return None, None, None, None, "virgin_flight_checksum_mismatch"

    events = []
    for day, code in markers:
        summary = "Urlaub" if code == "LVE" else "Off Day"
        events.append((f"{code.lower()}-{day:%Y%m%d}", day,
                       day + timedelta(days=1), summary, True))
    for index, leg in enumerate(legs, 1):
        flight = f"VS{int(leg['number'])}"
        base = f"{flight} {leg['dep']} - {leg['arr']}"
        summary = (f"{leg['briefing_local']} LT Briefing {leg['dep']} "
                   f"· {base}")
        events.append((f"leg-{leg['date']:%Y%m%d}-{flight}-{index}",
                       leg["departure"], leg["arrival"], summary, False))
    events.sort(key=lambda event: event[1].isoformat())
    return events, start.year, start.month, {
        "format": "virgin_atlantic_flight_based_roster",
        "period": f"{start.isoformat()}..{end.isoformat()}",
        "timescale": "UTC",
        "flight_count": len(legs),
        "marker_count": len(markers),
        "block_minutes": parsed_block,
    }, None
