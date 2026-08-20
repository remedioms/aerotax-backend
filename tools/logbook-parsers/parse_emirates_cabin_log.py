#!/usr/bin/env python3
"""Emirates ``Cabin Crew Flight Log`` PDF -> AeroX logbook legs.

The export prints one complete operated leg per visual row.  Departure and
arrival are local airport timestamps; no document-wide block-time total is
printed.  The parser therefore uses a strict structural control instead:

* the exact title and all eight table headings must be present;
* every numbered page must be present exactly once and in order;
* every source line beginning with a date and Emirates flight number must
  match the complete row grammar;
* both airport timezones must be known and every reconstructed UTC duration
  must be positive and plausible.

This deliberately does not infer cabin flights from the user's profile.  A
SWISS profile can import an Emirates export, and an unrelated PDF with an
Emirates-looking filename must not match this parser.
"""

import os
import re
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pdfplumber


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from airport_tz import airport_tz  # noqa: E402


TITLE = "Emirates Cabin Crew Flight Log"
HEADINGS = ("Date", "Flight", "From", "To", "Departure", "Arrival",
            "Aircraft", "Registration")
DATE_TOKEN = r"\d{2} [A-Z][a-z]{2} \d{4}"
CLOCK_TOKEN = DATE_TOKEN + r" \d{2}:\d{2}"
ROW_START_RE = re.compile(
    rf"^\s*{DATE_TOKEN}\s+EK\d{{1,4}}[A-Z]?\b")
ROW_RE = re.compile(
    rf"^\s*(?P<date>{DATE_TOKEN})\s+"
    r"(?P<flight>EK\d{1,4}[A-Z]?)\s+"
    r"(?P<from>[A-Z]{3})\s+(?P<to>[A-Z]{3})\s+"
    rf"(?P<departure>{CLOCK_TOKEN})\s+"
    rf"(?P<arrival>{CLOCK_TOKEN})\s+"
    r"(?P<aircraft>.+?)\s+\((?P<type>[A-Z0-9]{4})\)"
    r"(?:\s+(?P<registration>A6-[A-Z0-9]{3}))?\s*$")
PAGE_RE = re.compile(r"(?m)^\s*Page\s+(\d+)\s*$")


def _text(page):
    return page.extract_text(x_tolerance=2, y_tolerance=3) or ""


def matches_pdf(path):
    try:
        with pdfplumber.open(path) as pdf:
            if not pdf.pages:
                return False
            first = _text(pdf.pages[0])
    except Exception:
        return False
    return (TITLE in first[:1200]
            and all(heading in first[:2000] for heading in HEADINGS))


def _parse_local(value, station):
    timezone_name = airport_tz(station)
    if not timezone_name:
        raise ValueError(
            f"Emirates cabin log: Zeitzone fuer {station} nicht aufloesbar")
    try:
        naive = datetime.strptime(value, "%d %b %Y %H:%M")
    except ValueError as exc:
        raise ValueError(
            f"Emirates cabin log: ungueltiger Zeitstempel {value!r}") from exc
    tz = ZoneInfo(timezone_name)
    first = naive.replace(tzinfo=tz, fold=0)
    second = naive.replace(tzinfo=tz, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ValueError(
            f"Emirates cabin log: lokale Zeit ist mehrdeutig: {station} {value}")
    # A nonexistent spring-forward wall clock does not round-trip unchanged.
    roundtrip = first.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)
    if roundtrip != naive:
        raise ValueError(
            f"Emirates cabin log: lokale Zeit existiert nicht: {station} {value}")
    return first


def _period(first, last):
    start, end = first[:7], last[:7]
    return start if start == end else f"{start}–{end}"


def parse_pdf(path):
    with pdfplumber.open(path) as pdf:
        if not pdf.pages:
            raise ValueError("Emirates cabin log: leeres PDF")
        page_texts = [_text(page) for page in pdf.pages]
        first = page_texts[0]
        if TITLE not in first[:1200] \
                or not all(heading in first[:2000] for heading in HEADINGS):
            raise ValueError("Emirates cabin log: Format-Signatur fehlt")

        legs = []
        seen = set()
        previous_departure = None
        source_rows = 0
        for page_number, text in enumerate(page_texts, 1):
            if not all(heading in text[:1800] for heading in HEADINGS):
                raise ValueError(
                    f"Emirates cabin log: Tabellenkopf fehlt auf Seite {page_number}")
            printed_pages = [int(value) for value in PAGE_RE.findall(text)]
            if printed_pages != [page_number]:
                raise ValueError(
                    "Emirates cabin log: Seitennummern unvollstaendig oder "
                    f"widerspruechlich auf Seite {page_number}")

            for line in text.splitlines():
                if not ROW_START_RE.match(line):
                    continue
                source_rows += 1
                match = ROW_RE.fullmatch(line)
                if not match:
                    raise ValueError(
                        "Emirates cabin log: Flugzeile ist unvollstaendig: "
                        f"Seite {page_number}, Zeile {source_rows}")
                row = match.groupdict()
                origin, destination = row["from"], row["to"]
                if origin == destination:
                    raise ValueError(
                        "Emirates cabin log: identische Start-/Zielstation")
                dep = _parse_local(row["departure"], origin)
                arr = _parse_local(row["arrival"], destination)
                printed_day = datetime.strptime(
                    row["date"], "%d %b %Y").date()
                if printed_day != dep.date():
                    raise ValueError(
                        "Emirates cabin log: Datum widerspricht Abflugdatum")
                dep_utc = dep.astimezone(timezone.utc)
                arr_utc = arr.astimezone(timezone.utc)
                seconds = (arr_utc - dep_utc).total_seconds()
                if not 0 < seconds <= 20 * 3600 or seconds % 60:
                    raise ValueError(
                        "Emirates cabin log: unplausible UTC-Blockzeit")
                if previous_departure and dep_utc < previous_departure:
                    raise ValueError(
                        "Emirates cabin log: Flugfolge ist nicht chronologisch")
                previous_departure = dep_utc
                key = (dep_utc, row["flight"], origin, destination)
                if key in seen:
                    raise ValueError(
                        "Emirates cabin log: doppelte Flugzeile in der Quelle")
                seen.add(key)
                leg = {
                    "date": printed_day.isoformat(),
                    "flight": row["flight"],
                    "from": origin,
                    "to": destination,
                    "dep_iso": dep_utc.strftime("%Y-%m-%dT%H:%M:00Z"),
                    "arr_iso": arr_utc.strftime("%Y-%m-%dT%H:%M:00Z"),
                    "block_min": int(seconds // 60),
                    "type": row["type"],
                    "role": "FB",
                }
                if row["registration"]:
                    leg["reg"] = row["registration"]
                legs.append(leg)

        if not legs or source_rows != len(legs):
            raise ValueError(
                "Emirates cabin log: keine oder nicht vollstaendig gelesene Legs")
        return legs, [], {
            "month": _period(legs[0]["date"], legs[-1]["date"]),
            "document_type": "emirates_cabin_crew_flight_log",
            "pages": len(page_texts),
            "source_rows": source_rows,
            "verified_rows": len(legs),
            "block_min": sum(leg["block_min"] for leg in legs),
            "control": "all_source_rows_and_pages_verified",
        }
