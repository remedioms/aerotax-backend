"""Deterministic Lufthansa City NetLine/Crew ground-duty calendar parser.

The LHX ``Individual duty plan`` is a coordinate-based, three-column report.
This parser deliberately reads only the dated duty rows and the printed duty/
off-day checksums.  Names, comments and qualification appendices are ignored.

The currently verified LHX variant contains ground duties only.  A document
with flight time is rejected instead of treating a flight row like training.
"""

from __future__ import annotations

import io
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pdfplumber


_FORMAT_MARKER = "NetLine/Crew(LHX)"
_PERIOD_RE = re.compile(
    r"Period:\s*(\d{2})([A-Za-z]{3})(\d{2})\s*-\s*"
    r"(\d{2})([A-Za-z]{3})(\d{2})"
)
_DAY_RE = re.compile(r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun)(\d{2})")
_DUTY_TIME_RE = re.compile(r"\bDuty time\s+(\d{1,3}):(\d{2})\b")
_FLIGHT_TIME_RE = re.compile(r"\bFlight time\s+(\d{1,3}):(\d{2})\b")
_OFF_DAYS_RE = re.compile(r"\bOff days\s+(\d{1,2})\b")
_CLOCK_RE = re.compile(r"\d{4}")
_DUTY_RE = re.compile(r"[A-Z][A-Z0-9]{0,9}")
_STATION_RE = re.compile(r"[A-Z]{3}")
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_GERMAN_TZ = ZoneInfo("Europe/Berlin")


def _period(source):
    match = _PERIOD_RE.search(source or "")
    if not match:
        return None, None
    d1, m1, y1, d2, m2, y2 = match.groups()
    try:
        return (
            date(2000 + int(y1), _MONTHS[m1.upper()], int(d1)),
            date(2000 + int(y2), _MONTHS[m2.upper()], int(d2)),
        )
    except (KeyError, ValueError):
        return None, None


def _day_map(start, end):
    result = {}
    current = start
    while current <= end:
        token = f"{_WEEKDAYS[current.weekday()]}{current.day:02d}"
        if token in result:
            return None
        result[token] = current
        current += timedelta(days=1)
    return result


def _same_row(words, anchor, page_width):
    column_width = page_width / 3
    column = min(2, max(0, int(float(anchor["x0"]) / column_width)))
    left, right = column * column_width, (column + 1) * column_width
    return sorted(
        (word for word in words
         if left <= float(word["x0"]) < right
         and abs(float(word["top"]) - float(anchor["top"])) <= 2.5),
        key=lambda word: float(word["x0"]),
    )


def _duty_rows(page, day_map):
    words = page.extract_words(
        x_tolerance=1, y_tolerance=2, keep_blank_chars=False,
        use_text_flow=False,
    ) or []
    rows = {}
    for anchor in words:
        token = str(anchor.get("text") or "")
        # The month overview repeats every date horizontally above the detailed
        # table.  Only the lower three-column schedule is authoritative here.
        if float(anchor.get("top") or 0) < 120 or token not in day_map:
            continue
        if token in rows:
            return None, "lhx_duplicate_duty_day"
        values = [str(word.get("text") or "")
                  for word in _same_row(words, anchor, float(page.width))]
        try:
            index = values.index(token)
        except ValueError:
            return None, "lhx_unparsed_duty_day"
        values = values[index:]
        if len(values) < 3:
            return None, "lhx_unparsed_duty_day"
        duty, station = values[1], values[2]
        if not _DUTY_RE.fullmatch(duty) or not _STATION_RE.fullmatch(station):
            return None, "lhx_unparsed_duty_day"
        clocks = [value for value in values[3:] if _CLOCK_RE.fullmatch(value)]
        if duty == "O":
            if clocks:
                return None, "lhx_unexpected_off_times"
            rows[token] = {"duty": duty, "station": station,
                           "start": None, "end": None}
        else:
            if len(clocks) < 2:
                return None, "lhx_missing_duty_times"
            rows[token] = {"duty": duty, "station": station,
                           "start": clocks[0], "end": clocks[1]}
    if set(rows) != set(day_map):
        return None, "lhx_duty_date_mismatch"
    return rows, None


def _local_to_utc(day, hhmm):
    local = datetime(day.year, day.month, day.day,
                     int(hhmm[:2]), int(hhmm[2:]), tzinfo=_GERMAN_TZ)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def _local_duration(start, end):
    start_minutes = int(start[:2]) * 60 + int(start[2:])
    end_minutes = int(end[:2]) * 60 + int(end[2:])
    if end_minutes <= start_minutes:
        end_minutes += 24 * 60
    return end_minutes - start_minutes


def parse_lhx_netline_calendar(pdf_bytes, extracted_text=""):
    """Return ``(events, year, month, report, error)`` for an LHX plan."""
    source = str(extracted_text or "")
    if (_FORMAT_MARKER not in source[:1500]
            or "Individual duty plan" not in source[:500]):
        return None, None, None, None, "unsupported_pdf_format"
    start, end = _period(source)
    if start is None or end is None or end < start or (end - start).days > 62:
        return None, None, None, None, "lhx_invalid_period"
    day_map = _day_map(start, end)
    if day_map is None:
        return None, None, None, None, "lhx_ambiguous_period"

    flight_checksum = _FLIGHT_TIME_RE.search(source)
    if not flight_checksum:
        return None, None, None, None, "lhx_missing_flight_checksum"
    if int(flight_checksum.group(1)) * 60 + int(flight_checksum.group(2)):
        return None, None, None, None, "lhx_flight_plan_not_supported"

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            if len(pdf.pages) != 1:
                return None, None, None, None, "lhx_unexpected_page_count"
            rows, error = _duty_rows(pdf.pages[0], day_map)
    except Exception:
        return None, None, None, None, "pdf_extract_failed"
    if error:
        return None, None, None, None, error

    duty_checksum = _DUTY_TIME_RE.search(source)
    off_checksum = _OFF_DAYS_RE.search(source)
    if not duty_checksum or not off_checksum:
        return None, None, None, None, "lhx_missing_month_checksum"
    expected_duty = int(duty_checksum.group(1)) * 60 + int(duty_checksum.group(2))
    expected_off = int(off_checksum.group(1))
    parsed_off = sum(1 for row in rows.values() if row["duty"] == "O")
    parsed_duty = sum(
        _local_duration(row["start"], row["end"])
        for row in rows.values() if row["duty"] != "O"
    )
    if parsed_off != expected_off:
        return None, None, None, None, "lhx_off_checksum_mismatch"
    if parsed_duty != expected_duty:
        return None, None, None, None, "lhx_duty_checksum_mismatch"

    events = []
    for token, day in sorted(day_map.items(), key=lambda item: item[1]):
        row = rows[token]
        if row["duty"] == "O":
            events.append((f"off-{day:%Y%m%d}", day,
                           day + timedelta(days=1), "Off Day", True))
            continue
        duty_start = _local_to_utc(day, row["start"])
        duty_end = _local_to_utc(day, row["end"])
        if duty_end <= duty_start:
            duty_end += timedelta(days=1)
        events.append((f"duty-{day:%Y%m%d}-{row['duty']}",
                       duty_start, duty_end,
                       f"{row['duty']} · {row['station']}", False))

    return events, start.year, start.month, {
        "format": "lhx_netline_ground_duty_plan",
        "period": f"{start.isoformat()}..{end.isoformat()}",
        "timescale": "Europe/Berlin",
        "event_count": len(events),
        "duty_count": len(events) - parsed_off,
        "off_count": parsed_off,
        "duty_minutes": parsed_duty,
    }, None
