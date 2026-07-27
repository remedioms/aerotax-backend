"""Pro-Freund-Push-Steuerung (Owner-Auftrag 2026-07-27).

„Zu welchen Freunden bekomme ich Push — und welche Arten?"

Modell (siehe app.py `_PUSH_FRIEND_ART_TO_TYPES`):
  · Stufe je Freund: all (Default) / important / custom / none
  · „Wichtige"  = nur direkte, an MICH gerichtete Interaktionen dieses
    Freundes (DM + Anfragen/Verbindungen). Kein Ambient-Rauschen
    (Gruppen-Chat, Community/Forum/Trade, hangout_nearby).
  · „Keine"     = absolut. Auch DMs dieses Freundes. Der globale dm-Kanal
    bleibt für alle ANDEREN Freunde unberührt.
  · Reihenfolge: globale Kategorie zuerst (aus ist aus), dann Freundes-Gate —
    die Freundes-Stufe schränkt nur ein, sie schaltet nie frei.
  · FAIL-OPEN überall: kein Eintrag / unbekannter Typ / kaputte Struktur
    ⇒ senden.

Kein echtes APNs/Supabase: `_push_delivery_registrations` wird gemockt.
"""
import os
import sys

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

from unittest.mock import patch

import pytest

import app as A


ME = 'AT-recipient-0001'
FRIEND = 'AT-friend-0002'
OTHER = 'AT-friend-0003'


@pytest.fixture(autouse=True)
def _pin_app():
    _prev = sys.modules.get('app')
    sys.modules['app'] = A
    yield
    if _prev is not None:
        sys.modules['app'] = _prev


# ── reine Gate-Logik ────────────────────────────────────────────────────────

def allow(prefs, push_type, actor=FRIEND):
    return A._push_friend_prefs_allow(prefs, actor, push_type)


def test_no_entry_is_fail_open():
    assert allow({}, 'dm') is True
    assert allow({OTHER: {'level': 'none'}}, 'dm') is True


def test_missing_actor_or_broken_structure_is_fail_open():
    assert A._push_friend_prefs_allow({FRIEND: {'level': 'none'}}, None, 'dm') is True
    assert A._push_friend_prefs_allow(None, FRIEND, 'dm') is True
    assert A._push_friend_prefs_allow('kaputt', FRIEND, 'dm') is True
    assert allow({FRIEND: 42}, 'dm') is True
    assert allow({FRIEND: {'level': 'gibtsnicht'}}, 'dm') is True


def test_level_all_lets_everything_through():
    p = {FRIEND: {'level': 'all'}}
    for t in ('dm', 'group_message', 'wall_comment', 'hangout_nearby',
              'friend_request', 'trade_interest'):
        assert allow(p, t) is True, t


def test_level_none_blocks_everything_including_dm():
    """Owner-Entscheidung: „Keine" heißt keine — DMs dieses Freundes inklusive."""
    p = {FRIEND: {'level': 'none'}}
    for t in ('dm', 'group_message', 'group_added', 'friend_request',
              'friend_accept', 'wall_comment', 'forum_reply', 'friend_remind',
              'trade_interest', 'hangout_nearby'):
        assert allow(p, t) is False, t


def test_level_none_also_blocks_unknown_future_types():
    assert allow({FRIEND: {'level': 'none'}}, 'brandneuer_typ') is False


def test_level_none_shorthand_string():
    assert allow({FRIEND: 'none'}, 'dm') is False


def test_level_important_keeps_direct_interactions_only():
    p = {FRIEND: {'level': 'important'}}
    # durch: direkte Interaktionen
    for t in ('dm', 'friend_request', 'buddy_request', 'friend_accept',
              'friend_accepted', 'buddy_accepted'):
        assert allow(p, t) is True, t
    # geblockt: Ambient
    for t in ('group_message', 'group_added', 'wall_comment',
              'wall_comment_reply', 'forum_reply', 'forum_mention',
              'friend_remind', 'trade_interest', 'trade_closed',
              'hangout_nearby'):
        assert allow(p, t) is False, t


def test_level_important_is_fail_open_for_unmapped_types():
    """Nicht per-Freund steuerbare Typen (roster_change & Co.) dürfen an einer
    Stufe unterhalb von „Keine" nie hängenbleiben."""
    p = {FRIEND: {'level': 'important'}}
    assert allow(p, 'roster_change') is True
    assert allow(p, 'flight_update') is True
    assert allow(p, None) is True


def test_level_custom_respects_the_chip_selection():
    p = {FRIEND: {'level': 'custom', 'types': ['dm', 'community']}}
    assert allow(p, 'dm') is True
    assert allow(p, 'forum_reply') is True
    assert allow(p, 'trade_closed') is True
    assert allow(p, 'group_message') is False
    assert allow(p, 'hangout_nearby') is False
    assert allow(p, 'friend_request') is False


def test_level_custom_without_types_is_fail_open_in_the_gate():
    """Die Normalisierung im Endpoint macht daraus 'none'; ein direkt in der DB
    gelandetes custom-ohne-types darf trotzdem nicht still alles fressen."""
    assert allow({FRIEND: {'level': 'custom'}}, 'dm') is True


def test_art_map_only_covers_actor_triggered_types():
    """Selbst ausgelöste Typen dürfen nie per-Freund steuerbar sein — sonst
    könnte eine Freundes-Stufe den eigenen Dienstplan-Push abwürgen."""
    for t in ('roster_change', 'duty_change', 'flight_update',
              'inbound_departure', 'inbound_arrival', 'inbound_delay',
              'family_message', 'family_reply', 'family_reaction'):
        assert t not in A._PUSH_TYPE_TO_FRIEND_ART, t


# ── Normalisierung (was der Endpoint speichert) ─────────────────────────────

def test_normalize_default_all_is_stored_as_nothing():
    assert A._push_friend_pref_normalize({'level': 'all'}) is None
    assert A._push_friend_pref_normalize(None) is None
    assert A._push_friend_pref_normalize('all') is None


def test_normalize_custom_with_all_arts_collapses_to_default():
    everything = list(A._PUSH_FRIEND_ART_KEYS)
    assert A._push_friend_pref_normalize({'level': 'custom',
                                          'types': everything}) is None


def test_normalize_custom_without_types_becomes_none():
    assert A._push_friend_pref_normalize({'level': 'custom', 'types': []}) == \
        {'level': 'none', 'types': []}


def test_normalize_drops_unknown_arts_and_dedupes():
    out = A._push_friend_pref_normalize(
        {'level': 'custom', 'types': ['dm', 'dm', 'quatsch', 'COMMUNITY']})
    assert out == {'level': 'custom', 'types': ['dm', 'community']}


def test_normalize_rejects_unknown_level():
    assert A._push_friend_pref_normalize({'level': 'vielleicht'}) is None


# ── Integration in _send_push_notification ─────────────────────────────────

def _registrations(friend_prefs=None, prefs=None):
    legacy = {'token': ME, 'apns_token': 'AAAA', 'bundle_id': 'aerotax.AeroTax'}
    if friend_prefs is not None:
        legacy['friend_prefs'] = friend_prefs
    if prefs is not None:
        legacy['prefs'] = prefs
    return [legacy], legacy


def _send(friend_prefs=None, prefs=None, push_type='dm', actor=FRIEND):
    with patch.dict(os.environ, {'APNS_AUTH_KEY': 'x'}), \
         patch.object(A, '_push_delivery_registrations',
                      return_value=_registrations(friend_prefs, prefs)), \
         patch.object(A, '_send_apns', return_value=(True, None)) as apns:
        detail = A._send_push_notification(
            ME, 'Titel', 'Body', data={'type': push_type},
            actor_token=actor, _return_detail=True)
    return detail, apns


def test_send_is_suppressed_terminally_for_level_none():
    detail, apns = _send({FRIEND: {'level': 'none'}})
    assert detail['reason'] == 'suppressed_by_friend_preference'
    # terminal ⇒ der Outbox-Worker retried nicht ewig
    assert detail['terminal'] is True and detail['ok'] is True
    apns.assert_not_called()


def test_send_passes_for_a_different_friend():
    detail, apns = _send({OTHER: {'level': 'none'}})
    assert detail['reason'] != 'suppressed_by_friend_preference'
    apns.assert_called_once()


def test_send_without_actor_token_is_never_gated():
    detail, apns = _send({FRIEND: {'level': 'none'}}, actor=None)
    assert detail['reason'] != 'suppressed_by_friend_preference'
    apns.assert_called_once()


def test_send_important_blocks_ambient_but_keeps_dm():
    blocked, _ = _send({FRIEND: {'level': 'important'}},
                       push_type='hangout_nearby')
    assert blocked['reason'] == 'suppressed_by_friend_preference'
    passed, apns = _send({FRIEND: {'level': 'important'}}, push_type='dm')
    assert passed['reason'] != 'suppressed_by_friend_preference'
    apns.assert_called_once()


def test_global_category_wins_over_a_permissive_friend_level():
    """Die Freundes-Stufe schaltet NICHT frei: dm global aus bleibt aus."""
    detail, apns = _send({FRIEND: {'level': 'all'}}, prefs={'dm': False},
                         push_type='dm')
    assert detail['reason'] == 'suppressed_by_preference'
    apns.assert_not_called()


def test_actor_token_never_leaks_into_the_apns_payload():
    """`data` landet 1:1 top-level im APNs-Payload — der Auslöser-Token darf da
    nie auftauchen (er reist nur im server-seitigen Outbox-Payload)."""
    captured = {}

    def _fake_apns(apns_token, title, body, data=None, **kw):
        captured['data'] = dict(data or {})
        return True, None

    with patch.dict(os.environ, {'APNS_AUTH_KEY': 'x'}), \
         patch.object(A, '_push_delivery_registrations',
                      return_value=_registrations({FRIEND: {'level': 'all'}})), \
         patch.object(A, '_send_apns', side_effect=_fake_apns):
        A._send_push_notification(ME, 'T', 'B', data={'type': 'dm'},
                                  actor_token=FRIEND, _return_detail=True)
    assert FRIEND not in captured['data'].values()
    assert 'actor_token' not in captured['data']


# ── Outbox-Transport ───────────────────────────────────────────────────────

def test_enqueue_puts_actor_in_the_payload_not_in_data():
    seen = {}

    class _RPC:
        def execute(self):
            return type('R', (), {'data': {'outbox_id': 'x1', 'inserted': True}})()

    class _SB:
        def rpc(self, name, params):
            seen.update(params)
            return _RPC()

    with patch.object(A, 'SB_AVAILABLE', True), patch.object(A, 'sb', _SB()):
        A._push_outbox_enqueue(ME, 'T', 'B', data={'type': 'dm'},
                               actor_token=FRIEND)
    payload = seen['p_payload']
    assert payload['actor_token'] == FRIEND
    assert 'actor_token' not in payload['data']


# ── Endpoint: POST/GET /api/push/prefs ─────────────────────────────────────

@pytest.fixture
def store():
    """In-Memory-Registry statt Supabase/Disk."""
    state = {}

    def _load(tok):
        return dict(state.get(tok) or {})

    def _save(tok, reg):
        state[tok] = dict(reg)
        return True

    with patch.object(A, '_push_load', side_effect=_load), \
         patch.object(A, '_push_save', side_effect=_save), \
         patch.object(A, '_request_bearer_matches', return_value=True):
        yield state


def _post(client, body):
    return client.post('/api/push/prefs', json=body)


def test_endpoint_old_client_without_friend_prefs_still_works(store):
    c = A.app.test_client()
    r = _post(c, {'token': ME, 'prefs': {'dm': False, 'quatsch': True}})
    assert r.status_code == 200
    assert r.get_json()['prefs'] == {'dm': False}
    assert store[ME]['friend_prefs'] == {}


def test_endpoint_stores_and_merges_friend_prefs(store):
    c = A.app.test_client()
    _post(c, {'token': ME, 'prefs': {'dm': True},
              'friend_prefs': {FRIEND: 'none'}})
    # zweiter Sync fasst den ersten Freund nicht an
    r = _post(c, {'token': ME, 'friend_prefs': {
        OTHER: {'level': 'custom', 'types': ['dm']}}})
    fp = r.get_json()['friend_prefs']
    assert fp[FRIEND] == {'level': 'none', 'types': []}
    assert fp[OTHER] == {'level': 'custom', 'types': ['dm']}
    # globale prefs bleiben erhalten, obwohl der 2. Call keine schickte
    assert store[ME]['prefs'] == {'dm': True}


def test_endpoint_reset_to_all_removes_the_entry(store):
    c = A.app.test_client()
    _post(c, {'token': ME, 'friend_prefs': {FRIEND: 'none'}})
    r = _post(c, {'token': ME, 'friend_prefs': {FRIEND: {'level': 'all'}}})
    assert r.get_json()['friend_prefs'] == {}


def test_endpoint_rejects_body_without_prefs_and_friend_prefs(store):
    c = A.app.test_client()
    assert _post(c, {'token': ME}).status_code == 400


def test_endpoint_requires_bearer_binding():
    c = A.app.test_client()
    with patch.object(A, '_request_bearer_matches', return_value=False):
        r = _post(c, {'token': ME, 'friend_prefs': {FRIEND: 'none'}})
    assert r.status_code == 401


def test_endpoint_caps_the_number_of_stored_friends(store):
    c = A.app.test_client()
    many = {f'AT-f{i:04d}': 'none'
            for i in range(A._PUSH_FRIEND_PREFS_MAX + 25)}
    r = _post(c, {'token': ME, 'friend_prefs': many})
    assert len(r.get_json()['friend_prefs']) == A._PUSH_FRIEND_PREFS_MAX


def test_get_endpoint_returns_both_pref_blocks(store):
    c = A.app.test_client()
    _post(c, {'token': ME, 'prefs': {'community': False},
              'friend_prefs': {FRIEND: 'important'}})
    r = c.get(f'/api/push/prefs/{ME}')
    assert r.status_code == 200
    body = r.get_json()
    assert body['prefs'] == {'community': False}
    assert body['friend_prefs'][FRIEND]['level'] == 'important'


def test_get_endpoint_is_bearer_bound():
    c = A.app.test_client()
    with patch.object(A, '_request_bearer_matches', return_value=False):
        assert c.get(f'/api/push/prefs/{ME}').status_code == 401


def test_idempotency_key_is_unchanged_by_actor_token():
    """Sonst bekäme JEDER bereits zugestellte Event nach dem Deploy einen neuen
    Hash und würde doppelt gepusht."""
    a = A._push_outbox_key(ME, 'T', 'B', {'type': 'dm'}, None, 1, None,
                           idempotency_key='chat:42:me')
    b = A._push_outbox_key(ME, 'T', 'B', {'type': 'dm'}, None, 1, None,
                           idempotency_key='chat:42:me')
    assert a == b
