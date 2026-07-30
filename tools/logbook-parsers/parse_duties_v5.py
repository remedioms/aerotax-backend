#!/usr/bin/env python3
"""OffBlock-„Duties"-CSV → normalisierte AeroX-Import-Legs (Format 2d).

  python3 parse_duties_v5.py <quelle.csv> <ziel.json>

Neuaufbau von /tmp/parse_duties_v4.py (tmp-Cleanup hat v1-v4 gelöscht).
Regeln aus ~/Desktop/AeroX-Feature-Docs/Flugbuch.md Abschnitt 2d.
"""
import csv
import json
import re
import sys
from datetime import datetime, timedelta

ROLES = {
    'pilot in command': 'PIC',
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
    'flight attendant': 'FB',
    'flugbegleiter': 'FB',
    'flugbegleiterin': 'FB',
    'cabin': 'FB',
}
# echte Flugnummer: 2-3 Zeichen Präfix + 1-4 Ziffern (+ optionaler Buchstabe)
RE_FLIGHT = re.compile(r'^[A-Z0-9]{2,3}\s?\d{1,4}[A-Z]?$')


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


def norm_flight(txt):
    """„4U 602" → „4U602"; Tages-Suffix „DE 2199 /14" → „DE2199";
    Leg-Sequenz-Suffix „LH8264-1" → „LH8264" (Claudia Sachwitz, 07-27:
    dieselbe Flugnummer steht im selben Export auch ohne Suffix)."""
    t = (txt or '').strip().upper()
    if not t:
        return None
    t = t.split('/')[0].strip()            # Tages-Suffix ab '/' abschneiden
    t = re.sub(r'\s+', '', t)
    t = re.sub(r'^([A-Z0-9]{2,3}\d{1,4})-\d{1,2}$', r'\1', t)
    return t or None


def norm_sim_code(txt):
    """Sim-Code steht bei OffBlock in der Flugnummern-Spalte und darf NICHT
    entleert werden („BTSF OT TT 773" bleibt lesbar); nur Tages-Suffix ab
    '/' weg."""
    t = (txt or '').strip().upper()
    if not t:
        return None
    t = re.sub(r'\s+', ' ', t.split('/')[0].strip())
    return t or None


def norm_int(txt):
    t = (txt or '').strip()
    return int(t) if t.isdigit() and int(t) > 0 else 0


def main():
    src, dst = sys.argv[1], sys.argv[2]
    rows = list(csv.DictReader(open(src, encoding='utf-8-sig'), delimiter=';'))

    legs, sims = [], []
    skipped = {'kein_datum': 0, 'summenzeile': 0, 'platzhalter': 0,
               'dubletten': 0, 'leer': 0}
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

        role = ROLES.get(s(r, 'Function').lower(), s(r, 'Function') or None)
        total = hhmm_to_min(s(r, 'Total time'))

        # ── Simulator ────────────────────────────────────────────────────
        if typ == 'Simulator':
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
        reg = re.sub(r'\s+', '', s(r, 'Aircraft registration').upper())
        if reg and reg not in ('G-ENERIC', 'GENERIC', 'UNKNOWN', 'N/A', '-'):
            leg['reg'] = reg
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
    print(f'CSV-Zeilen  : {len(rows)}')
    print(f'Legs        : {len(legs)}   {block // 60}:{block % 60:02d} Block')
    print(f'Landungen   : {ldg}')
    print(f'Sims        : {len(sims)}   {simmin // 60}:{simmin % 60:02d}')
    print(f'Zeitraum    : {legs[0]["date"]} → {legs[-1]["date"]}' if legs else '')
    print(f'Übersprungen: {skipped}')
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
