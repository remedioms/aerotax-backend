"""has_track-Read flight-indiziert (Latenz-Wurzel Flugsuche 2026-07-22).

Root-Cause: aircraft_track (~19,4 Mio Breadcrumb-Rows) hat KEINEN Index auf
(origin, dest) — der unkeyed origin/dest+seen_ts-Scan in
`_route_track_flight_set` kostete live 8–9 s PRO TAG und machte route-history
(9 s bei days=3, 18 s bei days=7) und damit die kalte Flug-Detailseite zum
Latenz-Pol (das flight-detail-Aggregat kappte den history-Subcall am
6-s-Budget → Seite brauchte 6 s UND kam ohne Historie). Die Spalte `flight`
IST indiziert (0,25 s) und der Caller kennt die Flugnummern des Tages aus den
Board-Rows → serverseitig per `.in_('flight', …)` einschränken.

Deckt ab (hermetisch, alle Quellen gemockt):
  • ax_route_history reicht die Tages-Flugnummern (roh + `_fn_norm`-Variante +
    cs_map-Operating-Nummer) an `_route_track_flight_set`.
  • Tag ohne Flüge → GAR KEINE Track-Query.
  • `_route_track_flight_set` setzt den `.in_`-Filter, liefert das normalisierte
    Set, und schaltet bei leerer flight_nos-Liste kurz (kein SB-Kontakt);
    `flight_nos=None` = alter unkeyed Fallback ohne `.in_`.
"""
import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import app as A


def _fake_local_now(code):
    return datetime(2026, 7, 12, 18, 51)


def _row(flight, dest, sched, delay=0, known=True):
    return {'flight': flight, 'airline': flight[:2], 'dest_iata': dest,
            'sched': sched, 'delay_min': (delay if known else None),
            'delay_known': known, 'cancelled': False, 'gaveup': False}


def _call_route_history(dep_store, cs_map=None, days=1, captured=None):
    """ax_route_history SIN→FRA mit gemockten Quellen; captured sammelt die
    flight_nos, die am Track-Read ankommen."""
    def fake_store(key):
        k = (key or '').upper()
        return [] if k.endswith('#ARR') else dep_store

    def fake_trk(frm, to, day, lo, hi, flight_nos=None):
        if captured is not None:
            captured.append(flight_nos)
        return set()

    with patch.object(A, '_airport_local_now', side_effect=_fake_local_now), \
            patch.object(A, '_store_key_for',
                         side_effect=lambda ap, kind:
                         (ap + '#ARR') if kind == 'arrival' else ap), \
            patch.object(A, '_departed_rows_from_store',
                         side_effect=fake_store), \
            patch.object(A, '_board_rows_from_obs_for_date',
                         return_value=[]), \
            patch.object(A, '_ax_codeshare_map', return_value=(cs_map or {})), \
            patch.object(A, '_route_track_flight_set', side_effect=fake_trk), \
            patch.object(A, '_sched_block_min', return_value=None):
        with A.app.test_request_context(
                f'/api/ax/route-history/SIN/FRA?days={days}'):
            resp = A.ax_route_history('SIN', 'FRA')
    if isinstance(resp, tuple):
        resp = resp[0]
    return resp.get_json()


def test_track_query_receives_day_flight_numbers():
    # SQ026 (roh) muss als SQ026 + SQ26 (norm) + LH9999 (cs_map-Operating —
    # die Faltung kann den Entry auf eine nie beobachtete Operating-Nummer
    # umlabeln) am Track-Read ankommen.
    captured = []
    dep = [_row('SQ026', 'FRA', '2026-07-12T06:40:00', delay=0)]
    _call_route_history(dep, cs_map={'SQ26': 'LH9999'}, captured=captured)
    assert len(captured) == 1
    assert captured[0] is not None
    assert {'SQ026', 'SQ26', 'LH9999'} <= set(captured[0])


def test_day_without_flights_skips_track_query():
    captured = []
    _call_route_history([], days=2, captured=captured)
    assert captured == []


class _FakeQuery:
    def __init__(self, calls, data):
        self._calls = calls
        self._data = data
        self.filters = {}

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self.filters['eq:' + col] = val
        return self

    def gte(self, col, val):
        self.filters['gte:' + col] = val
        return self

    def lt(self, col, val):
        self.filters['lt:' + col] = val
        return self

    def in_(self, col, vals):
        self.filters['in:' + col] = list(vals)
        return self

    def limit(self, n):
        return self

    def execute(self):
        self._calls.append(self.filters)
        return SimpleNamespace(data=self._data)


class _FakeSB:
    def __init__(self, calls, data):
        self._calls = calls
        self._data = data

    def table(self, name):
        assert name == 'aircraft_track'
        return _FakeQuery(self._calls, self._data)


def test_route_track_flight_set_applies_in_filter():
    calls = []
    fake = _FakeSB(calls, [{'flight': 'SQ26'}, {'flight': 'LH0400'}])
    with patch.object(A, 'sb', fake):
        got = A._route_track_flight_set(
            'SIN', 'FRA', '2026-07-12', 'lo', 'hi',
            flight_nos={'SQ026', 'SQ26'})
    assert got == {'SQ26', 'LH400'}          # Ausgabe bleibt _fn_norm-normalisiert
    assert len(calls) == 1
    assert calls[0]['in:flight'] == ['SQ026', 'SQ26']    # sortiert, serverseitig
    assert calls[0]['eq:origin'] == 'SIN'
    assert calls[0]['eq:dest'] == 'FRA'


def test_route_track_flight_set_empty_list_short_circuits():
    calls = []
    with patch.object(A, 'sb', _FakeSB(calls, [])):
        got = A._route_track_flight_set(
            'SIN', 'FRA', '2026-07-12', 'lo', 'hi', flight_nos=set())
    assert got == set()
    assert calls == []                       # gar kein SB-Kontakt


def test_route_track_flight_set_none_keeps_unkeyed_fallback():
    calls = []
    with patch.object(A, 'sb', _FakeSB(calls, [{'flight': 'LH400'}])):
        got = A._route_track_flight_set(
            'SIN', 'FRA', '2026-07-12', 'lo', 'hi', flight_nos=None)
    assert got == {'LH400'}
    assert len(calls) == 1
    assert 'in:flight' not in calls[0]       # alter Scan-Pfad unverändert
