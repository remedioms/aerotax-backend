#!/usr/bin/env python3
"""OffBlock-„Duties"-CSV → normalisierte AeroX-Import-Legs (Format 2d).

  python3 parse_duties_v8.py <quelle.csv> <ziel.json> [--cutoff YYYY-MM-DD]
                             [--keep-planned] [--keep-sim-as-flight]
                             [--dup-window MIN] [--keep-duplicates]

NEU in v8 (Upload #23, Miguel Martins Goncalves, LH Kabine) — DOPPELT
VERBUCHTE FLÜGE:
Sein Export enthält denselben Flug mehrfach, teils byte-gleich (8×
LH1436 FRA→LED am 19.03.16), teils als zweite Variante mit den
GEPLANTEN statt den geflogenen Zeiten (12:20–15:00 / 02:40 neben
12:22–15:10 / 02:48). v7 warf nur die exakten Dubletten weg; die
Plan-Variante überlebte, bekam vom Kollisions-Schutz das Suffix „(2)"
und wäre als eigener Leg mit eigener Blockzeit im Flugbuch gelandet
(5 Phantom-Legs, +9:15 erfundene Blockzeit — Invariante 5).
Deshalb jetzt:

1. **Cluster-Merge statt First-Wins-Dedupe**: Legs mit gleichem
   `date|flight|from|to`, deren Abflugzeiten höchstens `--dup-window`
   Minuten (Default 15) auseinanderliegen, sind DERSELBE Flug. Gewinner
   der Gruppe ist die Variante mit den meisten Quellzeilen (Tiebreak:
   Kennzeichen vorhanden, dann Muster, dann früheste Zeit) — bei
   OffBlock ist das die Ist-Variante. Fehlende Felder (reg/type/pic_name/
   remarks/Landungen/Nachtzeit/pf) werden aus den anderen Zeilen der
   Gruppe NACHGEFÜLLT: das First-Wins-Dedupe von v5–v7 verlor sonst
   z.B. den PIC-Namen, weil die erste von acht identischen Zeilen ihn
   nicht trug. Nichts wird still verworfen — jede zusammengelegte
   Gruppe steht im Report. `--keep-duplicates` schaltet es ab (v7-Verhalten).
   Echte Wiederholungen derselben Strecke am selben Tag (Stunden
   auseinander) sind davon NICHT betroffen und bekommen weiter das
   Kollisions-Suffix `(2)` aus `legkeys.dedupe_keys`.
2. **Kabinen-Ränge**: `Purser 1/2/3` → `PU1/PU2/PU3`, `Purser` → `PU`
   (v7 hätte den Rohwert „Purser 1" als Rolle gespeichert). `role` ist
   im Backend reine Durchreiche (kein Konsument schaltet darauf).
3. **Ehrliche Kontrollsumme**: die Quellsumme zählt jetzt auch die
   Minuten mit, die durch Merge/Dedupe wegfallen, und weist sie
   getrennt aus: `Quelle − zusammengelegt == geparst` muss aufgehen.
   In v7 stand bei Doppelzeilen zwangsläufig „ABWEICHUNG", ohne dass man
   sah, ob Legs verloren gingen.

NEU in v7 (Upload #22, Sander Heutink, Lufthansa Cargo) — ENGLISCHE EXPORTE:
OffBlock schreibt die Werte der Spalte `Type` in der Sprache des Accounts. Die
SPALTENNAMEN sind immer englisch (deshalb fiel es nie auf), die WERTE nicht:
bisher kamen nur deutsche Exporte („Flug"), Sanders Datei sagt „Flight".
v6 verwarf damit ALLE 2808 Flüge still als „Summenzeile" und lieferte 0 Legs
bei 121 Sims — ein Import, der ohne die Kontrollsumme unbemerkt leer geblieben
wäre. `TYPE_FLIGHT`/`TYPE_SIM` sammeln jetzt beide Sprachen; deutsche
Alt-Exporte laufen unverändert.

Ableitung von parse_duties_v5.py (rückwärtskompatibel), gebaut für Upload #21
(Martin Dilly, Discover/4Y, ATPL-Ausbildung + A320-Typerating + Linienflug).
NEU gegenüber v5 — alles generisch, greift bei Alt-Exporten nicht:

1. **Rollen** `Pilot in Command Under Supervision` → PICUS,
   `Flugschüler` → FS (Token FS steckt schon im Store, Christos-Import).
2. **Trainings-Codes in der Flugnummern-Spalte bleiben lesbar**
   (`MCC APS MISSION 3`, `C4_52 P-Check`, `D4_32/33`, `LBA Skilltest`).
   v5 hat blind Whitespace gefressen und ab `/` abgeschnitten — das ist NUR
   für echte Flugnummern richtig (Tages-Suffix „DE 2199 /14").
3. **SIM-als-Flug** (Regel schon in v4, in v5 verloren): Zeilen mit
   Type=Flug und EASA-FSTD-Gerätenummer werden als Simulator behandelt, wenn
   sie keine echte Flugnummer+Strecke tragen oder Start=Ziel ist. Die Nummer
   allein reicht nicht: ein verifizierter OffBlock/FCL-Doppel-Export enthielt
   acht echte Langstrecken-Legs mit realer Flugnummer und Route, aber einem
   FSTD-Code als fehlbelegtem Kennzeichen. `--keep-sim-as-flight` schaltet
   die Erkennung ab.
4. **Noch nicht geflogene (geplante) Zeilen** werden ab `--cutoff`
   (Default: heute) übersprungen. OffBlock zieht den kommenden Dienstplan
   als Vorbelegung in den Export: keine Klassenzeit (pic/single/multi/dual
   alle 00:00), kein Kennzeichen, keine Landungen/Starts, PF=Nein, kein
   PIC-Name — und Rundzeiten (02:05) statt Ist-Zeiten (02:04). Solche Legs
   wären erfundene Blockzeit im Flugbuch. Zeilen VOR dem Cutoff werden
   NIE verworfen (nur gewarnt), damit ältere Exporte unverändert laufen.

Regeln aus ~/Desktop/AeroX-Feature-Docs/Flugbuch.md Abschnitt 2d.
"""
import csv
import json
import re
import sys
from datetime import date, datetime, timedelta

ROLES = {
    'pilot in command': 'PIC',
    'pilot in command under supervision': 'PICUS',
    'picus': 'PICUS',
    'second in command': 'SIC',
    'first officer': 'FO',
    'senior first officer': 'SFO',
    'copilot': 'FO',
    'co-pilot': 'FO',
    'flight instructor': 'FI',
    'instructor': 'FI',
    'student': 'DUAL',
    'dual': 'DUAL',
    'flugschüler': 'FS',
    'flugschueler': 'FS',
    'flugschülerin': 'FS',
    'flight attendant': 'FB',
    'flugbegleiter': 'FB',
    'flugbegleiterin': 'FB',
    'cabin': 'FB',
    'purser': 'PU',
}
# „Purser 1"/„Purser 2" (LH-Kabinenrang, im Export auch als „P1"/„P2" in der
# Crew-Liste) → PU1/PU2; die Rangziffer bleibt erhalten.
RE_PURSER = re.compile(r'^purser\s*(\d)$')
# echte Flugnummer: 2-3 Zeichen Präfix + 1-4 Ziffern (+ optionaler Buchstabe)
RE_FLIGHT = re.compile(r'^[A-Z0-9]{2,3}\s?\d{1,4}[A-Z]?$')
# EASA-FSTD-Gerätenummer statt Kennzeichen („DE-1A-040", „DE-2B-007")
RE_FSTD = re.compile(r'^[A-Z]{2}-\d[A-Z]-\d{1,4}$')
# Zeitspalten, die belegen, dass der Flug WIRKLICH stattgefunden hat
TIME_CLASS_COLS = ('pic_time', 'Single pilot', 'Multi pilot', 'Dual')

# Werte der Spalte `Type` — OffBlock lokalisiert sie nach Account-Sprache.
# Kleinschreibung beim Vergleich, damit Gross-/Kleinschreibung egal ist.
TYPE_FLIGHT = {'flug', 'flight'}
TYPE_SIM = {'simulator'}

# Minimale Signatur des OffBlock-Duties-Exports. Der Upload-Watchdog benutzt
# sie für echtes Content-Routing; eine bloße .csv-Endung reicht nicht, weil
# der öffentliche Import bewusst auch Exporte anderer Logbuch-Apps annimmt.
REQUIRED_HEADERS = {
    'Type', 'Date', 'Function', 'Departure place', 'Departure time',
    'Arrival place', 'Arrival time', 'Total time', 'Flight number',
    'Aircraft registration', 'Aircraft ICAO',
}

# OffBlock's dedicated Logbook export (observed in upload #616) contains the
# same facts as the established Duties export, but uses shorter/title-cased
# column names.  Keep the content router strict by accepting aliases only for
# known OffBlock fields, then canonicalize them before applying the existing
# validation and control sums.
COLUMN_ALIASES = {
    'Departure place': ('Departure',),
    'Departure time': ('Departure Time',),
    'Arrival place': ('Arrival',),
    'Arrival time': ('Arrival Time',),
    'Total time': ('Total Time',),
    'Flight number': ('Flight Number',),
    'Aircraft registration': ('Registration',),
    'Pilot flying': ('Pilot Flying',),
    'Landing day (count)': ('Landing Day',),
    'Landing night (count)': ('Landing Night',),
    'PIC': ('PIC Name',),
    'SIC': ('SIC Name',),
    'SFO': ('SFO Name',),
    'FO': ('FO Name',),
}

# Felder, die beim Cluster-Merge aus schwächeren Zeilen nachgefüllt werden
# dürfen (Zeiten/Blockzeit NICHT — die kommen ausschließlich vom Gewinner).
FILL_FIELDS = ('reg', 'type', 'pic_name', 'remarks', 'night_min',
               'to_day', 'to_night', 'ldg_day', 'ldg_night', 'pf', 'role')


def s(row, key):
    """Feld sicher als getrimmter String (CSV liefert None bei Kurzzeilen)."""
    return (row.get(key) or '').strip()


def _column_names(canonical):
    return (canonical,) + COLUMN_ALIASES.get(canonical, ())


def _canonical_row(row, number):
    """Map the two proven OffBlock header variants onto the Duties schema.

    If an export ever contains both spellings, accepting contradictory values
    would make the result depend on alias order.  Stop for review instead.
    """
    canonical = dict(row)
    for target, aliases in COLUMN_ALIASES.items():
        values = [(name, s(row, name)) for name in (target,) + aliases
                  if s(row, name)]
        distinct = {value.casefold() for _, value in values}
        if len(distinct) > 1:
            names = ', '.join(name for name, _ in values)
            raise ValueError(
                f'OffBlock-Zeile {number}: widersprüchliche Spalten {names}')
        if values:
            canonical[target] = values[0][1]
    return canonical


def fstd_row_is_sim(registration, departure, arrival, flight_number):
    """True only when an FSTD registration is corroborated as a session.

    OffBlock can put an FSTD device code into the registration field of an
    otherwise independently confirmed operating flight. A real airline
    flight number plus a non-local route wins over that one bad field.
    """
    registration = re.sub(r'\s+', '', (registration or '').upper())
    departure = (departure or '').strip().upper()
    arrival = (arrival or '').strip().upper()
    flight_number = norm_flight(flight_number)
    if not RE_FSTD.fullmatch(registration):
        return False
    has_operating_evidence = (
        departure and arrival and departure != arrival
        and RE_FLIGHT.fullmatch(flight_number or '') is not None
    )
    return not has_operating_evidence


def hhmm_to_min(txt):
    txt = (txt or '').strip()
    if not txt or ':' not in txt:
        return None
    try:
        h, m = txt.split(':')[:2]
        return int(h) * 60 + int(m)
    except ValueError:
        return None


def parse_date(txt):
    """DD.MM.YY → date (Pivot: <70 ⇒ 20YY)."""
    txt = (txt or '').strip()
    m = re.match(r'^(\d{1,2})\.(\d{1,2})\.(\d{2,4})$', txt)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000 if y < 70 else 1900
    try:
        return datetime(y, mo, d)
    except ValueError:
        return None


def norm_role(txt):
    t = (txt or '').strip()
    if not t:
        return None
    low = t.lower()
    m = RE_PURSER.match(low)
    if m:
        return f'PU{m.group(1)}'
    return ROLES.get(low, t)


def is_real_flight_number(txt):
    """Sieht der Wert wie eine echte Flugnummer aus (dann v5-Normalisierung)?
    „4Y536"/„DE 2199 /14"/„LH8264-1" ja — „D1_7"/„MCC APS MISSION 3" nein."""
    t = re.sub(r'\s+', '', (txt or '').strip().upper().split('/')[0])
    t = re.sub(r'^([A-Z0-9]{2,3}\d{1,4})-\d{1,2}$', r'\1', t)
    return bool(t) and bool(RE_FLIGHT.match(t))


def norm_flight(txt):
    """Echte Flugnummer: „4Y 536" → „4Y536"; Tages-Suffix „DE 2199 /14" →
    „DE2199"; Leg-Sequenz „LH8264-1" → „LH8264" (Claudia, 07-27).
    Trainings-/Lektions-Code (D4_32/33, MCC APS MISSION 3, C4_52 P-Check):
    unverändert lesbar halten, nur Mehrfach-Whitespace kollabieren — das
    Backend strippt für den Leg-Key selbst die Leerzeichen."""
    t = (txt or '').strip().upper()
    if not t:
        return None
    if not is_real_flight_number(t):
        return re.sub(r'\s+', ' ', t) or None
    t = t.split('/')[0].strip()            # Tages-Suffix ab '/' abschneiden
    t = re.sub(r'\s+', '', t)
    t = re.sub(r'^([A-Z0-9]{2,3}\d{1,4})-\d{1,2}$', r'\1', t)
    return t or None


def norm_sim_code(txt):
    """Sim-Code steht bei OffBlock in der Flugnummern-Spalte und darf NICHT
    entleert werden („BTSF OT TT 773" bleibt lesbar); nur Tages-Suffix ab
    '/' weg, wenn es überhaupt eine Flugnummer-Form ist."""
    t = (txt or '').strip().upper()
    if not t:
        return None
    if is_real_flight_number(t):
        t = t.split('/')[0].strip()
    return re.sub(r'\s+', ' ', t) or None


def norm_int(txt):
    t = (txt or '').strip()
    return int(t) if t.isdigit() and int(t) > 0 else 0


def flown_minutes(r):
    """Summe der Klassen-/PIC-Zeitspalten — >0 heißt: Flug ist geflogen und
    verbucht (OffBlock füllt sie erst beim Abhaken). ACHTUNG: bei
    KABINEN-Exporten sind diese Spalten durchgängig 00:00 — dort tragen
    Kennzeichen/PIC/Landungen die Beweislast (siehe looks_unflown)."""
    return sum(hhmm_to_min(s(r, c)) or 0 for c in TIME_CLASS_COLS)


def looks_unflown(r):
    """Vorbelegung aus dem kommenden Dienstplan (nichts davon ist Ist-Daten).
    Bewusst streng: EIN Indiz für „geflogen" genügt, um die Zeile zu halten."""
    if flown_minutes(r) > 0:
        return False
    if s(r, 'Aircraft registration'):
        return False
    if any(norm_int(s(r, c)) for c in
           ('Take off day (count)', 'Take off night (count)',
            'Landing day (count)', 'Landing night (count)')):
        return False
    if s(r, 'Pilot flying').lower() in ('ja', 'yes', 'true', '1'):
        return False
    if any(s(r, c) for c in ('PIC', 'SIC', 'SFO', 'FO')):
        return False
    return True


def is_planned_for_cutoff(r, duty_date, cutoff):
    """Noch nicht importierbare Dienstplanzeile relativ zum Ist-Cutoff.

    Ein Datum *nach* dem Cutoff ist per Definition noch nicht geflogen. Einige
    OffBlock-Exporte befüllen für kommende Umläufe bereits PIC/FO sowie
    Zeitklassen; diese Planwerte dürfen ``looks_unflown`` nicht als Ist-Beleg
    überstimmen. Am Cutoff-Tag selbst bleibt dagegen die bestehende
    Evidenzprüfung aktiv, weil ein bereits absolvierter Flug desselben Tages
    legitim importiert werden kann.
    """
    return duty_date > cutoff or (
        duty_date == cutoff and looks_unflown(r)
    )


def is_unflown_past(r, duty_date, cutoff):
    """Historische Zeile ohne Ist-Beleg: behalten, aber sichtbar warnen.

    Die Prüfung ist bewusst getrennt von `is_planned_for_cutoff`, weil jener
    Helper für Daten vor dem Cutoff per Definition immer False liefert.
    """
    return duty_date < cutoff and looks_unflown(r)


def is_zero_duration_marker(r):
    """Historischer OffBlock-Marker ohne ableitbare Flug- oder FSTD-Zeit.

    Einzelne alte Exporte enthalten Type=Flight-Zeilen mit identischer
    Abflug-/Ankunftszeit am selben Ort, Total time 00:00 und ohne jede
    Flug-/Muster-/Kennzeichenangabe. Das ist kein Mitternachtsflug: Würde die
    Zeile wie ein normaler Leg behandelt, machte ``arr <= dep`` daraus einen
    erfundenen 24-Stunden-Leg ohne Blockzeit.
    """
    dep_p, arr_p = s(r, 'Departure place'), s(r, 'Arrival place')
    dep_t, arr_t = s(r, 'Departure time'), s(r, 'Arrival time')
    total = hhmm_to_min(s(r, 'Total time'))
    return (
        s(r, 'Type').strip().lower() in TYPE_FLIGHT
        and bool(dep_p) and dep_p.upper() == arr_p.upper()
        and bool(dep_t) and dep_t == arr_t
        and total == 0
        and not s(r, 'Flight number')
        and not s(r, 'Aircraft registration')
        and not s(r, 'Aircraft ICAO')
    )


def merge_clusters(legs, window_min, report):
    """Doppelt verbuchte Flüge zusammenlegen (siehe Kopf, Punkt 1).

    `legs` ist nach (date, dep_iso) sortiert. Gruppiert wird auf
    `date|flight|from|to`; innerhalb der Gruppe bilden Legs einen Cluster,
    solange ihr Abflug höchstens `window_min` Minuten vom Cluster-Anker
    entfernt liegt. Rückgabe: neue Leg-Liste."""
    def dep_min(l):
        iso = l.get('dep_iso') or ''
        if len(iso) < 16:
            return None
        return int(iso[11:13]) * 60 + int(iso[14:16])

    buckets = {}
    order = []
    for l in legs:
        k = (l['date'], (l.get('flight') or '').upper(),
             l.get('from') or '', l.get('to') or '')
        if k not in buckets:
            buckets[k] = []
            order.append(k)
        buckets[k].append(l)

    out = []
    for k in order:
        group = buckets[k]
        if len(group) == 1:
            out.append(group[0])
            continue
        clusters = []
        for l in group:
            dm = dep_min(l)
            placed = False
            for c in clusters:
                anchor = dep_min(c[0])
                if dm is None or anchor is None:
                    if dm is None and anchor is None:
                        c.append(l)
                        placed = True
                        break
                    continue
                if abs(dm - anchor) <= window_min:
                    c.append(l)
                    placed = True
                    break
            if not placed:
                clusters.append([l])
        for c in clusters:
            if len(c) == 1:
                out.append(c[0])
                continue
            # Varianten innerhalb des Clusters: gleiche Zeit+Blockzeit =
            # dieselbe Quellvariante. Gewinner = meiste Quellzeilen.
            variants = {}
            for l in c:
                sig = (l.get('dep_iso'), l.get('arr_iso'), l.get('block_min'))
                variants.setdefault(sig, []).append(l)
            ranked = sorted(
                variants.items(),
                key=lambda kv: (-len(kv[1]),
                                0 if any(x.get('reg') for x in kv[1]) else 1,
                                0 if any(x.get('type') for x in kv[1]) else 1,
                                kv[0][0] or ''))
            win_sig, win_rows = ranked[0]
            base = dict(win_rows[0])
            # Felder aus ALLEN Zeilen des Clusters nachfüllen (Gewinner zuerst)
            for l in win_rows[1:] + [x for sig, rows in ranked[1:] for x in rows]:
                for f in FILL_FIELDS:
                    if base.get(f) in (None, '', 0, False) and l.get(f) not in (None, '', 0, False):
                        base[f] = l[f]
            dropped_block = sum(l.get('block_min', 0) for l in c) - \
                base.get('block_min', 0)
            report.append({
                'date': base['date'],
                'flight': base.get('flight') or '—',
                'route': f"{base.get('from')}→{base.get('to')}",
                'rows': len(c),
                'kept': f"{(win_sig[0] or '')[11:16]}-{(win_sig[1] or '')[11:16]}"
                        f" ({(base.get('block_min') or 0) // 60}:"
                        f"{(base.get('block_min') or 0) % 60:02d})",
                'dropped': [f"{(sig[0] or '')[11:16]}-{(sig[1] or '')[11:16]}"
                            f" ({(sig[2] or 0) // 60}:{(sig[2] or 0) % 60:02d})"
                            f" ×{len(rows)}"
                            for sig, rows in ranked[1:]]
                           + ([f"identisch ×{len(win_rows) - 1}"]
                              if len(win_rows) > 1 else []),
                'dropped_block': dropped_block,
            })
            out.append(base)
    out.sort(key=lambda x: (x['date'], x.get('dep_iso') or ''))
    return out


def matches_csv(path):
    """True nur für das belegte OffBlock-Duties-Schema.

    Dekodier-/CSV-Fehler sind hier ein schlichtes Nicht-Match; die eigentliche
    Parse-Funktion bleibt streng und wirft bei einem erkannten, aber kaputten
    Export einen ValueError zur manuellen Prüfung.
    """
    try:
        with open(path, encoding='utf-8-sig', newline='') as handle:
            fields = set(csv.DictReader(handle, delimiter=';').fieldnames or [])
        return all(any(name in fields for name in _column_names(required))
                   for required in REQUIRED_HEADERS)
    except (OSError, UnicodeError, csv.Error):
        return False


def parse_csv(src, *, cutoff=None, keep_planned=False,
              keep_sim_as_flight=False, keep_duplicates=False, window=15):
    """OffBlock-Duties-Datei für eingebettete Aufrufer parsen.

    Rückgabe entspricht den PDF-Parsern des Watchdogs: ``legs, sims, report``.
    Der CLI-Pfad darunter nutzt exakt dieselbe Funktion und behält damit seine
    bisherigen Schutzregeln und Kontrollsummen.
    """
    cutoff = cutoff or date.today()
    if not matches_csv(src):
        raise ValueError('kein OffBlock-Duties-CSV-Kopf')
    with open(src, encoding='utf-8-sig', newline='') as handle:
        rows = [_canonical_row(row, number)
                for number, row in enumerate(
                    csv.DictReader(handle, delimiter=';'), start=2)]

    legs, sims = [], []
    skipped = {'kein_datum': 0, 'summenzeile': 0, 'platzhalter': 0,
               'leer': 0, 'geplant': 0, 'nullzeit_marker': 0}
    planned_rows, unflown_past, sim_as_flight, merged = [], [], [], []
    # unabhängige Kontrollsumme direkt aus der Quelle (ALLE Flug-Zeilen, auch
    # die später zusammengelegten — die Differenz wird getrennt ausgewiesen)
    src_block = src_ldg = 0

    for r in rows:
        typ = s(r, 'Type')
        dt = parse_date(s(r, 'Date'))
        if not dt:
            skipped['leer'] += 1        # EBT-Artefakte / Kurzzeilen ohne Datum
            continue
        typ_l = typ.strip().lower()
        if typ_l not in TYPE_FLIGHT | TYPE_SIM:
            skipped['summenzeile'] += 1
            continue

        if is_zero_duration_marker(r):
            skipped['nullzeit_marker'] += 1
            continue

        # ── noch nicht geflogen (Dienstplan-Vorbelegung) ──────────────────
        if is_planned_for_cutoff(r, dt.date(), cutoff):
            if not keep_planned:
                skipped['geplant'] += 1
                planned_rows.append((dt.strftime('%Y-%m-%d'), typ,
                                     s(r, 'Flight number'),
                                     f"{s(r, 'Departure place')}→"
                                     f"{s(r, 'Arrival place')}",
                                     s(r, 'Total time')))
                continue
        elif is_unflown_past(r, dt.date(), cutoff):
            unflown_past.append((dt.strftime('%Y-%m-%d'),
                                 s(r, 'Flight number')))

        role = norm_role(s(r, 'Function'))
        total = hhmm_to_min(s(r, 'Total time'))
        reg_raw = re.sub(r'\s+', '', s(r, 'Aircraft registration').upper())

        # ── Simulator ────────────────────────────────────────────────────
        # entweder Type=Simulator ODER als „Flug" verbuchte FFS-Session
        # (Kennzeichen ist eine EASA-FSTD-Gerätenummer)
        is_fstd_row = (
            typ_l in TYPE_FLIGHT
            and fstd_row_is_sim(
                reg_raw, s(r, 'Departure place'), s(r, 'Arrival place'),
                s(r, 'Flight number'))
            and not keep_sim_as_flight
        )
        if typ_l in TYPE_SIM or is_fstd_row:
            code = (norm_sim_code(s(r, 'Flight number'))
                    or s(r, 'Aircraft ICAO') or s(r, 'Notes')[:40] or None)
            sim = {'date': dt.strftime('%Y-%m-%d'),
                   'place': s(r, 'Departure place') or None,
                   'duration_min': total}
            if code:
                sim['code'] = code
            if role:
                sim['role'] = role
            instr = s(r, 'Instructor')
            if instr and not re.match(r'^[\d:]+$', instr):
                sim['instructor'] = instr
            sims.append({k: v for k, v in sim.items() if v is not None})
            if is_fstd_row:
                sim_as_flight.append((dt.strftime('%Y-%m-%d'), reg_raw, code,
                                      total,
                                      norm_int(s(r, 'Landing day (count)'))
                                      + norm_int(s(r, 'Landing night (count)'))))
            continue

        # ── Flug ─────────────────────────────────────────────────────────
        dep_p, arr_p = s(r, 'Departure place'), s(r, 'Arrival place')
        dep_t, arr_t = s(r, 'Departure time'), s(r, 'Arrival time')
        flight = norm_flight(s(r, 'Flight number'))

        # Platzhalter 00:00–23:59 (URLAUB/MEDICAL): nur behalten, wenn eine
        # echte Flugnummer dransteht — dann ohne Blockzeit.
        placeholder = (dep_t == '00:00' and arr_t == '23:59')
        if placeholder and not (flight and RE_FLIGHT.match(flight)):
            skipped['platzhalter'] += 1
            continue

        if not dep_p and not arr_p and not flight:
            skipped['leer'] += 1
            continue

        leg = {'date': dt.strftime('%Y-%m-%d')}
        if flight:
            leg['flight'] = flight
        if dep_p:
            leg['from'] = dep_p.upper()[:4]
        if arr_p:
            leg['to'] = arr_p.upper()[:4]

        # Zeiten (UTC laut Export). Übernacht: arr +1 Tag wenn arr <= dep.
        dep_m, arr_m = hhmm_to_min(dep_t), hhmm_to_min(arr_t)
        if dep_m is not None and not placeholder:
            dep_dt = dt + timedelta(minutes=dep_m)
            leg['dep_iso'] = dep_dt.strftime('%Y-%m-%dT%H:%M:00Z')
            if arr_m is not None:
                arr_dt = dt + timedelta(minutes=arr_m)
                if arr_m <= dep_m:
                    arr_dt += timedelta(days=1)
                leg['arr_iso'] = arr_dt.strftime('%Y-%m-%dT%H:%M:00Z')

        if not placeholder and total and 0 < total < 1200:
            leg['block_min'] = total
            src_block += total

        # Reg säubern: Leerzeichen raus („D- AIZI"), Platzhalter verwerfen.
        # G-ENERIC ist KEIN Kennzeichen, sondern der Dummy des Exports —
        # Leg bleibt, nur das Feld fällt weg (Raten wäre Erfinden).
        if (reg_raw and not RE_FSTD.fullmatch(reg_raw)
                and reg_raw not in ('G-ENERIC', 'GENERIC', 'UNKNOWN',
                                    'N/A', '-')):
            leg['reg'] = reg_raw
        icao = s(r, 'Aircraft ICAO').upper()
        if icao:
            leg['type'] = icao
        if s(r, 'Pilot flying').lower() in ('ja', 'yes', 'true', '1'):
            leg['pf'] = True

        for key, col in (('to_day', 'Take off day (count)'),
                         ('to_night', 'Take off night (count)'),
                         ('ldg_day', 'Landing day (count)'),
                         ('ldg_night', 'Landing night (count)')):
            v = norm_int(s(r, col))
            if v:
                leg[key] = v
        src_ldg += norm_int(s(r, 'Landing day (count)')) \
            + norm_int(s(r, 'Landing night (count)'))

        night = hhmm_to_min(s(r, 'Night time'))
        if night:
            leg['night_min'] = night
        if role:
            leg['role'] = role

        pic = s(r, 'PIC')
        if pic and pic.lower() not in ('ich', 'self', 'me'):
            leg['pic_name'] = pic

        notes = s(r, 'Notes') or s(r, 'Remarks&Endorsements description')
        if notes:
            leg['remarks'] = notes[:200]

        legs.append(leg)

    legs.sort(key=lambda x: (x['date'], x.get('dep_iso') or ''))
    n_raw = len(legs)
    if not keep_duplicates:
        legs = merge_clusters(legs, window, merged)
    sims.sort(key=lambda x: x['date'])

    from legkeys import dedupe_keys
    collisions = dedupe_keys(legs)

    # ── Report ───────────────────────────────────────────────────────────
    block = sum(l.get('block_min', 0) for l in legs)
    ldg = sum(l.get('ldg_day', 0) + l.get('ldg_night', 0) for l in legs)
    simmin = sum(x.get('duration_min') or 0 for x in sims)
    merged_block = sum(m['dropped_block'] for m in merged)
    merged_ldg = src_ldg - ldg
    ok_block = (src_block - merged_block) == block
    ok_ldg = (src_ldg - merged_ldg) == ldg
    if not ok_block or not ok_ldg:
        raise ValueError(
            'OffBlock-Kontrollsumme abweichend: '
            f'Block={ok_block}, Landungen={ok_ldg}'
        )
    report = {
        'filename': src.rsplit('/', 1)[-1],
        'month': (f'{legs[0]["date"][:7]}–{legs[-1]["date"][:7]}'
                  if legs else 'leer'),
        'rows': len(rows),
        'legs': len(legs),
        'raw_legs': n_raw,
        'sim_sessions': len(sims),
        'block_min': block,
        'sim_min': simmin,
        'landings': ldg,
        'merged': merged,
        'merged_block_min': merged_block,
        'collisions': collisions,
        'skipped': skipped,
        'planned_rows': planned_rows,
        'unflown_past': unflown_past,
        'sim_as_flight': sim_as_flight,
        'cutoff': cutoff.isoformat(),
        'dup_window_min': window,
        'control': 'OK',
    }
    return legs, sims, report


def _print_report(src, report):
    """Menschenlesbarer Bericht des historischen CLI-Werkzeugs."""
    legs = report['legs']
    block = report['block_min']
    ldg = report['landings']
    simmin = report['sim_min']
    n_raw = report['raw_legs']
    merged = report['merged']
    merged_block = report['merged_block_min']
    collisions = report['collisions']
    planned_rows = report['planned_rows']
    unflown_past = report['unflown_past']
    sim_as_flight = report['sim_as_flight']
    merged_ldg = sum(m.get('dropped_landings', 0) for m in merged)
    print(f'Quelle      : {src}')
    print(f'CSV-Zeilen  : {report["rows"]}   '
          f'Cutoff geplant: {report["cutoff"]}   '
          f'Dup-Fenster: {report["dup_window_min"]} min')
    print(f'Legs        : {legs}   {block // 60}:{block % 60:02d} Block'
          f'   (roh {n_raw}, zusammengelegt {n_raw - legs})')
    print(f'Landungen   : {ldg}')
    print(f'Sims        : {report["sim_sessions"]}   '
          f'{simmin // 60}:{simmin % 60:02d}')
    print(f'Übersprungen: {report["skipped"]}')
    if planned_rows:
        print(f'GEPLANT (nicht importiert, {len(planned_rows)}): '
              f'{planned_rows[0][0]} → {planned_rows[-1][0]}')
        for p in planned_rows:
            print(f'   {p[0]}  {p[1]:<9} {p[2]:<20} {p[3]:<12} {p[4]}')
    if unflown_past:
        print(f'WARNUNG unbelegte Zeilen VOR Cutoff (behalten): {unflown_past}')
    if sim_as_flight:
        smin = sum(x[3] or 0 for x in sim_as_flight)
        sldg = sum(x[4] for x in sim_as_flight)
        print(f'SIM-als-Flug → sim[] ({len(sim_as_flight)}, '
              f'{smin // 60}:{smin % 60:02d}, {sldg} Sim-Landungen '
              f'zählen NICHT als Flug-Landungen):')
        for x in sim_as_flight:
            print(f'   {x[0]}  {x[1]:<10} {x[2]:<28} {x[3]} min  {x[4]} Ldg')
    if merged:
        print(f'DOPPELT VERBUCHT → zusammengelegt ({len(merged)} Flüge, '
              f'{merged_block // 60}:{merged_block % 60:02d} Blockzeit und '
              f'{merged_ldg} Landungen fielen weg):')
        for m in merged:
            print(f'   {m["date"]}  {m["flight"]:<9} {m["route"]:<10} '
                  f'{m["rows"]} Zeilen → behalten {m["kept"]}, '
                  f'verworfen {", ".join(m["dropped"])}')
    print(f'Key-Kollisionen aufgeloest: {len(collisions)}')
    for c in collisions:
        print(f'   {c[0]}  {c[1]}  {c[2]}  dep {c[3]}  -> Suffix ({c[4]})')
    print('KONTROLLE   : OK')


def main():
    argv = [a for a in sys.argv[1:]]
    keep_planned = '--keep-planned' in argv
    keep_sim_as_flight = '--keep-sim-as-flight' in argv
    keep_duplicates = '--keep-duplicates' in argv
    window = 15
    if '--dup-window' in argv:
        window = int(argv[argv.index('--dup-window') + 1])
    cutoff = date.today()
    if '--cutoff' in argv:
        cutoff = datetime.strptime(argv[argv.index('--cutoff') + 1],
                                   '%Y-%m-%d').date()
    pos = [a for a in argv if not a.startswith('--')
           and not re.match(r'^\d{4}-\d{2}-\d{2}$', a) and not a.isdigit()]
    src, dst = pos[0], pos[1]
    legs, sims, report = parse_csv(
        src, cutoff=cutoff, keep_planned=keep_planned,
        keep_sim_as_flight=keep_sim_as_flight,
        keep_duplicates=keep_duplicates, window=window)
    with open(dst, 'w', encoding='utf-8') as handle:
        json.dump({'legs': legs, 'sim': sims}, handle, ensure_ascii=False)
    _print_report(src, report)


if __name__ == '__main__':
    main()
