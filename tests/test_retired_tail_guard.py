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

    def in_(self, *a, **k):
        return self

    def order(self, *a, **k):
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


# ══════════════════════════════════════════════════════════════════════════════
# ax_flight_info: Detail-Ausgabe scrubbt Museums-Reg (Owner-Screenshot LH781,
# „MASCHINE DABTL · Boeing 747-400" auf der Detailseite trotz Roster-Wächter)
# ══════════════════════════════════════════════════════════════════════════════
def _flight_info_json(active):
    """ax_flight_info hermetisch aufrufen — Board-Row trägt die Museums-Reg."""
    import blueprints.aerox_data_blueprint as BP
    today = A._airport_local_now('FRA').strftime('%Y-%m-%d')
    obs_row = {'airport': 'FRA', 'flight': 'LH781', 'dest_iata': 'SIN',
               'dest_name': 'Singapore', 'airline': 'LH', 'gate': 'F58',
               'terminal': '2', 'status': 'Departed', 'sched': '23:40',
               'esti': None, 'max_delay_min': 0, 'cancelled': False,
               'reg': 'DABTL', 'type_code': 'B744', 'date': today}
    p1, p2, _ = _with_sb({'airport_delay_obs': [obs_row]})
    with p1, p2, \
            patch.object(A, '_tail_recently_active', return_value=active), \
            patch.object(A, '_flight_from_live_board', return_value=None), \
            patch.object(A, '_flight_obs_merged', return_value=None), \
            patch.object(A, '_arrival_gate_terminal',
                         return_value=(None, None)), \
            patch.object(BP, '_flight_facts_from_obs', return_value={}):
        with A.app.test_request_context('/api/ax/flight-info/LH781'):
            resp = A.ax_flight_info('LH781')
    if isinstance(resp, tuple):
        resp = resp[0]
    return resp.get_json()


def test_flight_info_scrubs_museum_reg_and_recent_regs():
    d = _flight_info_json(active=False)
    assert d['found'] is True
    assert d['reg'] is None
    assert d['type'] is None
    assert d['recent_regs'] == []
    # Identität/Strecke bleiben — nur die Maschine wird weggelassen.
    assert d['origin'] == 'FRA' and d['dest'] == 'SIN'


def test_flight_info_keeps_active_reg():
    d = _flight_info_json(active=True)
    assert d['reg'] == 'DABTL'
    assert d['type'] == 'B744'
    assert d['recent_regs'] and d['recent_regs'][0]['reg'] == 'DABTL'


# ══════════════════════════════════════════════════════════════════════════════
# Blueprint: _flight_facts_from_obs scrubbt reg/type am Ausgang
# (Consumer: resolve-flight, uflight, flight-detail — alle erben den Scrub)
# ══════════════════════════════════════════════════════════════════════════════
def _facts_with_reg(active):
    import blueprints.aerox_data_blueprint as BP
    dep_row = {'airport': 'SIN', 'flight': 'LH781', 'dest_iata': 'FRA',
               'sched': '2026-07-12T23:40:00', 'esti': None, 'gate': 'F58',
               'terminal': '2', 'status': 'Departed', 'max_delay_min': 0,
               'cancelled': False, 'reg': 'DABTL', 'type_code': 'B744',
               'date': '2026-07-12'}
    fake = _FakeSB({'airport_delay_obs': [dep_row]})
    with patch.object(BP, '_sb', return_value=fake), \
            patch.object(A, '_tail_recently_active', return_value=active):
        return BP._flight_facts_from_obs('LH781', '2026-07-12')


def test_facts_from_obs_scrubs_museum_reg():
    facts = _facts_with_reg(active=False)
    assert facts.get('reg') is None
    assert facts.get('type') is None
    # Die übrigen Board-Fakten bleiben (Zeit-Wissen ≠ Tail-Wissen).
    assert facts.get('dep_iata') == 'SIN'


def test_facts_from_obs_keeps_active_reg():
    facts = _facts_with_reg(active=True)
    assert facts.get('reg') == 'DABTL'
    assert facts.get('type') == 'B744'


# ══════════════════════════════════════════════════════════════════════════════
# Unified-Resolver: identity.reg/aircraft.reg aus der Routen-Kaskade
# (Warehouse-`flights`.tail) fällt ebenfalls unter den Wächter
# ══════════════════════════════════════════════════════════════════════════════
def _unified_with_warehouse_reg(active):
    import blueprints.aerox_data_blueprint as BP
    import blueprints.warehouse_reader as WR
    rt = {'src': 'SIN', 'dst': 'FRA', 'source': 'warehouse_board',
          'confidence': 'confirmed', 'reg': 'D-ABTL', 'flight_no': 'LH781'}
    with patch.object(BP, '_aircraft_live_flight', return_value=None), \
            patch.object(WR, 'route_for_flight', return_value=rt), \
            patch.object(BP, '_flight_facts_from_obs', return_value={}), \
            patch.object(BP, '_tail_active_guard', return_value=active):
        return BP._resolve_unified_flight_core(
            'LH781', '2026-07-12', False, None, None, False)


def test_unified_resolver_scrubs_museum_reg():
    res = _unified_with_warehouse_reg(active=False)
    assert res['found'] is True
    assert res['identity']['reg'] is None
    assert res['aircraft']['reg'] is None


def test_unified_resolver_keeps_active_reg():
    res = _unified_with_warehouse_reg(active=True)
    assert res['identity']['reg'] == 'D-ABTL'
    assert res['aircraft']['reg'] == 'D-ABTL'
