"""Unified Flight-Info P0 — der geteilte Board-Fakten-Mapper `_obs_rows_to_facts`
+ die Datums-Disziplin von `_flight_facts_from_obs`.

Beweist den DEP+ARR-Merge (die eine Stelle, die künftig alle Screens lesen):
Soll/Ist Ab+Ankunft, Gate, Delay, Reg/Typ — nur echte Werte, None/'' weg.
Seit dem Zeit-Normalisierungs-P0 zusätzlich: sched/esti kommen als ISO MIT
Station-Offset raus (Wanduhr bleibt Station-lokal lesbar, Offset macht sie
eindeutig) — egal ob die Row bare 'HH:MM', naive Lokal-ISO oder Offset-ISO trug.
"""
import types

import blueprints.aerox_data_blueprint as axd
from blueprints.aerox_data_blueprint import (_flight_facts_from_obs,
                                             _obs_rows_to_facts)


def test_dep_and_arr_rows_merge_full_facts():
    # Reale Form (LH146 FRA→NUE, 2026-07-09): DEP-Row FRA + ARR-Row NUE#ARR.
    # Formate absichtlich gemischt: bare 'HH:MM', Offset-ISO ohne Doppelpunkt,
    # naive Lokal-ISO → ALLE kommen als Station-Offset-ISO raus.
    dep = {'airport': 'FRA', 'flight': 'LH146', 'dest_iata': 'NUE',
           'date': '2026-07-09',
           'sched': '16:50', 'esti': '2026-07-09T17:30:00+0200', 'gate': 'A58',
           'terminal': '1', 'status': 'Abgeflogen', 'max_delay_min': 30,
           'cancelled': False, 'reg': 'DAIBH', 'type_code': 'A319'}
    arr = {'airport': 'NUE#ARR', 'flight': 'LH146', 'dest_iata': 'FRA',
           'date': '2026-07-09',
           'sched': '17:35', 'esti': '2026-07-09T18:20:00', 'gate': None,
           'terminal': None, 'status': 'delayed', 'max_delay_min': 45,
           'cancelled': False}
    f = _obs_rows_to_facts(dep, arr)
    # Abflugseite — bare '16:50' → Servicedatum + FRA-Offset (Juli = +02:00)
    assert f['sched_dep'] == '2026-07-09T16:50:00+02:00'
    assert f['est_dep'] == '2026-07-09T17:30:00+02:00'
    assert f['gate'] == 'A58'
    assert f['dep_delay_min'] == 30
    assert f['reg'] == 'DAIBH'
    assert f['type'] == 'A319'
    # Ankunftseite — Station-TZ der ARR-Row = ZIEL (NUE); naive ISO kriegt Offset
    assert f['sched_arr'] == '2026-07-09T17:35:00+02:00'
    assert f['est_arr'] == '2026-07-09T18:20:00+02:00'
    assert f['arr_delay_min'] == 45
    # Route steckt auch in den Obs (DEP-Row airport→dest_iata)
    assert f['dep_iata'] == 'FRA'
    assert f['arr_iata'] == 'NUE'


def test_route_from_arr_row_only():
    # Nur ARR-Row (NUE#ARR, dest_iata=FRA=Herkunft) → Route trotzdem ableitbar.
    arr = {'airport': 'NUE#ARR', 'flight': 'LH146', 'dest_iata': 'FRA',
           'date': '2026-07-09',
           'sched': '17:35', 'esti': '2026-07-09T18:20:00'}
    f = _obs_rows_to_facts(None, arr)
    assert f['arr_iata'] == 'NUE'
    assert f['dep_iata'] == 'FRA'
    assert f['sched_arr'] == '2026-07-09T17:35:00+02:00'
    assert f['est_arr'] == '2026-07-09T18:20:00+02:00'


def test_dep_only_no_arr_row():
    # Row OHNE Servicedatum: bare Zeit ist nicht eindeutig datierbar →
    # Rohwert bleibt erhalten (nichts erfinden), Rest wie gehabt.
    dep = {'airport': 'FRA', 'flight': 'LH146', 'sched': '16:50', 'esti': None,
           'gate': '', 'status': '', 'max_delay_min': None}
    f = _obs_rows_to_facts(dep, None)
    assert f['sched_dep'] == '16:50'
    assert 'est_dep' not in f       # None → weggelassen (nie erfunden)
    assert 'gate' not in f          # '' → weggelassen
    assert 'dep_delay_min' not in f  # None → weggelassen
    assert 'sched_arr' not in f      # keine ARR-Row → keine Ankunft
    # Unbekannte Station-TZ → ebenfalls Rohwert behalten
    f2 = _obs_rows_to_facts({'airport': 'XX', 'date': '2026-07-09',
                             'sched': '16:50'}, None)
    assert f2['sched_dep'] == '16:50'


def test_midnight_wrap_est_after_zero():
    # est '00:30' bei sched '23:50' = NACH Mitternacht → +1 Tag (est<sched−4h).
    dep = {'airport': 'FRA', 'flight': 'LH146', 'date': '2026-07-09',
           'sched': '23:50', 'esti': '00:30'}
    f = _obs_rows_to_facts(dep, None)
    assert f['sched_dep'] == '2026-07-09T23:50:00+02:00'
    assert f['est_dep'] == '2026-07-10T00:30:00+02:00'


def test_cancelled_flag_from_either_side():
    assert _obs_rows_to_facts({'cancelled': True}, None).get('cancelled') is True
    assert _obs_rows_to_facts(None, {'cancelled': True}).get('cancelled') is True
    assert _obs_rows_to_facts(None, None) == {}


# ── _flight_facts_from_obs: Datums-Disziplin (P1-3) ─────────────────────────

class _FakeQ:
    def __init__(self, rows):
        self._rows = rows

    def __getattr__(self, name):
        # select/in_/eq/order/limit … → chainbar
        def _chain(*a, **kw):
            return self
        return _chain

    def execute(self):
        return types.SimpleNamespace(data=self._rows)


class _FakeSB:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        return _FakeQ(self._rows)


def test_yday_gate_row_does_not_beat_today(monkeypatch):
    """Der Kernfall: eine GESTRIGE Final-Row (mit Gate/Ist-Zeit) desselben
    täglichen Flugs darf die HEUTIGE Row nicht schlagen — Rows werden nach
    Datum partitioniert, angefragtes Datum strikt zuerst."""
    yday_final = {'airport': 'FRA', 'flight': 'LH146', 'dest_iata': 'NUE',
                  'date': '2026-07-08', 'sched': '16:50', 'esti': '17:42',
                  'gate': 'A58', 'reg': 'DAIBH'}
    today_plain = {'airport': 'FRA', 'flight': 'LH146', 'dest_iata': 'NUE',
                   'date': '2026-07-09', 'sched': '16:50', 'esti': None,
                   'gate': None, 'reg': None}
    # updated_at desc: die gestrige Final-Row liegt „frischer" im Result —
    # trotzdem muss die heutige gewinnen.
    monkeypatch.setattr(axd, '_sb',
                        lambda: _FakeSB([yday_final, today_plain]))
    f = _flight_facts_from_obs('LH146', '2026-07-09')
    assert f['sched_dep'] == '2026-07-09T16:50:00+02:00'
    assert 'est_dep' not in f          # gestrige Ist-Zeit darf NICHT durchsickern
    assert 'gate' not in f             # gestriges Gate ebenso wenig
    assert not f.get('stale')


def test_yday_fallback_marks_stale(monkeypatch):
    """Overnight-Fall: das angefragte Datum ist leer → yday-Row als Fallback,
    aber transparent als stale/obs_date markiert."""
    yday_only = {'airport': 'FRA', 'flight': 'LH146', 'dest_iata': 'NUE',
                 'date': '2026-07-08', 'sched': '16:50', 'esti': None,
                 'gate': 'A58'}
    monkeypatch.setattr(axd, '_sb', lambda: _FakeSB([yday_only]))
    f = _flight_facts_from_obs('LH146', '2026-07-09')
    assert f['gate'] == 'A58'
    assert f['sched_dep'] == '2026-07-08T16:50:00+02:00'
    assert f.get('stale') is True
    assert f.get('obs_date') == '2026-07-08'


def test_enrich_ignores_stale_yday_facts(monkeypatch):
    """P5-Nachfix: liefert _flight_facts_from_obs nur einen Vortags-Fallback
    (stale=True), darf _enrich_flight_status_with_obs die gestrige Geister-
    Ankunft NICHT in den Detail-Screen füllen."""
    stale = {'sched_arr': '2026-07-08T18:05:00+02:00',
             'est_arr': '2026-07-08T18:20:00+02:00',
             'arr_status': 'Gelandet', 'dep_status': 'Abgeflogen',
             'stale': True, 'obs_date': '2026-07-08'}
    monkeypatch.setattr(axd, '_flight_facts_from_obs', lambda *a, **k: stale)
    monkeypatch.setattr(axd, '_flight_times_free_first', lambda *a, **k: {})
    out = axd._enrich_flight_status_with_obs(
        {'flight': 'LH146', 'dep_iata': 'FRA', 'arr_iata': 'NUE'},
        date='2026-07-09')
    assert not out.get('sched_arr')
    assert not out.get('est_arr')
    assert not out.get('status_category')     # kein Geister-"arrived"


def test_enrich_fills_fresh_facts(monkeypatch):
    """Frische Fakten füllen weiterhin (Gate darf nicht alles blocken)."""
    fresh = {'sched_arr': '2026-07-09T18:05:00+02:00',
             'est_arr': '2026-07-09T18:20:00+02:00', 'arr_status': 'Gelandet'}
    monkeypatch.setattr(axd, '_flight_facts_from_obs', lambda *a, **k: fresh)
    monkeypatch.setattr(axd, '_flight_times_free_first', lambda *a, **k: {})
    out = axd._enrich_flight_status_with_obs(
        {'flight': 'LH146', 'dep_iata': 'FRA', 'arr_iata': 'NUE'},
        date='2026-07-09')
    assert out['sched_arr'] == '2026-07-09T18:05:00+02:00'
    assert out['status_category'] == 'arrived'
