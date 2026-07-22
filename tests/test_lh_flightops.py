"""LH FlightOps Crew API (Engine B): Authorization Code + PKCE + Duty-Events→ICS.
Rein offline: PKCE-Korrektheit, Authorize-URL-Bau, Token-Exchange (HTTP gemockt),
Duty-Events-Parser gegen die dokumentierte Response-Shape. Kein Live-Call."""
import base64
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blueprints import lh_flightops as fo


def test_pkce_s256_correct():
    v, c = fo._pkce_pair()
    # Challenge = base64url(sha256(verifier)) ohne Padding
    expect = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b'=').decode()
    assert c == expect
    assert '=' not in v and '=' not in c


def test_noop_when_unconfigured(monkeypatch):
    monkeypatch.setattr(fo, '_KEY', ''); monkeypatch.setattr(fo, '_SECRET', '')
    assert fo.flightops_configured() is False
    import app as backend
    r = backend.app.test_client().get('/api/lh/flightops/oauth/start?token=AT-X')
    assert r.status_code == 503


def test_oauth_start_builds_authorize_url(monkeypatch):
    monkeypatch.setattr(fo, '_KEY', 'CID'); monkeypatch.setattr(fo, '_SECRET', 'SEC')
    monkeypatch.setattr(fo, '_SCOPE', 'https://mock.cms.fra.dlh.de/publicCrewApi')
    monkeypatch.setattr(fo, '_REDIRECT_URI', 'aerox://lhcrew/callback')
    import app as backend
    r = backend.app.test_client().get('/api/lh/flightops/oauth/start?token=AT-USER')
    assert r.status_code == 200
    d = r.get_json()
    url = d['authorize_url']
    assert url.startswith('https://oauth-test.lufthansa.com/lhcrew/oauth/authorize?')
    for frag in ('response_type=code', 'client_id=CID', 'code_challenge_method=S256',
                 'scope=https%3A%2F%2Fmock.cms.fra.dlh.de%2FpublicCrewApi',
                 'redirect_uri=aerox%3A%2F%2Flhcrew%2Fcallback'):
        assert frag in url, frag
    # state ist im Store hinterlegt (Flow-Bindung)
    assert fo._flow_take(d['state']) is not None


def test_exchange_stores_tokens(monkeypatch):
    monkeypatch.setattr(fo, '_KEY', 'CID'); monkeypatch.setattr(fo, '_SECRET', 'SEC')
    fo._flow_put('STATE1', 'VERIFIER', 'AT-USER-FO')
    monkeypatch.setattr(fo, '_exchange_code',
                        lambda code, ver: {'access': 'ACC', 'refresh': 'REF',
                                           'scope': 'sc', 'expires_at': 9e18})
    saved = {}
    monkeypatch.setattr(fo, '_tokens_save', lambda tok, t: saved.update({tok: t}) or True)
    import app as backend
    r = backend.app.test_client().post('/api/lh/flightops/oauth/exchange',
                                       json={'code': 'CODE', 'state': 'STATE1'})
    assert r.status_code == 200 and r.get_json()['connected'] is True
    assert saved['AT-USER-FO']['access'] == 'ACC'


def test_exchange_rejects_bad_state(monkeypatch):
    monkeypatch.setattr(fo, '_KEY', 'CID'); monkeypatch.setattr(fo, '_SECRET', 'SEC')
    import app as backend
    r = backend.app.test_client().post('/api/lh/flightops/oauth/exchange',
                                       json={'code': 'CODE', 'state': 'NOPE'})
    assert r.status_code == 400 and r.get_json()['error'] == 'state_invalid_or_expired'


# Response-Shape exakt aus der Doku (rosterDays[].events[])
DUTY = {
    "pkNumber": "123456A",
    "rosterDays": [
        {"day": "2026-05-01T00:00:00Z", "events": [
            {"eventType": "flight", "eventCategory": "flight",
             "eventDetails": "LH400", "wholeDay": False,
             "startTime": "2026-05-01T08:55:00Z", "startLocation": "FRA",
             "endTime": "2026-05-01T17:35:00Z", "endLocation": "JFK",
             "eventAttributes": {"rotationId": 1, "dayOfShift": 1}}]},
        {"day": "2026-05-02T00:00:00Z", "events": [
            {"eventType": "hotel", "eventCategory": "flight",
             "eventDetails": "Layover", "wholeDay": True,
             "startLocation": "JFK", "endLocation": "JFK"}]},
        {"day": "2026-05-05T00:00:00Z", "events": [
            {"eventType": "ground_event", "eventCategory": "off",
             "eventDetails": "", "wholeDay": True}]},
    ],
}


def test_duty_events_to_ics_flight_and_markers():
    ics = fo.duty_events_to_ics(DUTY)
    assert ics is not None
    assert 'LH400: FRA-JFK' in ics
    assert 'DTSTART:20260501T085500Z' in ics
    assert 'DTEND:20260501T173500Z' in ics
    assert 'Layover JFK' in ics           # hotel-Event → Layover
    assert 'Off Day' in ics               # off-Kategorie
    assert 'DTSTART;VALUE=DATE:20260505' in ics


def test_duty_events_to_ics_roundtrips_through_parser():
    """Das synthetische ICS muss vom bestehenden Parser als echter Flug-Sektor
    gelesen werden (reuse der Pipeline)."""
    import app as backend
    ics = fo.duty_events_to_ics(DUTY)
    events = backend._parse_ics_to_events(ics)
    secs = backend._build_ical_sectors(events)
    d = secs.get('2026-05-01') or []
    assert [(s['flight'], s['from'], s['to']) for s in d] == [('LH400', 'FRA', 'JFK')]


def test_duty_events_to_ics_empty():
    assert fo.duty_events_to_ics({'rosterDays': []}) is None
    assert fo.duty_events_to_ics(None) is None


def test_ping_endpoint(monkeypatch):
    monkeypatch.setattr(fo, '_KEY', ''); monkeypatch.setattr(fo, '_SECRET', '')
    import app as backend
    r = backend.app.test_client().get('/api/lh/flightops/ping')
    assert r.status_code == 200
    d = r.get_json()
    assert d['configured'] is False
    assert 'oauth-test.lufthansa.com' in d['authorize_url']
    assert 'api-sandbox.lufthansa.com' in d['base']
