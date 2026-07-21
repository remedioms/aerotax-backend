"""Geräte-Abruf des myTime-iCal (LH-Calendar-Share-Warnung 2026-07-21):
die App holt die ICS selbst und POSTet ics_text (+ics_text_2). Der Server
zieht bei ausgeschaltetem Kill-Switch nichts mehr selbst."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as backend

ICS_DUTY = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:d1@dev
DTSTART:20260810T060000Z
DTEND:20260810T081500Z
SUMMARY:LH 400: FRA-JFK
END:VEVENT
END:VCALENDAR"""

ICS_OFF = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:o1@dev
DTSTART;VALUE=DATE:20260812
DTEND;VALUE=DATE:20260813
SUMMARY:Off Day
END:VEVENT
END:VCALENDAR"""


def _import(token, **body):
    with backend.app.test_request_context(json=body):
        rv = backend.import_calendar_feed(token)
    resp, status = (rv if isinstance(rv, tuple) else (rv, 200))
    return status, resp.get_json()


def _cleanup(token):
    for p in (backend._user_profile_path(token),
              os.path.join(backend._USER_HISTORY_DIR, 'briefings', f'{token}.json')):
        try:
            os.remove(p)
        except OSError:
            pass


def test_device_single_ics_text_imports():
    token = 'AT-DEV-ICAL-1'
    _cleanup(token)
    status, p = _import(token, ics_text=ICS_DUTY)
    assert status == 200 and p['ok'] is True
    assert p['events_count'] >= 1


def test_device_dual_ics_text_merges_both_feeds():
    token = 'AT-DEV-ICAL-2'
    _cleanup(token)
    status, p = _import(token, ics_text=ICS_DUTY, ics_text_2=ICS_OFF)
    assert status == 200 and p['ok'] is True
    # Duty-Flug + Off-Day = 2 Events gemerged.
    assert p['events_count'] >= 2
    assert p.get('events_count_2') == 1


def test_killswitch_disables_server_refresh():
    prev = os.environ.get('AEROX_SERVER_ICAL_REFRESH')
    try:
        os.environ['AEROX_SERVER_ICAL_REFRESH'] = '0'
        assert backend._server_ical_refresh_enabled() is False
        # _maybe_refresh_calendar_feed ist dann ein No-Op (wirft nie, kein Fetch).
        backend._maybe_refresh_calendar_feed('AT-DEV-ICAL-1')
        os.environ['AEROX_SERVER_ICAL_REFRESH'] = '1'
        assert backend._server_ical_refresh_enabled() is True
    finally:
        if prev is None:
            os.environ.pop('AEROX_SERVER_ICAL_REFRESH', None)
        else:
            os.environ['AEROX_SERVER_ICAL_REFRESH'] = prev
