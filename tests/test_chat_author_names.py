"""Absender-Namen im Crew-Chat (Forum-Meldung Till Becke, 2026-08-01).

Till (Captain, LH/FRA): „Beim Crewchat stehen Kürzel wie CC und CA, aber nicht
die Namen." Ursache war NICHT ein Rang-Code, sondern ein Namens-Platzhalter im
Client, dessen Initialen wie ein Dienst-Kürzel aussahen. Serverseitig fehlte
schlicht der Name: die Nachricht trug nur den GEKÜRZTEN `author_token`, und den
konnte der Client nur gegen die eigene Freundesliste auflösen — in einem
Crew-Chat ist der Absender aber meistens kein Freund.

Zwei Wege, beide hier abgesichert:
  1. Beim SENDEN wird der Name gestempelt (`author_name`) — für alles Neue.
  2. Beim LESEN wird der Bestand nachaufgelöst (`_chat_author_identities`) —
     ohne das bliebe der GESAMTE bestehende Verlauf kryptisch.
"""
from unittest.mock import patch

import app as A


TOKEN = 'AT-B0B1F772711A4009'          # Till Becke (user_profiles, verifiziert)
TRUNC = TOKEN[:16] + '…'               # 'AT-B0B1F772711A4…'
CHANNEL = 'group__788e6e5b'


def _clear_cache():
    with A._chat_author_cache_lock:
        A._chat_author_cache.clear()


class _FakeQuery:
    """Minimaler PostgREST-Builder-Stub (select→like→limit→execute)."""

    def __init__(self, rows, seen):
        self._rows, self._seen = rows, seen

    def select(self, *a, **k):
        return self

    def like(self, col, pattern):
        self._seen.append((col, pattern))
        return self

    def limit(self, n):
        return self

    def execute(self):
        return type('R', (), {'data': self._rows})()


def _sb_returning(rows, seen=None):
    seen = seen if seen is not None else []
    fake = type('SB', (), {'table': lambda self, name: _FakeQuery(rows, seen)})()
    return fake, seen


# ── 1. Nachauflösung des Bestands ──────────────────────────────────────────

def test_alte_nachricht_bekommt_echten_namen_ueber_prefix():
    """Der gekürzte Token wird per PK-Prefix auf das volle Profil aufgelöst."""
    _clear_cache()
    sb, seen = _sb_returning([{'token': TOKEN, 'name': 'Till Becke',
                               'avatar_url': 'u/av.jpg'}])
    with patch.object(A, 'SB_AVAILABLE', True), patch.object(A, 'sb', sb):
        out = A._chat_author_identities([TRUNC])
    assert out[TRUNC]['name'] == 'Till Becke'
    assert out[TRUNC]['avatar_url'] == 'u/av.jpg'
    # Das '…' darf NIE Teil des LIKE-Musters sein.
    assert seen == [('token', 'AT-B0B1F772711A4%')]


def test_mehrdeutiger_prefix_liefert_KEINEN_namen():
    """Zwei Profile mit gleichem 16-Zeichen-Prefix ⇒ lieber gar kein Name als
    ein falscher an einer fremden Nachricht."""
    _clear_cache()
    sb, _ = _sb_returning([
        {'token': TOKEN, 'name': 'Till Becke'},
        {'token': 'AT-B0B1F772711A4999', 'name': 'Jemand Anderes'},
    ])
    with patch.object(A, 'SB_AVAILABLE', True), patch.object(A, 'sb', sb):
        out = A._chat_author_identities([TRUNC])
    assert out[TRUNC] == {}


def test_profil_ohne_namen_liefert_keinen_namen():
    """141 der 2454 Profile haben einen leeren Namen — daraus darf kein
    Platzhalter-Kürzel gebaut werden, der Client zeigt seinen ehrlichen."""
    _clear_cache()
    sb, _ = _sb_returning([{'token': TOKEN, 'name': '   '}])
    with patch.object(A, 'SB_AVAILABLE', True), patch.object(A, 'sb', sb):
        out = A._chat_author_identities([TRUNC])
    assert 'name' not in out[TRUNC]


def test_like_injection_wird_abgewiesen():
    """'%' im Token würde fremde Profile matchen → gar nicht erst abfragen."""
    _clear_cache()
    sb, seen = _sb_returning([{'token': 'AT-EGAL', 'name': 'Fremd'}])
    with patch.object(A, 'SB_AVAILABLE', True), patch.object(A, 'sb', sb):
        out = A._chat_author_identities(['AT-%…'])
    assert out['AT-%…'] == {}
    assert seen == []


def test_sb_fehler_wird_nicht_gecacht_und_wirft_nicht():
    """Ein SB-Aussetzer darf nicht 5 Minuten lang „kein Name" einfrieren."""
    _clear_cache()

    class Boom:
        def table(self, name):
            raise RuntimeError('sb down')

    with patch.object(A, 'SB_AVAILABLE', True), patch.object(A, 'sb', Boom()):
        out = A._chat_author_identities([TRUNC])
    assert out == {}
    with A._chat_author_cache_lock:
        assert TRUNC not in A._chat_author_cache


def test_cache_verhindert_zweite_abfrage():
    _clear_cache()
    sb, seen = _sb_returning([{'token': TOKEN, 'name': 'Till Becke'}])
    with patch.object(A, 'SB_AVAILABLE', True), patch.object(A, 'sb', sb):
        A._chat_author_identities([TRUNC])
        A._chat_author_identities([TRUNC])
    assert len(seen) == 1


# ── 2. Der Leseweg als Ganzes ──────────────────────────────────────────────

def test_get_chat_messages_fuellt_namen_fuer_alte_nachrichten():
    _clear_cache()
    alt = {'id': 'm1', 'channel_id': CHANNEL, 'author_token': TRUNC,
           'text': 'Moin', 'ts': 1.0, 'iso': '2026-07-20T10:00:00'}
    with (
        A.app.test_request_context(method='GET'),
        patch.object(A, '_chat_path', return_value='/tmp/c.json'),
        patch.object(A, '_channel_access_error', return_value=None),
        patch.object(A, '_dm_load_recent', return_value=[alt]),
        patch.object(A, '_chat_author_identities',
                     return_value={TRUNC: {'name': 'Till Becke',
                                           'avatar_url': 'u/av.jpg'}}),
    ):
        resp = A.get_chat_messages('AT-LESER-0000000000', CHANNEL)
    msg = resp.get_json()['messages'][0]
    assert msg['author_name'] == 'Till Becke'
    assert msg['author_avatar'] == 'u/av.jpg'
    # Historische Fast-Bearer werden nicht erneut ausgeliefert: stabile,
    # nicht rückrechenbare Legacy-Referenz statt 16/19 Credential-Zeichen.
    assert msg['author_token'].startswith('AXP-')
    assert not msg['author_token'].startswith('AT-')


def test_sende_stempel_gewinnt_ueber_nachaufloesung():
    """Steht der NAME schon auf der Zeile, bleibt er unangetastet.

    GEÄNDERT 2026-08-01: vorher prüfte dieser Test zusätzlich, dass dann GAR
    NICHT nachgeschlagen wird. Das gilt nicht mehr — der AVATAR wird seit
    dieser Runde immer live aufgelöst, weil er sonst auf dem Stand des Sendens
    einfriert (Owner: im Hangout-Chat klebte das Logo von vor drei Uploads,
    während die Karte darüber das aktuelle zeigte). Der Resolver ist gecacht,
    die Auflösung kostet also nicht pro Verlauf eine Abfrage je Autor.

    Der Name bleibt gestempelt: er ist der Name zum Sendezeitpunkt.
    Siehe tests/test_chat_avatar_live.py.
    """
    _clear_cache()
    neu = {'id': 'm2', 'channel_id': CHANNEL, 'author_token': TRUNC,
           'text': 'Neu', 'ts': 2.0, 'iso': '2026-08-01T10:00:00',
           'author_name': 'Till Becke'}
    with (
        A.app.test_request_context(method='GET'),
        patch.object(A, '_chat_path', return_value='/tmp/c.json'),
        patch.object(A, '_channel_access_error', return_value=None),
        patch.object(A, '_dm_load_recent', return_value=[neu]),
        # return_value MUSS ein Dict sein — die echte Funktion liefert
        # {token: {'name','avatar_url'}}. Ein nackter MagicMock waere hier
        # wahrheitswertig und landete als Avatar im JSON.
        patch.object(A, '_chat_author_identities', return_value={}) as resolve,
    ):
        resp = A.get_chat_messages('AT-LESER-0000000000', CHANNEL)
    assert resp.get_json()['messages'][0]['author_name'] == 'Till Becke'
    # Nachgeschlagen wird jetzt (fuer den Avatar) — der Name darf davon aber
    # nicht ueberschrieben werden, und genau das sichert die Zeile darueber.
    resolve.assert_called_once()


def test_senden_stempelt_den_namen_mit():
    """Neue Nachrichten tragen den Namen ab dem Senden — auch für Empfänger,
    die den Absender nicht als Freund haben."""
    saved = {}
    with (
        A.app.test_request_context(method='POST', json={'text': 'Servus'}),
        patch.object(A, '_chat_path', return_value='/tmp/c.json'),
        patch.object(A, '_channel_access_error', return_value=None),
        patch.object(A, '_dm_load_recent', return_value=[]),
        patch.object(A, '_token_rate_limited', return_value=False),
        patch.object(A, '_chat_push_fanout_async'),
        patch.object(A, '_dm_messages_save_to_supabase', return_value=True),
        patch.object(A, '_profile_load', return_value={
            'profile': {'name': 'Till Becke', 'avatar_url': 'u/av.jpg'}}),
        patch.object(A, '_dm_save_messages',
                     side_effect=lambda cid, msgs: saved.update(m=msgs)),
    ):
        resp = A.send_chat_message(TOKEN, CHANNEL)
    msg = resp.get_json()['message']
    assert msg['author_name'] == 'Till Becke'
    assert msg['author_avatar'] == 'u/av.jpg'
    # Neue Nachrichten tragen eine adressierbare öffentliche Referenz, nie das
    # Login-Credential oder dessen fast vollständigen Präfix.
    assert msg['author_token'].startswith('AXU-')
    assert not msg['author_token'].startswith('AT-')


def test_senden_ohne_profilnamen_stempelt_nichts():
    """Leeres Profil ⇒ kein erfundener Name; der Client zeigt „AeroX-Crew"."""
    with (
        A.app.test_request_context(method='POST', json={'text': 'Hi'}),
        patch.object(A, '_chat_path', return_value='/tmp/c.json'),
        patch.object(A, '_channel_access_error', return_value=None),
        patch.object(A, '_dm_load_recent', return_value=[]),
        patch.object(A, '_token_rate_limited', return_value=False),
        patch.object(A, '_chat_push_fanout_async'),
        patch.object(A, '_dm_messages_save_to_supabase', return_value=True),
        patch.object(A, '_profile_load', return_value={'profile': {'name': ' '}}),
        patch.object(A, '_dm_save_messages'),
    ):
        resp = A.send_chat_message(TOKEN, CHANNEL)
    assert 'author_name' not in resp.get_json()['message']


def test_namensfehler_kostet_niemals_den_verlauf():
    """Wenn die Auflösung explodiert, kommen die Nachrichten trotzdem."""
    _clear_cache()
    alt = {'id': 'm3', 'channel_id': CHANNEL, 'author_token': TRUNC,
           'text': 'Verlauf', 'ts': 3.0, 'iso': '2026-07-20T10:00:00'}
    with (
        A.app.test_request_context(method='GET'),
        patch.object(A, '_chat_path', return_value='/tmp/c.json'),
        patch.object(A, '_channel_access_error', return_value=None),
        patch.object(A, '_dm_load_recent', return_value=[alt]),
        patch.object(A, '_chat_author_identities',
                     side_effect=RuntimeError('boom')),
    ):
        resp = A.get_chat_messages('AT-LESER-0000000000', CHANNEL)
    msgs = resp.get_json()['messages']
    assert len(msgs) == 1 and msgs[0]['text'] == 'Verlauf'
    assert not msgs[0].get('author_name')
