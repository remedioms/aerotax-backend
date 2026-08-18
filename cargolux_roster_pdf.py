"""Deterministic parser for Cargolux Personal Crew Schedule Report PDFs.

The schedule table is coordinate based.  We intentionally crop before the
``Crew`` column and stop before the training/hotel/expiry appendices: coworker
names, hotel addresses, phone numbers and document credentials never enter the
calendar pipeline.
"""

from __future__ import annotations

import io
import re
from datetime import datetime, timedelta

import pdfplumber


_MARKER = "Personal Crew Schedule Report"
_CREW_COLUMN_X0 = 665.0
_LINE_TOLERANCE = 3.0
_COLUMN_LIMITS = (83.0, 164.0, 280.0, 350.0, 465.0, 546.0, 610.0, 665.0)
_COLUMN_NAMES = (
    "date", "duties", "details", "report", "actual", "debrief",
    "credits", "indicators",
)
_DATE_RE = re.compile(
    r"(?P<date>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<weekday>Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b"
)
_ROUTE_RE = re.compile(r"\b(?P<dep>[A-Z]{3})\s*-\s*(?P<arr>[A-Z]{3})\b")
_DUTY_RE = re.compile(r"\b(?P<duty>(?:CV)?\d{3,4}[A-Z]?)\b", re.IGNORECASE)
_CLOCK_TOKEN = r"\d{1,2}:\d{2}(?:\+1|⁺¹|⁺1)?"
_RANGE_RE = re.compile(
    rf"(?P<start>{_CLOCK_TOKEN})\s*-\s*(?P<end>{_CLOCK_TOKEN})"
)
_CLOCK_RE = re.compile(_CLOCK_TOKEN)
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _clock_token(value):
    match = _CLOCK_RE.search(str(value or "").replace("⁺¹", "+1").replace("⁺1", "+1"))
    return match.group(0) if match else ""


def _clock_datetime(day, token):
    normalized = str(token or "").replace("⁺¹", "+1").replace("⁺1", "+1")
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(\+1)?", normalized)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    value = datetime(day.year, day.month, day.day, hour, minute)
    if match.group(3):
        value += timedelta(days=1)
    return value


def _column_name(x0):
    for name, limit in zip(_COLUMN_NAMES, _COLUMN_LIMITS):
        if x0 < limit:
            return name
    return None


def _page_schedule_lines(page):
    """Return schedule-table lines, excluding the privacy-sensitive Crew col."""
    words = page.extract_words(
        x_tolerance=2, y_tolerance=2, keep_blank_chars=False,
        use_text_flow=False,
    ) or []
    words = [word for word in words if float(word.get("x0") or 0) < _CREW_COLUMN_X0]
    words.sort(key=lambda word: (float(word.get("top") or 0), float(word.get("x0") or 0)))

    groups = []
    for word in words:
        top = float(word.get("top") or 0)
        if not groups or abs(top - groups[-1]["top"]) > _LINE_TOLERANCE:
            groups.append({"top": top, "words": [word]})
        else:
            groups[-1]["words"].append(word)
            count = len(groups[-1]["words"])
            groups[-1]["top"] = ((groups[-1]["top"] * (count - 1)) + top) / count

    in_schedule = False
    output = []
    for group in groups:
        group_words = sorted(group["words"], key=lambda word: float(word.get("x0") or 0))
        full_text = _clean(" ".join(str(word.get("text") or "") for word in group_words))
        if "Schedule Details" in full_text:
            in_schedule = True
            continue
        if not in_schedule:
            continue
        if "Total Hours and Statistics" in full_text or full_text.startswith("Generated on"):
            break
        if ("Date" in full_text and "Duties" in full_text
                and "Actual" in full_text and "Debrief" in full_text):
            continue

        columns = {name: [] for name in _COLUMN_NAMES}
        for word in group_words:
            name = _column_name(float(word.get("x0") or 0))
            if name:
                columns[name].append(str(word.get("text") or ""))
        line = {name: _clean(" ".join(columns[name])) for name in _COLUMN_NAMES}
        line["top"] = group["top"]
        line["text"] = full_text
        if any(line[name] for name in _COLUMN_NAMES):
            output.append(line)
    return output


def _records_from_lines(lines):
    anchors = []
    for line in lines:
        match = _DATE_RE.search(line.get("date") or line.get("text") or "")
        if not match:
            continue
        try:
            day = datetime.strptime(match.group("date"), "%d/%m/%Y").date()
        except ValueError:
            continue
        anchors.append({
            "top": float(line["top"]), "day": day,
            "weekday": match.group("weekday"),
            "page": int(line.get("page") or 0), "lines": [],
        })

    for line in lines:
        if not anchors:
            break
        page = int(line.get("page") or 0)
        same_page = [candidate for candidate in anchors if candidate["page"] == page]
        if not same_page:
            continue
        anchor = min(
            same_page,
            key=lambda candidate: abs(candidate["top"] - float(line["top"])),
        )
        anchor["lines"].append(line)
    return anchors


def _nearest_value(lines, origin_top, column, pattern=None, used=None):
    candidates = []
    for index, line in enumerate(lines):
        if used is not None and index in used:
            continue
        value = _clean(line.get(column))
        if not value or (pattern is not None and not pattern.search(value)):
            continue
        candidates.append((abs(float(line["top"]) - origin_top), index, value))
    if not candidates:
        return None, None
    _, index, value = min(candidates)
    return index, value


def _record_events(record):
    day = record["day"]
    lines = sorted(record["lines"], key=lambda line: float(line["top"]))
    if _WEEKDAYS[day.weekday()] != record["weekday"]:
        raise ValueError("invalid_roster_day")

    report = ""
    debrief = ""
    for line in lines:
        report = report or _clock_token(line.get("report"))
        debrief = debrief or _clock_token(line.get("debrief"))

    events = []
    used_actual = set()
    used_duties = set()
    route_lines = []
    for index, line in enumerate(lines):
        route = _ROUTE_RE.search(line.get("details") or "")
        if route:
            route_lines.append((index, line, route))

    for leg_number, (line_index, line, route) in enumerate(route_lines, 1):
        actual_index, actual = _nearest_value(
            lines, float(line["top"]), "actual", _RANGE_RE, used_actual)
        if actual_index is None:
            continue
        used_actual.add(actual_index)
        range_match = _RANGE_RE.search(actual.replace("⁺¹", "+1").replace("⁺1", "+1"))
        if not range_match:
            continue

        duty_index, duty_value = _nearest_value(
            lines, float(line["top"]), "duties", _DUTY_RE, used_duties)
        if duty_index is None:
            continue
        used_duties.add(duty_index)
        duty_match = _DUTY_RE.search(duty_value)
        if not duty_match:
            continue
        duty = duty_match.group("duty").upper()
        flight = duty if duty.startswith("CV") else f"CV{duty}"

        start = _clock_datetime(day, range_match.group("start"))
        end = _clock_datetime(day, range_match.group("end"))
        if start is None or end is None:
            continue
        if end <= start:
            end += timedelta(days=1)
        dep, arr = route.group("dep"), route.group("arr")
        base = f"{flight} {dep} - {arr}"
        summary = base
        if leg_number == 1 and report:
            summary = f"{report.replace('+1', '')} UTC Briefing {dep} · {base}"
        uid = f"leg-{day.strftime('%Y%m%d')}-{duty}-{leg_number}"
        events.append((uid, start, end, summary, False))

    if events:
        return events

    duties = _clean(" ".join(line.get("duties") or "" for line in lines))
    details = _clean(" ".join(line.get("details") or "" for line in lines))
    actual = ""
    for line in lines:
        if _RANGE_RE.search((line.get("actual") or "").replace("⁺¹", "+1")):
            actual = line.get("actual") or ""
            break

    if "Off Days" in details or re.fullmatch(r"[AB](?:\s+[AB])*", duties):
        return [(f"off-{day.strftime('%Y%m%d')}", day,
                 day + timedelta(days=1), "Off Day", True)]

    station_match = re.fullmatch(r"([A-Z]{3})(?:\s+\1)*", duties)
    if station_match and not actual:
        station = station_match.group(1)
        return [(f"layover-{day.strftime('%Y%m%d')}-{station}", day,
                 day + timedelta(days=1), "LAYOVER", True, None, station)]

    range_match = _RANGE_RE.search(actual.replace("⁺¹", "+1").replace("⁺1", "+1"))
    if range_match:
        start = _clock_datetime(day, report or range_match.group("start"))
        end = _clock_datetime(day, debrief or range_match.group("end"))
        if start is not None and end is not None:
            if end <= start:
                end += timedelta(days=1)
            label = _clean(f"Training {duties} {details}") or "Training"
            return [(f"training-{day.strftime('%Y%m%d')}", start, end,
                     label, False)]
    return []


def parse_cargolux_calendar(pdf_bytes, extracted_text=""):
    """Return ``(events, year, month, report, error)`` for a Cargolux roster."""
    marker_text = str(extracted_text or "")
    if (_MARKER not in marker_text or "CARGOLUX" not in marker_text.upper()
            or "All times in UTC" not in marker_text):
        return None, None, None, None, "unsupported_pdf_format"

    period_match = re.search(
        r"(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})"
        r"\s*\(All times in UTC\)", marker_text)
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            lines = []
            for page_index, page in enumerate(pdf.pages[:20]):
                page_lines = _page_schedule_lines(page)
                for line in page_lines:
                    line["page"] = page_index
                lines.extend(page_lines)
    except Exception:
        return None, None, None, None, "pdf_extract_failed"

    events = []
    try:
        for record in _records_from_lines(lines):
            events.extend(_record_events(record))
    except ValueError as exc:
        return None, None, None, None, str(exc)
    if not events:
        return None, None, None, None, "no_roster_days"

    # Timed duties use ``datetime`` while off/layover rows use ``date``.
    # Python deliberately does not order those types directly; ISO ordering
    # preserves the chronological contract for both.
    first = min((event[1] for event in events), key=lambda value: value.isoformat())
    report = {
        "period": (period_match.group(1) + " - " + period_match.group(2))
        if period_match else f"{first.year:04d}-{first.month:02d}",
        "timescale": "UTC",
    }
    return events, first.year, first.month, report, None
