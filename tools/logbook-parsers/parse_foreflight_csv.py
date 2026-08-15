#!/usr/bin/env python3
"""Strict ForeFlight CSV export -> AeroX logbook facts.

ForeFlight's CSV export is a multi-table document: an aircraft table is
followed by a ``Flights Table`` marker and its own header.  It is therefore
not compatible with a normal ``csv.DictReader`` starting at row one.  This
parser deliberately accepts only that documented signature and verifies that
every positive flight/simulator minute in the source was represented.
"""

import csv
import os
from datetime import datetime


SIGNATURE = "ForeFlight Logbook Import"
FLIGHTS_MARKER = "Flights Table"
REQUIRED_FLIGHT_COLUMNS = {
    "Date", "AircraftID", "From", "To", "TotalTime", "Night",
    "SimulatedFlight", "Landing Full-Stop Day", "Landing Full-Stop Night",
}


def _cell(value):
    return str(value or "").strip()


def _duration_minutes(value):
    text = _cell(value)
    if not text:
        return 0
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError(f"ungültige ForeFlight-Dauer: {text!r}")
    try:
        hours, minutes = map(int, parts)
    except ValueError as exc:
        raise ValueError(f"ungültige ForeFlight-Dauer: {text!r}") from exc
    if hours < 0 or not 0 <= minutes < 60:
        raise ValueError(f"ungültige ForeFlight-Dauer: {text!r}")
    return hours * 60 + minutes


def _count(value):
    text = _cell(value)
    if not text:
        return 0
    try:
        number = int(float(text))
    except ValueError as exc:
        raise ValueError(f"ungültiger ForeFlight-Zähler: {text!r}") from exc
    if number < 0:
        raise ValueError(f"negativer ForeFlight-Zähler: {text!r}")
    return number


def _role(row):
    for column, role in (("PIC", "PIC"), ("PICUS", "PICUS"),
                         ("SIC", "SIC"), ("DualReceived", "DUAL")):
        if _duration_minutes(row.get(column)) > 0:
            return role
    return None


def _landing(row, modern, legacy):
    # New exports populate the EASA field; older rows only retain the deprecated
    # compatibility columns.  An explicit modern zero is authoritative.
    if _cell(row.get(modern)):
        return _count(row.get(modern))
    return _count(row.get(legacy))


def _read(path):
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.reader(handle))


def matches_csv(path):
    try:
        rows = _read(path)
    except (OSError, UnicodeError, csv.Error):
        return False
    if not rows or _cell(rows[0][0] if rows[0] else "") != SIGNATURE:
        return False
    return any(_cell(row[0] if row else "") == FLIGHTS_MARKER for row in rows)


def parse_csv(path):
    rows = _read(path)
    if not rows or _cell(rows[0][0] if rows[0] else "") != SIGNATURE:
        raise ValueError("kein ForeFlight-CSV-Kopf")

    aircraft = {}
    aircraft_header = None
    flight_header = None
    flight_start = None
    for index, row in enumerate(rows):
        first = _cell(row[0] if row else "")
        if first == "AircraftID" and "TypeCode" in row and aircraft_header is None:
            aircraft_header = [_cell(value) for value in row]
            continue
        if first == FLIGHTS_MARKER:
            if index + 1 >= len(rows):
                raise ValueError("ForeFlight Flights-Header fehlt")
            flight_header = [_cell(value) for value in rows[index + 1]]
            flight_start = index + 2
            break
        if aircraft_header and first:
            record = dict(zip(aircraft_header, row))
            aircraft[first.upper()] = {
                "type": _cell(record.get("TypeCode")) or None,
                "model": _cell(record.get("Model")) or None,
            }

    if flight_header is None or flight_start is None:
        raise ValueError("ForeFlight Flights-Tabelle fehlt")
    missing = sorted(REQUIRED_FLIGHT_COLUMNS - set(flight_header))
    if missing:
        raise ValueError("ForeFlight-Spalten fehlen: " + ", ".join(missing))

    legs, sims = [], []
    source_flight_min = source_sim_min = 0
    skipped_zero_rows = 0
    dates = []
    for number, values in enumerate(rows[flight_start:], start=flight_start + 1):
        if not any(_cell(value) for value in values):
            continue
        padded = values + [""] * max(0, len(flight_header) - len(values))
        row = dict(zip(flight_header, padded))
        date_text = _cell(row.get("Date"))
        if not date_text:
            continue
        try:
            day = datetime.strptime(date_text, "%Y-%m-%d").date().isoformat()
        except ValueError as exc:
            raise ValueError(f"ForeFlight-Zeile {number}: Datum {date_text!r}") from exc
        dates.append(day)

        total_min = _duration_minutes(row.get("TotalTime"))
        sim_min = _duration_minutes(row.get("SimulatedFlight"))
        source_flight_min += total_min
        source_sim_min += sim_min
        aircraft_id = _cell(row.get("AircraftID")).upper()

        if total_min > 0:
            origin = _cell(row.get("From")).upper()
            destination = _cell(row.get("To")).upper()
            if not origin or not destination:
                raise ValueError(
                    f"ForeFlight-Zeile {number}: Route für Flugzeit fehlt")
            info = aircraft.get(aircraft_id, {})
            leg = {
                "date": day,
                "flight": None,
                "from": origin,
                "to": destination,
                "block_min": total_min,
                "reg": aircraft_id or None,
                "type": info.get("type") or info.get("model"),
                "role": _role(row),
                "night_min": _duration_minutes(row.get("Night")) or None,
                "ldg_day": _landing(
                    row, "Landing Full-Stop Day", "DayLandingsFullStop"),
                "ldg_night": _landing(
                    row, "Landing Full-Stop Night", "NightLandingsFullStop"),
                "remarks": _cell(row.get("PilotComments"))[:500] or None,
            }
            legs.append({key: value for key, value in leg.items()
                         if value is not None})
        elif sim_min > 0:
            sims.append({
                "date": day,
                "code": aircraft_id or "FSTD",
                "duration_min": sim_min,
                "role": _role(row),
                "place": _cell(row.get("From")).upper() or None,
            })
        else:
            skipped_zero_rows += 1

    parsed_flight_min = sum(leg["block_min"] for leg in legs)
    parsed_sim_min = sum(sim["duration_min"] for sim in sims)
    if parsed_flight_min != source_flight_min:
        raise ValueError(
            f"ForeFlight-Flugzeit {parsed_flight_min} != Quelle {source_flight_min}")
    if parsed_sim_min != source_sim_min:
        raise ValueError(
            f"ForeFlight-Simulatorzeit {parsed_sim_min} != Quelle {source_sim_min}")
    if not legs and not sims:
        raise ValueError("ForeFlight-CSV enthält keine Flugbuchzeiten")

    first, last = min(dates), max(dates)
    return legs, sims, {
        "filename": os.path.basename(path),
        "month": first[:7] if first[:7] == last[:7] else f"{first[:7]}–{last[:7]}",
        "legs": len(legs),
        "sim_sessions": len(sims),
        "block_min": parsed_flight_min,
        "sim_min": parsed_sim_min,
        "zero_time_rows_skipped": skipped_zero_rows,
        "control": "OK",
    }
