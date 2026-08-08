"""EIN POSITIONS-SNAPSHOT GEHOERT ZU GENAU EINER TAGES-INSTANZ
(Sweep-Befund 2026-08-09, LH712 FRA->ICN).

DER DRITTE, EIGENE 24-h-NACHBAR-WEG — weder der Betriebstag-Schluessel
(`_station_operating_day`) noch der stale-Vertrag von
`_flight_facts_from_obs` beruehren ihn.

MELDUNG DES WAECHTERS (ops/hetzner/sweep_kalender.py, Regel R2b):
    R2b zukunft-airborne: AT-FACFEB... 2026-08-09 LH712 FRA->ICN
    status='airborne'

BELEG, PRODUKTION, gelesen 2026-08-08T21:09:55Z (nicht Fixture-geraten):

  1. Der Sektor aus `/api/user/friend-roster` (Auszug, unveraendert):
        flight LH712  FRA->ICN
        dep_iso  2026-08-09T13:35:00Z      arr_iso 2026-08-10T00:55:00Z
        est_dep_iso null   est_arr_iso null   delay_known false   reg null
        obs_sides {"dep": null, "arr": null}
        phase "AIRBORNE"   phase_conf "observed"   status "airborne"
     Der Abflug lag 16,6 h in der ZUKUNFT. Alle Zeit-, Delay- und
     Identitaets-Felder waren sauber — vergiftet war NUR der Status. Genau
     deshalb sahen die heute (2026-08-08) reparierten Wege nichts davon:
     `obs_sides` beide None heisst, die Board-Seite hat gar nichts geliefert.

  2. `aircraft_live` zum selben Zeitpunkt (die einzige Quelle, die eine
     `phase_conf='observed'` erzeugen kann — eine echte Positions-Beobachtung):
        flight LH712  callsign DLH712  reg_display D-AIXB
        origin FRA  dest ICN  lat 43.63197  lon 87.03042
        alt_ft 39100  gs_kt 579  on_ground false
        seen_ts 2026-08-08T21:01:09+00:00
     Das ist die Instanz von GESTERN, ueber Xinjiang, mitten im Reiseflug.

URSACHE: `aircraft_live` traegt KEIN Datum. Die Zeile beantwortet nur „welche
Maschine fliegt die Nummer X gerade nach Y". Bei einer TAEGLICH fliegenden
Langstrecke (Blockzeit ~11 h) ist die gestrige Maschine zum Abfragezeitpunkt
noch in der Luft und matcht die Zeile von MORGEN punktgenau — Flugnummer
stimmt, Ziel stimmt, Snapshot ist frisch. Ueber die FlightState-Engine (T3:
Position + Kinematik ⇒ AIRBORNE, conf=observed) wurde daraus ein
`status='airborne'` an einem Flug, der noch gar nicht gestartet war.

WARUM DER VORHANDENE RIEGEL NICHT GRIFF: `app._obs_dep_same_instance` prueft
BOARD-/WAREHOUSE-Rows anhand ihrer Spalten `date` und `sched`. Ein
Positions-Snapshot hat beide nicht — er lief nie durch diesen Riegel, sondern
ueber `_aircraft_live_pos` an ihm vorbei. Und `leg_status_gate.gated_leg_status`
raeumt ausschliesslich einen physikalisch unmoeglichen TERMINALEN Status
('landed'); nicht-terminale Status wie 'airborne' laufen dort bewusst
unveraendert durch.

REICHWEITE (Prod-Messung, gleicher Zeitpunkt): von 307 gespeicherten
Zukunfts-Sektoren im Anreicherungs-Fenster (Abflug > jetzt+1 h, < jetzt+27 h)
hatten 38 einen frischen `aircraft_live`-Treffer derselben Flugnummer mit
demselben Ziel — u.a. LH414 MUC->IAD, LH458 MUC->SFO, LH716 FRA->HND,
LH780 FRA->SIN, DE2454 FRA->YVR, LX139 HKG->ZRH. Kein Einzelfall.

FIX: `_aircraft_live_pos(..., sched_dep_iso=...)` bindet den Snapshot an die
Instanz — `seen_ts` muss in [dep-6 h, dep+20 h] liegen
(`leg_status_gate.live_pos_same_instance`, DIESELBEN Schwellen wie
`_gate_facts_dep_against_leg` / `_lh_facts_same_instance`). Ohne
`sched_dep_iso` bleibt alles wie bisher (fail-open).

GEGENPROBE (Pflicht, Owner-Regel „fliegt gerade muss stimmen"): ein Flug, der
WIRKLICH in der Luft ist, muss weiterhin 'airborne' tragen. Ein Fix, der echte
Live-Zustaende unterdrueckt, waere schlechter als der Fehler.
"""
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as A  # noqa: E402
from blueprints import aerox_data_blueprint as AXD  # noqa: E402
from blueprints import flight_state as FS  # noqa: E402
from blueprints import leg_status_gate as G  # noqa: E402

import _clock_freeze as CF  # noqa: E402


# ── Der Messzeitpunkt des Vorfalls, sekundengenau wie gelesen ───────────────
VORFALL_UTC = datetime(2026, 8, 8, 21, 9, 55, tzinfo=timezone.utc)

# ── Die echten Werte ────────────────────────────────────────────────────────
LEG_MORGEN_DEP = '2026-08-09T13:35:00Z'      # der beanstandete Sektor
LEG_MORGEN_ARR = '2026-08-10T00:55:00Z'
LEG_HEUTE_DEP = '2026-08-08T13:35:00Z'       # die Instanz, die WIRKLICH fliegt
LEG_HEUTE_ARR = '2026-08-09T00:55:00Z'
SNAPSHOT_SEEN = '2026-08-08T21:01:09+00:00'


@pytest.fixture(autouse=True)
def _freeze_am_vorfall(monkeypatch):
    """Uhr auf den Vorfalls-Zeitpunkt — die geteilte Mechanik aus
    `_clock_freeze`, nur mit einem anderen Anker (die Frozen-Klassen lesen die
    Modul-Globals zur AUFRUF-Zeit, ein Patch darauf wirkt also durch)."""
    monkeypatch.setattr(CF, 'FROZEN_UTC', VORFALL_UTC)
    monkeypatch.setattr(CF, 'FROZEN_EPOCH', VORFALL_UTC.timestamp())
    monkeypatch.setattr(CF, 'FROZEN_DATE', VORFALL_UTC.date())
    CF.apply_frozen_clock(monkeypatch, app_module=A,
                          extra_modules=(sys.modules[__name__],))
    yield


@pytest.fixture(autouse=True)
def _saubere_memos():
    A._FLIGHT_MERGE_CACHE.clear()
    FS._PRIOR_STORE.clear()
    yield
    A._FLIGHT_MERGE_CACHE.clear()
    FS._PRIOR_STORE.clear()


def _snapshot(seen_ts=SNAPSHOT_SEEN):
    """1:1 die Prod-Zeile aus `aircraft_live` (D-AIXB ueber Xinjiang)."""
    return {'lat': 43.63197, 'lon': 87.03042, 'track': 77,
            'gs': 579, 'alt': 39100, 'on_ground': False,
            'source': 'aircraft_live', 'seen_ts': seen_ts,
            'callsign': 'DLH712'}


# ═══════════════════════════════════════════════════════════════════════════
# 1. DIE PURE REGEL
# ═══════════════════════════════════════════════════════════════════════════

def test_vorfall_snapshot_gehoert_nicht_zum_morgigen_leg():
    """Der Vorfall, auf die reine Regel reduziert: der Snapshot von 21:01 Z am
    08.08. kann nicht zum Leg mit Soll-Abflug 09.08. 13:35 Z gehoeren."""
    assert G.live_pos_same_instance(SNAPSHOT_SEEN, LEG_MORGEN_DEP) is False


def test_echt_fliegender_flug_bleibt_seine_eigene_instanz():
    """GEGENPROBE der puren Regel: derselbe Snapshot am Leg, das ihn erzeugt
    hat (Abflug 08.08. 13:35 Z, seit 7,4 h unterwegs) — muss durchgehen."""
    assert G.live_pos_same_instance(SNAPSHOT_SEEN, LEG_HEUTE_DEP) is True


@pytest.mark.parametrize('seen,erwartet', [
    ('2026-08-08T13:40:00Z', True),    # 5 min nach Abflug — Steigflug
    ('2026-08-08T23:50:00Z', True),    # 10,25 h spaeter — noch derselbe Flug
    ('2026-08-08T08:35:00Z', True),    # 5 h VOR Plan — Randfall, toleriert
    ('2026-08-08T07:34:00Z', False),   # 6,02 h vor Plan — jenseits der Schranke
    ('2026-08-09T09:36:00Z', False),   # 20,02 h danach — jenseits der Schranke
])
def test_instanz_fenster_schwellen(seen, erwartet):
    """Die Schranken sind exakt -6 h / +20 h — dieselben Zahlen, die
    `_gate_facts_dep_against_leg` benutzt, nicht eigene."""
    assert G.live_pos_same_instance(seen, LEG_HEUTE_DEP) is erwartet


def test_24h_nachbar_in_BEIDE_richtungen():
    """Der 24-h-Nachbar derselben taeglichen Flugnummer faellt in beiden
    Richtungen heraus — vorwaerts (Vortags-Maschine am morgigen Leg, der
    gemeldete Fall) wie rueckwaerts (heutige Maschine am gestrigen Leg, der
    Fall der Freunde-Live-Karte)."""
    assert G.live_pos_same_instance(
        '2026-08-08T15:00:00Z', '2026-08-09T13:35:00Z') is False   # -24 h
    assert G.live_pos_same_instance(
        '2026-08-09T15:00:00Z', '2026-08-08T13:35:00Z') is False   # +24 h


def test_regel_faellt_offen_aus_ohne_evidenz():
    """Keine Evidenz ⇒ altes Verhalten. Es wird nie eine Position wegen
    fehlender/kaputter Metadaten verworfen (Owner-Regel: nichts raten)."""
    assert G.live_pos_same_instance(None, LEG_MORGEN_DEP) is True
    assert G.live_pos_same_instance(SNAPSHOT_SEEN, None) is True
    assert G.live_pos_same_instance('kaputt', LEG_MORGEN_DEP) is True
    assert G.live_pos_same_instance(SNAPSHOT_SEEN, 'kaputt') is True


def test_margen_sind_projektweit_dieselben():
    """Eine Quelle fuer die Schwellen — sonst driften drei Zahlen auseinander."""
    assert (G.DEP_EARLY_MARGIN_H, G.DEP_LATE_MARGIN_H) == (6, 20)
    assert A._DEP_EARLY_MARGIN_H is G.DEP_EARLY_MARGIN_H
    assert A._DEP_LATE_MARGIN_H is G.DEP_LATE_MARGIN_H


# ═══════════════════════════════════════════════════════════════════════════
# 2. DIE QUELLE — `_aircraft_live_pos`
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def _store(monkeypatch):
    """Der NAS-/Supabase-Store antwortet mit der echten Vorfalls-Zeile."""
    monkeypatch.setattr(
        AXD, '_nas_live_pos',
        lambda **kw: (_snapshot(), ('FRA', 'ICN'), 'D-AIXB', '748'))
    yield


def test_quelle_verwirft_den_nachbartags_snapshot(_store):
    pos, route, reg, _ty = AXD._aircraft_live_pos(
        flight='LH712', dep='ICN', sched_dep_iso=LEG_MORGEN_DEP)
    assert (pos, route, reg) == (None, None, None)


def test_quelle_liefert_den_eigenen_snapshot(_store):
    """GEGENPROBE: fuer das Leg, das gerade fliegt, kommt die Position (samt
    Reg und Route) unveraendert an — der Riegel darf nichts Echtes fressen."""
    pos, route, reg, ty = AXD._aircraft_live_pos(
        flight='LH712', dep='ICN', sched_dep_iso=LEG_HEUTE_DEP)
    assert pos is not None and pos['alt'] == 39100 and pos['on_ground'] is False
    assert route == ('FRA', 'ICN') and reg == 'D-AIXB' and ty == '748'


def test_quelle_ohne_sollzeit_unveraendert(_store):
    """Alt-Aufrufer ohne `sched_dep_iso` bekommen exakt das bisherige
    Verhalten — der Riegel ist opt-in, nicht global."""
    pos, route, reg, _ty = AXD._aircraft_live_pos(flight='LH712', dep='ICN')
    assert pos is not None and route == ('FRA', 'ICN') and reg == 'D-AIXB'


def test_quelle_gated_auch_den_supabase_pfad(monkeypatch):
    """Der Riegel sitzt an der GEMEINSAMEN Stelle: auch ohne NAS (reiner
    Supabase-Fallback) faellt der Nachbartag heraus."""
    monkeypatch.setattr(AXD, '_nas_live_pos', lambda **kw: None)

    class _Q:
        def __init__(self, rows):
            self.rows = rows
            self.col = self.val = None

        def select(self, *a, **k):
            return self

        def eq(self, col, val):
            if col in ('flight', 'callsign', 'reg'):
                self.col, self.val = col, val
            return self

        def gt(self, *a, **k):
            return self

        def limit(self, *a, **k):
            return self

        def execute(self):
            data = [r for r in self.rows
                    if self.col and r.get(self.col) == self.val]
            return type('R', (), {'data': data})()

    rows = [{'flight': 'LH712', 'callsign': 'DLH712', 'reg': 'DAIXB',
             'reg_display': 'D-AIXB', 'lat': 43.63197, 'lon': 87.03042,
             'track': 77, 'gs_kt': 579, 'alt_ft': 39100, 'origin': 'FRA',
             'dest': 'ICN', 'ac_type': '748', 'on_ground': False,
             'seen_ts': SNAPSHOT_SEEN, 'updated_at': SNAPSHOT_SEEN}]
    monkeypatch.setattr(AXD, '_sb',
                        lambda: type('SB', (), {'table': lambda s, n: _Q(rows)})())

    assert AXD._aircraft_live_pos(flight='LH712', dep='ICN',
                                  sched_dep_iso=LEG_MORGEN_DEP)[0] is None
    # Gegenprobe auf demselben Pfad
    assert AXD._aircraft_live_pos(flight='LH712', dep='ICN',
                                  sched_dep_iso=LEG_HEUTE_DEP)[0] is not None


# ═══════════════════════════════════════════════════════════════════════════
# 3. DER VORFALL END-TO-END — `_enrich_leg_delays`
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def _enrich_umgebung(monkeypatch):
    """Genau die Lage des Vorfalls: die Board-Seite liefert NICHTS
    (`obs_sides` dep/arr beide None), die persistente Quelle auch nicht — die
    EINZIGE Evidenz ist der Positions-Snapshot. Die FlightState-Engine laeuft
    wie in Produktion (FLIGHTSTATE_LIVE_FRIENDS=1)."""
    monkeypatch.setenv('FLIGHTSTATE_LIVE_FRIENDS', '1')
    monkeypatch.delenv('FLIGHTSTATE_SHADOW', raising=False)
    monkeypatch.setattr(A, '_flight_obs_merged', lambda *a, **k: {
        'delay_known': False, 'delay_min': None, 'delay_side': None,
        'dep_delay_min': None, 'arr_delay_min': None, 'status': None,
        'cancelled': False, 'esti_dep': None, 'esti_arr': None,
        'sched_dep': None, 'sched_arr': None, 'reg': None,
        'sides': {'dep': None, 'arr': None},
    })
    monkeypatch.setattr(AXD, '_flight_facts_from_obs', lambda *a, **k: {},
                        raising=False)
    monkeypatch.setattr(AXD, '_fr24_flight_by_number', lambda *a, **k: None,
                        raising=False)
    monkeypatch.setattr(
        AXD, '_nas_live_pos',
        lambda **kw: (_snapshot(), ('FRA', 'ICN'), 'D-AIXB', '748'))
    yield


def _sektor(dep_iso, arr_iso):
    return {'flight': 'LH712', 'from': 'FRA', 'to': 'ICN',
            'dep_iso': dep_iso, 'arr_iso': arr_iso}


def test_vorfall_zukuenftiges_leg_ist_nicht_airborne(_enrich_umgebung):
    """DER GEMELDETE FALL. Ein Flug, der erst in 16,6 h abhebt, darf keinen
    Status aus der Familie airborne/landed tragen — genau das ist die Regel,
    an der der Waechter R2b anschlaegt."""
    sec = _sektor(LEG_MORGEN_DEP, LEG_MORGEN_ARR)
    A._enrich_leg_delays([sec], '2026-08-09', free_only=True)

    st = (sec.get('status') or '').lower()
    assert 'airborne' not in st and 'landed' not in st, sec
    assert sec.get('phase') != 'AIRBORNE', sec
    # und kein Kollateralschaden: es wurde auch keine Zeit/Reg erfunden
    assert sec.get('est_dep_iso') is None and sec.get('est_arr_iso') is None
    assert sec.get('reg') is None


def test_gegenprobe_echt_fliegendes_leg_bleibt_airborne(_enrich_umgebung):
    """PFLICHT-GEGENPROBE: dasselbe Leg, dieselbe Position — aber die Instanz,
    die tatsaechlich in der Luft ist (Abflug 08.08. 13:35 Z). Sie MUSS
    weiterhin 'airborne' melden. Die App lebt davon, dass „fliegt gerade"
    stimmt; ein Fix, der echte Live-Zustaende unterdrueckt, waere schlechter
    als der Fehler."""
    sec = _sektor(LEG_HEUTE_DEP, LEG_HEUTE_ARR)
    A._enrich_leg_delays([sec], '2026-08-08', free_only=True)

    assert sec.get('phase') == 'AIRBORNE', sec
    assert 'airborne' in (sec.get('status') or '').lower(), sec


def test_gegenprobe_ohne_riegel_waere_der_fehler_da(_enrich_umgebung, monkeypatch):
    """EHRLICHKEITS-PROBE: mit ausgehaengtem Riegel reproduziert derselbe Test
    den gemeldeten Fehler wortwoertlich. Ohne diese Zeile wuesste niemand, ob
    der Test ueberhaupt am Fehler haengt oder an einer Nebenbedingung."""
    monkeypatch.setattr(AXD, '_live_pos_instance_ok', lambda pos, sched: True)
    sec = _sektor(LEG_MORGEN_DEP, LEG_MORGEN_ARR)
    A._enrich_leg_delays([sec], '2026-08-09', free_only=True)
    assert sec.get('status') == 'airborne'
    assert sec.get('phase') == 'AIRBORNE'


def test_nachbar_rueckwaerts_gestriges_leg_nicht_airborne(_enrich_umgebung):
    """Die andere Richtung derselben Fehlerklasse: ein Leg von GESTERN
    (07.08., laengst gelandet) darf nicht 'airborne' werden, nur weil die
    heutige Rotation derselben Nummer gerade fliegt.

    `past_horizon_h` = 24*35 wie der ECHTE Aufrufer (friend-roster, app.py
    „_enrich_leg_delays(..., past_horizon_h=24 * 35)") — mit dem Default (30 h)
    wuerde das Leg schon vom Horizont-Guard uebersprungen und der Test wuerde
    gar nichts messen."""
    sec = _sektor('2026-08-07T13:35:00Z', '2026-08-08T00:55:00Z')
    A._enrich_leg_delays([sec], '2026-08-07', free_only=True,
                         past_horizon_h=24 * 35)
    # Beleg, dass das Leg wirklich durch die Anreicherung lief (sonst waere
    # die Zusicherung unten leer):
    assert sec.get('status_observed_at'), sec
    assert sec.get('phase') != 'AIRBORNE', sec
    assert 'airborne' not in (sec.get('status') or '').lower(), sec
