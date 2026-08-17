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
    """`in_`/`limit` kamen 2026-07-31 fuer den Stale-Sweep dazu (er liest ALLE
    aktiven Zeilen und dazu die Roster-Sektoren der betroffenen Tokens)."""

    def __init__(self, sb, attr='rows'):
        self.sb = sb
        self.attr = attr
        self.filters = {}
        self.ins = {}
        self._limit = None
        self._cols = ''
        self._update = None

    def select(self, _cols):
        self._cols = _cols or ''
        return self

    def update(self, values):
        """`_store_last_state` schreibt den zuletzt gesendeten Zustand zurück."""
        self._update = dict(values or {})
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def in_(self, key, values):
        self.ins[key] = list(values)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        # Fehlende Migration 20260813: PostgREST antwortet auf eine unbekannte
        # Spalte mit 42703 — der Read WIRFT, er liefert keine leere Liste.
        if (getattr(self.sb, 'no_last_state_column', False)
                and ('last_content_state' in (self._cols or '')
                     or 'last_content_state' in (self._update or {}))):
            raise RuntimeError(
                'column live_activities.last_content_state does not exist '
                '(42703)')
        live = [r for r in getattr(self.sb, self.attr)
                if all(r.get(k) == v for k, v in self.filters.items())
                and all(r.get(k) in v for k, v in self.ins.items())]
        if self._update is not None:
            for r in live:
                r.update(self._update)
            return _Res([dict(r) for r in live])
        rows = [dict(r) for r in live]
        if self._limit is not None:
            rows = rows[:self._limit]
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
        self.briefings = []          # user_ical_briefings (nur der Sweep)
        self.obs = []                # airport_delay_obs (R3: Landungs-Beleg)
        self.rpc_calls = []
        self._seq = 0
        self.fail_rpc = set()
        # True = DB ohne Migration 20260813_live_activity_last_state.sql.
        self.no_last_state_column = False

    # ── Client-Oberfläche ────────────────────────────────────────────────
    def table(self, name):
        if name == 'user_ical_briefings':
            return _Table(self, 'briefings')
        if name == 'airport_delay_obs':
            return _Table(self, 'obs')
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
                # Migration 20260813b: ein rotierter Token gehört zu einer
                # anderen Karte — sein Zustand hat keine Vergangenheit.
                row['last_content_state'] = None
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
                # Migration 20260813b: die Karte ist weg, ihr Zustand darf die
                # nächste (dieselbe Zeile!) nicht mehr befüllen.
                row['last_content_state'] = None
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
                             'stored': True, 'content_state_stored': False}

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
    r = _push(client, event='reboot')
    assert r.status_code == 400 and r.get_json()['error'] == 'bad_event'


def test_internal_push_start_ohne_attributes_400(client, sb, auth, apns):
    """`start` OHNE `attributes` ist nicht ausführbar (ActivityKit verlangt
    `attributes-type` + `attributes`). Ehrliches 400 statt stillem Fallback."""
    _register(client, kind='start', la_token=LA_TOKEN_A, activity_id=None)
    r = _push(client, event='start')
    assert r.status_code == 400
    assert r.get_json()['error'] == 'missing_attributes'
    assert apns['client'].sent == []


def test_internal_push_start_ist_jetzt_erlaubt(client, sb, auth, apns):
    """Der `start`-Verbots-Zweig ist am 2026-08-11 gefallen: OHNE ihn war der
    Push-to-Start-Pfad über diesen Endpoint unerreichbar — genau der
    Tester-Befund „nächster Flug erscheint erst nach App-Öffnen"."""
    _register(client, kind='start', la_token=LA_TOKEN_A, activity_id=None)
    r = _push(client, event='start',
              attributes={'flightNo': 'LH400', 'from': 'FRA', 'to': 'JFK',
                          'startedAt': '2026-07-27T09:00:00Z'})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body['event'] == 'start' and body['sent'] == 1
    aps = apns['client'].sent[0]['payload']['aps']
    assert aps['event'] == 'start'
    assert aps['attributes-type'] == 'DutyActivityAttributes'
    assert aps['attributes'] == {'flightNo': 'LH400', 'from': 'FRA',
                                 'to': 'JFK',
                                 'startedAt': LA._to_apple_date(
                                     '2026-07-27T09:00:00Z')}


def test_internal_push_start_startet_keine_zweite_karte(client, sb, auth,
                                                        apns):
    """Läuft schon eine Activity, gewinnt sie — `start` heisst „darf
    entstehen", nicht „starte auf jeden Fall" (EINE Karte pro Dienst)."""
    _register(client, kind='start', la_token=LA_TOKEN_A, activity_id=None)
    _register(client, kind='update', la_token=LA_TOKEN_B)
    r = _push(client, event='start',
              attributes={'startedAt': '2026-07-27T09:00:00Z'})
    assert r.status_code == 200
    assert r.get_json()['event'] == 'update'
    assert apns['client'].sent[0]['token'] == LA_TOKEN_B
    assert 'attributes' not in apns['client'].sent[0]['payload']['aps']


def test_start_cooldown_verhindert_die_zweite_karte(client, sb, auth, apns):
    """DAS START-SPAM-LOCH: zwischen Push-to-Start und dem Hochladen des
    Update-Tokens sieht der Server weiterhin keine `update`-Zeile. Ein zweites
    Event in diesem Fenster darf keine ZWEITE Karte erzeugen."""
    _register(client, kind='start', la_token=LA_TOKEN_A, activity_id=None)
    attrs = {'flightNo': 'LH400', 'from': 'FRA', 'to': 'JFK',
             'startedAt': '2026-07-27T09:00:00Z'}
    r1 = _push(client, event='start', attributes=attrs)
    assert r1.get_json()['sent'] == 1

    # Inhaltlich ANDERER State (sonst griffe schon der Digest-Dedupe) —
    # es muss der Cooldown sein, der bremst.
    r2 = _push(client, state=_state(kicker='ABFLUG'), event='start',
               attributes=attrs)
    assert r2.status_code == 200
    assert r2.get_json()['skipped'] == 'start_cooldown'
    assert r2.get_json()['sent'] == 0
    assert len(apns['client'].sent) == 1


def test_start_cooldown_greift_nach_einem_fehlschlag_nicht(client, sb, auth,
                                                           apns):
    """Ein GESCHEITERTER Start darf den nächsten nicht sperren — sonst reicht
    ein einzelner APNs-Aussetzer für einen Dienst ohne Karte."""
    _register(client, kind='start', la_token=LA_TOKEN_A, activity_id=None)
    attrs = {'startedAt': '2026-07-27T09:00:00Z'}
    apns['script'](lambda host, idx: (500, None) if idx == 0 else (200, None))
    r1 = _push(client, event='start', attributes=attrs)
    assert r1.get_json()['sent'] == 0 and r1.get_json()['failed'] == 1

    r2 = _push(client, state=_state(kicker='ABFLUG'), event='start',
               attributes=attrs)
    assert r2.get_json()['sent'] == 1
    assert len(apns['client'].sent) == 2


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
    # `dep_status` ergaenzt 2026-07-31: est_arr behauptet `inFlight` nur noch
    # mit ABFLUG-BELEG (Owner: „nur nicht Fake-Abflug wenn keins"). Genau das
    # liefert LH fuer einen Flieger in der Luft — auf Prod gemessen tragen
    # 4 von 4 laufenden Langstrecken 'Flight Departed'.
    facts = {'est_dep': '2026-07-28T08:40:00Z',
             'sched_arr': '2026-07-28T19:40:00Z',
             'est_arr': '2026-07-28T19:58:00Z',
             'dep_status': 'Flight Departed'}
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


def test_push_for_affected_traegt_die_flugnummer_im_state(sb, auth, apns):
    """Die Pille darf nicht an den beim Start eingefrorenen `attributes`
    hängen — nach einem Turnaround stünde dort sonst weiter der Hinflug."""
    sb._upsert({'p_user_token': TOKEN, 'p_kind': 'update',
                'p_activity_id': ACT_ID, 'p_la_token': LA_TOKEN_A,
                'p_bundle_id': BUNDLE, 'p_environment': 'prod',
                'p_device_id': None, 'p_platform': 'ios'})
    LA.push_for_affected([(TOKEN, _mqtt_sector())], 'est_dep', 'LH400',
                         '2026-07-27')
    cs = apns['client'].sent[0]['payload']['aps']['content-state']
    assert cs['flightNo'] == 'LH400'


# ════════════════════════════════════════════════════════════════════════════
# 8b) PUSH-TO-START AUS DEM FANOUT (2026-08-11)
#
#     Tester-Befund: „Live Activities aktualisieren sich nicht selbstständig —
#     der nächste Flug erscheint erst nach App-Öffnen." Ursache war NICHT der
#     fehlende Push-Weg (der war gebaut), sondern der einzige Produzent, der
#     ihn ohne `attributes` aufrief: ohne die nimmt `push_live_activity` die
#     `start`-Zeile gar nicht erst in die Hand.
# ════════════════════════════════════════════════════════════════════════════

def _mqtt_sector(dep_in_h=1.0, block_h=8.0, now=None):
    """Sektor mit Abflug relativ zu JETZT — der Start-Gate hängt am Fenster."""
    from datetime import timedelta
    now = now or datetime.now(timezone.utc)
    dep = now + timedelta(hours=dep_in_h)
    return {'flight': 'LH400', 'from': 'FRA', 'to': 'JFK',
            'dep_iso': dep.isoformat().replace('+00:00', 'Z'),
            'arr_iso': (dep + timedelta(hours=block_h)).isoformat()
                       .replace('+00:00', 'Z')}


def _start_row(sb):
    sb._upsert({'p_user_token': TOKEN, 'p_kind': 'start',
                'p_activity_id': None, 'p_la_token': LA_TOKEN_A,
                'p_bundle_id': BUNDLE, 'p_environment': 'prod',
                'p_device_id': None, 'p_platform': 'ios'})


def test_fanout_erzeugt_die_karte_per_push_to_start(sb, auth, apns):
    """DER FIX. Nur ein push-to-start-Token registriert (App war seit der
    letzten Landung nicht offen) — der Fanout muss die Karte ERZEUGEN."""
    _start_row(sb)
    sent = LA.push_for_affected([(TOKEN, _mqtt_sector())], 'est_dep', 'LH400',
                                '2026-07-27')
    assert sent == 1
    aps = apns['client'].sent[0]['payload']['aps']
    assert aps['event'] == 'start'
    assert aps['attributes-type'] == 'DutyActivityAttributes'
    attrs = aps['attributes']
    # Feldnamen/Typen 1:1 gegen Swift `DutyActivityAttributes`.
    assert set(attrs) == {'flightNo', 'from', 'to', 'startedAt'}
    assert attrs['flightNo'] == 'LH400'
    assert attrs['from'] == 'FRA' and attrs['to'] == 'JFK'
    assert isinstance(attrs['startedAt'], float)
    # Apple-Referenzdatum, nicht Unix — dieselbe Epoche wie die content-state-
    # Dates (aps.timestamp dagegen ist Unix).
    now_unix = datetime.now(timezone.utc).timestamp()
    assert abs((attrs['startedAt'] + APPLE_EPOCH) - now_unix) < 60


def test_fanout_startet_keine_zweite_karte_wenn_eine_laeuft(sb, auth, apns):
    """EINE Live Activity pro Dienst: die laufende `update`-Zeile gewinnt."""
    _start_row(sb)
    sb._upsert({'p_user_token': TOKEN, 'p_kind': 'update',
                'p_activity_id': ACT_ID, 'p_la_token': LA_TOKEN_B,
                'p_bundle_id': BUNDLE, 'p_environment': 'prod',
                'p_device_id': None, 'p_platform': 'ios'})
    LA.push_for_affected([(TOKEN, _mqtt_sector())], 'est_dep', 'LH400',
                         '2026-07-27')
    assert len(apns['client'].sent) == 1
    assert apns['client'].sent[0]['token'] == LA_TOKEN_B
    assert apns['client'].sent[0]['payload']['aps']['event'] == 'update'


def test_fanout_startet_nicht_zweimal_hintereinander(sb, auth, apns):
    """Zwischen Start und dem Hochladen des Update-Tokens sieht der Server
    weiter keine `update`-Zeile. Ohne Cooldown wären das zwei Karten."""
    _start_row(sb)
    LA.push_for_affected([(TOKEN, _mqtt_sector())], 'est_dep', 'LH400',
                         '2026-07-27')
    LA.push_for_affected([(TOKEN, _mqtt_sector(dep_in_h=1.5))], 'est_dep',
                         'LH400', '2026-07-27')
    assert len(apns['client'].sent) == 1


def test_fanout_startet_nicht_lange_vor_dem_abflug(sb, auth, apns):
    """MQTT-Topics stehen bis 48 h im Voraus. Eine Karte, die 30 h vor Abflug
    entsteht, würde der eigene Sweep binnen 5 min wieder beenden."""
    _start_row(sb)
    sent = LA.push_for_affected([(TOKEN, _mqtt_sector(dep_in_h=30))],
                                'est_dep', 'LH400', '2026-07-27')
    assert sent == 0 and apns['client'].sent == []


def test_fanout_startet_nicht_auf_arrived(sb, auth, apns):
    """Eine Karte, die als „GELANDET" entsteht, hat nie einen Flug gezeigt."""
    _start_row(sb)
    sent = LA.push_for_affected([(TOKEN, _mqtt_sector(dep_in_h=-8))],
                                'arrived', 'LH400', '2026-07-27')
    assert sent == 0 and apns['client'].sent == []


def test_fanout_aktualisiert_arrived_weiterhin(sb, auth, apns):
    """Kein Start ≠ kein Update: eine LAUFENDE Karte muss die Landung sehen."""
    sb._upsert({'p_user_token': TOKEN, 'p_kind': 'update',
                'p_activity_id': ACT_ID, 'p_la_token': LA_TOKEN_A,
                'p_bundle_id': BUNDLE, 'p_environment': 'prod',
                'p_device_id': None, 'p_platform': 'ios'})
    sent = LA.push_for_affected([(TOKEN, _mqtt_sector(dep_in_h=-8))],
                                'arrived', 'LH400', '2026-07-27')
    assert sent == 1
    assert apns['client'].sent[0]['payload']['aps']['event'] == 'update'


def test_fanout_startet_nicht_ohne_bekannten_abflug(sb, auth, apns):
    """Kein Abflug ⇒ kein Fenster ⇒ kein Start. Geraten wird nichts."""
    _start_row(sb)
    from datetime import timedelta
    arr = (datetime.now(timezone.utc) + timedelta(hours=3))
    sector = {'flight': 'LH400', 'from': 'FRA', 'to': 'JFK',
              'arr_iso': arr.isoformat().replace('+00:00', 'Z')}
    sent = LA.push_for_affected([(TOKEN, sector)], 'est_arr', 'LH400',
                                '2026-07-27',
                                facts={'est_arr': sector['arr_iso'],
                                       'dep_status': 'Flight Departed'})
    assert sent == 0 and apns['client'].sent == []


def test_start_attributes_spiegeln_den_swift_vertrag():
    """`_ATTRIBUTE_FIELDS` IST der Wire-Contract gegen
    `ios/AeroTax/Shared/DutyActivityAttributes.swift`. Wer dort ein Feld
    hinzufügt, muss es hier eintragen — sonst wirft der Normalizer es weg."""
    assert LA._ATTRIBUTE_FIELDS == {'flightNo': 'str', 'from': 'str',
                                    'to': 'str', 'startedAt': 'date'}
    assert LA._REQUIRED_ATTRIBUTE_KEYS == ('startedAt',)
    attrs = LA._mqtt_start_attributes('est_dep', 'LH400', 'FRA', 'JFK',
                                      datetime.now(timezone.utc),
                                      datetime.now(timezone.utc))
    assert set(attrs) <= set(LA._ATTRIBUTE_FIELDS)


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


# ════════════════════════════════════════════════════════════════════════════
# R2 — die Karte darf nicht ewig „im Flug" luegen (Audit 2026-07-31)
# ════════════════════════════════════════════════════════════════════════════
# `push_for_affected` ist der EINZIGE Produzent dieser Live Activities. Geht ein
# `arrived`-Event verloren — QoS 0 + clean session (kein Replay), dazu ~10
# Daemon-Deploys/Tag mit je 50-60 s Blindfenster —, blieb die Karte fuer IMMER
# im Zustand „inFlight" und zaehlte auf eine laengst vergangene Ankunft.
#
#   (a) `aps.stale-date` wirkt OHNE App-Update: ActivityKit liest den Header
#       selbst und markiert die Karte ab dann als veraltet.
#   (b) Der Sweep beendet Karten, fuer die es keine Grundlage mehr gibt —
#       mit einem ECHTEN letzten bekannten Zeitpunkt, nie mit einer erfundenen
#       Landezeit.

from datetime import timedelta                                    # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _clock_freeze import FROZEN_UTC, apply_frozen_clock          # noqa: E402


# ── (a) stale-date ──────────────────────────────────────────────────────────

def test_stale_after_bezieht_sich_auf_den_gezeigten_zeitpunkt():
    """Nicht „jetzt + fixe Dauer": eine Karte, die auf eine Ankunft in 9 h
    zeigt, darf nicht nach zwei Stunden als veraltet gelten."""
    now = 1_800_000_000.0
    target = now + 9 * 3600
    assert LA._stale_after_s(target, now_ts=now) == 9 * 3600 + LA._LA_STALE_GRACE_S


def test_stale_after_klemmt_nach_unten():
    """Ein bereits vergangener Zeitpunkt darf die Karte nicht rueckwirkend
    veralten lassen — sonst flackert sie beim ersten Update nach der Landung."""
    now = 1_800_000_000.0
    assert LA._stale_after_s(now - 10 * 3600, now_ts=now) == LA._LA_STALE_MIN_S


def test_stale_after_klemmt_nach_oben():
    now = 1_800_000_000.0
    assert LA._stale_after_s(now + 40 * 3600, now_ts=now) == LA._LA_STALE_MAX_S


def test_stale_after_ohne_bezugszeitpunkt_behauptet_nichts():
    assert LA._stale_after_s(None) is None
    assert LA._stale_after_s('kein-datum') is None


def _sector(dep_h, arr_h, base=None):
    base = base or datetime.now(timezone.utc)
    return {'from': 'MUC', 'to': 'ORD',
            'dep_iso': (base + timedelta(hours=dep_h)).isoformat(),
            'arr_iso': (base + timedelta(hours=arr_h)).isoformat()}


def test_fanout_setzt_stale_date_auf_ankunft_plus_kulanz(client, sb, auth,
                                                          apns):
    """Der Kern von R2a: jedes Fanout-Update traegt jetzt eine Verfallsmarke."""
    _register(client)
    base = datetime.now(timezone.utc)
    sector = _sector(-6, 3, base)                 # mitten im Langstreckenflug
    LA.push_for_affected([(TOKEN, sector)], 'departed', 'LH433', '2026-07-22')
    aps = apns['client'].sent[-1]['payload']['aps']
    arr = base + timedelta(hours=3)
    assert 'stale-date' in aps
    assert abs(aps['stale-date']
               - (arr.timestamp() + LA._LA_STALE_GRACE_S)) <= 5
    # Unix-Sekunden, NICHT Apple-Referenzdatum (zwei Epochen im selben Payload).
    assert aps['stale-date'] > APPLE_EPOCH


def test_stale_date_wandert_mit_jeder_neuen_schaetzung_mit(client, sb, auth,
                                                           apns):
    """Solange Events kommen, wird die Marke nachgeschoben. Bleiben sie aus,
    laeuft genau die zuletzt gesetzte ab."""
    _register(client)
    base = datetime.now(timezone.utc)
    LA.push_for_affected([(TOKEN, _sector(-6, 3, base))], 'departed', 'LH433',
                         '2026-07-22')
    first = apns['client'].sent[-1]['payload']['aps']['stale-date']
    LA.push_for_affected([(TOKEN, _sector(-6, 4, base))], 'est_arr', 'LH433',
                         '2026-07-22',
                         facts={'dep_status': 'Flight Departed'})
    second = apns['client'].sent[-1]['payload']['aps']['stale-date']
    # +/-2 s: `aps.timestamp` MUSS strikt monoton steigen, zwei Pushes in
    # derselben Sekunde bekommen deshalb last+1 (s. _next_timestamp).
    assert abs((second - first) - 3600) <= 2


def test_stale_date_erzeugt_keinen_zusaetzlichen_push(client, sb, auth, apns):
    """Die Marke steht im aps-Block, nicht im content-state — sie darf den
    Digest-Schutz nicht aushebeln (Apple drosselt Live-Activity-Pushes hart)."""
    _register(client)
    base = datetime.now(timezone.utc)
    sector = _sector(-6, 3, base)
    LA.push_for_affected([(TOKEN, sector)], 'departed', 'LH433', '2026-07-22')
    assert len(apns['client'].sent) == 1
    LA.push_for_affected([(TOKEN, sector)], 'departed', 'LH433', '2026-07-22')
    assert len(apns['client'].sent) == 1          # unveraendert ⇒ kein Push


# ── (b) zeitbasiertes Ende ──────────────────────────────────────────────────

@pytest.fixture
def frozen(monkeypatch):
    """EINE eingefrorene Uhr fuer Blueprint, lh_mqtt (liefert die Fenster-
    Definition) und dieses Testmodul — sonst rechnen Test-Eingabe und
    Produktion gegen verschiedene „jetzt"."""
    from blueprints import lh_mqtt as _mqtt
    apply_frozen_clock(monkeypatch,
                       extra_modules=(LA, _mqtt, sys.modules[__name__]),
                       app_module=A)
    LA._sweep_state['last'] = 0.0
    LA._sweep_state['running'] = False
    yield FROZEN_UTC
    LA._sweep_state['last'] = 0.0
    LA._sweep_state['running'] = False


def _brief(sectors, token=TOKEN, day_offset=0):
    d = (FROZEN_UTC.date() + timedelta(days=day_offset)).isoformat()
    return {'token': token, 'datum': d,
            'updated_at': FROZEN_UTC.isoformat(), 'sectors': sectors}


def _age_row(sb_fake, hours):
    row = sb_fake.row()
    row['updated_at'] = (FROZEN_UTC - timedelta(hours=hours)).isoformat()
    return row


def test_sweep_beendet_karte_ohne_laufendes_leg(client, sb, auth, apns, frozen):
    """Der Fall aus dem Audit: `arrived` ist verlorengegangen, die Karte stuende
    sonst bis in alle Ewigkeit auf „im Flug"."""
    _register(client)
    _age_row(sb, 0)
    sb.briefings.append(_brief([_sector(-11, -5, FROZEN_UTC)]))
    counts = LA.sweep_stale_live_activities(now_utc=FROZEN_UTC)
    assert counts['ended'] == 1 and counts['kept'] == 0
    aps = apns['client'].sent[-1]['payload']['aps']
    assert aps['event'] == 'end'
    # sofort weg — Owner: „muss schon richtig sein sonst weg."
    assert aps['dismissal-date'] == aps['timestamp']
    assert sb.row()['active'] is False
    assert sb.row()['end_reason'] == 'stale_no_running_leg'


def test_sweep_laesst_langstrecke_mitten_im_flug_in_ruhe(client, sb, auth, apns,
                                                         frozen):
    """DIE Gegenprobe. Genau diese Karten waren der Grund fuer R1 — der Sweep
    darf sie unter keinen Umstaenden abraeumen."""
    _register(client)
    _age_row(sb, 0)
    sb.briefings.append(_brief([_sector(-6, 3, FROZEN_UTC)]))
    counts = LA.sweep_stale_live_activities(now_utc=FROZEN_UTC)
    assert counts == {'checked': 1, 'ended': 0, 'kept': 1, 'no_roster': 0,
                      'hard_age': 0, 'landed': 0}
    assert not apns['client'].sent
    assert sb.row()['active'] is True


def test_sweep_laesst_die_karte_vor_dem_abflug_in_ruhe(client, sb, auth, apns,
                                                       frozen):
    """Der Client startet die Activity zum Briefing, also Stunden VOR dem
    Abflug. Ein Sweep, der nur „laeuft gerade" kennt, wuerde sie sofort
    wegraeumen."""
    _register(client)
    _age_row(sb, 0)
    sb.briefings.append(_brief([_sector(3, 12, FROZEN_UTC)], day_offset=0))
    assert LA.sweep_stale_live_activities(now_utc=FROZEN_UTC)['kept'] == 1
    assert not apns['client'].sent


def test_sweep_gibt_der_geplanten_ankunft_kulanz(client, sb, auth, apns,
                                                 frozen):
    """Nicht auf die Minute: erst nach dem Abo-Fenster (Ankunft +1 h) PLUS
    90 min Kulanz wird beendet. Eine verspaetete Landung darf die Karte nicht
    verlieren."""
    _register(client)
    _age_row(sb, 0)
    sb.briefings.append(_brief([_sector(-8, -2, FROZEN_UTC)]))   # arr vor 2 h
    assert LA.sweep_stale_live_activities(now_utc=FROZEN_UTC)['kept'] == 1
    assert not apns['client'].sent


def test_sweep_erfindet_keine_zeit(client, sb, auth, apns, frozen):
    """Owner-Regel: lieber keine Zeile als ein synthetisierter Wert. Der
    Abschluss traegt die LETZTE BEKANNTE Ankunft — nicht „jetzt" und keine
    geschaetzte Landung."""
    _register(client)
    _age_row(sb, 0)
    arr = FROZEN_UTC - timedelta(hours=5)
    sb.briefings.append(_brief([_sector(-11, -5, FROZEN_UTC)]))
    LA.sweep_stale_live_activities(now_utc=FROZEN_UTC)
    state = apns['client'].sent[-1]['payload']['aps']['content-state']
    assert abs(state['mainTime'] - (arr.timestamp() - APPLE_EPOCH)) < 2


def test_sweep_ohne_roster_beleg_beendet_nichts(client, sb, auth, apns, frozen):
    """Keine Daten sind kein Beleg fuer „ist vorbei" — dieselbe Regel wie bei
    der Frische-Schranke im MQTT-Fanout."""
    _register(client)
    _age_row(sb, 2)
    counts = LA.sweep_stale_live_activities(now_utc=FROZEN_UTC)
    assert counts['no_roster'] == 1 and counts['ended'] == 0
    assert not apns['client'].sent
    assert sb.row()['active'] is True


def test_sweep_reissleine_bei_uralter_zeile(client, sb, auth, apns, frozen):
    """Ohne Roster-Beleg greift nur die Reissleine: eine Activity, die seit
    14 h niemand angefasst hat, KANN nichts Aktuelles mehr zeigen — ActivityKit
    beendet sie ohnehin nach spaetestens 8 h Laufzeit."""
    _register(client)
    _age_row(sb, LA._LA_SWEEP_HARD_MAX_AGE_H + 2)
    counts = LA.sweep_stale_live_activities(now_utc=FROZEN_UTC)
    assert counts['ended'] == 1 and counts['hard_age'] == 1
    assert sb.row()['end_reason'] == 'stale_max_age'


def test_sweep_zaehlt_fremde_tokens_nicht_als_beleg(client, sb, auth, apns,
                                                    frozen):
    """Der Roster-Read ist gebuendelt — ein laufender Flug eines ANDEREN Users
    darf die Karte hier nicht am Leben halten."""
    _register(client)
    _age_row(sb, 0)
    sb.briefings.append(_brief([_sector(-6, 3, FROZEN_UTC)], token='AT-FREMD'))
    sb.briefings.append(_brief([_sector(-11, -5, FROZEN_UTC)]))
    assert LA.sweep_stale_live_activities(now_utc=FROZEN_UTC)['ended'] == 1


def test_sweep_legt_die_zeile_auch_bei_apns_fehler_still(client, sb, auth,
                                                         apns, frozen):
    """Sonst pusht der Fanout weiter gegen eine Karte, die nachweislich nichts
    Richtiges mehr zeigt."""
    _register(client)
    _age_row(sb, 0)
    sb.briefings.append(_brief([_sector(-11, -5, FROZEN_UTC)]))
    apns['script'](lambda host, idx: (500, 'InternalServerError'))
    assert LA.sweep_stale_live_activities(now_utc=FROZEN_UTC)['ended'] == 1
    assert sb.row()['active'] is False


def test_sweep_ruehrt_beendete_zeilen_nicht_mehr_an(client, sb, auth, apns,
                                                    frozen):
    """Idempotenz: ein zweiter Lauf darf nicht noch einmal pushen."""
    _register(client)
    _age_row(sb, 0)
    sb.briefings.append(_brief([_sector(-11, -5, FROZEN_UTC)]))
    LA.sweep_stale_live_activities(now_utc=FROZEN_UTC)
    n = len(apns['client'].sent)
    assert LA.sweep_stale_live_activities(now_utc=FROZEN_UTC)['checked'] == 0
    assert len(apns['client'].sent) == n


def test_sweep_macht_keinen_lh_call(client, sb, auth, apns, frozen,
                                    monkeypatch):
    """Der Sweep rechnet AUSSCHLIESSLICH gegen gespeicherte Roster-Zeiten. Ein
    LH-Call pro Karte waere genau die Quota-Falle, aus der dieses Modul kommt."""
    from blueprints import lh_mqtt as _mqtt
    monkeypatch.setattr(_mqtt, 'lh_flight_facts',
                        lambda *a, **k: pytest.fail('Sweep ruft LH'))
    _register(client)
    _age_row(sb, 0)
    sb.briefings.append(_brief([_sector(-11, -5, FROZEN_UTC)]))
    assert LA.sweep_stale_live_activities(now_utc=FROZEN_UTC)['ended'] == 1


def test_sweep_ohne_supabase_tut_nichts(monkeypatch, frozen):
    """SB-Ausfall heisst „ich weiss nichts" — nicht „alle Karten weg"."""
    monkeypatch.setattr(LA, '_sb', lambda: None)
    assert LA.sweep_stale_live_activities(now_utc=FROZEN_UTC)['ended'] == 0


def test_sweep_killswitch(client, sb, auth, apns, frozen, monkeypatch):
    monkeypatch.setenv('AEROX_LA_SWEEP', '0')
    _register(client)
    _age_row(sb, 99)
    counts = LA.sweep_stale_live_activities(now_utc=FROZEN_UTC)
    assert counts.get('disabled') is True and counts['ended'] == 0
    assert LA.kick_sweep() is False


def test_kick_sweep_deckelt_sich(monkeypatch, frozen):
    """Drei Gunicorn-Worker mal alle 300 s Topic-Poll — ohne Deckel liefe der
    Sweep dauernd."""
    started = []
    monkeypatch.setattr(LA.threading, 'Thread',
                        lambda **kw: started.append(kw) or _NoopThread())
    assert LA.kick_sweep() is True
    assert LA.kick_sweep() is False
    assert len(started) == 1


class _NoopThread:
    def start(self):
        return None


def test_sweep_endpoint_secret_gate(client, sb, auth, monkeypatch):
    monkeypatch.setenv('ADSB_POLL_SECRET', 'geheim')
    assert client.post('/api/internal/live-activity/sweep').status_code == 403
    r = client.post('/api/internal/live-activity/sweep',
                    headers={'X-Poll-Secret': 'geheim'})
    assert r.status_code == 200 and r.get_json()['ok'] is True


# ════════════════════════════════════════════════════════════════════════════
# R3 (Tibor 2026-08-02) — die BESTAETIGTE LANDUNG beendet die Karte
# ════════════════════════════════════════════════════════════════════════════
# Befund: die Karte klebte 12+ min nach dem Aufsetzen auf „im Flug / noch
# 0:00" (LH1457). Die Landung stand um 07:04Z als „Gelandet" in
# `airport_delay_obs` — nur reichte sie niemand an die Activity weiter: der
# MQTT-Fanout sah kein `arrived`-Event, und der Stale-Sweep rechnete
# ausschliesslich gegen PLANZEITEN.
#
# Owner-Staffelung: bestaetigter Fakt darf weiterlaufen; nur-Plan nie in den
# Flug-Zustand; traegt der letzte bestaetigte Fakt nicht mehr (bestaetigte
# Ankunft + 60 min), endet die Aktivitaet.

def _leg(dep_h, arr_h, base=None, flight='LH1457', frm='MUC', to='FRA'):
    base = base or FROZEN_UTC
    return {'flight': flight, 'from': frm, 'to': to,
            'dep_iso': (base + timedelta(hours=dep_h)).isoformat(),
            'arr_iso': (base + timedelta(hours=arr_h)).isoformat()}


def _obs(landed_min_ago=12, status='Gelandet', flight='LH1457', to='FRA',
         stamp_min_ago=None, cancelled=False, esti=True):
    """Eine ARR-Board-Zeile, wie der Poller sie schreibt (Board-Zeiten sind
    STATIONS-LOKAL — hier FRA, also Europe/Berlin)."""
    from zoneinfo import ZoneInfo
    arr = FROZEN_UTC - timedelta(minutes=landed_min_ago)
    local = arr.astimezone(ZoneInfo('Europe/Berlin'))
    stamp = FROZEN_UTC - timedelta(
        minutes=stamp_min_ago if stamp_min_ago is not None
        else max(0, landed_min_ago - 2))
    return {'airport': f'{to}#ARR', 'flight': flight,
            'date': local.date().isoformat(),
            'sched': local.strftime('%H:%M'),
            'esti': local.strftime('%H:%M') if esti else None,
            'status': status, 'cancelled': cancelled,
            'updated_at': stamp.isoformat(),
            'esti_changed_at': stamp.isoformat()}


def test_bestaetigte_landung_beendet_die_karte(client, sb, auth, apns, frozen):
    """DER Tibor-Fall. Ohne diesen Zweig stuende die Karte noch ~2,5 h."""
    _register(client)
    _age_row(sb, 0)
    sb.briefings.append(_brief([_leg(-1.5, -0.2)]))
    sb.obs.append(_obs(landed_min_ago=12))
    counts = LA.sweep_stale_live_activities(now_utc=FROZEN_UTC)
    assert counts['ended'] == 1 and counts['landed'] == 1
    aps = apns['client'].sent[-1]['payload']['aps']
    assert aps['event'] == 'end'
    state = aps['content-state']
    assert state['kicker'] == 'GELANDET'
    assert state['arrConfirmed'] is True and state['depConfirmed'] is True
    assert sb.row()['end_reason'] == 'arrived_confirmed'


def test_abschluss_traegt_die_ECHTE_landezeit(client, sb, auth, apns, frozen):
    """Owner-Regel „lieber keine Zeile als ein synthetisierter Wert": im
    Abschluss steht die gemessene Landung, nicht „jetzt" und nicht die
    Plan-Ankunft."""
    _register(client)
    _age_row(sb, 0)
    sb.briefings.append(_brief([_leg(-1.5, -0.2)]))       # Plan-Ankunft: -12 min
    sb.obs.append(_obs(landed_min_ago=25))                # gemessen: -25 min
    LA.sweep_stale_live_activities(now_utc=FROZEN_UTC)
    state = apns['client'].sent[-1]['payload']['aps']['content-state']
    ist = FROZEN_UTC - timedelta(minutes=25)
    assert abs(state['mainTime'] - (ist.timestamp() - APPLE_EPOCH)) < 60
    assert abs(state['estArr'] - (ist.timestamp() - APPLE_EPOCH)) < 60


def test_karte_verschwindet_erst_zur_60_min_frist(client, sb, auth, apns,
                                                  frozen):
    """Die Staffelung sagt „bestaetigte Ankunft + 60 min" — nicht „sofort
    weg". Die dismissal-date liegt genau auf dieser Frist."""
    _register(client)
    _age_row(sb, 0)
    sb.briefings.append(_brief([_leg(-1.5, -0.2)]))
    sb.obs.append(_obs(landed_min_ago=12))
    LA.sweep_stale_live_activities(now_utc=FROZEN_UTC)
    aps = apns['client'].sent[-1]['payload']['aps']
    # `dismissal-date` sind Unix-Sekunden ab `aps.timestamp` (die Uhr des
    # Sende-Moments) — die Karte steht noch die RESTLICHE Frist: 60 min minus
    # der 12 min, die seit der Landung vergangen sind.
    rest = LA._LA_LANDED_HOLD_S - 12 * 60
    assert abs(aps['dismissal-date'] - (aps['timestamp'] + rest)) <= 90
    assert aps['dismissal-date'] > aps['timestamp']       # nicht sofort weg


def test_eingefrorene_prognose_ist_keine_landung(client, sb, auth, apns,
                                                 frozen):
    """Die Messhuerde (`esti_changed_at`, Migration 20260802): ein Board, das
    „Gelandet" schaltet, aber die Plan-`esti` von vor drei Stunden stehen
    laesst, beendet hier gar nichts. Terminaler Status beweist die LANDUNG,
    nicht die ZEIT."""
    _register(client)
    _age_row(sb, 0)
    sb.briefings.append(_brief([_leg(-1.5, -0.2)]))
    sb.obs.append(_obs(landed_min_ago=12, stamp_min_ago=180))
    counts = LA.sweep_stale_live_activities(now_utc=FROZEN_UTC)
    assert counts['landed'] == 0 and counts['kept'] == 1
    assert not apns['client'].sent


def test_prognose_status_beendet_nichts(client, sb, auth, apns, frozen):
    """„Erwartet"/„Anflug" ist eine Vorhersage — keine Landung."""
    _register(client)
    _age_row(sb, 0)
    sb.briefings.append(_brief([_leg(-1.5, -0.2)]))
    for status in ('Erwartet', 'Anflug', 'Estimated', 'Verspätet', None):
        sb.obs[:] = [_obs(landed_min_ago=12, status=status)]
        assert LA.sweep_stale_live_activities(
            now_utc=FROZEN_UTC)['landed'] == 0
    assert not apns['client'].sent


def test_landung_in_der_zukunft_ist_keine_landung(client, sb, auth, apns,
                                                  frozen):
    _register(client)
    _age_row(sb, 0)
    sb.briefings.append(_brief([_leg(-1.5, 0.5)]))
    sb.obs.append(_obs(landed_min_ago=-20))               # „gelandet" in 20 min
    assert LA.sweep_stale_live_activities(now_utc=FROZEN_UTC)['landed'] == 0
    assert not apns['client'].sent


def test_turnaround_verliert_seine_karte_nicht(client, sb, auth, apns, frozen):
    """Ein Umlauf hat mehrere Legs. Landet Leg 1, laeuft die Karte weiter —
    beendet wird erst, wenn kein Abflug mehr aussteht."""
    _register(client)
    _age_row(sb, 0)
    sb.briefings.append(_brief([_leg(-1.5, -0.2, flight='LH1457'),
                                _leg(1.0, 2.5, flight='LH1458',
                                     frm='FRA', to='MUC')]))
    sb.obs.append(_obs(landed_min_ago=12))
    counts = LA.sweep_stale_live_activities(now_utc=FROZEN_UTC)
    assert counts['landed'] == 0 and counts['kept'] == 1
    assert not apns['client'].sent


def test_lh455_vortags_instanz_beendet_die_karte_nicht(client, sb, auth, apns,
                                                       frozen):
    """Vorfall 02.08. (Check-in-Pushes) auf der Live-Activity-Achse
    gegengeprueft: LH455 SFO-FRA ist ein Rot-Augen-Flug, die HEUTE in FRA
    gelandete Maschine gehoert zur Instanz von GESTERN. Die Board-Zeile traegt
    das ANKUNFTS-Lokaldatum und damit dieselbe Flugnummer am selben Tag — sie
    darf die Karte des heute abend startenden Legs nicht beenden.

    Zwei unabhaengige Riegel greifen: `_closing_sector` gibt None, solange ein
    Abflug aussteht, und `confirmed_arrival` verwirft jede Ankunft VOR dem
    eigenen Abflug."""
    _register(client)
    _age_row(sb, 0)
    # Juliens Leg startet in 3 h (SFO) und landet morgen frueh in FRA.
    leg = _leg(3, 14, flight='LH455', frm='SFO', to='FRA')
    sb.briefings.append(_brief([leg]))
    sb.obs.append(_obs(landed_min_ago=60, flight='LH455'))
    counts = LA.sweep_stale_live_activities(now_utc=FROZEN_UTC)
    assert counts['landed'] == 0 and not apns['client'].sent
    # Auch die reine Funktion sagt Nein — unabhaengig vom Turnaround-Riegel.
    assert LA.confirmed_arrival(sb.obs, leg, FROZEN_UTC) is None


def test_landung_eines_fremden_legs_beendet_nichts(client, sb, auth, apns,
                                                   frozen):
    """Der Board-Read ist gebuendelt — die Landung einer ANDEREN Flugnummer
    darf die Karte nicht schliessen."""
    _register(client)
    _age_row(sb, 0)
    sb.briefings.append(_brief([_leg(-1.5, -0.2, flight='LH1457')]))
    sb.obs.append(_obs(landed_min_ago=12, flight='LH999'))
    assert LA.sweep_stale_live_activities(now_utc=FROZEN_UTC)['landed'] == 0


def test_sektor_ohne_flugnummer_beendet_nichts(client, sb, auth, apns, frozen):
    """Ohne Flugnummer gibt es keinen Beleg — und ohne Beleg kein Ende
    (dieselbe Regel wie beim Roster-Read)."""
    _register(client)
    _age_row(sb, 0)
    sb.briefings.append(_brief([_sector(-2, -0.2, FROZEN_UTC)]))
    sb.obs.append(_obs(landed_min_ago=12, to='ORD'))
    assert LA.sweep_stale_live_activities(now_utc=FROZEN_UTC)['landed'] == 0


def test_landungs_zweig_macht_keinen_lh_call(client, sb, auth, apns, frozen,
                                             monkeypatch):
    """Der Beleg kommt aus GESPEICHERTEN Board-Zeilen. Ein LH- oder FR24-Call
    pro Karte waere genau die Kosten-Falle, aus der dieses Modul kommt."""
    from blueprints import lh_mqtt as _mqtt
    monkeypatch.setattr(_mqtt, 'lh_flight_facts',
                        lambda *a, **k: pytest.fail('Sweep ruft LH'))
    _register(client)
    _age_row(sb, 0)
    sb.briefings.append(_brief([_leg(-1.5, -0.2)]))
    sb.obs.append(_obs(landed_min_ago=12))
    assert LA.sweep_stale_live_activities(now_utc=FROZEN_UTC)['landed'] == 1


def test_fehlende_stempel_spalte_bricht_den_sweep_nicht(client, sb, auth, apns,
                                                        frozen, monkeypatch):
    """fcm_token-Lehre: laeuft die Migration irgendwo noch nicht, darf der
    Sweep nicht ausfallen — dann traegt der Altbestands-Stempel
    (`updated_at`)."""
    real_table = sb.table

    def _table(name):
        t = real_table(name)
        if name != 'airport_delay_obs':
            return t
        orig_select = t.select

        def _select(cols):
            if 'esti_changed_at' in str(cols):
                raise RuntimeError('column esti_changed_at does not exist')
            return orig_select(cols)

        t.select = _select
        return t

    monkeypatch.setattr(sb, 'table', _table)
    _register(client)
    _age_row(sb, 0)
    sb.briefings.append(_brief([_leg(-1.5, -0.2)]))
    row = _obs(landed_min_ago=12)
    row.pop('esti_changed_at')
    sb.obs.append(row)
    assert LA.sweep_stale_live_activities(now_utc=FROZEN_UTC)['landed'] == 1


def test_beendete_karte_wird_nicht_zweimal_beendet(client, sb, auth, apns,
                                                   frozen):
    _register(client)
    _age_row(sb, 0)
    sb.briefings.append(_brief([_leg(-1.5, -0.2)]))
    sb.obs.append(_obs(landed_min_ago=12))
    LA.sweep_stale_live_activities(now_utc=FROZEN_UTC)
    n = len(apns['client'].sent)
    assert LA.sweep_stale_live_activities(now_utc=FROZEN_UTC)['checked'] == 0
    assert len(apns['client'].sent) == n


def test_fanout_loescht_die_bestaetigung_nicht_mehr(client, sb, auth, apns):
    """Ein Push ERSETZT das ContentState vollstaendig. Bis heute schickte der
    Fanout `depConfirmed`/`arrConfirmed` nie mit — jedes Event drehte die
    Karte damit auf „nicht bestaetigt" zurueck und nahm ihr das Recht,
    offline fortzuschalten."""
    _register(client)
    LA.push_for_affected([(TOKEN, _sector(-3, 3))], 'departed', 'LH1457',
                         '2026-08-02')
    state = apns['client'].sent[-1]['payload']['aps']['content-state']
    assert state['depConfirmed'] is True
    assert 'arrConfirmed' not in state          # noch nicht gelandet
    LA.push_for_affected([(TOKEN, _sector(-3, -0.1))], 'arrived', 'LH1457',
                         '2026-08-02')
    state = apns['client'].sent[-1]['payload']['aps']['content-state']
    assert state['arrConfirmed'] is True and state['depConfirmed'] is True


def test_ohne_abflug_beleg_bleibt_die_bestaetigung_weg(client, sb, auth, apns):
    """Eine ETA-Korrektur am Gate belegt keinen Abflug — dann darf auch kein
    `depConfirmed` mitfahren (sonst waere es genau der Fake-Abflug)."""
    _register(client)
    LA.push_for_affected([(TOKEN, _sector(-1, 3))], 'est_arr', 'LH1457',
                         '2026-08-02', facts={'dep_status': 'Boarding'})
    state = apns['client'].sent[-1]['payload']['aps']['content-state']
    assert 'depConfirmed' not in state and 'arrConfirmed' not in state


def test_mqtt_arrived_beendet_die_karte_NICHT(client, sb, auth, apns):
    """Das Ende bleibt beim Sweep, der den ganzen Roster sieht. Der Fanout
    kennt pro Ereignis nur EIN Leg und wuerde einen Umlauf mitten im
    Turnaround abschneiden."""
    _register(client)
    LA.push_for_affected([(TOKEN, _sector(-3, -0.1))], 'arrived', 'LH1457',
                         '2026-08-02')
    aps = apns['client'].sent[-1]['payload']['aps']
    assert aps['event'] == 'update' and 'dismissal-date' not in aps


# ════════════════════════════════════════════════════════════════════════════
# Owner-Nachschaerfung 2026-07-31 — Ultra-Langstrecke + kein Fake-Abflug
# ════════════════════════════════════════════════════════════════════════════
# Woertlich: „ne Langstrecke die 12h geht kann sie mit guten Werten schon
# zeigen auch ohne Internet — nur nicht Fake-Abflug wenn keins."
#
# Zwei getrennte Forderungen:
#   1. Der stale-date-Deckel haengt an der ANKUNFT, nicht an einem festen
#      Fenster ab jetzt — sonst graut eine 14-h-Strecke mitten im Flug aus.
#   2. `inFlight` NUR mit echtem Abflug-Beleg. Eine verstrichene Planzeit ist
#      keiner.

def _seed_row(sb_fake):
    """Aktive update-Zeile ohne den HTTP-Registrierungsweg."""
    sb_fake._upsert({'p_user_token': TOKEN, 'p_kind': 'update',
                     'p_activity_id': ACT_ID, 'p_la_token': LA_TOKEN_A,
                     'p_bundle_id': BUNDLE, 'p_environment': 'prod',
                     'p_device_id': None, 'p_platform': 'ios'})


# ── 1) Deckel an der Ankunft ────────────────────────────────────────────────

def test_ultralangstrecke_graut_nicht_mitten_im_flug_aus():
    """LH715 HND-MUC, 14h20 Block — stand real im R1-Beweis-Set dieser Runde.
    Mit dem alten 12-h-Deckel waere die Marke 2h20 VOR der Landung gefallen."""
    now = 1_800_000_000.0
    block_s = int(14.333 * 3600)
    stale = LA._stale_after_s(now + block_s, now_ts=now)
    assert stale > block_s, 'Karte veraltet VOR ihrer eigenen Ankunft'
    assert stale == block_s + LA._LA_STALE_GRACE_S


@pytest.mark.parametrize('block_h', [12.5, 13, 14.333, 15, 17, 19])
def test_kein_linienflug_laeuft_in_den_not_deckel(block_h):
    """Der Deckel ist gegen Datenmuell da, nicht gegen Fluege. Keine reale
    Blockzeit darf ihn beruehren."""
    now = 1_800_000_000.0
    block_s = int(block_h * 3600)
    assert LA._stale_after_s(now + block_s, now_ts=now) == \
        block_s + LA._LA_STALE_GRACE_S


def test_not_deckel_greift_erst_bei_datenmuell():
    """Ankunft im naechsten Jahr (verrutschtes Datum) wird geklemmt."""
    now = 1_800_000_000.0
    assert LA._stale_after_s(now + 365 * 24 * 3600, now_ts=now) \
        == LA._LA_STALE_MAX_S
    assert LA._LA_STALE_MAX_S == LA._LA_STALE_MAX_BLOCK_H * 3600 \
        + LA._LA_STALE_GRACE_S


def test_boden_bleibt_fuer_den_luegen_fall():
    """Die Gegenrichtung darf sich NICHT gelockert haben: eine Ankunft, die
    laengst vorbei ist (verlorenes arrived), graut weiter schnell aus."""
    now = 1_800_000_000.0
    assert LA._stale_after_s(now - 6 * 3600, now_ts=now) == LA._LA_STALE_MIN_S
    assert LA._LA_STALE_MIN_S == 15 * 60


# ── 2) Kein Fake-Abflug ─────────────────────────────────────────────────────

def test_departure_is_proven_nur_mit_echtem_signal():
    assert LA._departure_is_proven('departed', None) is True
    assert LA._departure_is_proven('arrived', None) is True
    assert LA._departure_is_proven('diverted', None) is True
    # Board-Actual (LH FlightStatus.Definition), englisch wie deutsch
    for s in ('Flight Departed', 'Abgeflogen', 'Landed', 'Gelandet',
              'In Flight', 'Diverted'):
        assert LA._departure_is_proven('est_arr', {'dep_status': s}) is True, s
    # VOR-Abflug-Zustaende belegen nichts — auch wenn die Planzeit durch ist
    for s in ('Scheduled', 'On Time', 'Delayed', 'Boarding', 'Gate Closed',
              'Cancelled', '', None):
        assert LA._departure_is_proven('est_arr', {'dep_status': s}) is False, s
    assert LA._departure_is_proven('est_arr', {}) is False
    assert LA._departure_is_proven('est_arr', None) is False


def test_est_arr_am_gate_behauptet_keinen_flugzustand(sb, auth, apns):
    """DER FAKE-ABFLUG-FALL: LH schickt eine ETA-Korrektur, waehrend der
    Flieger noch am Gate steht. Vorher sprang die Karte damit auf `inFlight` —
    die „Flug-Animation ohne Pushback"."""
    _seed_row(sb)
    sector = {'flight': 'LH454', 'from': 'FRA', 'to': 'SFO',
              'dep_iso': '2026-07-28T08:25:00Z',
              'arr_iso': '2026-07-28T19:40:00Z'}
    facts = {'est_dep': '2026-07-28T09:40:00Z',
             'est_arr': '2026-07-28T20:58:00Z',
             'dep_status': 'Delayed'}
    LA.push_for_affected([(TOKEN, sector)], 'est_arr', 'LH454', '2026-07-28',
                         facts=facts)
    cs = apns['client'].sent[0]['payload']['aps']['content-state']
    assert cs['phase'] != 'inFlight'
    assert cs['phase'] == 'briefing'
    # Die Karte zeigt weiter auf den ABFLUG …
    assert cs['mainTime'] == cs['estDep'] == cs['countdownTarget']
    # … die frische Ankunftsschaetzung wird trotzdem mitgeliefert (die
    # 28.07.-Reparatur bleibt intakt, nur die Behauptung faellt weg).
    assert cs['estArr'] == LA._to_apple_date(facts['est_arr'])


def test_verstrichene_planzeit_ohne_ereignis_macht_keinen_inflight(sb, auth,
                                                                   apns):
    """Owner-Regel woertlich: eine verstrichene PLAN-Abflugzeit ist KEIN Beleg
    — die Maschine kann am Gate stehen."""
    _seed_row(sb)
    sector = {'flight': 'LH454', 'from': 'FRA', 'to': 'SFO',
              'dep_iso': '2020-01-01T08:25:00Z',      # Jahre her
              'arr_iso': '2020-01-01T19:40:00Z'}
    LA.push_for_affected([(TOKEN, sector)], 'est_arr', 'LH454', '2020-01-01',
                         facts={'est_dep': '2020-01-01T08:40:00Z',
                                'est_arr': '2020-01-01T19:58:00Z'})
    cs = apns['client'].sent[0]['payload']['aps']['content-state']
    assert cs['phase'] != 'inFlight'


def test_keine_phase_haengt_an_der_uhr():
    """VERIFIZIERTER NICHT-BEFUND, festgenagelt: die Phase kommt
    ausschliesslich aus der Ereignis-Tabelle. Kaeme je eine Uhr-Ableitung
    dazu, muss dieser Test brechen und jemand muss sie gegen die Owner-Regel
    pruefen."""
    assert set(LA._MQTT_PHASE_KICKER) == {
        'est_dep', 'cancelled', 'diverted', 'departed', 'est_arr', 'arrived'}
    # Jede Art, die `inFlight` behauptet, ist ENTWEDER selbst ein Abflug-Beleg
    # ODER muss in push_for_affected gegated sein. Kommt eine neue inFlight-Art
    # dazu, faellt sie hier auf — und jemand muss entscheiden, welches von
    # beidem gilt. `_GUARDED` ist die vollstaendige Liste der gegateten Arten.
    _GUARDED = {'est_arr'}
    ungedeckt = {k for k, (p, _) in LA._MQTT_PHASE_KICKER.items()
                 if p == 'inFlight'
                 and not LA._departure_is_proven(k, None)
                 and k not in _GUARDED}
    assert not ungedeckt, f'inFlight ohne Beleg und ohne Gate: {ungedeckt}'
    # …und das Gate muss auch wirklich greifen (nicht nur gelistet sein).
    for kind in _GUARDED:
        assert LA._departure_is_proven(kind, {'dep_status': 'Scheduled'}) \
            is False


def test_departed_ereignis_bleibt_der_direkte_weg_in_den_flugzustand(sb, auth,
                                                                     apns):
    """Gegenprobe: das ECHTE Abflug-Ereignis braucht keinen dep_status."""
    _seed_row(sb)
    sector = {'flight': 'LH454', 'from': 'FRA', 'to': 'SFO',
              'dep_iso': '2026-07-28T08:25:00Z',
              'arr_iso': '2026-07-28T19:40:00Z'}
    LA.push_for_affected([(TOKEN, sector)], 'departed', 'LH454', '2026-07-28',
                         facts={'est_arr': '2026-07-28T19:55:00Z'})
    cs = apns['client'].sent[0]['payload']['aps']['content-state']
    assert cs['phase'] == 'inFlight' and cs['kicker'] == 'GESTARTET'
    assert cs['mainTime'] == cs['estArr']


# ── Feld-Erhaltung gegen Teil-Absender (Vorfall Nr. 4, 13.08.2026) ──────────

def test_teilabsender_loescht_die_kette_nicht_mehr(sb, auth, apns):
    """DER BOARDING-FALL: Der Client hat eine volle Karte gesendet (Kette mit
    Pickup/Boarding, Anzeige-Zone, Städten). Danach schickt der MQTT-Fanout
    sein handgebautes Teil-Dict (est_dep) — APNs ersetzt vollständig. Vorher
    verschwand damit die ganze Kette vom Sperrbildschirm; jetzt erhält der
    Server die phasen-unabhängigen Felder aus dem zuletzt gesendeten Zustand."""
    _seed_row(sb)
    voll = {'stateVersion': 2, 'phase': 'briefing', 'kicker': 'PICKUP',
            'generatedAt': '2026-08-12T00:30:00Z',
            'mainTime': '2026-08-12T09:50:00+09:00',
            'countdownTarget': '2026-08-12T12:20:00+09:00',
            'flightNo': 'LH713', 'fromIATA': 'ICN', 'toIATA': 'FRA',
            'schedDep': '2026-08-12T12:20:00+09:00',
            'displayTZIdentifier': 'Asia/Seoul',
            'fromCity': 'Seoul', 'toCity': 'Frankfurt',
            'chain': [{'label': 'Pickup',
                       'time': '2026-08-12T09:50:00+09:00', 'state': 'current'},
                      {'label': 'Boarding',
                       'time': '2026-08-12T11:40:00+09:00', 'state': 'upcoming'}]}
    r1 = LA.push_live_activity(TOKEN, voll)
    assert r1['sent'] == 1

    teil = {'stateVersion': 2, 'phase': 'briefing', 'kicker': 'ABFLUG',
            'generatedAt': '2026-08-12T03:00:00Z',
            'mainTime': '2026-08-12T12:35:00+09:00',
            'countdownTarget': '2026-08-12T12:35:00+09:00',
            'flightNo': 'LH713', 'fromIATA': 'ICN', 'toIATA': 'FRA',
            'schedDep': '2026-08-12T12:20:00+09:00',
            'estDep': '2026-08-12T12:35:00+09:00'}
    r2 = LA.push_live_activity(TOKEN, teil)
    assert r2['sent'] == 1
    cs = apns['client'].sent[-1]['payload']['aps']['content-state']
    # Der Teil-Absender gewinnt, wo er etwas sagt …
    assert cs['kicker'] == 'ABFLUG'
    # … aber die Kette samt Boarding, Zone und Städte bleiben stehen.
    assert [s['label'] for s in cs['chain']] == ['Pickup', 'Boarding']
    assert cs['displayTZIdentifier'] == 'Asia/Seoul'
    assert cs['fromCity'] == 'Seoul' and cs['toCity'] == 'Frankfurt'


def test_teilabsender_ueberschreibt_erhaltene_felder_wenn_er_sie_setzt(sb, auth, apns):
    """Erhaltung heisst NICHT einfrieren: setzt der neue Absender das Feld
    selbst, gewinnt er."""
    _seed_row(sb)
    basis = {'stateVersion': 2, 'generatedAt': '2026-08-12T00:30:00Z',
             'mainTime': '2026-08-12T09:50:00+09:00'}
    LA.push_live_activity(TOKEN, dict(basis, phase='briefing', kicker='PICKUP',
                                      displayTZIdentifier='Asia/Seoul'))
    LA.push_live_activity(TOKEN, dict(basis, phase='briefing', kicker='ABFLUG',
                                      displayTZIdentifier='Europe/Berlin'))
    cs = apns['client'].sent[-1]['payload']['aps']['content-state']
    assert cs['displayTZIdentifier'] == 'Europe/Berlin'


def test_mqtt_teilabsender_behaelt_gecachte_ankunftszeit(sb, auth, apns):
    """Eine spaetere Abflugmeldung ohne ETA darf die zuvor empfangene neue
    Ankunft nicht wieder auf die Roster-Planzeit setzen."""
    _seed_row(sb)
    sector = {'flight': 'LH454', 'from': 'FRA', 'to': 'SFO',
              'dep_iso': '2026-08-17T06:25:00Z',
              'arr_iso': '2026-08-17T17:40:00Z'}
    first_facts = {'est_dep': '2026-08-17T06:31:00Z',
                   'sched_arr': '2026-08-17T17:40:00Z',
                   'est_arr': '2026-08-17T18:02:00Z',
                   'dep_status': 'Flight Departed'}
    assert LA.push_for_affected([(TOKEN, sector)], 'est_arr', 'LH454',
                                '2026-08-17', facts=first_facts) == 1
    cached_eta = apns['client'].sent[-1]['payload']['aps'][
        'content-state']['estArr']

    # Das naechste Ereignis weiss nur etwas ueber den Abflug. `arr_iso` ist
    # weiterhin die Planzeit und darf nicht als neue ETA missverstanden werden.
    assert LA.push_for_affected([(TOKEN, sector)], 'departed', 'LH454',
                                '2026-08-17',
                                facts={'est_dep': '2026-08-17T06:33:00Z'}) == 1
    cs = apns['client'].sent[-1]['payload']['aps']['content-state']
    assert cs['estArr'] == cached_eta
    assert cs['schedArr'] != cs['estArr']
    assert cs['countdownTarget'] == cached_eta


def test_neue_eta_gewinnt_gegen_gecachte_eta(sb, auth, apns):
    _seed_row(sb)
    sector = {'flight': 'LH454', 'from': 'FRA', 'to': 'SFO',
              'dep_iso': '2026-08-17T06:25:00Z',
              'arr_iso': '2026-08-17T17:40:00Z'}
    for eta in ('2026-08-17T18:02:00Z', '2026-08-17T18:14:00Z'):
        LA.push_for_affected([(TOKEN, sector)], 'est_arr', 'LH454',
                             '2026-08-17',
                             facts={'est_arr': eta,
                                    'dep_status': 'Flight Departed'})
    cs = apns['client'].sent[-1]['payload']['aps']['content-state']
    assert cs['estArr'] == LA._to_apple_date('2026-08-17T18:14:00Z')
    assert cs['countdownTarget'] == cs['estArr']


def test_flug_cache_wandert_nicht_in_naechstes_leg():
    old = _state(flightNo='LH454', fromIATA='FRA', toIATA='SFO',
                 schedDep=_stored_date('2026-08-17T06:25:00Z'),
                 estArr=_stored_date('2026-08-17T18:02:00Z'))
    new = _state(flightNo='LH455', fromIATA='SFO', toIATA='FRA',
                 schedDep=_stored_date('2026-08-18T10:00:00Z'))
    merged = LA._merge_cached_state(new, old)
    assert 'estArr' not in merged


def test_flug_cache_erkennt_flugnummer_mit_oder_ohne_leerzeichen():
    old = _state(flightNo='LH 454', fromIATA='FRA', toIATA='SFO',
                 schedDep=_stored_date('2026-08-17T06:25:00Z'),
                 estArr=_stored_date('2026-08-17T18:02:00Z'))
    new = _state(flightNo='LH454', fromIATA='FRA', toIATA='SFO',
                 schedDep=_stored_date('2026-08-17T06:25:00Z'))
    assert LA._merge_cached_state(new, old)['estArr'] == old['estArr']


def test_explizites_null_darf_auch_eta_loeschen():
    old = _state(flightNo='LH454', fromIATA='FRA', toIATA='SFO',
                 schedDep=_stored_date('2026-08-17T06:25:00Z'),
                 estArr=_stored_date('2026-08-17T18:02:00Z'))
    incoming = _state(flightNo='LH454', fromIATA='FRA', toIATA='SFO',
                      schedDep=_stored_date('2026-08-17T06:25:00Z'))
    merged = LA._merge_cached_state(incoming, old, cleared={'estArr'})
    assert 'estArr' not in merged


def test_end_event_erhaelt_nichts(sb, auth, apns):
    """Ein `end` beendet — da wird nichts aus alten Zustaenden angereichert."""
    _seed_row(sb)
    basis = {'stateVersion': 2, 'generatedAt': '2026-08-12T00:30:00Z',
             'mainTime': '2026-08-12T09:50:00+09:00'}
    LA.push_live_activity(TOKEN, dict(basis, phase='briefing', kicker='PICKUP',
                                      chain=[{'label': 'Pickup',
                                              'time': '2026-08-12T09:50:00+09:00',
                                              'state': 'current'}]))
    LA.push_live_activity(TOKEN, dict(basis, phase='turnaround',
                                      kicker='GELANDET'), event='end')
    cs = apns['client'].sent[-1]['payload']['aps']['content-state']
    assert 'chain' not in cs


def test_explizites_null_loescht_das_feld_endgueltig(sb, auth, apns):
    """GRABSTEIN (F2b): ohne ihn wäre die Erhaltung eine Einbahnstrasse — ein
    einmal gesendetes Feld liesse sich NIE wieder löschen, die Kette des
    letzten Dienstes klebte auf dem Sperrbildschirm. `null` heisst „weg", und
    zwar auch beim NÄCHSTEN Teil-Absender."""
    _seed_row(sb)
    basis = {'stateVersion': 2, 'generatedAt': '2026-08-12T00:30:00Z',
             'mainTime': '2026-08-12T09:50:00+09:00', 'phase': 'briefing'}
    kette = [{'label': 'Pickup', 'time': '2026-08-12T09:50:00+09:00',
              'state': 'current'}]
    LA.push_live_activity(TOKEN, dict(basis, kicker='PICKUP', chain=kette,
                                      displayTZIdentifier='Asia/Seoul'))
    # Absender leert die Kette BEWUSST (JSON-null), Zone sagt er nicht.
    LA.push_live_activity(TOKEN, dict(basis, kicker='ABFLUG', chain=None))
    cs = apns['client'].sent[-1]['payload']['aps']['content-state']
    assert 'chain' not in cs, 'Grabstein missachtet — Kette wieder eingesetzt'
    assert cs['displayTZIdentifier'] == 'Asia/Seoul', 'zu viel gelöscht'

    # Und der Grabstein hält: der nächste Absender ohne chain bekommt sie
    # nicht aus dem gespeicherten Zustand zurück.
    LA.push_live_activity(TOKEN, dict(basis, kicker='BOARDING'))
    assert 'chain' not in apns['client'].sent[-1]['payload']['aps'][
        'content-state']


def test_normalize_meldet_die_geleerten_keys():
    """Wire-Kompatibilität: die alte 2-Tupel-Signatur bleibt, `cleared` gibt es
    nur auf Anfrage."""
    roh = _state(chain=None, fromCity=None, toCity='Frankfurt')
    assert len(LA._normalize_content_state(roh)) == 2
    state, problems, cleared = LA._normalize_content_state(roh,
                                                           with_cleared=True)
    assert cleared == {'chain', 'fromCity'}
    assert 'chain' not in state and state['toCity'] == 'Frankfurt'
    assert not LA._fatal_problems(problems)


# ── Registrierung lädt den App-Zustand hoch (F2a, 13.08.2026) ───────────────

def test_register_nimmt_content_state_als_erhaltungs_grundlage(client, sb,
                                                               auth, apns):
    """OHNE diesen Weg war die Feld-Erhaltung in Produktion wirkungslos: der
    Server kann nur erhalten, was er selbst schon einmal GESENDET hat — die
    Karte baut aber die App. Der erste Teil-Absender (MQTT-Fanout) löschte
    deshalb weiterhin die Dienst-Kette."""
    kette = [{'label': 'Pickup', 'time': '2026-08-12T09:50:00+09:00',
              'state': 'current'}]
    body = {'token': TOKEN, 'la_token': LA_TOKEN_A, 'kind': 'update',
            'bundle_id': BUNDLE, 'apns_env': 'prod', 'platform': 'ios',
            'activity_id': ACT_ID,
            'content_state': _state(chain=kette, flightNo='LH713',
                                    fromIATA='ICN', toIATA='FRA',
                                    schedDep='2026-08-12T03:20:00Z',
                                    displayTZIdentifier='Asia/Seoul')}
    r = client.post('/api/push/register-live-activity', json=body,
                    headers=_auth_hdr())
    assert r.status_code == 200 and r.get_json()['content_state_stored'] is True
    # Gespeichert wird der NORMALISIERTE Zustand (Datum = Apple-Sekunden) —
    # exakt das, was auch ein eigener Push gesendet hätte.
    gespeichert = sb.row(kind='update')['last_content_state']
    assert [s['label'] for s in gespeichert['chain']] == ['Pickup']
    assert gespeichert['chain'][0]['time'] == LA._to_apple_date(
        '2026-08-12T09:50:00+09:00')

    # Der Fanout schickt sein handgebautes Teil-Dict — die Kette überlebt.
    res = LA.push_live_activity(
        TOKEN, _state(kicker='ABFLUG', flightNo='LH713', fromIATA='ICN',
                      toIATA='FRA', schedDep='2026-08-12T03:20:00Z'))
    assert res['sent'] == 1
    cs = apns['client'].sent[-1]['payload']['aps']['content-state']
    assert [s['label'] for s in cs['chain']] == ['Pickup']
    assert cs['displayTZIdentifier'] == 'Asia/Seoul'


def test_register_mit_kaputtem_content_state_registriert_trotzdem(client, sb,
                                                                  auth):
    """Der TOKEN ist das Wichtige. Ein Typ-Fehler im mitgelieferten Zustand
    kostet nur die Erhaltung, nie die Registrierung (sonst wäre die Karte gar
    nicht mehr erreichbar)."""
    body = {'token': TOKEN, 'la_token': LA_TOKEN_A, 'kind': 'update',
            'apns_env': 'prod', 'activity_id': ACT_ID,
            'content_state': _state(progress='schnell')}
    r = client.post('/api/push/register-live-activity', json=body,
                    headers=_auth_hdr())
    assert r.status_code == 200
    assert r.get_json() == {'ok': True, 'kind': 'update',
                            'activity_id': ACT_ID, 'stored': True,
                            'content_state_stored': False}
    assert not sb.row(kind='update').get('last_content_state')


def test_end_wirft_den_zustand_weg(client, sb, auth, apns):
    """Registry-ZEILEN werden über Activities hinweg wiederverwendet. Ohne
    Reset erbte die NÄCHSTE Karte die Kette der alten (Ketten-Geist) — hier
    in beiden Speichern: DB-Spalte (Migration 20260813b) und In-Process."""
    _seed_row(sb)
    kette = [{'label': 'Pickup', 'time': '2026-08-12T09:50:00+09:00',
              'state': 'current'}]
    LA.push_live_activity(TOKEN, _state(chain=kette))
    assert sb.row(kind='update')['last_content_state']
    r = client.post('/api/live-activity/end',
                    json={'token': TOKEN, 'activity_id': ACT_ID},
                    headers=_auth_hdr())
    assert r.status_code == 200 and r.get_json()['ended'] == 1
    assert sb.row(kind='update')['last_content_state'] is None
    assert not [s for s in LA._LAST_SENT.values() if s.get('state')]


# ── Fehlende Migration 20260813 darf keine Pushes verschlucken (F1) ─────────

def test_fehlende_spalte_stoppt_die_pushes_nicht(sb, auth, apns, caplog):
    """DEPLOY-GEFAHR (Klasse fcm_token 01.08.): läuft der Code gegen eine DB
    ohne `last_content_state`, wirft der Registry-Read 42703. Vorher lieferte
    er dann [] — JEDER Push wäre still übersprungen worden, obwohl Tokens da
    sind. Jetzt: EIN Retry mit dem alten Spaltensatz, lauter Warn-Log, Feature
    läuft (nur die Erhaltung kommt bis zur Migration aus dem Prozess)."""
    _seed_row(sb)
    sb.no_last_state_column = True
    LA._ROW_SELECT_FALLBACK['until'] = 0.0
    with caplog.at_level('WARNING'):
        res = LA.push_live_activity(TOKEN, _state())
    assert res['sent'] == 1, res
    assert len(apns['client'].sent) == 1
    assert any('20260813_live_activity_last_state' in r.message % r.args
               for r in caplog.records if r.levelname == 'WARNING')
    # Zweiter Push geht ohne den Fehlversuch direkt auf den Alt-Satz.
    assert LA._ROW_SELECT_FALLBACK['until'] > 0
    assert LA.push_live_activity(TOKEN, _state(kicker='ABFLUG'))['sent'] == 1


def test_echter_store_ausfall_bleibt_ein_ausfall(sb, auth, apns, monkeypatch):
    """Gegenprobe: der Retry darf einen echten Ausfall nicht in ein „alles gut"
    umdeuten — ohne Zeilen wird nichts gesendet."""
    _seed_row(sb)
    LA._ROW_SELECT_FALLBACK['until'] = 0.0

    def boom(_name):
        raise RuntimeError('connection refused')

    monkeypatch.setattr(sb, 'table', boom)
    res = LA.push_live_activity(TOKEN, _state())
    assert res == {'ok': True, 'sent': 0, 'skipped': 'no_target',
                   'event': 'update'}
    assert apns['client'].sent == []


# ── Build 347: Start/Refresh ohne App-Oeffnen ───────────────────────────────

def _stored_date(iso):
    """So liegt ein Date in `last_content_state` (Apple-Referenzsekunden)."""
    return LA._to_apple_date(iso)


def test_gespeicherter_pickup_startet_im_selben_fenster_wie_ios():
    state = _state(
        phase='preDuty', kicker='AUS DEM HAUS',
        mainTime=_stored_date('2026-08-17T05:07:00Z'),
        generatedAt=_stored_date('2026-08-16T18:00:00Z'),
        chain=[{'label': 'Aus dem Haus',
                'time': _stored_date('2026-08-17T05:07:00Z'),
                'state': 'current'}])
    before = datetime.fromisoformat('2026-08-17T04:06:59+00:00').timestamp()
    inside = datetime.fromisoformat('2026-08-17T04:07:00+00:00').timestamp()
    assert LA._stored_state_wants_start(state, before) is False
    assert LA._stored_state_wants_start(state, inside) is True


def test_gespeicherte_apple_dates_werden_nicht_doppelt_umgerechnet():
    iso = '2026-08-17T05:07:00Z'
    stored = _stored_date(iso)
    expected = datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp()
    assert LA._stored_date_to_unix(stored) == expected


def test_meilenstein_refresh_feuert_einmal_pro_passierter_marke():
    state = _state(
        generatedAt=_stored_date('2026-08-17T04:00:00Z'),
        mainTime=_stored_date('2026-08-17T08:25:00Z'),
        chain=[
            {'label': 'Aus dem Haus',
             'time': _stored_date('2026-08-17T05:07:00Z'),
             'state': 'current'},
            {'label': 'Briefing',
             'time': _stored_date('2026-08-17T06:35:00Z'),
             'state': 'upcoming'},
        ])
    now = datetime.fromisoformat('2026-08-17T05:08:00+00:00').timestamp()
    first = LA._milestone_due(state, 0, now)
    assert first == datetime.fromisoformat(
        '2026-08-17T05:07:00+00:00').timestamp()
    # Ein erfolgreicher Push setzt `last_timestamp` hinter die Marke.
    assert LA._milestone_due(state, int(now), now + 60) is None


def test_countdown_stufenwechsel_ist_ein_server_meilenstein():
    target_iso = '2026-08-17T08:35:00Z'
    state = _state(
        generatedAt=_stored_date('2026-08-17T04:00:00Z'),
        mainTime=_stored_date(target_iso),
        countdownTarget=_stored_date(target_iso))
    boundary = datetime.fromisoformat('2026-08-17T07:35:00+00:00').timestamp()
    assert boundary in LA._state_milestones(state)
    assert LA._milestone_due(state, boundary - 1, boundary) == boundary


def test_eta_push_darf_prepickup_nicht_auf_abflug_umschalten():
    previous = _state(
        phase='preDuty', kicker='AUS DEM HAUS',
        mainTime=_stored_date('2026-08-17T05:07:00Z'),
        countdownTarget=_stored_date('2026-08-17T05:07:00Z'))
    incoming = _state(
        phase='briefing', kicker='ABFLUG',
        mainTime=_stored_date('2026-08-17T08:25:00Z'),
        estDep=_stored_date('2026-08-17T08:25:00Z'))
    merged = LA._preserve_prepickup_phase(incoming, previous)
    assert merged['phase'] == 'preDuty'
    assert merged['kicker'] == 'AUS DEM HAUS'
    assert merged['mainTime'] == previous['mainTime']
    assert merged['estDep'] == incoming['estDep']


def test_sweep_startet_gespeicherten_pickup_wirklich_per_apns(
        client, sb, auth, apns, frozen):
    _register(client, kind='start', activity_id=None)
    row = sb.row(kind='start', activity_id=None)
    mark = FROZEN_UTC + timedelta(minutes=30)
    row['last_content_state'] = _state(
        phase='preDuty', kicker='AUS DEM HAUS',
        mainTime=_stored_date(mark.isoformat()),
        countdownTarget=_stored_date(mark.isoformat()),
        generatedAt=_stored_date(
            (FROZEN_UTC - timedelta(hours=1)).isoformat()),
        chain=[{'label': 'Aus dem Haus',
                'time': _stored_date(mark.isoformat()),
                'state': 'current'}])

    counts = LA.sweep_stale_live_activities(now_utc=FROZEN_UTC)

    assert counts['started'] == 1
    aps = apns['client'].sent[-1]['payload']['aps']
    assert aps['event'] == 'start'
    assert aps['content-state']['phase'] == 'preDuty'


def test_sweep_refreshes_laufende_activity_am_meilenstein_nur_einmal(
        client, sb, auth, apns, frozen):
    _register(client)
    row = sb.row()
    mark = FROZEN_UTC - timedelta(minutes=1)
    row['last_timestamp'] = int(
        (FROZEN_UTC - timedelta(hours=2)).timestamp())
    row['last_content_state'] = _state(
        phase='preDuty', kicker='AUS DEM HAUS',
        generatedAt=_stored_date(
            (FROZEN_UTC - timedelta(hours=2)).isoformat()),
        chain=[{'label': 'Aus dem Haus',
                'time': _stored_date(mark.isoformat()),
                'state': 'current'}])

    first = LA.sweep_stale_live_activities(now_utc=FROZEN_UTC)
    sent = len(apns['client'].sent)
    second = LA.sweep_stale_live_activities(
        now_utc=FROZEN_UTC + timedelta(seconds=30))

    assert first['milestone_updates'] == 1
    assert second.get('milestone_updates', 0) == 0
    assert len(apns['client'].sent) == sent
