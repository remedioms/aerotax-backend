"""Merge-Semantik des Flugbuch-Wächters (tools/logbook-parsers).

Der Wächter verschmilzt neue Parser-Legs mit dem BESTEHENDEN
`ax_logbook_import` des Nutzers (eine Zeile pro Token). Diese Tests sichern
die vier Eigenschaften, an denen Datenverlust oder Erfindung hinge:

  1. Union statt Ersetzen — Bestehendes überlebt jeden Re-Import.
  2. Identischer Schlüssel + identische Blockzeit = Dublette (kein Doppel-Leg).
  3. Identischer Schlüssel + ABWEICHENDE Blockzeit = Konflikt → ValueError
     (der Wächter schickt den Batch dann in die manuelle Prüfung).
  4. Was der LESER im Backend nicht auseinanderhalten kann, wird vor dem
     Schreiben nummeriert — sonst frisst sein gröberer Schlüssel ein Leg.

Dazu die Format-Erkennung selbst: `_try_parsers` entpackt die Rückgaben
ZWEIER Parser mit VERSCHIEDENER Stelligkeit. Der Test läuft deshalb gegen die
echten Parser-Module (nur die PDF-Text-Ebene ist synthetisch).
"""

import os
import sys

import pdfplumber
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools", "logbook-parsers"))

import logbook_watchdog  # noqa: E402
from logbook_watchdog import (  # noqa: E402
    _capped_source, dedupe_for_reader, merge_legs, merge_sims,
)


def _leg(date, flight, frm, to, dep, block, **kw):
    leg = {"date": date, "flight": flight, "from": frm, "to": to,
           "dep_iso": dep, "block_min": block}
    leg.update(kw)
    return leg


OLD = [
    _leg("2022-07-01", "LH100", "FRA", "JFK", "2022-07-01T10:00:00Z", 480),
    _leg("2022-07-03", "LH101", "JFK", "FRA", "2022-07-03T22:00:00Z", 430),
]


def test_union_keeps_existing_and_adds_new():
    new = [_leg("2026-02-06", "LH1642", "MUC", "GDN",
                "2026-02-06T10:26:00Z", 92)]
    merged, added = merge_legs(OLD, new)
    assert added == 1
    assert len(merged) == 3
    # Bestehende Legs unverändert enthalten (Identität, nicht nur Anzahl).
    for leg in OLD:
        assert leg in merged


def test_duplicate_key_same_block_is_noop():
    merged, added = merge_legs(OLD, [dict(OLD[0])])
    assert added == 0
    assert len(merged) == 2


def test_duplicate_key_different_block_raises():
    clash = dict(OLD[0], block_min=999)
    with pytest.raises(ValueError, match="Merge-Konflikt"):
        merge_legs(OLD, [clash])


def test_merge_sorts_by_date_then_dep():
    new = [_leg("2022-07-01", "LH050", "FRA", "MUC",
                "2022-07-01T06:00:00Z", 60)]
    merged, _ = merge_legs(OLD, new)
    assert [l["flight"] for l in merged] == ["LH050", "LH100", "LH101"]


def test_legs_without_dep_iso_fall_back_to_block_key():
    # Alt-Importe (z.B. Condor-Historie) können Legs ohne dep_iso tragen —
    # der Schlüssel weicht dann auf die Blockzeit aus, statt zu kollidieren.
    # ABER: der Leser im Backend kennt nur `date|flight|from|to`. Beide Legs
    # trügen dort denselben Schlüssel, das erste verschwände samt Landungen.
    # Deshalb bekommt die zweite Belegung vor dem Schreiben ihr „(2)".
    a = {"date": "2024-01-01", "flight": "DE123", "from": "FRA", "to": "PMI",
         "block_min": 130}
    b = dict(a, block_min=131)  # gleiche Strecke, anderer Block ⇒ eigener Key
    merged, added = merge_legs([a], [b])
    assert added == 1 and len(merged) == 2
    assert len(dedupe_for_reader(merged)) == 1
    assert [l["flight"] for l in merged] == ["DE123", "DE123(2)"]


def test_dedupe_is_stable_across_reruns_and_numbers_a_third_leg():
    # Zweiter Lauf über eine bereits nummerierte Liste darf weder umbenennen
    # noch „(2)" doppelt vergeben — sonst kollidierten die Legs erneut.
    legs = [{"date": "2024-01-01", "flight": "DE123", "from": "FRA",
             "to": "PMI", "block_min": 130 + i} for i in range(3)]
    dedupe_for_reader(legs)
    assert [l["flight"] for l in legs] == ["DE123", "DE123(2)", "DE123(3)"]
    dedupe_for_reader(legs)
    assert [l["flight"] for l in legs] == ["DE123", "DE123(2)", "DE123(3)"]


def test_suffixed_leg_is_not_reimported_as_new():
    # Derselbe Upload ein zweites Mal: das gespeicherte „DE123(2)" und das
    # frisch geparste „DE123" sind DASSELBE Leg (Suffix ist Lesehilfe, keine
    # Identität) — sonst wüchse das Flugbuch bei jedem Re-Upload.
    stored = {"date": "2024-01-01", "flight": "DE123(2)", "from": "FRA",
              "to": "PMI", "block_min": 131}
    fresh = dict(stored, flight="DE123")
    merged, added = merge_legs([stored], [fresh])
    assert added == 0 and len(merged) == 1


def test_source_label_is_capped_instead_of_growing_forever():
    label = "Watchdog: lh_flugstunden 2026-01"
    for month in range(2, 7):
        label = _capped_source(label, f"Watchdog: lh_flugstunden 2026-{month:02d}")
    assert label == ("… (+3) + Watchdog: lh_flugstunden 2026-04 + "
                     "Watchdog: lh_flugstunden 2026-05 + "
                     "Watchdog: lh_flugstunden 2026-06")
    assert _capped_source(None, "Watchdog: cfg_flugstunden 2026-05") == \
        "Watchdog: cfg_flugstunden 2026-05"


def test_empty_existing_is_fine():
    merged, added = merge_legs(None, OLD)
    assert added == 2 and len(merged) == 2


class _FakePage:
    """Nur die zwei pdfplumber-Methoden, die die Parser benutzen."""

    def __init__(self, text, words):
        self._text, self._words = text, words

    def extract_text(self):
        return self._text

    def extract_words(self, **_kwargs):
        return self._words


class _FakePDF:
    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _word(text, x0, top):
    return {"text": text, "x0": x0, "x1": x0 + 10, "top": top,
            "bottom": top + 8}


def _lh_pdf():
    """Leerer LH-Monat 07/2022: Kopf + Summenzeile mit Nullwerten."""
    text = "Flugstunden - Übersicht für Monat 07 / 2022"
    words = [_word("07", 50, 720), _word("0", 170, 720), _word("0,00", 200, 720)]
    return _FakePDF([_FakePage(text, words)])


def _cfg_pdf():
    """Leerer Condor-Monat 05/2026 — gleicher Kopf, eigener Summenblock."""
    text = "Condor Flugstunden - Übersicht für Monat 05 / 2026"
    words = [_word("Anzahl", 250, 700), _word("Landungen", 300, 700),
             _word("0", 300, 720), _word("0,00", 370, 720)]
    return _FakePDF([_FakePage(text, words)])


def test_try_parsers_unpacks_both_real_parser_signatures(monkeypatch, tmp_path):
    # Der Wächter entpackte fest dreistellig — die Condor-Variante gibt aber
    # nur (legs, report) zurück. Jeder Condor-Upload starb deshalb an einem
    # ValueError, der als „Kontrolle verletzt" in `review` gedeutet wurde.
    # Bewusst gegen die ECHTEN Parser: nur die PDF-Ebene ist synthetisch.
    lh = tmp_path / "lh.upload"
    condor = tmp_path / "condor.upload"
    lh.write_bytes(b"%PDF-fake-lh")
    condor.write_bytes(b"%PDF-fake-condor")
    docs = {str(lh): _lh_pdf(), str(condor): _cfg_pdf()}
    monkeypatch.setattr(pdfplumber, "open", lambda path: docs[path])

    name, legs, sims, report = logbook_watchdog._try_parsers(str(lh))
    assert (name, legs, sims) == ("lh_flugstunden", [], [])
    assert report["month"] == "2022-07"

    name, legs, sims, report = logbook_watchdog._try_parsers(str(condor))
    assert (name, legs, sims) == ("cfg_flugstunden", [], [])
    assert report["month"] == "2026-05"


def test_try_parsers_reports_unknown_pdf_without_touching_a_parser(
        monkeypatch, tmp_path):
    path = tmp_path / "unknown.upload"
    path.write_bytes(b"%PDF-fake-unknown")
    monkeypatch.setattr(pdfplumber, "open",
                        lambda path: _FakePDF([_FakePage("Bordkarte", [])]))
    assert logbook_watchdog._try_parsers(str(path)) == \
        ("unsupported", None, None, None)


def test_try_parsers_routes_offblock_csv_by_content(tmp_path):
    path = tmp_path / "duties.upload"
    path.write_text(
        "Type;Date;Function;Departure place;Departure time;Arrival place;"
        "Arrival time;Total time;Flight number;Aircraft registration;"
        "Aircraft ICAO;Pilot flying;Landing day (count)\n"
        "Flight;01.01.24;First Officer;FRA;08:00;MUC;09:10;01:10;"
        "LH100;D-AIZI;A320;Yes;1\n",
        encoding="utf-8",
    )
    name, legs, sims, report = logbook_watchdog._try_parsers(str(path))
    assert name == "offblock_duties"
    assert sims == [] and report["control"] == "OK"
    assert legs == [{
        "date": "2024-01-01", "flight": "LH100", "from": "FRA",
        "to": "MUC", "dep_iso": "2024-01-01T08:00:00Z",
        "arr_iso": "2024-01-01T09:10:00Z", "block_min": 70,
        "reg": "D-AIZI", "type": "A320", "pf": True,
        "ldg_day": 1, "role": "FO",
    }]


def test_try_parsers_turns_corrupt_pdf_into_terminal_unsupported(tmp_path):
    path = tmp_path / "broken.upload"
    path.write_bytes(b"%PDF-this-is-not-a-real-pdf")
    assert logbook_watchdog._try_parsers(str(path)) == \
        ("unsupported", None, None, None)


def test_try_parsers_does_not_send_arbitrary_csv_to_pdfplumber(
        monkeypatch, tmp_path):
    path = tmp_path / "other.upload"
    path.write_text("foo,bar\n1,2\n", encoding="utf-8")
    monkeypatch.setattr(pdfplumber, "open",
                        lambda _path: (_ for _ in ()).throw(
                            AssertionError("pdfplumber darf nicht laufen")))
    assert logbook_watchdog._try_parsers(str(path)) == \
        ("unsupported", None, None, None)


def test_merge_sims_dedupes_and_sorts():
    old = [{"date": "2022-05-02", "code": "MUC327", "duration_min": 240}]
    new = [dict(old[0]),
           {"date": "2022-05-01", "code": "MUC327", "duration_min": 240}]
    merged = merge_sims(old, new)
    assert len(merged) == 2
    assert merged[0]["date"] == "2022-05-01"
