"""Redeem-Invite = Sofort-Verbindung + Push an den Einladenden (T4 2026-07-12).

Vorher: redeem_friend_invite legte nur eine pending-Anfrage an — der
Einladende bekam keinen dedizierten Redeem-Push und musste den Scan seines
EIGENEN, signierten QR-Codes (= seine Zustimmung, TTL 15 min) noch manuell
annehmen. Jetzt:
  • Scan verbindet DIREKT (beide accepted-Kanten, Muster accept_friend_request).
  • GENAU EIN Push an den Aussteller: „X ist jetzt mit dir verbunden."
    (type friend_accept → Pref friend_accepted); der friend_request-Push des
    Cores ist im Redeem-Pfad unterdrückt (notify=False, kein Doppel-Push).
  • Bestands-Freundschaft/Block bleiben still (kein Lügen-Push).

KEIN echtes APNs/SB: _push_notify_async + friends-Persistenz sind gemockt.
"""
import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import sys
from unittest.mock import patch

import pytest

import app as A

ISSUER = 'AT-REDEEM-ISSUER-1'
SCANNER = 'AT-REDEEM-SCANNER-1'


@pytest.fixture(autouse=True)
def _pin_app():
    prev = sys.modules.get('app')
    sys.modules['app'] = A
    yield
    if prev is not None:
        sys.modules['app'] = prev


class _MemFriends:
    """In-Memory-Ersatz für _friends_load/_friends_save*(SB aus)."""

    def __init__(self):
        self.store = {}

    def load(self, tok):
        return self.store.setdefault(
            tok, {'friends': [], 'requests_in': [], 'requests_out': []})

    def save(self, tok, data):
        self.store[tok] = data


def _redeem(mem, pushes, invite, scanner=SCANNER,
            blocked=None, rate_limited=False):
    with patch.object(A, '_friends_load', side_effect=mem.load), \
         patch.object(A, '_friends_save', side_effect=mem.save), \
         patch.object(A, '_friends_save_disk_only', side_effect=mem.save), \
         patch.object(A, 'SB_AVAILABLE', False), \
         patch.object(A, '_token_rate_limited',
                      return_value=rate_limited), \
         patch.object(A, '_blocked_by',
                      return_value=set(blocked or [])), \
         patch.object(A, '_profile_load',
                      return_value={'profile': {'name': 'Miguel'}}), \
         patch.object(A, '_push_notify_async',
                      side_effect=lambda *a, **k: pushes.append((a, k))):
        with A.app.test_request_context(
                f'/api/user/friend-requests/{scanner}/redeem-invite',
                method='POST', json={'invite': invite}):
            return A.redeem_friend_invite(scanner)


def test_redeem_verbindet_direkt_und_pusht_friend_accept():
    mem, pushes = _MemFriends(), []
    invite = A._make_friend_invite(ISSUER)
    resp = _redeem(mem, pushes, invite)
    body = resp.get_json() if not isinstance(resp, tuple) else resp[0].get_json()
    assert body.get('ok') is True
    assert body.get('connected') is True
    # Beidseitig verbunden — keine hängende pending-Anfrage mehr.
    assert SCANNER in mem.load(ISSUER)['friends']
    assert ISSUER in mem.load(SCANNER)['friends']
    assert SCANNER not in mem.load(ISSUER)['requests_in']
    assert ISSUER not in mem.load(SCANNER)['requests_out']
    # GENAU EIN Push, an den AUSSTELLER, mit friend_accept-Typ (Pref
    # friend_accepted) und dem geforderten Text.
    assert len(pushes) == 1
    args, kwargs = pushes[0]
    assert args[0] == ISSUER
    assert 'ist jetzt mit dir verbunden' in args[2]
    assert (kwargs.get('data') or {}).get('type') == 'friend_accept'
    assert A._PUSH_TYPE_TO_PREF['friend_accept'] == 'friend_accepted'


def test_redeem_bestandsfreundschaft_kein_push():
    mem, pushes = _MemFriends(), []
    mem.store[SCANNER] = {'friends': [ISSUER], 'requests_in': [],
                          'requests_out': []}
    mem.store[ISSUER] = {'friends': [SCANNER], 'requests_in': [],
                         'requests_out': []}
    invite = A._make_friend_invite(ISSUER)
    resp = _redeem(mem, pushes, invite)
    body = resp.get_json() if not isinstance(resp, tuple) else resp[0].get_json()
    assert body.get('already_friends') is True
    assert pushes == []          # keine NEUE Verbindung → kein Push


def test_redeem_block_bleibt_still():
    mem, pushes = _MemFriends(), []
    invite = A._make_friend_invite(ISSUER)
    resp = _redeem(mem, pushes, invite, blocked=[SCANNER])
    body = resp.get_json() if not isinstance(resp, tuple) else resp[0].get_json()
    assert body.get('silenced') is True
    assert pushes == []
    assert SCANNER not in mem.load(ISSUER)['friends']


def test_redeem_rate_limit_reicht_429_durch():
    mem, pushes = _MemFriends(), []
    invite = A._make_friend_invite(ISSUER)
    resp = _redeem(mem, pushes, invite, rate_limited=True)
    assert isinstance(resp, tuple) and resp[1] == 429
    assert pushes == []


def test_redeem_ungueltiger_invite_400():
    mem, pushes = _MemFriends(), []
    resp = _redeem(mem, pushes, 'kaputt')
    assert isinstance(resp, tuple) and resp[1] == 400
    assert pushes == []


def test_redeem_eigener_invite_400():
    mem, pushes = _MemFriends(), []
    invite = A._make_friend_invite(SCANNER)
    resp = _redeem(mem, pushes, invite)
    assert isinstance(resp, tuple) and resp[1] == 400
    assert pushes == []


def test_send_friend_request_pfad_pusht_weiter_friend_request():
    """Regressions-Schutz: der normale /send-Pfad (notify=True Default)
    schickt weiterhin die „möchte dir folgen"-Anfrage."""
    mem, pushes = _MemFriends(), []
    with patch.object(A, '_friends_load', side_effect=mem.load), \
         patch.object(A, '_friends_save', side_effect=mem.save), \
         patch.object(A, '_friends_save_disk_only', side_effect=mem.save), \
         patch.object(A, 'SB_AVAILABLE', False), \
         patch.object(A, '_token_rate_limited', return_value=False), \
         patch.object(A, '_blocked_by', return_value=set()), \
         patch.object(A, '_profile_load',
                      return_value={'profile': {'name': 'Miguel'}}), \
         patch.object(A, '_push_notify_async',
                      side_effect=lambda *a, **k: pushes.append((a, k))):
        with A.app.test_request_context(
                f'/api/user/friend-requests/{SCANNER}/send', method='POST',
                json={'friend_token': ISSUER}):
            A.send_friend_request(SCANNER)
    assert len(pushes) == 1
    args, kwargs = pushes[0]
    assert args[0] == ISSUER
    assert (kwargs.get('data') or {}).get('type') == 'friend_request'
    # pending, NICHT verbunden (Accept bleibt beim Empfänger).
    assert ISSUER not in mem.load(SCANNER)['friends']
