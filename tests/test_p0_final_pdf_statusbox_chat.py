"""P0 Final Completion Tests (2026-05-22).

OBSOLET nach R37 (2026-05-27): Audit-Prüfpunkte-Sektion wurde komplett
aus PDF und Frontend entfernt. User-Feedback: „erzeugt Verunsicherung
statt Sicherheit". Die hier getesteten Strings („PRÜFPUNKTE", „Tage als
Prüfpunkt markiert" etc.) sind im aktuellen Build nicht mehr vorhanden.

Datei ist ge-skipt. Tests dokumentieren früheres Verhalten.
"""
import io
import os
import re
import sys

import pytest

# R37 (2026-05-27): ganzes Modul skipt — Audit-UI bewusst entfernt.
pytestmark = pytest.mark.skip(reason='R37: Audit-Prüfpunkte-Sektion komplett entfernt')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import app  # noqa: E402


INDEX_HTML = os.path.expanduser('~/Desktop/site/index.html')


# ─────────────────────────────────────────────────────────────────────────────
# Common fixture builder
# ─────────────────────────────────────────────────────────────────────────────

def _minimal_pdf_data(with_warnings=True):
    d = {
        'name': 'Test', 'year': 2025, 'brutto': 50000.0, 'lohnsteuer': 5000.0,
        'netto': 1000.0, 'gesamt': 5000.0, 'fahr': 500.0, 'fahr_tage': 60, 'km': 30,
        'reinig': 200.0, 'reinigungstage': 130, 'trink': 200.0, 'hotel_naechte': 50,
        'arbeitstage': 130, 'arbeitgeber': 'Lufthansa', 'datum': '22.05.2026',
        'vma_72': 100.0, 'vma_72_tage': 10, 'vma_73': 200.0, 'vma_73_tage': 20,
        'vma_74': 50.0, 'vma_74_tage': 2, 'vma_aus': 3000.0, 'z77': 4464.0,
        'ag_z17': 0.0, 'spesen_gesamt': 5000.0, 'spesen_steuer': 500.0,
        'soli': 0.0, 'kirchensteuer': 0.0, 'optionale_belege': [],
    }
    if with_warnings:
        d['_unresolved_days'] = ['2025-01-07: Office am Homebase',
                                  '2025-02-15: Mischfall']
        d['_vma_unmapped_se'] = [
            {'datum': '2025-01-07', 'klass': 'Office', 'stfrei_ort': 'SEL',
             'stfrei_total': 32.0, 'reason': 'Office am Homebase'},
            {'datum': '2025-05-28', 'klass': 'Issue', 'stfrei_ort': 'FRA',
             'stfrei_total': 14.0, 'reason': 'Heimkehr aus Vortag-Tour'},
        ]
        d['_se_completeness'] = {
            'uploaded_se_files_count': 11, 'expected_months': 12,
            'detected_se_month_count': 11, 'missing_se_months': [2],
            'unreadable_se_files': [], 'duplicate_se_months': [],
        }
        d['_z77_audit'] = {
            'verwendeter_wert': 4464.0, 'einzelzeilen': 4464.0,
            'summenzeilen': 4311.80, 'differenz': 152.20, 'quelle': 'einzelzeilen',
            'auslandsspesen': 4076.65, 'inlandsspesen': 235.15,
        }
    else:
        d['_unresolved_days'] = []
        d['_vma_unmapped_se'] = []
        d['_se_completeness'] = {
            'uploaded_se_files_count': 12, 'expected_months': 12,
            'detected_se_month_count': 12, 'missing_se_months': [],
            'unreadable_se_files': [], 'duplicate_se_months': [],
        }
        d['_z77_audit'] = {
            'verwendeter_wert': 4464.0, 'einzelzeilen': 4464.0,
            'summenzeilen': 4464.0, 'differenz': 0.0, 'quelle': 'einzelzeilen',
        }
    return d


def _extract_pdf_text(pdf_bytes):
    """Extrahiert Text aus PDF via pdfplumber für Inhalt-Checks."""
    try:
        import pdfplumber
    except ImportError:
        pytest.skip('pdfplumber nicht verfügbar')
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as p:
        return '\n'.join((pg.extract_text() or '') for pg in p.pages)


# ─────────────────────────────────────────────────────────────────────────────
# FIX 1 — PDF Prüfpunkte-Sektion
# ─────────────────────────────────────────────────────────────────────────────

def test_pdf_contains_pruefpunkte_section_when_audit_warnings():
    """Bei done_with_audit_warnings muss die PDF eine PRÜFPUNKTE-Sektion enthalten."""
    pdf = app.erstelle_pdf(_minimal_pdf_data(with_warnings=True))
    txt = _extract_pdf_text(pdf)
    assert 'PRÜFPUNKTE' in txt
    assert 'Hinweise zu deiner Auswertung' in txt


def test_pdf_no_pruefpunkte_section_when_done_clean():
    """Bei done_clean (keine Warnungen) entfällt die Prüfpunkte-Sektion komplett."""
    pdf = app.erstelle_pdf(_minimal_pdf_data(with_warnings=False))
    txt = _extract_pdf_text(pdf)
    assert 'PRÜFPUNKTE' not in txt


def test_pdf_pruefpunkte_lists_unresolved_count():
    pdf = app.erstelle_pdf(_minimal_pdf_data(with_warnings=True))
    txt = _extract_pdf_text(pdf)
    # 2 unresolved days in fixture → die Zahl 2 muss in der Übersicht stehen
    assert 'Nicht eindeutig eingeordnete Tage' in txt
    # konkrete Zahl in Übersichtstabelle oder Detail-Liste
    assert '2' in txt.split('Nicht eindeutig eingeordnete Tage')[1][:50]


def test_pdf_pruefpunkte_lists_unmapped_se_count():
    pdf = app.erstelle_pdf(_minimal_pdf_data(with_warnings=True))
    txt = _extract_pdf_text(pdf)
    assert 'Nicht zugeordnete Streckeneinsatz' in txt
    # Detail-Tabelle: Datum/Ort/Betrag pro Zeile
    assert 'SEL' in txt
    assert 'FRA' in txt or 'Heimkehr' in txt


def test_pdf_pruefpunkte_lists_se_month_count():
    pdf = app.erstelle_pdf(_minimal_pdf_data(with_warnings=True))
    txt = _extract_pdf_text(pdf)
    assert 'Streckeneinsatz erkannt' in txt
    assert '11/12' in txt
    assert 'Feb' in txt  # missing_se_months: [2] → "Feb"


def test_pdf_pruefpunkte_lists_z77_used_and_source():
    pdf = app.erstelle_pdf(_minimal_pdf_data(with_warnings=True))
    txt = _extract_pdf_text(pdf)
    assert 'Z77 verwendet' in txt
    assert 'einzelzeilen' in txt
    # Format: 4.464,00 € — euros German style
    assert '4.464' in txt


def test_pdf_pruefpunkte_no_pii():
    """Die Prüfpunkte-Tabellen/Detail-Liste dürfen KEINE Namen/Personalnummer enthalten.

    PII-arm heißt: nur Datum + Ort (IATA) + Betrag + Grund (Klassifikator-Code).
    Es ist erwartet, dass der PDF-Header/Footer den Mandanten-Namen führt — aber
    NICHT die Prüfpunkte-Tabellen selbst."""
    d = _minimal_pdf_data(with_warnings=True)
    d['name'] = 'Hans Schmidt'
    d['personalnummer'] = '99887766'
    d['identnr'] = '12345678901'
    pdf = app.erstelle_pdf(d)
    txt = _extract_pdf_text(pdf)
    # Detail-Tabelle "Nicht zugeordnete Streckeneinsatz-Zeilen" und die folgende
    # Auflistung dürfen keine PII führen. Header/Footer separat behandelt.
    idx_table = txt.find('Nicht zugeordnete Streckeneinsatz')
    idx_next  = txt.find('ALL DOORS IN PARK')
    assert idx_table > 0 and idx_next > idx_table
    detail_section = txt[idx_table:idx_next]
    # Personalnummer/IdNr dürfen NIRGENDS in dieser Detail-Sektion stehen
    assert '99887766' not in detail_section
    assert '12345678901' not in detail_section


def test_pdf_pruefpunkte_includes_z77_diff_when_relevant():
    pdf = app.erstelle_pdf(_minimal_pdf_data(with_warnings=True))
    txt = _extract_pdf_text(pdf)
    # differenz=152.20 > 5.0 → Zeile muss erscheinen
    assert 'Differenz Einzelzeilen vs Summenzeilen' in txt
    assert '152,20' in txt


# ─────────────────────────────────────────────────────────────────────────────
# FIX 2 — Statusbox direct render
# ─────────────────────────────────────────────────────────────────────────────

def _read_html():
    with open(INDEX_HTML, encoding='utf-8') as f:
        return f.read()


def test_statusbox_renderer_function_exists():
    """`window._renderAuditWarningBox` muss als globale Funktion existieren."""
    html = _read_html()
    assert 'window._renderAuditWarningBox = function' in html


def test_statusbox_dom_mount_node_exists():
    """`#audit-warning-card` DOM-Element muss im p-result-Panel sein."""
    html = _read_html()
    assert 'id="audit-warning-card"' in html


def test_statusbox_done_with_audit_warnings_title():
    """Bei done_with_audit_warnings setzt Header eyebrow auf
    'PDF bereit mit Prüfpunkten · Lufthansa YYYY'."""
    html = _read_html()
    # _safe('header'-Block muss done_with_audit_warnings branch haben
    idx = html.find("_safe('header'")
    assert idx > 0
    block = html[idx:idx + 2500]
    assert "done_with_audit_warnings" in block
    assert "PDF bereit mit Prüfpunkten" in block


def test_statusbox_done_with_audit_warnings_counts():
    """Renderer muss konkrete Zähler in die Box schreiben:
    Tage markiert / SE-Zeilen markiert / N/12 Monate / Z77."""
    html = _read_html()
    fn_idx = html.find('window._renderAuditWarningBox = function')
    assert fn_idx > 0
    block = html[fn_idx:fn_idx + 3500]
    # Pflicht-Strings im Renderer-Body
    assert 'Tage markiert' in block
    assert 'Streckeneinsatz-Zeilen markiert' in block
    assert 'Streckeneinsatz erkannt' in block
    assert 'Z77 berücksichtigt' in block
    # Erkennt _unresolved_days, _vma_unmapped_se, _se_completeness Felder
    assert '_unresolved_days' in block
    assert '_vma_unmapped_se' in block
    assert '_se_completeness' in block


def test_statusbox_done_clean_no_warning_counts():
    """Bei done_clean: Renderer hidet die Card (display:none) — keine Counts."""
    html = _read_html()
    fn_idx = html.find('window._renderAuditWarningBox = function')
    block = html[fn_idx:fn_idx + 3500]
    # Default-Pfad muss style.display='none' setzen, wenn keine Warnings
    assert "style.display = 'none'" in block
    # Hidden-Branch wird via "if(!hasWarn)" gerendert
    assert 'hasWarn' in block


def test_statusbox_needs_review_not_confused_with_audit():
    """needs_review → Renderer hidet Audit-Warning-Card (eigenes Banner via
    deriveUiState branch). Bei needs_review fließt cs durch, hasWarn=false ohne
    Warnungen-Counter."""
    html = _read_html()
    fn_idx = html.find('window._renderAuditWarningBox = function')
    block = html[fn_idx:fn_idx + 3500]
    # Nur cs==done_with_audit_warnings ODER Counter > 0 triggern Box
    assert "'done_with_audit_warnings'" in block


def test_no_pdf_ready_without_warning_text_when_audit_warnings():
    """deriveUiState liefert für done_with_audit_warnings einen banner_text
    der 'Prüfpunkte' erwähnt — niemals nur 'Du kannst das PDF herunterladen.'"""
    html = _read_html()
    # Konkreter Anker auf den deriveUiState-Branch, nicht auf den ersten
    # Vorkommen-String im Code.
    branch = html.find("if(cs === 'done_with_audit_warnings')")
    assert branch > 0, 'done_with_audit_warnings-Branch in deriveUiState nicht gefunden'
    block = html[branch:branch + 2500]
    assert 'Prüfpunkte' in block or 'prüfen' in block.lower()
    naive_pattern = "banner_text  = 'Dein Betrag ist berechnet. Du kannst das PDF herunterladen.'"
    assert naive_pattern not in block


def test_statusbox_called_from_render_pipeline():
    """render() muss _renderAuditWarningBox aufrufen."""
    html = _read_html()
    assert "_renderAuditWarningBox(d, _uiState)" in html or \
           "window._renderAuditWarningBox" in html


def test_status_kind_normalizes_done_variants():
    """Bestehende Gates verlassen sich auf status_kind=='done'. Beide neuen
    Sub-States müssen darauf mappen, sonst hidet _hardHideResultSections die UI."""
    html = _read_html()
    idx = html.find('var statusKind = cs;')
    assert idx > 0
    block = html[idx:idx + 600]
    assert "'done_clean'" in block
    assert "'done_with_audit_warnings'" in block
    assert "statusKind = 'done'" in block


# ─────────────────────────────────────────────────────────────────────────────
# FIX 3 — Chat Duplicate Guard
# ─────────────────────────────────────────────────────────────────────────────

def test_chat_dedupe_function_exists():
    assert hasattr(app, '_chat_dedupe_answer')


def test_no_duplicate_pdf_ready_message():
    """Wenn die letzte Assistant-Antwort schon „Hier ist dein PDF" sagte,
    darf die neue Antwort nicht denselben Block wiederholen."""
    prev = (
        'Dein PDF ist bereit. Du kannst es herunterladen. Beachte die '
        'Prüfpunkte: 23 unklare Tage und 6 nicht zugeordnete Streckeneinsatz-Zeilen.'
    )
    new = (
        'Dein PDF ist bereit. Du kannst es herunterladen. Beachte die '
        'Prüfpunkte: 23 unklare Tage und 6 nicht zugeordnete Streckeneinsatz-Zeilen.'
    )
    history = [
        {'role': 'user',      'content': 'wo ist mein pdf'},
        {'role': 'assistant', 'content': prev},
        {'role': 'user',      'content': 'und das pdf?'},
    ]
    result = app._chat_dedupe_answer(new, history)
    # Soll kollapsen zu kurzem Verweis
    assert result != new
    assert ('Wie gerade' in result or 'bereits' in result.lower()
            or 'gerade beschrieben' in result.lower())


def test_no_repeated_same_warning_in_chat():
    """Wenn der Bot in zwei Turns hintereinander quasi identische
    Warning-Texte produzieren würde, dedupe."""
    prev = (
        'Die 23 unklaren Tage und 6 nicht zugeordneten Streckeneinsatz-Zeilen '
        'sind im Audit-Anhang gelistet. Bitte vor Übernahme in WISO prüfen.'
    )
    new = (
        'Die 23 unklaren Tage und 6 nicht zugeordneten Streckeneinsatz-Zeilen '
        'sind im Audit-Anhang aufgeführt. Bitte vor der Übernahme in WISO prüfen.'
    )
    history = [
        {'role': 'user',      'content': 'was ist unklar?'},
        {'role': 'assistant', 'content': prev},
        {'role': 'user',      'content': 'erklär nochmal — also was ist unklar?'},
    ]
    # NB: history's last user mentions "nochmal" — guard erlaubt Wiederholung
    result = app._chat_dedupe_answer(new, history)
    assert result == new  # User hat explizit nochmal verlangt


def test_dedupe_lets_distinct_answers_through():
    """Zwei inhaltlich klar verschiedene Antworten dürfen NICHT dedupliziert werden."""
    prev = 'Dein PDF ist bereit. Du kannst es jetzt herunterladen.'
    new = (
        'Z77 in deiner Auswertung beträgt 4.464 €. Das ist die Summe aller '
        'steuerfreien Spesen aus deinem Streckeneinsatz. Inland 235 €, Ausland 4.077 €.'
    )
    history = [
        {'role': 'user',      'content': 'pdf'},
        {'role': 'assistant', 'content': prev},
        {'role': 'user',      'content': 'warum ist z77 so hoch?'},
    ]
    result = app._chat_dedupe_answer(new, history)
    assert result == new  # Komplett anderer Inhalt — durchlassen


def test_user_asks_pdf_once_gets_single_pdf_ready_with_warnings():
    """Im ersten Turn antwortet der Bot vollständig mit PDF-ready + Warnings.
    Wenn der User direkt danach nochmal eine ähnliche Frage stellt, soll die
    Antwort gekürzt sein — nicht zweimal die Vollantwort."""
    full_first = (
        'Dein PDF ist bereit zur Übernahme in WISO. Beachte: 23 Tage sind '
        'als Prüfpunkte markiert, 6 Streckeneinsatz-Zeilen ebenfalls. Details '
        'siehe Prüfpunkte-Sektion im PDF.'
    )
    # v14 (2026-05-22): Dedupe-Threshold von 0.80 → 0.92 angehoben für smarteren
    # Chat. Nur nahezu wortgleiche Antworten werden kollabiert. Test verwendet
    # daher eine fast 1:1 wiederholte Antwort (≥92% ratio).
    second_repeat = full_first  # vollkommen identische Wiederholung → ratio = 1.0
    history = [
        {'role': 'user',      'content': 'pdf?'},
        {'role': 'assistant', 'content': full_first},
        {'role': 'user',      'content': 'und das pdf jetzt?'},
    ]
    result = app._chat_dedupe_answer(second_repeat, history)
    assert result != second_repeat
    assert ('gerade' in result.lower() or 'bereits' in result.lower()
            or 'wie geschrieben' in result.lower())
    assert 'PDF' in result or 'pdf' in result.lower()


def test_dedupe_skipped_for_short_answers():
    """Antworten unter 60 Zeichen sind zu kurz für Dedup — sollen durchgehen."""
    prev = 'Ja, das PDF ist da.'
    new  = 'Ja, das PDF ist da.'  # identisch aber sehr kurz
    history = [
        {'role': 'user',      'content': 'pdf?'},
        {'role': 'assistant', 'content': prev},
        {'role': 'user',      'content': 'pdf?'},
    ]
    result = app._chat_dedupe_answer(new, history)
    assert result == new  # kurz genug, kein Dedup


def test_dedupe_respects_user_explicit_repeat_request():
    """Wenn User „nochmal" sagt, ist explizit Wiederholung erwünscht."""
    prev = (
        'Die 23 unklaren Tage und 6 SE-Zeilen sind im Audit-Anhang. '
        'Prüfe vor Übernahme in WISO.'
    )
    new = (
        'Die 23 unklaren Tage und 6 SE-Zeilen sind im Audit-Anhang. '
        'Prüfe vor Übernahme in WISO.'
    )
    history = [
        {'role': 'user',      'content': 'was ist unklar?'},
        {'role': 'assistant', 'content': prev},
        {'role': 'user',      'content': 'bitte nochmal erklären'},
    ]
    result = app._chat_dedupe_answer(new, history)
    assert result == new  # User wollte explicit Wiederholung


def test_dedupe_handles_empty_history_gracefully():
    """Erste Nachricht: keine vorherigen Antworten — durchlassen."""
    new = ('Dein PDF ist bereit, beachte aber die Prüfpunkte: 23 Tage und 6 '
           'Streckeneinsatz-Zeilen sind markiert.')
    result = app._chat_dedupe_answer(new, [])
    assert result == new


def test_dedupe_inserted_in_chat_handler():
    """Statisches Audit: `_chat_dedupe_answer` wird im `/api/chat`-Handler aufgerufen."""
    with open(os.path.join(os.path.dirname(__file__), '..', 'app.py')) as f:
        src = f.read()
    chat_route_idx = src.find("@app.route('/api/chat', methods=['POST'])")
    assert chat_route_idx > 0
    # Handler ist groß — Window muss bis zur nächsten Route reichen.
    next_route = src.find('@app.route', chat_route_idx + 10)
    handler_block = src[chat_route_idx:next_route if next_route > 0 else chat_route_idx + 12000]
    assert '_chat_dedupe_answer' in handler_block
    # Aufruf passiert VOR dem Persistieren in chat_history
    call_idx = handler_block.find('_chat_dedupe_answer(answer')
    persist_idx = handler_block.find("'role': 'assistant', 'content': answer")
    assert 0 < call_idx < persist_idx, 'Dedupe muss vor dem Speichern stehen'
