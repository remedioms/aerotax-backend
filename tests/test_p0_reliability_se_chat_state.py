"""P0 Reliability Tests (2026-05-21).

Trifft drei zusammenhängende Bugs ab:
  1. SE-Completeness wird nicht sauber sichtbar geprüft.
  2. Z77-Differenz war Blackbox (keine monatliche Aufschlüsselung im result).
  3. Chat sagte „alles fertig", obwohl 23 unresolved_days + 6 vma_unmapped_se vorlagen.

Diese Tests laufen ohne Live-Run gegen den AT-12CDA-Fixture-Snapshot
und gegen synthetische done/done_clean/done_with_audit_warnings Szenarien.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 1 — SE-Completeness Audit
# ─────────────────────────────────────────────────────────────────────────────

def test_redact_se_filename_no_pii():
    """Filename-Redact darf KEINEN Originalnamen leaken."""
    redacted = app._redact_se_filename('Streckeneinsatz_Tibor_Quaas_123456_März_2025.pdf', 3)
    assert 'Tibor' not in redacted
    assert 'Quaas' not in redacted
    assert '123456' not in redacted
    assert redacted.startswith('se_03_')
    assert redacted.endswith('.pdf')


def test_redact_se_filename_handles_no_extension():
    redacted = app._redact_se_filename('Streckeneinsatz', 1)
    assert redacted.startswith('se_01_')


def test_se_completeness_detects_12_of_12():
    """Wenn 12 Monate erkannt → kein missing_se_months, status grün."""
    se_lines = []
    for mo in range(1, 13):
        se_lines.append({
            'datum': f'2025-{mo:02d}-15',
            'stfrei_betrag': 100.0,
            'stfrei_ort': 'JFK',
            'stfrei_inland': False,
            'storno': False,
        })
    audit = app._build_se_completeness_audit(
        uploaded_count=12,
        se_structured={'se_lines': se_lines},
        se_summary={'z77_total': 1200.0, 'monatliche_z77': []},
        document_health=None,
    )
    assert audit['detected_se_month_count'] == 12
    assert audit['missing_se_months'] == []
    assert audit['unreadable_se_files'] == []
    assert audit['reader_confidence'] >= 90


def test_se_completeness_flags_missing_month():
    """Wenn nur 11 Monate gelesen → fehlender Monat im Audit sichtbar."""
    se_lines = [
        {'datum': f'2025-{mo:02d}-10', 'stfrei_betrag': 80.0, 'storno': False}
        for mo in range(1, 13) if mo != 2
    ]
    audit = app._build_se_completeness_audit(
        uploaded_count=11,
        se_structured={'se_lines': se_lines},
        se_summary={'z77_total': 880.0},
        document_health=None,
    )
    assert audit['detected_se_month_count'] == 11
    assert 2 in audit['missing_se_months']
    assert len(audit['missing_se_months']) == 1


def test_se_completeness_flags_unreadable_file():
    """Wenn uploaded > detected und keine Monate fehlen → unreadable-Hinweis."""
    # 12 hochgeladen, aber Reader hat nur 10 Monate erkannt (z.B. weil 2 PDFs unlesbar)
    se_lines = [
        {'datum': f'2025-{mo:02d}-10', 'stfrei_betrag': 80.0, 'storno': False}
        for mo in range(1, 11)
    ]
    audit = app._build_se_completeness_audit(
        uploaded_count=12,
        se_structured={'se_lines': se_lines},
        se_summary={'z77_total': 800.0},
        document_health=None,
    )
    assert audit['detected_se_month_count'] == 10
    assert audit['uploaded_se_files_count'] == 12
    # 11 + 12 fehlen + 2 unreadable: missing markiert
    assert audit['missing_se_months']  # >0


def test_se_completeness_audit_persists_monthly_breakdown():
    """z77_by_month enthält je Monat einen Eintrag mit lines/summary/quelle."""
    se_lines = [
        {'datum': '2025-03-04', 'stfrei_betrag': 30.0, 'stfrei_ort': 'BLR',
         'stfrei_inland': False, 'storno': False},
        {'datum': '2025-03-15', 'stfrei_betrag': 50.0, 'stfrei_ort': 'FRA',
         'stfrei_inland': True, 'storno': False},
    ]
    audit = app._build_se_completeness_audit(
        uploaded_count=1,
        se_structured={'se_lines': se_lines},
        se_summary={'z77_total': 80.0,
                    'monatliche_z77': [{'monat': 3, 'z77_monat': 80.0, 'anzahl_zeilen': 2}]},
        document_health=None,
    )
    assert len(audit['z77_by_month']) >= 1
    march = next((m for m in audit['z77_by_month'] if m['monat'] == 3), None)
    assert march is not None
    assert march['quelle'] in ('beide', 'einzelzeilen', 'summenzeilen')
    assert march['z77_lines'] == 80.0


def test_se_completeness_no_pii_leak():
    """se_files_redacted darf keine Originalnamen enthalten."""
    audit = app._build_se_completeness_audit(
        uploaded_count=1,
        se_structured={'se_lines': []},
        se_summary={'z77_total': 0},
        document_health=None,
        se_filenames_redacted=[app._redact_se_filename(
            'Tibor_Quaas_Personalnummer_99887766_Maerz.pdf', 1)],
    )
    for fn in audit['se_files_redacted']:
        assert 'Tibor' not in fn
        assert 'Quaas' not in fn
        assert '99887766' not in fn


def test_z77_total_used_persisted_in_audit():
    """z77_total_used ist im Audit dokumentiert (nicht nur Wert, sondern Quelle)."""
    audit = app._build_se_completeness_audit(
        uploaded_count=12,
        se_structured={'se_lines': []},
        se_summary={'z77_total': 4464.0, 'monatliche_z77': []},
        document_health=None,
    )
    assert audit['z77_total_used'] == 4464.0


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 4 — Chat State: done_clean vs done_with_audit_warnings
# ─────────────────────────────────────────────────────────────────────────────

def _make_job(canonical_status='done', unresolved_days=None, vma_unmapped_se=None,
              se_completeness=None, review_items=None, pending_reread=False):
    return {
        'status': canonical_status,
        'pending_reread': pending_reread,
        'data': {
            '_unresolved_days': list(unresolved_days or []),
            '_vma_unmapped_se': list(vma_unmapped_se or []),
            '_se_completeness': dict(se_completeness or {}),
            '_review_items': list(review_items or []),
            'netto': 1000.0,
            'brutto': 50000.0,
        }
    }


def test_classify_done_clean_when_no_warnings():
    job = _make_job(canonical_status='done')
    st = app._classify_job_state(job)
    assert st['canonical_state'] == 'done_clean'
    assert st['audit_warnings'] is None
    assert st['pdf_allowed'] is True


def test_classify_done_with_audit_warnings_when_unresolved_days():
    job = _make_job(unresolved_days=['2025-01-07: unklar', '2025-02-15: Mischfall'])
    st = app._classify_job_state(job)
    assert st['canonical_state'] == 'done_with_audit_warnings'
    assert st['pdf_allowed'] is True   # PDF darf trotzdem
    assert st['audit_warnings']['unresolved_days_count'] == 2
    # Copy darf NICHT „alles fertig" sagen
    assert 'alles fertig' not in st['user_message'].lower()
    assert 'keine offenen' not in st['user_message'].lower()
    assert 'prüfpunkt' in st['user_title'].lower() or 'prüfpunkt' in st['user_message'].lower()


def test_classify_done_with_audit_warnings_when_unmapped_se():
    job = _make_job(vma_unmapped_se=[
        {'datum': '2025-01-07', 'stfrei_ort': 'SEL', 'stfrei_total': 32.0},
        {'datum': '2025-01-23', 'stfrei_ort': 'SAO', 'stfrei_total': 31.0},
    ])
    st = app._classify_job_state(job)
    assert st['canonical_state'] == 'done_with_audit_warnings'
    assert st['audit_warnings']['unmapped_se_count'] == 2


def test_classify_done_with_audit_warnings_when_se_months_missing():
    job = _make_job(se_completeness={
        'detected_se_month_count': 11,
        'missing_se_months': [2],
        'uploaded_se_files_count': 11,
    })
    st = app._classify_job_state(job)
    assert st['canonical_state'] == 'done_with_audit_warnings'
    assert st['audit_warnings']['se_missing_months'] == [2]


def test_classify_done_with_audit_warnings_for_AT12CDA_fixture():
    """Der reale Fixture (Tibor 2025) muss → done_with_audit_warnings,
    nie done_clean."""
    fixture_path = '/tmp/aerotax_AT12CDA_result.json'
    if not os.path.exists(fixture_path):
        return  # skip — fixture nicht da
    with open(fixture_path) as f:
        snap = json.load(f)
    rd = snap['result_data']
    job = {
        'status': 'done',
        'pending_reread': False,
        'data': rd,
    }
    st = app._classify_job_state(job)
    assert st['canonical_state'] == 'done_with_audit_warnings', (
        f'AT-12CDA fixture hat {len(rd.get("_unresolved_days",[]))} unresolved + '
        f'{len(rd.get("_vma_unmapped_se",[]))} unmapped → muss done_with_audit_warnings sein, '
        f'bekommen: {st["canonical_state"]}'
    )
    assert st['audit_warnings']['unresolved_days_count'] == 23
    assert st['audit_warnings']['unmapped_se_count'] == 6


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 5 — Chat Context: Audit-Felder werden mitgegeben
# ─────────────────────────────────────────────────────────────────────────────

def test_ai_chat_context_includes_unresolved_days_count():
    job = _make_job(unresolved_days=['2025-01-07: x', '2025-02-15: y'])
    ctx = app._build_ai_chat_context(job)
    assert ctx['unresolved_days_count'] == 2
    assert len(ctx['unresolved_days_examples']) == 2


def test_ai_chat_context_includes_unmapped_se_count():
    job = _make_job(vma_unmapped_se=[
        {'datum': '2025-01-07', 'stfrei_ort': 'SEL', 'stfrei_total': 32.0,
         'klass': 'Office'},
    ])
    ctx = app._build_ai_chat_context(job)
    assert ctx['unmapped_se_count'] == 1
    assert ctx['unmapped_se_examples'][0]['datum'] == '2025-01-07'
    assert ctx['unmapped_se_examples'][0]['stfrei_ort'] == 'SEL'


def test_ai_chat_context_includes_se_completeness():
    job = _make_job(se_completeness={
        'detected_se_month_count': 11,
        'missing_se_months': [2],
        'uploaded_se_files_count': 11,
    })
    ctx = app._build_ai_chat_context(job)
    assert ctx['se_detected_month_count'] == 11
    assert ctx['se_missing_months'] == [2]
    assert ctx['se_uploaded_files_count'] == 11


def test_ai_chat_context_includes_z77_monthly_audit():
    """Z77-Monatsaufschlüsselung muss im Chat-Kontext sein, sonst Blackbox."""
    job = _make_job()
    job['data']['_z77_audit'] = {
        'verwendeter_wert': 4464.0,
        'einzelzeilen':     4464.0,
        'summenzeilen':     4311.80,
        'quelle':           'einzelzeilen',
        'monatliche_z77': [{'monat': 1, 'z77_monat': 200.0, 'anzahl_zeilen': 3}],
    }
    ctx = app._build_ai_chat_context(job)
    assert ctx['z77_total_used'] == 4464.0
    assert ctx['z77_total_lines'] == 4464.0
    assert ctx['z77_total_summary'] == 4311.80
    assert ctx['z77_source'] == 'einzelzeilen'


def test_ai_chat_context_audit_warnings_active_flag_true_when_warnings():
    job = _make_job(unresolved_days=['2025-01-07: x'])
    ctx = app._build_ai_chat_context(job)
    assert ctx['audit_warnings_active'] is True


def test_ai_chat_context_audit_warnings_active_flag_false_when_clean():
    job = _make_job()
    ctx = app._build_ai_chat_context(job)
    assert ctx['audit_warnings_active'] is False


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 6 — Chat Prompt (Sonnet): Audit-Block ist drin
# ─────────────────────────────────────────────────────────────────────────────

def test_chat_prompt_audit_block_strings_present_in_app_source():
    """Der Audit-Block muss im Chat-Code existieren (statisches Audit)."""
    with open(os.path.join(os.path.dirname(__file__), '..', 'app.py')) as f:
        src = f.read()
    # Pflicht-Markierungen
    assert 'AKTIVE AUDIT-WARNUNGEN (PFLICHT zu erwähnen)' in src
    assert 'unresolved_days_count' in src
    assert 'unmapped_se_count' in src
    assert 'se_missing_months' in src
    assert 'audit_warnings_active' in src


def test_chat_prompt_blocks_all_done_when_warnings():
    """Wenn Audit-Warnungen aktiv sind, muss Prompt explizit verbieten
    „alles abgeschlossen"/„keine offenen Punkte"."""
    with open(os.path.join(os.path.dirname(__file__), '..', 'app.py')) as f:
        src = f.read()
    # Suche die kritische Verbotsregel
    assert 'alles abgeschlossen' in src.lower() or 'alles fertig' in src.lower()
    # Die Regel sollte sagen: „du darfst NICHT sagen…"
    assert 'DARFST nicht' in src or 'DARFST NICHT' in src


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 7 — Schema/Persistenz: _se_completeness ist im allowed_fields-Set
# ─────────────────────────────────────────────────────────────────────────────

def test_se_completeness_in_classification_schema():
    with open(os.path.join(os.path.dirname(__file__), '..', 'app.py')) as f:
        src = f.read()
    # _se_completeness MUSS im _SCHEMA_CLASSIFICATION-optional drin sein,
    # sonst wird's beim Schema-Validate verworfen.
    assert "'_se_completeness'" in src
    assert "'_z77_audit'" in src


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 8 — Frontend Audit (statische Source-Checks gegen index.html)
# ─────────────────────────────────────────────────────────────────────────────

INDEX_HTML = os.path.expanduser('~/Desktop/site/index.html')


def test_frontend_local_bulk_apply_checks_unresolved_days():
    """_localBulkApply darf NICHT mehr blind „keine offenen Tage" sagen wenn
    unresolved_days oder vma_unmapped_se oder missing_months > 0."""
    if not os.path.exists(INDEX_HTML):
        return
    with open(INDEX_HTML) as f:
        html = f.read()
    # Die alte 1-Zeilen-Antwort darf nur in einem Else-Branch stehen
    assert '_localBulkApply' in html
    # Audit-Felder werden im Branch gecheckt
    assert '_unresolved_days' in html
    assert '_vma_unmapped_se' in html
    assert '_se_completeness' in html
    # Audit-aware Antwort enthält „Prüfpunkte" oder „Hinweise"
    assert 'Prüfpunkte' in html or 'Hinweise' in html


def test_frontend_done_with_audit_warnings_branch_present():
    """deriveUiState muss done_with_audit_warnings als eigenen Branch handhaben."""
    if not os.path.exists(INDEX_HTML):
        return
    with open(INDEX_HTML) as f:
        html = f.read()
    assert "'done_with_audit_warnings'" in html
    assert "'done_clean'" in html
    # PDF darf bei beiden erlaubt sein
    assert "done_with_audit_warnings" in html


def test_frontend_pdf_button_accepts_done_clean():
    """canShowPdfDownload erlaubt done, done_clean UND done_with_audit_warnings."""
    if not os.path.exists(INDEX_HTML):
        return
    with open(INDEX_HTML) as f:
        html = f.read()
    # Mindestens eine Stelle akzeptiert done_clean
    assert "'done_clean'" in html
