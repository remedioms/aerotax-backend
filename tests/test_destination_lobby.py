from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import app as A


TOKEN = 'AT-DESTINATION-LOBBY-123456'
NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def _sector(flight, origin, destination, dep, arr, **extra):
    return {
        'flight': flight,
        'from': origin,
        'to': destination,
        'dep_iso': dep,
        'arr_iso': arr,
        **extra,
    }


def _days(outbound_dep='2026-08-04T20:00:00Z'):
    return {
        '2026-08-04': {
            'ical_sectors': [
                _sector('LH400', 'FRA', 'JFK',
                        '2026-08-04T02:00:00Z', '2026-08-04T12:00:00Z'),
                _sector('LH401', 'JFK', 'FRA', outbound_dep,
                        '2026-08-05T04:00:00Z'),
            ],
        },
    }


def _compute(days=None, now=NOW, homebase='FRA'):
    with (
        patch.object(A, '_crew_roster_days', return_value=days or _days()),
        patch.object(A, '_profile_homebase_cached', return_value=homebase),
    ):
        return A._destination_lobby_compute(TOKEN, now=now)


def test_exactly_eight_hours_opens_lobby_at_arrival():
    lobby = _compute()
    assert lobby['iata'] == 'JFK'
    assert lobby['channel_id'] == 'group__destination_JFK'
    assert lobby['minimum_stay_hours'] == 8
    assert lobby['message_ttl_hours'] == 24


def test_under_eight_hours_never_opens_lobby():
    assert _compute(_days('2026-08-04T19:59:59Z')) is None


def test_lobby_is_hidden_before_arrival_and_at_departure():
    assert _compute(now=datetime(2026, 8, 4, 11, 59, tzinfo=timezone.utc)) is None
    assert _compute(now=datetime(2026, 8, 4, 20, 0, tzinfo=timezone.utc)) is None


def test_homebase_ground_time_is_not_a_destination_lobby():
    assert _compute(homebase='JFK') is None


def test_multi_day_layover_with_confirmed_inner_day_opens():
    days = {
        '2026-08-04': {
            'ical_sectors': [_sector(
                'LH400', 'FRA', 'JFK',
                '2026-08-04T02:00:00Z', '2026-08-04T12:00:00Z')],
        },
        '2026-08-05': {
            'marker': 'Layover [JFK] (Tag 2/3)',
            'reader_facts': {'layover_ort': 'JFK',
                             'overnight_after_day': True},
        },
        '2026-08-06': {
            'ical_sectors': [_sector(
                'LH401', 'JFK', 'FRA',
                '2026-08-06T20:00:00Z', '2026-08-07T04:00:00Z')],
        },
    }
    lobby = _compute(
        days, now=datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc))
    assert lobby is not None
    assert lobby['iata'] == 'JFK'


def test_explicit_free_days_split_same_station_into_two_tours():
    """Katja 16.08.: FRA ist Dienstanker, Profil-Homebase aber HAM.

    Eine Heimkehr GIG-FRA und der neun Tage spaetere neue FRA-DEL-Umlauf sind
    trotz identischer Anschlussstation kein durchgehender Aufenthalt. Die
    expliziten freien/Urlaubstage muessen die Lobby fail-closed unterbrechen.
    """
    days = {
        '2026-08-08': {
            'ical_sectors': [_sector(
                'LH501', 'GIG', 'FRA',
                '2026-08-07T19:21:00Z', '2026-08-08T06:11:00Z')],
        },
        '2026-08-09': {
            'klass': 'FREI',
            'marker': 'Off Day (FREE)',
            'reader_facts': {},
        },
        '2026-08-17': {
            'ical_sectors': [_sector(
                'LH760', 'FRA', 'DEL',
                '2026-08-17T11:20:00Z', '2026-08-17T19:25:00Z')],
        },
    }
    assert _compute(
        days,
        now=datetime(2026, 8, 16, 18, 0, tzinfo=timezone.utc),
        homebase='HAM',
    ) is None


def test_missing_inner_roster_day_keeps_sparse_import_layover_compatible():
    days = {
        '2026-08-04': {
            'ical_sectors': [_sector(
                'LH400', 'FRA', 'JFK',
                '2026-08-04T02:00:00Z', '2026-08-04T12:00:00Z')],
        },
        # 05.08. fehlt: viele valide iCal/PDF-Importe speichern nur die Legs.
        '2026-08-06': {
            'ical_sectors': [_sector(
                'LH401', 'JFK', 'FRA',
                '2026-08-06T20:00:00Z', '2026-08-07T04:00:00Z')],
        },
    }
    lobby = _compute(
        days, now=datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc))
    assert lobby is not None
    assert lobby['iata'] == 'JFK'


def test_explicit_ground_duty_breaks_same_station_candidate():
    days = {
        '2026-08-04': {
            'ical_sectors': [_sector(
                'LH400', 'FRA', 'JFK',
                '2026-08-04T02:00:00Z', '2026-08-04T12:00:00Z')],
        },
        '2026-08-05': {
            'marker': 'Mandatory Training',
        },
        '2026-08-06': {
            'ical_sectors': [_sector(
                'LH401', 'JFK', 'FRA',
                '2026-08-06T20:00:00Z', '2026-08-07T04:00:00Z')],
        },
    }
    assert _compute(
        days, now=datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)) is None


def test_utc_midnight_departure_day_is_not_an_inner_break():
    """Der Roster-Key, nicht das UTC-Datum, bestimmt volle Zwischentage."""
    days = {
        '2026-08-04': {
            'ical_sectors': [_sector(
                'LH400', 'FRA', 'JFK',
                '2026-08-04T02:00:00Z', '2026-08-04T12:00:00Z')],
        },
        '2026-08-05': {
            'marker': 'Layover JFK',
            'reader_facts': {'layover_ort': 'JFK'},
        },
        '2026-08-06': {
            'ical_sectors': [_sector(
                'LH401', 'JFK', 'FRA',
                # Lokal noch 06.08., in UTC bereits 07.08.
                '2026-08-07T00:30:00Z', '2026-08-07T08:30:00Z')],
        },
    }
    lobby = _compute(
        days, now=datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc))
    assert lobby is not None
    assert lobby['iata'] == 'JFK'


def test_lobby_response_exposes_only_anonymous_member_count():
    lobby = _compute()
    with (
        A.app.test_request_context('/'),
        patch.object(A, '_destination_lobby_for_token', return_value=lobby),
        patch.object(A, '_destination_lobby_presence_touch_count', return_value=7),
    ):
        payload = A.get_destination_lobby(TOKEN).get_json()['lobby']
    assert payload['member_count'] == 7
    assert 'members' not in payload
    assert 'member_tokens' not in payload
    assert 'profiles' not in payload


def test_presence_rpc_receives_hash_not_user_token():
    class _RPC:
        def execute(self):
            return SimpleNamespace(data=4)

    class _SB:
        calls = []

        def rpc(self, name, params):
            self.calls.append((name, params))
            return _RPC()

    fake_sb = _SB()
    lobby = _compute()
    with (
        patch.object(A, 'SB_AVAILABLE', True),
        patch.object(A, 'sb', fake_sb),
        patch.object(A, '_DESTINATION_LOBBY_PRESENCE_RPC_DISABLED', False),
        patch.object(A, '_public_user_ref', return_value='AXU-' + 'A' * 32),
        patch.object(A, '_supabase_execute_with_timeout',
                     side_effect=lambda _name, fn: (fn(), False)),
    ):
        assert A._destination_lobby_presence_touch_count(TOKEN, lobby) == 4
    _, params = fake_sb.calls[0]
    assert params['p_member_hash'] != TOKEN
    assert len(params['p_member_hash']) == 64
    assert params['p_user_ref'].startswith('AXU-')
    assert TOKEN not in str(params)


def test_destination_channel_requires_matching_active_roster_lobby():
    with A.app.test_request_context('/'):
        with patch.object(A, '_destination_lobby_for_token', return_value=None):
            payload, status = A._channel_access_error(
                TOKEN, 'group__destination_JFK')
        assert status == 403
        assert payload.get_json()['error'] == 'destination_lobby_unavailable'

        with patch.object(A, '_destination_lobby_for_token', return_value={
            'channel_id': 'group__destination_JFK',
        }):
            assert A._channel_access_error(
                TOKEN, 'group__destination_JFK') is None
            payload, status = A._channel_access_error(
                TOKEN, 'group__destination_LAX')
            assert status == 403


def test_destination_messages_are_limited_to_arrival_and_24_hours():
    now_ts = NOW.timestamp()
    lobby = {
        'channel_id': 'group__destination_JFK',
        'available_since': '2026-08-04T12:00:00Z',
    }
    messages = [
        {'id': 'before', 'author_token': 'AXU_old', 'text': 'old',
         'ts': now_ts - 1},
        {'id': 'visible', 'author_token': 'AXU_new', 'text': 'hi',
         'ts': now_ts + 1},
    ]
    with (
        A.app.test_request_context('/?since_ts=0'),
        patch.object(A, '_channel_access_error', return_value=None),
        patch.object(A, '_destination_lobby_for_token', return_value=lobby),
        patch.object(A, '_destination_lobby_now', return_value=NOW),
        # Anzeige-GET nutzt seit 06.08. den Schnellpfad _dm_load_recent.
        patch.object(A, '_dm_load_recent', return_value=messages),
        patch.object(A, '_chat_author_identities', return_value={}),
    ):
        response = A.get_chat_messages(TOKEN, 'group__destination_JFK')
    assert [m['id'] for m in response.get_json()['messages']] == ['visible']


def test_destination_send_enqueues_message_push_after_join():
    channel = 'group__destination_JFK'
    with (
        A.app.test_request_context('/', method='POST', json={'text': 'Hallo'}),
        patch.object(A, '_chat_path', return_value='/tmp/chat.json'),
        patch.object(A, '_channel_access_error', return_value=None),
        patch.object(A, '_token_rate_limited', return_value=False),
        patch.object(A, '_profile_load', return_value={'profile': {}}),
        patch.object(A, '_dm_messages_save_to_supabase', return_value=True),
        patch.object(A, '_dm_load_messages_from_disk', return_value=[]),
        patch.object(A, '_dm_save_messages_disk'),
        patch.object(A, '_chat_push_fanout_async') as push,
    ):
        response = A.send_chat_message(TOKEN, channel)
    assert response.get_json()['ok'] is True
    push.assert_called_once()
    assert push.call_args.args[:3] == (TOKEN, channel, 'Hallo')


def test_destination_fanout_only_targets_active_joined_presence():
    recipient = 'AT-ABCDEF0123456789'
    with (
        patch.object(A, '_destination_lobby_push_recipients',
                     return_value=[recipient]) as lookup,
        patch.object(A, '_profile_load',
                     return_value={'profile': {'name': 'Jana'}}),
        patch.object(A, '_muted_by', return_value=[]),
        patch.object(A, '_blocked_by', return_value=[]),
        patch.object(A, '_push_outbox_enqueue', return_value='child-1') as enqueue,
    ):
        assert A._chat_push_fanout_async(
            TOKEN, 'group__destination_JFK', 'Hallo', message_id='m-1',
            _from_outbox=True) is True
    lookup.assert_called_once_with(
        'group__destination_JFK', author_token=TOKEN)
    assert enqueue.call_args.args[:3] == (
        recipient, 'Jana · Destination Lobby · JFK', 'Hallo')
    assert enqueue.call_args.kwargs['data']['type'] == 'group_message'
    assert enqueue.call_args.kwargs['data']['channel_id'] == \
        'group__destination_JFK'
    assert enqueue.call_args.kwargs['idempotency_key'] == \
        f'chat:m-1:{recipient}'


def test_destination_presence_lookup_excludes_author_and_uses_expiry_filters():
    class Query:
        def __init__(self):
            self.calls = []

        def select(self, value):
            self.calls.append(('select', value)); return self

        def eq(self, key, value):
            self.calls.append(('eq', key, value)); return self

        def lte(self, key, value):
            self.calls.append(('lte', key, value)); return self

        def gt(self, key, value):
            self.calls.append(('gt', key, value)); return self

        def limit(self, value):
            self.calls.append(('limit', value)); return self

        def execute(self):
            return SimpleNamespace(data=[
                {'user_ref': 'AXU-author'},
                {'user_ref': 'AXU-recipient'},
                {'user_ref': 'AXU-recipient'},
                {'user_ref': 'AXU-invalid'},
            ])

    query = Query()
    decoded = {
        'AXU-author': TOKEN,
        'AXU-recipient': 'AT-ABCDEF0123456789',
        'AXU-invalid': None,
    }
    fake_sb = SimpleNamespace(table=lambda name: query)
    with (
        patch.object(A, 'SB_AVAILABLE', True),
        patch.object(A, 'sb', fake_sb),
        patch.object(A, '_destination_lobby_now', return_value=NOW),
        patch.object(A, '_token_from_public_user_ref',
                     side_effect=lambda ref: decoded.get(ref)),
    ):
        assert A._destination_lobby_push_recipients(
            'group__destination_JFK', author_token=TOKEN
        ) == ['AT-ABCDEF0123456789']
    assert ('eq', 'channel_id', 'group__destination_JFK') in query.calls
    assert any(call[:2] == ('lte', 'available_since') for call in query.calls)
    assert any(call[:2] == ('gt', 'expires_at') for call in query.calls)


def test_destination_fanout_does_not_push_on_join_itself():
    lobby = _compute()
    with (
        A.app.test_request_context('/'),
        patch.object(A, '_destination_lobby_for_token', return_value=lobby),
        patch.object(A, '_destination_lobby_presence_touch_count', return_value=2),
        patch.object(A, '_chat_push_fanout_async') as push,
    ):
        payload = A.get_destination_lobby(TOKEN).get_json()['lobby']
    assert payload['member_count'] == 2
    push.assert_not_called()


def test_send_rejects_stale_lobby_session():
    """Review-P1 06.08.: ein replayter POST aus einer FRÜHEREN Aufenthalts-
    Session (Client sendet session_id) darf nicht im aktuellen Lobby-
    Zeitraum landen — 410, keine Nachricht, kein Push."""
    with (
        A.app.test_request_context(
            '/send', method='POST',
            json={'text': 'hallo aus der alten session',
                  'session_id': 'destination_JFK_1111111111'}),
        patch.object(A, '_channel_access_error', return_value=None),
        patch.object(A, '_destination_lobby_for_token', return_value={
            'session_id': 'destination_JFK_2222222222',
            'channel_id': 'group__destination_JFK',
        }),
    ):
        resp = A.send_chat_message(TOKEN, 'group__destination_JFK')
    payload, status = resp if isinstance(resp, tuple) else (resp, 200)
    assert status == 410
    assert payload.get_json()['error'] == 'lobby_session_expired'


def test_send_accepts_current_lobby_session():
    """Gegenprobe: die AKTUELLE Session passiert das Gate — gleiche Mock-
    Kulisse wie test_destination_send_enqueues_message_push_after_join."""
    channel = 'group__destination_JFK'
    with (
        A.app.test_request_context(
            '/', method='POST',
            json={'text': 'Hallo',
                  'session_id': 'destination_JFK_2222222222'}),
        patch.object(A, '_chat_path', return_value='/tmp/chat.json'),
        patch.object(A, '_channel_access_error', return_value=None),
        patch.object(A, '_destination_lobby_for_token', return_value={
            'session_id': 'destination_JFK_2222222222',
            'channel_id': channel,
        }),
        patch.object(A, '_token_rate_limited', return_value=False),
        patch.object(A, '_profile_load', return_value={'profile': {}}),
        patch.object(A, '_dm_messages_save_to_supabase', return_value=True),
        patch.object(A, '_dm_load_messages_from_disk', return_value=[]),
        patch.object(A, '_dm_save_messages_disk'),
        patch.object(A, '_chat_push_fanout_async'),
    ):
        response = A.send_chat_message(TOKEN, channel)
    assert response.get_json()['ok'] is True


# ── Fail-closed ohne belastbare Homebase (Review 17.08.) ────────────────────
# Der Docstring von `_destination_lobby_compute` sagt „Fehlende/unklare
# Rosterdaten fallen geschlossen aus" — fuer die Homebase galt das NICHT: bei
# leerem Profilfeld verglich `iata == homebase` gegen '' und die eigene Basis
# wurde zur Auto-Lobby (Katja-Regel: die eigene Homebase ist nie eine Lobby).

def test_missing_homebase_never_auto_joins_a_lobby():
    for homebase in (None, '', '   ', 'FRANKFURT', 'FR', '123'):
        assert _compute(homebase=homebase) is None, homebase


def test_missing_homebase_blocks_the_lobby_at_the_own_base():
    # Genau der gefaehrliche Fall: der Aufenthalt LIEGT an der eigenen Basis.
    # Mit Homebase greift der Ausschluss, ohne Homebase darf nicht ersatzweise
    # gejoint werden.
    assert _compute(homebase='JFK') is None
    assert _compute(homebase=None) is None
