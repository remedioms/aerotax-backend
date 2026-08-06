# Cross-Date-Guard für die dep-Row-Auswahl (Owner LH780 2026-08-06, 03:00):
# Die GESTRIGE 21:50-Board-Row (date=Vortag, esti 22:25 „+35", nach Mitternacht
# noch im Heute-Store und zuletzt „gestartet") dekorierte das MORGIGE Leg
# derselben täglichen Flugnummer — die Verspätung „flackerte" auf der
# Briefing-Karte, je nachdem welcher Status-/Cache-Stand gerade gewann.
# `_board_day_midnight_ok` allein lässt den Fall durch (diff=-1 bei
# sched-Stunde ≥21 ist Red-Eye-Toleranz und passt auf JEDEN Abendflug).
# Der Guard vergleicht deshalb Leg-Sollzeit und Row-Zeitstempel direkt
# (±6 h, Flughafen-Ortszeit): `_obs_dep_same_instance`.
from datetime import timezone

import pytest

import app as A

from _clock_freeze import FROZEN_DATE, apply_frozen_clock


@pytest.fixture(autouse=True)
def _freeze_clock(monkeypatch):
    apply_frozen_clock(monkeypatch, app_module=A)
    yield


@pytest.fixture(autouse=True)
def _clear_caches():
    A._FLIGHT_MERGE_CACHE.clear()
    yield
    A._FLIGHT_MERGE_CACHE.clear()


TODAY = FROZEN_DATE.isoformat()            # 2026-07-16 (eingefrorene Uhr)
YESTERDAY = '2026-07-15'

# Die Vortags-Row, wie sie nach Mitternacht real im Heute-Store lag:
VORTAGS_ROW = {'flight': 'LH780', 'date': YESTERDAY, 'sched': '21:50',
               'esti': f'{YESTERDAY}T22:25:00+0200', 'status': 'gestartet',
               'dest_iata': 'SIN'}
# Soll-Abflug des HEUTIGEN Legs: 21:50 CEST = 19:50Z.
LEG_DEP_ISO = f'{TODAY}T19:50:00Z'


# ── Helper-Ebene ─────────────────────────────────────────────────────────────
def test_vortags_row_mit_identischer_wanduhr_ist_fremde_instanz():
    assert A._obs_dep_same_instance(
        VORTAGS_ROW, 'FRA', TODAY, LEG_DEP_ISO) is False


def test_mitternachtsrutscher_bleibt_dieselbe_instanz():
    # Red-Eye: Board führt den Lauf unter dem Vortag (sched 23:55), das Leg
    # keyt auf heute mit Abflug 00:30 Ortszeit (= 22:30Z am Vortag).
    row = dict(VORTAGS_ROW, sched='23:55')
    leg = f'{YESTERDAY}T22:30:00Z'          # 00:30 CEST am TODAY
    assert A._obs_dep_same_instance(row, 'FRA', TODAY, leg) is True


def test_row_ohne_datum_bleibt_tolerant():
    row = {k: v for k, v in VORTAGS_ROW.items() if k != 'date'}
    assert A._obs_dep_same_instance(row, 'FRA', TODAY, LEG_DEP_ISO) is True


def test_fallback_ohne_leg_sollzeit_exaktes_datum_passt():
    row = dict(VORTAGS_ROW, date=TODAY)
    assert A._obs_dep_same_instance(row, 'FRA', TODAY, None) is True


def test_helper_wirft_nie(monkeypatch):
    assert A._obs_dep_same_instance({'date': 'kaputt', 'sched': object()},
                                    'FRA', TODAY, LEG_DEP_ISO) in (True, False)


# ── Merge-Ebene (_flight_obs_merged → _obs_lookup, Heute-Store-Pfad) ─────────
def _stores_mit_vortags_row(monkeypatch):
    def fake_store(key):
        return [dict(VORTAGS_ROW)] if key == 'FRA' else []
    monkeypatch.setattr(A, '_departed_rows_from_store', fake_store)


def test_merge_verwirft_vortags_row_mit_leg_sollzeit(monkeypatch):
    _stores_mit_vortags_row(monkeypatch)
    m = A._flight_obs_merged('LH780', date=TODAY, dep_iata='FRA',
                             arr_iata='SIN', live=False,
                             dep_sched_iso=LEG_DEP_ISO)
    # Einzige Kandidatin ist die fremde Vortags-Instanz ⇒ EHRLICH nichts.
    assert m is None or not m.get('has_dep')


def test_merge_ohne_sollzeit_behaelt_altes_verhalten(monkeypatch):
    # Ohne Leg-Sollzeit greift nur die (dokumentiert schwächere) Mitternachts-
    # Toleranz — Bestands-Caller ohne dep_sched_iso ändern ihr Verhalten nicht.
    _stores_mit_vortags_row(monkeypatch)
    m = A._flight_obs_merged('LH780', date=TODAY, dep_iata='FRA',
                             arr_iata='SIN', live=False)
    assert m is not None and m.get('has_dep')


def test_merge_cache_trennt_nach_sollzeit(monkeypatch):
    # Der Cache-Key muss dep_sched_iso enthalten — sonst vergiftet ein Aufruf
    # ohne Sollzeit den nächsten mit.
    _stores_mit_vortags_row(monkeypatch)
    m1 = A._flight_obs_merged('LH780', date=TODAY, dep_iata='FRA',
                              arr_iata='SIN', live=False)
    m2 = A._flight_obs_merged('LH780', date=TODAY, dep_iata='FRA',
                              arr_iata='SIN', live=False,
                              dep_sched_iso=LEG_DEP_ISO)
    assert m1 is not None and m1.get('has_dep')
    assert m2 is None or not m2.get('has_dep')
