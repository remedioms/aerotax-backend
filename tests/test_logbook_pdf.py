"""EASA-FCL.050-PDF-Export des Flugbuchs (Owner-Freigabe 01.08.).

Beweist die drei Zusagen des PLAN-Docs:
  1. Quelle ist EXAKT get_logbook (Summen im PDF == totals der API),
  2. Übertrag/Gesamt-Mechanik inkl. Vor-Logbuch-carryover (nur range=all),
  3. Sim strikt getrennt (FSTD-Summe erscheint, zählt nie in die Flug-Summe).
Nutzt dieselben Seeds wie test_logbook (manual-briefings + Import-Blob)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as backend
from tests.test_logbook import TOKEN, _seed, _seed_import, IMPORT_BLOB, _get


def _pdf(query=''):
    with backend.app.test_request_context(f'/x?{query}'):
        return backend.get_logbook_pdf(TOKEN)


def _pdf_text(resp):
    import pdfplumber
    from io import BytesIO
    assert resp.mimetype == 'application/pdf'
    with pdfplumber.open(BytesIO(resp.get_data())) as pdf:
        return '\n'.join((p.extract_text() or '') for p in pdf.pages)


def _hhmm(m):
    return f'{m // 60}:{m % 60:02d}'


def test_pdf_gesamt_entspricht_api_totals():
    _seed()
    r = _get()
    text = _pdf_text(_pdf())
    assert 'FCL.050' in text
    assert 'LH400' in text and 'FRA' in text
    # Gesamt-Blockzeit im PDF == totals der API (EINE Quelle, keine zweite
    # Summen-Implementierung).
    assert _hhmm(r['totals']['block_min']) in text
    assert 'Gesamt' in text and 'Diese Seite' in text and 'Übertrag' in text


def test_pdf_carryover_nur_bei_range_all():
    _seed()
    _seed_import()
    r = _get()
    carry = IMPORT_BLOB['meta']['carryover_min']
    text = _pdf_text(_pdf())
    # Gesamt = API-Totals + Vor-Logbuch-Übertrag, und die Fußnote weist ihn aus.
    assert _hhmm(r['totals']['block_min'] + carry) in text
    assert 'Vor-Logbuch' in text
    # Einzel-Jahr: kein Übertrag aus dem Vor-Logbuch.
    text_2026 = _pdf_text(_pdf('range=2026'))
    assert 'Vor-Logbuch' not in text_2026
    # 2026er-Summe = nur die drei Roster-Legs (520+450+60).
    assert _hhmm(520 + 450 + 60) in text_2026
    assert 'LH500' not in text_2026        # 2019er-Import-Leg gefiltert


def test_pdf_carryover_includes_historic_landings():
    _seed()
    blob = dict(IMPORT_BLOB)
    blob['meta'] = {
        **IMPORT_BLOB['meta'],
        'carryover_ldg_day': 1050,
        'carryover_ldg_night': 7,
        'carryover_landings': 1057,
    }
    _seed_import(blob)
    text = _pdf_text(_pdf())
    assert '1057 Landungen aus dem Vor-Logbuch' in text
    assert 'Landungen des Vor-Logbuchs sind nicht erfasst' not in text


def test_pdf_sim_getrennt():
    _seed()
    _seed_import()
    text = _pdf_text(_pdf())
    assert 'FSTD' in text
    assert 'FSTD gesamt: 4:00' in text     # 240 min aus dem Import-Blob
    # Die Flug-Gesamtsumme enthält die Sim-Zeit NICHT: 1660 (Flug) + 7800
    # (Übertrag) = 9460 min — mit Sim wären es 9700.
    assert _hhmm(9460) in text
    assert _hhmm(9700) not in text


def test_pdf_leerer_range_ist_ehrlich():
    _seed()
    text = _pdf_text(_pdf('range=1999'))
    assert 'Keine Flüge im gewählten Zeitraum' in text


def test_pdf_fmt_faa_wird_ehrlich_abgelehnt():
    _seed()
    resp = _pdf('fmt=faa')
    body = resp[0].get_json() if isinstance(resp, tuple) else resp.get_json()
    code = resp[1] if isinstance(resp, tuple) else resp.status_code
    assert code == 400 and body['error'] == 'fmt_unsupported'


def test_pdf_blockzeit_ist_die_ist_differenz_nicht_blz68():
    """Der EASA-Export darf der App nicht widersprechen: eine importierte
    BLZ68-Durchschnittszahl verliert gegen die gemessenen Ist-Zeiten
    (Tester-Meldung 10.08.2026, gleiche Regel wie iOS)."""
    from tests.test_logbook import BLZ68_BLOB
    _seed()
    _seed_import(BLZ68_BLOB)
    r = _get()
    text = _pdf_text(_pdf('range=2026-06'))
    assert 'LH620' in text and 'LH777' in text
    assert _hhmm(252) in text and _hhmm(265) in text
    assert _hhmm(235) not in text                  # BLZ68-Durchschnitt
    # Gesamt im PDF == totals der API (EINE Quelle).
    assert _hhmm(r['totals']['block_min']) in _pdf_text(_pdf())
