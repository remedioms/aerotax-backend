#!/usr/bin/env python3
"""FlightLog ``Pilot Logbook`` EASA PDF -> verified AeroX facts.

FlightLog prints one complete 26-column EASA table per landscape page.  The
format has no flight-number column and does not state a timezone, so neither
fact is invented.  Every dated row must instead be a fully timed flight or a
simulator session.  Page totals, previous-page carry values and cumulative
totals are reconciled for every printed duration and movement counter.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime

import pdfplumber

from legkeys import dedupe_keys


TABLE_SETTINGS = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
}
DATE_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{2})$")
DURATION_RE = re.compile(r"^(\d{1,5}):(\d{2})$")
CLOCK_RE = re.compile(r"^(\d{2}):(\d{2})$")
AIRPORT_RE = re.compile(r"^[A-Z0-9]{3,4}$")
PAGE_RE = re.compile(r"\bPage\s+(\d+)\s*/\s*(\d+)\b", re.IGNORECASE)
COVER_RANGE_RE = re.compile(
    r"Flights from\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\s+"
    r"through\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})",
    re.IGNORECASE,
)

COLUMNS = (
    "date", "dep", "dep_time", "arr", "arr_time", "type", "reg",
    "single_se", "single_me", "multi", "helicopter", "total",
    "pic_name", "to_day", "to_night", "ldg_day", "ldg_night", "pic",
    "sic", "dual", "instructor", "night", "actual_instrument",
    "sim_type", "sim_time", "remarks",
)
DURATION_COLUMNS = (
    "single_se", "single_me", "multi", "helicopter", "total", "pic",
    "sic", "dual", "instructor", "night", "actual_instrument",
    "sim_time",
)
COUNT_COLUMNS = ("to_day", "to_night", "ldg_day", "ldg_night")
CONTROL_COLUMNS = DURATION_COLUMNS + COUNT_COLUMNS


def _clean(value):
    return " ".join(str(value or "").split()).strip()


def _squeezed(value):
    return re.sub(r"\s+", "", str(value or "")).upper()


def _row(values):
    if len(values) != len(COLUMNS):
        raise ValueError("FlightLog table column count changed")
    return {key: _clean(value) for key, value in zip(COLUMNS, values)}


def _duration(value):
    raw = _clean(value)
    if not raw:
        return 0
    match = DURATION_RE.fullmatch(raw)
    if not match or int(match.group(2)) > 59:
        raise ValueError("invalid FlightLog duration")
    return int(match.group(1)) * 60 + int(match.group(2))


def _count(value):
    raw = _clean(value)
    if not raw:
        return 0
    if not raw.isdigit():
        raise ValueError("invalid FlightLog movement count")
    return int(raw)


def _clock(value):
    match = CLOCK_RE.fullmatch(_clean(value))
    if not match:
        raise ValueError("invalid FlightLog clock")
    hour, minute = map(int, match.groups())
    if hour > 23 or minute > 59:
        raise ValueError("invalid FlightLog clock")
    return hour * 60 + minute


def _date(value):
    match = DATE_RE.fullmatch(_clean(value))
    if not match:
        return None
    day, month, year = map(int, match.groups())
    year += 2000 if year < 70 else 1900
    return date(year, month, day)


def _table(page):
    tables = page.extract_tables(table_settings=TABLE_SETTINGS) or []
    if len(tables) != 1:
        raise ValueError("FlightLog page table missing or ambiguous")
    table = tables[0]
    if len(table) < 6:
        raise ValueError("FlightLog page table is incomplete")
    if any(len(row) != len(COLUMNS) for row in table):
        raise ValueError("FlightLog table column count changed")
    first = [_squeezed(value) for value in table[0]]
    second = [_squeezed(value) for value in table[1]]
    expected_first = {
        0: "DATE", 1: "DEPARTURE", 3: "ARRIVAL", 5: "AIRCRAFT",
        7: "SINGLEPILOTTIME", 9: "MULTIPILOTTIME", 10: "HELICOPTER",
        11: "TOTALTIME", 12: "NAMEPIC", 13: "T/O", 15: "LNDGS",
        17: "PILOTFUNCTIONTIME", 21: "CONDITIONOFFLIGHT",
        23: "SIMULATOR", 25: "REMARKSANDENDORSEMENTS",
    }
    expected_second = {
        1: "PLACE", 2: "TIME", 3: "PLACE", 4: "TIME", 5: "MODEL",
        6: "REG.", 7: "SE", 8: "ME", 13: "DAY", 14: "NIGHT",
        15: "DAY", 16: "NIGHT", 17: "PIC", 18: "SIC", 19: "DUAL",
        20: "INS", 21: "NIGHT", 22: "ACTUALINST", 23: "TYPE",
        24: "TIME",
    }
    if (any(first[index] != value for index, value in expected_first.items())
            or any(second[index] != value
                   for index, value in expected_second.items())):
        raise ValueError("FlightLog table header changed")
    return table


def _metric_values(row):
    return {
        key: (_duration(row[key]) if key in DURATION_COLUMNS
              else _count(row[key]))
        for key in CONTROL_COLUMNS
    }


def _assert_metrics(actual, expected, message):
    differences = [key for key in CONTROL_COLUMNS
                   if int(actual.get(key) or 0)
                   != int(expected.get(key) or 0)]
    if differences:
        raise ValueError(
            f"FlightLog {message}: {','.join(differences)}")


def matches_pdf(path):
    """Match only FlightLog's lined 26-column EASA export."""
    try:
        with pdfplumber.open(path) as pdf:
            if len(pdf.pages) < 2:
                return False
            cover = _squeezed(pdf.pages[0].extract_text() or "")
            if "FLIGHTLOG" not in cover or "PILOTLOGBOOK" not in cover:
                return False
            _table(pdf.pages[1])
            return True
    except Exception:
        return False


def parse_pdf(path):
    legs, sims = [], []
    running = {key: 0 for key in CONTROL_COLUMNS}
    page_reports = []
    dated_rows = 0

    with pdfplumber.open(path) as pdf:
        if len(pdf.pages) < 2:
            raise ValueError("FlightLog pages missing")
        cover_text = pdf.pages[0].extract_text() or ""
        cover = _squeezed(cover_text)
        if "FLIGHTLOG" not in cover or "PILOTLOGBOOK" not in cover:
            raise ValueError("FlightLog cover missing")
        range_match = COVER_RANGE_RE.search(cover_text)
        if not range_match:
            raise ValueError("FlightLog cover date range missing")
        cover_start = datetime.strptime(
            range_match.group(1), "%d %b %Y").date()
        cover_end = datetime.strptime(
            range_match.group(2), "%d %b %Y").date()

        expected_pages = len(pdf.pages) - 1
        for page_index, page in enumerate(pdf.pages[1:], 1):
            page_match = PAGE_RE.search(page.extract_text() or "")
            if (not page_match or int(page_match.group(1)) != page_index
                    or int(page_match.group(2)) != expected_pages):
                raise ValueError("FlightLog page sequence mismatch")
            table = _table(page)
            page_metrics = {key: 0 for key in CONTROL_COLUMNS}
            footers = {}

            for raw in table[2:]:
                item = _row(raw)
                day = _date(item["date"])
                if day is None:
                    label = item["date"].upper()
                    if label in (
                            "TOTAL THIS PAGE", "TOTAL PREVIOUS PAGES",
                            "TOTAL"):
                        if label in footers:
                            raise ValueError("duplicate FlightLog footer")
                        footers[label] = _metric_values(item)
                    elif any(item.values()):
                        raise ValueError("unknown FlightLog table row")
                    continue

                dated_rows += 1
                metrics = _metric_values(item)
                for key, value in metrics.items():
                    page_metrics[key] += value
                total = metrics["total"]
                sim_min = metrics["sim_time"]
                if bool(total) == bool(sim_min):
                    raise ValueError(
                        "FlightLog row is neither one flight nor one simulator")
                day_iso = day.isoformat()

                if sim_min:
                    if any(item[key] for key in (
                            "dep", "dep_time", "arr", "arr_time")):
                        raise ValueError("FlightLog simulator has flight route")
                    sims.append({
                        "date": day_iso,
                        "duration_min": sim_min,
                        "code": item["sim_type"] or item["type"] or "FSTD",
                        "_source_format": "flightlog_easa",
                    })
                    continue

                dep, arr = item["dep"].upper(), item["arr"].upper()
                if (not AIRPORT_RE.fullmatch(dep)
                        or not AIRPORT_RE.fullmatch(arr)):
                    raise ValueError("FlightLog flight route missing")
                departure = _clock(item["dep_time"])
                arrival = _clock(item["arr_time"])
                elapsed = (arrival - departure) % (24 * 60)
                if elapsed != total:
                    raise ValueError("FlightLog clock/total mismatch")
                leg = {
                    "date": day_iso, "from": dep, "to": arr,
                    "block_min": total,
                    "_source_format": "flightlog_easa",
                }
                if item["type"]:
                    leg["type"] = item["type"].replace(" ", "").upper()
                if item["reg"]:
                    leg["reg"] = item["reg"].replace(" ", "").upper()
                for key in ("to_day", "to_night", "ldg_day", "ldg_night"):
                    if metrics[key]:
                        leg[key] = metrics[key]
                if metrics["night"]:
                    if metrics["night"] > total:
                        raise ValueError("FlightLog night exceeds total")
                    leg["night_min"] = metrics["night"]
                if metrics["actual_instrument"]:
                    if metrics["actual_instrument"] > total:
                        raise ValueError(
                            "FlightLog instrument time exceeds total")
                    leg["ifr_min"] = metrics["actual_instrument"]
                for key, role in (
                        ("pic", "PIC"), ("sic", "FO"),
                        ("dual", "DUAL"), ("instructor", "FI")):
                    if metrics[key]:
                        leg["role"] = role
                        break
                if item["remarks"]:
                    leg["remarks"] = item["remarks"][:500]
                legs.append(leg)

            required = {
                "TOTAL THIS PAGE", "TOTAL PREVIOUS PAGES", "TOTAL"}
            if set(footers) != required:
                raise ValueError("FlightLog page controls missing")
            _assert_metrics(
                footers["TOTAL THIS PAGE"], page_metrics,
                "page-total mismatch")
            _assert_metrics(
                footers["TOTAL PREVIOUS PAGES"], running,
                "previous-total mismatch")
            for key, value in page_metrics.items():
                running[key] += value
            _assert_metrics(
                footers["TOTAL"], running,
                "cumulative-total mismatch")
            page_reports.append({
                "page": page_index,
                "block_min": page_metrics["total"],
                "sim_min": page_metrics["sim_time"],
            })

    if not dated_rows or not legs:
        raise ValueError("FlightLog contains no flight facts")
    legs.sort(key=lambda leg: (leg["date"], leg.get("from") or "",
                               leg.get("to") or ""))
    sims.sort(key=lambda sim: (sim["date"], sim.get("code") or ""))
    fact_days = ([date.fromisoformat(leg["date"]) for leg in legs]
                 + [date.fromisoformat(sim["date"]) for sim in sims])
    if min(fact_days) != cover_start:
        raise ValueError("FlightLog first date differs from cover")
    last_fact = max(fact_days)
    if last_fact != cover_end:
        raise ValueError("FlightLog last date differs from cover")
    collisions = dedupe_keys(legs)
    return legs, sims, {
        "parser": "parse_flightlog_easa.py",
        "format": "flightlog_pilot_logbook_easa",
        "month": f"{min(fact_days).isoformat()[:7]}–{last_fact.isoformat()[:7]}",
        "control": "OK",
        "rows": dated_rows,
        "pages": len(page_reports),
        "dedupe_suffixes": collisions,
        "totals": {
            "legs": len(legs),
            "block_min": running["total"],
            "landings": running["ldg_day"] + running["ldg_night"],
            "night_min": running["night"],
            "instrument_min": running["actual_instrument"],
            "sim_sessions": len(sims),
            "sim_min": running["sim_time"],
        },
    }
