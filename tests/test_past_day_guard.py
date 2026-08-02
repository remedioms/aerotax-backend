"""Tibor-Vorfall 2026-08-02 („Tirana fehlt"): Schutzplanke für geflogene Tage.

Echter Hergang: Historien-Import mit Fenster-Ende 31.07. → BCN-Hotel-Spanne
nicht ableitbar (Weiterflug außerhalb des Fensters) → Datums-Fallback-VEVENT
31.07.–01.08. ragt EINEN Tag über das Fenster hinaus → REPLACE-Aufbau
(`_ics_events_to_briefings`, rebuilt_dates) entkernte den 01.08.: die
geflogenen Legs BCN-FRA + FRA-TIA wichen einem bloßen
„Layover [BCN] (Tag 2/2)"-Marker; das Reconcile rettete nichts (Tag stand in
feed_dates). Beide Apps (eigener Kalender + Freund-Profil) zeigten den Umlauf
falsch, Tirana fehlte.

Regel der Planke (`_preserve_past_flown_days`): Datum < heute UND alter Tag
hat ical_sectors UND der Neuaufbau hat keine → alter Tagessatz bleibt komplett
stehen. Zukunft behält REPLACE-Semantik (Streichungen müssen verschwinden).
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as backend


def _day(offset):
    return (datetime.now() + timedelta(days=offset)).strftime('%Y-%m-%d')


def _day8(offset):
    return (datetime.now() + timedelta(days=offset)).strftime('%Y%m%d')


def _wrap(vevents):
    return 'BEGIN:VCALENDAR\r\nVERSION:2.0\r\n' + vevents + '\r\nEND:VCALENDAR'


def _flown_day(date_str):
    """Gespeicherter Tagessatz mit geflogenen Legs (wie Tibors 01.08.)."""
    return {
        'ical_summary': ('06:15 LT Pickup BCN · Layover [BCN] (Tag 2/2) · '
                         'LH 1137: BCN-FRA · LH 1454: FRA-TIA · '
                         'Layover [TIA] (Tag 1/2)'),
        'ical_location': 'BCN, BCN - FRA, FRA - TIA, TIA',
        'ical_sectors': [
            {'flight': 'LH1137', 'from': 'BCN', 'to': 'FRA',
             'dep_iso': f'{date_str}T06:12:00Z', 'arr_iso': f'{date_str}T08:15:00Z'},
            {'flight': 'LH1454', 'from': 'FRA', 'to': 'TIA',
             'dep_iso': f'{date_str}T09:16:00Z', 'arr_iso': f'{date_str}T11:23:00Z'},
        ],
    }


def _marker_only_import(first8, last_exclusive8):
    """Fenster-Import, der den Tag nur per Datums-Layover-Marker anfasst —
    exakt der Hotel-Fallback aus duty_events_to_ics (VALUE=DATE, DTEND
    exklusiv einen Tag ÜBER den letzten Hotel-Tag hinaus)."""
    return _wrap(
        'BEGIN:VEVENT\r\n'
        'UID:fo-hotel@test\r\n'
        'SUMMARY:Layover [BCN]\r\n'
        'LOCATION:BCN\r\n'
        f'DTSTART;VALUE=DATE:{first8}\r\n'
        f'DTEND;VALUE=DATE:{last_exclusive8}\r\n'
        'END:VEVENT')


def _run_import(ics_text, existing):
    evs = backend._parse_ics_to_events(ics_text)
    briefings, _ = backend._ics_events_to_briefings(evs, existing=existing)
    backend._attach_sectors(briefings, evs, existing=existing)
    backend._preserve_past_flown_days(briefings, existing)
    return briefings


def test_tibor_yesterday_keeps_flown_sectors():
    """Der Kernfall: Marker-Fallback über das Fensterende entkernt den Vortag
    nicht mehr — geflogene Sektoren + Summary bleiben stehen."""
    yesterday = _day(-1)
    existing = {yesterday: _flown_day(yesterday)}
    # Fallback-Event: vorgestern..gestern (DTEND exklusiv = heute-0)
    briefings = _run_import(_marker_only_import(_day8(-2), _day8(0)), existing)
    day = briefings.get(yesterday) or {}
    flights = [s.get('flight') for s in (day.get('ical_sectors') or [])]
    assert flights == ['LH1137', 'LH1454'], \
        f'Geflogene Sektoren müssen den Marker-Import überleben, got {flights}'
    assert 'LH 1137: BCN-FRA' in (day.get('ical_summary') or ''), \
        'Der alte Tagessatz muss komplett stehen bleiben (Summary inkl. Flüge)'


def test_future_day_still_replaced():
    """Zukunft behält REPLACE: ein gestrichener künftiger Umlauf darf NICHT
    von der Planke wiederbelebt werden."""
    tomorrow = _day(1)
    existing = {tomorrow: _flown_day(tomorrow)}
    briefings = _run_import(_marker_only_import(_day8(1), _day8(2)), existing)
    day = briefings.get(tomorrow) or {}
    assert not (day.get('ical_sectors') or []), \
        'Zukunftstag muss dem sektorlosen Neuaufbau folgen (Streichung)'
    assert 'LH 1137' not in (day.get('ical_summary') or '')


def test_past_day_with_fresh_flights_takes_new_data():
    """Liefert der Import echte Flug-Events für den vergangenen Tag, gewinnt
    der Neuaufbau — die Planke greift NUR bei sektorlosem Rebuild."""
    yesterday = _day(-1)
    existing = {yesterday: _flown_day(yesterday)}
    ics = _wrap(
        'BEGIN:VEVENT\r\n'
        'UID:fo-neu@test\r\n'
        'SUMMARY:LH 1139: BCN-FRA\r\n'
        'LOCATION:BCN - FRA\r\n'
        f'DTSTART:{_day8(-1)}T061500Z\r\n'
        f'DTEND:{_day8(-1)}T082000Z\r\n'
        'END:VEVENT')
    briefings = _run_import(ics, existing)
    day = briefings.get(yesterday) or {}
    flights = [s.get('flight') for s in (day.get('ical_sectors') or [])]
    assert flights == ['LH1139'], \
        f'Neuaufbau MIT Sektoren muss gewinnen, got {flights}'


def test_past_marker_day_without_old_sectors_untouched():
    """Ein legitimer sektorloser Vergangenheits-Tag (reine Layover-
    Fortsetzung) bleibt, wie der Import ihn baut — kein Alt-Zwang."""
    yesterday = _day(-1)
    existing = {yesterday: {'ical_summary': 'Layover [ATH] (Tag 2/2)',
                            'ical_location': 'ATH'}}
    briefings = _run_import(_marker_only_import(_day8(-2), _day8(0)), existing)
    day = briefings.get(yesterday) or {}
    assert 'Layover [BCN]' in (day.get('ical_summary') or ''), \
        'Ohne alte Sektoren gilt normaler REPLACE'
