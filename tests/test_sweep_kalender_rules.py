"""Regeln des Kalender-Waechters `ops/hetzner/sweep_kalender.py` (2026-08-09).

WARUM ES DIESE DATEI GIBT: das Skript lag bis heute NUR auf dem Server
(/opt/aerox/sweep_kalender.py), in keinem Repo und in keinem Test. Ein Skript,
das Alarme verschickt, war damit nicht nachvollziehbar und jede Aenderung daran
unwiederbringlich. Seit 2026-08-09 liegt es unter `ops/hetzner/` und seine
Regeln sind eine reine Funktion (`pruefe_sektor`) — diese Datei prueft sie.

DER OWNER-BEFUND, der die Aenderung ausgeloest hat: die alte R2
(„zukunft-mit-ist") meldete JEDE est_dep/est_arr an einem Flug, dessen Abflug
> 1 h in der Zukunft liegt. Lufthansa kuendigt Verspaetungen aber voellig
legitim im Voraus an. BELEG aus der Produktion (Messung 2026-08-08T20:00Z,
`airport_delay_obs`): Zeile `CGN#ARR EW593`, Verkehrstag 2026-08-09,
sched 01:15, esti 2026-08-09T02:37 (+82 min), `esti_changed_at`
2026-08-08T18:20:24Z — die Schaetzung stand rund 7 h VOR dem Ereignis in der
Datenbank. Die alte Regel haette das als Verstoss gemeldet.

Ein Alarm, der staendig falsch anschlaegt, wird ignoriert — und dann geht der
echte unter. Deshalb prueft R2 jetzt nicht mehr „gibt es eine Schaetzung",
sondern „gibt es eine MESSUNG an einem Flug, der noch nicht stattgefunden hat".
"""
import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

_SWEEP_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'ops', 'hetzner', 'sweep_kalender.py')


def _load():
    spec = importlib.util.spec_from_file_location('_sweep_kalender', _SWEEP_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)         # darf KEINE Env/Netz brauchen
    return mod


SK = _load()

# Fester Messzeitpunkt (aware, UTC) — alle Zeiten unten explizit mit Zone.
NOW = datetime(2026, 8, 8, 19, 49, tzinfo=timezone.utc)
TAG = 'AT-TEST01… 2026-08-08 LH757 BOM->FRA'


def _codes(viol):
    """Nur die Regel-Kuerzel (z.B. 'R2c') aus den Meldungstexten."""
    return sorted({v.split()[0] for v in viol})


# ═══ 1. Der Owner-Fall: angekuendigte Verspaetung ist KEIN Verstoss ══════════
def test_angekuendigte_verspaetung_ist_kein_verstoss():
    """Abflug morgen, Tafel meldet heute schon +31 min. Das ist die normale
    Vorab-Ansage — der Waechter muss stumm bleiben."""
    dep = NOW + timedelta(hours=25)
    arr = dep + timedelta(hours=8)
    sec = {
        'flight': 'LH757', 'from': 'BOM', 'to': 'FRA',
        'dep_iso': dep.isoformat(), 'arr_iso': arr.isoformat(),
        'est_dep_iso': (dep + timedelta(minutes=31)).isoformat(),
        'est_arr_iso': (arr + timedelta(minutes=16)).isoformat(),
        'status': 'geplant', 'arr_measured': False,
    }
    viol, hinweis = SK.pruefe_sektor(sec, TAG, NOW)
    assert viol == [], f'Vorab-Ansage darf nicht melden, meldete: {viol}'
    assert hinweis == []


def test_ew593_der_echte_vorab_beleg():
    """Der real gemessene Beleg (s. Modul-Docstring): eine Schaetzung, die
    rund 7 h vor dem Ereignis geschrieben wurde. Muss still bleiben."""
    dep = datetime(2026, 8, 8, 22, 40, tzinfo=timezone.utc)      # ~00:40 CGN
    arr = datetime(2026, 8, 8, 23, 15, tzinfo=timezone.utc)      # 01:15 lokal
    sec = {
        'flight': 'EW593', 'from': 'STR', 'to': 'CGN',
        'dep_iso': dep.isoformat(), 'arr_iso': arr.isoformat(),
        'est_arr_iso': (arr + timedelta(minutes=82)).isoformat(),
        'status': 'geplant',
    }
    assert SK.pruefe_sektor(sec, TAG, NOW)[0] == []


def test_sehr_grosse_aber_reale_verspaetung_bleibt_still():
    """Zwischen −6 h und +20 h wird NICHTS beanstandet — auch ein Delay von
    einem halben Tag ist real und darf den Waechter nicht ausloesen."""
    dep = NOW + timedelta(hours=30)
    arr = dep + timedelta(hours=2)
    for delay_h in (0, 1, 5, 12, 19):
        sec = {
            'dep_iso': dep.isoformat(), 'arr_iso': arr.isoformat(),
            'est_dep_iso': (dep + timedelta(hours=delay_h)).isoformat(),
            'est_arr_iso': (arr + timedelta(hours=delay_h)).isoformat(),
            'status': 'geplant',
        }
        assert SK.pruefe_sektor(sec, TAG, NOW)[0] == [], f'{delay_h} h ist real'


# ═══ 2. GEGENPROBE: wofuer R2 gebaut wurde, faengt sie weiter ════════════════
def test_gegenprobe_gelandet_an_einem_zukunftsflug_schlaegt_alarm():
    """DER Kernfall der Regel. Ein Flug, der erst morgen abfliegt, kann nicht
    gelandet sein — das MUSS weiterhin melden."""
    dep = NOW + timedelta(hours=25)
    sec = {'dep_iso': dep.isoformat(),
           'arr_iso': (dep + timedelta(hours=8)).isoformat(),
           'status': 'landed'}
    viol = SK.pruefe_sektor(sec, TAG, NOW)[0]
    assert 'R2b' in _codes(viol)
    assert any('zukunft-landed' in v for v in viol)


def test_gegenprobe_alle_landed_synonyme():
    dep = NOW + timedelta(hours=25)
    for st in ('landed', 'Gelandet', 'ARRIVED'):
        sec = {'dep_iso': dep.isoformat(), 'status': st}
        assert 'R2b' in _codes(SK.pruefe_sektor(sec, TAG, NOW)[0]), st


def test_zukunftsflug_airborne_schlaegt_alarm():
    """Real beobachtet am 2026-08-08T19:59Z: LH712 FRA->ICN mit dem Datum
    2026-08-09 trug status='airborne', obwohl der Flug erst am naechsten Tag
    um 15:35 FRA-Ortszeit startet. Der Nachbartag hatte abgefaerbt."""
    dep = NOW + timedelta(hours=18)
    sec = {'dep_iso': dep.isoformat(), 'status': 'airborne'}
    viol = SK.pruefe_sektor(sec, TAG, NOW)[0]
    assert any('zukunft-airborne' in v for v in viol)


def test_arr_measured_an_einem_zukunftsflug_schlaegt_alarm():
    """R2a — die schaerfste Trennlinie, die die Daten hergeben. `arr_measured`
    wird in app._enrich_leg_delays NUR gesetzt, wenn eine Ankunftszeit mit
    terminalem Status UND plausiblem `esti_changed_at` belegt ist: eine
    Messung. An einem noch nicht gestarteten Flug ist sie unmoeglich."""
    dep = NOW + timedelta(hours=25)
    arr = dep + timedelta(hours=8)
    sec = {'dep_iso': dep.isoformat(), 'arr_iso': arr.isoformat(),
           'est_arr_iso': (arr + timedelta(minutes=5)).isoformat(),
           'arr_measured': True, 'status': 'geplant'}
    assert 'R2a' in _codes(SK.pruefe_sektor(sec, TAG, NOW)[0])


def test_arr_measured_false_meldet_nicht():
    """Gegenprobe zu R2a: dasselbe Leg OHNE Mess-Beleg ist eine Prognose."""
    dep = NOW + timedelta(hours=25)
    arr = dep + timedelta(hours=8)
    sec = {'dep_iso': dep.isoformat(), 'arr_iso': arr.isoformat(),
           'est_arr_iso': (arr + timedelta(minutes=5)).isoformat(),
           'arr_measured': False, 'status': 'geplant'}
    assert SK.pruefe_sektor(sec, TAG, NOW)[0] == []


# ═══ 3. R2c — Physik statt Schwellwert ══════════════════════════════════════
def test_r2c_ist_zeit_in_der_vergangenheit_an_einem_zukunftsflug():
    """Der Original-Vorfall LH757 BOM->FRA, mit den ECHTEN Werten aus der
    Sweep-Mail vom 2026-08-08:
        dep_iso  2026-08-08T21:10Z   (Abflug steht noch bevor)
        est_dep  2026-08-07T21:41Z   (liegt 22 h in der VERGANGENHEIT)
        est_arr  2026-08-08T06:21Z   (ebenfalls vorbei)
    Eine Schaetzung zeigt nach vorn. Was hier steht, ist der 24-h-Nachbar."""
    sec = {
        'flight': 'LH757', 'from': 'BOM', 'to': 'FRA',
        'dep_iso': '2026-08-08T21:10:00Z',
        'arr_iso': '2026-08-09T06:05:00Z',
        'est_dep_iso': '2026-08-07T21:41:00Z',
        'est_arr_iso': '2026-08-08T06:21:00Z',
        'status': 'grounded',
    }
    viol = SK.pruefe_sektor(sec, TAG, NOW)[0]
    codes = _codes(viol)
    assert 'R2c' in codes, viol          # Ist-Zeit in der Vergangenheit
    assert 'R4' in codes, viol           # est_arr weit vor Plan-Ankunft
    assert 'R6' in codes, viol           # est_dep weit vor Plan-Abflug
    assert sum('R2c' in v for v in viol) == 2, 'dep- UND arr-Seite melden'


def test_r2c_schweigt_wenn_die_schaetzung_in_die_zukunft_zeigt():
    dep = NOW + timedelta(hours=3)
    sec = {'dep_iso': dep.isoformat(),
           'est_dep_iso': (dep + timedelta(minutes=45)).isoformat(),
           'status': 'geplant'}
    assert SK.pruefe_sektor(sec, TAG, NOW)[0] == []


def test_r2_guard_band_ein_flug_kurz_vor_abflug_wird_nicht_beurteilt():
    """Innerhalb FUTURE_GUARD_H (1 h) urteilt R2 nicht — Uhren-Versatz
    zwischen Scraper und Tafel darf keine Fehlalarme erzeugen."""
    dep = NOW + timedelta(minutes=20)
    sec = {'dep_iso': dep.isoformat(), 'status': 'abgeflogen',
           'est_dep_iso': (NOW - timedelta(minutes=5)).isoformat()}
    assert not any(v.startswith('R2') for v in SK.pruefe_sektor(sec, TAG, NOW)[0])


# ═══ 4. R6 — das neue Spiegelbild von R4 auf der Abflug-Seite ═══════════════
# Alle vier Faelle sind ECHTE Werte aus roster_snapshots, gemessen 2026-08-08.
@pytest.mark.parametrize('flight,dep_iso,est_dep_iso', [
    ('LH099',  '2026-08-07T09:00:00Z', '2026-08-06T11:18:00+02:00'),
    ('LH1801', '2026-08-08T09:50:00Z', '2026-08-07T12:04:00+02:00'),
    ('LH488',  '2026-08-07T13:35:00Z', '2026-08-06T15:58:00+02:00'),
    ('LH1750', '2026-08-08T06:32:00Z', '2026-08-07T09:05:00+02:00'),
])
def test_r6_faengt_die_gemessenen_24h_nachbarn(flight, dep_iso, est_dep_iso):
    """Diese vier Sektoren lagen am 08.08.2026 mit einer um ~24 h
    verschobenen est_dep im Bestand — und KEINE der alten Regeln sah sie:
    R2 schaut nur in die Zukunft (das sind Vergangenheits-Legs), R4 nur auf
    die Ankunfts-Seite (die war korrekt)."""
    sec = {'flight': flight, 'dep_iso': dep_iso, 'est_dep_iso': est_dep_iso,
           'status': 'landed'}
    viol = SK.pruefe_sektor(sec, TAG, NOW)[0]
    assert 'R6' in _codes(viol), viol
    assert any('frueh' in v for v in viol)


def test_r6_faengt_auch_die_andere_richtung():
    """LH046 FRA->HAJ, 2026-08-05: est_dep lag +24 h SPAETER als der Plan.
    Live gemessen im Sweep-Lauf 2026-08-08T19:59Z."""
    sec = {'flight': 'LH046', 'dep_iso': '2026-08-05T15:27:00Z',
           'est_dep_iso': '2026-08-06T17:32:00+02:00'}
    assert 'R6' in _codes(SK.pruefe_sektor(sec, TAG, NOW)[0])


def test_r6_gegenprobe_normale_verspaetung_bleibt_still():
    """Gegenprobe: ein bereits geflogenes Leg mit realer Verspaetung darf
    nicht melden — sonst waere R6 genauso stumpf wie die alte R2."""
    dep = NOW - timedelta(hours=5)
    for delay_min in (0, 12, 45, 180, 600):
        sec = {'dep_iso': dep.isoformat(),
               'est_dep_iso': (dep + timedelta(minutes=delay_min)).isoformat(),
               'status': 'landed'}
        assert not any(v.startswith('R6')
                       for v in SK.pruefe_sektor(sec, TAG, NOW)[0]), delay_min


# ═══ 5. R1/R3/R4/R5 unveraendert (Schutz vor Kollateralschaden) ═════════════
def test_r1_stale_airborne_unveraendert():
    arr = NOW - timedelta(hours=9)
    sec = {'arr_iso': arr.isoformat(), 'dep_iso': (arr - timedelta(hours=2)).isoformat(),
           'status': 'airborne'}
    assert 'R1' in _codes(SK.pruefe_sektor(sec, TAG, NOW)[0])


def test_r3_arr_vor_dep_unveraendert():
    dep = NOW - timedelta(hours=4)
    sec = {'dep_iso': dep.isoformat(), 'arr_iso': (dep + timedelta(hours=2)).isoformat(),
           'est_dep_iso': (dep + timedelta(minutes=30)).isoformat(),
           'est_arr_iso': (dep + timedelta(minutes=10)).isoformat()}
    assert 'R3' in _codes(SK.pruefe_sektor(sec, TAG, NOW)[0])


def test_r4_frueh_arm_unveraendert():
    """Der bisherige R4-Fall (est_arr > 6 h VOR Plan) meldet weiter."""
    arr = NOW - timedelta(hours=2)
    sec = {'arr_iso': arr.isoformat(),
           'est_arr_iso': (arr - timedelta(hours=24)).isoformat()}
    viol = SK.pruefe_sektor(sec, TAG, NOW)[0]
    assert any('R4 wrongday-est-arr-frueh' in v for v in viol)


def test_r5_bleibt_ein_hinweis_kein_verstoss():
    arr = NOW - timedelta(hours=30)
    sec = {'arr_iso': arr.isoformat(), 'status': 'landed'}
    viol, hinweis = SK.pruefe_sektor(sec, TAG, NOW)
    assert viol == []
    assert hinweis and hinweis[0].startswith('R5')


# ═══ 6. Robustheit: der Waechter darf nie sterben ═══════════════════════════
def test_pruefe_sektor_wirft_nie_bei_muell():
    for sec in ({}, {'dep_iso': 'kaputt', 'est_dep_iso': None},
                {'dep_iso': None, 'arr_iso': '', 'status': None},
                {'est_arr_iso': 'x', 'arr_measured': 'ja'}):
        viol, hinweis = SK.pruefe_sektor(sec, TAG, NOW)
        assert isinstance(viol, list) and isinstance(hinweis, list)


def test_wrapper_sentinel_name_unveraendert():
    """`ops/hetzner/sweep_wrapper.sh` greppt exakt `SWEEP_VIOLATIONS_R1R4=`.
    Wird der Schluessel umbenannt, faellt der Wrapper still auf den Sentinel
    'ERR' zurueck und mailt bei JEDEM Lauf — deshalb hier festgenagelt."""
    with open(_SWEEP_PATH) as f:
        src = f.read()
    assert 'SWEEP_VIOLATIONS_R1R4=' in src
    wrapper = os.path.join(os.path.dirname(_SWEEP_PATH), 'sweep_wrapper.sh')
    with open(wrapper) as f:
        assert 'SWEEP_VIOLATIONS_R1R4' in f.read()


def test_skript_ist_ohne_env_importierbar():
    """Das Skript darf beim Import keine Env/kein Netz brauchen — sonst waere
    es wieder untestbar (genau der Zustand vor dem 2026-08-09)."""
    assert callable(SK.pruefe_sektor)
    assert SK.SB is None and SK.KEY is None
