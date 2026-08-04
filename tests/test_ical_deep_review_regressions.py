"""Grenzfälle aus dem Aero-X-Deep-Review vom 03.08.2026."""

import logging

import app as A
from blueprints import lh_flightops as FO


def _calendar(*events, timezone_block=''):
    return '\r\n'.join([
        'BEGIN:VCALENDAR', 'VERSION:2.0', timezone_block,
        *events, 'END:VCALENDAR', '',
    ])


def _event(lines):
    return '\r\n'.join(['BEGIN:VEVENT', *lines, 'END:VEVENT'])


def test_same_flight_and_route_twice_on_one_day_keeps_both_legs():
    text = _calendar(
        _event(['UID:morning', 'DTSTART:20260803T080000Z',
                'DTEND:20260803T100000Z', 'SUMMARY:LH 999: FRA-JFK',
                'LOCATION:FRA - JFK']),
        _event(['UID:evening', 'DTSTART:20260803T180000Z',
                'DTEND:20260803T200000Z', 'SUMMARY:LH 999: FRA-JFK',
                'LOCATION:FRA - JFK']),
    )
    sectors = A._build_ical_sectors(
        A._parse_ics_to_events_v2(text), identity_mode='v2')
    legs = [leg for day in sectors.values() for leg in day]
    assert [leg['dep_iso'] for leg in legs] == [
        '2026-08-03T08:00:00Z', '2026-08-03T18:00:00Z']


def test_numeric_offset_and_quoted_tzid_keep_correct_absolute_time():
    text = _calendar(
        _event(['UID:offset', 'DTSTART:20260803T140000+0200',
                'DTEND:20260803T160000+0200', 'SUMMARY:TRAINING']),
        _event(['UID:quoted', 'DTSTART;TZID="Europe/Berlin":20260804T140000',
                'DTEND;TZID="Europe/Berlin":20260804T160000', 'SUMMARY:TRAINING']),
    )
    by_uid = {event['uid']: event for event in A._parse_ics_to_events_v2(text)}
    assert by_uid['offset']['start_iso'] == '2026-08-03T12:00:00Z'
    assert by_uid['quoted']['start_iso'] == '2026-08-04T12:00:00Z'


def test_embedded_vtimezone_is_used_for_custom_tzid():
    zone = '\r\n'.join([
        'BEGIN:VTIMEZONE', 'TZID:AeroX/FixedTwo', 'BEGIN:STANDARD',
        'DTSTART:19700101T000000', 'TZOFFSETFROM:+0200',
        'TZOFFSETTO:+0200', 'TZNAME:AX2', 'END:STANDARD',
        'END:VTIMEZONE',
    ])
    text = _calendar(
        _event(['UID:custom-zone',
                'DTSTART;TZID=AeroX/FixedTwo:20260803T140000',
                'DTEND;TZID=AeroX/FixedTwo:20260803T150000',
                'SUMMARY:TRAINING']),
        timezone_block=zone)
    event = A._parse_ics_to_events_v2(text)[0]
    assert event['start_iso'] == '2026-08-03T12:00:00Z'


def test_recurring_flight_keeps_absolute_times_for_every_occurrence():
    text = _calendar(_event([
        'UID:daily-flight', 'DTSTART;TZID=Europe/Berlin:20260803T080000',
        'DTEND;TZID=Europe/Berlin:20260803T100000',
        'RRULE:FREQ=DAILY;COUNT=2', 'SUMMARY:LH 123: FRA-MUC',
        'LOCATION:FRA - MUC',
    ]))
    events = A._parse_ics_to_events_v2(text)
    sectors = A._build_ical_sectors(events, identity_mode='v2')
    legs = [leg for day in sorted(sectors) for leg in sectors[day]]
    assert len(legs) == 2
    assert [leg['dep_iso'] for leg in legs] == [
        '2026-08-03T06:00:00Z', '2026-08-04T06:00:00Z']


def test_exdate_and_cancelled_recurrence_id_remove_only_their_occurrence():
    text = _calendar(
        _event(['UID:series', 'DTSTART:20260803T080000Z',
                'DTEND:20260803T090000Z', 'RRULE:FREQ=DAILY;COUNT=4',
                'EXDATE:20260804T080000Z', 'SUMMARY:STANDBY']),
        _event(['UID:series', 'RECURRENCE-ID:20260805T080000Z',
                'SEQUENCE:2', 'STATUS:CANCELLED']),
    )
    events = A._parse_ics_to_events_v2(text)
    assert [event['start'] for event in events] == ['2026-08-03', '2026-08-06']


def test_exdate_may_remove_the_master_occurrence_itself():
    text = _calendar(_event([
        'UID:master-exdate',
        'DTSTART;TZID=Europe/Berlin:20260803T090000',
        'DTEND;TZID=Europe/Berlin:20260803T100000',
        'RRULE:FREQ=DAILY;COUNT=2',
        'EXDATE;TZID=Europe/Berlin:20260803T090000',
        'SUMMARY:LH 400: FRA-JFK',
    ]))
    events = A._parse_ics_to_events_v2(text)
    assert [event['start'] for event in events] == ['2026-08-04']


def test_recurrence_id_may_move_master_without_leaving_duplicate():
    text = _calendar(
        _event(['UID:move-master',
                'DTSTART;TZID=Europe/Berlin:20260803T090000',
                'DTEND;TZID=Europe/Berlin:20260803T100000',
                'RRULE:FREQ=DAILY;COUNT=2',
                'SUMMARY:LH 400: FRA-JFK']),
        _event(['UID:move-master',
                'RECURRENCE-ID;TZID=Europe/Berlin:20260803T090000',
                'DTSTART;TZID=Europe/Berlin:20260803T110000',
                'DTEND;TZID=Europe/Berlin:20260803T120000',
                'SUMMARY:LH 400: FRA-JFK']),
    )
    events = A._parse_ics_to_events_v2(text)
    assert len(events) == 2
    assert sorted(event['start_iso'] for event in events) == [
        '2026-08-03T09:00:00Z', '2026-08-04T07:00:00Z']


def test_monthly_recurrence_is_expanded():
    text = _calendar(_event([
        'UID:monthly', 'DTSTART:20260803T080000Z',
        'DTEND:20260803T090000Z', 'RRULE:FREQ=MONTHLY;COUNT=3',
        'SUMMARY:MEDICAL',
    ]))
    assert [event['start'] for event in A._parse_ics_to_events_v2(text)] == [
        '2026-08-03', '2026-09-03', '2026-10-03']


def test_higher_sequence_wins_instead_of_earlier_duplicate():
    text = _calendar(
        _event(['UID:revision', 'SEQUENCE:1', 'DTSTART:20260803T080000Z',
                'DTEND:20260803T100000Z', 'SUMMARY:LH 123: FRA-MUC',
                'LOCATION:FRA - MUC']),
        _event(['UID:revision', 'SEQUENCE:2', 'DTSTART:20260803T090000Z',
                'DTEND:20260803T110000Z', 'SUMMARY:LH 123: FRA-MUC',
                'LOCATION:FRA - MUC']),
    )
    events = A._parse_ics_to_events_v2(text)
    assert len(events) == 1
    assert events[0]['start_iso'] == '2026-08-03T09:00:00Z'


def test_regular_250_event_feed_is_not_cut_at_200():
    events = [
        {'start': f'2026-{1 + i // 28:02d}-{1 + i % 28:02d}',
         'summary': f'DUTY {i}'}
        for i in range(250)
    ]
    assert len(A._select_relevant_feed_events(
        events, A._FEED_PROCESS_EVENT_CAP_V2)) == 250


def test_flightops_flight_without_end_time_remains_incomplete_sector(monkeypatch):
    monkeypatch.setenv('AEROX_ROSTER_V2_AIRLINES', 'LUFTHANSA')
    payload = {'rosterDays': [{'day': '2026-08-03', 'events': [{
        'eventCategory': 'FLIGHT', 'eventType': 'FLIGHT',
        'eventDetails': 'LH999', 'startLocation': 'FRA',
        'endLocation': 'JFK', 'startTime': '2026-08-03T08:00:00Z',
        'endTime': None,
    }]}]}
    ics = FO.duty_events_to_ics(payload)
    events = A._parse_ics_to_events_v2(ics)
    sectors = A._build_ical_sectors(events, identity_mode='v2')
    leg = next(iter(sectors.values()))[0]

    assert leg['flight'] == 'LH999'
    assert leg['from'] == 'FRA'
    assert leg['to'] == 'JFK'
    assert leg['dep_iso'] == '2026-08-03T08:00:00Z'
    assert leg['arr_iso'] == ''
    assert leg['incomplete'] == 'missing_end_time'


def test_lower_priority_eventkit_cannot_erase_fresh_pickup_or_legs():
    authoritative = {'2026-08-03': {
        'ical_summary': '13:00 LT Pickup JFK · LH 401: JFK-FRA',
        'ical_start_iso': '2026-08-03T17:00:00Z',
        'ical_end_iso': '2026-08-04T02:00:00Z',
        'ical_sectors': [{'flight': 'LH401', 'from': 'JFK', 'to': 'FRA'}],
        'ical_layover_ort': 'JFK',
    }}
    stale_eventkit = {
        '2026-08-03': {
            'ical_summary': 'LH 401',
            'ical_start_iso': '2026-08-03T18:00:00Z',
            'ical_end_iso': '2026-08-04T03:00:00Z',
            'ical_sectors': [],
        },
        '2026-08-05': {'ical_summary': 'TRAINING'},
    }

    merged = A._merge_lower_priority_briefings(authoritative, stale_eventkit)

    assert merged['2026-08-03'] == authoritative['2026-08-03']
    assert merged['2026-08-05']['ical_summary'] == 'TRAINING'


def test_roster_v2_is_default_off_and_legacy_output_stays_authoritative(
        monkeypatch):
    monkeypatch.delenv('AEROX_ROSTER_V2_AIRLINES', raising=False)
    monkeypatch.setattr(
        A, '_profile_load',
        lambda token: (_ for _ in ()).throw(
            AssertionError('disabled V2 must not load a profile')))
    text = _calendar(
        _event(['UID:morning', 'DTSTART:20260803T080000Z',
                'DTEND:20260803T100000Z', 'SUMMARY:LH 999: FRA-JFK',
                'LOCATION:FRA - JFK']),
        _event(['UID:evening', 'DTSTART:20260803T180000Z',
                'DTEND:20260803T200000Z', 'SUMMARY:LH 999: FRA-JFK',
                'LOCATION:FRA - JFK']),
    )
    selected = A._parse_ics_to_events(text, token='owner')
    assert selected == A._parse_ics_to_events_legacy(text)
    legacy_legs = [
        leg for day in A._build_ical_sectors(selected).values() for leg in day]
    assert len(legacy_legs) == 1
    assert A._FEED_PROCESS_EVENT_CAP == 200


def test_roster_v2_can_be_enabled_for_one_airline_only(monkeypatch):
    monkeypatch.setenv('AEROX_ROSTER_V2_AIRLINES', 'LUFTHANSA')
    monkeypatch.setattr(
        A, '_profile_load',
        lambda token: {'profile': {'airline': (
            'Lufthansa' if token == 'lh-user' else 'Swiss')}})
    text = _calendar(_event([
        'UID:offset', 'DTSTART:20260803T140000+0200',
        'DTEND:20260803T160000+0200', 'SUMMARY:TRAINING',
    ]))
    assert A._parse_ics_to_events(
        text, token='lh-user') == A._parse_ics_to_events_v2(text)
    assert A._parse_ics_to_events(
        text, token='lx-user') == A._parse_ics_to_events_legacy(text)
    assert A._feed_process_event_cap('lh-user') == 2000
    assert A._feed_process_event_cap('lx-user') == 200


def test_shadow_diff_contains_counts_and_hashes_but_no_roster_text(
        monkeypatch, caplog):
    monkeypatch.setenv('AEROX_ROSTER_V2_SHADOW', '1')
    monkeypatch.setattr(
        A, '_profile_load',
        lambda token: {'profile': {'airline': 'Lufthansa'}})
    secret_summary = 'PRIVATE CREW EVENT LH 123'
    with caplog.at_level(logging.WARNING):
        A._roster_shadow_record(
            'fixture',
            [{'uid': 'private-uid', 'summary': secret_summary,
              'start_iso': '2026-08-03T08:00:00Z'}],
            [], token='AT-0123456789ABCDEF')
    rendered = '\n'.join(record.getMessage() for record in caplog.records)
    assert 'missing_count' in rendered
    assert secret_summary not in rendered
    assert 'private-uid' not in rendered
    assert 'AT-0123456789ABCDEF' not in rendered
