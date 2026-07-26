"""LH FlightOps Crew API (Engine B): Authorization Code + PKCE + Duty-Events→ICS.
Rein offline: PKCE-Korrektheit, Authorize-URL-Bau, Token-Exchange (HTTP gemockt),
Duty-Events-Parser gegen die dokumentierte Response-Shape. Kein Live-Call."""
import base64
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blueprints import lh_flightops as fo
from blueprints.crew_live_state import duty_from_roster_day


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
    # myTime-Paritaet: Flugnummer mit Space + LOCATION-Zeile (Routing-Quelle!).
    assert 'LH 400: FRA-JFK' in ics
    assert 'LOCATION:FRA - JFK' in ics
    assert 'DTSTART:20260501T085500Z' in ics
    assert 'DTEND:20260501T173500Z' in ics
    assert 'Layover [JFK]' in ics         # hotel-Event → Layover [IATA]
    assert 'LOCATION:JFK' in ics          # ohne LOCATION kein ical_layover_ort
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


# Tims echter KRK-Morgen (2026-07-25, „Fehler im Feed"): Live-Shape der
# FlightOps-API — DH-Deadhead in eventDetails, Hotel-Event OHNE Zeiten
# (wholeDay=false, endLocation null), OFFDUTY/GROUNDEVENT mit det='FREE'.
# Ohne LOCATION-Zeilen hatte der Tag kein Routing (flownSectors=0) und iOS
# stufte den 4-Leg-Diensttag mit Layover-Marker als reinen Ruhetag ein →
# der Feed sprang am Layover-Morgen auf den MORGIGEN Umlauf.
DUTY_TIM = {
    "pkNumber": "123456A",
    "rosterDays": [
        {"day": "2026-07-25T00:00:00Z", "events": [
            {"eventType": "FLIGHT", "eventCategory": "flight",
             "eventDetails": "DH LH1623", "wholeDay": False,
             "startTime": "2026-07-25T11:25:00Z", "startLocation": "KRK",
             "endTime": "2026-07-25T12:50:00Z", "endLocation": "MUC"},
            {"eventType": "FLIGHT", "eventCategory": "flight",
             "eventDetails": "LH2068", "wholeDay": False,
             "startTime": "2026-07-25T15:15:00Z", "startLocation": "MUC",
             "endTime": "2026-07-25T16:30:00Z", "endLocation": "HAM"},
            {"eventType": "HOTEL", "eventCategory": "hotel",
             "eventDetails": "Hotel", "wholeDay": False,
             "startTime": None, "endTime": None,
             "startLocation": "BRE", "endLocation": None}]},
        {"day": "2026-07-27T00:00:00Z", "events": [
            {"eventType": "GROUNDEVENT", "eventCategory": "OFFDUTY",
             "eventDetails": "FREE", "wholeDay": True,
             "startLocation": "MUC", "endLocation": "MUC"}]},
        {"day": "2026-07-28T00:00:00Z", "events": [
            {"eventType": "GROUNDEVENT", "eventCategory": "ABSENCE",
             "eventDetails": "U1", "wholeDay": True,
             "startLocation": "FRA", "endLocation": "FRA"}]},
    ],
}


def test_duty_events_to_ics_deadhead_and_locations():
    ics = fo.duty_events_to_ics(DUTY_TIM)
    assert ics is not None
    # Deadhead-Flag bleibt erhalten, Flugnummer im myTime-Format.
    assert 'DH LH 1623: KRK-MUC' in ics
    assert 'LOCATION:KRK - MUC' in ics
    assert 'LH 2068: MUC-HAM' in ics
    # Hotel ohne Zeiten mit IATA-LOCATION — Quelle für ical_layover_ort.
    assert 'Layover [BRE]' in ics
    assert 'LOCATION:BRE' in ics
    # Hotel-Station BRE passt zu KEINEM Leg (die Legs landen HAM) → Spanne
    # nicht ableitbar → Fallback aufs alte Datums-Verhalten für den ganzen
    # Lauf (erster Tag … letzter Tag+2), damit der Layover-Morgen seinen
    # Marker behält (Tim/KRK 25.07.).
    # OFFDUTY/FREE → myTime-Prosa.
    assert 'Off Day (FREE)' in ics
    # ABSENCE/U1 (Urlaub) → 'Absence (U1)' — iOS mappt ABSENCE auf Urlaub;
    # der nackte Code 'U1' machte den Urlaubstag sonst zum Dienst (Remo).
    assert 'Absence (U1)' in ics


def test_duty_events_to_ics_day_gets_routing_and_layover_ort():
    """Durch die echte Import-Pipeline: der Diensttag bekommt LOCATION-Routing
    (flownSectors-Quelle im Client) und den Layover-Ort aus dem Hotel-Event."""
    import app as backend
    ics = fo.duty_events_to_ics(DUTY_TIM)
    events = backend._parse_ics_to_events(ics)
    briefings, _ = backend._ics_events_to_briefings(events)
    b = briefings.get('2026-07-25') or {}
    loc = (b.get('ical_location') or '')
    assert 'KRK - MUC' in loc and 'MUC - HAM' in loc
    assert (b.get('ical_layover_ort') or '') == 'BRE'
    # Layover-Marker reist im Summary mit (Übernachtungs-Spanne Tag 1/2).
    assert 'Layover [BRE]' in (b.get('ical_summary') or '')


def test_duty_events_to_ics_empty():
    assert fo.duty_events_to_ics({'rosterDays': []}) is None
    assert fo.duty_events_to_ics(None) is None


# Miguels echter SFO-Tag (2026-07-28): BRIEFING kommt MIT startTime aber OHNE
# endTime — vorher fiel es dadurch in den Ganztags-Zweig und die echte
# Report-Zeit (06:35Z = 08:35 LT FRA) ging verloren; die App riet Abflug−60
# („falsche Briefing-Zeiten seit dem Update", Miguel + Thomas Radlmeier).
DUTY_BRIEFING = {
    "pkNumber": "123456A",
    "rosterDays": [
        {"day": "2026-07-28T00:00:00Z", "events": [
            {"eventType": "BRIEFING", "eventCategory": "DUTY",
             "eventDetails": "Briefing", "wholeDay": False,
             "startTime": "2026-07-28T06:35:00Z", "startLocation": "FRA",
             "endTime": None, "endLocation": "FRA"},
            {"eventType": "FLIGHT", "eventCategory": "FLIGHT",
             "eventDetails": "LH454", "wholeDay": False,
             "startTime": "2026-07-28T08:25:00Z", "startLocation": "FRA",
             "endTime": "2026-07-28T19:55:00Z", "endLocation": "SFO"}]},
    ],
}


def test_duty_events_to_ics_briefing_keeps_report_time():
    """BRIEFING ohne endTime bleibt ZEITBEHAFTET (echte Report-Zeit) und trägt
    den kanonischen LH-Marker „HH:MM LT Briefing FRA" in Station-Ortszeit —
    exakt die Form, die _corrected_briefing_start_iso + iOS lesen."""
    ics = fo.duty_events_to_ics(DUTY_BRIEFING)
    assert ics is not None
    # Zeitbehaftet mit der ECHTEN Report-Zeit, kein VALUE=DATE-Ganztag mehr.
    assert 'DTSTART:20260728T063500Z' in ics
    assert 'DTSTART;VALUE=DATE:20260728' not in ics
    # Kanonischer Marker in FRA-Ortszeit (Juli = UTC+2 → 08:35).
    assert '08:35 LT Briefing FRA' in ics


def test_duty_events_to_ics_briefing_drives_day_start():
    """Durch die Roster-Pipeline: der Tag beginnt am BRIEFING (06:35Z), nicht
    am Abflug (08:25Z) — genau das war der gemeldete Fehler."""
    import app as backend
    ics = fo.duty_events_to_ics(DUTY_BRIEFING)
    events = backend._parse_ics_to_events(ics)
    briefing = [e for e in events if 'LT Briefing' in (e.get('summary') or '')]
    assert len(briefing) == 1
    assert (briefing[0].get('start_iso') or '').startswith('2026-07-28T06:35')
    # Flug-Sektor bleibt unangetastet.
    secs = backend._build_ical_sectors(events)
    d = secs.get('2026-07-28') or []
    assert [(s['flight'], s['from'], s['to']) for s in d] == [('LH454', 'FRA', 'SFO')]


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
    assert ics and 'LH 400: FRA-JFK' in ics


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
    # LIVE 2026-07-23: 401 invalid_token beim Refresh = toter Grant (stale
    # Sandbox-Tokens nach Prod-Key-Wechsel) -> fatal
    monkeypatch.setattr('urllib.request.urlopen',
                        _mk(401, b'{"error":"invalid_token"}'))
    tok, err = fo._token_request(b'x=1')
    assert tok is None and err['fatal'] is True
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
    saves = []
    monkeypatch.setattr(fo, '_FATAL_GRACE_SEC', 0)   # kein echtes Warten im Test
    monkeypatch.setattr(fo, '_tokens_load', lambda tok: {
        'access': 'OLD', 'refresh': 'R', 'expires_at': _t.time() - 10})
    monkeypatch.setattr(fo, '_tokens_save', lambda tok, t: saves.append(dict(t)) or True)
    monkeypatch.setattr(fo, '_refresh', lambda r: (
        None, {'http': 400, 'oauth': 'invalid_grant', 'fatal': True}))
    pushes = []
    monkeypatch.setattr(fo, '_notify_relogin', lambda tok: pushes.append(tok))
    assert fo._valid_access('AT-U') is None
    # Save 1 = Cross-Container-Guard (Tokens unverändert), Save 2 = Flag.
    assert saves[0]['refresh_guard']['rt8'] == fo._rt8('R')
    assert saves[0]['access'] == 'OLD' and saves[0]['refresh'] == 'R'
    flagged = saves[-1]
    assert flagged.get('needs_relogin') is True and 'access' not in flagged
    assert pushes == ['AT-U']
    # needs_relogin → connected False + kein weiterer Refresh-Versuch
    monkeypatch.setattr(fo, '_tokens_load', lambda tok: dict(flagged))
    monkeypatch.setattr(fo, '_refresh', lambda r: (_ for _ in ()).throw(
        AssertionError('toter Grant darf nicht weiter refreshen')))
    assert fo.flightops_connected('AT-U') is False
    assert fo._valid_access('AT-U') is None


def test_refresh_fatal_race_loser_does_not_kill_winner(monkeypatch):
    """Cross-Container-Rotations-Race (Miguels Grant 2026-07-24 03:01Z tot):
    LH rotiert den Refresh-Token bei JEDEM Refresh. Unser Refresh mit 'R'
    schlägt fatal fehl (der PARALLELE Gewinner — Poll-Cron im anderen
    Container — hat 'R' schon verbraucht und R2/NEW persistiert). Der
    Verlierer darf dann: NICHT flaggen, NICHT pushen, den Gewinner-Stand
    NIE überschreiben — und liefert dessen frischen Access weiter."""
    import time as _t
    monkeypatch.setattr(fo, '_FATAL_GRACE_SEC', 0)
    states = [
        {'access': 'OLD', 'refresh': 'R', 'expires_at': _t.time() - 10},   # vor Lock
        {'access': 'OLD', 'refresh': 'R', 'expires_at': _t.time() - 10},   # im Lock
        {'access': 'NEW', 'refresh': 'R2', 'expires_at': _t.time() + 999}, # Grace-Reload
    ]
    monkeypatch.setattr(fo, '_tokens_load', lambda tok: states.pop(0))

    def _save_only_guard(tok, t):
        # Erlaubt ist NUR der Cross-Container-Guard-Save (Tokens identisch,
        # kein Flag) — alles andere würde den Gewinner-Stand überschreiben.
        assert t.get('access') == 'OLD' and t.get('refresh') == 'R'
        assert not t.get('needs_relogin') and t.get('refresh_guard')
        return True
    monkeypatch.setattr(fo, '_tokens_save', _save_only_guard)
    monkeypatch.setattr(fo, '_refresh', lambda r: (
        None, {'http': 401, 'oauth': 'invalid_token', 'fatal': True}))
    monkeypatch.setattr(fo, '_notify_relogin', lambda tok: (_ for _ in ()).throw(
        AssertionError('Race-Verlierer darf keinen Re-Login-Push senden')))
    assert fo._valid_access('AT-U') == 'NEW'


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
    # Einziger Save = Cross-Container-Guard; Tokens selbst UNANGETASTET →
    # nächster Versuch refresht normal weiter.
    assert len(saved) == 1
    assert saved[0]['access'] == 'OLD' and saved[0]['refresh'] == 'R'
    assert not saved[0].get('needs_relogin')
    assert saved[0]['refresh_guard']['rt8'] == fo._rt8('R')


def test_refresh_guard_loser_adopts_winner_without_lh_call(monkeypatch):
    """Cross-Container-Guard (2. Grant-Verlust 2026-07-25 02:54Z): Sieht ein
    Prozess einen FRISCHEN Guard für DENSELBEN Refresh-Token, wartet er und
    übernimmt die rotierten Tokens des Gewinners — KEIN LH-Call, KEIN Save
    (sonst würde der stale Stand den Gewinner clobbern)."""
    import time as _t
    monkeypatch.setattr(fo, '_GUARD_WAIT_SEC', 0)
    guarded = {'access': 'OLD', 'refresh': 'R', 'expires_at': _t.time() - 10,
               'refresh_guard': {'rt8': fo._rt8('R'), 'ts': _t.time()}}
    states = [dict(guarded), dict(guarded),                             # vor/im Lock
              {'access': 'NEW', 'refresh': 'R2', 'expires_at': _t.time() + 999}]
    monkeypatch.setattr(fo, '_tokens_load', lambda tok: states.pop(0))
    monkeypatch.setattr(fo, '_tokens_save', lambda tok, t: (_ for _ in ()).throw(
        AssertionError('Guard-Verlierer darf nichts zurückschreiben')))
    monkeypatch.setattr(fo, '_refresh', lambda r: (_ for _ in ()).throw(
        AssertionError('Guard-Verlierer darf den RT nicht doppelt verheizen')))
    assert fo._valid_access('AT-U') == 'NEW'


def test_refresh_claim_rpc_loser_adopts_winner(monkeypatch):
    """Atomares RPC-Claim: False = anderer Prozess refresht DIESEN RT gerade
    → warten, neu laden, Gewinner-Tokens übernehmen. KEIN LH-Call, KEIN
    Save — auch ganz OHNE lokalen Soft-Guard im geladenen Stand."""
    import time as _t
    monkeypatch.setattr(fo, '_GUARD_WAIT_SEC', 0)
    monkeypatch.setattr(fo, '_claim_refresh_sb', lambda tok, rt: False)
    old = {'access': 'OLD', 'refresh': 'R', 'expires_at': _t.time() - 10}
    states = [dict(old), dict(old),
              {'access': 'NEW', 'refresh': 'R2', 'expires_at': _t.time() + 999}]
    monkeypatch.setattr(fo, '_tokens_load', lambda tok: states.pop(0))
    monkeypatch.setattr(fo, '_tokens_save', lambda tok, t: (_ for _ in ()).throw(
        AssertionError('Claim-Verlierer darf nichts zurückschreiben')))
    monkeypatch.setattr(fo, '_refresh', lambda r: (_ for _ in ()).throw(
        AssertionError('Claim-Verlierer darf den RT nicht doppelt verheizen')))
    assert fo._valid_access('AT-U') == 'NEW'


def test_refresh_claim_rpc_winner_skips_guard_save(monkeypatch):
    """Claim True = Guard wurde server-seitig atomar gesetzt → kein
    zusätzlicher Soft-Guard-Save; einziger Save sind die rotierten Tokens."""
    saves = []
    monkeypatch.setattr(fo, '_claim_refresh_sb', lambda tok, rt: True)
    monkeypatch.setattr(fo, '_tokens_load', lambda tok: {
        'access': 'OLD', 'refresh': 'R', 'expires_at': 0})
    monkeypatch.setattr(fo, '_tokens_save', lambda tok, t: saves.append(dict(t)) or True)
    monkeypatch.setattr(fo, '_refresh', lambda r: (
        {'access': 'NEW', 'refresh': 'R2', 'scope': 's', 'expires_at': 9e18}, None))
    assert fo._valid_access('AT-U') == 'NEW'
    assert len(saves) == 1 and saves[0]['refresh'] == 'R2'
    assert 'refresh_guard' not in saves[0]


def test_refresh_claim_rpc_still_foreign_gives_up(monkeypatch):
    """Nach Warten+Reload ist der RT unverändert und das Claim IMMER NOCH
    fremd → aufgeben (None) statt busy-loopen oder LH anfassen."""
    import time as _t
    monkeypatch.setattr(fo, '_GUARD_WAIT_SEC', 0)
    monkeypatch.setattr(fo, '_claim_refresh_sb', lambda tok, rt: False)
    old = {'access': 'OLD', 'refresh': 'R', 'expires_at': _t.time() - 10}
    states = [dict(old), dict(old), dict(old)]
    monkeypatch.setattr(fo, '_tokens_load', lambda tok: states.pop(0))
    monkeypatch.setattr(fo, '_tokens_save', lambda tok, t: (_ for _ in ()).throw(
        AssertionError('kein Save ohne Claim')))
    monkeypatch.setattr(fo, '_refresh', lambda r: (_ for _ in ()).throw(
        AssertionError('kein LH-Call ohne Claim')))
    monkeypatch.setattr(fo, '_notify_relogin', lambda tok: (_ for _ in ()).throw(
        AssertionError('kein Push ohne Claim')))
    assert fo._valid_access('AT-U') is None


def test_refresh_guard_stale_does_not_block(monkeypatch):
    """Abgelaufener Guard (Refresher gecrasht o.ä.) darf den Refresh nicht
    dauerhaft blockieren — nach _REFRESH_GUARD_SEC wird normal refresht."""
    import time as _t
    saves = []
    monkeypatch.setattr(fo, '_tokens_load', lambda tok: {
        'access': 'OLD', 'refresh': 'R', 'expires_at': 0,
        'refresh_guard': {'rt8': fo._rt8('R'), 'ts': _t.time() - 9999}})
    monkeypatch.setattr(fo, '_tokens_save', lambda tok, t: saves.append(dict(t)) or True)
    monkeypatch.setattr(fo, '_refresh', lambda r: (
        {'access': 'NEW', 'refresh': 'R2', 'scope': 's', 'expires_at': 9e18}, None))
    assert fo._valid_access('AT-U') == 'NEW'
    # frischer Guard geschrieben, dann rotierte Tokens OHNE Guard persistiert
    assert saves[0]['refresh_guard']['ts'] > _t.time() - 60
    assert saves[-1]['refresh'] == 'R2' and 'refresh_guard' not in saves[-1]


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


def test_crewlist_serves_cache_when_grant_dead(monkeypatch):
    """Last-Good-Cache (Owner 2026-07-24): toter Grant → letzte Liste mit
    cached:true statt 401 — die Crew-Fläche ist nie leer."""
    monkeypatch.setattr(fo, '_valid_access', lambda tok: None)
    monkeypatch.setattr(fo, '_crew_cache_get', lambda tok, f, d: {
        'flight': 'LH582', 'date': '2026-07-26',
        'crew': [{'name': 'MUSTERMANN, MAX', 'pk': '1', 'category': 'CPT'}],
        'cached_at': 1234.0})
    import app as backend
    r = backend.app.test_client().post('/api/lh/flightops/crewlist/testtok-fo',
                                       json={'flight': 'LH582', 'date': '2026-07-26'})
    d = r.get_json()
    assert r.status_code == 200 and d['ok'] is True
    assert d['cached'] is True and d['crew'][0]['name'] == 'MUSTERMANN, MAX'


def test_crewlist_dead_grant_without_cache_stays_401(monkeypatch):
    monkeypatch.setattr(fo, '_valid_access', lambda tok: None)
    monkeypatch.setattr(fo, '_crew_cache_get', lambda tok, f, d: None)
    import app as backend
    r = backend.app.test_client().post('/api/lh/flightops/crewlist/testtok-fo',
                                       json={'flight': 'LH582', 'date': '2026-07-26'})
    assert r.status_code == 401
    assert r.get_json()['error'] == 'not_connected'


def test_crewlist_success_populates_cache(monkeypatch):
    monkeypatch.setattr(fo, '_valid_access', lambda tok: 'ACC')
    monkeypatch.setattr(fo, '_resolve_link_params', lambda *a, **k: {
        'accessCode': 'SECRET42', 'departureAirport': 'FRA',
        'arrivalAirport': 'CAI'})
    monkeypatch.setattr(fo, 'crew_list', lambda *a, **k: {'crewMembers': []})
    monkeypatch.setattr(fo, 'parse_crew_list', lambda resp: [
        {'name': 'MUSTERMANN, MAX', 'pk': '1', 'category': 'CPT'}])
    monkeypatch.setattr(fo, '_match_aerox_profiles', lambda crew: {})
    put = {}
    monkeypatch.setattr(fo, '_crew_cache_put',
                        lambda tok, f, d, crew: put.update(
                            {'flight': f, 'date': d, 'n': len(crew)}))
    import app as backend
    r = backend.app.test_client().post('/api/lh/flightops/crewlist/testtok-fo',
                                       json={'flight': 'LH582', 'date': '2026-07-26'})
    assert r.status_code == 200 and r.get_json()['ok'] is True
    assert put == {'flight': 'LH582', 'date': '2026-07-26', 'n': 1}


def test_crew_cache_put_get_lru(monkeypatch):
    """Cache-Helper direkt: Put/Get über den Profil-Mirror + LRU-Kappung."""
    store = {}
    import app as backend
    monkeypatch.setattr(backend, '_profile_load',
                        lambda tok: {'profile': dict(store)})
    monkeypatch.setattr(backend, '_profile_save',
                        lambda tok, prof: store.update(prof) or True)
    for i in range(fo._CREW_CACHE_MAX + 3):
        fo._crew_cache_put('AT-U', f'LH{i}', '2026-07-26',
                           [{'name': f'N{i}'}])
    lst = store['flightops_crew_cache']
    assert len(lst) == fo._CREW_CACHE_MAX          # LRU-gekappt
    assert fo._crew_cache_get('AT-U', 'LH0', '2026-07-26') is None  # rausgealtert
    hit = fo._crew_cache_get('AT-U', f'LH{fo._CREW_CACHE_MAX + 2}', '2026-07-26')
    assert hit and hit['crew'][0]['name'] == f'N{fo._CREW_CACHE_MAX + 2}'


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


def test_exchange_endpoint_unwraps_token_tuple(monkeypatch):
    """Regression Live-500 2026-07-23: _token_request liefert (tok, err) —
    der Exchange-Endpoint muss das reine Token-Dict speichern. Mockt bewusst
    _token_request (NICHT _exchange_code), damit der echte Unwrap-Pfad läuft."""
    monkeypatch.setattr(fo, '_KEY', 'CID'); monkeypatch.setattr(fo, '_SECRET', 'SEC')
    fo._flow_put('STATE-TUP', 'VERIFIER', 'AT-USER-TUP')
    monkeypatch.setattr(fo, '_token_request', lambda body: (
        {'access': 'ACC2', 'refresh': 'REF2', 'scope': 'sc', 'expires_at': 9e18}, None))
    saved = {}
    monkeypatch.setattr(fo, '_tokens_save', lambda tok, t: saved.update({tok: t}) or True)
    import app as backend
    r = backend.app.test_client().post('/api/lh/flightops/oauth/exchange',
                                       json={'code': 'C', 'state': 'STATE-TUP'})
    assert r.status_code == 200 and r.get_json()['connected'] is True
    assert saved['AT-USER-TUP']['access'] == 'ACC2'
    # und der Fehlerfall bleibt ein sauberer 502, kein 500
    fo._flow_put('STATE-TUP2', 'VERIFIER', 'AT-USER-TUP')
    monkeypatch.setattr(fo, '_token_request', lambda body: (
        None, {'http': 400, 'oauth': 'invalid_grant', 'fatal': True}))
    r2 = backend.app.test_client().post('/api/lh/flightops/oauth/exchange',
                                        json={'code': 'C', 'state': 'STATE-TUP2'})
    assert r2.status_code == 502


def test_refresh_fatal_race_guard_skips_flag_when_rotated(monkeypatch):
    """LH rotiert den Refresh-Token bei JEDEM Refresh (live 2026-07-23) — ein
    paralleler Refresh darf beim Verlierer des Races KEIN needs_relogin
    ausloesen. Simuliert: beim Re-Load nach dem fatal-Fehler liegt schon ein
    NEUER (rotierter) Refresh-Token mit gueltigem Access im Store."""
    import time as _t
    stale = {'access': 'OLD', 'refresh': 'R1', 'expires_at': 0}
    fresh = {'access': 'NEWACC', 'refresh': 'R2', 'expires_at': _t.time() + 3000}
    loads = [dict(stale), dict(fresh)]
    monkeypatch.setattr(fo, '_tokens_load', lambda tok: loads.pop(0))
    monkeypatch.setattr(fo, '_tokens_save', lambda tok, t: (_ for _ in ()).throw(
        AssertionError('Race darf nichts speichern/flaggen')))
    monkeypatch.setattr(fo, '_refresh', lambda r: (
        None, {'http': 401, 'oauth': 'invalid_token', 'fatal': True}))
    monkeypatch.setattr(fo, '_notify_relogin', lambda tok: (_ for _ in ()).throw(
        AssertionError('Race darf keinen Re-Login-Push senden')))
    assert fo._valid_access('AT-U') == 'NEWACC'


def test_crewlist_endpoint_attaches_aerox_profiles(monkeypatch):
    """Owner 2026-07-23: Crew-Mitglieder mit AeroX-Account bekommen ihr
    PUBLIC-Profil (token/avatar/…) an den Listen-Eintrag — Match primär via
    LH-Personalnummer."""
    _pass_auth_gate(monkeypatch)
    monkeypatch.setattr(fo, '_KEY', 'k'); monkeypatch.setattr(fo, '_SECRET', 's')
    monkeypatch.setattr(fo, '_valid_access', lambda tok: 'ACC')
    monkeypatch.setattr(fo, '_links_load',
                        lambda tok: fo.extract_duty_links(DUTY_LINKS))
    monkeypatch.setattr(fo, 'crew_list',
                        lambda tok, f, d, dep, arr, ac: REAL_CREWLIST)
    monkeypatch.setattr(fo, '_match_aerox_profiles', lambda members: {
        '095599C': {'token': 'AT-SOEREN', 'name': 'Soeren Roenelt',
                    'airline': 'Lufthansa', 'homebase': 'FRA',
                    'position': 'CP', 'avatar_url': 'https://cdn/x.jpg'}})
    import app as backend
    r = backend.app.test_client().post('/api/lh/flightops/crewlist/AT-U',
                                       headers={'Authorization': 'Bearer AT-U'},
                                       json={'flight': 'LH400',
                                             'date': '2026-07-24'})
    crew = r.get_json()['crew']
    assert crew[0]['aerox']['token'] == 'AT-SOEREN'
    assert crew[0]['aerox']['avatar_url'] == 'https://cdn/x.jpg'
    assert 'aerox' not in crew[1]          # kein Match → Feld fehlt


def test_store_own_pk_idempotent(monkeypatch):
    import app as backend
    saved = []
    monkeypatch.setattr(backend, '_profile_load',
                        lambda tok: {'profile': {'name': 'M'}})
    monkeypatch.setattr(backend, '_profile_save',
                        lambda tok, prof: saved.append(dict(prof)) or True)
    fo._store_own_pk('AT-U', '123456A')
    assert saved[-1]['lh_pk_number'] == '123456A'
    # unverändert → kein zweiter Save
    monkeypatch.setattr(backend, '_profile_load',
                        lambda tok: {'profile': {'lh_pk_number': '123456A'}})
    n = len(saved)
    fo._store_own_pk('AT-U', '123456A')
    assert len(saved) == n
    fo._store_own_pk('AT-U', '')       # leer → no-op
    assert len(saved) == n


# ─────────────────────────────────────────────────────────────────────────────
# REGRESSION 2026-07-26 — echte Prod-Payloads (COMMON_DUTY_EVENTS, live gezogen)
#
# (1) Tibor „Tag 2/2 in Athen": Hotel-Events kommen OHNE Zeiten und EINES PRO
#     NACHT. Der alte Datums-Event Tag..Tag+2 pro Hotel-Event machte N konstant
#     2, stapelte bei Mehr-Nacht-Layovern „(Tag 2/2) · (Tag 1/2)" auf EINEN Tag
#     und erfand einen Layover-Tag hinter der letzten Nacht.
# (2) „B4 löst einen freien Tag aus": Bürodienst kommt als
#     eventCategory=GROUNDDUTY mit dem NACKTEN Hauscode in eventDetails.
# ─────────────────────────────────────────────────────────────────────────────

# Echter Payload eines verbundenen FlightOps-Users (anonymisiert): 3 Nächte KIX,
# Hotel-Event an DREI aufeinanderfolgenden rosterDays, ein Tag OHNE Events
# mittendrin, Weiterflug erst am 29.07.
DUTY_KIX_MULTINIGHT = {
    "rosterDays": [
        {"day": "2026-07-25Z", "events": [
            {"eventType": "BRIEFING", "eventCategory": "briefing",
             "eventDetails": "Briefing", "wholeDay": False,
             "startTime": "2026-07-25T08:40:00Z", "startLocation": "MUC",
             "endTime": None, "endLocation": None},
            {"eventType": "FLIGHT", "eventCategory": "flight",
             "eventDetails": "LH742", "wholeDay": False,
             "startTime": "2026-07-25T10:39:00Z", "startLocation": "MUC",
             "endTime": "2026-07-25T22:20:00Z", "endLocation": "KIX"},
            {"eventType": "HOTEL", "eventCategory": "hotel",
             "eventDetails": "Hotel", "wholeDay": False, "startTime": None,
             "startLocation": "KIX", "endTime": None, "endLocation": None}]},
        {"day": "2026-07-26Z", "events": [
            {"eventType": "HOTEL", "eventCategory": "hotel",
             "eventDetails": "Hotel", "wholeDay": False, "startTime": None,
             "startLocation": "KIX", "endTime": None, "endLocation": None}]},
        {"day": "2026-07-27Z", "events": [
            {"eventType": "HOTEL", "eventCategory": "hotel",
             "eventDetails": "Hotel", "wholeDay": False, "startTime": None,
             "startLocation": "KIX", "endTime": None, "endLocation": None}]},
        {"day": "2026-07-28Z", "events": []},
        {"day": "2026-07-29Z", "events": [
            {"eventType": "FLIGHT", "eventCategory": "flight",
             "eventDetails": "LH743", "wholeDay": False,
             "startTime": "2026-07-29T00:30:00Z", "startLocation": "KIX",
             "endTime": "2026-07-29T14:40:00Z", "endLocation": "MUC"}]},
    ]
}

# Tibors echter Athen-Umlauf (26.–28.07.2026): EINE Nacht, Hotel am Ankunftstag.
DUTY_TIBOR_ATH = {
    "rosterDays": [
        {"day": "2026-07-26Z", "events": [
            {"eventType": "BRIEFING", "eventCategory": "briefing",
             "eventDetails": "Briefing", "wholeDay": False,
             "startTime": "2026-07-26T14:35:00Z", "startLocation": "FRA"},
            {"eventType": "FLIGHT", "eventCategory": "flight",
             "eventDetails": "LH690", "wholeDay": False,
             "startTime": "2026-07-26T16:35:00Z", "startLocation": "FRA",
             "endTime": "2026-07-26T20:15:00Z", "endLocation": "TLV"}]},
        {"day": "2026-07-27Z", "events": [
            {"eventType": "FLIGHT", "eventCategory": "flight",
             "eventDetails": "LH691", "wholeDay": False,
             "startTime": "2026-07-26T22:10:00Z", "startLocation": "TLV",
             "endTime": "2026-07-27T00:20:00Z", "endLocation": "ATH"},
            {"eventType": "HOTEL", "eventCategory": "hotel",
             "eventDetails": "Hotel", "wholeDay": False, "startTime": None,
             "startLocation": "ATH", "endTime": None, "endLocation": None}]},
        {"day": "2026-07-28Z", "events": [
            {"eventType": "FLIGHT", "eventCategory": "flight",
             "eventDetails": "LH691", "wholeDay": False,
             "startTime": "2026-07-28T01:10:00Z", "startLocation": "ATH",
             "endTime": "2026-07-28T04:15:00Z", "endLocation": "FRA"}]},
    ]
}


def _briefings(payload):
    import app as backend
    ics = fo.duty_events_to_ics(payload)
    assert ics is not None
    return backend._ics_events_to_briefings(backend._parse_ics_to_events(ics))[0]


def test_multinight_layover_emits_one_vevent_with_real_span():
    """3 Hotel-Events derselben Nachtfolge → GENAU EIN Layover-VEVENT über die
    echte Spanne Ankunft…Weiterflug (nicht 3× Tag..Tag+2)."""
    ics = fo.duty_events_to_ics(DUTY_KIX_MULTINIGHT)
    assert ics.count('SUMMARY:Layover [KIX]') == 1
    assert 'DTSTART:20260725T222000Z' in ics      # Ankunft LH742
    assert 'DTEND:20260729T003000Z' in ics        # Abflug LH743
    # kein Datums-Event mehr für das Hotel
    assert 'DTSTART;VALUE=DATE:20260726' not in ics


def test_multinight_layover_day_labels_are_honest():
    """N ist die ECHTE Nächtezahl (früher immer 2) und kein Tag trägt zwei
    widersprüchliche Layover-Segmente."""
    b = _briefings(DUTY_KIX_MULTINIGHT)
    assert 'Layover [KIX] (Tag 1/4)' in (b['2026-07-26'].get('ical_summary') or '')
    assert 'Layover [KIX] (Tag 2/4)' in (b['2026-07-27'].get('ical_summary') or '')
    assert 'Layover [KIX] (Tag 3/4)' in (b['2026-07-28'].get('ical_summary') or '')
    assert 'Layover [KIX] (Tag 4/4)' in (b['2026-07-29'].get('ical_summary') or '')
    for d in ('2026-07-26', '2026-07-27', '2026-07-28', '2026-07-29'):
        s = b[d].get('ical_summary') or ''
        assert s.count('Layover [KIX]') == 1, (d, s)
    # Der Layover-Ort steht jetzt an JEDEM echten Hotel-Tag (vorher nur Tag 1
    # je Hotel-Event, weil ohne Zeiten der except-Zweig griff) — der Heimkehr-
    # Morgen (Abflug 02:30 Ortszeit Berlin) zählt korrekt NICHT mit.
    assert b['2026-07-26'].get('ical_layover_ort') == 'KIX'
    assert b['2026-07-27'].get('ical_layover_ort') == 'KIX'
    assert b['2026-07-28'].get('ical_layover_ort') == 'KIX'
    assert b['2026-07-29'].get('ical_layover_ort') != 'KIX'


def test_tibor_athen_single_night_labels():
    """Tibors Athen-Tour: Tag 1/2 am Ankunftstag, Tag 2/2 am Rückflugtag —
    und KEIN Layover-Segment an einem Tag vor Tour-Start."""
    b = _briefings(DUTY_TIBOR_ATH)
    assert 'Layover [ATH]' not in (b['2026-07-26'].get('ical_summary') or '')
    assert 'Layover [ATH] (Tag 1/2)' in (b['2026-07-27'].get('ical_summary') or '')
    assert 'Layover [ATH] (Tag 2/2)' in (b['2026-07-28'].get('ical_summary') or '')
    assert '2026-07-29' not in b          # kein erfundener Tag hinter der Tour
    assert b['2026-07-27'].get('ical_layover_ort') == 'ATH'


def _f(det, s, sl, e, el):
    return {"eventType": "FLIGHT", "eventCategory": "flight",
            "eventDetails": det, "wholeDay": False, "startTime": s,
            "startLocation": sl, "endTime": e, "endLocation": el}


def _hotel(station):
    return {"eventType": "HOTEL", "eventCategory": "hotel",
            "eventDetails": "Hotel", "wholeDay": False, "startTime": None,
            "startLocation": station, "endTime": None, "endLocation": None}


def test_hotel_without_derivable_next_leg_falls_back_to_dates():
    """Layover am Rand des Import-Fensters (Weiterflug noch nicht im Import):
    Spanne nicht bestimmbar → EIN Datums-Event fuer den ganzen Lauf. Altes
    Verhalten, aber garantiert nur EINMAL — kein Stapeln."""
    payload = {"rosterDays": [
        {"day": "2026-07-25Z", "events": [
            _f("LH742", "2026-07-25T10:39:00Z", "MUC",
               "2026-07-25T22:20:00Z", "KIX"), _hotel("KIX")]},
    ]}
    ics = fo.duty_events_to_ics(payload)
    assert ics.count('SUMMARY:Layover [KIX]') == 1
    assert 'DTSTART;VALUE=DATE:20260725' in ics
    assert 'DTEND;VALUE=DATE:20260727' in ics


def test_layover_span_stops_when_crew_leaves_otherwise():
    """ADVERSARIAL (Review 26.07.): kommt die Crew ohne Flug-Leg heim (Bahn),
    darf die Suche NICHT bis zum naechsten Abflug ab dieser Station
    weiterlaufen — sonst entsteht ein Wochen-Layover, das freie Tage als
    Layover stempelt (reproduziert: 21 Tage, 18 freie Tage betroffen)."""
    payload = {"rosterDays": [
        {"day": "2026-07-25Z", "events": [
            _f("LH100", "2026-07-25T10:00:00Z", "FRA",
               "2026-07-25T11:00:00Z", "CGN"), _hotel("CGN")]},
        {"day": "2026-07-26Z", "events": [
            _f("LH200", "2026-07-26T09:00:00Z", "FRA",
               "2026-07-26T10:00:00Z", "MUC")]},
        {"day": "2026-08-14Z", "events": [
            _f("LH300", "2026-08-14T08:00:00Z", "CGN",
               "2026-08-14T09:00:00Z", "FRA")]},
    ]}
    b = _briefings(payload)
    lay = [d for d in b if (b[d].get('ical_layover_ort') or '') == 'CGN']
    assert len(lay) <= 2, lay
    assert '2026-08-01' not in b


def test_same_station_twice_in_one_day_keeps_the_real_night():
    """ADVERSARIAL: FRA-MUC-FRA morgens + FRA-MUC abends + Hotel MUC. Wird der
    ABFLUG am Hotel-Tag verankert, gewinnt der Morgen-Rueckflug → 1-h-Spanne,
    die 6-h-Regel verwirft sie, und die echte Nacht verschwindet komplett."""
    payload = {"rosterDays": [
        {"day": "2026-07-25Z", "events": [
            _f("LH1", "2026-07-25T04:00:00Z", "FRA", "2026-07-25T05:00:00Z", "MUC"),
            _f("LH2", "2026-07-25T06:00:00Z", "MUC", "2026-07-25T07:00:00Z", "FRA"),
            _f("LH3", "2026-07-25T18:00:00Z", "FRA", "2026-07-25T19:00:00Z", "MUC"),
            _hotel("MUC")]},
        {"day": "2026-07-26Z", "events": [
            _f("LH4", "2026-07-26T07:00:00Z", "MUC", "2026-07-26T08:00:00Z", "FRA")]},
    ]}
    ics = fo.duty_events_to_ics(payload)
    assert 'DTSTART:20260725T190000Z' in ics       # der ABEND-Flug
    assert 'DTEND:20260726T070000Z' in ics
    b = _briefings(payload)
    assert b['2026-07-25'].get('ical_layover_ort') == 'MUC'


def test_turnaround_out_of_the_layover_station_keeps_every_night():
    """ADVERSARIAL: JFK-Nacht, JFK-YYZ-JFK-Turnaround, JFK-Nacht, Heimflug.
    Der Turnaround darf den Aufenthalt nicht zerschneiden."""
    payload = {"rosterDays": [
        {"day": "2026-07-25Z", "events": [
            _f("LH400", "2026-07-25T12:00:00Z", "FRA",
               "2026-07-25T19:00:00Z", "JFK"), _hotel("JFK")]},
        {"day": "2026-07-26Z", "events": [
            _f("LH8000", "2026-07-26T10:00:00Z", "JFK",
               "2026-07-26T11:30:00Z", "YYZ"),
            _f("LH8001", "2026-07-26T14:00:00Z", "YYZ",
               "2026-07-26T15:30:00Z", "JFK"), _hotel("JFK")]},
        {"day": "2026-07-28Z", "events": [
            _f("LH401", "2026-07-28T01:00:00Z", "JFK",
               "2026-07-28T09:00:00Z", "FRA")]},
    ]}
    ics = fo.duty_events_to_ics(payload)
    assert ics.count('SUMMARY:Layover [JFK]') == 1
    assert 'DTSTART:20260725T190000Z' in ics       # ERSTE Ankunft
    assert 'DTEND:20260728T010000Z' in ics         # LETZTER Abflug
    b = _briefings(payload)
    assert b['2026-07-27'].get('ical_layover_ort') == 'JFK'


def test_leg_timestamps_with_unexpected_shape_degrade_safely():
    """Nur 'YYYY-MM-DDTHH:MM:SSZ' ist lexikografisch sicher vergleichbar.
    Offsets/Millisekunden duerfen nie eine falsche Spanne erzeugen."""
    payload = {"rosterDays": [
        {"day": "2026-07-25Z", "events": [
            _f("LH1", "2026-07-25T10:00:00+02:00", "FRA",
               "2026-07-25T11:00:00.500Z", "KIX"), _hotel("KIX")]},
    ]}
    ics = fo.duty_events_to_ics(payload)
    assert 'DTSTART;VALUE=DATE:20260725' in ics    # Fallback, keine Zeit-Spanne
    assert 'SUMMARY:Layover [KIX]' in ics


def test_short_turnaround_hotel_does_not_set_layover_ort():
    """Die 6-h-Mindestbodenzeit-Regel lief für FlightOps NIE (Datums-Events
    haben keine start_iso → der except-Zweig griff immer). Mit echter Spanne
    greift sie wieder: <6 h Boden ⇒ kein Layover-Ort."""
    payload = {"rosterDays": [
        {"day": "2026-07-26Z", "events": [
            {"eventType": "FLIGHT", "eventCategory": "flight",
             "eventDetails": "LH999", "wholeDay": False,
             "startTime": "2026-07-26T18:00:00Z", "startLocation": "FRA",
             "endTime": "2026-07-26T21:15:00Z", "endLocation": "TLV"},
            {"eventType": "HOTEL", "eventCategory": "hotel",
             "eventDetails": "Hotel", "wholeDay": False, "startTime": None,
             "startLocation": "TLV", "endTime": None, "endLocation": None},
            {"eventType": "FLIGHT", "eventCategory": "flight",
             "eventDetails": "LH998", "wholeDay": False,
             "startTime": "2026-07-26T23:10:00Z", "startLocation": "TLV",
             "endTime": "2026-07-27T02:30:00Z", "endLocation": "FRA"}]},
    ]}
    b = _briefings(payload)
    for d in b:
        assert b[d].get('ical_layover_ort') != 'TLV', (d, b[d])


# ── BUG 4 · B4 = Bürodienst ─────────────────────────────────────────────────
DUTY_OFFICE_B4 = {
    "rosterDays": [
        {"day": "2026-07-25Z", "events": [
            {"eventType": "GROUNDEVENT", "eventCategory": "GROUNDDUTY",
             "eventDetails": "B4", "wholeDay": False,
             "startTime": "2026-07-25T06:30:00Z", "startLocation": "MUC",
             "endTime": "2026-07-25T15:00:00Z", "endLocation": "MUC"}]},
        {"day": "2026-07-26Z", "events": [
            {"eventType": "GROUNDEVENT", "eventCategory": "GROUNDDUTY",
             "eventDetails": "EMCRM", "wholeDay": False,
             "startTime": "2026-07-26T06:30:00Z", "startLocation": "MUC",
             "endTime": "2026-07-26T15:00:00Z", "endLocation": "MUC"}]},
    ]
}


def test_groundduty_office_code_gets_mytime_prose():
    """FlightOps schickt den nackten Hauscode; myTime schreibt für DENSELBEN
    Tag „Office Day (B4)". Wir minten die myTime-Prosa — dadurch greift die
    bestehende Dienst-Erkennung ohne Sonderweg. Andere GROUNDDUTY-Details
    (Training) bleiben unverändert roh."""
    ics = fo.duty_events_to_ics(DUTY_OFFICE_B4)
    assert 'SUMMARY:Office Day (B4)' in ics
    assert 'SUMMARY:EMCRM' in ics
    b = _briefings(DUTY_OFFICE_B4)
    assert b['2026-07-25'].get('ical_summary') == 'Office Day (B4)'
    # Zeiten des Bürodienstes bleiben erhalten (Dienst-Fenster 06:30–15:00Z).
    assert (b['2026-07-25'].get('ical_start_iso') or '').startswith('2026-07-25T06:30')
    assert (b['2026-07-25'].get('ical_end_iso') or '').startswith('2026-07-25T15:00')


def test_office_codes_are_the_agreed_marker_contract():
    """MARKER-VERTRAG mit iOS (Models/RosterEventClassifier.swift),
    Owner-Entscheid 26.07.2026: Buerodienst = Token `B` + GENAU EINE Ziffer
    2-9. Divergiert das, klassifizieren App und Backend denselben Tag
    verschieden — genau die Bug-Klasse, die hier gefixt wird."""
    for n in range(2, 10):
        assert fo.is_office_day_code('B%d' % n)
    # AUSNAHMEN aus dem CRS-Handbuch: nacktes B = Betriebsunfall (Abwesenheit),
    # B1 = Teilzeit-Vertragsart (kein Tagessymbol).
    assert not fo.is_office_day_code('B')
    assert not fo.is_office_day_code('B1')
    assert not fo.is_office_day_code('B0')
    # Token-Grenzen: kein Praefix-Match.
    for bad in ('B45', 'B455', 'B4A', 'AB4', 'B 4', ''):
        assert not fo.is_office_day_code(bad), bad


def test_office_codes_count_as_ground_duty_evidence():
    import app as backend
    for n in range(2, 10):
        code = 'B%d' % n
        assert backend._summary_has_ground_duty(code), code
        assert backend._summary_has_ground_duty('OFFICE DAY (%s)' % code)
    # Praefix darf NICHT zuenden.
    assert not backend._summary_has_ground_duty('B455')
    assert not backend._summary_has_ground_duty('B45')
    # CRS-Ausnahmen: nacktes B (Betriebsunfall) und B1 (Vertragsart).
    assert not backend._summary_has_ground_duty('B')
    assert not backend._summary_has_ground_duty('B1')
    assert not backend._summary_has_ground_duty('ABSENCE (B)')
    # echte freie Tage bleiben frei
    assert not backend._summary_has_ground_duty('OFF DAY')
    assert not backend._summary_has_ground_duty('OFF DAY (OF)')
    assert not backend._summary_has_ground_duty('OFF DAY (ORTSTAG)')


def test_merged_off_plus_office_day_is_not_free():
    """DER eigentliche Bug: myTime legt am selben Tag zwei VEVENTs an, der
    Import merged sie zu „Off Day (OF) - B4" — das Off-Segment stempelte den
    Tag frei, obwohl daneben ein Buerodienst steht. Muss in BEIDEN Backend-
    Pfaden greifen (Kalender-klass UND Crew-/Family-Marker)."""
    import app as backend
    up = 'OFF DAY (OF) · B4'
    assert backend._summary_has_ground_duty(up)
    assert backend._summary_has_ground_duty('OFF DAY (OF) · B7')
    klass = ('OFF' if ('OFF DAY' in up
                       and not backend._summary_has_ground_duty(up)) else None)
    assert klass is None
    assert duty_from_roster_day(None, 'Off Day (OF) · B4') != 'free'
    assert duty_from_roster_day(None, 'Off Day (B4)') != 'free'
    # Der reine freie Tag bleibt frei — sonst waere der Guard zu breit.
    assert duty_from_roster_day(None, 'Off Day (OF)') == 'free'
    assert duty_from_roster_day(None, 'Off Day (FREE) · Off Day (==)') == 'free'


def test_office_day_klass_is_not_off():
    import app as backend
    up = 'OFFICE DAY (B4)'
    klass = ('OFF' if ('OFF DAY' in up
                       and not backend._summary_has_ground_duty(up)) else None)
    assert klass is None
    assert duty_from_roster_day(None, 'Office Day (B4)') != 'free'


def test_groundduty_prose_only_for_an_exact_code():
    """„MED B4 MUC" ist kein Buerotag — der Code darf das Event nicht
    umetikettieren (er bleibt aber Boden-Dienst-Beweis)."""
    import app as backend
    payload = {"rosterDays": [{"day": "2026-07-26Z", "events": [
        {"eventType": "GROUNDEVENT", "eventCategory": "GROUNDDUTY",
         "eventDetails": "MED B4 MUC", "wholeDay": False,
         "startTime": "2026-07-26T06:30:00Z", "startLocation": "MUC",
         "endTime": "2026-07-26T15:00:00Z", "endLocation": "MUC"}]}]}
    ics = fo.duty_events_to_ics(payload)
    assert 'SUMMARY:MED B4 MUC' in ics
    assert 'Office Day' not in ics
    assert backend._summary_has_ground_duty('MED B4 MUC')
