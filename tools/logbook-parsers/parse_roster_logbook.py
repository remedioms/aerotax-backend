#!/usr/bin/env python3
"""Historical roster PDFs -> AeroX logbook JSON.

Supported, deliberately narrow formats:

* Lufthansa Jeppesen ``Acknowledged Roster`` / ``Released Roster``
  (LH and Lufthansa Cargo/YF)
* Lufthansa ``Crew Assignment System`` roster PDFs
* Condor ``Duty plan requested at ... - All times: Local FRA``
* Condor NetLine/Crew ``Individual duty plan``

These documents can contain future planned duties. A logbook may only contain
completed flying. The standalone parser therefore defaults to the document's
own generation time; the production watcher supplies its processing time and
still excludes every leg whose arrival is in the future. Deadheads and ground
duties are never converted into flying legs.

Usage::

    python3 parse_roster_logbook.py SOURCE.pdf [SOURCE.pdf ...] --out out.json
"""

import argparse
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pdfplumber

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
from legkeys import dedupe_keys


MONTHS = {
    name: number for number, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun",
         "jul", "aug", "sep", "oct", "nov", "dec"), 1)
}
ACK_HEADER = (
    "Date (LT) Trip ID Report (LT) Pos Activity From To Start (UTC) End (UTC)"
)
ACK_POS_ROLE = {
    "CP": "PIC",
    "AC": "PIC",
    "FO": "FO",
    "SF": "SFO",
    "PU": "FB",
    "P1": "FB",
}
ACK_FULL_LEG = re.compile(
    r"^(?:(?P<trip>\d{4,}(?:_\d+)?)\s+)?"
    r"(?:(?P<report>\d{1,2}:\d{2})\s+)?"
    r"(?P<pos>CP|AC|FO|SF|PU|P1)\s+"
    r"(?P<num>\d{1,4}[A-Z]?)\s+(?P<frm>[A-Z]{3})\s+(?P<to>[A-Z]{3})\s+"
    r"(?:(?P<start_day>\(\d{1,2}\))\s+)?"
    r"(?P<start>\d{1,2}:\d{2})\s+"
    r"(?:(?P<end_day>\(\d{1,2}\))\s+)?"
    r"(?P<end>\d{1,2}:\d{2})\s+(?P<type>[A-Z0-9]{3})\b"
)
ACK_SPLIT_LEG = re.compile(
    r"^(?:(?P<trip>\d{4,}(?:_\d+)?)\s+)?"
    r"(?:(?P<report>\d{1,2}:\d{2})\s+)?"
    r"(?P<pos>CP|AC|FO|SF|PU|P1)\s+"
    r"(?P<num>\d{1,4}[A-Z]?)\s+(?P<station>[A-Z]{3})\s+"
    r"(?:(?P<utc_day>\(\d{1,2}\))\s+)?"
    r"(?P<clock>\d{1,2}:\d{2})\s+(?P<type>[A-Z0-9]{3})\b"
)
ACK_CREATED = re.compile(
    r"Created\s+(\d{2})([A-Za-z]{3})(\d{4})\s+"
    r"(\d{1,2}):(\d{2})\s+\(UTC\)\s+by\s+Jeppesen"
)
ACK_KIND = re.compile(r"(?im)^\s*(Acknowledged|Released) Roster\s*$")
ACK_COMPANY = re.compile(r"Company Name:\s*(LH|YF)\b")

CONDOR_CREATED = re.compile(
    r"Duty plan requested at\s+(\d{2})([A-Z]{3})(\d{2})\s+"
    r"(\d{1,2}):(\d{2})z"
)
CONDOR_PERIOD = re.compile(r"\b(0[1-9]|1[0-2])/(20\d{2})\b")
CONDOR_TOTAL = re.compile(
    r"BT\s+DH\s+LSW\s+BZW\s+Off claim\s+Off assigned[^\n]*\n"
    r"\s*(\d{1,3}):(\d{2})\b")
CONDOR_FLIGHT = re.compile(
    r"^P\s+"
    r"(?:(?P<weekday>Mo|Tu|We|Th|Fr|Sa|Su)\s+(?P<day>\d{1,2})\s+)?"
    r"(?P<flight>DE\d{3,4}[A-Z]?)\s+"
    r"(?P<type>[A-Z0-9]{3})\s+(?P<reg>[A-Z0-9]{5})\s+"
    r"(?P<pos>[A-Z]{2})\s+C\d+\s+"
    r"(?:(?P<report>\d{1,2}:\d{2})\s+)?"
    r"(?P<frm>[A-Z]{3})\s+(?P<start>\d{1,2}:\d{2})\s+-\s+"
    r"(?P<end>\d{1,2}:\d{2})\s+(?P<to>[A-Z]{3})\b"
)

CONDOR_INDIVIDUAL_CREATED = re.compile(
    r"printed by CREWLINK\s+(\d{2})([A-Za-z]{3})(\d{2})\s+"
    r"(\d{1,2}):(\d{2})"
)
CONDOR_INDIVIDUAL_PERIOD = re.compile(
    r"Period:\s*(\d{2})([A-Za-z]{3})(\d{2})\s*-\s*"
    r"(\d{2})([A-Za-z]{3})(\d{2})"
)
CONDOR_INDIVIDUAL_TOTAL = re.compile(r"Flight time\s+(\d{1,3}):(\d{2})")
CONDOR_INDIVIDUAL_DAY = re.compile(
    r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)(\d{2})\b"
)
CONDOR_INDIVIDUAL_FLIGHT = re.compile(
    # pdfplumber can leave the tail of the neighbouring column in front of a
    # row (``:58] DE ...``). It can also keep the day's anchor on that row
    # (``Tue14 DE ...``). Both are layout artefacts, not part of the flight.
    # Keep the expression anchored so DH/DE and prose/PNR references still
    # cannot become operating legs.
    r"^(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\d{2}\s+)?"
    r"(?:[^\s]{1,12}\]\s+)?DE\s+"
    # Condor also operates two-digit designators (for example DE30/DE31).
    r"(?P<number>\d{2,4}[A-Z]?)\s+(?:R\s+)?"
    # On an overnight UTC row NetLine can print the station-local departure
    # day between the designator and origin (``DE 2403 /06 YYZ ...``). The
    # leading weekday/day anchor remains the authoritative UTC roster day.
    r"(?:/\d{2}\s+)?"
    r"(?P<frm>[A-Z]{3})\s+(?P<start>\d{4})\s+"
    r"(?P<end>\d{4})\s+(?P<to>[A-Z]{3})\s+"
    # At the right edge of a printed column CREWLINK can append the duty's
    # summary to the final flight row (``[FT 09:21]``).  It is not another
    # time value, but rejecting that otherwise complete row made the strict
    # document total differ by exactly the duration of the last leg.
    # Roster revisions print either the jurisdiction marker (JU) or the crew
    # role (for example ST) at the right edge of an otherwise complete row.
    r"(?P<type>[A-Z0-9]{3})(?:\s+(?:JU|CP|FO|PU|ST))?"
    r"(?:\s+\[FT\s+\d{1,2}:\d{2}\])?$"
)


def _month(value):
    try:
        return MONTHS[value[:3].lower()]
    except (KeyError, TypeError):
        raise ValueError(f"unknown month token: {value!r}")


def _clock(day, value, tzinfo=timezone.utc):
    hour, minute = (int(part) for part in value.split(":"))
    if hour > 23 or minute > 59:
        raise ValueError("invalid roster clock")
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tzinfo)


def _month_shift(year, month, offset):
    serial = year * 12 + month - 1 + offset
    return serial // 12, serial % 12 + 1


def _marker_day(reference, marker):
    if not marker:
        return reference
    dom = int(marker.strip("()"))
    candidates = []
    for offset in (-1, 0, 1):
        year, month = _month_shift(reference.year, reference.month, offset)
        try:
            candidates.append(date(year, month, dom))
        except ValueError:
            pass
    if not candidates:
        raise ValueError("invalid acknowledged-roster UTC day")
    return min(candidates, key=lambda item: abs((item - reference).days))


def _printed_day(period, dom, weekday):
    candidates = []
    for offset in (-1, 0, 1):
        year, month = _month_shift(period[0], period[1], offset)
        try:
            item = date(year, month, int(dom))
        except ValueError:
            continue
        if item.strftime("%a") == weekday:
            candidates.append(item)
    current = [item for item in candidates
               if (item.year, item.month) == period]
    if current:
        return current[0]
    if not candidates:
        raise ValueError("acknowledged-roster weekday/date mismatch")
    return min(candidates,
               key=lambda item: abs((item - date(period[0], period[1], 1)).days))


def _iso_utc(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z")


def _leg(carrier, number, frm, to, start, end, aircraft_type, role,
         registration=None, remarks=None):
    block = int((end - start).total_seconds() // 60)
    if not 0 < block < 1200:
        raise ValueError("invalid roster block time")
    result = {
        "date": start.astimezone(timezone.utc).date().isoformat(),
        "flight": f"{carrier}{number.lstrip('0') or '0'}",
        "from": frm,
        "to": to,
        "dep_iso": _iso_utc(start),
        "arr_iso": _iso_utc(end),
        "block_min": block,
        "type": aircraft_type,
        "role": role,
    }
    if registration:
        result["reg"] = registration
    if remarks:
        result["remarks"] = remarks
    return result


def _utc_cutoff(value, fallback):
    cutoff = value if value is not None else fallback
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    return cutoff.astimezone(timezone.utc)


def parse_acknowledged_text(text, validate_totals=True, completed_at=None,
                            preserve_source_month=False):
    """Return historical flight legs plus source metadata."""
    kind_match = ACK_KIND.search(text[:500])
    company_match = ACK_COMPANY.search(text[:1500])
    if not kind_match or ACK_HEADER not in text or not company_match:
        raise ValueError("unsupported acknowledged-roster format")
    roster_kind = kind_match.group(1).lower()
    company = company_match.group(1).upper()
    created_matches = ACK_CREATED.findall(text)
    if not created_matches:
        raise ValueError("acknowledged-roster creation timestamp missing")
    created_values = {
        datetime(int(year), _month(mon), int(day), int(hour), int(minute),
                 tzinfo=timezone.utc)
        for day, mon, year, hour, minute in created_matches
    }
    if len(created_values) != 1:
        raise ValueError("acknowledged-roster creation timestamps disagree")
    created = created_values.pop()
    cutoff = _utc_cutoff(completed_at, created)

    legs = []
    period = None
    in_table = False
    current_day = None
    last_day = None
    pending = None
    future = deadheads = 0

    monthly_totals = {}
    month_headers = list(re.finditer(
        r"(?m)^Month:\s*([A-Za-z]+)\s+(\d{4})$", text))
    for index, header in enumerate(month_headers):
        month_number = _month(header.group(1))
        key = (int(header.group(2)), month_number)
        end_offset = (month_headers[index + 1].start()
                      if index + 1 < len(month_headers) else len(text))
        segment = text[header.end():end_offset]
        abbreviation = header.group(1)[:3]
        summary = re.search(
            rf"(?im)^{re.escape(abbreviation)}\s+(\d{{1,3}}):(\d{{2}})\b",
            segment)
        if summary:
            value = int(summary.group(1)) * 60 + int(summary.group(2))
            previous = monthly_totals.get(key)
            if previous is not None and previous != value:
                raise ValueError("acknowledged-roster monthly totals disagree")
            monthly_totals[key] = value

    def append_leg(match, start_day, end_day, start_clock, end_clock,
                   aircraft_type, role):
        nonlocal future
        start = _clock(start_day, start_clock)
        end = _clock(end_day, end_clock)
        if end <= start:
            end += timedelta(days=1)
        if end > cutoff:
            future += 1
            return
        leg = _leg(
            "LH", match.group("num"), match.group("frm"), match.group("to"),
            start, end, aircraft_type, role,
            remarks="Acknowledged Roster; completed before document creation",
        )
        leg["_roster_month"] = f"{current_day.year:04d}-{current_day.month:02d}"
        legs.append(leg)

    for raw in text.splitlines():
        line = raw.strip()
        month_match = re.match(r"^Month:\s*([A-Za-z]+)\s+(\d{4})$", line)
        if month_match:
            new_period = (int(month_match.group(2)),
                          _month(month_match.group(1)))
            # Continuation pages repeat the month header but start directly
            # with the remaining legs of the previous printed day.  Preserve
            # that day across an identical header; reset only for a real new
            # month.
            if new_period != period:
                period = new_period
                current_day = last_day = None
            in_table = False
            continue
        if line.startswith(ACK_HEADER):
            if period is None:
                raise ValueError("acknowledged-roster month missing")
            in_table = True
            continue
        if line.startswith("Created "):
            in_table = False
            continue
        if not in_table or not line:
            continue
        if line.startswith(("Difference ", "Max ", "Min ", "[duty]")):
            continue

        day_match = re.match(
            r"^(\d{2})\s+(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b\s*(.*)$", line)
        rest = line
        if day_match:
            current_day = _printed_day(period, day_match.group(1),
                                       day_match.group(2))
            if last_day and current_day < last_day - timedelta(days=1):
                raise ValueError("non-monotonic acknowledged-roster day")
            last_day = current_day
            rest = (day_match.group(3) or "").strip()
            if not rest:
                continue
        if current_day is None:
            continue

        # Deadheads have their own position token and must never become legs.
        if re.match(r"^(?:\d{1,2}:\d{2}\s+)?DH\s+\d{1,4}[A-Z]?\b", rest):
            deadheads += 1
            continue

        full = ACK_FULL_LEG.match(rest)
        if full:
            if pending:
                raise ValueError("unclosed acknowledged-roster split leg")
            start_day = _marker_day(current_day, full.group("start_day"))
            end_day = _marker_day(current_day, full.group("end_day"))
            if start_day > end_day:
                end_day = start_day
            append_leg(full, start_day, end_day, full.group("start"),
                       full.group("end"), full.group("type"),
                       ACK_POS_ROLE[full.group("pos")])
            continue

        split = ACK_SPLIT_LEG.match(rest)
        if split:
            clock_day = _marker_day(current_day, split.group("utc_day"))
            if pending:
                if pending["num"] != split.group("num"):
                    raise ValueError("acknowledged-roster split number mismatch")
                start = _clock(pending["day"], pending["clock"])
                end = _clock(clock_day, split.group("clock"))
                if end <= start:
                    end += timedelta(days=1)
                if end > cutoff:
                    future += 1
                else:
                    leg = _leg(
                        "LH", pending["num"], pending["station"],
                        split.group("station"), start, end,
                        pending["type"], pending["role"],
                        remarks=("Acknowledged Roster; completed before "
                                 "document creation"),
                    )
                    leg["_roster_month"] = (
                        f"{current_day.year:04d}-{current_day.month:02d}")
                    legs.append(leg)
                pending = None
            else:
                pending = {
                    "num": split.group("num"),
                    "station": split.group("station"),
                    "clock": split.group("clock"),
                    "day": clock_day,
                    "type": split.group("type"),
                    "role": ACK_POS_ROLE[split.group("pos")],
                }
    if pending:
        raise ValueError("unclosed acknowledged-roster split leg")
    unique_legs = []
    seen_leg_rows = set()
    duplicate_rows = 0
    for leg in legs:
        identity = tuple(
            leg.get(key) for key in (
                "date", "flight", "from", "to", "dep_iso", "arr_iso",
                "block_min", "type", "role", "_roster_month"))
        # Adjacent month sections intentionally repeat a trip that crosses a
        # month boundary.  It is the same flight, not a second logbook leg.
        if identity in seen_leg_rows:
            duplicate_rows += 1
            continue
        seen_leg_rows.add(identity)
        unique_legs.append(leg)
    legs = unique_legs
    parsed_monthly = defaultdict(int)
    for leg in legs:
        parsed_monthly[leg["_roster_month"]] += leg["block_min"]
    checked_totals = {}
    for (year, month), expected in monthly_totals.items():
        # The generation month's total includes still-planned future flying;
        # only fully elapsed months are a valid control sum for a logbook.
        if (year, month) >= (created.year, created.month):
            continue
        key = f"{year:04d}-{month:02d}"
        actual = parsed_monthly.get(key, 0)
        # Cargo/YF's printed "Blocktime" contains contractual credit
        # adjustments (the production samples differ by exactly 01:30 from
        # the sum of their explicit leg rows). It is not a mathematical
        # flight-time checksum and must not be presented as one. Every YF leg
        # is instead controlled through its printed UTC start/end pair.
        if validate_totals and company == "LH" and actual != expected:
            raise ValueError(
                f"acknowledged-roster block total mismatch for {key}: "
                f"source={expected} parsed={actual}")
        checked_totals[key] = expected
    if not preserve_source_month:
        for leg in legs:
            leg.pop("_roster_month", None)
    return legs, {
        "format": f"lufthansa_{company.lower()}_{roster_kind}_roster",
        "created_at": created.isoformat(),
        "completed_cutoff": cutoff.isoformat(),
        "coverage_months": sorted({
            f"{int(match.group(2)):04d}-{_month(match.group(1)):02d}"
            for match in month_headers
        }),
        "future_legs_excluded": future,
        "deadheads_excluded": deadheads,
        "duplicate_carry_rows_excluded": duplicate_rows,
        "verified_monthly_block_totals": (
            checked_totals if company == "LH" else {}),
        "source_monthly_block_totals": {
            f"{year:04d}-{month:02d}": value
            for (year, month), value in monthly_totals.items()
        },
        "monthly_total_control": (
            "verified" if company == "LH"
            else "not_applicable_yf_credit_time"),
    }


def _condor_registration(raw):
    value = raw.strip().upper()
    return f"D-{value[1:]}" if re.fullmatch(r"D[A-Z]{4}", value) else value


def parse_condor_text(text):
    if "Duty plan requested at" not in text or "All times: Local FRA" not in text:
        raise ValueError("unsupported Condor duty-plan format")
    created_match = CONDOR_CREATED.search(text)
    period_match = CONDOR_PERIOD.search(text[:1000])
    total_match = CONDOR_TOTAL.search(text[:1500])
    if not created_match or not period_match or not total_match:
        raise ValueError(
            "Condor duty-plan period, creation time or block total missing")
    day, mon, year2, hour, minute = created_match.groups()
    created = datetime(2000 + int(year2), _month(mon), int(day), int(hour),
                       int(minute), tzinfo=timezone.utc)
    period = (int(period_match.group(2)), int(period_match.group(1)))
    berlin = ZoneInfo("Europe/Berlin")
    current_day = None
    legs = []
    future = 0
    seen_candidate = 0
    all_block_min = 0

    for raw in text.splitlines():
        line = raw.strip()
        # A day is often introduced by a ground duty (for example SB90) and
        # the operating legs follow as continuation rows without a repeated
        # date.  Every printed duty row is therefore a valid day anchor, not
        # only a row that already contains a flight.
        anchor = re.match(
            r"^P\s+(Mo|Tu|We|Th|Fr|Sa|Su)\s+(\d{1,2})\b", line)
        if anchor:
            current_day = date(period[0], period[1], int(anchor.group(2)))
            if current_day.strftime("%a")[:2] != anchor.group(1):
                raise ValueError("Condor duty-plan weekday/date mismatch")
        match = CONDOR_FLIGHT.match(line)
        if not match:
            continue
        seen_candidate += 1
        if match.group("day"):
            current_day = date(period[0], period[1], int(match.group("day")))
        if current_day is None:
            raise ValueError("Condor continuation leg without day anchor")
        if match.group("weekday") and current_day.strftime("%a")[:2] != match.group("weekday"):
            raise ValueError("Condor duty-plan weekday/date mismatch")
        start = _clock(current_day, match.group("start"), berlin)
        end = _clock(current_day, match.group("end"), berlin)
        if end <= start:
            end += timedelta(days=1)
        block = int((end - start).total_seconds() // 60)
        if not 0 < block < 1200:
            raise ValueError("invalid Condor duty-plan block time")
        all_block_min += block
        if end.astimezone(timezone.utc) > created:
            future += 1
            continue
        legs.append(_leg(
            "DE", match.group("flight")[2:], match.group("frm"),
            match.group("to"), start, end, match.group("type"), "FB",
            registration=_condor_registration(match.group("reg")),
            remarks=("Condor Duty Plan; Local FRA times; completed before "
                     "document creation"),
        ))
    if not seen_candidate:
        raise ValueError("Condor duty-plan contains no flight rows")
    expected_block_min = (int(total_match.group(1)) * 60
                          + int(total_match.group(2)))
    if all_block_min != expected_block_min:
        raise ValueError(
            "Condor duty-plan block total mismatch: "
            f"source={expected_block_min} parsed={all_block_min}")
    return legs, {
        "format": "condor_duty_plan",
        "created_at": created.isoformat(),
        "period": f"{period[0]:04d}-{period[1]:02d}",
        "future_legs_excluded": future,
        "deadheads_excluded": 0,
        "verified_source_block_total": expected_block_min,
    }


def _compact_clock(day, value):
    if not re.fullmatch(r"\d{4}", value):
        raise ValueError("invalid compact roster clock")
    return _clock(day, f"{value[:2]}:{value[2:]}")


def _individual_day(period_start, period_end, previous, weekday, dom):
    candidates = []
    cursor = period_start
    while cursor <= period_end:
        if cursor.day == int(dom) and cursor.strftime("%a") == weekday:
            candidates.append(cursor)
        cursor += timedelta(days=1)
    if previous is not None:
        candidates = [candidate for candidate in candidates
                      if candidate > previous]
    if not candidates:
        raise ValueError("Condor individual-plan weekday/date mismatch")
    return candidates[0]


def parse_condor_individual_segments(header_text, segments, role):
    """Parse schedule columns cropped from a NetLine Individual duty plan.

    The PDF paints three independent schedule columns on each landscape page.
    Whole-page extraction interleaves their glyphs, so callers must provide
    the columns in chronological reading order.  The printed flight-time total
    remains a strict end-to-end control over all (past and future) flight rows.
    """
    if ("Individual duty plan" not in header_text
            or "NetLine/Crew(CFG)" not in header_text):
        raise ValueError("unsupported Condor individual duty-plan format")
    created_match = CONDOR_INDIVIDUAL_CREATED.search(header_text)
    period_match = CONDOR_INDIVIDUAL_PERIOD.search(header_text)
    total_match = CONDOR_INDIVIDUAL_TOTAL.search(header_text)
    if not created_match or not period_match or not total_match:
        raise ValueError(
            "Condor individual-plan timestamp, period or total missing")
    created_day, created_mon, created_year, created_hour, created_minute = (
        created_match.groups())
    # CREWLINK prints the workstation-local timestamp.  Its timezone is not
    # stated in the document, so creation-day legs are conservatively excluded
    # instead of guessing.  Earlier roster flight clocks are UTC: their raw
    # differences reconcile exactly to the printed Flight time control total.
    created_local = datetime(
        2000 + int(created_year), _month(created_mon), int(created_day),
        int(created_hour), int(created_minute), tzinfo=ZoneInfo("Europe/Berlin"))
    start_day, start_mon, start_year, end_day, end_mon, end_year = (
        period_match.groups())
    period_start = date(2000 + int(start_year), _month(start_mon),
                        int(start_day))
    period_end = date(2000 + int(end_year), _month(end_mon), int(end_day))
    if period_end < period_start:
        raise ValueError("invalid Condor individual-plan period")
    role_map = {"CP": "PIC", "FO": "FO", "PU": "FB", "ST": "FB"}
    if role not in role_map:
        raise ValueError("unsupported Condor individual-plan crew role")

    legs = []
    previous_day = current_day = None
    all_block_min = 0
    seen_candidate = future = current_day_excluded = 0
    for segment in segments:
        for raw in segment.splitlines():
            line = raw.strip()
            # A summary value from the neighbouring printed column can touch
            # the day label (for example ``:00] Sat18``).  The date token
            # itself remains intact, so find it instead of requiring column 0.
            anchor = CONDOR_INDIVIDUAL_DAY.search(line)
            if anchor:
                current_day = _individual_day(
                    period_start, period_end, previous_day,
                    anchor.group(1), anchor.group(2))
                previous_day = current_day
            match = CONDOR_INDIVIDUAL_FLIGHT.match(line)
            if not match:
                continue
            if current_day is None:
                raise ValueError(
                    "Condor individual-plan flight without day anchor")
            seen_candidate += 1
            start = _compact_clock(current_day, match.group("start"))
            end = _compact_clock(current_day, match.group("end"))
            if end <= start:
                end += timedelta(days=1)
            block = int((end - start).total_seconds() // 60)
            if not 0 < block < 1200:
                raise ValueError("invalid Condor individual-plan block time")
            all_block_min += block
            if end.date() >= created_local.date():
                if end.date() == created_local.date():
                    current_day_excluded += 1
                else:
                    future += 1
                continue
            legs.append(_leg(
                "DE", match.group("number"), match.group("frm"),
                match.group("to"), start, end, match.group("type"),
                role_map[role],
                remarks=("Condor Individual Duty Plan; roster times UTC; "
                         "completed before document creation"),
            ))
    if not seen_candidate:
        raise ValueError("Condor individual-plan contains no flight rows")
    expected_block_min = (int(total_match.group(1)) * 60
                          + int(total_match.group(2)))
    if all_block_min != expected_block_min:
        raise ValueError(
            "Condor individual-plan block total mismatch: "
            f"source={expected_block_min} parsed={all_block_min}")
    return legs, {
        "format": "condor_individual_duty_plan",
        "created_at": created_local.astimezone(timezone.utc).isoformat(),
        "period": f"{period_start.isoformat()}/{period_end.isoformat()}",
        "future_legs_excluded": future,
        "creation_day_legs_excluded": current_day_excluded,
        "deadheads_excluded": 0,
        "verified_source_block_total": expected_block_min,
        "source_role": role,
    }


def parse_condor_individual_pdf(pdf):
    if len(pdf.pages) < 3:
        raise ValueError("Condor individual-plan pages missing")
    header_text = "\n".join(
        (page.extract_text(x_tolerance=2, y_tolerance=2) or "")
        for page in pdf.pages)
    first = pdf.pages[0]
    role_text = first.crop((70, 50, 280, 130)).extract_text(
        x_tolerance=2, y_tolerance=2) or ""
    roles = set(re.findall(r"\b(?:CP|FO|PU|ST)\b", role_text))
    if len(roles) != 1:
        raise ValueError("Condor individual-plan crew role is ambiguous")

    segments = []
    found_total = False
    for page in pdf.pages:
        if page.width < 700 or page.height < 500:
            raise ValueError("unexpected Condor individual-plan page geometry")
        for column in range(3):
            segment = page.crop((
                page.width * column / 3,
                page.height * 0.15,
                page.width * (column + 1) / 3,
                page.height * 0.95,
            )).extract_text(x_tolerance=2, y_tolerance=2) or ""
            if "Flight time" in segment:
                segments.append(segment.split("Flight time", 1)[0])
                found_total = True
                break
            segments.append(segment)
        if found_total:
            break
    if not found_total:
        raise ValueError("Condor individual-plan schedule summary missing")
    return parse_condor_individual_segments(
        header_text, segments, roles.pop())


def parse_cas_pdf(path, completed_at=None, preserve_source_month=False):
    """Convert the already verified CAS calendar rows into logbook legs.

    CAS contains no reliable aircraft registration/type or landing columns,
    so those fields stay absent. UTC start/end, flight number and routing are
    taken only from the deterministic coordinate parser used by the calendar
    import. All flight tuples must convert; a partial conversion is rejected.
    """
    from cas_roster_parser import parse_cas_roster_pdf

    result, error = parse_cas_roster_pdf(path, carrier="LH")
    if error or not result:
        raise ValueError(f"CAS roster parse failed: {error or 'empty'}")
    printed = result.get("printed_at")
    if not isinstance(printed, datetime):
        raise ValueError("CAS roster print timestamp missing")
    created = printed.replace(tzinfo=timezone.utc) if printed.tzinfo is None \
        else printed.astimezone(timezone.utc)
    cutoff = _utc_cutoff(completed_at, created)
    flight_re = re.compile(
        r"(?:^|·\s*)(LH\d{2,4}[A-Z]?)\s+([A-Z]{3})\s+-\s+([A-Z]{3})$")
    legs = []
    candidates = future = 0
    for event in result.get("events") or []:
        if len(event) < 5 or event[4]:
            continue
        match = flight_re.search(str(event[3] or ""))
        if not match:
            continue
        candidates += 1
        start, end = event[1], event[2]
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            raise ValueError("CAS roster flight chronology missing")
        start = start.replace(tzinfo=timezone.utc) if start.tzinfo is None \
            else start.astimezone(timezone.utc)
        end = end.replace(tzinfo=timezone.utc) if end.tzinfo is None \
            else end.astimezone(timezone.utc)
        block = int((end - start).total_seconds() // 60)
        if not 0 < block < 1200:
            raise ValueError("invalid CAS roster block time")
        if end > cutoff:
            future += 1
            continue
        leg = {
            "date": start.date().isoformat(),
            "flight": match.group(1),
            "from": match.group(2),
            "to": match.group(3),
            "dep_iso": _iso_utc(start),
            "arr_iso": _iso_utc(end),
            "block_min": block,
            "remarks": "Lufthansa CAS roster; UTC schedule row",
            "_roster_month": start.strftime("%Y-%m"),
        }
        legs.append(leg)
    expected = int((result.get("counts") or {}).get("flight_legs") or 0)
    if not expected or candidates != expected:
        raise ValueError(
            f"CAS roster flight conversion incomplete: {candidates}!={expected}")
    if not preserve_source_month:
        for leg in legs:
            leg.pop("_roster_month", None)
    coverage_dates = list(result.get("coverage_dates") or [])
    coverage_months = sorted({day[:7] for day in coverage_dates
                              if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)})
    return legs, {
        "format": "lufthansa_cas_roster",
        "created_at": created.isoformat(),
        "completed_cutoff": cutoff.isoformat(),
        "period": result.get("period"),
        "coverage_dates": coverage_dates,
        "coverage_months": coverage_months,
        "flight_rows_verified": expected,
        "future_legs_excluded": future,
        "warnings": list(result.get("warnings") or []),
    }


def parse_text(text, completed_at=None, preserve_source_month=False):
    if ACK_KIND.search(text[:500]):
        return parse_acknowledged_text(
            text, completed_at=completed_at,
            preserve_source_month=preserve_source_month)
    if "Duty plan requested at" in text[:1000]:
        return parse_condor_text(text)
    if "Individual duty plan" in text[:1000]:
        raise ValueError(
            "Condor individual duty plan requires PDF column extraction")
    raise ValueError("unsupported roster-logbook PDF format")


def _merge_source_legs(parsed_sources):
    """Prefer the newest document for cross-file revisions of one leg key.

    Multiple same-key legs inside that winning source remain intact; the
    regular AeroX collision suffixing handles genuine repeated shuttle legs.
    """
    by_key = defaultdict(lambda: defaultdict(list))
    source_created = {}
    month_sources = defaultdict(set)
    for source_id, legs, meta in parsed_sources:
        source_created[source_id] = datetime.fromisoformat(meta["created_at"])
        for month in meta.get("coverage_months") or []:
            month_sources[month].add(source_id)
        for leg in legs:
            key = (leg["date"], leg["flight"], leg["from"], leg["to"])
            by_key[key][source_id].append(leg)
    # A roster is a complete plan revision for every printed month. A newer
    # document must therefore replace that whole month's older assignment,
    # including flights whose number/routing changed or disappeared. A plain
    # leg-key merge would incorrectly retain both revisions.
    month_winners = {
        month: max(sources, key=lambda source: source_created[source])
        for month, sources in month_sources.items()
    }
    merged = []
    superseded = 0
    for sources in by_key.values():
        eligible = {}
        for source_id, source_legs in sources.items():
            kept = [leg for leg in source_legs
                    if not leg.get("_roster_month")
                    or month_winners.get(leg["_roster_month"], source_id)
                    == source_id]
            superseded += len(source_legs) - len(kept)
            if kept:
                eligible[source_id] = kept
        if not eligible:
            continue
        winner = max(eligible, key=lambda item: source_created[item])
        merged.extend(eligible[winner])
        superseded += sum(len(value) for key, value in sources.items()
                          if key != winner and key in eligible)
    merged.sort(key=lambda leg: (leg["date"], leg["dep_iso"], leg["flight"]))
    return merged, superseded


def parse_sources(paths, completed_at=None, preserve_source_month=False):
    parsed = []
    reports = []
    seen_hashes = set()
    duplicate_files = 0
    for path in paths:
        blob = open(path, "rb").read()
        digest = hashlib.sha256(blob).hexdigest()
        if digest in seen_hashes:
            duplicate_files += 1
            continue
        seen_hashes.add(digest)
        with pdfplumber.open(path) as pdf:
            text = "\n".join((page.extract_text() or "") for page in pdf.pages)
            if ("Individual duty plan" in text[:1000]
                    and "NetLine/Crew(CFG)" in text[:1000]):
                legs, meta = parse_condor_individual_pdf(pdf)
            elif ("Crew Assignment System" in text[:1500]
                  and ("Einsatzplan" in text[:1500]
                       or "Dienstplan" in text[:1500])):
                legs, meta = parse_cas_pdf(
                    path, completed_at=completed_at,
                    preserve_source_month=True)
            else:
                legs, meta = parse_text(
                    text, completed_at=completed_at,
                    preserve_source_month=True)
        source_id = digest[:16]
        parsed.append((source_id, legs, meta))
        reports.append({**meta, "sha256_prefix": source_id,
                        "legs_before_merge": len(legs)})
    legs, superseded = _merge_source_legs(parsed)
    collisions = dedupe_keys(legs)
    if not preserve_source_month:
        for leg in legs:
            leg.pop("_roster_month", None)
    for leg in legs:
        for code in (leg["from"], leg["to"]):
            if not re.fullmatch(r"[A-Z]{3}", code):
                raise ValueError("invalid airport code")
        if leg["arr_iso"] <= leg["dep_iso"] or leg["block_min"] <= 0:
            raise ValueError("invalid parsed leg chronology")
    report = {
        "parser": "parse_roster_logbook.py",
        "sources": reports,
        "duplicate_files_skipped": duplicate_files,
        "superseded_revision_legs": superseded,
        "dedupe_suffixes": collisions,
        "coverage_months": sorted({
            month for source in reports
            for month in (source.get("coverage_months") or [])
        }),
        "source_created_at": max(
            (source["created_at"] for source in reports), default=None),
        "totals": {
            "legs": len(legs),
            "block_min": sum(leg["block_min"] for leg in legs),
            "landings": 0,
        },
    }
    report["month"] = (
        "–".join((report["coverage_months"][0],
                  report["coverage_months"][-1]))
        if len(report["coverage_months"]) > 1
        else (report["coverage_months"][0]
              if report["coverage_months"] else "unknown"))
    return {"legs": legs, "sim": [], "report": report}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("sources", nargs="+")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    payload = parse_sources(args.sources)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    totals = payload["report"]["totals"]
    print(f"{totals['legs']} historical legs / {totals['block_min']} min; "
          f"0 landings (source contains none)")


if __name__ == "__main__":
    main()
