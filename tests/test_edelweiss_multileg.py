"""Edelweiss-Outlook Multi-Leg-Duties (Yanic Kittel, 2026-07-18).

Edelweiss schreibt EIN VEVENT pro Dienst und listet ALLE Legs pipe-getrennt im
SUMMARY ('CC8 (WK38 SJO-LIR) | CC8 (WK38 LIR-ZRH)'); DTSTART = erster Abflug,
DTEND = letzte Ankunft. Der Parser zog vorher nur das erste Routing mit der
GANZEN Dienstspanne → Tages-Touren wirkten als One-Way (falscher Layover),
der Dreiecks-Rückflug fehlte („steigt in LIR aus"), block_minutes war die
Dienstspanne. Diese Tests sichern den Multi-Leg-Pfad UND dass Ein-Leg-
Summaries (LH myTime / SWISS) unverändert bleiben.
"""
import app as A


TRIANGLE = 'CC8 (WK38 SJO-LIR) | CC8 (WK38 LIR-ZRH)'
THREE = 'CC3 (WK364 ZRH-JSI) | CC3 (WK364 JSI-PVK) | CC3 (WK364 PVK-ZRH)'


def test_multi_leg_summary_triangle():
    legs = A._ics_parse_multi_leg_summary(TRIANGLE)
    assert legs == [('WK38', 'SJO', 'LIR'), ('WK38', 'LIR', 'ZRH')]


def test_multi_leg_summary_three_legs():
    legs = A._ics_parse_multi_leg_summary(THREE)
    assert [(f, a, b) for f, a, b in legs] == [
        ('WK364', 'ZRH', 'JSI'), ('WK364', 'JSI', 'PVK'), ('WK364', 'PVK', 'ZRH')]


def test_multi_leg_summary_single_lh_unchanged():
    # LH-myTime-Format: exakt EIN Leg, gleiche Werte wie der Single-Parser.
    legs = A._ics_parse_multi_leg_summary('LH 390: FRA-LUX')
    assert len(legs) == 1
    assert legs[0][1:] == ('FRA', 'LUX')


def test_build_sectors_splits_triangle_with_interpolated_times():
    ev = {'summary': TRIANGLE,
          'start': '2026-07-19',
          'start_iso': '2026-07-19T19:20:00Z',
          'end_iso': '2026-07-20T10:15:00Z'}
    secs = A._build_ical_sectors([ev]).get('2026-07-19')
    assert secs is not None and len(secs) == 2
    assert (secs[0]['from'], secs[0]['to']) == ('SJO', 'LIR')
    assert (secs[1]['from'], secs[1]['to']) == ('LIR', 'ZRH')
    assert secs[0]['flight'] == 'WK38' and secs[1]['flight'] == 'WK38'
    # Rand-Zeiten sind die ECHTEN Duty-Grenzen; die Mitte ist stetig.
    assert secs[0]['dep_iso'] == '2026-07-19T19:20:00Z'
    assert secs[1]['arr_iso'] == '2026-07-20T10:15:00Z'
    assert secs[0]['arr_iso'] == secs[1]['dep_iso']


def test_briefings_multi_leg_legs_and_block():
    ev = {'summary': TRIANGLE,
          'start': '2026-07-19', 'end': '2026-07-20',
          'start_iso': '2026-07-19T19:20:00Z',
          'end_iso': '2026-07-20T10:15:00Z',
          '_multiday_dates': ['2026-07-19']}
    b = A._ics_events_to_briefings([ev])
    if isinstance(b, tuple):
        b = b[0]
    day = b.get('2026-07-19') or {}
    legs = day.get('legs') or []
    assert [(l['from'], l['to']) for l in legs] == [('SJO', 'LIR'), ('LIR', 'ZRH')]
    # Block = Spanne (895) minus ~40 min Boden je Zwischenstopp.
    assert day.get('block_minutes') == 895 - 40


def test_briefings_single_leg_block_unchanged():
    ev = {'summary': 'CC8 (WK36 ZRH-SJO)',
          'start': '2026-07-17', 'end': '2026-07-17',
          'start_iso': '2026-07-17T06:40:00Z',
          'end_iso': '2026-07-17T20:34:00Z',
          '_multiday_dates': ['2026-07-17']}
    b = A._ics_events_to_briefings([ev])
    if isinstance(b, tuple):
        b = b[0]
    day = b.get('2026-07-17') or {}
    assert day.get('block_minutes') == 834
    legs = day.get('legs') or []
    assert len(legs) == 1 and (legs[0]['from'], legs[0]['to']) == ('ZRH', 'SJO')
    # Flugnummer = Klammer-Token, nicht der alte Fehlparse 'CC8 (WK36'.
    assert legs[0]['flight'] == 'WK36'
