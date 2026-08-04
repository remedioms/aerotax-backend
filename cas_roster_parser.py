"""Deterministic calendar parser for Lufthansa CAS roster PDFs.

The legacy :mod:`cas_table_parser` is intentionally tailored to tax-document
cross checks.  This module consumes the same coordinate-reconstructed rows but
builds calendar events.  It deliberately stays independent from Flask so the
date and revision rules can be regression-tested without private PDF fixtures.
"""
from __future__ import annotations

import calendar
import hashlib
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from cas_table_parser import _lines_from_pdf


_WEEKDAY_INDEX = {
    'Mo': 0, 'Di': 1, 'Mi': 2, 'Do': 3, 'Fr': 4, 'Sa': 5, 'So': 6,
}
_MONTHS = {
    'JAN': 1, 'FEB': 2, 'MAR': 3, 'MÄR': 3, 'APR': 4, 'MAY': 5,
    'MAI': 5, 'JUN': 6, 'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10,
    'OKT': 10, 'NOV': 11, 'DEC': 12, 'DEZ': 12,
}
_DAY_RE = re.compile(r'^(Mo|Di|Mi|Do|Fr|Sa|So)\s+(\d{1,2})\b\s*(.*)$')
_PERIOD_RE = re.compile(
    r'\bMonat\s*:?\s*([A-ZÄÖÜ]{3})(?:\s*-\s*([A-ZÄÖÜ]{3}))?\s+(20\d{2})\b',
    re.I,
)
_MONTH_RANGE_RE = re.compile(
    r'\b([A-ZÄÖÜ]{3})\s*-\s*([A-ZÄÖÜ]{3})\s+(20\d{2})\b', re.I)
_MONTH_YEAR_RE = re.compile(r'\b([A-ZÄÖÜ]{3})\s+(20\d{2})\b', re.I)
_PRINT_RE = re.compile(
    r'\bDruckdatum\s*:?\s*(\d{1,2})\s+([A-ZÄÖÜ]{3})\s+(20\d{2})\s+'
    r'(\d{1,2}):(\d{2})\b',
    re.I,
)
_PRINT_FALLBACK_RE = re.compile(
    r'\b(\d{1,2})\s+([A-ZÄÖÜ]{3})\s+(20\d{2})\s+'
    r'(\d{1,2}):(\d{2})\b', re.I)
_BRIEF_RE = re.compile(
    r'Briefingzeit\s*\(LT\s+([A-Z]{3})\)\s*:\s*'
    r'(\d{1,2})/(\d{1,2})/(\d{2,4})\s+(\d{1,2}:\d{2})',
    re.I,
)
_FLIGHT_TOKEN_RE = re.compile(r'^LH(\d{2,4})(?:-\d+)?$', re.I)
_PAIR_RE = re.compile(r'^([0-2]\d:[0-5]\d)-([0-2]\d:[0-5]\d)?$')
_ARR_ONLY_RE = re.compile(r'^-([0-2]\d:[0-5]\d)$')
_IATA_RE = re.compile(r'^[A-Z]{3}$')

_NON_STATIONS = {
    'ALL', 'BZW', 'CAS', 'EUO', 'EUR', 'EURO', 'FB', 'FBA', 'FDZ',
    'FZM', 'LAW', 'LTG', 'MAX', 'MTV', 'NTF', 'OAC',
    'OCP', 'OFF', 'OSF', 'PUB', 'RTK', 'UTC',
} | set(_MONTHS)

_EMPTY_MARKERS = {'', '-', '--', '---', '=', '==', 'VERTTRAULICH'}
_NON_ACTIVITY_CODES = {
    'MO', 'DI', 'MI', 'DO', 'FR', 'SA', 'SO',
    'KEINE', 'WEITEREN', 'EINSÄTZE', 'GEPLANT',
}
_MARKER_LABELS = {
    'OFF': 'Off Day',
    'U': 'Urlaub',
    'RES': 'Reserve',
    'SB_F': 'Standby',
    'SB_M': 'Standby',
}


def _month_after(year: int, month: int) -> Tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _month_before(year: int, month: int) -> Tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _parse_period(rows: Sequence[Tuple[int, str]]) -> Optional[Dict[str, Any]]:
    joined = ' '.join(line for _, line in rows[:20])
    match = _PERIOD_RE.search(joined) or _MONTH_RANGE_RE.search(joined)
    if match:
        start_name = match.group(1).upper()
        end_name = (match.group(2) or match.group(1)).upper()
        year_text = match.group(3)
        explicit_range = bool(match.group(2))
    else:
        single = next(
            (candidate for candidate in _MONTH_YEAR_RE.finditer(joined)
             if candidate.group(1).upper() in _MONTHS),
            None,
        )
        if not single:
            return None
        start_name = end_name = single.group(1).upper()
        year_text = single.group(2)
        explicit_range = False
    start_month = _MONTHS.get(start_name)
    end_month = _MONTHS.get(end_name)
    if not start_month or not end_month:
        return None
    start_year = int(year_text)
    end_year = start_year + (1 if end_month < start_month else 0)
    return {
        'start_year': start_year,
        'start_month': start_month,
        'end_year': end_year,
        'end_month': end_month,
        'label': (f'{start_name}-{end_name} {start_year}'
                  if explicit_range else f'{start_name} {start_year}'),
    }


def _parse_printed_at(rows: Sequence[Tuple[int, str]]) -> Optional[datetime]:
    joined = ' '.join(line for _, line in rows[:20])
    match = _PRINT_RE.search(joined) or _PRINT_FALLBACK_RE.search(joined)
    if not match:
        return None
    month = _MONTHS.get(match.group(2).upper())
    if not month:
        return None
    try:
        return datetime(
            int(match.group(3)), month, int(match.group(1)),
            int(match.group(4)), int(match.group(5)),
        )
    except ValueError:
        return None


def _parse_homebase(rows: Sequence[Tuple[int, str]]) -> str:
    header = ' '.join(line for _, line in rows[:30])
    match = re.search(r'\bHomebase\s+([A-Z]{3})\b', header, re.I)
    if not match:
        match = re.search(r'\bLIN\s+([A-Z]{3})\b', header)
    return match.group(1).upper() if match else 'FRA'


def _candidate_dates(period: Dict[str, Any]) -> List[date]:
    sy, sm = period['start_year'], period['start_month']
    ey, em = period['end_year'], period['end_month']
    py, pm = _month_before(sy, sm)
    ny, nm = _month_after(ey, em)
    first = date(py, pm, max(1, calendar.monthrange(py, pm)[1] - 7))
    last = date(ny, nm, min(10, calendar.monthrange(ny, nm)[1]))
    out = []
    cur = first
    while cur <= last:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def _resolve_day(
    weekday: str,
    dom: int,
    candidates: Sequence[date],
    previous: Optional[date],
) -> Optional[date]:
    weekday_index = _WEEKDAY_INDEX.get(weekday)
    matches = [
        candidate for candidate in candidates
        if candidate.day == dom and candidate.weekday() == weekday_index
        and (previous is None or candidate >= previous)
    ]
    return min(matches) if matches else None


def _is_station(token: str) -> bool:
    return bool(_IATA_RE.fullmatch(token or '')) and token not in _NON_STATIONS


def _parse_leg(line: str) -> Optional[Dict[str, Any]]:
    tokens = line.split()
    flight_index = None
    flight = None
    for index, token in enumerate(tokens):
        match = _FLIGHT_TOKEN_RE.fullmatch(token)
        if match:
            flight_index = index
            flight = 'LH' + match.group(1)
            break
    if flight_index is None:
        return None

    pair_index = None
    pair = None
    for index in range(flight_index + 1, len(tokens)):
        match = _PAIR_RE.fullmatch(tokens[index])
        if match:
            pair_index = index
            pair = match
            break
    if pair_index is None or pair is None:
        return None

    origin = next(
        (tokens[index] for index in range(pair_index - 1, flight_index, -1)
         if _is_station(tokens[index])),
        None,
    )
    destination = next(
        (tokens[index] for index in range(pair_index + 1,
                                           min(len(tokens), pair_index + 4))
         if _is_station(tokens[index])),
        None,
    )
    if not origin:
        return None
    return {
        'flight': flight,
        'origin': origin,
        'destination': destination,
        'departure': pair.group(1),
        'arrival': pair.group(2),
    }


def _arrival_closer(line: str) -> Optional[Tuple[str, str]]:
    tokens = line.split()
    for index, token in enumerate(tokens[:-1]):
        match = _ARR_ONLY_RE.fullmatch(token)
        if match and _is_station(tokens[index + 1]):
            return tokens[index + 1], match.group(1)
    return None


def _first_pair(line: str) -> Optional[Tuple[int, re.Match[str]]]:
    for index, token in enumerate(line.split()):
        match = _PAIR_RE.fullmatch(token)
        if match and match.group(2):
            return index, match
    return None


def _activity_code(rest: str) -> str:
    for token in rest.split():
        cleaned = token.strip(',:;()').upper()
        if (cleaned in _EMPTY_MARKERS or cleaned.isdigit()
                or cleaned in {'FB', 'EURO'}):
            continue
        if _is_station(cleaned):
            continue
        if (cleaned not in _NON_ACTIVITY_CODES
                and re.fullmatch(r'[A-ZÄÖÜ][A-ZÄÖÜ0-9_/-]{0,24}', cleaned)):
            return cleaned
    return ''


def _ground_event(day: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if day['legs']:
        return None
    for line in day['lines']:
        pair_info = _first_pair(line)
        if not pair_info:
            continue
        pair_index, pair = pair_info
        tokens = line.split()
        stations = [token.strip(',:;()').upper() for token in tokens
                    if _is_station(token.strip(',:;()').upper())]
        code = _activity_code(day['rest']) or _activity_code(line)
        if not code:
            continue
        return {
            'code': code,
            'station': (stations[-1] if stations else None),
            'start': pair.group(1),
            'end': pair.group(2),
        }
    return None


def _dt(day: date, hhmm: str) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(':'))
    return datetime(day.year, day.month, day.day, hour, minute)


def _uid(parts: Iterable[Any]) -> str:
    raw = '|'.join(str(part or '') for part in parts)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20]


def rows_to_calendar_events(
    rows: Sequence[Tuple[int, str]],
    carrier: str = 'LH',
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Convert coordinate-reconstructed CAS rows to calendar event tuples.

    Returns ``(result, None)`` or ``(None, error_code)``.  ``result`` contains
    ``events`` grouped per source day plus the complete set of covered dates.
    Event tuples match ``app._pdf_events_to_ics``.
    """
    header = ' '.join(line for _, line in rows[:20])
    if ('Crew Assignment System' not in header
            or 'Einsatzplan' not in header
            or 'UTC' not in ' '.join(line for _, line in rows)):
        return None, 'unsupported_pdf_format'
    period = _parse_period(rows)
    if not period:
        return None, 'no_planning_period'

    candidates = _candidate_dates(period)
    days: List[Dict[str, Any]] = []
    by_date: Dict[date, Dict[str, Any]] = {}
    previous: Optional[date] = None
    current: Optional[Dict[str, Any]] = None
    pending_leg: Optional[Dict[str, Any]] = None
    warnings: List[str] = []

    for _page, raw_line in rows:
        line = ' '.join((raw_line or '').split())
        if not line:
            continue
        day_match = _DAY_RE.match(line)
        if day_match:
            resolved = _resolve_day(
                day_match.group(1), int(day_match.group(2)), candidates, previous)
            if resolved is None:
                warnings.append('unresolved_day')
                current = None
                continue
            previous = resolved
            current = {
                'date': resolved,
                'rest': (day_match.group(3) or '').strip(),
                'lines': [line],
                'legs': [],
                'briefing': None,
                'briefing_station': None,
                'closer_station': None,
            }
            days.append(current)
            by_date[resolved] = current

            if pending_leg:
                closer = _arrival_closer(line)
                if closer:
                    pending_leg['destination'] = closer[0]
                    pending_leg['arrival'] = closer[1]
                    pending_leg['arrival_date'] = resolved
                    current['closer_station'] = closer[0]
                    pending_leg = None

            leg = _parse_leg(line)
            if leg:
                leg['departure_date'] = resolved
                current['legs'].append(leg)
                if not leg.get('arrival'):
                    pending_leg = leg
            continue

        briefing_match = _BRIEF_RE.search(line)
        if briefing_match:
            yy = int(briefing_match.group(4))
            yy = yy if yy >= 100 else 2000 + yy
            try:
                briefing_date = date(
                    yy, int(briefing_match.group(3)), int(briefing_match.group(2)))
            except ValueError:
                briefing_date = None
            target = by_date.get(briefing_date) if briefing_date else current
            if target is not None:
                target['briefing_station'] = briefing_match.group(1).upper()
                target['briefing'] = briefing_match.group(5)
                target['lines'].append(line)
            continue

        if current is None:
            continue
        current['lines'].append(line)
        if pending_leg:
            closer = _arrival_closer(line)
            if closer:
                pending_leg['destination'] = closer[0]
                pending_leg['arrival'] = closer[1]
                pending_leg['arrival_date'] = current['date']
                current['closer_station'] = closer[0]
                pending_leg = None
        leg = _parse_leg(line)
        if leg:
            leg['departure_date'] = current['date']
            current['legs'].append(leg)
            if not leg.get('arrival'):
                pending_leg = leg

    if pending_leg:
        warnings.append('unclosed_overnight_leg')

    event_days: Dict[str, List[Tuple[Any, ...]]] = {}
    counters = {'flight_legs': 0, 'ground': 0, 'all_day': 0}
    homebase = _parse_homebase(rows)
    for day in days:
        source_date: date = day['date']
        day_events: List[Tuple[Any, ...]] = []
        for leg_index, leg in enumerate(day['legs']):
            if not leg.get('destination') or not leg.get('arrival'):
                warnings.append('incomplete_leg')
                continue
            start = _dt(leg['departure_date'], leg['departure'])
            arrival_date = leg.get('arrival_date') or leg['departure_date']
            end = _dt(arrival_date, leg['arrival'])
            if end <= start:
                end += timedelta(days=1)
            number = re.sub(r'^LH', '', leg['flight'], flags=re.I)
            flight_name = f'{carrier}{number}'
            summary = f'{flight_name} {leg["origin"]} - {leg["destination"]}'
            if leg_index == 0 and day.get('briefing'):
                station = day.get('briefing_station') or leg['origin']
                summary = (f'{day["briefing"]} LT Briefing {station} · '
                           f'{summary}')
            suffix = _uid((source_date, 'leg', leg_index, flight_name,
                           leg['origin'], leg['destination']))
            day_events.append((suffix, start, end, summary, False))
            counters['flight_legs'] += 1

        ground = _ground_event(day)
        if ground:
            start = _dt(source_date, ground['start'])
            end = _dt(source_date, ground['end'])
            if end <= start:
                end += timedelta(days=1)
            label = _MARKER_LABELS.get(ground['code'], ground['code'])
            if ground.get('station'):
                label = f'{label} {ground["station"]}'
            suffix = _uid((source_date, 'ground', ground['code']))
            day_events.append((suffix, start, end, label, False))
            counters['ground'] += 1

        if not day_events:
            code = _activity_code(day['rest'])
            if code and code not in _EMPTY_MARKERS and code != 'FB':
                label = _MARKER_LABELS.get(code, code)
                location = None
                if (code == 'X' and day.get('closer_station')
                        and day['closer_station'] != homebase):
                    location = day['closer_station']
                    label = f'Layover {location}'
                suffix = _uid((source_date, 'day', code))
                day_events.append((suffix, source_date,
                                   source_date + timedelta(days=1),
                                   label, True, None, location))
                counters['all_day'] += 1

        event_days[source_date.isoformat()] = day_events

    if not any(event_days.values()):
        return None, 'no_roster_days'
    return {
        'period': period['label'],
        'period_start': f'{period["start_year"]:04d}-{period["start_month"]:02d}',
        'period_end': f'{period["end_year"]:04d}-{period["end_month"]:02d}',
        'printed_at': _parse_printed_at(rows),
        'coverage_dates': sorted(event_days),
        'event_days': event_days,
        'events': [event for key in sorted(event_days)
                   for event in event_days[key]],
        'counts': counters,
        'warnings': sorted(set(warnings)),
    }, None


def parse_cas_roster_pdf(
    pdf_bytes_or_path: Any,
    carrier: str = 'LH',
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Read a CAS PDF and return deterministic calendar-event tuples."""
    try:
        rows = _lines_from_pdf(pdf_bytes_or_path)
    except Exception:
        return None, 'pdf_extract_failed'
    return rows_to_calendar_events(rows, carrier=carrier)


def merge_cas_roster_results(results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge complete roster revisions day-by-day.

    CAS change PDFs repeat unchanged days around their effective change.  A
    later print therefore replaces the complete event set for every date it
    covers; merely deduplicating equal events would retain duties that were
    explicitly removed by the later plan.
    """
    ordered = sorted(
        enumerate(result for result in results if isinstance(result, dict)),
        key=lambda item: (
            item[1].get('printed_at') or datetime.min,
            item[0],
        ),
    )
    event_days: Dict[str, List[Tuple[Any, ...]]] = {}
    sources_by_day: Dict[str, int] = {}
    warnings = set()
    periods = []
    for source_index, result in ordered:
        warnings.update(result.get('warnings') or [])
        if result.get('period') and result['period'] not in periods:
            periods.append(result['period'])
        source_days = result.get('event_days') or {}
        for day in result.get('coverage_dates') or []:
            event_days[day] = list(source_days.get(day) or [])
            sources_by_day[day] = source_index
    events = [event for day in sorted(event_days)
              for event in event_days[day]]
    return {
        'events': events,
        'event_days': event_days,
        'coverage_dates': sorted(event_days),
        'sources_by_day': sources_by_day,
        'periods': periods,
        'warnings': sorted(warnings),
    }
