#!/usr/bin/env python3
"""EASA-FCL.050-Logbuch-PDF (OffBlock-Export) → AeroX-Import-Legs.

  python3 parse_fcl050_v2.py <quelle.pdf> <ziel.json>

Neuaufbau von /tmp/parse_fcl050.py. Statt Textzeilen zu raten werden die
ECHTEN Tabellenlinien des PDF benutzt: vertikale Kanten = Spaltengrenzen,
horizontale Kanten = Zeilenbänder. A- und B-Seite teilen sich dasselbe
y-Raster, die B-Seite wird über die y-Bänder der A-Seite gelesen.

A-Seite: DATE | DEP PLACE/TIME | ARR PLACE/TIME | MAKE,MODEL | REGISTRATION
         | SE | ME | MULTI-PILOT | TOTAL TIME OF FLIGHT | NAME PIC
         | TAKEOFFS DAY/NIGHT | LANDINGS DAY/NIGHT
B-Seite: NIGHT | IFR | PIC | CO-PILOT | DUAL | INSTRUCTOR
         | STD DATE/TYPE/TOTAL TIME OF SESSION | REMARKS

GOTCHA: Simulator-Zeilen (Typ S###, Ort==Ort) stehen SOWOHL in der
Flugtabelle ALS AUCH in der STD-Sektion — der Export zählt sie dadurch in
seine eigene Flug-Endsumme mit hinein. Hier: aus den Legs raus, Sims allein
aus der STD-Sektion. Die Endsummen-Prüfung rechnet die Doppelzählung heraus.
"""
import json
import re
import sys

import pdfplumber

A_COLS = {'date': (48, 97), 'from': (97, 139), 'dep': (139, 180),
          'to': (180, 222), 'arr': (222, 263), 'type': (263, 324),
          'reg': (324, 384), 'se': (384, 420), 'me': (420, 456),
          'mp': (456, 491), 'total': (491, 530), 'name': (530, 637),
          'to_day': (637, 666), 'to_night': (666, 694),
          'ldg_day': (694, 722), 'ldg_night': (722, 751)}
B_COLS = {'night': (79, 122), 'ifr': (122, 166), 'pic': (166, 215),
          'copilot': (215, 264), 'dual': (264, 314), 'instr': (314, 363),
          'std_date': (363, 425), 'std_type': (425, 486),
          'std_dur': (486, 548), 'remarks': (548, 736)}

RE_DATE = re.compile(r'^(\d{2})/(\d{2})/(\d{2})$')
RE_TIME = re.compile(r'^(\d{1,5}):(\d{2})$')   # Endsummen haben 4-stellige h
RE_SIM_TYPE = re.compile(r'^S\d{2,3}$')


def bands(page):
    """Horizontale Kanten → Zeilenbänder (Duplikate < 3pt zusammenfassen)."""
    hs = sorted({round(e['top'], 1) for e in page.horizontal_edges})
    out = []
    for h in hs:
        if not out or h - out[-1] > 3:
            out.append(h)
    return out


def cells(words, top, bot, cols):
    """Wörter eines y-Bandes den Spalten zuordnen."""
    out = {k: [] for k in cols}
    for w in words:
        if not (top <= w['top'] < bot):
            continue
        for k, (x0, x1) in cols.items():
            if x0 <= w['x0'] < x1:
                out[k].append(w['text'])
                break
    return {k: ' '.join(v).strip() for k, v in out.items()}


def to_min(txt):
    m = RE_TIME.match((txt or '').strip())
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def to_int(txt):
    t = (txt or '').strip()
    return int(t) if t.isdigit() and int(t) > 0 else 0


def iso_date(txt):
    m = RE_DATE.match((txt or '').strip())
    if not m:
        return None
    d, mo, y = m.group(1), m.group(2), int(m.group(3))
    return f'{2000 + y if y < 70 else 1900 + y}-{mo}-{d}'


def main():
    src, dst = sys.argv[1], sys.argv[2]
    legs, sims = [], []
    page_totals = []          # („TOTAL TIME"-Zeile je Seite) zur Kontrolle

    with pdfplumber.open(src) as pdf:
        pages = pdf.pages
        # A-Seiten sind die geraden Indizes ab 1 (Seite 2 = 1A, Seite 4 = 2A)
        for i in range(1, len(pages) - 1, 2):
            pa, pb = pages[i], pages[i + 1]
            wa, wb = pa.extract_words(), pb.extract_words()
            ba = bands(pa)

            for j in range(len(ba) - 1):
                top, bot = ba[j], ba[j + 1]
                if bot - top < 10:
                    continue
                A = cells(wa, top, bot, A_COLS)
                date = iso_date(A['date'])
                if not date:
                    # Summenzeilen tragen ihr Label quer über mehrere Spalten
                    # („TOTAL TIME"), nicht in der Datumsspalte.
                    joined = ' '.join(A.values())
                    if 'TOTAL' in joined:
                        page_totals.append((pa.page_number, joined, A))
                    continue

                B = cells(wb, top, bot, B_COLS)
                typ = A['type'].strip().upper()
                block = to_min(A['total'])

                # Simulator: steht doppelt (Flugtabelle + STD) → hier raus
                if RE_SIM_TYPE.match(typ) and A['from'] == A['to']:
                    continue

                leg = {'date': date}
                if A['from']:
                    leg['from'] = A['from'].upper()[:4]
                if A['to']:
                    leg['to'] = A['to'].upper()[:4]

                dep, arr = to_min(A['dep']), to_min(A['arr'])
                if dep is not None:
                    leg['dep_iso'] = (f'{date}T{dep // 60:02d}:'
                                      f'{dep % 60:02d}:00Z')
                    if arr is not None:
                        import datetime as _dt
                        d0 = _dt.date.fromisoformat(date)
                        if arr <= dep:
                            d0 += _dt.timedelta(days=1)
                        leg['arr_iso'] = (f'{d0.isoformat()}T{arr // 60:02d}:'
                                          f'{arr % 60:02d}:00Z')
                if block and 0 < block < 1200:
                    leg['block_min'] = block
                if typ:
                    leg['type'] = typ
                reg = A['reg'].replace(' ', '').upper()
                if reg:
                    leg['reg'] = reg

                for k in ('to_day', 'to_night', 'ldg_day', 'ldg_night'):
                    v = to_int(A[k])
                    if v:
                        leg[k] = v

                night = to_min(B['night'])
                if night:
                    leg['night_min'] = night

                # Rolle: die belegte Funktions-Spalte der B-Seite
                for col, role in (('pic', 'PIC'), ('copilot', 'FO'),
                                  ('dual', 'DUAL'), ('instr', 'FI')):
                    if to_min(B[col]):
                        leg['role'] = role
                        break

                # NAME-Spalte führt die ganze Crew in wechselnder Reihenfolge
                # → NICHT als pic_name verwertbar, aber als Notiz ehrlich.
                crew = re.findall(r'[A-ZÄÖÜ][A-ZÄÖÜ\'\-\. ]+,\s*[A-ZÄÖÜ]\.',
                                  A['name'])
                if crew:
                    leg['remarks'] = 'Crew: ' + ', '.join(
                        c.strip() for c in crew[:4])
                legs.append(leg)

            # ── STD-Sektion der B-Seite = die echten Sim-Sessions ─────────
            for j in range(len(bands(pb)) - 1):
                bb_ = bands(pb)
                top, bot = bb_[j], bb_[j + 1]
                if bot - top < 8:
                    continue
                B = cells(wb, top, bot, B_COLS)
                sdate = iso_date(B['std_date'])
                if not sdate:
                    continue
                dur = to_min(B['std_dur'])
                sim = {'date': sdate, 'duration_min': dur}
                if B['std_type']:
                    sim['code'] = B['std_type'].strip().upper()
                sims.append({k: v for k, v in sim.items() if v is not None})

    legs.sort(key=lambda x: (x['date'], x.get('dep_iso') or ''))
    sims.sort(key=lambda x: x['date'])

    # Der Export mischt IATA- und ICAO-Schreibweise für DENSELBEN Flieger
    # („339" neben „A339") — das zerlegt die Muster-Statistik in zwei Kacheln.
    # Vereinheitlichen NUR, wenn die A-Form im selben Logbuch belegt ist;
    # sonst bliebe es Raten (aus „737" darf nie „A737" werden).
    present = {l.get('type') for l in legs if l.get('type')}
    renamed = {}
    for l in legs:
        t_ = l.get('type') or ''
        if t_.isdigit() and 'A' + t_ in present:
            renamed[t_] = 'A' + t_
            l['type'] = 'A' + t_

    from legkeys import dedupe_keys
    collisions = dedupe_keys(legs)
    json.dump({'legs': legs, 'sim': sims}, open(dst, 'w'), ensure_ascii=False)

    block = sum(l.get('block_min', 0) for l in legs)
    ldg = sum(l.get('ldg_day', 0) + l.get('ldg_night', 0) for l in legs)
    to_ = sum(l.get('to_day', 0) + l.get('to_night', 0) for l in legs)
    smin = sum(s.get('duration_min') or 0 for s in sims)
    print(f'Legs      : {len(legs)}   Block {block // 60}:{block % 60:02d}')
    print(f'Starts    : {to_}   Landungen: {ldg}')
    print(f'Sims      : {len(sims)}   {smin // 60}:{smin % 60:02d}')
    print(f'Zeitraum  : {legs[0]["date"]} → {legs[-1]["date"]}')
    print(f'mit Typ   : {sum(1 for l in legs if "type" in l)}  '
          f'mit Reg: {sum(1 for l in legs if "reg" in l)}  '
          f'mit Rolle: {sum(1 for l in legs if "role" in l)}')
    if renamed:
        print(f'Typ vereinheitlicht: '
              f'{", ".join(f"{a}->{b}" for a, b in renamed.items())}')
    print(f'Key-Kollisionen aufgeloest: {len(collisions)}')
    for c in collisions:
        print(f'   {c[0]}  {c[1]}  {c[2]}  dep {c[3]}  -> Suffix ({c[4]})')
    # ── Abgleich gegen die Endsumme, die das PDF selbst ausweist ─────────
    final = [t for t in page_totals if 'TOTAL TIME' in t[1]]
    if final:
        pn, _, last = final[-1]
        tot = to_min(last['total'])
        pto = to_int(last['to_day']) + to_int(last['to_night'])
        pldg = to_int(last['ldg_day']) + to_int(last['ldg_night'])
        print(f'\n── Abgleich mit der PDF-Endsumme (Seite {pn}) ──')
        print(f'  PDF TOTAL TIME     : {last["total"]}   '
              f'TO {last["to_day"]}/{last["to_night"]}   '
              f'LDG {last["ldg_day"]}/{last["ldg_night"]}')
        print(f'  davon STD/Simulator: {smin // 60}:{smin % 60:02d}   '
              '(der Export zählt Sim-Zeilen doppelt in die Flugsumme)')
        rest = tot - smin if tot else None
        print(f'  ⇒ echte Flugzeit    : {rest // 60}:{rest % 60:02d}   '
              f'| geparst {block // 60}:{block % 60:02d}   '
              f'{"OK" if rest == block else "ABWEICHUNG"}')
        print(f'  ⇒ Starts            : {pto}   | geparst {to_}   '
              f'{"OK" if pto == to_ else "ABWEICHUNG"}')
        print(f'  ⇒ Landungen         : {pldg}   | geparst {ldg}   '
              f'{"OK" if pldg == ldg else "ABWEICHUNG"}')
        if not (rest == block and pto == to_ and pldg == ldg):
            sys.exit(1)


if __name__ == '__main__':
    main()
