"""SEC-Regression 17.08.: `/api/me` gibt keine fremden AT-Credentials aus —
auch nicht als TEILSTRING einer zusammengesetzten ID.

Befund: `GET /api/me/crew-chat/inbox` lieferte `channel_id = dm__<AT-a>__<AT-b>`
mit zwei ROHEN Bearer-Credentials. Der Redactor (`_publicize_foreign_user_refs`)
prueft mit `fullmatch` und griff dort nicht — das AT des Freundes verliess damit
den Server, obwohl 9be4818 `friend_token` bereits projiziert hatte.

Fix: der `/api/me`-Antwortpfad schwaerzt jetzt auch eingebettete Vorkommen
(`composites=True`); das eigene Credential bleibt wie beim friend_token-Muster
byteidentisch. Die Empfangsseite (`/api/me/crew-chat/channel/...`,
`/inbox/mark-read`) loest die AXU-Referenz serverseitig ueber die Freund-/
Crew-Beziehung des Aufrufers wieder auf — fail-closed.
"""

import re

import app as A

OWNER = 'AT-1234567890abcdef'
FRIEND = 'AT-cafebabedeadbeef'
STRANGER = 'AT-0f0f0f0f0f0f0f0f'
RAW_AT_RE = re.compile(r'AT-[A-Fa-f0-9]{16}')


def _valid(_token):
    return A._TokenValidationResult(
        A._TokenValidationState.VALID, 'owner@example.test')


def _resp(value):
    return A.app.make_response(value)


def _all_strings(node):
    """Rekursiv jeden String im JSON-Baum (Werte UND Dict-Keys)."""
    if isinstance(node, dict):
        for key, nested in node.items():
            if isinstance(key, str):
                yield key
            yield from _all_strings(nested)
    elif isinstance(node, list):
        for item in node:
            yield from _all_strings(item)
    elif isinstance(node, str):
        yield node


# ── Ausgabeseite ────────────────────────────────────────────────────────────

def test_inbox_response_carries_no_foreign_raw_token_anywhere(monkeypatch):
    monkeypatch.setattr(A, '_validate_token', _valid)
    channel = A._dm_channel(OWNER, FRIEND)
    monkeypatch.setattr(
        A, 'get_dm_inbox',
        lambda token: A.jsonify({'count': 1, 'inbox': [{
            'friend_token': FRIEND,
            'channel_id': channel,
            'last_message_preview': 'hi',
            'unread_count': 0,
        }]}))
    with A.app.test_request_context(
            '/api/me/crew-chat/inbox',
            headers={'Authorization': f'Bearer {OWNER}'}):
        payload = _resp(A.me_chat_inbox()).get_json()

    found = {match for text in _all_strings(payload)
             for match in RAW_AT_RE.findall(text)}
    # Rekursiver Scan ueber das GANZE JSON: das einzige rohe AT darf das
    # eigene Credential sein.
    assert found <= {OWNER}, f'fremdes AT-Credential in der Antwort: {found}'
    assert FRIEND not in A.json.dumps(payload)

    entry = payload['inbox'][0]
    assert entry['friend_token'] == A._public_user_ref(FRIEND)
    assert entry['channel_id'] == f'dm__{OWNER}__{A._public_user_ref(FRIEND)}'


def test_channel_history_echo_is_projected_too(monkeypatch):
    monkeypatch.setattr(A, '_validate_token', _valid)
    monkeypatch.setattr(A, '_crew_dm_pair_authorized', lambda a, b: True)
    internal = A._dm_channel(OWNER, FRIEND)
    seen = []
    monkeypatch.setattr(
        A, 'get_chat_messages',
        lambda token, channel: seen.append(channel) or A.jsonify(
            {'channel': channel, 'messages': []}))
    public_channel = f'dm__{OWNER}__{A._public_user_ref(FRIEND)}'
    with A.app.test_request_context(
            f'/api/me/crew-chat/channel/{public_channel}',
            headers={'Authorization': f'Bearer {OWNER}'}):
        payload = _resp(A.me_chat_channel(public_channel)).get_json()
    # Der Handler bekommt den INTERNEN Kanal, die Antwort echot die
    # projizierte Form.
    assert seen == [internal]
    assert payload['channel'] == public_channel
    assert FRIEND not in A.json.dumps(payload)


def test_composite_redaction_keeps_legacy_fullmatch_path_untouched():
    # Der historische URL-Prefix-Redactor darf NICHT global auf `search`
    # umgestellt werden: zusammengesetzte Legacy-Wire-Formen bleiben dort
    # byteidentisch.
    composite = {'channel_id': f'dm__{OWNER}__{FRIEND}'}
    legacy = A._publicize_foreign_user_refs(composite, viewer_token=OWNER)
    assert legacy['channel_id'] == f'dm__{OWNER}__{FRIEND}'


# ── Empfangsseite (Resolver) ────────────────────────────────────────────────

def test_channel_ref_resolver_happy_path(monkeypatch):
    monkeypatch.setattr(A, '_crew_dm_pair_authorized', lambda a, b: True)
    public_channel = f'dm__{OWNER}__{A._public_user_ref(FRIEND)}'
    assert (A._me_chat_channel_internal(OWNER, public_channel)
            == A._dm_channel(OWNER, FRIEND))


def test_channel_ref_resolver_is_order_independent(monkeypatch):
    monkeypatch.setattr(A, '_crew_dm_pair_authorized', lambda a, b: True)
    swapped = f'dm__{A._public_user_ref(FRIEND)}__{OWNER}'
    assert (A._me_chat_channel_internal(OWNER, swapped)
            == A._dm_channel(OWNER, FRIEND))


def test_channel_ref_resolver_allows_separator_inside_encrypted_ref(monkeypatch):
    """URL-safe base64 can contain ``__``; it is payload, not a third member."""
    public_ref = 'AXU-ciphertext__with__separator'
    monkeypatch.setattr(
        A, '_token_from_public_user_ref',
        lambda value: FRIEND if value == public_ref else None)
    monkeypatch.setattr(A, '_crew_dm_pair_authorized', lambda a, b: True)

    channel = f'dm__{OWNER}__{public_ref}'
    assert (A._me_chat_channel_internal(OWNER, channel)
            == A._dm_channel(OWNER, FRIEND))


def test_channel_ref_resolver_fails_closed_without_relationship(monkeypatch):
    monkeypatch.setattr(A, '_crew_dm_pair_authorized', lambda a, b: False)
    stranger_channel = f'dm__{OWNER}__{A._public_user_ref(STRANGER)}'
    assert A._me_chat_channel_internal(OWNER, stranger_channel) is None


def test_channel_ref_resolver_fails_closed_on_tampered_ref(monkeypatch):
    monkeypatch.setattr(A, '_crew_dm_pair_authorized', lambda a, b: True)
    tampered = f'dm__{OWNER}__{A._public_user_ref(FRIEND)[:-4]}AAAA'
    assert A._me_chat_channel_internal(OWNER, tampered) is None


def test_channel_ref_resolver_rejects_channel_without_the_caller(monkeypatch):
    monkeypatch.setattr(A, '_crew_dm_pair_authorized', lambda a, b: True)
    foreign_pair = (f'dm__{A._public_user_ref(FRIEND)}'
                    f'__{A._public_user_ref(STRANGER)}')
    assert A._me_chat_channel_internal(OWNER, foreign_pair) is None


def test_group_channels_pass_through_unchanged():
    assert (A._me_chat_channel_internal(OWNER, 'group__destination_JFK')
            == 'group__destination_JFK')


def test_channel_route_rejects_unresolvable_ref(monkeypatch):
    monkeypatch.setattr(A, '_validate_token', _valid)
    monkeypatch.setattr(A, '_crew_dm_pair_authorized', lambda a, b: False)
    called = []
    monkeypatch.setattr(
        A, 'get_chat_messages',
        lambda token, channel: called.append(channel) or A.jsonify({}))
    bad = f'dm__{OWNER}__{A._public_user_ref(STRANGER)}'
    with A.app.test_request_context(
            f'/api/me/crew-chat/channel/{bad}',
            headers={'Authorization': f'Bearer {OWNER}'}):
        response = _resp(A.me_chat_channel(bad))
    assert response.status_code == 400
    assert response.get_json()['error'] == 'invalid_channel_ref'
    assert called == []


def test_mark_read_resolves_the_public_channel_handle(monkeypatch):
    monkeypatch.setattr(A, '_validate_token', _valid)
    monkeypatch.setattr(A, '_crew_dm_pair_authorized', lambda a, b: True)
    seen = []

    def _fake_mark_read(token):
        seen.append((token, (A.request.get_json(silent=True) or {}).get(
            'channel_id')))
        return A.jsonify({'ok': True})

    monkeypatch.setattr(A, 'dm_mark_read', _fake_mark_read)
    public_channel = f'dm__{OWNER}__{A._public_user_ref(FRIEND)}'
    with A.app.test_request_context(
            '/api/me/crew-chat/inbox/mark-read', method='POST',
            json={'channel_id': public_channel},
            headers={'Authorization': f'Bearer {OWNER}'}):
        response = _resp(A.me_chat_mark_read())
    assert response.get_json()['ok'] is True
    assert seen == [(OWNER, A._dm_channel(OWNER, FRIEND))]


def test_dm_wrapper_response_also_hides_the_composite_channel(monkeypatch):
    # `get_dm` delegiert an `get_chat_messages` und echot dessen `channel` —
    # derselbe Composite-Leak wie in der Inbox, nur ueber den DM-Wrapper.
    monkeypatch.setattr(A, '_validate_token', _valid)
    internal = A._dm_channel(OWNER, FRIEND)
    monkeypatch.setattr(
        A, 'get_dm',
        lambda token, friend: A.jsonify({'channel': internal, 'messages': []}))
    with A.app.test_request_context(
            f'/api/me/crew-chat/dm/{A._public_user_ref(FRIEND)}',
            headers={'Authorization': f'Bearer {OWNER}'}):
        payload = _resp(A.me_chat_dm(FRIEND)).get_json()
    found = {match for text in _all_strings(payload)
             for match in RAW_AT_RE.findall(text)}
    assert found <= {OWNER}, f'fremdes AT-Credential in der Antwort: {found}'
    assert payload['channel'] == f'dm__{OWNER}__{A._public_user_ref(FRIEND)}'
