"""Ausgemusterte-Tails-Wächter (Owner-Screenshot 2026-07-12 17:43).

Root-Cause: der Fraport-Feed liefert für manche Langstrecken-Flugnummern
MUSEUMS-Regs (LH780→D-ABVU, LH781→D-ABTL — seit Jahren ausgemusterte 747-400),
die über airport_delay_obs/`flights` bis in Tibors Roster-Legs durchgereicht
wurden. Der Wächter `_tail_recently_active` lässt einen Tail nur noch durch,
wenn die Reg in den letzten 60 Tagen in aircraft_live ODER aircraft_track
gesehen wurde (EXISTS-Query, 24 h in-process gecacht pro Reg).

Deckt ab:
  • _tail_recently_active — Cache-Semantik, beide Tabellen, Fail-Open bei
    SB-down/Query-Fehler, Reg-Normalisierung (D-ABVU == DABVU).
  • _leg_tail             — Museums-Reg wird NICHT geliefert; aktive Reg schon.
  • _enrich_leg_delays    — reg UND tail werden am Sektor weggelassen, wenn die
    Reg nicht verifiziert ist; aktive Regs unverändert.

KEIN echter Netz-/DB-Zugriff: sb / _flight_obs_merged werden gemockt.
"""
import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

import app as A


@pytest.fixture(autouse=True)
def _pin_and_clear():
    # sys.modules-Pin (gleiche Order-Kontamination wie test_leg_tail_enrichment):
    # test_calculation.py tauscht sys.modules['app'] per Reimport aus.
    import sys
    _prev = sys.modules.get('app')
    sys.modules['app'] = A
    A._TAIL_ACTIVE_CACHE.clear()
    A._FLIGHT_MERGE_CACHE.clear()
    A._AX_CODESHARE_CACHE['ts'] = 0.0
    A._AX_CODESHARE_CACHE['map'] = {}
    yield
    A._TAIL_ACTIVE_CACHE.clear()
    A._FLIGHT_MERGE_CACHE.clear()
    if _prev is not None:
        sys.modules['app'] = _prev


def _merged(reg=None, delay_known=False):
    return {'ok': True, 'delay_known': delay_known, 'reg': reg,
            'delay_min': None, 'delay_side': None, 'dep_delay_min': None,
            'arr_delay_min': None, 'status': None, 'cancelled': False,
            'esti_dep': None, 'esti_arr': None,
            'sides': {'dep': None, 'arr': None}}


class _FakeQuery:
    """Chainbare Supabase-Query-Attrappe; `rows_by_table` steuert das Ergebnis."""

    def __init__(self, table, rows_by_table, calls):
        self._table = table
        self._rows_by_table = rows_by_table
        self._calls = calls

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def gt(self, *a, **k):
        return self

    def gte(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        self._calls.append(self._table)
        rows = self._rows_by_table.get(self._table)
        if isinstance(rows, Exception):
            raise rows
        return MagicMock(data=rows if rows is not None else [])


class _FakeSB:
    def __init__(self, rows_by_table):
        self.rows_by_table = rows_by_table
        self.calls = []

    def table(self, name):
        return _FakeQuery(name, self.rows_by_table, self.calls)


def _with_sb(rows_by_table):
    fake = _FakeSB(rows_by_table)
    return patch.object(A, 'sb', fake), patch.object(A, 'SB_AVAILABLE', True), fake


# ══════════════════════════════════════════════════════════════════════════════
# _tail_recently_active
# ══════════════════════════════════════════════════════════════════════════════
def test_active_in_aircraft_live():
    p1, p2, fake = _with_sb({'aircraft_live': [{'reg': 'DAIXY'}]})
    with p1, p2:
        assert A._tail_recently_active('D-AIXY') is True
    # aircraft_live-Treffer → aircraft_track wird gar nicht mehr gefragt.
    assert fake.calls == ['aircraft_live']


def test_active_only_in_aircraft_track():
    p1, p2, fake = _with_sb({'aircraft_live': [],
                             'aircraft_track': [{'reg': 'DAIXY'}]})
    with p1, p2:
        assert A._tail_recently_active('D-AIXY') is True
    assert fake.calls == ['aircraft_live', 'aircraft_track']


def test_museum_reg_is_inactive():
    p1, p2, _ = _with_sb({'aircraft_live': [], 'aircraft_track': []})
    with p1, p2:
        assert A._tail_recently_active('D-ABVU') is False


def test_result_cached_per_reg_24h():
    p1, p2, fake = _with_sb({'aircraft_live': [], 'aircraft_track': []})
    with p1, p2:
        assert A._tail_recently_active('D-ABVU') is False
        assert A._tail_recently_active('D-ABVU') is False
    # 2. Aufruf kommt aus dem 24h-Memo → keine weiteren Queries.
    assert fake.calls == ['aircraft_live', 'aircraft_track']


def test_reg_normalized_shares_cache_entry():
    # D-ABVU und DABVU sind derselbe Airframe → EIN Cache-Eintrag.
    p1, p2, fake = _with_sb({'aircraft_live': [], 'aircraft_track': []})
    with p1, p2:
        A._tail_recently_active('D-ABVU')
        A._tail_recently_active('DABVU')
    assert fake.calls == ['aircraft_live', 'aircraft_track']


def test_fail_open_when_sb_unavailable():
    with patch.object(A, 'SB_AVAILABLE', False), patch.object(A, 'sb', None):
        assert A._tail_recently_active('D-ABVU') is True
    # Nicht verifizierbar wird NICHT als definitive Antwort gecacht.
    assert 'DABVU' not in A._TAIL_ACTIVE_CACHE


def test_fail_open_on_query_error():
    p1, p2, _ = _with_sb({'aircraft_live': RuntimeError('boom')})
    with p1, p2:
        assert A._tail_recently_active('D-ABVU') is True


def test_empty_reg_is_false():
    assert A._tail_recently_active('') is False
    assert A._tail_recently_active(None) is False


# ══════════════════════════════════════════════════════════════════════════════
# _leg_tail mit Wächter
# ══════════════════════════════════════════════════════════════════════════════
def test_leg_tail_drops_museum_reg():
    with patch.object(A, '_flight_obs_merged', return_value=_merged(reg='D-ABVU')), \
            patch.object(A, '_tail_recently_active', return_value=False):
        assert A._leg_tail('LH780', date='2026-07-12', dep_iata='FRA',
                           arr_iata='SIN') is None


def test_leg_tail_keeps_active_reg():
    with patch.object(A, '_flight_obs_merged', return_value=_merged(reg='D-AIXY')), \
            patch.object(A, '_tail_recently_active', return_value=True):
        assert A._leg_tail('LH400', date='2026-07-12', dep_iata='FRA',
                           arr_iata='JFK') == 'D-AIXY'


# ══════════════════════════════════════════════════════════════════════════════
# _enrich_leg_delays mit Wächter
# ══════════════════════════════════════════════════════════════════════════════
def _today_sector(flight='LH780', frm='FRA', to='SIN'):
    dep = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    return {'flight': flight, 'from': frm, 'to': to, 'dep_iso': dep}


def test_enrich_delays_drops_museum_tail_and_reg():
    s = _today_sector()
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(reg='D-ABVU', delay_known=True)), \
            patch.object(A, '_tail_recently_active', return_value=False):
        A._enrich_leg_delays([s], None)
    assert 'tail' not in s
    assert s.get('reg') is None
    # Die übrigen gemessenen Felder bleiben (Delay-Wissen ≠ Tail-Wissen).
    assert s.get('delay_known') is True


def test_enrich_delays_keeps_active_tail():
    s = _today_sector(flight='LH400', to='JFK')
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(reg='D-AIXY', delay_known=True)), \
            patch.object(A, '_tail_recently_active', return_value=True):
        A._enrich_leg_delays([s], None)
    assert s.get('tail') == 'D-AIXY'
    assert s.get('reg') == 'D-AIXY'
