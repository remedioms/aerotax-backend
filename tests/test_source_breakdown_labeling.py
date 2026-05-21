"""Source-Labeling-Tests (Highest-Defensible-Produktregel).

result_data.source_breakdown muss vorhanden sein und für jeden Bucket
Type + Label + user_inputs + star_required korrekt setzen.

Statik-Audit gegen app.py + index.html (kein Live-Backend).
"""

import os
import re
import pytest


APP_PY = '/Users/miguelschumann/Desktop/aerotax-backend/app.py'
INDEX  = '/Users/miguelschumann/Desktop/site/index.html'


# ════════════════════════════════════════════════════════════════════
# Backend: result_data.source_breakdown
# ════════════════════════════════════════════════════════════════════

def _src():
    return open(APP_PY, encoding='utf-8').read()

def _html():
    return open(INDEX, encoding='utf-8').read()


def test_result_data_has_source_breakdown_block():
    """app.py setzt source_breakdown im result_data Output."""
    src = _src()
    assert "'source_breakdown'" in src, 'source_breakdown Block fehlt in result_data'


def test_source_breakdown_has_block_a_block_b_erstattung():
    """Top-level Buckets in source_breakdown."""
    src = _src()
    for key in ('block_a', 'block_b', 'erstattung', 'legend'):
        assert f"'{key}'" in src, f'source_breakdown.{key} fehlt'


def test_block_a_fahr_marked_as_mixed_with_user_km():
    """Block A Fahr ist mixed mit user_inputs=['km'] + star_required=True."""
    src = _src()
    # Block of fahr
    m = re.search(
        r"'fahr'\s*:\s*\{[\s\S]{0,800}'star_required'\s*:\s*True", src
    )
    assert m, 'fahr Block muss star_required=True haben'
    fahr_block = m.group(0)
    assert "'mixed'" in fahr_block
    assert "'km'" in fahr_block


def test_block_a_reinig_marked_as_calculated():
    """Reinigung ist calculated (Pauschal-Ansatz), star_required=False."""
    src = _src()
    m = re.search(
        r"'reinig'\s*:\s*\{[\s\S]{0,400}'star_required'\s*:\s*False", src
    )
    assert m, 'reinig Block muss calculated + star_required=False sein'
    reinig_block = m.group(0)
    assert "'calculated'" in reinig_block
    assert 'Pauschal' in reinig_block


def test_block_a_trink_marked_as_calculated():
    src = _src()
    m = re.search(
        r"'trink'\s*:\s*\{[\s\S]{0,400}'star_required'\s*:\s*False", src
    )
    assert m
    assert "'calculated'" in m.group(0)


def test_block_a_opt_zu_marked_as_user_with_star():
    """Optionale Belege sind user-input → star_required=True."""
    src = _src()
    m = re.search(
        r"'opt_zu'\s*:\s*\{[\s\S]{0,500}'star_required'\s*:\s*True", src
    )
    assert m
    assert "'user'" in m.group(0)


def test_block_b_vma_aus_marked_as_mixed():
    """Z76 ist mixed (CAS + SE + BMF)."""
    src = _src()
    bk_idx = src.find("'source_breakdown'")
    assert bk_idx > 0
    section = src[bk_idx:bk_idx + 5000]
    # Capture full vma_aus dict-block: from 'vma_aus' to next dict closure
    m = re.search(r"'vma_aus'\s*:\s*\{[\s\S]+?\},", section)
    assert m, 'vma_aus Block muss vorhanden sein'
    block = m.group(0)
    assert "'type': 'mixed'" in block, f'type=mixed fehlt: {block[:300]}'
    assert 'CAS' in block or 'Layover' in block
    assert 'SE' in block or 'stfrei' in block


def test_block_b_vma_74_marked_as_document():
    """Z74 (Inland 24h) ist document-only (CAS overnight)."""
    src = _src()
    m = re.search(
        r"'vma_74'\s*:\s*\{[\s\S]{0,400}'type'\s*:\s*'document'", src
    )
    assert m


def test_erstattung_z17_marked_as_document():
    """AG-Z17 ist document (LSB Zeile 17)."""
    src = _src()
    bk_idx = src.find("'source_breakdown'")
    section = src[bk_idx:bk_idx + 5000]
    m = re.search(r"'ag_z17'\s*:\s*\{[\s\S]+?\},", section)
    assert m
    block = m.group(0)
    assert "'type': 'document'" in block
    assert 'LSB' in block or 'Lohnsteuerbescheinigung' in block


def test_erstattung_z77_marked_as_document():
    """Z77 ist document (SE Summenzeilen)."""
    src = _src()
    bk_idx = src.find("'source_breakdown'")
    section = src[bk_idx:bk_idx + 5000]
    # Suche z77 mit nachfolgendem document-type
    m = re.search(r"'z77'\s*:\s*\{[\s\S]+?\},", section)
    assert m
    block = m.group(0)
    assert "'type': 'document'" in block
    assert 'SE' in block or 'Streckeneinsatz' in block


def test_legend_block_has_all_required_keys():
    """legend hat *, CAS, SE, LSB, BMF, Pauschal-Ansatz."""
    src = _src()
    m = re.search(r"'legend'\s*:\s*\{[\s\S]{0,1200}\}", src)
    assert m
    legend_block = m.group(0)
    for key in ['*', 'CAS', 'SE', 'LSB', 'BMF', 'Pauschal-Ansatz']:
        assert f"'{key}'" in legend_block, f'Legend-Key „{key}" fehlt'


def test_no_unlabeled_user_influenced_amount():
    """Statik: jeder Bucket der user_inputs hat MUSS star_required korrekt setzen."""
    src = _src()
    # Find all dict-blocks with user_inputs
    for m in re.finditer(
        r"'user_inputs'\s*:\s*\[([^\]]*)\]", src
    ):
        inputs = m.group(1)
        # Get surrounding context to find star_required
        ctx_start = max(0, m.start() - 400)
        ctx_end = min(len(src), m.end() + 200)
        ctx = src[ctx_start:ctx_end]
        if inputs.strip() and inputs.strip() != '':
            # has user_inputs → MUST have star_required True or False explicit
            assert 'star_required' in ctx, \
                f'Bucket mit user_inputs braucht star_required: {inputs}'


# ════════════════════════════════════════════════════════════════════
# PDF source-legend
# ════════════════════════════════════════════════════════════════════

def test_pdf_contains_source_legend_text():
    """PDF Berechnung-Section enthält die Source-Legende."""
    src = _src()
    # Legend-Text-Konstanten
    assert 'Quellen-Übersicht' in src
    assert 'Nutzerangabe' in src
    assert 'CAS' in src and 'SE' in src and 'LSB' in src and 'BMF' in src
    assert 'Pauschal-Ansatz' in src


def test_pdf_legend_appears_after_einzutragend():
    """Legende kommt NACH 'Einzutragender Gesamtbetrag (A + B)'."""
    src = _src()
    idx_final = src.find('Einzutragender Gesamtbetrag (A + B)')
    idx_legend = src.find('Quellen-Übersicht')
    assert idx_final > 0 and idx_legend > 0
    assert idx_legend > idx_final, 'Legende muss NACH der Final-Zeile kommen'


# ════════════════════════════════════════════════════════════════════
# UI source-legend
# ════════════════════════════════════════════════════════════════════

def test_ui_contains_source_legend_text():
    """UI Detail-Tabelle enthält die user-friendly Source-Legende „Woher kommen die Werte?"."""
    html = _html()
    assert 'Woher kommen die Werte?' in html
    assert 'Dienstplan / CAS' in html
    assert 'Streckeneinsatz' in html
    assert 'Lohnsteuerdaten' in html
    assert 'BMF-Pauschalen' in html
    assert 'Deine Angabe *' in html
    assert 'Pauschal-Ansatz' in html


def test_ui_km_marked_with_star():
    """km im Detail-Tabelle hat *-Marker als Nutzerangabe-Hinweis."""
    html = _html()
    # Fahrtkosten-Zeile enthält km mit * (Nutzerangabe-Marker)
    assert "' km* '" in html or "km* ×" in html, \
        'km muss mit *-Marker als Nutzerangabe gekennzeichnet sein'


# ════════════════════════════════════════════════════════════════════
# No marker-only / no unbacked foreign day (regression guards from previous fix)
# ════════════════════════════════════════════════════════════════════

def test_no_marker_only_tax_decision_in_code():
    """Statik-Guard: kein Z76 nur per Marker ohne CAS/SE-Evidence.
    (Wir prüfen, dass die VMA-Pfade Layover/SE-Bedingungen haben.)"""
    src = _src()
    # Die Hauptpfade müssen layover_ort oder se.stfrei_ort checken
    assert "se.get('stfrei_ort')" in src
    assert "_is_inland_code" in src
