"""Merge-Semantik des Flugbuch-Wächters (tools/logbook-parsers).

Der Wächter verschmilzt neue Parser-Legs mit dem BESTEHENDEN
`ax_logbook_import` des Nutzers (eine Zeile pro Token). Diese Tests sichern
die vier Eigenschaften, an denen Datenverlust oder Erfindung hinge:

  1. Union statt Ersetzen — Bestehendes überlebt jeden Re-Import.
  2. Identischer Schlüssel + identische Blockzeit = Dublette (kein Doppel-Leg).
  3. Identischer Schlüssel + höchstens 1 Minute Rundungsdifferenz = Dublette;
     größere Abweichung = Konflikt → ValueError (manuelle Prüfung).
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


def test_duplicate_key_one_minute_rounding_drift_is_noop():
    rounded = dict(OLD[0], block_min=OLD[0]["block_min"] - 1)
    merged, added = merge_legs(OLD, [rounded])
    assert added == 0
    assert len(merged) == 2
    assert merged[0]["block_min"] == OLD[0]["block_min"]


def test_duplicate_key_different_block_raises():
    clash = dict(OLD[0], block_min=999)
    with pytest.raises(ValueError, match="Merge-Konflikt"):
        merge_legs(OLD, [clash])


def test_merge_sorts_by_date_then_dep():
    new = [_leg("2022-07-01", "LH050", "FRA", "MUC",
                "2022-07-01T06:00:00Z", 60)]
    merged, _ = merge_legs(OLD, new)
    assert [l["flight"] for l in merged] == ["LH050", "LH100", "LH101"]


def test_legs_without_dep_iso_do_not_duplicate_on_block_drift():
    # Alt-Importe (z.B. Condor-Historie, FAA-Layouts) tragen Legs ohne dep_iso.
    # Die Blockzeit stand früher im Schlüssel — eine Minute Rundungsdifferenz
    # war damit eine neue Identität und erzeugte STILL ein zweites Leg mit
    # doppelten Landungen. Blockzeit ist eine Messung, keine Identität: die
    # Toleranz in merge_legs greift, der Bestand gewinnt.
    a = {"date": "2024-01-01", "flight": "DE123", "from": "FRA", "to": "PMI",
         "block_min": 500}
    b = dict(a, block_min=501)
    merged, added = merge_legs([a], [b])
    assert added == 0
    assert len(merged) == 1
    assert merged[0]["block_min"] == 500      # Bestand gewinnt
    assert merged[0]["flight"] == "DE123"     # keine „(2)"-Belegung
    # Andere Richtung (kleinerer Wert kommt neu) genauso.
    merged, added = merge_legs([dict(a, block_min=501)], [dict(a, block_min=500)])
    assert added == 0 and len(merged) == 1 and merged[0]["block_min"] == 501


def test_legs_without_dep_iso_still_conflict_on_a_real_difference():
    # Kein stilles Zusammenlegen echter Unterschiede: das bleibt ein Konflikt
    # und schickt den Batch in `review`.
    a = {"date": "2024-01-01", "flight": "DE123", "from": "FRA", "to": "PMI",
         "block_min": 130}
    with pytest.raises(ValueError, match="Merge-Konflikt"):
        merge_legs([a], [dict(a, block_min=190)])


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


def test_try_parsers_accepts_expense_statement_without_inventing_legs(
        monkeypatch, tmp_path):
    path = tmp_path / "expense.upload"
    path.write_bytes(b"%PDF-expense")
    text = ("Streckeneinsatz-Abrechnung\n"
            "Datum Ab An Spesenanspruch stfrei\n17.07.2024 14:00")
    monkeypatch.setattr(pdfplumber, "open",
                        lambda _path: _FakePDF([_FakePage(text, [])]))
    import parse_fcl050_v2
    import parse_faa_logbook
    monkeypatch.setattr(parse_fcl050_v2, "matches_pdf", lambda _path: False)
    monkeypatch.setattr(parse_faa_logbook, "matches_pdf", lambda _path: False)
    name, legs, sims, report = logbook_watchdog._try_parsers(str(path))
    assert name == "informational_pdf"
    assert legs == sims == []
    assert report["document_type"] == "streckeneinsatzabrechnung"


def test_try_parsers_accepts_aggregate_statistics_without_fake_legs(
        monkeypatch, tmp_path):
    path = tmp_path / "stats.upload"
    path.write_bytes(b"%PDF-stats")
    text = ("Flight Time and Landings\n"
            "Total since entry: 3203:28 703\n")
    monkeypatch.setattr(pdfplumber, "open",
                        lambda _path: _FakePDF([_FakePage(text, [])]))
    import parse_fcl050_v2
    import parse_faa_logbook
    monkeypatch.setattr(parse_fcl050_v2, "matches_pdf", lambda _path: False)
    monkeypatch.setattr(parse_faa_logbook, "matches_pdf", lambda _path: False)
    name, legs, sims, report = logbook_watchdog._try_parsers(str(path))
    assert name == "informational_pdf"
    assert legs == sims == []
    assert report["document_type"] == "aggregate_flight_time_statistics"


def test_try_parsers_routes_fcl050_before_generic_pdf(monkeypatch, tmp_path):
    import parse_fcl050_v2
    path = tmp_path / "fcl.upload"
    path.write_bytes(b"%PDF-fcl")
    report = {"month": "2012-01–2026-08", "carryover_min": 42}
    monkeypatch.setattr(parse_fcl050_v2, "matches_pdf", lambda _path: True)
    monkeypatch.setattr(parse_fcl050_v2, "parse_pdf",
                        lambda _path: ([OLD[0]], [], report))
    assert logbook_watchdog._try_parsers(str(path)) == (
        "offblock_fcl050", [OLD[0]], [], report)


def test_try_parsers_routes_faa_logbook_before_generic_pdf(
        monkeypatch, tmp_path):
    import parse_faa_logbook
    import parse_fcl050_v2
    path = tmp_path / "faa.upload"
    path.write_bytes(b"%PDF-faa")
    report = {"month": "2012-01–2026-08", "carryover_min": 42}
    monkeypatch.setattr(parse_fcl050_v2, "matches_pdf", lambda _path: False)
    monkeypatch.setattr(parse_faa_logbook, "matches_pdf", lambda _path: True)
    monkeypatch.setattr(parse_faa_logbook, "parse_pdf",
                        lambda _path: ([OLD[0]], [], report))
    assert logbook_watchdog._try_parsers(str(path)) == (
        "offblock_faa", [OLD[0]], [], report)


def test_faa_generic_sim_twins_yield_to_descriptive_easa_rows():
    sims = [
        {"date": "2026-01-10", "code": "RC25_1", "duration_min": 240},
        {"date": "2026-01-10", "code": "FSTD", "duration_min": 240},
        # A second generic session has no descriptive twin and must survive.
        {"date": "2026-01-10", "code": "FSTD", "duration_min": 180},
    ]
    kept, removed = logbook_watchdog.remove_generic_faa_sim_twins(sims)
    assert removed == 1
    assert kept == [sims[0], sims[2]]


def test_try_parsers_routes_jeppesen_roster(monkeypatch, tmp_path):
    import parse_fcl050_v2
    import parse_roster_logbook
    path = tmp_path / "roster.upload"
    path.write_bytes(b"%PDF-roster")
    text = "Released Roster\nMonth: July 2026\nCompany Name: YF"
    monkeypatch.setattr(pdfplumber, "open",
                        lambda _path: _FakePDF([_FakePage(text, [])]))
    monkeypatch.setattr(parse_fcl050_v2, "matches_pdf", lambda _path: False)
    payload = {"legs": [OLD[0]], "sim": [],
               "report": {"month": "2026-07"}}
    monkeypatch.setattr(parse_roster_logbook, "parse_sources",
                        lambda *_args, **_kwargs: payload)
    assert logbook_watchdog._try_parsers(str(path)) == (
        "roster_logbook", [OLD[0]], [], payload["report"])


def test_roster_batch_keeps_only_newest_complete_month_revision():
    old = {"id": 1, "parser": "roster_logbook",
           "report": {"coverage_months": ["2026-06"],
                      "source_created_at": "2026-05-20T13:00:00+00:00"},
           "legs": [dict(OLD[0], _roster_month="2026-06")]}
    new = {"id": 2, "parser": "roster_logbook",
           "report": {"coverage_months": ["2026-06"],
                      "source_created_at": "2026-06-03T17:26:00+00:00"},
           "legs": [dict(OLD[1], _roster_month="2026-06")]}
    assert logbook_watchdog.resolve_roster_revisions([old, new]) == 1
    assert old["legs"] == []
    assert new["legs"] == [OLD[1]]


def test_merge_dedupes_faa_twin_without_clock_against_fcl_leg():
    fcl = dict(OLD[0], type="B 757-300", reg="D-ABCD",
               ldg_day=1, night_min=20)
    faa = {key: value for key, value in fcl.items() if key != "dep_iso"}
    faa["type"] = "B757-300"  # templates format spaces differently
    merged, added = merge_legs([fcl], [faa])
    assert added == 0 and merged == [fcl]


def test_fcl_cleanup_removes_only_pure_sim_copy():
    pure_sim = {"date": "2022-01-10", "from": "FRA", "to": "FRA"}
    landing_training = {"date": "2022-01-11", "from": "FRA", "to": "FRA",
                        "ldg_day": 3}
    flight = dict(OLD[0])
    kept, removed = logbook_watchdog.remove_fcl_sim_leg_artifacts(
        [pure_sim, landing_training, flight],
        [{"date": "2022-01-10"}, {"date": "2022-01-11"}])
    assert removed == 1
    assert kept == [landing_training, flight]


def test_try_parsers_turns_corrupt_pdf_into_terminal_unsupported(tmp_path):
    path = tmp_path / "broken.upload"
    path.write_bytes(b"%PDF-this-is-not-a-real-pdf")
    assert logbook_watchdog._try_parsers(str(path)) == \
        ("unsupported", None, None, None)


def test_try_parsers_does_not_send_arbitrary_csv_to_pdfplumber(
        monkeypatch, tmp_path):
    import builtins

    path = tmp_path / "other.upload"
    path.write_text("foo,bar\n1,2\n", encoding="utf-8")
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "parse_edw_xlsx":
            raise AssertionError("XLSX runtime darf für CSV nicht laden")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
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
