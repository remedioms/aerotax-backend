#!/usr/bin/env python3
"""Historical roster PDFs -> AeroX logbook JSON.

Supported, deliberately narrow formats:

* Lufthansa Jeppesen ``Acknowledged Roster`` (confirmedPlan/yearPlan)
* Condor ``Duty plan requested at ... - All times: Local FRA``

Both documents can contain future planned duties.  A logbook may only contain
completed flying, so this parser requires the document's own generation time
and drops every leg whose arrival lies after that timestamp.  Deadheads and
ground duties are never converted into flying legs.

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


def parse_acknowledged_text(text, validate_totals=True):
    """Return historical flight legs plus source metadata."""
    if (not re.search(r"(?im)^\s*Acknowledged Roster\s*$", text[:500])
            or ACK_HEADER not in text
            or not re.search(r"Company Name:\s*LH\b", text[:1500])):
        raise ValueError("unsupported acknowledged-roster format")
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
        if end > created:
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
                if end > created:
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
        if validate_totals and actual != expected:
            raise ValueError(
                f"acknowledged-roster block total mismatch for {key}: "
                f"source={expected} parsed={actual}")
        checked_totals[key] = expected
    for leg in legs:
        leg.pop("_roster_month", None)
    return legs, {
        "format": "lufthansa_acknowledged_roster",
        "created_at": created.isoformat(),
        "future_legs_excluded": future,
        "deadheads_excluded": deadheads,
        "duplicate_carry_rows_excluded": duplicate_rows,
        "verified_monthly_block_totals": checked_totals,
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


def parse_text(text):
    if re.search(r"(?im)^\s*Acknowledged Roster\s*$", text[:500]):
        return parse_acknowledged_text(text)
    if "Duty plan requested at" in text[:1000]:
        return parse_condor_text(text)
    raise ValueError("unsupported roster-logbook PDF format")


def _merge_source_legs(parsed_sources):
    """Prefer the newest document for cross-file revisions of one leg key.

    Multiple same-key legs inside that winning source remain intact; the
    regular AeroX collision suffixing handles genuine repeated shuttle legs.
    """
    by_key = defaultdict(lambda: defaultdict(list))
    source_created = {}
    for source_id, legs, meta in parsed_sources:
        source_created[source_id] = datetime.fromisoformat(meta["created_at"])
        for leg in legs:
            key = (leg["date"], leg["flight"], leg["from"], leg["to"])
            by_key[key][source_id].append(leg)
    merged = []
    superseded = 0
    for sources in by_key.values():
        winner = max(sources, key=lambda item: source_created[item])
        merged.extend(sources[winner])
        superseded += sum(len(value) for key, value in sources.items()
                          if key != winner)
    merged.sort(key=lambda leg: (leg["date"], leg["dep_iso"], leg["flight"]))
    return merged, superseded


def parse_sources(paths):
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
        legs, meta = parse_text(text)
        source_id = digest[:16]
        parsed.append((source_id, legs, meta))
        reports.append({**meta, "sha256_prefix": source_id,
                        "legs_before_merge": len(legs)})
    legs, superseded = _merge_source_legs(parsed)
    collisions = dedupe_keys(legs)
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
        "totals": {
            "legs": len(legs),
            "block_min": sum(leg["block_min"] for leg in legs),
            "landings": 0,
        },
    }
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
