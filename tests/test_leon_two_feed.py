"""LEON-Integration für AeroWest (Privatjet, Hannover; leon.aero) · 2026-07-20.

Verifiziertes LEON-Feed-Format: SUMMARY `(AWH23E) Flight LBG - OXF`,
LOCATION `LBG - OXF (D-CAWX)` (Route + Kennzeichen), DTSTART/DTEND UTC.
AeroWest verbindet ZWEI Kalender-Links (Duty + Off-Days) — der Import holt
beide, konkateniert per UID-Dedupe und schickt alles durch die EINE Pipeline.

Coverage:
- LEON-Summary-Parse (Callsign/Route/Reg → Sektor mit `tail`)
- UID-Parse + Zwei-Feed-Merge (`_merge_feed_events`: Dedupe, Duty gewinnt,
  Events ohne UID werden nie verschluckt)
- Import-Endpoint mit `url_2`: Gesamt-/Per-Link-Zähler, ein kaputter Link
  blockiert den anderen NICHT (error_1/error_2, Reconcile-Skip), beide kaputt
  → ehrlicher 502, Einzel-Link-Response bleibt key-identisch
- `_canonical_airline_key('AeroWest')` → eigener Bucket ohne Sonderfall

ALLE URLs/Keys synthetisch — keine echten Feed-URLs in Tests (Vertraulichkeit).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as backend

FIXTURE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'fixtures', 'leon_synthetic.ics')

SYN_DUTY_URL = 'https://leon.example.test/synthetic/duty.ics'
SYN_OFF_URL = 'https://leon.example.test/synthetic/off.ics'

# Off-Days-Feed inline: 1 Off-Day + 1 UID-Duplikat aus dem Duty-Feed (Dedupe).
OFF_FEED_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Synthetic LEON Test Feed//DE
BEGIN:VEVENT
UID:leon-off-0001@synthetic.test
DTSTART;VALUE=DATE:20260813
DTEND;VALUE=DATE:20260814
SUMMARY:Off Day
END:VEVENT
BEGIN:VEVENT
UID:leon-duty-0001@synthetic.test
DTSTART:20260810T060000Z
DTEND:20260810T081500Z
SUMMARY:(AWH99X) Flight HAJ - CDG
LOCATION:HAJ - CDG (D-CTES)
END:VEVENT
END:VCALENDAR
"""


def _duty_text():
    with open(FIXTURE_PATH) as f:
        return f.read()


def _duty_events():
    return backend._parse_ics_to_events(_duty_text())


# ── LEON-Summary-Parse ───────────────────────────────────────────────────────

def test_leon_helper_parses_callsign_route_and_reg():
    flt, frm, to, reg = backend._ics_parse_leon_flight(
        '(AWH23E) FLIGHT LBG - OXF', 'LBG - OXF (D-CAWX)')
    assert (flt, frm, to, reg) == ('AWH23E', 'LBG', 'OXF', 'D-CAWX')


def test_leon_helper_ignores_non_leon_summaries():
    # LH-/SWISS-/Marker-Summaries dürfen NIE in den LEON-Zweig fallen.
    for s in ('LH400: FRA - JFK', 'LX1270 ZRH 1236 CPH 1405', 'LAYOVER',
              'Standby HAJ', 'OFF DAY'):
        flt, frm, to, reg = backend._ics_parse_leon_flight(s.upper(), '')
        assert frm is None and to is None, f'false positive for {s!r}'


def test_leon_sector_has_flight_route_and_tail():
    secs = backend._build_ical_sectors(_duty_events())
    day = secs.get('2026-08-10') or []
    assert len(day) == 2, f'expected 2 legs on 2026-08-10, got {day}'
    first = day[0]
    assert first['flight'] == 'AWH99X'
    assert first['from'] == 'HAJ' and first['to'] == 'CDG'
    # Reg aus der LOCATION-Klammer landet als Sektor-tail (Roster-deklarierte
    # Maschine — die Board-Anreicherung überspringt Sektoren mit tail).
    assert first.get('tail') == 'D-CTES'
    # KEIN tail_inferred-Marker → _strip_inferred_tails lässt das tail stehen.
    assert 'tail_inferred' not in first


def test_leon_sector_without_reg_has_no_tail():
    secs = backend._build_ical_sectors(_duty_events())
    vie = (secs.get('2026-08-12') or [])
    assert vie and vie[0]['flight'] == 'AWH97X'
    assert 'tail' not in vie[0], 'ohne LOCATION-Klammer darf kein tail erfunden werden'


def test_leon_non_flight_events_stay_marker_days():
    # Standby läuft wie bisher als Marker-Tag durch die Briefing-Pipeline.
    briefings, _ = backend._ics_events_to_briefings(_duty_events(), existing={})
    assert '2026-08-11' in briefings
    assert 'STANDBY' in (briefings['2026-08-11'].get('ical_summary') or '').upper()
    assert not briefings['2026-08-11'].get('ical_sectors')


# ── UID + Zwei-Feed-Merge ────────────────────────────────────────────────────

def test_parser_captures_uid():
    evs = _duty_events()
    uids = {e.get('uid') for e in evs}
    assert 'leon-duty-0001@synthetic.test' in uids


def test_merge_dedupes_by_uid_duty_wins():
    duty = _duty_events()                                   # 4 Events
    off = backend._parse_ics_to_events(OFF_FEED_ICS)        # 2 Events, 1 Dup
    merged = backend._merge_feed_events(duty, off)
    assert len(merged) == 5, f'expected 4+2-1 dedupe, got {len(merged)}'
    dup = [e for e in merged if e.get('uid') == 'leon-duty-0001@synthetic.test']
    assert len(dup) == 1


def test_merge_keeps_events_without_uid():
    a = [{'summary': 'A'}, {'summary': 'B'}]
    b = [{'summary': 'A'}]      # gleicher Inhalt, aber KEINE UID → nie dedupen
    assert len(backend._merge_feed_events(a, b)) == 3


# ── Import-Endpoint (Zwei-Link) ──────────────────────────────────────────────

def _patch_persistence(monkeypatch, fetch_map):
    """Profil-/Briefing-Persistenz + Netz-Fetch für Endpoint-Tests stubben.
    `fetch_map`: url → (text, err) — gleiche Signatur wie
    `_fetch_calendar_feed_text` (der einzige Netz-Punkt des Endpoints)."""
    saved = {}
    monkeypatch.setattr(backend, '_fetch_calendar_feed_text',
                        lambda url: fetch_map.get(url, (None, 'fetch_failed')))
    monkeypatch.setattr(backend, '_profile_load', lambda t: {})
    monkeypatch.setattr(backend, '_profile_load_from_disk', lambda t: {})
    monkeypatch.setattr(
        backend, '_profile_save',
        lambda t, p, full_disk_payload=None: saved.update(
            {'profile': p, 'disk': full_disk_payload}))
    monkeypatch.setattr(backend, '_ical_briefings_load', lambda t: {})
    monkeypatch.setattr(backend, '_ical_briefings_save', lambda t, b: saved.update({'briefings': b}) or True)
    monkeypatch.setattr(
        backend, '_reconcile_month_briefings',
        lambda t, b, e, full_clean=False: {'feed_dates': len(e), 'cleared': 0,
                                           'window': 'stubbed'})
    return saved


def test_import_two_feeds_counts_and_merge(monkeypatch):
    saved = _patch_persistence(monkeypatch, {
        SYN_DUTY_URL: (_duty_text(), None),
        SYN_OFF_URL: (OFF_FEED_ICS, None),
    })
    client = backend.app.test_client()
    r = client.post('/api/user/calendar-feed/tok-leon-test/import',
                    json={'url': SYN_DUTY_URL, 'url_2': SYN_OFF_URL})
    assert r.status_code == 200
    j = r.get_json()
    assert j['ok'] is True
    assert j['events_count'] == 5          # 4 Duty + 2 Off − 1 UID-Dup
    assert j['events_count_1'] == 4
    assert j['events_count_2'] == 2
    assert 'error_1' not in j and 'error_2' not in j
    # Off-Day aus Feed 2 landet als Briefing-Tag; Duty-Legs als Sektoren.
    briefings = saved.get('briefings') or {}
    assert '2026-08-13' in briefings
    assert len(briefings.get('2026-08-10', {}).get('ical_sectors') or []) == 2
    # Beide Slots persistiert: calendar_feed (merged events) + calendar_feed_2.
    prof = saved.get('profile') or {}
    assert prof.get('calendar_feed', {}).get('url') == SYN_DUTY_URL
    assert len(prof.get('calendar_feed', {}).get('events') or []) == 5
    assert prof.get('calendar_feed_2', {}).get('url') == SYN_OFF_URL
    assert prof.get('calendar_feed_2', {}).get('events_count') == 2


def test_broken_off_link_does_not_block_duty(monkeypatch):
    saved = _patch_persistence(monkeypatch, {
        SYN_DUTY_URL: (_duty_text(), None),
        # SYN_OFF_URL fehlt in der Map → (None, 'fetch_failed')
    })
    client = backend.app.test_client()
    r = client.post('/api/user/calendar-feed/tok-leon-test/import',
                    json={'url': SYN_DUTY_URL, 'url_2': SYN_OFF_URL})
    assert r.status_code == 200
    j = r.get_json()
    assert j['ok'] is True
    assert j['error_2'] == 'fetch_failed_2'
    assert j['events_count_1'] == 4 and j['events_count_2'] == 0
    assert j['events_count'] == 4
    # Partieller Fehler → Reconcile übersprungen (keine Off-Tage wegräumen).
    assert j['reconcile'].get('skipped') == 'partial_feed_failure'
    # Duty-Briefings trotzdem importiert.
    assert '2026-08-10' in (saved.get('briefings') or {})


def test_broken_duty_link_still_imports_off_days(monkeypatch):
    saved = _patch_persistence(monkeypatch, {
        SYN_OFF_URL: (OFF_FEED_ICS, None),
    })
    client = backend.app.test_client()
    r = client.post('/api/user/calendar-feed/tok-leon-test/import',
                    json={'url': SYN_DUTY_URL, 'url_2': SYN_OFF_URL})
    assert r.status_code == 200
    j = r.get_json()
    assert j['ok'] is True
    assert j['error_1'] == 'fetch_failed'
    assert j['events_count_1'] == 0 and j['events_count_2'] == 2
    assert j['reconcile'].get('skipped') == 'partial_feed_failure'
    assert '2026-08-13' in (saved.get('briefings') or {})
    # J5-Lektion: gescheiterter Duty-Fetch darf KEIN frisches imported_at auf
    # den Primär-Slot stempeln (sonst blockt der 6h-Throttle jeden Retry).
    prof = saved.get('profile') or {}
    assert 'calendar_feed' not in prof


def test_both_links_broken_is_honest_502(monkeypatch):
    _patch_persistence(monkeypatch, {})
    client = backend.app.test_client()
    r = client.post('/api/user/calendar-feed/tok-leon-test/import',
                    json={'url': SYN_DUTY_URL, 'url_2': SYN_OFF_URL})
    assert r.status_code == 502
    j = r.get_json()
    assert j['ok'] is False
    assert j['error'] == 'fetch_failed'
    assert j['error_2'] == 'fetch_failed_2'


def test_single_link_response_has_no_two_feed_keys(monkeypatch):
    _patch_persistence(monkeypatch, {SYN_DUTY_URL: (_duty_text(), None)})
    client = backend.app.test_client()
    r = client.post('/api/user/calendar-feed/tok-leon-test/import',
                    json={'url': SYN_DUTY_URL})
    assert r.status_code == 200
    j = r.get_json()
    assert j['ok'] is True and j['events_count'] == 4
    for k in ('events_count_1', 'events_count_2', 'error_1', 'error_2'):
        assert k not in j, f'Einzel-Link-Response muss key-identisch bleiben ({k})'


def test_invalid_second_link_is_honest_bad_url_2(monkeypatch):
    # Explizit geschickter, aber unbrauchbarer url_2 → bad_url_2 (kein stilles
    # Weglassen); der Duty-Link importiert trotzdem.
    _patch_persistence(monkeypatch, {SYN_DUTY_URL: (_duty_text(), None)})
    client = backend.app.test_client()
    r = client.post('/api/user/calendar-feed/tok-leon-test/import',
                    json={'url': SYN_DUTY_URL, 'url_2': 'nicht-eine-url'})
    assert r.status_code == 200
    j = r.get_json()
    assert j['ok'] is True
    assert j['error_2'] == 'bad_url_2'
    assert j['events_count_1'] == 4 and j['events_count_2'] == 0


def test_single_link_fetch_fail_stays_502(monkeypatch):
    _patch_persistence(monkeypatch, {})
    client = backend.app.test_client()
    r = client.post('/api/user/calendar-feed/tok-leon-test/import',
                    json={'url': SYN_DUTY_URL})
    assert r.status_code == 502
    assert r.get_json()['error'] == 'fetch_failed'


# ── Airline-Bucket ───────────────────────────────────────────────────────────

def test_canonical_airline_key_aerowest_own_bucket():
    # KEIN Sonderfall nötig: unbekannte Airline → eigener UPPER-Bucket.
    assert backend._canonical_airline_key('AeroWest') == 'AEROWEST'
    assert backend._canonical_airline_key(' aerowest ') == 'AEROWEST'
