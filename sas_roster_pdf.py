"""Deterministic parser for SAS Airside roster exports.

Airside prints one fully-qualified date per table row.  Flight STD/STA values
are airport-local wall times (a CPH-BOS row would otherwise have an impossible
block time), so every sector is converted with the timezone of its respective
station.  Actual times and PIC identifiers are deliberately ignored: a roster
calendar represents the published schedule, not the historic operation.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from airport_tz import airport_tz


_FORMAT_TITLE = 'Airside Roster Export'
_FORMAT_HEADER = 'Day Date Duty Meal Activity C/I From STD STA ATD ATA PIC To C/O Stop'
_ROW_RE = re.compile(
    r'^(?P<weekday>Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+'
    r'(?P<date>\d{2}[A-Z]{3}\d{2})\s+(?P<rest>.+)$')
_FLIGHT_RE = re.compile(r'^SK(?P<number>\d{1,4})(?P<suffix>[A-Z]?)$')
_IATA_RE = re.compile(r'^[A-Z]{3}$')
_TIME_RE = re.compile(r'^(?:[01]\d|2[0-3]):[0-5]\d$')
_GROUND_RE = re.compile(
    r'^(?P<activity>[A-Za-z][A-Za-z0-9_/-]*)\s+'
    r'(?P<start>(?:[01]\d|2[0-3]):[0-5]\d)\s+'
    r'(?P<end>(?:[01]\d|2[0-3]):[0-5]\d)'
    r'(?:\s+\d{1,3}:[0-5]\d)?$')
_OFF_RE = re.compile(r'^F(?:S|\d+)?$')


def _printed_date(value: str) -> Optional[date]:
    try:
        return datetime.strptime(value.title(), '%d%b%y').date()
    except (TypeError, ValueError):
        return None


def _clock(day: date, hhmm: str, tz: ZoneInfo) -> datetime:
    parsed = time.fromisoformat(hhmm)
    return datetime.combine(day, parsed, tzinfo=tz)


def _tz(iata: str) -> Optional[ZoneInfo]:
    name = airport_tz(iata)
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def _flight_number(token: str) -> str:
    match = _FLIGHT_RE.fullmatch(token or '')
    if not match:
        return token
    number = match.group('number').lstrip('0') or '0'
    return f"SK{number}{match.group('suffix')}"


def _flight_row(day: date, rest: str) -> Optional[Dict[str, object]]:
    tokens = rest.split()
    flight_index = next(
        (index for index, token in enumerate(tokens)
         if _FLIGHT_RE.fullmatch(token)),
        None,
    )
    if flight_index is None:
        return None
    if len(tokens) < flight_index + 5:
        return {'error': 'sas_malformed_flight_row'}

    flight, frm, std, sta = tokens[flight_index:flight_index + 4]
    if (not _IATA_RE.fullmatch(frm)
            or not _TIME_RE.fullmatch(std)
            or not _TIME_RE.fullmatch(sta)):
        return {'error': 'sas_malformed_flight_row'}
    tail = tokens[flight_index + 4:]
    to = next((token for token in tail if _IATA_RE.fullmatch(token)), None)
    if not to:
        return {'error': 'sas_malformed_flight_row'}
    return {
        'kind': 'flight', 'day': day, 'flight': _flight_number(flight),
        'from': frm, 'to': to, 'std': std, 'sta': sta,
    }


def _infer_homebase(rows: List[Dict[str, object]], hint: Optional[str]) -> Optional[str]:
    normalized_hint = str(hint or '').strip().upper()
    if _IATA_RE.fullmatch(normalized_hint) and _tz(normalized_hint):
        return normalized_hint
    stations = []
    first_departure = None
    for row in rows:
        if row.get('kind') != 'flight':
            continue
        first_departure = first_departure or str(row['from'])
        stations.extend((str(row['from']), str(row['to'])))
    if not stations:
        return None
    counts = Counter(stations)
    best_count = max(counts.values())
    tied = {station for station, count in counts.items() if count == best_count}
    if first_departure in tied:
        return first_departure
    return sorted(tied)[0]


def _sector_event(row: Dict[str, object], uid: str,
                  briefing: Optional[Tuple[date, str]]):
    frm, to = str(row['from']), str(row['to'])
    dep_tz, arr_tz = _tz(frm), _tz(to)
    if dep_tz is None or arr_tz is None:
        return None, 'sas_unknown_airport_timezone'

    dep = _clock(row['day'], str(row['std']), dep_tz)
    arr_day = row['day']
    arr = _clock(arr_day, str(row['sta']), arr_tz)
    for _ in range(3):
        if arr.astimezone(timezone.utc) > dep.astimezone(timezone.utc):
            break
        arr_day += timedelta(days=1)
        arr = _clock(arr_day, str(row['sta']), arr_tz)
    block = arr.astimezone(timezone.utc) - dep.astimezone(timezone.utc)
    if not timedelta(minutes=1) <= block <= timedelta(hours=20):
        return None, 'sas_invalid_block'

    base = f"{row['flight']} {frm} - {to}"
    summary = base
    if briefing is not None:
        briefing_day, briefing_hhmm = briefing
        if briefing_day == row['day']:
            summary = f'{briefing_hhmm} LT Briefing {frm} · {base}'
    event = (
        uid,
        dep.replace(tzinfo=None),
        arr.astimezone(timezone.utc).replace(tzinfo=None),
        summary,
        False,
        dep_tz.key,
    )
    return event, None


def _ground_event(row: Dict[str, object], uid: str, homebase: Optional[str]):
    activity = str(row['activity']).upper()
    start, end = str(row['start']), str(row['end'])
    if start == '00:00' and end in ('00:00', '23:59'):
        summary = ('Off Day' if _OFF_RE.fullmatch(activity)
                   else 'Urlaub' if activity == 'VA'
                   else activity)
        return (uid, row['day'], row['day'] + timedelta(days=1),
                summary, True), None

    if not homebase:
        return None, 'sas_homebase_missing'
    base_tz = _tz(homebase)
    if base_tz is None:
        return None, 'sas_unknown_airport_timezone'
    start_dt = _clock(row['day'], start, base_tz)
    end_dt = _clock(row['day'], end, base_tz)
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)
    summary = ('Reserve' if re.fullmatch(r'R\d+', activity)
               else 'Standby' if activity.startswith('SBY')
               else activity)
    return (
        uid,
        start_dt.replace(tzinfo=None),
        end_dt.astimezone(timezone.utc).replace(tzinfo=None),
        summary,
        False,
        base_tz.key,
    ), None


def parse_sas_airside_calendar(text: str, homebase: Optional[str] = None):
    """Return ``(events, year, month, report, error)`` for an Airside PDF.

    Every dated row must be understood.  A changed column layout therefore
    enters review instead of silently dropping duties or assigning a wrong
    station/date.
    """
    source = text or ''
    if (_FORMAT_TITLE not in source[:1000]
            or _FORMAT_HEADER not in source
            or 'airside@sas.se' not in source.lower()):
        return None, None, None, None, 'unsupported_pdf_format'

    rows: List[Dict[str, object]] = []
    previous_day = None
    for raw in source.splitlines():
        line = ' '.join(raw.split())
        match = _ROW_RE.match(line)
        if not match:
            continue
        row_day = _printed_date(match.group('date'))
        if row_day is None or row_day.strftime('%a') != match.group('weekday'):
            return None, None, None, None, 'sas_weekday_mismatch'
        if previous_day is not None and row_day < previous_day:
            return None, None, None, None, 'sas_non_monotonic_date'
        previous_day = row_day
        rest = match.group('rest').strip()

        flight = _flight_row(row_day, rest)
        if flight is not None:
            if flight.get('error'):
                return None, None, None, None, flight['error']
            rows.append(flight)
            continue

        parts = rest.split()
        if parts and parts[0].lower() in ('checkin', 'checkout'):
            if len(parts) < 2 or not _TIME_RE.fullmatch(parts[1]):
                return None, None, None, None, 'sas_malformed_check_row'
            rows.append({
                'kind': parts[0].lower(), 'day': row_day, 'time': parts[1],
            })
            continue

        ground = _GROUND_RE.fullmatch(rest)
        if ground:
            rows.append({
                'kind': 'ground', 'day': row_day,
                'activity': ground.group('activity'),
                'start': ground.group('start'), 'end': ground.group('end'),
            })
            continue
        return None, None, None, None, 'sas_unparsed_row'

    if not rows:
        return None, None, None, None, 'no_roster_days'
    inferred_homebase = _infer_homebase(rows, homebase)
    events = []
    pending_briefing = None
    flight_count = marker_count = 0
    for index, row in enumerate(rows):
        kind = row['kind']
        if kind == 'checkin':
            pending_briefing = (row['day'], str(row['time']))
            continue
        if kind == 'checkout':
            pending_briefing = None
            continue
        if kind == 'flight':
            event, error = _sector_event(
                row, f'leg-{index}', pending_briefing)
            pending_briefing = None
            flight_count += 1
        else:
            event, error = _ground_event(
                row, f'duty-{index}', inferred_homebase)
            marker_count += 1
        if error:
            return None, None, None, None, error
        events.append(event)

    if not events:
        return None, None, None, None, 'no_roster_days'
    dated_rows = [row['day'] for row in rows]
    first_day, last_day = min(dated_rows), max(dated_rows)
    return events, first_day.year, first_day.month, {
        'format': 'sas_airside_roster',
        'period': f'{first_day.isoformat()}..{last_day.isoformat()}',
        'homebase': inferred_homebase,
        'rows': len(rows),
        'flight_count': flight_count,
        'marker_count': marker_count,
    }, None
