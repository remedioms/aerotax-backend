# -*- coding: utf-8 -*-
"""Boden-Blöcke mit eigenen Zeiten (Tibor/SBAO, 18.08.2026).

Ein Airport-Standby NACH dem Flug (Donnerstag: LH1109 SZG-FRA, danach
„Standby (SBAO)" 08:00–09:30 LT) überlebte den Tages-Merge nur als Wort im
Summary — DTSTART/DTEND des Standby-VEVENTs gingen verloren, die
Tages-Timeline (rendert nur ical_sectors) zeigte den Block gar nicht.
Getimte Standby-/Reserve-Events reisen jetzt additiv als
``ground_events`` mit ihren echten Zeiten mit.
"""

import app


def _tag(events):
    out, _anzahl = app._ics_events_to_briefings(events)
    assert '2026-08-20' in out
    return out['2026-08-20']


def test_standby_nach_flug_behaelt_seine_zeiten():
    tag = _tag([
        {'start': '2026-08-20', 'summary': 'LH 1109: SZG-FRA',
         'start_iso': '2026-08-20T04:00:00Z', 'end_iso': '2026-08-20T05:00:00Z'},
        {'start': '2026-08-20', 'summary': 'Standby (SBAO)', 'location': 'FRA',
         'start_iso': '2026-08-20T06:00:00Z', 'end_iso': '2026-08-20T07:30:00Z'},
    ])
    ge = tag.get('ground_events')
    assert ge == [{'label': 'Standby (SBAO)', 'station': 'FRA',
                   'start_iso': '2026-08-20T06:00:00Z',
                   'end_iso': '2026-08-20T07:30:00Z'}]
    # Summary-Merge unverändert.
    assert 'Standby (SBAO)' in (tag.get('ical_summary') or '')


def test_flug_und_layover_erzeugen_keine_ground_events():
    tag = _tag([
        {'start': '2026-08-20', 'summary': 'LH 1109: SZG-FRA',
         'start_iso': '2026-08-20T04:00:00Z', 'end_iso': '2026-08-20T05:00:00Z'},
        {'start': '2026-08-20', 'summary': 'Layover [SZG]', 'location': 'SZG',
         'start_iso': '2026-08-19T18:00:00Z', 'end_iso': '2026-08-20T03:00:00Z'},
    ])
    assert 'ground_events' not in tag


def test_standby_ohne_zeiten_wird_nicht_erfunden():
    tag = _tag([
        {'start': '2026-08-20', 'summary': 'Standby (SBAO)'},
    ])
    # Keine-Fake-Werte-Regel: ohne DTSTART/DTEND keine ground_events-Zeile.
    assert 'ground_events' not in tag


def test_doppeltes_event_wird_dedupet():
    ev = {'start': '2026-08-20', 'summary': 'Standby (SBAO)', 'location': 'FRA',
          'start_iso': '2026-08-20T06:00:00Z', 'end_iso': '2026-08-20T07:30:00Z'}
    tag = _tag([ev, dict(ev)])
    assert len(tag.get('ground_events') or []) == 1
