"""Gemessene Landung schlaegt die Tafel-Prognose (Owner-Befund 01.08.2026).

VORFALL: Der Owner sah im Freunde-Kalender fuer LH1137 (BCN-FRA, 01.08.)
"08:12-10:15" und darunter "Ist 08:12-10:15". Die 10:15 hat es nie gegeben:

  geplant       10:05
  tatsaechlich  10:03   (die Maschine war 2 Minuten ZU FRUEH)
  angezeigt     10:15   = 10:05 Plan + 10 min ANGESAGTE Verspaetung

Dieselbe Backend-Antwort trug beide Wahrheiten gleichzeitig:
`info.esti_arr = 10:15 / arr_delay_min = +10` (Flughafentafel) und
`resolve.est_arr = 10:03 / arr_delay_min = -2` (Messung aus dem Warehouse).

URSACHE: `_enrich_leg_delays` nahm den Merge-Wert `m` (Tafel) als Wahrheit und
liess die persistenten Ist-Fakten NUR Luecken fuellen ("nie einen vorhandenen
m-Wert ueberschreiben"). Eine Tafel-Prognose wird nach der Landung aber nie
korrigiert — sie gewann damit dauerhaft gegen die Messung, und die App zeigte
eine Uhrzeit, die nie stattgefunden hat, als "Ist".

REGEL: Ist die Soll-Ankunft eines Legs VORBEI, gewinnt die Messung. Das ist die
Owner-Regel "lieber keine Zeile als ein synthetisierter Wert" — hier: lieber die
gemessene Zeit als eine hochgerechnete.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import app as A
import blueprints.aerox_data_blueprint as ADB


def _iso(dt):
    # Genau die Form, die im Roster steht ('…Z'). `%z` liefert '+0000' OHNE
    # Doppelpunkt — das parst `datetime.fromisoformat` nicht, das Leg gälte
    # dann faelschlich als "nicht vergangen" und der Test wuerde am
    # eigentlichen Pfad vorbeilaufen.
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _run(monkeypatch, *, arr_in_past, board_arr, board_delay,
         measured_arr, measured_delay):
    """Ein Leg durch _enrich_leg_delays schicken; Tafel und Messung getrennt
    vorgeben. Liefert den angereicherten Sektor."""
    base = datetime.now(timezone.utc).replace(microsecond=0)
    arr_iso = base - timedelta(hours=2) if arr_in_past else base + timedelta(hours=2)
    dep_iso = arr_iso - timedelta(hours=2)

    sec = {
        'flight': 'LH1137', 'from': 'BCN', 'to': 'FRA',
        'dep_iso': _iso(dep_iso), 'arr_iso': _iso(arr_iso),
    }

    # Tafel-Merge: traegt eine Prognose (esti_arr) + angesagte Verspaetung.
    monkeypatch.setattr(A, '_flight_obs_merged', lambda *a, **k: {
        'delay_known': True, 'delay_min': board_delay, 'delay_side': 'arr',
        'dep_delay_min': 0, 'arr_delay_min': board_delay,
        'status': 'Boarding', 'cancelled': False,
        'esti_dep': None, 'esti_arr': board_arr,
        'reg': 'DAIRM', 'sides': {'dep': 'obs', 'arr': 'obs'},
        'sched_dep': None, 'sched_arr': None,
    })
    # Board-Esti wandert durch diesen Konverter — hier 1:1 durchreichen, damit
    # der Test die Prognose unveraendert im Ergebnis sieht.
    monkeypatch.setattr(A, '_board_local_to_utc_iso',
                        lambda v, station=None: v)
    # Persistente Ist-Fakten: die MESSUNG.
    monkeypatch.setattr(ADB, '_flight_facts_from_obs', lambda *a, **k: {
        'est_dep': None, 'est_arr': measured_arr,
        'dep_delay_min': None, 'arr_delay_min': measured_delay,
        'arr_status': None, 'sched_arr': None, 'reg': 'DAIRM',
    })
    monkeypatch.setattr(A, '_gate_facts_arr_against_leg', lambda f, _a: f)

    A._enrich_leg_delays([sec], arr_iso.strftime('%Y-%m-%d'), free_only=False)
    return sec


def test_gemessene_landung_ersetzt_tafel_prognose(monkeypatch):
    """Der gemeldete Fall: Leg ist gelandet, Tafel sagt +10, gemessen -2."""
    sec = _run(monkeypatch,
               arr_in_past=True,
               board_arr='2026-08-01T10:15:00+0200', board_delay=10,
               measured_arr='2026-08-01T10:03:00+02:00', measured_delay=-2)

    assert sec['est_arr_iso'] == '2026-08-01T10:03:00+02:00', (
        'Die gemessene Landung muss die Tafel-Prognose ersetzen — sonst zeigt '
        'die App eine Uhrzeit als "Ist", die nie stattgefunden hat.')
    assert sec['arr_delay_min'] == -2
    # Die Ein-Zahl-Verspaetung muss mitziehen, sonst stuende neben der
    # korrigierten Zeit weiter die alte Prognose-Zahl (+10).
    assert sec['delay_min'] == -2
    assert sec['delay_side'] == 'arr'


def test_zukuenftiges_leg_behaelt_die_prognose(monkeypatch):
    """Solange das Leg NICHT gelandet ist, ist die Tafel die beste Quelle —
    hier darf die Messung nicht vorgreifen (sie waere die einer anderen
    Rotation oder gar nicht vorhanden)."""
    sec = _run(monkeypatch,
               arr_in_past=False,
               board_arr='2026-08-01T10:15:00+0200', board_delay=10,
               measured_arr='2026-08-01T10:03:00+02:00', measured_delay=-2)

    assert sec['est_arr_iso'] == '2026-08-01T10:15:00+0200'
    assert sec['arr_delay_min'] == 10


def test_ohne_messung_bleibt_die_tafel_stehen(monkeypatch):
    """Kein Ist-Fakt vorhanden → nichts erfinden, nichts wegnehmen."""
    base = datetime.now(timezone.utc).replace(microsecond=0)
    arr_iso = base - timedelta(hours=2)
    sec = {
        'flight': 'LH1137', 'from': 'BCN', 'to': 'FRA',
        'dep_iso': _iso(arr_iso - timedelta(hours=2)), 'arr_iso': _iso(arr_iso),
    }
    monkeypatch.setattr(A, '_flight_obs_merged', lambda *a, **k: {
        'delay_known': True, 'delay_min': 10, 'delay_side': 'arr',
        'dep_delay_min': 0, 'arr_delay_min': 10,
        'status': 'Boarding', 'cancelled': False,
        'esti_dep': None, 'esti_arr': '2026-08-01T10:15:00+0200',
        'reg': 'DAIRM', 'sides': {'dep': 'obs', 'arr': 'obs'},
        'sched_dep': None, 'sched_arr': None,
    })
    monkeypatch.setattr(A, '_board_local_to_utc_iso',
                        lambda v, station=None: v)
    monkeypatch.setattr(ADB, '_flight_facts_from_obs', lambda *a, **k: None)
    monkeypatch.setattr(A, '_gate_facts_arr_against_leg', lambda f, _a: f)

    A._enrich_leg_delays([sec], arr_iso.strftime('%Y-%m-%d'), free_only=False)
    assert sec['est_arr_iso'] == '2026-08-01T10:15:00+0200'
    assert sec['arr_delay_min'] == 10


def test_gelandetes_leg_ohne_messung_zeigt_keine_hochgerechnete_zeit(monkeypatch):
    """Owner 01.08.2026: „will keine Schaetzungen."

    Die Tafel trug nur einen TEXT ("10 min"), daraus wurde `Plan + 10` = 10:15
    hochgerechnet. Ist das Leg gelandet und liefert die Messung nichts, darf
    diese erfundene Uhrzeit NICHT stehenbleiben — lieber keine Zeile als ein
    synthetisierter Wert. Die gemeldete Verspaetungs-ZAHL bleibt erhalten, die
    ist eine echte Ansage."""
    base = datetime.now(timezone.utc).replace(microsecond=0)
    arr_iso = base - timedelta(hours=2)
    sec = {
        'flight': 'LH1137', 'from': 'BCN', 'to': 'FRA',
        'dep_iso': _iso(arr_iso - timedelta(hours=2)), 'arr_iso': _iso(arr_iso),
    }
    monkeypatch.setattr(A, '_flight_obs_merged', lambda *a, **k: {
        'delay_known': True, 'delay_min': 10, 'delay_side': 'arr',
        'dep_delay_min': 0, 'arr_delay_min': 10,
        'status': 'Boarding', 'cancelled': False,
        'esti_dep': None, 'esti_arr': '2026-08-01T10:15:00+0200',
        'esti_arr_announced': True,          # <- aus Tafel-TEXT hochgerechnet
        'reg': 'DAIRM', 'sides': {'dep': 'obs', 'arr': 'obs'},
        'sched_dep': None, 'sched_arr': None,
    })
    monkeypatch.setattr(A, '_board_local_to_utc_iso', lambda v, station=None: v)
    monkeypatch.setattr(ADB, '_flight_facts_from_obs', lambda *a, **k: None)
    monkeypatch.setattr(A, '_gate_facts_arr_against_leg', lambda f, _a: f)

    A._enrich_leg_delays([sec], arr_iso.strftime('%Y-%m-%d'), free_only=False)

    assert sec['est_arr_iso'] is None, (
        'Eine aus einer Ansage hochgerechnete Ankunft darf auf einem '
        'gelandeten Leg nicht als Zeit stehenbleiben.')
    assert sec['arr_delay_min'] == 10, 'Die gemeldete Zahl bleibt erhalten.'


def test_hochrechnung_bleibt_solange_das_leg_fliegt(monkeypatch):
    """Vor der Landung ist die Ansage die beste verfuegbare Information —
    sie wird als ERWARTET gezeigt, nicht geloescht."""
    sec = _run(monkeypatch,
               arr_in_past=False,
               board_arr='2026-08-01T10:15:00+0200', board_delay=10,
               measured_arr=None, measured_delay=None)
    assert sec['est_arr_iso'] == '2026-08-01T10:15:00+0200'
