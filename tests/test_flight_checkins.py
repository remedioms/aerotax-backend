"""Für einen Flug einchecken → Ereignis-Pushes (Forum-Wunsch 2026-07-31).

Der Kern dieser Suite ist NICHT „kommt ein Push an", sondern das Gegenteil:
OHNE BELEG ENTSTEHT KEINE MELDUNG. Eine verstrichene Planzeit ist kein Abflug
(die Maschine kann am Gate stehen) und keine Landung; eine Plan-Ankunft ist
keine geschätzte Ankunft. Genau diese Verwechslung hätte am 2026-07-31 beim
Flug des Owners (LH455 SFO→FRA) eine falsche „ist gelandet"-Meldung an fremde
Leute geschickt — sein Roster-Sektor trug `status: null` und `est_arr: null`.

Kein echtes Supabase, kein echtes APNs: `_sb` und `_do_push` sind Seams.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import pytest

import app as A  # noqa: F401  (setzt sys.modules['app'] für die Lazy-Imports)
from blueprints import flight_checkins as FC


NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _pin_app():
    _prev = sys.modules.get('app')
    sys.modules['app'] = A
    yield
    if _prev is not None:
        sys.modules['app'] = _prev


# ══════════════════════════════════════════════════════════════════════════
# „landet in etwa einer Stunde" — die Schätz-Regel
# ══════════════════════════════════════════════════════════════════════════

def test_eta_ohne_geschaetzte_ankunft_schweigt():
    """Liegt NUR die Planzeit vor, gibt der Aufrufer None herein — und es
    entsteht keine Meldung. Das ist der Fall des Owner-Fluges."""
    assert FC.eta_one_hour_due(NOW, None, True, False) is False


def test_eta_ohne_belegten_abflug_schweigt():
    """Eine Restzeit auf eine Maschine zu rechnen, die noch am Gate steht,
    ist keine Aussage über eine Landung."""
    est = (NOW + timedelta(minutes=40)).isoformat()
    assert FC.eta_one_hour_due(NOW, est, False, False) is False


def test_eta_im_fenster_meldet():
    est = (NOW + timedelta(minutes=40)).isoformat()
    assert FC.eta_one_hour_due(NOW, est, True, False) is True


def test_eta_ausserhalb_des_fensters_schweigt():
    zu_frueh = (NOW + timedelta(minutes=95)).isoformat()
    zu_spaet = (NOW + timedelta(minutes=2)).isoformat()
    vorbei = (NOW - timedelta(minutes=10)).isoformat()
    assert FC.eta_one_hour_due(NOW, zu_frueh, True, False) is False
    assert FC.eta_one_hour_due(NOW, zu_spaet, True, False) is False
    assert FC.eta_one_hour_due(NOW, vorbei, True, False) is False


def test_eta_nur_einmal():
    est = (NOW + timedelta(minutes=40)).isoformat()
    assert FC.eta_one_hour_due(NOW, est, True, True) is False


# ══════════════════════════════════════════════════════════════════════════
# Landungs-Rückfall (wenn KEIN arrived-Event kam)
# ══════════════════════════════════════════════════════════════════════════

def test_landung_nur_bei_landed_bucket():
    dep_at = (NOW - timedelta(hours=2)).isoformat()
    assert FC.landed_confirmed_by_board('airborne', NOW, dep_at) is False
    assert FC.landed_confirmed_by_board(None, NOW, dep_at) is False
    assert FC.landed_confirmed_by_board('landed', NOW, dep_at) is True


def test_landung_braucht_belegten_abflug():
    assert FC.landed_confirmed_by_board('landed', NOW, None) is False


def test_landung_direkt_nach_abflug_ist_unglaubwuerdig():
    """Stale Vortags-Instanz derselben täglichen Flugnummer: das Board
    behauptet „gelandet", während die Maschine gerade erst weg ist."""
    dep_at = (NOW - timedelta(minutes=3)).isoformat()
    assert FC.landed_confirmed_by_board('landed', NOW, dep_at) is False


# ══════════════════════════════════════════════════════════════════════════
# Texte — keine erfundenen Werte, Schätzung ist als solche beschriftet
# ══════════════════════════════════════════════════════════════════════════

def test_texte_tragen_route_nur_wenn_bekannt():
    t, b = FC.build_message('departed', 'LH455', 'SFO', 'FRA')
    assert 'LH455' in t and 'SFO–FRA' in b
    t2, b2 = FC.build_message('departed', 'LH455', None, None)
    assert '–' not in b2 and 'None' not in b2


def test_eta_text_sagt_dass_es_eine_schaetzung_ist():
    _, b = FC.build_message('eta_1h', 'LH455', 'SFO', 'FRA')
    assert 'voraussichtlich' in b
    assert 'Geschätzt' in b


def test_texte_ohne_uhrzeit():
    """Eine Uhrzeit ohne Zonen-Bezug ist die teuerste Fehlerklasse des
    Projekts — in einem Push-Text ist für „Ortszeit <Stadt>" kein Platz."""
    for kind in ('departed', 'eta_1h', 'arrived'):
        _, b = FC.build_message(kind, 'LH455', 'SFO', 'FRA')
        assert ':' not in b


# ══════════════════════════════════════════════════════════════════════════
# Topic-Datum = LOKALES Abflugdatum am Startflughafen
# ══════════════════════════════════════════════════════════════════════════

def test_topic_datum_ist_lokal_am_start():
    """LH455 SFO→FRA hebt am 30.07. 23:00 UTC ab = 16:00 Ortszeit SFO.
    Das Broker-Topic keyt auf den 30.07., nicht auf den UTC-Tag."""
    assert FC.topic_date_for('2026-07-30T23:00:00Z', 'SFO',
                             '2026-07-31') == '2026-07-30'


def test_topic_datum_faellt_auf_client_datum_zurueck():
    assert FC.topic_date_for(None, None, '2026-07-31') == '2026-07-31'
    assert FC.topic_date_for(None, None, 'kaputt') is None


# ══════════════════════════════════════════════════════════════════════════
# Event-Fanout
# ══════════════════════════════════════════════════════════════════════════

def _row(**over):
    r = {'id': 1, 'user_token': 'AT-abc', 'flight_no': 'LH455',
         'flight_date': '2026-07-30', 'dep_iata': 'SFO', 'arr_iata': 'FRA',
         'sent': {}}
    r.update(over)
    return r


class _Recorder:
    def __init__(self):
        self.pushes = []
        self.marks = []

    def push(self, token, title, body, data=None, idempotency_key=None):
        self.pushes.append({'token': token, 'title': title, 'body': body,
                            'data': data or {}, 'key': idempotency_key})
        return 'outbox-1'


@pytest.fixture
def rec(monkeypatch):
    r = _Recorder()
    monkeypatch.setattr(FC, '_do_push', r.push)
    monkeypatch.setattr(FC, '_mark_sent',
                        lambda rid, sent, kind, now: r.marks.append((rid, kind)))
    return r


def test_departed_event_meldet_einmal(monkeypatch, rec):
    monkeypatch.setattr(FC, '_rows_for_flight', lambda f, d: [_row()])
    n = FC.notify_flight_event('departed', 'LH455', '2026-07-30', now_utc=NOW)
    assert n == 1
    assert rec.pushes[0]['data']['type'] == 'flight_departed'
    assert rec.pushes[0]['key'] == 'fcheck:LH455:2026-07-30:departed:AT-abc'
    assert rec.marks == [(1, 'departed')]


def test_bereits_gemeldetes_ereignis_pusht_nicht_erneut(monkeypatch, rec):
    monkeypatch.setattr(FC, '_rows_for_flight',
                        lambda f, d: [_row(sent={'departed': True})])
    assert FC.notify_flight_event('departed', 'LH455', '2026-07-30',
                                  now_utc=NOW) == 0
    assert rec.pushes == []


def test_nachbartag_bekommt_keine_fremde_meldung(monkeypatch, rec):
    """Tägliche Flugnummer: das Abo vom 29.07. darf beim Event des 30.07.
    nicht mitfeuern."""
    monkeypatch.setattr(FC, '_rows_for_flight',
                        lambda f, d: [_row(flight_date='2026-07-29')])
    assert FC.notify_flight_event('departed', 'LH455', '2026-07-30',
                                  now_utc=NOW) == 0
    assert rec.pushes == []


def test_nur_departed_und_arrived_melden(monkeypatch, rec):
    monkeypatch.setattr(FC, '_rows_for_flight', lambda f, d: [_row()])
    for kind in ('gate', 'est_dep', 'est_arr', 'schedule', 'other'):
        assert FC.notify_flight_event(kind, 'LH455', '2026-07-30',
                                      now_utc=NOW) == 0
    assert rec.pushes == []


def test_arrived_event_meldet_landung(monkeypatch, rec):
    monkeypatch.setattr(FC, '_rows_for_flight',
                        lambda f, d: [_row(sent={'departed': True})])
    assert FC.notify_flight_event('arrived', 'LH455', '2026-07-30',
                                  now_utc=NOW) == 1
    assert rec.pushes[0]['data']['type'] == 'flight_landed'


# ══════════════════════════════════════════════════════════════════════════
# Sweep — ohne Beleg passiert NICHTS
# ══════════════════════════════════════════════════════════════════════════

class _FakeTable:
    def __init__(self, rows, sink):
        self._rows = rows
        self._sink = sink

    def select(self, *a, **k):
        return self

    def delete(self):
        self._sink['deleted'] = True
        return self

    def update(self, patch):
        self._sink.setdefault('updates', []).append(patch)
        return self

    def eq(self, *a, **k):
        return self

    def lt(self, *a, **k):
        return self

    def in_(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        class _R:
            data = self._rows
        return _R()


class _FakeSB:
    def __init__(self, rows):
        self.rows = rows
        self.sink = {}

    def table(self, _name):
        return _FakeTable(self.rows, self.sink)


def test_sweep_ohne_fakten_meldet_nichts(monkeypatch, rec):
    """Der dokumentierte Owner-Fall: kein Status, keine geschätzte Ankunft.
    Der Sweep darf daraus NICHTS ableiten."""
    sb = _FakeSB([_row(sent={'departed': True,
                             'departed_at': (NOW - timedelta(hours=5)).isoformat()})])
    monkeypatch.setattr(FC, '_sb', lambda: sb)
    monkeypatch.setattr(FC, '_facts_for', lambda row: {'est_arr': None,
                                                       'bucket': None})
    stats = FC.sweep(now_utc=NOW)
    assert stats['eta'] == 0 and stats['landed'] == 0
    assert rec.pushes == []


def test_sweep_meldet_eta_bei_echter_schaetzung(monkeypatch, rec):
    sb = _FakeSB([_row(sent={'departed': True,
                             'departed_at': (NOW - timedelta(hours=5)).isoformat()})])
    monkeypatch.setattr(FC, '_sb', lambda: sb)
    monkeypatch.setattr(FC, '_facts_for', lambda row: {
        'est_arr': (NOW + timedelta(minutes=45)).isoformat(),
        'bucket': 'airborne'})
    stats = FC.sweep(now_utc=NOW)
    assert stats['eta'] == 1
    assert rec.pushes[0]['data']['type'] == 'flight_eta_1h'


def test_sweep_annonciert_niemals_den_abflug(monkeypatch, rec):
    """Ein Board-Status, der auf „airborne" springt, schaltet die
    Folge-Meldungen frei — er BEHAUPTET aber nie „ist abgeflogen". Diese
    Aussage bleibt dem MQTT-Event vorbehalten (der Board-Pfad ist am
    2026-07-31 nachweislich löchrig)."""
    sb = _FakeSB([_row(sent={})])
    monkeypatch.setattr(FC, '_sb', lambda: sb)
    monkeypatch.setattr(FC, '_facts_for', lambda row: {'est_arr': None,
                                                       'bucket': 'airborne'})
    FC.sweep(now_utc=NOW)
    assert [p['data']['type'] for p in rec.pushes] == []


# ══════════════════════════════════════════════════════════════════════════
# Endpoints — IDOR-Gate
# ══════════════════════════════════════════════════════════════════════════

def _client():
    A.app.config['TESTING'] = True
    return A.app.test_client()


# Tokens hier BEWUSST ohne das echte `AT-`-Präfix: sonst greift schon das
# globale before_request-Gate (_bug004_token_auth_gate) mit 401 und der Test
# würde gar nicht mehr das Bearer-Gate DIESER Endpoints prüfen.
def test_checkin_ohne_bearer_ist_verboten(monkeypatch):
    monkeypatch.setattr(FC, '_bearer_ok', lambda t: False)
    r = _client().post('/api/flight/checkin/tok-fremd',
                       json={'flight': 'LH455', 'date': '2026-07-30'})
    assert r.status_code == 403


def test_checkins_liste_ohne_bearer_ist_verboten(monkeypatch):
    monkeypatch.setattr(FC, '_bearer_ok', lambda t: False)
    r = _client().get('/api/flight/checkins/tok-fremd')
    assert r.status_code == 403


def test_checkin_ohne_flugnummer_ist_400(monkeypatch):
    monkeypatch.setattr(FC, '_bearer_ok', lambda t: True)
    r = _client().post('/api/flight/checkin/tok-abc', json={'flight': ''})
    assert r.status_code == 400


def test_push_typen_haengen_am_bestehenden_dienstplan_schalter():
    """Kein vierter Schalter (Owner-Regel): die drei neuen Typen müssen im
    bestehenden Pref-Mapping stehen, sonst wären sie unfilterbar."""
    for t in ('flight_departed', 'flight_eta_1h', 'flight_landed'):
        assert A._PUSH_TYPE_TO_PREF.get(t) == 'roster_change'
