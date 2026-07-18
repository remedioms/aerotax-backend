"""airport_delay_obs Egress-Reduktion — Regressions-Tests für die drei Fixes:

  FIX 1  DB-seitiger Einzelflug-Filter in `_delay_obs_rows_for_date` (statt den
         ganzen Board-Tag zu laden und in Python auf EINEN Flug zu filtern).
         Beweist: bei gesetztem `flight` wird ein `.in_('flight', <varianten>)`-
         Filter angewandt, die Varianten decken führende-Null-Formen ab, und die
         Zielzeile (auch als 0-aufgefüllte LH0919) bleibt im Ergebnis.

  FIX 2  Retention-Prune `prune_old_delay_obs` — löscht `date < today-14` und ist
         prozess-lokal auf ~1×/Tag gedrosselt (2. Sofort-Aufruf = No-Op).

  FIX 3  Spaltenprojektion des Poll-Loop-Vollreloads: `_DELAY_STORE_LOAD_COLS`
         enthält kein '*' und genau die Felder, die der Loader liest.
"""
import datetime as _dt

import pytest
from unittest.mock import patch

import app as A  # noqa: E402


# ---------------------------------------------------------------------------
# Chainbarer PostgREST-Stub, der die angewandten Filter aufzeichnet.
# ---------------------------------------------------------------------------
class _FakeQ:
    def __init__(self, sb):
        self._sb = sb
        self._select_cols = None
        self._eq = {}
        self._in = {}
        self._lt = {}
        self._is_delete = False

    def select(self, cols):
        self._select_cols = cols
        self._sb.selects.append(cols)
        return self

    def delete(self):
        self._is_delete = True
        return self

    def eq(self, col, val):
        self._eq[col] = val
        return self

    def in_(self, col, vals):
        self._in[col] = list(vals)
        self._sb.in_filters.append((col, list(vals)))
        return self

    def lt(self, col, val):
        self._lt[col] = val
        return self

    def range(self, lo, hi):
        self._range = (lo, hi)
        return self

    def execute(self):
        if self._is_delete:
            self._sb.deletes.append({'lt': dict(self._lt)})
            return type('R', (), {'data': list(self._sb.delete_returns)})()
        # Select: eq(date/airport/dest_iata) + in_(flight) auf die Store-Rows anwenden.
        rows = list(self._sb.rows)
        for col, val in self._eq.items():
            rows = [r for r in rows if r.get(col) == val]
        for col, vals in self._in.items():
            rows = [r for r in rows if r.get(col) in vals]
        return type('R', (), {'data': rows})()


class _FakeSB:
    def __init__(self, rows=None, delete_returns=None):
        self.rows = list(rows or [])
        self.selects = []
        self.in_filters = []
        self.deletes = []
        self.delete_returns = list(delete_returns or [])
        self._last = None

    def table(self, name):
        self._last = _FakeQ(self)
        return self._last


# ===========================================================================
# FIX 1 — DB-seitiger Einzelflug-Filter
# ===========================================================================
def test_fix1_flight_applies_in_filter_and_keeps_target():
    # Board mit vielen Flügen + der Zielzeile als 0-aufgefüllte Variante (LH0919).
    rows = [
        {'date': '2026-07-10', 'airport': 'FRA', 'flight': 'LH0919',
         'sched': '08:00', 'dest_iata': 'LHR', 'max_delay_min': 5},
        {'date': '2026-07-10', 'airport': 'FRA', 'flight': 'LH400',
         'sched': '09:00', 'dest_iata': 'JFK', 'max_delay_min': 0},
        {'date': '2026-07-10', 'airport': 'FRA', 'flight': 'BA111',
         'sched': '10:00', 'dest_iata': 'LHR', 'max_delay_min': 0},
    ]
    fsb = _FakeSB(rows=rows)
    with patch.object(A, 'SB_AVAILABLE', True), patch.object(A, 'sb', fsb):
        out = A._delay_obs_rows_for_date('2026-07-10', 'FRA', flight='LH919')

    # Ein in_('flight', ...) Filter WURDE angewandt.
    assert fsb.in_filters, 'expected an .in_(flight, ...) filter on the DB query'
    col, variants = fsb.in_filters[0]
    assert col == 'flight'
    # Die führende-Null-Variante (LH0919) IST unter den DB-Varianten...
    assert 'LH0919' in variants and 'LH919' in variants
    # ...und die Zielzeile (0-aufgefüllt gespeichert) kommt zurück, der Rest nicht.
    assert len(out) == 1 and out[0]['flight'] == 'LH0919'


def test_fix1_no_flight_no_in_filter():
    """Ohne `flight` bleibt das Alt-Verhalten: KEIN in_(flight)-Filter."""
    rows = [{'date': '2026-07-10', 'airport': 'FRA', 'flight': 'LH919',
             'sched': '08:00', 'dest_iata': 'LHR', 'max_delay_min': 0}]
    fsb = _FakeSB(rows=rows)
    with patch.object(A, 'SB_AVAILABLE', True), patch.object(A, 'sb', fsb):
        out = A._delay_obs_rows_for_date('2026-07-10', 'FRA')
    assert not fsb.in_filters
    assert len(out) == 1


def test_fix1_variants_cover_leading_zero_forms():
    v = A._obs_flight_variants('LH919')
    assert 'LH919' in v and 'LH0919' in v and 'LH00919' in v
    # SQ26 <-> SQ026 Symmetrie (Eingabe in beliebiger Form).
    assert set(A._obs_flight_variants('SQ026')) == set(A._obs_flight_variants('SQ26'))
    # Unparsebares bleibt als reine upper/space-Form (kein Crash).
    assert A._obs_flight_variants('  x ') == ['X']
    assert A._obs_flight_variants('') == []


def test_fix1_board_wrapper_threads_flight_and_arr_key():
    """`_board_rows_from_obs_for_date` reicht `flight` durch — auch für einen
    #ARR-Store-Key (Ankunfts-Historie)."""
    rows = [
        {'date': '2026-07-10', 'airport': 'LHR#ARR', 'flight': 'LH918',
         'sched': '11:00', 'dest_iata': 'FRA', 'max_delay_min': 0},
        {'date': '2026-07-10', 'airport': 'LHR#ARR', 'flight': 'BA900',
         'sched': '11:30', 'dest_iata': 'MUC', 'max_delay_min': 0},
    ]
    fsb = _FakeSB(rows=rows)
    with patch.object(A, 'SB_AVAILABLE', True), patch.object(A, 'sb', fsb):
        out = A._board_rows_from_obs_for_date('2026-07-10', 'LHR#ARR', None,
                                              flight='LH918')
    assert fsb.in_filters and fsb.in_filters[0][0] == 'flight'
    assert [r['flight'] for r in out] == ['LH918']


# ===========================================================================
# FIX 2 — Retention-Prune + Throttle
# ===========================================================================
@pytest.fixture(autouse=True)
def _reset_prune_throttle():
    A._delay_obs_prune_last_ts = 0.0
    yield
    A._delay_obs_prune_last_ts = 0.0


def test_fix2_prune_deletes_lt_cutoff_and_throttles():
    # 2 „gelöschte" Rows als delete-Return, damit deleted-count > 0.
    fsb = _FakeSB(delete_returns=[{'id': 1}, {'id': 2}])
    with patch.object(A, 'SB_AVAILABLE', True), patch.object(A, 'sb', fsb):
        n1 = A.prune_old_delay_obs()
        # 2. Sofort-Aufruf → Throttle greift → No-Op, KEIN weiterer Delete.
        n2 = A.prune_old_delay_obs()

    assert n1 == 2
    assert n2 == 0
    assert len(fsb.deletes) == 1, 'second immediate call must be throttled (no-op)'

    # Cutoff ist date-Spaltenform 'YYYY-MM-DD' und ~ heute-14.
    cutoff = fsb.deletes[0]['lt'].get('date')
    assert cutoff is not None and len(cutoff) == 10
    expected = (_dt.datetime.utcnow() - _dt.timedelta(days=14)).strftime('%Y-%m-%d')
    assert cutoff == expected
    assert A._DELAY_OBS_RETENTION_DAYS == 14


def test_fix2_prune_force_bypasses_throttle():
    fsb = _FakeSB(delete_returns=[{'id': 1}])
    with patch.object(A, 'SB_AVAILABLE', True), patch.object(A, 'sb', fsb):
        A.prune_old_delay_obs(force=True)
        A.prune_old_delay_obs(force=True)
    assert len(fsb.deletes) == 2


def test_fix2_prune_never_raises_on_sb_error():
    class _BoomSB:
        def table(self, name):
            raise RuntimeError('sb down')
    with patch.object(A, 'SB_AVAILABLE', True), patch.object(A, 'sb', _BoomSB()):
        # Wirft NICHT, gibt 0 zurück.
        assert A.prune_old_delay_obs(force=True) == 0

    with patch.object(A, 'SB_AVAILABLE', False), patch.object(A, 'sb', None):
        assert A.prune_old_delay_obs(force=True) == 0


# ===========================================================================
# FIX 3 — Spaltenprojektion statt select('*')
# ===========================================================================
def test_fix3_projection_has_no_star_and_covers_read_fields():
    cols = A._DELAY_STORE_LOAD_COLS
    assert '*' not in cols
    # Genau die Felder, die _delay_store_load_from_sb liest.
    for f in ('flight', 'sched', 'max_delay_min', 'cancelled', 'dest_iata',
              'dest_name', 'gate', 'terminal', 'airline', 'esti', 'reg',
              'type_code', 'status'):
        assert f in cols, f'projection missing consumed field {f}'
    # date/airport (aus dem Filter bekannt) NICHT projiziert.
    proj = set(c.strip() for c in cols.split(','))
    assert 'date' not in proj and 'airport' not in proj and 'updated_at' not in proj


def test_fix3_loader_uses_projection_not_star():
    """Der Vollreload verwendet die Projektion, nicht select('*')."""
    rows = [{'flight': 'LH919', 'sched': '08:00', 'max_delay_min': 7,
             'cancelled': False, 'dest_iata': 'LHR', 'dest_name': 'London',
             'gate': 'A1', 'terminal': '1', 'airline': 'LH', 'esti': '08:07',
             'reg': 'D-AIAA', 'type_code': 'A320', 'status': 'departed'}]
    fsb = _FakeSB(rows=[dict(r, date='2026-07-10', airport='FRA') for r in rows])
    # In-Memory-Store/Marker leeren, damit der Read tatsächlich passiert.
    A._delay_store_sb_loaded_date.clear()
    with patch.object(A, 'SB_AVAILABLE', True), patch.object(A, 'sb', fsb), \
            patch.object(A, '_sb_retry', lambda name, fn: fn()):
        A._delay_store_load_from_sb('2026-07-10', 'FRA')
    assert fsb.selects, 'loader should have issued a select'
    assert all('*' not in s for s in fsb.selects)
    assert A._DELAY_STORE_LOAD_COLS in fsb.selects
