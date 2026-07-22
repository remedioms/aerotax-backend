"""Edelweiss-Outlook Duty-Fenster → echte Flugzeiten (Yanic, 2026-07-23).

Der Feed schreibt pro Dienst EIN VEVENT mit DTSTART = REPORT-Zeit und
DTEND = DIENST-ENDE. Vorher nahm die Pipeline das 1:1 als Flugzeit:
falsches Briefing (45 min VOR dem Report), Legs ohne Bodenzeit, OUT-Flüge
12 h „lang", und die ganztägigen LAY-Events (ohne LOCATION) ließen den
PUJ-Layover Ende Juli ohne Ort — Kalender-Balken riss an der Monatsgrenze,
Spesen (All-Inclusive PUJ/POP) fehlten. Szenarien hier = echter Yanic-Feed.
"""
import app as A


def _ev(summary, start_iso, end_iso, **kw):
    ev = {'summary': summary, 'start': start_iso[:10],
          'start_iso': start_iso, 'end': end_iso[:10], 'end_iso': end_iso,
          '_is_date_only_start': False, '_is_date_only_end': False}
    ev.update(kw)
    ev['_multiday_dates'] = A._ics_multiday_dates(ev)
    return ev


RHO = 'CC2 (WK346 ZRH-RHO) | CC2 (WK347 RHO-ZRH)'
TPA_OUT = 'CC6 (WK4 ZRH-TPA)'
TPA_RET = 'CC6 (WK5 TPA-ZRH)'
PUJ_OUT = 'CC8 (WK34 ZRH-PUJ)'
PUJ_RET = 'CC8 (WK35 PUJ-ZRH)'


def test_wk_station_geography():
    assert A._wk_station_is_short_haul('RHO') is True    # Europe/Athens
    assert A._wk_station_is_short_haul('LCA') is True    # Asia/Nicosia
    assert A._wk_station_is_short_haul('HRG') is True    # Africa/Cairo
    assert A._wk_station_is_short_haul('PUJ') is False   # Karibik
    assert A._wk_station_is_short_haul('SFJ') is False   # Grönland
    assert A._wk_station_is_short_haul('QQZ') is None    # unbekannt


def test_short_haul_turnaround_offsets_and_ground_time():
    """RHO-Tagesumlauf: Abflug = Report + 1 h, letzte Ankunft = Duty-Ende
    − 30 min, 45 min Boden zwischen den Legs."""
    ev = _ev(RHO, '2026-07-23T11:05:00Z', '2026-07-23T19:45:00Z')
    A._edelweissify_roster_events([ev])
    b = ev['_wk_leg_times']
    assert len(b) == 2
    assert b[0][0] == '2026-07-23T12:05:00Z'          # Report 11:05 + 1 h
    assert b[1][1] == '2026-07-23T19:15:00Z'          # Duty-Ende − 30 min
    gap = A._iso_minutes_between(b[0][1], b[1][0])
    assert gap == 45                                   # Bodenzeit
    # Block = Fenster (7 h 10) − Boden (45) = 6 h 25
    assert ev['_wk_block_minutes'] == 385


def test_long_haul_single_leg_offsets():
    """WK4 ZRH-TPA: Report + 1 h 30 = 11:10Z (realer Slot 13:10 LT),
    Ankunft = 22:00Z − 30 min = 21:30Z (real ~17:20 Ortszeit)."""
    ev = _ev(TPA_OUT, '2026-08-10T09:40:00Z', '2026-08-10T22:00:00Z')
    A._edelweissify_roster_events([ev])
    assert ev['_wk_leg_times'] == [
        ('2026-08-10T11:10:00Z', '2026-08-10T21:30:00Z')]


def test_lh_mytime_wk_prosa_leg_not_stamped():
    """LH-myTime-Prosa („WK 123: ZRH - PMI") trägt ECHTE Flugzeiten —
    darf den Stempel nie bekommen."""
    ev = _ev('WK 123: ZRH - PMI', '2026-07-23T06:00:00Z',
             '2026-07-23T08:10:00Z')
    A._edelweissify_roster_events([ev])
    assert '_wk_leg_times' not in ev
    lh = _ev('LH 463: MIA - FRA', '2026-07-23T20:00:00Z',
             '2026-07-24T05:00:00Z')
    A._edelweissify_roster_events([lh])
    assert '_wk_leg_times' not in lh


def test_sectors_and_legs_use_stamped_times():
    ev = _ev(RHO, '2026-07-23T11:05:00Z', '2026-07-23T19:45:00Z')
    A._edelweissify_roster_events([ev])
    secs = A._build_ical_sectors([ev])['2026-07-23']
    assert [s['dep_iso'] for s in secs] == \
        ['2026-07-23T12:05:00Z', '2026-07-23T16:02:30Z']
    briefings, _ = A._ics_events_to_briefings([ev], existing={})
    day = briefings['2026-07-23']
    # DTSTART bleibt Report-Zeit (Briefing-Anzeige 13:05 LT).
    assert day['ical_start_iso'] == '2026-07-23T11:05:00Z'
    assert day['block_minutes'] == 385
    legs = day['legs']
    assert legs[0]['dep'] == '14:05'                  # 12:05Z in ZRH
    assert legs[1]['arr'] == '21:15'                  # 19:15Z in ZRH


def test_single_leg_stamped_sector_not_duty_window():
    """Ein-Leg-Duty (WK34 ZRH-PUJ): Sektor = echte Flugzeit, nicht
    Report→Duty-Ende (vorher 12 h 05 „Flugzeit" — Yanic-Punkt 4)."""
    ev = _ev(PUJ_OUT, '2026-07-28T09:55:00Z', '2026-07-28T22:00:00Z')
    A._edelweissify_roster_events([ev])
    secs = A._build_ical_sectors([ev])['2026-07-28']
    assert secs[0]['dep_iso'] == '2026-07-28T11:25:00Z'
    assert secs[0]['arr_iso'] == '2026-07-28T21:30:00Z'


def test_puj_layover_synthesis_cross_month():
    """LAY-Tage (ganztägig, ohne LOCATION) zwischen OUT 28.7. und Rückflug
    1.8. → synthetisches LAYOVER PUJ; ical_layover_ort 28.–31.7. UND 1.8.
    (Monatsgrenze), Heimkehr-Tag 2.8. ohne."""
    events = [
        _ev(PUJ_OUT, '2026-07-28T09:55:00Z', '2026-07-28T22:00:00Z'),
        {'summary': 'LAY', 'start': '2026-07-29', 'end': '2026-07-30',
         '_is_date_only_start': True, '_is_date_only_end': True,
         '_multiday_dates': ['2026-07-29']},
        _ev(PUJ_RET, '2026-08-01T21:55:00Z', '2026-08-02T09:07:00Z'),
    ]
    A._edelweissify_roster_events(events)
    events = A._generic_layover_synthesis(events, token=None)
    lays = [e for e in events if e.get('summary') == 'LAYOVER']
    assert len(lays) == 1 and lays[0]['location'] == 'PUJ'
    briefings, _ = A._ics_events_to_briefings(events, existing={})
    for d in ('2026-07-28', '2026-07-29', '2026-07-30', '2026-07-31',
              '2026-08-01'):
        assert briefings[d].get('ical_layover_ort') == 'PUJ', d
    assert briefings.get('2026-08-02', {}).get('ical_layover_ort') is None


def test_no_false_layover_between_home_turnarounds():
    """Aufeinanderfolgende ZRH-Tagesumläufe (Multi-Leg, enden an der
    Homebase) erzeugen KEIN Phantom-Layover — der Duty-Block-Endpunkt
    (letzte Ankunft ZRH), nicht das erste Leg-Ziel, zählt."""
    events = [
        _ev('CC2 (WK360 ZRH-SMI) | CC2 (WK361 SMI-ZRH)',
            '2026-08-06T08:00:00Z', '2026-08-06T16:00:00Z'),
        _ev('CC3 (WK364 ZRH-JSI) | CC3 (WK364 JSI-PVK) | CC3 (WK364 PVK-ZRH)',
            '2026-08-07T08:55:00Z', '2026-08-07T17:35:00Z'),
    ]
    A._edelweissify_roster_events(events)
    out = A._generic_layover_synthesis(list(events), token=None)
    assert not [e for e in out if e.get('summary') == 'LAYOVER']


def test_interpolation_fallback_without_stamp_unchanged():
    """Ohne Stempel (z. B. Plausi-Gate) bleibt die alte Gleichverteilung."""
    ev = _ev(RHO, '2026-07-23T11:05:00Z', '2026-07-23T19:45:00Z')
    secs = A._build_ical_sectors([ev])['2026-07-23']
    assert secs[0]['dep_iso'] == '2026-07-23T11:05:00Z'
    assert secs[1]['arr_iso'] == '2026-07-23T19:45:00Z'
