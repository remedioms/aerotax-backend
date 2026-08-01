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


# ── Quellen-Kaskade: Scraper (frei) → LH → FR24 (bezahlt) ───────────────────
# Owner 01.08.2026: "der airport scrapper ist free und zu bevorzugen, aber sonst
# LH und wenn da keine Verfuegbarkeit dann f24, immer aber richtige Werte, egal
# ob es dann kostet."

def _run_kaskade(monkeypatch, *, scraper_arr, lh_arr, fr24_arr,
                 board_sched_arr='2026-08-01T10:05:00+02:00'):
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
        'sched_dep': None, 'sched_arr': board_sched_arr,
    })
    monkeypatch.setattr(A, '_board_local_to_utc_iso', lambda v, station=None: v)
    monkeypatch.setattr(A, '_gate_facts_arr_against_leg', lambda f, _a: f)

    aufrufe = {'frei': 0, 'lh': 0, 'fr24': 0}

    def facts(*a, **k):
        if k.get('lh_cached_only'):
            aufrufe['frei'] += 1
            return {'est_arr': scraper_arr, 'arr_delay_min': None} if scraper_arr else None
        aufrufe['lh'] += 1
        return {'est_arr': lh_arr, 'arr_delay_min': None} if lh_arr else None

    monkeypatch.setattr(ADB, '_flight_facts_from_obs', facts)
    monkeypatch.setattr(ADB, '_fr24_flight_by_number',
                        lambda fn, d=None: (aufrufe.__setitem__('fr24', aufrufe['fr24'] + 1)
                                            or ({'sched_arr': fr24_arr, 'dep_iata': 'BCN',
                                                 'arr_iata': 'FRA'} if fr24_arr else None)))

    A._enrich_leg_delays([sec], arr_iso.strftime('%Y-%m-%d'), free_only=False)
    return sec, aufrufe


def test_scraper_gewinnt_und_kostet_nichts(monkeypatch):
    """Hat der freie Scraper die Messung, wird NICHTS bezahlt."""
    sec, ruf = _run_kaskade(monkeypatch,
                            scraper_arr='2026-08-01T10:03:00+02:00',
                            lh_arr='2026-08-01T09:59:00+02:00',
                            fr24_arr='2026-08-01T09:55:00+02:00')
    assert sec['est_arr_iso'] == '2026-08-01T10:03:00+02:00'
    assert ruf['lh'] == 0 and ruf['fr24'] == 0, 'freie Quelle reicht → kein LH, kein FR24'


def test_lh_laeuft_in_stufe_1_mit_und_blockiert_nie(monkeypatch):
    """LH ist Stufe 2, wird aber INNERHALB von `_flight_facts_from_obs` mit
    `lh_cached_only=True` abgefragt (die Funktion legt LH ueber den Scraper).

    Ein blockierender LH-Call ist hier verboten: er ist global auf 5/s
    gedrosselt und serialisiert damit ALLE Worker — auf 25-30 Legs sind das
    5-12 s Wartezeit (Vorfall 22.07.). Geld darf kosten, Wartezeit nicht.
    Festgehalten auch in tests/aerox/test_friend_roster_cold_latency.py."""
    sec, ruf = _run_kaskade(monkeypatch,
                            scraper_arr='2026-08-01T10:03:00+02:00',
                            lh_arr=None,
                            fr24_arr='2026-08-01T09:55:00+02:00')
    assert sec['est_arr_iso'] == '2026-08-01T10:03:00+02:00'
    assert ruf['lh'] == 0, 'NIE ein blockierender LH-Call auf dem Fan-out'
    assert ruf['fr24'] == 0, 'freie Wahrheit vorhanden → nichts bezahlen'


def test_erst_wenn_frei_und_lh_leer_sind_kostet_fr24(monkeypatch):
    """Der gemeldete Fall: nur FR24 kennt die echte Landung (10:03)."""
    sec, ruf = _run_kaskade(monkeypatch,
                            scraper_arr=None, lh_arr=None,
                            fr24_arr='2026-08-01T10:03:00+02:00')
    assert sec['est_arr_iso'] == '2026-08-01T10:03:00+02:00', (
        'Ohne freie Wahrheit MUSS der bezahlte Weg die richtige Zeit liefern.')
    assert ruf['fr24'] == 1
    # Soll 10:05 gegen Ist 10:03 → 2 Minuten zu frueh. Die Zahl muss zur Zeit
    # passen, sonst stuende neben 10:03 weiter die alte "+10".
    assert sec['arr_delay_min'] == -2
    assert sec['delay_min'] == -2


def test_ohne_belastbare_sollzeit_bleibt_die_zahl_leer(monkeypatch):
    """Kein absolutes Board-Soll → keine Verspaetungszahl erfinden."""
    sec, _ = _run_kaskade(monkeypatch,
                          scraper_arr=None, lh_arr=None,
                          fr24_arr='2026-08-01T10:03:00+02:00',
                          board_sched_arr='10:05')   # nackte Ortszeit
    assert sec['est_arr_iso'] == '2026-08-01T10:03:00+02:00'
    assert sec['arr_delay_min'] is None, 'lieber keine Zahl als eine falsche'
