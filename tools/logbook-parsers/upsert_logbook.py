#!/usr/bin/env python3
"""Parser-JSON → SB `ax_logbook_import` upserten (Token-PK).

  python3 upsert_logbook.py <parsed.json> <token> <filename-label> [meta-json]

Direkter Prod-Postgres via flight-warehouse DATABASE_URL. Re-Import
desselben Users = Upsert derselben Zeile (Overlay bleibt oberste Instanz).
"""
import json
import os
import sys

import psycopg2

ENV = os.path.expanduser('~/Developer/flight-warehouse/.env.nas')
URL = [l.split('=', 1)[1].strip() for l in open(ENV)
       if l.startswith('DATABASE_URL=')][0]


def main():
    path, token, label = sys.argv[1], sys.argv[2], sys.argv[3]
    extra = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {}

    data = json.load(open(path))
    legs, sims = data['legs'], data.get('sim', [])
    block = sum(l.get('block_min', 0) for l in legs)
    ldg = sum(l.get('ldg_day', 0) + l.get('ldg_night', 0) for l in legs)

    meta = {
        'source': label,
        'legs': len(legs),
        'block_min': block,
        'landings': ldg,
        'sim_sessions': len(sims),
        'sim_min': sum(s.get('duration_min') or 0 for s in sims),
        'first_date': legs[0]['date'] if legs else None,
        'last_date': legs[-1]['date'] if legs else None,
    }
    meta.update(extra)

    conn = psycopg2.connect(URL)
    conn.autocommit = True
    cur = conn.cursor()
    # GOTCHA Supavisor-Pooler (:6543 in .env.nas): liefert Sessions mit
    # `default_transaction_read_only=on` → Writes brechen sonst zufaellig mit
    # ReadOnlySqlTransaction ab (traf Upload #21 mitten im Import).
    cur.execute('SET default_transaction_read_only = off')
    cur.execute("""
        insert into public.ax_logbook_import
            (token, filename, imported_at, legs, sim, meta)
        values (%s, %s, now(), %s::jsonb, %s::jsonb, %s::jsonb)
        on conflict (token) do update set
            filename    = excluded.filename,
            imported_at = excluded.imported_at,
            legs        = excluded.legs,
            sim         = excluded.sim,
            meta        = excluded.meta
    """, (token, label, json.dumps(legs, ensure_ascii=False),
          json.dumps(sims, ensure_ascii=False),
          json.dumps(meta, ensure_ascii=False)))

    # Rücklesen als Verifikation (nicht dem INSERT glauben)
    cur.execute('select jsonb_array_length(legs), '
                'jsonb_array_length(coalesce(sim, \'[]\'::jsonb)), '
                'octet_length(legs::text), meta '
                'from public.ax_logbook_import where token=%s', (token,))
    n_legs, n_sim, size, m = cur.fetchone()
    ok = (n_legs == len(legs) and n_sim == len(sims))
    print(f'{token}  {label}')
    print(f'  gespeichert: legs={n_legs} sim={n_sim} ({size // 1024} KB) '
          f'{"OK" if ok else "ABWEICHUNG!"}')
    print(f'  meta: {json.dumps(m, ensure_ascii=False)}')
    if not ok:
        sys.exit(1)


if __name__ == '__main__':
    main()
