"""LH FlightOps Crew API — MOCK-Client-Gerüst (Engine B). Rein offline: prüft
die verifizierbare Client-Hälfte (Config/No-op/Token-Cache/Drossel), HTTP
gemockt. KEIN Response-Parser getestet (Shape noch nicht gegen echten Mock
gesehen — kommt separat)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blueprints import lh_flightops as fo


def test_noop_when_unconfigured(monkeypatch):
    monkeypatch.setattr(fo, '_KEY', ''); monkeypatch.setattr(fo, '_SECRET', '')
    assert fo.flightops_configured() is False
    assert fo.flightops_ready() is False
    assert fo._token() is None
    assert fo.flightops_get('/x') is None


def test_configured_but_no_base_is_not_ready(monkeypatch):
    monkeypatch.setattr(fo, '_KEY', 'k'); monkeypatch.setattr(fo, '_SECRET', 's')
    monkeypatch.setattr(fo, '_BASE', '')
    assert fo.flightops_configured() is True
    assert fo.flightops_ready() is False        # Base-URL fehlt → Endpoints no-op
    # Endpoint-Call bleibt no-op ohne Base
    assert fo.flightops_get('/flight-operations/dutyEvents') is None


def test_get_uses_token_and_base(monkeypatch):
    monkeypatch.setattr(fo, '_KEY', 'k'); monkeypatch.setattr(fo, '_SECRET', 's')
    monkeypatch.setattr(fo, '_BASE', 'https://mock.example/v1')
    monkeypatch.setattr(fo, '_token', lambda: 'TOK')
    monkeypatch.setattr(fo, '_budget_ok', lambda: True)
    captured = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"DutyEvents":{"ok":true}}'

    def _fake_urlopen(req, timeout=0):
        captured['url'] = req.full_url
        captured['auth'] = req.headers.get('Authorization')
        return _Resp()

    monkeypatch.setattr(fo.urllib.request, 'urlopen', _fake_urlopen)
    out = fo.flightops_get('/flight-operations/dutyEvents', {'crewId': 'X'})
    assert out == {'DutyEvents': {'ok': True}}
    assert captured['url'].startswith('https://mock.example/v1/flight-operations/dutyEvents?')
    assert 'crewId=X' in captured['url']
    assert captured['auth'] == 'Bearer TOK'


def test_token_cache(monkeypatch):
    monkeypatch.setattr(fo, '_KEY', 'k'); monkeypatch.setattr(fo, '_SECRET', 's')
    monkeypatch.setattr(fo, '_tok_val', None); monkeypatch.setattr(fo, '_tok_exp', 0.0)
    calls = {'n': 0}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            calls['n'] += 1
            return b'{"access_token":"AAA","expires_in":3600}'

    monkeypatch.setattr(fo.urllib.request, 'urlopen', lambda req, timeout=0: _Resp())
    assert fo._token() == 'AAA'
    assert fo._token() == 'AAA'         # zweiter Aufruf aus Cache
    assert calls['n'] == 1              # nur EIN echter Token-Fetch


def test_ping_endpoint_shape(monkeypatch):
    monkeypatch.setattr(fo, '_KEY', ''); monkeypatch.setattr(fo, '_SECRET', '')
    import app as backend
    client = backend.app.test_client()
    r = client.get('/api/lh/flightops/ping')
    assert r.status_code == 200
    d = r.get_json()
    assert d['configured'] is False and d['token_ok'] is False and 'note' in d
