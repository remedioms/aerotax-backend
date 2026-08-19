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


# Prod-DDL (geprueft 17.08.2026, information_schema): `transfer_min` ist
# `integer NOT NULL DEFAULT 0`. Ein explizites `None` im Insert ist damit KEIN
# NULL-Wert, sondern ein 23502-Fehler — genau der Unterschied, den ein
# Fake ohne Constraint verschluckt (Blocker: 500 bei jeder Hotel-Meldung ohne
# Transferzeit). Der Fake bildet das jetzt nach.
_NOT_NULL_COLUMNS = {
    'crew_hotel_directory': ('airline', 'iata', 'hotel', 'transfer_min',
                             'status', 'votes', 'active'),
}


class _NotNullViolation(Exception):
    """PostgREST-Aequivalent: {'code': '23502', ...}."""


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
        for row in (payload if isinstance(payload, list) else [payload]):
            for column in _NOT_NULL_COLUMNS.get(self._table, ()):
                if column in row and row[column] is None:
                    raise _NotNullViolation(
                        f'23502 null value in column "{column}" of relation '
                        f'"{self._table}" violates not-null constraint')
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

    def ilike(self, *_a, **_k):
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


def test_condor_serve_returns_only_hotels_in_own_roster(client, monkeypatch):
    fake = _FakeSB({'crew_hotel_directory': [
        {'iata': 'JFK', 'base': 'FRA', 'hotel': 'Own Roster Hotel',
         'transfer_min': 40, 'votes': 3},
        {'iata': 'LAX', 'base': 'FRA', 'hotel': 'Other Crew Hotel',
         'transfer_min': 30, 'votes': 2},
    ]})
    monkeypatch.setattr(app, 'sb', fake)
    monkeypatch.setattr(app, 'SB_AVAILABLE', True)
    monkeypatch.setattr(
        app, '_profile_load',
        lambda t: {'profile': {'airline': 'Condor'}})
    monkeypatch.setattr(app, '_ical_briefings_load', lambda t: {
        '2099-07-03': {
            'ical_imported_at': '2099-06-01T00:00:00',
            'ical_layover_ort': 'JFK',
        },
    })
    r = client.get('/api/ax/crew-hotels?token=AT-condor')
    assert r.status_code == 200
    body = r.get_json()
    assert body['airline'] == 'CONDOR'
    assert body['count'] == 1
    assert body['hotels'][0]['iata'] == 'JFK'


# ── WLAN-Code: airline-gegatet und nur an aktiven Hotels ─────────────────────

def test_serve_includes_wifi_code_without_contributor_hash(client, monkeypatch):
    fake = _FakeSB({'crew_hotel_directory': [
        {'iata': 'SFO', 'base': 'FRA', 'hotel': 'Hilton Union Square',
         'transfer_min': 30, 'votes': 3, 'wifi_code': 'Crew-2026!',
         'wifi_updated_at': '2026-08-18T20:00:00+00:00',
         'wifi_updated_by': 'must-not-leave-server'},
    ]})
    monkeypatch.setattr(app, 'sb', fake)
    monkeypatch.setattr(app, 'SB_AVAILABLE', True)
    _airline(monkeypatch, 'Lufthansa')

    body = client.get('/api/ax/crew-hotels?token=AT-x').get_json()

    assert body['hotels'][0]['wifi_code'] == 'Crew-2026!'
    assert 'wifi_updated_by' not in body['hotels'][0]


def test_wifi_code_updates_exact_active_hotel(client, monkeypatch):
    fake = _FakeSB({'crew_hotel_directory': [
        {'id': 'hotel-sfo', 'iata': 'SFO', 'base': 'FRA',
         'hotel': 'Hilton Union Square', 'official_name': None,
         'wifi_code': None},
        {'id': 'hotel-lax', 'iata': 'LAX', 'base': 'FRA',
         'hotel': 'Other Hotel', 'official_name': None,
         'wifi_code': None},
    ]})
    monkeypatch.setattr(app, 'sb', fake)
    monkeypatch.setattr(app, 'SB_AVAILABLE', True)
    _airline(monkeypatch, 'Lufthansa')

    r = client.post('/api/ax/crew-hotels/wifi?token=AT-secret',
                    data=json.dumps({'iata': 'SFO', 'base': 'FRA',
                                     'hotel': 'Hilton Union Square',
                                     'wifi_code': ' Crew 2026! '}),
                    content_type='application/json')

    assert r.status_code == 200
    assert r.get_json()['wifi_code'] == ' Crew 2026! '
    table, payload = fake.sink['updates'][-1]
    assert table == 'crew_hotel_directory'
    assert payload['wifi_code'] == ' Crew 2026! '
    assert payload['wifi_updated_by'] != 'AT-secret'
    assert len(payload['wifi_updated_by']) == 32


def test_wifi_code_accepts_official_display_name(client, monkeypatch):
    fake = _FakeSB({'crew_hotel_directory': [
        {'id': 'hotel-yul', 'iata': 'YUL', 'base': None,
         'hotel': 'Crowd Hotel Name', 'official_name': 'Sofitel Montréal',
         'wifi_code': 'old'},
    ]})
    monkeypatch.setattr(app, 'sb', fake)
    monkeypatch.setattr(app, 'SB_AVAILABLE', True)
    _airline(monkeypatch, 'Lufthansa')

    r = client.post('/api/ax/crew-hotels/wifi?token=AT-x',
                    data=json.dumps({'iata': 'YUL', 'hotel': 'Sofitel Montréal',
                                     'wifi_code': 'new-code'}),
                    content_type='application/json')

    assert r.status_code == 200
    assert r.get_json()['status'] == 'updated'


def test_wifi_code_rejects_unknown_hotel(client, monkeypatch):
    fake = _FakeSB({'crew_hotel_directory': [
        {'id': 'hotel-sfo', 'iata': 'SFO', 'base': 'FRA',
         'hotel': 'Hilton Union Square', 'official_name': None},
    ]})
    monkeypatch.setattr(app, 'sb', fake)
    monkeypatch.setattr(app, 'SB_AVAILABLE', True)
    _airline(monkeypatch, 'Lufthansa')

    r = client.post('/api/ax/crew-hotels/wifi?token=AT-x',
                    data=json.dumps({'iata': 'SFO', 'hotel': 'Fake Hotel',
                                     'wifi_code': 'code'}),
                    content_type='application/json')

    assert r.status_code == 404
    assert r.get_json()['error'] == 'unknown_hotel'
    assert fake.sink['updates'] == []


def test_wifi_code_never_crosses_homebase_rows(client, monkeypatch):
    fake = _FakeSB({'crew_hotel_directory': [
        {'id': 'hotel-fra', 'iata': 'SFO', 'base': 'FRA',
         'hotel': 'Hilton Union Square', 'official_name': None},
    ]})
    monkeypatch.setattr(app, 'sb', fake)
    monkeypatch.setattr(app, 'SB_AVAILABLE', True)
    _airline(monkeypatch, 'Lufthansa')

    r = client.post('/api/ax/crew-hotels/wifi?token=AT-x',
                    data=json.dumps({'iata': 'SFO', 'base': 'MUC',
                                     'hotel': 'Hilton Union Square',
                                     'wifi_code': 'code'}),
                    content_type='application/json')

    assert r.status_code == 404
    assert r.get_json()['error'] == 'unknown_hotel'
    assert fake.sink['updates'] == []


def test_wifi_code_rejects_controls_and_whitespace_only(client, monkeypatch):
    monkeypatch.setattr(app, 'SB_AVAILABLE', True)
    _airline(monkeypatch, 'Lufthansa')
    r = client.post('/api/ax/crew-hotels/wifi?token=AT-x',
                    data=json.dumps({'iata': 'SFO', 'hotel': 'Hotel',
                                     'wifi_code': '\n\t'}),
                    content_type='application/json')
    assert r.status_code == 400
    assert r.get_json()['error'] == 'invalid_wifi_code'


def test_condor_wifi_rejects_station_outside_own_roster(client, monkeypatch):
    monkeypatch.setattr(app, 'SB_AVAILABLE', True)
    monkeypatch.setattr(app, '_profile_load',
                        lambda t: {'profile': {'airline': 'Condor'}})
    monkeypatch.setattr(app, '_ical_briefings_load', lambda t: {
        '2099-07-03': {'ical_imported_at': '2099-06-01T00:00:00',
                       'ical_layover_ort': 'JFK'},
    })
    r = client.post('/api/ax/crew-hotels/wifi?token=AT-condor',
                    data=json.dumps({'iata': 'LAX', 'hotel': 'Other Hotel',
                                     'wifi_code': 'code'}),
                    content_type='application/json')
    assert r.status_code == 403
    assert r.get_json()['error'] == 'station_not_in_own_roster'


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
    # AUTO-FREIGABE-Politik (Owner 2026-07-19): Station OHNE aktives Hotel →
    # erster Vorschlag geht SOFORT live (approved_new). Der Vorsichts-Pfad
    # (suggested + 2-Stimmen-Promotion) gilt nur noch für BELEGTE Stationen.
    assert r.get_json()['status'] == 'approved_new'
    assert len(fake.sink['inserts']) == 1
    _tbl, payload = fake.sink['inserts'][0]
    assert payload['airline'] == 'LUFTHANSA'
    assert payload['iata'] == 'YUL'          # normalisiert
    assert payload['status'] == 'approved'
    assert payload['suggested_by'] and payload['suggested_by'] != 'AT-x'  # gehasht


def test_suggest_without_transfer_time_reaches_the_database(client, monkeypatch):
    """Ohne Transferzeit darf die Meldung NICHT an der Spalte scheitern.

    Regression 17.08.: der Insert schickte `transfer_min: None` gegen
    `int NOT NULL DEFAULT 0` — jede Hotel-Meldung ohne Transferzeit endete als
    500 `db` und der Vorschlag war weg. Der Key fehlt jetzt, der Spalten-Default
    greift.
    """
    fake = _FakeSB({'crew_hotel_directory': []})
    monkeypatch.setattr(app, 'sb', fake)
    monkeypatch.setattr(app, 'SB_AVAILABLE', True)
    _airline(monkeypatch, 'Lufthansa')

    r = client.post('/api/ax/crew-hotels/suggest?token=AT-x',
                    data=json.dumps({'iata': 'CLJ',
                                     'hotel': 'Hampton by Hilton Cluj-Napoca'}),
                    content_type='application/json')

    assert r.status_code == 200
    assert r.get_json()['status'] == 'approved_new'
    _tbl, payload = fake.sink['inserts'][0]
    assert 'transfer_min' not in payload
    assert payload['iata'] == 'CLJ'


def test_both_insert_paths_share_one_row_builder():
    """Der `suggested`-Pfad (belegte Station) hatte denselben Fehler.

    Er ist mit dem No-op-Filter dieses Fakes nicht separat ansteuerbar (beide
    Selects sehen dieselben Zeilen), deshalb wird hier die eigentliche Garantie
    gepinnt: es gibt genau EINEN Row-Builder und beide Inserts benutzen ihn —
    ein neu eingefuegtes Literal-Dict faellt auf.
    """
    import inspect
    source = inspect.getsource(app.ax_crew_hotels_suggest)
    assert source.count('_hotel_row(') == 3          # def + 2 Aufrufstellen
    assert '.insert(\n            _hotel_row(' in source \
        or '.insert(_hotel_row(' in source
    assert "'transfer_min': tmin" not in source


def test_explicit_none_would_violate_the_real_column(client, monkeypatch):
    """Beweist, dass die Constraint-Simulation echt ist (sonst waere der Test
    oben ein Papiertiger, der auch den kaputten Stand gruen faerbt)."""
    fake = _FakeSB({'crew_hotel_directory': []})
    with pytest.raises(_NotNullViolation):
        fake.table('crew_hotel_directory').insert(
            {'airline': 'LUFTHANSA', 'iata': 'CLJ', 'hotel': 'X',
             'transfer_min': None, 'status': 'approved', 'votes': 1,
             'active': True}).execute()


def test_known_transfer_time_is_still_written(client, monkeypatch):
    fake = _FakeSB({'crew_hotel_directory': []})
    monkeypatch.setattr(app, 'sb', fake)
    monkeypatch.setattr(app, 'SB_AVAILABLE', True)
    _airline(monkeypatch, 'Lufthansa')

    r = client.post('/api/ax/crew-hotels/suggest?token=AT-x',
                    data=json.dumps({'iata': 'CLJ', 'hotel': 'Hampton Hotel',
                                     'transfer_min': 0}),
                    content_type='application/json')

    assert r.status_code == 200
    _tbl, payload = fake.sink['inserts'][0]
    # Explizite 0 heisst „fußläufig" und bleibt eine echte Angabe.
    assert payload['transfer_min'] == 0


def test_suggest_rejects_invalid_transfer_time(client, monkeypatch):
    fake = _FakeSB({'crew_hotel_directory': []})
    monkeypatch.setattr(app, 'sb', fake)
    monkeypatch.setattr(app, 'SB_AVAILABLE', True)
    _airline(monkeypatch, 'Lufthansa')

    r = client.post('/api/ax/crew-hotels/suggest?token=AT-x',
                    data=json.dumps({'iata': 'CLJ', 'hotel': 'Hampton Hotel',
                                     'transfer_min': 'unknown'}),
                    content_type='application/json')

    assert r.status_code == 400
    assert r.get_json()['error'] == 'invalid_transfer_min'
    assert fake.sink['inserts'] == []


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


def test_condor_suggest_rejects_station_outside_own_roster(client, monkeypatch):
    monkeypatch.setattr(app, 'SB_AVAILABLE', True)
    monkeypatch.setattr(
        app, '_profile_load',
        lambda t: {'profile': {'airline': 'Condor'}})
    monkeypatch.setattr(app, '_ical_briefings_load', lambda t: {
        '2099-07-03': {
            'ical_imported_at': '2099-06-01T00:00:00',
            'ical_layover_ort': 'JFK',
        },
    })
    r = client.post('/api/ax/crew-hotels/suggest?token=AT-condor',
                    data=json.dumps({'iata': 'LAX', 'hotel': 'Not My Hotel'}),
                    content_type='application/json')
    assert r.status_code == 403
    assert r.get_json()['error'] == 'station_not_in_own_roster'


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
    assert app._canonical_airline_key('Condor') == 'CONDOR'
    assert app._canonical_airline_key('DE') == 'CONDOR'
    assert app._canonical_airline_key('CFG') == 'CONDOR'
    assert app._canonical_airline_key('') == ''
    assert app._canonical_airline_key(None) == ''


# ── Lufthansa Cargo: eigene ANZEIGE, operativ derselbe Bucket ─────────────────
# Owner 2026-07-29: Cargo wird als eigene Airline wählbar (leichter Kollegen
# finden), ist aber operativ identisch zu Lufthansa Main. Der Kanonisierer MUSS
# beides zusammenführen — sonst fiele Cargo-Crew aus Crewhotels, Airline-Forum
# und der Hangout-Zielgruppe „nur meine Airline" ihrer Main-Kollegen heraus.

def test_canonical_airline_key_cargo_faellt_mit_lufthansa_zusammen():
    for raw in ('Lufthansa Cargo', 'lufthansa cargo', '  Lufthansa Cargo AG ',
                'LH Cargo', 'lh cargo', 'GEC', 'gec', 'LCAG'):
        assert app._canonical_airline_key(raw) == 'LUFTHANSA', raw


def test_canonical_airline_key_cargo_bricht_bestand_nicht():
    """BESTANDSSCHUTZ: wer heute „Lufthansa"/„LH"/„DLH" trägt, behält exakt
    seinen Bucket — die neue Cargo-Wahl ist rein opt-in, es wird niemand
    migriert und niemand aus dem LH-Bucket herausgelöst."""
    assert app._canonical_airline_key('Lufthansa') == 'LUFTHANSA'
    assert app._canonical_airline_key('LH') == 'LUFTHANSA'
    assert app._canonical_airline_key('DLH') == 'LUFTHANSA'
    assert app._canonical_airline_key('Deutsche Lufthansa AG') == 'LUFTHANSA'


def test_canonical_airline_key_cargo_kollidiert_nicht_mit_nachbarn():
    """Der Cargo-Zweig darf weder den Lufthansa-City-Bucket kapern noch
    fremde Cargo-Airlines einsammeln (eigener Bucket bleibt eigener Bucket)."""
    assert app._canonical_airline_key('Lufthansa City') == 'LUFTHANSA CITY'
    assert app._canonical_airline_key('Lufthansa CityLine') == 'LUFTHANSA CITY'
    assert app._canonical_airline_key('Cargolux') == 'CARGOLUX'
    assert app._canonical_airline_key('Turkish Cargo') == 'TURKISH CARGO'
    assert app._canonical_airline_key('Cargo') == 'CARGO'


def test_canonical_airline_label_nennt_den_wirklich_gefilterten_kreis():
    """Das Zielgruppen-Label darf keine Trennung versprechen, die der Filter
    nicht macht: „Lufthansa Cargo" filtert auf ALLE Lufthansa."""
    assert app._canonical_airline_label('Lufthansa Cargo') == 'Lufthansa'
    assert app._canonical_airline_label('LH') == 'Lufthansa'
    assert app._canonical_airline_label('lx') == 'SWISS'
    # Ist der Roh-String selbst der Bucket-Name, gewinnt die Schreibweise des Users.
    assert app._canonical_airline_label('Lufthansa') == 'Lufthansa'
    assert app._canonical_airline_label('SWISS') == 'SWISS'
    assert app._canonical_airline_label('AeroWest') == 'AeroWest'
    assert app._canonical_airline_label('') == ''
    assert app._canonical_airline_label(None) == ''


def test_hangout_audience_cargo_sieht_lufthansa_treffs_und_umgekehrt():
    """Kernversprechen: die Zielgruppe „nur meine Airline" eines Cargo-Piloten
    trifft die Main-Kollegen an derselben Station — und der Treff eines
    Main-Kollegen bleibt für Cargo sichtbar. Sonst wäre die neue Wahl eine
    Verschlechterung."""
    aud_cargo = app._hangout_audience_normalize(
        {'airline': 'same'}, {'airline': 'Lufthansa Cargo'})
    assert aud_cargo['airline'] == 'LUFTHANSA'
    assert aud_cargo['airline_label'] == 'Lufthansa'
    assert app._hangout_audience_matches(aud_cargo, {'airline': 'Lufthansa'})
    assert app._hangout_audience_matches(aud_cargo, {'airline': 'LH'})

    aud_main = app._hangout_audience_normalize(
        {'airline': 'same'}, {'airline': 'Lufthansa'})
    assert app._hangout_audience_matches(aud_main, {'airline': 'Lufthansa Cargo'})
    # Fremde Airlines bleiben draußen (fail-closed wie bisher).
    assert not app._hangout_audience_matches(aud_main, {'airline': 'SWISS'})
    assert not app._hangout_audience_matches(aud_main, {'airline': ''})
