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


def test_date_z_format():
    assert fo._date_z('2016-10-01') == '2016-10-01Z'
    assert fo._date_z('2016-10-01Z') == '2016-10-01Z'
    assert fo._date_z('2016-10-01T00:00:00') == '2016-10-01Z'


def test_is_mock():
    # Default-Base ist die Sandbox/mock
    assert fo.is_mock() is True


def test_duty_events_error_shape_is_none(monkeypatch):
    monkeypatch.setattr(fo, '_KEY', 'k'); monkeypatch.setattr(fo, '_SECRET', 's')
    # _api_get liefert die Gateway-Fehler-Shape → duty_events muss None geben
    monkeypatch.setattr(fo, '_api_get', lambda tok, path, params=None: {
        'serviceHost': 'x', 'processingErrors': [{'code': 500, 'type': 'NoHttpResponse'}]})
    assert fo.duty_events('AT-U', '2016-10-01', '2016-10-31') is None


def test_all_9_services_have_client_methods():
    # Konsole 2026-07-22 listet genau diese 9 Services
    assert set(fo.FLIGHTOPS_SERVICES) == {
        'COMMON_DUTY_EVENTS', 'COMMON_CREWLIST', 'COMMON_CREW_ROTATION',
        'COMMON_CHECK_IN_TIMES', 'COMMON_FLIGHT_LEG_DETAILS', 'COMMON_LANDING_REPORT',
        'COMMON_CREW_HOTEL_INFO', 'COMMON_AIRPORT_WEATHER', 'COMMON_SIMULATOR_CREWLIST'}


def test_client_methods_build_correct_paths(monkeypatch):
    calls = []
    monkeypatch.setattr(fo, '_api_get',
                        lambda tok, path, params=None: calls.append((path, params)) or {})
    fo.crew_list('T', 'LH400', '2016-10-01', 'FRA', 'JFK', 'AC1')
    fo.crew_rotation('T', '12345')
    fo.landing_report('T', 'LH400', '2016-10-01', 'FRA')
    fo.flight_leg_details('T', 'LH400', '2016-10-01', 'FRA', 'JFK')
    fo.crew_hotel('T', 'jfk', provider='LHP')
    fo.check_in_times('T', 'LH400', '2016-10-01', 'FRA', 'JFK')
    fo.airport_weather('T', 'fra')
    paths = [c[0] for c in calls]
    assert '/COMMON_CREWLIST' in paths and '/COMMON_CREW_ROTATION' in paths
    assert '/COMMON_LANDING_REPORT' in paths and '/COMMON_FLIGHT_LEG_DETAILS' in paths
    assert '/COMMON_CREW_HOTEL_INFO' in paths and '/COMMON_CHECK_IN_TIMES' in paths
    assert '/COMMON_AIRPORT_WEATHER' in paths
    # Check-In: doku-bestätigte Params (nicht Datumsfenster)
    ci = dict([c for c in calls if c[0] == '/COMMON_CHECK_IN_TIMES'][0][1])
    assert ci['flightDesignator'] == 'LH400' and ci['dutyType'] == 'OD' and ci['crewCategory'] == 'COC'
    # Datum wird zu YYYY-MM-DDZ, Station upper
    cl = dict(calls[0][1]); assert cl['flightDate'] == '2016-10-01Z' and cl['departureAirport'] == 'FRA'
    hotel = dict(calls[4][1]); assert hotel['station'] == 'JFK'


def test_landing_performed(monkeypatch):
    monkeypatch.setattr(fo, 'landing_report',
                        lambda *a: {'landingPerformed': True, 'tailsign': 'D-AIHY'})
    assert fo.landing_performed('T', 'LH400', '2016-10-01', 'FRA') is True
    monkeypatch.setattr(fo, 'landing_report',
                        lambda *a: {'processingErrors': [{'code': 500}]})
    assert fo.landing_performed('T', 'LH400', '2016-10-01', 'FRA') is None


# Echte MOCK-Responses (live gezogen 2026-07-22) — gegen die Realität testen.
REAL_LANDING = {"pkNumber": "123456A", "flightDesignator": "LH400",
                "flightDate": "2016-10-01Z", "departureAirport": "FRA",
                "destinationAirport": "XYZ", "tailsign": "DAISQ",
                "events": {"aircraft": {"out": "2016-10-01T10:04:00Z",
                                        "off": "2016-10-01T10:18:00Z",
                                        "on": "2016-10-01T13:44:00Z",
                                        "in": "2016-10-01T14:02:00Z"}},
                "landingPerformed": "true", "lowVisibilityApproach": "unkown"}
REAL_CREWLIST = {"flightDesignator": "LH400", "crewMembers": [
    {"pkNumber": "095599C", "crewPosition": "CP", "lastName": "ROENELT",
     "firstName": "SOEREN", "dutyCode": "OD"},
    {"pkNumber": "681411I", "crewPosition": "FO", "lastName": "ABBAS",
     "firstName": "BENJAMIN", "dutyCode": "OD"}]}
REAL_HOTEL = {"provider": "LHP", "station": "DUB", "hotelInformation": [
    {"forAirline": "Lufthansa",
     "hotelContact": {"company": "Crowne Plaza Hotel", "lastName": "M",
                      "phone": "+353 1 443 1234", "mobilePhone": ""},
     "hotelTransferContact": {"company": "Crowne Plaza shuttle bus",
                              "phone": "+353 1 443 1234"}}]}


def test_landing_facts_string_bool_and_blocktime(monkeypatch):
    # landingPerformed kommt als STRING 'true' — muss echtes True werden
    monkeypatch.setattr(fo, 'landing_report', lambda *a: REAL_LANDING)
    f = fo.landing_report_facts('T', 'LH400', '2016-10-01', 'FRA')
    assert f['landed'] is True
    assert f['tail'] == 'D-AISQ'
    assert f['block_min'] == 238          # 10:04 → 14:02 = 3:58
    assert fo.landing_performed('T', 'LH400', '2016-10-01', 'FRA') is True


def test_parse_crew_list_real():
    cl = fo.parse_crew_list(REAL_CREWLIST)
    assert cl[0] == {'position': 'CP', 'name': 'Soeren Roenelt',
                     'pk': '095599C', 'duty': 'OD'}
    assert cl[1]['position'] == 'FO' and cl[1]['name'] == 'Benjamin Abbas'


def test_parse_crew_hotel_real():
    h = fo.parse_crew_hotel(REAL_HOTEL)
    assert h[0]['hotel'] == 'Crowne Plaza Hotel'
    assert h[0]['airline'] == 'Lufthansa' and h[0]['station'] == 'DUB'
    assert h[0]['transfer'] == 'Crowne Plaza shuttle bus'


def test_service_get_rejects_bad_service(monkeypatch):
    monkeypatch.setattr(fo, '_api_get', lambda tok, path, params=None: {'called': path})
    assert fo.service_get('T', 'DROP TABLE') is None
    assert fo.service_get('T', 'common_crewlist') == {'called': '/COMMON_CREWLIST'}


def test_ping_endpoint(monkeypatch):
    monkeypatch.setattr(fo, '_KEY', ''); monkeypatch.setattr(fo, '_SECRET', '')
    import app as backend
    r = backend.app.test_client().get('/api/lh/flightops/ping')
    assert r.status_code == 200
    d = r.get_json()
    assert d['configured'] is False
    assert 'oauth-test.lufthansa.com' in d['authorize_url']
    assert 'api-sandbox.lufthansa.com' in d['base']
