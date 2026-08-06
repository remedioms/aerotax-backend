#!/usr/bin/env python3
"""NetLine/Crew(CFG) „History (only notified states)" (CREWLINK-dhi-PDF)
→ AeroX-Flugbuch-JSON.

    python3 parse_netline_history.py <quelle.pdf> <ziel.json> --role FO

Verifiziert an Upload #236 (Jakob Wunderlich, Period 01Jan26–01Mar26,
41 Seiten, 15 Revisionen). Format-Wahrheiten:

* Das Dokument druckt pro NOTIFIKATION (Revision) den KOMPLETTEN Zeitraum
  als Tages-Blöcke mit zwei Spalten: links „old duties" (Stand VOR der
  Notifikation, mit Legenden-Marker `1`/`*` am Rand), rechts hinter `|`
  „new duties" (Stand danach). Die Revisionen stehen NEUESTE ZUERST; der
  Zeitstempel (`26Feb26-23:21`) steht als eigene Zeile unter den von der
  Revision berührten Tages-Headern. Finaler Plan = new-Spalte der ERSTEN
  Revision im Dokument — „letzter Auftritt gewinnt" wäre der Urplan!
* Leg-Zeile: `DE 4094 BER 1550 1700 FRA 320` — die strenge Regex VERLANGT
  den AC-Typ am Ende. Deadhead-Flüge (`DH/DE 4075 FRA 1315 1420 BER`)
  tragen KEINEN AC-Typ und sind keine Flugbuch-Legs; ebenso Bahn
  (`DH/ICE 525G QDU 0522 0639 QFA`), Bodenwege (`GT/…`), Standby/Kurse
  (`SB90/RE12/HS4/DGR/SEC/CRM/EM/OD2/CWX/MED/E_FDP`), `C/I`/`C/O`,
  Frei-Marker (`U/ORT/OFF/OFF_2`). Zeiten UTC (NetLine-Konvention, wie
  `parse_netline_idp.py`); arr <= dep ⇒ Folgetag.
* Optionales Tages-Suffix `/17` zwischen Flugnummer und Station wie beim
  IDP (Commit 544cf56) — wird gestrippt.

KONTROLLEN (die Quelle druckt KEINE Summen — deshalb strukturell, alle
harte Abbrüche):
1. Jede new-Zeile muss klassifizierbar sein (unbekannte Zeile = Abbruch;
   nichts fällt still raus). Hotel-Folgezeilen hängen nur direkt an
   `Hotel:`-Zeilen.
2. REVISIONSKETTE: old-Spalte von Revision k == new-Spalte der nächst-
   älteren Revision k+1 — für JEDEN Tag (normalisierte Dienst-Elemente,
   ohne Hotel-/Status-ID-Zeilen). Eine verschluckte Leg-Zeile reißt die
   Kette.
3. STATIONS-KETTE pro Tag: C/I-Station → Elemente (from==Kettenstand,
   Kette=to) → C/O-Station. Ein fehlendes Element bricht die Kette.
4. Revisions-Zeitstempel streng fallend; jede Revision deckt exakt den
   Period-Zeitraum ab; jeder Tag in jeder Revision genau einmal.

Landungen/PF/Kennzeichen stehen NICHT in der Quelle und werden NICHT
erfunden (Invariante 5); AC-Typ roh (`32Q`), Rolle kommt vom Operator
(`--role`, aus Flugstundenübersicht/Profil belegt).
"""

import argparse
import collections
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import pdfplumber

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from legkeys import dedupe_keys

RE_DAY_HDR = re.compile(r"^(\d{2}[A-Z][a-z]{2}\d{2})-{3,}$")
RE_REV_TS = re.compile(r"^(\d{2}[A-Z][a-z]{2}\d{2})-(\d{2}):(\d{2})$")
RE_PERIOD = re.compile(
    r"Period\s*:\s*(\d{2}[A-Z][a-z]{2}\d{2})\s*-\s*(\d{2}[A-Z][a-z]{2}\d{2})")
RE_LEG = re.compile(
    r"^([A-Z]{2}) ?(\d{1,4}[A-Z]?)(?: /\d{1,2})? "
    r"([A-Z]{3}) (\d{4}) (\d{4}) ([A-Z]{3}) ([A-Z0-9]{2,4})$")
RE_STATE_ID = re.compile(r"^[A-Z]\d{4,6}[A-Z]?/\d{2}[A-Z][a-z]{2}$")
RE_CI = re.compile(r"^C/I ([A-Z]{3}) (\d{4})$")
RE_CO = re.compile(r"^C/O (\d{4}) ([A-Z]{3})$")
RE_TRANSFER = re.compile(
    r"^(GT|DH)/(\S+) ?(\S+)? ([A-Z]{3}) (\d{4}) (\d{4}) ([A-Z]{3})$")
RE_GROUND = re.compile(r"^([A-Z][A-Z0-9_]{0,7}) ([A-Z]{3}) (\d{4})(?: (\d{4}))?$")
RE_FREE = re.compile(r"^(U|ORT|OFF|OFF_\d+) ([A-Z]{3})$")
RE_BARE_DAY = re.compile(r"^\d{2}[A-Z][a-z]{2}\d{2}$")
RE_FURNITURE = re.compile(
    r"^(NetLine/|printed by|\(\d{6}[A-Z]?/|old duties|new duties$"
    r"|\S{1,2} > |>{1,2} |>> |>>|Code\b|Crew Member|History \(|Period )")

VALID_ROLES = ("PIC", "PICUS", "SFO", "FO", "FB")

# Spaltengeometrie (kalibriert an Upload #236): Legenden-Marker (`1`/`*`)
# bei x≈50–60, old-Spalte ab x≈70, `|`-Trenner ≈ x 304, new-Spalte dahinter.
MARKER_X_MAX = 65
RE_MARKER = re.compile(r"^[0-9*+]{1,2}$")
PIPE_X_DEFAULT = 300


def classify(line):
    """Eine Spalten-Zeile → (kind, payload). Unbekannt → ('unknown', line)."""
    line = line.strip()
    if not line:
        return "empty", None
    if RE_FURNITURE.match(line):
        return "furniture", None
    if RE_BARE_DAY.fullmatch(line):
        return "bare_day", None
    if RE_STATE_ID.fullmatch(line):
        return "state_id", None
    if line.startswith("Hotel:"):
        return "hotel", None
    match = RE_LEG.fullmatch(line)
    if match:
        carrier, number, frm, dep, arr, to, actype = match.groups()
        return "leg", {"flight": carrier + number, "from": frm, "to": to,
                       "dep": dep, "arr": arr, "type": actype}
    match = RE_CI.fullmatch(line)
    if match:
        return "ci", {"station": match.group(1), "time": match.group(2)}
    match = RE_CO.fullmatch(line)
    if match:
        return "co", {"station": match.group(2), "time": match.group(1)}
    match = RE_TRANSFER.fullmatch(line)
    if match:
        return "transfer", {"mode": f"{match.group(1)}/{match.group(2)}",
                            "from": match.group(4), "to": match.group(7)}
    match = RE_FREE.fullmatch(line)
    if match:
        return "free", {"code": match.group(1), "station": match.group(2)}
    match = RE_GROUND.fullmatch(line)
    if match:
        return "ground", {"code": match.group(1), "station": match.group(2)}
    if line == "EAC":
        return "ground_bare", {"code": "EAC"}
    return "unknown", line


def normalized_elements(lines):
    """Zeilen einer Tages-Spalte → vergleichbare Dienst-Elemente.

    Hotel-/Status-ID-/Möblierungs-Zeilen fliegen raus (ändern sich zwischen
    Revisionen, ohne dass sich der Dienst ändert). Wirft bei unbekannten
    Zeilen, außer sie hängen direkt an einer Hotel-Zeile (Umbruch).
    """
    elements, after_hotel = [], False
    for line in lines:
        kind, payload = classify(line)
        if kind == "unknown":
            if after_hotel:
                continue
            raise ValueError(f"unbekannte Zeile: {line!r}")
        if kind == "hotel":
            after_hotel = True
            continue
        if kind in ("state_id", "furniture", "empty", "bare_day"):
            continue
        after_hotel = False
        elements.append((kind, tuple(sorted((payload or {}).items()))))
    return elements


def check_station_chain(day, lines):
    """Stations-Kontinuität der finalen Tages-Spalte (harter Abbruch)."""
    chain = None
    for line in lines:
        kind, payload = classify(line)
        if kind in ("hotel", "state_id", "furniture", "empty", "bare_day",
                    "unknown", "ground_bare"):
            continue
        if kind == "ci":
            chain = payload["station"]
        elif kind in ("leg", "transfer"):
            if chain is not None and payload["from"] != chain:
                raise ValueError(
                    f"{day}: Stations-Kette gerissen — {kind} startet "
                    f"{payload['from']}, erwartet {chain} "
                    f"(verlorene Zeile?)")
            chain = payload["to"]
        elif kind in ("ground", "free", "co"):
            if chain is not None and payload["station"] != chain:
                raise ValueError(
                    f"{day}: Stations-Kette gerissen — {kind} in "
                    f"{payload['station']}, erwartet {chain}")
            chain = payload["station"]


def split_row(words, pipe_x):
    """Wortliste einer Zeile → (old_text, new_text), Marker gestrippt."""
    old_words, new_words = [], []
    for word in sorted(words, key=lambda w: w["x0"]):
        if word["text"] == "|":
            continue
        if word["x0"] < MARKER_X_MAX and RE_MARKER.fullmatch(word["text"]):
            continue  # Legenden-Marker (1/2/*/+) der old-Spalte
        (old_words if word["x0"] < pipe_x else new_words).append(word["text"])
    return " ".join(old_words).strip(), " ".join(new_words).strip()


def extract_rows(path):
    """PDF → Zeilenstrom [(kind, payload)]: Tages-Header, Revisions-TS,
    (old, new)-Inhaltszeilen. Der `|`-Trenner liefert die Spaltengrenze."""
    rows = []
    with pdfplumber.open(path) as pdf:
        period = RE_PERIOD.search(pdf.pages[0].extract_text() or "")
        if not period:
            raise ValueError("kein 'Period :'-Kopf — ist das eine "
                             "NetLine-History?")
        pipe_xs = []
        for page in pdf.pages:
            for word in page.extract_words(x_tolerance=1, y_tolerance=2):
                if word["text"] == "|":
                    pipe_xs.append(word["x0"])
        pipe_x = (sorted(pipe_xs)[len(pipe_xs) // 2]
                  if pipe_xs else PIPE_X_DEFAULT)
        for page in pdf.pages:
            words = page.extract_words(x_tolerance=1, y_tolerance=2)
            lines = collections.defaultdict(list)
            for word in words:
                lines[round(word["top"] / 3)].append(word)
            for _, row_words in sorted(lines.items()):
                row_words.sort(key=lambda w: w["x0"])
                full = " ".join(w["text"] for w in row_words)
                hdr = RE_DAY_HDR.match(full.split(" ")[0]) if full else None
                if hdr:
                    rows.append(("day", hdr.group(1)))
                    continue
                if RE_REV_TS.fullmatch(full.strip()):
                    rows.append(("rev_ts", full.strip()))
                    continue
                old, new = split_row(row_words, pipe_x)
                if old or new:
                    rows.append(("content", (old, new)))
    return period.groups(), rows


def parse_netline_date(token):
    return datetime.strptime(token, "%d%b%y")


def build_passes(rows, period):
    """Zeilenstrom → Revisions-Pässe [{ts, days: {day: (old[], new[])}}].

    Pass-Grenze = Rückwärtssprung in der Tagesfolge. Jeder Pass muss den
    kompletten Period-Zeitraum exakt einmal abdecken und genau EINEN
    Revisions-Zeitstempel tragen.
    """
    start, end = (parse_netline_date(t) for t in period)
    expected_days = []
    cursor = start
    while cursor <= end:
        expected_days.append(cursor.strftime("%d%b%y"))
        cursor += timedelta(days=1)

    passes, current, current_day = [], None, None
    for kind, payload in rows:
        if kind == "day":
            if (current is None
                    or (current_day is not None
                        and parse_netline_date(payload)
                        < parse_netline_date(current_day))):
                current = {"ts": None, "days": collections.OrderedDict()}
                passes.append(current)
            if payload in current["days"]:
                raise ValueError(
                    f"Tag {payload} doppelt in einer Revision — "
                    f"Pass-Grenze nicht erkannt")
            current["days"][payload] = ([], [])
            current_day = payload
        elif kind == "rev_ts":
            if current is None:
                raise ValueError("Revisions-Zeitstempel vor erstem Tag")
            ts = datetime.strptime(payload, "%d%b%y-%H:%M")
            if current["ts"] is not None and current["ts"] != ts:
                raise ValueError(
                    f"Revision trägt zwei Zeitstempel: {current['ts']} "
                    f"vs {ts}")
            current["ts"] = ts
        elif kind == "content" and current is not None and current_day:
            old, new = payload
            if old:
                current["days"][current_day][0].append(old)
            if new:
                current["days"][current_day][1].append(new)

    for index, pass_ in enumerate(passes):
        if list(pass_["days"].keys()) != expected_days:
            raise ValueError(
                f"Revision {index + 1} deckt den Zeitraum nicht exakt ab "
                f"({len(pass_['days'])} von {len(expected_days)} Tagen)")
        if pass_["ts"] is None:
            raise ValueError(f"Revision {index + 1} ohne Zeitstempel")
    ts_list = [p["ts"] for p in passes]
    if ts_list != sorted(ts_list, reverse=True):
        raise ValueError(
            "Revisions-Zeitstempel nicht streng fallend — Annahme "
            "'neueste zuerst' verletzt, Abbruch")
    return passes


def check_revision_chain(passes):
    """old(Revision k) muss new(Revision k+1) entsprechen — pro Tag.

    Ausnahme Ur-Revision (letzte im Dokument): ihre old-Spalte ist leer.
    """
    for k in range(len(passes) - 1):
        newer, older = passes[k], passes[k + 1]
        for day in newer["days"]:
            old_side = normalized_elements(newer["days"][day][0])
            prev_new = normalized_elements(older["days"][day][1])
            if old_side != prev_new:
                raise ValueError(
                    f"Revisionskette gerissen an {day}: old-Spalte von "
                    f"{newer['ts']:%d%b%y-%H:%M} != new-Spalte von "
                    f"{older['ts']:%d%b%y-%H:%M} (verlorene Zeile?)")


def final_legs(passes, role):
    """new-Spalte der neuesten Revision → normalisierte Flugbuch-Legs."""
    newest = passes[0]
    legs, counts = [], collections.Counter()
    for day, (_, new_lines) in newest["days"].items():
        normalized_elements(new_lines)      # Vokabular-Wache (wirft)
        check_station_chain(day, new_lines)
        day_date = parse_netline_date(day)
        for line in new_lines:
            kind, payload = classify(line)
            counts[kind] += 1
            if kind != "leg":
                continue
            dep = day_date.replace(
                hour=int(payload["dep"][:2]), minute=int(payload["dep"][2:]),
                tzinfo=timezone.utc)
            arr = day_date.replace(
                hour=int(payload["arr"][:2]), minute=int(payload["arr"][2:]),
                tzinfo=timezone.utc)
            if arr <= dep:
                arr += timedelta(days=1)
            block_min = int((arr - dep).total_seconds() // 60)
            if not 0 < block_min < 1200:
                raise ValueError(f"{day} {payload['flight']}: Blockzeit "
                                 f"{block_min} min außerhalb 0..20h")
            legs.append({
                "date": dep.date().isoformat(),
                "flight": payload["flight"],
                "from": payload["from"],
                "to": payload["to"],
                "dep_iso": dep.isoformat().replace("+00:00", "Z"),
                "arr_iso": arr.isoformat().replace("+00:00", "Z"),
                "block_min": block_min,
                "type": payload["type"],
                "role": role,
                "remarks": (f"aus NetLine-Duty-History; letzter "
                            f"notifizierter Stand "
                            f"{newest['ts']:%d%b%y %H:%M}, geplante "
                            f"Zeiten, UTC"),
            })
    return legs, counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("out")
    parser.add_argument("--role", required=True, choices=VALID_ROLES,
                        help="Rolle aus unabhängiger Quelle (Flugstunden-"
                             "übersicht/Profil) — steht nicht im Dokument")
    args = parser.parse_args()

    period, rows = extract_rows(args.source)
    passes = build_passes(rows, period)
    check_revision_chain(passes)
    legs, counts = final_legs(passes, args.role)

    legs.sort(key=lambda leg: (leg["date"], leg["dep_iso"]))
    collisions = dedupe_keys(legs)
    block_total = sum(l["block_min"] for l in legs)
    payload = {
        "legs": legs,
        "sim": [],
        "report": {
            "parser": "parse_netline_history.py",
            "period": f"{period[0]} - {period[1]}",
            "revisions": [f"{p['ts']:%d%b%y-%H:%M}" for p in passes],
            "final_line_kinds": dict(counts),
            "dedupe_suffixes": collisions,
            "totals": {
                "legs": len(legs),
                "block_min": block_total,
                "landings": 0,
            },
        },
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(f"Period {period[0]}–{period[1]}: {len(passes)} Revisionen "
          f"(Kette OK), {len(legs)} finale Legs / {block_total} min "
          f"({block_total // 60}:{block_total % 60:02d}), "
          f"{counts['transfer']} Transfers, 0 Landungen (Quelle trägt "
          f"keine)")
    if collisions:
        print(f"Leg-Key-Kollisionen verlustfrei nummeriert: {len(collisions)}")


if __name__ == "__main__":
    main()
