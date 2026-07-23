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


# Response-Shape aus der Doku — eventType GROSS + ohne Unterstrich
# ('FLIGHT','HOTEL','GROUNDEVENT'), testet die Normalisierung (Claude-Web-Hinweis
# 2026-07-22: Doku nutzt Großschreibung, nicht 'ground_event').
DUTY = {
    "pkNumber": "123456A",
    "rosterDays": [
        {"day": "2026-05-01T00:00:00Z", "events": [
            {"eventType": "FLIGHT", "eventCategory": "FLIGHT",
             "eventDetails": "LH400", "wholeDay": False,
             "startTime": "2026-05-01T08:55:00Z", "startLocation": "FRA",
             "endTime": "2026-05-01T17:35:00Z", "endLocation": "JFK",
             "eventAttributes": {"rotationId": 1, "dayOfShift": 1}}]},
        {"day": "2026-05-02T00:00:00Z", "events": [
            {"eventType": "HOTEL", "eventCategory": "FLIGHT",
             "eventDetails": "Layover", "wholeDay": True,
             "startLocation": "JFK", "endLocation": "JFK"}]},
        {"day": "2026-05-05T00:00:00Z", "events": [
            {"eventType": "GROUNDEVENT", "eventCategory": "OFF",
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


# ── LH-Known-Issue: Einelement-Arrays kommen als SKALAR ──────────────────────
def test_as_list_scalar_collapse():
    assert fo._as_list(None) == []
    assert fo._as_list([1, 2]) == [1, 2]
    assert fo._as_list({'a': 1}) == [{'a': 1}]
    assert fo._as_list('x') == ['x']


def test_duty_events_to_ics_single_day_scalar_shape():
    # rosterDays UND events als nacktes Objekt statt [Objekt] (Known Issue)
    resp = {'pkNumber': '1A', 'rosterDays': {
        'day': '2026-05-01T00:00:00Z',
        'events': {'eventType': 'FLIGHT', 'eventCategory': 'FLIGHT',
                   'eventDetails': 'LH400', 'wholeDay': False,
                   'startTime': '2026-05-01T08:55:00Z', 'startLocation': 'FRA',
                   'endTime': '2026-05-01T17:35:00Z', 'endLocation': 'JFK'}}}
    ics = fo.duty_events_to_ics(resp)
    assert ics and 'LH400: FRA-JFK' in ics


def test_parsers_single_element_scalar_shape():
    one_crew = {'crewMembers': {'pkNumber': '1B', 'crewPosition': 'CP',
                                'lastName': 'ROENELT', 'firstName': 'S',
                                'dutyCode': 'OD'}}
    assert fo.parse_crew_list(one_crew)[0]['position'] == 'CP'
    one_hotel = {'station': 'DUB', 'hotelInformation': {
        'forAirline': 'Lufthansa',
        'hotelContact': {'company': 'Crowne Plaza Hotel'}}}
    assert fo.parse_crew_hotel(one_hotel)[0]['hotel'] == 'Crowne Plaza Hotel'


# ── Refresh-Fehler-Differenzierung (Never-Re-Login-Strategie) ────────────────
def test_token_request_classifies_fatal_vs_transient(monkeypatch):
    import io
    import urllib.error as ue

    def _mk(code, body):
        def _raise(req, timeout=0):
            raise ue.HTTPError('u', code, 'x', {}, io.BytesIO(body))
        return _raise
    monkeypatch.setattr('urllib.request.urlopen',
                        _mk(400, b'{"error":"invalid_grant"}'))
    tok, err = fo._token_request(b'x=1')
    assert tok is None and err['fatal'] is True and err['oauth'] == 'invalid_grant'
    monkeypatch.setattr('urllib.request.urlopen',
                        _mk(503, b'{"error":"service_unavailable"}'))
    tok, err = fo._token_request(b'x=1')
    assert tok is None and err['fatal'] is False
    # 403 Rate-Limit (Mashery-Text, kein OAuth-Error-Feld) → transient
    monkeypatch.setattr('urllib.request.urlopen',
                        _mk(403, b'{"Error":"Developer Over Rate"}'))
    tok, err = fo._token_request(b'x=1')
    assert tok is None and err['fatal'] is False


def test_refresh_fatal_marks_relogin_and_pushes_once(monkeypatch):
    import time as _t
    saved = {}
    monkeypatch.setattr(fo, '_tokens_load', lambda tok: {
        'access': 'OLD', 'refresh': 'R', 'expires_at': _t.time() - 10})
    monkeypatch.setattr(fo, '_tokens_save', lambda tok, t: saved.update(t) or True)
    monkeypatch.setattr(fo, '_refresh', lambda r: (
        None, {'http': 400, 'oauth': 'invalid_grant', 'fatal': True}))
    pushes = []
    monkeypatch.setattr(fo, '_notify_relogin', lambda tok: pushes.append(tok))
    assert fo._valid_access('AT-U') is None
    assert saved.get('needs_relogin') is True and 'access' not in saved
    assert pushes == ['AT-U']
    # needs_relogin → connected False + kein weiterer Refresh-Versuch
    monkeypatch.setattr(fo, '_tokens_load', lambda tok: dict(saved))
    monkeypatch.setattr(fo, '_refresh', lambda r: (_ for _ in ()).throw(
        AssertionError('toter Grant darf nicht weiter refreshen')))
    assert fo.flightops_connected('AT-U') is False
    assert fo._valid_access('AT-U') is None


def test_refresh_transient_keeps_tokens(monkeypatch):
    saved = []
    monkeypatch.setattr(fo, '_tokens_load', lambda tok: {
        'access': 'OLD', 'refresh': 'R', 'expires_at': 0})
    monkeypatch.setattr(fo, '_tokens_save', lambda tok, t: saved.append(t) or True)
    monkeypatch.setattr(fo, '_refresh', lambda r: (
        None, {'http': 503, 'oauth': 'service_unavailable', 'fatal': False}))
    monkeypatch.setattr(fo, '_notify_relogin', lambda tok: (_ for _ in ()).throw(
        AssertionError('transient darf keinen Re-Login-Push senden')))
    assert fo._valid_access('AT-U') is None
    assert saved == []          # Tokens UNANGETASTET → nächster Versuch normal


def test_refresh_rotation_persists_new_keeps_old(monkeypatch):
    saved = {}
    monkeypatch.setattr(fo, '_tokens_load', lambda tok: {
        'access': 'OLD', 'refresh': 'R1', 'expires_at': 0})
    monkeypatch.setattr(fo, '_tokens_save', lambda tok, t: saved.update(t) or True)
    monkeypatch.setattr(fo, '_refresh', lambda r: (
        {'access': 'NEW', 'refresh': 'R2', 'scope': 's', 'expires_at': 9e18}, None))
    assert fo._valid_access('AT-U') == 'NEW'
    assert saved['refresh'] == 'R2'      # Rotation → neuer Token persistiert
    monkeypatch.setattr(fo, '_refresh', lambda r: (
        {'access': 'NEW2', 'refresh': None, 'scope': 's', 'expires_at': 9e18}, None))
    assert fo._valid_access('AT-U') == 'NEW2'
    assert saved['refresh'] == 'R1'      # keine Rotation → bewährter bleibt


def test_status_reports_needs_relogin(monkeypatch):
    monkeypatch.setattr(fo, '_tokens_load', lambda tok: {
        'refresh': 'R', 'needs_relogin': True, 'scope': 's'})
    import app as backend
    d = backend.app.test_client().get('/api/lh/flightops/status/AT-U').get_json()
    assert d['connected'] is False and d['needs_relogin'] is True


# ── Duty-Events-_links → accessCode/Service-Referenzen ───────────────────────
_L = 'https://api.lufthansa.com/v1/flight_operations/crew_services'
DUTY_LINKS = {
    'pkNumber': '123456A',
    'rosterDays': [{'day': '2026-07-24T00:00:00Z', 'events': [
        {'eventType': 'FLIGHT', 'eventCategory': 'FLIGHT',
         'eventDetails': 'LH400', 'wholeDay': False,
         'startTime': '2026-07-24T08:55:00Z', 'startLocation': 'FRA',
         'endTime': '2026-07-24T17:35:00Z', 'endLocation': 'JFK',
         '_links': {
             'crewList': {'href': _L + '/COMMON_CREWLIST?flightDesignator=LH400'
                          '&flightDate=2026-07-24Z&departureAirport=FRA'
                          '&arrivalAirport=JFK&accessCode=SECRET42'},
             'checkInTimes': {'href': _L + '/COMMON_CHECK_IN_TIMES?'
                              'flightDesignator=LH400&flightDate=2026-07-24Z'
                              '&departureAirport=FRA&arrivalAirport=JFK'
                              '&dutyType=OD&crewCategory=COC'},
             'landingReport': {'href': _L + '/COMMON_LANDING_REPORT?'
                               'flightDesignator=LH400&flightDate=2026-07-24Z'
                               '&departureAirport=FRA'}}}]}]}


def test_extract_duty_links_and_find():
    links = fo.extract_duty_links(DUTY_LINKS)
    services = sorted(l['service'] for l in links)
    assert services == ['checkintimes', 'crewlist', 'landingreport']
    p = fo._links_find(links, 'crewlist', 'LH400', '2026-07-24', 'FRA', 'JFK')
    assert p['accessCode'] == 'SECRET42'
    ci = fo._links_find(links, 'checkintimes', 'lh400', '2026-07-24')
    assert ci['dutyType'] == 'OD' and ci['crewCategory'] == 'COC'
    # landingReport hat kein arrivalAirport → Match trotzdem (arr nur wenn vorhanden)
    lr = fo._links_find(links, 'landingreport', 'LH400', '2026-07-24', 'FRA', 'JFK')
    assert lr['departureAirport'] == 'FRA'
    assert fo._links_find(links, 'crewlist', 'LH401', '2026-07-24') is None


def test_extract_duty_links_scalar_event():
    # Known Issue: einzelnes Event als Skalar
    resp = {'rosterDays': {'day': 'x', 'events': {
        'eventType': 'FLIGHT', '_links': {'crewList': {
            'href': _L + '/COMMON_CREWLIST?flightDesignator=LH1&flightDate='
                    '2026-07-24Z&departureAirport=FRA&arrivalAirport=MUC'
                    '&accessCode=A1'}}}}}
    links = fo.extract_duty_links(resp)
    assert links[0]['params']['accessCode'] == 'A1'


def test_resolve_link_params_live_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(fo, '_flow_dir', lambda: str(tmp_path))
    # Cache leer → Tages-Fenster wird live geladen und gecacht
    calls = []
    monkeypatch.setattr(fo, 'duty_events',
                        lambda tok, fd, td: calls.append((fd, td)) or DUTY_LINKS)
    p = fo._resolve_link_params('AT-U', 'crewlist', 'LH400', '2026-07-24')
    assert p['accessCode'] == 'SECRET42'
    assert calls == [('2026-07-24', '2026-07-24')]
    # zweiter Aufruf: aus dem Cache, KEIN weiterer API-Call
    p2 = fo._resolve_link_params('AT-U', 'crewlist', 'LH400', '2026-07-24')
    assert p2['accessCode'] == 'SECRET42' and len(calls) == 1


def test_parse_check_in_times():
    resp = {'briefingRoom': 'B123', 'briefingBegin': '2026-07-24T07:30:00Z',
            'crewBusDeparture': '2026-07-24T08:10:00Z', 'irrelevant': 'x'}
    t = fo.parse_check_in_times(resp)
    assert t == {'briefingRoom': 'B123',
                 'briefingBegin': '2026-07-24T07:30:00Z',
                 'crewBusDeparture': '2026-07-24T08:10:00Z'}
    assert fo.parse_check_in_times({'processingErrors': [{'code': 500}]}) == {}



def _pass_auth_gate(monkeypatch):
    """Token-Binding-Gate (bug004): Pfad-Token muss in auth_users existieren +
    Bearer==Pfad-Token. Pattern wie test_crewlog_persistence."""
    import app as A
    monkeypatch.setattr(
        A, '_validate_token',
        lambda _t: A._TokenValidationResult(
            A._TokenValidationState.VALID, 'test@aerox.test'))


def test_crewlist_endpoint_resolves_access_from_links(monkeypatch):
    _pass_auth_gate(monkeypatch)
    monkeypatch.setattr(fo, '_KEY', 'k'); monkeypatch.setattr(fo, '_SECRET', 's')
    monkeypatch.setattr(fo, '_valid_access', lambda tok: 'ACC')
    monkeypatch.setattr(fo, '_links_load',
                        lambda tok: fo.extract_duty_links(DUTY_LINKS))
    got = {}
    monkeypatch.setattr(fo, 'crew_list',
                        lambda tok, f, d, dep, arr, ac:
                        got.update(ac=ac, dep=dep, arr=arr) or REAL_CREWLIST)
    import app as backend
    r = backend.app.test_client().post('/api/lh/flightops/crewlist/AT-U',
                                       headers={'Authorization': 'Bearer AT-U'},
                                       json={'flight': 'LH400',
                                             'date': '2026-07-24'})
    assert r.status_code == 200
    assert r.get_json()['crew'][0]['position'] == 'CP'
    assert got['ac'] == 'SECRET42' and got['dep'] == 'FRA' and got['arr'] == 'JFK'


def test_crewlist_endpoint_404_without_access(monkeypatch):
    _pass_auth_gate(monkeypatch)
    monkeypatch.setattr(fo, '_KEY', 'k'); monkeypatch.setattr(fo, '_SECRET', 's')
    monkeypatch.setattr(fo, '_valid_access', lambda tok: 'ACC')
    monkeypatch.setattr(fo, '_links_load', lambda tok: [])
    monkeypatch.setattr(fo, 'duty_events', lambda tok, fd, td: None)
    import app as backend
    r = backend.app.test_client().post('/api/lh/flightops/crewlist/AT-U',
                                       headers={'Authorization': 'Bearer AT-U'},
                                       json={'flight': 'LH999',
                                             'date': '2026-07-24'})
    assert r.status_code == 404
    assert r.get_json()['error'] == 'no_access_code'


def test_checkin_endpoint_prefers_link_params(monkeypatch):
    _pass_auth_gate(monkeypatch)
    monkeypatch.setattr(fo, '_KEY', 'k'); monkeypatch.setattr(fo, '_SECRET', 's')
    monkeypatch.setattr(fo, '_valid_access', lambda tok: 'ACC')
    monkeypatch.setattr(fo, '_links_load',
                        lambda tok: fo.extract_duty_links(DUTY_LINKS))
    got = {}
    monkeypatch.setattr(fo, 'service_get',
                        lambda tok, svc, params: got.update(svc=svc, p=params) or
                        {'briefingBegin': '2026-07-24T07:30:00Z'})
    import app as backend
    r = backend.app.test_client().post('/api/lh/flightops/checkin/AT-U',
                                       headers={'Authorization': 'Bearer AT-U'},
                                       json={'flight': 'LH400',
                                             'date': '2026-07-24'})
    assert r.status_code == 200
    assert r.get_json()['times']['briefingBegin'] == '2026-07-24T07:30:00Z'
    assert got['svc'] == 'COMMON_CHECK_IN_TIMES'
    assert got['p']['dutyType'] == 'OD' and got['p']['crewCategory'] == 'COC'


# ── Periodischer Voll-Refresh (Cron-Endpoint) ────────────────────────────────
class _SyncThread:
    def __init__(self, target=None, args=(), daemon=None):
        self._t, self._a = target, args

    def start(self):
        self._t(*self._a)


def test_refresh_all_runs_all_connected(monkeypatch):
    monkeypatch.setattr(fo, '_KEY', 'k'); monkeypatch.setattr(fo, '_SECRET', 's')
    monkeypatch.delenv('ADSB_POLL_SECRET', raising=False)
    monkeypatch.setattr(fo, '_connected_tokens', lambda: ['AT-A', 'AT-B'])
    ran = []
    monkeypatch.setattr(fo.threading, 'Thread', _SyncThread)
    monkeypatch.setattr(fo, '_refresh_all_work',
                        lambda toks: ran.extend(toks) or
                        fo._refresh_all_state.update(running=False))
    import app as backend
    d = backend.app.test_client().post(
        '/api/internal/flightops/refresh-all').get_json()
    assert d['ok'] is True and d['users'] == 2
    assert ran == ['AT-A', 'AT-B']
    assert fo._refresh_all_state['running'] is False


def test_refresh_all_requires_secret_when_set(monkeypatch):
    monkeypatch.setattr(fo, '_KEY', 'k'); monkeypatch.setattr(fo, '_SECRET', 's')
    monkeypatch.setenv('ADSB_POLL_SECRET', 'topsecret')
    import app as backend
    c = backend.app.test_client()
    assert c.post('/api/internal/flightops/refresh-all').status_code == 403
    monkeypatch.setattr(fo, '_connected_tokens', lambda: [])
    d = c.post('/api/internal/flightops/refresh-all',
               headers={'X-Poll-Secret': 'topsecret'}).get_json()
    assert d['ok'] is True and d.get('users') == 0


def test_refresh_all_work_counts_and_releases_lock(monkeypatch):
    calls = []
    monkeypatch.setattr(fo, 'flightops_connected',
                        lambda tok: tok != 'AT-DEAD')
    monkeypatch.setattr(fo, 'flightops_import',
                        lambda tok: calls.append(tok) or ({}, 200))
    monkeypatch.setattr(fo.time, 'sleep', lambda s: None)
    fo._refresh_all_state['running'] = True
    fo._refresh_all_work(['AT-A', 'AT-DEAD', 'AT-B'])
    assert calls == ['AT-A', 'AT-B']
    st = fo._refresh_all_state
    assert st['running'] is False
    assert st['last']['ok'] == 2 and st['last']['skipped'] == 1
