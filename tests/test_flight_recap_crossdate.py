"""Rückblick-Karte: der eigene Roster schlägt die Board-Beobachtung.

OWNER-BEFUND 10.08.2026 (LH781 SIN→FRA). Die Karte zeigte:
    „7:57 h Flugzeit"   für einen Flug, der 12:20 h dauerte
    „AN 06:35 FRA"      obwohl um 06:14 gelandet wurde

Zwei Ursachen griffen ineinander:
  1. `_flight_obs_merged` sucht die Beobachtung auf DEM ANGEFRAGTEN TAG. Der
     Flug ging am 09.08. um 23:54 Ortszeit Singapur und landete am 10.08. — auf
     dem 09.08. fand der Resolver deshalb die Ankunft der Instanz von
     VORGESTERN und gar keinen Abflug.
  2. Ohne `block_time_min` fällt die App auf die Dauer der aufgezeichneten
     Flugspur zurück. Die hat über Asien Lücken ⇒ 7:57 statt 12:20.

Der Roster trägt die Wahrheit als UTC-INSTANTS und kennt den Tageswechsel
deshalb von selbst. Diese Datei hält fest, dass er gewinnt — und dass er es nur
mit echtem Beleg tut.
"""
import os
import sys

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as _app_beim_import  # noqa: E402,F401  (Blueprint-Registrierung)
from blueprints import aerox_data_blueprint as ax  # noqa: E402


def _app():
    """Das AKTUELL geladene app-Modul — nicht der Verweis vom Import.

    ⚠️ SUITE-FALLE (hier reproduziert): `test_calculation.py` importiert `app`
    absichtlich neu. Ein beim Import festgehaltener Modulverweis zeigt danach
    auf die ALTE Instanz, waehrend der Endpunkt ueber `_life_app` zur
    Laufzeit `sys.modules['app']` aufloest — also die NEUE. Ein Monkeypatch
    auf den alten Verweis landet dann ins Leere: solo gruen, in der Suite rot.
    Das ist dieselbe Klasse, die in diesem Repo schon einmal einen halben Tag
    gekostet hat (nicht restaurierte Modul-Stubs, alphabetisch frueher).
    """
    return sys.modules['app']


# Miguels echte Zahlen (Prod-Payload 10.08.2026).
SEKTOR = {'flight': 'LH781', 'from': 'SIN', 'to': 'FRA',
          'dep_iso': '2026-08-09T15:54:00Z',     # 23:54 Ortszeit Singapur
          'arr_iso': '2026-08-10T04:14:00Z',     # 06:14 Ortszeit Frankfurt
          'arr_measured': True}
BRIEFS = {'2026-08-09': {'ical_sectors': [SEKTOR]}}

# Was der Board-Resolver lieferte: Ankunft der VORTAGS-Instanz, kein Abflug.
OBS_FALSCH = {'dep_iata': 'SIN', 'arr_iata': 'FRA', 'delay_known': True,
              'delay_min': 0, 'cancelled': False,
              'esti_dep': None, 'esti_arr': '2026-08-09T06:35:00',
              'sched_dep': None, 'sched_arr': '2026-08-09T06:40:00'}


GESEHEN = {}


def _mock(monkeypatch, briefs=BRIEFS, obs=OBS_FALSCH):
    GESEHEN.clear()
    monkeypatch.setattr(_app(), '_ical_briefings_load', lambda t: dict(briefs),
                        raising=False)

    def _merge(*a, **k):
        GESEHEN.update(k)
        return dict(obs) if obs else None
    monkeypatch.setattr(_app(), '_flight_obs_merged', _merge, raising=False)
    ax._LIFECYCLE_MEMO.clear()


def _hole(client, token='testtoken-recap'):
    # Token-Bindung ist scharf (`AEROX_REQUIRE_TOKEN_BINDING`): owner-scoped
    # Routen brauchen den passenden Bearer, sonst 401.
    r = client.get('/api/ax/flight-recap/' + token
                   + '?flight_no=LH781&date=2026-08-09&dep_iata=SIN&arr_iata=FRA',
                   headers={'Authorization': 'Bearer ' + token})
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    return r.get_json()


# ── Der Owner-Fall, 1:1 ─────────────────────────────────────────────────────

def test_rueckflug_ueber_mitternacht_nimmt_die_roster_blockzeit(monkeypatch):
    _mock(monkeypatch)
    d = _hole(_app().app.test_client())
    assert d['block_time_min'] == 740, 'SIN→FRA sind 12:20 h, nicht 7:57.'


def test_angezeigte_zeiten_ergeben_die_angezeigte_dauer(monkeypatch):
    """Die Zahlen müssen zueinander passen. Im Owner-Screenshot stand eine
    Dauer über zwei Uhrzeiten, die sie nicht ergeben — das ist der Fehler, den
    ein Nutzer sofort sieht und der die ganze Karte unglaubwürdig macht."""
    _mock(monkeypatch)
    d = _hole(_app().app.test_client())
    dep, arr = ax._recap_utc(d['actual_dep']), ax._recap_utc(d['actual_arr'])
    assert dep is not None and arr is not None
    assert round((arr - dep).total_seconds() / 60) == d['block_time_min']
    # Und die Ankunft ist die ECHTE, nicht die der Vortags-Instanz.
    assert arr.isoformat().startswith('2026-08-10T04:14')


def test_ankunftstag_wird_an_den_resolver_durchgereicht(monkeypatch):
    """`_flight_obs_merged` kann Übernacht-Legs seit 16.07. korrekt binden — es
    braucht dafür `arr_date`. Der Rückblick hat ihn nie übergeben, deshalb griff
    die Ankunfts-Seite die Zeile der gestrigen Rotation. Ohne diese Weitergabe
    ist die Karte auch dann falsch, wenn die Blockzeit stimmt."""
    _mock(monkeypatch)
    _hole(_app().app.test_client())
    assert GESEHEN.get('arr_date') == '2026-08-10', \
        'Der Ankunftstag muss mitgehen, sonst sucht der Resolver am falschen Tag.'


def test_tagesflug_reicht_keinen_ankunftstag_durch(monkeypatch):
    """Landet der Flug am Abflugtag, bleibt `arr_date` None — sonst würde die
    Sonderbehandlung ohne Not auf jedem gewöhnlichen Leg laufen."""
    tags = dict(SEKTOR, dep_iso='2026-08-09T06:00:00Z',
                arr_iso='2026-08-09T09:00:00Z')
    _mock(monkeypatch, briefs={'2026-08-09': {'ical_sectors': [tags]}})
    _hole(_app().app.test_client())
    assert GESEHEN.get('arr_date') is None


# ── Die Grenzen: nur mit echtem Beleg ───────────────────────────────────────

def test_ohne_eigenen_roster_bleibt_alles_beim_alten(monkeypatch):
    """Kein Sektor ⇒ keine Korrektur. Die Board-Antwort ist dann das Beste, was
    wir haben — erfunden wird nichts."""
    _mock(monkeypatch, briefs={})
    d = _hole(_app().app.test_client())
    assert d['block_time_min'] is None
    assert d['actual_arr'] == '2026-08-09T06:35:00'


def test_naive_roster_zeiten_werden_nicht_verwendet(monkeypatch):
    """Ein Instant OHNE Zonen-Suffix ist nicht umrechenbar. Ihn als UTC zu
    lesen wäre geraten — dieselbe Fehlerklasse, die den Bug erzeugt hat."""
    naiv = dict(SEKTOR, dep_iso='2026-08-09T15:54:00', arr_iso='2026-08-10T04:14:00')
    _mock(monkeypatch, briefs={'2026-08-09': {'ical_sectors': [naiv]}})
    assert _hole(_app().app.test_client())['block_time_min'] is None


def test_fremder_sektor_bindet_nicht(monkeypatch):
    """Gleiche Flugnummer, andere Strecke (Folgesektor eines Umlaufs) darf die
    Zeiten NICHT liefern."""
    fremd = dict(SEKTOR, **{'from': 'FRA', 'to': 'JFK'})
    _mock(monkeypatch, briefs={'2026-08-09': {'ical_sectors': [fremd]}})
    assert _hole(_app().app.test_client())['block_time_min'] is None


def test_leg_am_folgetag_wird_auch_gefunden(monkeypatch):
    """LH keyt Nachtflüge mal auf den Abflug-, mal auf den Ankunftstag. Beide
    Tage müssen durchsucht werden, sonst greift der Fix genau bei der Hälfte
    der Nachtflüge nicht."""
    _mock(monkeypatch, briefs={'2026-08-10': {'ical_sectors': [SEKTOR]}})
    assert _hole(_app().app.test_client())['block_time_min'] == 740


# ── Der Cache darf keine fremden Zeiten ausliefern ──────────────────────────

def test_memo_trennt_die_nutzer(monkeypatch):
    """Die Antwort hängt am EIGENEN Roster — der prozessweite Memo-Cache muss
    deshalb den Token im Schlüssel tragen. Ohne das bekäme der Kollege auf
    demselben Flug fremde Zeiten serviert."""
    client = _app().app.test_client()
    _mock(monkeypatch)
    assert _hole(client, token='testtoken-user-a')['block_time_min'] == 740
    # Zweiter Nutzer, gleicher Flug, aber KEIN eigener Beleg.
    monkeypatch.setattr(_app(), '_ical_briefings_load', lambda t: {},
                        raising=False)
    assert _hole(client, token='testtoken-user-b')['block_time_min'] is None
