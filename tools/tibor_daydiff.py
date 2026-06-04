#!/usr/bin/env python3
"""Tag-für-Tag-Genauigkeits-Oracle: Engine vs. kanzleigeprüfte FollowMe-Referenz.

Vergleicht die Engine-Tageswerte (by_date) mit golden day_classification und
kategorisiert jede Abweichung — deckt auf, ob die Gesamtsumme nur durch
gegenläufige Fehler stimmt.

Nutzung:
  AEROTAX_ALLOW_BOOT_WITHOUT_KEY=1 python3 tools/tibor_daydiff.py            # disclosure OFF
  AEROTAX_ALLOW_BOOT_WITHOUT_KEY=1 AEROTAX_SE_DISCLOSE_VMA=1 python3 tools/tibor_daydiff.py  # live-Modus

Kategorien: MISS (Engine 0, Gold>0) · PHANTOM (Engine>0, Gold 0) ·
            Z72 (Inland>8h fehlt) · RATE (voll_24h vs An/Abreise) · BUCKET.
"""
import os, sys, json
THIS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(THIS)
sys.path.insert(0, ROOT); sys.path.insert(0, THIS)
import tibor_diff  # noqa: E402

FIX = os.path.join(ROOT, 'tests', 'fixtures', 'followme_golden_tibor_2025.json')


def _amt(v):
    try:
        return float(v.get('amount', 0) or 0)
    except Exception:
        return 0.0


def main():
    r, _ = tibor_diff.run_pipeline(2025, 'FRA')
    gold = json.load(open(FIX))['day_classification']
    bd = r.by_date or {}
    cats = {'MISS': [], 'PHANTOM': [], 'Z72': [], 'RATE': [], 'BUCKET': []}
    for d in sorted(set(gold) | set(x for x in bd if x.startswith('2025'))):
        gk = gold.get(d, {}); ek = bd.get(d, {})
        gkl = gk.get('klass', '—'); gb = float(gk.get('betrag', 0) or 0)
        if gkl in ('—', 'NO_VMA'):
            gb = 0; gkl = '—'
        ekl = (ek.get('klass') or 'none'); eb = _amt(ek)
        if abs(gb - eb) < 0.5 and (gb > 0) == (eb > 0):
            continue
        row = (d, gkl, gb, gk.get('dauer_h'), ekl, eb, round(eb - gb))
        if gb > 0 and eb == 0:
            (cats['Z72'] if gkl == 'Z72' else cats['MISS']).append(row)
        elif eb > 0 and gb == 0:
            cats['PHANTOM'].append(row)
        elif gkl == 'Z72' or (gkl.startswith('Z7') and ekl.startswith('Z7')):
            cats['RATE'].append(row)
        else:
            cats['BUCKET'].append(row)
    disclose = os.environ.get('AEROTAX_SE_DISCLOSE_VMA', '') in ('1', 'true', 'on')
    print('═══ Tag-für-Tag: Engine vs FollowMe-Golden (Tibor 2025, disclosure=%s) ═══'
          % ('ON' if disclose else 'OFF'))
    net = 0
    for c, L in cats.items():
        s = sum(x[6] for x in L); net += s
        print('  %-8s %3d Tage  Σ%+5.0f €' % (c, len(L), s))
    print('  ' + '-' * 32)
    print('  NETTO Σ%+.0f €  (Vorsicht: gegenläufige Fehler können sich aufheben)' % net)
    if '--days' in sys.argv:
        for c, L in cats.items():
            if not L:
                continue
            print('\n### %s' % c)
            for x in L:
                print('   %s gold %s/%.0f(dh=%s) eng %s/%.0f Δ%+.0f'
                      % (x[0], x[1], x[2], round(x[3], 1) if x[3] else x[3], x[4], x[5], x[6]))


if __name__ == '__main__':
    main()
