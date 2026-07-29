"""Live Activities (P6) — rein offline: kein APNs, kein Supabase, kein Netz.

Gemockt wird genau an drei Nähten:
  • httpx: Fake-Modul in sys.modules + `A._APNS_HTTP_CLIENT = None` +
    `A._apns_get_jwt` → 'jwt-test' (identisches Muster wie
    tests/test_duty_change_push.py).
  • Supabase: `live_activity._sb` → `_FakeSB`. Die Fake-RPCs spiegeln die
    Semantik aus supabase_migrations/20260727_live_activities.sql (Konflikt-Key
    = (user_token, kind, coalesce(activity_id,''))). Die ECHTE Eindeutigkeit
    macht der Expression-Index in Postgres — hier wird der Vertrag getestet,
    den der Python-Code gegen ihn annimmt (Param-Namen, Reset-Verhalten).
  • Auth: `A._validate_token` → VALID. Das Bearer-Binding läuft ECHT
    (app.py `_request_bearer_matches`, constant-time) — es ist der eigentliche
    Schutz dieser Body-Token-Routen.
"""
import json
import os
import sys
import types
from datetime import datetime, timezone

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import pytest
from flask import Flask
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as A
from blueprints import live_activity as LA


TOKEN = 'AT-LIVEACT-OWNER-0001'
LA_TOKEN_A = 'aaaa1111bbbb2222'
LA_TOKEN_B = 'cccc3333dddd4444'
ACT_ID = 'A1B2C3D4-0000-4000-8000-000000000001'
BUNDLE = 'aerotax.AeroTax'

# 2001-01-01T00:00:00Z in Unix-Sekunden — Apples Referenzdatum.
APPLE_EPOCH = 978307200


# ════════════════════════════════════════════════════════════════════════════
# Fakes
# ════════════════════════════════════════════════════════════════════════════

class _Res:
    def __init__(self, data):
        self.data = data


class _Table:
    def __init__(self, sb):
        self.sb = sb
        self.filters = {}

    def select(self, _cols):
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def execute(self):
        rows = [dict(r) for r in self.sb.rows
                if all(r.get(k) == v for k, v in self.filters.items())]
        return _Res(rows)


class _Rpc:
    def __init__(self, sb, name, params):
        self.sb, self.name, self.params = sb, name, params

    def execute(self):
        self.sb.rpc_calls.append((self.name, dict(self.params)))
        return _Res(self.sb.run(self.name, self.params))


class _FakeSB:
    """Minimal-Postgres für die drei RPCs + den Row-Read."""

    def __init__(self):
        self.rows = []
        self.rpc_calls = []
        self._seq = 0
        self.fail_rpc = set()

    # ── Client-Oberfläche ────────────────────────────────────────────────
    def table(self, name):
        assert name == 'live_activities', name
        return _Table(self)

    def rpc(self, name, params):
        return _Rpc(self, name, params)

    # ── RPC-Semantik (Spiegel der Migration) ─────────────────────────────
    def run(self, name, p):
        if name in self.fail_rpc:
            raise RuntimeError('rpc down')
        if name == 'upsert_live_activity':
            return self._upsert(p)
        if name == 'end_live_activity':
            return self._end(p)
        if name == 'live_activity_mark_result':
            return self._mark(p)
        raise AssertionError('unexpected rpc ' + name)

    def _key(self, token, kind, activity_id):
        return (token, kind, activity_id or '')

    def _upsert(self, p):
        key = self._key(p['p_user_token'], p['p_kind'], p.get('p_activity_id'))
        env = p.get('p_environment') or 'unknown'
        for row in self.rows:
            if self._key(row['user_token'], row['kind'],
                         row.get('activity_id')) != key:
                continue
            rotated = row['la_token'] != p['p_la_token']
            row['la_token'] = p['p_la_token']
            row['bundle_id'] = p.get('p_bundle_id') or row.get('bundle_id')
            if env != 'unknown':
                row['environment'] = env
            row['device_id'] = p.get('p_device_id') or row.get('device_id')
            row['platform'] = p.get('p_platform') or row.get('platform')
            row['active'] = True
            row['ended_at'] = None
            row['end_reason'] = None
            if rotated:
                row['failure_count'] = 0
                row['content_digest'] = None
            return row['id']
        self._seq += 1
        row_id = f'row-{self._seq}'
        self.rows.append({
            'id': row_id, 'user_token': p['p_user_token'],
            'kind': p['p_kind'], 'activity_id': p.get('p_activity_id'),
            'la_token': p['p_la_token'], 'bundle_id': p.get('p_bundle_id'),
            'environment': env, 'device_id': p.get('p_device_id'),
            'platform': p.get('p_platform') or 'ios', 'active': True,
            'content_digest': None, 'last_timestamp': None,
            'failure_count': 0, 'ended_at': None, 'end_reason': None,
        })
        return row_id

    def _end(self, p):
        want = p.get('p_activity_id') or ''
        n = 0
        for row in self.rows:
            if (row['user_token'] == p['p_user_token']
                    and row['kind'] == 'update'
                    and (row.get('activity_id') or '') == want
                    and row['active']):
                row['active'] = False
                row['ended_at'] = 'now'
                row['end_reason'] = p.get('p_reason') or 'client'
                n += 1
        return n

    def _mark(self, p):
        for row in self.rows:
            if row['id'] != p['p_id']:
                continue
            if p['p_ok'] and p.get('p_digest'):
                row['content_digest'] = p['p_digest']
            ts = p.get('p_timestamp')
            if ts is not None and ts > (row.get('last_timestamp') or 0):
                row['last_timestamp'] = ts
            row['failure_count'] = 0 if p['p_ok'] else row['failure_count'] + 1
            if p.get('p_environment'):
                row['environment'] = p['p_environment']
            if p.get('p_dead'):
                row['active'] = False
                row['ended_at'] = 'now'
                row['end_reason'] = p.get('p_reason') or 'apns_dead'
        return None

    # ── Test-Hilfen ──────────────────────────────────────────────────────
    def row(self, kind='update', activity_id=ACT_ID):
        key = self._key(TOKEN, kind, activity_id)
        for row in self.rows:
            if self._key(row['user_token'], row['kind'],
                         row.get('activity_id')) == key:
                return row
        return None


class _FakeResp:
    def __init__(self, status_code=200, reason=None):
        self.status_code = status_code
        self._reason = reason
        self.text = json.dumps({'reason': reason}) if reason else ''

    def json(self):
        return {'reason': self._reason} if self._reason else {}


class _FakeAPNs:
    """Fake-httpx-Client. `script(host, idx)` → (status_code, reason)."""

    def __init__(self, script=None):
        self.sent = []
        self.script = script or (lambda host, idx: (200, None))

    def post(self, url, headers=None, content=None):
        host = url.split('//', 1)[1].split('/', 1)[0]
        self.sent.append({
            'host': host, 'token': url.rsplit('/', 1)[-1],
            'headers': dict(headers or {}),
            'payload': json.loads(content.decode('utf-8')),
        })
        status, reason = self.script(host, len(self.sent) - 1)
        return _FakeResp(status, reason)

    @property
    def hosts(self):
        return [s['host'] for s in self.sent]


class _ValidToken:
    state = A._TokenValidationState.VALID
    email = 'owner@example.test'


# ════════════════════════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _pin_app():
    """`tests/test_calculation.py` reimportiert app.py und tauscht dabei
    `sys.modules['app']`. Das Blueprint bindet app-Funktionen per `_app_attr`
    LATE (über `import app`) — ohne dieses Pinning greifen unsere
    `patch.object(A, ...)` auf ein anderes Modul-Objekt als der Produktionscode
    zur Laufzeit auflöst, und die Tests fallen je nach Reihenfolge um.
    Identisches Muster wie tests/test_duty_change_push.py."""
    prev = sys.modules.get('app')
    sys.modules['app'] = A
    yield
    if prev is not None:
        sys.modules['app'] = prev


@pytest.fixture(autouse=True)
def _clean_module_state(monkeypatch):
    LA._LAST_SENT.clear()
    monkeypatch.delenv('APNS_USE_SANDBOX', raising=False)
    monkeypatch.delenv('ADSB_POLL_SECRET', raising=False)
    yield
    LA._LAST_SENT.clear()


@pytest.fixture
def sb(monkeypatch):
    fake = _FakeSB()
    monkeypatch.setattr(LA, '_sb', lambda: fake)
    return fake


@pytest.fixture
def client():
    app = Flask(__name__)
    app.register_blueprint(LA.live_activity_bp)
    return app.test_client()


@pytest.fixture
def apns(monkeypatch):
    """Fake-httpx + JWT. Der Sender läuft dabei ECHT (Header-Bau inklusive)."""
    holder = {'client': _FakeAPNs()}
    fake_httpx = types.ModuleType('httpx')
    fake_httpx.Client = lambda *a, **k: holder['client']
    monkeypatch.setitem(sys.modules, 'httpx', fake_httpx)
    monkeypatch.setattr(A, '_APNS_HTTP_CLIENT', None, raising=False)
    monkeypatch.setattr(A, '_apns_get_jwt', lambda: 'jwt-test')

    def _script(fn):
        holder['client'].script = fn

    holder['script'] = _script
    yield holder
    monkeypatch.setattr(A, '_APNS_HTTP_CLIENT', None, raising=False)


@pytest.fixture
def auth(monkeypatch):
    monkeypatch.setattr(A, '_validate_token', lambda t: _ValidToken())


def _auth_hdr(token=TOKEN):
    return {'Authorization': f'Bearer {token}'}


def _register(client, kind='update', la_token=LA_TOKEN_A, activity_id=ACT_ID,
              apns_env='prod', token=TOKEN, hdr=None):
    body = {'token': token, 'la_token': la_token, 'kind': kind,
            'bundle_id': BUNDLE, 'apns_env': apns_env, 'platform': 'ios',
            'device_id': 'dev-1'}
    if activity_id and kind == 'update':
        body['activity_id'] = activity_id
    return client.post('/api/push/register-live-activity', json=body,
                       headers=_auth_hdr(token) if hdr is None else hdr)


def _state(**over):
    base = {'stateVersion': 2, 'phase': 'inFlight', 'kicker': 'LANDUNG',
            'mainTime': '2026-07-27T18:30:00Z',
            'generatedAt': '2026-07-27T12:00:00Z'}
    base.update(over)
    return base


def _push(client, state=None, **body_over):
    body = {'user_token': TOKEN, 'content_state': state or _state()}
    body.update(body_over)
    return client.post('/api/internal/live-activity/push', json=body)


# ════════════════════════════════════════════════════════════════════════════
# 1) Registrierung
# ════════════════════════════════════════════════════════════════════════════

def test_register_start_and_update(client, sb, auth):
    r1 = _register(client, kind='start', la_token=LA_TOKEN_A, activity_id=None)
    assert r1.status_code == 200, r1.get_json()
    assert r1.get_json() == {'ok': True, 'kind': 'start', 'activity_id': None,
                             'stored': True}

    r2 = _register(client, kind='update', la_token=LA_TOKEN_B)
    assert r2.status_code == 200
    assert r2.get_json()['activity_id'] == ACT_ID

    assert len(sb.rows) == 2
    start = sb.row(kind='start', activity_id=None)
    upd = sb.row(kind='update')
    # Start-Token trägt KEINE activity_id (es gibt noch keine Activity).
    assert start['activity_id'] is None and start['la_token'] == LA_TOKEN_A
    assert upd['activity_id'] == ACT_ID and upd['la_token'] == LA_TOKEN_B
    assert upd['environment'] == 'prod' and upd['bundle_id'] == BUNDLE
    # Genau ein atomarer Upsert pro Registrierung.
    assert [n for n, _ in sb.rpc_calls] == ['upsert_live_activity'] * 2


def test_second_update_token_replaces_not_duplicates(client, sb, auth):
    """Der Update-Token ROTIERT. Jede Erneuerung muss die vorige Zeile
    derselben activity_id überschreiben — sonst pusht der Fanout gegen einen
    toten Token und die Activity friert still ein."""
    assert _register(client, la_token=LA_TOKEN_A).status_code == 200
    assert _register(client, la_token=LA_TOKEN_B).status_code == 200
    rows = [r for r in sb.rows if r['kind'] == 'update']
    assert len(rows) == 1
    assert rows[0]['la_token'] == LA_TOKEN_B


def test_rotated_token_resets_digest_and_failures(client, sb, auth):
    _register(client, la_token=LA_TOKEN_A)
    row = sb.row()
    row['content_digest'] = 'stale-digest'
    row['failure_count'] = 3
    _register(client, la_token=LA_TOKEN_B)
    # Frischer Token hat noch nie einen Zustand gesehen: das nächste Update
    # muss auch bei identischem Inhalt durchgehen.
    assert sb.row()['content_digest'] is None
    assert sb.row()['failure_count'] == 0


def test_register_start_and_update_do_not_share_a_row(client, sb, auth):
    _register(client, kind='start', la_token=LA_TOKEN_A, activity_id=None)
    _register(client, kind='update', la_token=LA_TOKEN_A)
    assert len({r['kind'] for r in sb.rows}) == 2


def test_register_bearer_mismatch_401(client, sb, auth):
    r = _register(client, hdr={'Authorization': 'Bearer AT-SOMEONE-ELSE'})
    assert r.status_code == 401
    assert r.get_json()['error'] == 'token_binding_required'
    assert sb.rows == []


def test_register_without_bearer_401(client, sb, auth):
    r = _register(client, hdr={})
    assert r.status_code == 401
    assert r.get_json()['error'] == 'token_binding_required'


def test_register_missing_fields_400(client, sb, auth):
    r = client.post('/api/push/register-live-activity',
                    json={'token': TOKEN, 'kind': 'update'},
                    headers=_auth_hdr())
    assert r.status_code == 400 and r.get_json()['error'] == 'missing_fields'


def test_register_bad_kind_400(client, sb, auth):
    r = _register(client, kind='wat')
    assert r.status_code == 400


def test_register_store_down_is_503_not_fake_success(client, sb, auth):
    """Der iOS-Client setzt seinen lastSent-Marker NUR bei 2xx. Ein
    vorgetäuschtes 200 bei SB-Ausfall würde den Token für immer verlieren."""
    sb.fail_rpc.add('upsert_live_activity')
    r = _register(client)
    assert r.status_code == 503
    assert r.get_json()['error'] == 'live_activity_store_unavailable'


def test_register_invalid_token_401(client, sb, monkeypatch):
    class _Invalid:
        state = A._TokenValidationState.INVALID
        email = None
    monkeypatch.setattr(A, '_validate_token', lambda t: _Invalid())
    r = _register(client)
    assert r.status_code == 401 and r.get_json()['error'] == 'invalid_token'


def test_register_auth_store_unavailable_503(client, sb, monkeypatch):
    class _Unavail:
        state = A._TokenValidationState.UNAVAILABLE
        email = None
    monkeypatch.setattr(A, '_validate_token', lambda t: _Unavail())
    r = _register(client)
    assert r.status_code == 503


def test_register_unknown_env_does_not_overwrite_learned(client, sb, auth):
    _register(client, la_token=LA_TOKEN_A, apns_env='sandbox')
    assert sb.row()['environment'] == 'sandbox'
    _register(client, la_token=LA_TOKEN_A, apns_env='')
    assert sb.row()['environment'] == 'sandbox'


# ════════════════════════════════════════════════════════════════════════════
# 2) /api/live-activity/end
# ════════════════════════════════════════════════════════════════════════════

def test_end_sets_active_false(client, sb, auth):
    _register(client, la_token=LA_TOKEN_A)
    assert sb.row()['active'] is True
    r = client.post('/api/live-activity/end',
                    json={'token': TOKEN, 'activity_id': ACT_ID},
                    headers=_auth_hdr())
    assert r.status_code == 200
    assert r.get_json() == {'ok': True, 'ended': 1}
    row = sb.row()
    assert row['active'] is False and row['end_reason'] == 'client'
    assert row['ended_at'] is not None


def test_end_is_idempotent(client, sb, auth):
    _register(client, la_token=LA_TOKEN_A)
    client.post('/api/live-activity/end',
                json={'token': TOKEN, 'activity_id': ACT_ID},
                headers=_auth_hdr())
    r = client.post('/api/live-activity/end',
                    json={'token': TOKEN, 'activity_id': ACT_ID},
                    headers=_auth_hdr())
    assert r.status_code == 200 and r.get_json()['ended'] == 0


def test_end_bearer_mismatch_401(client, sb, auth):
    _register(client, la_token=LA_TOKEN_A)
    r = client.post('/api/live-activity/end',
                    json={'token': TOKEN, 'activity_id': ACT_ID},
                    headers={'Authorization': 'Bearer AT-ATTACKER'})
    assert r.status_code == 401
    assert r.get_json()['error'] == 'token_binding_required'
    assert sb.row()['active'] is True


def test_end_missing_activity_id_400(client, sb, auth):
    r = client.post('/api/live-activity/end', json={'token': TOKEN},
                    headers=_auth_hdr())
    assert r.status_code == 400


def test_ended_row_is_no_longer_a_push_target(client, sb, auth, apns):
    _register(client, la_token=LA_TOKEN_A)
    client.post('/api/live-activity/end',
                json={'token': TOKEN, 'activity_id': ACT_ID},
                headers=_auth_hdr())
    r = _push(client)
    assert r.status_code == 200
    assert r.get_json()['skipped'] == 'no_target'
    assert apns['client'].sent == []


# ════════════════════════════════════════════════════════════════════════════
# 3) content-state-Normalisierung + Datums-Kodierung
# ════════════════════════════════════════════════════════════════════════════

def test_normalize_drops_none_values():
    state, problems = LA._normalize_content_state(
        _state(route=None, deltaMin=None, progress=None, cancelled=None))
    assert problems == []
    # Kein JSON-`null` im Payload: ein `null` auf einem nicht-optionalen
    # Swift-Feld sprengt das Decoding, Absenz ist immer sicher.
    assert 'route' not in state and 'deltaMin' not in state
    assert 'progress' not in state and 'cancelled' not in state
    assert None not in state.values()


def test_normalize_refuses_unknown_keys():
    state, problems = LA._normalize_content_state(
        _state(bogusField='x', delayMinutes=7))
    assert 'bogusField' not in state and 'delayMinutes' not in state
    assert sorted(problems) == ['unknown_key:bogusField',
                                'unknown_key:delayMinutes']
    # Unbekannte Keys sind verworfen + geloggt, aber nicht fatal.
    assert LA._fatal_problems(problems) == []


def test_roster_frozen_note_is_part_of_the_contract():
    """Abnahme 2026-07-27: iOS kennt `rosterFrozenNote` (String?, ContentState),
    der Backend-Vertrag kannte den Key nicht — der Normalizer warf ihn als
    unknown_key weg und die Lockscreen-Karte verlor den Satz."""
    state, problems = LA._normalize_content_state(
        _state(rosterFrozen=True,
               rosterFrozenNote='Dienstplan-Verbindung erneuern'))
    assert problems == []
    assert state['rosterFrozen'] is True
    # String bleibt String — KEINE Datums-/Zahl-Koersion auf einem Textfeld.
    assert state['rosterFrozenNote'] == 'Dienstplan-Verbindung erneuern'
    # Typ-Schutz: ein Nicht-String passt nicht zum Swift-`String?`.
    _, p2 = LA._normalize_content_state(_state(rosterFrozenNote=7))
    assert 'bad_type:rosterFrozenNote' in p2


def test_leg_timezones_are_part_of_the_contract():
    """Nachzug 2026-07-29: iOS leitet aus `fromTZIdentifier`/`toTZIdentifier`
    die ORTSZONE der Marke ab (`DutyAnchor.markZone`). Der Normalizer warf
    beide Keys als unknown_key weg — die Live Activity zeigte den Dienstbeginn
    in San Francisco deshalb weiter in Frankfurter Zeit, während das Widget
    daneben schon die Ortszeit anzeigte."""
    state, problems = LA._normalize_content_state(
        _state(fromTZIdentifier='America/Los_Angeles',
               toTZIdentifier='Europe/Berlin',
               flightNo='LH455',
               mainTimeIsToday=False,
               mainTimeDayLabel='Do 30.07'))
    assert problems == []
    assert state['fromTZIdentifier'] == 'America/Los_Angeles'
    assert state['toTZIdentifier'] == 'Europe/Berlin'
    assert state['flightNo'] == 'LH455'
    assert state['mainTimeIsToday'] is False
    assert state['mainTimeDayLabel'] == 'Do 30.07'


def test_normalize_missing_required_key_is_fatal():
    # `stateVersion`/`phase`/`kicker`/`mainTime`/`generatedAt` sind in Swift
    # nicht-optional, und der synthetisierte Decoder nutzt KEINE Defaults.
    bad = _state()
    del bad['kicker']
    del bad['stateVersion']
    _, problems = LA._normalize_content_state(bad)
    fatal = LA._fatal_problems(problems)
    assert len(fatal) == 1 and fatal[0].startswith('missing:')
    assert 'kicker' in fatal[0] and 'stateVersion' in fatal[0]


def test_normalize_bad_type_is_fatal():
    _, problems = LA._normalize_content_state(_state(progress='fast'))
    assert 'bad_type:progress' in LA._fatal_problems(problems)


def test_dates_encode_as_apple_reference_seconds():
    """DIE zentrale Entscheidung: ActivityKit dekodiert content-state mit einem
    PLAIN JSONDecoder (default `.deferredToDate`) ⇒ `Date` ist eine ZAHL in
    Sekunden seit 2001-01-01, nicht ISO-8601 und nicht Unix."""
    iso = '2026-07-27T18:30:00Z'
    expect = datetime(2026, 7, 27, 18, 30, tzinfo=timezone.utc).timestamp() \
        - APPLE_EPOCH
    state, problems = LA._normalize_content_state(_state(mainTime=iso))
    assert problems == []
    assert isinstance(state['mainTime'], float)
    assert not isinstance(state['mainTime'], str)
    assert state['mainTime'] == pytest.approx(expect)
    # Plausibilität: ein Datum in 2026 liegt bei ~8.1e8 Referenz-Sekunden.
    assert 8.0e8 < state['mainTime'] < 8.5e8


def test_date_accepts_datetime_iso_and_unix():
    dt = datetime(2026, 7, 27, 18, 30, tzinfo=timezone.utc)
    want = dt.timestamp() - APPLE_EPOCH
    assert LA._to_apple_date(dt) == pytest.approx(want)
    assert LA._to_apple_date('2026-07-27T18:30:00Z') == pytest.approx(want)
    # Zahlen sind IMMER Unix-Sekunden — keine „schon konvertiert?"-Heuristik.
    assert LA._to_apple_date(dt.timestamp()) == pytest.approx(want)
    assert LA._to_apple_date(int(dt.timestamp())) == pytest.approx(want)
    assert LA._to_apple_date(None) is None
    assert LA._to_apple_date('nope') is None


def test_chain_is_flattened_with_numeric_times():
    state, problems = LA._normalize_content_state(_state(chain=[
        {'label': 'Report', 'time': '2026-07-27T10:00:00Z', 'state': 'done'},
        {'label': 'Abflug', 'time': '2026-07-27T11:00:00Z', 'state': 'current'},
    ]))
    assert problems == []
    assert [s['label'] for s in state['chain']] == ['Report', 'Abflug']
    assert all(isinstance(s['time'], float) for s in state['chain'])


def test_chain_incomplete_step_is_fatal():
    _, problems = LA._normalize_content_state(
        _state(chain=[{'label': 'Report'}]))
    assert any(p.startswith('missing:chain[0]') for p in problems)


def test_digest_ignores_generated_at_only():
    a = LA._normalize_content_state(_state())[0]
    b = LA._normalize_content_state(_state(generatedAt='2026-07-27T13:00:00Z'))[0]
    c = LA._normalize_content_state(_state(kicker='ABFLUG'))[0]
    assert LA._content_digest(a) == LA._content_digest(b)
    assert LA._content_digest(a) != LA._content_digest(c)


# ════════════════════════════════════════════════════════════════════════════
# 4) APNs-Payload/Headers
# ════════════════════════════════════════════════════════════════════════════

def test_apns_headers_are_exactly_right(client, sb, auth, apns):
    _register(client, la_token=LA_TOKEN_A, apns_env='prod')
    before = int(datetime.now(timezone.utc).timestamp())
    r = _push(client)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['sent'] == 1
    sent = apns['client'].sent
    assert len(sent) == 1
    hdr = sent[0]['headers']
    assert set(hdr) == {'authorization', 'apns-topic', 'apns-push-type',
                        'apns-priority', 'apns-expiration'}
    assert hdr['authorization'] == 'bearer jwt-test'
    # Das `.push-type.liveactivity`-SUFFIX ist Pflicht — ohne es: 400 BadTopic.
    assert hdr['apns-topic'] == 'aerotax.AeroTax.push-type.liveactivity'
    assert hdr['apns-push-type'] == 'liveactivity'
    assert hdr['apns-priority'] == '10'
    assert before + 3600 <= int(hdr['apns-expiration']) <= before + 3601 + 5
    assert sent[0]['host'] == 'api.push.apple.com'
    assert sent[0]['token'] == LA_TOKEN_A


def test_apns_priority_5_is_passed_through(client, sb, auth, apns):
    _register(client, la_token=LA_TOKEN_A)
    _push(client, priority=5)
    assert apns['client'].sent[0]['headers']['apns-priority'] == '5'


def test_aps_payload_shape(client, sb, auth, apns):
    _register(client, la_token=LA_TOKEN_A)
    _push(client, state=_state(route='FRA–JFK', deltaMin=12, progress=0.42),
          stale_after_s=900, relevance=75)
    aps = apns['client'].sent[0]['payload']['aps']
    assert aps['event'] == 'update'
    # aps.timestamp ist UNIX-Sekunden (ActivityKit liest es selbst) — anders
    # als die Dates IN content-state.
    now = int(datetime.now(timezone.utc).timestamp())
    assert isinstance(aps['timestamp'], int)
    assert abs(aps['timestamp'] - now) < 30
    assert aps['stale-date'] == aps['timestamp'] + 900
    assert aps['relevance-score'] == 75.0
    assert 'dismissal-date' not in aps          # nur bei event='end'
    assert 'attributes-type' not in aps         # nur bei event='start'
    cs = aps['content-state']
    assert cs['phase'] == 'inFlight' and cs['route'] == 'FRA–JFK'
    assert cs['deltaMin'] == 12 and cs['progress'] == 0.42
    assert isinstance(cs['mainTime'], float)


def test_end_event_carries_dismissal_date(client, sb, auth, apns):
    _register(client, la_token=LA_TOKEN_A)
    r = _push(client, event='end', dismiss_after_s=60)
    assert r.status_code == 200 and r.get_json()['sent'] == 1
    aps = apns['client'].sent[0]['payload']['aps']
    assert aps['event'] == 'end'
    assert aps['dismissal-date'] == aps['timestamp'] + 60


def test_push_to_start_when_only_start_token_exists(client, sb, auth, apns):
    _register(client, kind='start', la_token=LA_TOKEN_A, activity_id=None)
    r = _push(client, attributes={'flightNo': 'LH400', 'from': 'FRA',
                                  'to': 'JFK',
                                  'startedAt': '2026-07-27T09:00:00Z'})
    assert r.status_code == 200
    body = r.get_json()
    assert body['event'] == 'start' and body['sent'] == 1
    aps = apns['client'].sent[0]['payload']['aps']
    assert aps['event'] == 'start'
    assert aps['attributes-type'] == 'DutyActivityAttributes'
    assert aps['attributes']['flightNo'] == 'LH400'
    assert isinstance(aps['attributes']['startedAt'], float)


def test_update_row_wins_over_start_row(client, sb, auth, apns):
    _register(client, kind='start', la_token=LA_TOKEN_A, activity_id=None)
    _register(client, kind='update', la_token=LA_TOKEN_B)
    r = _push(client, attributes={'startedAt': '2026-07-27T09:00:00Z'})
    assert r.get_json()['event'] == 'update'
    assert apns['client'].sent[0]['token'] == LA_TOKEN_B


def test_push_to_start_requires_startedAt(client, sb, auth, apns):
    _register(client, kind='start', la_token=LA_TOKEN_A, activity_id=None)
    r = _push(client, attributes={'flightNo': 'LH400'})
    assert r.status_code == 400
    assert r.get_json()['error'] == 'invalid_attributes'
    assert apns['client'].sent == []


def test_internal_push_rejects_bad_content_state(client, sb, auth, apns):
    _register(client, la_token=LA_TOKEN_A)
    bad = _state()
    del bad['mainTime']
    r = _push(client, state=bad)
    assert r.status_code == 400
    assert r.get_json()['error'] == 'invalid_content_state'
    assert apns['client'].sent == []


# ════════════════════════════════════════════════════════════════════════════
# 5) Rate-Limit (Digest) + Monotonie
# ════════════════════════════════════════════════════════════════════════════

def test_unchanged_content_is_skipped_without_apns_call(client, sb, auth, apns):
    """Apple drosselt Live-Activity-Pushes hart: identischer Inhalt ⇒ kein
    Push. Der Countdown selbst braucht NIE einen — die Activity rendert ihn
    client-seitig mit `Text(timerInterval:)`."""
    _register(client, la_token=LA_TOKEN_A)
    assert _push(client).get_json()['sent'] == 1
    assert len(apns['client'].sent) == 1

    # Gleicher Inhalt, nur ein neuer generatedAt-Stempel.
    r = _push(client, state=_state(generatedAt='2026-07-27T12:05:00Z'))
    assert r.status_code == 200
    body = r.get_json()
    assert body['sent'] == 0 and body['unchanged'] == 1
    assert body['skipped'] == 'unchanged'
    assert len(apns['client'].sent) == 1        # KEIN zweiter APNs-Call


def test_real_change_is_sent(client, sb, auth, apns):
    _register(client, la_token=LA_TOKEN_A)
    _push(client)
    r = _push(client, state=_state(deltaMin=20))
    assert r.get_json()['sent'] == 1
    assert len(apns['client'].sent) == 2


def test_force_overrides_digest_skip(client, sb, auth, apns):
    _register(client, la_token=LA_TOKEN_A)
    _push(client)
    assert _push(client, force=True).get_json()['sent'] == 1
    assert len(apns['client'].sent) == 2


def test_failed_send_does_not_poison_the_digest(client, sb, auth, apns):
    """Ein fehlgeschlagener Push darf den Digest NICHT fortschreiben, sonst
    würde der nächste ECHTE Versuch als „unverändert" verworfen."""
    _register(client, la_token=LA_TOKEN_A, apns_env='prod')
    apns['script'](lambda host, idx: (429, 'TooManyRequests'))
    r = _push(client)
    assert r.get_json()['failed'] == 1
    assert sb.row()['content_digest'] is None
    assert sb.row()['failure_count'] == 1
    assert sb.row()['active'] is True           # kein Prune bei 429

    apns['script'](lambda host, idx: (200, None))
    assert _push(client).get_json()['sent'] == 1


def test_timestamp_increases_monotonically(client, sb, auth, apns):
    """iOS verwirft ein Update, dessen aps.timestamp <= dem letzten ist. Zwei
    Pushes in derselben Sekunde wären sonst nur einer."""
    _register(client, la_token=LA_TOKEN_A)
    _push(client, state=_state(deltaMin=1))
    _push(client, state=_state(deltaMin=2))
    _push(client, state=_state(deltaMin=3))
    stamps = [s['payload']['aps']['timestamp'] for s in apns['client'].sent]
    assert len(stamps) == 3
    assert stamps[0] < stamps[1] < stamps[2]
    assert sb.row()['last_timestamp'] == stamps[-1]


# ════════════════════════════════════════════════════════════════════════════
# 6) Umgebungs-Retry (der Fehler, der dieses Projekt schon Geld gekostet hat)
# ════════════════════════════════════════════════════════════════════════════

def test_dead_reason_in_wrong_env_retries_and_does_not_prune(client, sb, auth,
                                                             apns):
    """Debug-Build ⇒ SANDBOX-Token. Ein prod-Send antwortet 400
    BadDeviceToken — eine naive Implementierung löscht dabei einen völlig
    gesunden Token."""
    _register(client, la_token=LA_TOKEN_A, apns_env='prod')
    apns['script'](lambda host, idx: (400, 'BadDeviceToken')
                   if 'sandbox' not in host else (200, None))
    r = _push(client)
    assert r.get_json()['sent'] == 1
    assert apns['client'].hosts == ['api.push.apple.com',
                                    'api.sandbox.push.apple.com']
    row = sb.row()
    assert row['active'] is True                # NICHT geprunt
    assert row['end_reason'] is None
    assert row['failure_count'] == 0
    # Die echte Umgebung wird GELERNT → nächster Push startet dort.
    assert row['environment'] == 'sandbox'


def test_learned_env_is_used_first_on_next_push(client, sb, auth, apns):
    _register(client, la_token=LA_TOKEN_A, apns_env='prod')
    apns['script'](lambda host, idx: (400, 'BadDeviceToken')
                   if 'sandbox' not in host else (200, None))
    _push(client, state=_state(deltaMin=1))
    apns['client'].sent.clear()
    apns['script'](lambda host, idx: (200, None))
    _push(client, state=_state(deltaMin=2))
    assert apns['client'].hosts == ['api.sandbox.push.apple.com']


def test_dead_in_both_envs_marks_row_inactive(client, sb, auth, apns):
    _register(client, la_token=LA_TOKEN_A, apns_env='prod')
    apns['script'](lambda host, idx: (410, 'Unregistered'))
    r = _push(client)
    body = r.get_json()
    assert body['sent'] == 0 and body['dead'] == 1
    assert apns['client'].hosts == ['api.push.apple.com',
                                    'api.sandbox.push.apple.com']
    row = sb.row()
    assert row['active'] is False
    assert row['end_reason'] == 'apns_unregistered'


def test_non_dead_failure_never_retries_other_env(client, sb, auth, apns):
    _register(client, la_token=LA_TOKEN_A, apns_env='prod')
    apns['script'](lambda host, idx: (400, 'BadTopic'))
    assert _push(client).get_json()['failed'] == 1
    assert apns['client'].hosts == ['api.push.apple.com']
    assert sb.row()['active'] is True


def test_unknown_env_follows_sandbox_flag(client, sb, auth, apns, monkeypatch):
    monkeypatch.setenv('APNS_USE_SANDBOX', '1')
    _register(client, la_token=LA_TOKEN_A, apns_env='')
    _push(client)
    assert apns['client'].hosts == ['api.sandbox.push.apple.com']


def test_no_jwt_is_a_soft_failure(client, sb, auth, apns, monkeypatch):
    monkeypatch.setattr(A, '_apns_get_jwt', lambda: None)
    _register(client, la_token=LA_TOKEN_A)
    r = _push(client)
    assert r.status_code == 200 and r.get_json()['failed'] == 1
    assert apns['client'].sent == []


# ════════════════════════════════════════════════════════════════════════════
# 7) Internes Secret-Gate
# ════════════════════════════════════════════════════════════════════════════

def test_internal_push_secret_gate(client, sb, auth, apns, monkeypatch):
    monkeypatch.setenv('ADSB_POLL_SECRET', 's3cret')
    _register(client, la_token=LA_TOKEN_A)
    r = client.post('/api/internal/live-activity/push',
                    json={'user_token': TOKEN, 'content_state': _state()})
    assert r.status_code == 403 and r.get_json()['error'] == 'forbidden'
    assert apns['client'].sent == []

    r2 = client.post('/api/internal/live-activity/push',
                     json={'user_token': TOKEN, 'content_state': _state()},
                     headers={'X-Poll-Secret': 's3cret'})
    assert r2.status_code == 200 and r2.get_json()['sent'] == 1


def test_internal_push_bad_event_400(client, sb, auth, apns):
    _register(client, la_token=LA_TOKEN_A)
    r = _push(client, event='start')   # 'start' wird abgeleitet, nie gefordert
    assert r.status_code == 400 and r.get_json()['error'] == 'bad_event'


def test_internal_push_missing_fields_400(client, sb, auth, apns):
    r = client.post('/api/internal/live-activity/push', json={'user_token': ''})
    assert r.status_code == 400


# ════════════════════════════════════════════════════════════════════════════
# 8) MQTT-Hook — seit P7 (2026-07-27) in lh_mqtt.py VERDRAHTET. Der frühere
#    Tripwire-Platzhalter ist laut eigener Anweisung durch echte Fanout-Tests
#    in tests/test_lh_mqtt.py ersetzt (test_event_triggers_live_activity_* /
#    test_inbound_path_never_calls_live_activity). Hier bleibt nur der
#    Signatur-Vertrag, auf den sich die Aufruf-Zeile in lh_mqtt.py verlässt.
# ════════════════════════════════════════════════════════════════════════════

def test_push_for_affected_signature_contract():
    import inspect
    assert callable(LA.push_for_affected)
    assert list(inspect.signature(LA.push_for_affected).parameters) == \
        ['affected', 'kind', 'flight_disp', 'topic_date', 'facts']


def test_push_for_affected_sends_for_delay(sb, auth, apns, monkeypatch):
    sb._upsert({'p_user_token': TOKEN, 'p_kind': 'update',
                'p_activity_id': ACT_ID, 'p_la_token': LA_TOKEN_A,
                'p_bundle_id': BUNDLE, 'p_environment': 'prod',
                'p_device_id': None, 'p_platform': 'ios'})
    sector = {'flight': 'LH400', 'from': 'FRA', 'to': 'JFK',
              'dep_iso': '2026-07-27T15:10:00Z',
              'arr_iso': '2026-07-27T23:35:00Z'}
    facts = {'est_dep': '2026-07-27T15:40:00Z',
             'sched_dep': '2026-07-27T15:10:00Z', 'dep_delay_min': 30}
    sent = LA.push_for_affected([(TOKEN, sector)], 'est_dep', 'LH400',
                                '2026-07-27', facts=facts)
    assert sent == 1
    cs = apns['client'].sent[0]['payload']['aps']['content-state']
    assert cs['phase'] == 'briefing' and cs['deltaMin'] == 30
    assert cs['route'] == 'FRA–JFK' and cs['footLeading'] == 'LH400'
    assert isinstance(cs['estDep'], float)
    assert 'cancelled' not in cs                # kein erfundenes Signal


def test_push_for_affected_carries_leg_timezones(sb, auth, apns):
    """Der Fanout ersetzt das ContentState VOLLSTÄNDIG. Ohne die Zonen hätte
    ein einziges MQTT-Event die Ortszeit auf dem Sperrbildschirm wieder in
    Homebase-/Gerätezeit gedreht — während das Home-Widget die Ortszeit zeigt.
    """
    sb._upsert({'p_user_token': TOKEN, 'p_kind': 'update',
                'p_activity_id': ACT_ID, 'p_la_token': LA_TOKEN_A,
                'p_bundle_id': BUNDLE, 'p_environment': 'prod',
                'p_device_id': None, 'p_platform': 'ios'})
    sector = {'flight': 'LH454', 'from': 'FRA', 'to': 'SFO',
              'dep_iso': '2026-07-28T08:25:00Z',
              'arr_iso': '2026-07-28T19:40:00Z'}
    LA.push_for_affected([(TOKEN, sector)], 'departed', 'LH454',
                         '2026-07-28',
                         facts={'est_arr': '2026-07-28T19:55:00Z'})
    cs = apns['client'].sent[0]['payload']['aps']['content-state']
    assert cs['fromTZIdentifier'] == 'Europe/Berlin'
    assert cs['toTZIdentifier'] == 'America/Los_Angeles'


def test_push_for_affected_invents_no_timezone(sb, auth, apns):
    """Unbekannter IATA-Code ⇒ Feld fällt weg. Der Client behauptet dann keine
    Ortszeit, statt eine geratene Zone anzuzeigen."""
    sb._upsert({'p_user_token': TOKEN, 'p_kind': 'update',
                'p_activity_id': ACT_ID, 'p_la_token': LA_TOKEN_A,
                'p_bundle_id': BUNDLE, 'p_environment': 'prod',
                'p_device_id': None, 'p_platform': 'ios'})
    sector = {'flight': 'XX1', 'from': 'FRA', 'to': 'QQQ',
              'dep_iso': '2026-07-28T08:25:00Z',
              'arr_iso': '2026-07-28T19:40:00Z'}
    LA.push_for_affected([(TOKEN, sector)], 'departed', 'XX1', '2026-07-28',
                         facts={'est_arr': '2026-07-28T19:55:00Z'})
    cs = apns['client'].sent[0]['payload']['aps']['content-state']
    assert cs['fromTZIdentifier'] == 'Europe/Berlin'
    assert 'toTZIdentifier' not in cs


def test_push_for_affected_ignores_irrelevant_kinds(sb, auth, apns):
    assert LA.push_for_affected([(TOKEN, {})], 'gate', 'LH400',
                                '2026-07-27') == 0
    assert LA.push_for_affected([], 'est_dep', 'LH400', '2026-07-27') == 0
    assert apns['client'].sent == []


def test_push_for_affected_est_arr_updates_arrival_countdown(sb, auth, apns):
    # Owner 2026-07-28 („arrival time was wrong the whole time"): est_arr-
    # Events wurden verworfen — jetzt aktualisieren sie die Karte, und der
    # Countdown zeigt auf die ANKUNFT, nicht den Abflug.
    sb._upsert({'p_user_token': TOKEN, 'p_kind': 'update',
                'p_activity_id': ACT_ID, 'p_la_token': LA_TOKEN_A,
                'p_bundle_id': BUNDLE, 'p_environment': 'prod',
                'p_device_id': None, 'p_platform': 'ios'})
    sector = {'flight': 'LH454', 'from': 'FRA', 'to': 'SFO',
              'dep_iso': '2026-07-28T08:25:00Z',
              'arr_iso': '2026-07-28T19:40:00Z'}
    facts = {'est_dep': '2026-07-28T08:40:00Z',
             'sched_arr': '2026-07-28T19:40:00Z',
             'est_arr': '2026-07-28T19:58:00Z'}
    sent = LA.push_for_affected([(TOKEN, sector)], 'est_arr', 'LH454',
                                '2026-07-28', facts=facts)
    assert sent == 1
    cs = apns['client'].sent[0]['payload']['aps']['content-state']
    assert cs['phase'] == 'inFlight' and cs['kicker'] == 'ANKUNFT'
    # mainTime/countdownTarget = die FRISCHE est_arr, nicht sched/est_dep.
    assert cs['mainTime'] == cs['estArr'] == cs['countdownTarget']
    assert cs['estArr'] != cs['schedArr']


def test_push_for_affected_departed_counts_down_to_arrival(sb, auth, apns):
    # Beim Abflug zählt die Karte ab sofort zur Ankunft — mit den (in lh_mqtt
    # jetzt auch für departed geforcten) LH-Fakten, nicht der Roster-SOLL-Zeit.
    sb._upsert({'p_user_token': TOKEN, 'p_kind': 'update',
                'p_activity_id': ACT_ID, 'p_la_token': LA_TOKEN_A,
                'p_bundle_id': BUNDLE, 'p_environment': 'prod',
                'p_device_id': None, 'p_platform': 'ios'})
    sector = {'flight': 'LH454', 'from': 'FRA', 'to': 'SFO',
              'dep_iso': '2026-07-28T08:25:00Z',
              'arr_iso': '2026-07-28T19:40:00Z'}
    facts = {'est_dep': '2026-07-28T08:40:00Z',
             'est_arr': '2026-07-28T19:55:00Z'}
    LA.push_for_affected([(TOKEN, sector)], 'departed', 'LH454',
                         '2026-07-28', facts=facts)
    cs = apns['client'].sent[0]['payload']['aps']['content-state']
    assert cs['countdownTarget'] == cs['estArr']


def test_push_for_affected_skips_sector_without_times(sb, auth, apns):
    sb._upsert({'p_user_token': TOKEN, 'p_kind': 'update',
                'p_activity_id': ACT_ID, 'p_la_token': LA_TOKEN_A,
                'p_bundle_id': BUNDLE, 'p_environment': 'prod',
                'p_device_id': None, 'p_platform': 'ios'})
    sent = LA.push_for_affected([(TOKEN, {'from': 'FRA', 'to': 'JFK'})],
                                'est_dep', 'LH400', '2026-07-27')
    assert sent == 0 and apns['client'].sent == []


# ════════════════════════════════════════════════════════════════════════════
# 9) Degradierung ohne Supabase
# ════════════════════════════════════════════════════════════════════════════

def test_push_without_supabase_is_a_no_target_noop(client, auth, apns,
                                                   monkeypatch):
    monkeypatch.setattr(LA, '_sb', lambda: None)
    r = _push(client)
    assert r.status_code == 200 and r.get_json()['skipped'] == 'no_target'
    assert apns['client'].sent == []


def test_end_without_supabase_is_503(client, auth, monkeypatch):
    monkeypatch.setattr(LA, '_sb', lambda: None)
    r = client.post('/api/live-activity/end',
                    json={'token': TOKEN, 'activity_id': ACT_ID},
                    headers=_auth_hdr())
    assert r.status_code == 503


def test_blueprint_is_registered_on_the_real_app():
    """Die EINE Wiring-Zeile in app.py:93-115 muss greifen."""
    rules = {r.rule for r in A.app.url_map.iter_rules()}
    assert '/api/push/register-live-activity' in rules
    assert '/api/live-activity/end' in rules
    assert '/api/internal/live-activity/push' in rules
