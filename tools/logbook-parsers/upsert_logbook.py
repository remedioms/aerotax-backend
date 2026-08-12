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


def _database_url():
    with open(ENV, encoding='utf-8') as fh:
        return [line.split('=', 1)[1].strip() for line in fh
                if line.startswith('DATABASE_URL=')][0]


def _args(argv):
    args = list(argv[1:])
    upload_id = None
    if '--upload-id' in args:
        i = args.index('--upload-id')
        try:
            upload_id = int(args[i + 1])
        except (IndexError, ValueError):
            raise SystemExit('--upload-id braucht eine positive numerische Upload-ID')
        del args[i:i + 2]
        if upload_id < 1:
            raise SystemExit('--upload-id muss positiv sein')
    if len(args) not in (3, 4):
        raise SystemExit('usage: upsert_logbook.py parsed.json token label [meta-json] [--upload-id ID]')
    return args[0], args[1], args[2], (json.loads(args[3]) if len(args) == 4 else {}), upload_id


def _complete_upload_after_verified(cur, upload_id, token):
    """Atomically terminalize an operator upload and enqueue exactly one push."""
    cur.execute('select token, status from public.ax_logbook_upload where id=%s for update',
                (upload_id,))
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f'Upload #{upload_id} nicht gefunden')
    if row[0] != token:
        raise RuntimeError(f'Upload #{upload_id} gehört nicht zum angegebenen Token')
    if row[1] == 'completed':
        return False
    cur.execute("""
        update public.ax_logbook_upload
        set processed=true, status='completed', completed_at=now(),
            push_enqueued_at=now()
        where id=%s and token=%s and status <> 'completed'
        returning id
    """, (upload_id, token))
    if not cur.fetchone():
        return False
    payload = json.dumps({
        'title': 'Flugbuch-Import fertig',
        'body': 'Deine importierten Flüge und Stunden sind jetzt im Flugbuch.',
        'data': {'type': 'logbook_import_completed',
                 'localization_key': 'logbook_import_completed', 'job_id': upload_id,
                 'deep_link': 'aerox://more/logbook'},
    }, ensure_ascii=False)
    cur.execute('select * from public.enqueue_push_outbox(%s, %s, %s::jsonb)',
                (f'logbook-import-completed:{upload_id}', token, payload))
    return True


def main():
    path, token, label, extra, upload_id = _args(sys.argv)

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

    conn = psycopg2.connect(_database_url())
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute('SET default_transaction_read_only = off')
    conn.autocommit = False
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
    if not ok:
        conn.rollback()
        raise RuntimeError('Rücklese-Verifikation von ax_logbook_import fehlgeschlagen')
    completed = (_complete_upload_after_verified(cur, upload_id, token)
                 if upload_id is not None else False)
    conn.commit()
    print(f'{token}  {label}')
    print(f'  gespeichert: legs={n_legs} sim={n_sim} ({size // 1024} KB) '
          f'{"OK" if ok else "ABWEICHUNG!"}')
    print(f'  meta: {json.dumps(m, ensure_ascii=False)}')
    if upload_id is not None:
        print(f'  upload #{upload_id}: {"completed + push outbox" if completed else "bereits completed"}')


if __name__ == '__main__':
    main()
