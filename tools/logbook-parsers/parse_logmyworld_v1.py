#!/usr/bin/env python3
"""logmy.world-Export (ZIP: flights.json + XLSX) → AeroX-Import-Legs (Format 2d).

  python3 parse_logmyworld_v1.py <flights.json> <flights.xlsx> <ziel.json>

NEUE QUELLE (Upload #25, Viktor Bruhn, LH/FRA Kabine): logmy.world ist ein
Flight-Tracking-Dienst für Crew+Vielflieger. Der Account-Export liefert
flights.json (API-Dump, Zeiten als UTC-Timestamps, Flughäfen nur als UUID)
und ein XLSX mit aufgelösten IATA-Codes — beide werden über
(Flugnummer, geplante lokale Abflugzeit) gejoint; der Join muss für JEDE
Zeile aufgehen, sonst Abbruch.

Eigenheiten dieser Quelle:
1. **Drei Leg-Kategorien**: traveller_type=cabin_crew (Dienst, Rolle FB),
   pax+work (Deadhead → Remark „DH"), pax+private (Privatreise als
   Passagier → Remark). Nichts wird still gefiltert — der User hat seinen
   kompletten Flug-Log hochgeladen (Sander-Präzedenz: Quelle spiegeln).
2. **block_min = actual_block_duration** (der Wert, den seine App anzeigt
   und auch ins XLSX exportiert). Bei 5 von 440 Legs widerspricht das Feld
   der Differenz der UTC-Timestamps; wo der Widerspruch > TOL Minuten ist
   (Handeingabe-Fehler, bei LH630 wäre Airborne > Block), werden die
   Uhrzeiten WEGGELASSEN statt widersprüchliche Daten zu spiegeln
   (Florian-Regel: Uhrzeiten weglassen, wenn uneindeutig).
3. **Muster aus aircraft_series**: explizite Series→ICAO-Tabelle
   (deterministisch, kein Raten); unbekannte Series → kein type, Report.
4. **Flugnummern-Müll**: „LH595!!!!!!!!!KML" → „LH595" (nur wenn ein
   echtes Flugnummern-Präfix erkennbar ist; Rest in den Report).
5. Datum = UTC-Datum des Actual-Block-Abflugs (OffBlock-Konvention,
   Roster-Überlapp-Keys matchen damit).
"""
import json
import re
import sys
from collections import Counter
from datetime import datetime

import openpyxl

from legkeys import dedupe_keys

TOL_MIN = 3  # Toleranz Feld-Dauer vs. Timestamp-Differenz (Rundung)

RE_FLIGHT_PREFIX = re.compile(r'^([A-Z]{1,3}\s?\d{1,4}[A-Z]?)')

# ICAO-Typcodes, eindeutig aus dem expliziten Series-String der Quelle —
# kein Raten, jede Zeile ist eine feste Zuordnung.
SERIES_ICAO = {
    'A319-100': 'A319',
    'A320-200': 'A320',
    'A320-200N': 'A20N',
    'A321-100': 'A321',
    'A321-200': 'A321',
    'A321-200N': 'A21N',
    'A330-300': 'A333',
    'A340-300': 'A343',
    'A340-600': 'A346',
    'A350-900': 'A359',
    'B747-400': 'B744',
    'B747-8': 'B748',
    'B787-9': 'B789',
    'CRJ900': 'CRJ9',
    'CRJ1000': 'CRJX',
    'ERJ 190-300': 'E295',
}


def ts(x):
    return datetime.fromisoformat(x.replace('+00', '+00:00'))


def norm_flight(raw, junk_report):
    t = (raw or '').strip().upper()
    m = RE_FLIGHT_PREFIX.match(t)
    if m:
        clean = re.sub(r'\s+', '', m.group(1))
        if clean != t:
            junk_report.append((raw, clean))
        return clean
    return re.sub(r'\s+', ' ', t) or None


def main():
    src_json, src_xlsx, dst = sys.argv[1], sys.argv[2], sys.argv[3]
    flights = json.load(open(src_json))['data']

    ws = openpyxl.load_workbook(src_xlsx)['Flight list']
    xlsx = {}
    for r in ws.iter_rows(min_row=3, values_only=True):
        key = (r[1], f'{str(r[2])[:10]} {str(r[5])[:5]}')
        if key in xlsx:
            sys.exit(f'XLSX-Join-Key doppelt: {key}')
        xlsx[key] = r

    legs, junk, dropped_times, unknown_series = [], [], [], Counter()
    src_block = 0
    cat = Counter()

    for f in sorted(flights, key=lambda x: x['departure_actual_block_utc_ts']):
        key = (f['flight_number'],
               f'{(f.get("departure_scheduled_block_date") or "")[:10]} '
               f'{(f.get("departure_scheduled_block_time") or "")[:5]}')
        x = xlsx.get(key)
        if x is None:
            sys.exit(f'Kein XLSX-Match für {key} — Join kaputt, Abbruch.')

        dep = ts(f['departure_actual_block_utc_ts'])
        arr = ts(f['arrival_actual_block_utc_ts'])
        dur = f['actual_block_duration']
        src_block += dur or 0
        ts_diff = int((arr - dep).total_seconds() // 60)

        leg = {'date': dep.strftime('%Y-%m-%d')}
        fl = norm_flight(f['flight_number'], junk)
        if fl:
            leg['flight'] = fl
        leg['from'] = str(x[8]).upper()[:4]   # Departure Airport Code
        leg['to'] = str(x[19]).upper()[:4]    # Arrival Airport Code

        if abs(ts_diff - dur) <= TOL_MIN:
            leg['dep_iso'] = dep.strftime('%Y-%m-%dT%H:%M:00Z')
            leg['arr_iso'] = arr.strftime('%Y-%m-%dT%H:%M:00Z')
        else:
            dropped_times.append(
                (leg['date'], fl, f'{leg["from"]}→{leg["to"]}',
                 f'Feld {dur} min vs. Timestamps {ts_diff} min'))

        if dur and 0 < dur < 1200:
            leg['block_min'] = dur

        reg = re.sub(r'\s+', '', (f.get('aircraft_registration') or '').upper())
        if reg and reg not in ('G-ENERIC', 'GENERIC', 'UNKNOWN', 'N/A', '-'):
            leg['reg'] = reg
        series = (f.get('aircraft_series') or '').strip()
        icao = SERIES_ICAO.get(series)
        if icao:
            leg['type'] = icao
        elif series:
            unknown_series[series] += 1

        tt, reason = f.get('traveller_type'), f.get('travel_reason')
        cat[(tt, reason)] += 1
        parts = []
        if tt == 'cabin_crew':
            leg['role'] = 'FB'
        elif reason == 'work':
            parts.append('DH')
        elif reason == 'private':
            parts.append('privat als Passagier')
        comment = (f.get('comments') or '').strip()
        if comment:
            parts.append(comment)
        if parts:
            leg['remarks'] = ' · '.join(parts)[:200]

        legs.append(leg)

    collisions = dedupe_keys(legs)
    json.dump({'legs': legs, 'sim': []}, open(dst, 'w'), ensure_ascii=False)

    block = sum(l.get('block_min', 0) for l in legs)
    print(f'Quelle      : {src_json} + XLSX-Join {len(xlsx)} Zeilen')
    print(f'Legs        : {len(legs)}   {block // 60}:{block % 60:02d} Block')
    print(f'Zeitraum    : {legs[0]["date"]} → {legs[-1]["date"]}')
    print(f'Kategorien  : {dict(cat)}')
    print(f'mit reg     : {sum(1 for l in legs if l.get("reg"))}   '
          f'mit type: {sum(1 for l in legs if l.get("type"))}   '
          f'mit remarks: {sum(1 for l in legs if l.get("remarks"))}')
    if junk:
        print(f'Flugnummern bereinigt ({len(junk)}): {junk}')
    if dropped_times:
        print(f'Uhrzeiten WEGGELASSEN (widersprüchlich, {len(dropped_times)}):')
        for d in dropped_times:
            print(f'   {d[0]}  {d[1]:<8} {d[2]:<10} {d[3]}')
    if unknown_series:
        print(f'Series OHNE ICAO-Zuordnung (type weggelassen): '
              f'{dict(unknown_series)}')
    print(f'Key-Kollisionen aufgeloest: {len(collisions)}')
    for c in collisions:
        print(f'   {c[0]}  {c[1]}  {c[2]}  dep {c[3]}  -> Suffix ({c[4]})')
    ok = (len(legs) == len(flights)
          and block == sum(min(f["actual_block_duration"], 1199)
                           for f in flights
                           if 0 < (f["actual_block_duration"] or 0)))
    print(f'KONTROLLE   : {len(legs)}/{len(flights)} Legs, Block Quelle '
          f'{src_block // 60}:{src_block % 60:02d} = geparst '
          f'{block // 60}:{block % 60:02d} '
          f'({"OK" if ok else "ABWEICHUNG"})')
    if not ok:
        sys.exit(1)


if __name__ == '__main__':
    main()
