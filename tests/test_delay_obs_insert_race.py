"""Regression: zwei Poller dürfen beim manuellen Delay-Upsert nicht kollidieren."""

from types import SimpleNamespace

import app as A
from blueprints import poll_scheduler


class _RaceQuery:
    def __init__(self):
        self.mode = None
        self.update_calls = 0
        self.insert_calls = 0

    def update(self, _payload):
        self.mode = 'update'
        return self

    def insert(self, _payload):
        self.mode = 'insert'
        return self

    def eq(self, *_args):
        return self

    def execute(self):
        if self.mode == 'insert':
            self.insert_calls += 1
            raise RuntimeError(
                'duplicate key value violates unique constraint '
                '"airport_delay_obs_pkey" (SQLSTATE 23505)')
        self.update_calls += 1
        return SimpleNamespace(data=[] if self.update_calls == 1 else [{'ok': True}])


class _RaceSupabase:
    def __init__(self):
        self.query = _RaceQuery()

    def table(self, name):
        assert name == 'airport_delay_obs'
        return self.query


def test_duplicate_insert_race_retries_update_instead_of_requeue(monkeypatch):
    fake = _RaceSupabase()
    monkeypatch.setattr(A, 'SB_AVAILABLE', True)
    monkeypatch.setattr(A, 'sb', fake)
    monkeypatch.setattr(poll_scheduler, 'obs_write_needed', lambda _payload: True)
    monkeypatch.setattr(poll_scheduler, 'obs_mark_written', lambda _payload: None)
    monkeypatch.setattr(A, '_delay_obs_requeue',
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            AssertionError('23505 darf nicht requeued werden')))

    assert A._delay_obs_write_through(
        '2026-08-16', 'LH400', '09:00', 7, False, 'FRA', 'departed') is True
    assert fake.query.insert_calls == 1
    assert fake.query.update_calls == 2
