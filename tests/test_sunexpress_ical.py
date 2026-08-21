"""SunExpress duty-block iCal normalization regressions."""

import app as A


SUNEXPRESS_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Synthetic SunExpress Roster Test//EN
BEGIN:VEVENT
UID:sunexpress-duty@synthetic.test
DTSTART:20260821T200500Z
DTEND:20260822T024000Z
SUMMARY:✈️ AYT TBS AYT
LOCATION:(2005Z-0240Z)
DESCRIPTION:2005Z 2305L Report\\n2105Z-2315Z 0005L-0315L AYT-TBS XQ302 TCSNN\\n0015Z-0240Z 0415L-0540L TBS-AYT XQ303 TCSNN
END:VEVENT
BEGIN:VEVENT
UID:sunexpress-positioning@synthetic.test
DTSTART:20260823T030000Z
DTEND:20260823T113000Z
SUMMARY:✈️ (DH) AYT DIY AYT GZT
LOCATION:(0300Z-1130Z)
DESCRIPTION:0300Z 0600L Report\\n0400Z-0540Z 0700L-0840L AYT-DIY XQ7112 TCSOM\\n0615Z-0805Z 0915L-1105L DIY-AYT XQ7113 TCSOM\\n1010Z-1130Z 1310L-1430L (AYT-GZT) Positioning XQ7646
END:VEVENT
BEGIN:VEVENT
UID:sunexpress-ground@synthetic.test
DTSTART:20260824T110000Z
DTEND:20260824T150000Z
SUMMARY:🔖 Unknown - GT
LOCATION:(1100Z-1500Z) COV-ASR
END:VEVENT
BEGIN:VEVENT
UID:sunexpress-standby@synthetic.test
DTSTART:20260825T110000Z
DTEND:20260825T180000Z
SUMMARY:🛰 Stand-by 3 - SB3
LOCATION:(1100Z-1800Z) AYT
END:VEVENT
BEGIN:VEVENT
UID:sunexpress-checkin@synthetic.test
DTSTART:20260826T110000Z
DTEND:20260826T110000Z
SUMMARY:✈️ COV
LOCATION:(1100Z-1100Z)
DESCRIPTION:1100Z 1400L Check In
END:VEVENT
END:VCALENDAR
"""


def _profile(airline='Sun Express'):
    return {'profile': {'airline': airline, 'homebase': 'AYT'}}


def _normalized(monkeypatch, airline='Sun Express'):
    monkeypatch.setattr(A, '_profile_load', lambda _token: _profile(airline))
    events = A._parse_ics_to_events(SUNEXPRESS_ICS)
    return A._sunexpressify_roster_events(events, token='sunexpress-test')


def test_adapter_is_strictly_profile_gated(monkeypatch):
    events = _normalized(monkeypatch, airline='Lufthansa')
    assert events[0]['summary'] == '✈️ AYT TBS AYT'
    assert '_exact_leg_times' not in events[0]


def test_duty_description_becomes_exact_numbered_sectors(monkeypatch):
    events = _normalized(monkeypatch)
    duty = events[0]
    assert duty['summary'] == 'XQ302 AYT - TBS | XQ303 TBS - AYT'
    assert duty['_exact_leg_times'] == [
        ('2026-08-21T21:05:00Z', '2026-08-21T23:15:00Z'),
        ('2026-08-22T00:15:00Z', '2026-08-22T02:40:00Z'),
    ]
    assert duty['_exact_block_minutes'] == 275
    assert A._build_ical_sectors([duty])['2026-08-21'] == [
        {'flight': 'XQ302', 'from': 'AYT', 'to': 'TBS',
         'dep_iso': '2026-08-21T21:05:00Z',
         'arr_iso': '2026-08-21T23:15:00Z'},
        {'flight': 'XQ303', 'from': 'TBS', 'to': 'AYT',
         'dep_iso': '2026-08-22T00:15:00Z',
         'arr_iso': '2026-08-22T02:40:00Z'},
    ]


def test_positioning_and_ground_rows_never_invent_flight_numbers(monkeypatch):
    events = _normalized(monkeypatch)
    positioning = events[1]
    assert positioning['summary'].endswith('DH XQ7646 AYT - GZT')
    sectors = A._build_ical_sectors([positioning])['2026-08-23']
    assert sectors[-1]['flight'] == 'DH XQ7646'
    assert positioning['_exact_block_minutes'] == 210

    ground = events[2]
    assert ground['summary'] == 'Ground Transfer COV → ASR (GT)'
    assert ground['location'] == 'COV'
    assert A._build_ical_sectors([ground]) == {}


def test_standby_and_checkin_are_visible_ground_duties(monkeypatch):
    events = _normalized(monkeypatch)
    assert events[3]['summary'] == 'Standby AYT (SB3)'
    assert events[3]['location'] == 'AYT'
    assert events[4]['summary'] == '14:00 LT Briefing COV'
    assert events[4]['location'] == 'COV'
    assert A._build_ical_sectors(events[3:]) == {}


def test_full_real_shape_passes_airline_display_contract(monkeypatch):
    events = _normalized(monkeypatch)
    contract = A._airline_display_contract(events)
    assert contract['ok'] is True
    assert contract['display_mode'] == 'flight_schedule'
    assert contract['sector_count'] == 5
    assert contract['flight_days'] == 2


def test_one_malformed_route_line_keeps_event_fail_closed(monkeypatch):
    monkeypatch.setattr(A, '_profile_load', lambda _token: _profile())
    event = {
        'summary': '✈️ AYT TBS AYT',
        'description': ('2105Z-2315Z 0005L-0315L AYT-TBS XQ302 TCSNN\n'
                        'bad route TBS-AYT'),
        'start_iso': '2026-08-21T20:05:00Z',
        'end_iso': '2026-08-22T02:40:00Z',
    }
    original = dict(event)
    A._sunexpressify_roster_events([event], token='sunexpress-test')
    assert event == original
