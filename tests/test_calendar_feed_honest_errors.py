"""Kalender-Import: Fehler darf nicht wie Leere aussehen (Owner 2026-08-18).

Eurowings-Befund: 5 von 5 EW-Usern trugen den flybase-Portal-Link ein — von
aussen liefert er eine leere Antwort, der Import speicherte events=[] als
Erfolg und die Crew sah nie einen Dienstplan (und nie einen Fehler).

Festgenagelt:
1. flybase.eurowings.com wird wie der Discover-Portal-Link EHRLICH abgelehnt
   (400 + Message mit den funktionierenden Wegen) — auch der Auto-Refresh
   fasst solche gespeicherten Links nie wieder an.
2. Eine Antwort ohne BEGIN:VCALENDAR (leer / Login-HTML) ist ein Fehler
   (not_an_ical), kein leerer Kalender — inkl. Refresh-Backoff-Zaehler.
3. Der Zweitlink meldet dasselbe als per-Link-Fehler, ohne den Duty-Link
   zu blockieren.

ALLE URLs synthetisch bzw. nur der Host echt — nie ein echter User-Link.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as backend

FLYBASE_URL = 'https://flybase.eurowings.com/f5-w-abc123/irgendwas.ics'
SYN_URL = 'https://roster.example.test/synthetic/duty.ics'
SYN_URL_2 = 'https://roster.example.test/synthetic/off.ics'

VALID_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Synthetic Test Feed//DE
BEGIN:VEVENT
UID:syn-duty-0001@synthetic.test
DTSTART:20260820T060000Z
DTEND:20260820T081500Z
SUMMARY:EW1234 DUS - PMI
END:VEVENT
END:VCALENDAR
"""


def _patch_persistence(monkeypatch, fetch_map):
    saved = {}
    failures = []
    monkeypatch.setattr(backend, '_fetch_calendar_feed_text',
                        lambda url: fetch_map.get(url, (None, 'fetch_failed')))
    monkeypatch.setattr(
        backend, '_calendar_feed_note_refresh_failure',
        lambda token, url, slot='calendar_feed': failures.append((url, slot)))
    monkeypatch.setattr(backend, '_profile_load', lambda t: {})
    monkeypatch.setattr(backend, '_profile_load_from_disk', lambda t: {})
    monkeypatch.setattr(
        backend, '_profile_save',
        lambda t, p, full_disk_payload=None: saved.update(
            {'profile': p, 'disk': full_disk_payload}))
    monkeypatch.setattr(backend, '_ical_briefings_load', lambda t: {})
    monkeypatch.setattr(backend, '_ical_briefings_save',
                        lambda t, b: saved.update({'briefings': b}) or True)
    monkeypatch.setattr(
        backend, '_reconcile_month_briefings',
        lambda t, b, e, full_clean=False, prev_feed_min=None,
        prev_feed_min_at=None: {'feed_dates': len(e), 'cleared': 0,
                                'removed_dates': [], 'window': 'stubbed'})
    return saved, failures


def test_flybase_link_is_rejected_honestly(monkeypatch):
    saved, _ = _patch_persistence(monkeypatch, {})
    client = backend.app.test_client()
    r = client.post('/api/user/calendar-feed/tok-ew-test/import',
                    json={'url': FLYBASE_URL})
    assert r.status_code == 400
    j = r.get_json()
    assert j['ok'] is False
    assert j['error'] == 'intranet_only_link'
    assert 'offblock' in j['message']
    assert 'profile' not in saved      # nichts gespeichert


def test_flybase_link_never_auto_refreshes():
    assert backend._calendar_feed_url_is_company_intranet(FLYBASE_URL)
    assert backend._calendar_feed_url_is_company_intranet(
        'webcal://flybase.eurowings.com/710119')
    assert not backend._calendar_feed_url_is_company_intranet(SYN_URL)
    assert not backend._calendar_feed_url_is_company_intranet('')


def test_empty_response_is_an_error_not_an_empty_roster(monkeypatch):
    saved, failures = _patch_persistence(monkeypatch, {SYN_URL: ('', None)})
    client = backend.app.test_client()
    r = client.post('/api/user/calendar-feed/tok-ew-test/import',
                    json={'url': SYN_URL})
    assert r.status_code == 502
    j = r.get_json()
    assert j['ok'] is False
    assert j['error'] == 'not_an_ical'
    assert 'profile' not in saved      # KEIN events=[]-„Erfolg" persistiert
    assert (SYN_URL, 'calendar_feed') in failures   # Backoff gezaehlt


def test_login_html_is_an_error_too(monkeypatch):
    saved, _ = _patch_persistence(
        monkeypatch, {SYN_URL: ('<html><body>Login required</body></html>',
                                None)})
    client = backend.app.test_client()
    r = client.post('/api/user/calendar-feed/tok-ew-test/import',
                    json={'url': SYN_URL})
    assert r.status_code == 502
    assert r.get_json()['error'] == 'not_an_ical'
    assert 'profile' not in saved


def test_valid_feed_still_imports(monkeypatch):
    saved, failures = _patch_persistence(
        monkeypatch, {SYN_URL: (VALID_ICS, None)})
    client = backend.app.test_client()
    r = client.post('/api/user/calendar-feed/tok-ew-test/import',
                    json={'url': SYN_URL})
    assert r.status_code == 200
    j = r.get_json()
    assert j['ok'] is True
    assert j['events_count'] == 1
    assert failures == []


def test_html_second_link_does_not_block_duty(monkeypatch):
    saved, failures = _patch_persistence(
        monkeypatch, {SYN_URL: (VALID_ICS, None),
                      SYN_URL_2: ('<html>login</html>', None)})
    client = backend.app.test_client()
    r = client.post('/api/user/calendar-feed/tok-ew-test/import',
                    json={'url': SYN_URL, 'url_2': SYN_URL_2})
    assert r.status_code == 200
    j = r.get_json()
    assert j['ok'] is True
    assert j['events_count_1'] == 1
    assert j.get('error_2') == 'not_an_ical_2'
    assert (SYN_URL_2, 'calendar_feed_2') in failures
