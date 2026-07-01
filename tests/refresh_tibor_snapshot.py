"""Tibor 2025 Snapshot Refresher (DEV-only).

Liest Tibors lokale Source-PDFs, ruft hybrid_analyze direkt auf und schreibt
das resultierende `tage_detail` (+classification dict) als JSON-Fixture, das
dann von tests/test_tibor_followme_acceptance.py geprüft wird.

KOSTET API-CREDITS (~1-2€ pro Run wegen 12+ Sonnet-Calls für CAS).
Nur ausführen wenn ein Bug-Fix verifiziert werden soll.

USAGE:
    cd ~/Desktop/aerotax-backend
    export ANTHROPIC_API_KEY=sk-ant-...
    python3 tests/refresh_tibor_snapshot.py

Schreibt:
    tests/fixtures/tibor_aerotax_v11_raw_initial.json  (tage_detail Liste)
    tests/fixtures/tibor_aerotax_classification.json   (Aggregat-Werte)
"""
import json
import os
import sys
import time
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE.parent))

# Sanity: ANTHROPIC_API_KEY muss gesetzt sein
if not os.environ.get('ANTHROPIC_API_KEY'):
    print('FEHLER: ANTHROPIC_API_KEY nicht gesetzt. Aborting.', file=sys.stderr)
    sys.exit(1)

import app  # noqa: E402

# Private Original-PDFs (nicht im Repo) — Override: env AEROTAX_PRIVATE_DOCS_ROOT
TIBOR_BASE = Path(
    os.environ.get('AEROTAX_PRIVATE_DOCS_ROOT') or Path.home() / 'Desktop' / 'Downloads'
) / 'Tibor' / '2025'
TIBOR_DP_DIR = TIBOR_BASE / 'Dienstplan'
LSB_PDF = TIBOR_BASE / 'Lohnsteuerbescheinigung 2025.pdf'
SE_PDF = TIBOR_BASE / '2025 Streckeneinsatzabrechnungen.pdf'

OUT_TAGE_DETAIL = _HERE / 'fixtures' / 'tibor_aerotax_v11_raw_initial.json'
OUT_CLASSIFICATION = _HERE / 'fixtures' / 'tibor_aerotax_classification.json'


def main():
    if not LSB_PDF.exists():
        print(f'FEHLER: {LSB_PDF} fehlt', file=sys.stderr)
        sys.exit(1)
    if not SE_PDF.exists():
        print(f'FEHLER: {SE_PDF} fehlt', file=sys.stderr)
        sys.exit(1)
    if not TIBOR_DP_DIR.exists():
        print(f'FEHLER: {TIBOR_DP_DIR} fehlt', file=sys.stderr)
        sys.exit(1)

    cas_files = sorted(TIBOR_DP_DIR.glob('*.pdf'))
    print(f'CAS-Dateien gefunden: {len(cas_files)}')
    if len(cas_files) < 1:
        print('FEHLER: keine CAS-PDFs gefunden', file=sys.stderr)
        sys.exit(1)

    # Files-Dict im Format das hybrid_analyze erwartet: (bytes, filename) tuples
    files = {
        'lsb': [(LSB_PDF.read_bytes(), LSB_PDF.name)],
        'se':  [(SE_PDF.read_bytes(), SE_PDF.name)],
        'cas': [(p.read_bytes(), p.name) for p in cas_files],
    }
    form = {
        'year': '2025',
        'base': 'Frankfurt (FRA)',
        'km': '28',
        'anfahrt_min': '30',
        'homebase': 'FRA',
    }

    print(f'Pipeline startet (kann 3-5 Min dauern, ~1-2€ API-Costs)...')
    t0 = time.time()
    hr = app.hybrid_analyze(form, files, job_id='tibor-snapshot-refresh')
    elapsed = time.time() - t0
    print(f'Pipeline fertig nach {elapsed:.1f}s')

    cls = (hr or {}).get('classification') or {}
    tage_detail = cls.get('tage_detail') or cls.get('_tage_detail') or []
    if not tage_detail:
        # Fallback: hr['classification'] direkt
        for k in ('_tage_detail', 'tage_detail'):
            if isinstance(cls.get(k), list):
                tage_detail = cls[k]
                break
    print(f'tage_detail: {len(tage_detail)} Einträge')

    OUT_TAGE_DETAIL.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_TAGE_DETAIL, 'w') as f:
        json.dump(tage_detail, f, indent=2, ensure_ascii=False, default=str)
    print(f'geschrieben: {OUT_TAGE_DETAIL}')

    # Aggregat-Werte
    aggregate = {
        'fahr_tage':      cls.get('fahr_tage'),
        'arbeitstage':    cls.get('arbeitstage'),
        'hotel_naechte':  cls.get('hotel_naechte'),
        'reinigungstage': cls.get('reinigungstage'),
        'vma_72_tage':    cls.get('vma_72_tage'),
        'vma_73_tage':    cls.get('vma_73_tage'),
        'vma_74_tage':    cls.get('vma_74_tage'),
        'vma_aus':        cls.get('vma_aus'),
        'unresolved_days': cls.get('unresolved_days', []),
        '_v11_cas_used':  cls.get('_v11_cas_used'),
    }
    with open(OUT_CLASSIFICATION, 'w') as f:
        json.dump(aggregate, f, indent=2, ensure_ascii=False, default=str)
    print(f'geschrieben: {OUT_CLASSIFICATION}')

    # Quick summary
    print('\n--- Aggregat ---')
    for k, v in aggregate.items():
        if k == 'unresolved_days':
            print(f'  {k}: {len(v) if isinstance(v, list) else v}')
        else:
            print(f'  {k}: {v}')

    print('\nFertig. Jetzt testen:')
    print('  pytest tests/test_tibor_followme_acceptance.py -v')


if __name__ == '__main__':
    main()
