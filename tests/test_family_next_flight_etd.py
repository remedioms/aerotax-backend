"""Family next_flight: ETD = Abflug des ERSTEN Legs, nicht der Dienst-Beginn.

BUG (Prod, Tibor 2026-08-11): „family sagt tibor fliegt um 12 aber er fliegt
um 04". `next_flight_etd_iso` kam aus `ical_start` des Tages-Rows — bei Tibors
ICN-Rückflug der PICKUP 09:50 LT (00:50Z), fast 3 h vor dem LH-713-Abflug
(~03:35Z). Zusammen mit der (parallel gefixten) Stations-Zonen-Anzeige im
iOS-Client las die Familie eine Phantasie-Abflugzeit.

`_next_flight_etd_refined` zieht die Flugnummer aus dem Summary
(„LH 713: ICN-FRA") und fragt den zentralen Dual-Side-Resolver
(`app._flight_obs_merged`, free-only, memoisiert) nach der echten Plan-/
Est-Abflugzeit. Ohne Treffer bleibt der bisherige Tagesstart — keine
erfundenen Zeiten (Keine-Fake-Werte-Regel).
"""
import os

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import sys                                                # noqa: E402

import pytest                                             # noqa: E402

import app as A                                           # noqa: E402
import blueprints.family_watch as FW                      # noqa: E402

TIBOR_SUMMARY = ('Layover [ICN] (Tag 3/3) · 09:50 LT Pickup ICN '
                 '· LH 713: ICN-FRA')
TAGESSTART = '2026-08-12T00:50:00+00:00'   # = Pickup, NICHT der Abflug


@pytest.fixture(autouse=True)
def _pin_app():
    # sys.modules['app']-Pin gegen die test_calculation-Reimport-Kontamination
    # (gleiches Muster wie test_family_status_layover_gap).
    prev = sys.modules.get('app')
    sys.modules['app'] = A
    yield
    if prev is not None:
        sys.modules['app'] = prev


TIBOR_RAW_EVENT = {
    'legs': [{'from': 'ICN', 'to': 'FRA', 'dep': '12:20', 'arr': '18:40',
              'flight': 'LH 713'}],
    'ical_sectors': [{'from': 'ICN', 'to': 'FRA', 'flight': 'LH713',
                      'dep_iso': '2026-08-12T03:20:00Z',
                      'arr_iso': '2026-08-12T16:40:00Z'}],
}


def test_roster_sektor_schlaegt_alles(monkeypatch):
    """Tibors echter Row: der Import trägt die Leg-Abflugzeit als Instant
    (12:20 LT ICN = 03:20Z) — kein Resolver-Call nötig."""
    def boom(*a, **k):
        raise AssertionError('Resolver darf bei Sektor-Treffer nicht laufen')

    monkeypatch.setattr(A, '_flight_obs_merged', boom, raising=False)
    out = FW._next_flight_etd_refined(TIBOR_SUMMARY, '2026-08-12',
                                      'ICN', 'FRA', TAGESSTART,
                                      raw_event=TIBOR_RAW_EVENT)
    assert out == '2026-08-12T03:20:00Z'


def test_sektor_fremdes_legpaar_zaehlt_nicht(monkeypatch):
    """Ein Sektor eines ANDEREN Legs (z.B. Vortags-Anreise) darf die ETD nicht
    stellen — dann entscheidet die restliche Kaskade."""
    monkeypatch.setattr(A, '_flight_obs_merged', lambda *a, **k: None,
                        raising=False)
    fremd = {'ical_sectors': [{'from': 'FRA', 'to': 'ICN',
                               'dep_iso': '2026-08-09T10:00:00Z'}]}
    out = FW._next_flight_etd_refined(TIBOR_SUMMARY, '2026-08-12',
                                      'ICN', 'FRA', TAGESSTART,
                                      raw_event=fremd)
    assert out == '2026-08-12T00:50:00Z'


def test_resolver_abflug_schlaegt_tagesstart(monkeypatch):
    calls = {}

    def fake_resolver(fno, date=None, dep_iata=None, arr_iata=None,
                      free_only=False, live=True):
        calls['args'] = (fno, date, dep_iata, arr_iata, free_only, live)
        return {'sched_dep': '2026-08-12T03:35:00+00:00'}

    monkeypatch.setattr(A, '_flight_obs_merged', fake_resolver, raising=False)
    out = FW._next_flight_etd_refined(TIBOR_SUMMARY, '2026-08-12',
                                      'ICN', 'FRA', TAGESSTART)
    assert out == '2026-08-12T03:35:00Z'
    # Flugnummer normalisiert, Leg-Paar exakt, IMMER free-only (Family-Fan-out
    # darf keinen bezahlten Spend auslösen) UND cached-only (`live=False`):
    # der Family-Status ist ein Hot Path, er darf auf kein Live-Board warten.
    assert calls['args'] == ('LH713', '2026-08-12', 'ICN', 'FRA', True, False)


def test_hot_path_wartet_nie_auf_ein_live_board(monkeypatch):
    """Gegenprobe zum Default: NUR mit `cached_only=False` darf der Resolver
    live scannen. Ohne diesen Riegel hing der Family-Status an fremden
    HTTP-Timeouts (ein Board-Scan pro Freund/Tag)."""
    seen = {}

    def fake_resolver(fno, date=None, dep_iata=None, arr_iata=None,
                      free_only=False, live=True):
        seen['live'] = live
        return None

    monkeypatch.setattr(A, '_flight_obs_merged', fake_resolver, raising=False)
    FW._next_flight_etd_refined(TIBOR_SUMMARY, '2026-08-12', 'ICN', 'FRA',
                                TAGESSTART)
    assert seen['live'] is False
    FW._next_flight_etd_refined(TIBOR_SUMMARY, '2026-08-12', 'ICN', 'FRA',
                                TAGESSTART, cached_only=False)
    assert seen['live'] is True


def test_est_schlaegt_sched(monkeypatch):
    monkeypatch.setattr(
        A, '_flight_obs_merged',
        lambda *a, **k: {'sched_dep': '2026-08-12T03:35:00+00:00',
                         'esti_dep': '2026-08-12T04:10:00+00:00'},
        raising=False)
    out = FW._next_flight_etd_refined(TIBOR_SUMMARY, '2026-08-12',
                                      'ICN', 'FRA', TAGESSTART)
    assert out == '2026-08-12T04:10:00Z'


def test_ohne_resolver_treffer_bleibt_tagesstart(monkeypatch):
    monkeypatch.setattr(A, '_flight_obs_merged', lambda *a, **k: None,
                        raising=False)
    out = FW._next_flight_etd_refined(TIBOR_SUMMARY, '2026-08-12',
                                      'ICN', 'FRA', TAGESSTART)
    assert out == '2026-08-12T00:50:00Z'


def test_ohne_flugnummer_im_summary_bleibt_tagesstart(monkeypatch):
    def boom(*a, **k):
        raise AssertionError('Resolver darf ohne Flugnummer nicht laufen')

    monkeypatch.setattr(A, '_flight_obs_merged', boom, raising=False)
    out = FW._next_flight_etd_refined('FRA-ICN ohne Flugnummer', '2026-08-12',
                                      'FRA', 'ICN', TAGESSTART)
    assert out == '2026-08-12T00:50:00Z'


def test_ohne_fallback_und_ohne_treffer_none(monkeypatch):
    monkeypatch.setattr(A, '_flight_obs_merged', lambda *a, **k: None,
                        raising=False)
    assert FW._next_flight_etd_refined(TIBOR_SUMMARY, '2026-08-12',
                                       'ICN', 'FRA', None) is None


def test_resolver_absturz_faellt_leise_zurueck(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError('resolver kaputt')

    monkeypatch.setattr(A, '_flight_obs_merged', boom, raising=False)
    out = FW._next_flight_etd_refined(TIBOR_SUMMARY, '2026-08-12',
                                      'ICN', 'FRA', TAGESSTART)
    assert out == '2026-08-12T00:50:00Z'
