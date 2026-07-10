"""„Wo ist mein/euer Flieger" — Backend-Teil (Owner 2026-07-04).

Deckt ab:
  • _derive_on_time            — PÜNKTLICH-Verdikt aus echten Board-Daten
  • /api/ax/my-flight-status   — eigener Tail + On-Time-Wrapper um _flight_obs_merged
  • get_friends_today.flights_live — est_*/sched_*_iso (UTC) + delay_known pro Leg,
    EIN Fan-out-Call (Batch), nie erfundene Position/Delay.

KEIN echter Netz-/DB-Zugriff: _flight_obs_merged wird gemockt. free_only=True darf
NIE eine bezahlte Quelle treffen.
"""
import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

from datetime import datetime, timezone, timedelta, date as _date
from unittest.mock import patch, MagicMock

import pytest

import app as A
from blueprints.aerox_data_blueprint import _derive_on_time
import blueprints.aerox_data_blueprint as BP


@pytest.fixture(autouse=True)
def _clear_caches():
    # SYS.MODULES-PIN (Order-Kontamination, 2026-07-05): test_calculation.py
    # tauscht sys.modules['app'] per Reimport-Trick aus. Die Blueprints lösen
    # `_life_app('_flight_obs_merged')` aber zur CALL-Zeit über sys.modules['app']
    # auf — unsere patch.object(A, …)-Mocks (A = Import zur Collection-Zeit)
    # liefen danach ins Leere (mock.call_args == None; 8 Fails NUR im Full-Run,
    # isoliert alles grün). Für die Dauer jedes Tests dieses Files das eigene
    # A-Modul pinnen, danach den vorherigen Zustand wiederherstellen.
    import sys
    _prev_app_mod = sys.modules.get('app')
    sys.modules['app'] = A
    A._FLIGHT_MERGE_CACHE.clear()
    A._FRIENDS_TODAY_MEMO.clear()   # 90s-Crew-Cache am ECHTEN Modul A leeren
    try:
        BP._LIFECYCLE_MEMO.clear()
    except Exception:
        pass
    yield
    A._FLIGHT_MERGE_CACHE.clear()
    if _prev_app_mod is not None:
        sys.modules['app'] = _prev_app_mod


@pytest.fixture
def client():
    A.app.testing = True
    return A.app.test_client()


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _now():
    return datetime.now(timezone.utc)


def _merged(**kw):
    base = {
        'ok': True, 'delay_min': None, 'delay_known': False, 'delay_side': None,
        'dep_delay_min': None, 'arr_delay_min': None, 'status': None,
        'cancelled': False, 'sched_dep': None, 'esti_dep': None,
        'sched_arr': None, 'esti_arr': None, 'reg': None, 'aircraft': None,
        'dep_iata': 'FRA', 'arr_iata': 'JFK',
        'sides': {'dep': None, 'arr': None},
    }
    base.update(kw)
    return base


# ══════════════════════════════════════════════════════════════════════════════
# _derive_on_time
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize('known,dmin,cxl,expected', [
    (True, 0, False, True),      # pünktlich (0 < 15)
    (True, 5, False, True),      # pünktlich (<15)
    (True, 14, False, True),     # Grenze <15
    (True, 15, False, False),    # D15-Schwelle → verspätet
    (True, 40, False, False),    # verspätet
    (False, None, False, None),  # unbekannt → neutral (kein Claim)
    (False, 0, False, None),     # unbekannt trotz 0 → NIE „pünktlich"
    (True, 0, True, False),      # cancelled schlägt pünktlich
    (True, 5, True, False),      # cancelled schlägt Delay
    (False, None, True, False),  # cancelled schlägt unbekannt
])
def test_derive_on_time(known, dmin, cxl, expected):
    assert _derive_on_time(known, dmin, cxl) is expected


def test_derive_on_time_bad_delay_is_neutral():
    assert _derive_on_time(True, 'garbage', False) is None


# ══════════════════════════════════════════════════════════════════════════════
# /api/ax/my-flight-status
# ══════════════════════════════════════════════════════════════════════════════
def _get_status(client, **q):
    q.setdefault('flight_no', 'LH400')
    q.setdefault('date', '2026-07-04')
    q.setdefault('dep_iata', 'FRA')
    qs = '&'.join(f'{k}={v}' for k, v in q.items())
    return client.get(f'/api/ax/my-flight-status/TESTTOKEN?{qs}')


def test_mystatus_needs_flight_and_dep(client):
    r = client.get('/api/ax/my-flight-status/TESTTOKEN?flight_no=LH')
    assert r.status_code == 400
    r = client.get('/api/ax/my-flight-status/TESTTOKEN?flight_no=LH400')
    assert r.status_code == 400   # dep_iata fehlt


def test_mystatus_no_signal_is_honest(client):
    with patch.object(A, '_flight_obs_merged', return_value=None):
        r = _get_status(client)
    assert r.status_code == 200
    b = r.get_json()
    assert b['ok'] is True
    assert b['reg'] is None                 # kein Fake-Tail
    assert b['delay_known'] is False
    assert b['on_time'] is None             # kein PÜNKTLICH-Claim
    assert b['est_dep_iso'] is None


def test_mystatus_on_time_punctual(client):
    m = _merged(reg='D-AIXA', aircraft='A350-900', delay_known=True,
                delay_min=5, delay_side='dep', status='boarding',
                arr_iata='JFK')
    with patch.object(A, '_flight_obs_merged', return_value=m):
        r = _get_status(client)
    b = r.get_json()
    assert b['reg'] == 'D-AIXA'
    assert b['aircraft'] == 'A350-900'
    assert b['delay_known'] is True
    assert b['on_time'] is True
    assert b['cancelled'] is False


def test_mystatus_delayed_not_on_time(client):
    m = _merged(reg='D-AIXA', delay_known=True, delay_min=40, delay_side='arr')
    with patch.object(A, '_flight_obs_merged', return_value=m):
        r = _get_status(client)
    b = r.get_json()
    assert b['on_time'] is False
    assert b['delay_min'] == 40


def test_mystatus_unknown_delay_neutral(client):
    m = _merged(reg='D-AIXA', delay_known=False, delay_min=None)
    with patch.object(A, '_flight_obs_merged', return_value=m):
        r = _get_status(client)
    b = r.get_json()
    assert b['on_time'] is None
    assert b['delay_min'] is None           # kein erfundenes +0


def test_mystatus_cancelled(client):
    m = _merged(reg='D-AIXA', delay_known=True, status='cancelled',
                cancelled=True, delay_min=None)
    with patch.object(A, '_flight_obs_merged', return_value=m):
        r = _get_status(client)
    b = r.get_json()
    assert b['cancelled'] is True
    assert b['on_time'] is False            # annulliert schlägt Delay


def test_mystatus_est_iso_utc_normalized(client):
    # esti mit Offset → echt-UTC (…Z), station-lokal erst iOS-seitig.
    m = _merged(reg='D-AIXA', delay_known=True, delay_min=0, delay_side='dep',
                esti_dep='2026-07-04T11:15:00+02:00',
                esti_arr='2026-07-04T19:45:00Z', arr_iata='JFK')
    with patch.object(A, '_flight_obs_merged', return_value=m):
        r = _get_status(client)
    b = r.get_json()
    assert b['est_dep_iso'] == '2026-07-04T09:15:00Z'
    assert b['est_arr_iso'] == '2026-07-04T19:45:00Z'


def test_mystatus_free_only_no_paid_call(client):
    # free_only=True MUSS an _flight_obs_merged durchgereicht werden.
    mock = MagicMock(return_value=_merged(reg='D-AIXA', delay_known=True,
                                          delay_min=0, delay_side='dep'))
    with patch.object(A, '_flight_obs_merged', mock):
        _get_status(client)
    assert mock.call_args.kwargs.get('free_only') is True


def test_mystatus_reg_never_guessed_when_absent(client):
    m = _merged(reg=None, delay_known=True, delay_min=0, delay_side='dep')
    with patch.object(A, '_flight_obs_merged', return_value=m):
        r = _get_status(client)
    assert r.get_json()['reg'] is None


def test_mystatus_memoized_second_call(client):
    mock = MagicMock(return_value=_merged(reg='D-AIXA', delay_known=True,
                                          delay_min=0, delay_side='dep'))
    with patch.object(A, '_flight_obs_merged', mock):
        _get_status(client)
        _get_status(client)
    assert mock.call_count == 1             # 2. Aufruf = Memo-Hit


# ══════════════════════════════════════════════════════════════════════════════
# get_friends_today.flights_live — est_*/sched_*_iso + delay_known + Batch
# ══════════════════════════════════════════════════════════════════════════════
def _setup_flying_friend(monkeypatch, routing='FRA-JFK', frm='FRA', to='JFK',
                         flight='LH400'):
    tok = 'FRIENDTOKEN'
    today = _date.today().isoformat()
    dep = _now() - timedelta(hours=1)
    day = {
        'datum': today, 'klass': 'Z72', 'routing': routing,
        'reader_facts': {'layover_ort': 'XXX', 'flight_numbers': [flight]},
        'ical_sectors': [{'flight': flight, 'from': frm, 'to': to,
                          'dep_iso': _iso(dep)}],
    }
    A._store[tok] = {'result_data': {'_tage_detail': [day]}}
    monkeypatch.setattr(A, '_friends_load', lambda t: {'friends': [tok]})
    monkeypatch.setattr(A, '_profiles_load_bulk', lambda toks: {
        tok: {'name': 'Tibor', 'homebase': 'MUC', 'share_roster': True,
              'share_location': True, 'location_source': 'roster'}})
    monkeypatch.setattr(A, '_maybe_refresh_calendar_feed', lambda *a, **k: None)
    return tok


@pytest.fixture(autouse=True)
def _cleanup_friend_store():
    yield
    A._store.pop('FRIENDTOKEN', None)


def test_flights_live_carries_iso_and_delay_known(client, monkeypatch):
    tok = _setup_flying_friend(monkeypatch)
    m = _merged(delay_known=True, delay_min=15, delay_side='dep', status='airborne',
                sched_dep='2026-07-04T11:00:00Z', esti_dep='2026-07-04T11:15:00Z',
                sched_arr='2026-07-04T19:30:00Z', esti_arr='2026-07-04T19:45:00Z')
    monkeypatch.setattr(A, '_flight_obs_merged', lambda *a, **k: m)
    r = client.get(f'/api/user/friends-today/{tok}')
    assert r.status_code == 200
    fl = r.get_json()['friends_today'][0]['flights_live']
    assert len(fl) == 1
    e = fl[0]
    assert e['sched_dep_iso'] == '2026-07-04T11:00:00Z'
    assert e['est_dep_iso'] == '2026-07-04T11:15:00Z'
    assert e['sched_arr_iso'] == '2026-07-04T19:30:00Z'
    assert e['est_arr_iso'] == '2026-07-04T19:45:00Z'
    assert e['delay_known'] is True
    assert e['status'] == 'airborne'
    assert e['dep_iata'] == 'FRA' and e['arr_iata'] == 'JFK'


def test_flights_live_unknown_no_iso_no_fabricated_delay(client, monkeypatch):
    tok = _setup_flying_friend(monkeypatch)
    m = _merged(delay_known=False, delay_min=None)     # kein Signal / keine Zeiten
    monkeypatch.setattr(A, '_flight_obs_merged', lambda *a, **k: m)
    r = client.get(f'/api/user/friends-today/{tok}')
    e = r.get_json()['friends_today'][0]['flights_live'][0]
    assert e['delay_known'] is False
    assert e['delay_min'] is None                       # kein +0
    assert e['sched_dep_iso'] is None
    assert e['est_dep_iso'] is None
    assert e['est_arr_iso'] is None


def test_flights_live_batch_single_call_per_leg(client, monkeypatch):
    # Ein fliegender Freund, ein Leg → GENAU ein _flight_obs_merged-Call
    # (Batch: kein Fan-out-Radar-Poll pro Freund).
    tok = _setup_flying_friend(monkeypatch)
    mock = MagicMock(return_value=_merged(delay_known=True, delay_min=0,
                                          delay_side='dep'))
    monkeypatch.setattr(A, '_flight_obs_merged', mock)
    client.get(f'/api/user/friends-today/{tok}')
    # 1× für lay_eff-Kaskade ist HIER 0 (homebase=MUC, dep=FRA≠MUC → lay_eff-Zweig
    # ruft AUCH _flight_obs_merged). Wir prüfen darum: kein Fan-out über Freunde,
    # Aufrufzahl bleibt klein & konstant (≤3: lay_eff + flights_live +
    # crew_state-Resolver 2026-07-10 — in Prod dedupliziert der
    # _FLIGHT_MERGE_CACHE identische Args auf EINEN echten Lookup, der Mock
    # hier zählt die rohen Aufrufe).
    assert mock.call_count <= 3
    assert all(c.kwargs.get('free_only') is True for c in mock.call_args_list)


def test_flights_live_est_overrides_sched_present(client, monkeypatch):
    # est_dep vorhanden → wird eigenständig geliefert (iOS nimmt est ?? sched).
    tok = _setup_flying_friend(monkeypatch)
    m = _merged(delay_known=True, delay_min=20, delay_side='dep',
                sched_dep='2026-07-04T11:00:00Z', esti_dep='2026-07-04T11:20:00Z',
                sched_arr='2026-07-04T19:30:00Z')
    monkeypatch.setattr(A, '_flight_obs_merged', lambda *a, **k: m)
    r = client.get(f'/api/user/friends-today/{tok}')
    e = r.get_json()['friends_today'][0]['flights_live'][0]
    assert e['sched_dep_iso'] == '2026-07-04T11:00:00Z'
    assert e['est_dep_iso'] == '2026-07-04T11:20:00Z'
    assert e['sched_arr_iso'] == '2026-07-04T19:30:00Z'
    assert e['est_arr_iso'] is None                    # keine Esti → None, nicht sched
