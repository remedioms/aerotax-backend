#!/usr/bin/env python3
"""Condor-SAP-„Flugstunden - Übersicht" (CFG-Variante) → AeroX-Flugbuch-JSON.

    python3 parse_cfg_flugstunden.py --out ziel.json monat1.pdf [monat2.pdf ...]

Verifiziert an Upload #238 (Jakob Wunderlich, 05/2026). Gleiche Report-Familie
wie die LH-Variante (`parse_lh_flugstunden.py`), aber eigenes Layout:

* Detailzeile: `DATUM STRECKE [A|E] FROM OFF-ON TO TC BLOCKZEIT [L] FAKTOR
  ANR.FL-ZEIT | DH-STD  A/C /FZM /PIC` — Zeiten sind DEZIMALSTUNDEN (2,12 =
  2 h 07 min), nicht MIN./FAKT-Minuten.
* `TC 00` + `FAKTOR 1,00` = selbst geflogenes Leg (Wert in ANR. FL-ZEIT).
  `TC 01`/`TC 10` + `FAKTOR 0,00` = Deadhead/Bodenzeit (Wert in DH-STD) —
  darunter auch Bahn-Positionierungen (`IC…`-„Flugnummern") und
  Bodenblöcke mit FROM==TO. Deadheads sind KEINE Flugbuch-Legs.
* `A`/`E` in der Spalte hinter STRECKE markieren Dienstanfang/-ende; Zeilen
  ohne TC sind Tages-Status (FREIER TAG, URLAUB, BEREITSCHAFT, OFF …).
* `L` in der V/L-Spalte = persönliche Landung. Tag/Nacht wie beim LH-Parser
  über die bürgerliche Dämmerung am Zielflughafen (Zeiten OFF/ON = UTC).
* Monats-Summenblock (letzte Seite): „Anzahl Landungen" und „Effektive
  Flugstd." — die Kumulationszeile (mit `==>`) ist die Jahres-Summe und
  wird übersprungen.

KONTROLLE (harter Abbruch, Toleranz wie LH-Parser ±2 min Dezimal-Rundung):
Σ Leg-Blockminuten == „Effektive Flugstd." des Monats UND Anzahl L-Marker ==
„Anzahl Landungen". Eine Zeile, die still verloren geht, reißt die Summe.
Simulator: Trägt der Monat „anrech. Simulator Stunden" > 0, bricht der Lauf
ab — Sim-Zeilen dieser Quelle sind noch unbelegt, es wird nichts geraten.
"""

import argparse
import json
import os
import re
import sys

import pdfplumber

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from legkeys import dedupe_keys
from parse_lh_flugstunden import (
    CONTROL_ROUNDING_TOLERANCE_MIN,
    RE_HEADER,
    _clock_instants,
    control_minutes_match,
    decimal_hours_minutes,
    is_civil_night,
    normalized_registration,
)

RE_DATE = re.compile(r"^(\d{2})\.(\d{2})\.$")
RE_FLIGHT = re.compile(r"^([A-Z]{2})(\d{2,4}[A-Z]?)$")
RE_DECIMAL = re.compile(r"^\d+,\d{2}$")
RE_TC = re.compile(r"^\d{2}$")

# Spaltenfenster (x0), kalibriert an Upload #238; die Header-Anker werden vor
# dem Parsen gegen diese Fenster geprüft — verrutscht das Layout, bricht der
# Lauf ab statt still falsche Spalten zu lesen.
COLUMNS = {
    "datum": (44, 75),
    "strecke": (75, 125),
    "ae": (125, 150),
    "from": (150, 190),
    "offon": (190, 250),
    "to": (250, 289),
    "tc": (289, 309),
    "block": (309, 339),
    "vl": (339, 356),
    "faktor": (356, 394),
    "anr": (394, 428),
    "dh": (428, 456),
    "rest": (456, 595),
}
HEADER_ANCHORS = {
    "DATUM": "datum", "STRECKE": "strecke", "FROM": "from", "OFF": "offon",
    "TO": "to", "TC": "tc", "BLOCK": "block", "FAKTOR": "faktor",
    "ANR.": "anr", "DH-": "dh", "A/C": "rest",
}

ROLE_MAP = {"CP": "PIC", "CPT": "PIC", "FO": "FO", "SFO": "SFO"}


def role_from_funktion(funktion):
    """Dienststellen-Funktion → Flugbuch-Rolle. Kabinenfunktionen → FB."""
    return ROLE_MAP.get((funktion or "").strip().upper(), "FB")


def extract_cells(row_words):
    """Wortliste einer Detailzeile → Spalten-Dict nach COLUMNS-Fenstern."""
    cells = {}
    for word in sorted(row_words, key=lambda w: w["x0"]):
        for name, (x0, x1) in COLUMNS.items():
            if x0 <= word["x0"] < x1:
                cells[name] = (cells.get(name, "") + " " + word["text"]).strip()
                break
    return cells


def classify_row(cells):
    """Detailzeile klassifizieren: ('leg'|'dh'|'day', payload).

    Wirft ValueError bei jeder Kombination, die nicht eindeutig belegt ist —
    lieber Abbruch als ein geratenes Leg (Invariante 5, keine Fake-Werte).
    """
    tc = cells.get("tc")
    label = f"{cells.get('datum')} {cells.get('strecke') or cells.get('ae')}"
    if tc is None:
        # Tages-Status (FREIER TAG/URLAUB/BEREITSCHAFT (STANDBY)/OFF/…):
        # kein TC. Beschreibungstext darf in die Zeitfenster ragen, echte
        # Uhrzeiten oder Dezimalwerte dürfen es nicht (verlorene Leg-Zeile!).
        clock = re.search(r"\d{2}:\d{2}", cells.get("offon") or "")
        values = [v for v in (cells.get("anr"), cells.get("dh"),
                              cells.get("block"), cells.get("faktor"))
                  if v and RE_DECIMAL.fullmatch(v)]
        if clock or values:
            raise ValueError(f"Zeile ohne TC trägt Zeiten: {label}")
        return "day", None
    if not RE_TC.fullmatch(tc):
        raise ValueError(f"unlesbarer TC {tc!r}: {label}")
    vl = cells.get("vl")
    if vl not in (None, "L"):
        raise ValueError(f"unbekannter V/L-Wert {vl!r}: {label}")
    if tc == "00":
        if cells.get("faktor") != "1,00":
            raise ValueError(f"TC 00 ohne FAKTOR 1,00: {label}")
        if not cells.get("anr") or cells.get("dh"):
            raise ValueError(f"TC 00 ohne eindeutige ANR. FL-ZEIT: {label}")
        return "leg", None
    if tc in ("01", "10"):
        if cells.get("faktor") not in (None, "0,00"):
            raise ValueError(f"TC {tc} mit FAKTOR {cells.get('faktor')!r}: "
                             f"{label}")
        if not cells.get("dh") or cells.get("anr"):
            raise ValueError(f"TC {tc} ohne eindeutige DH-STD: {label}")
        if vl == "L":
            raise ValueError(f"Deadhead mit Landemarker: {label}")
        return "dh", None
    raise ValueError(f"unbekannter TC-Code {tc!r}: {label}")


def _check_header_anchors(words):
    """Detail-Tabellenkopf gegen die COLUMNS-Fenster prüfen (Layout-Wache)."""
    missing = []
    for text, column in HEADER_ANCHORS.items():
        x0, x1 = COLUMNS[column]
        if not any(w["text"] == text and x0 <= w["x0"] < x1 for w in words):
            missing.append(text)
    if missing:
        raise ValueError(
            "Detail-Tabellenkopf passt nicht zu den kalibrierten Spalten "
            f"(fehlend: {', '.join(missing)}) — Layout geprüft nachziehen"
        )


def _monthly_summary(page):
    """Monats-Summenzeile der letzten Seite: Landungen/Effektiv/Simulator.

    Der Block druckt zwei Wertzeilen: Kumulation bis Abrechnungsmonat (mit
    `==>`-Pfeil) und den Monat selbst. Nur der Monat zählt als Kontrolle.
    """
    words = page.extract_words(x_tolerance=1, y_tolerance=2)
    anchors = [w for w in words
               if w["text"] == "Landungen" and 290 <= w["x0"] < 335]
    if not anchors:
        raise ValueError("Summenblock ohne 'Anzahl Landungen'-Spalte")
    header_top = max(w["top"] for w in anchors)
    rows = {}
    for w in words:
        if w["top"] <= header_top + 4:
            continue
        rows.setdefault(round(w["top"] / 3), []).append(w)
    for _, row in sorted(rows.items()):
        texts = [w["text"] for w in row]
        if "==>" in texts:
            continue
        landings = [w["text"] for w in row
                    if 290 <= w["x0"] < 335 and w["text"].isdigit()]
        effective = [w["text"] for w in row
                     if 365 <= w["x0"] < 410 and RE_DECIMAL.fullmatch(w["text"])]
        simulator = [w["text"] for w in row
                     if 210 <= w["x0"] < 260 and RE_DECIMAL.fullmatch(w["text"])]
        if landings or effective:
            return {
                "landings": int(landings[0]) if landings else 0,
                "effective_min": (decimal_hours_minutes(effective[0])
                                  if effective else 0),
                "sim_min": (decimal_hours_minutes(simulator[0])
                            if simulator else 0),
            }
    raise ValueError("Monats-Summenzeile nicht gefunden")


def parse_pdf(path):
    with pdfplumber.open(path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        header = RE_HEADER.search(text)
        if not header:
            raise ValueError(f"kein Flugstundenübersicht-Kopf: {path}")
        if "Condor" not in text:
            raise ValueError(f"kein Condor-Dokument (LH-Variante? → "
                             f"parse_lh_flugstunden.py): {path}")
        month, year = map(int, header.groups())
        funktion_match = re.search(r"Funktion\s+([A-Z]{2,3})\b", text)
        role = role_from_funktion(funktion_match.group(1)
                                  if funktion_match else None)

        summary = _monthly_summary(pdf.pages[-1])
        if summary["sim_min"] > 0:
            raise ValueError(
                f"Monat {month:02d}/{year} weist {summary['sim_min']} min "
                "Simulator aus — Sim-Zeilen der CFG-Variante sind unbelegt, "
                "Import abgebrochen statt geraten"
            )

        legs, deadheads = [], []
        day_rows = landing_marks = 0
        for page in pdf.pages:
            words = page.extract_words(x_tolerance=1, y_tolerance=2)
            date_words = [w for w in words
                          if RE_DATE.fullmatch(w["text"] or "")
                          and COLUMNS["datum"][0] <= w["x0"]
                          < COLUMNS["datum"][1]]
            if date_words:
                _check_header_anchors(words)
            for date_word in date_words:
                row = [w for w in words
                       if abs(w["top"] - date_word["top"]) < 2.5]
                cells = extract_cells(row)
                kind, _ = classify_row(cells)
                if kind == "day":
                    day_rows += 1
                    continue

                day, printed_month = map(
                    int, RE_DATE.fullmatch(date_word["text"]).groups())
                if printed_month != month:
                    raise ValueError(
                        f"Zeilenmonat {printed_month:02d} != Kopf {month:02d}")
                flight_match = RE_FLIGHT.fullmatch(
                    (cells.get("strecke") or "").replace(" ", ""))
                if not flight_match:
                    raise ValueError(
                        f"unlesbare Flugnummer {cells.get('strecke')!r} am "
                        f"{cells.get('datum')}")
                frm, to = cells.get("from"), cells.get("to")
                if not (frm and to and re.fullmatch(r"[A-Z]{3}", frm)
                        and re.fullmatch(r"[A-Z]{3}", to)):
                    raise ValueError(f"Route fehlt: {cells.get('datum')} "
                                     f"{cells.get('strecke')}")
                dep, arr = _clock_instants(year, month, day,
                                           cells.get("offon") or "")

                if kind == "dh":
                    deadheads.append({
                        "date": dep.date().isoformat(),
                        "flight": flight_match.group(0),
                        "duration_min": decimal_hours_minutes(cells["dh"]),
                    })
                    continue

                if frm == to:
                    raise ValueError(
                        f"TC-00-Zeile mit FROM==TO ({frm}) am "
                        f"{cells.get('datum')} — Semantik unbelegt, Abbruch")
                block_min = decimal_hours_minutes(cells["block"])
                anr_min = decimal_hours_minutes(cells["anr"])
                if abs(block_min - anr_min) > 1:
                    raise ValueError(
                        f"BLOCKZEIT {block_min} != ANR. FL-ZEIT {anr_min} am "
                        f"{cells.get('datum')} {flight_match.group(0)}")
                if not 0 < block_min < 1200:
                    raise ValueError(
                        f"Blockzeit außerhalb 0..20h: {cells.get('datum')} "
                        f"{flight_match.group(0)}")

                rest = (cells.get("rest") or "").split("/")
                raw_reg = rest[0].strip() if rest else ""
                actype = rest[1].strip() if len(rest) > 1 else ""
                if raw_reg == "" and actype == "000000":
                    actype = ""
                leg = {
                    "date": dep.date().isoformat(),
                    "flight": flight_match.group(0),
                    "from": frm,
                    "to": to,
                    "dep_iso": dep.isoformat().replace("+00:00", "Z"),
                    "arr_iso": arr.isoformat().replace("+00:00", "Z"),
                    "block_min": block_min,
                    "reg": normalized_registration(raw_reg),
                    "type": actype or None,
                    "role": role,
                    "remarks": (f"Condor-Flugstundenübersicht "
                                f"{month:02d}/{year}; BLOCK-Zeit, "
                                f"OFF/ON UTC"),
                }
                if cells.get("vl") == "L":
                    landing_marks += 1
                    night = is_civil_night(arr, to)
                    leg["ldg_day"] = 0 if night else 1
                    leg["ldg_night"] = 1 if night else 0
                legs.append({k: v for k, v in leg.items() if v is not None})

        block_total = sum(l["block_min"] for l in legs)
        deadhead_min = sum(d["duration_min"] for d in deadheads)
        errors = []
        if not control_minutes_match(block_total, summary["effective_min"]):
            errors.append(f"Effektiv {block_total} != PDF "
                          f"{summary['effective_min']} min")
        if landing_marks != summary["landings"]:
            errors.append(f"Landungen {landing_marks} != PDF "
                          f"{summary['landings']}")
        if errors:
            raise ValueError(f"{os.path.basename(path)}: " + "; ".join(errors))

        return legs, {
            "filename": os.path.basename(path),
            "month": f"{year:04d}-{month:02d}",
            "role": role,
            "legs": len(legs),
            "day_rows": day_rows,
            "deadheads_skipped": len(deadheads),
            "block_min": block_total,
            "deadhead_min": deadhead_min,
            "flight_landings": sum(
                l.get("ldg_day", 0) + l.get("ldg_night", 0) for l in legs),
            "control": "OK",
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("sources", nargs="+")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    legs, months = [], []
    for source in args.sources:
        parsed_legs, report = parse_pdf(source)
        legs.extend(parsed_legs)
        months.append(report)
        print(f"{report['month']}: {report['legs']} Legs / "
              f"{report['block_min']} min, {report['deadheads_skipped']} DH / "
              f"{report['deadhead_min']} min, "
              f"{report['flight_landings']} Landungen — Kontrolle OK")

    legs.sort(key=lambda leg: (leg["date"], leg.get("dep_iso") or ""))
    collisions = dedupe_keys(legs)
    payload = {
        "legs": legs,
        "sim": [],
        "report": {
            "parser": "parse_cfg_flugstunden.py",
            "months": months,
            "dedupe_suffixes": collisions,
            "totals": {
                "legs": len(legs),
                "block_min": sum(l["block_min"] for l in legs),
                "landings": sum(l.get("ldg_day", 0) + l.get("ldg_night", 0)
                                for l in legs),
            },
        },
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps(payload["report"]["totals"], ensure_ascii=False))
    if collisions:
        print(f"Leg-Key-Kollisionen verlustfrei nummeriert: {len(collisions)}")


if __name__ == "__main__":
    main()
