"""Geräte-Transfer-Sync des SMP-Lernfortschritts (2026-08-02).

Ein einzelner JSON-Blob pro User, damit ein Geräte-Wechsel (altes iPhone ->
neues iPhone) den SMP-Lernfortschritt nicht auf Null zurücksetzt.

Kern der Suite ist NICHT „kommt der Blob an", sondern die DoS-Lehre des
Repos: der Größen-Cap (256 KB) muss VOR dem JSON-Parse greifen (exakt das
app.py:post_telemetry_diagnostics-Muster), Bearer ist Pflicht, und die
Response enthält niemals owner_token (Token=Credential-Regel).

Kein echtes Supabase: `_sb_client` ist ein Seam (Fake-Table), `_authed_token`
wird für die Business-Logik-Tests direkt gemockt (analog test_flight_checkins
`_bearer_ok`); ein Test prüft die reale 401-Kette ohne jeden Mock.
"""
import json

from flask import Flask

from blueprints import smp_user_cards_blueprint as M


TOKEN = "AT-SMP-PROGRESS-OWNER"


def _client():
    app = Flask(__name__)
    app.register_blueprint(M.smp_user_cards_bp)
    return app.test_client()


# ── Fake Supabase: eine Zeile pro owner_token ────────────────────────────
class _Result:
    def __init__(self, data):
        self.data = data


class _FakeProgressTable:
    def __init__(self, store, calls):
        self.store = store
        self.calls = calls
        self._filters = {}
        self._mode = None
        self._pending = None

    def select(self, *_a, **_k):
        self._mode = 'select'
        return self

    def eq(self, key, value):
        self._filters[key] = value
        return self

    def limit(self, *_a, **_k):
        return self

    def upsert(self, row, **kw):
        self._mode = 'upsert'
        self._pending = dict(row)
        self._pending_kw = kw
        return self

    def execute(self):
        if self._mode == 'upsert':
            self.calls.append((dict(self._pending), dict(self._pending_kw)))
            self.store[self._pending['owner_token']] = dict(self._pending)
            return _Result([dict(self._pending)])
        token = self._filters.get('owner_token')
        row = self.store.get(token)
        return _Result([dict(row)] if row else [])


class _FakeSB:
    def __init__(self, store=None):
        self.store = store if store is not None else {}
        self.upsert_calls = []

    def table(self, name):
        assert name == 'ax_smp_progress'
        return _FakeProgressTable(self.store, self.upsert_calls)


def _auth_ok(monkeypatch, token=TOKEN):
    monkeypatch.setattr(M, '_authed_token', lambda: (token, None))


def _wire_sb(monkeypatch, sb):
    monkeypatch.setattr(M, '_sb_client', lambda: (sb, True))


# ══════════════════════════════════════════════════════════════════════════
# Bearer-Pflicht (reale Kette, kein Mock auf _authed_token)
# ══════════════════════════════════════════════════════════════════════════

def test_put_ohne_bearer_ist_401():
    r = _client().put('/api/ax/smp/progress',
                      json={'blob': {'x': 1}, 'updated_at': '2026-08-02T00:00:00Z'})
    assert r.status_code == 401


def test_get_ohne_bearer_ist_401():
    r = _client().get('/api/ax/smp/progress')
    assert r.status_code == 401


# ══════════════════════════════════════════════════════════════════════════
# Größen-Cap VOR dem JSON-Parse (DoS-Klasse)
# ══════════════════════════════════════════════════════════════════════════

def test_put_oversize_ist_413_bevor_geparsed_wird(monkeypatch):
    """Ein Body > 256 KB wird per 413 abgelehnt — auch wenn er gar kein
    valides JSON ist. Käme der Größen-Check NACH dem Parse, würde kaputtes
    JSON hier stattdessen ein 400 (body_must_be_object) auslösen."""
    _auth_ok(monkeypatch)
    sb = _FakeSB()
    _wire_sb(monkeypatch, sb)
    garbage = b'{not valid json' + b'x' * (300 * 1024)
    r = _client().put(
        '/api/ax/smp/progress',
        data=garbage,
        content_type='application/json',
    )
    assert r.status_code == 413
    assert r.get_json()['error'] == 'payload_too_large'
    # Nie bis zum Storage-Layer vorgedrungen.
    assert sb.upsert_calls == []


def test_put_knapp_unter_cap_geht_durch(monkeypatch):
    _auth_ok(monkeypatch)
    sb = _FakeSB()
    _wire_sb(monkeypatch, sb)
    blob = {'progress': 'x' * (200 * 1024)}
    r = _client().put(
        '/api/ax/smp/progress',
        json={'blob': blob, 'updated_at': '2026-08-02T00:00:00Z'},
    )
    assert r.status_code == 200
    assert r.get_json()['ok'] is True


# ══════════════════════════════════════════════════════════════════════════
# Rate-Limit
# ══════════════════════════════════════════════════════════════════════════

def test_put_rate_limited_ist_429(monkeypatch):
    _auth_ok(monkeypatch)
    _wire_sb(monkeypatch, _FakeSB())
    monkeypatch.setattr(M, '_rate_limited', lambda *a, **k: True)
    r = _client().put(
        '/api/ax/smp/progress',
        json={'blob': {'x': 1}, 'updated_at': '2026-08-02T00:00:00Z'},
    )
    assert r.status_code == 429


# ══════════════════════════════════════════════════════════════════════════
# Validierung
# ══════════════════════════════════════════════════════════════════════════

def test_put_ohne_blob_ist_400(monkeypatch):
    _auth_ok(monkeypatch)
    _wire_sb(monkeypatch, _FakeSB())
    r = _client().put('/api/ax/smp/progress',
                      json={'updated_at': '2026-08-02T00:00:00Z'})
    assert r.status_code == 400
    assert r.get_json()['error'] == 'invalid_blob'


def test_put_mit_kaputtem_updated_at_ist_400(monkeypatch):
    _auth_ok(monkeypatch)
    _wire_sb(monkeypatch, _FakeSB())
    r = _client().put('/api/ax/smp/progress',
                      json={'blob': {'x': 1}, 'updated_at': 'not-a-date'})
    assert r.status_code == 400
    assert r.get_json()['error'] == 'invalid_updated_at'


def test_put_body_kein_objekt_ist_400(monkeypatch):
    _auth_ok(monkeypatch)
    _wire_sb(monkeypatch, _FakeSB())
    r = _client().put('/api/ax/smp/progress', json=[1, 2, 3])
    assert r.status_code == 400
    assert r.get_json()['error'] == 'body_must_be_object'


# ══════════════════════════════════════════════════════════════════════════
# Upsert schreibt BEIDE Zeitstempel — Client-Version + Server-Uhr
# ══════════════════════════════════════════════════════════════════════════

def test_put_speichert_client_updated_at_und_eigene_server_uhr(monkeypatch):
    _auth_ok(monkeypatch)
    sb = _FakeSB()
    _wire_sb(monkeypatch, sb)
    r = _client().put(
        '/api/ax/smp/progress',
        json={'blob': {'level': 3}, 'updated_at': '2026-08-01T10:00:00Z'},
    )
    assert r.status_code == 200
    row, kw = sb.upsert_calls[0]
    assert row['owner_token'] == TOKEN
    assert row['blob'] == {'level': 3}
    assert row['updated_at'] == '2026-08-01T10:00:00Z'
    assert row['server_updated_at']  # gesetzt, vom Server, unabhängig vom Client-Wert
    assert row['server_updated_at'] != row['updated_at']
    assert kw.get('on_conflict') == 'owner_token'
    # Niemals ein fremdes Token in der Antwort.
    assert 'owner_token' not in r.get_json()


# ══════════════════════════════════════════════════════════════════════════
# GET — vorhanden / nichts vorhanden / Storage-Ausfall
# ══════════════════════════════════════════════════════════════════════════

def test_get_liefert_gespeicherten_blob(monkeypatch):
    _auth_ok(monkeypatch)
    sb = _FakeSB(store={TOKEN: {
        'owner_token': TOKEN,
        'blob': {'level': 3},
        'updated_at': '2026-08-01T10:00:00Z',
        'server_updated_at': '2026-08-01T10:00:01Z',
    }})
    _wire_sb(monkeypatch, sb)
    r = _client().get('/api/ax/smp/progress')
    assert r.status_code == 200
    payload = r.get_json()
    assert payload == {'ok': True, 'blob': {'level': 3},
                       'updated_at': '2026-08-01T10:00:00Z'}
    assert 'owner_token' not in payload
    assert 'server_updated_at' not in payload


def test_get_ohne_vorherigen_upload_ist_kein_fehler(monkeypatch):
    """Frisches Gerät, noch nie synced — ok:true mit blob:null, kein 404."""
    _auth_ok(monkeypatch)
    _wire_sb(monkeypatch, _FakeSB())
    r = _client().get('/api/ax/smp/progress')
    assert r.status_code == 200
    assert r.get_json() == {'ok': True, 'blob': None, 'updated_at': None}


def test_get_bei_storage_ausfall_ist_503(monkeypatch):
    _auth_ok(monkeypatch)
    monkeypatch.setattr(M, '_sb_client', lambda: (None, False))
    r = _client().get('/api/ax/smp/progress')
    assert r.status_code == 503
    assert r.get_json()['error'] == 'storage_unavailable'


def test_put_bei_storage_ausfall_ist_503(monkeypatch):
    _auth_ok(monkeypatch)
    monkeypatch.setattr(M, '_sb_client', lambda: (None, False))
    r = _client().put('/api/ax/smp/progress',
                      json={'blob': {'x': 1}, 'updated_at': '2026-08-02T00:00:00Z'})
    assert r.status_code == 503


# ══════════════════════════════════════════════════════════════════════════
# Zwei PUTs für denselben Owner überschreiben (Upsert, kein Duplikat)
# ══════════════════════════════════════════════════════════════════════════

def test_zweiter_put_ueberschreibt_und_get_sieht_den_neueren_stand(monkeypatch):
    _auth_ok(monkeypatch)
    sb = _FakeSB()
    _wire_sb(monkeypatch, sb)
    client = _client()
    client.put('/api/ax/smp/progress',
               json={'blob': {'level': 1}, 'updated_at': '2026-08-01T10:00:00Z'})
    client.put('/api/ax/smp/progress',
               json={'blob': {'level': 5}, 'updated_at': '2026-08-02T09:00:00Z'})
    r = client.get('/api/ax/smp/progress')
    payload = r.get_json()
    assert payload['blob'] == {'level': 5}
    assert payload['updated_at'] == '2026-08-02T09:00:00Z'
    assert len(sb.upsert_calls) == 2
