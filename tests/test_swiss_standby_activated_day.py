"""SWISS Standby-aktivierter Tag (Ivan Delcev, SWISS PU, realer Feed-Tag
2026-07-17): wenn Crew aus dem Standby aktiviert wird, trägt der Tag im
SWISS-iCal SOWOHL das SBY-VEVENT (`SBYAD`) ALS AUCH die zugeteilten
Flug-VEVENTs. Der Backend-Parser MUSS beide Wahrheiten liefern:

  • `ical_summary` = Same-Day-Merge mit SBY-Code UND allen Flug-Zeilen
    (Reihenfolge wie im Feed: „SBYAD · LX1830 … · LX1831 …")
  • `ical_sectors` = die echten Pro-Leg-Sektoren der Flug-VEVENTs
    (die Flüge dürfen NICHT vom SBY-Event verschluckt werden)

Der Anzeige-Bug lag im iOS-Classifier (SBY-Token gewann gegen die Flug-
Zeilen); dieser Test friert das KORREKTE Backend-Verhalten als Regression-
Guard ein — verifiziert gegen den echten Live-Datensatz des Testers
(user_ical_briefings 2026-07-17: secs=2, Summary beginnt mit „SBYAD · ").
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as backend


# Nachbau des realen Tags: SBY-Event zuerst (so liefert es der SWISS-Feed —
# genau diese Reihenfolge triggerte den iOS-Classifier-Bug), dann die beiden
# zugeteilten Flüge. Zeiten: ZRH=CEST(+2), ATH=EEST(+3) im Juli.
ICS_SBY_ACTIVATED = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
DTSTART:20260717T030000Z
DTEND:20260717T150000Z
SUMMARY:SBYAD
CATEGORIES:SWISS DUTY SCHEDULE
END:VEVENT
BEGIN:VEVENT
DTSTART:20260717T072700Z
DTEND:20260717T095900Z
SUMMARY:LX1830 ZRH 0927 ATH 1259 32Q [FA]
CATEGORIES:SWISS DUTY SCHEDULE
END:VEVENT
BEGIN:VEVENT
DTSTART:20260717T110100Z
DTEND:20260717T143100Z
SUMMARY:LX1831 ATH 1401 ZRH 1631 32Q [FA]
CATEGORIES:SWISS DUTY SCHEDULE
END:VEVENT
END:VCALENDAR
"""


def _import_day():
    events = backend._parse_ics_to_events(ICS_SBY_ACTIVATED)
    events = backend._swissify_roster_events(events)
    briefings, imported = backend._ics_events_to_briefings(events, existing={})
    backend._attach_sectors(briefings, events)
    assert imported >= 3, f'alle 3 VEVENTs müssen zählen, got {imported}'
    assert '2026-07-17' in briefings, f'Tag fehlt: {sorted(briefings)}'
    return briefings['2026-07-17']


def test_sby_activated_day_keeps_flights_in_summary():
    """Same-Day-Merge: SBY-Code UND beide Flug-Zeilen bleiben im Summary."""
    b = _import_day()
    summ = b.get('ical_summary') or ''
    assert 'SBYAD' in summ, f'SBY-Code weg: {summ}'
    assert 'LX1830' in summ and 'LX1831' in summ, f'Flug-Zeile weg: {summ}'
    # Feed-Reihenfolge (SBY zuerst) bleibt erhalten — exakt die Marker-Form,
    # die der iOS-Classifier korrekt als Tour-mit-Standby lesen muss.
    assert summ.startswith('SBYAD'), f'Reihenfolge geändert: {summ}'


def test_sby_activated_day_keeps_flight_sectors():
    """Die Flug-VEVENTs landen als echte Pro-Leg-Sektoren in ical_sectors —
    das SBY-Event verschluckt sie nicht (Kern der Dienstplan-Wahrheit)."""
    b = _import_day()
    secs = b.get('ical_sectors') or []
    legs = [(s.get('flight'), s.get('from'), s.get('to')) for s in secs]
    assert legs == [('LX1830', 'ZRH', 'ATH'), ('LX1831', 'ATH', 'ZRH')], legs


def test_airport_standby_code_passes_through_verbatim():
    """Airport-Standby „APSBY-32S" (realer SWISS-Code, Ivan 2026-07-18): der
    Parser reicht den Roh-Code 1:1 durch (iCal-1:1-Regel) — die Klassifikation
    Standby-vs-Dienst ist Client-Sache (RosterEventClassifier)."""
    ics = ("BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\n"
           "DTSTART:20260718T040000Z\nDTEND:20260718T160000Z\n"
           "SUMMARY:APSBY-32S\nCATEGORIES:SWISS DUTY SCHEDULE\n"
           "END:VEVENT\nEND:VCALENDAR\n")
    events = backend._parse_ics_to_events(ics)
    events = backend._swissify_roster_events(events)
    briefings, _ = backend._ics_events_to_briefings(events, existing={})
    assert briefings['2026-07-18']['ical_summary'] == 'APSBY-32S'
    # Kein Phantom-Sektor aus einem Standby-Event.
    backend._attach_sectors(briefings, events)
    assert not briefings['2026-07-18'].get('ical_sectors')
