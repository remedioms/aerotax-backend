"""Tests für den Crew-Pünktlichkeits-Score (D15-Monats-OTP).

Deckt _crew_flights_for_month, _member_monthly_punctuality,
_crew_punctuality_leaderboard, die Rang-/Persistenz-Helper und den
GET /api/ax/punctuality/<token>-Endpoint ab.

DOMÄNEN-INVARIANTEN (siehe app.py):
  • Delay wird NIE erfunden — arr_delay_min None ⇒ no_signal, nie 0/pünktlich.
  • cancelled ist separater Counter, nie in on_time/delayed/sample.
  • Mindest-Stichprobe min_sample=3 gewertete Flüge, sonst insufficient_sample.
  • Betriebstag/Monatszuordnung über dep_iso (UTC).
  • Flugnummern via _fn_norm normalisieren (LH0839 == LH839).
Alle DB-/Warehouse-Zugriffe sind gemockt.
"""
import os
import sys
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import app as A  # noqa: E402


# ════════════════════════════════════════════════════════════════════
# Helfer
# ════════════════════════════════════════════════════════════════════

def _sector(flight, dep_iso, frm='FRA', to='JFK', arr_iso=None):
    return {'flight': flight, 'from': frm, 'to': to,
            'dep_iso': dep_iso, 'arr_iso': arr_iso or dep_iso}


def _roster(*days):
    """days = list of (datum, [sectors])."""
    return {'tage': [{'datum': d, 'ical_sectors': secs} for d, secs in days]}


def _merged(arr_delay=None, cancelled=False, arr_cancelled=False):
    """Ein _flight_obs_merged-artiger Record. arr_delay=None ⇒ no_signal."""
    return {'ok': True, 'arr_delay_min': arr_delay,
            'cancelled': cancelled, 'arr_cancelled': arr_cancelled}


def _obs_map(mapping):
    """Baut eine _flight_obs_merged-Ersatzfunktion aus {norm_fn: record|None}."""
    def _fn(flight_no, date=None, dep_iata=None, arr_iata=None, live=True,
            free_only=False):
        return mapping.get(A._fn_norm(flight_no))
    return _fn


def _run_member(roster, obs, token='AT-ME', year=2026, month=6, min_sample=3):
    with patch.object(A, '_roster_snapshot_read', return_value=roster):
        with patch.object(A, '_flight_obs_merged', side_effect=obs):
            return A._member_monthly_punctuality(token, year, month,
                                                 min_sample=min_sample)


# ════════════════════════════════════════════════════════════════════
# _crew_flights_for_month
# ════════════════════════════════════════════════════════════════════

def test_flights_for_month_basic_collect():
    roster = _roster(('2026-06-05', [_sector('LH400', '2026-06-05T08:00:00Z',
                                             'FRA', 'JFK')]))
    with patch.object(A, '_roster_snapshot_read', return_value=roster):
        fl = A._crew_flights_for_month('AT-ME', 2026, 6)
    assert len(fl) == 1
    assert fl[0]['fn'] == 'LH400'
    assert fl[0]['date'] == '2026-06-05'
    assert fl[0]['dep'] == 'FRA'
    assert fl[0]['arr'] == 'JFK'


def test_flights_for_month_uses_dep_iso_not_datum():
    """Betriebstag kommt aus dep_iso, nicht aus day.datum."""
    roster = _roster(('2026-05-31', [_sector('LH400', '2026-06-01T02:00:00Z')]))
    with patch.object(A, '_roster_snapshot_read', return_value=roster):
        fl = A._crew_flights_for_month('AT-ME', 2026, 6)
    assert len(fl) == 1 and fl[0]['date'] == '2026-06-01'


def test_flights_for_month_redeye_month_edge_counts_june():
    """Red-Eye dep_iso 2026-06-30T23:50Z (Ankunft 07-01) → zählt zu Juni."""
    roster = _roster(('2026-06-30', [_sector('LH777', '2026-06-30T23:50:00Z',
                                             arr_iso='2026-07-01T06:30:00Z')]))
    with patch.object(A, '_roster_snapshot_read', return_value=roster):
        june = A._crew_flights_for_month('AT-ME', 2026, 6)
        july = A._crew_flights_for_month('AT-ME', 2026, 7)
    assert len(june) == 1
    assert len(july) == 0


def test_flights_for_month_first_of_next_month_excluded():
    """dep_iso 2026-07-01T00:10Z zählt NICHT zu Juni."""
    roster = _roster(('2026-07-01', [_sector('LH888', '2026-07-01T00:10:00Z')]))
    with patch.object(A, '_roster_snapshot_read', return_value=roster):
        june = A._crew_flights_for_month('AT-ME', 2026, 6)
    assert len(june) == 0


def test_flights_for_month_december_boundary():
    """Dezember-Monatsende korrekt (31.12. drin, 01.01. draußen)."""
    roster = _roster(
        ('2025-12-31', [_sector('LH1', '2025-12-31T10:00:00Z')]),
        ('2026-01-01', [_sector('LH2', '2026-01-01T10:00:00Z')]),
    )
    with patch.object(A, '_roster_snapshot_read', return_value=roster):
        dec = A._crew_flights_for_month('AT-ME', 2025, 12)
    assert [f['fn'] for f in dec] == ['LH1']


def test_flights_for_month_datum_fallback_when_no_dep_iso():
    roster = _roster(('2026-06-10', [{'flight': 'LH9', 'from': 'FRA',
                                      'to': 'JFK', 'dep_iso': ''}]))
    with patch.object(A, '_roster_snapshot_read', return_value=roster):
        fl = A._crew_flights_for_month('AT-ME', 2026, 6)
    assert len(fl) == 1 and fl[0]['date'] == '2026-06-10'


def test_flights_for_month_empty_roster():
    with patch.object(A, '_roster_snapshot_read', return_value={'tage': []}):
        assert A._crew_flights_for_month('AT-ME', 2026, 6) == []


# ════════════════════════════════════════════════════════════════════
# _member_monthly_punctuality — Klassifikation & Aggregation
# ════════════════════════════════════════════════════════════════════

def _flights(n, fn_prefix='LH'):
    return _roster(*[('2026-06-%02d' % (d + 1),
                      [_sector('%s%d' % (fn_prefix, 100 + d),
                               '2026-06-%02dT08:00:00Z' % (d + 1))])
                     for d in range(n)])


def test_member_all_on_time_score_100():
    roster = _flights(5)
    obs = _obs_map({A._fn_norm('LH%d' % (100 + d)): _merged(arr_delay=3)
                    for d in range(5)})
    res = _run_member(roster, obs)
    assert res['status'] == 'ok'
    assert res['score_pct'] == 100
    assert res['on_time'] == res['sample'] == 5
    assert res['delayed'] == 0


def test_member_all_delayed_score_0():
    roster = _flights(5)
    obs = _obs_map({A._fn_norm('LH%d' % (100 + d)): _merged(arr_delay=45)
                    for d in range(5)})
    res = _run_member(roster, obs)
    assert res['status'] == 'ok'
    assert res['score_pct'] == 0
    assert res['delayed'] == res['sample'] == 5
    assert res['on_time'] == 0


def test_member_mixed_12_of_13_is_92():
    """round(12/13*100) == 92."""
    roster = _flights(13)
    mapping = {A._fn_norm('LH%d' % (100 + d)): _merged(arr_delay=2)
               for d in range(12)}
    mapping[A._fn_norm('LH112')] = _merged(arr_delay=40)  # der 13. = delayed
    res = _run_member(roster, _obs_map(mapping))
    assert res['status'] == 'ok'
    assert res['sample'] == 13
    assert res['on_time'] == 12 and res['delayed'] == 1
    assert res['score_pct'] == 92
    assert abs(res['score_raw'] - 12 / 13) < 1e-9


def test_member_exactly_min_sample_is_ok():
    roster = _flights(3)
    obs = _obs_map({A._fn_norm('LH%d' % (100 + d)): _merged(arr_delay=1)
                    for d in range(3)})
    res = _run_member(roster, obs)
    assert res['status'] == 'ok' and res['sample'] == 3


def test_member_below_min_sample_insufficient():
    roster = _flights(2)
    obs = _obs_map({A._fn_norm('LH%d' % (100 + d)): _merged(arr_delay=1)
                    for d in range(2)})
    res = _run_member(roster, obs)
    assert res['status'] == 'insufficient_sample'
    assert res['score_pct'] is None
    assert res['score_raw'] is None
    assert res['sample'] == 2


def test_member_zero_rated_but_flights_present_insufficient():
    """0 gewertete Flüge, aber Roster hat Flüge ohne Signal → insufficient, sample 0."""
    roster = _flights(4)
    obs = _obs_map({A._fn_norm('LH%d' % (100 + d)): None for d in range(4)})
    res = _run_member(roster, obs)
    assert res['status'] == 'insufficient_sample'
    assert res['sample'] == 0
    assert res['no_signal'] == 4
    assert res['score_pct'] is None


def test_member_no_flights_status():
    with patch.object(A, '_roster_snapshot_read', return_value={'tage': []}):
        res = A._member_monthly_punctuality('AT-ME', 2026, 6)
    assert res['status'] == 'no_flights'
    assert res['sample'] == 0


def test_member_merged_none_is_no_signal_not_denominator():
    roster = _flights(5)
    mapping = {A._fn_norm('LH%d' % (100 + d)): _merged(arr_delay=2)
               for d in range(4)}
    mapping[A._fn_norm('LH104')] = None  # kein Warehouse-Record
    res = _run_member(roster, _obs_map(mapping))
    assert res['sample'] == 4  # der None-Flug NICHT im Nenner
    assert res['no_signal'] == 1
    assert res['score_pct'] == 100


def test_member_arr_delay_none_is_no_signal_not_zero():
    """arr_delay_min None (Arr-Seite fehlt/kein delay_known) → no_signal, NICHT 0."""
    roster = _flights(4)
    mapping = {A._fn_norm('LH%d' % (100 + d)): _merged(arr_delay=2)
               for d in range(3)}
    mapping[A._fn_norm('LH103')] = _merged(arr_delay=None)
    res = _run_member(roster, _obs_map(mapping))
    assert res['sample'] == 3
    assert res['no_signal'] == 1
    assert res['on_time'] == 3


def test_member_cancelled_separate_counter():
    roster = _flights(5)
    mapping = {A._fn_norm('LH%d' % (100 + d)): _merged(arr_delay=2)
               for d in range(4)}
    mapping[A._fn_norm('LH104')] = _merged(cancelled=True)
    res = _run_member(roster, _obs_map(mapping))
    assert res['cancelled'] == 1
    assert res['sample'] == 4  # cancelled NICHT im Nenner
    assert res['on_time'] == 4
    assert res['score_pct'] == 100


def test_member_arr_cancelled_flag_also_counts():
    roster = _flights(4)
    mapping = {A._fn_norm('LH%d' % (100 + d)): _merged(arr_delay=2)
               for d in range(3)}
    mapping[A._fn_norm('LH103')] = _merged(arr_cancelled=True)
    res = _run_member(roster, _obs_map(mapping))
    assert res['cancelled'] == 1
    assert res['sample'] == 3


def test_member_threshold_15_inclusive_on_time():
    roster = _flights(3)
    obs = _obs_map({A._fn_norm('LH%d' % (100 + d)): _merged(arr_delay=15)
                    for d in range(3)})
    res = _run_member(roster, obs)
    assert res['on_time'] == 3 and res['delayed'] == 0


def test_member_delay_16_is_delayed():
    roster = _flights(3)
    obs = _obs_map({A._fn_norm('LH%d' % (100 + d)): _merged(arr_delay=16)
                    for d in range(3)})
    res = _run_member(roster, obs)
    assert res['delayed'] == 3 and res['on_time'] == 0


def test_member_negative_delay_is_on_time():
    roster = _flights(3)
    obs = _obs_map({A._fn_norm('LH%d' % (100 + d)): _merged(arr_delay=-12)
                    for d in range(3)})
    res = _run_member(roster, obs)
    assert res['on_time'] == 3


def test_member_fn_norm_matches_leading_zero():
    """Roster 'LH0400' matcht Warehouse-Record unter 'LH400'."""
    roster = _roster(
        ('2026-06-01', [_sector('LH0400', '2026-06-01T08:00:00Z')]),
        ('2026-06-02', [_sector('LH0400', '2026-06-02T08:00:00Z')]),
        ('2026-06-03', [_sector('LH0400', '2026-06-03T08:00:00Z')]),
    )
    obs = _obs_map({'LH400': _merged(arr_delay=3)})
    res = _run_member(roster, obs)
    assert res['sample'] == 3 and res['on_time'] == 3


def test_member_median_delay_computed():
    roster = _flights(5)
    delays = [2, 20, 4, 30, 10]
    mapping = {A._fn_norm('LH%d' % (100 + d)): _merged(arr_delay=delays[d])
               for d in range(5)}
    res = _run_member(roster, _obs_map(mapping))
    assert res['median_delay'] == 10  # sorted [2,4,10,20,30] → 10


def test_member_delay_threshold_field_is_15():
    roster = _flights(3)
    obs = _obs_map({A._fn_norm('LH%d' % (100 + d)): _merged(arr_delay=1)
                    for d in range(3)})
    res = _run_member(roster, obs)
    assert res['delay_threshold_min'] == 15
    assert res['min_sample'] == 3


# ════════════════════════════════════════════════════════════════════
# _punct_median / _punct_rank_entries
# ════════════════════════════════════════════════════════════════════

def test_punct_median_odd_even_empty():
    assert A._punct_median([]) is None
    assert A._punct_median([5]) == 5.0
    assert A._punct_median([1, 3]) == 2.0
    assert A._punct_median([9, 1, 5]) == 5.0


def test_rank_descending_number_one_highest():
    entries = [
        {'token': 'a', 'name': 'A', 'score': 80, 'score_raw': 0.80, 'sample': 10,
         'median_delay': 5},
        {'token': 'b', 'name': 'B', 'score': 98, 'score_raw': 0.98, 'sample': 20,
         'median_delay': 2},
        {'token': 'c', 'name': 'C', 'score': 90, 'score_raw': 0.90, 'sample': 15,
         'median_delay': 3},
    ]
    ordered = A._punct_rank_entries(entries)
    assert [e['name'] for e in ordered] == ['B', 'C', 'A']
    assert [e['rank'] for e in ordered] == [1, 2, 3]


def test_rank_tie_score_different_sample_higher_sample_first():
    """Gleicher Score, unterschiedliche sample → höhere sample rankt zuerst,
    beide bekommen denselben (Competition-)Rang."""
    entries = [
        {'token': 'a', 'name': 'A', 'score': 92, 'score_raw': 0.92, 'sample': 9,
         'median_delay': 5},
        {'token': 'b', 'name': 'B', 'score': 92, 'score_raw': 0.9231, 'sample': 13,
         'median_delay': 5},
    ]
    ordered = A._punct_rank_entries(entries)
    assert [e['name'] for e in ordered] == ['B', 'A']
    assert [e['rank'] for e in ordered] == [1, 1]


def test_rank_tie_score_and_sample_median_breaks():
    entries = [
        {'token': 'a', 'name': 'A', 'score': 90, 'score_raw': 0.90, 'sample': 10,
         'median_delay': 12},
        {'token': 'b', 'name': 'B', 'score': 90, 'score_raw': 0.90, 'sample': 10,
         'median_delay': 4},
    ]
    ordered = A._punct_rank_entries(entries)
    assert [e['name'] for e in ordered] == ['B', 'A']  # niedrigerer Median zuerst
    assert [e['rank'] for e in ordered] == [1, 1]


def test_rank_full_tie_name_deterministic():
    entries = [
        {'token': 'z', 'name': 'Zoe', 'score': 88, 'score_raw': 0.88, 'sample': 8,
         'median_delay': 3},
        {'token': 'a', 'name': 'Ada', 'score': 88, 'score_raw': 0.88, 'sample': 8,
         'median_delay': 3},
    ]
    ordered = A._punct_rank_entries(entries)
    assert [e['name'] for e in ordered] == ['Ada', 'Zoe']
    assert [e['rank'] for e in ordered] == [1, 1]


def test_rank_competition_1_1_3():
    entries = [
        {'token': 'a', 'name': 'A', 'score': 98, 'score_raw': 0.98, 'sample': 10,
         'median_delay': 1},
        {'token': 'b', 'name': 'B', 'score': 98, 'score_raw': 0.98, 'sample': 10,
         'median_delay': 1},
        {'token': 'c', 'name': 'C', 'score': 70, 'score_raw': 0.70, 'sample': 10,
         'median_delay': 9},
    ]
    ordered = A._punct_rank_entries(entries)
    assert [e['rank'] for e in ordered] == [1, 1, 3]


# ════════════════════════════════════════════════════════════════════
# _crew_punctuality_leaderboard
# ════════════════════════════════════════════════════════════════════

def _member_res(status='ok', score=None, raw=None, sample=0, median=None):
    return {'status': status, 'month': '2026-06', 'score_pct': score,
            'score': score, 'score_raw': raw, 'on_time': 0, 'delayed': 0,
            'cancelled': 0, 'sample': sample, 'no_signal': 0,
            'median_delay': median, 'delay_threshold_min': 15, 'min_sample': 3}


def _run_leaderboard(member_map, friends, profiles, token='AT-ME'):
    def _mm(tok, year, month, min_sample=3):
        return member_map[tok]
    with patch.object(A, '_friends_load',
                      return_value={'token': token, 'friends': friends}):
        with patch.object(A, '_profiles_load_bulk', return_value=profiles):
            with patch.object(A, '_member_monthly_punctuality', side_effect=_mm):
                return A._crew_punctuality_leaderboard(token, 2026, 6)


def test_leaderboard_ranking_descending():
    member_map = {
        'AT-ME': _member_res('ok', 92, 0.92, 13, 5),
        'AT-A': _member_res('ok', 98, 0.98, 21, 2),
        'AT-B': _member_res('ok', 80, 0.80, 9, 8),
    }
    profiles = {'AT-ME': {'name': 'Me'}, 'AT-A': {'name': 'Anna'},
                'AT-B': {'name': 'Ben'}}
    lb = _run_leaderboard(member_map, ['AT-A', 'AT-B'], profiles)
    names = [m['name'] for m in lb['ranked']]
    assert names == ['Anna', 'Me', 'Ben']
    assert [m['rank'] for m in lb['ranked']] == [1, 2, 3]
    assert lb['total_ranked'] == 3


def test_leaderboard_tie_same_rank():
    member_map = {
        'AT-ME': _member_res('ok', 92, 0.9231, 13, 5),
        'AT-A': _member_res('ok', 98, 0.98, 21, 2),
        'AT-B': _member_res('ok', 92, 0.92, 9, 5),
    }
    profiles = {'AT-ME': {'name': 'Miguel'}, 'AT-A': {'name': 'Anna'},
                'AT-B': {'name': 'Ben'}}
    lb = _run_leaderboard(member_map, ['AT-A', 'AT-B'], profiles)
    by_name = {m['name']: m for m in lb['ranked']}
    assert by_name['Anna']['rank'] == 1
    assert by_name['Miguel']['rank'] == 2
    assert by_name['Ben']['rank'] == 2  # gleicher Score wie Miguel → Rang 2


def test_leaderboard_insufficient_separated():
    member_map = {
        'AT-ME': _member_res('ok', 92, 0.92, 13, 5),
        'AT-A': _member_res('insufficient_sample', None, None, 2, None),
        'AT-B': _member_res('no_flights', None, None, 0, None),
    }
    profiles = {'AT-ME': {'name': 'Me'}, 'AT-A': {'name': 'Cara'},
                'AT-B': {'name': 'Dan'}}
    lb = _run_leaderboard(member_map, ['AT-A', 'AT-B'], profiles)
    assert [m['name'] for m in lb['ranked']] == ['Me']
    assert [m['name'] for m in lb['insufficient']] == ['Cara']  # Dan (no_flights) raus
    assert all(m.get('rank') is None for m in lb['insufficient'])


def test_leaderboard_excludes_share_roster_false():
    member_map = {
        'AT-ME': _member_res('ok', 92, 0.92, 13, 5),
        'AT-A': _member_res('ok', 99, 0.99, 30, 1),
    }
    profiles = {'AT-ME': {'name': 'Me'},
                'AT-A': {'name': 'Secret', 'share_roster': False}}
    lb = _run_leaderboard(member_map, ['AT-A'], profiles)
    assert [m['name'] for m in lb['ranked']] == ['Me']
    assert lb['total_ranked'] == 1


def test_leaderboard_self_always_included_even_share_false_profile():
    """share_roster-Gate gilt nur für Friends, nie für den User selbst."""
    member_map = {'AT-ME': _member_res('ok', 88, 0.88, 5, 6)}
    profiles = {'AT-ME': {'name': 'Me', 'share_roster': False}}
    lb = _run_leaderboard(member_map, [], profiles)
    assert [m['name'] for m in lb['ranked']] == ['Me']


# ════════════════════════════════════════════════════════════════════
# _punct_trunc_token / _punct_persist_me (history)
# ════════════════════════════════════════════════════════════════════

def test_trunc_token_self_and_friend():
    assert A._punct_trunc_token('AT-123456789012345', True) == 'self'
    ft = A._punct_trunc_token('AT-123456789012345', False)
    assert ft.startswith('tok:') and ft.endswith('…')
    assert 'AT-123456789012345' not in ft  # roher Token darf NICHT leaken


def test_persist_me_only_when_ok():
    calls = []
    with patch.object(A, '_profile_metadata_merge_sb',
                      side_effect=lambda t, p: calls.append((t, p)) or True):
        A._punct_persist_me('AT-ME', _member_res('insufficient_sample', None,
                                                  None, 2), None, 1, None)
    assert calls == []  # insufficient schreibt keinen Score


def test_persist_me_history_append_new_month():
    cached = {'month': '2026-05', 'history': [
        {'month': '2026-05', 'score': 88, 'rank': 3, 'sample': 17}]}
    res = _member_res('ok', 92, 0.92, 13, 5)
    captured = {}
    with patch.object(A, '_profile_metadata_merge_sb',
                      side_effect=lambda t, p: captured.update(p) or True):
        A._punct_persist_me('AT-ME', res, 2, 6, cached)
    hist = captured['punctuality']['history']
    assert [h['month'] for h in hist] == ['2026-05', '2026-06']


def test_persist_me_history_dedupe_same_month():
    cached = {'month': '2026-06', 'history': [
        {'month': '2026-06', 'score': 70, 'rank': 5, 'sample': 10}]}
    res = _member_res('ok', 92, 0.92, 13, 5)
    captured = {}
    with patch.object(A, '_profile_metadata_merge_sb',
                      side_effect=lambda t, p: captured.update(p) or True):
        A._punct_persist_me('AT-ME', res, 2, 6, cached)
    hist = captured['punctuality']['history']
    assert len(hist) == 1
    assert hist[0]['score'] == 92  # ersetzt, nicht dupliziert


def test_persist_me_history_cap_24():
    old = [{'month': '20%02d-01' % i, 'score': 50, 'rank': 1, 'sample': 5}
           for i in range(25)]
    cached = {'month': '2099-12', 'history': old}
    res = _member_res('ok', 92, 0.92, 13, 5)
    captured = {}
    with patch.object(A, '_profile_metadata_merge_sb',
                      side_effect=lambda t, p: captured.update(p) or True):
        A._punct_persist_me('AT-ME', res, 2, 6, cached)
    hist = captured['punctuality']['history']
    assert len(hist) <= 24


def test_persist_me_merge_patches_only_punctuality_key():
    """Merge patcht NUR den punctuality-Zweig (kein Avatar-Clobber)."""
    res = _member_res('ok', 92, 0.92, 13, 5)
    captured = {}
    with patch.object(A, '_profile_metadata_merge_sb',
                      side_effect=lambda t, p: captured.update({'p': p}) or True):
        A._punct_persist_me('AT-ME', res, 2, 6, None)
    patch_arg = captured['p']
    assert set(patch_arg.keys()) == {'punctuality'}


# ════════════════════════════════════════════════════════════════════
# Endpoint GET /api/ax/punctuality/<token>
# ════════════════════════════════════════════════════════════════════

def _client():
    A.app.config['TESTING'] = True
    return A.app.test_client()


def _fresh_cache(month='2026-06', score=92, rank=2, sample=13):
    return {'month': month, 'score': score, 'rank': rank, 'sample': sample,
            'total_crew': 6, 'on_time': 12, 'delayed': 1, 'cancelled': 1,
            'no_signal': 4, 'delay_threshold_min': 15,
            'updated_at': datetime.now(timezone.utc).strftime(
                '%Y-%m-%dT%H:%M:%SZ'), 'history': []}


def test_endpoint_invalid_token_404():
    with patch.object(A, '_validate_token_exists', return_value=None):
        r = _client().get('/api/ax/punctuality/AT-BAD')
    assert r.status_code == 404
    assert r.get_json()['error'] == 'invalid_token'


def test_endpoint_rate_limited_429():
    with patch.object(A, '_validate_token_exists', return_value='x@y.z'):
        with patch.object(A, '_token_rate_limited', return_value=True):
            r = _client().get('/api/ax/punctuality/AT-ME')
    assert r.status_code == 429


def test_endpoint_invalid_month_400():
    with patch.object(A, '_validate_token_exists', return_value='x@y.z'):
        with patch.object(A, '_token_rate_limited', return_value=False):
            r = _client().get('/api/ax/punctuality/AT-ME?month=2026-13')
    assert r.status_code == 400


def test_endpoint_fresh_cache_no_board_scan():
    """Frischer Cache + gleicher Monat → Antwort aus Cache, KEIN Leaderboard-Compute."""
    scan = {'called': False}

    def _lb(*a, **k):
        scan['called'] = True
        return {'ranked': [], 'insufficient': [], 'total_ranked': 0, 'members': []}

    with patch.object(A, '_validate_token_exists', return_value='x@y.z'):
        with patch.object(A, '_token_rate_limited', return_value=False):
            with patch.object(A, 'SB_AVAILABLE', True):
                with patch.object(A, '_profiles_load_bulk',
                                  return_value={'AT-ME': {'punctuality': _fresh_cache()}}):
                    with patch.object(A, '_crew_punctuality_leaderboard', side_effect=_lb):
                        r = _client().get('/api/ax/punctuality/AT-ME?month=2026-06')
    assert r.status_code == 200
    body = r.get_json()
    assert body['cached'] is True
    assert body['me']['score'] == 92
    assert scan['called'] is False  # KEIN Board-Scan


def test_endpoint_refresh_forces_recompute():
    lb = {'ranked': [{'token': 'AT-ME', 'is_me': True, 'name': 'Me',
                      'avatar_url': None, 'rank': 1,
                      'res': _member_res('ok', 100, 1.0, 5, 0)}],
          'insufficient': [], 'total_ranked': 1,
          'members': [{'token': 'AT-ME', 'is_me': True, 'name': 'Me',
                       'rank': 1, 'res': _member_res('ok', 100, 1.0, 5, 0)}]}
    with patch.object(A, '_validate_token_exists', return_value='x@y.z'):
        with patch.object(A, '_token_rate_limited', return_value=False):
            with patch.object(A, 'SB_AVAILABLE', True):
                with patch.object(A, '_profiles_load_bulk',
                                  return_value={'AT-ME': {'punctuality': _fresh_cache()}}):
                    with patch.object(A, '_crew_punctuality_leaderboard',
                                      return_value=lb) as m_lb:
                        with patch.object(A, '_profile_metadata_merge_sb', return_value=True):
                            r = _client().get('/api/ax/punctuality/AT-ME?month=2026-06&refresh=1')
    assert r.status_code == 200
    assert m_lb.called
    assert r.get_json()['me']['score'] == 100


def test_endpoint_default_month_is_current_utc():
    now = datetime.now(timezone.utc)
    expected = '%04d-%02d' % (now.year, now.month)
    lb = {'ranked': [], 'insufficient': [], 'total_ranked': 0,
          'members': [{'token': 'AT-ME', 'is_me': True, 'name': 'Me',
                       'res': _member_res('no_flights', None, None, 0)}]}
    with patch.object(A, '_validate_token_exists', return_value='x@y.z'):
        with patch.object(A, '_token_rate_limited', return_value=False):
            with patch.object(A, 'SB_AVAILABLE', True):
                with patch.object(A, '_profiles_load_bulk', return_value={'AT-ME': {}}):
                    with patch.object(A, '_crew_punctuality_leaderboard', return_value=lb):
                        r = _client().get('/api/ax/punctuality/AT-ME')
    assert r.get_json()['month'] == expected


def test_endpoint_sb_down_with_cache_returns_stale():
    with patch.object(A, '_validate_token_exists', return_value='x@y.z'):
        with patch.object(A, '_token_rate_limited', return_value=False):
            with patch.object(A, 'SB_AVAILABLE', False):
                with patch.object(A, '_profiles_load_bulk',
                                  return_value={'AT-ME': {'punctuality': _fresh_cache()}}):
                    r = _client().get('/api/ax/punctuality/AT-ME?month=2026-06')
    assert r.status_code == 200
    assert r.get_json().get('stale') is True


def test_endpoint_sb_down_no_cache_503():
    with patch.object(A, '_validate_token_exists', return_value='x@y.z'):
        with patch.object(A, '_token_rate_limited', return_value=False):
            with patch.object(A, 'SB_AVAILABLE', False):
                with patch.object(A, '_profiles_load_bulk', return_value={'AT-ME': {}}):
                    r = _client().get('/api/ax/punctuality/AT-ME?month=2026-06')
    assert r.status_code == 503
    assert r.get_json()['error'] == 'storage_unavailable'


def test_endpoint_leaderboard_truncates_friend_tokens():
    lb = {
        'ranked': [
            {'token': 'AT-ME', 'is_me': True, 'name': 'Me', 'avatar_url': None,
             'rank': 1, 'res': _member_res('ok', 98, 0.98, 20, 2)},
            {'token': 'AT-FRIEND-RAW-9999', 'is_me': False, 'name': 'Anna',
             'avatar_url': None, 'rank': 2, 'res': _member_res('ok', 90, 0.90, 10, 3)},
        ],
        'insufficient': [], 'total_ranked': 2,
        'members': [
            {'token': 'AT-ME', 'is_me': True, 'name': 'Me', 'rank': 1,
             'res': _member_res('ok', 98, 0.98, 20, 2)},
        ],
    }
    with patch.object(A, '_validate_token_exists', return_value='x@y.z'):
        with patch.object(A, '_token_rate_limited', return_value=False):
            with patch.object(A, 'SB_AVAILABLE', True):
                with patch.object(A, '_profiles_load_bulk', return_value={'AT-ME': {}}):
                    with patch.object(A, '_crew_punctuality_leaderboard', return_value=lb):
                        with patch.object(A, '_profile_metadata_merge_sb', return_value=True):
                            r = _client().get('/api/ax/punctuality/AT-ME?month=2026-06')
    body = r.get_json()
    toks = [row['token'] for row in body['leaderboard']]
    assert 'self' in toks
    assert all('AT-FRIEND-RAW-9999' != t for t in toks)
    friend_row = next(row for row in body['leaderboard'] if not row['is_me'])
    assert friend_row['token'].startswith('tok:') and friend_row['token'].endswith('…')
