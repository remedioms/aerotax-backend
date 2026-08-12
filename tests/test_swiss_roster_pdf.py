"""Deterministic SWISS roster normalization into the shared ICS parser."""

import app as backend


SWISS = """Historical published roster
Period: March 2026
All times local
Date Report Tags Pos IFF Activity From To Dep Arr A/C Layover / LN Flt hrs
28 Sat 16:00 FA LX052 ZRH BOS 17:54 20:11 359 24:29
01 Sun 21:10 FA LX053 BOS 22:22 359
02 Mon FA ZRH 11:16 69:14
03 Tue ***
05 Thu P FA SCR1 ZRH 09:00 17:00
27 Fri 16:50 DH LX052 ZRH BOS 17:49 21:39
Created 10Aug2026 12:57 (ZRH)
"""


def test_swiss_historical_roster_preserves_split_leg_ground_and_free_day(
        monkeypatch):
    monkeypatch.setattr(
        backend, 'airport_tz',
        lambda iata: {'ZRH': 'Europe/Zurich',
                      'BOS': 'America/New_York'}.get(iata, 'UTC'))
    ics, error = backend._swiss_roster_text_to_ics(SWISS)
    assert error is None
    assert 'LX52 ZRH - BOS' in ics
    assert 'LX53 BOS - ZRH' in ics
    assert 'SUMMARY:Off Day' in ics
    assert 'SUMMARY:SCR1 ZRH' in ics
    assert 'SUMMARY:DH LX52 ZRH - BOS' in ics
    events = backend._parse_ics_to_events_v2(ics)
    split = next(event for event in events if 'LX53 ' in event['summary'])
    assert split['start_iso'] == '2026-03-02T03:22:00Z'
    assert split['end_iso'] == '2026-03-02T10:16:00Z'


def test_swiss_prepublication_spaced_flight_numbers_are_supported():
    text = """Pre-publication report
March 2026
Your preliminary monthly roster (all times local)
Date Report Tags Pos IFF Activity From To Dep Arr A/C Checkout Layover LN
07 Sat 08:20 FA LX 66 ZRH MIA 09:50 14:40 333
Printed 16Feb2026 20:52:45
"""
    ics, error = backend._swiss_roster_text_to_ics(text)
    assert error is None
    assert 'LX66 ZRH - MIA' in ics


def test_swiss_parser_refuses_statistics_only_pdf():
    ics, error = backend._swiss_roster_text_to_ics(
        'Flight Time and Landings\nAug 2026 F/A 777 52:50 4')
    assert ics is None and error == 'unsupported_pdf_format'
    assert backend._pdf_informational_only_kind(
        'Flight Time and Landings\nTotal since entry: 3203:28 703\n') == \
        'aggregate_flight_time_statistics'
