#!/usr/bin/env python3
"""Flugbuch-Upload-Store abrufen (SB ax_logbook_upload).

Neue App-Uploads landen seit 07-24 direkt in Supabase — keine Mail-Anhänge
mehr nötig. Dieses Skript listet unverarbeitete Uploads und speichert sie
nach /tmp/logbook-uploads/<id>-<name>-<filename>.

  python3 ~/aerox-oracle-prep/fetch-logbook-uploads.py            # listen
  python3 ~/aerox-oracle-prep/fetch-logbook-uploads.py --save     # + Dateien
  python3 ~/aerox-oracle-prep/fetch-logbook-uploads.py --done 3   # id als verarbeitet markieren

DB-Zugang: flight-warehouse .env.nas (direkter Prod-Postgres-Pooler).
Nach dem Einspielen ins Flugbuch (ax_logbook_import) die Zeile mit --done
markieren — Aufräumen (delete alter processed-Zeilen) bewusst manuell.
"""
import base64
import os
import re
import sys

import psycopg2

ENV = os.path.expanduser('~/Developer/flight-warehouse/.env.nas')
URL = [l.split('=', 1)[1].strip() for l in open(ENV)
       if l.startswith('DATABASE_URL=')][0]
OUT = '/tmp/logbook-uploads'


def main():
    conn = psycopg2.connect(URL)
    conn.autocommit = True
    cur = conn.cursor()
    if '--done' in sys.argv:
        uid = int(sys.argv[sys.argv.index('--done') + 1])
        cur.execute('update public.ax_logbook_upload set processed=true '
                    'where id=%s', (uid,))
        print(f'id {uid} → processed ({cur.rowcount} Zeile)')
        return
    cur.execute('select id, token, name, airline, homebase, filename, '
                'sha256, size_bytes, note, created_at '
                'from public.ax_logbook_upload where not processed '
                'order by id')
    rows = cur.fetchall()
    if not rows:
        print('keine unverarbeiteten Uploads')
        return
    for r in rows:
        print(f'#{r[0]} {r[9]:%Y-%m-%d %H:%M}Z  {r[2] or "?"} · {r[3] or "?"}'
              f' · {r[4] or "?"}  tok={r[1]}  {r[5]} ({r[7] // 1024} KB)'
              f'  sha256 {r[6][:16]}' + (f'  Notiz: {r[8]}' if r[8] else ''))
    if '--save' in sys.argv:
        os.makedirs(OUT, exist_ok=True)
        for r in rows:
            cur.execute('select data_b64 from public.ax_logbook_upload '
                        'where id=%s', (r[0],))
            blob = base64.b64decode(cur.fetchone()[0])
            safe = re.sub(r'[^A-Za-z0-9._ -]', '_',
                          f'{r[0]}-{r[2] or "unbekannt"}-{r[5]}')
            path = os.path.join(OUT, safe)
            with open(path, 'wb') as f:
                f.write(blob)
            print('  →', path)


if __name__ == '__main__':
    main()
