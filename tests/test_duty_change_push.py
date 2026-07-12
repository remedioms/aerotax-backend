"""Dienstplan-Push mit Lockscreen-Buttons (T3, 2026-07-12).

Der roster_change-Push sendete weder `aps.category` noch `roster_change_id` —
die iOS-Buttons (PushService, UNNotificationCategory 'DUTY_CHANGE' mit
DUTY_ACCEPT/DUTY_REJECT) waren dadurch nie sichtbar/funktional. Jetzt:

  • _send_apns trägt optional `category` als aps.category im Payload.
  • take_roster_snapshot sendet category='DUTY_CHANGE' + roster_change_id
    (== datum des Changes bei genau EINER Änderung, sonst '*' = alle pending —
    beides versteht /api/user/roster-changes/<token>/decide, den der iOS-
    Handler via respondRosterChange ruft).
  • Die Buttons bestätigen NUR Kenntnisnahme (pending → history), sie mutieren
    NIE den Dienstplan selbst — decide fasst nur das Changes-Log + die
    Snapshot-Baseline an.

KEIN echtes APNs/SB: httpx bzw. _push_notify_async werden gemockt.
"""
import json
import os
import sys
import types
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

from unittest.mock import patch, MagicMock

import pytest

import app as A


@pytest.fixture(autouse=True)
def _pin_app():
    _prev = sys.modules.get('app')
    sys.modules['app'] = A
    yield
    if _prev is not None:
        sys.modules['app'] = _prev


# ══════════════════════════════════════════════════════════════════════════════
# _send_apns: aps.category
# ══════════════════════════════════════════════════════════════════════════════
def _run_send_apns(**kwargs):
    """Führt _send_apns mit gefaktem httpx aus und liefert den gesendeten
    JSON-Payload zurück."""
    sent = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, content=None):
            sent['payload'] = json.loads(content.decode('utf-8'))
            return _Resp()

    fake_httpx = types.ModuleType('httpx')
    fake_httpx.Client = _Client
    with patch.dict(sys.modules, {'httpx': fake_httpx}), \
            patch.object(A, '_apns_get_jwt', return_value='jwt-test'):
        ok, reason = A._send_apns('feedfacefeedface', 'Titel', 'Body', **kwargs)
    assert ok is True and reason is None
    return sent['payload']


def test_send_apns_sets_category():
    payload = _run_send_apns(category='DUTY_CHANGE',
                             data={'type': 'roster_change',
                                   'roster_change_id': '2026-07-15'})
    assert payload['aps']['category'] == 'DUTY_CHANGE'
    # data-Keys landen top-level im Payload (iOS liest userInfo["roster_change_id"]).
    assert payload['roster_change_id'] == '2026-07-15'
    assert payload['type'] == 'roster_change'


def test_send_apns_without_category_unchanged():
    payload = _run_send_apns(data={'type': 'dm'})
    assert 'category' not in payload['aps']


# ══════════════════════════════════════════════════════════════════════════════
# take_roster_snapshot: category + roster_change_id am Push
# ══════════════════════════════════════════════════════════════════════════════
def _snapshot_env(tmp_path, old_tage):
    """Patcht Snapshot-Read/-Save + Changes-Pfad auf tmp und fängt den Push."""
    changes_file = tmp_path / 'roster_changes_test.json'
    push = MagicMock()
    return (
        patch.object(A, '_roster_snapshot_read',
                     return_value={'tage': old_tage} if old_tage else {}),
        patch.object(A, '_roster_snapshot_save', return_value=True),
        patch.object(A, '_roster_snapshot_path',
                     return_value=str(tmp_path / 'snap.json')),
        patch.object(A, '_roster_changes_path', return_value=str(changes_file)),
        patch.object(A, '_crew_flight_ingest', return_value=None),
        patch.object(A, '_push_notify_async', push),
        push,
        changes_file,
    )


def _tag(datum, klass='Flug', routing='FRA-JFK'):
    return {'datum': datum, 'klass': klass, 'routing': routing}


def test_single_change_pushes_datum_as_change_id(tmp_path):
    old = [_tag('2026-07-15', routing='FRA-JFK')]
    new = [_tag('2026-07-15', routing='FRA-MIA')]     # modified
    p1, p2, p3, p4, p5, p6, push, _cf = _snapshot_env(tmp_path, old)
    with p1, p2, p3, p4, p5, p6:
        client = A.app.test_client()
        r = client.post('/api/user/roster-snapshot/testtoken123',
                        json={'tage': new})
    assert r.status_code == 200
    assert r.get_json()['changes_count'] == 1
    assert push.call_count == 1
    kwargs = push.call_args.kwargs
    assert kwargs['category'] == 'DUTY_CHANGE'
    assert kwargs['data']['type'] == 'roster_change'
    assert kwargs['data']['roster_change_id'] == '2026-07-15'


def test_multiple_changes_push_bulk_change_id(tmp_path):
    old = [_tag('2026-07-15'), _tag('2026-07-16')]
    new = [_tag('2026-07-15', routing='FRA-MIA'),
           _tag('2026-07-16', routing='FRA-GRU')]
    p1, p2, p3, p4, p5, p6, push, _cf = _snapshot_env(tmp_path, old)
    with p1, p2, p3, p4, p5, p6:
        client = A.app.test_client()
        r = client.post('/api/user/roster-snapshot/testtoken123',
                        json={'tage': new})
    assert r.status_code == 200
    assert r.get_json()['changes_count'] == 2
    kwargs = push.call_args.kwargs
    assert kwargs['category'] == 'DUTY_CHANGE'
    assert kwargs['data']['roster_change_id'] == '*'


def test_first_baseline_sends_no_push(tmp_path):
    # Erster Import = nur Baseline, kein Push-Spam (bestehende Regel).
    p1, p2, p3, p4, p5, p6, push, _cf = _snapshot_env(tmp_path, old_tage=None)
    with p1, p2, p3, p4, p5, p6:
        client = A.app.test_client()
        r = client.post('/api/user/roster-snapshot/testtoken123',
                        json={'tage': [_tag('2026-07-15')]})
    assert r.status_code == 200
    assert push.call_count == 0


# ══════════════════════════════════════════════════════════════════════════════
# Round-Trip: die vom Push gelieferte ID versteht der decide-Endpoint
# ══════════════════════════════════════════════════════════════════════════════
def test_decide_accepts_pushed_datum_id(tmp_path):
    old = [_tag('2026-07-15')]
    new = [_tag('2026-07-15', routing='FRA-MIA')]
    p1, p2, p3, p4, p5, p6, push, changes_file = _snapshot_env(tmp_path, old)
    with p1, p2, p3, p4, p5, p6:
        client = A.app.test_client()
        client.post('/api/user/roster-snapshot/testtoken123', json={'tage': new})
        change_id = push.call_args.kwargs['data']['roster_change_id']
        # iOS DUTY_ACCEPT → respondRosterChange → decide {datum, decision}.
        r = client.post('/api/user/roster-changes/testtoken123/decide',
                        json={'datum': change_id, 'decision': 'accept'})
    assert r.status_code == 200
    data = json.loads(changes_file.read_text())
    # Kenntnisnahme: pending → history, KEINE Roster-Mutation.
    assert data['pending'] == []
    assert data['history'][-1]['datum'] == '2026-07-15'
    assert data['history'][-1]['status'] == 'accepted'


def test_decide_accepts_bulk_star_id(tmp_path):
    old = [_tag('2026-07-15'), _tag('2026-07-16')]
    new = [_tag('2026-07-15', routing='FRA-MIA'),
           _tag('2026-07-16', routing='FRA-GRU')]
    p1, p2, p3, p4, p5, p6, push, changes_file = _snapshot_env(tmp_path, old)
    with p1, p2, p3, p4, p5, p6:
        client = A.app.test_client()
        client.post('/api/user/roster-snapshot/testtoken123', json={'tage': new})
        change_id = push.call_args.kwargs['data']['roster_change_id']
        assert change_id == '*'
        r = client.post('/api/user/roster-changes/testtoken123/decide',
                        json={'datum': change_id, 'decision': 'reject'})
    assert r.status_code == 200
    assert r.get_json()['decided'] == 2
    data = json.loads(changes_file.read_text())
    assert data['pending'] == []
    assert {h['status'] for h in data['history']} == {'rejected'}
