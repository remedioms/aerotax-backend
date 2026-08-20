#!/usr/bin/env python3
"""SWISS ``Historical published roster`` PDF -> AeroX logbook legs.

This is deliberately a narrow parser for the CrewLink/Jeppesen document seen
in production upload #673.  The source prints departure and arrival clocks in
the respective airport's local time and prints a document-wide monthly flight
time.  We therefore accept a document only when every airport timezone is
known and the reconstructed UTC block minutes equal that printed total.

Deadheads (``DH``) are never logbook flying.  Overnight long-haul rows may be
split over two printed calendar rows: the first contains flight/origin/off,
the second destination/on.  The explicit second date is retained rather than
guessed from clock order.
"""

import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pdfplumber


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from airport_tz import airport_tz  # noqa: E402


HEADER = "Historical published roster"
TABLE_MARKERS = ("Date", "Report", "Pos", "Activity", "From", "To",
                 "Dep", "Arr", "A/C", "Flt", "hrs")
PERIOD_RE = re.compile(r"(?im)^\s*Period:\s*([A-Za-z]{3,9})\s+(20\d{2})\s*$")
TOTAL_RE = re.compile(
    r"Total\s+flight\s+time\s+in\s+([A-Za-z]{3,9})\s*:\s*(\d{1,3}):(\d{2})",
    re.IGNORECASE)
CREATED_RE = re.compile(
    r"Created\s+(\d{1,2})([A-Za-z]{3})(20\d{2})\s+"
    r"(\d{1,2}):(\d{2})\s+\(([A-Z]{3})\)\s+by\b",
    re.IGNORECASE)
FLIGHT_RE = re.compile(r"^LX\d{1,4}[A-Z]?$", re.IGNORECASE)
CLOCK_RE = re.compile(r"^(?:[01]?\d|2[0-3]):[0-5]\d$")
IATA_RE = re.compile(r"^[A-Z]{3}$")

MONTHS = {
    name: number for number, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun",
         "jul", "aug", "sep", "oct", "nov", "dec"), 1)
}


def _month(token):
    try:
        return MONTHS[token[:3].lower()]
    except (KeyError, TypeError):
        raise ValueError(f"SWISS roster: unbekannter Monat {token!r}")


def _document_text(pdf):
    return "\n".join(page.extract_text(x_tolerance=2, y_tolerance=3) or ""
                     for page in pdf.pages)


def matches_pdf(path):
    try:
        with pdfplumber.open(path) as pdf:
            text = _document_text(pdf)
    except Exception:
        return False
    return (HEADER.lower() in text[:1000].lower()
            and "All times local" in text[:2000]
            and PERIOD_RE.search(text) is not None
            and TOTAL_RE.search(text) is not None)


def _group_words(page):
    """Return visual rows, preserving the table's x-position semantics."""
    groups = []
    for word in sorted(page.extract_words(x_tolerance=1, y_tolerance=2),
                       key=lambda item: (item["top"], item["x0"])):
        if not groups or abs(groups[-1][0] - word["top"]) > 2.1:
            groups.append([word["top"], [word]])
        else:
            groups[-1][1].append(word)
    return [sorted(words, key=lambda item: item["x0"])
            for _, words in groups]


def _header_positions(rows):
    for words in rows:
        by_text = {word["text"]: float(word["x0"]) for word in words}
        if all(marker in by_text for marker in TABLE_MARKERS):
            return {
                "date": by_text["Date"], "report": by_text["Report"],
                "pos": by_text["Pos"], "activity": by_text["Activity"],
                "from": by_text["From"], "to": by_text["To"],
                "dep": by_text["Dep"], "arr": by_text["Arr"],
                "type": by_text["A/C"], "after_type": by_text.get("Layover", 10**9),
            }
    raise ValueError("SWISS roster: Tabellenkopf fehlt")


def _cells(words, columns):
    ordered = [(name, x) for name, x in columns.items() if name != "after_type"]
    ordered.sort(key=lambda item: item[1])
    out = {name: [] for name, _ in ordered}
    for word in words:
        x = float(word["x0"])
        if x >= columns["after_type"]:
            continue
        candidates = [item for item in ordered if item[1] <= x + 1.0]
        if not candidates:
            continue
        out[candidates[-1][0]].append(word["text"])
    return {key: " ".join(value).strip() for key, value in out.items()}


def _clock(local_day, value, station):
    tz_name = airport_tz(station)
    if not tz_name:
        raise ValueError(f"SWISS roster: Zeitzone fuer {station} nicht aufloesbar")
    hour, minute = (int(part) for part in value.split(":"))
    return datetime(local_day.year, local_day.month, local_day.day,
                    hour, minute, tzinfo=ZoneInfo(tz_name))


def _arrival(dep, dep_day, arr_clock, station, explicit_day=None):
    if explicit_day is not None:
        candidates = [_clock(explicit_day, arr_clock, station)]
    else:
        candidates = [_clock(dep_day + timedelta(days=offset), arr_clock, station)
                      for offset in (0, 1)]
    valid = [candidate for candidate in candidates
             if 0 < (candidate.astimezone(timezone.utc)
                     - dep.astimezone(timezone.utc)).total_seconds() < 20 * 3600]
    if len(valid) != 1:
        raise ValueError("SWISS roster: lokale Ankunft nicht eindeutig")
    return valid[0]


def _role(position):
    pos = (position or "").upper()
    if pos in {"CP", "CPT", "CMD"}:
        return "PIC"
    if pos in {"FO", "SFO"}:
        return pos
    if pos in {"MC", "FA", "PU", "CC"}:
        return "FB"
    return pos or "FB"


def _leg(pending, destination, arr_clock, arr_day):
    origin = pending["from"]
    dep = _clock(pending["day"], pending["dep"], origin)
    arr = _arrival(dep, pending["day"], arr_clock, destination,
                   explicit_day=arr_day)
    block = int((arr.astimezone(timezone.utc)
                 - dep.astimezone(timezone.utc)).total_seconds() // 60)
    return {
        "date": pending["day"].isoformat(),
        "flight": pending["flight"].upper(),
        "from": origin,
        "to": destination,
        "dep_iso": dep.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z"),
        "arr_iso": arr.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z"),
        "block_min": block,
        "type": pending["type"].upper(),
        "role": _role(pending["pos"]),
    }


def parse_pdf(path):
    with pdfplumber.open(path) as pdf:
        text = _document_text(pdf)
        if HEADER.lower() not in text[:1000].lower() or "All times local" not in text[:2000]:
            raise ValueError("SWISS roster: Format-Signatur fehlt")
        period_match = PERIOD_RE.search(text)
        total_match = TOTAL_RE.search(text)
        created_match = CREATED_RE.search(text)
        if not period_match or not total_match or not created_match:
            raise ValueError("SWISS roster: Zeitraum, Summe oder Erstellzeit fehlt")
        year = int(period_match.group(2))
        month = _month(period_match.group(1))
        if _month(total_match.group(1)) != month:
            raise ValueError("SWISS roster: Summenmonat widerspricht Zeitraum")
        expected = int(total_match.group(2)) * 60 + int(total_match.group(3))
        if int(total_match.group(3)) >= 60:
            raise ValueError("SWISS roster: ungueltige Monatssumme")

        created = datetime(
            int(created_match.group(3)), _month(created_match.group(2)),
            int(created_match.group(1)), int(created_match.group(4)),
            int(created_match.group(5)),
            tzinfo=ZoneInfo(airport_tz(created_match.group(6)) or "UTC"))
        if created.date() < date(year, month, 1):
            raise ValueError("SWISS roster: Erstellzeit liegt vor dem Zeitraum")

        legs = []
        deadheads = 0
        pending = None
        current_day = None
        for page in pdf.pages:
            rows = _group_words(page)
            columns = _header_positions(rows)
            for words in rows:
                cell = _cells(words, columns)
                day_match = re.match(r"^(\d{1,2})\b", cell.get("date", ""))
                if day_match:
                    try:
                        current_day = date(year, month, int(day_match.group(1)))
                    except ValueError as ex:
                        raise ValueError("SWISS roster: ungueltiges Tagesdatum") from ex

                activity = cell.get("activity", "").replace(" ", "").upper()
                pos = cell.get("pos", "").upper()
                origin = cell.get("from", "").upper()
                destination = cell.get("to", "").upper()
                dep_clock = cell.get("dep", "")
                arr_clock = cell.get("arr", "")
                aircraft = cell.get("type", "").upper()

                # Complete the previous overnight row before considering a
                # new activity on this visual row.
                if pending and not activity and IATA_RE.fullmatch(destination) \
                        and CLOCK_RE.fullmatch(arr_clock) and current_day:
                    if pending["pos"] != pos:
                        raise ValueError("SWISS roster: Overnight-Position widerspricht")
                    if not pending["deadhead"]:
                        legs.append(_leg(pending, destination, arr_clock, current_day))
                    pending = None
                    continue

                if not FLIGHT_RE.fullmatch(activity):
                    continue
                if pending:
                    raise ValueError("SWISS roster: unvollstaendiges Overnight-Leg")
                if not current_day or not IATA_RE.fullmatch(origin) \
                        or not CLOCK_RE.fullmatch(dep_clock) or not aircraft:
                    raise ValueError(f"SWISS roster: unvollstaendige Flugzeile {activity}")

                is_deadhead = pos == "DH"
                if is_deadhead:
                    deadheads += 1
                base = {"day": current_day, "pos": pos, "flight": activity,
                        "from": origin, "dep": dep_clock, "type": aircraft,
                        "deadhead": is_deadhead}
                if IATA_RE.fullmatch(destination) and CLOCK_RE.fullmatch(arr_clock):
                    if not is_deadhead:
                        dep = _clock(current_day, dep_clock, origin)
                        arr = _arrival(dep, current_day, arr_clock, destination)
                        legs.append(_leg(base, destination, arr_clock,
                                         arr.astimezone(ZoneInfo(
                                             airport_tz(destination))).date()))
                else:
                    pending = base

        if pending:
            raise ValueError("SWISS roster: Overnight-Leg endet ohne Ankunft")
        if not legs:
            raise ValueError("SWISS roster: keine operativen Legs")
        parsed = sum(leg["block_min"] for leg in legs)
        if parsed != expected:
            raise ValueError(
                f"SWISS roster: Flugzeit-Summe weicht ab: Quelle={expected} Parser={parsed}")

        legs.sort(key=lambda leg: (leg["dep_iso"], leg["flight"]))
        return legs, [], {
            "month": f"{year:04d}-{month:02d}",
            "created_at": created.astimezone(timezone.utc).isoformat(),
            "verified_source_block_total": expected,
            "block_min": parsed,
            "deadheads_skipped": deadheads,
            "legs": len(legs),
        }

