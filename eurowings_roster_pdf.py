"""Deterministic Eurowings NetLine/Crew duty-plan calendar parser.

The document is a coordinate-based, three-column ``Individual duty plan``.
Only the compact month overview and strict schedule-leg rows are read.  Crew
lists and the free-text appendices are never copied into calendar data.
"""

from __future__ import annotations

import io
import re
from datetime import date, datetime, timedelta

import pdfplumber


_FORMAT_MARKERS = ("NetLine/Crew(EWG)", "NetLine/Crew(EW)")
_PERIOD_RE = re.compile(
    r"Period:\s*(\d{2})([A-Za-z]{3})(\d{2})\s*-\s*"
    r"(\d{2})([A-Za-z]{3})(\d{2})"
)
_DAY_RE = re.compile(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)(\d{2})\b")
_WEEKDAY_RE = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)$")
_LEG_RE = re.compile(
    r"\b(EW)\s?(\d{2,4})\s*(?:/\s?\d{1,2})?\s+(?:R\s+)?"
    r"([A-Z]{3})\s+(\d{4})\s+(\d{4})\s+([A-Z]{3})"
    r"\s+([0-9A-Z]{2,4})\b"
)
_FLIGHT_TIME_RE = re.compile(r"\bFlight time\s+(\d{1,3}):(\d{2})\b")
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_STATUS = {
    "fld": "flight",
    "dty": "duty",
    "off": "off",
    "vac": "vacation",
    "sby": "standby",
    "sim": "simulator",
    "abs": "absence",
    "sic": "absence",
    "tsp": "transport",
    "x": "free",
}


def _minutes(hhmm):
    return int(hhmm[:2]) * 60 + int(hhmm[2:])


def _clock(day, hhmm):
    return datetime(day.year, day.month, day.day,
                    int(hhmm[:2]), int(hhmm[2:]))


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


def _dates(start, end):
    result = []
    current = start
    while current <= end:
        result.append(current)
        current += timedelta(days=1)
    return result


def _day_map(start, end):
    result = {}
    for day in _dates(start, end):
        token = f"{_WEEKDAYS[day.weekday()]}{day.day:02d}"
        if token in result:
            return None
        result[token] = day
    return result


def _day_anchors(words, day_map):
    """Return ``(anchor_word, day_token)`` for both observed overview layouts.

    Older EWG exports emit one word (``Thu13``). Newer EW exports place the
    weekday and day number as two words with a small vertical offset. The
    printed weekday and the exact planning period still have to agree, so a
    nearby arbitrary number can never become a roster date.
    """
    result = []
    seen = set()
    for word in words:
        raw = str(word.get("text") or "")
        match = _DAY_RE.fullmatch(raw)
        if match and raw in day_map and raw not in seen:
            result.append((word, raw))
            seen.add(raw)
            continue
        weekday = _WEEKDAY_RE.fullmatch(raw)
        if not weekday:
            continue
        candidates = [
            candidate for candidate in words
            if re.fullmatch(r"\d{2}", str(candidate.get("text") or ""))
            and 0 <= float(candidate.get("x0") or 0)
            - float(word.get("x0") or 0) <= 12
            and abs(float(candidate.get("top") or 0)
                    - float(word.get("top") or 0)) <= 5
        ]
        if len(candidates) != 1:
            continue
        token = f"{weekday.group(1)}{candidates[0]['text']}"
        if token not in day_map or token in seen:
            continue
        anchor = dict(word)
        anchor["top"] = min(float(word.get("top") or 0),
                            float(candidates[0].get("top") or 0))
        result.append((anchor, token))
        seen.add(token)
    return result


def _line_words(words, page_width):
    """Linearize strict leg rows inside each of the three schedule columns."""
    # The lower schedule can use the same joined/split date layouts as the
    # overview. Its candidate map is intentionally limited to real printed
    # dates later by the caller's planning-period map.
    day_words = []
    for word in words:
        match = _DAY_RE.fullmatch(str(word.get("text") or ""))
        if match:
            day_words.append((word, match.group(0)))
            continue
        weekday = _WEEKDAY_RE.fullmatch(str(word.get("text") or ""))
        if not weekday:
            continue
        candidates = [
            candidate for candidate in words
            if re.fullmatch(r"\d{2}", str(candidate.get("text") or ""))
            and 0 <= float(candidate.get("x0") or 0)
            - float(word.get("x0") or 0) <= 12
            and abs(float(candidate.get("top") or 0)
                    - float(word.get("top") or 0)) <= 5
        ]
        if len(candidates) == 1:
            day_words.append((word,
                              f"{weekday.group(1)}{candidates[0]['text']}"))

    def geometric_line(row):
        """Rejoin glyph tokens that CUBE draws as separate text objects.

        A current EW export compresses two rows to fit a busy duty.  There,
        ``EW 6893 MUC ...`` is extracted as ``E``, ``W``, ``6``, ``8`` ...
        even though the glyph boxes touch.  Only touching boxes are joined;
        real field gaps remain spaces and the anchored leg regex stays the
        final acceptance gate.
        """
        result = ""
        previous = None
        for candidate in sorted(row, key=lambda item: float(item["x0"])):
            value = str(candidate.get("text") or "")
            if not value:
                continue
            gap = (None if previous is None else
                   float(candidate["x0"]) - float(previous["x1"]))
            result += ("" if gap is None or gap <= 1.0 else " ") + value
            previous = candidate
        return result

    output = []
    seen_rows = set()
    column_width = page_width / 3
    for word in words:
        raw = str(word.get("text") or "")
        if raw not in ("EW", "E"):
            continue
        if raw == "E" and not any(
                str(candidate.get("text") or "") == "W"
                and 0 <= float(candidate["x0"]) - float(word["x1"]) <= 1.0
                and abs(float(candidate["top"])
                        - float(word["top"])) <= 0.5
                for candidate in words):
            continue
        column = min(2, max(0, int(float(word["x0"]) / column_width)))
        row_key = (column, round(float(word["top"]), 1))
        if row_key in seen_rows:
            continue
        seen_rows.add(row_key)
        left, right = column * column_width, (column + 1) * column_width
        same_row = [
            candidate for candidate in words
            if left <= float(candidate["x0"]) < right
            and float(candidate["x0"]) >= float(word["x0"]) - 0.1
            and abs(float(candidate["top"])
                    - float(word["top"])) <= 2.5
        ]
        line = geometric_line(same_row)
        leg = _LEG_RE.search(line)
        if not leg:
            continue
        anchors = [
            (candidate, token) for candidate, token in day_words
            if left <= float(candidate["x0"]) < right
            and float(candidate["top"]) <= float(word["top"]) + 0.1
        ]
        if not anchors:
            continue
        day_token = max(anchors, key=lambda item: float(item[0]["top"]))[1]
        output.append((day_token, leg.groups()))
    return output


def _overview(page, day_map):
    """Read the privacy-free top strip: day, status, duty start and end."""
    words = page.extract_words(
        x_tolerance=2, y_tolerance=2, keep_blank_chars=False,
        use_text_flow=False,
    ) or []
    anchors = [
        (word, token) for word, token in _day_anchors(words, day_map)
        if float(word.get("top") or 0) < 65
    ]
    if {token for _, token in anchors} != set(day_map):
        return None, "eurowings_overview_date_mismatch"

    rows = {}
    for anchor, token in anchors:
        x0 = float(anchor["x0"])
        candidates = [
            word for word in words
            if 5 <= float(word.get("top") or 0)
            - float(anchor.get("top") or 0) <= 15
            and abs(float(word.get("x0") or 0) - x0) <= 10
            and str(word.get("text") or "").lower() in _STATUS
        ]
        if len(candidates) != 1:
            return None, "eurowings_unparsed_day_status"
        status_word = candidates[0]
        status = _STATUS[str(status_word["text"]).lower()]
        clocks = sorted(
            (word for word in words
             if 2 < float(word.get("top") or 0)
             - float(status_word.get("top") or 0) < 16
             and abs(float(word.get("x0") or 0) - x0) <= 10
             and re.fullmatch(r"\d{4}", str(word.get("text") or ""))),
            key=lambda word: float(word["top"]),
        )
        values = [str(word["text"]) for word in clocks]
        if status in (
                "flight", "standby", "duty", "simulator", "transport") \
                and len(values) != 2:
            return None, "eurowings_missing_duty_times"
        if status == "absence" and len(values) not in (0, 2):
            return None, "eurowings_missing_duty_times"
        if status in ("off", "vacation", "free") and values:
            return None, "eurowings_unexpected_ground_times"
        rows[day_map[token]] = {
            "status": status,
            "start": values[0] if values else None,
            "end": values[1] if values else None,
        }
    return rows, None


def parse_eurowings_netline_calendar(pdf_bytes, extracted_text=""):
    """Return ``(events, year, month, report, error)`` for an EWG plan."""
    source = str(extracted_text or "")
    if (not any(marker in source[:1500] for marker in _FORMAT_MARKERS)
            or "Individual duty plan" not in source[:500]):
        return None, None, None, None, "unsupported_pdf_format"
    start, end = _period(source)
    if start is None or end is None or end < start or (end - start).days > 62:
        return None, None, None, None, "eurowings_invalid_period"
    day_map = _day_map(start, end)
    if day_map is None:
        return None, None, None, None, "eurowings_ambiguous_period"

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            if not pdf.pages:
                return None, None, None, None, "pdf_extract_failed"
            overview, error = _overview(pdf.pages[0], day_map)
            if error:
                return None, None, None, None, error
            raw_legs = []
            for page in pdf.pages[:20]:
                words = page.extract_words(
                    x_tolerance=2, y_tolerance=2, keep_blank_chars=False,
                    use_text_flow=False,
                ) or []
                raw_legs.extend(_line_words(words, float(page.width)))
    except Exception:
        return None, None, None, None, "pdf_extract_failed"

    legs = []
    seen = set()
    for token, values in raw_legs:
        if token not in day_map:
            return None, None, None, None, "eurowings_leg_date_mismatch"
        key = (token,) + values
        if key in seen:
            continue
        seen.add(key)
        carrier, number, dep, off, on, arr, aircraft = values
        day = day_map[token]
        departure = _clock(day, off)
        arrival = _clock(day, on)
        if arrival <= departure:
            arrival += timedelta(days=1)
        block = int((arrival - departure).total_seconds() // 60)
        if not 1 <= block <= 20 * 60:
            return None, None, None, None, "eurowings_invalid_block"
        legs.append({
            "day": day, "carrier": carrier, "number": number,
            "dep": dep, "arr": arr, "departure": departure,
            "arrival": arrival, "block": block, "aircraft": aircraft,
        })
    checksum = _FLIGHT_TIME_RE.search(source)
    if not checksum:
        return None, None, None, None, "eurowings_missing_flight_checksum"
    expected_block = int(checksum.group(1)) * 60 + int(checksum.group(2))
    parsed_block = sum(leg["block"] for leg in legs)
    if parsed_block != expected_block:
        return None, None, None, None, "eurowings_flight_checksum_mismatch"

    flight_days = {leg["day"] for leg in legs}
    if any(row["status"] == "flight" and day not in flight_days
           for day, row in overview.items()):
        return None, None, None, None, "eurowings_missing_flight_day"
    if any(day in flight_days and row["status"] not in ("flight",)
           for day, row in overview.items()):
        return None, None, None, None, "eurowings_unexpected_flight_day"
    if (not legs
            and not any(row["status"] in (
                "duty", "simulator", "standby", "absence", "transport")
                for row in overview.values())):
        return None, None, None, None, "no_roster_days"

    events = []
    marker_count = 0
    for day, row in overview.items():
        if row["status"] == "off":
            events.append((f"off-{day:%Y%m%d}", day,
                           day + timedelta(days=1), "Off Day", True))
            marker_count += 1
        elif row["status"] == "vacation":
            events.append((f"vac-{day:%Y%m%d}", day,
                           day + timedelta(days=1), "Urlaub", True))
            marker_count += 1
        elif row["status"] == "free":
            events.append((f"free-{day:%Y%m%d}", day,
                           day + timedelta(days=1), "Off Day", True))
            marker_count += 1
        elif row["status"] == "standby":
            duty_start = _clock(day, row["start"])
            duty_end = _clock(day, row["end"])
            if duty_end <= duty_start:
                duty_end += timedelta(days=1)
            events.append((f"standby-{day:%Y%m%d}", duty_start, duty_end,
                           "Standby", False))
            marker_count += 1
        elif row["status"] in (
                "duty", "simulator", "absence", "transport"):
            label = {
                "duty": "Duty",
                "simulator": "Simulator",
                "absence": "Absence",
                "transport": "Transport",
            }[row["status"]]
            if row["start"] and row["end"]:
                duty_start = _clock(day, row["start"])
                duty_end = _clock(day, row["end"])
                if duty_end <= duty_start:
                    duty_end += timedelta(days=1)
                events.append((f"ground-{day:%Y%m%d}", duty_start, duty_end,
                               label, False))
            else:
                events.append((f"ground-{day:%Y%m%d}", day,
                               day + timedelta(days=1), label, True))
            marker_count += 1

    day_leg_number = {}
    for leg in sorted(legs, key=lambda item: item["departure"]):
        day = leg["day"]
        day_leg_number[day] = day_leg_number.get(day, 0) + 1
        flight = f"{leg['carrier']}{int(leg['number'])}"
        base = f"{flight} {leg['dep']} - {leg['arr']}"
        summary = base
        if day_leg_number[day] == 1:
            briefing = overview[day]["start"]
            summary = (f"{briefing[:2]}:{briefing[2:]} UTC Briefing "
                       f"{leg['dep']} · {base}")
        events.append((f"leg-{day:%Y%m%d}-{flight}-{day_leg_number[day]}",
                       leg["departure"], leg["arrival"], summary, False))

    events.sort(key=lambda event: event[1].isoformat())
    return events, start.year, start.month, {
        "format": "eurowings_netline_individual_duty_plan",
        "period": f"{start.isoformat()}..{end.isoformat()}",
        "timescale": "UTC",
        "flight_count": len(legs),
        "marker_count": marker_count,
        "block_minutes": parsed_block,
    }, None
