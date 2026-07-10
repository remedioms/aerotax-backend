"""Planmäßige Flugdauer / Ankunftszeit — GRATIS aus Soll-Ab/-An abgeleitet.

Owner-Wunsch 2026-07-09 (Screenshot LH1128 FRA→BCN „hier fehlt abflug ankunft
zeit … es fehlt auch flugzeit"): Ankunftszeit + Flugdauer sind bereits im
Warehouse (route-history sched_arr, ~67-100% Abdeckung), nur nicht überall
verdrahtet. Fix: reiner TZ-korrekter Helper `_sched_block_min` + Ausgabe in
`/api/flight/<token>/status` (Suche/Dienstplan) und `/api/ax/route-history`
(„DIESE STRECKE ZULETZT"). KEIN Drittanbieter-Spend, keine erfundene Dauer.

Kritisch getestet:
  • TZ-Korrektheit: dep/arr stehen je in IHRER Stations-Ortszeit → cross-TZ
    (FRA→JFK) muss über UTC gerechnet werden, nicht per String-Subtraktion.
  • Plausibilitäts-Guard: fehlend/negativ/unrealistisch → None (nie erfunden).
  • Endpoint-Wiring: duration_min erscheint in flight_status wenn beide Soll-
    Zeiten da sind, sonst None.
  • route-history: duration_min pro Flug bei dep-beobachteten Legs, NICHT bei
    reinen arr-Rows (dort ist sched==sched_arr).
"""
import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

import app as A


@pytest.fixture(autouse=True)
def _pin_app_module():
    # Gleiches sys.modules-Pinning wie test_my_flight_status (Order-Kontamination
    # durch test_calculation's Reimport-Trick).
    import sys
    _prev = sys.modules.get('app')
    sys.modules['app'] = A
    try:
        A._FLIGHT_MERGE_CACHE.clear()
    except Exception:
        pass
    yield
    if _prev is not None:
        sys.modules['app'] = _prev


@pytest.fixture
def client():
    A.app.testing = True
    return A.app.test_client()


# ─────────────────────────── _sched_block_min (pure) ───────────────────────────

def test_block_same_timezone():
    """Innereuropäisch (beide Europe/…, im Juli UTC+2): einfache Differenz."""
    # FRA 11:00 → MUC 12:05 = 65 min
    assert A._sched_block_min('2026-07-09T11:00:00', 'FRA',
                              '2026-07-09T12:05:00', 'MUC') == 65
    # FRA 11:00 → BCN 13:05 = 125 min (2h05, echte LH1128-Dauer)
    assert A._sched_block_min('2026-07-09T11:00:00', 'FRA',
                              '2026-07-09T13:05:00', 'BCN') == 125


def test_block_cross_timezone():
    """DER kritische Fall: FRA (UTC+2) → JFK (UTC-4, EDT). Naive String-Diff
    ergäbe 2h50 (falsch); UTC-korrekt sind es 8h50 = 530 min."""
    assert A._sched_block_min('2026-07-09T11:00:00', 'FRA',
                              '2026-07-09T13:50:00', 'JFK') == 530


def test_block_overnight():
    """Overnight-Flug (arr am Folgetag) — Datum in den Board-Strings macht es
    korrekt. FRA 22:00 → JFK 00:30(+1) EDT."""
    m = A._sched_block_min('2026-07-09T22:00:00', 'FRA',
                           '2026-07-10T00:30:00', 'JFK')
    # 22:00 CEST = 20:00Z ; 00:30(+1) EDT = 04:30Z(+1) → 8h30 = 510
    assert m == 510


def test_block_missing_inputs_none():
    for args in (
        (None, 'FRA', '2026-07-09T12:00:00', 'MUC'),
        ('2026-07-09T11:00:00', 'FRA', None, 'MUC'),
        ('2026-07-09T11:00:00', None, '2026-07-09T12:00:00', 'MUC'),
        ('2026-07-09T11:00:00', 'FRA', '2026-07-09T12:00:00', None),
        ('', '', '', ''),
    ):
        assert A._sched_block_min(*args) is None


def test_block_negative_none():
    """Ankunft vor Abflug (Datenfehler) → None, nie eine negative Dauer."""
    assert A._sched_block_min('2026-07-09T11:00:00', 'FRA',
                              '2026-07-09T10:00:00', 'FRA') is None


def test_block_zero_none():
    """Exakt 0 Minuten ist keine echte Flugdauer → None (Guard 0<min<=20h)."""
    assert A._sched_block_min('2026-07-09T11:00:00', 'FRA',
                              '2026-07-09T11:00:00', 'FRA') is None


def test_block_implausibly_long_none():
    """>20 h → None. TZ-Helper gemockt, damit die Plausibilitätsgrenze isoliert
    geprüft wird (unabhängig von echten Flughäfen)."""
    with patch.object(A, '_board_local_to_utc_iso',
                      side_effect=['2026-07-09T00:00:00Z',
                                   '2026-07-10T02:00:00Z']):  # 26 h
        assert A._sched_block_min('x', 'AAA', 'y', 'BBB') is None


def test_block_unknown_tz_none():
    """Unbekannte Stations-TZ (Helper gibt None) → None statt naiver Rechnung."""
    with patch.object(A, '_board_local_to_utc_iso',
                      side_effect=['2026-07-09T09:00:00Z', None]):
        assert A._sched_block_min('x', 'FRA', 'y', 'ZZZ') is None


# ─────────────────────── flight_status endpoint wiring ───────────────────────

def _merged_record(sched_arr='2026-07-09T13:05:00'):
    return {
        'ok': True, 'flight': 'LH1128',
        'dep_iata': 'FRA', 'arr_iata': 'BCN',
        'airline': 'LH', 'origin_name': 'Frankfurt', 'dest_name': 'Barcelona',
        'sched_dep': '2026-07-09T11:00:00', 'sched_arr': sched_arr,
        'esti_dep': None, 'esti_arr': None,
        'gate_dep': 'A17', 'terminal_dep': '1',
        'gate_arr': None, 'terminal_arr': None,
        'status': '', 'cancelled': False,
        'dep_delay_min': 0, 'arr_delay_min': None,
        'delay_min': 0, 'delay_side': 'dep', 'delay_known': True,
        'reg': 'DAINV', 'aircraft': 'A20N',
        'sides': {'dep': 'obs', 'arr': 'obs'},
        'has_dep': True, 'has_arr': sched_arr is not None,
    }


def test_flight_status_includes_duration(client):
    """duration_min (=125) + sched_arr erscheinen im flight_status, wenn beide
    Soll-Zeiten beobachtet sind — GRATIS, source bleibt aerox_obs_merged."""
    with patch.object(A, '_validate_token_exists', return_value='u1'), \
         patch.object(A, '_flight_obs_merged', return_value=_merged_record()):
        r = client.get('/api/flight/AT-DURTEST/status?number=LH1128')
    assert r.status_code == 200
    body = r.get_json()
    assert body.get('ok') is True
    assert body.get('source') == 'aerox_obs_merged'
    f = body['flight']
    assert f['sched_dep'] == '2026-07-09T11:00:00'
    assert f['sched_arr'] == '2026-07-09T13:05:00'
    assert f['duration_min'] == 125


def test_flight_status_duration_none_without_arr(client):
    """Kein sched_arr beobachtet → duration_min None (nichts erfunden)."""
    with patch.object(A, '_validate_token_exists', return_value='u1'), \
         patch.object(A, '_flight_obs_merged',
                      return_value=_merged_record(sched_arr=None)):
        r = client.get('/api/flight/AT-DURTEST/status?number=LH1128')
    assert r.status_code == 200
    f = r.get_json()['flight']
    assert f['sched_arr'] is None
    assert f['duration_min'] is None


# ─────────────────────── route-history endpoint wiring ───────────────────────

def _route_rows():
    dep_row = {'flight': 'LH1128', 'airline': 'LH', 'dest_iata': 'BCN',
               'sched': '2026-07-09T11:00:00', 'delay_min': 0,
               'delay_known': True, 'cancelled': False}
    arr_row = {'flight': 'LH1128', 'airline': 'LH', 'dest_iata': 'FRA',
               'sched': '2026-07-09T13:05:00', 'delay_min': 0,
               'delay_known': True, 'cancelled': False}
    return dep_row, arr_row


def _fixed_now(_key):
    return datetime(2026, 7, 9, 15, 0, tzinfo=timezone.utc)


def test_route_history_includes_duration(client):
    """route-history-Flug trägt duration_min=125 bei dep+arr-Beobachtung."""
    dep_row, arr_row = _route_rows()

    def fake_departed(key):
        return [arr_row] if str(key).endswith('#ARR') else [dep_row]

    with patch.object(A, '_airport_local_now', side_effect=_fixed_now), \
         patch.object(A, '_departed_rows_from_store', side_effect=fake_departed), \
         patch.object(A, '_ax_codeshare_map', return_value={}):
        r = client.get('/api/ax/route-history/FRA/BCN?days=1')
    assert r.status_code == 200
    days = r.get_json()['recent_days']
    assert days, 'expected at least one day'
    flights = days[0]['flights']
    lh = next(f for f in flights if f['flight'] == 'LH1128')
    assert lh.get('sched_arr') == '2026-07-09T13:05:00'
    assert lh.get('duration_min') == 125


def test_route_history_no_duration_for_arr_only(client):
    """Reiner arr-Row (Abflug-Board sah den Flug nicht): sched==sched_arr →
    KEINE erfundene Dauer aus einer Nullspanne."""
    _dep, arr_row = _route_rows()

    def fake_departed(key):
        # Nur die Ankunftsseite liefert etwas; dep-Store ist leer.
        return [arr_row] if str(key).endswith('#ARR') else []

    with patch.object(A, '_airport_local_now', side_effect=_fixed_now), \
         patch.object(A, '_departed_rows_from_store', side_effect=fake_departed), \
         patch.object(A, '_ax_codeshare_map', return_value={}):
        r = client.get('/api/ax/route-history/FRA/BCN?days=1')
    assert r.status_code == 200
    days = r.get_json()['recent_days']
    if days:
        for f in days[0]['flights']:
            assert 'duration_min' not in f or f['duration_min'] is None


def test_route_history_windowed_escalates_on_zero_hits():
    """Dünne Strecke (FRA-NBJ): days=3 hat 0 Beobachtungen → das Aggregat
    weitet automatisch auf 7 Tage (Sweep 2026-07-10) statt ohne Flugzeit/
    Landung zu bleiben. Bei Treffern im 3d-Fenster KEIN zweiter Call."""
    import blueprints.aerox_data_blueprint as BP
    calls = []

    def fake_subcall(app_obj, path, view, *args):
        calls.append(path)
        if 'days=3' in path:
            return {'ok': True, 'total': 0, 'recent_days': []}
        return {'ok': True, 'total': 2,
                'recent_days': [{'date': '2026-07-08', 'count': 2, 'flights': []}]}

    with patch.object(BP, '_detail_subcall', side_effect=fake_subcall):
        h = BP._route_history_windowed(A.app, 'FRA', 'NBJ')
    assert h is not None and h['total'] == 2
    assert calls == ['/api/ax/route-history/FRA/NBJ?days=3',
                     '/api/ax/route-history/FRA/NBJ?days=7']

    # Treffer schon im 3d-Fenster → genau EIN Call (Latenz-Sweet-Spot bleibt).
    calls.clear()
    with patch.object(BP, '_detail_subcall',
                      side_effect=lambda *a: (calls.append(a[1]) or
                                              {'ok': True, 'total': 5,
                                               'recent_days': []})):
        h2 = BP._route_history_windowed(A.app, 'FRA', 'JFK')
    assert h2['total'] == 5 and calls == ['/api/ax/route-history/FRA/JFK?days=3']


# ─────────────────────── tail-history endpoint wiring ───────────────────────

def _fake_flights_sb(rows):
    """Chainbares Fake-Supabase für den flights-Query der tail-history
    (table→select→in_→order→order→limit→execute)."""
    from unittest.mock import MagicMock
    from types import SimpleNamespace
    q = MagicMock()
    for m in ('table', 'select', 'in_', 'eq', 'order', 'limit', 'gte', 'gt'):
        getattr(q, m).return_value = q
    q.execute.return_value = SimpleNamespace(data=rows)
    return q


def test_tail_history_includes_duration(client):
    """tail-history-Leg trägt sched_arr + duration_min — flights.sched_arr ist
    absolut-UTC (+00:00), _sched_block_min bleibt korrekt (kein Doppel-Shift)."""
    import blueprints.aerox_data_blueprint as BP
    rows = [{
        'op_flight_no': 'LH1128', 'origin': 'FRA', 'destination': 'BCN',
        'service_date': '2026-07-08',
        'sched_dep': '2026-07-08T12:45:00+00:00',
        'sched_arr': '2026-07-08T14:50:00+00:00',   # 125 min
        'est_dep': None, 'est_arr': None,
        'status': 'landed', 'tail': 'DAINV', 'hex': '3c6789',
    }]
    with patch.object(BP, '_sb', return_value=_fake_flights_sb(rows)):
        r = client.get('/api/ax/tail-history?reg=D-AINV')
    assert r.status_code == 200
    legs = r.get_json()['legs']
    assert legs and legs[0]['flight_no'] == 'LH1128'
    assert legs[0]['sched_arr'] == '2026-07-08T14:50:00+00:00'
    assert legs[0]['duration_min'] == 125


def test_tail_history_no_arr_no_duration(client):
    """Leg ohne sched_arr → duration_min None (nichts erfunden)."""
    import blueprints.aerox_data_blueprint as BP
    rows = [{
        'op_flight_no': 'LH1128', 'origin': 'FRA', 'destination': 'BCN',
        'service_date': '2026-07-08', 'sched_dep': '2026-07-08T12:45:00+00:00',
        'sched_arr': None, 'est_dep': None, 'est_arr': None,
        'status': 'landed', 'tail': 'DAINV', 'hex': '3c6789',
    }]
    with patch.object(BP, '_sb', return_value=_fake_flights_sb(rows)):
        r = client.get('/api/ax/tail-history?reg=D-AINV')
    assert r.status_code == 200
    legs = r.get_json()['legs']
    assert legs and legs[0]['sched_arr'] is None
    assert legs[0]['duration_min'] is None


def test_tail_history_fr24_fallback_when_warehouse_empty(client):
    """Warehouse leer (D-AIXS nie getafelt) + own=1 (EIGENE Maschine, mit
    Bearer — App sendet ihn immer) → FR24-by-registration Fallback, Ergebnis
    mit source='fr24' UND permanent gespeichert (_crowdsource_flight_obs)."""
    import blueprints.aerox_data_blueprint as BP
    fr_legs = [{
        'flight_no': 'LH416', 'src': 'FRA', 'dst': 'IAD', 'day': '2026-07-06',
        'sched_dep': '2026-07-06T09:30:00Z', 'sched_arr': '2026-07-06T17:30:00Z',
        'duration_min': 480, 'status': 'landed', 'reg': 'DAIHW', 'type': 'A346',
        'dep_iata': 'FRA', 'arr_iata': 'IAD', 'aircraft': 'A346',
    }]
    with patch.object(BP, '_sb', return_value=_fake_flights_sb([])), \
         patch.object(BP, '_fr24_available', return_value=True), \
         patch.object(BP, '_fr24_budget_ok', return_value=True), \
         patch.object(BP, '_fr24_flights_by_reg', return_value=fr_legs), \
         patch.object(A, '_crowdsource_flight_obs', return_value=True) as mcs:
        r = client.get('/api/ax/tail-history?reg=D-AIXS&own=1',
                       headers={'Authorization': 'Bearer AT-TEST'})
    assert r.status_code == 200
    body = r.get_json()
    assert body['source'] == 'fr24'
    assert body['legs'][0]['flight_no'] == 'LH416'
    assert body['legs'][0]['duration_min'] == 480
    assert mcs.called            # permanent ins Warehouse geschrieben


def test_tail_history_own_without_bearer_stays_free(client):
    """own=1 OHNE Authorization-Bearer schaltet KEIN Paid mehr (Sweep
    2026-07-10, Klasse A: own war ein reiner Client-Query-Param — anonymes
    curl konnte Credits ziehen). Warehouse-Teil antwortet normal."""
    import blueprints.aerox_data_blueprint as BP
    with patch.object(BP, '_sb', return_value=_fake_flights_sb([])), \
         patch.object(BP, '_fr24_available', return_value=True), \
         patch.object(BP, '_fr24_budget_ok', return_value=True), \
         patch.object(BP, '_fr24_flights_by_reg') as mfr:
        r = client.get('/api/ax/tail-history?reg=D-AIXS&own=1')
    assert r.status_code == 200
    body = r.get_json()
    assert body['source'] == 'warehouse' and body['legs'] == []
    mfr.assert_not_called()


def test_tail_history_fr24_window_escalates_3_7_14(client):
    """Fenster dynamisch (Sweep 2026-07-10): leere FR24-Stufen eskalieren
    3→7→14 Tage — selten fliegende Maschine (Leg vor 12 Tagen) wird noch
    gefunden, aktive Maschinen bleiben beim billigen 3d-Fenster."""
    import blueprints.aerox_data_blueprint as BP
    fr_legs = [{'flight_no': 'LH590', 'src': 'FRA', 'dst': 'NBO',
                'day': '2026-06-28', 'sched_dep': '2026-06-28T20:00:00Z',
                'sched_arr': '2026-06-29T04:35:00Z', 'duration_min': 515,
                'status': 'landed'}]
    seen_days = []

    def fake_by_reg(reg, days=7, limit=12):
        seen_days.append(days)
        return fr_legs if days == 14 else []

    with patch.object(BP, '_sb', return_value=_fake_flights_sb([])), \
         patch.object(BP, '_fr24_available', return_value=True), \
         patch.object(BP, '_fr24_budget_ok', return_value=True), \
         patch.object(BP, '_fr24_flights_by_reg', side_effect=fake_by_reg), \
         patch.object(A, '_crowdsource_flight_obs', return_value=True):
        r = client.get('/api/ax/tail-history?reg=D-AIXS&own=1',
                       headers={'Authorization': 'Bearer AT-TEST'})
    assert r.status_code == 200
    body = r.get_json()
    assert seen_days == [3, 7, 14]
    assert body['source'] == 'fr24'
    assert body['legs'][0]['flight_no'] == 'LH590'


def test_tail_history_anonymous_no_paid(client):
    """OHNE own/watch bleibt der Endpoint Warehouse-only: kein Paid-per-Tap —
    auch bei Warehouse-Miss wird FR24 NICHT angefasst (Kosten-Review 2026-07-09)."""
    import blueprints.aerox_data_blueprint as BP
    with patch.object(BP, '_sb', return_value=_fake_flights_sb([])), \
         patch.object(BP, '_fr24_available', return_value=True), \
         patch.object(BP, '_fr24_budget_ok', return_value=True), \
         patch.object(BP, '_fr24_flights_by_reg') as mfr:
        r = client.get('/api/ax/tail-history?reg=D-AIXS')
    assert r.status_code == 200
    body = r.get_json()
    assert body['source'] == 'warehouse' and body['legs'] == []
    mfr.assert_not_called()


def test_tail_history_no_fr24_when_warehouse_has_data(client):
    """Hat das Warehouse Legs, wird FR24 NICHT angefasst (free-first)."""
    import blueprints.aerox_data_blueprint as BP
    rows = [{'op_flight_no': 'LH1128', 'origin': 'FRA', 'destination': 'BCN',
             'service_date': '2026-07-08', 'sched_dep': '2026-07-08T12:45:00+00:00',
             'sched_arr': '2026-07-08T14:50:00+00:00', 'est_dep': None,
             'est_arr': None, 'status': 'landed', 'tail': 'DAINV', 'hex': 'x'}]
    with patch.object(BP, '_sb', return_value=_fake_flights_sb(rows)), \
         patch.object(BP, '_fr24_flights_by_reg') as mfr:
        r = client.get('/api/ax/tail-history?reg=D-AINV')
    assert r.status_code == 200
    assert r.get_json()['source'] == 'warehouse'
    mfr.assert_not_called()


# ─────────────────── flight_status FR24 paid fallback ───────────────────

def test_flight_status_fr24_fallback_when_free_empty(client):
    """Freie Quellen leer (LH714 nicht getafelt) → FR24 flight-summary Fallback,
    source='fr24', Ergebnis permanent gespeichert."""
    import blueprints.aerox_data_blueprint as BP
    fr = {
        'flight': 'LH714', 'airline': '', 'airline_name': '',
        'dep_iata': 'MUC', 'dep_name': '', 'arr_iata': 'HND', 'arr_name': '',
        'sched_dep': '2026-07-09T14:00:00Z', 'sched_arr': '2026-07-10T09:00:00Z',
        'est_dep': None, 'est_arr': None, 'duration_min': 600,
        'dep_gate': '', 'dep_terminal': '', 'arr_gate': '', 'arr_terminal': '',
        'arr_baggage': '', 'status': 'landed', 'status_category': 'landed',
        'aircraft': 'A359', 'reg': 'DAIXS', 'dep_delay_min': None,
        'arr_delay_min': None, 'delay_min': None, 'delay_side': None,
    }
    with patch.object(A, '_validate_token_exists', return_value='u1'), \
         patch.object(A, '_flight_obs_merged', return_value=None), \
         patch.object(BP, '_fr24_available', return_value=True), \
         patch.object(BP, '_fr24_flight_by_number', return_value=fr), \
         patch.object(A, '_crowdsource_flight_obs', return_value=True) as mcs:
        r = client.get('/api/flight/AT-FR24/status?number=LH714')
    assert r.status_code == 200
    body = r.get_json()
    assert body.get('source') == 'fr24'
    assert body['flight']['arr_iata'] == 'HND'
    assert body['flight']['duration_min'] == 600
    assert mcs.called


def test_flight_status_no_fr24_when_free_has_data(client):
    """Freie Merge liefert etwas → FR24 wird NICHT angefasst (free-first)."""
    import blueprints.aerox_data_blueprint as BP
    with patch.object(A, '_validate_token_exists', return_value='u1'), \
         patch.object(A, '_flight_obs_merged', return_value=_merged_record()), \
         patch.object(BP, '_fr24_flight_by_number') as mfr:
        r = client.get('/api/flight/AT-FR24/status?number=LH1128')
    assert r.status_code == 200
    assert r.get_json().get('source') == 'aerox_obs_merged'
    mfr.assert_not_called()


# ─────────────────── FR24 airline prewarm endpoint ───────────────────

def test_fr24_prewarm_imports_to_warehouse(client):
    """Prewarm (localhost, kein Secret gesetzt) holt Airline-Flüge + schreibt sie
    permanent (_crowdsource_flight_obs)."""
    import os as _os
    import blueprints.aerox_data_blueprint as BP
    legs = [{'flight_no': '4Y100', 'src': 'FRA', 'dst': 'BCN',
             'day': '2026-07-09', 'sched_dep': '2026-07-09T06:00:00Z',
             'sched_arr': '2026-07-09T08:05:00Z', 'duration_min': 125,
             'status': 'landed', 'reg': 'DAIKO', 'type': 'A333',
             'dep_iata': 'FRA', 'arr_iata': 'BCN', 'aircraft': 'A333'}]
    _prev = _os.environ.pop('ADSB_POLL_SECRET', None)
    try:
        with patch.object(BP, '_fr24_flights_by_airline', return_value=legs), \
             patch.object(A, '_crowdsource_flight_obs', return_value=True) as mcs:
            r = client.post('/api/ax/fr24-prewarm?icao=OCN&days=2')
    finally:
        if _prev is not None:
            _os.environ['ADSB_POLL_SECRET'] = _prev
    assert r.status_code == 200
    body = r.get_json()
    assert body['ok'] and body['icao'] == 'OCN'
    assert body['fetched'] == 1 and body['imported'] == 1
    assert mcs.called


def test_fr24_prewarm_wrong_secret_401(client):
    import os as _os
    import blueprints.aerox_data_blueprint as BP
    with patch.dict(_os.environ, {'ADSB_POLL_SECRET': 'sekret'}), \
         patch.object(BP, '_fr24_flights_by_airline') as mfa:
        r = client.post('/api/ax/fr24-prewarm?icao=OCN',
                        headers={'X-Poll-Secret': 'falsch'})
    assert r.status_code == 401
    mfa.assert_not_called()


# ─────────── LH752 mixed-day guard (dep heute + arr von gestern) ───────────

def test_merged_drops_stale_prior_day_arrival():
    """Nachtflug LH752: heutiger Abflug 13:00 FRA + Ankunfts-Obs 00:46 HYD (=
    gestriges Exemplar, UTC vor dem Abflug) → arr verworfen, NICHT fälschlich
    'Arrived'."""
    dep_row = {'flight': 'LH752', 'sched': '2026-07-09T13:00:00',
               'dest_iata': 'HYD', 'delay_min': 15, 'delay_known': True,
               'status': 'Departed', 'cancelled': False}
    arr_row = {'flight': 'LH752', 'sched': '2026-07-09T00:46:00',
               'dest_iata': 'FRA', 'delay_min': 0, 'delay_known': True,
               'status': 'Arrived', 'cancelled': False}

    def fake_dep(key):
        return [arr_row] if str(key).endswith('#ARR') else [dep_row]

    with patch.object(A, '_departed_rows_from_store', side_effect=fake_dep):
        m = A._flight_obs_merged('LH752', date=None, dep_iata='FRA',
                                 arr_iata='HYD', live=False, free_only=True)
    assert m is not None
    assert m['has_dep'] is True
    assert m['has_arr'] is False          # gestrige Ankunft verworfen
    assert m['status'] != 'Arrived'       # heutiger Flug nicht „gelandet"


def test_merged_keeps_valid_same_instance_arrival():
    """Gegenprobe: normale Ankunft NACH dem Abflug bleibt erhalten."""
    dep_row = {'flight': 'LH1128', 'sched': '2026-07-09T11:00:00',
               'dest_iata': 'BCN', 'delay_min': 0, 'delay_known': True,
               'status': 'Departed', 'cancelled': False}
    arr_row = {'flight': 'LH1128', 'sched': '2026-07-09T13:05:00',
               'dest_iata': 'FRA', 'delay_min': 0, 'delay_known': True,
               'status': 'Arrived', 'cancelled': False}

    def fake_dep(key):
        return [arr_row] if str(key).endswith('#ARR') else [dep_row]

    with patch.object(A, '_departed_rows_from_store', side_effect=fake_dep):
        m = A._flight_obs_merged('LH1128', date=None, dep_iata='FRA',
                                 arr_iata='BCN', live=False, free_only=True)
    assert m is not None and m['has_arr'] is True


# ─────────────────── resolve-callsign endpoint ───────────────────

def test_resolve_callsign_returns_true_flight(client):
    """GET /api/ax/resolve-callsign/OCN601 → echter Flug via FR24, permanent."""
    import blueprints.aerox_data_blueprint as BP
    fr = {'flight': '4Y60', 'callsign': 'OCN601', 'dep_iata': 'FRA',
          'arr_iata': 'RSW', 'reg': 'DAIKO', 'aircraft': 'A333',
          'sched_dep': '2026-07-09T12:00:00Z', 'sched_arr': None,
          'duration_min': None, 'status': ''}
    with patch.object(BP, '_aircraft_live_flight', return_value=None), \
         patch.object(BP, '_fr24_flight_by_callsign', return_value=fr), \
         patch.object(A, '_crowdsource_flight_obs', return_value=True) as mcs:
        r = client.get('/api/ax/resolve-callsign/OCN601')
    assert r.status_code == 200
    body = r.get_json()
    assert body['ok'] and body['source'] == 'fr24'
    assert body['flight']['flight'] == '4Y60'
    assert body['flight']['arr_iata'] == 'RSW'
    assert mcs.called


def test_resolve_callsign_not_found(client):
    import blueprints.aerox_data_blueprint as BP
    with patch.object(BP, '_fr24_flight_by_callsign', return_value=None):
        r = client.get('/api/ax/resolve-callsign/OCN999')
    assert r.status_code == 200
    assert r.get_json()['ok'] is False


def test_aircraft_live_flight_free_callsign():
    """aircraft_live (gratis) liefert echten Funknamen + Route + Reg für aktiven Flug."""
    import blueprints.aerox_data_blueprint as BP
    row = {'flight': 'LH1412', 'callsign': 'DLH8UA', 'reg': 'DAINY',
           'reg_display': 'D-AINY', 'ac_type': 'A20N', 'origin': 'FRA',
           'dest': 'BEG', 'on_ground': False, 'seen_ts': '2026-07-09T12:50:00Z'}
    with patch.object(BP, '_sb', return_value=_fake_flights_sb([row])):
        f = BP._aircraft_live_flight(flight='LH1412')
    assert f is not None
    assert f['callsign'] == 'DLH8UA' and f['dep_iata'] == 'FRA' and f['arr_iata'] == 'BEG'
    assert f['reg'] == 'DAINY' and f['aircraft'] == 'A20N'
    assert f['source'] == 'aircraft_live'


def test_resolve_flight_free_first_no_fr24_credit(client):
    """resolve-flight nimmt ZUERST das gratis aircraft_live → KEIN FR24-Credit.
    (_flight_times_free_first isoliert gemockt: der Zeiten-Enrich hat einen
    eigenen free→paid-Pfad samt echtem gRPC-Call — hier geht es NUR um die
    Identitäts-/Routen-Auflösung, kein Netz im Unit-Test.)"""
    import blueprints.aerox_data_blueprint as BP
    live = {'flight': 'LH1412', 'callsign': 'DLH8UA', 'dep_iata': 'FRA',
            'arr_iata': 'BEG', 'reg': 'DAINY', 'aircraft': 'A20N',
            'source': 'aircraft_live'}
    with patch.object(BP, '_aircraft_live_flight', return_value=live), \
         patch.object(BP, '_flight_times_free_first', return_value={}), \
         patch.object(BP, '_fr24_flight_by_number') as mfr:
        r = client.get('/api/ax/resolve-flight/LH1412')
    assert r.status_code == 200
    body = r.get_json()
    assert body['ok'] and body['source'] == 'aircraft_live'
    assert body['flight']['callsign'] == 'DLH8UA'
    mfr.assert_not_called()          # gratis, kein FR24


def test_resolve_flight_returns_truth_with_real_callsign(client):
    """GET /api/ax/resolve-flight/LH1412 → echte Route + ECHTER Funkname (DLH8UA,
    nicht DLH1412) via FR24, permanent."""
    import blueprints.aerox_data_blueprint as BP
    fr = {'flight': 'LH1412', 'callsign': 'DLH8UA', 'dep_iata': 'FRA',
          'arr_iata': 'BEG', 'reg': 'DAINY', 'aircraft': 'A20N',
          'sched_dep': '2026-07-09T12:00:00Z', 'sched_arr': None,
          'duration_min': None, 'status': ''}
    with patch.object(BP, '_aircraft_live_flight', return_value=None), \
         patch.object(BP, '_flight_times_free_first', return_value={}), \
         patch.object(BP, '_fr24_flight_by_number', return_value=fr), \
         patch.object(A, '_crowdsource_flight_obs', return_value=True) as mcs:
        r = client.get('/api/ax/resolve-flight/LH1412')
    assert r.status_code == 200
    body = r.get_json()
    assert body['ok'] and body['source'] == 'fr24'
    assert body['flight']['arr_iata'] == 'BEG'
    assert body['flight']['callsign'] == 'DLH8UA'
    assert mcs.called


def test_resolve_flight_implausible_prefix_no_paid(client):
    """Plausi-Gate (Kosten-Review 2026-07-09): Fantasie-Präfix ('9Z' ist keine
    Airline in der Referenz) erreicht den Paid-Zweig NIE — jeder FR24-Miss
    kostete sonst trotzdem Credits."""
    import blueprints.aerox_data_blueprint as BP
    with patch.object(BP, '_aircraft_live_flight', return_value=None), \
         patch.object(BP, '_fr24_flight_by_number') as mfr:
        r = client.get('/api/ax/resolve-flight/9Z999')
    assert r.status_code == 200
    assert r.get_json()['ok'] is False
    mfr.assert_not_called()


def test_resolve_flight_obs_facts_before_paid(client):
    """P1-2: kennt die permanente Obs-Quelle die Route, wird der Request GRATIS
    beantwortet — FR24 wird NICHT angefasst."""
    import blueprints.aerox_data_blueprint as BP
    facts = {'dep_iata': 'FRA', 'arr_iata': 'BCN',
             'sched_dep': '2026-07-09T11:00:00', 'sched_arr': '2026-07-09T13:05:00',
             'reg': 'DAINV', 'type': 'A21N', 'dep_status': 'Departed'}
    with patch.object(BP, '_aircraft_live_flight', return_value=None), \
         patch.object(BP, '_flight_facts_from_obs', return_value=facts), \
         patch.object(BP, '_fr24_flight_by_number') as mfr:
        r = client.get('/api/ax/resolve-flight/LH1128')
    assert r.status_code == 200
    body = r.get_json()
    assert body['ok'] and body['source'] == 'aerox_obs'
    assert body['flight']['dep_iata'] == 'FRA'
    assert body['flight']['arr_iata'] == 'BCN'
    mfr.assert_not_called()


def test_harvest_routes_noop_no_external_calls(client):
    """adsbdb-Kappung: der Endpoint bleibt (alte Builds rufen ihn), macht aber
    weder adsbdb- noch Cache-Zugriffe mehr — reiner ok-No-op."""
    import blueprints.aerox_data_blueprint as BP
    with patch.object(BP, '_adsbdb_route') as mad, \
         patch.object(BP, '_cache_get') as mcg:
        r = client.post('/api/ax/harvest-routes',
                        json={'callsigns': ['DLH123', 'RYR55']})
    assert r.status_code == 200
    body = r.get_json()
    assert body['ok'] and body['harvested'] == 0
    mad.assert_not_called()
    mcg.assert_not_called()


def test_track_prune_403_without_secret(client):
    """Leeres ADSB_POLL_SECRET → 403 statt fail-open (Lösch-Endpoint)."""
    import os as _os
    _prev = _os.environ.pop('ADSB_POLL_SECRET', None)
    try:
        r = client.post('/api/internal/track-prune')
    finally:
        if _prev is not None:
            _os.environ['ADSB_POLL_SECRET'] = _prev
    assert r.status_code == 403
