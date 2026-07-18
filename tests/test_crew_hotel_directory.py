"""Per-Airline Crew-Hotel-Verzeichnis (dauerhafter Weg, 2026-07-18).

Sichert die Kern-Garantien:
- Serve ist airline-getrennt: ein SWISS-User bekommt NIE die LH-Liste; ohne
  erkannte Airline → leer (kein falscher Default).
- Suggest schreibt `status='suggested'` mit Airline aus dem Profil — kein direkter
  Live-Effekt (Owner bestätigt).
- Admin-Endpoints (approve/deactivate/pending) sind X-Admin-Token-gegated.
"""
import json
import app
import pytest


class _FakeQuery:
    def __init__(self, sink, table):
        self._sink = sink
        self._table = table
        self._op = None
        self._payload = None
        self._rows = list(sink['data'].get(table, []))

    # -- schreibende Ops merken --
    def insert(self, payload):
        self._op = 'insert'
        self._payload = payload
        self._sink['inserts'].append((self._table, payload))
        return self

    def update(self, payload):
        self._op = 'update'
        self._payload = payload
        self._sink['updates'].append((self._table, payload))
        return self

    def delete(self):
        self._op = 'delete'
        return self

    def select(self, *_a, **_k):
        self._op = 'select'
        return self

    # -- Filter sind für den Test no-ops (Airline-Gate wird separat geprüft) --
    def eq(self, *_a, **_k):
        return self

    def neq(self, *_a, **_k):
        return self

    def is_(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        if self._op == 'insert':
            rows = self._payload if isinstance(self._payload, list) else [self._payload]
            out = [{**r, 'id': f'id-{i}'} for i, r in enumerate(rows)]
            return type('R', (), {'data': out})()
        return type('R', (), {'data': self._rows})()


class _FakeSB:
    def __init__(self, data=None):
        self.sink = {'data': data or {}, 'inserts': [], 'updates': []}

    def table(self, name):
        return _FakeQuery(self.sink, name)


@pytest.fixture
def client():
    app.app.config['TESTING'] = True
    return app.app.test_client()


def _airline(monkeypatch, airline):
    monkeypatch.setattr(app, '_profile_load', lambda t: {'profile': {'airline': airline}})
    monkeypatch.setattr(app, '_ical_briefings_load',
                        lambda t: {'2026-07-01': {'ical_imported_at': '2026-06-01T00:00:00'}})


# ── Serve: airline-getrennt ───────────────────────────────────────────────────

def test_serve_returns_only_own_airline(client, monkeypatch):
    fake = _FakeSB({'crew_hotel_directory': [
        {'iata': 'YUL', 'base': None, 'hotel': 'Sofitel Montreal Golden Mile', 'transfer_min': 40, 'votes': 3},
    ]})
    monkeypatch.setattr(app, 'sb', fake)
    monkeypatch.setattr(app, 'SB_AVAILABLE', True)
    _airline(monkeypatch, 'Lufthansa')
    r = client.get('/api/ax/crew-hotels?token=AT-x')
    assert r.status_code == 200
    body = r.get_json()
    assert body['airline'] == 'LUFTHANSA'
    assert body['hotels'][0]['hotel'].startswith('Sofitel')


def test_serve_no_airline_is_empty(client, monkeypatch):
    # Kein Profil-Airline → leer, KEIN falscher LH-Default für Fremd-Airline.
    monkeypatch.setattr(app, '_profile_load', lambda t: {'profile': {'airline': ''}})
    monkeypatch.setattr(app, '_ical_briefings_load', lambda t: {})
    r = client.get('/api/ax/crew-hotels?token=AT-x')
    assert r.status_code == 200
    assert r.get_json() == {'airline': '', 'count': 0, 'hotels': []}


# ── Suggest: schreibt status='suggested' mit Profil-Airline ────────────────────

def test_suggest_writes_suggested_row(client, monkeypatch):
    fake = _FakeSB({'crew_hotel_directory': []})
    monkeypatch.setattr(app, 'sb', fake)
    monkeypatch.setattr(app, 'SB_AVAILABLE', True)
    _airline(monkeypatch, 'Lufthansa')
    r = client.post('/api/ax/crew-hotels/suggest?token=AT-x',
                    data=json.dumps({'iata': 'yul', 'hotel': 'Sofitel Montreal Golden Mile',
                                     'transfer_min': 40}),
                    content_type='application/json')
    assert r.status_code == 200
    assert r.get_json()['status'] == 'suggested'
    assert len(fake.sink['inserts']) == 1
    _tbl, payload = fake.sink['inserts'][0]
    assert payload['airline'] == 'LUFTHANSA'
    assert payload['iata'] == 'YUL'          # normalisiert
    assert payload['status'] == 'suggested'  # NIE direkt approved
    assert payload['suggested_by'] and payload['suggested_by'] != 'AT-x'  # gehasht


def test_suggest_rejects_bad_iata(client, monkeypatch):
    monkeypatch.setattr(app, 'SB_AVAILABLE', True)
    _airline(monkeypatch, 'Lufthansa')
    r = client.post('/api/ax/crew-hotels/suggest?token=AT-x',
                    data=json.dumps({'iata': 'XX', 'hotel': 'Foo'}),
                    content_type='application/json')
    assert r.status_code == 400


def test_suggest_without_airline_rejected(client, monkeypatch):
    monkeypatch.setattr(app, '_profile_load', lambda t: {'profile': {'airline': ''}})
    monkeypatch.setattr(app, '_ical_briefings_load', lambda t: {})
    r = client.post('/api/ax/crew-hotels/suggest?token=AT-x',
                    data=json.dumps({'iata': 'YUL', 'hotel': 'Foo'}),
                    content_type='application/json')
    assert r.status_code == 400


# ── Admin: X-Admin-Token-Gate ─────────────────────────────────────────────────

def test_admin_approve_requires_token(client, monkeypatch):
    monkeypatch.setattr(app, '_recovery_pepper', lambda: 'SECRET')
    r = client.post('/api/admin/crew-hotels/approve',
                    data=json.dumps({'id': 'id-1'}), content_type='application/json')
    assert r.status_code == 401


def test_admin_approve_direct_correction(client, monkeypatch):
    fake = _FakeSB({'crew_hotel_directory': []})
    monkeypatch.setattr(app, 'sb', fake)
    monkeypatch.setattr(app, 'SB_AVAILABLE', True)
    monkeypatch.setattr(app, '_recovery_pepper', lambda: 'SECRET')
    r = client.post('/api/admin/crew-hotels/approve',
                    data=json.dumps({'airline': 'Lufthansa', 'iata': 'YUL',
                                     'hotel': 'Sofitel Montreal Golden Mile', 'transfer_min': 40}),
                    content_type='application/json',
                    headers={'X-Admin-Token': 'SECRET'})
    assert r.status_code == 200
    assert r.get_json()['ok'] is True
    _tbl, payload = fake.sink['inserts'][0]
    assert payload['airline'] == 'LUFTHANSA'
    assert payload['status'] == 'approved'
    assert payload['active'] is True


def test_admin_deactivate_requires_token(client, monkeypatch):
    monkeypatch.setattr(app, '_recovery_pepper', lambda: 'SECRET')
    r = client.post('/api/admin/crew-hotels/deactivate',
                    data=json.dumps({'id': 'id-1'}), content_type='application/json')
    assert r.status_code == 401


def test_admin_deactivate_sets_inactive(client, monkeypatch):
    fake = _FakeSB({'crew_hotel_directory': []})
    monkeypatch.setattr(app, 'sb', fake)
    monkeypatch.setattr(app, 'SB_AVAILABLE', True)
    monkeypatch.setattr(app, '_recovery_pepper', lambda: 'SECRET')
    r = client.post('/api/admin/crew-hotels/deactivate',
                    data=json.dumps({'id': 'id-9'}), content_type='application/json',
                    headers={'X-Admin-Token': 'SECRET'})
    assert r.status_code == 200
    _tbl, payload = fake.sink['updates'][0]
    assert payload['active'] is False


# ── Canonical airline key: LH/DLH/„Lufthansa" → EIN Bucket ────────────────────

def test_canonical_airline_key():
    assert app._canonical_airline_key('Lufthansa') == 'LUFTHANSA'
    assert app._canonical_airline_key('LH') == 'LUFTHANSA'
    assert app._canonical_airline_key('dlh') == 'LUFTHANSA'
    assert app._canonical_airline_key('SWISS') == 'SWISS'
    assert app._canonical_airline_key('lx') == 'SWISS'
    assert app._canonical_airline_key('Eurowings') == 'EUROWINGS'
    assert app._canonical_airline_key('') == ''
    assert app._canonical_airline_key(None) == ''
