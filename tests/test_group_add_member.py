"""Layover-Gruppe: EINE Person direkt hinzufügen (KEINE Freundschaft nötig).

Owner-Entscheidung 2026-07-16: Layover-Chats sind Zweck-Gruppen auf Zeit. In der
Einladen-Suche fügt der „Hinzufügen"-Button jede gefundene Person DIREKT zur
Gruppe hinzu — keine Freundschafts-Anfrage. `POST
/api/user/friend-groups/<token>/<group_id>/add-member` Body {target_token}.

Regeln getestet:
  • Mitglied (Owner ODER in members) fügt einen Fremden hinzu ✓ (+ Push).
  • Nicht-Mitglied ⇒ 403 not_a_member.
  • Family-Target ⇒ 400 family_account_not_addable.
  • Idempotent: schon Mitglied ⇒ ok, kein zweiter Push.

KEIN echtes SB/APNs — Row-Lookup, Family-Check, Push sind gemockt.
"""
import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import sys
from unittest.mock import patch

import pytest

import app as A

OWNER = 'AT-GRP-OWNER-1'
MEMBER = 'AT-GRP-MEMBER-1'
STRANGER = 'AT-GRP-STRANGER-1'   # Nicht-Mitglied
TARGET = 'AT-GRP-TARGET-1'       # neu hinzuzufügen
FAMILY = 'AT-GRP-FAMILY-1'
GID = 'abc12345'


@pytest.fixture(autouse=True)
def _pin_app():
    prev = sys.modules.get('app')
    sys.modules['app'] = A
    yield
    if prev is not None:
        sys.modules['app'] = prev


def _call(caller, target, *, row, family_targets=None, updates=None, pushes=None):
    """Ruft den Endpoint direkt auf (umgeht before_request-Gate wie die anderen
    Social-Tests). `row` = die Gruppen-Zeile die _friend_group_row_by_id liefert
    (None ⇒ group_not_found). `updates` sammelt SB-member-Updates, `pushes` die
    Push-Enqueues."""
    updates = updates if updates is not None else []
    pushes = pushes if pushes is not None else []
    fam = set(family_targets or [])

    class _Tbl:
        def update(self, payload):
            self._payload = payload
            return self

        def eq(self, *a, **k):
            return self

        def execute(self):
            updates.append(self._payload)
            return self

    class _SB:
        def table(self, *a, **k):
            return _Tbl()

    with patch.object(A, '_friend_group_row_by_id', return_value=row), \
         patch.object(A, '_is_family_account',
                      side_effect=lambda t: t in fam), \
         patch.object(A, 'SB_AVAILABLE', True), \
         patch.object(A, 'sb', _SB()), \
         patch.object(A, '_profile_load',
                      return_value={'profile': {'name': 'Miguel'}}), \
         patch.object(A, '_push_token_ref', side_effect=lambda t: t[-4:]), \
         patch.object(A, '_push_outbox_enqueue',
                      side_effect=lambda *a, **k: pushes.append((a, k)) or 'push1'):
        with A.app.test_request_context(
                f'/api/user/friend-groups/{caller}/{GID}/add-member',
                method='POST', json={'target_token': target}):
            resp = A.add_member_to_group(caller, GID)
    body = resp[0].get_json() if isinstance(resp, tuple) else resp.get_json()
    status = resp[1] if isinstance(resp, tuple) else 200
    return status, body, updates, pushes


def _row(members):
    return {'id': GID, 'owner_token': OWNER, 'name': 'JFK Layover',
            'members': list(members)}


def test_member_adds_stranger_ok_and_pushes():
    status, body, updates, pushes = _call(
        MEMBER, TARGET, row=_row([MEMBER]))
    assert status == 200
    assert body['ok'] is True
    assert body['already_member'] is False
    # Ziel landet in members (für Push-Fanout).
    assert updates and TARGET in updates[-1]['members']
    assert MEMBER in updates[-1]['members']
    # Genau ein „hinzugefügt"-Push ans Ziel.
    assert len(pushes) == 1
    assert pushes[0][0][0] == TARGET
    assert pushes[0][1]['data']['type'] == 'group_added'
    assert pushes[0][1]['data']['group_id'] == GID


def test_owner_adds_stranger_ok():
    status, body, updates, _ = _call(OWNER, TARGET, row=_row([]))
    assert status == 200 and body['ok'] is True
    assert updates and TARGET in updates[-1]['members']


def test_non_member_forbidden():
    status, body, updates, pushes = _call(
        STRANGER, TARGET, row=_row([MEMBER]))
    assert status == 403
    assert body['error'] == 'not_a_member'
    assert updates == []   # nichts geschrieben
    assert pushes == []


def test_family_target_rejected():
    status, body, updates, pushes = _call(
        OWNER, FAMILY, row=_row([]), family_targets={FAMILY})
    assert status == 400
    assert body['error'] == 'family_account_not_addable'
    assert updates == []
    assert pushes == []


def test_idempotent_already_member_no_second_push():
    # TARGET ist bereits Mitglied → ok, KEIN erneutes Update/Push.
    status, body, updates, pushes = _call(
        OWNER, TARGET, row=_row([TARGET]))
    assert status == 200
    assert body['ok'] is True
    assert body['already_member'] is True
    assert updates == []
    assert pushes == []


def test_missing_target_400():
    status, body, _, _ = _call(OWNER, '', row=_row([]))
    assert status == 400
    assert body['error'] == 'target_token_required'


def test_group_not_found_404():
    status, body, _, _ = _call(OWNER, TARGET, row=None)
    assert status == 404
    assert body['error'] == 'group_not_found'
