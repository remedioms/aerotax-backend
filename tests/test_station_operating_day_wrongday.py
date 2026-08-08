"""BETRIEBSTAG STATT UTC-TAG — 24-h-Nachbar am Red-Eye (Sweep-Befund 2026-08-08).

SYMPTOM (Monitor `sweep_kalender.py`, wörtlich):
    R2 zukunft-mit-ist: AT-B04481… 2026-08-08 LH757 BOM->FRA
        est_dep=2026-08-07T21:41:00Z est_arr=2026-08-08T06:21:00Z
    R4 wrongday-est:    AT-B04481… 2026-08-08 LH757 BOM->FRA
        est_arr=2026-08-08T06:21:00Z sched_arr=2026-08-09T06:05:00Z

ROOT CAUSE: der Dienstplan war RICHTIG (dep 2026-08-08T21:10Z, arr
2026-08-09T06:05Z = LH757-Muster, an BOM 09.08. 02:40 Ortszeit). Falsch war die
ANREICHERUNG: `_enrich_leg_delays` bildete den Datums-Schlüssel als UTC-Tag des
Abflugs ('2026-08-08'). Flughafen-Tafeln, `airport_delay_obs` und die LH Open
API keyen aber am KALENDERTAG DER ABFLUG-STATION — an BOM (UTC+5:30) ist das
der 09.08. Mit dem UTC-Tag antwortete LH mit dem Lauf der VORNACHT (Prod-Cache
`lhfacts:LH757:2026-08-08:BOM:FRA`: est_dep 03:11 IST, est_arr 08:21 CEST) —
exakt die beanstandeten Werte.

Die vorhandenen Guards griffen nicht: `_obs_dep_same_instance` (LH780,
2026-08-06) prüft nur BOARD-Rows, und der LH-Overlay läuft genau dann, wenn
beide Board-Seiten FEHLEN. Deshalb hier zwei Ebenen:
  1. `_station_operating_day` — der Schlüssel selbst wird stations-lokal
     gerechnet (die Ursache),
  2. `_lh_facts_same_instance` / `_gate_facts_dep_against_leg` — Physik-Riegel,
     falls eine Quelle trotzdem eine fremde Instanz liefert.

GEGENPROBE (Pflicht): ein Flug, der WIRKLICH am Zielort über Mitternacht
landet, muss weiterhin den Ankunfts-Betriebstag +1 bekommen (LH756 FRA→BOM).
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as A  # noqa: E402
from blueprints import lh_open_api  # noqa: E402

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


# ── Das Original-Szenario, auf die eingefrorene Uhr gelegt ───────────────────
# FROZEN_UTC = 2026-07-16 10:00Z. Der Red-Eye startet am selben UTC-Tag um
# 21:10Z — an BOM (UTC+5:30, keine Sommerzeit) ist das bereits der 17.07.,
# 02:40 Ortszeit. Ankunft 06:05Z ist in FRA der 17.07., 08:05 Ortszeit.
DEP_UTC = '2026-07-16T21:10:00Z'
ARR_UTC = '2026-07-17T06:05:00Z'
BOM_DAY = '2026-07-17'          # Betriebstag an der ABFLUG-Station
UTC_DAY = '2026-07-16'          # der Tag, mit dem bisher gefragt wurde


def _sector():
    return {'flight': 'LH757', 'from': 'BOM', 'to': 'FRA',
            'dep_iso': DEP_UTC, 'arr_iso': ARR_UTC,
            'sched_dep_iso': DEP_UTC, 'sched_arr_iso': ARR_UTC}


# ═══ 1. Der Helper selbst ════════════════════════════════════════════════════
def test_station_operating_day_bom_red_eye_ist_der_folgetag():
    assert A._station_operating_day(DEP_UTC, 'BOM') == BOM_DAY
    assert A._station_operating_day(DEP_UTC, 'BOM') != UTC_DAY


def test_station_operating_day_westzone_ist_der_vortag():
    # LAX (UTC−7): ein Abflug 00:30Z ist lokal noch der Vorabend.
    assert A._station_operating_day('2026-07-17T00:30:00Z', 'LAX') \
        == '2026-07-16'


def test_station_operating_day_ohne_zonenversatz_unveraendert():
    # FRA-Mittagsabflug: Betriebstag == UTC-Tag (keine Verhaltensänderung).
    assert A._station_operating_day('2026-07-16T11:00:00Z', 'FRA') \
        == '2026-07-16'


def test_station_operating_day_akzeptiert_datetime_und_naiv_als_utc():
    aware = datetime(2026, 7, 16, 21, 10, tzinfo=timezone.utc)
    naive = datetime(2026, 7, 16, 21, 10)
    assert A._station_operating_day(aware, 'BOM') == BOM_DAY
    assert A._station_operating_day(naive, 'BOM') == BOM_DAY


def test_station_operating_day_raet_nie():
    # Unbekannte Station / unparsbare Zeit → None, damit der Aufrufer sein
    # bisheriges Verhalten behält (Owner-Regel „keine Fake-Werte").
    assert A._station_operating_day(DEP_UTC, 'ZZZ') is None
    assert A._station_operating_day(DEP_UTC, '') is None
    assert A._station_operating_day('kaputt', 'BOM') is None
    assert A._station_operating_day(None, 'BOM') is None


# ═══ 2. Instanz-Riegel für die LH-Fakten ═════════════════════════════════════
LH_NACHBAR = {          # der Lauf der VORNACHT (das, was live geliefert wurde)
    'sched_dep': '2026-07-16T02:40:00+05:30',
    'est_dep': '2026-07-16T03:11:00+05:30',
    'dep_delay_min': 31,
    'sched_arr': '2026-07-16T08:05:00+02:00',
    'est_arr': '2026-07-16T08:21:00+02:00',
    'arr_delay_min': 16,
    'reg': 'D-ABQB', 'type': '789',
    'dep_iata': 'BOM', 'arr_iata': 'FRA',
}
LH_RICHTIG = {          # derselbe Flug, aber die Instanz DIESES Legs
    'sched_dep': '2026-07-17T02:40:00+05:30',
    'est_dep': '2026-07-17T03:40:00+05:30',
    'dep_delay_min': 60,
    'sched_arr': '2026-07-17T08:05:00+02:00',
    'est_arr': '2026-07-17T09:00:00+02:00',
    'arr_delay_min': 55,
    'reg': 'D-ABQC', 'type': '789',
    'dep_iata': 'BOM', 'arr_iata': 'FRA',
}


def test_lh_facts_same_instance_wirft_24h_nachbarn_raus():
    assert A._lh_facts_same_instance(LH_NACHBAR, DEP_UTC) is False
    assert A._lh_facts_same_instance(LH_RICHTIG, DEP_UTC) is True


def test_lh_facts_same_instance_erlaubt_umplanung():
    # LH terminiert einen Flug real um Stunden um (LH757 07.08.: 02:40 → 06:30).
    umgeplant = dict(LH_RICHTIG, sched_dep='2026-07-17T06:30:00+05:30')
    assert A._lh_facts_same_instance(umgeplant, DEP_UTC) is True


def test_lh_facts_same_instance_ohne_evidenz_tolerant():
    assert A._lh_facts_same_instance(LH_RICHTIG, None) is True
    assert A._lh_facts_same_instance({'est_dep': 'x'}, DEP_UTC) is True
    assert A._lh_facts_same_instance({}, DEP_UTC) is True
    assert A._lh_facts_same_instance({'sched_dep': 'kaputt'}, DEP_UTC) is True


# ═══ 3. Der LH-Overlay fragt den BETRIEBSTAG ═════════════════════════════════
def test_lh_overlay_fragt_stations_betriebstag_nicht_utc_tag(monkeypatch):
    gefragt = {}

    def fake(fn, d, dep, arr, force=False, cached_only=False, caller=None,
             shared_read=True):
        gefragt['date'] = d
        return dict(LH_RICHTIG) if d == BOM_DAY else dict(LH_NACHBAR)

    monkeypatch.setattr(lh_open_api, 'lh_flight_facts', fake)
    out = A._lh_fill_obs_merged(None, 'LH757', UTC_DAY, 'BOM', 'FRA',
                                dep_sched_iso=DEP_UTC)
    assert gefragt['date'] == BOM_DAY, 'LH keyt am Abflug-Ortstag, nicht in UTC'
    assert out is not None
    assert out['est_dep_iso'].startswith('2026-07-16T22:10')   # 03:40 IST 17.07.
    assert out['est_arr_iso'].startswith('2026-07-17T07:00')   # 09:00 CEST


def test_lh_overlay_ohne_sollzeit_unveraendert(monkeypatch):
    # Bestands-Caller ohne dep_sched_iso verhalten sich exakt wie vorher.
    gefragt = {}

    def fake(fn, d, dep, arr, force=False, cached_only=False, caller=None,
             shared_read=True):
        gefragt['date'] = d
        return dict(LH_NACHBAR)

    monkeypatch.setattr(lh_open_api, 'lh_flight_facts', fake)
    A._lh_fill_obs_merged(None, 'LH757', UTC_DAY, 'BOM', 'FRA')
    assert gefragt['date'] == UTC_DAY


def test_lh_overlay_verwirft_fremde_instanz(monkeypatch):
    # Zweite Schranke: liefert die Quelle trotz richtigem Schlüssel den
    # Nachbartag, darf NICHTS davon ans Leg (lieber keine Zeile als eine
    # falsche).
    monkeypatch.setattr(lh_open_api, 'lh_flight_facts',
                        lambda *a, **k: dict(LH_NACHBAR))
    assert A._lh_fill_obs_merged(None, 'LH757', UTC_DAY, 'BOM', 'FRA',
                                 dep_sched_iso=DEP_UTC) is None
    rec = {'sched_dep': '02:40', 'esti_dep': None, 'sched_arr': None,
           'esti_arr': None, 'arr_delay_min': None, 'est_dep_iso': None,
           'est_arr_iso': None, 'reg': None, 'delay_min': None}
    out = A._lh_fill_obs_merged(dict(rec), 'LH757', UTC_DAY, 'BOM', 'FRA',
                                dep_sched_iso=DEP_UTC)
    assert out == rec, 'ein Board-Record darf nicht mit Fremd-Fakten wachsen'


# ═══ 4. Physik-Riegel auf der Abflug-Seite (Spiegel zum arr-Gate) ════════════
def test_gate_facts_dep_wirft_24h_nachbarn_beider_richtungen():
    frueh = A._gate_facts_dep_against_leg(
        {'est_dep': '2026-07-15T21:41:00Z', 'dep_delay_min': 31,
         'dep_status': 'gestartet', 'reg': 'D-ABQB'}, DEP_UTC)
    assert frueh['est_dep'] is None and frueh['dep_delay_min'] is None
    assert frueh['reg'] == 'D-ABQB', 'reg/Gate bleiben unangetastet'
    spaet = A._gate_facts_dep_against_leg(
        {'est_dep': '2026-07-17T21:41:00Z'}, DEP_UTC)
    assert spaet['est_dep'] is None


def test_gate_facts_dep_laesst_echte_verspaetung_stehen():
    # Auch eine sehr große, aber reale Verspätung bleibt erhalten.
    for delay_h in (0, 1, 5, 12, 19):
        ist = (datetime(2026, 7, 16, 21, 10, tzinfo=timezone.utc)
               + timedelta(hours=delay_h)).isoformat()
        out = A._gate_facts_dep_against_leg({'est_dep': ist}, DEP_UTC)
        assert out['est_dep'] == ist, f'{delay_h} h Delay ist real'


def test_gate_facts_dep_wirft_nie():
    assert A._gate_facts_dep_against_leg(None, DEP_UTC) is None
    assert A._gate_facts_dep_against_leg({'est_dep': 'x'}, DEP_UTC) \
        == {'est_dep': 'x'}
    assert A._gate_facts_dep_against_leg({'est_dep': DEP_UTC}, None) \
        == {'est_dep': DEP_UTC}


# ═══ 5. Ende-zu-Ende: welchen Tag fragt `_enrich_leg_delays`? ════════════════
def _capture_merge(monkeypatch):
    """Fängt die Datums-Argumente ab, mit denen der Enricher fragt."""
    calls = []

    def fake(flight_no, date=None, dep_iata=None, arr_iata=None, live=True,
             free_only=False, arr_date=None, dep_sched_iso=None):
        calls.append({'flight': flight_no, 'date': date, 'arr_date': arr_date,
                      'dep': dep_iata, 'arr': arr_iata,
                      'dep_sched_iso': dep_sched_iso})
        return None        # None ⇒ der Enricher schreibt NICHTS ans Leg

    monkeypatch.setattr(A, '_flight_obs_merged', fake)
    return calls


def test_enrich_fragt_den_abflug_betriebstag_der_station(monkeypatch):
    calls = _capture_merge(monkeypatch)
    A._enrich_leg_delays([_sector()], UTC_DAY, free_only=True)
    assert calls, 'der Leg muss im Anreicherungsfenster liegen'
    c = calls[0]
    assert c['date'] == BOM_DAY, (
        'Datums-Schlüssel muss der Betriebstag an BOM sein, nicht der UTC-Tag')
    assert c['dep_sched_iso'] == DEP_UTC
    # Abflug- UND Ankunfts-Betriebstag sind hier DERSELBE Tag (BOM 17.07.
    # 02:40 → FRA 17.07. 08:05) ⇒ kein arr-Sonderweg nötig.
    assert c['arr_date'] is None


def test_gegenprobe_echte_uebernachtankunft_bekommt_plus_einen_tag(monkeypatch):
    # LH756 FRA→BOM: Abflug 16.07. 13:00 FRA-Ortszeit, Ankunft 17.07. 01:00
    # BOM-Ortszeit. Das ist eine ECHTE Ankunft am Folgetag — der Ankunfts-
    # Betriebstag MUSS +1 bleiben, sonst hat der Fix den Nico-LH423-Fix
    # zerstört.
    calls = _capture_merge(monkeypatch)
    A._enrich_leg_delays([{
        'flight': 'LH756', 'from': 'FRA', 'to': 'BOM',
        'dep_iso': '2026-07-16T11:00:00Z',
        'arr_iso': '2026-07-16T19:30:00Z',
    }], UTC_DAY, free_only=True)
    assert calls
    c = calls[0]
    assert c['date'] == '2026-07-16', 'FRA-Mittagsabflug bleibt der 16.07.'
    assert c['arr_date'] == '2026-07-17', (
        'die Ankunft liegt an BOM auf dem Folgetag — arr-Seite muss dort lesen')


def test_gegenprobe_reiner_tagesflug_unveraendert(monkeypatch):
    # Kein Zonenversatz, keine Mitternacht: Verhalten muss byte-gleich bleiben.
    calls = _capture_merge(monkeypatch)
    A._enrich_leg_delays([{
        'flight': 'LH100', 'from': 'FRA', 'to': 'MUC',
        'dep_iso': '2026-07-16T09:00:00Z',
        'arr_iso': '2026-07-16T10:00:00Z',
    }], UTC_DAY, free_only=True)
    assert calls
    assert calls[0]['date'] == '2026-07-16'
    assert calls[0]['arr_date'] is None


def test_der_beanstandete_leg_bekommt_keine_fremden_ist_zeiten(monkeypatch):
    """Der Sweep-Fall komplett: LH liefert NUR den Nachbartag → das Leg bleibt
    ohne est_*. Vorher standen dort est_dep/est_arr des Vortags (R2 + R4)."""
    monkeypatch.setattr(lh_open_api, 'lh_flight_facts',
                        lambda *a, **k: dict(LH_NACHBAR))
    monkeypatch.setattr(A, '_departed_rows_from_store', lambda key: [])
    monkeypatch.setattr(A, '_board_rows_from_obs_for_date',
                        lambda *a, **k: [])
    sec = _sector()
    A._enrich_leg_delays([sec], UTC_DAY, free_only=True)
    assert sec.get('est_dep_iso') is None
    assert sec.get('est_arr_iso') is None
    # Und der Plan des Legs bleibt unangetastet.
    assert sec['dep_iso'] == DEP_UTC and sec['arr_iso'] == ARR_UTC
