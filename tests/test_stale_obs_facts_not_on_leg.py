"""STALE-FAKTEN GEHOEREN NICHT AN EIN LEG (Sweep-Nachforschung 2026-08-09).

Der ZWEITE, EIGENE Fehler neben dem Betriebstag-Bug vom 2026-08-08.

SYMPTOM (Prod-Messung 2026-08-08T19:49Z ueber alle `roster_snapshots`):
40 von 77 069 gespeicherten Sektoren trugen eine um ~24 h verschobene
`est_dep_iso` bei KORREKTER `est_arr_iso`. Die Abflugstationen (MUC, MAD, ZRH,
PMI, VIE, BCN, GWT) haben mittags KEINEN Betriebstag-Versatz — der
Datums-Schluessel war also richtig. Beispiele, wortwoertlich aus dem Bestand:

    LH099  MUC->FRA  dep 2026-08-07T09:00Z  est_dep 2026-08-06T11:18:00+02:00
    LH1801 MAD->MUC  dep 2026-08-08T09:50Z  est_dep 2026-08-07T12:04:00+02:00
    LH488  MUC->SEA  dep 2026-08-07T13:35Z  est_dep 2026-08-06T15:58:00+02:00
    LH1750 MUC->ATH  dep 2026-08-08T06:32Z  est_dep 2026-08-07T09:05:00+02:00

URSACHE, belegt an den Rohdaten: `airport_delay_obs` enthaelt fuer LH99
(so schreibt das Board die Nummer) nur die Verkehrstage 02./04./06.08. — fuer
den 07.08. gibt es KEINE Zeile. `_flight_facts_from_obs_uncached` liest
bewusst d−1/d/d+1 (die ARR-Row eines Nachtflugs liegt auf d+1) und faellt,
wenn fuer den angefragten Tag gar nichts da ist, auf den Nachbartag zurueck.
Es MARKIERT das ehrlich: `facts['stale'] = True`, `facts['obs_date']`.

Der Vertrag dieser Markierung steht woertlich im Code
(`_merge_lh_into_facts`): „ist die Board-Seite eine Vortags-/Fremdtag-
Beobachtung (`stale`), wird sie downstream komplett verworfen". JEDER andere
Konsument haelt ihn ein — der flight-info-Dual-Side-Merge, das
`foreign_day_arr`-Gate, der Detail-Merge, und `_enrich_flight_status_with_obs`
(dafuer existiert seit dem P5-Nachfix `test_enrich_ignores_stale_yday_facts`).
NUR `_enrich_leg_delays` (Roster/friend-roster) und der Flugbuch-Enricher
lasen die Markierung nie.

Die est_arr blieb richtig, weil sie schon aus dem Live-Merge `m` stand und die
Facts per Konstruktion nur LUECKEN fuellen (app.py: `if s.get('est_dep_iso')
is None and _facts.get('est_dep')`). Genau daher das Bild „est_dep um 24 h
verschoben, est_arr korrekt".

GEGENPROBE (Pflicht): frische, nicht-stale Fakten muessen weiterhin genau so
ans Leg — sonst hat der Fix die Anreicherung erschlagen statt sie zu heilen.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as A  # noqa: E402
from blueprints import aerox_data_blueprint as AXD  # noqa: E402

from _clock_freeze import FROZEN_UTC, apply_frozen_clock  # noqa: E402


@pytest.fixture(autouse=True)
def _freeze_clock(monkeypatch):
    apply_frozen_clock(monkeypatch, app_module=A,
                       extra_modules=(sys.modules[__name__],))
    yield


@pytest.fixture(autouse=True)
def _clear_caches():
    A._FLIGHT_MERGE_CACHE.clear()
    yield
    A._FLIGHT_MERGE_CACHE.clear()


@pytest.fixture(autouse=True)
def _no_board_no_paid(monkeypatch):
    """Kein Live-Board, kein Warehouse, kein bezahlter Fallback: der Test soll
    AUSSCHLIESSLICH den Facts-Pfad messen."""
    monkeypatch.setattr(A, '_flight_obs_merged', lambda *a, **k: None)
    monkeypatch.setattr(AXD, '_fr24_flight_by_number', lambda *a, **k: None,
                        raising=False)
    yield


# FROZEN_UTC = 2026-07-16 10:00Z. Das Leg liegt am Vortag → `_is_past`.
LEG_DEP = '2026-07-15T09:00:00Z'
LEG_ARR = '2026-07-15T09:57:00Z'
LEG_DAY = '2026-07-15'


def _sector():
    """LH099 MUC->FRA, 1:1 die Form des beanstandeten Bestands-Sektors."""
    return {'flight': 'LH099', 'from': 'MUC', 'to': 'FRA',
            'dep_iso': LEG_DEP, 'arr_iso': LEG_ARR}


# Die Fakten, die die Blueprint-Quelle im Vorfall wirklich lieferte: die
# Beobachtung des VORTAGS, korrekt als stale markiert.
STALE_NACHBARTAG = {
    'sched_dep': '2026-07-14T11:16:00+02:00',
    'est_dep':   '2026-07-14T11:18:00+02:00',
    'sched_arr': '2026-07-14T11:56:00+02:00',
    'est_arr':   '2026-07-14T11:55:00+02:00',
    'dep_status': 'Abgeflogen', 'arr_status': 'Gelandet',
    'arr_delay_min': 2, 'arr_delay_known': True, 'delay_known': True,
    'reg': 'D-AIBH', 'type': '319',
    'arr_obs_at': '2026-07-14T12:10:00Z',
    'arr_esti_changed_at': '2026-07-14T12:05:00Z',
    'stale': True, 'obs_date': '2026-07-14',
}

# Dieselbe Struktur, aber vom RICHTIGEN Tag (kein stale-Marker).
FRISCH_HEUTE = {
    'sched_dep': '2026-07-15T11:00:00+02:00',
    'est_dep':   '2026-07-15T11:18:00+02:00',
    'sched_arr': '2026-07-15T11:57:00+02:00',
    'est_arr':   '2026-07-15T12:05:00+02:00',
    'dep_status': 'Abgeflogen', 'arr_status': 'Gelandet',
    'arr_delay_min': 8, 'arr_delay_known': True, 'delay_known': True,
    'reg': 'D-AIBH', 'type': '319',
    'arr_obs_at': '2026-07-15T12:20:00Z',
    'arr_esti_changed_at': '2026-07-15T12:10:00Z',
}


def _facts(monkeypatch, payload):
    monkeypatch.setattr(AXD, '_flight_facts_from_obs',
                        lambda *a, **k: dict(payload) if payload else {})


# ═══ 1. Der Vorfall ═════════════════════════════════════════════════════════
def test_stale_fakten_landen_nicht_am_leg(monkeypatch):
    """Der beanstandete Fall: die Quelle liefert NUR den Nachbartag (stale) →
    am Leg darf danach KEINE Ist-Zeit, keine Delay-Zahl, kein Status und
    keine Reg des fremden Tages stehen.

    EHRLICHE ABGRENZUNG (gemessen, indem der stale-Riegel testweise entfernt
    wurde): die beiden est_*-Zeilen faengt seit dem 2026-08-08 bereits
    `_gate_facts_dep_against_leg` / `_gate_facts_arr_against_leg` als
    Physik-Schranke ab. Was OHNE diesen Fix nachweislich durchkam, ist die
    `reg` — die Physik-Gates raeumen bewusst nur Zeit-/Delay-/Status-Felder
    und lassen Identitaets-Felder stehen. Deshalb ist die reg-Zeile hier die
    eigentlich scharfe Zusicherung; die est_*-Zeilen sichern zusaetzlich ab,
    dass der Schutz nicht allein an der Breite des Physik-Fensters haengt."""
    _facts(monkeypatch, STALE_NACHBARTAG)
    sec = _sector()
    A._enrich_leg_delays([sec], LEG_DAY, free_only=True)
    assert sec.get('est_dep_iso') is None, sec.get('est_dep_iso')
    assert sec.get('est_arr_iso') is None
    assert sec.get('reg') is None
    assert sec.get('arr_delay_min') is None
    # Der Plan des Legs bleibt unangetastet.
    assert sec['dep_iso'] == LEG_DEP and sec['arr_iso'] == LEG_ARR


def test_der_gemessene_wert_taucht_nirgends_auf(monkeypatch):
    """Explizit: der konkrete Wert aus dem Bestand (`…T11:18:00+02:00` am
    FALSCHEN Tag) darf in KEINEM Feld des Sektors erscheinen."""
    _facts(monkeypatch, STALE_NACHBARTAG)
    sec = _sector()
    A._enrich_leg_delays([sec], LEG_DAY, free_only=True)
    assert '2026-07-14' not in repr(sec)


# ═══ 2. GEGENPROBE: frische Fakten fuellen weiterhin ════════════════════════
def test_gegenprobe_frische_fakten_fuellen_das_leg(monkeypatch):
    """Ohne `stale` muss exakt das bisherige Verhalten bleiben — sonst hat der
    Fix die Anreicherung erschlagen."""
    _facts(monkeypatch, FRISCH_HEUTE)
    sec = _sector()
    A._enrich_leg_delays([sec], LEG_DAY, free_only=True)
    assert sec.get('est_dep_iso') == '2026-07-15T11:18:00+02:00'
    assert sec.get('est_arr_iso') == '2026-07-15T12:05:00+02:00'
    assert sec.get('arr_delay_min') == 8
    assert sec.get('reg') == 'D-AIBH'


def test_gegenprobe_stale_marker_ist_das_einzige_unterscheidungsmerkmal(
        monkeypatch):
    """Beweis, dass wirklich `stale` entscheidet und nicht die Datumslage:
    dieselben Vortags-Zeiten OHNE Marker gehen (wie bisher) durch — dort
    greifen dann die Physik-Gates, nicht dieser Fix."""
    ohne_marker = {k: v for k, v in STALE_NACHBARTAG.items()
                   if k not in ('stale', 'obs_date')}
    _facts(monkeypatch, ohne_marker)
    sec = _sector()
    A._enrich_leg_delays([sec], LEG_DAY, free_only=True)
    # `_gate_facts_dep_against_leg` (2026-08-08) faengt den 24-h-Nachbarn hier
    # als zweite Verteidigungslinie — die Ist-Zeit darf trotzdem nicht kleben.
    assert sec.get('est_dep_iso') is None


# ═══ 3. Der Ankunftstag-Nachschlag (`_fc2`) ═════════════════════════════════
def test_uebernacht_nachschlag_nimmt_keine_stale_ankunft(monkeypatch):
    """Fuer ein Uebernacht-Leg fragt der Enricher die ARR-Seite am Ankunfts-
    Betriebstag nach. Auch dieser zweite Zug darf keinen Nachbartag nehmen."""
    rufe = []

    def fake(flight_no, date, dep_iata=None, arr_iata=None,
             lh_cached_only=False):
        rufe.append(date)
        return {'est_arr': '2026-07-15T22:00:00+02:00',
                'arr_status': 'Gelandet',
                'stale': True, 'obs_date': '2026-07-15'}

    monkeypatch.setattr(AXD, '_flight_facts_from_obs', fake)
    sec = {'flight': 'LH455', 'from': 'SFO', 'to': 'FRA',
           'dep_iso': '2026-07-15T02:00:00Z',
           'arr_iso': '2026-07-15T21:30:00Z'}
    A._enrich_leg_delays([sec], LEG_DAY, free_only=True)
    assert sec.get('est_arr_iso') is None


# ═══ 4. Flugbuch (FCL.050): kein Beleg aus einer fremden Rotation ═══════════
def test_flugbuch_nimmt_keine_stale_reg_und_keine_stale_landung(monkeypatch):
    """Das Flugbuch ist ein Nachweis-Dokument. Eine Reg oder eine Landezeit
    aus der Rotation des Nachbartages waere ein erfundener Beleg — und `reg`
    ist genau die Fehlerklasse „Rotations-Fallback riet fremde Reg"
    (Tibor-Welle 01.08.2026)."""
    gespeichert = {}
    monkeypatch.setattr(AXD, '_flight_facts_from_obs',
                        lambda *a, **k: dict(STALE_NACHBARTAG))
    monkeypatch.setattr(A, '_logbook_facts_load', lambda tok: {})
    monkeypatch.setattr(A, '_logbook_facts_save',
                        lambda tok, f: gespeichert.update(f))
    _run_logbook_enrich(A, 'tok-test', [('k1', 'LH099', LEG_DAY, 'MUC', 'FRA')])
    assert gespeichert, 'der Enricher muss sein Ergebnis festhalten'
    eintrag = gespeichert['k1']
    assert eintrag['reg'] is None
    assert eintrag['type'] is None
    assert eintrag['actual_arr_iso'] is None


def test_gegenprobe_flugbuch_nimmt_frische_fakten(monkeypatch):
    gespeichert = {}
    monkeypatch.setattr(AXD, '_flight_facts_from_obs',
                        lambda *a, **k: dict(FRISCH_HEUTE))
    monkeypatch.setattr(A, '_logbook_facts_load', lambda tok: {})
    monkeypatch.setattr(A, '_logbook_facts_save',
                        lambda tok, f: gespeichert.update(f))
    _run_logbook_enrich(A, 'tok-test2', [('k1', 'LH099', LEG_DAY, 'MUC', 'FRA')])
    eintrag = gespeichert['k1']
    assert eintrag['reg'] == 'D-AIBH'
    assert eintrag['type'] == '319'
    assert eintrag['actual_arr_iso'] == '2026-07-15T10:05:00Z'


def _run_logbook_enrich(app_module, token, wanted):
    """`_logbook_enrich_async` startet einen Thread — hier synchron abwarten,
    damit der Test deterministisch bleibt."""
    import threading
    vorher = set(threading.enumerate())
    app_module._logbook_enrich_async(token, wanted)
    for t in threading.enumerate():
        if t not in vorher and t.is_alive():
            t.join(timeout=20)
