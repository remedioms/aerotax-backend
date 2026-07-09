"""Planmäßige Flugdauer / Ankunftszeit — GRATIS aus Soll-Ab/-An abgeleitet.

Owner-Wunsch 2026-07-09 (Screenshot LH1128 FRA→BCN „hier fehlt abflug ankunft
zeit … es fehlt auch flugzeit"): Ankunftszeit + Flugdauer sind bereits im
Warehouse (route-history sched_arr, ~67-100% Abdeckung), nur nicht überall
verdrahtet. Fix: reiner TZ-korrekter Helper `_sched_block_min` + Ausgabe in
`/api/flight/<token>/status` (Suche/Dienstplan) und `/api/ax/route-history`
(„DIESE STRECKE ZULETZT"). KEIN Drittanbieter-Spend, keine erfundene Dauer.

Kritisch getestet:
  • TZ-Korrektheit: dep/arr stehen je in IHRER Stations-Ortszeit → cross-TZ
    (FRA→JFK) muss über UTC gerechnet werden, nicht per String-Subtraktion.
  • Plausibilitäts-Guard: fehlend/negativ/unrealistisch → None (nie erfunden).
  • Endpoint-Wiring: duration_min erscheint in flight_status wenn beide Soll-
    Zeiten da sind, sonst None.
  • route-history: duration_min pro Flug bei dep-beobachteten Legs, NICHT bei
    reinen arr-Rows (dort ist sched==sched_arr).
"""
import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

import app as A


@pytest.fixture(autouse=True)
def _pin_app_module():
    # Gleiches sys.modules-Pinning wie test_my_flight_status (Order-Kontamination
    # durch test_calculation's Reimport-Trick).
    import sys
    _prev = sys.modules.get('app')
    sys.modules['app'] = A
    try:
        A._FLIGHT_MERGE_CACHE.clear()
    except Exception:
        pass
    yield
    if _prev is not None:
        sys.modules['app'] = _prev


@pytest.fixture
def client():
    A.app.testing = True
    return A.app.test_client()


# ─────────────────────────── _sched_block_min (pure) ───────────────────────────

def test_block_same_timezone():
    """Innereuropäisch (beide Europe/…, im Juli UTC+2): einfache Differenz."""
    # FRA 11:00 → MUC 12:05 = 65 min
    assert A._sched_block_min('2026-07-09T11:00:00', 'FRA',
                              '2026-07-09T12:05:00', 'MUC') == 65
    # FRA 11:00 → BCN 13:05 = 125 min (2h05, echte LH1128-Dauer)
    assert A._sched_block_min('2026-07-09T11:00:00', 'FRA',
                              '2026-07-09T13:05:00', 'BCN') == 125


def test_block_cross_timezone():
    """DER kritische Fall: FRA (UTC+2) → JFK (UTC-4, EDT). Naive String-Diff
    ergäbe 2h50 (falsch); UTC-korrekt sind es 8h50 = 530 min."""
    assert A._sched_block_min('2026-07-09T11:00:00', 'FRA',
                              '2026-07-09T13:50:00', 'JFK') == 530


def test_block_overnight():
    """Overnight-Flug (arr am Folgetag) — Datum in den Board-Strings macht es
    korrekt. FRA 22:00 → JFK 00:30(+1) EDT."""
    m = A._sched_block_min('2026-07-09T22:00:00', 'FRA',
                           '2026-07-10T00:30:00', 'JFK')
    # 22:00 CEST = 20:00Z ; 00:30(+1) EDT = 04:30Z(+1) → 8h30 = 510
    assert m == 510


def test_block_missing_inputs_none():
    for args in (
        (None, 'FRA', '2026-07-09T12:00:00', 'MUC'),
        ('2026-07-09T11:00:00', 'FRA', None, 'MUC'),
        ('2026-07-09T11:00:00', None, '2026-07-09T12:00:00', 'MUC'),
        ('2026-07-09T11:00:00', 'FRA', '2026-07-09T12:00:00', None),
        ('', '', '', ''),
    ):
        assert A._sched_block_min(*args) is None


def test_block_negative_none():
    """Ankunft vor Abflug (Datenfehler) → None, nie eine negative Dauer."""
    assert A._sched_block_min('2026-07-09T11:00:00', 'FRA',
                              '2026-07-09T10:00:00', 'FRA') is None


def test_block_zero_none():
    """Exakt 0 Minuten ist keine echte Flugdauer → None (Guard 0<min<=20h)."""
    assert A._sched_block_min('2026-07-09T11:00:00', 'FRA',
                              '2026-07-09T11:00:00', 'FRA') is None


def test_block_implausibly_long_none():
    """>20 h → None. TZ-Helper gemockt, damit die Plausibilitätsgrenze isoliert
    geprüft wird (unabhängig von echten Flughäfen)."""
    with patch.object(A, '_board_local_to_utc_iso',
                      side_effect=['2026-07-09T00:00:00Z',
                                   '2026-07-10T02:00:00Z']):  # 26 h
        assert A._sched_block_min('x', 'AAA', 'y', 'BBB') is None


def test_block_unknown_tz_none():
    """Unbekannte Stations-TZ (Helper gibt None) → None statt naiver Rechnung."""
    with patch.object(A, '_board_local_to_utc_iso',
                      side_effect=['2026-07-09T09:00:00Z', None]):
        assert A._sched_block_min('x', 'FRA', 'y', 'ZZZ') is None


# ─────────────────────── flight_status endpoint wiring ───────────────────────

def _merged_record(sched_arr='2026-07-09T13:05:00'):
    return {
        'ok': True, 'flight': 'LH1128',
        'dep_iata': 'FRA', 'arr_iata': 'BCN',
        'airline': 'LH', 'origin_name': 'Frankfurt', 'dest_name': 'Barcelona',
        'sched_dep': '2026-07-09T11:00:00', 'sched_arr': sched_arr,
        'esti_dep': None, 'esti_arr': None,
        'gate_dep': 'A17', 'terminal_dep': '1',
        'gate_arr': None, 'terminal_arr': None,
        'status': '', 'cancelled': False,
        'dep_delay_min': 0, 'arr_delay_min': None,
        'delay_min': 0, 'delay_side': 'dep', 'delay_known': True,
        'reg': 'DAINV', 'aircraft': 'A20N',
        'sides': {'dep': 'obs', 'arr': 'obs'},
        'has_dep': True, 'has_arr': sched_arr is not None,
    }


def test_flight_status_includes_duration(client):
    """duration_min (=125) + sched_arr erscheinen im flight_status, wenn beide
    Soll-Zeiten beobachtet sind — GRATIS, source bleibt aerox_obs_merged."""
    with patch.object(A, '_validate_token_exists', return_value='u1'), \
         patch.object(A, '_flight_obs_merged', return_value=_merged_record()):
        r = client.get('/api/flight/AT-DURTEST/status?number=LH1128')
    assert r.status_code == 200
    body = r.get_json()
    assert body.get('ok') is True
    assert body.get('source') == 'aerox_obs_merged'
    f = body['flight']
    assert f['sched_dep'] == '2026-07-09T11:00:00'
    assert f['sched_arr'] == '2026-07-09T13:05:00'
    assert f['duration_min'] == 125


def test_flight_status_duration_none_without_arr(client):
    """Kein sched_arr beobachtet → duration_min None (nichts erfunden)."""
    with patch.object(A, '_validate_token_exists', return_value='u1'), \
         patch.object(A, '_flight_obs_merged',
                      return_value=_merged_record(sched_arr=None)):
        r = client.get('/api/flight/AT-DURTEST/status?number=LH1128')
    assert r.status_code == 200
    f = r.get_json()['flight']
    assert f['sched_arr'] is None
    assert f['duration_min'] is None


# ─────────────────────── route-history endpoint wiring ───────────────────────

def _route_rows():
    dep_row = {'flight': 'LH1128', 'airline': 'LH', 'dest_iata': 'BCN',
               'sched': '2026-07-09T11:00:00', 'delay_min': 0,
               'delay_known': True, 'cancelled': False}
    arr_row = {'flight': 'LH1128', 'airline': 'LH', 'dest_iata': 'FRA',
               'sched': '2026-07-09T13:05:00', 'delay_min': 0,
               'delay_known': True, 'cancelled': False}
    return dep_row, arr_row


def _fixed_now(_key):
    return datetime(2026, 7, 9, 15, 0, tzinfo=timezone.utc)


def test_route_history_includes_duration(client):
    """route-history-Flug trägt duration_min=125 bei dep+arr-Beobachtung."""
    dep_row, arr_row = _route_rows()

    def fake_departed(key):
        return [arr_row] if str(key).endswith('#ARR') else [dep_row]

    with patch.object(A, '_airport_local_now', side_effect=_fixed_now), \
         patch.object(A, '_departed_rows_from_store', side_effect=fake_departed), \
         patch.object(A, '_ax_codeshare_map', return_value={}):
        r = client.get('/api/ax/route-history/FRA/BCN?days=1')
    assert r.status_code == 200
    days = r.get_json()['recent_days']
    assert days, 'expected at least one day'
    flights = days[0]['flights']
    lh = next(f for f in flights if f['flight'] == 'LH1128')
    assert lh.get('sched_arr') == '2026-07-09T13:05:00'
    assert lh.get('duration_min') == 125


def test_route_history_no_duration_for_arr_only(client):
    """Reiner arr-Row (Abflug-Board sah den Flug nicht): sched==sched_arr →
    KEINE erfundene Dauer aus einer Nullspanne."""
    _dep, arr_row = _route_rows()

    def fake_departed(key):
        # Nur die Ankunftsseite liefert etwas; dep-Store ist leer.
        return [arr_row] if str(key).endswith('#ARR') else []

    with patch.object(A, '_airport_local_now', side_effect=_fixed_now), \
         patch.object(A, '_departed_rows_from_store', side_effect=fake_departed), \
         patch.object(A, '_ax_codeshare_map', return_value={}):
        r = client.get('/api/ax/route-history/FRA/BCN?days=1')
    assert r.status_code == 200
    days = r.get_json()['recent_days']
    if days:
        for f in days[0]['flights']:
            assert 'duration_min' not in f or f['duration_min'] is None
