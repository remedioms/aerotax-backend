"""Unified Flight-Info P0 — der geteilte Board-Fakten-Mapper `_obs_rows_to_facts`.

Beweist den DEP+ARR-Merge (die eine Stelle, die künftig alle Screens lesen):
Soll/Ist Ab+Ankunft, Gate, Delay, Reg/Typ — nur echte Werte, None/'' weg.
"""
from blueprints.aerox_data_blueprint import _obs_rows_to_facts


def test_dep_and_arr_rows_merge_full_facts():
    # Reale Form (LH146 FRA→NUE, 2026-07-09): DEP-Row FRA + ARR-Row NUE#ARR.
    dep = {'airport': 'FRA', 'flight': 'LH146', 'dest_iata': 'NUE',
           'sched': '16:50', 'esti': '2026-07-09T17:30:00+0200', 'gate': 'A58',
           'terminal': '1', 'status': 'Abgeflogen', 'max_delay_min': 30,
           'cancelled': False, 'reg': 'DAIBH', 'type_code': 'A319'}
    arr = {'airport': 'NUE#ARR', 'flight': 'LH146', 'dest_iata': 'FRA',
           'sched': '17:35', 'esti': '2026-07-09T18:20:00', 'gate': None,
           'terminal': None, 'status': 'delayed', 'max_delay_min': 45,
           'cancelled': False}
    f = _obs_rows_to_facts(dep, arr)
    # Abflugseite
    assert f['sched_dep'] == '16:50'
    assert f['est_dep'] == '2026-07-09T17:30:00+0200'
    assert f['gate'] == 'A58'
    assert f['dep_delay_min'] == 30
    assert f['reg'] == 'DAIBH'
    assert f['type'] == 'A319'
    # Ankunftseite — der Kern des Fixes: sie ist jetzt DA
    assert f['sched_arr'] == '17:35'
    assert f['est_arr'] == '2026-07-09T18:20:00'
    assert f['arr_delay_min'] == 45
    # Route steckt auch in den Obs (DEP-Row airport→dest_iata)
    assert f['dep_iata'] == 'FRA'
    assert f['arr_iata'] == 'NUE'


def test_route_from_arr_row_only():
    # Nur ARR-Row (NUE#ARR, dest_iata=FRA=Herkunft) → Route trotzdem ableitbar.
    arr = {'airport': 'NUE#ARR', 'flight': 'LH146', 'dest_iata': 'FRA',
           'sched': '17:35', 'esti': '2026-07-09T18:20:00'}
    f = _obs_rows_to_facts(None, arr)
    assert f['arr_iata'] == 'NUE'
    assert f['dep_iata'] == 'FRA'
    assert f['sched_arr'] == '17:35'


def test_dep_only_no_arr_row():
    dep = {'airport': 'FRA', 'flight': 'LH146', 'sched': '16:50', 'esti': None,
           'gate': '', 'status': '', 'max_delay_min': None}
    f = _obs_rows_to_facts(dep, None)
    assert f['sched_dep'] == '16:50'
    assert 'est_dep' not in f       # None → weggelassen (nie erfunden)
    assert 'gate' not in f          # '' → weggelassen
    assert 'dep_delay_min' not in f  # None → weggelassen
    assert 'sched_arr' not in f      # keine ARR-Row → keine Ankunft


def test_cancelled_flag_from_either_side():
    assert _obs_rows_to_facts({'cancelled': True}, None).get('cancelled') is True
    assert _obs_rows_to_facts(None, {'cancelled': True}).get('cancelled') is True
    assert _obs_rows_to_facts(None, None) == {}
