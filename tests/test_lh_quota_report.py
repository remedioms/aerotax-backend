"""Regressionen fuer den taeglichen LH-Quotenreport.

Open API und FlightOps laufen mit getrennten Keys und unterschiedlichen
Limits. Ein hoher interner Skip-Zaehler ist kein Provider-Request.
"""
import runpy
import sys
import types
from pathlib import Path


REPORT = Path(__file__).parents[1] / 'ops' / 'hetzner' / 'lh_quota_report.py'


def _run_report(monkeypatch, capsys, rows):
    fake = types.ModuleType('blueprints.aerox_data_blueprint')
    fake.lh_quota_snapshot = lambda _hours: {'hours': rows}
    monkeypatch.setitem(sys.modules, 'blueprints.aerox_data_blueprint', fake)
    runpy.run_path(str(REPORT), run_name='__main__')
    return capsys.readouterr().out


def test_flightops_1500_and_large_skip_count_are_not_over_quota(
        monkeypatch, capsys):
    out = _run_report(monkeypatch, capsys, [{
        'hour_utc': '2026082201',
        'keys': {
            'lhopen': {'total': 431, 'callers': {}},
            'lhopen_skip': {'total': 4100, 'callers': {'shared_hit': 4100}},
            'lhfo': {'total': 1524,
                     'callers': {'common_duty_events': 1500}},
        },
    }])

    assert 'Peak 431/1000/h' in out
    assert 'Peak 1524/20000/h' in out
    assert 'UEBER DEM PROVIDER-KONTINGENT' not in out
    assert 'lhopen_skip  4100' not in out


def test_only_real_sent_family_can_trigger_provider_alarm(monkeypatch, capsys):
    out = _run_report(monkeypatch, capsys, [{
        'hour_utc': '2026082202',
        'keys': {
            'lhopen': {'total': 1000, 'callers': {'obs_merge': 1000}},
            'lhfo': {'total': 19999, 'callers': {}},
        },
    }])

    assert 'UEBER DEM PROVIDER-KONTINGENT:' in out
    assert '2026082202 UTC  lhopen  1000' in out
    assert '2026082202 UTC  lhfo' not in out
