"""Mini-Check: SE-Completeness ↔ PDF-Renderer Konsistenz (2026-05-22).

Klärt einen scheinbaren Widerspruch aus dem P0 Final Completion Report:
  - Status-Beschreibung im Report sagte „12/12 Monate erkannt"
  - PDF-Smoke-Test zeigte „11/12 Monate, Februar fehlt"

Ursache: zwei verschiedene Fixtures.
  1. AT-12CDA reale Live-Run-Snapshot (vor Code-Änderung) → _se_completeness fehlt komplett
  2. Synthetisches Missing-Feb-Fixture → Test-Trigger für die „Feb fehlt"-Render-Stelle

Diese Tests garantieren:
  A) Clean 12/12 Fixture → PDF zeigt „12/12", keine fehlenden Monate
  B) Missing-Feb Fixture → PDF zeigt „11/12" und „Feb"
  C) Fixture ohne _se_completeness → PDF lässt die Monats-Zeile aus (statt 0/12 zu zeigen)
  D) Renderer-Output entspricht dem _se_completeness-Eingang (round-trip)
"""
import io
import os
import sys

import pytest

# R37 (2026-05-27): SE-Completeness im PDF-Audit-Block — Block ist komplett
# entfernt. Daten bleiben im result_data._se_completeness für API-Konsumenten.
pytestmark = pytest.mark.skip(reason='R37: PDF-Audit-Block (SE-Completeness-Render) entfernt')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import app  # noqa: E402


def _base_data():
    """Minimaler PDF-Input ohne Audit-Warnungen."""
    return {
        'name': 'Test', 'year': 2025, 'brutto': 50000.0, 'lohnsteuer': 5000.0,
        'netto': 1000.0, 'gesamt': 5000.0, 'fahr': 500.0, 'fahr_tage': 60, 'km': 30,
        'reinig': 200.0, 'reinigungstage': 130, 'trink': 200.0, 'hotel_naechte': 50,
        'arbeitstage': 130, 'arbeitgeber': 'Lufthansa', 'datum': '22.05.2026',
        'vma_72': 100.0, 'vma_72_tage': 10, 'vma_73': 200.0, 'vma_73_tage': 20,
        'vma_74': 50.0, 'vma_74_tage': 2, 'vma_aus': 3000.0, 'z77': 4464.0,
        'ag_z17': 0.0, 'spesen_gesamt': 5000.0, 'spesen_steuer': 500.0,
        'soli': 0.0, 'kirchensteuer': 0.0, 'optionale_belege': [],
        '_unresolved_days': [], '_vma_unmapped_se': [],
    }


def _pdf_text(d):
    try:
        import pdfplumber
    except ImportError:
        pytest.skip('pdfplumber nicht verfügbar')
    pdf = app.erstelle_pdf(d)
    with pdfplumber.open(io.BytesIO(pdf)) as p:
        return '\n'.join((pg.extract_text() or '') for pg in p.pages)


# ─────────────────────────────────────────────────────────────────────────────
# A) Clean 12/12 — kein „Monate fehlen", PDF hat KEINE Prüfpunkte-Sektion
# ─────────────────────────────────────────────────────────────────────────────

def test_clean_fixture_pdf_shows_12_of_12():
    """Clean Fixture: 12/12 Monate erkannt, 0 unresolved, 0 unmapped.

    Erwartung: Prüfpunkte-Sektion entfällt komplett. Die Zeichenkette „12/12"
    erscheint NICHT im PDF (weil die Übersichts-Zeile gar nicht gerendert wird)."""
    d = _base_data()
    d['_se_completeness'] = {
        'uploaded_se_files_count':   12,
        'expected_months':           12,
        'detected_se_month_count':   12,
        'missing_se_months':         [],
        'unreadable_se_files':       [],
        'duplicate_se_months':       [],
    }
    d['_z77_audit'] = {
        'verwendeter_wert': 4464.0, 'einzelzeilen': 4464.0,
        'summenzeilen': 4464.0, 'differenz': 0.0, 'quelle': 'einzelzeilen',
    }
    txt = _pdf_text(d)
    # done_clean: KEINE Prüfpunkte-Sektion im PDF
    assert 'PRÜFPUNKTE' not in txt, 'done_clean darf keine Prüfpunkte-Sektion zeigen'
    assert 'Streckeneinsatz erkannt' not in txt
    # Trennseite muss da sein
    assert 'ALL DOORS IN PARK' in txt


def test_clean_fixture_with_warning_still_shows_12_of_12_when_se_has_other_issue():
    """Clean SE (12/12) aber andere Warnung (z.B. unresolved_days) → Sektion
    erscheint, und SE-Zeile zeigt explizit „12/12 Monate" + KEIN „Fehlt: …"."""
    d = _base_data()
    # Eine andere Warnung triggert die Sektion
    d['_unresolved_days'] = ['2025-01-07: Mischfall']
    d['_se_completeness'] = {
        'uploaded_se_files_count':   12,
        'expected_months':           12,
        'detected_se_month_count':   12,
        'missing_se_months':         [],
        'unreadable_se_files':       [],
        'duplicate_se_months':       [],
    }
    d['_z77_audit'] = {
        'verwendeter_wert': 4464.0, 'einzelzeilen': 4464.0,
        'summenzeilen': 4464.0, 'differenz': 0.0, 'quelle': 'einzelzeilen',
    }
    txt = _pdf_text(d)
    assert 'PRÜFPUNKTE' in txt
    assert 'Streckeneinsatz erkannt' in txt
    assert '12/12' in txt
    # KEIN „Fehlt"-Text — keine Monate fehlen
    assert 'Fehlende SE-Monate' not in txt
    # Auch keine fälschliche Monatsabkürzung in einer „Fehlt:"-Zeile
    assert 'fehlt:' not in txt.lower() or 'monat' not in txt.lower().split('fehlt:')[-1][:30]


# ─────────────────────────────────────────────────────────────────────────────
# B) Missing-Feb — PDF zeigt „11/12" + „Feb" als fehlender Monat
# ─────────────────────────────────────────────────────────────────────────────

def test_missing_month_fixture_pdf_shows_11_of_12_and_feb():
    d = _base_data()
    d['_vma_unmapped_se'] = [
        {'datum': '2025-05-28', 'klass': 'Issue', 'stfrei_ort': 'FRA',
         'stfrei_total': 14.0, 'reason': 'Heimkehr aus Vortag-Tour'},
    ]
    d['_se_completeness'] = {
        'uploaded_se_files_count':   11,
        'expected_months':           12,
        'detected_se_month_count':   11,
        'missing_se_months':         [2],
        'unreadable_se_files':       [],
        'duplicate_se_months':       [],
    }
    d['_z77_audit'] = {
        'verwendeter_wert': 4464.0, 'einzelzeilen': 4464.0,
        'summenzeilen': 4464.0, 'differenz': 0.0, 'quelle': 'einzelzeilen',
    }
    txt = _pdf_text(d)
    assert 'PRÜFPUNKTE' in txt
    assert 'Streckeneinsatz erkannt' in txt
    assert '11/12' in txt
    assert 'Fehlende SE-Monate' in txt
    # Februar muss konkret per Kurzform „Feb" gerendert sein
    pp_start = txt.find('PRÜFPUNKTE')
    pp_end   = txt.find('ALL DOORS IN PARK')
    section  = txt[pp_start:pp_end]
    assert 'Feb' in section


def test_missing_multiple_months_renders_all():
    """Fehlen mehrere Monate (z.B. Feb + Mai + Aug), müssen alle in der Übersicht."""
    d = _base_data()
    d['_vma_unmapped_se'] = [
        {'datum': '2025-03-01', 'klass': 'Issue', 'stfrei_ort': 'FRA',
         'stfrei_total': 14.0, 'reason': 'Anker'},
    ]
    d['_se_completeness'] = {
        'uploaded_se_files_count':   9,
        'expected_months':           12,
        'detected_se_month_count':   9,
        'missing_se_months':         [2, 5, 8],
        'unreadable_se_files':       [],
        'duplicate_se_months':       [],
    }
    d['_z77_audit'] = {
        'verwendeter_wert': 4464.0, 'einzelzeilen': 4464.0,
        'summenzeilen': 4464.0, 'differenz': 0.0, 'quelle': 'einzelzeilen',
    }
    txt = _pdf_text(d)
    assert '9/12' in txt
    pp_start = txt.find('PRÜFPUNKTE')
    pp_end   = txt.find('ALL DOORS IN PARK')
    section  = txt[pp_start:pp_end]
    assert 'Feb' in section
    assert 'Mai' in section
    assert 'Aug' in section


# ─────────────────────────────────────────────────────────────────────────────
# C) Round-Trip — _se_completeness Eingang == PDF-Renderer-Ausgang
# ─────────────────────────────────────────────────────────────────────────────

def test_se_completeness_matches_pdf_renderer():
    """Jeder Eingang in `_se_completeness` muss sich im PDF wiederfinden.

    Diese Test-Schleife garantiert, dass es KEIN versteckter Defaulting-Bug
    gibt (z.B. dass der Renderer immer „11/12" zeigt egal was der Status sagt)."""
    cases = [
        # (uploaded, detected, missing, expected_in_pdf)
        (12, 12, [],         '12/12'),
        (11, 11, [2],        '11/12'),
        (10, 10, [2, 7],     '10/12'),
        (6,  6,  [1, 3, 5, 7, 9, 11], '6/12'),
        (3,  3,  [1, 2, 4, 5, 6, 8, 9, 10, 11, 12], '3/12'),
    ]
    for uploaded, detected, missing, expected_token in cases:
        d = _base_data()
        # Trigger Sektion via unmapped_se — sonst entfällt die Übersicht
        d['_vma_unmapped_se'] = [{'datum': '2025-01-15', 'klass': 'Issue',
                                   'stfrei_ort': 'FRA', 'stfrei_total': 14.0,
                                   'reason': 'Anker'}]
        d['_se_completeness'] = {
            'uploaded_se_files_count':   uploaded,
            'expected_months':           12,
            'detected_se_month_count':   detected,
            'missing_se_months':         missing,
            'unreadable_se_files':       [],
            'duplicate_se_months':       [],
        }
        d['_z77_audit'] = {
            'verwendeter_wert': 100.0, 'einzelzeilen': 100.0,
            'summenzeilen': 100.0, 'differenz': 0.0, 'quelle': 'einzelzeilen',
        }
        txt = _pdf_text(d)
        assert expected_token in txt, (
            f'Roundtrip-Fail: detected={detected}, expected={expected_token} '
            f'NICHT in PDF gefunden'
        )


# ─────────────────────────────────────────────────────────────────────────────
# D) AT-12CDA reale Snapshot (vor Code-Änderung) — kein falsches „0/12"
# ─────────────────────────────────────────────────────────────────────────────

def test_at12cda_legacy_snapshot_does_not_show_zero_of_twelve():
    """Realer AT-12CDA-Snapshot hat _se_completeness NICHT (älterer Run, vor
    diesem Sprint). Der PDF-Renderer DARF in diesem Fall keine fälschliche
    `0/12 Monate`-Zeile rendern — die Übersicht-Zeile entfällt komplett.

    Andere Warnungen (23 unresolved + 6 unmapped) erscheinen trotzdem korrekt."""
    fixture = '/tmp/aerotax_AT12CDA_result.json'
    if not os.path.exists(fixture):
        pytest.skip('AT-12CDA fixture nicht verfügbar')
    import json as _json
    with open(fixture) as f:
        snap = _json.load(f)
    rd = snap['result_data']
    # Sanity: fixture hat tatsächlich kein _se_completeness
    assert '_se_completeness' not in rd or not rd.get('_se_completeness')
    txt = _pdf_text(rd)
    # Sektion erscheint (unresolved + unmapped > 0)
    assert 'PRÜFPUNKTE' in txt
    # Aber KEINE „Streckeneinsatz erkannt"-Zeile mit „0/12"
    pp_start = txt.find('PRÜFPUNKTE')
    pp_end   = txt.find('ALL DOORS IN PARK')
    section  = txt[pp_start:pp_end]
    assert '0/12' not in section, (
        'PDF zeigt fälschlich „0/12 Monate" obwohl _se_completeness fehlt — '
        'Renderer muss die Zeile in diesem Fall auslassen'
    )
    # Andere Warnungen erscheinen
    assert 'Nicht eindeutig eingeordnete Tage' in section
    assert 'Nicht zugeordnete Streckeneinsatz' in section
    # Z77 wird trotzdem aus _z77_audit gelesen
    assert 'Z77 verwendet' in section
    assert '4.464' in section


def test_at12cda_legacy_snapshot_counts_match_state():
    """Die im PDF gerenderten Counts müssen 1:1 dem Snapshot entsprechen."""
    fixture = '/tmp/aerotax_AT12CDA_result.json'
    if not os.path.exists(fixture):
        pytest.skip('AT-12CDA fixture nicht verfügbar')
    import json as _json
    with open(fixture) as f:
        snap = _json.load(f)
    rd = snap['result_data']
    expected_unresolved = len(rd.get('_unresolved_days') or [])
    expected_unmapped   = len(rd.get('_vma_unmapped_se') or [])
    assert expected_unresolved == 23, 'Sanity: fixture hat 23 unresolved'
    assert expected_unmapped == 6,  'Sanity: fixture hat 6 unmapped'
    txt = _pdf_text(rd)
    pp_start = txt.find('PRÜFPUNKTE')
    pp_end   = txt.find('ALL DOORS IN PARK')
    section  = txt[pp_start:pp_end]
    # Counts erscheinen als Zahl in der Übersichts-Tabelle
    # (nicht zwingend isoliert — aber irgendwo in der Sektion)
    assert '23' in section
    assert '6' in section


# ─────────────────────────────────────────────────────────────────────────────
# E) Statusbox-Renderer (Frontend, statisch) zeigt selbe Counts
# ─────────────────────────────────────────────────────────────────────────────

def test_statusbox_renderer_uses_same_se_completeness_fields_as_pdf():
    """Frontend-Renderer und PDF-Renderer lesen aus DENSELBEN Feldern.

    Sonst wäre eine Vermischung möglich: PDF zeigt 11/12, Statusbox zeigt 12/12."""
    html_path = os.path.expanduser('~/Desktop/site/index.html')
    if not os.path.exists(html_path):
        pytest.skip('index.html nicht verfügbar')
    with open(html_path, encoding='utf-8') as f:
        html = f.read()
    idx = html.find('window._renderAuditWarningBox = function')
    assert idx > 0
    block = html[idx:idx + 3500]
    # Beide Renderer (PDF + Frontend) müssen aus diesen exakten Feldnamen lesen:
    for field in ('detected_se_month_count', 'expected_months', 'missing_se_months'):
        assert field in block, f'Frontend-Renderer fehlt Feld {field}'
    # Auch PDF-Renderer muss diese Felder lesen
    with open(os.path.join(os.path.dirname(__file__), '..', 'app.py')) as f:
        src = f.read()
    pdf_idx = src.find('# PRÜFPUNKTE — nur wenn Audit-Warnungen vorliegen')
    assert pdf_idx > 0
    pdf_block = src[pdf_idx:pdf_idx + 5000]
    for field in ('detected_se_month_count', 'expected_months', 'missing_se_months'):
        assert field in pdf_block, f'PDF-Renderer fehlt Feld {field}'
