"""Live-Delay-Awareness — Backend-Teil (Owner 2026-07-04).

Deckt ab:
  • _flight_status_bucket   — Board-Status → landed/airborne/grounded/None
  • _enrich_leg_delays      — Pro-Leg-Anreicherung der ical_sectors[]
  • _flight_obs_merged      — echter Dual-Side-Merge (mit gemockten Board/Store)
  • get_briefings           — Serve-Time-Enrichment nur today/today+1
  • get_friends_today       — lay_eff Echter-Status-Kaskade (Tibor-Fall)

KEIN echter Netz-/DB-Zugriff: _flight_from_free_board / _flight_from_live_board /
_departed_rows_from_store / Loader werden gemockt. free_only=True darf NIE eine
bezahlte AeroDataBox-Quelle treffen (explizit asserted).
"""
import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

from datetime import datetime, timezone, timedelta, date as _date
from unittest.mock import patch, MagicMock

import pytest

import app as A


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures / Helpers
# ──────────────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _clear_caches():
    """Merge-/Codeshare-Cache vor jedem Test leeren (date=None keyt sonst über
    Tests hinweg auf denselben Eintrag)."""
    A._FLIGHT_MERGE_CACHE.clear()
    A._AX_CODESHARE_CACHE['ts'] = 0.0
    A._AX_CODESHARE_CACHE['map'] = {}
    yield
    A._FLIGHT_MERGE_CACHE.clear()


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _now():
    return datetime.now(timezone.utc)


def _sector(flight='LH400', frm='FRA', to='MUC', dep_iso=None, arr_iso=None):
    s = {'flight': flight, 'from': frm, 'to': to}
    if dep_iso is not None:
        s['dep_iso'] = dep_iso
    if arr_iso is not None:
        s['arr_iso'] = arr_iso
    return s


def _merged(delay_min=None, delay_known=False, delay_side=None,
            dep_delay_min=None, arr_delay_min=None, status=None,
            cancelled=False, esti_dep=None, esti_arr=None, reg=None,
            sides=None):
    """Ein _flight_obs_merged-artiges Dict."""
    return {
        'ok': True, 'delay_min': delay_min, 'delay_known': delay_known,
        'delay_side': delay_side, 'dep_delay_min': dep_delay_min,
        'arr_delay_min': arr_delay_min, 'status': status,
        'cancelled': cancelled, 'esti_dep': esti_dep, 'esti_arr': esti_arr,
        'reg': reg, 'sides': sides or {'dep': None, 'arr': None},
    }


# ══════════════════════════════════════════════════════════════════════════════
# _flight_status_bucket
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize('status,expected', [
    ('Landed', 'landed'),
    ('Gelandet 14:23', 'landed'),
    ('Arrived', 'landed'),
    ('At Gate', 'landed'),
    ('on blocks', 'landed'),
    ('angekommen', 'landed'),
    ('Departed', 'airborne'),
    ('Airborne', 'airborne'),
    ('en-route', 'airborne'),
    ('En Route', 'airborne'),
    ('Abgeflogen', 'airborne'),
    ('im Flug', 'airborne'),
    ('Scheduled', 'grounded'),
    ('Boarding', 'grounded'),
    ('Gate Open', 'grounded'),
    ('Delayed', 'grounded'),
    ('Verspätet', 'grounded'),
    ('Estimated 12:40', 'grounded'),
    ('', None),
    (None, None),
    ('Unicorn', None),
])
def test_status_bucket(status, expected):
    assert A._flight_status_bucket(status) == expected


def test_status_bucket_landed_beats_gate_open_substring():
    # „at gate" (Ankunft) ≠ „gate open" (Abflug-Boarding).
    assert A._flight_status_bucket('At Gate 12') == 'landed'
    assert A._flight_status_bucket('Gate Open') == 'grounded'


def test_status_bucket_case_and_space_insensitive():
    assert A._flight_status_bucket('  LANDED  ') == 'landed'
    assert A._flight_status_bucket('DePaRtEd') == 'airborne'


# ══════════════════════════════════════════════════════════════════════════════
# _enrich_leg_delays — Kern
# ══════════════════════════════════════════════════════════════════════════════
def test_enrich_on_time_leg():
    secs = [_sector()]
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_min=0, delay_known=True,
                                           delay_side='dep', dep_delay_min=0)):
        A._enrich_leg_delays(secs, '2026-07-04')
    s = secs[0]
    assert s['delay_known'] is True
    assert s['delay_min'] == 0
    assert s['delay_side'] == 'dep'


def test_enrich_dep_delayed_no_arr():
    secs = [_sector()]
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_min=35, delay_known=True,
                                           delay_side='dep', dep_delay_min=35)):
        A._enrich_leg_delays(secs, '2026-07-04')
    assert secs[0]['delay_min'] == 35
    assert secs[0]['delay_side'] == 'dep'
    assert secs[0]['dep_delay_min'] == 35


def test_enrich_arr_delay_wins():
    secs = [_sector()]
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_min=40, delay_known=True,
                                           delay_side='arr', dep_delay_min=10,
                                           arr_delay_min=40)):
        A._enrich_leg_delays(secs, '2026-07-04')
    assert secs[0]['delay_min'] == 40
    assert secs[0]['delay_side'] == 'arr'
    assert secs[0]['arr_delay_min'] == 40
    assert secs[0]['dep_delay_min'] == 10


def test_enrich_arr_known_dep_unknown():
    secs = [_sector()]
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_min=40, delay_known=True,
                                           delay_side='arr', dep_delay_min=None,
                                           arr_delay_min=40)):
        A._enrich_leg_delays(secs, '2026-07-04')
    assert secs[0]['delay_side'] == 'arr'
    assert secs[0]['delay_min'] == 40
    assert secs[0]['dep_delay_min'] is None


def test_enrich_unknown_no_fabricated_zero():
    secs = [_sector()]
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_min=None, delay_known=False)):
        A._enrich_leg_delays(secs, '2026-07-04')
    assert secs[0]['delay_known'] is False
    assert secs[0]['delay_min'] is None
    assert secs[0]['dep_delay_min'] is None
    assert secs[0]['arr_delay_min'] is None


def test_enrich_none_merged_writes_nothing():
    secs = [_sector()]
    with patch.object(A, '_flight_obs_merged', return_value=None):
        A._enrich_leg_delays(secs, '2026-07-04')
    # Legacy/kein Signal → gar keine neuen Keys (abwärtskompatibel).
    assert 'delay_known' not in secs[0]
    assert 'delay_min' not in secs[0]
    assert 'status' not in secs[0]


def test_enrich_cancelled_leg():
    secs = [_sector()]
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_min=None, delay_known=True,
                                           status='cancelled', cancelled=True)):
        A._enrich_leg_delays(secs, '2026-07-04')
    assert secs[0]['cancelled'] is True
    assert secs[0]['delay_known'] is True
    # keine positive Delay-Behauptung.
    assert secs[0]['delay_min'] is None


def test_enrich_passes_est_and_reg_and_sides():
    secs = [_sector()]
    m = _merged(delay_min=25, delay_known=True, delay_side='dep',
                status='airborne', esti_dep='2026-07-04T08:35:00Z',
                esti_arr='2026-07-04T10:10:00Z', reg='D-AIXY',
                sides={'dep': 'live', 'arr': 'obs'})
    with patch.object(A, '_flight_obs_merged', return_value=m):
        A._enrich_leg_delays(secs, '2026-07-04')
    assert secs[0]['est_dep_iso'] == '2026-07-04T08:35:00Z'
    assert secs[0]['est_arr_iso'] == '2026-07-04T10:10:00Z'
    assert secs[0]['reg'] == 'D-AIXY'
    assert secs[0]['status'] == 'airborne'
    assert secs[0]['obs_sides'] == {'dep': 'live', 'arr': 'obs'}


def test_enrich_fn_norm_equivalence():
    # LH0839 muss als LH839 nachgeschlagen werden.
    secs = [_sector(flight='LH0839')]
    mock = MagicMock(return_value=_merged(delay_min=5, delay_known=True,
                                          delay_side='dep'))
    with patch.object(A, '_flight_obs_merged', mock):
        A._enrich_leg_delays(secs, '2026-07-04')
    called_fn = mock.call_args.args[0] if mock.call_args.args else \
        mock.call_args.kwargs.get('flight_no')
    # erstes Positional-Arg ist die Flugnummer
    assert mock.call_args.args[0] == 'LH839'


def test_enrich_codeshare_folds_to_operating():
    secs = [_sector(flight='UA8841')]
    mock = MagicMock(return_value=_merged(delay_min=12, delay_known=True,
                                          delay_side='dep'))
    with patch.object(A, '_ax_codeshare_map', return_value={'UA8841': 'LH400'}), \
            patch.object(A, '_flight_obs_merged', mock):
        A._enrich_leg_delays(secs, '2026-07-04')
    # Nachschlag über die OPERATING-Nummer.
    assert mock.call_args.args[0] == 'LH400'
    # Marketing-Nummer bleibt additiv erhalten.
    assert secs[0]['also_as'] == 'UA8841'


def test_enrich_codeshare_runs_once_per_leg():
    secs = [_sector(flight='UA8841')]
    mock = MagicMock(return_value=_merged(delay_known=True, delay_min=0,
                                          delay_side='dep'))
    with patch.object(A, '_ax_codeshare_map', return_value={'UA8841': 'LH400'}), \
            patch.object(A, '_flight_obs_merged', mock):
        A._enrich_leg_delays(secs, '2026-07-04')
    assert mock.call_count == 1


def test_enrich_missing_flight_number_skipped_gracefully():
    secs = [{'from': 'FRA', 'to': 'MUC'},           # keine Flugnr
            _sector(flight='LH400')]                # valider Leg
    calls = []

    def _fake(fn, **kw):
        calls.append(fn)
        return _merged(delay_min=3, delay_known=True, delay_side='dep')
    with patch.object(A, '_flight_obs_merged', side_effect=_fake):
        A._enrich_leg_delays(secs, '2026-07-04')
    assert 'delay_known' not in secs[0]      # skip, keine Exception
    assert secs[1]['delay_known'] is True     # anderer Leg dennoch angereichert
    assert calls == ['LH400']


def test_enrich_short_flight_number_skipped():
    secs = [_sector(flight='LH')]
    with patch.object(A, '_flight_obs_merged',
                      side_effect=AssertionError('should not be called')):
        A._enrich_leg_delays(secs, '2026-07-04')
    assert 'delay_known' not in secs[0]


def test_enrich_bad_iata_skipped():
    secs = [_sector(frm='FRANKFURT', to='MUC')]
    with patch.object(A, '_flight_obs_merged',
                      side_effect=AssertionError('should not be called')):
        A._enrich_leg_delays(secs, '2026-07-04')
    assert 'delay_known' not in secs[0]


def test_enrich_future_leg_over_27h_skipped():
    dep = _now() + timedelta(hours=30)
    secs = [_sector(dep_iso=_iso(dep))]
    with patch.object(A, '_flight_obs_merged',
                      side_effect=AssertionError('should not scan far future')):
        A._enrich_leg_delays(secs, dep.strftime('%Y-%m-%d'))
    assert 'delay_known' not in secs[0]


def test_enrich_deep_past_leg_skipped():
    dep = _now() - timedelta(hours=40)
    secs = [_sector(dep_iso=_iso(dep))]
    with patch.object(A, '_flight_obs_merged',
                      side_effect=AssertionError('should not scan deep past')):
        A._enrich_leg_delays(secs, dep.strftime('%Y-%m-%d'))
    assert 'delay_known' not in secs[0]


def test_enrich_near_future_within_27h_enriched():
    dep = _now() + timedelta(hours=3)
    secs = [_sector(dep_iso=_iso(dep))]
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_min=15, delay_known=True,
                                           delay_side='dep')):
        A._enrich_leg_delays(secs, dep.strftime('%Y-%m-%d'))
    assert secs[0]['delay_min'] == 15


def test_enrich_leg_date_derived_from_dep_iso_not_daykey():
    # dep_iso ist die autoritative Tages-Quelle (Tag-Grenze / West-TZ).
    dep = _now().replace(microsecond=0)
    secs = [_sector(dep_iso=_iso(dep))]
    mock = MagicMock(return_value=_merged(delay_known=False))
    with patch.object(A, '_flight_obs_merged', mock):
        A._enrich_leg_delays(secs, '1999-01-01')   # falscher Tages-Key
    passed_date = mock.call_args.kwargs.get('date')
    assert passed_date == dep.strftime('%Y-%m-%d')


def test_enrich_west_tz_est_arr_stays_utc():
    # FRA->JFK: est_arr_iso bleibt UTC, unverändert durchgereicht.
    dep = _now() + timedelta(hours=1)
    secs = [_sector(frm='FRA', to='JFK', dep_iso=_iso(dep))]
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_known=True, delay_min=0,
                                           delay_side='arr',
                                           esti_arr='2026-07-04T22:10:00Z')):
        A._enrich_leg_delays(secs, dep.strftime('%Y-%m-%d'))
    assert secs[0]['est_arr_iso'] == '2026-07-04T22:10:00Z'


def test_enrich_west_tz_naive_board_converted_to_utc():
    # REALES naives Board-Format (KEIN 'Z') an einer West-TZ: JFK 14:23 local
    # (America/New_York, im Juli EDT = UTC-4) → 18:23Z. GENAU EINE Verschiebung,
    # keine Doppelverschiebung. arr-Seite nutzt die TZ des ANDEREN Flughafens.
    dep = _now() + timedelta(hours=1)
    secs = [_sector(frm='JFK', to='LHR', dep_iso=_iso(dep))]
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_known=True, delay_min=20,
                                           delay_side='dep',
                                           esti_dep='2026-07-04T14:23:00',
                                           esti_arr='2026-07-05T02:10:00')):
        A._enrich_leg_delays(secs, dep.strftime('%Y-%m-%d'))
    # dep mit airport_tz('JFK'): 14:23 EDT → 18:23Z.
    assert secs[0]['est_dep_iso'] == '2026-07-04T18:23:00Z'
    # arr mit airport_tz('LHR'): 02:10 BST (UTC+1 im Juli) → 01:10Z.
    assert secs[0]['est_arr_iso'] == '2026-07-05T01:10:00Z'


def test_enrich_naive_board_unknown_tz_is_none_not_naive():
    # Unbekannte Stations-TZ → est_*_iso ist None, NIE ein naiver String durchgereicht.
    dep = _now() + timedelta(hours=1)
    secs = [_sector(frm='ZZZ', to='QQQ', dep_iso=_iso(dep))]
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_known=True, delay_min=5,
                                           delay_side='dep',
                                           esti_dep='2026-07-04T14:23:00',
                                           esti_arr='2026-07-04T16:00:00')):
        A._enrich_leg_delays(secs, dep.strftime('%Y-%m-%d'))
    assert secs[0]['est_dep_iso'] is None
    assert secs[0]['est_arr_iso'] is None


def test_enrich_naive_board_fra_uses_berlin():
    # FRA (airport_tz liefert dort None) → Europe/Berlin. 08:00 CEST (Juli, +2) → 06:00Z.
    dep = _now() + timedelta(hours=1)
    secs = [_sector(frm='FRA', to='MUC', dep_iso=_iso(dep))]
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_known=True, delay_min=0,
                                           delay_side='dep',
                                           esti_dep='2026-07-04T08:00:00')):
        A._enrich_leg_delays(secs, dep.strftime('%Y-%m-%d'))
    assert secs[0]['est_dep_iso'] == '2026-07-04T06:00:00Z'


def test_enrich_offset_carrying_esti_normalized_to_utc():
    # Board mit Offset (+02:00) → nach UTC normalisiert, station-TZ ignoriert.
    dep = _now() + timedelta(hours=1)
    secs = [_sector(frm='JFK', to='LHR', dep_iso=_iso(dep))]
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_known=True, delay_min=0,
                                           delay_side='dep',
                                           esti_dep='2026-07-04T10:35:00+02:00')):
        A._enrich_leg_delays(secs, dep.strftime('%Y-%m-%d'))
    assert secs[0]['est_dep_iso'] == '2026-07-04T08:35:00Z'


def test_enrich_no_dep_iso_deep_future_date_skipped():
    # Leg OHNE dep_iso, date = +3 Tage → grober Datums-Guard überspringt (kein Scan).
    far = (_date.today() + timedelta(days=3)).isoformat()
    secs = [{'flight': 'LH400', 'from': 'FRA', 'to': 'MUC', 'date': far}]
    with patch.object(A, '_flight_obs_merged',
                      side_effect=AssertionError('should not scan deep future')):
        A._enrich_leg_delays(secs, far)
    assert 'delay_known' not in secs[0]


def test_enrich_no_dep_iso_deep_past_date_skipped():
    past = (_date.today() - timedelta(days=3)).isoformat()
    secs = [{'flight': 'LH400', 'from': 'FRA', 'to': 'MUC', 'date': past}]
    with patch.object(A, '_flight_obs_merged',
                      side_effect=AssertionError('should not scan deep past')):
        A._enrich_leg_delays(secs, past)
    assert 'delay_known' not in secs[0]


def test_enrich_no_dep_iso_today_date_enriched():
    # Leg OHNE dep_iso, aber date = heute → angereichert (Tag-von wird bedient).
    today = _date.today().isoformat()
    secs = [{'flight': 'LH400', 'from': 'FRA', 'to': 'MUC', 'date': today}]
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_known=True, delay_min=12,
                                           delay_side='dep')):
        A._enrich_leg_delays(secs, today)
    assert secs[0]['delay_known'] is True
    assert secs[0]['delay_min'] == 12


def test_enrich_no_dep_iso_uses_daykey_when_no_sector_date():
    # Kein sector['date'], aber Tages-Key = heute → angereichert (Fallback auf date-Param).
    today = _date.today().isoformat()
    secs = [{'flight': 'LH400', 'from': 'FRA', 'to': 'MUC'}]
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_known=True, delay_min=7,
                                           delay_side='dep')):
        A._enrich_leg_delays(secs, today)
    assert secs[0]['delay_min'] == 7


def test_enrich_cancelled_still_emits_est_or_none():
    # cancelled=True: est_* wird trotzdem gemäß Regel emittiert (hier naiv-JFK→UTC),
    # aber KEINE positive Delay-Behauptung.
    dep = _now() + timedelta(hours=1)
    secs = [_sector(frm='JFK', to='LHR', dep_iso=_iso(dep))]
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_known=True, status='cancelled',
                                           cancelled=True,
                                           esti_dep='2026-07-04T14:23:00')):
        A._enrich_leg_delays(secs, dep.strftime('%Y-%m-%d'))
    assert secs[0]['cancelled'] is True
    assert secs[0]['est_dep_iso'] == '2026-07-04T18:23:00Z'
    assert secs[0]['delay_min'] is None


def test_enrich_multi_leg_independent_status():
    secs = [_sector(flight='LH1', frm='FRA', to='MUC'),
            _sector(flight='LH2', frm='MUC', to='VIE'),
            _sector(flight='LH3', frm='VIE', to='FRA')]

    def _fake(fn, **kw):
        return {
            'LH1': _merged(delay_known=True, status='landed', delay_min=0,
                           delay_side='arr'),
            'LH2': _merged(delay_known=True, status='airborne', delay_min=20,
                           delay_side='dep', dep_delay_min=20),
            'LH3': _merged(delay_known=False, status='scheduled'),
        }[fn]
    with patch.object(A, '_flight_obs_merged', side_effect=_fake):
        A._enrich_leg_delays(secs, '2026-07-04')
    assert secs[0]['status'] == 'landed'
    assert secs[1]['status'] == 'airborne' and secs[1]['delay_min'] == 20
    assert secs[2]['status'] == 'scheduled' and secs[2]['delay_min'] is None


def test_enrich_free_only_flag_forwarded():
    secs = [_sector()]
    mock = MagicMock(return_value=_merged(delay_known=False))
    with patch.object(A, '_flight_obs_merged', mock):
        A._enrich_leg_delays(secs, '2026-07-04', free_only=True)
    assert mock.call_args.kwargs.get('free_only') is True


def test_enrich_empty_and_non_list_safe():
    assert A._enrich_leg_delays([], '2026-07-04') == []
    assert A._enrich_leg_delays(None, '2026-07-04') is None
    assert A._enrich_leg_delays('nope', '2026-07-04') == 'nope'


def test_enrich_non_dict_element_safe():
    secs = ['garbage', _sector(flight='LH400')]
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_known=True, delay_min=1,
                                           delay_side='dep')):
        A._enrich_leg_delays(secs, '2026-07-04')
    assert secs[1]['delay_known'] is True


def test_enrich_returns_same_list_in_place():
    secs = [_sector()]
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_known=True, delay_min=0,
                                           delay_side='dep')):
        out = A._enrich_leg_delays(secs, '2026-07-04')
    assert out is secs


def test_enrich_null_vs_false_distinction_preserved():
    # delay_known=False (geprüft, kein Signal) ist explizit gesetzt — NICHT
    # weggelassen (Legacy=abwesend). So bleibt die null≠false-Unterscheidung.
    secs = [_sector()]
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_known=False)):
        A._enrich_leg_delays(secs, '2026-07-04')
    assert secs[0].get('delay_known') is False   # gesetzt, nicht None/abwesend


# ══════════════════════════════════════════════════════════════════════════════
# _flight_obs_merged — echter Merge mit gemockten Board/Store-Zeilen
# ══════════════════════════════════════════════════════════════════════════════
def _store_from(mapping):
    """Baut eine _departed_rows_from_store-Ersatzfunktion: key → rows[]."""
    def _fn(key):
        return list(mapping.get(key, []))
    return _fn


def _row(flight='LH400', dest_iata=None, delay_min=0, delay_known=None,
         cancelled=False, esti=None, status=None, sched='2026-07-04T08:00',
         reg=None):
    return {'flight': flight, 'dest_iata': dest_iata, 'delay_min': delay_min,
            'delay_known': delay_known, 'cancelled': cancelled, 'esti': esti,
            'status': status, 'sched': sched, 'reg': reg}


def _run_merged(store_map, fn='LH400', dep='FRA', arr='MUC', free_only=True):
    with patch.object(A, '_flight_from_free_board', return_value=None), \
            patch.object(A, '_flight_from_live_board',
                         MagicMock(side_effect=AssertionError('paid board!'))), \
            patch.object(A, '_departed_rows_from_store',
                         side_effect=_store_from(store_map)):
        # date=None → interner _is_today_at gibt True → Store-Pfad (heute).
        return A._flight_obs_merged(fn, date=None, dep_iata=dep, arr_iata=arr,
                                    live=True, free_only=free_only)


def test_merged_on_time_dep_known():
    m = _run_merged({'FRA': [_row(delay_min=0, delay_known=True, dest_iata='MUC')]})
    assert m is not None
    assert m['delay_known'] is True
    assert m['delay_min'] == 0
    assert m['delay_side'] == 'dep'


def test_merged_dep_delayed():
    m = _run_merged({'FRA': [_row(delay_min=35, delay_known=True, dest_iata='MUC')]})
    assert m['delay_min'] == 35
    assert m['delay_side'] == 'dep'
    assert m['dep_delay_min'] == 35


def test_merged_arr_wins_over_dep():
    m = _run_merged({
        'FRA': [_row(delay_min=10, delay_known=True, dest_iata='MUC')],
        'MUC#ARR': [_row(delay_min=40, delay_known=True, dest_iata='FRA')],
    })
    assert m['delay_side'] == 'arr'
    assert m['delay_min'] == 40
    assert m['dep_delay_min'] == 10
    assert m['arr_delay_min'] == 40


def test_merged_arr_known_dep_unknown():
    m = _run_merged({
        'FRA': [_row(delay_min=0, delay_known=False, dest_iata='MUC')],
        'MUC#ARR': [_row(delay_min=40, delay_known=True, dest_iata='FRA')],
    })
    assert m['delay_side'] == 'arr'
    assert m['delay_min'] == 40
    assert m['dep_delay_min'] is None       # dep-Seite unbekannt → null


def test_merged_no_obs_returns_none():
    m = _run_merged({})
    assert m is None


def test_merged_no_fabricated_zero_when_unknown():
    # dep-Row 0 ohne Wissen, keine arr-Row → delay_min null, NICHT 0.
    m = _run_merged({'FRA': [_row(delay_min=0, delay_known=False, dest_iata='MUC')]})
    assert m is not None
    assert m['delay_known'] is False
    assert m['delay_min'] is None


def test_merged_cancelled_known():
    m = _run_merged({'FRA': [_row(delay_min=0, cancelled=True, dest_iata='MUC')]})
    assert m['cancelled'] is True
    assert m['delay_known'] is True


def test_merged_arr_store_key_uses_hash_arr():
    # Ankunfts-Seite liest airport='MUC#ARR' mit dest_iata=Herkunft(FRA).
    m = _run_merged({'MUC#ARR': [_row(delay_min=20, delay_known=True,
                                      dest_iata='FRA')]})
    assert m is not None
    assert m['arr_delay_min'] == 20
    assert m['delay_side'] == 'arr'


def test_merged_reg_passthrough_from_arr_side():
    m = _run_merged({
        'FRA': [_row(delay_min=0, delay_known=True, dest_iata='MUC')],
        'MUC#ARR': [_row(delay_min=0, delay_known=True, dest_iata='FRA',
                         reg='D-AIXY')],
    })
    assert m['reg'] == 'D-AIXY'


def test_merged_free_only_never_calls_paid_board():
    # _flight_from_live_board ist mit AssertionError bestückt; kcommt es je durch
    # würde _run_merged werfen. Erfolgreiche Assertion = kein Paid-Call.
    m = _run_merged({'FRA': [_row(delay_min=5, delay_known=True, dest_iata='MUC')]})
    assert m['delay_min'] == 5


def test_merged_memoized_second_call_no_recompute():
    store = _store_from({'FRA': [_row(delay_min=5, delay_known=True,
                                      dest_iata='MUC')]})
    counter = {'n': 0}

    def _counting(key):
        counter['n'] += 1
        return store(key)
    A._FLIGHT_MERGE_CACHE.clear()
    with patch.object(A, '_flight_from_free_board', return_value=None), \
            patch.object(A, '_departed_rows_from_store', side_effect=_counting):
        A._flight_obs_merged('LH400', date=None, dep_iata='FRA', arr_iata='MUC',
                             free_only=True)
        after_first = counter['n']
        A._flight_obs_merged('LH400', date=None, dep_iata='FRA', arr_iata='MUC',
                             free_only=True)
        after_second = counter['n']
    assert after_first > 0
    assert after_second == after_first     # 2. Aufruf = Cache-Hit, kein Store-Read


# ══════════════════════════════════════════════════════════════════════════════
# get_briefings — Serve-Time-Enrichment (nur today/today+1)
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture
def client():
    A.app.testing = True
    return A.app.test_client()


def _briefings_map():
    today = _date.today().isoformat()
    tomorrow = (_date.today() + timedelta(days=1)).isoformat()
    far = (_date.today() + timedelta(days=5)).isoformat()
    return today, tomorrow, far, {
        today: {'ical_sectors': [_sector(flight='LH100', frm='FRA', to='MUC')]},
        tomorrow: {'ical_sectors': [_sector(flight='LH200', frm='MUC', to='FRA')]},
        far: {'ical_sectors': [_sector(flight='LH300', frm='FRA', to='JFK')]},
    }


def test_get_briefings_enriches_today_and_tomorrow_only(client):
    today, tomorrow, far, data = _briefings_map()

    def _fake(fn, **kw):
        return _merged(delay_known=True, delay_min=17, delay_side='dep',
                       status='boarding')
    with patch.object(A, '_maybe_refresh_calendar_feed', return_value=None), \
            patch.object(A, '_manual_briefings_load', return_value=data), \
            patch.object(A, '_ical_briefings_load', return_value={}), \
            patch.object(A, '_flight_obs_merged', side_effect=_fake):
        r = client.get('/api/user/briefing/TESTTOKEN')
    assert r.status_code == 200
    body = r.get_json()['briefings']
    assert body[today]['ical_sectors'][0]['delay_min'] == 17
    assert body[tomorrow]['ical_sectors'][0]['delay_min'] == 17
    # Ferner Tag (>today+1) NICHT angefasst.
    assert 'delay_known' not in body[far]['ical_sectors'][0]


def test_get_briefings_single_datum_enriched(client):
    today, tomorrow, far, data = _briefings_map()
    with patch.object(A, '_maybe_refresh_calendar_feed', return_value=None), \
            patch.object(A, '_manual_briefings_load', return_value=data), \
            patch.object(A, '_ical_briefings_load', return_value={}), \
            patch.object(A, '_flight_obs_merged',
                         return_value=_merged(delay_known=True, delay_min=9,
                                              delay_side='dep')):
        r = client.get(f'/api/user/briefing/TESTTOKEN?datum={today}')
    assert r.status_code == 200
    assert r.get_json()['briefing']['ical_sectors'][0]['delay_min'] == 9


def test_get_briefings_no_signal_is_backward_compatible(client):
    today, tomorrow, far, data = _briefings_map()
    with patch.object(A, '_maybe_refresh_calendar_feed', return_value=None), \
            patch.object(A, '_manual_briefings_load', return_value=data), \
            patch.object(A, '_ical_briefings_load', return_value={}), \
            patch.object(A, '_flight_obs_merged', return_value=None):
        r = client.get('/api/user/briefing/TESTTOKEN')
    body = r.get_json()['briefings']
    # kein Signal → keine neuen Keys, Legacy-Form.
    assert set(body[today]['ical_sectors'][0].keys()) == {'flight', 'from', 'to'}


def test_get_briefings_enrich_exception_does_not_break(client):
    today, tomorrow, far, data = _briefings_map()
    with patch.object(A, '_maybe_refresh_calendar_feed', return_value=None), \
            patch.object(A, '_manual_briefings_load', return_value=data), \
            patch.object(A, '_ical_briefings_load', return_value={}), \
            patch.object(A, '_flight_obs_merged',
                         side_effect=RuntimeError('boom')):
        r = client.get('/api/user/briefing/TESTTOKEN')
    # Enrichment-Fehler darf den Endpoint nicht 500en.
    assert r.status_code == 200
    assert today in r.get_json()['briefings']


# ══════════════════════════════════════════════════════════════════════════════
# get_friends_today — lay_eff Echter-Status-Kaskade
# ══════════════════════════════════════════════════════════════════════════════
def _setup_friend(monkeypatch, first_dep_offset_h=-1.0, layover_ort='XXX',
                  routing='BLL-CPH', frm='BLL', to='CPH', flight='LH400'):
    """Ein Friend mit heutigem Tour-Tag. Gibt (token, day) zurück."""
    tok = 'FRIENDTOKEN'
    today = _date.today().isoformat()
    dep = _now() + timedelta(hours=first_dep_offset_h)
    day = {
        'datum': today,
        'klass': 'Z72',
        'routing': routing,
        'reader_facts': {'layover_ort': layover_ort,
                         'flight_numbers': [flight]},
        'ical_sectors': [_sector(flight=flight, frm=frm, to=to,
                                 dep_iso=_iso(dep))],
    }
    A._store[tok] = {'result_data': {'_tage_detail': [day]}}
    monkeypatch.setattr(A, '_friends_load', lambda t: {'friends': [tok]})
    monkeypatch.setattr(A, '_profiles_load_bulk', lambda toks: {
        tok: {'name': 'Tibor', 'homebase': 'FRA', 'share_roster': True,
              'share_location': True, 'location_source': 'roster'}})
    monkeypatch.setattr(A, '_maybe_refresh_calendar_feed', lambda *a, **k: None)
    return tok


def _friend_layover(client, tok):
    r = client.get(f'/api/user/friends-today/{tok}')
    assert r.status_code == 200
    out = r.get_json()['friends_today']
    assert len(out) == 1
    return out[0]['layover']


def test_layeff_airborne_keeps_planned_layover(client, monkeypatch):
    tok = _setup_friend(monkeypatch, first_dep_offset_h=-1.0, layover_ort='XXX')
    monkeypatch.setattr(A, '_flight_obs_merged',
                        lambda *a, **k: _merged(delay_known=True,
                                                status='airborne'))
    assert _friend_layover(client, tok) == 'XXX'      # planned, NICHT an frm gepinnt


def test_layeff_landed_advances_to_destination(client, monkeypatch):
    tok = _setup_friend(monkeypatch, first_dep_offset_h=-3.0, layover_ort='XXX',
                        to='CPH')
    monkeypatch.setattr(A, '_flight_obs_merged',
                        lambda *a, **k: _merged(delay_known=True,
                                                status='landed'))
    assert _friend_layover(client, tok) == 'CPH'      # ans Ziel des ersten Legs


def test_layeff_grounded_pins_to_departure_even_past_grace(client, monkeypatch):
    # Tibor: real +90 delayed, seit 5h überfällig, aber noch NICHT abgeflogen.
    tok = _setup_friend(monkeypatch, first_dep_offset_h=-5.0, layover_ort='XXX')
    monkeypatch.setattr(A, '_flight_obs_merged',
                        lambda *a, **k: _merged(delay_known=True,
                                                status='delayed', delay_min=90,
                                                delay_side='dep'))
    assert _friend_layover(client, tok) == 'BLL'      # echtes Signal schlägt Uhr


def test_layeff_status_none_known_dep_delay_pins_departure(client, monkeypatch):
    # TIBOR-KERNFALL: status=None (kein bucketbarer Board-Status, sehr häufig),
    # ABER bekannter Abflug-Delay (delay_known=True, dep_delay_min>0). Plan-Abflug
    # ist längst vorbei (−5h, jenseits der 4h-Grace) → früher fiel er in den
    # Uhr-Zweig und die Crew erschien fälschlich am Layover. Jetzt: am Abflughafen.
    tok = _setup_friend(monkeypatch, first_dep_offset_h=-5.0, layover_ort='XXX',
                        routing='BLL-FRA', frm='BLL', to='FRA')
    monkeypatch.setattr(A, '_flight_obs_merged',
                        lambda *a, **k: _merged(delay_known=True, status=None,
                                                delay_min=75, dep_delay_min=75,
                                                delay_side='dep'))
    assert _friend_layover(client, tok) == 'BLL'      # bekannter Delay pinnt an frm


def test_layeff_status_none_zero_delay_falls_to_grace(client, monkeypatch):
    # status=None + delay_known aber dep_delay_min=0 → KEIN Delay-Pin; jenseits der
    # 4h-Grace → geplanter Layover (kein Signal, dass er noch am Abflughafen steht).
    tok = _setup_friend(monkeypatch, first_dep_offset_h=-5.0, layover_ort='XXX')
    monkeypatch.setattr(A, '_flight_obs_merged',
                        lambda *a, **k: _merged(delay_known=True, status=None,
                                                delay_min=0, dep_delay_min=0,
                                                delay_side='dep'))
    assert _friend_layover(client, tok) == 'XXX'


def test_layeff_cancelled_pins_to_departure(client, monkeypatch):
    # cancelled=True: Flug annulliert → Crew nie losgeflogen, bleibt am
    # Abflughafen (BLL), egal ob ein delay_min vorhanden ist. cancelled schlägt
    # jeden Status-Bucket.
    tok = _setup_friend(monkeypatch, first_dep_offset_h=-5.0, layover_ort='XXX',
                        routing='BLL-FRA', frm='BLL', to='FRA')
    monkeypatch.setattr(A, '_flight_obs_merged',
                        lambda *a, **k: _merged(delay_known=True,
                                                status='cancelled', cancelled=True,
                                                delay_min=None))
    assert _friend_layover(client, tok) == 'BLL'


def test_layeff_cancelled_beats_landed_status(client, monkeypatch):
    # Selbst wenn ein Board fälschlich 'landed' meldet: cancelled hat strikten
    # Vorrang → Crew bleibt am Abflughafen, NICHT ans Ziel vorgerückt.
    tok = _setup_friend(monkeypatch, first_dep_offset_h=-5.0, layover_ort='XXX',
                        routing='BLL-FRA', frm='BLL', to='FRA')
    monkeypatch.setattr(A, '_flight_obs_merged',
                        lambda *a, **k: _merged(delay_known=True,
                                                status='landed', cancelled=True))
    assert _friend_layover(client, tok) == 'BLL'


def test_layeff_no_signal_within_grace_pins_departure(client, monkeypatch):
    tok = _setup_friend(monkeypatch, first_dep_offset_h=-1.0, layover_ort='XXX')
    monkeypatch.setattr(A, '_flight_obs_merged', lambda *a, **k: None)
    assert _friend_layover(client, tok) == 'BLL'      # 4h-Grace-Fallback greift


def test_layeff_no_signal_past_grace_uses_planned(client, monkeypatch):
    tok = _setup_friend(monkeypatch, first_dep_offset_h=-5.0, layover_ort='XXX')
    monkeypatch.setattr(A, '_flight_obs_merged', lambda *a, **k: None)
    assert _friend_layover(client, tok) == 'XXX'      # Grace abgelaufen → planned


def test_layeff_homebase_dep_never_overridden(client, monkeypatch):
    # Erster Abflug ab Homebase (FRA) → nie überschrieben.
    tok = _setup_friend(monkeypatch, first_dep_offset_h=-1.0, layover_ort='XXX',
                        frm='FRA', to='CPH', routing='FRA-CPH')
    monkeypatch.setattr(A, '_flight_obs_merged',
                        lambda *a, **k: _merged(delay_known=True,
                                                status='delayed', delay_min=90,
                                                delay_side='dep'))
    assert _friend_layover(client, tok) == 'XXX'      # Basis-Guard: planned bleibt


def test_layeff_share_roster_false_hidden(client, monkeypatch):
    tok = _setup_friend(monkeypatch, first_dep_offset_h=-1.0)
    monkeypatch.setattr(A, '_profiles_load_bulk', lambda toks: {
        tok: {'name': 'Tibor', 'homebase': 'FRA', 'share_roster': False}})
    monkeypatch.setattr(A, '_flight_obs_merged', lambda *a, **k: None)
    r = client.get(f'/api/user/friends-today/{tok}')
    assert r.get_json()['friends_today'] == []       # Opt-out respektiert


def test_layeff_privacy_gate_gps_vs_roster(client, monkeypatch):
    # location_source='gps' → _friend_facing_city gibt GPS-Stadt, nicht lay_eff.
    tok = _setup_friend(monkeypatch, first_dep_offset_h=-1.0, layover_ort='XXX')
    monkeypatch.setattr(A, '_profiles_load_bulk', lambda toks: {
        tok: {'name': 'Tibor', 'homebase': 'FRA', 'share_roster': True,
              'share_location': True, 'location_source': 'gps',
              'current_city': 'Reykjavik'}})
    monkeypatch.setattr(A, '_flight_obs_merged',
                        lambda *a, **k: _merged(delay_known=True,
                                                status='airborne'))
    r = client.get(f'/api/user/friends-today/{tok}')
    out = r.get_json()['friends_today'][0]
    assert out['current_city'] == 'Reykjavik'        # gps-Modus: GPS-Stadt


def _teardown_store():
    A._store.pop('FRIENDTOKEN', None)


@pytest.fixture(autouse=True)
def _cleanup_friend_store():
    yield
    A._store.pop('FRIENDTOKEN', None)
