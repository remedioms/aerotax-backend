#!/usr/bin/env python3
"""Strict parser for AeroX's compact eight-column flight-history CSV.

Rows are headerless and have the shape
``date,from,to,flight,class,duration_min,note,Duty|Private``.  Only ``Duty``
rows are operating logbook facts; private passenger trips remain outside the
professional crew logbook.  The exact width and value domains keep this
content router from claiming arbitrary headerless CSV files.
"""

import csv
import os
import re
from datetime import datetime


RE_AIRPORT = re.compile(r"^[A-Z]{3}$")
RE_FLIGHT = re.compile(r"^[A-Z0-9]{2,3}\d{1,4}[A-Z]?$")
CLASSES = {"Y", "C", "F", "W", ""}
KINDS = {"Duty", "Private"}


def _read(path):
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.reader(handle))


def _valid_row(row):
    if len(row) != 8:
        return False
    date_text, origin, destination, flight, cabin, duration, _, kind = row
    try:
        datetime.strptime(date_text.strip(), "%Y-%m-%d")
        minutes = int(duration.strip())
    except (ValueError, TypeError):
        return False
    return (RE_AIRPORT.fullmatch(origin.strip().upper()) is not None
            and RE_AIRPORT.fullmatch(destination.strip().upper()) is not None
            and RE_FLIGHT.fullmatch(
                re.sub(r"\s+", "", flight.strip().upper())) is not None
            and cabin.strip().upper() in CLASSES
            and minutes >= 0 and kind.strip() in KINDS)


def matches_csv(path):
    try:
        rows = _read(path)
    except (OSError, UnicodeError, csv.Error):
        return False
    return len(rows) >= 5 and all(_valid_row(row) for row in rows)


def parse_csv(path):
    rows = _read(path)
    if len(rows) < 5 or not all(_valid_row(row) for row in rows):
        raise ValueError("kein kompaktes AeroX-Flugverlaufs-CSV")

    legs = []
    private_skipped = zero_skipped = 0
    duty_minutes = 0
    duty_rows = 0
    for date_text, origin, destination, flight, cabin, duration, note, kind in rows:
        if kind.strip() == "Private":
            private_skipped += 1
            continue
        duty_rows += 1
        minutes = int(duration.strip())
        duty_minutes += minutes
        if minutes <= 0:
            zero_skipped += 1
            continue
        remarks = note.strip()
        if cabin.strip():
            remarks = (remarks + "; " if remarks else "") + \
                f"Reiseklasse {cabin.strip().upper()}"
        legs.append({
            "date": datetime.strptime(date_text.strip(), "%Y-%m-%d").date().isoformat(),
            "flight": re.sub(r"\s+", "", flight.strip().upper()),
            "from": origin.strip().upper(),
            "to": destination.strip().upper(),
            "block_min": minutes,
            "remarks": remarks or None,
        })

    parsed_minutes = sum(leg["block_min"] for leg in legs)
    if parsed_minutes != duty_minutes:
        # A zero-minute source row is allowed but must be explicit in the audit.
        if parsed_minutes + 0 != duty_minutes:
            raise ValueError(
                f"Duty-Minuten {parsed_minutes} != Quelle {duty_minutes}")
    if not legs:
        raise ValueError("CSV enthält keine geflogenen Duty-Zeilen")
    dates = sorted(leg["date"] for leg in legs)
    first, last = dates[0], dates[-1]
    return legs, [], {
        "filename": os.path.basename(path),
        "month": first[:7] if first[:7] == last[:7] else f"{first[:7]}–{last[:7]}",
        "source_rows": len(rows),
        "duty_rows": duty_rows,
        "legs": len(legs),
        "block_min": parsed_minutes,
        "private_rows_skipped": private_skipped,
        "zero_time_rows_skipped": zero_skipped,
        "control": "OK",
    }
