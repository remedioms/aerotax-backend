"""Deterministic Lufthansa City cabin-training schedule PDF parser.

The LHX ``GC Initial, OCC und TYPE`` course plan is a landscape weekly
matrix, not a normal CrewAccess roster.  Dates are printed in the weekday
headers; weekend dates (and, in one verified template, the first Monday) are
omitted but are fixed by the other dated columns in the same seven-day row.

Only the compact header area of each day column is read.  Detailed course
contents, trainers and dress-code notes deliberately never enter calendar
storage.  The full date strip, printed weekdays, activity, time range and
location/category rows form the deterministic acceptance contract.
"""

from __future__ import annotations

import io
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pdfplumber


_TITLE_MARKER = "Übersicht GC Initial, OCC und TYPE Cabin Crews"
_COURSE_MARKER = "Kursplan LHX"
_DATE_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")
_TIME_RANGE_RE = re.compile(
    r"\b([0-2]?\d):([0-5]\d)\s*[\-–—]\s*"
    r"([0-2]?\d):([0-5]\d)\b"
)
_WEEKDAYS = {
    "Montag": 0,
    "Dienstag": 1,
    "Mittwoch": 2,
    "Donnerstag": 3,
    "Freitag": 4,
    "Samstag": 5,
    "Sonntag": 6,
}
_GERMAN_TZ = ZoneInfo("Europe/Berlin")


def _clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _word_lines(words):
    """Group words into visual lines while retaining bold information."""
    ordered = sorted(words, key=lambda word: (
        float(word.get("top") or 0), float(word.get("x0") or 0)))
    groups = []
    for word in ordered:
        top = float(word.get("top") or 0)
        if not groups or abs(top - groups[-1][0]) > 2.5:
            groups.append([top, []])
        groups[-1][1].append(word)
    result = []
    for top, line_words in groups:
        line_words.sort(key=lambda word: float(word.get("x0") or 0))
        text = _clean_text(" ".join(str(word.get("text") or "")
                                    for word in line_words))
        if not text:
            continue
        bold = any("bold" in str(word.get("fontname") or "").lower()
                   for word in line_words)
        result.append({"top": top, "text": text, "bold": bold})
    return result


def _weekday_anchors(words):
    candidates = []
    for word in words:
        raw = str(word.get("text") or "")
        match = re.match(
            r"^(Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag)"
            r"(?:,|$)", raw)
        token = match.group(1) if match else None
        if token and float(word.get("top") or 0) < 180:
            candidates.append((float(word["top"]), float(word["x0"]), token))
    if not candidates:
        return None
    # The schedule header is the only visual row with at least five weekday
    # labels.  Group by baseline to avoid matching words in course prose.
    for seed_top in sorted({round(item[0], 1) for item in candidates}):
        row = [item for item in candidates if abs(item[0] - seed_top) <= 2.5]
        unique = {}
        for top, x0, token in row:
            unique.setdefault(token, (top, x0, token))
        row = sorted(unique.values(), key=lambda item: item[1])
        if len(row) < 5:
            continue
        indexes = [_WEEKDAYS[item[2]] for item in row]
        if indexes != list(range(len(indexes))):
            continue
        return row
    return None


def _column_bounds(anchors, page_width):
    xs = [item[1] for item in anchors]
    gaps = sorted(xs[index + 1] - xs[index]
                  for index in range(len(xs) - 1))
    typical_gap = gaps[len(gaps) // 2] if gaps else float(page_width)
    bounds = []
    for index, x0 in enumerate(xs):
        # Every weekday token starts at the left padding of its table column.
        # Midpoints would cut wide cells in half and mix alternating columns.
        left = max(0.0, x0 - 6.0)
        right = ((xs[index + 1] - 6.0) if index < len(xs) - 1
                 else min(float(page_width), x0 + typical_gap - 6.0))
        bounds.append((left, right))
    return bounds


def _parse_date_token(text):
    match = _DATE_RE.search(text or "")
    if not match:
        return None
    try:
        return date(int(match.group(3)), int(match.group(2)),
                    int(match.group(1)))
    except ValueError:
        return None


def _page_days(words, anchors, page_width):
    bounds = _column_bounds(anchors, page_width)
    explicit = []
    for index, (top, _x0, _weekday) in enumerate(anchors):
        left, right = bounds[index]
        header = _clean_text(" ".join(
            str(word.get("text") or "") for word in sorted(
                (word for word in words
                 if left <= float(word.get("x0") or 0) < right
                 and abs(float(word.get("top") or 0) - top) <= 3.0),
                key=lambda word: float(word.get("x0") or 0))))
        parsed = _parse_date_token(header)
        if parsed:
            explicit.append((index, parsed))
    if not explicit:
        return None, bounds, "lhx_training_missing_week_date"
    monday_candidates = {parsed - timedelta(days=index)
                         for index, parsed in explicit}
    if len(monday_candidates) != 1:
        return None, bounds, "lhx_training_conflicting_week_dates"
    monday = monday_candidates.pop()
    days = []
    for index, (_top, _x0, weekday) in enumerate(anchors):
        day = monday + timedelta(days=index)
        if day.weekday() != _WEEKDAYS[weekday]:
            return None, bounds, "lhx_training_weekday_mismatch"
        days.append(day)
    return days, bounds, None


def _strip_time(text):
    text = _TIME_RANGE_RE.sub("", text or "")
    text = re.sub(r"\bUhr\b", "", text, flags=re.IGNORECASE)
    return _clean_text(text).strip(" /-")


def _cell_contract(words, left, right, header_top):
    region = [word for word in words
              if left <= float(word.get("x0") or 0) < right
              and header_top + 5 <= float(word.get("top") or 0)
              <= header_top + 105]
    lines = _word_lines(region)
    if not lines:
        return None, "lhx_training_empty_day"

    time_match = None
    for line in lines:
        match = _TIME_RANGE_RE.search(line["text"])
        if match:
            time_match = match
            break

    location_index = next((index for index, line in enumerate(lines)
                           if line["text"].startswith("MUC ")
                           or line["text"] == "Attrappe"), None)
    activity_limit = (lines[location_index]["top"] if location_index is not None
                      else header_top + 50)
    activity_parts = []
    for line in lines:
        if line["top"] >= activity_limit:
            break
        raw = line["text"]
        if (raw.startswith("(") or raw.startswith("Frühgruppe")
                or raw.startswith("Spätgruppe")):
            continue
        cleaned = _strip_time(raw)
        if cleaned:
            activity_parts.append(cleaned)
    activity = _clean_text(" · ".join(activity_parts))
    if not activity:
        return None, "lhx_training_missing_activity"

    location = None
    category = None
    after_location = 0
    if location_index is not None:
        location_parts = [lines[location_index]["text"]]
        after_location = location_index + 1
        while after_location < len(lines):
            continuation = lines[after_location]["text"]
            if (re.fullmatch(r"\d+(?:\.\d+)+", continuation)
                    or continuation == "Kairo"):
                location_parts.append(continuation)
                after_location += 1
                continue
            break
        location = _clean_text(" ".join(location_parts))

    category_index = None
    if location_index is not None:
        for index in range(after_location, len(lines)):
            if lines[index]["bold"]:
                category_index = index
                break
    elif "OFF" not in activity.upper():
        for index, line in enumerate(lines):
            if (line["bold"] and line["top"] > header_top + 35
                    and line["text"] not in activity_parts):
                category_index = index
                break
    if category_index is not None:
        category_parts = [lines[category_index]["text"]]
        previous_top = lines[category_index]["top"]
        for line in lines[category_index + 1:]:
            if not line["bold"] or line["top"] - previous_top > 12:
                break
            category_parts.append(line["text"])
            previous_top = line["top"]
        category = _clean_text(" ".join(category_parts))

    if "OFF" in activity.upper():
        if time_match:
            return None, "lhx_training_off_with_time"
        return {
            "activity": "Off Day", "category": None, "location": None,
            "start": None, "end": None, "kind": "off",
        }, None

    home_study = "HOME STUDY" in activity.upper()
    if time_match:
        start_hour, start_minute, end_hour, end_minute = (
            int(value) for value in time_match.groups())
        if (start_hour > 23 or end_hour > 23
                or (end_hour * 60 + end_minute)
                <= (start_hour * 60 + start_minute)):
            return None, "lhx_training_invalid_time_range"
        if not location or not category:
            return None, "lhx_training_incomplete_timed_day"
        return {
            "activity": activity, "category": category,
            "location": location,
            "start": (start_hour, start_minute),
            "end": (end_hour, end_minute), "kind": "training",
        }, None
    if home_study:
        return {
            "activity": activity, "category": category or "HSP",
            "location": location, "start": None, "end": None,
            "kind": "home_study",
        }, None
    return None, "lhx_training_missing_time_range"


def _summary(contract):
    activity = contract["activity"]
    category = contract.get("category")
    if activity == "Off Day":
        return activity
    if contract.get("kind") == "home_study":
        return activity
    if category and category.lower() not in activity.lower():
        return f"{activity} · {category}"
    return activity


def _local_to_utc(day, clock):
    local = datetime(day.year, day.month, day.day, clock[0], clock[1],
                     tzinfo=_GERMAN_TZ)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def parse_lhx_training_calendar(pdf_bytes, extracted_text=""):
    """Return ``(events, year, month, report, error)`` for an LHX course plan."""
    source = str(extracted_text or "")
    normalized = _clean_text(source)
    if (_TITLE_MARKER not in normalized[:2500]
            or _COURSE_MARKER not in source):
        return None, None, None, None, "unsupported_pdf_format"

    parsed = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            if not 2 <= len(pdf.pages) <= 12:
                return None, None, None, None, \
                    "lhx_training_unexpected_page_count"
            for page in pdf.pages[1:]:
                words = page.extract_words(
                    x_tolerance=1, y_tolerance=2, keep_blank_chars=False,
                    use_text_flow=False, extra_attrs=["fontname", "size"],
                ) or []
                anchors = _weekday_anchors(words)
                if anchors is None:
                    return None, None, None, None, \
                        "lhx_training_missing_week_header"
                days, bounds, error = _page_days(words, anchors, page.width)
                if error:
                    return None, None, None, None, error
                header_top = anchors[0][0]
                for index, day in enumerate(days):
                    contract, error = _cell_contract(
                        words, bounds[index][0], bounds[index][1], header_top)
                    if error:
                        return None, None, None, None, error
                    parsed.append((day, contract))
    except Exception:
        return None, None, None, None, "pdf_extract_failed"

    if not parsed:
        return None, None, None, None, "lhx_training_no_days"
    parsed.sort(key=lambda item: item[0])
    dates = [item[0] for item in parsed]
    if len(set(dates)) != len(dates):
        return None, None, None, None, "lhx_training_duplicate_date"
    expected = [dates[0] + timedelta(days=index)
                for index in range((dates[-1] - dates[0]).days + 1)]
    if dates != expected or not 14 <= len(dates) <= 90:
        return None, None, None, None, "lhx_training_date_strip_mismatch"

    events = []
    timed_count = off_count = home_study_count = 0
    for day, contract in parsed:
        summary = _summary(contract)
        uid = f"lhx-training-{day:%Y%m%d}"
        if contract["start"] is None:
            events.append((uid, day, day + timedelta(days=1), summary, True,
                           None, contract.get("location")))
            off_count += int(contract["kind"] == "off")
            home_study_count += int(contract["kind"] == "home_study")
            continue
        start = _local_to_utc(day, contract["start"])
        end = _local_to_utc(day, contract["end"])
        events.append((uid, start, end, summary, False, None,
                       contract.get("location")))
        timed_count += 1

    if timed_count < 5 or off_count < 2:
        return None, None, None, None, "lhx_training_activity_checksum_failed"
    return events, dates[0].year, dates[0].month, {
        "format": "lhx_cabin_initial_training_schedule",
        "period": f"{dates[0].isoformat()}..{dates[-1].isoformat()}",
        "timescale": "Europe/Berlin",
        "event_count": len(events),
        "timed_count": timed_count,
        "off_count": off_count,
        "home_study_count": home_study_count,
    }, None
