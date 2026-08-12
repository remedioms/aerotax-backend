"""Strict FAA Logbook Pro parser regressions."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools", "logbook-parsers"))

import parse_faa_logbook as parser  # noqa: E402


class Page:
    def __init__(self, rows, number):
        self.rows = rows
        self.page_number = number
        self.closed = False

    def extract_words(self):
        return self.rows

    def close(self):
        self.closed = True


class PDF:
    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


EMPTY_A = {key: "" for key in parser.A_COLS}
EMPTY_B = {key: "" for key in parser.B_COLS}


def test_faa_rows_reconcile_flight_sim_and_landings(monkeypatch, tmp_path):
    page_a = Page([
        {**EMPTY_A, "date": "01/01/24", "from": "FRA", "to": "MUC",
         "total": "01:00", "type": "A320", "reg": "D-AIZI",
         "ldg_day": "1"},
        {**EMPTY_A, "date": "02/01/24", "from": "FRA", "to": "FRA"},
        {**EMPTY_A, "reg": "AMOUNT", "from": "FOREWARDED",
         "total": "10:00", "ldg_day": "5"},
        {**EMPTY_A, "from": "TOTALS", "to": "TO DATE",
         "total": "11:00", "ldg_day": "6"},
        EMPTY_A,
    ], 2)
    page_b = Page([
        {**EMPTY_B, "pic": "01:00", "night": "00:20"},
        {**EMPTY_B, "sim": "01:00"},
        {**EMPTY_B, "sim": "02:00"},
        {**EMPTY_B, "sim": "03:00"},
        EMPTY_B,
    ], 3)
    cover = Page([], 1)
    fake_pdf = PDF([cover, page_a, page_b])
    monkeypatch.setattr(parser.pdfplumber, "open", lambda _path: fake_pdf)
    monkeypatch.setattr(parser, "bands",
                        lambda page: [index * 10
                                      for index in range(len(page.rows) + 1)])
    monkeypatch.setattr(parser, "cells",
                        lambda rows, top, _bottom, _columns:
                        rows[int(top // 10)])
    source = tmp_path / "faa.pdf"
    source.write_bytes(b"%PDF-fake")

    legs, sims, report = parser.parse_pdf(str(source))
    assert legs == [{
        "date": "2024-01-01", "from": "FRA", "to": "MUC",
        "block_min": 60, "_source_format": "offblock_faa",
        "type": "A320", "reg": "D-AIZI", "ldg_day": 1,
        "night_min": 20, "role": "PIC",
    }]
    assert sims == [{"date": "2024-01-02", "duration_min": 60,
                     "code": "FSTD"}]
    assert report["control"] == "OK"
    assert report["carryover_min"] == 600
    assert report["carryover_ldg_day"] == 5
    assert report["carryover_ldg_night"] == 0
    assert report["carryover_landings"] == 5
    assert report["final_landings"] == 6
    assert page_a.closed and page_b.closed


def test_faa_control_mismatch_is_rejected(monkeypatch, tmp_path):
    page_a = Page([
        {**EMPTY_A, "date": "01/01/24", "from": "FRA", "to": "MUC",
         "total": "01:00"},
        {**EMPTY_A, "reg": "AMOUNT", "from": "FOREWARDED",
         "total": "10:00"},
        {**EMPTY_A, "from": "TOTALS", "to": "TO DATE",
         "total": "12:00"}, EMPTY_A,
    ], 2)
    page_b = Page([EMPTY_B, EMPTY_B, EMPTY_B, EMPTY_B], 3)
    monkeypatch.setattr(parser.pdfplumber, "open",
                        lambda _path: PDF([Page([], 1), page_a, page_b]))
    monkeypatch.setattr(parser, "bands",
                        lambda page: [index * 10
                                      for index in range(len(page.rows) + 1)])
    monkeypatch.setattr(parser, "cells",
                        lambda rows, top, _bottom, _columns:
                        rows[int(top // 10)])
    source = tmp_path / "bad-faa.pdf"
    source.write_bytes(b"%PDF-fake")
    try:
        parser.parse_pdf(str(source))
    except ValueError as exc:
        assert "Block 60 != PDF delta 120 min" in str(exc)
    else:
        raise AssertionError("FAA total mismatch was accepted")
