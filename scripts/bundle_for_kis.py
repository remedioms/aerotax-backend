#!/usr/bin/env python3
"""Bündelt alle relevanten AeroTax-Files in ein einziges Text-File für KI-Briefing.
Output: ~/Desktop/AeroTax_Bundle.txt

Reihenfolge bewusst gewählt:
1. FILES.md (Briefing/Architektur — KI sollte zuerst das hier lesen)
2. CLAUDE.md (Arbeitsprinzipien)
3. referenz_faelle.txt (Wissens-Buch — Domain-Wissen für Klassifikation)
4. requirements.txt + Procfile (Setup)
5. tests/test_calculation.py (verständlichster Code → Domain-Logik versteht man hier zuerst)
6. bmf_data.py (Datentabelle)
7. app.py (Hauptcode — am Ende weil größtes File)
8. index.html (Frontend)
9. supabase_schema.sql (DB-Schema)
"""
import os
from datetime import datetime

REPO = os.path.expanduser('~/Desktop/aerotax-backend')
SITE = os.path.expanduser('~/Desktop/site')
OUT = os.path.expanduser('~/Desktop/AeroTax_Bundle.txt')

# (label, abs_path, language_hint)
FILES = [
    # ── BRIEFINGS (zuerst lesen) ──
    ('FILES.md',                 f'{REPO}/FILES.md',                  'markdown'),
    ('RECHENWEG.md',             f'{REPO}/RECHENWEG.md',              'markdown'),
    ('CLAUDE.md',                f'{REPO}/CLAUDE.md',                 'markdown'),

    # ── DOMAIN-WISSEN (in den Opus-Prompt geladen) ──
    ('referenz_faelle.txt',      f'{REPO}/referenz_faelle.txt',       'text'),
    ('referenz_easa.txt (HISTORISCH — wird NICHT mehr geladen)',
                                  f'{REPO}/referenz_easa.txt',         'text'),

    # ── SETUP / CONFIG ──
    ('requirements.txt',         f'{REPO}/requirements.txt',          'text'),
    ('Procfile (Render Deploy)', f'{REPO}/Procfile',                  'text'),
    ('Dockerfile (HISTORISCH — Fly.io-Migration, nicht aktiv)',
                                  f'{REPO}/Dockerfile',                'dockerfile'),
    ('fly.toml (HISTORISCH — Fly.io-Migration, nicht aktiv)',
                                  f'{REPO}/fly.toml',                  'toml'),
    ('.gitignore',               f'{REPO}/.gitignore',                'text'),

    # ── DATEN ──
    ('bmf_data.py (BMF-Auslandsspesen-Tabelle)',
                                  f'{REPO}/bmf_data.py',               'python'),
    ('qa_seed.json (Forum-Seed, inaktives Feature)',
                                  f'{REPO}/qa_seed.json',              'json'),
    ('supabase_schema.sql',      f'{REPO}/supabase_schema.sql',       'sql'),

    # ── TESTS ──
    ('tests/test_calculation.py',f'{REPO}/tests/test_calculation.py', 'python'),

    # ── HAUPTCODE BACKEND ──
    ('app.py (HAUPT-BACKEND, ~7700 Zeilen)',
                                  f'{REPO}/app.py',                    'python'),

    # ── FRONTEND ──
    ('frontend/index.html (statisches Single-Page, kein Build)',
                                  f'{SITE}/index.html',                'html'),

    # ── HELPER-SKRIPTE ──
    ('scripts/generate_action_guide.py (Action-Guide-PDF-Generator)',
                                  f'{REPO}/scripts/generate_action_guide.py', 'python'),
    ('scripts/bundle_for_kis.py (DIESES Skript — Selbstreferenz)',
                                  f'{REPO}/scripts/bundle_for_kis.py', 'python'),
]


def make_bundle():
    parts = []
    # Header
    parts.append('═' * 80)
    parts.append('  AEROTAX — KOMPLETT-BUNDLE FÜR KI-MITARBEITER')
    parts.append('═' * 80)
    parts.append('')
    parts.append(f'Generiert: {datetime.now().strftime("%Y-%m-%d %H:%M")}')
    parts.append('')
    parts.append('Dieses File enthält alle relevanten Source-Files des AeroTax-Projekts.')
    parts.append('Beginne mit FILES.md (gleich darunter) — das ist das Architektur-Briefing.')
    parts.append('')
    parts.append('Konvention: Jedes File ist abgegrenzt durch eine Zeile mit "═══ FILE: <pfad> ═══".')
    parts.append('Such darin um schnell zu navigieren.')
    parts.append('')
    parts.append('Inhalts-Übersicht (in dieser Reihenfolge):')
    for i, (label, _, _) in enumerate(FILES, 1):
        parts.append(f'  {i:>2}. {label}')
    parts.append('')
    parts.append('═' * 80)
    parts.append('')

    total_lines = 0
    skipped = []

    for label, path, lang in FILES:
        if not os.path.exists(path):
            skipped.append(label)
            continue
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            skipped.append(f'{label} (Fehler: {e})')
            continue

        line_count = content.count('\n') + 1
        total_lines += line_count
        size_kb = len(content) / 1024

        parts.append('')
        parts.append('═' * 80)
        parts.append(f'  ═══ FILE: {label}')
        parts.append(f'  Pfad: {path}')
        parts.append(f'  Größe: {size_kb:.1f} KB · {line_count} Zeilen · Typ: {lang}')
        parts.append('═' * 80)
        parts.append('')
        parts.append(content)

    # Footer
    parts.append('')
    parts.append('═' * 80)
    parts.append('  ENDE BUNDLE')
    parts.append('═' * 80)
    parts.append(f'  Gesamt: {total_lines} Zeilen über {len(FILES) - len(skipped)} Files')
    if skipped:
        parts.append(f'  Übersprungen: {", ".join(skipped)}')
    parts.append('')

    bundle = '\n'.join(parts)

    with open(OUT, 'w', encoding='utf-8') as f:
        f.write(bundle)

    out_size = os.path.getsize(OUT) / 1024
    print(f'Bundle erstellt: {OUT}')
    print(f'  Größe: {out_size:.1f} KB')
    print(f'  Zeilen: {total_lines}')
    print(f'  Files:  {len(FILES) - len(skipped)} eingebunden')
    if skipped:
        print(f'  Übersprungen: {skipped}')


if __name__ == '__main__':
    make_bundle()
