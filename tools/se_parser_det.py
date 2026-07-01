"""Deterministischer Parser für die Lufthansa Streckeneinsatz-Abrechnung (SE).

Die SE-Abrechnung ist die FINANZAMT-relevante Quelle für steuerfreie Auslands-
Spesen — und damit FollowMe's Wahrheit für Z76 (Auslands-VMA). AeroTax koppelt
Z76 bisher fälschlich ans CAS-Routing; korrekt ist die SE-stfrei-Ort-Spalte.

Spaltengenaues Parsen über x-Koordinaten (extract_words), NICHT Regex-Raten —
der kollabierte Text ist mehrdeutig (z.B. '33,60 FRA 8 CPH'). Spalten (x0):
  Datum ~71 | Ab ~120 | An ~149 | Spesen-Betrag ~177 | Spesen-Ort ~236 |
  Zwölftel ~269 | stfrei-Betrag ~311 | stfrei-Ort ~340-378 | Steuer ~382 | ...
Storno: Zeile endet auf 'X' mit negativem Betrag → markiert, Korrekturzeile folgt.

Kein Sonnet, kein Netz. Output: SE-Rows kompatibel zum Builder:
  {datum, stfrei_ort, stfrei_inland, stfrei_betrag, storno}.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from airport_tz import airport_country as _atz_country
except Exception:
    _atz_country = None

_DATE = re.compile(r'^\d{2}\.\d{2}\.\d{4}$')

# x-Band der stfrei-Ort-Spalte (aus Header-Analyse: stfrei-Ort-IATA bei x0≈340-378)
STFREI_ORT_X = (335.0, 378.0)
STFREI_BETRAG_X = (300.0, 335.0)
# Zwölftel-Spalte (Header x0≈269-295): 12 = Volltag (voll_24h), <12 = An-/Abreise
ZWOELFTEL_X = (265.0, 300.0)


def _is_inland(iata):
    if not iata or not _atz_country:
        return None
    iso = _atz_country(iata.upper())
    return (iso == 'DE') if iso else None


def _group_lines(words, tol=3.0):
    """Wörter nach y (top) in Zeilen gruppieren."""
    lines = []
    for w in sorted(words, key=lambda w: (round(w['top']), w['x0'])):
        if lines and abs(w['top'] - lines[-1][0]) <= tol:
            lines[-1][1].append(w)
        else:
            lines.append([w['top'], [w]])
    return [sorted(ws, key=lambda w: w['x0']) for _, ws in lines]


def parse_se_pdf(pdf_path):
    import pdfplumber
    rows = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for line in _group_lines(page.extract_words()):
                if not line or not _DATE.match(line[0]['text']):
                    continue
                dd, mm, yyyy = line[0]['text'].split('.')
                datum = f'{yyyy}-{mm}-{dd}'
                texts = [w['text'] for w in line]
                # Storno: Zeile endet auf 'X' + negativer Betrag
                neg = any('-' in t and re.search(r'\d,\d{2}-', t) for t in texts)
                if texts[-1] == 'X' and neg:
                    rows.append({'datum': datum, 'stfrei_ort': '', 'stfrei_inland': None,
                                 'stfrei_betrag': 0.0, 'storno': True, '_raw': ' '.join(texts)})
                    continue
                # stfrei-Ort: IATA-Token (3 Großbuchstaben) im x-Band
                stfrei_ort = ''
                stfrei_betrag = 0.0
                zwoelftel = 0
                for w in line:
                    t = w['text']
                    if (len(t) == 3 and t.isalpha() and t.isupper()
                            and STFREI_ORT_X[0] <= w['x0'] <= STFREI_ORT_X[1]):
                        stfrei_ort = t
                    if re.match(r'^\d{1,3}(?:\.\d{3})*,\d{2}$', t) \
                            and STFREI_BETRAG_X[0] <= w['x0'] <= STFREI_BETRAG_X[1]:
                        stfrei_betrag = float(t.replace('.', '').replace(',', '.'))
                    if re.match(r'^\d{1,2}$', t) \
                            and ZWOELFTEL_X[0] <= w['x0'] <= ZWOELFTEL_X[1]:
                        zwoelftel = int(t)
                rows.append({
                    'datum':         datum,
                    'stfrei_ort':    stfrei_ort,
                    'stfrei_inland': _is_inland(stfrei_ort),
                    'stfrei_betrag': stfrei_betrag if stfrei_betrag else 1.0,
                    'zwoelftel':     zwoelftel,
                    'storno':        False,
                    '_raw':          ' '.join(texts),
                })
    return rows


if __name__ == '__main__':
    import os as _os
    path = sys.argv[1] if len(sys.argv) > 1 else _os.path.join(
        _os.environ.get('AEROTAX_PRIVATE_DOCS_ROOT') or _os.path.expanduser('~/Desktop/Downloads'),
        'Tibor', '2025', '2025 Streckeneinsatzabrechnungen.pdf')
    rows = parse_se_pdf(path)
    active = [r for r in rows if not r['storno']]
    foreign = [r for r in active if r['stfrei_inland'] is False]
    inland = [r for r in active if r['stfrei_inland'] is True]
    none = [r for r in active if r['stfrei_inland'] is None]
    print(f'{len(rows)} Zeilen ({len(active)} aktiv, {len([r for r in rows if r["storno"]])} storno)')
    print(f'  stfrei-Ort Ausland: {len(foreign)}  Inland: {len(inland)}  unklar: {len(none)}')
    if none:
        print('  UNKLARE (kein stfrei-Ort erkannt):')
        for r in none[:15]:
            print(f"    {r['datum']}  {r['_raw'][:72]}")
    print('  erste 14 aktive:')
    for r in active[:14]:
        print(f"    {r['datum']} ort={r['stfrei_ort'] or '—':<4} inland={r['stfrei_inland']} betrag={r['stfrei_betrag']}")
