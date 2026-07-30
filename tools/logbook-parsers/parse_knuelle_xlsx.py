#!/usr/bin/env python3
"""Vanessa Knülles handgebautes Condor-Workbook → AeroX-Import-Legs.

  python3 parse_knuelle_xlsx.py <quelle.xlsx> <ziel.json>

Spalten: DATE | Simulator | FLT.NR. DE- | B757 DAB- | B767 DAB- | FROM | TO |
         OFF | ON | TIME | CAPTAIN | SFO | FO | APPROACH | LDG

Eigenheiten dieser Quelle (belegt, nicht geraten):
* Die beiden Reg-Spalten sind NICHT typtreu — 14 Kennzeichen stehen in der
  falschen Spalte. Kennzeichen (D-AB + Suffix) gewinnt, Typ kommt aus
  `aircraft_registry`. Widerspricht die Registry der 757/767-Familie oder
  kennt sie das Kennzeichen nicht, bleibt der Typ LEER statt geraten.
* Landungen ohne Tag/Nacht-Split → alle nach ldg_day; Summe stimmt, der
  Split ist im Leg-Editor korrigierbar (Overlay schlägt Import).
* Rolle aus der LEEREN Crew-Spalte: CPT+SFO benannt ⇒ sie ist FO,
  CPT+FO benannt ⇒ sie ist SFO, nur CPT benannt ⇒ Copilot-Default FO.
"""
import datetime
import json
import os
import re
import sys

import openpyxl
import psycopg2

ENV = os.path.expanduser('~/Developer/flight-warehouse/.env.nas')
URL = [l.split('=', 1)[1].strip() for l in open(ENV)
       if l.startswith('DATABASE_URL=')][0]
# Typen, die zur 757/767-Familie des Workbooks passen
FAMILY_OK = ('B75', 'B76')


def t(v):
    return str(v).strip() if v is not None else ''


def mins(v):
    if isinstance(v, datetime.timedelta):
        return int(v.total_seconds() // 60)
    if isinstance(v, datetime.time):
        return v.hour * 60 + v.minute
    s = t(v)
    if ':' in s:
        p = s.split(':')
        return int(p[0]) * 60 + int(p[1])
    return 0


def reg_types(regs):
    """Kennzeichen → Typ aus aircraft_registry (570k Zeilen)."""
    conn = psycopg2.connect(URL)
    cur = conn.cursor()
    cur.execute('select reg, actype from public.aircraft_registry '
                'where reg = any(%s)', (sorted(regs),))
    out = {r[0]: r[1] for r in cur.fetchall() if r[1]}
    conn.close()
    return out


def main():
    src, dst = sys.argv[1], sys.argv[2]
    rows = list(openpyxl.load_workbook(src, data_only=True)
                .active.iter_rows(values_only=True))[1:]

    # Runde 1: alle Kennzeichen sammeln und in EINEM Query auflösen
    regs = set()
    for r in rows:
        for c in (3, 4):
            if t(r[c]):
                regs.add('D-AB' + t(r[c]).upper())
    types = reg_types(regs)

    # Jahres-Vertipper heilen: Das Workbook ist chronologisch geführt. Fällt
    # ein Datum aus der Reihe (Jahr vor 1990 — vor ihrer Laufbahn), aber
    # Tag+Monat liegen zwischen Vorgänger und Nachfolger, ist das Jahr des
    # Vorgängers belegt (nicht geraten). Alles andere bleibt unangetastet.
    dates = [r[0] if isinstance(r[0], datetime.datetime) else None
             for r in rows]
    fixed = []
    for i, d in enumerate(dates):
        if d is None or d.year >= 1990:
            continue
        prev = next((x for x in reversed(dates[:i]) if x and x.year >= 1990),
                    None)
        nxt = next((x for x in dates[i + 1:] if x and x.year >= 1990), None)
        if not (prev and nxt):
            continue
        cand = d.replace(year=prev.year)
        if prev <= cand <= nxt:
            fixed.append((d.date(), cand.date()))
            dates[i] = cand

    legs, sims = [], []
    unresolved, sim_ldg = [], 0

    for r, d in zip(rows, dates):
        if d is None:
            continue
        date = d.strftime('%Y-%m-%d')

        # ── Simulator (Spalte 1 belegt, z.B. „B757/ FRA") ────────────────
        if t(r[1]):
            dev = t(r[1])
            m = re.match(r'^\s*([A-Z0-9]+)\s*/\s*([A-Z]{3})\s*$', dev.upper())
            sim = {'date': date, 'duration_min': mins(r[9]) or None}
            if m:
                sim['code'] = m.group(1)
                sim['place'] = m.group(2)
            else:
                sim['code'] = dev
            if t(r[14]).isdigit():
                sim_ldg += int(t(r[14]))     # Landung auf Sim-Zeile
            sims.append({k: v for k, v in sim.items() if v is not None})
            continue

        # ── Flug (FROM belegt) ───────────────────────────────────────────
        if not t(r[5]):
            continue

        leg = {'date': date, 'from': t(r[5]).upper()[:4],
               'to': t(r[6]).upper()[:4]}

        nr = t(r[2]).upper()
        if nr:
            leg['flight'] = 'DE' + nr

        # Zeiten: OFF/ON sind timedelta ab Mitternacht (UTC laut Quelle)
        off, on = mins(r[7]), mins(r[8]) if r[8] is not None else None
        dep = d + datetime.timedelta(minutes=off)
        leg['dep_iso'] = dep.strftime('%Y-%m-%dT%H:%M:00Z')
        if on is not None:
            arr = d + datetime.timedelta(minutes=on)
            if on <= off:
                arr += datetime.timedelta(days=1)
            leg['arr_iso'] = arr.strftime('%Y-%m-%dT%H:%M:00Z')

        block = mins(r[9])
        if 0 < block < 1200:
            leg['block_min'] = block

        suffix = t(r[3]).upper() or t(r[4]).upper()
        if suffix:
            reg = 'D-AB' + suffix
            leg['reg'] = reg
            ty = types.get(reg)
            if ty and ty[:3] in FAMILY_OK:
                leg['type'] = ty
            else:
                # Registry kennt das Kennzeichen nicht oder widerspricht der
                # 757/767-Familie → Typ bleibt leer, Fall wird gemeldet.
                unresolved.append((date, reg, ty or 'nicht in Registry',
                                   leg['from'], leg['to']))

        if t(r[14]).isdigit() and int(t(r[14])) > 0:
            leg['ldg_day'] = int(t(r[14]))

        cpt, sfo, fo = t(r[10]), t(r[11]), t(r[12])
        if cpt and sfo and not fo:
            leg['role'] = 'FO'
        elif cpt and fo and not sfo:
            leg['role'] = 'SFO'
        elif cpt:
            leg['role'] = 'FO'
        if cpt:
            leg['pic_name'] = cpt

        app = t(r[13])
        if app:
            leg['remarks'] = f'Anflug {app}'

        legs.append(leg)

    legs.sort(key=lambda x: (x['date'], x.get('dep_iso') or ''))
    sims.sort(key=lambda x: x['date'])
    json.dump({'legs': legs, 'sim': sims}, open(dst, 'w'), ensure_ascii=False)

    block = sum(l.get('block_min', 0) for l in legs)
    ldg = sum(l.get('ldg_day', 0) for l in legs)
    smin = sum(s.get('duration_min') or 0 for s in sims)
    print(f'Legs      : {len(legs)}   Block {block // 60}:{block % 60:02d}')
    print(f'Landungen : {ldg} (+ {sim_ldg} auf einer Sim-Zeile = '
          f'{ldg + sim_ldg}, wie Summenzeile)')
    print(f'Sims      : {len(sims)}   {smin // 60}:{smin % 60:02d}')
    print(f'Zeitraum  : {legs[0]["date"]} → {legs[-1]["date"]}')
    print(f'mit Typ   : {sum(1 for l in legs if "type" in l)} / '
          f'{sum(1 for l in legs if "reg" in l)} mit Kennzeichen')
    print(f'Rollen    : FO={sum(1 for l in legs if l.get("role") == "FO")} '
          f'SFO={sum(1 for l in legs if l.get("role") == "SFO")} '
          f'ohne={sum(1 for l in legs if "role" not in l)}')
    print(f'Typ ungeklärt (Kennzeichen bleibt, Typ leer): {len(unresolved)}')
    for u in unresolved:
        print(f'   {u[0]}  {u[1]}  Registry={u[2]}  {u[3]}→{u[4]}')
    print(f'Jahres-Vertipper geheilt: {len(fixed)}')
    for a, b in fixed:
        print(f'   {a} → {b} (durch Nachbarzeilen belegt)')


if __name__ == '__main__':
    main()
