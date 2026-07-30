"""LH FlightOps Crew API (Engine B): Authorization Code + PKCE + Duty-Events→ICS.
Rein offline: PKCE-Korrektheit, Authorize-URL-Bau, Token-Exchange (HTTP gemockt),
Duty-Events-Parser gegen die dokumentierte Response-Shape. Kein Live-Call."""
import base64
import datetime as _dtmod
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
    monkeypatch.setattr(fo, '_api_get',
                        lambda tok, path, params=None, interactive=False: {
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
                        lambda tok, path, params=None, interactive=False:
                        calls.append((path, params)) or {})
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
    monkeypatch.setattr(fo, '_api_get',
                        lambda tok, path, params=None, interactive=False:
                        {'called': path, 'interactive': interactive})
    assert fo.service_get('T', 'DROP TABLE') is None
    assert fo.service_get('T', 'common_crewlist') == {
        'called': '/COMMON_CREWLIST', 'interactive': False}
    # Nutzer-Taps dürfen den reservierten Headroom nutzen (2026-07-29).
    assert fo.service_get('T', 'common_crewlist',
                          interactive=True)['interactive'] is True


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


# ── Single-Refresher-Architektur (Umbau 2026-07-27 nach Grant-Burn #4) ──────
# Rotieren darf NUR noch der Refresher-Loop (Poll-Container, ein Thread).
# Diese Tests registrieren den Test-Thread über _as_refresher als DEN
# Rotations-Thread — exakt das, was _refresher_main im Poll-Container tut.
import contextlib as _ctx
import threading as _thr


@_ctx.contextmanager
def _as_refresher():
    old = fo._REFRESHER_THREAD_ID[0]
    fo._REFRESHER_THREAD_ID[0] = _thr.get_ident()
    try:
        yield
    finally:
        fo._REFRESHER_THREAD_ID[0] = old


def test_valid_access_never_refreshes(monkeypatch):
    """Web-Worker-/Cron-Lesepfad: abgelaufener AT ⇒ None + Vormerkung für den
    Refresher — NIEMALS ein eigener Refresh oder auch nur ein Claim. Die
    Racefläche aus Burn #1–#4 (jeder Prozess rotiert selbst) existiert nicht
    mehr."""
    import time as _t
    fo._refresh_wanted.clear()
    monkeypatch.setattr(fo, '_tokens_load', lambda tok, fresh=False: {
        'access': 'OLD', 'refresh': 'R', 'expires_at': _t.time() - 10})
    monkeypatch.setattr(fo, '_refresh', lambda r: (_ for _ in ()).throw(
        AssertionError('Lesepfad darf NIE refreshen')))
    monkeypatch.setattr(fo, '_claim_refresh_sb', lambda tok, rt: (_ for _ in ()).throw(
        AssertionError('Lesepfad darf nicht einmal claimen')))
    monkeypatch.setattr(fo, '_tokens_save', lambda tok, t: (_ for _ in ()).throw(
        AssertionError('Lesepfad darf nichts schreiben')))
    assert fo._valid_access('AT-U') is None
    assert 'AT-U' in fo._refresh_wanted
    st, acc = fo._access_state('AT-U')
    assert st == 'pending' and acc is None
    # gültiger AT wird normal serviert
    monkeypatch.setattr(fo, '_tokens_load', lambda tok, fresh=False: {
        'access': 'ACC', 'refresh': 'R', 'expires_at': _t.time() + 999})
    assert fo._valid_access('AT-U') == 'ACC'
    fo._refresh_wanted.clear()


def test_refresh_refuses_outside_refresher_thread(monkeypatch):
    """Choke-Point-Gate: _refresh — die EINZIGE Stelle, die grant_type=
    refresh_token sendet — verweigert außerhalb des Refresher-Threads,
    OHNE einen HTTP-Call abzusetzen. Damit ist RT-Reuse durch fremde
    Code-Pfade strukturell unmöglich."""
    reqs = []
    monkeypatch.setattr(fo, '_token_request',
                        lambda body: reqs.append(body) or (None, {
                            'http': 503, 'oauth': None, 'fatal': False}))
    assert fo._REFRESHER_THREAD_ID[0] != _thr.get_ident()
    tok, err = fo._refresh('R')
    assert tok is None and err.get('refused') is True
    assert reqs == []                    # Gate griff VOR dem Token-Request
    # innerhalb des Gates geht der Call normal raus
    with _as_refresher():
        fo._refresh('R')
    assert len(reqs) == 1 and b'refresh_token' in reqs[0]


def test_refresher_claim_unavailable_is_fail_closed(monkeypatch):
    """Burn-#4-Kernfix: Claim-Infra down/degradiert ⇒ KEIN LH-Call, Refresh
    vertagt. (Vorher refreshte der blinde Soft-Guard-Fallback trotzdem —
    254/571 tote Grants.) Es gibt KEINEN Soft-Guard mehr."""
    import time as _t
    monkeypatch.setattr(fo, '_claim_refresh_sb', lambda tok, rt: None)
    monkeypatch.setattr(fo, '_tokens_load', lambda tok, fresh=False: {
        'access': 'OLD', 'refresh': 'R', 'expires_at': _t.time() - 10})
    monkeypatch.setattr(fo, '_refresh', lambda r: (_ for _ in ()).throw(
        AssertionError('ohne bestätigtes Claim KEIN LH-Call (fail-closed)')))
    monkeypatch.setattr(fo, '_tokens_save', lambda tok, t: (_ for _ in ()).throw(
        AssertionError('fail-closed schreibt auch nichts')))
    with _as_refresher():
        assert fo._refresher_refresh_grant('AT-U') == 'skipped_claim_unavailable'


def test_refresher_claim_foreign_no_call(monkeypatch):
    """Claim False (Alt-Code-Container im Deploy-Übergang hält den RT):
    bewusst NICHTS tun — kein LH-Call, kein Save, kein Flag. Der nächste
    Tick liest einfach das Ergebnis des anderen."""
    import time as _t
    monkeypatch.setattr(fo, '_claim_refresh_sb', lambda tok, rt: False)
    monkeypatch.setattr(fo, '_tokens_load', lambda tok, fresh=False: {
        'access': 'OLD', 'refresh': 'R', 'expires_at': _t.time() - 10})
    monkeypatch.setattr(fo, '_refresh', lambda r: (_ for _ in ()).throw(
        AssertionError('fremdes Claim ⇒ kein LH-Call')))
    monkeypatch.setattr(fo, '_tokens_save', lambda tok, t: (_ for _ in ()).throw(
        AssertionError('fremdes Claim ⇒ kein Save')))
    with _as_refresher():
        assert fo._refresher_refresh_grant('AT-U') == 'skipped_claim_foreign'


def test_refresher_rotates_and_persists_new_keeps_old(monkeypatch):
    """Happy-Path des Refreshers: Claim gewonnen → Rotation → neuer RT wird
    DB-bestätigt persistiert; rotiert LH den RT nicht mit, bleibt der
    bewährte gespeichert."""
    import time as _t
    saved = {}
    monkeypatch.setattr(fo, '_claim_refresh_sb', lambda tok, rt: True)
    monkeypatch.setattr(fo, '_tokens_load', lambda tok, fresh=False: {
        'access': 'OLD', 'refresh': 'R1', 'expires_at': _t.time() - 10})
    monkeypatch.setattr(fo, '_save_rotated_cas', lambda tok, c, t: None)
    monkeypatch.setattr(fo, '_tokens_mirror_raw', lambda tok: {'refresh': 'R1'})
    monkeypatch.setattr(fo, '_tokens_save', lambda tok, t: saved.update(t) or True)
    monkeypatch.setattr(fo, '_refresh', lambda r: (
        {'access': 'NEW', 'refresh': 'R2', 'scope': 's', 'expires_at': 9e18}, None))
    with _as_refresher():
        assert fo._refresher_refresh_grant('AT-U') == 'rotated'
    assert saved['refresh'] == 'R2'
    monkeypatch.setattr(fo, '_refresh', lambda r: (
        {'access': 'NEW2', 'refresh': None, 'scope': 's', 'expires_at': 9e18}, None))
    with _as_refresher():
        assert fo._refresher_refresh_grant('AT-U') == 'rotated'
    assert saved['refresh'] == 'R1'      # keine Rotation → bewährter bleibt


def test_refresher_fresh_grant_untouched(monkeypatch):
    """Ein AT mit reichlich Restlaufzeit wird NICHT rotiert (Refreshes sind
    selten und planbar — nur der Vorlauf _REFRESH_AHEAD_S triggert)."""
    import time as _t
    monkeypatch.setattr(fo, '_tokens_load', lambda tok, fresh=False: {
        'access': 'ACC', 'refresh': 'R', 'expires_at': _t.time() + 3000})
    monkeypatch.setattr(fo, '_claim_refresh_sb', lambda tok, rt: (_ for _ in ()).throw(
        AssertionError('frischer Grant: kein Claim nötig')))
    monkeypatch.setattr(fo, '_refresh', lambda r: (_ for _ in ()).throw(
        AssertionError('frischer Grant: kein LH-Call')))
    with _as_refresher():
        assert fo._refresher_refresh_grant('AT-U') == 'fresh'


def test_refresher_fatal_flags_on_fresh_state(monkeypatch):
    """Wirklich toter Grant: needs_relogin wird auf dem FRISCH geladenen
    Stand geflaggt (nie ein alter Snapshot zurückgeschrieben), forensisch
    geloggt, kein Push (Owner-Entscheid)."""
    import time as _t
    saves = []
    pushes = []
    monkeypatch.setattr(fo, '_FATAL_GRACE_SEC', 0)
    monkeypatch.setattr(fo, '_claim_refresh_sb', lambda tok, rt: True)
    state = {'access': 'OLD', 'refresh': 'R', 'expires_at': _t.time() - 10}
    monkeypatch.setattr(fo, '_tokens_load', lambda tok, fresh=False: dict(state))
    monkeypatch.setattr(fo, '_tokens_mirror_raw', lambda tok: dict(state))
    monkeypatch.setattr(fo, '_tokens_save', lambda tok, t: saves.append(dict(t)) or True)
    monkeypatch.setattr(fo, '_refresh', lambda r: (
        None, {'http': 400, 'oauth': 'invalid_grant', 'fatal': True}))
    monkeypatch.setattr(fo, '_notify_relogin', lambda tok: pushes.append(tok))
    with _as_refresher():
        assert fo._refresher_refresh_grant('AT-U') == 'dead'
    flagged = saves[-1]
    assert flagged.get('needs_relogin') is True and 'access' not in flagged
    assert pushes == ['AT-U']
    # needs_relogin → connected False, Lesepfad None, Refresher fasst ihn
    # nicht mehr an
    monkeypatch.setattr(fo, '_tokens_load', lambda tok, fresh=False: dict(flagged))
    assert fo.flightops_connected('AT-U') is False
    assert fo._valid_access('AT-U') is None
    with _as_refresher():
        assert fo._refresher_refresh_grant('AT-U') == 'dead_flagged'


def test_refresher_fatal_race_does_not_kill_winner(monkeypatch):
    """Deploy-Übergangs-Race: unser fatal kam nur, weil ein Alt-Code-
    Container den RT parallel verbraucht und schon R2 persistiert hat.
    Der Refresher darf dann NICHT flaggen und nichts überschreiben."""
    import time as _t
    monkeypatch.setattr(fo, '_FATAL_GRACE_SEC', 0)
    monkeypatch.setattr(fo, '_claim_refresh_sb', lambda tok, rt: True)
    monkeypatch.setattr(fo, '_tokens_load', lambda tok, fresh=False: {
        'access': 'OLD', 'refresh': 'R', 'expires_at': _t.time() - 10})
    monkeypatch.setattr(fo, '_tokens_mirror_raw', lambda tok: {
        'access': 'NEW', 'refresh': 'R2', 'expires_at': _t.time() + 999})
    monkeypatch.setattr(fo, '_tokens_save', lambda tok, t: (_ for _ in ()).throw(
        AssertionError('Race-Verlierer darf nichts speichern/flaggen')))
    monkeypatch.setattr(fo, '_refresh', lambda r: (
        None, {'http': 401, 'oauth': 'invalid_token', 'fatal': True}))
    monkeypatch.setattr(fo, '_notify_relogin', lambda tok: (_ for _ in ()).throw(
        AssertionError('Race-Verlierer darf keinen Re-Login ausrufen')))
    with _as_refresher():
        assert fo._refresher_refresh_grant('AT-U') == 'raced'


def test_refresher_transient_keeps_tokens(monkeypatch):
    """Transienter LH-Fehler (Wartung/Rate-Limit/Netz): Tokens bleiben
    unangetastet, kein Flag — der nächste Tick versucht es erneut."""
    import time as _t
    monkeypatch.setattr(fo, '_claim_refresh_sb', lambda tok, rt: True)
    monkeypatch.setattr(fo, '_tokens_load', lambda tok, fresh=False: {
        'access': 'OLD', 'refresh': 'R', 'expires_at': _t.time() - 10})
    monkeypatch.setattr(fo, '_tokens_save', lambda tok, t: (_ for _ in ()).throw(
        AssertionError('transient darf nichts schreiben')))
    monkeypatch.setattr(fo, '_refresh', lambda r: (
        None, {'http': 503, 'oauth': 'service_unavailable', 'fatal': False}))
    monkeypatch.setattr(fo, '_notify_relogin', lambda tok: (_ for _ in ()).throw(
        AssertionError('transient darf keinen Re-Login ausrufen')))
    with _as_refresher():
        assert fo._refresher_refresh_grant('AT-U') == 'transient'


def test_refresher_parked_rotation_blocks_further_rotation(monkeypatch):
    """CHAOS: SB-Timeout mid-Rotation — der Save nach der Rotation blieb
    unbestätigt (RT nur im Prozess geparkt). Der Refresher rotiert diesen
    Grant dann NICHT weiter (eine Kette unpersistierter RTs stirbt mit dem
    Prozess), sondern versucht nur den Nachsave; nach der Heilung läuft
    alles normal."""
    import time as _t
    fo._rotated_pending.clear()
    fo._rotated_pending['AT-U'] = {
        'tokens': {'access': 'NEW', 'refresh': 'R2',
                   'expires_at': _t.time() + 3000},
        'consumed_rt': 'R1', 'ts': _t.time()}
    monkeypatch.setattr(fo, '_refresh', lambda r: (_ for _ in ()).throw(
        AssertionError('geparkter Grant darf NICHT weiterrotieren')))
    monkeypatch.setattr(fo, '_claim_refresh_sb', lambda tok, rt: (_ for _ in ()).throw(
        AssertionError('geparkter Grant claimt nicht')))
    # Nachsave schlägt weiter fehl → save_pending
    import app as backend
    monkeypatch.setattr(backend, '_profile_load',
                        lambda tok, fresh=False: {'profile': {
                            'flightops_tokens': {'access': 'OLD',
                                                 'refresh': 'R1'}}})
    monkeypatch.setattr(fo, '_tokens_save', lambda tok, t: False)
    with _as_refresher():
        assert fo._refresher_refresh_grant('AT-U') == 'save_pending'
    assert 'AT-U' in fo._rotated_pending
    # SB wieder da → Nachsave heilt, Parkplatz leer
    monkeypatch.setattr(fo, '_tokens_save', lambda tok, t: True)
    with _as_refresher():
        assert fo._refresher_refresh_grant('AT-U') == 'save_healed'
    assert 'AT-U' not in fo._rotated_pending


def test_refresher_two_parallel_processes_single_lh_call(monkeypatch):
    """CHAOS: zwei 'Prozesse' (simuliert über das geteilte atomare Claim)
    wollen denselben RT rotieren — genau EINER bekommt den Zuschlag und
    macht den einzigen LH-Call; der andere tut NICHTS."""
    import time as _t
    claim_state = {'taken': False}

    def _claim(tok, rt):
        if claim_state['taken']:
            return False
        claim_state['taken'] = True
        return True
    calls = []
    monkeypatch.setattr(fo, '_claim_refresh_sb', _claim)
    monkeypatch.setattr(fo, '_tokens_load', lambda tok, fresh=False: {
        'access': 'OLD', 'refresh': 'R1', 'expires_at': _t.time() - 10})
    monkeypatch.setattr(fo, '_save_rotated_cas', lambda tok, c, t: None)
    monkeypatch.setattr(fo, '_tokens_mirror_raw', lambda tok: {'refresh': 'R1'})
    monkeypatch.setattr(fo, '_tokens_save', lambda tok, t: True)
    monkeypatch.setattr(fo, '_refresh', lambda r: calls.append(r) or (
        {'access': 'NEW', 'refresh': 'R2', 'scope': 's', 'expires_at': 9e18}, None))
    with _as_refresher():
        r1 = fo._refresher_refresh_grant('AT-U')
        r2 = fo._refresher_refresh_grant('AT-U')
    assert sorted([r1, r2]) == ['rotated', 'skipped_claim_foreign']
    assert calls == ['R1']               # exakt EIN LH-Call


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
    monkeypatch.setattr(fo, '_access_state', lambda tok: ('ok', 'ACC'))
    monkeypatch.setattr(fo, '_resolve_link_params', lambda *a, **k: {
        'accessCode': 'SECRET42', 'departureAirport': 'FRA',
        'arrivalAirport': 'CAI'})
    monkeypatch.setattr(fo, 'crew_list', lambda *a, **k: {'crewMembers': []})
    monkeypatch.setattr(fo, 'parse_crew_list', lambda resp: [
        {'name': 'MUSTERMANN, MAX', 'pk': '1', 'category': 'CPT'}])
    monkeypatch.setattr(fo, '_match_aerox_profiles', lambda crew, **_kw: {})
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
    """LEGACY-Pfad (Tabelle fehlt/SB weg): Put/Get über den Profil-Mirror
    inkl. der 8er-Kappung. Genau diese Kappung war die Owner-Wurzel — sie
    bleibt nur noch als Notnagel für den Migrations-Zeitraum bestehen."""
    store = {}
    import app as backend
    monkeypatch.setattr(backend, 'SB_AVAILABLE', False)   # → Tabelle aus
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


# ── Crew-Cache in eigener Tabelle (Owner-Bug 2026-07-28) ────────────────────
# Bewiesene Wurzel: NICHT die Durabilität (der Profil-Mirror überlebte alle
# Deploys), sondern der 8er-Deckel — ein frisch angesehenes Leg verdrängte ein
# künftiges. Diese Tests halten das Gegenteil fest.

class _CrewTbl:
    """Minimal-Supabase für flightops_crew_cache (PK token,flight,date)."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.deletes = []
        self.fail = False

    # sb.table(name) → self
    def table(self, name):
        assert name == fo._CREW_CACHE_TABLE, name
        self._f, self._op = {}, None
        self._in = None
        return self

    def select(self, *_a, **_k):
        self._op = 'select'
        return self

    def upsert(self, row, on_conflict=None):
        self._op, self._row, self._conflict = 'upsert', row, on_conflict
        return self

    def delete(self):
        self._op = 'delete'
        return self

    def eq(self, col, val):
        self._f[col] = val
        return self

    def lt(self, col, val):
        self._f['<' + col] = val
        return self

    def in_(self, col, vals):
        self._in = (col, list(vals))
        return self

    def limit(self, _n):
        return self

    def execute(self):
        if self.fail:
            raise RuntimeError('PGRST205 table not found')
        if self._op == 'upsert':
            assert self._conflict == 'token,flight,flight_date'
            key = (self._row['token'], self._row['flight'],
                   self._row['flight_date'])
            self.rows = [r for r in self.rows
                         if (r['token'], r['flight'], r['flight_date']) != key]
            self.rows.append(dict(self._row))
            return type('R', (), {'data': [dict(self._row)]})()
        if self._op == 'delete':
            self.deletes.append(dict(self._f))
            keep, gone = [], []
            for r in self.rows:
                if (r['token'] == self._f.get('token')
                        and r['flight_date'] < self._f.get('<flight_date', '')):
                    gone.append(r)
                else:
                    keep.append(r)
            self.rows = keep
            return type('R', (), {'data': gone})()
        out = [r for r in self.rows
               if all(r.get(k) == v for k, v in self._f.items())]
        if self._in:
            col, vals = self._in
            out = [r for r in out if r.get(col) in vals]
        return type('R', (), {'data': [dict(r) for r in out]})()


def _crew_tbl(monkeypatch, rows=None):
    import app as backend
    tbl = _CrewTbl(rows)
    monkeypatch.setattr(backend, 'sb', tbl)
    monkeypatch.setattr(backend, 'SB_AVAILABLE', True)
    monkeypatch.setattr(fo, '_crew_tbl_state', [0.0, True])
    monkeypatch.setattr(fo, '_crew_prune_seen', {})
    # Legacy-Profil leer: die Tabelle ist die einzige Wahrheit im Test.
    monkeypatch.setattr(backend, '_profile_load', lambda tok: {'profile': {}})
    return tbl


def test_crew_cache_table_has_no_eviction_cap(monkeypatch):
    """DIE Regression: 30 Legs cachen, ALLE bleiben abrufbar. Vorher hätte
    Leg 9 das erste (typischerweise ein künftiges) verdrängt."""
    _crew_tbl(monkeypatch)
    for i in range(30):
        fo._crew_cache_put('AT-U', f'LH{400 + i}', '2026-08-%02d' % (i + 1),
                           [{'name': f'N{i}'}])
    for i in range(30):
        hit = fo._crew_cache_get('AT-U', f'LH{400 + i}', '2026-08-%02d' % (i + 1))
        assert hit and hit['crew'][0]['name'] == f'N{i}', i


def test_crew_cache_survives_new_write_of_other_leg(monkeypatch):
    """Owner-Szenario 1:1 — der Live-Abruf des Hinflugs darf den gecachten
    RÜCKFLUG nicht rauswerfen."""
    _crew_tbl(monkeypatch)
    fo._crew_cache_put('AT-U', 'LH455', '2026-07-30', [{'name': 'RUECK'}])
    fo._crew_cache_put('AT-U', 'LH454', '2026-07-28', [{'name': 'HIN'}])
    assert fo._crew_cache_get('AT-U', 'LH455', '2026-07-30')['crew'][0]['name'] == 'RUECK'


def test_crew_cache_date_slack_one_day(monkeypatch):
    """Red-Eye/Z-vs-LT: ±1 Tag trifft, exaktes Datum gewinnt."""
    _crew_tbl(monkeypatch)
    fo._crew_cache_put('AT-U', 'LH455', '2026-07-30', [{'name': 'EXAKT'}])
    fo._crew_cache_put('AT-U', 'LH455', '2026-07-31', [{'name': 'PLUS1'}])
    assert fo._crew_cache_get('AT-U', 'LH455', '2026-07-30')['crew'][0]['name'] == 'EXAKT'
    assert fo._crew_cache_get('AT-U', 'LH455', '2026-07-31')['crew'][0]['name'] == 'PLUS1'
    # 29.07. hat keinen eigenen Eintrag → Toleranz greift auf den 30.
    assert fo._crew_cache_get('AT-U', 'LH455', '2026-07-29')['crew'][0]['name'] == 'EXAKT'
    # 3 Tage daneben ist KEIN Treffer (die Toleranz bleibt eng).
    assert fo._crew_cache_get('AT-U', 'LH455', '2026-08-03') is None


def test_crew_cache_prunes_only_old_flight_dates(monkeypatch):
    tbl = _crew_tbl(monkeypatch)
    old = (fo._dt.date.today()
           - fo._dt.timedelta(days=fo._CREW_CACHE_KEEP_DAYS + 5)).isoformat()
    tbl.rows.append({'token': 'AT-U', 'flight': 'LH1', 'flight_date': old,
                     'crew': [{'name': 'ALT'}], 'cached_at': 1.0})
    fo._crew_cache_put('AT-U', 'LH2', '2026-08-01', [{'name': 'NEU'}])
    assert [r['flight'] for r in tbl.rows] == ['LH2']
    # Zweiter Put in derselben Stunde räumt NICHT nochmal (ein DELETE reicht).
    fo._crew_cache_put('AT-U', 'LH3', '2026-08-02', [{'name': 'NEU2'}])
    assert len(tbl.deletes) == 1


def test_crew_cache_falls_back_to_profile_when_table_missing(monkeypatch):
    """Migration noch nicht angewandt → alter Profil-Cache, kein Hard-Fail."""
    tbl = _crew_tbl(monkeypatch)
    tbl.fail = True
    store = {}
    import app as backend
    monkeypatch.setattr(backend, '_profile_load',
                        lambda tok: {'profile': dict(store)})
    monkeypatch.setattr(backend, '_profile_save',
                        lambda tok, prof: store.update(prof) or True)
    fo._crew_cache_put('AT-U', 'LH582', '2026-07-26', [{'name': 'MAX'}])
    assert store['flightops_crew_cache'][0]['crew'][0]['name'] == 'MAX'
    assert fo._crew_cache_get('AT-U', 'LH582', '2026-07-26')['crew'][0]['name'] == 'MAX'


def test_crew_cache_lazy_migrates_legacy_profile(monkeypatch):
    """Alt-Bestand wandert beim ersten Read in die Tabelle, der Profil-Key
    wird geleert (der Blob liegt im Hot-Path von _profile_load)."""
    tbl = _crew_tbl(monkeypatch)
    store = {'flightops_crew_cache': [
        {'flight': 'LH582', 'date': '2026-07-26', 'crew': [{'name': 'A'}],
         'cached_at': 1.0},
        {'flight': 'LH583', 'date': '2026-07-26', 'crew': [{'name': 'B'}],
         'cached_at': 2.0},
    ]}
    import app as backend
    monkeypatch.setattr(backend, '_profile_load',
                        lambda tok: {'profile': dict(store)})
    monkeypatch.setattr(backend, '_profile_save',
                        lambda tok, prof: store.update(prof) or True)
    hit = fo._crew_cache_get('AT-U', 'LH582', '2026-07-26')
    assert hit['crew'][0]['name'] == 'A'
    assert sorted(r['flight'] for r in tbl.rows) == ['LH582', 'LH583']
    assert store['flightops_crew_cache'] == []
    # Danach kommt alles aus der Tabelle — auch der zweite Alt-Eintrag.
    assert fo._crew_cache_get('AT-U', 'LH583', '2026-07-26')['crew'][0]['name'] == 'B'


def test_crewlist_pending_refresh_is_503_not_relogin(monkeypatch):
    """Abgelaufener AT + laufender Refresher ist KEIN „neu verbinden"-Fall —
    sonst schickt die App den User grundlos in den LH-Login."""
    monkeypatch.setattr(fo, '_access_state', lambda tok: ('pending', None))
    monkeypatch.setattr(fo, '_crew_cache_get', lambda tok, f, d: None)
    import app as backend
    r = backend.app.test_client().post('/api/lh/flightops/crewlist/testtok-fo',
                                       json={'flight': 'LH582', 'date': '2026-07-26'})
    assert r.status_code == 503
    assert r.get_json()['error'] == 'token_refresh_pending'


def test_crewlist_empty_live_list_keeps_last_good(monkeypatch):
    """LH räumt die Liste nach dem Flug weg. Eine leere Live-Antwort darf die
    letzte gute nicht verdrängen (sonst ist das Nachschlagen im Logbuch tot)."""
    monkeypatch.setattr(fo, '_access_state', lambda tok: ('ok', 'ACC'))
    monkeypatch.setattr(fo, '_resolve_link_params',
                        lambda *a, **k: {'accessCode': 'S', 'departureAirport': 'FRA',
                                         'arrivalAirport': 'CAI'})
    monkeypatch.setattr(fo, 'crew_list', lambda *a, **k: {'crewMembers': []})
    monkeypatch.setattr(fo, 'parse_crew_list', lambda resp: [])
    monkeypatch.setattr(fo, '_crew_cache_get', lambda tok, f, d: {
        'flight': 'LH582', 'date': '2026-07-26',
        'crew': [{'name': 'MAX'}], 'cached_at': 9.0})
    puts = []
    monkeypatch.setattr(fo, '_crew_cache_put',
                        lambda *a: puts.append(a))
    import app as backend
    r = backend.app.test_client().post('/api/lh/flightops/crewlist/testtok-fo',
                                       json={'flight': 'LH582', 'date': '2026-07-26'})
    d = r.get_json()
    assert r.status_code == 200 and d['cached'] is True
    assert d['crew'][0]['name'] == 'MAX'
    assert puts == []            # leere Liste wird NIE gecached


def test_crewlist_uses_interactive_key_budget(monkeypatch):
    """Der Crew-Button ist nutzerausgelöst → 950/h statt 700/h. Unter dem
    Hintergrund-Deckel fielen Taps in vollen Stunden als 502/404 aus
    (Prod-Log 2026-07-28 06:00/06:52/06:56)."""
    monkeypatch.setattr(fo, '_access_state', lambda tok: ('ok', 'ACC'))
    seen = {}
    monkeypatch.setattr(fo, '_resolve_link_params',
                        lambda *a, **k: seen.update(links=k.get('interactive'))
                        or {'accessCode': 'S'})
    monkeypatch.setattr(fo, 'crew_list',
                        lambda *a, **k: seen.update(crew=k.get('interactive'))
                        or {'crewMembers': []})
    monkeypatch.setattr(fo, 'parse_crew_list', lambda resp: [{'name': 'MAX'}])
    monkeypatch.setattr(fo, '_match_aerox_profiles', lambda crew, **_kw: {})
    monkeypatch.setattr(fo, '_crew_cache_put', lambda *a: None)
    import app as backend
    r = backend.app.test_client().post('/api/lh/flightops/crewlist/testtok-fo',
                                       json={'flight': 'LH582', 'date': '2026-07-26'})
    assert r.status_code == 200
    assert seen == {'links': True, 'crew': True}


def test_crewlist_background_callers_stay_on_background_budget():
    """Gegenprobe: das Briefing (Hintergrund) darf den Interaktiv-Headroom
    NICHT anfassen — crew_list/_resolve_link_params defaulten auf False."""
    import inspect
    assert inspect.signature(fo.crew_list).parameters['interactive'].default is False
    assert inspect.signature(
        fo._resolve_link_params).parameters['interactive'].default is False


def test_crew_cache_table_in_delete_cascade():
    """PII (Klarnamen fremder Crew) muss bei der Kontolöschung mitgehen."""
    import pathlib
    import app as backend
    txt = pathlib.Path(backend.__file__).read_text()
    assert "('flightops_crew_cache',    'token')" in txt


def test_status_reports_needs_relogin(monkeypatch):
    monkeypatch.setattr(fo, '_tokens_load', lambda tok, fresh=False: {
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
                        lambda tok, fd, td, interactive=False:
                        calls.append((fd, td)) or DUTY_LINKS)
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
    monkeypatch.setattr(fo, '_access_state', lambda tok: ('ok', 'ACC'))
    monkeypatch.setattr(fo, '_links_load',
                        lambda tok: fo.extract_duty_links(DUTY_LINKS))
    got = {}
    monkeypatch.setattr(fo, 'crew_list',
                        lambda tok, f, d, dep, arr, ac, **k:
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
    monkeypatch.setattr(fo, '_access_state', lambda tok: ('ok', 'ACC'))
    monkeypatch.setattr(fo, '_links_load', lambda tok: [])
    monkeypatch.setattr(fo, 'duty_events',
                        lambda tok, fd, td, interactive=False: None)
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
    monkeypatch.setattr(fo, '_access_state', lambda tok: ('ok', 'ACC'))
    monkeypatch.setattr(fo, '_links_load',
                        lambda tok: fo.extract_duty_links(DUTY_LINKS))
    got = {}
    monkeypatch.setattr(fo, 'service_get',
                        lambda tok, svc, params, interactive=False:
                        got.update(svc=svc, p=params, ia=interactive) or
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
    # Der Check-in hängt an einem Nutzer-Tap → interaktives Budget-Gate.
    assert got['ia'] is True


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
    monkeypatch.setattr(fo, '_access_state',
                        lambda tok: (('disconnected', None) if tok == 'AT-DEAD'
                                     else ('ok', 'ACC')))
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


def test_import_endpoint_reports_pending_as_transient(monkeypatch):
    """AT abgelaufen, Grant lebt: der Import antwortet 503
    token_refresh_pending (transient, iOS zeigt KEINE Relogin-Karte) —
    nie 401 not_connected. Der Refresher rotiert; refresht wird hier NIE."""
    import time as _t
    _pass_auth_gate(monkeypatch)
    monkeypatch.setattr(fo, '_KEY', 'k'); monkeypatch.setattr(fo, '_SECRET', 's')
    fo._refresh_wanted.clear()
    monkeypatch.setattr(fo, '_tokens_load', lambda tok, fresh=False: {
        'access': 'OLD', 'refresh': 'R', 'expires_at': _t.time() - 10})
    monkeypatch.setattr(fo, '_refresh', lambda r: (_ for _ in ()).throw(
        AssertionError('Import-Endpoint darf nie refreshen')))
    import app as backend
    r = backend.app.test_client().post('/api/lh/flightops/import/AT-U',
                                       headers={'Authorization': 'Bearer AT-U'},
                                       json={})
    assert r.status_code == 503
    assert r.get_json()['error'] == 'token_refresh_pending'
    assert 'AT-U' in fo._refresh_wanted        # Vormerkung für den Refresher
    fo._refresh_wanted.clear()
    # Status bleibt währenddessen connected (keine Relogin-Karte)
    d = backend.app.test_client().get('/api/lh/flightops/status/AT-U').get_json()
    assert d['connected'] is True and d['needs_relogin'] is False


def test_crewlist_endpoint_attaches_aerox_profiles(monkeypatch):
    """Owner 2026-07-23: Crew-Mitglieder mit AeroX-Account bekommen ihr
    PUBLIC-Profil (token/avatar/…) an den Listen-Eintrag — Match primär via
    LH-Personalnummer."""
    _pass_auth_gate(monkeypatch)
    monkeypatch.setattr(fo, '_KEY', 'k'); monkeypatch.setattr(fo, '_SECRET', 's')
    monkeypatch.setattr(fo, '_access_state', lambda tok: ('ok', 'ACC'))
    monkeypatch.setattr(fo, '_links_load',
                        lambda tok: fo.extract_duty_links(DUTY_LINKS))
    monkeypatch.setattr(fo, 'crew_list',
                        lambda tok, f, d, dep, arr, ac, **k: REAL_CREWLIST)
    monkeypatch.setattr(fo, '_match_aerox_profiles', lambda members, **_kw: {
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
    for n in range(1, 10):
        assert fo.is_office_day_code('B%d' % n)
    # AUSNAHME aus dem CRS-Handbuch: nacktes B = Betriebsunfall (Abwesenheit,
    # reduziert den Freitage-Anspruch) — KEIN Buerodienst.
    assert not fo.is_office_day_code('B')
    assert not fo.is_office_day_code('B0')
    # B1 IST Buerodienst (Owner-Entscheid 27.07.2026): 74 von 84 echten
    # B1-Tagen tragen LHs eigenes Label „Office Day (B1)". Das CRS fuehrt B1
    # zusaetzlich als Teilzeit-Vertragsart — im Tages-Kontext gilt Dienst.
    assert fo.is_office_day_code('B1')
    # Token-Grenzen: kein Praefix-Match.
    for bad in ('B45', 'B455', 'B4A', 'AB4', 'B 4', ''):
        assert not fo.is_office_day_code(bad), bad


def test_office_codes_count_as_ground_duty_evidence():
    import app as backend
    for n in range(1, 10):
        code = 'B%d' % n
        assert backend._summary_has_ground_duty(code), code
        assert backend._summary_has_ground_duty('OFFICE DAY (%s)' % code)
    # Praefix darf NICHT zuenden.
    assert not backend._summary_has_ground_duty('B455')
    assert not backend._summary_has_ground_duty('B45')
    # CRS-Ausnahme: nacktes B = Betriebsunfall/Abwesenheit, kein Buerodienst.
    assert not backend._summary_has_ground_duty('B')
    assert not backend._summary_has_ground_duty('ABSENCE (B)')
    assert not backend._summary_has_ground_duty('B0')
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
    assert duty_from_roster_day(None, 'Off Day (OF) · B1') != 'free'
    assert duty_from_roster_day(None, 'Off Day (B4)') != 'free'
    # nacktes B bleibt Abwesenheit, kein Dienst-Beweis
    assert duty_from_roster_day(None, 'Off Day (OF) · B') == 'free'
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


# ── HOTEL-PICKUP aus COMMON_CREW_ROTATION ────────────────────────────────────
# Owner-Fund 2026-07-26: die seit dem direkten FlightOps-Login fehlende
# Pickup-Zeit steckt in COMMON_CREW_ROTATION (legs[].pickupTime/pickupTimeLT),
# NICHT in COMMON_CHECK_IN_TIMES. Die beiden Payloads unten sind die ZWEI in
# PROD gemessenen Faelle (RN 169929 DTW / RN 171012 KIX) — inklusive des
# Mitternachts-Wraps, an dem eine naive Fassung zerbricht.

def _rot_fixture():
    import json as _json
    import os as _os
    p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                      'fixtures', 'flightops_COMMON_CREW_ROTATION.json')
    with open(p) as f:
        return _json.load(f)


def test_parse_rotation_pickups_reads_the_real_repo_fixture():
    """Echte COMMON_CREW_ROTATION-Response: nur Legs MIT pickupTime landen im
    Dict, hotelName wird mitgenommen, das luegende `hotel`-Flag ignoriert."""
    p = fo.parse_rotation_pickups(_rot_fixture())
    # LH492 FRA-YVR (pickupTime gesetzt) und LH493 YVR-FRA (gesetzt) — je mit
    # und ohne Flugnummer-Schluessel.
    assert p[('LH492', 'FRA', 'YVR', '20260817')]['pickup_utc'] == '2026-08-17T09:10:00Z'
    assert p[('LH493', 'YVR', 'FRA', '20260819')]['pickup_utc'] == '2026-08-19T21:05:00Z'
    assert p[('', 'FRA', 'YVR', '20260817')]['station'] == 'FRA'
    # hotelName mitgenommen, obwohl `hotel` im Payload False ist (Flag luegt) —
    # und zwar der Name der ABFLUG-Station des Pickups: LH492 startet in FRA,
    # dort hat die Crew genaechtigt, und dieser Name steht am HINFLUG-Leg
    # LH117 MUC-FRA ('H9941671'). 'H9945745' ist das YVR-Hotel und gehoert an
    # den YVR-Rueckflug, nicht hierhin.
    assert p[('LH492', 'FRA', 'YVR', '20260817')]['hotel'] == 'H9941671'
    assert p[('LH493', 'YVR', 'FRA', '20260819')]['hotel'] == 'H9945745'
    assert _rot_fixture()['rotations'][0]['shifts'][1]['legs'][0]['hotel'] is False
    # LH117 MUC-FRA und LH104 FRA-MUC haben pickupTime null → KEIN Eintrag.
    assert not [k for k in p if k[0] == 'LH117']
    assert not [k for k in p if k[0] == 'LH104']


def test_parse_rotation_pickups_prod_shape_with_pickuptimelt():
    """PROD-Shape mit fertiger Ortszeit (pickupTimeLT) — beide Owner-Messungen."""
    resp = {"rotations": [{"rotationNumber": "169929", "shifts": [
        {"shiftNumber": 1, "legs": [
            {"flightDesignator": "LH443", "departureAirport": "DTW",
             "arrivalAirport": "FRA", "depatureDate": "2026-07-26T20:00:00Z",
             "pickupTime": "2026-07-26T18:00:00Z", "pickupTimeLT": "14:00",
             "hotelName": "The Henry Hotel Dearborn", "hotel": False}]}]}]}
    p = fo.parse_rotation_pickups(resp)
    v = p[('LH443', 'DTW', 'FRA', '20260726')]
    assert v['pickup_lt'] == '14:00'
    assert v['pickup_utc'] == '2026-07-26T18:00:00Z'
    assert v['hotel'] == 'The Henry Hotel Dearborn'


def test_rot_hhmm_lt_accepts_both_shapes_and_rejects_nonsense():
    assert fo._rot_hhmm_lt('2026-07-29T06:50:00') == '06:50'
    assert fo._rot_hhmm_lt('06:50') == '06:50'
    assert fo._rot_hhmm_lt('0650') == '06:50'
    assert fo._rot_hhmm_lt('6:50') == '06:50'
    for bad in ('', None, '2599', '25:99', 'abc', '24:00'):
        assert fo._rot_hhmm_lt(bad) is None


# RN 169929 · LH443 DTW→FRA · Abflug 26.07. 20:00Z · Pickup 18:00Z = 14:00 LT
DUTY_DTW_PICKUP = {
    "rosterDays": [
        {"day": "2026-07-25Z", "events": [
            {"eventType": "FLIGHT", "eventCategory": "flight",
             "eventDetails": "LH442", "wholeDay": False,
             "startTime": "2026-07-25T09:50:00Z", "startLocation": "FRA",
             "endTime": "2026-07-25T19:05:00Z", "endLocation": "DTW",
             "eventAttributes": {"rotationId": 169929, "dayOfShift": 1}},
            {"eventType": "HOTEL", "eventCategory": "hotel",
             "eventDetails": "Hotel", "wholeDay": False, "startTime": None,
             "startLocation": "DTW", "endTime": None, "endLocation": None}]},
        {"day": "2026-07-26Z", "events": [
            {"eventType": "BRIEFING", "eventCategory": "briefing",
             "eventDetails": "Briefing", "wholeDay": False,
             "startTime": "2026-07-26T19:00:00Z", "startLocation": "DTW",
             "endTime": None, "endLocation": None},
            {"eventType": "FLIGHT", "eventCategory": "flight",
             "eventDetails": "LH443", "wholeDay": False,
             "startTime": "2026-07-26T20:00:00Z", "startLocation": "DTW",
             "endTime": "2026-07-27T04:10:00Z", "endLocation": "FRA",
             "eventAttributes": {"rotationId": 169929, "dayOfShift": 2}}]},
    ]
}

PICKUP_DTW = {('LH443', 'DTW', 'FRA', '20260726'): {
    'pickup_utc': '2026-07-26T18:00:00Z', 'pickup_lt': '14:00',
    'hotel': 'The Henry Hotel Dearborn', 'station': 'DTW'}}

# RN 171012 · LH743 KIX→MUC · Abflug 29.07. 00:30Z · Pickup 28.07. 21:50Z =
# 06:50 LT (KIX) — der MITTERNACHTS-WRAP: Pickup und Abflug liegen in
# VERSCHIEDENEN Berlin-Kalendertagen.
PICKUP_KIX = {('LH743', 'KIX', 'MUC', '20260729'): {
    'pickup_utc': '2026-07-28T21:50:00Z', 'pickup_lt': '06:50',
    'hotel': 'Hotel New Otani Osaka', 'station': 'KIX'}}


def test_pickup_vevent_summary_is_read_by_the_parser():
    """Ziel-Form „HH:MM LT Pickup XXX" — genau das, was parse_pickup_hhmm liest."""
    from blueprints.crew_live_state import parse_pickup_hhmm
    ics = fo.duty_events_to_ics(DUTY_DTW_PICKUP, pickups=PICKUP_DTW)
    assert 'SUMMARY:14:00 LT Pickup DTW' in ics
    assert parse_pickup_hhmm('14:00 LT Pickup DTW') == (14, 0)
    # Zeitbehaftetes Event am ECHTEN Pickup-Zeitpunkt (gleicher Berlin-Tag wie
    # der Abflug → keine Verschiebung noetig).
    assert 'DTSTART:20260726T180000Z' in ics
    # Der Pickup steht VOR dem Flug-VEVENT (myTime-Reihenfolge im Tages-Summary).
    assert ics.index('Pickup DTW') < ics.index('LH 443: DTW-FRA')


def test_pickup_lands_on_the_days_summary_and_is_read_per_day():
    """Ende-zu-Ende durch die echte Feed-Pipeline: der Pickup muss im
    ical_summary DES RUECKFLUGTAGS stehen, denn _rc_pickup_hhmm liest pro TAG."""
    import app as backend
    ics = fo.duty_events_to_ics(DUTY_DTW_PICKUP, pickups=PICKUP_DTW)
    b = backend._ics_events_to_briefings(backend._parse_ics_to_events(ics))[0]
    day = b['2026-07-26']
    assert 'Pickup DTW' in (day.get('ical_summary') or '')
    assert backend._rc_pickup_hhmm(day) == '14:00'


def test_pickup_midnight_wrap_lands_on_the_return_leg_day():
    """RN 171012: Pickup 28.07. 21:50Z (Berlin 23:50 am 28.), Abflug 29.07.
    00:30Z (Berlin 02:30 am 29.). Ein VEVENT am echten Pickup-Zeitpunkt fiele
    auf den 28. — also auf den Layover-Tag statt auf den Rueckflug-Tag, und
    der Tages-Leser fand ihn nie. DTSTART wird deshalb auf den Abflug gezogen;
    die WAHRHEIT bleibt im Summary und wird unten exakt rekonstruiert."""
    import app as backend
    from blueprints.crew_live_state import parse_pickup_hhmm, pickup_utc_for_leg
    assert fo._berlin_day('2026-07-28T21:50:00Z') == '2026-07-28'
    assert fo._berlin_day('2026-07-29T00:30:00Z') == '2026-07-29'
    ics = fo.duty_events_to_ics(DUTY_KIX_MULTINIGHT, pickups=PICKUP_KIX)
    assert 'SUMMARY:06:50 LT Pickup KIX' in ics
    assert 'DTSTART:20260728T215000Z' not in ics      # NICHT am 28. gebucketet
    b = backend._ics_events_to_briefings(backend._parse_ics_to_events(ics))[0]
    assert 'Pickup KIX' in (b['2026-07-29'].get('ical_summary') or '')
    assert backend._rc_pickup_hhmm(b['2026-07-29']) == '06:50'
    # ...und NICHT am Layover-Tag davor (dort haette der echte Zeitpunkt
    # gebucketet und der Tages-Leser haette ihn nie gefunden).
    assert 'Pickup' not in (b['2026-07-28'].get('ical_summary') or '')
    # Der echte UTC-Zeitpunkt ist aus dem Summary voll rekonstruierbar
    # (Tagesabzug beim Wrap macht pickup_utc_for_leg selbst).
    got = pickup_utc_for_leg(parse_pickup_hhmm('06:50 LT Pickup KIX'),
                             '2026-07-29T00:30:00Z', 'Asia/Tokyo')
    assert got.strftime('%Y-%m-%dT%H:%M:%SZ') == '2026-07-28T21:50:00Z'


def test_no_pickup_value_means_no_pickup_event():
    """Grundregel: nie raten. Ohne Wert kein Event — auch nicht aus dem
    Briefing abgeleitet (DUTY_DTW_PICKUP hat ein Briefing um 19:00Z)."""
    ics = fo.duty_events_to_ics(DUTY_DTW_PICKUP)
    assert 'Pickup' not in ics
    assert 'Briefing DTW' in ics
    for empty in ({}, None, {('LH999', 'XXX', 'YYY', '20260726'): {}}):
        assert 'Pickup' not in fo.duty_events_to_ics(DUTY_DTW_PICKUP, pickups=empty)


def test_pickup_disappears_again_when_lh_deletes_the_value():
    """Florian: LH traegt spaet nach und LOESCHT spaeter wieder. Der Marker
    darf nicht kleben — derselbe Roster ohne Wert ergibt kein Event."""
    with_pu = fo.duty_events_to_ics(DUTY_DTW_PICKUP, pickups=PICKUP_DTW)
    assert 'Pickup DTW' in with_pu
    after_delete = fo.duty_events_to_ics(DUTY_DTW_PICKUP, pickups={})
    assert 'Pickup' not in after_delete


def test_pickup_without_pickuptimelt_derives_station_local_time():
    """Aeltere Service-Version ohne pickupTimeLT → Ortszeit aus dem UTC-Wert
    an der ABFLUG-Station (DTW = America/Detroit, 18:00Z = 14:00 LT)."""
    pu = {('LH443', 'DTW', 'FRA', '20260726'): {
        'pickup_utc': '2026-07-26T18:00:00Z', 'pickup_lt': None,
        'hotel': None, 'station': 'DTW'}}
    ics = fo.duty_events_to_ics(DUTY_DTW_PICKUP, pickups=pu)
    assert 'SUMMARY:14:00 LT Pickup DTW' in ics


def test_pickup_only_for_the_layover_return_never_the_homebase_departure():
    """Am Homebase-Abflug liefert LH nie eine pickupTime; selbst wenn ein Wert
    fuer das Hinflug-Leg dastuende, darf er nur an SEINEM Leg haengen."""
    pu = dict(PICKUP_DTW)
    pu[('LH442', 'FRA', 'DTW', '20260725')] = {
        'pickup_utc': '2026-07-25T08:00:00Z', 'pickup_lt': '10:00',
        'hotel': None, 'station': 'FRA'}
    ics = fo.duty_events_to_ics(DUTY_DTW_PICKUP, pickups=pu)
    assert 'SUMMARY:10:00 LT Pickup FRA' in ics
    assert 'SUMMARY:14:00 LT Pickup DTW' in ics
    # und jeder Pickup steht direkt vor SEINEM Leg
    assert ics.index('Pickup FRA') < ics.index('LH 442: FRA-DTW')
    assert ics.index('Pickup DTW') < ics.index('LH 443: DTW-FRA')


def test_pickup_leg_match_tolerates_a_day_shift_only_with_flightnumber():
    """Verschiebt LH das Leg um einen Tag, matcht der Flugnummer-Schluessel
    noch (+-1 Tag). OHNE Flugnummer darf das NICHT greifen, sonst matcht ein
    taeglicher Umlauf auf dieselbe Route den falschen Tag."""
    ok = {('LH443', 'DTW', 'FRA', '20260727'): {
        'pickup_utc': '2026-07-26T18:00:00Z', 'pickup_lt': '14:00',
        'hotel': None, 'station': 'DTW'}}
    assert fo._pickup_for_leg(ok, 'LH443', 'DTW', 'FRA', '20260726T200000Z')
    bad = {('', 'DTW', 'FRA', '20260727'): {
        'pickup_utc': '2026-07-26T18:00:00Z', 'pickup_lt': '14:00',
        'hotel': None, 'station': 'DTW'}}
    assert fo._pickup_for_leg(bad, '', 'DTW', 'FRA', '20260726T200000Z') is None
    # 2 Tage Versatz ist auch mit Flugnummer zu viel
    far = {('LH443', 'DTW', 'FRA', '20260728'): {'pickup_utc': 'x'}}
    assert fo._pickup_for_leg(far, 'LH443', 'DTW', 'FRA', '20260726T200000Z') is None


# ── FALLBACK-QUELLE: Pickup aus dem myTime-/Kalender-Link ────────────────────
# Owner 2026-07-27: „die Pickup-Zeit wird nicht aus dem iCal-Kalender-Link
# geholt, wenn sie aus der primaeren Quelle fehlt". Fuer FlightOps-User ist der
# Kalender-Link server- (app._maybe_refresh_calendar_feed) UND geraeteseitig
# (RosterFeedDeviceSync) pausiert, und der Import baut jeden Tag frisch auf —
# ohne pickupTime von LH gab es GAR KEINEN Pickup. Diese Ebene holt ihn nach.

def _mytime_ics(*events):
    """Minimales myTime-foermiges ICS. `events` = (summary, dtstart_stamp)."""
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//myTime//DE']
    for i, (summ, stamp) in enumerate(events):
        lines += ['BEGIN:VEVENT', f'UID:mt-{i}@mytime', f'DTSTART:{stamp}',
                  f'DTEND:{stamp}', f'SUMMARY:{summ}', 'END:VEVENT']
    lines.append('END:VCALENDAR')
    return '\r\n'.join(lines)


def test_ical_pickup_candidates_reads_both_mytime_schreibweisen():
    """LH schreibt Pickup-VEVENTs in beiden Formen — beide muessen ankommen,
    und der Summary bleibt WOERTLICH (er traegt die Ortszeit)."""
    got = fo.ical_pickup_candidates(_mytime_ics(
        ('14:00 LT Pickup DTW', '20260726T180000Z'),
        ('Pickup 1430', '20260728T120000Z')))
    assert [c['summary'] for c in got] == ['14:00 LT Pickup DTW', 'Pickup 1430']
    assert got[0]['utc'] == '2026-07-26T18:00:00Z'


def test_ical_pickup_candidates_ignores_everything_that_is_no_pickup():
    """Fluege, Layover, Briefings und unplausible Zeiten sind keine Kandidaten."""
    assert fo.ical_pickup_candidates(_mytime_ics(
        ('LH 443: DTW-FRA', '20260726T200000Z'),
        ('19:00 LT Briefing DTW', '20260726T190000Z'),
        ('Pickup 2599', '20260726T180000Z'))) == []
    assert fo.ical_pickup_candidates('') == []
    assert fo.ical_pickup_candidates('kein ICS') == []


def test_ical_pickup_fills_the_gap_the_primary_source_left():
    """KERNFALL: LH liefert keinen pickupTime (pickups=None), der myTime-Link
    hat den Marker. Ende-zu-Ende durch die echte Feed-Pipeline."""
    import app as backend
    ics = fo.duty_events_to_ics(DUTY_DTW_PICKUP)
    assert 'Pickup' not in ics                       # Primaerquelle: nichts
    merged = fo.merge_ical_pickups(ics, fo.ical_pickup_candidates(
        _mytime_ics(('14:00 LT Pickup DTW', '20260726T180000Z'))))
    assert 'SUMMARY:14:00 LT Pickup DTW' in merged
    # Reihenfolge wie im myTime-Feed: der Pickup ist der frueheste Termin.
    assert merged.index('Pickup DTW') < merged.index('LH 443: DTW-FRA')
    b = backend._ics_events_to_briefings(backend._parse_ics_to_events(merged))[0]
    assert backend._rc_pickup_hhmm(b['2026-07-26']) == '14:00'


def test_ical_pickup_never_overrides_the_primary_source():
    """PRIMARY WINS: steht fuer den Tag schon ein Pickup aus
    COMMON_CREW_ROTATION, bleibt das ICS unveraendert."""
    ics = fo.duty_events_to_ics(DUTY_DTW_PICKUP, pickups=PICKUP_DTW)
    merged = fo.merge_ical_pickups(ics, fo.ical_pickup_candidates(
        _mytime_ics(('13:00 LT Pickup DTW', '20260726T170000Z'))))
    assert merged == ics
    assert '13:00 LT Pickup' not in merged


def test_ical_pickup_needs_an_anchor_leg_within_six_hours():
    """GEISTER-RIEGEL: ein Pickup ohne Abflug 0…6 h danach (stornierter oder
    laengst vergangener Umlauf im noch nicht nachgezogenen Kalender) darf NIE
    in den Roster wandern — gleiches Fenster wie im Primaerpfad."""
    ics = fo.duty_events_to_ics(DUTY_DTW_PICKUP)
    for stamp in ('20260726T100000Z',      # 10 h vor dem Abflug
                  '20260726T210000Z',      # NACH dem Abflug
                  '20260701T180000Z'):     # ganz anderer Umlauf
        merged = fo.merge_ical_pickups(ics, fo.ical_pickup_candidates(
            _mytime_ics(('14:00 LT Pickup DTW', stamp))))
        assert 'Pickup' not in merged, stamp


def test_ical_pickup_midnight_wrap_lands_on_the_return_leg_day():
    """Wie im Primaerpfad (RN 171012): Pickup 28.07. 21:50Z, Abflug 29.07.
    00:30Z — verschiedene Berlin-Tage. Der Marker gehoert auf den RUECKFLUG-Tag,
    denn _rc_pickup_hhmm liest pro TAG."""
    import app as backend
    ics = fo.duty_events_to_ics(DUTY_KIX_MULTINIGHT)
    assert 'Pickup' not in ics
    merged = fo.merge_ical_pickups(ics, fo.ical_pickup_candidates(
        _mytime_ics(('06:50 LT Pickup KIX', '20260728T215000Z'))))
    b = backend._ics_events_to_briefings(backend._parse_ics_to_events(merged))[0]
    assert backend._rc_pickup_hhmm(b['2026-07-29']) == '06:50'
    assert not backend._rc_pickup_hhmm(b.get('2026-07-28') or {})


def test_ical_pickup_one_marker_per_day_latest_plausible_wins():
    """Mehrere Kandidaten fuer denselben Tag (LH traegt nach/korrigiert):
    genau EIN Marker, und zwar der SPAETESTE plausible — dieselbe Regel wie
    pickup_utc_for_leg."""
    ics = fo.duty_events_to_ics(DUTY_DTW_PICKUP)
    merged = fo.merge_ical_pickups(ics, fo.ical_pickup_candidates(_mytime_ics(
        ('13:00 LT Pickup DTW', '20260726T170000Z'),
        ('14:00 LT Pickup DTW', '20260726T180000Z'))))
    assert merged.count('BEGIN:VEVENT') == ics.count('BEGIN:VEVENT') + 1
    assert 'SUMMARY:14:00 LT Pickup DTW' in merged
    assert '13:00 LT Pickup' not in merged


def test_pickup_ical_candidates_are_cached_and_respect_the_killswitch(monkeypatch):
    """Der myTime-Share darf NICHT im Import-Takt gehaemmert werden: ein Abruf
    je TTL, und der Server-Kill-Switch (AEROX_SERVER_ICAL_REFRESH=0) schaltet
    ihn komplett ab."""
    import app as backend
    calls = []
    text = _mytime_ics(('14:00 LT Pickup DTW', '20260726T180000Z'))
    monkeypatch.setattr(backend, '_fetch_calendar_feed_text',
                        lambda u: (calls.append(u), (text, None))[1])
    fo._pickup_ical_cache.clear()
    monkeypatch.setattr(backend, '_server_ical_refresh_enabled', lambda: False)
    assert fo.pickup_ical_candidates_for('tokA', 'https://x/f.ics') == []
    assert calls == []
    monkeypatch.setattr(backend, '_server_ical_refresh_enabled', lambda: True)
    first = fo.pickup_ical_candidates_for('tokA', 'https://x/f.ics')
    assert len(first) == 1 and len(calls) == 1
    assert fo.pickup_ical_candidates_for('tokA', 'https://x/f.ics') == first
    assert len(calls) == 1                      # zweiter Aufruf aus dem Cache
    fo._pickup_ical_cache.clear()


def test_pickup_ical_fetch_failure_keeps_the_last_good_stand(monkeypatch):
    """Ein toter Link darf den Pickup nicht wegnehmen — Grace auf den Cache."""
    import app as backend
    text = _mytime_ics(('14:00 LT Pickup DTW', '20260726T180000Z'))
    monkeypatch.setattr(backend, '_server_ical_refresh_enabled', lambda: True)
    fo._pickup_ical_cache.clear()
    monkeypatch.setattr(backend, '_fetch_calendar_feed_text', lambda u: (text, None))
    good = fo.pickup_ical_candidates_for('tokB', 'https://x/f.ics')
    assert len(good) == 1
    # TTL kuenstlich abgelaufen + Fetch kaputt → letzter guter Stand bleibt.
    fo._pickup_ical_cache['tokB'] = (0.0, good)
    monkeypatch.setattr(backend, '_fetch_calendar_feed_text',
                        lambda u: (None, 'fetch_failed'))
    assert fo.pickup_ical_candidates_for('tokB', 'https://x/f.ics') == good
    fo._pickup_ical_cache.clear()


def test_apply_ical_pickup_fallback_is_inert_without_a_stored_link(monkeypatch):
    """Kein Kalender-Link im Profil ⇒ das FlightOps-ICS bleibt Byte-identisch
    (der Normalfall fuer User, die NUR ueber FlightOps reinkamen)."""
    ics = fo.duty_events_to_ics(DUTY_DTW_PICKUP, pickups=PICKUP_DTW)
    monkeypatch.setattr(fo, 'pickup_ical_url', lambda tok, body=None: '')
    assert fo.apply_ical_pickup_fallback('tok', ics) == ics


def test_merge_ical_pickups_is_a_noop_without_usable_input():
    ics = fo.duty_events_to_ics(DUTY_DTW_PICKUP)
    for cands in ([], None, [{}], [{'utc': 'kaputt', 'summary': 'Pickup 1000'}]):
        assert fo.merge_ical_pickups(ics, cands) == ics
    assert fo.merge_ical_pickups(None, [{'utc': 'x', 'summary': 'y'}]) is None


# ── LAST-GOOD: ein Import darf einen bekannten Pickup NIE loeschen ───────────
# Owner 2026-07-29, belegter Fall (Miguel, Layover SFO): der Marker „12:40 LT
# Pickup SFO" stand am 27.07. im gespeicherten Roster und war nach dem Import
# am 29.07. 14:57Z weg — Primaerquelle stumm (Abflug 30 h 43 min voraus, knapp
# ausserhalb des Horizonts), Zweitquelle stumm (myTime-Fetch kaputt, Grace lag
# nur im Prozess-Cache, Container an dem Tag 2x neu). Weil der Feed-Import
# jeden angefassten Tag frisch aufbaut, UPSERTet ein pickup-loser Import den
# Tag OHNE Marker. Diese Ebene macht das unmoeglich.

# Die Aufraeum-Grenze haengt an der Wanduhr; die Fixtures sind auf den 25./26.
# Juli 2026 datiert. Deshalb laufen die Last-Good-Tests mit fixer Uhr.
_LG_NOW = _dtmod.datetime(2026, 7, 26, 12, 0, tzinfo=_dtmod.timezone.utc)


def _apply_lg(token, ics):
    return fo.apply_pickup_last_good(token, ics, now=_LG_NOW)


def _fake_profiles(monkeypatch):
    """In-Memory-Profilspeicher (SB/Disk-frei) mit der ECHTEN Signatur von
    _profile_load/_profile_save — inkl. Kopier-Semantik."""
    import copy
    import app as backend
    store = {}

    def _load(token, fresh=False):
        return copy.deepcopy(store.get(token) or {'token': token, 'profile': {}})

    def _save(token, profile, full_disk_payload=None):
        store[token] = {'token': token, 'profile': copy.deepcopy(profile)}
        return True

    monkeypatch.setattr(backend, '_profile_load', _load)
    monkeypatch.setattr(backend, '_profile_save', _save)
    return store


def _ics_with_pickup():
    """FlightOps-ICS + Pickup aus dem myTime-Link (der Normal-Lauf)."""
    return fo.merge_ical_pickups(
        fo.duty_events_to_ics(DUTY_DTW_PICKUP),
        fo.ical_pickup_candidates(
            _mytime_ics(('14:00 LT Pickup DTW', '20260726T180000Z'))))


def test_pickups_in_ics_reads_marker_plus_anker():
    """Was gemerkt wird, ist der Marker MIT seinem Anker-Leg — ohne Anker
    koennte er spaeter nie geprueft wieder eingehaengt werden."""
    assert fo.pickups_in_ics(_ics_with_pickup()) == [{
        'day': '2026-07-26', 'utc': '2026-07-26T18:00:00Z',
        'summary': '14:00 LT Pickup DTW', 'anchor': '2026-07-26T20:00:00Z'}]
    # Ohne Anker (Pickup ohne passenden Abflug) wird nichts gemerkt.
    assert fo.pickups_in_ics(_mytime_ics(
        ('14:00 LT Pickup DTW', '20260726T180000Z'))) == []
    assert fo.pickups_in_ics('') == [] and fo.pickups_in_ics(None) == []


def test_last_good_pickup_survives_an_import_where_both_sources_are_silent(monkeypatch):
    """KERNFALL (a): Lauf 1 liefert den Marker, Lauf 2 liefert NICHTS —
    der Tag behaelt ihn trotzdem, Ende-zu-Ende durch die Feed-Pipeline."""
    import app as backend
    _fake_profiles(monkeypatch)
    good = _ics_with_pickup()
    assert _apply_lg('AT-LG1', good) == good     # nichts zu heilen
    assert [s['summary'] for s in fo.pickup_last_good_load('AT-LG1')] \
        == ['14:00 LT Pickup DTW']
    naked = fo.duty_events_to_ics(DUTY_DTW_PICKUP)               # beide Quellen stumm
    assert 'Pickup' not in naked
    healed = _apply_lg('AT-LG1', naked)
    assert 'SUMMARY:14:00 LT Pickup DTW' in healed
    b = backend._ics_events_to_briefings(backend._parse_ics_to_events(healed))[0]
    assert backend._rc_pickup_hhmm(b['2026-07-26']) == '14:00'


def test_last_good_pickup_falls_when_the_anchor_leg_moves(monkeypatch):
    """KERNFALL (b): der ankernde Leg ist verschoben/gestrichen ⇒ der alte
    Pickup wird NICHT wiederbelebt. „Nie loeschen" gilt nur solange der Anker
    steht — ein verschobener Umlauf hat auch eine andere Pickup-Zeit, und die
    duerfen wir nicht raten (Owner-Regel: nur echte Quellen)."""
    import copy
    _fake_profiles(monkeypatch)
    _apply_lg('AT-LG2', _ics_with_pickup())
    assert fo.pickup_last_good_load('AT-LG2')
    # Abflug 1 h spaeter — das lose 0…6-h-Fenster wuerde den Alt-Wert noch
    # durchlassen, der Anker-Vergleich nicht.
    moved = copy.deepcopy(DUTY_DTW_PICKUP)
    moved['rosterDays'][1]['events'][1]['startTime'] = '2026-07-26T21:00:00Z'
    assert 'Pickup' not in _apply_lg(
        'AT-LG2', fo.duty_events_to_ics(moved))
    # Und der gemerkte Stand ist damit auch weg (der Tag war abgedeckt).
    assert fo.pickup_last_good_load('AT-LG2') == []
    # Umlauf komplett gestrichen (Tag ohne Leg) ⇒ ebenfalls kein Marker.
    _fake_profiles(monkeypatch)
    _apply_lg('AT-LG3', _ics_with_pickup())
    gone = copy.deepcopy(DUTY_DTW_PICKUP)
    gone['rosterDays'][1]['events'] = []
    assert 'Pickup' not in _apply_lg(
        'AT-LG3', fo.duty_events_to_ics(gone))


def test_a_live_source_always_beats_the_last_good_value(monkeypatch):
    """Korrektur-Fall: LH/myTime traegt 13:30 nach ⇒ der neue Wert gewinnt und
    wird das neue Last-Good. Der Riegel friert nichts ein."""
    _fake_profiles(monkeypatch)
    _apply_lg('AT-LG4', _ics_with_pickup())
    corrected = fo.merge_ical_pickups(
        fo.duty_events_to_ics(DUTY_DTW_PICKUP), fo.ical_pickup_candidates(
            _mytime_ics(('13:30 LT Pickup DTW', '20260726T173000Z'))))
    out = _apply_lg('AT-LG4', corrected)
    assert 'SUMMARY:13:30 LT Pickup DTW' in out
    assert '14:00 LT Pickup' not in out
    assert [s['summary'] for s in fo.pickup_last_good_load('AT-LG4')] \
        == ['13:30 LT Pickup DTW']


def test_last_good_survives_a_fresh_process_after_a_deploy(monkeypatch):
    """KERNFALL (d): genau Miguels Lage — neuer Container (Prozess-Cache leer),
    myTime-Fetch kaputt, Primaerquelle stumm. Der Profil-Stand traegt."""
    import app as backend
    _fake_profiles(monkeypatch)
    monkeypatch.setattr(backend, '_server_ical_refresh_enabled', lambda: True)
    monkeypatch.setattr(fo, 'pickup_ical_url',
                        lambda tok, body=None: 'https://x/f.ics')
    text = _mytime_ics(('14:00 LT Pickup DTW', '20260726T180000Z'))
    monkeypatch.setattr(backend, '_fetch_calendar_feed_text', lambda u: (text, None))
    fo._pickup_ical_cache.clear()
    ics = fo.duty_events_to_ics(DUTY_DTW_PICKUP)
    run1 = _apply_lg(
        'AT-LG5', fo.apply_ical_pickup_fallback('AT-LG5', ics))
    assert 'SUMMARY:14:00 LT Pickup DTW' in run1
    # DEPLOY: Prozess-Cache weg, Link tot.
    fo._pickup_ical_cache.clear()
    monkeypatch.setattr(backend, '_fetch_calendar_feed_text',
                        lambda u: (None, 'fetch_failed'))
    run2 = _apply_lg(
        'AT-LG5', fo.apply_ical_pickup_fallback('AT-LG5', ics))
    assert 'SUMMARY:14:00 LT Pickup DTW' in run2
    b = backend._ics_events_to_briefings(backend._parse_ics_to_events(run2))[0]
    assert backend._rc_pickup_hhmm(b['2026-07-26']) == '14:00'
    fo._pickup_ical_cache.clear()


def test_last_good_handles_the_midnight_wrap_round_trip(monkeypatch):
    """Der Wrap-Fall (RN 171012) muss durch Merken+Wiedereinhaengen IDENTISCH
    wieder herauskommen — der Marker gehoert auf den RUECKFLUG-Tag."""
    import app as backend
    _fake_profiles(monkeypatch)
    wrapped = fo.merge_ical_pickups(
        fo.duty_events_to_ics(DUTY_KIX_MULTINIGHT), fo.ical_pickup_candidates(
            _mytime_ics(('06:50 LT Pickup KIX', '20260728T215000Z'))))
    _apply_lg('AT-LG6', wrapped)
    healed = _apply_lg(
        'AT-LG6', fo.duty_events_to_ics(DUTY_KIX_MULTINIGHT))
    b = backend._ics_events_to_briefings(backend._parse_ics_to_events(healed))[0]
    assert backend._rc_pickup_hhmm(b['2026-07-29']) == '06:50'
    assert not backend._rc_pickup_hhmm(b.get('2026-07-28') or {})


def test_last_good_writes_the_profile_only_on_real_change(monkeypatch):
    """Ein refresh-all-Lauf ueber alle Profile darf nicht pro User einen
    Profil-Write kosten — nur inhaltliche Aenderungen schreiben."""
    import app as backend
    _fake_profiles(monkeypatch)
    writes = []
    orig = backend._profile_save
    monkeypatch.setattr(backend, '_profile_save',
                        lambda tok, prof, full_disk_payload=None:
                        (writes.append(tok), orig(tok, prof))[1])
    good = _ics_with_pickup()
    _apply_lg('AT-LG7', good)
    assert writes == ['AT-LG7']
    for _ in range(3):
        _apply_lg('AT-LG7', good)
    assert writes == ['AT-LG7']                 # unveraendert ⇒ kein Write
    # Ein Import ganz ohne Pickup und ohne Vorstand schreibt gar nichts.
    _apply_lg('AT-LG8', fo.duty_events_to_ics(DUTY_KIX_MULTINIGHT))
    assert writes == ['AT-LG7']


def test_last_good_keeps_days_this_import_did_not_cover(monkeypatch):
    """Ein Import mit engerem Fenster darf die Marker der uebrigen Tage nicht
    stillschweigend wegwerfen — ersetzt wird nur, was das ICS abdeckt."""
    _fake_profiles(monkeypatch)
    prev = [{'day': '2026-08-10', 'utc': '2026-08-10T05:00:00Z',
             'summary': '07:00 LT Pickup SFO', 'anchor': '2026-08-10T08:00:00Z',
             'ts': '2026-07-26T10:00:00Z'}]
    keep = fo.pickup_last_good_store('AT-LG9', _ics_with_pickup(),
                                     previous=prev, now=_LG_NOW)
    assert sorted(k['day'] for k in keep) == ['2026-07-26', '2026-08-10']
    # Deckt der Import den Tag AB und liefert dort nichts, faellt der Eintrag —
    # genau so stirbt ein wirklich gestrichener Pickup.
    keep2 = fo.pickup_last_good_store('AT-LG9',
                                      fo.duty_events_to_ics(DUTY_DTW_PICKUP),
                                      previous=keep, now=_LG_NOW)
    assert [k['day'] for k in keep2] == ['2026-08-10']


def test_last_good_prunes_past_days_and_stale_entries(monkeypatch):
    """Aufraeumen: vergangene Tage und lange nicht mehr bestaetigte Eintraege
    fallen raus, damit das Profil nicht waechst."""
    from datetime import datetime, timezone
    _fake_profiles(monkeypatch)
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    prev = [
        {'day': '2026-07-26', 'utc': '2026-07-26T18:00:00Z',      # laengst vorbei
         'summary': '14:00 LT Pickup DTW', 'anchor': '2026-07-26T20:00:00Z',
         'ts': '2026-07-26T10:00:00Z'},
        {'day': '2026-09-01', 'utc': '2026-09-01T05:00:00Z',      # zu lange her
         'summary': '07:00 LT Pickup JFK', 'anchor': '2026-09-01T08:00:00Z',
         'ts': '2026-07-01T10:00:00Z'},
        {'day': '2026-08-25', 'utc': '2026-08-25T05:00:00Z',      # bleibt
         'summary': '07:00 LT Pickup SFO', 'anchor': '2026-08-25T08:00:00Z',
         'ts': '2026-08-19T10:00:00Z'}]
    keep = fo.pickup_last_good_store('AT-LGA', '', previous=prev, now=now)
    assert [k['day'] for k in keep] == ['2026-08-25']


def test_last_good_layer_is_inert_for_users_without_any_pickup(monkeypatch):
    """Kein Pickup, kein Vorstand ⇒ das ICS bleibt Byte-identisch."""
    _fake_profiles(monkeypatch)
    ics = fo.duty_events_to_ics(DUTY_DTW_PICKUP)
    assert _apply_lg('AT-LGB', ics) == ics
    assert _apply_lg('AT-LGB', None) is None
    assert _apply_lg('AT-LGB', '') == ''


def test_last_good_strict_anchor_needs_the_field(monkeypatch):
    """Ein Kandidat OHNE anchor darf im strikten Modus nichts einhaengen —
    sonst waere Last-Good schwaecher gerieglt als die Live-Quelle."""
    ics = fo.duty_events_to_ics(DUTY_DTW_PICKUP)
    loose = [{'utc': '2026-07-26T18:00:00Z', 'summary': '14:00 LT Pickup DTW'}]
    assert fo.merge_ical_pickups(ics, loose, strict_anchor=True) == ics
    assert 'Pickup' in fo.merge_ical_pickups(ics, loose)
    tagged = [dict(loose[0], anchor='2026-07-26T20:00:00Z')]
    assert 'Pickup' in fo.merge_ical_pickups(ics, tagged, strict_anchor=True)


# ── Welche Umlaeufe kosten einen Call? ───────────────────────────────────────

def _now(iso):
    from datetime import datetime, timezone
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


def test_pickup_rotation_ids_only_layover_returns_in_the_horizon():
    """Nur Legs, die AB einer Hotel-Station starten und im Horizont liegen."""
    # 26.07. 12:00Z → LH443 (26.07. 20:00Z ab DTW mit Hotel-Nacht) qualifiziert.
    assert fo.pickup_rotation_ids(DUTY_DTW_PICKUP,
                                 now=_now('2026-07-26T12:00:00')) == ['169929']
    # Das Hinflug-Leg LH442 startet an FRA — dort liegt keine Hotel-Nacht →
    # bei einem Fenster, das NUR den Hinflug enthaelt, faellt kein Call an.
    assert fo.pickup_rotation_ids(DUTY_DTW_PICKUP,
                                 now=_now('2026-07-25T06:00:00'),
                                 horizon_h=6) == []
    # Weit vorne: LH traegt den Wert erst ~1 Tag vorher nach → kein Call.
    assert fo.pickup_rotation_ids(DUTY_DTW_PICKUP,
                                 now=_now('2026-07-20T12:00:00')) == []
    # Schon abgeflogen und ausserhalb des Rueckblicks → kein Call.
    assert fo.pickup_rotation_ids(DUTY_DTW_PICKUP,
                                 now=_now('2026-07-27T12:00:00')) == []


def test_far_horizon_fires_only_while_no_pickup_is_known():
    """KERNFALL (c): Miguels Abflug lag beim Import 30 h 43 min voraus — knapp
    hinter der 30-h-Kante, also fiel der Rotations-Call aus. Der Fern-Horizont
    (36 h) holt ihn, kostet aber NUR, solange wir fuer genau dieses Leg noch
    keinen Pickup kennen."""
    # LH443 startet 26.07. 20:00Z.
    known = {'2026-07-26T20:00'}
    far = _now('2026-07-25T13:15:00')          # 30 h 45 min voraus
    assert fo.pickup_rotation_ids(DUTY_DTW_PICKUP, now=far) == ['169929']
    assert fo.pickup_rotation_ids(DUTY_DTW_PICKUP, now=far,
                                  known_anchors=known) == []
    # Ein BEKANNTER Pickup eines ANDEREN Legs schuetzt nicht.
    assert fo.pickup_rotation_ids(DUTY_DTW_PICKUP, now=far,
                                  known_anchors={'2026-08-01T20:00'}) == ['169929']
    # Innerhalb der 30 h wird immer frisch geholt (LH korrigiert die Zeit noch).
    assert fo.pickup_rotation_ids(DUTY_DTW_PICKUP, now=_now('2026-07-26T12:00:00'),
                                  known_anchors=known) == ['169929']
    # Grenzen: 35 h ja, 37 h nein.
    assert fo.pickup_rotation_ids(DUTY_DTW_PICKUP,
                                  now=_now('2026-07-25T09:00:00')) == ['169929']
    assert fo.pickup_rotation_ids(DUTY_DTW_PICKUP,
                                  now=_now('2026-07-25T07:00:00')) == []
    assert fo._ROT_PICKUP_HORIZON_H == 30 and fo._ROT_PICKUP_HORIZON_FAR_H == 36


def test_pickup_rotation_ids_keeps_a_just_departing_leg_in_the_lookback():
    """Die Kachel „Dienst heute" liest den Marker bis zum Abflug — ein Leg, das
    gerade weg ist, behaelt seinen Call im 3-h-Rueckblick."""
    assert fo.pickup_rotation_ids(DUTY_DTW_PICKUP,
                                 now=_now('2026-07-26T22:00:00')) == ['169929']


def test_pickup_rotation_ids_dedupes_one_call_per_rotation():
    """Mehrere Layover-Rueckfluege desselben Umlaufs = EIN Call."""
    payload = {"rosterDays": [
        {"day": "2026-07-26Z", "events": [
            {"eventType": "HOTEL", "eventCategory": "hotel", "wholeDay": False,
             "startTime": None, "startLocation": "JFK"},
            {"eventType": "FLIGHT", "eventCategory": "flight",
             "eventDetails": "LH401", "wholeDay": False,
             "startTime": "2026-07-26T18:00:00Z", "startLocation": "JFK",
             "endTime": "2026-07-27T04:00:00Z", "endLocation": "FRA",
             "eventAttributes": {"rotationId": 555}}]},
        {"day": "2026-07-27Z", "events": [
            {"eventType": "HOTEL", "eventCategory": "hotel", "wholeDay": False,
             "startTime": None, "startLocation": "FRA"},
            {"eventType": "FLIGHT", "eventCategory": "flight",
             "eventDetails": "LH100", "wholeDay": False,
             "startTime": "2026-07-27T09:00:00Z", "startLocation": "FRA",
             "endTime": "2026-07-27T10:00:00Z", "endLocation": "MUC",
             "eventAttributes": {"rotationId": 555}}]}]}
    assert fo.pickup_rotation_ids(payload,
                                 now=_now('2026-07-26T12:00:00')) == ['555']


def test_pickup_rotation_ids_is_capped_and_ordered_earliest_first():
    days = []
    for i in range(11):
        d = f'2026-07-{26 + (i // 4):02d}'
        days.append({"day": f"{d}Z", "events": [
            {"eventType": "HOTEL", "eventCategory": "hotel", "wholeDay": False,
             "startTime": None, "startLocation": "JFK"},
            {"eventType": "FLIGHT", "eventCategory": "flight",
             "eventDetails": f"LH{400 + i}", "wholeDay": False,
             "startTime": f"{d}T{(i % 4) * 2 + 8:02d}:00:00Z",
             "startLocation": "JFK", "endTime": f"{d}T23:00:00Z",
             "endLocation": "FRA",
             "eventAttributes": {"rotationId": 900 + i}}]})
    got = fo.pickup_rotation_ids({"rosterDays": days},
                                 now=_now('2026-07-26T06:00:00'))
    # Kappe == _ROT_RN_PER_CALL: so viele passen in EINEN Call, mehr kostet
    # nichts extra und mehr als 6 Layover-Rueckfluege in 30 h gibt es nicht.
    assert len(got) == fo._ROT_MAX_PER_IMPORT == fo._ROT_RN_PER_CALL == 6
    assert got == ['900', '901', '902', '903', '904', '905']


def test_pickup_rotation_ids_needs_a_rotation_id_and_survives_garbage():
    no_attr = {"rosterDays": [{"day": "2026-07-26Z", "events": [
        {"eventType": "HOTEL", "eventCategory": "hotel", "wholeDay": False,
         "startTime": None, "startLocation": "DTW"},
        {"eventType": "FLIGHT", "eventCategory": "flight",
         "eventDetails": "LH443", "wholeDay": False,
         "startTime": "2026-07-26T20:00:00Z", "startLocation": "DTW",
         "endTime": "2026-07-27T04:00:00Z", "endLocation": "FRA"}]}]}
    assert fo.pickup_rotation_ids(no_attr, now=_now('2026-07-26T12:00:00')) == []
    assert fo.pickup_rotation_ids(None) == []
    assert fo.pickup_rotation_ids({'rosterDays': 'nonsense'}) == []
    assert fo.parse_rotation_pickups(None) == {}
    assert fo.parse_rotation_pickups({'rotations': 'nonsense'}) == {}


def test_pickup_rotation_ids_ignores_a_hotel_night_far_from_the_leg():
    """Dieselbe Station, aber die Hotel-Nacht liegt Wochen entfernt → das ist
    ein anderer Umlauf, kein Layover-Rueckflug. Kein Call."""
    payload = {"rosterDays": [
        {"day": "2026-07-02Z", "events": [
            {"eventType": "HOTEL", "eventCategory": "hotel", "wholeDay": False,
             "startTime": None, "startLocation": "DTW"}]},
        {"day": "2026-07-26Z", "events": [
            {"eventType": "FLIGHT", "eventCategory": "flight",
             "eventDetails": "LH443", "wholeDay": False,
             "startTime": "2026-07-26T20:00:00Z", "startLocation": "DTW",
             "endTime": "2026-07-27T04:00:00Z", "endLocation": "FRA",
             "eventAttributes": {"rotationId": 169929}}]}]}
    assert fo.pickup_rotation_ids(payload, now=_now('2026-07-26T12:00:00')) == []


# ── Cache / Kosten / Notbremse ───────────────────────────────────────────────

def _rot_resp(*legs):
    return {"rotations": [{"shifts": [{"legs": list(legs)}]}]}


_LEG_DTW = {"flightDesignator": "LH443", "departureAirport": "DTW",
            "arrivalAirport": "FRA", "depatureDate": "2026-07-26T20:00:00Z",
            "pickupTime": "2026-07-26T18:00:00Z", "pickupTimeLT": "14:00",
            "hotelName": "The Henry Hotel Dearborn"}


def _reset_rot_state(monkeypatch):
    monkeypatch.setattr(fo, '_rot_cache', {})
    monkeypatch.setattr(fo, '_rot_budget_memo', [0.0, 0])
    import blueprints.aerox_data_blueprint as adb
    monkeypatch.setattr(adb, '_budget_key_used', lambda k: 0)


def test_rotation_pickups_for_batches_all_misses_into_one_call(monkeypatch):
    """COMMON_CREW_ROTATION nimmt bis 6 RNs pro Request. Alle Cache-Misses
    eines Imports muessen in EINEN Call gehen — sonst reisst ein User mit 4
    Umlaeufen 4 Calls in <1 s gegen das 5/s-Limit des Keys."""
    _reset_rot_state(monkeypatch)
    calls = []

    def _rot(tok, *rns):
        calls.append(rns)
        return _rot_resp(_LEG_DTW)
    monkeypatch.setattr(fo, 'crew_rotation', _rot)
    got = fo.rotation_pickups_for('AT-X', ['1', '2', '3', '4', '1'])
    assert calls == [('1', '2', '3', '4')]          # EIN Call, dedupliziert
    assert got[('LH443', 'DTW', 'FRA', '20260726')]['pickup_lt'] == '14:00'
    # zweiter Lauf komplett aus dem Cache
    fo.rotation_pickups_for('AT-X', ['1', '2', '3', '4'])
    assert len(calls) == 1


def test_rotation_cache_is_scoped_per_token_not_per_rotation(monkeypatch):
    """Die Rotations-Response ist ROLLENSPEZIFISCH (Coc/Cab: verschiedene
    Hotels und Pickup-Zeiten). Ein Cache nur ueber die rotationId serviert dem
    Kollegen der anderen Rolle fremde Werte."""
    _reset_rot_state(monkeypatch)
    calls = []
    monkeypatch.setattr(fo, 'crew_rotation',
                        lambda tok, *rns: calls.append(tok) or _rot_resp(_LEG_DTW))
    fo.rotation_pickups_for('AT-COCKPIT', ['169929'])
    fo.rotation_pickups_for('AT-CABIN', ['169929'])
    assert calls == ['AT-COCKPIT', 'AT-CABIN']
    fo.rotation_pickups_for('AT-COCKPIT', ['169929'])
    assert calls == ['AT-COCKPIT', 'AT-CABIN']     # jetzt gecacht, pro Token


def test_transient_lh_failure_is_not_negative_cached(monkeypatch):
    """_api_get gibt bei HTTP 403/500/Timeout None zurueck und WIRFT NICHT. Als
    „kein Pickup" gecacht haette ein einziger LH-Schluckauf den Marker 30 min
    geloescht und beim Wiederauftauchen eine erfundene „Dienstplan-Aenderung"
    gepusht. Nur eine echte, geparste Antwort darf cachen."""
    _reset_rot_state(monkeypatch)
    calls = []
    monkeypatch.setattr(fo, 'crew_rotation',
                        lambda tok, *rns: calls.append('fail') or None)
    assert fo.rotation_pickups_for('AT-X', ['169929']) == {}
    assert fo.rotation_pickups_for('AT-X', ['169929']) == {}
    assert calls == ['fail', 'fail']                # KEIN Negativ-Cache
    # ...und die Erholung kommt sofort an
    monkeypatch.setattr(fo, 'crew_rotation',
                        lambda tok, *rns: calls.append('ok') or _rot_resp(_LEG_DTW))
    got = fo.rotation_pickups_for('AT-X', ['169929'])
    assert got[('LH443', 'DTW', 'FRA', '20260726')]['pickup_lt'] == '14:00'


def test_parsed_but_pickupless_rotation_is_cached(monkeypatch):
    """Eine ECHTE Antwort ohne pickupTime darf cachen — sonst fragt jeder Sync
    denselben Umlauf erneut ab."""
    _reset_rot_state(monkeypatch)
    calls = []
    monkeypatch.setattr(fo, 'crew_rotation', lambda tok, *rns: (
        calls.append(rns) or _rot_resp(
            {"flightDesignator": "LH100", "departureAirport": "MUC",
             "arrivalAirport": "FRA", "depatureDate": "2026-07-26T08:00:00Z",
             "pickupTime": None})))
    assert fo.rotation_pickups_for('AT-X', ['7']) == {}
    assert fo.rotation_pickups_for('AT-X', ['7']) == {}
    assert len(calls) == 1


def test_rotation_pickups_for_never_raises(monkeypatch):
    _reset_rot_state(monkeypatch)

    def _boom(tok, *rns):
        raise RuntimeError('LH down')
    monkeypatch.setattr(fo, 'crew_rotation', _boom)
    assert fo.rotation_pickups_for('AT-X', ['2']) == {}
    assert fo.rotation_pickups_for('AT-X', []) == {}
    assert fo.rotation_pickups_for('AT-X', None) == {}
    assert fo.rotation_pickups_for('AT-X', 5) == {}          # nicht iterierbar
    assert fo.rotation_pickups_for('AT-X', ['', None]) == {}


def test_rotation_pickups_for_skips_calls_above_the_hour_ceiling(monkeypatch):
    """Der Roster darf NIE an einem Pickup verhungern: steht der lhfo-Zaehler
    diese Stunde hoch, fallen die Rotations-Calls komplett weg."""
    import blueprints.aerox_data_blueprint as adb
    _reset_rot_state(monkeypatch)
    calls = []
    monkeypatch.setattr(fo, 'crew_rotation',
                        lambda tok, *rns: calls.append(rns) or _rot_resp(_LEG_DTW))
    monkeypatch.setattr(adb, '_budget_key_used',
                        lambda k: fo._ROT_LHFO_HOUR_CEILING + 1)
    assert fo.rotation_pickups_for('AT-X', ['169929']) == {}
    assert calls == []
    monkeypatch.setattr(fo, '_rot_budget_memo', [0.0, 0])
    monkeypatch.setattr(adb, '_budget_key_used', lambda k: 10)
    fo.rotation_pickups_for('AT-X', ['169929'])
    assert calls == [('169929',)]


def test_hour_ceiling_lookup_is_memoized_off_the_hot_path(monkeypatch):
    """_budget_key_used geht auf Supabase — einmal pro Minute genuegt, sonst
    ~227 zusaetzliche SELECTs pro refresh-all-Lauf im Roster-Hot-Path."""
    import blueprints.aerox_data_blueprint as adb
    _reset_rot_state(monkeypatch)
    hits = []
    monkeypatch.setattr(adb, '_budget_key_used', lambda k: hits.append(k) or 0)
    monkeypatch.setattr(fo, 'crew_rotation', lambda tok, *rns: _rot_resp(_LEG_DTW))
    for i in range(5):
        fo.rotation_pickups_for(f'AT-{i}', ['169929'])
    assert len(hits) == 1


def test_rotation_call_is_counted_under_its_own_caller_label(monkeypatch):
    """Der neue Call muss von Anfang an im lhfo-Zaehler sichtbar sein —
    Label kommt aus dem Pfad: lhfo:<YYYYMMDDHH>:common_crew_rotation."""
    seen = []
    import blueprints.lh_open_api as loa
    monkeypatch.setattr(loa, 'budget_inc',
                        lambda prefix, caller=None, units=1: seen.append((prefix, caller)))
    monkeypatch.setattr(fo, '_valid_access', lambda tok: 'ACCESS')
    monkeypatch.setattr(fo.urllib.request, 'urlopen',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('no net')))
    fo.crew_rotation('AT-X', '169929')
    assert ('lhfo', 'COMMON_CREW_ROTATION') in seen


# ── Haertungen aus dem adversarialen Review (2026-07-27) ─────────────────────

def test_duplicate_route_on_one_day_drops_the_flightless_key():
    """MUC-FRA-MUC-FRA ist ein alltaeglicher LH-Umlauf. Der flugnummernlose
    Schluessel waere dort NICHT eindeutig — der Pickup des einen Legs landete
    am anderen (sichtbar als „PU 15:00" an einem Tag, der 07:00 Ortszeit
    beginnt), waehrend das Leg MIT echtem Pickup leer blieb."""
    resp = {"rotations": [{"shifts": [{"legs": [
        {"flightDesignator": "LH100", "departureAirport": "MUC",
         "arrivalAirport": "FRA", "depatureDate": "2026-07-28T05:00:00Z",
         "pickupTime": "2026-07-28T03:00:00Z", "pickupTimeLT": "05:00"},
        {"flightDesignator": "LH101", "departureAirport": "FRA",
         "arrivalAirport": "MUC", "depatureDate": "2026-07-28T09:00:00Z",
         "pickupTime": None},
        {"flightDesignator": "LH102", "departureAirport": "MUC",
         "arrivalAirport": "FRA", "depatureDate": "2026-07-28T11:00:00Z",
         "pickupTime": None}]}]}]}
    p = fo.parse_rotation_pickups(resp)
    assert ('LH100', 'MUC', 'FRA', '20260728') in p        # exakter Treffer bleibt
    assert ('', 'MUC', 'FRA', '20260728') not in p         # Route ist 2x belegt
    # → das Leg OHNE Pickup bekommt keinen fremden Wert angehaengt
    assert fo._pickup_for_leg(p, 'LH102', 'MUC', 'FRA', '20260728T110000Z') is None
    assert fo._pickup_for_leg(p, 'LH100', 'MUC', 'FRA', '20260728T050000Z')
    # Eine EINMALIGE Route behaelt ihren flugnummernlosen Schluessel.
    single = fo.parse_rotation_pickups({"rotations": [{"shifts": [{"legs": [
        {"flightDesignator": "LH443", "departureAirport": "DTW",
         "arrivalAirport": "FRA", "depatureDate": "2026-07-26T20:00:00Z",
         "pickupTime": "2026-07-26T18:00:00Z"}]}]}]})
    assert ('', 'DTW', 'FRA', '20260726') in single


def test_duplicate_route_end_to_end_emits_no_bogus_pickup():
    payload = {"rosterDays": [{"day": "2026-07-28Z", "events": [
        {"eventType": "HOTEL", "eventCategory": "hotel", "wholeDay": False,
         "startTime": None, "startLocation": "MUC"},
        {"eventType": "FLIGHT", "eventCategory": "flight",
         "eventDetails": "LH100", "wholeDay": False,
         "startTime": "2026-07-28T05:00:00Z", "startLocation": "MUC",
         "endTime": "2026-07-28T06:00:00Z", "endLocation": "FRA"},
        {"eventType": "FLIGHT", "eventCategory": "flight",
         "eventDetails": "LH102", "wholeDay": False,
         "startTime": "2026-07-28T11:00:00Z", "startLocation": "MUC",
         "endTime": "2026-07-28T12:00:00Z", "endLocation": "FRA"}]}]}
    pu = fo.parse_rotation_pickups({"rotations": [{"shifts": [{"legs": [
        {"flightDesignator": "LH100", "departureAirport": "MUC",
         "arrivalAirport": "FRA", "depatureDate": "2026-07-28T05:00:00Z",
         "pickupTime": "2026-07-28T03:00:00Z", "pickupTimeLT": "05:00"},
        {"flightDesignator": "LH102", "departureAirport": "MUC",
         "arrivalAirport": "FRA", "depatureDate": "2026-07-28T11:00:00Z",
         "pickupTime": None}]}]}]})
    ics = fo.duty_events_to_ics(payload, pickups=pu)
    assert ics.count('LT Pickup MUC') == 1


def test_implausible_pickup_lead_is_refused():
    """0…6 h vor dem Abflug — genau das Fenster, das der Konsument
    (crew_live_state.pickup_utc_for_leg) ohnehin erzwingt. Was der still
    verwirft, darf gar nicht erst in den Roster geschrieben werden: sonst zeigt
    die App eine Zeit, die die Pre-Flight-Timeline nicht benutzt."""
    assert fo._pickup_lead_ok('2026-07-26T18:00:00Z', '2026-07-26T20:00:00Z')
    assert fo._pickup_lead_ok('2026-07-28T21:50:00Z', '2026-07-29T00:30:00Z')
    assert fo._pickup_lead_ok('2026-07-26T14:00:00Z', '2026-07-26T20:00:00Z')  # 6h
    # NACH dem Abflug
    assert not fo._pickup_lead_ok('2026-07-26T21:00:00Z', '2026-07-26T20:00:00Z')
    # 14 h Vorlauf
    assert not fo._pickup_lead_ok('2026-07-26T06:00:00Z', '2026-07-26T20:00:00Z')
    # 3 Tage alter Wert (Stale-Cache + -+1-Tag-Toleranz)
    assert not fo._pickup_lead_ok('2026-07-23T18:00:00Z', '2026-07-26T20:00:00Z')
    for bad in ((None, None), ('', '2026-07-26T20:00:00Z'), ('kaputt', 'x')):
        assert not fo._pickup_lead_ok(*bad)
    # ...und der Riegel greift im ICS-Pfad
    for bad_pu in ('2026-07-27T03:00:00Z', '2026-07-26T04:00:00Z'):
        pu = {('LH443', 'DTW', 'FRA', '20260726'): {
            'pickup_utc': bad_pu, 'pickup_lt': '12:00', 'hotel': None,
            'station': 'DTW'}}
        assert 'Pickup' not in fo.duty_events_to_ics(DUTY_DTW_PICKUP, pickups=pu)


def test_pickup_time_utc_wins_over_a_disagreeing_pickuptimelt():
    """Die angezeigte Ortszeit muss zu dem UTC-Wert passen, gegen den
    plausibilisiert wurde. Ein LT, das nicht dazu passt (LH rendert es in
    Base-TZ o. ae.), zeigte sonst eine falsche Zeit UND killte die
    Pre-Flight-Timeline still (6-h-Fenster)."""
    pu = {('LH443', 'DTW', 'FRA', '20260726'): {
        'pickup_utc': '2026-07-26T18:00:00Z', 'pickup_lt': '20:00',
        'hotel': None, 'station': 'DTW'}}
    ics = fo.duty_events_to_ics(DUTY_DTW_PICKUP, pickups=pu)
    # 18:00Z ist 14:00 in America/Detroit — nicht 20:00.
    assert 'SUMMARY:14:00 LT Pickup DTW' in ics
    assert '20:00 LT Pickup' not in ics


def test_pickup_rotation_ids_reads_scalar_event_attributes():
    """LH-Known-Issue: Ein-Element-Arrays kommen als Skalar. Defensiv in BEIDE
    Richtungen — als [{...}] gerendert waere das Feature sonst lautlos dunkel."""
    for attrs in ({"rotationId": 901}, [{"rotationId": 901}]):
        payload = {"rosterDays": [{"day": "2026-07-26Z", "events": [
            {"eventType": "HOTEL", "eventCategory": "hotel", "wholeDay": False,
             "startTime": None, "startLocation": "DTW"},
            {"eventType": "FLIGHT", "eventCategory": "flight",
             "eventDetails": "LH443", "wholeDay": False,
             "startTime": "2026-07-26T20:00:00Z", "startLocation": "DTW",
             "endTime": "2026-07-27T04:00:00Z", "endLocation": "FRA",
             "eventAttributes": attrs}]}]}
        assert fo.pickup_rotation_ids(
            payload, now=_now('2026-07-26T12:00:00')) == ['901']


def test_pickup_for_leg_survives_garbage_input():
    assert fo._pickup_for_leg(None, 'LH1', 'AAA', 'BBB', '20260726T000000Z') is None
    assert fo._pickup_for_leg('nonsense', 'LH1', 'AAA', 'BBB', '20260726T000000Z') is None
    assert fo._pickup_for_leg({}, 'LH1', None, 'BBB', '20260726T000000Z') is None
    assert fo._pickup_for_leg({('a',): 1}, 'LH1', 'AAA', 'BBB', None) is None


def test_repo_fixture_second_wrap_case_end_to_end():
    """Die Repo-Fixture traegt einen ZWEITEN, nicht dokumentierten Wrap-Fall:
    LH493 YVR-FRA, Pickup 19.08. 21:05Z (Berlin 23:05 am 19.), Abflug 23:15Z
    (Berlin 01:15 am 20.). Er muss auf dem 20. landen und exakt rekonstruierbar
    sein."""
    import app as backend
    from blueprints.crew_live_state import parse_pickup_hhmm, pickup_utc_for_leg
    p = fo.parse_rotation_pickups(_rot_fixture())
    payload = {"rosterDays": [
        {"day": "2026-08-17Z", "events": [
            {"eventType": "HOTEL", "eventCategory": "hotel", "wholeDay": False,
             "startTime": None, "startLocation": "YVR"}]},
        {"day": "2026-08-20Z", "events": [
            {"eventType": "FLIGHT", "eventCategory": "flight",
             "eventDetails": "LH493", "wholeDay": False,
             "startTime": "2026-08-19T23:15:00Z", "startLocation": "YVR",
             "endTime": "2026-08-20T08:55:00Z", "endLocation": "FRA"}]}]}
    ics = fo.duty_events_to_ics(payload, pickups=p)
    assert 'LT Pickup YVR' in ics
    b = backend._ics_events_to_briefings(backend._parse_ics_to_events(ics))[0]
    hh = backend._rc_pickup_hhmm(b['2026-08-20'])
    assert hh == '14:05'
    got = pickup_utc_for_leg(parse_pickup_hhmm(f'{hh} LT Pickup YVR'),
                             '2026-08-19T23:15:00Z', 'America/Vancouver')
    assert got.strftime('%Y-%m-%dT%H:%M:%SZ') == '2026-08-19T21:05:00Z'


def test_hotelname_is_paired_from_the_outbound_leg():
    """Live gemessen (2026-07-27, 24 PROD-Legs): hotelName haengt am HINFLUG
    (arrivalAirport = Hotel-Station), pickupTime am RUECKFLUG — NIE am selben
    Leg. Ein Parser, der nur Pickup-Legs ansieht, verliert jeden Hotelnamen."""
    resp = {"rotations": [{"shifts": [
        {"legs": [{"flightDesignator": "LH424", "departureAirport": "MUC",
                   "arrivalAirport": "BOS",
                   "depatureDate": "2026-07-25T13:00:00Z",
                   "pickupTime": None, "hotelName": "Hyatt Regency Boston",
                   "hotel": False}]},
        {"legs": [{"flightDesignator": "LH425", "departureAirport": "BOS",
                   "arrivalAirport": "MUC",
                   "depatureDate": "2026-07-27T00:00:00Z",
                   "pickupTime": "2026-07-26T22:15:00Z",
                   "pickupTimeLT": "18:15", "hotelName": None,
                   "hotel": False}]}]}]}
    p = fo.parse_rotation_pickups(resp)
    v = p[('LH425', 'BOS', 'MUC', '20260727')]
    assert v['hotel'] == 'Hyatt Regency Boston'
    assert v['station'] == 'BOS'
    assert v['pickup_lt'] == '18:15'
    # Das Hinflug-Leg selbst hat keinen Pickup → taucht nicht auf.
    assert not [k for k in p if k[0] == 'LH424']


def test_hotelname_does_not_leak_across_stations():
    """Zwei Layover in einem Umlauf → jede Station behaelt IHREN Namen."""
    resp = {"rotations": [{"shifts": [
        {"legs": [{"flightDesignator": "LH1", "departureAirport": "FRA",
                   "arrivalAirport": "LIS", "depatureDate": "2026-07-25T06:00:00Z",
                   "pickupTime": None, "hotelName": "Altis Grand Hotel"},
                  {"flightDesignator": "LH2", "departureAirport": "LIS",
                   "arrivalAirport": "WAW", "depatureDate": "2026-07-26T06:00:00Z",
                   "pickupTime": "2026-07-26T03:40:00Z", "pickupTimeLT": "03:40",
                   "hotelName": "Mercure Warszawa Grand"},
                  {"flightDesignator": "LH3", "departureAirport": "WAW",
                   "arrivalAirport": "FRA", "depatureDate": "2026-07-27T06:00:00Z",
                   "pickupTime": "2026-07-27T03:15:00Z", "pickupTimeLT": "05:15",
                   "hotelName": None}]}]}]}
    p = fo.parse_rotation_pickups(resp)
    assert p[('LH2', 'LIS', 'WAW', '20260726')]['hotel'] == 'Altis Grand Hotel'
    assert p[('LH3', 'WAW', 'FRA', '20260727')]['hotel'] == 'Mercure Warszawa Grand'


# ── Grant-Burn #4 (Massen-Burn 26.07.2026): Rotations-Save + Soft-Guard ─────
def test_rotation_save_failure_parks_tokens_and_heals(monkeypatch):
    """Schlägt der Persist NACH erfolgreicher LH-Rotation fehl (SB-Ausfall),
    darf der neue RT nicht verloren gehen: er wird geparkt, _tokens_load
    serviert ihn weiter (Mirror hängt am konsumierten RT) und persistiert
    nach, sobald SB wieder schreibbar ist."""
    import time as _t
    fo._rotated_pending.clear()
    monkeypatch.setattr(fo, '_ROTATED_SAVE_RETRIES', 2)
    monkeypatch.setattr(fo, '_ROTATED_SAVE_BACKOFF_S', 0)
    calls = []
    monkeypatch.setattr(fo, '_tokens_save', lambda tok, t: calls.append(dict(t)) and False)
    nt = {'access': 'NEW', 'refresh': 'R2', 'scope': 's',
          'expires_at': _t.time() + 3540}
    assert fo._tokens_save_rotated('AT-U', 'R1', nt) is False
    assert len(calls) == 2                      # echte Retries
    assert fo._rotated_pending['AT-U']['tokens']['refresh'] == 'R2'
    # Mirror zeigt noch den KONSUMIERTEN R1 → Prozess-Kopie ist die Wahrheit
    got = fo._rotated_pending_take('AT-U', {'access': 'OLD', 'refresh': 'R1'})
    assert got and got['refresh'] == 'R2'
    assert 'AT-U' in fo._rotated_pending       # Save schlug wieder fehl → bleibt
    # SB wieder da → Nachsave heilt und räumt den Parkplatz
    monkeypatch.setattr(fo, '_tokens_save', lambda tok, t: True)
    got = fo._rotated_pending_take('AT-U', {'access': 'OLD', 'refresh': 'R1'})
    assert got and got['refresh'] == 'R2'
    assert 'AT-U' not in fo._rotated_pending


def test_rotation_pending_discarded_when_foreign_state_newer(monkeypatch):
    """Hat INZWISCHEN ein anderer Prozess erfolgreich rotiert+persistiert
    (Mirror-RT ≠ konsumierter ≠ unserer), ist die Parkkopie Geschichte —
    sie darf den neueren Stand nie überschreiben."""
    fo._rotated_pending.clear()
    fo._rotated_pending['AT-U'] = {
        'tokens': {'access': 'NEW', 'refresh': 'R2'},
        'consumed_rt': 'R1', 'ts': __import__('time').time()}
    monkeypatch.setattr(fo, '_tokens_save', lambda tok, t: (_ for _ in ()).throw(
        AssertionError('verworfene Parkkopie darf nicht gespeichert werden')))
    assert fo._rotated_pending_take(
        'AT-U', {'access': 'X', 'refresh': 'R3'}) is None
    assert 'AT-U' not in fo._rotated_pending


def test_rotated_save_superseded_by_relogin_is_dropped(monkeypatch):
    """CHAOS: während der Rotation loggt sich der User neu ein (neue
    Familie). Der Rotations-Save der ALTEN Familie darf die neue nie
    überschreiben — CAS/Readback erkennt den fremden Stand, Kopie wird
    verworfen, nichts geparkt."""
    fo._rotated_pending.clear()
    monkeypatch.setattr(fo, '_save_rotated_cas', lambda tok, c, t: 'superseded')
    monkeypatch.setattr(fo, '_tokens_save', lambda tok, t: (_ for _ in ()).throw(
        AssertionError('superseded darf nie plain-saven')))
    assert fo._tokens_save_rotated('AT-U', 'R1', {
        'access': 'NEW', 'refresh': 'R2'}) is True
    assert 'AT-U' not in fo._rotated_pending
    # …und der Readback-Fallback (RPC fehlt) erkennt dasselbe:
    monkeypatch.setattr(fo, '_save_rotated_cas', lambda tok, c, t: None)
    monkeypatch.setattr(fo, '_tokens_mirror_raw', lambda tok: {'refresh': 'R9'})
    assert fo._tokens_save_rotated('AT-U', 'R1', {
        'access': 'NEW', 'refresh': 'R2'}) is True
    assert 'AT-U' not in fo._rotated_pending


def test_generic_profile_save_cannot_clobber_tokens(monkeypatch):
    """RACEFLÄCHEN-FUND 2026-07-27: generische Profil-Saves (Crew-Cache,
    Location, pk) tragen eine STALE flightops_tokens-Kopie im Blob — der
    Top-Level-Merge hätte damit eine frisch rotierte Familie überschrieben
    (konsumierter RT zurück im Mirror ⇒ nächster Refresh = Reuse = Burn).
    app._profile_save_to_supabase strippt den Key jetzt strukturell; nur
    der Token-Store (Writer-Flag) darf ihn schreiben."""
    import app as backend
    merged = []
    monkeypatch.setattr(backend, 'SB_AVAILABLE', True)
    monkeypatch.setattr(backend, '_profile_metadata_merge_sb',
                        lambda tok, meta: merged.append(dict(meta)) or True)

    class _T:
        def upsert(self, *a, **k):
            return self

        def execute(self):
            return type('R', (), {'data': []})()
    monkeypatch.setattr(backend, 'sb', type('SB', (), {
        'table': lambda self, n: _T()})())
    with backend.app.test_request_context():
        # generischer Save (z.B. Crew-Cache): Key wird GESTRIPPT
        backend._profile_save_to_supabase('AT-U', {
            'flightops_crew_cache': [1], 'flightops_tokens': {'refresh': 'STALE'}})
        assert 'flightops_tokens' not in merged[-1]
        assert 'flightops_crew_cache' in merged[-1]
        # Token-Store (Writer-Flag über _tokens_save): Key kommt durch
        monkeypatch.setattr(backend, '_profile_load_from_disk', lambda tok: {})
        monkeypatch.setattr(backend, '_user_profile_path', lambda tok: None)
        assert fo._tokens_save('AT-U', {'refresh': 'R2'}) is True
        assert merged[-1] == {'flightops_tokens': {'refresh': 'R2'}}


def test_refresher_due_selection_orders_by_expiry():
    """Fällig-Auswahl: nur Grants mit RT, ohne needs_relogin, innerhalb des
    Vorlaufs — knappste zuerst (planbare, entzerrte Rotationen).

    POLICY-UPDATE 2026-07-28 (Quota-Diät, Lazy Rotation): »läuft bald ab« ist
    nur noch die NOTWENDIGE Bedingung — rotiert wird bei Bedarf (Demand) oder
    Keepalive (>20 h ohne Rotation). Deshalb wird hier Demand für die beiden
    fälligen Grants gesetzt; die geprüfte Zusage dieses Tests (Auswahl +
    Reihenfolge) ist unverändert. Die Lazy-Regel selbst prüft
    tests/test_lhfo_quota_diet.py."""
    now = 1000000.0
    scan = [
        ('AT-FRESH', {'refresh': 'R', 'expires_at': now + 7200}),
        ('AT-SOON', {'refresh': 'R', 'expires_at': now + 300}),
        ('AT-EXPIRED', {'refresh': 'R', 'expires_at': now - 50}),
        ('AT-DEAD', {'refresh': 'R', 'needs_relogin': True,
                     'expires_at': now - 50}),
        ('AT-NORT', {'expires_at': now - 50}),
    ]
    demand = {'AT-FRESH', 'AT-SOON', 'AT-EXPIRED', 'AT-DEAD', 'AT-NORT'}
    assert fo._refresher_due(scan, now=now,
                             demand=demand) == ['AT-EXPIRED', 'AT-SOON']


def test_refresher_exit_drain_sets_drain_and_joins(monkeypatch):
    """CHAOS Worker-Kill: gunicorn-Recycle/SIGTERM muss die laufende
    Rotation zu Ende kommen lassen (atexit-Drain + Join) statt sie zwischen
    LH-Rotation und Persist zu killen."""
    joined = []

    class _Th:
        def __init__(self):
            self._alive = True

        def is_alive(self):
            return self._alive

        def join(self, timeout=None):
            joined.append(timeout)
            self._alive = False
    fo._refresher_thread[0] = _Th()
    fo._refresher_state['drain'] = False
    fo._refresher_exit_drain()
    assert fo._refresher_state['drain'] is True
    assert joined and joined[0] == fo._EXIT_DRAIN_JOIN_SEC
    # Fake ohne is_alive → stiller No-Op
    fo._refresher_thread[0] = object()
    fo._refresher_exit_drain()
    fo._refresher_thread[0] = None
    fo._refresher_state.update(drain=False, busy=False, active=False)


def test_refresher_only_starts_with_role_env(monkeypatch):
    """Rollen-Gate: ohne LH_FLIGHTOPS_REFRESHER=1 startet NIRGENDS ein
    Refresher-Thread (Web-/MQTT-Container, Tests); mit Rolle + Creds genau
    einer pro Prozess."""
    monkeypatch.delenv('LH_FLIGHTOPS_REFRESHER', raising=False)
    monkeypatch.setattr(fo, '_KEY', 'k'); monkeypatch.setattr(fo, '_SECRET', 's')
    assert fo._maybe_start_refresher() is None
    monkeypatch.setenv('LH_FLIGHTOPS_REFRESHER', '1')
    started = []

    class _FakeTh:
        def __init__(self, *a, **k):
            started.append(k.get('name'))

        def start(self):
            pass
    monkeypatch.setattr(fo.threading, 'Thread', _FakeTh)
    th = fo._maybe_start_refresher()
    assert th is not None and started == ['fo-refresher']
    assert fo._maybe_start_refresher() is th        # idempotent
    fo._refresher_thread[0] = None


def test_relogin_watch_alerts_on_spike_and_dead_refresher(monkeypatch, tmp_path):
    """Wächter: needs_relogin-Sprung > N in ~1h ⇒ Alarm-Mail; ebenso ein
    konfigurierter, aber toter Refresher. Cooldown verhindert Mail-Spam."""
    import json as _json
    import app as backend
    monkeypatch.setattr(fo, '_RELOGIN_WATCH_STATE',
                        str(tmp_path / 'watch.json'))
    monkeypatch.delenv('ADSB_POLL_SECRET', raising=False)
    monkeypatch.setenv('LH_FLIGHTOPS_REFRESHER', '1')
    mails = []
    monkeypatch.setattr(fo, '_fo_watch_alert_mail',
                        lambda reasons, cnt, delta: mails.append(reasons) or True)
    counts = [10, 40]
    monkeypatch.setattr(fo, '_relogin_count', lambda: counts.pop(0))
    fo._refresher_state.update(active=False, drain=False, busy=False)
    c = backend.app.test_client()
    d1 = c.post('/api/internal/flightops/relogin-watch').get_json()
    # Lauf 1: Baseline (kein prev) — aber toter Refresher alarmiert sofort
    assert any('NICHT aktiv' in r for r in d1['reasons'])
    assert mails
    # Lauf 2: +30 in 1h → Spike-Grund dabei, aber Cooldown drosselt die Mail
    d2 = c.post('/api/internal/flightops/relogin-watch').get_json()
    assert d2['delta_1h'] == 30
    assert any('needs_relogin +30' in r for r in d2['reasons'])
    assert len(mails) == 1                       # Cooldown griff
    st = _json.loads((tmp_path / 'watch.json').read_text())
    assert st['count'] == 40


def test_refresh_all_exit_drain_sets_drain_and_joins(monkeypatch):
    """Worker-Recycle (gunicorn --max-requests) killte den refresh-all-Thread
    mid-Rotation — der atexit-Drain muss das drain-Flag setzen und auf den
    laufenden Grant warten. Wirft nie, auch mit Test-Fakes ohne is_alive."""
    joined = []

    class _Th:
        def __init__(self):
            self._alive = True

        def is_alive(self):
            return self._alive

        def join(self, timeout=None):
            joined.append(timeout)
            self._alive = False
    fo._refresh_all_thread[0] = _Th()
    fo._refresh_all_state['drain'] = False
    fo._refresh_all_state['running'] = True
    fo._refresh_all_exit_drain()
    assert fo._refresh_all_state['drain'] is True
    assert joined and joined[0] == fo._EXIT_DRAIN_JOIN_SEC
    # Fake ohne is_alive (Sync-Thread der Testsuite) → stiller No-Op
    fo._refresh_all_thread[0] = object()
    fo._refresh_all_exit_drain()
    fo._refresh_all_thread[0] = None
    fo._refresh_all_state.update(running=False, drain=False)


def test_relogin_watch_boot_grace_no_false_alarm(monkeypatch, tmp_path):
    """BOOT-KARENZ (Fehlalarm 27.07. 21:07): Wächter-Cron traf 38 s nach dem
    Containerstart — last_tick war noch 0.0 und „now − 0 > 15 min" meldete
    „Refresher-Loop steht", obwohl der Loop frisch aktiv war. Regel: ohne
    JEMALS einen Tick gibt es erst nach >10 min aktiver Laufzeit einen Grund;
    ein echter nie-tickender Loop alarmiert weiterhin."""
    import time as _time
    import app as backend
    monkeypatch.setattr(fo, '_RELOGIN_WATCH_STATE',
                        str(tmp_path / 'watch.json'))
    monkeypatch.delenv('ADSB_POLL_SECRET', raising=False)
    monkeypatch.setenv('LH_FLIGHTOPS_REFRESHER', '1')
    mails = []
    monkeypatch.setattr(fo, '_fo_watch_alert_mail',
                        lambda reasons, cnt, delta: mails.append(reasons) or True)
    monkeypatch.setattr(fo, '_relogin_count', lambda: 253)
    c = backend.app.test_client()

    # Frisch gebootet: aktiv seit 38 s, noch kein Tick ⇒ KEIN Grund, keine Mail.
    fo._refresher_state.update(active=True, drain=False, busy=False,
                               last_tick=0.0,
                               active_since=_time.time() - 38)
    d1 = c.post('/api/internal/flightops/relogin-watch').get_json()
    assert d1['reasons'] == []
    assert not mails

    # >10 min aktiv und IMMER NOCH nie getickt ⇒ ehrlicher Alarm.
    fo._refresher_state.update(active_since=_time.time() - 11 * 60)
    d2 = c.post('/api/internal/flightops/relogin-watch').get_json()
    assert any('NIE getickt' in r for r in d2['reasons'])

    # Normalfall: echter alter Tick > 15 min ⇒ „steht"-Grund unverändert.
    fo._refresher_state.update(last_tick=_time.time() - 16 * 60)
    d3 = c.post('/api/internal/flightops/relogin-watch').get_json()
    assert any('steht' in r for r in d3['reasons'])
    fo._refresher_state.update(active=False, last_tick=0.0, active_since=0.0)


# ═════════════════════════════════════════════════════════════════════════════
# GETEILTER Crew-Cache + Prefetch (Owner 2026-07-28: „Crew-Liste alles laden
# wenn verbunden, cachen und im Backend speichern. Crew mit dem gleichen Flug
# hat die Liste im Backend.")
# ═════════════════════════════════════════════════════════════════════════════

def test_crew_shared_ttl_staffel():
    """TTL-Staffel nach SCHADEN: Flugtag streng (45 min), künftiger Tag locker
    (6 h, dort handelt niemand danach), vergangener Tag praktisch dauerhaft
    (LH löscht die Liste nach dem Flug — der Cache IST dann die Wahrheit)."""
    import datetime as _d
    today = _d.date(2026, 7, 28)
    assert fo._crew_shared_ttl_s('2026-07-28', today) == fo._CREW_SHARED_TTL_TODAY_S
    assert fo._crew_shared_ttl_s('2026-07-30', today) == fo._CREW_SHARED_TTL_FUTURE_S
    assert fo._crew_shared_ttl_s('2026-07-27', today) == fo._CREW_SHARED_TTL_PAST_S
    # Unparsbares Datum ⇒ strengste Regel, nie die lockerste.
    assert fo._crew_shared_ttl_s('kaputt', today) == fo._CREW_SHARED_TTL_TODAY_S
    now = 1_000_000.0
    assert fo._crew_shared_fresh('2026-07-28', now - 600, now, today) is True
    assert fo._crew_shared_fresh('2026-07-28', now - 3600, now, today) is False
    assert fo._crew_shared_fresh('2026-07-30', now - 3600, now, today) is True
    assert fo._crew_shared_fresh('2026-07-27', now - 5 * 86400, now, today) is True
    # Ohne cached_at gibt es keine Frische-Aussage ⇒ kein Shortcut.
    assert fo._crew_shared_fresh('2026-07-28', None, now, today) is False


def test_crew_pick_best_prefers_exact_date_then_newest():
    """Exaktes Flugdatum schlägt die ±1-Tag-Toleranz; innerhalb des Datums
    gewinnt die jüngste Zeile — egal von welchem User."""
    rows = [
        {'token': 'A', 'flight_date': '2026-07-28', 'crew': [{'name': 'ALT'}],
         'cached_at': 100.0},
        {'token': 'B', 'flight_date': '2026-07-28', 'crew': [{'name': 'NEU'}],
         'cached_at': 900.0},
        {'token': 'C', 'flight_date': '2026-07-29', 'crew': [{'name': 'PLUS1'}],
         'cached_at': 9999.0},
    ]
    best = fo._crew_pick_best(rows, '2026-07-28')
    assert best['token'] == 'B' and best['crew'][0]['name'] == 'NEU'
    # Kein Eintrag am gefragten Tag ⇒ Toleranz greift auf den Nachbartag.
    assert fo._crew_pick_best(rows, '2026-07-30')['token'] == 'C'
    assert fo._crew_pick_best(rows, '2026-08-05') is None


def _shared_setup(monkeypatch, rows, links=True):
    """Tabelle mit fremden/eigenen Zeilen + verbundener User + Berechtigung."""
    _pass_auth_gate(monkeypatch)
    monkeypatch.setattr(fo, '_KEY', 'k'); monkeypatch.setattr(fo, '_SECRET', 's')
    monkeypatch.setattr(fo, '_access_state', lambda tok: ('ok', 'ACC'))
    tbl = _crew_tbl(monkeypatch, rows)
    monkeypatch.setattr(
        fo, '_links_load',
        lambda tok: fo.extract_duty_links(DUTY_LINKS) if links else [])
    return tbl


def test_crewlist_shared_hit_serves_without_lh_call(monkeypatch):
    """DER Owner-Fall: Kollege A hat LH400 schon geholt, Kollege B tippt auf
    den Crew-Button → Antwort aus dem Backend, KEIN LH-Call."""
    import time as _t
    _shared_setup(monkeypatch, [
        {'token': 'AT-KOLLEGE', 'flight': 'LH400', 'flight_date': '2026-07-24',
         'crew': [{'name': 'MUSTERMANN, MAX', 'pk': '095599C'},
                  {'name': 'ZWEITE, ZOE', 'pk': '111'}],
         'cached_at': _t.time() - 60}])
    # Vergangenes Flugdatum ⇒ PAST-TTL, die Zeile ist frisch genug.
    monkeypatch.setattr(fo, 'crew_list', lambda *a, **k: (_ for _ in ()).throw(
        AssertionError('geteilter Cache muss den LH-Call sparen')))
    monkeypatch.setattr(fo, '_match_aerox_profiles', lambda members, **_kw: {
        '095599C': {'token': 'AT-MAX', 'name': 'Max Mustermann',
                    'airline': 'Lufthansa', 'homebase': 'FRA',
                    'position': 'CP', 'avatar_url': None}})
    import app as backend
    r = backend.app.test_client().post('/api/lh/flightops/crewlist/AT-ICH',
                                       headers={'Authorization': 'Bearer AT-ICH'},
                                       json={'flight': 'LH400',
                                             'date': '2026-07-24'})
    d = r.get_json()
    assert r.status_code == 200 and d['ok'] is True
    assert d['cached'] is True and d['shared'] is True
    assert [m['name'] for m in d['crew']] == ['MUSTERMANN, MAX', 'ZWEITE, ZOE']
    # Anreicherung wird PRO VIEWER neu gerechnet, nicht mitgeliefert.
    assert d['crew'][0]['aerox']['token'] == 'AT-MAX'
    assert 'aerox' not in d['crew'][1]


def test_crewlist_shared_hit_reenriches_and_drops_cached_aerox(monkeypatch):
    """Die gecachte Zeile trägt eine ALTE aerox-Verknüpfung. Ausgeliefert wird
    ausschliesslich das frisch gematchte Ergebnis (kein Fremd-Snapshot)."""
    import time as _t
    _shared_setup(monkeypatch, [
        {'token': 'AT-KOLLEGE', 'flight': 'LH400', 'flight_date': '2026-07-24',
         'crew': [{'name': 'MUSTERMANN, MAX', 'pk': '095599C',
                   'aerox': {'token': 'AT-VERALTET', 'name': 'Alt'}}],
         'cached_at': _t.time() - 60}])
    monkeypatch.setattr(fo, 'crew_list', lambda *a, **k: (_ for _ in ()).throw(
        AssertionError('kein LH-Call erwartet')))
    seen = []
    monkeypatch.setattr(fo, '_match_aerox_profiles',
                        lambda members, **_kw: seen.append(members) or {})
    import app as backend
    d = backend.app.test_client().post(
        '/api/lh/flightops/crewlist/AT-ICH',
        headers={'Authorization': 'Bearer AT-ICH'},
        json={'flight': 'LH400', 'date': '2026-07-24'}).get_json()
    assert 'aerox' not in d['crew'][0]
    # Der Re-Match sieht die ROHE Liste, nicht die fremde Anreicherung.
    assert seen and 'aerox' not in seen[0][0]


def test_crewlist_shared_hit_needs_entitlement(monkeypatch):
    """SICHERHEIT: ohne eigene Cache-Zeile UND ohne eigenen crewlist-Link gibt
    es keinen Shortcut — sonst könnte jeder mit {flight,date} die Klarnamen
    einer fremden Crew abrufen. Der Request läuft den normalen Weg (hier:
    kein accessCode auflösbar ⇒ 404)."""
    import time as _t
    _shared_setup(monkeypatch, [
        {'token': 'AT-KOLLEGE', 'flight': 'LH400', 'flight_date': '2026-07-24',
         'crew': [{'name': 'GEHEIM, GERD'}], 'cached_at': _t.time() - 60}],
        links=False)
    monkeypatch.setattr(fo, 'duty_events',
                        lambda tok, fd, td, interactive=False: None)
    import app as backend
    r = backend.app.test_client().post('/api/lh/flightops/crewlist/AT-FREMD',
                                       headers={'Authorization': 'Bearer AT-FREMD'},
                                       json={'flight': 'LH400',
                                             'date': '2026-07-24'})
    assert r.status_code == 404 and r.get_json()['error'] == 'no_access_code'


def test_crewlist_stale_shared_entry_is_not_served(monkeypatch):
    """Flugtag + 2 h alte Zeile ⇒ über der 45-min-TTL ⇒ es wird live geholt."""
    import datetime as _d
    import time as _t
    today = _d.date.today().isoformat()
    _shared_setup(monkeypatch, [
        {'token': 'AT-KOLLEGE', 'flight': 'LH400', 'flight_date': today,
         'crew': [{'name': 'VERALTET, VERA'}], 'cached_at': _t.time() - 7200}])
    calls = []
    monkeypatch.setattr(fo, '_resolve_link_params', lambda *a, **k: {
        'accessCode': 'S', 'departureAirport': 'FRA', 'arrivalAirport': 'JFK'})
    monkeypatch.setattr(fo, 'crew_list',
                        lambda *a, **k: calls.append(a) or {'crewMembers': []})
    monkeypatch.setattr(fo, 'parse_crew_list', lambda resp: [{'name': 'FRISCH, FRIDA'}])
    monkeypatch.setattr(fo, '_match_aerox_profiles', lambda members, **_kw: {})
    import app as backend
    d = backend.app.test_client().post(
        '/api/lh/flightops/crewlist/AT-ICH',
        headers={'Authorization': 'Bearer AT-ICH'},
        json={'flight': 'LH400', 'date': today}).get_json()
    assert calls, 'stale ⇒ LH-Call'
    assert d['crew'][0]['name'] == 'FRISCH, FRIDA' and 'shared' not in d


def test_crewlist_force_bypasses_shared_cache(monkeypatch):
    """Pull-to-Refresh: `force:true` holt trotz frischer Zeile live."""
    import time as _t
    _shared_setup(monkeypatch, [
        {'token': 'AT-KOLLEGE', 'flight': 'LH400', 'flight_date': '2026-07-24',
         'crew': [{'name': 'CACHE, CARLA'}], 'cached_at': _t.time() - 60}])
    monkeypatch.setattr(fo, 'crew_list', lambda *a, **k: {'crewMembers': []})
    monkeypatch.setattr(fo, 'parse_crew_list', lambda resp: [{'name': 'LIVE, LARS'}])
    monkeypatch.setattr(fo, '_match_aerox_profiles', lambda members, **_kw: {})
    import app as backend
    d = backend.app.test_client().post(
        '/api/lh/flightops/crewlist/AT-ICH',
        headers={'Authorization': 'Bearer AT-ICH'},
        json={'flight': 'LH400', 'date': '2026-07-24', 'force': True}).get_json()
    assert d['crew'][0]['name'] == 'LIVE, LARS'


def test_crewlist_solo_paths_unchanged_without_table(monkeypatch):
    """REGRESSION: ohne Cache-Tabelle (SB weg / Migration fehlt) verhält sich
    der Endpoint exakt wie vorher — toter Grant + kein Cache = 401."""
    import app as backend
    monkeypatch.setattr(backend, 'SB_AVAILABLE', False)
    monkeypatch.setattr(fo, '_valid_access', lambda tok: None)
    monkeypatch.setattr(fo, '_crew_cache_get', lambda tok, f, d: None)
    r = backend.app.test_client().post('/api/lh/flightops/crewlist/testtok-fo',
                                       json={'flight': 'LH582',
                                             'date': '2026-07-26'})
    assert r.status_code == 401 and r.get_json()['error'] == 'not_connected'


# ── Prefetch ────────────────────────────────────────────────────────────────

def test_crew_prefetch_legs_uses_duty_links_only():
    """Die Legs kommen aus den crewlist-_links der Duty-Events-Response — die
    tragen accessCode/dep/arr fertig mit ⇒ KEIN Extra-Duty-Events-Call."""
    legs = fo._crew_prefetch_legs(DUTY_LINKS, today='2026-07-24')
    assert legs == [{'flight': 'LH400', 'date': '2026-07-24', 'dep': 'FRA',
                     'arr': 'JFK', 'access': 'SECRET42'}]
    # Ausserhalb des Horizonts (Flug liegt in der Vergangenheit) ⇒ nichts.
    assert fo._crew_prefetch_legs(DUTY_LINKS, today='2026-07-25') == []
    # HORIZONT 3 KALENDERTAGE (Korrektur 2026-07-29, s. Banner): heute + 2
    # liegt drin …
    assert fo._crew_prefetch_legs(DUTY_LINKS, today='2026-07-22') != []
    # … heute + 3 nicht mehr (der Monats-Horizont riss das Tagesbudget).
    assert fo._crew_prefetch_legs(DUTY_LINKS, today='2026-07-21') == []
    assert fo._crew_prefetch_legs(DUTY_LINKS, today='2026-07-01') == []


def test_crew_prefetch_legs_dedupes_and_caps():
    """Doppelte Links zählen einmal, die Kappe hält die Kosten pro Import fest."""
    def _leg(n, day):
        return {'eventType': 'FLIGHT', '_links': {'crewList': {'href':
                _L + f'/COMMON_CREWLIST?flightDesignator=LH{n}&flightDate='
                f'2026-07-{day}Z&departureAirport=FRA&arrivalAirport=MUC'
                '&accessCode=A' + str(n)}}}
    evs = []
    for i in range(fo._CREW_PREFETCH_MAX_LEGS + 4):
        evs.append(_leg(500 + i, '%02d' % (24 + (i % 3))))
        evs.append(_leg(500 + i, '%02d' % (24 + (i % 3))))     # Duplikat
    legs = fo._crew_prefetch_legs({'rosterDays': [{'day': 'x', 'events': evs}]},
                                  today='2026-07-24')
    assert len(legs) == fo._CREW_PREFETCH_MAX_LEGS
    assert len({(l['flight'], l['date']) for l in legs}) == len(legs)
    assert legs == sorted(legs, key=lambda x: (x['date'], x['flight']))


def _prefetch_env(monkeypatch, rows=None, hour=0, day=0):
    tbl = _crew_tbl(monkeypatch, rows)
    monkeypatch.setattr(fo, '_rot_hour_used', lambda: hour)
    monkeypatch.setattr(fo, '_lhfo_day_used', lambda: day)
    monkeypatch.setattr(fo, '_CREW_PREFETCH_SLEEP_S', 0.0)
    fo._refresh_all_state['drain'] = False
    return tbl


def test_crew_prefetch_warms_upcoming_legs(monkeypatch):
    """Nach dem Verbinden liegen die Crew-Listen der nächsten Legs im Backend —
    der Crew-Button ist warm, bevor der User ihn drückt."""
    tbl = _prefetch_env(monkeypatch)
    seen = []
    monkeypatch.setattr(fo, 'crew_list',
                        lambda tok, f, d, dep, arr, ac, interactive=False:
                        seen.append((f, d, ac, interactive)) or {'x': 1})
    monkeypatch.setattr(fo, 'parse_crew_list', lambda resp: [{'name': 'CREW'}])
    legs = [{'flight': 'LH400', 'date': '2026-07-29', 'dep': 'FRA',
             'arr': 'JFK', 'access': 'A1'},
            {'flight': 'LH401', 'date': '2026-07-31', 'dep': 'JFK',
             'arr': 'FRA', 'access': 'A2'}]
    assert fo._crew_prefetch_run('AT-U', legs)['prefetched'] == 2
    # HINTERGRUND-Priorität — der Prefetch darf interaktive Taps nie verdrängen.
    assert [s[3] for s in seen] == [False, False]
    assert sorted(r['flight'] for r in tbl.rows) == ['LH400', 'LH401']
    assert fo._crew_cache_get('AT-U', 'LH401', '2026-07-31')['crew'][0]['name'] == 'CREW'


def test_crew_prefetch_skips_leg_already_fresh_in_shared_cache(monkeypatch):
    """Der Kollegen-Effekt: was ein anderer User heute schon geholt hat, wird
    NICHT nochmal von LH geholt — genau das amortisiert die Quota über die
    ganze Crew."""
    import time as _t
    _prefetch_env(monkeypatch, [
        {'token': 'AT-KOLLEGE', 'flight': 'LH400', 'flight_date': '2026-07-29',
         'crew': [{'name': 'SCHON DA'}], 'cached_at': _t.time() - 3600}])
    calls = []
    monkeypatch.setattr(fo, 'crew_list', lambda *a, **k: calls.append(a) or {'x': 1})
    monkeypatch.setattr(fo, 'parse_crew_list', lambda resp: [{'name': 'NEU'}])
    legs = [{'flight': 'LH400', 'date': '2026-07-29', 'dep': 'FRA',
             'arr': 'JFK', 'access': 'A1'},
            {'flight': 'LH401', 'date': '2026-07-29', 'dep': 'JFK',
             'arr': 'FRA', 'access': 'A2'}]
    r = fo._crew_prefetch_run('AT-U', legs)
    assert r == {'prefetched': 1, 'skipped': 1, 'failed': 0}
    assert [c[1] for c in calls] == ['LH401']


def test_crew_prefetch_stops_at_own_budget_ceiling(monkeypatch):
    """Eigener, TIEFERER Deckel als das reguläre Hintergrund-Gate: der Prefetch
    hört als ERSTES auf, damit der Roster nie an einer Zugabe verhungert."""
    _prefetch_env(monkeypatch, hour=fo._CREW_PREFETCH_HOUR_CEILING)
    monkeypatch.setattr(fo, 'crew_list', lambda *a, **k: (_ for _ in ()).throw(
        AssertionError('Prefetch darf über dem Deckel nicht mehr rufen')))
    legs = [{'flight': 'LH400', 'date': '2026-07-29', 'dep': 'FRA',
             'arr': 'JFK', 'access': 'A1'}]
    assert fo._crew_prefetch_run('AT-U', legs) == {'prefetched': 0, 'skipped': 0,
                                                   'failed': 0}
    # Tagesdeckel wirkt genauso.
    _prefetch_env(monkeypatch, day=fo._CREW_PREFETCH_DAY_CEILING)
    assert fo._crew_prefetch_run('AT-U', legs)['prefetched'] == 0


def test_crew_prefetch_survives_api_gate_refusal(monkeypatch):
    """_api_get verweigert den Hintergrund-Call (Stundenbudget voll) → crew_list
    gibt None. Der Prefetch zählt das als verpasste Zugabe und schreibt NICHTS
    Falsches in den Cache."""
    tbl = _prefetch_env(monkeypatch)
    monkeypatch.setattr(fo, '_valid_access', lambda tok: 'ACC')
    monkeypatch.setattr(fo, '_rot_hour_used', lambda: 0)   # Prefetch-Deckel frei
    calls = []

    def _gate(tok, path, params=None, interactive=False):
        calls.append((path, interactive))
        return None                       # exakt das Verhalten des Budget-Gates
    monkeypatch.setattr(fo, '_api_get', _gate)
    legs = [{'flight': 'LH400', 'date': '2026-07-29', 'dep': 'FRA',
             'arr': 'JFK', 'access': 'A1'}]
    assert fo._crew_prefetch_run('AT-U', legs) == {'prefetched': 0, 'skipped': 0,
                                                   'failed': 1}
    assert calls == [('/COMMON_CREWLIST', False)]
    assert tbl.rows == []


def test_crew_prefetch_stops_on_deploy_drain(monkeypatch):
    """Deploy-Drain (Grant-Burn-Lehre): kein neuer LH-Call, sobald gedrained
    wird."""
    _prefetch_env(monkeypatch)
    monkeypatch.setattr(fo, 'crew_list', lambda *a, **k: (_ for _ in ()).throw(
        AssertionError('drain ⇒ kein Call')))
    fo._refresh_all_state['drain'] = True
    try:
        assert fo._crew_prefetch_run('AT-U', [{'flight': 'LH400',
                                               'date': '2026-07-29',
                                               'access': 'A1'}])['prefetched'] == 0
    finally:
        fo._refresh_all_state['drain'] = False


def test_crew_prefetch_kick_runs_once_per_cooldown(monkeypatch):
    """Der Kick läuft im Hintergrund (blockiert den Import nie) und ist pro
    Token durch den Cooldown gedeckelt — refresh-all alle 2 h startet ihn
    nicht bei jedem Lauf neu."""
    import time as _t
    fo._crew_prefetch_seen.clear()
    fo._crew_prefetch_active[0] = 0
    runs = []
    monkeypatch.setattr(fo, '_crew_prefetch_run',
                        lambda tok, legs, **k: runs.append((tok, legs)) or {})
    monkeypatch.setattr(fo, '_crew_prefetch_legs',
                        lambda resp, today=None: [{'flight': 'LH400',
                                                   'date': '2026-07-29',
                                                   'access': 'A1'}])
    assert fo._crew_prefetch_kick('AT-U', DUTY_LINKS) is True
    for _ in range(200):                  # Thread abwarten (max 2 s)
        if runs:
            break
        _t.sleep(0.01)
    assert runs and runs[0][0] == 'AT-U'
    # Zweiter Import binnen der Cooldown-Zeit ⇒ kein zweiter Lauf.
    assert fo._crew_prefetch_kick('AT-U', DUTY_LINKS) is False
    assert len(runs) == 1
    fo._crew_prefetch_seen.clear()


def test_import_kicks_prefetch_after_success(monkeypatch):
    """VERDRAHTUNG: der erfolgreiche Roster-Import wärmt die Crew-Listen —
    „alles laden, wenn verbunden". Der Prefetch bekommt die ECHTE
    Duty-Events-Response (die crewlist-_links stecken schon darin)."""
    _pass_auth_gate(monkeypatch)
    monkeypatch.setattr(fo, '_KEY', 'k'); monkeypatch.setattr(fo, '_SECRET', 's')
    monkeypatch.setattr(fo, '_access_state', lambda tok: ('ok', 'ACC'))
    monkeypatch.setattr(fo, 'duty_events',
                        lambda tok, fd, td, interactive=False: DUTY_LINKS)
    monkeypatch.setattr(fo, 'pickup_rotation_ids', lambda resp, **kw: [])
    monkeypatch.setattr(fo, 'duty_events_to_ics', lambda resp, pickups=None: 'ICS')
    monkeypatch.setattr(fo, 'apply_ical_pickup_fallback',
                        lambda tok, ics, url=None: ics)
    import app as backend
    monkeypatch.setattr(backend, 'import_calendar_feed',
                        lambda tok: backend.jsonify({'ok': True, 'events_count': 1}))
    kicked = []
    monkeypatch.setattr(fo, '_crew_prefetch_kick',
                        lambda tok, resp: kicked.append((tok, resp)) or True)
    r = backend.app.test_client().post('/api/lh/flightops/import/AT-U',
                                       headers={'Authorization': 'Bearer AT-U'},
                                       json={})
    assert r.status_code == 200 and r.get_json()['source'] == 'flightops'
    assert kicked and kicked[0][0] == 'AT-U'
    assert fo._crew_prefetch_legs(kicked[0][1], today='2026-07-24')[0]['access'] \
        == 'SECRET42'


# ── Verbindungs-ALTER (Owner-Frage: „wie lange schon verbunden?") ────────────
def test_status_meldet_verbindungsalter(monkeypatch):
    """`connected_at` → Alter in Tagen. Vorher war nur der TOD eines Grants
    (`relogin_at`) datiert, seine Geburt nicht."""
    import time as _t
    monkeypatch.setattr(fo, '_tokens_load', lambda tok, fresh=False: {
        'access': 'A', 'refresh': 'R', 'scope': 's',
        'connected_at': _t.time() - 3 * 86400,
        'first_connected_at': _t.time() - 10 * 86400, 'reconnects': 2})
    import app as backend
    d = backend.app.test_client().get('/api/lh/flightops/status/AT-U').get_json()
    assert d['connected'] is True
    assert 2.9 <= d['connected_days'] <= 3.1
    assert d['reconnects'] == 2
    assert d['first_connected_at'] < d['connected_at']


def test_status_erfindet_kein_alter_fuer_altgrants(monkeypatch):
    """Grants von VOR dem Deploy haben keinen Zeitstempel — dann fehlt die
    Zeile, statt ein geschätztes Alter zu erfinden (Design-Regel)."""
    monkeypatch.setattr(fo, '_tokens_load', lambda tok, fresh=False: {
        'access': 'A', 'refresh': 'R', 'scope': 's'})
    import app as backend
    d = backend.app.test_client().get('/api/lh/flightops/status/AT-U').get_json()
    assert d['connected'] is True
    assert 'connected_days' not in d and 'connected_at' not in d


def test_exchange_stempelt_verbindung_und_zaehlt_reconnects(monkeypatch):
    """Beim Neu-Verbinden: connected_at frisch, first_connected_at bleibt,
    reconnects +1 — und der tote needs_relogin-Zustand ist weg, weil
    _tokens_save den ganzen Blob ersetzt."""
    import time as _t
    alt = _t.time() - 30 * 86400
    monkeypatch.setattr(fo, 'flightops_configured', lambda: True)
    monkeypatch.setattr(fo, '_flow_take', lambda s: {
        'user_token': 'AT-U', 'verifier': 'V'})
    monkeypatch.setattr(fo, '_exchange_code',
                        lambda c, v: {'access': 'A', 'refresh': 'R2',
                                      'scope': 's'})
    monkeypatch.setattr(fo, '_tokens_mirror_raw', lambda tok: {
        'refresh': 'R1', 'needs_relogin': True, 'relogin_at': _t.time(),
        'first_connected_at': alt, 'reconnects': 1})
    saved = {}
    monkeypatch.setattr(fo, '_tokens_save',
                        lambda tok, t: (saved.update(t), True)[1])
    import app as backend
    d = backend.app.test_client().post(
        '/api/lh/flightops/oauth/exchange',
        json={'code': 'C', 'state': 'S'}).get_json()
    assert d['ok'] is True and d['connected'] is True
    assert saved['first_connected_at'] == alt      # überlebt das Relogin
    assert saved['reconnects'] == 2                # der Burn ist gezählt
    assert saved['connected_at'] > alt
    assert 'needs_relogin' not in saved and 'relogin_at' not in saved


def test_erstverbindung_zaehlt_keinen_reconnect(monkeypatch):
    """Wer sich ZUM ERSTEN MAL verbindet, hat 0 Reconnects — sonst sähe jeder
    Neu-User wie ein Grant-Burn-Opfer aus."""
    monkeypatch.setattr(fo, 'flightops_configured', lambda: True)
    monkeypatch.setattr(fo, '_flow_take', lambda s: {
        'user_token': 'AT-N', 'verifier': 'V'})
    monkeypatch.setattr(fo, '_exchange_code',
                        lambda c, v: {'access': 'A', 'refresh': 'R', 'scope': 's'})
    monkeypatch.setattr(fo, '_tokens_mirror_raw', lambda tok: {})
    saved = {}
    monkeypatch.setattr(fo, '_tokens_save',
                        lambda tok, t: (saved.update(t), True)[1])
    import app as backend
    backend.app.test_client().post('/api/lh/flightops/oauth/exchange',
                                   json={'code': 'C', 'state': 'S'})
    assert saved['reconnects'] == 0
    assert saved['connected_at'] == saved['first_connected_at']


# ── Identitäts-Verknüpfung: abgekürzte Namen verknüpfen NIE ─────────────────
# Anlass (Owner 2026-07-30): Die Kabinen-Liste führte „Marco C." — in Wahrheit
# Marco Comajuncosas Grether (pk 450460I), der GAR NICHT auf AeroX ist. Der
# frühere Fuzzy-Match (Vorname + Nachname-Initial, „genau ein Kandidat")
# verknüpfte ihn mit Marco CHRIST und öffnete beim Tap dessen echtes Profil.
# Die Eindeutigkeit war auf der FALSCHEN Seite geprüft: je weniger AeroX-User,
# desto „eindeutiger" der falsche Treffer.
# Diese Tests nageln fest, dass nur BEWEISE verknüpfen (pk oder voller Name).

class _R:
    def __init__(self, data):
        self.data = data


class _ProfTbl:
    """Minimal-Supabase für user_profiles — nur was _match_aerox_profiles nutzt.
    Merkt sich alle ilike-Muster, damit ein wiederkehrender Fuzzy-Query
    (Muster „Vorname %") im Test AUFFÄLLT statt still zurückzukommen."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.ilike_calls = []

    def table(self, name):
        assert name == 'user_profiles', name
        self._mode = self._pks = self._pat = None
        return self

    def select(self, *_a, **_k):
        return self

    def in_(self, col, vals):
        assert col == 'metadata->>lh_pk_number', col
        self._mode, self._pks = 'pk', [str(v) for v in vals]
        return self

    def ilike(self, col, pat):
        assert col == 'name', col
        self._mode, self._pat = 'name', pat
        self.ilike_calls.append(pat)
        return self

    def limit(self, _n):
        return self

    def execute(self):
        if self._mode == 'pk':
            return _R([r for r in self.rows
                       if str((r.get('metadata') or {}).get('lh_pk_number')
                              or '') in self._pks])
        if self._mode == 'name':
            pat = str(self._pat or '')
            if pat.endswith(' %'):          # Fuzzy-Präfix — darf nie vorkommen
                pre = pat[:-2].lower()
                return _R([r for r in self.rows
                           if str(r.get('name') or '').lower()
                           .startswith(pre + ' ')])
            return _R([r for r in self.rows
                       if str(r.get('name') or '').strip().lower()
                       == pat.strip().lower()])
        return _R([])


def _profiles(monkeypatch, rows):
    import app as backend
    tbl = _ProfTbl(rows)
    monkeypatch.setattr(backend, 'SB_AVAILABLE', True)
    monkeypatch.setattr(backend, 'sb', tbl, raising=False)
    return tbl


_MARCO_CHRIST = {'token': 'AT-CHRIST', 'name': 'Marco Christ',
                 'airline': 'Lufthansa', 'homebase': 'FRA', 'position': 'PU',
                 'metadata': {'lh_pk_number': '709758B'}}


def test_abbrev_name_never_links_the_wrong_person(monkeypatch):
    """DER VORFALL: „Marco C." darf Marco Christ NICHT bekommen."""
    tbl = _profiles(monkeypatch, [_MARCO_CHRIST])
    out = fo._match_aerox_profiles([{'name': 'Marco C.', 'pk': '',
                                     'position': 'P2'}])
    assert out == {}
    # Und der Fuzzy-Query selbst ist weg (kein „Marco %"-Präfix-Lookup mehr).
    assert not [p for p in tbl.ilike_calls if p.endswith(' %')]


def test_abbrev_name_never_links_even_when_the_real_person_is_on_aerox(monkeypatch):
    """Doppelnachname: LH kürzt auf den ERSTEN Nachnamen („Comajuncosas
    Grether" ⇒ „C."). Auch wenn der ECHTE Mensch auf AeroX wäre, wird auf
    einen Buchstaben nicht verknüpft — sonst hinge die Identität an der
    Frage, wer sonst noch zufällig passt."""
    real = dict(_MARCO_CHRIST, token='AT-REAL',
                name='Marco Comajuncosas Grether', metadata={})
    _profiles(monkeypatch, [real])
    assert fo._match_aerox_profiles(
        [{'name': 'Marco C.', 'pk': '', 'position': 'P2'}]) == {}


def test_pk_counterevidence_never_links(monkeypatch):
    """Die Crew-Liste nennt pk 450460I, das einzige Kandidaten-Profil trägt
    709758B — eine ABWEICHENDE pk ist ein aktiver Gegenbeweis."""
    _profiles(monkeypatch, [_MARCO_CHRIST])
    assert fo._match_aerox_profiles(
        [{'name': 'Marco C.', 'pk': '450460I', 'position': 'P2'}]) == {}


def test_exact_pk_still_links_and_only_public_fields(monkeypatch):
    """Der Beweis-Pfad bleibt unangetastet — wer mit LH verbunden ist, wird
    exakt getroffen. Und es verlässt NUR die Public-Shape das Backend."""
    _profiles(monkeypatch, [_MARCO_CHRIST])
    out = fo._match_aerox_profiles([{'name': 'Marco Christ',
                                     'pk': '709758B'}])
    assert list(out) == ['709758B']
    p = out['709758B']
    assert p['token'] == 'AT-CHRIST' and p['homebase'] == 'FRA'
    assert set(p) == {'token', 'name', 'airline', 'homebase', 'position',
                      'avatar_url'}
    assert 'metadata' not in p


def test_exact_full_name_still_links(monkeypatch):
    """Voller, eindeutiger Name bleibt ein zulässiger Beweis (kein Buchstabe)."""
    _profiles(monkeypatch, [_MARCO_CHRIST])
    out = fo._match_aerox_profiles([{'name': 'Marco Christ', 'pk': ''}])
    assert out.get('Marco Christ', {}).get('token') == 'AT-CHRIST'


def test_family_accounts_never_linked(monkeypatch):
    """Family-Accounts sind keine Crew und erscheinen nie in einer Crew-Liste."""
    fam = dict(_MARCO_CHRIST,
               metadata={'lh_pk_number': '709758B', 'account_type': 'family'})
    _profiles(monkeypatch, [fam])
    assert fo._match_aerox_profiles([{'name': 'Marco Christ',
                                      'pk': '709758B'}]) == {}


# ── Dritter Weg: ROSTER-BEWEIS (30.07.) ────────────────────────────────────
# Owner: „mit dem neusten update sehe ich keinen mehr auf AeroX" — richtig, denn
# 87 % der Crew-Eintraege sind abgekuerzt OHNE pk und aus LHs Daten nicht
# aufloesbar. Statt wieder zu raten kommt der Beweis aus EIGENER Quelle: wer
# diesen Flug an diesem Tag im eigenen Roster stehen hat, sitzt nachweislich
# drauf. Genau daran waere die Verwechslung gescheitert — Marco Christ stand
# nie auf LH762.

class _RosterProfTbl:
    """user_profiles + roster_snapshots, nur was der Matcher benutzt."""

    def __init__(self, profiles, rostered_tokens):
        self.profiles = list(profiles)
        self.rostered = list(rostered_tokens)
        self.probes = []

    def table(self, name):
        self._t = name
        self._mode = self._vals = self._pat = None
        return self

    def select(self, *_a, **_k):
        return self

    def contains(self, col, probe):
        assert col == 'payload', col
        self._mode, = ('contains',)
        self.probes.append(probe)
        return self

    def in_(self, col, vals):
        self._mode = ('in', col)
        self._vals = [str(v) for v in vals]
        return self

    def ilike(self, col, pat):
        assert col == 'name', col
        self._mode, self._pat = 'ilike', pat
        return self

    def limit(self, _n):
        return self

    def execute(self):
        if self._t == 'roster_snapshots':
            return _R([{'token': t} for t in self.rostered])
        m = self._mode
        if isinstance(m, tuple) and m[0] == 'in':
            if m[1] == 'token':
                return _R([p for p in self.profiles
                           if p.get('token') in self._vals])
            return _R([p for p in self.profiles
                       if str((p.get('metadata') or {}).get('lh_pk_number')
                              or '') in self._vals])
        if m == 'ilike':
            pat = str(self._pat or '').strip().lower()
            return _R([p for p in self.profiles
                       if str(p.get('name') or '').strip().lower() == pat])
        return _R([])


def _roster_setup(monkeypatch, profiles, rostered):
    import app as backend
    tbl = _RosterProfTbl(profiles, rostered)
    monkeypatch.setattr(backend, 'SB_AVAILABLE', True)
    monkeypatch.setattr(backend, 'sb', tbl, raising=False)
    return tbl


_ANITA = {'token': 'AT-ANITA', 'name': 'Anita Davis', 'airline': 'Lufthansa',
          'homebase': 'FRA', 'position': 'FA', 'metadata': {}}
_MARCO_REAL = {'token': 'AT-REAL', 'name': 'Marco Comajuncosas Grether',
               'airline': 'Lufthansa', 'homebase': 'FRA', 'position': 'P2',
               'metadata': {}}


def test_roster_proof_links_abbreviated_name(monkeypatch):
    """„Anita D." steht als Anita Davis selbst auf LH762 → belegt, verknuepft."""
    tbl = _roster_setup(monkeypatch, [_ANITA], ['AT-ANITA'])
    out = fo._match_aerox_profiles([{'name': 'Anita D.', 'pk': ''}],
                                   flight='LH762', date='2026-07-30')
    assert out.get('Anita D.', {}).get('token') == 'AT-ANITA'
    # Der Beweis wird wirklich am konkreten Leg gezogen, nicht global.
    assert tbl.probes and tbl.probes[0]['tage'][0]['datum'] == '2026-07-30'
    assert (tbl.probes[0]['tage'][0]['ical_sectors'][0]['flight'] == 'LH762')


def test_roster_proof_handles_double_surname(monkeypatch):
    """LH kuerzt „Comajuncosas Grether" auf den ERSTEN Nachnamen („C.").
    Das Initial muss deshalb gegen JEDES Nachnamen-Token passen."""
    _roster_setup(monkeypatch, [_MARCO_REAL], ['AT-REAL'])
    out = fo._match_aerox_profiles([{'name': 'Marco C.', 'pk': ''}],
                                   flight='LH762', date='2026-07-30')
    assert out.get('Marco C.', {}).get('token') == 'AT-REAL'


def test_incident_stays_fixed_marco_christ_is_not_on_the_flight(monkeypatch):
    """DIE REGRESSION: Marco Christ ist auf AeroX und heisst passend — steht
    aber nicht im Roster dieses Legs. Kein Beweis, kein Match."""
    _roster_setup(monkeypatch, [_MARCO_CHRIST], [])
    assert fo._match_aerox_profiles([{'name': 'Marco C.', 'pk': ''}],
                                    flight='LH762',
                                    date='2026-07-30') == {}


def test_roster_proof_requires_uniqueness(monkeypatch):
    """Zwei belegte Kandidaten auf dasselbe Kuerzel → lieber niemand."""
    zwei = dict(_MARCO_REAL, token='AT-ZWEI', name='Marco Conrad')
    _roster_setup(monkeypatch, [_MARCO_REAL, zwei], ['AT-REAL', 'AT-ZWEI'])
    assert fo._match_aerox_profiles([{'name': 'Marco C.', 'pk': ''}],
                                    flight='LH762',
                                    date='2026-07-30') == {}


def test_no_roster_proof_without_flight_and_date(monkeypatch):
    """Ohne Leg-Bezug gibt es nichts zu beweisen — dann lieber kein Match."""
    _roster_setup(monkeypatch, [_ANITA], ['AT-ANITA'])
    assert fo._match_aerox_profiles([{'name': 'Anita D.', 'pk': ''}]) == {}


def test_same_profile_is_never_linked_twice(monkeypatch):
    """Steht dieselbe Person schon per pk in der Liste, darf ein abgekuerzter
    Eintrag sie nicht ein zweites Mal bekommen."""
    anita_pk = dict(_ANITA, metadata={'lh_pk_number': '111222A'})
    _roster_setup(monkeypatch, [anita_pk], ['AT-ANITA'])
    out = fo._match_aerox_profiles(
        [{'name': 'Anita Davis', 'pk': '111222A'}, {'name': 'Anita D.', 'pk': ''}],
        flight='LH762', date='2026-07-30')
    assert out.get('111222A', {}).get('token') == 'AT-ANITA'
    assert 'Anita D.' not in out


# ── SIM-Crewliste (Mark Elser, Forum 2026-07-30) ────────────────────────────
# Ein Simulator-Termin hat KEINE Flugnummer: die Referenz wird allein über
# `forDate` adressiert. Genau deshalb konnte die gemeinsame Referenz-Suche sie
# nie finden — sie verlangt einen passenden `flightDesignator`.
#
# Fixture = die ECHTE Shape, live geholt am 2026-07-30 an einem SIM-Termin
# (Namen/Nummern ersetzt): die Antwort ist eine LISTE von Sessions, nicht ein
# Objekt wie bei COMMON_CREWLIST.

_SIM_RESP = [{
    'entries': [
        {'crewFunction': 'CPT', 'simulatorFunction': 'PF',
         'simulatorActivity': 'LPC', 'crewName': 'MUSTERMANN',
         'staffIdentifier': '111111A'},
        # ECHTE EIGENHEIT (live 2026-07-30): LH fuellt `staffIdentifier` NUR
        # beim Anfragenden. Bei den Kollegen ist das Feld da, aber LEER — die
        # AeroX-Verknuepfung (die bewusst NUR ueber die PK laeuft) kann fuer
        # sie also nie greifen. Sie muessen trotzdem in der Liste stehen.
        {'crewFunction': 'FO', 'simulatorFunction': 'PM',
         'simulatorActivity': 'CH', 'crewName': 'BEISPIEL, A',
         'staffIdentifier': ''},
        {'crewFunction': 'TRI', 'simulatorFunction': 'TRI',
         'simulatorActivity': 'BS', 'crewName': 'LEHRER, B',
         'staffIdentifier': ''},
    ],
    'errorMessage': None, 'errorReply': False, 'forDate': '2026-07-07Z',
    # Auch das echte Format: shift ist eine ZIFFER, simulator eine Nummer,
    # ftNumber traegt Muster+Session+Station.
    'ftNumber': 'A320 N FT66 FRA', 'shift': '3', 'simulator': '328',
}]


def test_sim_crewliste_wird_wie_die_flugcrew_normalisiert():
    crew = fo.parse_simulator_crewlist(_SIM_RESP)
    assert [c['pk'] for c in crew] == ['111111A', None, None]
    # Gleiche Schluessel wie die Flug-Crewliste, damit die App-Flaeche bleibt.
    assert set(crew[0]) == {'position', 'name', 'pk', 'duty'}
    # crewName ist EIN Feld im Format "NACHNAME, V" — Reihenfolge bleibt wie
    # von LH geliefert (Umsortieren waere geraten), nur die Schreibweise wird
    # lesbar gemacht.
    assert crew[1]['name'] == 'Beispiel, A'
    # Kollegen OHNE Personalnummer duerfen nicht rausfallen.
    assert len(crew) == 3


def test_sim_funktion_haengt_an_der_position_nur_wenn_sie_etwas_neues_sagt():
    crew = fo.parse_simulator_crewlist(_SIM_RESP)
    assert crew[0]['position'] == 'CPT · PF'      # PF ergaenzt CPT
    assert crew[2]['position'] == 'TRI'           # TRI == TRI, keine Dopplung


def test_sim_fehler_session_wird_uebersprungen():
    bad = [{'errorReply': True, 'errorMessage': 'nope', 'entries': [
        {'crewName': 'X', 'staffIdentifier': '999'}]}]
    assert fo.parse_simulator_crewlist(bad) == []
    assert fo.parse_simulator_crewlist(None) == []
    assert fo.parse_simulator_crewlist({'entries': []}) == []


def test_dieselbe_person_in_zwei_sessions_erscheint_einmal():
    """Dedupe muss AUCH ohne Personalnummer funktionieren (Kollegen haben
    keine) — sonst steht der Doppel-Slot zweimal in der Liste."""
    doppelt = _SIM_RESP + [dict(_SIM_RESP[0])]
    assert len(fo.parse_simulator_crewlist(doppelt)) == 3


def test_session_kopf_fuer_die_ueberschrift():
    info = fo.simulator_session_info(_SIM_RESP)
    assert info['simulator'] == '328' and info['shift'] == '3'
    assert info['ftNumber'] == 'A320 N FT66 FRA'
    assert fo.simulator_session_info([]) == {}


def test_sim_referenz_wird_ueber_das_datum_gefunden_nicht_ueber_die_flugnummer():
    """Der Kern des Bugs: `_links_find` verlangt einen flightDesignator und
    findet die SIM-Referenz deshalb NIE — `_links_find_sim` findet sie."""
    links = [
        {'service': 'simulatorcrewlist',
         'params': {'forDate': '2026-07-07Z', 'accessCode': 'AAA'}},
        {'service': 'crewlist',
         'params': {'flightDesignator': 'LH1558', 'flightDate': '2026-07-08Z',
                    'accessCode': 'BBB'}},
    ]
    assert fo._links_find_sim(links, '2026-07-07')['accessCode'] == 'AAA'
    assert fo._links_find_sim(links, '2026-07-08') is None      # SIM nur am 07.
    assert fo._links_find_sim(links, '') is None
    # Gegenprobe: die alte Suche findet die SIM-Referenz nicht.
    assert fo._links_find(links, 'simulatorcrewlist', '', '2026-07-07') is None


def test_interactive_landet_nicht_als_query_param_bei_lh():
    """Dieselbe Falle wie bei check_in_times: `interactive` muss ein eigener
    Keyword-Parameter sein, sonst geht es als Query-Param an LH raus."""
    import inspect
    sig = inspect.signature(fo.simulator_crewlist)
    assert 'interactive' in sig.parameters
    assert sig.parameters['interactive'].kind is not inspect.Parameter.VAR_KEYWORD
