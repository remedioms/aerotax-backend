"""Regression: planartig nach Sollzeit ist kein Pünktlichkeits-Beleg."""

import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

from datetime import datetime

import app as A


def _run(monkeypatch, row):
    monkeypatch.setattr(A, '_delay_obs_flush_pending', lambda: None)
    monkeypatch.setattr(A, '_delay_store_load_from_sb', lambda *a, **k: None)
    monkeypatch.setattr(A, '_airport_local_now', lambda _ap: datetime(2026, 8, 6, 10, 5))
    writes = []
    monkeypatch.setattr(A, '_delay_obs_write_through',
                        lambda *a, **k: writes.append((a, k)))
    A._delay_store.clear()
    A._delay_store_cancelled.clear()
    A._delay_store_meta.clear()
    A._delay_store_date = '2026-08-06'
    A._delay_store_sb_loaded_date = set()
    A._merge_into_delay_store([row], '2026-08-06', 'FRA')
    key = ('2026-08-06', 'FRA', 'LH42', '09:35')
    return key, writes


def _lh42(**updates):
    row = {
        'flight': 'LH42',
        'sched': '2026-08-06T09:35:00',
        'esti': '',
        'status': 'Geplant',
        'delay_min': 0,
        'cancelled': False,
        'dest_iata': 'HAJ',
        'airline': 'LH',
    }
    row.update(updates)
    return row


def test_plan_row_30_minutes_after_schedule_stays_unknown(monkeypatch):
    key, writes = _run(monkeypatch, _lh42())
    assert key not in A._delay_store
    assert A._delay_store_meta[key]['delay_known'] is False
    assert writes == []


def test_actual_departure_can_persist_zero_delay(monkeypatch):
    key, writes = _run(monkeypatch, _lh42(status='Abgeflogen'))
    assert A._delay_store[key] == 0
    assert A._delay_store_meta[key]['delay_known'] is True
    assert writes


def test_positive_delay_is_persisted_without_actual_status(monkeypatch):
    key, writes = _run(monkeypatch, _lh42(delay_min=26))
    assert A._delay_store[key] == 26
    assert A._delay_store_meta[key]['delay_known'] is True
    assert writes


def test_codeshare_fold_keeps_user_facing_iata_flight_number():
    flights = [
        {'flight': 'LH42', 'airline': 'LH', 'sched': '2026-08-06T09:35:00',
         'delay_known': True, 'delay_min': 26, 'obs': 'dep'},
        {'flight': 'DLH42', 'airline': 'DL', 'sched': '2026-08-06T09:35:00',
         'delay_known': True, 'delay_min': 26, 'obs': 'dep'},
    ]
    folded = A._fold_codeshare_flights(flights, {'LH42': 'DLH42'})
    assert len(folded) == 1
    assert folded[0]['flight'] == 'LH42'
    assert folded[0]['airline'] == 'LH'
    assert 'DLH42' in folded[0]['also_as']


def test_track_match_accepts_operating_alias_after_fold():
    entry = {'flight': 'LH42', 'also_as': ['DLH42']}
    keys = A._route_entry_track_keys(entry, {'LH42': 'DLH42'})
    assert {'LH42', 'DLH42'} <= keys
    assert keys & {'DLH42'}


def test_historical_punctuality_excludes_unknown_zero(monkeypatch):
    rows = [
        _lh42(),
        _lh42(flight='LH44', status='Abgeflogen', delay_min=0,
              max_delay_min=0),
        _lh42(flight='LH46', status='Delayed', delay_min=20,
              max_delay_min=20),
    ]
    monkeypatch.setattr(A, '_delay_obs_rows_for_date', lambda *a, **k: rows)
    stats = A._punctuality_stats_from_obs('2026-08-06', 'FRA')
    assert stats['scheduled_total'] == 3
    assert stats['unknown'] == 1
    assert stats['total'] == 2
    assert stats['on_time'] == 1
    assert stats['delayed'] == 1
    assert stats['on_time_pct'] == 50
