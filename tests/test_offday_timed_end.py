"""Audit 2026-07-31, Befund 2: Endzeiten getimter „Off Day (…)"-Termine.

myTime etikettiert auch Schulungs-/Medical-Termine MIT echten Uhrzeiten als
„Off Day (LMN_DM1)" etc. — `_ev_extends_duty` verwarf deren DTEND pauschal
(Regel gegen Layover-/Ganztages-Enden), die Endzeit ging verloren (end=None
in der DB, obwohl LH die endTime liefert). Echte Fälle: 19.04. LMN_DM1
12:00–16:00Z, 25.05. LMN_AI1 12:00–16:00Z, 30.08. LMHS 07:00–14:00Z.

Gegenproben: Ganztages-Freitage und Layover-Übernacht-Enden bleiben wie
bisher OHNE Duty-Ende (die Original-Fälle der Regel dürfen nicht zurück).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as backend


def _briefings(vevents):
    ics = 'BEGIN:VCALENDAR\r\nVERSION:2.0\r\n' + vevents + '\r\nEND:VCALENDAR'
    evs = backend._parse_ics_to_events(ics)
    b, _ = backend._ics_events_to_briefings(evs, existing={})
    return b


# ── Die drei echten Audit-Fälle ─────────────────────────────────────────────

def test_lmn_dm1_keeps_end_time():
    b = _briefings('BEGIN:VEVENT\r\nUID:a@t\r\n'
                   'SUMMARY:Off Day (LMN_DM1)\r\nLOCATION:FRA\r\n'
                   'DTSTART:20260419T120000Z\r\nDTEND:20260419T160000Z\r\n'
                   'END:VEVENT')
    d = b.get('2026-04-19') or {}
    assert d.get('ical_start_iso') == '2026-04-19T12:00:00Z'
    assert d.get('ical_end_iso') == '2026-04-19T16:00:00Z'


def test_lmn_ai1_keeps_end_time():
    b = _briefings('BEGIN:VEVENT\r\nUID:b@t\r\n'
                   'SUMMARY:Off Day (LMN_AI1)\r\nLOCATION:FRA\r\n'
                   'DTSTART:20260525T120000Z\r\nDTEND:20260525T160000Z\r\n'
                   'END:VEVENT')
    d = b.get('2026-05-25') or {}
    assert d.get('ical_end_iso') == '2026-05-25T16:00:00Z'


def test_lmhs_keeps_end_time():
    b = _briefings('BEGIN:VEVENT\r\nUID:c@t\r\n'
                   'SUMMARY:Off Day (LMHS)\r\nLOCATION:FRA\r\n'
                   'DTSTART:20260830T070000Z\r\nDTEND:20260830T140000Z\r\n'
                   'END:VEVENT')
    d = b.get('2026-08-30') or {}
    assert d.get('ical_end_iso') == '2026-08-30T14:00:00Z'


# ── Gegenproben: die Original-Fälle der Regel bleiben geschützt ─────────────

def test_allday_off_day_still_gets_no_duty_end():
    """Ganztages-Freitag (VALUE=DATE) — wie bisher kein Dienstende."""
    b = _briefings('BEGIN:VEVENT\r\nUID:d@t\r\n'
                   'SUMMARY:Off Day (FREE)\r\nLOCATION:FRA\r\n'
                   'DTSTART;VALUE=DATE:20260421\r\n'
                   'DTEND;VALUE=DATE:20260422\r\n'
                   'END:VEVENT')
    d = b.get('2026-04-21') or {}
    assert not d.get('ical_end_iso')


def test_layover_overnight_end_still_not_duty_end():
    """Das Layover-Ende (nächster Morgen) darf die Duty-Spanne des Flugtags
    weiterhin NICHT aufblähen (2026-06-Audit: Belastung 2-4x zu hoch)."""
    b = _briefings('BEGIN:VEVENT\r\nUID:e@t\r\n'
                   'SUMMARY:LH 400: FRA-JFK\r\nLOCATION:FRA - JFK\r\n'
                   'DTSTART:20260609T100000Z\r\nDTEND:20260609T182500Z\r\n'
                   'END:VEVENT\r\n'
                   'BEGIN:VEVENT\r\nUID:f@t\r\n'
                   'SUMMARY:Layover [JFK]\r\nLOCATION:JFK\r\n'
                   'DTSTART:20260609T192500Z\r\nDTEND:20260610T194000Z\r\n'
                   'END:VEVENT')
    d = b.get('2026-06-09') or {}
    assert d.get('ical_end_iso') == '2026-06-09T18:25:00Z'


def test_overnight_timed_off_span_still_excluded():
    """Getimter Off-Eintrag ÜBER Mitternacht (Übernacht-Spanne) bleibt draußen
    — nur Selber-Tag-Termine sind die belegte Ausnahme."""
    b = _briefings('BEGIN:VEVENT\r\nUID:g@t\r\n'
                   'SUMMARY:Off Day\r\nLOCATION:FRA\r\n'
                   'DTSTART:20260419T180000Z\r\nDTEND:20260420T060000Z\r\n'
                   'END:VEVENT')
    d = b.get('2026-04-19') or {}
    assert not d.get('ical_end_iso')


def test_zero_duration_off_event_still_excluded():
    """FlightOps-Fallback DTEND==DTSTART (endTime fehlte) → kein erfundenes
    Ende."""
    ev = {'summary': 'Off Day (LMN_DM1)', 'start': '2026-04-19',
          'end': '2026-04-19', 'start_iso': '2026-04-19T12:00:00Z',
          'end_iso': '2026-04-19T12:00:00Z',
          '_is_date_only_start': False, '_is_date_only_end': False}
    assert not backend._ev_extends_duty(ev['summary'], ev=ev)


def test_timed_off_event_unit():
    ev = {'summary': 'Off Day (LMHS)', 'start': '2026-08-30',
          'end': '2026-08-30', 'start_iso': '2026-08-30T07:00:00Z',
          'end_iso': '2026-08-30T14:00:00Z',
          '_is_date_only_start': False, '_is_date_only_end': False}
    assert backend._ev_extends_duty(ev['summary'], ev=ev)


def test_single_arg_backcompat_unchanged():
    """Alte Aufrufform (nur Summary) verhält sich exakt wie vorher."""
    assert not backend._ev_extends_duty('LAYOVER [JFK]')
    assert not backend._ev_extends_duty('Off Day (FREE)')
    assert not backend._ev_extends_duty('OFF')
    assert not backend._ev_extends_duty('Absence (U1)')
    assert not backend._ev_extends_duty('VISUM Termin')
    assert backend._ev_extends_duty('Office Day (B4)')
    assert backend._ev_extends_duty('LH 400: FRA-JFK')
    assert backend._ev_extends_duty('Briefing FRA')


def test_absence_with_times_still_excluded():
    """Nur die Off-Day-Klasse ist belegt — ein getimtes Absence bleibt wie
    bisher draußen (keine Ausweitung ohne Beleg)."""
    ev = {'summary': 'Absence (U1)', 'start': '2026-04-19',
          'end': '2026-04-19', 'start_iso': '2026-04-19T12:00:00Z',
          'end_iso': '2026-04-19T16:00:00Z',
          '_is_date_only_start': False, '_is_date_only_end': False}
    assert not backend._ev_extends_duty(ev['summary'], ev=ev)
