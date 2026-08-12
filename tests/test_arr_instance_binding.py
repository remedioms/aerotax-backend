"""DIE ANKUNFTS-SCHRANKE WAR EINSEITIG (Sweep-Befund 2026-08-09).

Auf der ABFLUG-Seite wurde die Asymmetrie am 08.08. behoben
(`_gate_facts_dep_against_leg`, Fenster [-6 h, +20 h]). Die ANKUNFTS-Seite hatte
seit dem 16.07. NUR eine Frueh-Schranke (`_OVERNIGHT_ARR_MARGIN_H = 6`) und gar
keine Spaet-Schranke — und ausserdem sahen die Facts-Gates nur EINE der drei
Quellen, aus denen `est_arr_iso` entsteht.

DIE ZWEI MELDUNGEN DES WAECHTERS (ops/hetzner/sweep_kalender.py, Regel R4),
Lauf 2026-08-08T21:43Z:

    R4 wrongday-est-arr-frueh: 2026-08-01 LH499 MEX->FRA
        est_arr=2026-07-31T13:00:02Z  sched_arr=2026-08-01T13:15:00Z   (-24,25 h)
    R4 wrongday-est-arr-spaet: 2026-08-04 LH755 BLR->FRA
        est_arr=2026-08-06T08:51:00+02:00  sched_arr=2026-08-05T07:05:00Z (+23,8 h)

BELEGE AUS DER PRODUKTION (lesend, 2026-08-08T21:56Z–22:05Z; kein Fixture-Raten):

  1. Die Roster-Sektoren selbst (`roster_snapshots.payload.tage[].ical_sectors`):
        {"to":"FRA","from":"MEX","flight":"LH499",
         "dep_iso":"2026-08-01T02:31:00Z","arr_iso":"2026-08-01T13:15:00Z"}
        {"to":"FRA","from":"BLR","flight":"LH755",
         "dep_iso":"2026-08-04T21:30:00Z","arr_iso":"2026-08-05T07:05:00Z",
         "sched_arr_iso":"2026-08-05T07:05:00Z"}

  2. LH755 — WOHER DER SPAETE WERT KAM. Beide Quellen des Enrichers lieferten
     denselben 24-h-Nachbarn (in Produktion nachgerechnet, gleiche Container):
        _flight_obs_merged('LH755', date='2026-08-05', 'BLR', 'FRA')
            -> esti_arr '2026-08-06T08:51:00+0200', sched_arr '2026-08-06T09:05:00'
        _flight_facts_from_obs('LH755', '2026-08-05', 'BLR', 'FRA')
            -> est_arr '2026-08-06T08:51:00+02:00',
               arr_status 'Gepäckausgabe beendet', arr_delay_min 0,
               arr_esti_changed_at '2026-08-06T07:05:02.34009+00:00'
     Die RICHTIGE Zeile lag daneben in derselben Tabelle:
        airport_delay_obs FRA#ARR date=2026-08-05 LH755
            sched '09:05'  esti '2026-08-05T09:37:00+0200'  (= +32 min)

  3. LH499 — WOHER DER FRUEHE WERT KAM. Nicht vom Board: der Merge lieferte
     korrekt `esti_arr 2026-08-01T15:15:00+0200`. Der gemeldete Wert steht
     woertlich im FR24-Bezahl-Cache (`ax_paid_call_cache`):
        Schluessel 'FN|LH499|2026-07-31' (geschrieben 2026-08-08T21:05:48Z)
            datetime_takeoff 2026-07-31T02:58:03Z
            datetime_landed  2026-07-31T13:00:02Z   reg D-ABYJ
        Schluessel 'FN|LH499|2026-08-01'
            datetime_takeoff 2026-08-01T02:56:23Z
            datetime_landed  2026-08-01T13:09:55Z   reg D-ABYN
     `_fr24_flight_by_number` baut aus dem Tag ein `flight_datetime_from/to`
     in UTC — beide Cache-Eintraege belegen das (Takeoff jeweils im UTC-Tag des
     Schluessels). Der Enricher gab dort aber `leg_date` mit, und das ist seit
     dem Betriebstag-Fix (2026-08-08) der Kalendertag der ABFLUG-STATION: fuer
     MEX (UTC-6) und Abflug 2026-08-01T02:31Z ist das der 31.07. FR24 lieferte
     also pflichtgemaess den Lauf des VORTAGES.

DREI LOECHER, DREI REPARATUREN:
  (a) `_gate_facts_arr_against_leg` bekommt die fehlende SPAET-Schranke.
  (b) `_gate_sector_est_arr` riegelt den fertigen Sektor-Wert ab — er deckt
      auch die zwei bis dahin voellig ungegateten Quellen (Board-Merge `m`,
      FR24-Eskalation NACH dem Facts-Gate).
  (c) Die FR24-Eskalation fragt den UTC-Tag des Soll-Abflugs statt des
      Stations-Betriebstags.

PFLICHT-GEGENPROBEN (Owner): eine ECHTE Verspaetung bleibt erhalten, eine
ECHTE Uebernacht-Ankunft am Folgetag bleibt erhalten, und ein Flug, der
WIRKLICH fliegt, bleibt „fliegt gerade".
"""
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as A  # noqa: E402
from blueprints import aerox_data_blueprint as AXD  # noqa: E402
from blueprints import crew_live_state as CLS  # noqa: E402
from blueprints import leg_status_gate as G  # noqa: E402

import _clock_freeze as CF  # noqa: E402


def _uhr(monkeypatch, when):
    """Alle Uhren auf einen ABSOLUTEN Zeitpunkt setzen (geteilte Mechanik aus
    `_clock_freeze`, nur mit eigenem Anker). Noetig, weil `_enrich_leg_delays`
    Legs ausserhalb von `heute ± Horizont` gar nicht erst anfasst — die
    Belege sind aber datiert und duerfen NICHT auf „jetzt" umgerechnet werden."""
    monkeypatch.setattr(CF, 'FROZEN_UTC', when)
    monkeypatch.setattr(CF, 'FROZEN_EPOCH', when.timestamp())
    monkeypatch.setattr(CF, 'FROZEN_DATE', when.date())
    CF.apply_frozen_clock(monkeypatch, app_module=A,
                          extra_modules=(sys.modules[__name__],))


# ═══ DIE ECHTEN WERTE (1:1 aus der Produktion, s. Modul-Docstring) ═════════
LH499_DEP = '2026-08-01T02:31:00Z'          # MEX, Ortstag 31.07. 20:31
LH499_ARR = '2026-08-01T13:15:00Z'
LH499_FR24_FALSCH = '2026-07-31T13:00:02Z'  # Landung des Vortages-Laufs
LH499_FR24_RICHTIG = '2026-08-01T13:09:55Z'
LH499_BOARD_ARR = '2026-08-01T13:15:00Z'    # esti 15:15+0200 -> UTC

LH755_DEP = '2026-08-04T21:30:00Z'
LH755_ARR = '2026-08-05T07:05:00Z'
LH755_EST_FALSCH = '2026-08-06T08:51:00+02:00'   # FRA#ARR-Zeile des 06.08.
LH755_EST_RICHTIG = '2026-08-05T09:37:00+02:00'  # FRA#ARR-Zeile des 05.08.


# ═══════════════════════════════════════════════════════════════════════════
# 1. DIE PURE REGEL — EIN Fenster, EIN Zahlenpaar
# ═══════════════════════════════════════════════════════════════════════════

def test_gemeldeter_frueher_fall_ist_eine_fremde_instanz():
    assert G.est_time_same_instance(LH499_FR24_FALSCH, LH499_ARR) is False


def test_gemeldeter_spaeter_fall_ist_eine_fremde_instanz():
    assert G.est_time_same_instance(LH755_EST_FALSCH, LH755_ARR) is False


def test_die_richtigen_werte_beider_faelle_bleiben():
    """GEGENPROBE: die tatsaechlich zum Leg gehoerenden Ist-Ankuenfte —
    beide aus derselben Produktion gelesen — muessen durchgehen."""
    assert G.est_time_same_instance(LH499_FR24_RICHTIG, LH499_ARR) is True
    assert G.est_time_same_instance(LH499_BOARD_ARR, LH499_ARR) is True
    assert G.est_time_same_instance(LH755_EST_RICHTIG, LH755_ARR) is True


def test_24h_nachbar_in_BEIDE_richtungen():
    """Der 24-h-Nachbar derselben taeglichen Nummer faellt auf der
    Ankunfts-Seite in beiden Richtungen heraus — genau die Symmetrie, die
    bis heute fehlte."""
    assert G.est_time_same_instance('2026-08-04T07:05:00Z', LH755_ARR) is False
    assert G.est_time_same_instance('2026-08-06T07:05:00Z', LH755_ARR) is False


@pytest.mark.parametrize('est,erwartet', [
    ('2026-08-05T06:05:00Z', True),    # 1 h ZU FRUEH gelandet — Rueckenwind
    ('2026-08-05T01:06:00Z', True),    # 5,98 h zu frueh — Randfall, toleriert
    ('2026-08-05T01:04:00Z', False),   # 6,02 h zu frueh — jenseits der Schranke
    ('2026-08-05T10:05:00Z', True),    # +3 h Verspaetung
    ('2026-08-05T19:05:00Z', True),    # +12 h Verspaetung (Technik + Crew-Ruhe)
    ('2026-08-06T03:04:00Z', True),    # +19,98 h — noch Verspaetung
    ('2026-08-06T03:06:00Z', False),   # +20,02 h — jenseits der Schranke
])
def test_instanz_fenster_schwellen_ankunft(est, erwartet):
    """Die Schranken sind exakt -6 h / +20 h — dieselben Zahlen wie auf der
    Abflug-Seite, nicht eigene."""
    assert G.est_time_same_instance(est, LH755_ARR) is erwartet


def test_regel_faellt_offen_aus_ohne_evidenz():
    assert G.est_time_same_instance(None, LH755_ARR) is True
    assert G.est_time_same_instance(LH755_EST_FALSCH, None) is True
    assert G.est_time_same_instance('kaputt', LH755_ARR) is True
    assert G.est_time_same_instance(LH755_EST_FALSCH, 'kaputt') is True


def test_es_gibt_genau_EIN_zahlenpaar_im_projekt():
    """Der eigentliche Auftrag: es duerfen nicht drei verschiedene Paare
    existieren. Abflug-Seite, Ankunfts-Seite und Positions-Riegel lesen
    dieselbe Definition."""
    assert (G.INSTANCE_EARLY_MARGIN_H, G.INSTANCE_LATE_MARGIN_H) == (6, 20)
    assert G.DEP_EARLY_MARGIN_H is G.INSTANCE_EARLY_MARGIN_H
    assert G.DEP_LATE_MARGIN_H is G.INSTANCE_LATE_MARGIN_H
    assert A._DEP_EARLY_MARGIN_H is G.INSTANCE_EARLY_MARGIN_H
    assert A._DEP_LATE_MARGIN_H is G.INSTANCE_LATE_MARGIN_H
    assert A._OVERNIGHT_ARR_MARGIN_H is G.INSTANCE_EARLY_MARGIN_H
    assert A._ARR_LATE_MARGIN_H is G.INSTANCE_LATE_MARGIN_H
    # Und der Positions-Riegel ist derselbe Mechanismus, nicht ein zweiter.
    assert G.live_pos_same_instance('2026-08-06T07:05:00Z', LH755_DEP) is False


# ═══════════════════════════════════════════════════════════════════════════
# 2. DAS FACTS-GATE — jetzt beidseitig
# ═══════════════════════════════════════════════════════════════════════════

def _facts_lh755(est_arr):
    """Die Facts-Struktur, die `_flight_facts_from_obs` in Produktion fuer
    LH755 lieferte (gekuerzt auf die entscheidenden Felder)."""
    return {'sched_dep': '2026-08-05T21:30:00+05:30', 'dep_status': 'Departed',
            'reg': 'DABTK', 'dep_delay_min': 0, 'dep_delay_known': True,
            'dep_iata': 'BLR', 'arr_iata': 'FRA',
            'sched_arr': '2026-08-06T09:05:00+02:00', 'est_arr': est_arr,
            'arr_terminal': '1', 'arr_status': 'Gepäckausgabe beendet',
            'arr_delay_min': 0, 'arr_delay_known': True,
            'arr_obs_at': '2026-08-06T10:18:02.51669+00:00',
            'arr_esti_changed_at': '2026-08-06T07:05:02.34009+00:00'}


def test_facts_gate_verwirft_die_spaete_fremd_rotation():
    """DER GEMELDETE SPAETE FALL. Vor dieser Aenderung lief er durch: das Gate
    kannte nur `est_arr < sched_arr - 6 h`."""
    f = A._gate_facts_arr_against_leg(_facts_lh755(LH755_EST_FALSCH), LH755_ARR)
    assert f['est_arr'] is None
    # Die ganze ARR-Seite geht mit — eine „0 min Verspaetung" aus einer
    # fremden Rotation ist genauso falsch wie deren Uhrzeit.
    for k in ('arr_delay_min', 'arr_status', 'arr_terminal',
              'arr_obs_at', 'arr_esti_changed_at'):
        assert f[k] is None, k
    # DEP-Seite und Identitaet bleiben unangetastet.
    assert f['reg'] == 'DABTK' and f['dep_status'] == 'Departed'
    assert f['dep_delay_min'] == 0


def test_facts_gate_verwirft_weiterhin_die_fruehe_fremd_rotation():
    """Der alte Fall (Tibor LH455-R4) darf durch die Umstellung nicht
    verloren gehen."""
    f = A._gate_facts_arr_against_leg(_facts_lh755('2026-08-04T09:37:00+02:00'),
                                      LH755_ARR)
    assert f['est_arr'] is None


def test_facts_gate_laesst_die_echte_ankunft_stehen():
    """GEGENPROBE: die richtige Zeile (+32 min) bleibt vollstaendig."""
    f = A._gate_facts_arr_against_leg(_facts_lh755(LH755_EST_RICHTIG), LH755_ARR)
    assert f['est_arr'] == LH755_EST_RICHTIG
    assert f['arr_status'] == 'Gepäckausgabe beendet'


def test_facts_gate_laesst_eine_echte_grosse_verspaetung_stehen():
    """PFLICHT-GEGENPROBE: +12 h sind eine Verspaetung, keine Fremd-Rotation."""
    f = A._gate_facts_arr_against_leg(_facts_lh755('2026-08-05T19:05:00Z'),
                                      LH755_ARR)
    assert f['est_arr'] == '2026-08-05T19:05:00Z'


# ═══════════════════════════════════════════════════════════════════════════
# 3. DER SEKTOR-RIEGEL AM AUSGANG — deckt die ungegateten Quellen
# ═══════════════════════════════════════════════════════════════════════════

def test_sektor_riegel_raeumt_die_fremde_ankunft_komplett():
    s = {'flight': 'LH755', 'from': 'BLR', 'to': 'FRA',
         'dep_iso': LH755_DEP, 'arr_iso': LH755_ARR,
         'est_arr_iso': LH755_EST_FALSCH, 'arr_delay_min': 0,
         'delay_known': True, 'delay_min': 0, 'delay_side': 'arr',
         'arr_measured': True}
    assert A._gate_sector_est_arr(s) is False
    assert s['est_arr_iso'] is None and s['arr_delay_min'] is None
    assert s['arr_measured'] is False and s['arr_wrong_instance'] is True
    assert s['delay_known'] is False and s['delay_side'] is None


def test_sektor_riegel_laesst_echte_uebernacht_ankunft_stehen():
    """PFLICHT-GEGENPROBE: LH755 landet planmaessig am FOLGETAG des Abflugs
    (dep 04.08. 21:30Z -> arr 05.08. 07:05Z). Der Riegel misst gegen die
    Soll-ANKUNFT dieses Legs, nicht gegen einen Kalendertag — die Ankunft am
    naechsten Tag muss unangetastet bleiben."""
    s = {'flight': 'LH755', 'from': 'BLR', 'to': 'FRA',
         'dep_iso': LH755_DEP, 'arr_iso': LH755_ARR,
         'est_arr_iso': LH755_EST_RICHTIG, 'arr_delay_min': 32,
         'delay_known': True, 'delay_min': 32, 'delay_side': 'arr'}
    assert A._gate_sector_est_arr(s) is True
    assert s['est_arr_iso'] == LH755_EST_RICHTIG and s['arr_delay_min'] == 32
    # Der Ankunftstag ist wirklich ein anderer als der Abflugtag.
    assert LH755_DEP[:10] != LH755_ARR[:10]


def test_sektor_riegel_ohne_evidenz_offen():
    s = {'arr_iso': None, 'est_arr_iso': LH755_EST_FALSCH}
    assert A._gate_sector_est_arr(s) is True
    assert s['est_arr_iso'] == LH755_EST_FALSCH


# ═══════════════════════════════════════════════════════════════════════════
# 4. END-TO-END — `_enrich_leg_delays`
# ═══════════════════════════════════════════════════════════════════════════

def _merge_lh755(esti_arr, sched_arr):
    """Der Rueckgabewert von `_flight_obs_merged`, wie in Produktion gemessen."""
    return {'delay_known': True, 'delay_min': 0, 'delay_side': 'arr',
            'dep_delay_min': 0, 'arr_delay_min': 0,
            'status': 'Gepäckausgabe beendet',
            'status_arr': 'Gepäckausgabe beendet', 'cancelled': False,
            'esti_dep': None, 'esti_arr': esti_arr,
            'sched_dep': '2026-08-05T21:30:00', 'sched_arr': sched_arr,
            'reg': 'DABTK', 'sides': {'dep': None, 'arr': None}}


@pytest.fixture
def _stille_umgebung(monkeypatch):
    """Kein Netz, keine Engine-Seiteneffekte, keine FR24-Kosten."""
    monkeypatch.delenv('FLIGHTSTATE_LIVE_FRIENDS', raising=False)
    monkeypatch.delenv('FLIGHTSTATE_SHADOW', raising=False)
    monkeypatch.setattr(AXD, '_nas_live_pos', lambda **kw: None, raising=False)
    monkeypatch.setattr(AXD, '_aircraft_live_pos',
                        lambda *a, **k: (None, None, None, None), raising=False)
    monkeypatch.setattr(AXD, '_fr24_flight_by_number', lambda *a, **k: None,
                        raising=False)
    A._FLIGHT_MERGE_CACHE.clear()
    yield
    A._FLIGHT_MERGE_CACHE.clear()


def test_e2e_lh755_der_gemeldete_spaete_fall(monkeypatch, _stille_umgebung):
    """DER GEMELDETE FALL, mit exakt den Werten, die beide Quellen in
    Produktion lieferten. Danach darf am Sektor KEINE Ist-Ankunft mehr haengen
    — lieber keine Zeile als der Lauf des Folgetages."""
    _uhr(monkeypatch, datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(A, '_flight_obs_merged',
                        lambda *a, **k: _merge_lh755(
                            '2026-08-06T08:51:00+0200', '2026-08-06T09:05:00'))
    monkeypatch.setattr(AXD, '_flight_facts_from_obs',
                        lambda *a, **k: _facts_lh755(LH755_EST_FALSCH),
                        raising=False)
    sec = {'flight': 'LH755', 'from': 'BLR', 'to': 'FRA',
           'dep_iso': LH755_DEP, 'arr_iso': LH755_ARR}
    A._enrich_leg_delays([sec], '2026-08-04', free_only=True)
    assert sec.get('est_arr_iso') is None, sec
    assert sec.get('arr_measured') is not True, sec


def test_e2e_lh755_gegenprobe_echte_verspaetung_bleibt(monkeypatch,
                                                       _stille_umgebung):
    """PFLICHT-GEGENPROBE mit der RICHTIGEN Zeile aus derselben Tabelle
    (FRA#ARR 05.08., esti 09:37+0200 = +32 min): die Verspaetung muss erhalten
    bleiben — inklusive Uhrzeit."""
    _uhr(monkeypatch, datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(A, '_flight_obs_merged',
                        lambda *a, **k: _merge_lh755(
                            '2026-08-05T09:37:00+0200', '2026-08-05T09:05:00'))
    monkeypatch.setattr(AXD, '_flight_facts_from_obs',
                        lambda *a, **k: _facts_lh755(LH755_EST_RICHTIG),
                        raising=False)
    sec = {'flight': 'LH755', 'from': 'BLR', 'to': 'FRA',
           'dep_iso': LH755_DEP, 'arr_iso': LH755_ARR}
    A._enrich_leg_delays([sec], '2026-08-04', free_only=True)
    assert sec.get('est_arr_iso') == LH755_EST_RICHTIG, sec


def test_e2e_lh499_fr24_wird_mit_dem_UTC_TAG_gefragt(monkeypatch,
                                                     _stille_umgebung):
    """URSACHE DES FRUEHEN FALLS: `_fr24_flight_by_number` baut ein UTC-Fenster
    (belegt durch beide Prod-Cache-Eintraege). Der Abflug liegt am
    2026-08-01T02:31Z — gefragt werden muss also der 01.08., nicht der MEX-
    Betriebstag 31.07."""
    gefragt = []

    _uhr(monkeypatch, datetime(2026, 8, 1, 18, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(A, '_flight_obs_merged', lambda *a, **k: {
        'delay_known': False, 'delay_min': None, 'delay_side': None,
        'dep_delay_min': None, 'arr_delay_min': None, 'status': None,
        'cancelled': False, 'esti_dep': None, 'esti_arr': None,
        'sched_dep': None, 'sched_arr': '2026-08-01T14:50:00', 'reg': None,
        'sides': {'dep': None, 'arr': None}})
    monkeypatch.setattr(AXD, '_flight_facts_from_obs', lambda *a, **k: {},
                        raising=False)

    def _fake_fr24(fn, day):
        gefragt.append(day)
        return None

    monkeypatch.setattr(AXD, '_fr24_flight_by_number', _fake_fr24,
                        raising=False)
    sec = {'flight': 'LH499', 'from': 'MEX', 'to': 'FRA',
           'dep_iso': LH499_DEP, 'arr_iso': LH499_ARR}
    A._enrich_leg_delays([sec], '2026-08-01', free_only=True)
    assert gefragt and gefragt[0] == '2026-08-01', gefragt
    # Der Stations-Betriebstag bleibt fuer die Board-Quellen richtig — nur
    # dieser eine Konsument rechnet in UTC.
    assert A._station_operating_day(LH499_DEP, 'MEX') == '2026-07-31'


def test_e2e_lh499_der_gemeldete_fruehe_fall(monkeypatch, _stille_umgebung):
    """Selbst wenn FR24 trotzdem den Vortages-Lauf liefert (alter Cache-
    Eintrag, verschobener Abflug), darf seine Landezeit nicht ans Leg."""
    _uhr(monkeypatch, datetime(2026, 8, 1, 18, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(A, '_flight_obs_merged', lambda *a, **k: {
        'delay_known': False, 'delay_min': None, 'delay_side': None,
        'dep_delay_min': None, 'arr_delay_min': None, 'status': None,
        'cancelled': False, 'esti_dep': None, 'esti_arr': None,
        'sched_dep': None, 'sched_arr': '2026-08-01T14:50:00', 'reg': None,
        'sides': {'dep': None, 'arr': None}})
    monkeypatch.setattr(AXD, '_flight_facts_from_obs', lambda *a, **k: {},
                        raising=False)
    monkeypatch.setattr(
        AXD, '_fr24_flight_by_number',
        lambda *a, **k: {'sched_arr': LH499_FR24_FALSCH, 'arr_iata': None,
                         'dep_iata': None},
        raising=False)
    sec = {'flight': 'LH499', 'from': 'MEX', 'to': 'FRA',
           'dep_iso': LH499_DEP, 'arr_iso': LH499_ARR}
    A._enrich_leg_delays([sec], '2026-08-01', free_only=True)
    assert sec.get('est_arr_iso') != LH499_FR24_FALSCH, sec


def test_e2e_lh499_gegenprobe_richtige_fr24_landung_bleibt(monkeypatch,
                                                           _stille_umgebung):
    """PFLICHT-GEGENPROBE: die ECHTE FR24-Landung dieses Legs
    (2026-08-01T13:09:55Z, D-ABYN) muss weiterhin ankommen."""
    _uhr(monkeypatch, datetime(2026, 8, 1, 18, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(A, '_flight_obs_merged', lambda *a, **k: {
        'delay_known': False, 'delay_min': None, 'delay_side': None,
        'dep_delay_min': None, 'arr_delay_min': None, 'status': None,
        'cancelled': False, 'esti_dep': None, 'esti_arr': None,
        'sched_dep': None, 'sched_arr': '2026-08-01T14:50:00', 'reg': None,
        'sides': {'dep': None, 'arr': None}})
    monkeypatch.setattr(AXD, '_flight_facts_from_obs', lambda *a, **k: {},
                        raising=False)
    monkeypatch.setattr(
        AXD, '_fr24_flight_by_number',
        lambda *a, **k: {'sched_arr': LH499_FR24_RICHTIG, 'arr_iata': None,
                         'dep_iata': None},
        raising=False)
    sec = {'flight': 'LH499', 'from': 'MEX', 'to': 'FRA',
           'dep_iso': LH499_DEP, 'arr_iso': LH499_ARR}
    A._enrich_leg_delays([sec], '2026-08-01', free_only=True)
    assert sec.get('est_arr_iso') == LH499_FR24_RICHTIG, sec


# ═══════════════════════════════════════════════════════════════════════════
# 5. AUFTRAG 2 — die zwei Aufrufer ohne Soll-Zeit
# ═══════════════════════════════════════════════════════════════════════════

# Der Positions-Snapshot des LH712-Vorfalls (Prod, 2026-08-08T21:09Z) — die
# gestrige Maschine, die am morgigen Leg klebte.
LH712_LEG_MORGEN = '2026-08-09T13:35:00Z'
LH712_LEG_HEUTE = '2026-08-08T13:35:00Z'
LH712_SEEN = '2026-08-08T21:01:09+00:00'


def _pos(seen_ts=LH712_SEEN):
    return {'lat': 43.63197, 'lon': 87.03042, 'track': 77, 'gs': 579,
            'alt': 39100, 'on_ground': False, 'source': 'aircraft_live',
            'seen_ts': seen_ts, 'callsign': 'DLH712'}


def test_build_live_lookup_reicht_die_sollzeit_durch(monkeypatch):
    """`crew_live_state.build_live_lookup` gab die Soll-Zeit bisher NICHT mit —
    der Store lief also ungebunden."""
    gesehen = {}

    def _fake(reg=None, flight=None, callsign=None, dep=None, max_age_min=35,
              sched_dep_iso=None):
        gesehen['sched'] = sched_dep_iso
        if not G.live_pos_same_instance(LH712_SEEN, sched_dep_iso):
            return None, None, None, None
        return _pos(), ('FRA', 'ICN'), 'D-AIXB', '748'

    monkeypatch.setattr(AXD, '_aircraft_live_pos', _fake)
    monkeypatch.setattr(AXD, '_free_crew_live_pos',
                        lambda *a, **k: (None, None, None, None))
    lk = CLS.build_live_lookup()

    assert lk('LH712', 'FRA', 'ICN', None, LH712_LEG_MORGEN) is None
    assert gesehen['sched'] == LH712_LEG_MORGEN
    # GEGENPROBE: das Leg, das wirklich fliegt, behaelt seine Position.
    out = lk('LH712', 'FRA', 'ICN', None, LH712_LEG_HEUTE)
    assert out and out['lat'] == 43.63197 and out['on_ground'] is False


def test_build_live_lookup_ohne_sollzeit_unveraendert(monkeypatch):
    """Fail-open: Alt-Aufrufer ohne Soll-Zeit sehen exakt das alte Verhalten."""
    monkeypatch.setattr(
        AXD, '_aircraft_live_pos',
        lambda **kw: (_pos(), ('FRA', 'ICN'), 'D-AIXB', '748'))
    monkeypatch.setattr(AXD, '_free_crew_live_pos',
                        lambda *a, **k: (None, None, None, None))
    out = CLS.build_live_lookup()('LH712', 'FRA', 'ICN')
    assert out and out['lat'] == 43.63197


def test_build_live_lookup_gated_auch_den_gRPC_fill(monkeypatch):
    """Der gRPC-Nachschlag (`_free_crew_live_pos`) keyt ebenfalls ohne Datum
    und liefert notfalls den LKG-Fix der Vor-Instanz — er braucht denselben
    Riegel."""
    monkeypatch.setattr(AXD, '_aircraft_live_pos',
                        lambda **kw: (None, None, None, None))
    monkeypatch.setattr(
        AXD, '_free_crew_live_pos',
        lambda *a, **k: (_pos(), ('FRA', 'ICN'), 'D-AIXB', '748'))
    lk = CLS.build_live_lookup()
    assert lk('LH712', 'FRA', 'ICN', None, LH712_LEG_MORGEN) is None
    assert lk('LH712', 'FRA', 'ICN', None, LH712_LEG_HEUTE) is not None


def test_crew_live_pos_refreshes_stale_store_with_newer_free_fix(monkeypatch):
    """LH732-Repro: ein vorhandener 12-min-LKG darf den gezielten kostenlosen
    Korridor nicht mehr blockieren. Nur der nachweislich neuere Fix gewinnt."""
    old = '2026-08-12T20:30:00Z'
    fresh = '2026-08-12T20:41:00Z'
    monkeypatch.setattr(AXD, '_pos_is_stale', lambda pos, minutes: True)
    monkeypatch.setattr(
        AXD, '_aircraft_live_pos',
        lambda **kw: ({**_pos(old), 'lat': 48.20, 'lon': 16.37},
                      ('FRA', 'PVG'), 'D-AIXF', 'A359'))
    calls = []

    def _free(*args):
        calls.append(args)
        return ({**_pos(fresh), 'lat': 47.31, 'lon': 20.16,
                 'source': 'fr24_grpc_corridor'},
                ('FRA', 'PVG'), 'D-AIXF', 'A359')

    monkeypatch.setattr(AXD, '_free_crew_live_pos', _free)

    out = AXD._crew_live_pos_free_first(
        'LH732', 'FRA', 'PVG', reg='D-AIXF', sched_dep_iso=None)

    assert calls == [('LH732', 'FRA', 'PVG')]
    assert out[0]['lat'] == 47.31
    assert out[0]['seen_ts'] == fresh
    assert out[0]['source'] == 'fr24_grpc_corridor'


def test_crew_live_pos_keeps_store_when_free_fix_is_older(monkeypatch):
    """Der Nachschlag ist fail-soft: ein alter Korridor-LKG teleportiert die
    Crew nie rueckwaerts, auch wenn der primaere Fix den Refresh ausloeste."""
    primary = {**_pos('2026-08-12T20:35:00Z'), 'lat': 48.0, 'lon': 17.0}
    older = {**_pos('2026-08-12T20:20:00Z'), 'lat': 50.0, 'lon': 10.0}
    monkeypatch.setattr(AXD, '_pos_is_stale', lambda pos, minutes: True)
    monkeypatch.setattr(
        AXD, '_aircraft_live_pos',
        lambda **kw: (primary, ('FRA', 'PVG'), 'D-AIXF', 'A359'))
    monkeypatch.setattr(
        AXD, '_free_crew_live_pos',
        lambda *a: (older, ('FRA', 'PVG'), 'D-AIXF', 'A359'))

    out = AXD._crew_live_pos_free_first('LH732', 'FRA', 'PVG')

    assert out[0]['lat'] == 48.0
    assert out[0]['seen_ts'] == primary['seen_ts']


def test_crew_live_pos_fresh_store_avoids_corridor_call(monkeypatch):
    """Kosten-/Last-Riegel: ein belegbar frischer Store-Fix bleibt O(1) und
    startet keinen zusaetzlichen kostenlosen gRPC-Aufruf."""
    primary = {**_pos('2026-08-12T20:41:30Z'), 'lat': 47.5, 'lon': 19.0}
    monkeypatch.setattr(AXD, '_pos_is_stale', lambda pos, minutes: False)
    monkeypatch.setattr(
        AXD, '_aircraft_live_pos',
        lambda **kw: (primary, ('FRA', 'PVG'), 'D-AIXF', 'A359'))
    monkeypatch.setattr(
        AXD, '_free_crew_live_pos',
        lambda *a: pytest.fail('fresh store must not call corridor'))

    out = AXD._crew_live_pos_free_first('LH732', 'FRA', 'PVG')

    assert out[0]['lat'] == 47.5


def test_resolver_gibt_den_soll_abflug_an_den_lookup(monkeypatch):
    """Der Resolver kennt den Soll-Abflug (`_norm_legs` macht daraus eine
    aware-UTC-Zeit) und muss ihn weiterreichen — sonst nuetzt der Riegel im
    Adapter nichts."""
    gesehen = {}

    def _lookup(flight_no, dep_iata, arr_iata, reg=None, sched_dep_iso=None):
        gesehen['sched'] = sched_dep_iso
        return None

    secs = [{'from': 'FRA', 'to': 'ICN', 'flight': 'LH712',
             'dep_iso': LH712_LEG_MORGEN, 'arr_iso': '2026-08-10T00:55:00Z'}]
    CLS.resolve_crew_live_state(
        secs, lambda *a, **k: None, _lookup,
        datetime(2026, 8, 8, 21, 9, 55, tzinfo=timezone.utc))
    assert gesehen.get('sched') is not None
    assert G.live_pos_same_instance(LH712_SEEN, gesehen['sched']) is False


def test_resolver_faellt_auf_alte_signaturen_zurueck():
    """Alt-Callsites/Test-Doubles mit 3 oder 4 Parametern duerfen nicht
    zerbrechen."""
    for _lk in (lambda f, d, a: {'lat': 1.0, 'lon': 2.0, 'on_ground': False},
                lambda f, d, a, r=None: {'lat': 1.0, 'lon': 2.0,
                                         'on_ground': False}):
        CLS.resolve_crew_live_state(
            [{'from': 'FRA', 'to': 'ICN', 'flight': 'LH712',
              'dep_iso': LH712_LEG_HEUTE, 'arr_iso': '2026-08-09T00:55:00Z'}],
            lambda *a, **k: None, _lk,
            datetime(2026, 8, 8, 21, 9, 55, tzinfo=timezone.utc))


def test_flug_detail_aggregat_bindet_live_an_die_sollzeit(monkeypatch):
    """Der Flug-Detail-Aggregat-Pfad sperrte live NUR fuer Abfragen in die
    VERGANGENHEIT (`_live_past`). Nach vorn stand das Tor offen — genau dort
    sitzt der Fehler (die Maschine von HEUTE haengt am Leg von MORGEN).
    `resolve_flight.sched_dep` liegt vor (Prod-Probe: '2026-08-08T15:35:00+02:00')
    und muss durchgereicht werden."""
    # Zeitstabil: der Test wurde ursprünglich mit dem nahen Datum 09.08.2026
    # geschrieben. Ab 10.08. wurde daraus eine absichtliche Vergangenheits-
    # Abfrage, für die das Produkt Live-Daten korrekt komplett überspringt.
    # Ein fernes Zukunftsdatum hält genau die eigentlich geprüfte Vorwärts-
    # Instanzbindung dauerhaft aktiv.
    future_date = '2099-08-09'
    future_sched = '2099-08-09T15:35:00+02:00'
    gesehen = {}

    def _fake_live(reg=None, flight=None, callsign=None, dep=None,
                   max_age_min=35, sched_dep_iso=None):
        gesehen['sched'] = sched_dep_iso
        if not G.live_pos_same_instance(LH712_SEEN, sched_dep_iso):
            return None, None, None, None
        return _pos(), ('FRA', 'ICN'), 'D-AIXB', '748'

    def _fake_subcall(app_obj, path, view_fn, *args):
        if '/resolve-flight/' in path or '/resolve-callsign/' in path:
            return {'ok': True, 'flight': {
                'flight': 'LH712', 'callsign': 'DLH712',
                'dep_iata': 'FRA', 'arr_iata': 'ICN', 'reg': 'D-AIXB',
                'sched_dep': future_sched,
                'sched_arr': '2099-08-10T09:55:00+09:00'}}
        return None

    monkeypatch.setattr(AXD, '_aircraft_live_pos', _fake_live)
    monkeypatch.setattr(AXD, '_detail_subcall', _fake_subcall)
    monkeypatch.setattr(AXD, '_route_history_windowed', lambda *a, **k: None)
    monkeypatch.setattr(AXD, '_ax_rate_limited', lambda *a, **k: False)
    monkeypatch.setattr(AXD, '_flight_times_free_first', lambda *a, **k: None,
                        raising=False)

    with A.app.test_request_context('/api/ax/flight-detail/LH712'
                                    f'?date={future_date}&fresh=1'):
        AXD.ax_flight_detail('LH712')
    # Der Soll-Abflug des ANGEFRAGTEN Tages kommt am Store an …
    assert gesehen.get('sched') == future_sched
    # … und der Snapshot der heutigen Maschine faellt damit heraus.
    assert G.live_pos_same_instance(LH712_SEEN, gesehen['sched']) is False
