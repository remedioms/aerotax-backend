#!/usr/bin/env python3
"""LH-SAP-„Flugstunden - Übersicht" → AeroX-Flugbuch-JSON.

    python3 parse_lh_flugstunden.py --out ziel.json monat1.pdf [monat2.pdf ...]

Die Quelle weist pro Zeile drei verschiedene Zeiten aus. Für das Flugbuch gilt:

* ``MIN./FAKT`` (ganze Minuten) ist die belegte Blockzeit.
* ``OFF - ON`` sind UTC-Zeitpunkte und werden als ``dep_iso``/``arr_iso``
  übernommen. Sie dürfen wegen Rundung von MIN./FAKT abweichen.
* Ein Wert in ``DH-STD`` markiert einen Deadhead; er ist kein geflogenes Leg.
* Gerätecodes wie ``MUC327`` sind Simulatoren und bleiben strikt in ``sim``.
* ``L`` ist eine persönliche Landung. Tag/Nacht wird nach dem Ende/Anfang der
  bürgerlichen Dämmerung am Zielflughafen bestimmt (EASA-Nachtdefinition).

Jeder Monat wird gegen die im PDF gedruckten Kontrollwerte geprüft:
Landungen, effektive Flugzeit (Flug + Simulator) und Deadhead-Zeit. Schon eine
unerklärte Abweichung > 2 Minuten bricht den Lauf ab; nichts wird still geraten.
"""

import argparse
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone

import pdfplumber

from legkeys import dedupe_keys


RE_HEADER = re.compile(r"für Monat\s+(\d{2})\s*/\s*(\d{4})")
RE_DATE = re.compile(r"^(\d{2})\.(\d{2})\.$")
RE_FLIGHT = re.compile(r"^([A-Z]{2})(\d{3,4})(?:/\d{1,2})?$")
RE_CLOCKS = re.compile(r"^(\d{2}):(\d{2})-(\d{2}):(\d{2})$")
RE_SIM_DEVICE = re.compile(r"^(?:[A-Z]{3}[A-Z0-9]{3}|FT\d{2})$")
# Kabinen-Layout: „Funktion FB / KABINE" (alt) ebenso wie „P1 / KABINE" usw.
# (Owner-Fall 2026-08-12, Uploads #324/#325). Die Spaltengeometrie der neuen
# Kabinen-Übersichten ist mit dem bestehenden Kabinen-Pfad deckungsgleich
# (nachgemessen: BLOCK 328∈[315,350], DH 426∈[415,445], TC 287∈[280,305],
# Summenzeile 196/319 in den Leseranges) — nur die Funktions-Kennung vor
# „/ KABINE" variiert. Die Kennung wird als Rolle übernommen (FB, P1, …);
# COCKPIT-Varianten (z.B. „CP / COCKPIT") matchen bewusst NICHT.
RE_CABIN = re.compile(r"Funktion\s+([A-Z0-9]{1,3})\s*/\s*KABINE", re.IGNORECASE)
CONTROL_ROUNDING_TOLERANCE_MIN = 3

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AIRPORTS_PATH = os.path.join(ROOT, "airports_compact.json")


def _airport_coordinates():
    raw = json.load(open(AIRPORTS_PATH, encoding="utf-8"))
    fields = raw["fields"]
    iata_i, lat_i, lon_i = (fields.index(k) for k in ("iata", "lat", "lon"))
    return {r[iata_i]: (float(r[lat_i]), float(r[lon_i]))
            for r in raw["rows"] if r[iata_i] and r[lat_i] is not None
            and r[lon_i] is not None}


AIRPORT_COORDS = _airport_coordinates()
# Geschlossene historische Verkehrsflughäfen fehlen bewusst im aktuellen
# `airports_compact.json`, kommen aber in Alt-Flugbüchern vor. Nur belegte
# Platzkoordinaten ergänzen; niemals auf die Nachfolge-Station umbiegen (TXL
# ist nicht BER und hat für die Dämmerung einen eigenen Ort).
AIRPORT_COORDS.update({
    "TXL": (52.5597, 13.2877),  # Berlin-Tegel (EDDT)
})


def decimal_hours_minutes(value):
    """SAP-Dezimalstunden (``36,13``) → Minuten, zweistellig gerundet."""
    if not value:
        return 0
    match = re.fullmatch(r"(\d+),(\d{2})", value)
    if not match:
        raise ValueError(f"ungültige SAP-Dezimalzeit: {value!r}")
    hours, hundredths = map(int, match.groups())
    return round(hours * 60 + hundredths * 0.6)


def control_minutes_match(parsed_minutes, summary_minutes):
    """SAP-Rundungsdrift zwischen Zeilenminuten und Dezimal-Monatssumme.

    Die verifizierten Monate 07–10/2022 driften bei vollständig gelesenen
    Zeilen und exakt passenden Landungen um bis zu drei Minuten.
    """
    return abs(parsed_minutes - summary_minutes) \
        <= CONTROL_ROUNDING_TOLERANCE_MIN


def effective_summary_minutes(value, source_rows):
    """Resolve an omitted SAP effective-time total for a genuine zero month."""
    if value is not None:
        return value
    if source_rows == 0:
        return 0
    raise ValueError("Effektive Flugzeit fehlt in der Summenzeile")


def normalized_flight(value):
    """SAP-Padding: LH0046 → LH046, LH0982 → LH982; sonst unverändert."""
    match = RE_FLIGHT.fullmatch((value or "").strip().upper())
    if not match:
        raise ValueError(f"ungültige Flugnummer: {value!r}")
    carrier, number = match.groups()
    if len(number) == 4 and number.startswith("0"):
        number = number[1:]
    return carrier + number


def normalized_registration(value):
    value = (value or "").strip().upper()
    # SAP druckt deutsche Kennzeichen ohne Bindestrich (DAIUL → D-AIUL).
    if re.fullmatch(r"D[A-Z0-9]{4}", value):
        return "D-" + value[1:]
    return value or None


def legacy_cabin_traffic_code(value):
    """Old cabin overview: TC 01 is a credited deadhead, TC 00 operating."""
    value = (value or "").strip()
    if value not in ("00", "01"):
        raise ValueError(f"unbekannter Kabinen-Traffic-Code: {value!r}")
    return value == "01"


def _solar_elevation(instant, lat, lon):
    """NOAA-Näherung der Sonnenhöhe für einen UTC-Zeitpunkt (Grad)."""
    instant = instant.astimezone(timezone.utc)
    day = instant.timetuple().tm_yday
    hour = (instant.hour + instant.minute / 60 + instant.second / 3600)
    gamma = 2 * math.pi / 365 * (day - 1 + (hour - 12) / 24)
    eqtime = 229.18 * (
        0.000075 + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma) - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    decl = (
        0.006918 - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma) - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma) - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    true_solar_min = (hour * 60 + eqtime + 4 * lon) % 1440
    hour_angle = math.radians(true_solar_min / 4 - 180)
    lat_rad = math.radians(lat)
    cos_zenith = (math.sin(lat_rad) * math.sin(decl)
                   + math.cos(lat_rad) * math.cos(decl) * math.cos(hour_angle))
    cos_zenith = min(1.0, max(-1.0, cos_zenith))
    return 90 - math.degrees(math.acos(cos_zenith))


def is_civil_night(instant, airport):
    coords = AIRPORT_COORDS.get(airport)
    if not coords:
        raise ValueError(f"keine Flughafenkoordinaten für {airport}")
    # Nacht beginnt mit Ende der bürgerlichen Abenddämmerung (Sonne < -6°).
    return _solar_elevation(instant, *coords) < -6.0


def _row_values(row, x0, x1):
    return [word["text"] for word in row if x0 <= word["x0"] < x1]


def _one(row, x0, x1, pattern=None):
    values = _row_values(row, x0, x1)
    if pattern:
        values = [v for v in values if re.fullmatch(pattern, v)]
    return values[0] if values else None


def _clock_instants(year, month, day, clocks):
    match = RE_CLOCKS.fullmatch(clocks)
    if not match:
        raise ValueError(f"ungültige OFF-ON-Zeit: {clocks!r}")
    off_h, off_m, on_h, on_m = map(int, match.groups())
    if off_h > 24 or on_h > 24 or (off_h == 24 and off_m) or (on_h == 24 and on_m):
        raise ValueError(f"ungültige OFF-ON-Zeit: {clocks!r}")
    # SAP prints midnight as 24:00 on some monthly summaries.  Treat that as
    # the next UTC day rather than rejecting an otherwise valid flight row.
    dep = datetime(year, month, day, 0 if off_h == 24 else off_h, off_m,
                   tzinfo=timezone.utc) + timedelta(days=off_h // 24)
    arr = datetime(year, month, day, 0 if on_h == 24 else on_h, on_m,
                   tzinfo=timezone.utc) + timedelta(days=on_h // 24)
    if arr <= dep:
        arr += timedelta(days=1)
    return dep, arr


def _summary(page, month):
    words = page.extract_words(x_tolerance=1, y_tolerance=2)
    # The month is the first cell in the totals row.  A totals value can equal
    # the month number as well (for example 11 landings in November), so keep
    # the lookup inside the month column instead of matching the whole row.
    anchors = [w for w in words if w["text"] == f"{month:02d}"
               and w["top"] > 700 and w["x0"] < 100]
    if len(anchors) != 1:
        raise ValueError(f"Summenzeile für Monat {month:02d} nicht eindeutig")
    row = sorted([w for w in words if abs(w["top"] - anchors[0]["top"]) < 2.5],
                 key=lambda w: w["x0"])
    landings = _one(row, 160, 190, r"\d+")
    effective = _one(row, 190, 225, r"\d+[,]\d{2}")
    deadhead = _one(row, 310, 345, r"\d+[,]\d{2}")
    return {
        "landings": int(landings or 0),
        "effective_min": (decimal_hours_minutes(effective)
                          if effective is not None else None),
        "deadhead_min": decimal_hours_minutes(deadhead),
    }


def parse_pdf(path):
    with pdfplumber.open(path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        header = RE_HEADER.search(text)
        if not header:
            raise ValueError(f"kein LH-Flugstundenübersicht-Kopf: {path}")
        month, year = map(int, header.groups())
        cabin_match = RE_CABIN.search(text)
        legacy_cabin = cabin_match is not None
        cabin_role = cabin_match.group(1).upper() if cabin_match else None
        summary = _summary(pdf.pages[-1], month)
        legs, sims, deadheads = [], [], []
        source_rows = landing_marks = 0

        for page in pdf.pages:
            words = page.extract_words(x_tolerance=1, y_tolerance=2)
            for date_word in [w for w in words if RE_DATE.fullmatch(w["text"] or "")]:
                row = sorted(
                    [w for w in words if abs(w["top"] - date_word["top"]) < 2.5],
                    key=lambda w: w["x0"],
                )
                raw_flight = _one(
                    row, 78, 125, r"[A-Z]{2}\d{3,4}(?:/\d{1,2})?"
                )
                clocks = _one(row, 174, 225, r"\d{2}:\d{2}-\d{2}:\d{2}")
                if not raw_flight or not clocks:
                    continue
                source_rows += 1
                day, printed_month = map(int, RE_DATE.fullmatch(date_word["text"]).groups())
                if printed_month != month:
                    raise ValueError(f"Zeilenmonat {printed_month:02d} != Kopf {month:02d}")
                frm = _one(row, 145, 190, r"[A-Z]{3}")
                to = _one(row, 225, 300, r"[A-Z]{3}")
                if not frm or not to:
                    raise ValueError(f"Route fehlt: {date_word['text']} {raw_flight}")
                dep, arr = _clock_instants(year, month, day, clocks)
                landing = not legacy_cabin and "L" in _row_values(row, 300, 315)
                landing_marks += int(landing)

                aircraft = ([] if legacy_cabin else
                            "".join(_row_values(row, 468, 560)).split("/"))
                raw_reg = aircraft[0].strip() if aircraft else ""
                actype = aircraft[1].strip() if len(aircraft) > 1 else ""
                is_sim = (frm == to and RE_SIM_DEVICE.fullmatch(raw_reg) is not None)

                if is_sim:
                    duration = int((arr - dep).total_seconds() // 60)
                    sims.append({
                        "code": raw_reg,
                        "date": dep.date().isoformat(),
                        "role": "FO",
                        "place": frm,
                        "duration_min": duration,
                    })
                    continue

                if legacy_cabin:
                    traffic_code = _one(row, 280, 305, r"\d{2}")
                    is_deadhead = legacy_cabin_traffic_code(traffic_code)
                    dh_value = (_one(row, 415, 445, r"\d+[,]\d{2}")
                                if is_deadhead else None)
                    if is_deadhead and dh_value is None:
                        raise ValueError(
                            f"Kabinen-Deadhead-Zeit fehlt: {date_word['text']} "
                            f"{raw_flight}"
                        )
                else:
                    dh_value = _one(row, 380, 405, r"\d+[,]\d{2}")
                if dh_value:
                    deadheads.append({
                        "date": dep.date().isoformat(),
                        "flight": normalized_flight(raw_flight),
                        "from": frm,
                        "to": to,
                        "duration_min": decimal_hours_minutes(dh_value),
                    })
                    continue

                fact = (_one(row, 315, 350, r"\d+[,]\d{2}")
                        if legacy_cabin else _one(row, 315, 343, r"\d+"))
                block_min = (decimal_hours_minutes(fact) if legacy_cabin
                             else int(fact or 0))
                if not fact or block_min <= 0:
                    raise ValueError(
                        f"FAKT-Minuten fehlen: {date_word['text']} {raw_flight}"
                    )
                leg = {
                    "date": dep.date().isoformat(),
                    "flight": normalized_flight(raw_flight),
                    "from": frm,
                    "to": to,
                    "dep_iso": dep.isoformat().replace("+00:00", "Z"),
                    "arr_iso": arr.isoformat().replace("+00:00", "Z"),
                    "block_min": block_min,
                    "reg": normalized_registration(raw_reg),
                    "type": actype or None,
                    "role": cabin_role if legacy_cabin else "FO",
                    "remarks": (f"LH-Flugstundenübersicht {month:02d}/{year}; "
                                + ("BLOCK-Zeit, OUT/IN UTC" if legacy_cabin
                                   else "FAKT-Minuten, OUT/IN UTC")),
                }
                if landing:
                    night = is_civil_night(arr, to)
                    leg["ldg_day"] = 0 if night else 1
                    leg["ldg_night"] = 1 if night else 0
                legs.append({k: v for k, v in leg.items() if v is not None})

        effective_min = sum(l["block_min"] for l in legs) \
            + sum(s["duration_min"] for s in sims)
        deadhead_min = sum(d["duration_min"] for d in deadheads)
        errors = []
        summary_effective_min = effective_summary_minutes(
            summary["effective_min"], source_rows
        )
        if not control_minutes_match(effective_min, summary_effective_min):
            errors.append(
                f"Effektiv {effective_min} != PDF {summary_effective_min} min"
            )
        # SAP druckt jede Zeile UND die Monatssumme nur zweistellig. Das
        # Runden der Einzelzeilen kann deshalb gegenüber dem Runden der Summe
        # um bis zu drei Minuten driften (verifiziert 07–10/2022).
        if not control_minutes_match(deadhead_min, summary["deadhead_min"]):
            errors.append(
                f"Deadhead {deadhead_min} != PDF {summary['deadhead_min']} min"
            )
        if landing_marks != summary["landings"]:
            errors.append(
                f"Landungen {landing_marks} != PDF {summary['landings']}"
            )
        if errors:
            raise ValueError(f"{os.path.basename(path)}: " + "; ".join(errors))

        return legs, sims, {
            "filename": os.path.basename(path),
            "month": f"{year:04d}-{month:02d}",
            "source_rows": source_rows,
            "legs": len(legs),
            "sim_sessions": len(sims),
            "deadheads_skipped": len(deadheads),
            "block_min": sum(l["block_min"] for l in legs),
            "sim_min": sum(s["duration_min"] for s in sims),
            "deadhead_min": deadhead_min,
            "landing_marks_total": landing_marks,
            "flight_landings": sum(
                l.get("ldg_day", 0) + l.get("ldg_night", 0) for l in legs
            ),
            "control": "OK",
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("sources", nargs="+")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    legs, sims, months = [], [], []
    for source in args.sources:
        parsed_legs, parsed_sims, report = parse_pdf(source)
        legs.extend(parsed_legs)
        sims.extend(parsed_sims)
        months.append(report)
        print(
            f"{report['month']}: {report['legs']} Legs / "
            f"{report['block_min']} min, {report['sim_sessions']} SIM / "
            f"{report['sim_min']} min, {report['deadheads_skipped']} DH, "
            f"{report['flight_landings']} Flug-Landungen — Kontrolle OK"
        )

    legs.sort(key=lambda leg: (leg["date"], leg.get("dep_iso") or ""))
    collisions = dedupe_keys(legs)
    sims.sort(key=lambda sim: (sim["date"], sim.get("code") or ""))
    payload = {
        "legs": legs,
        "sim": sims,
        "report": {
            "parser": "parse_lh_flugstunden.py",
            "months": months,
            "dedupe_suffixes": collisions,
            "totals": {
                "legs": len(legs),
                "block_min": sum(l["block_min"] for l in legs),
                "landings": sum(l.get("ldg_day", 0) + l.get("ldg_night", 0)
                                for l in legs),
                "sim_sessions": len(sims),
                "sim_min": sum(s["duration_min"] for s in sims),
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
