import importlib.util
from pathlib import Path


PARSER = (Path(__file__).parents[1] / 'tools' / 'logbook-parsers'
          / 'parse_netline_idp.py')
SPEC = importlib.util.spec_from_file_location('parse_netline_idp', PARSER)
NETLINE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(NETLINE)


def _word(text, x0, top):
    return {'text': text, 'x0': x0, 'top': top}


def test_geometry_keeps_interleaved_month_columns_separate():
    # Regression für Upload #121: Der logische extract_text-Strom war in
    # einer Zeile verschränkt. Positionsbasierte Wörter bleiben eindeutig.
    words = [
        _word('Sun01', 30, 90),
        _word('Tue10', 310, 90),
        _word('Wed18', 590, 90),
        # Unabhängige Wörter der linken/rechten Spalte auf derselben Höhe.
        _word('OFF', 75, 110),
        _word('ABMT', 610, 110),
        # Das echte Leg liegt ausschließlich in der mittleren Spalte.
        _word('DE', 340, 110),
        _word('2368', 355, 110),
        _word('R', 394, 110),
        _word('FRA', 402, 110),
        _word('1350', 424, 110),
        _word('0105', 445, 110),
        _word('HKT', 467, 110),
        _word('339', 485, 110),
    ]

    assert NETLINE.geometry_leg_lines_from_words(words, 840) == [
        'Tue10 DE 2368 R FRA 1350 0105 HKT 339'
    ]


def test_geometry_rejects_crew_info_echo_without_aircraft_type():
    words = [
        _word('Tue10', 310, 90),
        _word('DE', 340, 110),
        _word('2368', 355, 110),
        _word('FRA', 402, 110),
        _word('1350', 424, 110),
        _word('0105', 445, 110),
        _word('HKT', 467, 110),
    ]

    assert NETLINE.geometry_leg_lines_from_words(words, 840) == []
