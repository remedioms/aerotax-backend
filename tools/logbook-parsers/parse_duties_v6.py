#!/usr/bin/env python3
"""OffBlock-„Duties"-CSV → normalisierte AeroX-Import-Legs (Format 2d).

  python3 parse_duties_v6.py <quelle.csv> <ziel.json> [--cutoff YYYY-MM-DD]
                             [--keep-planned] [--keep-sim-as-flight]

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
   Type=Flug, deren „Kennzeichen" eine EASA-FSTD-Gerätenummer ist
   (`DE-1A-040` = LH Aviation Training A320-FFS), sind Simulator-Sessions
   und wandern nach `sim[]`. Sonst landen bei Martin 104 h FFS-Zeit und
   59 Sim-„Landungen" in der Flug-Blockzeit → Invariante 5 (FCL.050 trennt
   FSTD-Zeit) wäre gebrochen. `--keep-sim-as-flight` schaltet es ab.
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
}
# echte Flugnummer: 2-3 Zeichen Präfix + 1-4 Ziffern (+ optionaler Buchstabe)
RE_FLIGHT = re.compile(r'^[A-Z0-9]{2,3}\s?\d{1,4}[A-Z]?$')
# EASA-FSTD-Gerätenummer statt Kennzeichen („DE-1A-040", „DE-2B-007")
RE_FSTD = re.compile(r'^[A-Z]{2}-\d[A-Z]-\d{1,4}$')
# Zeitspalten, die belegen, dass der Flug WIRKLICH stattgefunden hat
TIME_CLASS_COLS = ('pic_time', 'Single pilot', 'Multi pilot', 'Dual')


def s(row, key):
    """Feld sicher als getrimmter String (CSV liefert None bei Kurzzeilen)."""
    return (row.get(key) or '').strip()


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
    verbucht (OffBlock füllt sie erst beim Abhaken)."""
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


def main():
    argv = [a for a in sys.argv[1:]]
    keep_planned = '--keep-planned' in argv
    keep_sim_as_flight = '--keep-sim-as-flight' in argv
    cutoff = date.today()
    if '--cutoff' in argv:
        cutoff = datetime.strptime(argv[argv.index('--cutoff') + 1],
                                   '%Y-%m-%d').date()
    pos = [a for a in argv if not a.startswith('--')
           and not re.match(r'^\d{4}-\d{2}-\d{2}$', a)]
    src, dst = pos[0], pos[1]
    rows = list(csv.DictReader(open(src, encoding='utf-8-sig'), delimiter=';'))

    legs, sims = [], []
    skipped = {'kein_datum': 0, 'summenzeile': 0, 'platzhalter': 0,
               'dubletten': 0, 'leer': 0, 'geplant': 0}
    planned_rows, unflown_past, sim_as_flight = [], [], []
    seen = set()
    # unabhängige Kontrollsumme direkt aus der Quelle
    src_block = src_ldg = 0

    for r in rows:
        typ = s(r, 'Type')
        dt = parse_date(s(r, 'Date'))
        if not dt:
            skipped['leer'] += 1        # EBT-Artefakte / Kurzzeilen ohne Datum
            continue
        if typ not in ('Flug', 'Simulator'):
            skipped['summenzeile'] += 1
            continue

        # ── noch nicht geflogen (Dienstplan-Vorbelegung) ──────────────────
        if looks_unflown(r):
            if dt.date() >= cutoff and not keep_planned:
                skipped['geplant'] += 1
                planned_rows.append((dt.strftime('%Y-%m-%d'), typ,
                                     s(r, 'Flight number'),
                                     f"{s(r, 'Departure place')}→"
                                     f"{s(r, 'Arrival place')}",
                                     s(r, 'Total time')))
                continue
            if dt.date() < cutoff:
                unflown_past.append((dt.strftime('%Y-%m-%d'),
                                     s(r, 'Flight number')))

        role = ROLES.get(s(r, 'Function').lower(), s(r, 'Function') or None)
        total = hhmm_to_min(s(r, 'Total time'))
        reg_raw = re.sub(r'\s+', '', s(r, 'Aircraft registration').upper())

        # ── Simulator ────────────────────────────────────────────────────
        # entweder Type=Simulator ODER als „Flug" verbuchte FFS-Session
        # (Kennzeichen ist eine EASA-FSTD-Gerätenummer)
        is_fstd_row = (typ == 'Flug' and RE_FSTD.match(reg_raw)
                       and not keep_sim_as_flight)
        if typ == 'Simulator' or is_fstd_row:
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
        if reg_raw and reg_raw not in ('G-ENERIC', 'GENERIC', 'UNKNOWN',
                                       'N/A', '-'):
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

        # Dedupe auf Leg-Key; Trainings-Doppel ohne Flugnummer mit Suffix
        key = (leg['date'], leg.get('flight') or '', leg.get('from') or '',
               leg.get('to') or '', leg.get('dep_iso') or '')
        if key in seen:
            skipped['dubletten'] += 1
            continue
        seen.add(key)
        legs.append(leg)

    legs.sort(key=lambda x: (x['date'], x.get('dep_iso') or ''))
    sims.sort(key=lambda x: x['date'])

    from legkeys import dedupe_keys
    collisions = dedupe_keys(legs)

    out = {'legs': legs, 'sim': sims}
    json.dump(out, open(dst, 'w'), ensure_ascii=False)

    # ── Report ───────────────────────────────────────────────────────────
    block = sum(l.get('block_min', 0) for l in legs)
    ldg = sum(l.get('ldg_day', 0) + l.get('ldg_night', 0) for l in legs)
    simmin = sum(x.get('duration_min') or 0 for x in sims)
    print(f'Quelle      : {src}')
    print(f'CSV-Zeilen  : {len(rows)}   Cutoff geplant: {cutoff}')
    print(f'Legs        : {len(legs)}   {block // 60}:{block % 60:02d} Block')
    print(f'Landungen   : {ldg}')
    print(f'Sims        : {len(sims)}   {simmin // 60}:{simmin % 60:02d}')
    print(f'Zeitraum    : {legs[0]["date"]} → {legs[-1]["date"]}' if legs else '')
    print(f'Übersprungen: {skipped}')
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
    print(f'Key-Kollisionen aufgeloest: {len(collisions)}')
    for c in collisions:
        print(f'   {c[0]}  {c[1]}  {c[2]}  dep {c[3]}  -> Suffix ({c[4]})')
    print('KONTROLLE   : Block aus Quelle direkt = '
          f'{src_block // 60}:{src_block % 60:02d} '
          f'({"OK" if src_block == block else "ABWEICHUNG"}), '
          f'Landungen Quelle = {src_ldg} '
          f'({"OK" if src_ldg == ldg else "ABWEICHUNG"})')


if __name__ == '__main__':
    main()
