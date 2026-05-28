#!/usr/bin/env python3
"""Liest _classifier_v2_audit + classification_v2 aus einem Live-Job.

Usage:
  python3 scripts/read_v2_audit.py <token-or-job-id>

Erkennt automatisch ob ein Token (12+ Zeichen, alphanumerisch ohne -)
oder Job-ID (UUID-Form) gegeben wurde und holt das Result via
/api/session/<token> oder /api/job/<job_id>.
"""
import argparse
import json
import sys
import urllib.request


BACKEND = 'https://aerotax-backend-443401186607.europe-west3.run.app'


def _fetch(path: str) -> dict:
    url = f'{BACKEND}{path}'
    print(f'GET {url}')
    req = urllib.request.Request(url, headers={'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode('utf-8'))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('id', help='Token oder Job-ID')
    p.add_argument('--full', action='store_true', help='Komplettes V2-Audit')
    p.add_argument('--days', action='store_true', help='Erste 30 tage_detail-Einträge')
    args = p.parse_args()

    ident = args.id.strip()
    # Heuristik: UUID-Format vs Token (8-char alphanumeric)
    if '-' in ident and len(ident) >= 30:
        data = _fetch(f'/api/job/{ident}')
    else:
        data = _fetch(f'/api/session/{ident}')

    rd = data.get('result_data') or {}
    audit = rd.get('_classifier_v2_audit')
    cls_v2 = rd.get('classification_v2')
    cls_legacy = rd.get('classification') or {}

    if not audit:
        print('\n[!] Kein _classifier_v2_audit im result. Backend hat V2-Branch noch nicht oder error.')
        # Diagnostik
        for k in ('canonical_state', 'errors', 'user_message'):
            if k in data:
                print(f'  {k}: {data[k]}')
        # Fallback: Cloud-Run-Logs nach dem Audit-Print durchsuchen
        job_id = data.get('job_id') or ''
        if job_id:
            print(f'\n[fallback] Suche V2-Print in Cloud-Run-Logs für job_id={job_id[:8]}…')
            import subprocess
            try:
                out = subprocess.check_output([
                    'gcloud', 'run', 'services', 'logs', 'read', 'aerotax-backend',
                    '--region', 'europe-west3', '--limit', '500',
                ], text=True, stderr=subprocess.DEVNULL)
                for line in out.split('\n'):
                    if '[classifier_v2]' in line:
                        print(' ', line.strip())
            except Exception as e:
                print(f'  (gcloud logs unavailable: {e})')
        return 1

    print('\n=== Classifier V2 Audit ===')
    print(f'Flag aktiv:        {audit.get("flag_active", False)}')
    print(f'V2 tours:          {audit.get("tours_count")}')
    print(f'V2 fahrtage:       {audit.get("fahrtage")}')
    print(f'V2 arbeitstage:    {audit.get("arbeitstage")}')
    print(f'V2 hotel:          {audit.get("hotel_naechte")}')
    print(f'V2 reinigung:      {audit.get("reinigungstage")}')
    print(f'V2 Z72: {audit.get("z72_eur"):>8.2f}€')
    print(f'V2 Z73: {audit.get("z73_eur"):>8.2f}€')
    print(f'V2 Z74: {audit.get("z74_eur"):>8.2f}€')
    print(f'V2 Z76: {audit.get("z76_eur"):>8.2f}€')

    print('\n=== Diff vs Legacy ===')
    print(f'Δ Z76:        {audit.get("delta_z76"):+.2f}€')
    print(f'Δ Hotel:      {audit.get("delta_hotel"):+d}')
    print(f'Δ Fahrtage:   {audit.get("delta_fahrtage"):+d}')
    print(f'Δ Arbeitstage:{audit.get("delta_arbeitstage"):+d}')

    print(f'\nTag-Diffs: {audit.get("tag_diffs_count")} insgesamt')
    for d in (audit.get('tag_diffs') or [])[:20]:
        leg = d.get('legacy', {})
        v2 = d.get('v2', {})
        print(f'  {d["datum"]}  legacy={leg.get("klass"):>6} €{leg.get("eur"):>6.2f}'
              f'  ↔  v2={v2.get("klass"):>6} €{v2.get("eur"):>6.2f}  ({v2.get("role")}, {v2.get("country")})')

    if cls_v2:
        print('\n=== classification_v2 (Flag aktiv) ===')
        print(f'engine:        {cls_v2.get("engine")} {cls_v2.get("engine_version")}')
        print(f'vma_aus:       {cls_v2.get("vma_aus"):>8.2f}€')
        print(f'vma_in:        {cls_v2.get("vma_in"):>8.2f}€')
        if cls_v2.get('warnings'):
            print(f'warnings:      {len(cls_v2["warnings"])} (first 3: {cls_v2["warnings"][:3]})')
    else:
        print('\n[!] classification_v2 NICHT im result → AEROTAX_V2_CLASSIFIER ENV ist OFF')

    if args.days:
        td = (cls_v2 or {}).get('tage_detail') or audit.get('tag_diffs') or []
        print(f'\n=== Tage_detail (first 30 of {len(td)}) ===')
        for e in td[:30]:
            print(f'  {e.get("datum"):11} klass={e.get("klass"):>7} eur={e.get("eur",0):>6.2f}'
                  f' role={e.get("role","-"):>14} country={e.get("country") or "-"}')

    if args.full:
        print('\n=== Full audit JSON ===')
        print(json.dumps(audit, indent=2, ensure_ascii=False)[:5000])

    return 0


if __name__ == '__main__':
    sys.exit(main())
