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
# Texte — keine erfundenen Werte, kein Disclaimer-Anhang
# ══════════════════════════════════════════════════════════════════════════

def test_texte_tragen_route_nur_wenn_bekannt():
    t, b = FC.build_message('departed', 'LH455', 'SFO', 'FRA')
    assert 'LH455' in t and 'SFO–FRA' in b
    t2, b2 = FC.build_message('departed', 'LH455', None, None)
    assert '–' not in b2 and 'None' not in b2


def test_eta_text_bleibt_eine_aussage():
    """„landet voraussichtlich in etwa einer Stunde" IST die Schätzung."""
    _, b = FC.build_message('eta_1h', 'LH455', 'SFO', 'FRA')
    assert b == 'LH455 landet voraussichtlich in etwa einer Stunde in FRA.'


def test_kein_push_traegt_einen_unsicherheits_disclaimer():
    """Owner 02.08.2026, wörtlich: „keine bestätigte landung kann weg was für
    ein blöder hinweis.. auch bei den anderen pushes". Der Zusatz war doppelt
    überflüssig — die Meldung feuert ohnehin nur gegen eine ECHTE geschätzte
    Ankunft, und „voraussichtlich" sagt dasselbe schon."""
    verboten = ('Geschätzt', 'bestätigte Landung', 'keine bestätigte',
                'ohne Gewähr', 'unverbindlich')
    for kind in ('departed', 'eta_1h', 'arrived'):
        for via in (None, 'Julien'):
            t, b = FC.build_message(kind, 'LH455', 'SFO', 'FRA', via_name=via)
            for wort in verboten:
                assert wort not in b and wort not in t, (kind, wort)


def test_texte_ohne_uhrzeit():
    """Eine Uhrzeit ohne Zonen-Bezug ist die teuerste Fehlerklasse des
    Projekts — in einem Push-Text ist für „Ortszeit <Stadt>" kein Platz."""
    for kind in ('departed', 'eta_1h', 'arrived'):
        _, b = FC.build_message(kind, 'LH455', 'SFO', 'FRA')
        assert ':' not in b


# ══════════════════════════════════════════════════════════════════════════
# WESSEN Flug? (Tibor 02.08.2026 — „Landet bald · LH455" ohne Kontext)
# ══════════════════════════════════════════════════════════════════════════

def test_titel_nennt_den_menschen_ueber_dessen_bordkarte_eingecheckt_wurde():
    t, b = FC.build_message('eta_1h', 'LH455', 'SFO', 'FRA',
                            via_name='Julien')
    assert t == 'Juliens Flug · LH455'
    assert b.startswith('LH455 landet voraussichtlich')


def test_titel_ohne_namen_sagt_wenigstens_warum():
    """Alte App-Builds schicken keinen Namen — dann NICHT raten, sondern den
    Anlass nennen."""
    for kind in ('departed', 'eta_1h', 'arrived'):
        t, _ = FC.build_message(kind, 'LH455', 'SFO', 'FRA')
        assert t == 'Dein Check-in · LH455'


def test_alle_drei_meldungen_tragen_denselben_kontext():
    for kind in ('departed', 'eta_1h', 'arrived'):
        t, _ = FC.build_message(kind, 'LH455', 'SFO', 'FRA', via_name='Julien')
        assert t == 'Juliens Flug · LH455'


def test_genitiv_mit_zischlaut_bekommt_nur_den_apostroph():
    assert FC.possessive('Julien') == 'Juliens'
    assert FC.possessive('Lukas') == "Lukas'"
    assert FC.possessive('Max') == "Max'"
    assert FC.possessive(None) is None


def test_nur_der_rufname_landet_im_push():
    """Ein Nachname gehört nicht in einen Push — und „Julien K.s Flug" wäre
    kein deutscher Satz."""
    assert FC.clean_via_name('Julien K.') == 'Julien'
    assert FC.clean_via_name('  Julien   Meier ') == 'Julien'


def test_muell_wird_nie_zu_einem_namen():
    """Ein Token, eine Mail oder eine Nummer darf nie im Titel stehen."""
    for schrott in (None, '', '  ', 'A', 'AT-abc123', 'a@b.de', '12345',
                    '·', '...', 'x' * 40):
        assert FC.clean_via_name(schrott) is None
    assert FC.context_title('LH455', 'AT-abc123') == 'Dein Check-in · LH455'


def test_name_aus_der_abo_zeile_landet_im_titel(monkeypatch, rec):
    """Ende-zu-Ende über den Event-Fanout: der Name hängt am ABO, nicht am
    Ereignis."""
    monkeypatch.setattr(FC, '_rows_for_flight',
                        lambda f, d: [_row(via_name='Julien')])
    assert FC.notify_flight_event('departed', 'LH455', '2026-07-30',
                                  now_utc=NOW) == 1
    assert rec.pushes[0]['title'] == 'Juliens Flug · LH455'


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


# ══════════════════════════════════════════════════════════════════════════
# VORFALL 02.08.2026 — die Flugnummer ist NICHT die Instanz (LH455 SFO→FRA)
# ══════════════════════════════════════════════════════════════════════════
# Belegt in der Prod-DB: Tibors Abo trug flight_date 2026-08-02 und
# dep_iso 2026-08-02T21:40Z (Abflug SFO heute Abend). In airport_delay_obs lag
# eine FRA#ARR-Zeile mit date=2026-08-02, esti 10:27 Ortszeit, Status
# „Gelandet" — das ANKUNFTSDATUM der Instanz vom 01.08. Bei Rot-Augen-Fluegen
# ist das Ankunftsdatum systematisch Abflugdatum+1; der Board-Reader liest
# d-1/d/d+1 und bevorzugt den exakten Datums-Treffer, also die falsche
# Instanz. Ergebnis: „Landet bald" 07:31Z und „Gelandet" 08:39Z fuer einen
# Flug, der noch gar nicht gestartet war.

# Der echte Zeitpunkt des Vorfalls (UTC) und die echten Werte der Zeile.
UNFALL_NOW = datetime(2026, 8, 2, 8, 39, tzinfo=timezone.utc)
UNFALL_DEP = '2026-08-02T21:40:00+00:00'        # Juliens Abflug SFO, heute Abend
FREMDE_ANKUNFT = '2026-08-02T08:27:00+00:00'    # Landung der Instanz vom 01.08.


def _lh455_row(**over):
    r = _row(flight_date='2026-08-02', dep_iso=UNFALL_DEP, via_name='Julien')
    r.update(over)
    return r


def test_lh455_kein_push_fuer_eine_fremde_instanz(monkeypatch, rec):
    """DER Vorfall, Ende-zu-Ende durch den Sweep: das Board meldet eine
    Landung, Juliens Maschine steht aber noch in SFO. Erwartung: 0 Pushes."""
    sb = _FakeSB([_lh455_row()])
    monkeypatch.setattr(FC, '_sb', lambda: sb)
    monkeypatch.setattr(FC, '_facts_for', lambda row: {
        'est_arr': FREMDE_ANKUNFT, 'bucket': 'landed'})
    stats = FC.sweep(now_utc=UNFALL_NOW)
    assert rec.pushes == []
    assert stats['eta'] == 0 and stats['landed'] == 0
    assert stats['not_started'] == 1


def test_lh455_falsche_flags_werden_zurueckgesetzt(monkeypatch, rec):
    """Selbstheilung: die Zeile stand mit departed/eta_1h/arrived=true da —
    ohne Reset bekaeme Tibor fuer den ECHTEN Flug heute Abend nie wieder eine
    Meldung."""
    sb = _FakeSB([_lh455_row(sent={'departed': True, 'eta_1h': True,
                                   'arrived': True,
                                   'departed_at': '2026-08-02T07:12:35Z'})])
    monkeypatch.setattr(FC, '_sb', lambda: sb)
    monkeypatch.setattr(FC, '_facts_for', lambda row: {
        'est_arr': FREMDE_ANKUNFT, 'bucket': 'landed'})
    stats = FC.sweep(now_utc=UNFALL_NOW)
    assert stats['repaired'] == 1
    assert sb.sink['updates'][0]['sent'] == {}
    assert rec.pushes == []


def test_lh455_mqtt_arrived_der_vortags_instanz_meldet_nichts(monkeypatch, rec):
    """Zweiter Riegel: selbst wenn ein `arrived`-Event dieselbe Zeile traefe,
    darf es fuer einen Flug, der erst heute Abend startet, nichts ausloesen."""
    monkeypatch.setattr(FC, '_rows_for_flight', lambda f, d: [_lh455_row()])
    assert FC.notify_flight_event('arrived', 'LH455', '2026-08-02',
                                  now_utc=UNFALL_NOW) == 0
    assert FC.notify_flight_event('departed', 'LH455', '2026-08-02',
                                  now_utc=UNFALL_NOW) == 0
    assert rec.pushes == []


def test_nach_dem_eigenen_abflug_meldet_der_pfad_wieder(monkeypatch, rec):
    """Gegenprobe — der Riegel darf den ECHTEN Flug nicht mitnehmen. Sieben
    Stunden nach Juliens Abflug ist die Instanz unterwegs, die Ankunft passt
    zum Abflug, und die Meldung geht raus."""
    dep = datetime(2026, 8, 2, 21, 40, tzinfo=timezone.utc)
    spaeter = dep + timedelta(hours=7)
    sb = _FakeSB([_lh455_row(sent={'departed': True,
                                   'departed_at': dep.isoformat()})])
    monkeypatch.setattr(FC, '_sb', lambda: sb)
    monkeypatch.setattr(FC, '_facts_for', lambda row: {
        'est_arr': (spaeter + timedelta(minutes=45)).isoformat(),
        'bucket': 'airborne'})
    assert FC.sweep(now_utc=spaeter)['eta'] == 1
    assert rec.pushes[0]['title'] == 'Juliens Flug · LH455'


def test_eine_ankunft_vor_dem_eigenen_abflug_gehoert_nicht_mir():
    assert FC.arrival_fits_instance(FREMDE_ANKUNFT, UNFALL_DEP) is False
    ok = '2026-08-03T06:00:00+00:00'                  # ~8 h nach dem Abflug
    assert FC.arrival_fits_instance(ok, UNFALL_DEP) is True
    zu_spaet = '2026-08-03T20:00:00+00:00'            # +22 h, andere Rotation
    assert FC.arrival_fits_instance(zu_spaet, UNFALL_DEP) is False
    # Ohne Abflug-Instant (alte Zeilen) gibt es kein Urteil.
    assert FC.arrival_fits_instance(ok, None) is True
    assert FC.arrival_fits_instance(None, UNFALL_DEP) is False


def test_instanz_start_hat_eine_fruehstart_kulanz():
    dep = datetime(2026, 8, 2, 21, 40, tzinfo=timezone.utc)
    assert FC.instance_started(dep.isoformat(), UNFALL_NOW) is False
    assert FC.instance_started(dep.isoformat(),
                               dep - timedelta(minutes=30)) is True
    assert FC.instance_started(dep.isoformat(),
                               dep - timedelta(hours=5)) is False
    # Unbekannter Abflug: verhaelt sich wie bisher (kein Urteil).
    assert FC.instance_started(None, UNFALL_NOW) is True


def test_fakten_der_fremden_instanz_werden_eingedampft():
    roh = {'est_arr': FREMDE_ANKUNFT, 'bucket': 'landed'}
    assert FC.facts_for_instance(roh, UNFALL_DEP, UNFALL_NOW) == {
        'est_arr': None, 'bucket': None}


def test_landed_ohne_pruefbare_ankunft_wird_fallengelassen():
    """Der Bucket ist die einzige Angabe OHNE eigenen Zeitstempel — er kann
    sich nur an einer passenden Ankunftszeit ausweisen."""
    dep = '2026-08-02T00:10:00+00:00'
    now = datetime(2026, 8, 2, 6, 0, tzinfo=timezone.utc)
    # Landung behauptet, aber keine Ankunftszeit dazu:
    assert FC.facts_for_instance({'bucket': 'landed'}, dep, now)['bucket'] is None
    # Landung behauptet, Ankunft liegt aber noch in der Zukunft:
    spaet = {'bucket': 'landed', 'est_arr': '2026-08-02T09:00:00+00:00'}
    assert FC.facts_for_instance(spaet, dep, now)['bucket'] is None
    # Mit passender, vergangener Ankunft zaehlt sie:
    echt = {'bucket': 'landed', 'est_arr': '2026-08-02T05:30:00+00:00'}
    got = FC.facts_for_instance(echt, dep, now)
    assert got['bucket'] == 'landed' and got['est_arr'] == echt['est_arr']


def test_alte_zeilen_ohne_dep_iso_verhalten_sich_wie_bisher():
    """`dep_iso` ist nullable; ohne den Instant faellt kein Urteil."""
    roh = {'est_arr': FREMDE_ANKUNFT, 'bucket': 'landed'}
    assert FC.facts_for_instance(roh, None, UNFALL_NOW) == roh


class _ColumnMissingTable(_FakeTable):
    """PostgREST vor dem Schema-Reload: JEDER Select mit `via_name` scheitert."""

    def __init__(self, rows, sink):
        super().__init__(rows, sink)
        self._cols = ''

    def select(self, *a, **k):
        self._cols = a[0] if a else ''
        return self

    def execute(self):
        if 'via_name' in self._cols:
            raise RuntimeError(
                'column flight_checkins.via_name does not exist')
        return super().execute()


def test_fehlende_spalte_killt_die_meldungen_nicht(monkeypatch, rec):
    """Die fcm_token-Lehre vom 01.08.: Code, der eine neue Spalte liest, darf
    ohne die Migration nicht den ganzen Pfad still abschalten. Ohne Spalte
    laufen die Meldungen weiter — nur eben ohne Namen im Titel."""
    monkeypatch.setattr(FC, '_via_name_available', True)
    sb = _FakeSB([_row(sent={'departed': True,
                             'departed_at': (NOW - timedelta(hours=5)).isoformat()})])
    monkeypatch.setattr(sb, 'table',
                        lambda _n: _ColumnMissingTable(sb.rows, sb.sink))
    monkeypatch.setattr(FC, '_sb', lambda: sb)
    monkeypatch.setattr(FC, '_facts_for', lambda row: {
        'est_arr': (NOW + timedelta(minutes=45)).isoformat(),
        'bucket': 'airborne'})
    assert FC.sweep(now_utc=NOW)['eta'] == 1
    assert rec.pushes[0]['title'] == 'Dein Check-in · LH455'
    assert FC._via_name_available is False


def test_checkin_speichert_den_rufnamen(monkeypatch):
    """Der Client kennt den Namen (er zeigt die Bordkarte an) — der Server
    übernimmt ihn, rät ihn aber nie."""
    monkeypatch.setattr(FC, '_bearer_ok', lambda t: True)
    monkeypatch.setattr(FC, '_via_name_available', True)
    stored = {}

    class _T(_FakeTable):
        def upsert(self, row, **k):
            stored.update(row)
            return self

    sb = _FakeSB([])
    monkeypatch.setattr(sb, 'table', lambda _n: _T(sb.rows, sb.sink))
    monkeypatch.setattr(FC, '_sb', lambda: sb)
    r = _client().post('/api/flight/checkin/tok-abc',
                       json={'flight': 'LH455', 'date': '2026-07-30',
                             'via_name': 'Julien K.'})
    assert r.status_code == 200, r.get_json()
    assert stored['via_name'] == 'Julien'
    assert r.get_json()['via_name'] == 'Julien'


def test_push_typen_haengen_am_bestehenden_dienstplan_schalter():
    """Kein vierter Schalter (Owner-Regel): die drei neuen Typen müssen im
    bestehenden Pref-Mapping stehen, sonst wären sie unfilterbar."""
    for t in ('flight_departed', 'flight_eta_1h', 'flight_landed'):
        assert A._PUSH_TYPE_TO_PREF.get(t) == 'roster_change'
