"""easyJet-iCal: Reportzeit, exakte Zulu-Blockzeit und iOS-Sektoren.

Das Fixture bildet nur das belegte operative Feed-Format nach und enthält
keine echte URL, Token oder persönlichen Kalendereinträge.
"""
import app as A


EASYJET_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Synthetic easyJet Roster Test//EN
BEGIN:VEVENT
UID:easyjet-flight-out@synthetic.test
DTSTART:20260821T102000Z
DTEND:20260821T135500Z
SUMMARY:8111  LGW-ALC
LOCATION:(1120Z-1355Z) LGW
END:VEVENT
BEGIN:VEVENT
UID:easyjet-flight-back@synthetic.test
DTSTART:20260821T135000Z
DTEND:20260821T172500Z
SUMMARY:8112  ALC-LGW
LOCATION:(1450Z-1725Z) ALC
END:VEVENT
BEGIN:VEVENT
UID:easyjet-standby@synthetic.test
DTSTART:20260822T050000Z
DTEND:20260822T130000Z
SUMMARY:CSBL  LGW-LGW
LOCATION:LGW
END:VEVENT
BEGIN:VEVENT
UID:easyjet-rest@synthetic.test
DTSTART;VALUE=DATE:20260823
DTEND;VALUE=DATE:20260824
SUMMARY:REST Rest Day
END:VEVENT
END:VCALENDAR
"""


def _profile(airline='easyJet'):
    return {'profile': {'airline': airline, 'homebase': 'LGW'}}


def _parsed_easyjet(monkeypatch):
    monkeypatch.setattr(A, '_profile_load', lambda _token: _profile())
    events = A._parse_ics_to_events(EASYJET_ICS)
    return A._easyjetify_roster_events(events, token='easyjet-test')


def test_adapter_is_strictly_profile_gated(monkeypatch):
    event = {'summary': '8111 LGW-ALC', 'location': '(1120Z-1355Z) LGW',
             'start_iso': '2026-08-21T10:20:00Z',
             'end_iso': '2026-08-21T13:55:00Z'}
    original = dict(event)
    monkeypatch.setattr(A, '_profile_load', lambda _token: _profile('Lufthansa'))
    A._easyjetify_roster_events([event], token='not-easyjet')
    assert event == original


def test_flight_keeps_report_time_and_uses_explicit_zulu_block(monkeypatch):
    events = _parsed_easyjet(monkeypatch)
    flight = events[0]
    assert flight['summary'] == 'U28111 LGW - ALC'
    assert flight['start_iso'] == '2026-08-21T10:20:00Z'  # Report, not departure
    assert flight['_exact_leg_times'] == [
        ('2026-08-21T11:20:00Z', '2026-08-21T13:55:00Z')]
    assert flight['_exact_block_minutes'] == 155

    sectors = A._build_ical_sectors(events)['2026-08-21']
    assert sectors == [
        {'flight': 'U28111', 'from': 'LGW', 'to': 'ALC',
         'dep_iso': '2026-08-21T11:20:00Z',
         'arr_iso': '2026-08-21T13:55:00Z'},
        {'flight': 'U28112', 'from': 'ALC', 'to': 'LGW',
         'dep_iso': '2026-08-21T14:50:00Z',
         'arr_iso': '2026-08-21T17:25:00Z'},
    ]

    briefings, _ = A._ics_events_to_briefings(events, existing={})
    day = briefings['2026-08-21']
    assert day['ical_start_iso'] == '2026-08-21T10:20:00Z'
    assert day['block_minutes'] == 310
    assert day['legs'] == [
        {'from': 'LGW', 'to': 'ALC', 'flight': 'U28111',
         'dep': '12:20', 'arr': '15:55'},
        {'from': 'ALC', 'to': 'LGW', 'flight': 'U28112',
         'dep': '16:50', 'arr': '18:25'},
    ]


def test_known_ground_codes_are_standby_not_pseudo_flights(monkeypatch):
    monkeypatch.setattr(A, '_profile_load', lambda _token: _profile())
    events = []
    for code in ('CSBL', 'CSBY', 'LSBY', 'PSBL'):
        events.append({
            'summary': f'{code} LGW-LGW', 'location': 'LGW',
            'start': '2026-08-22', 'end': '2026-08-22',
            'start_iso': '2026-08-22T05:00:00Z',
            'end_iso': '2026-08-22T13:00:00Z',
            '_multiday_dates': ['2026-08-22'],
        })
    A._easyjetify_roster_events(events, token='easyjet-test')
    assert [event['summary'] for event in events] == [
        'Standby LGW (CSBL)', 'Standby LGW (CSBY)',
        'Standby LGW (LSBY)', 'Standby LGW (PSBL)',
    ]
    assert A._build_ical_sectors(events) == {}
    briefings, _ = A._ics_events_to_briefings(events, existing={})
    day = briefings['2026-08-22']
    assert day['block_minutes'] == 0
    assert len(day['ground_events']) == 4


def test_import_endpoint_persists_easyjet_display_contract(monkeypatch):
    saved = {}
    profile = _profile()
    monkeypatch.setattr(
        A, '_fetch_calendar_feed_text',
        lambda _url: (EASYJET_ICS, None))
    monkeypatch.setattr(A, '_profile_load', lambda _token: profile)
    monkeypatch.setattr(A, '_profile_load_from_disk', lambda _token: profile)
    monkeypatch.setattr(
        A, '_profile_save',
        lambda _token, value, full_disk_payload=None: saved.update(
            {'profile': value, 'disk': full_disk_payload}) or True)
    monkeypatch.setattr(A, '_ical_briefings_load', lambda _token: {})
    monkeypatch.setattr(
        A, '_ical_briefings_save',
        lambda _token, value: saved.update({'briefings': value}) or True)
    monkeypatch.setattr(
        A, '_reconcile_month_briefings',
        lambda *_args, **_kwargs: {'feed_dates': 3, 'cleared': 0,
                                  'removed_dates': [], 'window': 'stubbed'})

    response = A.app.test_client().post(
        '/api/user/calendar-feed/easyjet-test/import',
        json={'url': 'https://calendar.example.test/easyjet.ics'})
    assert response.status_code == 200
    assert response.get_json()['ok'] is True

    briefings = saved['briefings']
    flight_day = briefings['2026-08-21']
    assert [sector['flight'] for sector in flight_day['ical_sectors']] == [
        'U28111', 'U28112']
    assert flight_day['ical_sectors'][0]['dep_iso'] == \
        '2026-08-21T11:20:00Z'
    standby_day = briefings['2026-08-22']
    assert 'Standby LGW (CSBL)' in standby_day['ical_summary']
    assert not standby_day.get('ical_sectors')
    assert briefings['2026-08-23']['ical_summary'] == 'REST Rest Day'


def test_malformed_location_is_not_guessed(monkeypatch):
    monkeypatch.setattr(A, '_profile_load', lambda _token: _profile())
    event = {'summary': '8111 LGW-ALC', 'location': 'LGW',
             'start_iso': '2026-08-21T10:20:00Z',
             'end_iso': '2026-08-21T13:55:00Z'}
    A._easyjetify_roster_events([event], token='easyjet-test')
    assert event['summary'] == '8111 LGW-ALC'
    assert '_exact_leg_times' not in event
