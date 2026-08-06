"""Schutztests für die NetLine-Duty-History (parse_netline_history).

Synthetische Zeilen — keine echten Nutzerdaten. Format-Wahrheiten aus
Upload #236: Revisionen NEUESTE ZUERST, old|new-Spalten je Tag, strenge
Leg-Regex mit AC-Typ-Pflicht, Revisionskette + Stationskette als Ersatz
für die fehlende gedruckte Kontrollsumme.
"""

import importlib.util
import os
import sys

import pytest


TOOLS = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                     "tools", "logbook-parsers")
sys.path.insert(0, TOOLS)
SPEC = importlib.util.spec_from_file_location(
    "parse_netline_history", os.path.join(TOOLS, "parse_netline_history.py")
)
PARSER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PARSER)


def test_leg_regex_requires_trailing_aircraft_type():
    kind, payload = PARSER.classify("DE 4094 BER 1550 1700 FRA 320")
    assert kind == "leg" and payload["flight"] == "DE4094"
    # Deadhead-Flüge drucken KEINEN AC-Typ — dürfen nie als Leg matchen.
    assert PARSER.classify("DE 4094 BER 1550 1700 FRA")[0] == "unknown"


def test_deadhead_and_ground_transfers_are_not_legs():
    assert PARSER.classify(
        "DH/DE 4075 FRA 1315 1420 BER")[0] == "transfer"
    assert PARSER.classify(
        "DH/ICE 525G QDU 0522 0639 QFA")[0] == "transfer"
    assert PARSER.classify(
        "GT/TRANSPO DUS 0437 0507 QDU")[0] == "transfer"


def test_day_suffix_between_flight_and_station_is_stripped():
    kind, payload = PARSER.classify("DE 2144 /17 SDQ 0240 1144 FRA 339")
    assert kind == "leg" and payload["flight"] == "DE2144"


def test_standby_courses_and_free_markers_are_ground():
    for line in ("SB90 DUS 0230 1830", "RE12 DUS 0630 2259",
                 "HS4 DUS 0700 1100", "E_FDP DUS 0545 0605",
                 "MED DUS 0730 1130"):
        assert PARSER.classify(line)[0] == "ground", line
    for line in ("U DUS", "ORT DUS", "OFF DUS", "OFF_2 DUS"):
        assert PARSER.classify(line)[0] == "free", line


def test_unknown_line_aborts_normalization():
    with pytest.raises(ValueError):
        PARSER.normalized_elements(["XYZZY völlig unbekannt"])


def test_hotel_continuation_lines_are_tolerated_only_after_hotel():
    # Umgebrochene Hotelzeile direkt nach `Hotel:` ist ok …
    PARSER.normalized_elements(
        ["Hotel: Beispielhaus Flughafen,", "Telefon 0123 456789"])
    # … dieselbe Zeile ohne Hotel-Anker bricht ab.
    with pytest.raises(ValueError):
        PARSER.normalized_elements(["Telefon 0123 456789"])


def test_station_chain_detects_missing_leg():
    day = "03Jan26"
    complete = [
        "C/I DUS 0437",
        "GT/TRANSPO DUS 0437 0507 QDU",
        "DH/ICE 525G QDU 0522 0639 QFA",
        "GT/FUSSWEG QFA 0649 0704 FRA",
        "DE 4313 FRA 0740 0830 ZRH 32Q",
        "DE 4318 ZRH 1035 1130 FRA 32Q",
        "DE 4319 FRA 1330 1420 ZRH 32Q",
        "C/O 1450 ZRH",
    ]
    PARSER.check_station_chain(day, complete)
    # Fällt der ZRH-FRA-Rückflug raus, reißt die Kette FRA≠ZRH.
    broken = complete[:5] + complete[6:]
    with pytest.raises(ValueError):
        PARSER.check_station_chain(day, broken)


def _mini_passes():
    """Zwei Revisionen über zwei Tage: neueste zuerst (wie im Dokument)."""
    newest = {"ts": PARSER.datetime(2026, 2, 26, 23, 21), "days": {
        "01Jan26": (["SB90 DUS 0230 1830"],
                    ["C/I DUS 0700", "DE 100 DUS 0800 0900 FRA 320",
                     "C/O 0930 FRA"]),
        "02Jan26": (["U DUS"], ["U DUS"]),
    }}
    oldest = {"ts": PARSER.datetime(2025, 12, 17, 14, 38), "days": {
        "01Jan26": ([], ["SB90 DUS 0230 1830"]),
        "02Jan26": ([], ["U DUS"]),
    }}
    return [newest, oldest]


def test_revision_chain_accepts_consistent_history():
    PARSER.check_revision_chain(_mini_passes())


def test_revision_chain_detects_swallowed_line():
    passes = _mini_passes()
    # Die old-Spalte der neuesten Revision verliert ihre Standby-Zeile —
    # genau das Symptom einer verschluckten Zeile.
    passes[0]["days"]["01Jan26"] = ([], passes[0]["days"]["01Jan26"][1])
    with pytest.raises(ValueError):
        PARSER.check_revision_chain(passes)


def test_final_legs_come_from_newest_revision_not_last_occurrence():
    legs, _ = PARSER.final_legs(_mini_passes(), "FO")
    # Ur-Revision hatte nur Standby; final ist der notifizierte Flug.
    assert [l["flight"] for l in legs] == ["DE100"]
    assert legs[0]["role"] == "FO"
    assert legs[0]["block_min"] == 60


def test_overnight_leg_rolls_arrival_to_next_day():
    passes = _mini_passes()
    passes[0]["days"]["02Jan26"] = (
        ["U DUS"],
        ["C/I FRA 2200", "DE 200 FRA 2300 0130 LPA 32N"])
    legs, _ = PARSER.final_legs(passes, "FO")
    red_eye = [l for l in legs if l["flight"] == "DE200"][0]
    assert red_eye["dep_iso"].startswith("2026-01-02T23:00")
    assert red_eye["arr_iso"].startswith("2026-01-03T01:30")
    assert red_eye["block_min"] == 150


def test_build_passes_requires_strictly_descending_timestamps():
    rows = []
    for ts in ("17Dec25-14:38", "26Feb26-23:21"):  # aufsteigend = falsch
        rows.append(("day", "01Jan26"))
        rows.append(("rev_ts", ts))
        rows.append(("content", ("", "U DUS")))
        rows.append(("day", "02Jan26"))
        rows.append(("content", ("", "U DUS")))
    with pytest.raises(ValueError):
        PARSER.build_passes(rows, ("01Jan26", "02Jan26"))


def test_build_passes_requires_full_period_coverage():
    rows = [("day", "01Jan26"), ("rev_ts", "17Dec25-14:38"),
            ("content", ("", "U DUS"))]
    with pytest.raises(ValueError):
        PARSER.build_passes(rows, ("01Jan26", "02Jan26"))
