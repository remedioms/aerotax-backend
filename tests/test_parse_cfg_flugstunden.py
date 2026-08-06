"""Schutztests für die Condor-Flugstundenübersicht (parse_cfg_flugstunden).

Synthetische Zellen/Wörter — keine echten Nutzerdaten. Die Regeln stammen
aus Upload #238 (05/2026): TC 00 + FAKTOR 1,00 = Leg, TC 01/10 = Deadhead,
Zeilen ohne TC = Tages-Status, `L` = Landung, Dezimalstunden.
"""

import importlib.util
import os
import sys

import pytest


TOOLS = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                     "tools", "logbook-parsers")
sys.path.insert(0, TOOLS)
SPEC = importlib.util.spec_from_file_location(
    "parse_cfg_flugstunden", os.path.join(TOOLS, "parse_cfg_flugstunden.py")
)
PARSER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PARSER)


def _cells(**updates):
    cells = {
        "datum": "01.05.",
        "strecke": "DE1710",
        "from": "FRA",
        "offon": "08:08-10:15",
        "to": "PMI",
        "tc": "00",
        "block": "2,12",
        "faktor": "1,00",
        "anr": "2,12",
    }
    cells.update(updates)
    return {k: v for k, v in cells.items() if v is not None}


def test_operating_leg_is_classified_as_leg():
    assert PARSER.classify_row(_cells())[0] == "leg"


def test_deadhead_codes_are_never_legs():
    for tc in ("01", "10"):
        cells = _cells(tc=tc, faktor="0,00", anr=None, dh="1,22")
        assert PARSER.classify_row(cells)[0] == "dh"


def test_day_status_row_without_tc_is_skipped():
    cells = {"datum": "06.05.", "strecke": "-", "ae": "FREIER",
             "from": "TAG"}
    assert PARSER.classify_row(cells)[0] == "day"


def test_standby_description_text_in_time_window_is_no_leg():
    # BEREITSCHAFT (STANDBY): „(STANDBY)" ragt ins OFF-ON-Fenster.
    cells = {"datum": "07.05.", "strecke": "SB", "ae": "BEREITSCHAFT",
             "offon": "(STANDBY)"}
    assert PARSER.classify_row(cells)[0] == "day"


def test_row_without_tc_but_with_clock_times_aborts():
    # Eine Leg-Zeile, deren TC-Glyphe verloren ging, darf NICHT still als
    # Tages-Status durchrutschen.
    with pytest.raises(ValueError):
        PARSER.classify_row(_cells(tc=None))


def test_operating_leg_without_faktor_aborts():
    with pytest.raises(ValueError):
        PARSER.classify_row(_cells(faktor="0,00"))


def test_unknown_tc_code_aborts():
    with pytest.raises(ValueError):
        PARSER.classify_row(_cells(tc="20"))


def test_deadhead_with_landing_marker_aborts():
    cells = _cells(tc="10", faktor="0,00", anr=None, dh="0,50", vl="L")
    with pytest.raises(ValueError):
        PARSER.classify_row(cells)


def test_role_mapping_only_maps_documented_functions():
    assert PARSER.role_from_funktion("FO") == "FO"
    assert PARSER.role_from_funktion("CP") == "PIC"
    assert PARSER.role_from_funktion("SFO") == "SFO"
    # Kabinen-/unbekannte Funktionen werden FB, nie geraten PIC.
    assert PARSER.role_from_funktion("PU") == "FB"
    assert PARSER.role_from_funktion(None) == "FB"


def test_extract_cells_respects_column_windows():
    words = [
        {"x0": 50, "text": "01.05."},
        {"x0": 81, "text": "DE1710"},
        {"x0": 156, "text": "FRA"},
        {"x0": 196, "text": "08:08-10:15"},
        {"x0": 253, "text": "PMI"},
        {"x0": 295, "text": "00"},
        {"x0": 322, "text": "2,12"},
        {"x0": 350, "text": "L"},
        {"x0": 376, "text": "1,00"},
        {"x0": 411, "text": "2,12"},
        {"x0": 458, "text": "DANMW"},
        {"x0": 491, "text": "/A321"},
    ]
    cells = PARSER.extract_cells(words)
    assert cells["tc"] == "00" and cells["vl"] == "L"
    assert cells["anr"] == "2,12" and "dh" not in cells
    assert cells["rest"] == "DANMW /A321"
