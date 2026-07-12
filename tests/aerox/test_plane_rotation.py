"""Rotations-Karte (Feature B, 2026-07-12): GET /api/ax/plane-rotation/<flight>.

Deckt ab:
  • _build_plane_rotation — Tages-Legs des Tails aus Warehouse (`flights`) +
    Board-Obs (`airport_delay_obs`), chronologisch, Delays gemergt, is_mine.
  • Deterministische Abflug-Folgerechnung: effektive Zubringer-Ankunft
    (est vor sched) + Typ-Turnaround-Minimum → „Zubringer +52 min → Abflug
    frühestens HH:MM" (KEIN „geschätzt" im Text — Domänenregel).
  • Museums-Tail-Wächter: nicht-aktive Reg → found:false statt Museums-Umlauf.
  • Keine Folgerechnung ohne echte Zubringer-Ankunftszeit (nichts erfinden).

KEIN Netz/DB: _sb + app-Helfer werden gemockt.
"""
import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

from unittest.mock import patch, MagicMock

import blueprints.aerox_data_blueprint as BP


class _FakeQuery:
    def __init__(self, table, rows_by_table):
        self._table = table
        self._rows = rows_by_table

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def in_(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        rows = self._rows.get(self._table)
        if isinstance(rows, Exception):
            raise rows
        return MagicMock(data=rows if rows is not None else [])


class _FakeSB:
    def __init__(self, rows_by_table):
        self.rows_by_table = rows_by_table

    def table(self, name):
        return _FakeQuery(name, self.rows_by_table)


def _life_app_map(extra=None):
    base = {
        '_fn_norm': lambda s: str(s or '').replace(' ', '').upper().strip(),
        # Merge kennt Reg + Typ des eigenen Flugs (A388 = Widebody → 60 min).
        '_flight_obs_merged': lambda *a, **k: {'reg': 'D-AIMC',
                                               'aircraft': 'A388'},
        '_board_local_to_utc_iso': lambda t, ap: None,
        # esti gesetzt = Delay beobachtet (vereinfachte, echte Semantik).
        '_obs_delay_known': lambda md, canc, esti, status, is_arr: esti is not None,
    }
    base.update(extra or {})
    def _fake(name, default=None):
        return base.get(name, default)
    return _fake


# Warehouse: Zubringer LH100 MUC→FRA (est_arr 09:52Z = +52) + mein LH101
# FRA→JFK (Soll-Abflug 10:00Z). Tail D-AIMC.
_FLIGHTS_ROWS = [
    {'op_flight_no': 'LH100', 'origin': 'MUC', 'destination': 'FRA',
     'sched_dep': '2026-07-12T07:00:00Z', 'est_dep': None,
     'sched_arr': '2026-07-12T09:00:00Z', 'est_arr': '2026-07-12T09:52:00Z',
     'status': 'Landed'},
    {'op_flight_no': 'LH101', 'origin': 'FRA', 'destination': 'JFK',
     'sched_dep': '2026-07-12T10:00:00Z', 'est_dep': None,
     'sched_arr': '2026-07-12T18:00:00Z', 'est_arr': None,
     'status': None},
]

# Ankunfts-Board FRA: LH100 kam +52 (esti gesetzt ⇒ Delay bekannt).
_OBS_ROWS = [
    {'airport': 'FRA#ARR', 'flight': 'LH100', 'dest_iata': 'MUC',
     'sched': '11:00', 'esti': '11:52', 'status': 'Gelandet',
     'max_delay_min': 52, 'cancelled': False, 'type_code': 'A388'},
]


def _rotation(active=True, flights=None, obs=None, life_extra=None):
    fake = _FakeSB({'flights': _FLIGHTS_ROWS if flights is None else flights,
                    'airport_delay_obs': _OBS_ROWS if obs is None else obs})
    with patch.object(BP, '_sb', return_value=fake), \
            patch.object(BP, '_life_app',
                         side_effect=_life_app_map(life_extra)), \
            patch.object(BP, '_tail_active_guard', return_value=active):
        return BP._build_plane_rotation('LH101', '2026-07-12', dep='FRA')


def test_rotation_legs_chronological_with_delays_and_is_mine():
    out = _rotation()
    assert out['found'] is True
    assert out['reg'] == 'D-AIMC'
    fns = [l['flight_no'] for l in out['legs']]
    assert fns == ['LH100', 'LH101']
    lh100 = out['legs'][0]
    assert lh100['arr_delay_min'] == 52
    assert lh100['is_mine'] is False
    assert out['legs'][1]['is_mine'] is True


def test_rotation_forecast_deterministic_chain_text():
    out = _rotation()
    fc = out['forecast']
    assert fc is not None
    # eff. Ankunft 09:52Z + 60 min (A388 Widebody) = 10:52Z → +52 auf 10:00Z.
    assert fc['dep_delay_min'] == 52
    assert fc['turnaround_min'] == 60
    assert fc['inbound_flight_no'] == 'LH100'
    # FRA im Juli = UTC+2 → frühestens 12:52 Ortszeit.
    assert fc['earliest_dep_hhmm'] == '12:52'
    assert fc['text'] == 'Zubringer +52 min → Abflug frühestens 12:52'
    # Domänenregel: das Wort „geschätzt" ist verboten.
    assert 'geschätzt' not in fc['text']


def test_rotation_museum_tail_yields_found_false():
    out = _rotation(active=False)
    assert out['found'] is False
    assert out['reg'] is None
    assert out['legs'] == []


def test_rotation_no_forecast_without_inbound_arrival_time():
    flights = [dict(_FLIGHTS_ROWS[0], est_arr=None, sched_arr=None),
               _FLIGHTS_ROWS[1]]
    out = _rotation(flights=flights, obs=[])
    assert out['found'] is True
    # Zubringer ohne echte Ankunftszeit → KEINE Folgerechnung (nichts erfinden).
    assert out['forecast'] is None


def test_rotation_on_time_inbound_says_planmaessig():
    # Zubringer landet früh (07:30Z) → 60 min Turnaround endet VOR Soll-Abflug.
    flights = [dict(_FLIGHTS_ROWS[0], est_arr='2026-07-12T07:30:00Z'),
               _FLIGHTS_ROWS[1]]
    obs = [dict(_OBS_ROWS[0], esti='09:30', max_delay_min=0)]
    out = _rotation(flights=flights, obs=obs)
    fc = out['forecast']
    assert fc['dep_delay_min'] == 0
    assert fc['text'] == 'Zubringer pünktlich → Abflug planmäßig 12:00'
