#!/usr/bin/env python3
"""ForeFlight ``Pilot Logbook`` EASA PDF -> AeroX import facts.

The export consists of three front-matter pages followed by paired A/B
logbook pages.  The A side carries the flight, role and landing columns; the
B side carries instrument/training values for the same rows.  No departure
clock or flight number is printed, so the parser deliberately leaves those
unknown instead of reconstructing them from remarks.

Every dated row must be either a flight with total time or a simulator
session.  Parsed flight time, day/night landings and simulator duration are
checked against the document's final cumulative totals.
"""

import re

import pdfplumber

from legkeys import dedupe_keys


DATE_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{2})$")
DURATION_RE = re.compile(r"^(\d{1,4}):(\d{2})$")
ROUTE_RE = re.compile(r"^([A-Z0-9]{3,4})\s+-\s+([A-Z0-9]{3,4})$")
AIRCRAFT_RE = re.compile(r"^(.+?)\s*\(([^()]+)\)$")

# Horizontal ranges from the PDF's stable letter-landscape table geometry.
A_COLS = {
    "aircraft": (52, 119),
    "route": (119, 242),
    "total": (242, 275),
    "pic": (275, 299),
    "copilot": (299, 327),
    "multipilot": (327, 362),
    "picus": (362, 393),
    "night": (393, 484),
    "ldg_day": (714, 746),
    "ldg_night": (746, 791),
}
B_COLS = {
    "ifr": (20, 55),
    "instructor": (147, 180),
    "dual": (180, 210),
    "simulator": (210, 241),
}


def _minutes(value):
    match = DURATION_RE.fullmatch((value or "").strip())
    if not match:
        return None
    hours, minutes = map(int, match.groups())
    if minutes > 59:
        raise ValueError("invalid ForeFlight duration")
    return hours * 60 + minutes


def _integer(value):
    value = (value or "").strip()
    return int(value) if value.isdigit() else 0


def _text(words, top, bounds, tolerance=3.0):
    x0, x1 = bounds
    selected = [word for word in words
                if x0 <= word["x0"] < x1
                and abs(word["top"] - top) <= tolerance]
    selected.sort(key=lambda word: word["x0"])
    return " ".join(word["text"] for word in selected).strip()


def _date(value):
    match = DATE_RE.fullmatch(value)
    if not match:
        raise ValueError("invalid ForeFlight date")
    day, month, year = map(int, match.groups())
    year += 2000 if year < 70 else 1900
    # ISO parsing performs the calendar validation for us.
    from datetime import date
    return date(year, month, day).isoformat()


def _summary_top(words, kind):
    """Return the y-coordinate of ``TOTALS THIS PAGE``/``TO DATE``."""
    for word in words:
        if word["text"].upper() != "TOTALS":
            continue
        same = {item["text"].upper() for item in words
                if abs(item["top"] - word["top"]) <= 1.5
                and 115 <= item["x0"] < 180}
        expected = {"TOTALS", "THIS", "PAGE"} if kind == "page" \
            else {"TOTALS", "TO", "DATE"}
        if expected.issubset(same):
            return word["top"]
    return None


def _source_total(words, top, bounds):
    if top is None:
        return None
    value = _text(words, top, bounds, tolerance=2.0)
    return _minutes(value)


def matches_pdf(path):
    """Match only the paired ForeFlight EASA ``Pilot Logbook`` layout."""
    try:
        with pdfplumber.open(path) as pdf:
            if len(pdf.pages) < 5 or (len(pdf.pages) - 3) % 2:
                return False
            cover = re.sub(r"\s+", " ",
                           pdf.pages[0].extract_text() or "").strip()
            a_text = re.sub(r"\s+", " ",
                            pdf.pages[3].extract_text() or "").upper()
            b_text = re.sub(r"\s+", " ",
                            pdf.pages[4].extract_text() or "").upper()
            return (cover.startswith("Pilot Logbook |")
                    and "TYPE OF PILOTING TIME" in a_text
                    and "CATEGORY / CLASS" in a_text
                    and "INSTRUMENT TRAINING" in b_text
                    and "ADDITIONAL COMMENTS AND REMARKS" in b_text)
    except Exception:
        return False


def parse_pdf(path):
    legs, sims = [], []
    source_block = source_day = source_night = source_sim = None
    running_block = 0
    row_count = empty_rows = 0

    with pdfplumber.open(path) as pdf:
        if len(pdf.pages) < 5 or (len(pdf.pages) - 3) % 2:
            raise ValueError("ForeFlight EASA page pairing is incomplete")

        for index in range(3, len(pdf.pages), 2):
            page_a, page_b = pdf.pages[index], pdf.pages[index + 1]
            words_a = page_a.extract_words(x_tolerance=1, y_tolerance=2)
            words_b = page_b.extract_words(x_tolerance=1, y_tolerance=2)
            header_a = (page_a.extract_text() or "").upper()
            header_b = (page_b.extract_text() or "").upper()
            if ("TYPE OF PILOTING TIME" not in header_a
                    or "INSTRUMENT" not in header_b
                    or "TRAINING" not in header_b):
                raise ValueError("ForeFlight EASA paired-page header missing")

            page_block = 0
            date_words = [word for word in words_a
                          if word["x0"] < 52
                          and DATE_RE.fullmatch(word["text"])]
            date_words.sort(key=lambda word: word["top"])
            if not date_words:
                raise ValueError("ForeFlight EASA page contains no dated rows")

            for date_word in date_words:
                row_count += 1
                top_a = date_word["top"]
                # The B-side baseline is consistently 8.2pt above the A-side
                # baseline in this export. Core numeric cells are single-line.
                top_b = top_a - 8.2
                day = _date(date_word["text"])
                total = _minutes(_text(words_a, top_a, A_COLS["total"]))
                sim_min = _minutes(
                    _text(words_b, top_b, B_COLS["simulator"]))
                if total and sim_min:
                    raise ValueError(
                        "ForeFlight EASA row is both flight and simulator")
                # ForeFlight retains incomplete draft records (for example an
                # aircraft/date with ``No Dest``) in the export. With neither
                # flight time nor simulator time they carry no importable
                # logbook fact and are excluded explicitly, not invented.
                if not total and not sim_min:
                    empty_rows += 1
                    continue

                aircraft = _text(words_a, top_a, A_COLS["aircraft"])
                aircraft_match = AIRCRAFT_RE.fullmatch(aircraft)
                reg = aircraft_match.group(1).replace(" ", "") \
                    if aircraft_match else aircraft.replace(" ", "")
                aircraft_type = aircraft_match.group(2).replace(" ", "") \
                    if aircraft_match else None

                if sim_min:
                    sim = {
                        "date": day,
                        "duration_min": sim_min,
                        "code": aircraft_type or reg or "FSTD",
                        "_source_format": "foreflight_easa",
                    }
                    sims.append(sim)
                    continue

                route = _text(words_a, top_a, A_COLS["route"])
                route_match = ROUTE_RE.fullmatch(route)
                if not route_match:
                    raise ValueError(
                        f"ForeFlight EASA flight route missing on {day}")
                leg = {
                    "date": day,
                    "from": route_match.group(1),
                    "to": route_match.group(2),
                    "block_min": total,
                    "_source_format": "foreflight_easa",
                }
                if reg:
                    leg["reg"] = reg
                if aircraft_type:
                    leg["type"] = aircraft_type.upper()

                role_values = {
                    key: _minutes(_text(words_a, top_a, A_COLS[key]))
                    for key in ("pic", "copilot", "multipilot", "picus")
                }
                if role_values["pic"]:
                    leg["role"] = "PIC"
                elif role_values["picus"]:
                    leg["role"] = "PICUS"
                elif role_values["copilot"] or role_values["multipilot"]:
                    leg["role"] = "FO"
                elif _minutes(_text(words_b, top_b, B_COLS["instructor"])):
                    leg["role"] = "FI"
                elif _minutes(_text(words_b, top_b, B_COLS["dual"])):
                    leg["role"] = "DUAL"

                night = _minutes(_text(words_a, top_a, A_COLS["night"]))
                if night:
                    leg["night_min"] = night
                ifr = _minutes(_text(words_b, top_b, B_COLS["ifr"]))
                if ifr:
                    leg["ifr_min"] = ifr
                for key in ("ldg_day", "ldg_night"):
                    value = _integer(_text(words_a, top_a, A_COLS[key]))
                    if value:
                        leg[key] = value
                legs.append(leg)
                page_block += total
                running_block += total

            page_top = _summary_top(words_a, "page")
            cumulative_top = _summary_top(words_a, "cumulative")
            printed_page = _source_total(
                words_a, page_top, A_COLS["total"])
            printed_running = _source_total(
                words_a, cumulative_top, A_COLS["total"])
            if printed_page is None or printed_page != page_block:
                raise ValueError(
                    "ForeFlight EASA page flight-total mismatch")
            if printed_running is None or printed_running != running_block:
                raise ValueError(
                    "ForeFlight EASA cumulative flight-total mismatch")
            source_block = printed_running
            source_day = _integer(
                _text(words_a, cumulative_top, A_COLS["ldg_day"], 2.0))
            source_night = _integer(
                _text(words_a, cumulative_top, A_COLS["ldg_night"], 2.0))

            # Simulator page/cumulative values share the simulator column but
            # have no labels on the B side. The greatest non-row value is the
            # monotonically increasing cumulative source control.
            row_tops_b = [word["top"] - 8.2 for word in date_words]
            footer_values = []
            for word in words_b:
                if not (B_COLS["simulator"][0] <= word["x0"]
                        < B_COLS["simulator"][1]):
                    continue
                value = _minutes(word["text"])
                if value is None or any(abs(word["top"] - row_top) <= 3
                                        for row_top in row_tops_b):
                    continue
                footer_values.append(value)
            if footer_values:
                source_sim = max(source_sim or 0, max(footer_values))

            page_a.close()
            page_b.close()

    if not row_count or not legs:
        raise ValueError("ForeFlight EASA contains no flight facts")
    parsed_day = sum(leg.get("ldg_day", 0) for leg in legs)
    parsed_night = sum(leg.get("ldg_night", 0) for leg in legs)
    parsed_sim = sum(sim["duration_min"] for sim in sims)
    if source_block != sum(leg["block_min"] for leg in legs):
        raise ValueError("ForeFlight EASA final flight-total mismatch")
    if (source_day, source_night) != (parsed_day, parsed_night):
        raise ValueError("ForeFlight EASA landing-total mismatch")
    if source_sim is None or source_sim != parsed_sim:
        raise ValueError("ForeFlight EASA simulator-total mismatch")

    legs.sort(key=lambda leg: leg["date"])
    sims.sort(key=lambda sim: sim["date"])
    collisions = dedupe_keys(legs)
    return legs, sims, {
        "parser": "parse_foreflight_easa.py",
        "format": "foreflight_easa_pilot_logbook",
        "month": f"{legs[0]['date'][:7]}–{legs[-1]['date'][:7]}",
        "control": "OK",
        "rows": row_count,
        "empty_rows_excluded": empty_rows,
        "dedupe_suffixes": collisions,
        "totals": {
            "legs": len(legs),
            "block_min": source_block,
            "landings": parsed_day + parsed_night,
            "sim_sessions": len(sims),
            "sim_min": source_sim,
        },
    }
