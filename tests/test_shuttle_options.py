"""Shuttler-Verbindungen (2026-08-07): Tages-Flugliste einer Strecke.

Für Pendler, die mit dem Flieger zur Base kommen: `route_flights` in
lh_open_api (flightstatus/route, 12-h-Cache) + `/api/ax/shuttle-options/<token>`
in app.py. Fixture-Form entspricht der Live-Probe vom 06.08. (FRA-HAM:
LH-Mainline + VL City Airlines + Condor DE in EINER Antwort — deshalb kein
is_lh_group-Gate).

Regeln, die hier festgenagelt werden:
  1. Skalar-Härtung: EIN Flug kommt als Objekt statt Liste (LH-Known-Issue).
  2. `answered=False` (Gate/5xx) wird NIE gecacht und NIE zu „keine Flüge";
     ein echtes 404 (answered=True, leer) wird gecacht.
  3. Zeiten sind Offset-ISO (Instants!) — auch ohne ScheduledTimeUTC-Block
     ergänzt `_ensure_offset` den Stations-Offset (Zeitzonen-Fehlerklasse).
  4. `operated_by` nur bei echtem Wet-Lease, nie „LH von LH".

Rein offline: kein Netz, kein Key, kein Supabase (unter pytest ist
`_shared_sb` hart None).

Run:
    AEROTAX_ALLOW_BOOT_WITHOUT_KEY=1 pytest tests/test_shuttle_options.py -v
"""
from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("AEROTAX_ALLOW_BOOT_WITHOUT_KEY", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blueprints import lh_open_api as lh


def _flight(mkt_al, mkt_no, dep_local, dep_utc, arr_local, arr_utc,
            op_al=None, status=None, dep_ap='FRA', arr_ap='HAM'):
    f = {
        'MarketingCarrier': {'AirlineID': mkt_al, 'FlightNumber': mkt_no},
        'Departure': {'AirportCode': dep_ap,
                      'ScheduledTimeLocal': {'DateTime': dep_local}},
        'Arrival': {'AirportCode': arr_ap,
                    'ScheduledTimeLocal': {'DateTime': arr_local}},
    }
    if dep_utc:
        f['Departure']['ScheduledTimeUTC'] = {'DateTime': dep_utc}
    if arr_utc:
        f['Arrival']['ScheduledTimeUTC'] = {'DateTime': arr_utc}
    if op_al:
        f['OperatingCarrier'] = {'AirlineID': op_al, 'FlightNumber': mkt_no}
    if status:
        f['FlightStatus'] = {'Code': status}
    return f


def _resp(flights):
    return {'FlightStatusResource': {'Flights': {'Flight': flights}}}


# ── Parser ───────────────────────────────────────────────────────────────────

def test_parse_liste_mit_utc_und_wetlease():
    data = _resp([
        _flight('LH', '0002', '2026-08-07T07:00', '2026-08-07T05:00Z',
                '2026-08-07T08:05', '2026-08-07T06:05Z'),
        _flight('VL', '018', '2026-08-07T13:00', '2026-08-07T11:00Z',
                '2026-08-07T14:05', '2026-08-07T12:05Z'),
        _flight('LH', '5960', '2026-08-07T09:00', '2026-08-07T07:00Z',
                '2026-08-07T10:05', '2026-08-07T08:05Z', op_al='CL'),
    ])
    out = lh.parse_route_flights(data)
    assert [r['flight'] for r in out] == ['LH2', 'VL18', 'LH5960']
    # Offset-ISO aus lokal−UTC (Sommer FRA = +02:00) — Instants, keine Wanduhr
    assert out[0]['sched_dep'] == '2026-08-07T07:00:00+02:00'
    assert out[0]['sched_arr'] == '2026-08-07T08:05:00+02:00'
    # operated_by NUR bei Abweichung
    assert 'operated_by' not in out[0]
    assert out[2]['operated_by'] == 'CL5960'
    assert out[0]['dep'] == 'FRA' and out[0]['arr'] == 'HAM'


def test_parse_skalar_ein_flug_als_objekt():
    data = _resp(_flight('DE', '4181', '2026-08-07T07:55',
                         '2026-08-07T05:55Z', '2026-08-07T09:00',
                         '2026-08-07T07:00Z'))
    out = lh.parse_route_flights(data)
    assert len(out) == 1 and out[0]['flight'] == 'DE4181'


def test_parse_status_durchgereicht_und_muell_uebersprungen():
    data = _resp([
        _flight('LH', '010', '2026-08-07T09:00', '2026-08-07T07:00Z',
                '2026-08-07T10:05', '2026-08-07T08:05Z', status='CD'),
        {'kaputt': True},                      # kein MarketingCarrier → skip
    ])
    out = lh.parse_route_flights(data)
    assert len(out) == 1 and out[0]['status'] == 'CD'


def test_parse_leere_und_kaputte_response():
    assert lh.parse_route_flights(None) == []
    assert lh.parse_route_flights({}) == []
    assert lh.parse_route_flights({'FlightStatusResource': {}}) == []


def _schedule(flights):
    return {'ScheduleResource': {'Schedule': [
        {'Flight': f} for f in flights
    ]}}


def test_parse_schedule_flights_direkt_und_offset_hart():
    direct = _flight('LH', '1963', '2026-08-26T07:20', None,
                     '2026-08-26T08:30', None,
                     dep_ap='BER', arr_ap='MUC')
    out = lh.parse_schedule_flights(_schedule([direct]), 'BER', 'MUC')
    assert len(out) == 1
    assert out[0]['flight'] == 'LH1963'
    assert out[0]['sched_dep'] == '2026-08-26T07:20:00+02:00'
    assert out[0]['sched_arr'] == '2026-08-26T08:30:00+02:00'


def test_parse_schedule_flights_laesst_umsteiger_und_fremde_route_weg():
    first = _flight('LH', '001', '2026-08-26T07:20', None,
                    '2026-08-26T08:30', None,
                    dep_ap='BER', arr_ap='FRA')
    second = _flight('LH', '002', '2026-08-26T09:20', None,
                     '2026-08-26T10:30', None,
                     dep_ap='FRA', arr_ap='MUC')
    wrong = _flight('LH', '003', '2026-08-26T07:20', None,
                    '2026-08-26T08:30', None,
                    dep_ap='BER', arr_ap='FRA')
    data = {'ScheduleResource': {'Schedule': [
        {'Flight': [first, second]}, {'Flight': wrong}
    ]}}
    assert lh.parse_schedule_flights(data, 'BER', 'MUC') == []


def test_route_flights_ferne_zukunft_nutzt_schedule(monkeypatch):
    _configured(monkeypatch)
    calls = []
    direct = _flight('LH', '1963', '2099-08-26T07:20', None,
                     '2099-08-26T08:30', None,
                     dep_ap='BER', arr_ap='MUC')

    def fake_get(path, caller=None):
        calls.append(path)
        return _schedule([direct])

    monkeypatch.setattr(lh, '_get', fake_get)
    monkeypatch.setattr(lh, 'last_call_answered', lambda: True)
    flights, answered = lh.route_flights('BER', 'MUC', '2099-08-26')
    assert answered and [f['flight'] for f in flights] == ['LH1963']
    assert calls == ['/operations/schedules/BER/MUC/2099-08-26'
                     '?directFlights=true&limit=100']


# ── _ensure_offset (Zeitzonen-Fehlerklasse) ─────────────────────────────────

def test_ensure_offset_ergaenzt_stations_offset():
    # Ohne UTC-Block liefert _side_times naives Lokal-ISO → Offset aus der
    # Stations-TZ (LIS = Europe/Lisbon, Sommer +01:00)
    assert lh._ensure_offset('2026-08-07T09:35', 'LIS') \
        == '2026-08-07T09:35:00+01:00'


def test_ensure_offset_laesst_vorhandenen_offset_stehen():
    assert lh._ensure_offset('2026-08-07T07:00:00+02:00', 'FRA') \
        == '2026-08-07T07:00:00+02:00'
    assert lh._ensure_offset('2026-08-07T05:00Z', 'FRA') == '2026-08-07T05:00Z'


def test_ensure_offset_unbekannte_station_bleibt_naiv():
    # NIE einen Offset erfinden — naiv bleibt naiv (Consumer datiert lokal)
    assert lh._ensure_offset('2026-08-07T09:35', 'QQZ') == '2026-08-07T09:35'


# ── route_flights: Cache- und Antwort-Semantik ──────────────────────────────

@pytest.fixture(autouse=True)
def _clean_route_memo():
    lh._route_memo.clear()
    yield
    lh._route_memo.clear()


def _configured(monkeypatch):
    monkeypatch.setattr(lh, 'lh_open_configured', lambda: True)


def test_route_flights_memo_ein_call_pro_strecke(monkeypatch):
    _configured(monkeypatch)
    calls = []

    def fake_get(path, caller=None):
        calls.append(path)
        return _resp([_flight('LH', '002', '2026-08-07T07:00',
                              '2026-08-07T05:00Z', '2026-08-07T08:05',
                              '2026-08-07T06:05Z')])

    monkeypatch.setattr(lh, '_get', fake_get)
    monkeypatch.setattr(lh, 'last_call_answered', lambda: True)
    f1, a1 = lh.route_flights('FRA', 'HAM', '2026-08-07')
    f2, a2 = lh.route_flights('fra', 'ham', '2026-08-07')   # normalisiert
    assert a1 and a2 and f1 == f2 and len(f1) == 1
    assert len(calls) == 1                                   # Memo-Hit


def test_route_flights_luecke_wird_nicht_gecacht(monkeypatch):
    _configured(monkeypatch)
    state = {'n': 0}

    def fake_get(path, caller=None):
        state['n'] += 1
        return None

    monkeypatch.setattr(lh, '_get', fake_get)
    monkeypatch.setattr(lh, 'last_call_answered', lambda: False)
    f1, a1 = lh.route_flights('FRA', 'HAM', '2026-08-07')
    assert f1 == [] and a1 is False
    # Lücke darf NICHT im Memo kleben — nächster Versuch fragt wieder
    f2, a2 = lh.route_flights('FRA', 'HAM', '2026-08-07')
    assert state['n'] == 2 and a2 is False


def test_route_flights_echtes_404_wird_gecacht(monkeypatch):
    _configured(monkeypatch)
    state = {'n': 0}

    def fake_get(path, caller=None):
        state['n'] += 1
        return None

    monkeypatch.setattr(lh, '_get', fake_get)
    monkeypatch.setattr(lh, 'last_call_answered', lambda: True)   # 404-Pfad
    f1, a1 = lh.route_flights('FRA', 'QQZ', '2026-08-07')
    f2, a2 = lh.route_flights('FRA', 'QQZ', '2026-08-07')
    assert f1 == [] and a1 is True and a2 is True
    assert state['n'] == 1                     # „keine Flüge" ist eine Antwort


def test_route_flights_validierung(monkeypatch):
    _configured(monkeypatch)
    monkeypatch.setattr(lh, '_get',
                        lambda *a, **k: pytest.fail('kein Call erwartet'))
    assert lh.route_flights('F', 'HAM', '2026-08-07') == ([], False)
    assert lh.route_flights('FRA', 'HAM', 'morgen') == ([], False)
    assert lh.route_flights('FRA', 'HAM', '') == ([], False)


# ── Endpoint /api/ax/shuttle-options/<token> ────────────────────────────────

@pytest.fixture(scope='module')
def appmod():
    import app as _app
    return _app


@pytest.fixture(scope='module')
def client(appmod):
    return appmod.app.test_client()


def test_endpoint_liefert_fluege(appmod, client, monkeypatch):
    monkeypatch.setattr(appmod, '_validate_token_exists', lambda t: True)
    monkeypatch.setattr(lh, 'lh_open_configured', lambda: True)
    monkeypatch.setattr(
        lh, 'route_flights',
        lambda frm, to, d, caller=None: ([{'flight': 'LH1167', 'dep': 'LIS',
                                           'arr': 'FRA'}], True))
    r = client.get('/api/ax/shuttle-options/AT-TEST?from=lis&to=fra'
                   '&date=2026-08-07')
    j = r.get_json()
    assert r.status_code == 200 and j['ok'] and j['answered']
    assert j['from'] == 'LIS' and j['flights'][0]['flight'] == 'LH1167'


def test_endpoint_reicht_condor_direktflug_ungefiltert_durch(appmod, client,
                                                             monkeypatch):
    """Shuttle ist eine Streckensuche, kein LH-Airline-Filter.

    Die LH-Route-Antwort kann Fremdcarrier enthalten; insbesondere darf eine
    DE-Verbindung für Condor-Crew nicht zwischen Provider und App verschwinden.
    """
    monkeypatch.setattr(appmod, '_validate_token_exists', lambda t: True)
    monkeypatch.setattr(lh, 'lh_open_configured', lambda: True)
    monkeypatch.setattr(
        lh, 'route_flights',
        lambda frm, to, d, caller=None: ([
            {'flight': 'LH2', 'dep': 'FRA', 'arr': 'HAM'},
            {'flight': 'DE4181', 'dep': 'FRA', 'arr': 'HAM'},
        ], True))
    r = client.get('/api/ax/shuttle-options/AT-CONDOR?from=FRA&to=HAM'
                   '&date=2026-08-22')
    j = r.get_json()
    assert r.status_code == 200 and j['ok'] and j['answered']
    assert [f['flight'] for f in j['flights']] == ['LH2', 'DE4181']


def test_endpoint_luecke_ist_kein_leeres_ergebnis(appmod, client, monkeypatch):
    monkeypatch.setattr(appmod, '_validate_token_exists', lambda t: True)
    monkeypatch.setattr(lh, 'lh_open_configured', lambda: True)
    monkeypatch.setattr(lh, 'route_flights',
                        lambda frm, to, d, caller=None: ([], False))
    r = client.get('/api/ax/shuttle-options/AT-TEST?from=LIS&to=FRA'
                   '&date=2026-08-07')
    j = r.get_json()
    assert j['ok'] and j['answered'] is False and j['flights'] == []


def test_endpoint_param_validierung(appmod, client, monkeypatch):
    monkeypatch.setattr(appmod, '_validate_token_exists', lambda t: True)
    for q in ('from=LIS&to=FRA&date=morgen', 'from=L&to=FRA&date=2026-08-07',
              'from=FRA&to=FRA&date=2026-08-07', ''):
        r = client.get(f'/api/ax/shuttle-options/AT-TEST?{q}')
        assert r.status_code == 400, q


def test_endpoint_invalid_token(appmod, client, monkeypatch):
    monkeypatch.setattr(appmod, '_validate_token_exists', lambda t: False)
    r = client.get('/api/ax/shuttle-options/AT-NIX?from=LIS&to=FRA'
                   '&date=2026-08-07')
    assert r.status_code == 404
