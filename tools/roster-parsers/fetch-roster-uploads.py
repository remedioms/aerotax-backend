#!/usr/bin/env python3
"""Fetch the private roster-PDF verification inbox from production.

Every valid PDF sent to ``/api/user/roster-pdf/<token>/import`` is retained in
the service-key-only ``ax_logbook_upload`` inbox with a dedicated type marker.
The watcher uses this helper to reproduce parser failures and verify successful
imports.  ``--done ID`` deletes only a verified roster row (data minimisation).

  python3 tools/roster-parsers/fetch-roster-uploads.py
  python3 tools/roster-parsers/fetch-roster-uploads.py --save
  python3 tools/roster-parsers/fetch-roster-uploads.py --done 123
"""
import base64
import os
import re
import sys

import psycopg2


ENV = os.path.expanduser('~/Developer/flight-warehouse/.env.nas')
OUT = '/tmp/roster-uploads'
KIND = 'AEROX_ROSTER_PDF_V1'


def _database_url():
    with open(ENV) as fh:
        for line in fh:
            if line.startswith('DATABASE_URL='):
                return line.split('=', 1)[1].strip()
    raise RuntimeError('DATABASE_URL missing')


def main():
    conn = psycopg2.connect(_database_url())
    conn.autocommit = True
    cur = conn.cursor()

    if '--done' in sys.argv:
        upload_id = int(sys.argv[sys.argv.index('--done') + 1])
        cur.execute(
            'delete from public.ax_logbook_upload where id=%s and note=%s',
            (upload_id, KIND))
        print(f'id {upload_id} -> verified and deleted ({cur.rowcount} row)')
        return

    cur.execute(
        'select id, token, name, airline, homebase, filename, sha256, '
        'size_bytes, created_at from public.ax_logbook_upload '
        'where not processed and note=%s order by id', (KIND,))
    rows = cur.fetchall()
    if not rows:
        print('no unverified roster uploads')
        return

    for row in rows:
        token_prefix = (row[1] or '')[:8]
        print(f'#{row[0]} {row[8]:%Y-%m-%d %H:%M}Z  '
              f'{row[2] or "?"} · {row[3] or "?"} · {row[4] or "?"}  '
              f'tok={token_prefix}...  {row[5]} ({row[7] // 1024} KB)  '
              f'sha256 {row[6][:16]}')

    if '--save' in sys.argv:
        os.makedirs(OUT, mode=0o700, exist_ok=True)
        os.chmod(OUT, 0o700)
        for row in rows:
            cur.execute(
                'select data_b64 from public.ax_logbook_upload '
                'where id=%s and note=%s', (row[0], KIND))
            stored = cur.fetchone()
            if not stored:
                continue
            blob = base64.b64decode(stored[0])
            safe = re.sub(r'[^A-Za-z0-9._ -]', '_',
                          f'{row[0]}-{row[5]}')
            path = os.path.join(OUT, safe)
            with open(path, 'wb') as fh:
                fh.write(blob)
            os.chmod(path, 0o600)
            print('  ->', path)


if __name__ == '__main__':
    main()
