"""LH-Overlay für den ZWEITEN Board-Leser (`_flight_obs_merged`, Engine A2
2026-07-22): der pure Shape-Adapter `_lh_apply_obs_fill` + die budget-gated
Hülle `_lh_fill_obs_merged`. Rein offline — LH wird gemonkeypatcht."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone

import app as A
from blueprints import lh_open_api

TODAY = datetime.now(timezone.utc).strftime('%Y-%m-%d')


LH = {  # Shape wie _obs_rows_to_facts / lh_flight_facts
    'sched_dep': '2026-07-22T10:55:00+02:00',
    'est_dep': '2026-07-22T11:05:00+02:00',
    'dep_delay_min': 10,
    'gate': 'Z16', 'terminal': '1',
    'sched_arr': '2026-07-22T13:35:00-04:00',
    'est_arr': '2026-07-22T13:03:00-04:00',
    'arr_delay_min': -32,
    'reg': 'D-AIHY', 'type': '346',
    'dep_iata': 'FRA', 'arr_iata': 'JFK',
}


def test_apply_fill_only_never_overwrites_board():
    rec = {'sched_dep': '10:55', 'esti_dep': '11:20', 'gate_dep': None,
           'sched_arr': None, 'esti_arr': None, 'arr_delay_min': None,
           'delay_min': None, 'delay_side': None, 'delay_known': False,
           'reg': None, 'aircraft': None, 'est_dep_iso': None,
           'est_arr_iso': None}
    out = A._lh_apply_obs_fill(rec, LH)
    # Board-Werte bleiben (Format-Stabilität!), Lücken kommen von LH.
    assert out['sched_dep'] == '10:55' and out['esti_dep'] == '11:20'
    assert out['gate_dep'] == 'Z16'
    assert out['sched_arr'] == LH['sched_arr']
    assert out['reg'] == 'D-AIHY' and out['aircraft'] == '346'
    assert out['lh'] is True


def test_apply_fill_builds_pure_lh_record_from_none():
    out = A._lh_apply_obs_fill(None, LH)
    assert out is not None
    assert out['sched_dep'] == LH['sched_dep']
    assert out['gate_dep'] == 'Z16' and out['gate_arr'] is None
    # Offset-ISO → absolute UTC (KEIN Doppel-Offset)
    assert out['est_dep_iso'].startswith('2026-07-22T09:05')
    assert out['est_arr_iso'].startswith('2026-07-22T17:03')
    # beste Ein-Zahl: arr gewinnt
    assert out['delay_min'] == -32 and out['delay_side'] == 'arr'
    assert out['delay_known'] is True


def test_apply_fill_reicht_echte_ist_zeiten_durch():
    """LHs `ActualTimeUTC` ist der EINZIGE Wert im Merge, der eine MESSUNG
    behauptet — `esti_*` ist auch nach der Landung nur die letzte Tafel-Zahl.
    Ohne dieses Durchreichen kann `obs_from_board_merged` kein `arr_time.actual`
    erzeugen und `eta_conf` bleibt strukturell für immer `estimated`."""
    lh = dict(LH, actual_dep='2026-07-22T11:05:00+02:00',
              actual_arr='2026-07-22T13:03:00-04:00')
    out = A._lh_apply_obs_fill(None, lh)
    # Offset-ISO → absolute UTC (kein Doppel-Offset, wie bei est_*_iso)
    assert out['actual_dep_iso'].startswith('2026-07-22T09:05')
    assert out['actual_arr_iso'].startswith('2026-07-22T17:03')


def test_apply_fill_ohne_actual_bleibt_leer():
    """Nur Estimated ⇒ KEINE Ist-Zeit. Ein est als actual auszugeben wäre genau
    der Etikettenschwindel, den die Kette abstellen soll."""
    out = A._lh_apply_obs_fill(None, LH)
    assert out['actual_dep_iso'] is None and out['actual_arr_iso'] is None
    # …und der Board-Record bekommt ohne LH-Actual auch keinen Schlüssel dazu.
    rec = {'sched_dep': '10:55', 'esti_dep': '11:20'}
    assert A._lh_apply_obs_fill(rec, LH).get('actual_arr_iso') is None


def test_apply_fill_empty_lh_keeps_none():
    assert A._lh_apply_obs_fill(None, {}) is None
    rec = {'sched_dep': '10:55'}
    assert A._lh_apply_obs_fill(rec, {}) is rec


def test_gate_skips_lh_when_board_complete(monkeypatch):
    calls = []
    monkeypatch.setattr(lh_open_api, 'lh_flight_facts',
                        lambda *a, **k: calls.append(a) or dict(LH))
    complete = {'sched_dep': '10:55', 'sched_arr': '13:35',
                'esti_arr': '13:03', 'arr_delay_min': -32}
    out = A._lh_fill_obs_merged(complete, 'LH400', TODAY, 'FRA', 'JFK')
    assert out is complete and not calls  # kein Budget-Verbrauch


def test_gate_fetches_for_gappy_board(monkeypatch):
    calls = []
    monkeypatch.setattr(lh_open_api, 'lh_flight_facts',
                        lambda *a, **k: calls.append(a) or dict(LH))
    gappy = {'sched_dep': '10:55', 'sched_arr': None, 'esti_arr': None,
             'arr_delay_min': None, 'delay_min': None, 'delay_side': None,
             'delay_known': False, 'reg': None, 'aircraft': None,
             'gate_dep': None, 'est_arr_iso': None, 'est_dep_iso': None,
             'esti_dep': None, 'dep_delay_min': None, 'terminal_dep': None,
             'gate_arr': None, 'terminal_arr': None, 'dep_iata': None,
             'arr_iata': None, 'cancelled': None}
    out = A._lh_fill_obs_merged(gappy, 'LH400', TODAY, 'FRA', 'JFK')
    assert calls and out['sched_arr'] == LH['sched_arr']
    assert out['sched_dep'] == '10:55'


def test_gate_ignores_non_group(monkeypatch):
    monkeypatch.setattr(lh_open_api, 'lh_flight_facts',
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError('kein LH-Call für Nicht-Group')))
    assert A._lh_fill_obs_merged(None, 'UA900', TODAY, None, None) is None


def test_pure_lh_record_carries_identity(monkeypatch):
    monkeypatch.setattr(lh_open_api, 'lh_flight_facts',
                        lambda *a, **k: dict(LH))
    out = A._lh_fill_obs_merged(None, 'LH400', TODAY, 'FRA', 'JFK')
    assert out['flight'] == 'LH400' and out['date'] == TODAY
    assert out['dep_iata'] == 'FRA' and out['arr_iata'] == 'JFK'


def test_gate_skips_far_and_historic_dates(monkeypatch):
    # Incident 2026-07-22: Historie-Iterationen (Logbuch) feuerten pro Tag
    # einen LH-Call → nur heute−1…+2 darf überhaupt LH sehen.
    monkeypatch.setattr(lh_open_api, 'lh_flight_facts',
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError('historische Tage rufen LH nie')))
    assert A._lh_fill_obs_merged(None, 'LH400', '2026-07-04', 'FRA', None) is None
    assert A._lh_fill_obs_merged(None, 'LH400', '2099-01-01', 'FRA', None) is None


def test_fill_uses_cached_only(monkeypatch):
    seen = {}
    def fake(fn, d, dep, arr, force=False, cached_only=False, caller=None):
        seen['cached_only'] = cached_only
        return dict(LH)
    monkeypatch.setattr(lh_open_api, 'lh_flight_facts', fake)
    A._lh_fill_obs_merged(None, 'LH400', TODAY, 'FRA', 'JFK')
    assert seen.get('cached_only') is True
