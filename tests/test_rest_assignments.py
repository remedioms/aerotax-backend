"""Die geteilte Pausen-Einteilung (Owner 2026-08-09).

Der Kern dieser Suite ist NICHT „kommt die Einteilung an", sondern das
Gegenteil: **wer nicht auf dem Leg sitzt, sieht nichts** — und wer im falschen
Bereich, am falschen Tag oder mit unlesbarer Position anfragt, ebenfalls nicht.

Kein echtes Supabase: `_sb` und `_profile_of` sind Seams.
"""
import os
import sys

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import pytest

import app as A  # noqa: F401  (setzt sys.modules['app'] für die Lazy-Imports)
from blueprints import rest_assignments as RA


PURSER = 'AT-PURSER'
FB = 'AT-FB'
PILOT = 'AT-PILOT'
FREMD = 'AT-FREMD'

FLUG = 'LH462'
TAG = '2026-08-09'
DEP, ARR = 'FRA', 'JFK'

RUHEN = [{'nummer': 1, 'start_min': 905, 'end_min': 983},
         {'nummer': 2, 'start_min': 998, 'end_min': 1076}]

POSITIONEN = {
    PURSER: {'position': 'P1', 'name': 'Miguel Schumann'},
    FB: {'position': 'FB', 'name': 'Tibor Nagy'},
    PILOT: {'position': 'CPT', 'name': 'Paula Krause'},
    FREMD: {'position': 'P1', 'name': 'Jemand Anders'},
}

# Wer hat welches Leg im EIGENEN Dienstplan? (token, flight, date)
ROSTER = {
    (PURSER, FLUG, TAG), (FB, FLUG, TAG), (PILOT, FLUG, TAG),
}


class _Tbl:
    """Minimal-Supabase für rest_assignments (PK flight,flight_date,bereich)."""

    def __init__(self):
        self.rows = []
        self.missing = False

    def table(self, name):
        self._name = name
        self._f, self._op, self._lt = {}, None, None
        return self

    def select(self, *_a, **_k):
        self._op = 'select'
        return self

    def upsert(self, row, on_conflict=None):
        self._op, self._row, self._conflict = 'upsert', row, on_conflict
        return self

    def delete(self):
        self._op = 'delete'
        return self

    def eq(self, col, val):
        self._f[col] = val
        return self

    def lt(self, col, val):
        self._lt = (col, val)
        return self

    def contains(self, col, val):
        self._f['@>' + col] = val
        return self

    def limit(self, _n):
        return self

    def execute(self):
        if self._name == RA.TABLE and self.missing:
            raise RuntimeError('PGRST205 table not found')
        if self._name == 'crew_flight_assignments':
            hit = (self._f.get('self_token'), self._f.get('flight_number'),
                   self._f.get('flight_date')) in ROSTER
            return type('R', (), {'data': [{'self_token': 'x'}] if hit else []})()
        if self._name == 'roster_snapshots':
            # Der Snapshot-Weg wird in dieser Suite nicht befüllt — der
            # crew_flight_assignments-Weg trägt. Leer heißt „kein Beleg".
            return type('R', (), {'data': []})()
        if self._op == 'upsert':
            assert self._conflict == 'flight,flight_date,bereich'
            key = (self._row['flight'], self._row['flight_date'],
                   self._row['bereich'])
            self.rows = [r for r in self.rows
                         if (r['flight'], r['flight_date'],
                             r['bereich']) != key]
            self.rows.append(dict(self._row))
            return type('R', (), {'data': [dict(self._row)]})()
        if self._op == 'delete':
            keep, gone = [], []
            for r in self.rows:
                by_eq = all(r.get(k) == v for k, v in self._f.items())
                by_lt = (self._lt is not None
                         and r.get(self._lt[0], '') < self._lt[1])
                (gone if (self._f and by_eq) or by_lt else keep).append(r)
            self.rows = keep
            return type('R', (), {'data': gone})()
        out = [r for r in self.rows
               if all(r.get(k) == v for k, v in self._f.items())]
        return type('R', (), {'data': [dict(r) for r in out]})()


@pytest.fixture(autouse=True)
def _seams(monkeypatch):
    tbl = _Tbl()
    RA._table_ok[0] = None
    monkeypatch.setattr(RA, '_sb', lambda: tbl)
    # Die Instanz dieser Regression ist absichtlich auf 09.08. gepinnt. Ohne
    # eingefrorenes "heute" loescht der produktive Alters-Pruner die gerade
    # geschriebene Fixture nach einigen Tagen (bzw. mitten im Volltest beim
    # UTC-Tageswechsel) und die Bereichs-/Autorisierungs-Tests werden
    # kalenderabhaengig. Der Pruner selbst hat eigene Tests.
    monkeypatch.setattr(RA, '_today', lambda: TAG)
    monkeypatch.setattr(RA, '_profile_of', lambda t: dict(POSITIONEN.get(t, {})))
    monkeypatch.setattr(RA, '_bearer_ok', lambda t: True)
    # Das globale Auth-Gate (`_bug004_token_auth_gate`) prüft jeden AT-Token
    # gegen auth_users und verlangt den passenden Bearer. Beides ist hier nicht
    # der Prüfgegenstand — die Test-Token existieren nicht. Dieselbe Abschaltung
    # wie in test_acknowledged_roster_pdf.py; das IDOR-Gate DIESES Moduls
    # (`_bearer_ok`) wird in `test_ohne_bearer_kein_zugriff` eigens geprüft.
    monkeypatch.setattr(A, '_validate_token', lambda *_a, **_k:
                        A._TokenValidationResult(
                            A._TokenValidationState.VALID, 'test@example.test'))
    monkeypatch.setattr(A, '_BUG004_REQUIRE_TOKEN_BINDING', False)
    yield tbl


@pytest.fixture
def client():
    A.app.config['TESTING'] = True
    with A.app.test_client() as c:
        yield c


def _put(client, token, **over):
    body = {'flight': FLUG, 'date': TAG, 'bereich': 'kabine',
            'dep': DEP, 'arr': ARR, 'ruhen': RUHEN}
    body.update(over)
    return client.put(f'/api/rest-assignment/{token}', json=body)


def _get(client, token, flight=FLUG, date=TAG, bereich='kabine'):
    return client.get(
        f'/api/rest-assignment/{token}/{flight}/{date}/{bereich}')


# ══════════════════════════════════════════════════════════════════════════
#  Der Plan enthält NUR ZEITEN
# ══════════════════════════════════════════════════════════════════════════

def test_plan_nimmt_nur_zeiten():
    assert RA.clean_ruhen(RUHEN) == RUHEN


def test_plan_mit_namen_ist_ungueltig():
    """Ein Name in der Einteilung ist kein „ignoriertes Feld", er ist ein
    Fehler — sonst wäre der stille Teil-Übernehmer der Weg, auf dem
    Personendaten in diese Tabelle geraten."""
    assert RA.clean_ruhen([{'nummer': 1, 'start_min': 1, 'end_min': 2,
                            'name': 'Marco C.'}]) is None
    assert RA.clean_ruhen([{'nummer': 1, 'start_min': 1, 'end_min': 2,
                            'pk': '450460I'}]) is None


def test_plan_grenzen():
    assert RA.clean_ruhen([]) is None
    assert RA.clean_ruhen('nope') is None
    assert RA.clean_ruhen([{'nummer': 1, 'start_min': 5, 'end_min': 5}]) is None
    assert RA.clean_ruhen([{'nummer': 1, 'start_min': -1, 'end_min': 5}]) is None
    assert RA.clean_ruhen([{'nummer': 1, 'start_min': 1,
                            'end_min': RA.MAX_MIN + 1}]) is None
    assert RA.clean_ruhen([{'nummer': 2, 'start_min': 1, 'end_min': 5}]) is None
    assert RA.clean_ruhen([{'nummer': True, 'start_min': 1,
                            'end_min': 5}]) is None
    assert RA.clean_ruhen([{'nummer': i + 1, 'start_min': i,
                            'end_min': i + 1}
                           for i in range(RA.MAX_RUHEN + 1)]) is None


def test_gespeicherter_plan_traegt_keine_zone():
    """Eine erfundene Zeitzone wäre ein synthetisierter Wert. Der Rechner
    arbeitet auf der Wanduhr des Legs — und die ist für die ganze Crew
    dieselbe."""
    p = RA.build_plan(RUHEN)
    assert p['clock'] == 'wanduhr'
    assert p['ruhen'] == RUHEN
    assert set(p) == {'v', 'clock', 'ruhen'}


# ══════════════════════════════════════════════════════════════════════════
#  Rolle → Bereich → Schreibrecht
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('pos,erwartet', [
    ('P1', 'kabine'), ('P2', 'kabine'), ('PU', 'kabine'), ('FB', 'kabine'),
    ('Purser', 'kabine'), ('Flugbegleiter', 'kabine'),
    ('CPT', 'cockpit'), ('FO', 'cockpit'), ('SF', 'cockpit'),
    ('First Officer', 'cockpit'),
    ('', None), (None, None), ('Bodenpersonal', None),
    # Produktvertrag 2026-08-02: die auswählbare Position „Crew" ist bei LH die
    # KABINE. iOS kennt diesen Zweig, `_hangout_role_of_position` nicht — ohne
    # den Nachtrag in `bereich_of_position` liefen App und Server still
    # auseinander. „Ground Crew" bleibt unbestimmbar (kein Teilstring-Match).
    ('Crew', 'kabine'), ('CREW', 'kabine'), ('Ground Crew', None),
])
def test_bereich_aus_position(pos, erwartet):
    assert RA.bereich_of_position(pos) == erwartet


@pytest.mark.parametrize('pos,bereich,darf', [
    ('P1', 'kabine', True), ('P2', 'kabine', True), ('Purser', 'kabine', True),
    ('SEN', 'kabine', True),
    ('FB', 'kabine', False), ('Flugbegleiter', 'kabine', False),
    ('CPT', 'cockpit', True), ('FO', 'cockpit', True),
    # Bereichs-Kreuzung: niemand schreibt in die fremde Gruppe.
    ('P1', 'cockpit', False), ('CPT', 'kabine', False),
    # Unlesbare Position ⇒ gar nichts.
    ('', 'kabine', False), (None, 'cockpit', False),
    ('Bodenpersonal', 'kabine', False),
])
def test_schreibrecht(pos, bereich, darf):
    assert RA.darf_einteilen(pos, bereich) is darf


def test_senior_first_officer_ist_kein_purser():
    """Reihenfolge-Falle: „SEN" ist ein Purser-Kürzel, „Senior First Officer"
    ist Cockpit. Der Bereich entscheidet ZUERST."""
    assert RA.bereich_of_position('Senior First Officer') == 'cockpit'
    assert RA.darf_einteilen('Senior First Officer', 'kabine') is False


# ══════════════════════════════════════════════════════════════════════════
#  Gate 1: ohne Leg im eigenen Roster passiert nichts
# ══════════════════════════════════════════════════════════════════════════

def test_fremder_flug_sieht_nichts(client):
    assert _put(client, PURSER).get_json()['ok'] is True
    # Jemand mit derselben Rolle, aber ohne dieses Leg im eigenen Dienstplan.
    r = _get(client, FREMD)
    assert r.status_code == 200
    assert r.get_json()['assignment'] is None


def test_fremder_darf_nicht_schreiben(client):
    r = _put(client, FREMD)
    assert r.status_code == 403
    assert r.get_json()['error'] == 'not_on_leg'


def test_fremde_flugnummer_sieht_nichts(client):
    _put(client, PURSER)
    assert _get(client, PURSER, flight='LX318').get_json()['assignment'] is None


# ══════════════════════════════════════════════════════════════════════════
#  Gate 2: Kabine sieht Cockpit nicht — und umgekehrt
# ══════════════════════════════════════════════════════════════════════════

def test_kabine_und_cockpit_sind_getrennt(client):
    assert _put(client, PURSER, bereich='kabine').get_json()['ok'] is True
    assert _put(client, PILOT, bereich='cockpit',
                ruhen=[{'nummer': 1, 'start_min': 60, 'end_min': 180}]
                ).get_json()['ok'] is True

    # Der Purser sieht seine Kabinen-Einteilung — und NICHT die des Cockpits.
    kab = _get(client, PURSER, bereich='kabine').get_json()['assignment']
    assert kab and kab['ruhen'] == RUHEN
    assert _get(client, PURSER,
                bereich='cockpit').get_json()['assignment'] is None

    # Der Pilot sieht seine Cockpit-Einteilung — und NICHT die der Kabine.
    cp = _get(client, PILOT, bereich='cockpit').get_json()['assignment']
    assert cp and cp['ruhen'][0]['start_min'] == 60
    assert _get(client, PILOT,
                bereich='kabine').get_json()['assignment'] is None


def test_flugbegleiter_liest_aber_schreibt_nicht(client):
    _put(client, PURSER)
    r = _put(client, FB)
    assert r.status_code == 403 and r.get_json()['error'] == 'not_allowed'
    gelesen = _get(client, FB).get_json()['assignment']
    assert gelesen and gelesen['ruhen'] == RUHEN


def test_unlesbare_position_sieht_nichts(client, monkeypatch):
    _put(client, PURSER)
    monkeypatch.setattr(RA, '_profile_of',
                        lambda t: {'position': '', 'name': 'Ohne Position'})
    assert _get(client, PURSER).get_json()['assignment'] is None


# ══════════════════════════════════════════════════════════════════════════
#  Gate 3: der Nachbartag greift nicht
# ══════════════════════════════════════════════════════════════════════════

def test_nachbartag_greift_nicht(client):
    """Die Fehlerfamilie der Nacht auf den 07.08.: ein Schlüssel, der den
    Nachbartag mitfängt. Hier gibt es KEINE ±1-Toleranz — weder im
    Roster-Beweis noch im Select."""
    _put(client, PURSER)
    for nachbar in ('2026-08-08', '2026-08-10'):
        assert _get(client, PURSER,
                    date=nachbar).get_json()['assignment'] is None


def test_schreiben_am_falschen_tag_faellt_durch(client):
    r = _put(client, PURSER, date='2026-08-10')
    assert r.status_code == 403 and r.get_json()['error'] == 'not_on_leg'


def test_datum_muss_exakt_sein():
    assert RA.norm_date('2026-8-9') is None
    assert RA.norm_date('2026-08-09T12:00:00Z') == '2026-08-09'
    assert RA.norm_date('') is None


# ══════════════════════════════════════════════════════════════════════════
#  Wer hat eingeteilt?
# ══════════════════════════════════════════════════════════════════════════

def test_autor_ist_sichtbar(client):
    _put(client, PURSER)
    a = _get(client, FB).get_json()['assignment']
    assert a['author_name'] == 'Miguel Schumann'
    assert a['author_self'] is False
    eigen = _get(client, PURSER).get_json()['assignment']
    assert eigen['author_self'] is True


def test_antwort_traegt_die_instanz_zur_gegenprobe(client):
    _put(client, PURSER)
    a = _get(client, PURSER).get_json()['assignment']
    assert (a['flight'], a['date'], a['dep'], a['arr']) == (FLUG, TAG, DEP, ARR)


def test_kein_personenbezug_in_der_zeile(_seams, client):
    """Die gespeicherte Zeile führt ausser dem Autor-Token KEINE Person."""
    _put(client, PURSER)
    row = _seams.rows[0]
    assert set(row) == {'flight', 'flight_date', 'bereich', 'dep', 'arr',
                        'author_token', 'plan', 'updated_at'}
    import json as _json
    txt = _json.dumps(row['plan'])
    assert 'Miguel' not in txt and 'Schumann' not in txt


# ══════════════════════════════════════════════════════════════════════════
#  Aufheben · alter Client · fehlende Tabelle
# ══════════════════════════════════════════════════════════════════════════

def test_aufheben_raeumt_auf(client):
    _put(client, PURSER)
    r = client.delete(
        f'/api/rest-assignment/{PURSER}/{FLUG}/{TAG}/kabine')
    assert r.status_code == 200
    assert _get(client, PURSER).get_json()['assignment'] is None


def test_flugbegleiter_kann_nicht_aufheben(client):
    _put(client, PURSER)
    r = client.delete(f'/api/rest-assignment/{FB}/{FLUG}/{TAG}/kabine')
    assert r.status_code == 403
    assert _get(client, FB).get_json()['assignment'] is not None


def test_ohne_bearer_kein_zugriff(client, monkeypatch):
    monkeypatch.setattr(RA, '_bearer_ok', lambda t: False)
    assert _put(client, PURSER).status_code == 403
    assert _get(client, PURSER).status_code == 403


def test_fehlende_tabelle_bricht_nichts(_seams, client):
    """Alter Stand ohne Migration: der Lese-Weg antwortet „keine Einteilung",
    er wirft nicht. Alte App-Builds fragen ohnehin nie — sie verlieren nichts."""
    _seams.missing = True
    r = _get(client, PURSER)
    assert r.status_code == 200 and r.get_json()['assignment'] is None
    assert _put(client, PURSER).status_code == 503


def test_konto_loeschen_nimmt_die_einteilung_mit():
    """DSGVO-Cascade in app.py — sonst bliebe eine „von X eingeteilt"-
    Zuschreibung ohne X zurück."""
    import inspect
    src = inspect.getsource(A.delete_account) if hasattr(A, 'delete_account') \
        else open(A.__file__).read()
    assert "('rest_assignments',        'author_token')" in src
