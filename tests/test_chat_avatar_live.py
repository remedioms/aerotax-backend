"""Chat-Avatare kommen LIVE aus dem Profil (Owner 2026-08-01).

Befund: `send` schrieb `author_avatar` als Stempel in die Nachricht, die
History füllte ihn nur nach, wenn er FEHLTE. Ein neu gesetztes Profilfoto kam
damit auf alten Nachrichten nie an — im Hangout-Chat stand das Logo von vor
drei Uploads, während die Hangout-Karte direkt darüber das aktuelle zeigte.

Wall und Forum lösen den Avatar längst live auf; der Chat war der letzte Ort
mit dem alten Snapshot.

Der NAME bleibt bewusst gestempelt: er ist der Name zum Sendezeitpunkt.
"""
import os

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import sys
from unittest.mock import patch

import pytest

import app as A


@pytest.fixture(autouse=True)
def _pin_app():
    prev = sys.modules.get('app')
    sys.modules['app'] = A
    yield
    if prev is not None:
        sys.modules['app'] = prev


TOKEN = 'AT-VIEWER0000000001'
AUTHOR = 'AT-AUTHOR0000000001'
AUTHOR_TRUNC = AUTHOR[:16] + '…'
CHANNEL = 'group__pin_abc123'


def _hole(msgs, idents):
    with patch.object(A, '_chat_path', return_value='x'), \
         patch.object(A, '_channel_access_error', return_value=None), \
         patch.object(A, '_dm_load_recent', return_value=msgs), \
         patch.object(A, '_chat_author_identities', return_value=idents), \
         A.app.test_request_context(f'/api/crew-chat/{TOKEN}/channel/{CHANNEL}'):
        return A.get_chat_messages(TOKEN, CHANNEL).get_json()['messages']


def _msg(**kw):
    m = {'id': 'm1', 'ts': 1.0, 'text': 'Hallo',
         'author_token': AUTHOR_TRUNC, 'author_name': 'AeroX'}
    m.update(kw)
    return m


def test_neues_profilfoto_ersetzt_den_alten_stempel():
    """Der eigentliche Bug."""
    msgs = [_msg(author_avatar='/api/user/avatar/alt/logo-von-frueher.png')]
    out = _hole(msgs, {AUTHOR_TRUNC: {'name': 'AeroX',
                                      'avatar_url': '/api/user/avatar/neu/aktuell.png'}})
    assert out[0]['author_avatar'] == '/api/user/avatar/neu/aktuell.png'


def test_geloeschtes_profilfoto_ueberlebt_nicht():
    """Profil aufgelöst, aber kein Foto → Stempel muss weg."""
    msgs = [_msg(author_avatar='/api/user/avatar/alt/logo.png')]
    out = _hole(msgs, {AUTHOR_TRUNC: {'name': 'AeroX'}})
    assert 'author_avatar' not in out[0]


def test_unaufloesbarer_autor_behaelt_seinen_stempel():
    """Kein Profil gefunden → der alte Stempel ist ehrlicher als nichts."""
    msgs = [_msg(author_avatar='/api/user/avatar/alt/logo.png')]
    out = _hole(msgs, {})
    assert out[0]['author_avatar'] == '/api/user/avatar/alt/logo.png'


def test_name_bleibt_gestempelt():
    """Der Name zum Sendezeitpunkt gewinnt — nur der Avatar wird live."""
    msgs = [_msg(author_name='Marie (damals)',
                 author_avatar='/alt.png')]
    out = _hole(msgs, {AUTHOR_TRUNC: {'name': 'Marie neu',
                                      'avatar_url': '/neu.png'}})
    assert out[0]['author_name'] == 'Marie (damals)'
    assert out[0]['author_avatar'] == '/neu.png'


def test_fehlender_name_wird_weiter_nachgefuellt():
    """Regression: Alt-Nachrichten ohne Stempel bekommen Namen + Avatar."""
    msgs = [_msg(author_name='')]
    out = _hole(msgs, {AUTHOR_TRUNC: {'name': 'AeroX', 'avatar_url': '/neu.png'}})
    assert out[0]['author_name'] == 'AeroX'
    assert out[0]['author_avatar'] == '/neu.png'


def test_leerer_verlauf_ruft_den_resolver_nicht():
    with patch.object(A, '_chat_path', return_value='x'), \
         patch.object(A, '_channel_access_error', return_value=None), \
         patch.object(A, '_dm_load_recent', return_value=[]), \
         patch.object(A, '_chat_author_identities') as res, \
         A.app.test_request_context(f'/api/crew-chat/{TOKEN}/channel/{CHANNEL}'):
        out = A.get_chat_messages(TOKEN, CHANNEL).get_json()['messages']
    assert out == [] and res.call_args[0][0] == []


def test_resolver_fehler_kostet_den_verlauf_nicht():
    """Avatare sind Beiwerk — ein Fehler darf die Nachrichten nie verschlucken."""
    msgs = [_msg(author_avatar='/alt.png')]
    with patch.object(A, '_chat_path', return_value='x'), \
         patch.object(A, '_channel_access_error', return_value=None), \
         patch.object(A, '_dm_load_recent', return_value=msgs), \
         patch.object(A, '_chat_author_identities',
                      side_effect=RuntimeError('db weg')), \
         A.app.test_request_context(f'/api/crew-chat/{TOKEN}/channel/{CHANNEL}'):
        out = A.get_chat_messages(TOKEN, CHANNEL).get_json()['messages']
    assert len(out) == 1 and out[0]['text'] == 'Hallo'
