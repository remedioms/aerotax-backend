"""MetricKit deliveries are durable and exactly-once by event identity."""

import os
import sys


os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import app as A  # noqa: E402


PAYLOAD = {
    'kind': 'hang', 'app_version': '2.2.8', 'build': '340',
    'os': 'iOS 26.0', 'device': 'iPhone17,2',
    'ts_begin': '2026-08-15T10:00:00Z',
    'ts_end': '2026-08-15T10:00:08Z', 'hang_duration_s': 8,
}


def test_event_key_is_stable_but_user_scoped():
    shuffled = dict(reversed(list(PAYLOAD.items())))
    assert A._mk_event_key(PAYLOAD, 'AT-A') == A._mk_event_key(shuffled, 'AT-A')
    assert A._mk_event_key(PAYLOAD, 'AT-A') != A._mk_event_key(PAYLOAD, 'AT-B')


def test_event_key_ignores_metrickit_delivery_window_but_not_event_facts():
    adjacent_window = dict(PAYLOAD, ts_begin='2026-08-15T10:01:00Z',
                           ts_end='2026-08-15T10:01:08Z')
    different_hang = dict(adjacent_window, hang_duration_s=9)
    assert A._mk_event_key(PAYLOAD, 'AT-A') == A._mk_event_key(adjacent_window, 'AT-A')
    assert A._mk_event_key(PAYLOAD, 'AT-A') != A._mk_event_key(different_hang, 'AT-A')


def test_duplicate_delivery_is_acknowledged_without_insert_or_alert(monkeypatch):
    monkeypatch.setattr(A, 'SB_AVAILABLE', True)
    monkeypatch.setattr(A, '_mk_event_exists', lambda _key: True)
    monkeypatch.setattr(
        A, '_mk_should_alert',
        lambda *_args: (_ for _ in ()).throw(
            AssertionError('duplicate must not enter alert gate')))

    class _SB:
        def table(self, _name):
            raise AssertionError('duplicate must not insert')

    monkeypatch.setattr(A, 'sb', _SB())
    response = A.app.test_client().post(
        '/api/telemetry/diagnostics', json=PAYLOAD,
        headers={'Authorization': 'Bearer AT-A'})
    assert response.status_code == 200
    assert response.get_json() == {
        'ok': True, 'stored': True, 'duplicate': True}
