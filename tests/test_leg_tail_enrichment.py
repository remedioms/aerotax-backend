"""Tail-/Kennzeichen-Anreicherung pro Leg (Owner 2026-07-04:
„Tails auf jedem Leg im Kalender bei Crew UND Freunde").

Deckt ab:
  • _leg_tail            — Board/Warehouse-reg NUR bei echtem Treffer, sonst None;
                           fn-Normalisierung + Codeshare-Faltung; free_only (kein
                           AeroDataBox-Spend); nie erfunden.
  • _enrich_leg_tails    — Pro-Leg-`tail` für Freunde-/Familien-Sektoren, additiv,
                           mit Zeitfenster-Guard.
  • _enrich_leg_delays   — setzt `tail` als Alias auf das gemessene reg (eigener
                           Roster), lässt es weg wenn kein reg.
  • get_friend_roster    — Freunde-Sheet trägt jetzt `tail` pro Leg, Privacy-Gate
                           (Freundschaft) unberührt.

KEIN echter Netz-/DB-Zugriff: _flight_obs_merged / Loader werden gemockt.
free_only=True darf NIE eine bezahlte Quelle treffen.
"""
import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

from datetime import datetime, timezone, timedelta, date as _date
from unittest.mock import patch, MagicMock

import pytest

import app as A


@pytest.fixture(autouse=True)
def _clear_caches():
    # SYS.MODULES-PIN (gleiche Order-Kontamination wie test_my_flight_status,
    # 2026-07-05): test_calculation.py tauscht sys.modules['app'] per Reimport aus;
    # Blueprints (family-watch _load_crew_roster_days u.a.) lösen app-Funktionen
    # zur CALL-Zeit über sys.modules['app'] auf → patch.object(A, …) lief nach
    # test_calculation ins Leere (isoliert grün, Full-Run rot). Pro Test unser
    # A-Modul pinnen, danach vorherigen Zustand wiederherstellen.
    import sys
    _prev_app_mod = sys.modules.get('app')
    sys.modules['app'] = A
    A._FLIGHT_MERGE_CACHE.clear()
    A._AX_CODESHARE_CACHE['ts'] = 0.0
    A._AX_CODESHARE_CACHE['map'] = {}
    yield
    A._FLIGHT_MERGE_CACHE.clear()
    if _prev_app_mod is not None:
        sys.modules['app'] = _prev_app_mod


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _now():
    return datetime.now(timezone.utc)


def _sector(flight='LH400', frm='FRA', to='MUC', dep_iso=None, **extra):
    s = {'flight': flight, 'from': frm, 'to': to}
    if dep_iso is not None:
        s['dep_iso'] = dep_iso
    s.update(extra)
    return s


def _merged(reg=None, delay_known=False):
    return {'ok': True, 'delay_known': delay_known, 'reg': reg,
            'delay_min': None, 'delay_side': None, 'dep_delay_min': None,
            'arr_delay_min': None, 'status': None, 'cancelled': False,
            'esti_dep': None, 'esti_arr': None,
            'sides': {'dep': None, 'arr': None}}


# ══════════════════════════════════════════════════════════════════════════════
# _leg_tail
# ══════════════════════════════════════════════════════════════════════════════
def test_leg_tail_returns_reg_on_hit():
    with patch.object(A, '_flight_obs_merged', return_value=_merged(reg='D-AIXY')):
        assert A._leg_tail('LH400', date='2026-07-04', dep_iata='FRA',
                           arr_iata='MUC') == 'D-AIXY'


def test_leg_tail_none_when_no_merge():
    with patch.object(A, '_flight_obs_merged', return_value=None):
        assert A._leg_tail('LH400', date='2026-07-04', dep_iata='FRA',
                           arr_iata='MUC') is None


def test_leg_tail_none_when_reg_absent():
    # Treffer, aber KEIN Kennzeichen bekannt → None (nie erfunden).
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(reg=None, delay_known=True)):
        assert A._leg_tail('LH400', date='2026-07-04', dep_iata='FRA',
                           arr_iata='MUC') is None


def test_leg_tail_empty_string_reg_is_none():
    with patch.object(A, '_flight_obs_merged', return_value=_merged(reg='   ')):
        assert A._leg_tail('LH400', dep_iata='FRA', arr_iata='MUC') is None


def test_leg_tail_strips_whitespace():
    with patch.object(A, '_flight_obs_merged', return_value=_merged(reg='  D-AIXY ')):
        assert A._leg_tail('LH400', dep_iata='FRA', arr_iata='MUC') == 'D-AIXY'


def test_leg_tail_fn_normalized():
    mock = MagicMock(return_value=_merged(reg='D-AIXY'))
    with patch.object(A, '_flight_obs_merged', mock):
        A._leg_tail('LH0839', date='2026-07-04', dep_iata='FRA', arr_iata='JFK')
    assert mock.call_args.args[0] == 'LH839'


def test_leg_tail_codeshare_folds_to_operating():
    mock = MagicMock(return_value=_merged(reg='D-AIXY'))
    with patch.object(A, '_ax_codeshare_map', return_value={'UA8841': 'LH400'}), \
            patch.object(A, '_flight_obs_merged', mock):
        A._leg_tail('UA8841', date='2026-07-04', dep_iata='FRA', arr_iata='MUC')
    assert mock.call_args.args[0] == 'LH400'


def test_leg_tail_forwards_free_only_true():
    mock = MagicMock(return_value=_merged(reg='D-AIXY'))
    with patch.object(A, '_flight_obs_merged', mock):
        A._leg_tail('LH400', dep_iata='FRA', arr_iata='MUC')
    assert mock.call_args.kwargs.get('free_only') is True


def test_leg_tail_short_fn_returns_none_without_call():
    with patch.object(A, '_flight_obs_merged',
                      side_effect=AssertionError('should not be called')):
        assert A._leg_tail('LH', dep_iata='FRA', arr_iata='MUC') is None


def test_leg_tail_never_raises_on_resolver_error():
    with patch.object(A, '_flight_obs_merged', side_effect=RuntimeError('boom')):
        assert A._leg_tail('LH400', dep_iata='FRA', arr_iata='MUC') is None


def test_leg_tail_free_only_never_calls_paid_board():
    # Realer _flight_obs_merged mit free_only=True darf NIE die bezahlte Quelle treffen.
    with patch.object(A, '_flight_from_free_board',
                      return_value={'reg': 'D-AIXY', 'delay_min': 0,
                                    'delay_known': True, 'dest_iata': 'MUC'}), \
            patch.object(A, '_flight_from_live_board',
                         MagicMock(side_effect=AssertionError('paid board!'))), \
            patch.object(A, '_departed_rows_from_store', return_value=[]):
        assert A._leg_tail('LH400', date=None, dep_iata='FRA',
                           arr_iata='MUC') == 'D-AIXY'


# ══════════════════════════════════════════════════════════════════════════════
# _enrich_leg_tails (Freunde/Familie)
# ══════════════════════════════════════════════════════════════════════════════
def test_enrich_tails_sets_tail_on_hit():
    secs = [_sector(date=_date.today().isoformat())]
    with patch.object(A, '_leg_tail', return_value='D-AIXY'):
        A._enrich_leg_tails(secs, _date.today().isoformat())
    assert secs[0]['tail'] == 'D-AIXY'


def test_enrich_tails_no_hit_leaves_field_absent():
    secs = [_sector(date=_date.today().isoformat())]
    with patch.object(A, '_leg_tail', return_value=None):
        A._enrich_leg_tails(secs, _date.today().isoformat())
    assert 'tail' not in secs[0]


def test_enrich_tails_backward_compatible_keys():
    # Kein Treffer → NUR die ursprünglichen Keys (additiv/abwärtskompatibel).
    secs = [_sector(date=_date.today().isoformat())]
    orig = set(secs[0].keys())
    with patch.object(A, '_leg_tail', return_value=None):
        A._enrich_leg_tails(secs, _date.today().isoformat())
    assert set(secs[0].keys()) == orig


def test_enrich_tails_respects_preset_tail_no_refetch():
    secs = [_sector(date=_date.today().isoformat(), tail='D-EXIST')]
    with patch.object(A, '_leg_tail',
                      side_effect=AssertionError('should not refetch')):
        A._enrich_leg_tails(secs, _date.today().isoformat())
    assert secs[0]['tail'] == 'D-EXIST'


def test_enrich_tails_future_leg_skipped():
    far = (_date.today() + timedelta(days=3)).isoformat()
    secs = [_sector(date=far)]
    with patch.object(A, '_leg_tail',
                      side_effect=AssertionError('should not scan deep future')):
        A._enrich_leg_tails(secs, far)
    assert 'tail' not in secs[0]


def test_enrich_tails_deep_past_leg_skipped():
    past = (_date.today() - timedelta(days=3)).isoformat()
    secs = [_sector(date=past)]
    with patch.object(A, '_leg_tail',
                      side_effect=AssertionError('should not scan deep past')):
        A._enrich_leg_tails(secs, past)
    assert 'tail' not in secs[0]


def test_enrich_tails_dep_iso_over_27h_skipped():
    dep = _now() + timedelta(hours=30)
    secs = [_sector(dep_iso=_iso(dep))]
    with patch.object(A, '_leg_tail',
                      side_effect=AssertionError('should not scan far future')):
        A._enrich_leg_tails(secs, dep.strftime('%Y-%m-%d'))
    assert 'tail' not in secs[0]


def test_enrich_tails_near_future_enriched():
    dep = _now() + timedelta(hours=3)
    secs = [_sector(dep_iso=_iso(dep))]
    with patch.object(A, '_leg_tail', return_value='D-AIXY'):
        A._enrich_leg_tails(secs, dep.strftime('%Y-%m-%d'))
    assert secs[0]['tail'] == 'D-AIXY'


def test_enrich_tails_bad_iata_skipped():
    secs = [_sector(frm='FRANKFURT', date=_date.today().isoformat())]
    with patch.object(A, '_leg_tail',
                      side_effect=AssertionError('should not be called')):
        A._enrich_leg_tails(secs, _date.today().isoformat())
    assert 'tail' not in secs[0]


def test_enrich_tails_short_fn_skipped():
    secs = [_sector(flight='LH', date=_date.today().isoformat())]
    with patch.object(A, '_leg_tail',
                      side_effect=AssertionError('should not be called')):
        A._enrich_leg_tails(secs, _date.today().isoformat())
    assert 'tail' not in secs[0]


def test_enrich_tails_multi_leg_independent():
    today = _date.today().isoformat()
    secs = [_sector(flight='LH1', frm='FRA', to='MUC', date=today),
            _sector(flight='LH2', frm='MUC', to='VIE', date=today)]

    def _fake(fn, **kw):
        return {'LH1': 'D-AAAA', 'LH2': None}[fn]
    with patch.object(A, '_leg_tail', side_effect=_fake):
        A._enrich_leg_tails(secs, today)
    assert secs[0]['tail'] == 'D-AAAA'
    assert 'tail' not in secs[1]


def test_enrich_tails_passes_leg_fields_to_helper():
    today = _date.today().isoformat()
    secs = [_sector(flight='LH400', frm='FRA', to='JFK', date=today)]
    mock = MagicMock(return_value='D-AIXY')
    with patch.object(A, '_leg_tail', mock):
        A._enrich_leg_tails(secs, today)
    assert mock.call_args.args[0] == 'LH400'
    assert mock.call_args.kwargs.get('dep_iata') == 'FRA'
    assert mock.call_args.kwargs.get('arr_iata') == 'JFK'
    assert mock.call_args.kwargs.get('date') == today


def test_enrich_tails_empty_and_non_list_safe():
    assert A._enrich_leg_tails([], _date.today().isoformat()) == []
    assert A._enrich_leg_tails(None, _date.today().isoformat()) is None
    assert A._enrich_leg_tails('nope', _date.today().isoformat()) == 'nope'


def test_enrich_tails_non_dict_element_safe():
    today = _date.today().isoformat()
    secs = ['garbage', _sector(flight='LH400', date=today)]
    with patch.object(A, '_leg_tail', return_value='D-AIXY'):
        A._enrich_leg_tails(secs, today)
    assert secs[1]['tail'] == 'D-AIXY'


def test_enrich_tails_returns_same_list_in_place():
    today = _date.today().isoformat()
    secs = [_sector(date=today)]
    with patch.object(A, '_leg_tail', return_value='D-AIXY'):
        out = A._enrich_leg_tails(secs, today)
    assert out is secs


# ══════════════════════════════════════════════════════════════════════════════
# _enrich_leg_delays — eigener Roster reicht reg als tail durch
# ══════════════════════════════════════════════════════════════════════════════
def test_delays_sets_tail_alias_from_reg():
    secs = [_sector()]
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(reg='D-AIXY', delay_known=True)):
        A._enrich_leg_delays(secs, _date.today().isoformat())
    assert secs[0]['tail'] == 'D-AIXY'
    assert secs[0]['reg'] == 'D-AIXY'


def test_delays_no_tail_when_reg_absent():
    secs = [_sector()]
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(reg=None, delay_known=True)):
        A._enrich_leg_delays(secs, _date.today().isoformat())
    assert 'tail' not in secs[0]


def test_delays_no_signal_no_tail():
    secs = [_sector()]
    with patch.object(A, '_flight_obs_merged', return_value=None):
        A._enrich_leg_delays(secs, _date.today().isoformat())
    assert 'tail' not in secs[0]


# ══════════════════════════════════════════════════════════════════════════════
# get_friend_roster — Freunde-Sheet trägt tail, Privacy unberührt
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture
def client():
    A.app.testing = True
    return A.app.test_client()


def test_friend_roster_carries_tail(client, monkeypatch):
    tok = 'MYTOKEN'
    friend = 'FRIENDTOK'
    today = _date.today().isoformat()
    day = {
        'datum': today,
        'klass': 'Z72',
        'routing': 'FRA-MUC',
        'reader_facts': {'layover_ort': 'MUC'},
        'ical_sectors': [_sector(flight='LH100', frm='FRA', to='MUC')],
    }
    A._store[friend] = {'result_data': {'_tage_detail': [day]}}
    monkeypatch.setattr(A, '_friends_load', lambda t: {'friends': [friend]})
    monkeypatch.setattr(A, '_maybe_refresh_calendar_feed', lambda *a, **k: None)
    try:
        with patch.object(A, '_flight_obs_merged',
                          return_value=_merged(reg='D-AIMM', delay_known=True)):
            r = client.get(f'/api/user/friend-roster/{tok}/{friend}')
        assert r.status_code == 200
        days = r.get_json()['days']
        assert days[0]['ical_sectors'][0]['tail'] == 'D-AIMM'
    finally:
        A._store.pop(friend, None)


def test_friend_roster_no_tail_when_no_board(client, monkeypatch):
    tok = 'MYTOKEN'
    friend = 'FRIENDTOK'
    today = _date.today().isoformat()
    day = {
        'datum': today, 'klass': 'Z72', 'routing': 'FRA-MUC',
        'reader_facts': {'layover_ort': 'MUC'},
        'ical_sectors': [_sector(flight='LH100', frm='FRA', to='MUC')],
    }
    A._store[friend] = {'result_data': {'_tage_detail': [day]}}
    monkeypatch.setattr(A, '_friends_load', lambda t: {'friends': [friend]})
    monkeypatch.setattr(A, '_maybe_refresh_calendar_feed', lambda *a, **k: None)
    try:
        with patch.object(A, '_flight_obs_merged', return_value=None):
            r = client.get(f'/api/user/friend-roster/{tok}/{friend}')
        assert r.status_code == 200
        assert 'tail' not in r.get_json()['days'][0]['ical_sectors'][0]
    finally:
        A._store.pop(friend, None)


def test_family_roster_carries_tail(monkeypatch):
    # Familien-Pfad (Blueprint _load_crew_roster_days) reicht denselben tail durch.
    from blueprints import family_watch as FW
    crew = 'CREWTOK'
    today = _date.today().isoformat()
    day = {
        'datum': today, 'klass': 'Z72', 'routing': 'FRA-MUC',
        'reader_facts': {'layover_ort': 'MUC'},
        'ical_sectors': [_sector(flight='LH100', frm='FRA', to='MUC')],
    }
    A._store[crew] = {'result_data': {'_tage_detail': [day]}}
    try:
        with patch.object(A, '_flight_obs_merged',
                          return_value=_merged(reg='D-AIMM', delay_known=True)):
            days = FW._load_crew_roster_days(crew, 60)
        assert days[0]['ical_sectors'][0]['tail'] == 'D-AIMM'
    finally:
        A._store.pop(crew, None)


def test_friend_roster_not_friends_403_no_tail_call(client, monkeypatch):
    # Privacy-Gate: keine Freundschaft → 403, kein Roster, kein Tail-Fetch.
    monkeypatch.setattr(A, '_friends_load', lambda t: {'friends': []})
    with patch.object(A, '_leg_tail',
                      side_effect=AssertionError('no roster leak')):
        r = client.get('/api/user/friend-roster/MYTOKEN/STRANGER')
    assert r.status_code == 403
    assert r.get_json()['shared'] is False


# ══════════════════════════════════════════════════════════════════════════════
# _carry_forward_turnaround_tails  (Owner 2026-07-04: nur Outstation-Return,
# NIE am Homebase-Hub — dort liefert das Abflugtafel-Scraping den echten Tail)
# ══════════════════════════════════════════════════════════════════════════════
def _leg(flight, frm, to, tail=None, dep=None, arr=None):
    s = {'flight': flight, 'from': frm, 'to': to}
    if tail is not None:
        s['tail'] = tail
    if dep is not None:
        s['dep_iso'] = dep
    if arr is not None:
        s['arr_iso'] = arr
    return s


def test_carry_forward_outstation_return_inherits_tail():
    # FRA→TIA (Tail beobachtet), Turnaround in TIA (Outstation), TIA→FRA zurück.
    secs = [
        _leg('LH1380', 'FRA', 'TIA', tail='D-AIMM',
             dep='2026-07-04T06:00:00Z', arr='2026-07-04T08:00:00Z'),
        _leg('LH1381', 'TIA', 'FRA', tail=None,
             dep='2026-07-04T09:00:00Z', arr='2026-07-04T11:00:00Z'),
    ]
    A._carry_forward_turnaround_tails(secs)
    assert secs[1].get('tail') == 'D-AIMM'
    assert secs[1].get('tail_inferred') is True


def test_carry_forward_skips_homebase_turnaround():
    # X→FRA (Tail beobachtet), dann FRA→X: Turnaround AN der Homebase (FRA=Duty-
    # Origin-Proxy). Selbst kurze Bodenzeit → Flieger kann wechseln → NICHT erben.
    secs = [
        _leg('LH11', 'FRA', 'MUC', tail='D-AAAA',
             dep='2026-07-04T06:00:00Z', arr='2026-07-04T07:00:00Z'),
        _leg('LH12', 'MUC', 'FRA', tail='D-BBBB',
             dep='2026-07-04T08:00:00Z', arr='2026-07-04T09:00:00Z'),
        _leg('LH13', 'FRA', 'MUC', tail=None,   # Turnaround an FRA (Homebase)
             dep='2026-07-04T11:00:00Z', arr='2026-07-04T12:00:00Z'),
    ]
    A._carry_forward_turnaround_tails(secs)
    assert 'tail' not in secs[2], "Homebase-Turnaround darf NICHT erben"


def test_carry_forward_explicit_homebase_param_wins_over_proxy():
    # Duty-Origin ist MUC, aber echte Homebase per Param = MUC → MUC-Turnaround skip.
    secs = [
        _leg('LH20', 'MUC', 'HAM', tail='D-CCCC',
             dep='2026-07-04T06:00:00Z', arr='2026-07-04T07:00:00Z'),
        _leg('LH21', 'HAM', 'MUC', tail=None,   # zurück nach MUC = Homebase
             dep='2026-07-04T08:00:00Z', arr='2026-07-04T09:00:00Z'),
    ]
    # Return endet an der Homebase MUC, Turnaround-Airport ist aber HAM (≠MUC) →
    # das ist ein echter Outstation-Return → erbt trotzdem.
    A._carry_forward_turnaround_tails(secs, homebase='MUC')
    assert secs[1].get('tail') == 'D-CCCC'


def test_carry_forward_skips_hub_onward_flight():
    # FRA→TIA, TIA→SKP (kein Raus-&-Zurück, Weiterflug) → kein sicherer Turnaround.
    secs = [
        _leg('LH1380', 'FRA', 'TIA', tail='D-AIMM',
             dep='2026-07-04T06:00:00Z', arr='2026-07-04T08:00:00Z'),
        _leg('LH1500', 'TIA', 'SKP', tail=None,
             dep='2026-07-04T09:00:00Z', arr='2026-07-04T10:00:00Z'),
    ]
    A._carry_forward_turnaround_tails(secs)
    assert 'tail' not in secs[1]


def test_carry_forward_skips_overnight_layover():
    # Outstation-Return, aber >4h Bodenzeit (Übernacht) → Flieger wechselt → skip.
    secs = [
        _leg('LH1380', 'FRA', 'TIA', tail='D-AIMM',
             dep='2026-07-03T18:00:00Z', arr='2026-07-03T20:00:00Z'),
        _leg('LH1381', 'TIA', 'FRA', tail=None,
             dep='2026-07-04T08:00:00Z', arr='2026-07-04T10:00:00Z'),
    ]
    A._carry_forward_turnaround_tails(secs)
    assert 'tail' not in secs[1]


# ══════════════════════════════════════════════════════════════════════════════
# _crowdsource_flight_obs  (Owner 2026-07-04: echte Route+Tail zurück ins
# Warehouse → nächster Lookup gratis; NUR echte reg, Pünktlichkeit ehrlich)
# ══════════════════════════════════════════════════════════════════════════════
def test_crowdsource_writes_paid_flight_with_reg():
    captured = {}
    def _fake_wt(date_str, fn, hhmm, max_delay, cancelled, airport,
                 status, meta=None, requeue_on_fail=True):
        captured.update(date=date_str, fn=fn, hhmm=hhmm, airport=airport,
                        meta=meta or {})
        return True
    flight = {'flight': 'LH400', 'reg': 'D-AIMM', 'dep_iata': 'FRA',
              'arr_iata': 'JFK', 'arr_name': 'New York', 'airline': 'LH',
              'sched_dep': '2026-07-04T10:25:00+02:00', 'status': 'Departed',
              'est_dep': '2026-07-04T10:40:00+02:00', 'dep_delay_min': 15,
              'aircraft': 'A350'}
    with patch.object(A, '_delay_obs_write_through', side_effect=_fake_wt):
        ok = A._crowdsource_flight_obs(flight, '2026-07-04', source='aerodatabox')
    assert ok is True
    assert captured['fn'] == 'LH400'
    assert captured['airport'] == 'FRA'
    assert captured['hhmm'] == '1025'
    assert captured['date'] == '2026-07-04'
    assert captured['meta']['reg'] == 'D-AIMM'
    assert captured['meta']['source'] == 'aerodatabox'
    assert captured['meta']['dest_iata'] == 'JFK'


def test_crowdsource_skips_without_reg():
    flight = {'flight': 'LH400', 'reg': '', 'dep_iata': 'FRA', 'arr_iata': 'JFK',
              'sched_dep': '2026-07-04T10:25:00+02:00'}
    with patch.object(A, '_delay_obs_write_through',
                      side_effect=AssertionError('must not write without reg')):
        assert A._crowdsource_flight_obs(flight, '2026-07-04') is False


def test_crowdsource_live_reg_only_is_delay_unknown():
    # Live-ADS-B Fall: nur Reg, keine Zeiten → write_through mit max_delay=0,
    # status=None; die Read-Seite (_obs_delay_known) darf das NICHT als pünktlich zählen.
    captured = {}
    def _fake_wt(date_str, fn, hhmm, max_delay, cancelled, airport,
                 status, meta=None, requeue_on_fail=True):
        captured.update(max_delay=max_delay, status=status, meta=meta or {})
        return True
    flight = {'flight': 'LH400', 'reg': 'D-AIMM', 'dep_iata': 'FRA',
              'arr_iata': 'JFK', 'sched_dep': None, 'status': None,
              'dep_delay_min': None}
    with patch.object(A, '_delay_obs_write_through', side_effect=_fake_wt):
        A._crowdsource_flight_obs(flight, '2026-07-04', source='live_adsb')
    assert captured['max_delay'] == 0
    assert captured['status'] is None
    # honest: this row must read as delay-UNKNOWN, not on-time
    assert A._obs_delay_known(captured['max_delay'], False, None, None, False) is False


def test_crowdsource_bad_input_never_raises():
    assert A._crowdsource_flight_obs(None) is False
    assert A._crowdsource_flight_obs({}) is False
    assert A._crowdsource_flight_obs({'reg': 'D-AIMM'}) is False  # no flight/dep
