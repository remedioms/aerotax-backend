"""Deterministic Condor PDF adapters for the calendar upload endpoint."""

import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


_DUTY_CREATED = re.compile(
    r"Duty plan requested at\s+(\d{2})([A-Z]{3})(\d{2})\s+"
    r"(\d{1,2}):(\d{2})z")
_DUTY_PERIOD = re.compile(r"\b(0[1-9]|1[0-2])/(20\d{2})\b")
_DUTY_TOTAL = re.compile(
    r"BT\s+DH\s+(?:EQH|LSW)\s+BZW\s+Off claim\s+Off assigned[^\n]*\n"
    r"\s*(\d{1,3}):(\d{2})\b")
_DUTY_FLIGHT = re.compile(
    r"^P\s+"
    r"(?:(?P<weekday>Mo|Tu|We|Th|Fr|Sa|Su)\s+(?P<day>\d{1,2})\s+)?"
    r"(?P<flight>DE\d{3,4}[A-Z]?)\s+"
    r"(?P<type>[A-Z0-9]{3})\s+(?P<reg>[A-Z0-9]{5})\s+"
    r"(?P<pos>[A-Z]{2})\s+C\d+\s+"
    r"(?:(?:\d{1,2}:\d{2})\s+){0,2}"
    r"(?P<frm>[A-Z]{3})\s+(?P<start>\d{1,2}:\d{2})\s+-\s+"
    r"(?P<end>\d{1,2}:\d{2})\s+(?P<to>[A-Z]{3})\b")
_DAY = re.compile(r"^P\s+(Mo|Tu|We|Th|Fr|Sa|Su)\s+(\d{1,2})\s+(.*)$")


def _clock(day, hhmm, tzinfo):
    hour, minute = (int(part) for part in hhmm.split(':'))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tzinfo)


def _event(uid, start, end, summary, all_day=False, tzid=None):
    if all_day:
        return (uid, start, end, summary, True)
    if tzid:
        return (uid, start.replace(tzinfo=None),
                end.astimezone(timezone.utc).replace(tzinfo=None),
                summary, False, tzid)
    return (uid, start.astimezone(timezone.utc).replace(tzinfo=None),
            end.astimezone(timezone.utc).replace(tzinfo=None), summary, False)


def duty_plan_events(text):
    """Return ``(events, year, month, report, error)`` for CUBE Dutyplan."""
    source = text or ''
    if 'Duty plan requested at' not in source:
        return None, None, None, None, 'unsupported_pdf_format'
    utc_times = 'All times: UTC' in source
    local_times = 'All times: Local FRA' in source
    if not (utc_times or local_times):
        return None, None, None, None, 'unsupported_pdf_format'
    created = _DUTY_CREATED.search(source)
    period = _DUTY_PERIOD.search(source[:1500])
    total = _DUTY_TOTAL.search(source[:2000])
    if not created or not period or not total:
        return None, None, None, None, 'condor_control_missing'
    month, year = int(period.group(1)), int(period.group(2))
    clock_tz = timezone.utc if utc_times else ZoneInfo('Europe/Berlin')
    tzid = None if utc_times else 'Europe/Berlin'
    current_day = None
    events = []
    operating_block = 0

    for raw in source.splitlines():
        line = ' '.join(raw.split())
        day_match = _DAY.match(line)
        if day_match:
            current_day = date(year, month, int(day_match.group(2)))
            if current_day.strftime('%a')[:2] != day_match.group(1):
                return None, None, None, None, 'condor_weekday_mismatch'

        flight = _DUTY_FLIGHT.match(line)
        if flight:
            if flight.group('day'):
                current_day = date(year, month, int(flight.group('day')))
            if current_day is None:
                return None, None, None, None, 'condor_day_missing'
            start = _clock(current_day, flight.group('start'), clock_tz)
            end = _clock(current_day, flight.group('end'), clock_tz)
            if end <= start:
                end += timedelta(days=1)
            block = int((end - start).total_seconds() // 60)
            if not 0 < block < 1200:
                return None, None, None, None, 'condor_invalid_block'
            operating_block += block
            summary = (f"{flight.group('flight')} {flight.group('frm')} - "
                       f"{flight.group('to')}")
            events.append(_event(f'leg-{len(events)}', start, end, summary,
                                 tzid=tzid))
            continue

        if current_day is None:
            continue
        if day_match:
            rest = day_match.group(3).strip()
        else:
            continuation = re.match(r'^P\s+(.*)$', line)
            if not continuation:
                continue
            rest = continuation.group(1).strip()
        duty = rest.split()[0] if rest else ''
        if not duty or duty.startswith('DE'):
            continue
        # Only deadheads are meaningful undated continuation duties.  Other
        # ground duties need their own printed day anchor.
        if not day_match and not duty.startswith('DH/'):
            continue

        route = re.search(
            r'\b([A-Z]{3})\s+(\d{1,2}:\d{2})\s+-\s+'
            r'(\d{1,2}:\d{2})(?:\s+([A-Z]{3}))?\b', rest)
        if route:
            start = _clock(current_day, route.group(2), clock_tz)
            end = _clock(current_day, route.group(3), clock_tz)
            if end <= start:
                end += timedelta(days=1)
            destination = route.group(4) or route.group(1)
            summary = (f'{duty} {route.group(1)} - {destination}'
                       if destination != route.group(1)
                       else f'{duty} {route.group(1)}')
            events.append(_event(f'duty-{len(events)}', start, end, summary,
                                 tzid=tzid))
        else:
            label = 'Off Day' if duty in ('OFF', 'S_OFF', 'ORT') else duty
            events.append(_event(f'day-{len(events)}', current_day,
                                 current_day + timedelta(days=1), label,
                                 all_day=True))

    expected = int(total.group(1)) * 60 + int(total.group(2))
    if operating_block != expected:
        return None, None, None, None, 'condor_block_total_mismatch'
    if not events:
        return None, None, None, None, 'no_roster_days'
    return events, year, month, {
        'format': 'condor_duty_plan', 'operating_block_min': operating_block,
        'control': 'OK',
    }, None


def flight_hours_events(data, text):
    """Strict CFG statement facts plus documented all-day status rows."""
    source = text or ''
    if 'Flugstunden - Übersicht' not in source or 'Condor' not in source:
        return None, None, None, None, 'unsupported_pdf_format'
    tools = Path(__file__).resolve().parent / 'tools' / 'logbook-parsers'
    tools_s = str(tools)
    if tools_s not in sys.path:
        sys.path.insert(0, tools_s)
    try:
        import parse_cfg_flugstunden
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as handle:
            handle.write(data)
            path = handle.name
        try:
            legs, report = parse_cfg_flugstunden.parse_pdf(path)
        finally:
            os.unlink(path)
    except ValueError:
        return None, None, None, None, 'condor_statement_control_failed'
    except Exception:
        return None, None, None, None, 'pdf_extract_failed'

    year, month = (int(part) for part in report['month'].split('-'))
    events = []
    for leg in legs:
        start = datetime.fromisoformat(leg['dep_iso'].replace('Z', '+00:00'))
        end = datetime.fromisoformat(leg['arr_iso'].replace('Z', '+00:00'))
        summary = f"{leg['flight']} {leg['from']} - {leg['to']}"
        events.append(_event(f'leg-{len(events)}', start, end, summary))

    for deadhead in report.pop('_deadheads', []):
        start = datetime.fromisoformat(
            deadhead['dep_iso'].replace('Z', '+00:00'))
        end = datetime.fromisoformat(
            deadhead['arr_iso'].replace('Z', '+00:00'))
        summary = (f"DH {deadhead['flight']} {deadhead['from']} - "
                   f"{deadhead['to']}")
        events.append(_event(f'dh-{len(events)}', start, end, summary))

    seen_days = set()
    for raw in source.splitlines():
        line = ' '.join(raw.split())
        row = re.match(r'^(\d{2})\.(\d{2})\.\s+\S+\s+(.+)$', line)
        if not row or re.search(r'\d{2}:\d{2}\s*-\s*\d{2}:\d{2}', line):
            continue
        day, printed_month, label = int(row.group(1)), int(row.group(2)), row.group(3)
        if printed_month != month:
            continue
        marker = next((name for name in (
            'FREIER TAG', 'FREI (TEILZEIT)', 'URLAUB',
            'BEREITSCHAFT (STANDBY)', 'RESERVEDIENST') if name in label), None)
        if not marker or day in seen_days:
            continue
        seen_days.add(day)
        event_day = date(year, month, day)
        summary = ('Off Day' if marker.startswith('FREI') else
                   'Standby' if marker.startswith('BEREITSCHAFT') else
                   'Reserve' if marker == 'RESERVEDIENST' else marker.title())
        events.append(_event(f'day-{len(events)}', event_day,
                             event_day + timedelta(days=1), summary,
                             all_day=True))
    events.sort(key=lambda event: event[1].isoformat())
    return events, year, month, {
        'format': 'condor_flight_hours_statement', **report,
    }, None


def parse_condor_calendar(data, text):
    result = duty_plan_events(text)
    if result[-1] == 'unsupported_pdf_format':
        result = flight_hours_events(data, text)
    return result
