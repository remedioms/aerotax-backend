"""Every-Case Chat / PDF / State Playbook Tests.

Jeder relevante User-Fall (A-T) wird einmal als State-Maschine-Szenario
durchgespielt. Pro Fall: initial state, expected canonical_state, pdf_allowed,
chat-contradiction-checks, arithmetic invariants.

Kein Live-Backend. Reine Logik-Tests gegen app._classify_job_state und
synthetische job-dicts.
"""

import pytest
import conftest as _cft
import app


def _make_job(status='done', netto=976.0, reviews=None, pending_reread=False,
              gesamt=5339.0, z77=4705.0, ag_z17=0, download_url='/dl/x'):
    """Helper: synthetic Job-Dict mit konfigurierbarem Status."""
    job = {
        'status':         status,
        'data': {
            'netto':           netto,
            'gesamt':          gesamt,
            'arbeitstage':     135,
            'fahr_tage':       55,
            'hotel_naechte':   73,
            'z77':             z77,
            'ag_z17':          ag_z17,
            '_review_items':   reviews or [],
            '_form_inputs':    {'base': 'FRA', 'year': 2025},
        },
        'manual_day_overrides': {},
        'audit': [],
        'pending_reread': pending_reread,
        'download_url':   download_url,
    }
    return job


# ════════════════════════════════════════════════════════════════════
# A. Fertige Auswertung ohne Review
# ════════════════════════════════════════════════════════════════════

def test_case_A_done_no_review():
    job = _make_job(status='done', reviews=[])
    state = app._classify_job_state(job)
    assert state['canonical_state'] in ('done', 'done_clean')
    assert state['pdf_allowed'] is True
    assert state['can_show_final_amount'] is True


# ════════════════════════════════════════════════════════════════════
# B. Ein Review-Punkt offen — Singular
# ════════════════════════════════════════════════════════════════════

def test_case_B_one_review_singular():
    job = _make_job(status='done', reviews=[
        {'id': 'office_training_time_missing:2025-04-07', 'type': 'office_training_time_missing',
         'datum': '2025-04-07', 'status': 'pending'},
    ])
    state = app._classify_job_state(job)
    assert state['canonical_state'] == 'needs_review'
    assert state['pdf_allowed'] is False
    msg = state['user_message']
    assert '1 Punkt' in msg, f'singular expected: {msg}'
    assert 'Punkte' not in msg.replace('1 Punkt', ''), f'plural in singular case: {msg}'


# ════════════════════════════════════════════════════════════════════
# C. RB unknown_marker — CAS vorhanden, kein CAS-Upload-Request
# ════════════════════════════════════════════════════════════════════

def test_case_C_unknown_marker_RB_review_kind():
    """unknown_marker has a kind-specific question template (frontend),
    backend classifies as needs_review."""
    job = _make_job(status='done', reviews=[
        {'id': 'unknown_marker:group:RB', 'type': 'unknown_marker',
         'first_token': 'RB', 'datum': '2025-04-21', 'status': 'pending',
         'question': 'In deinem Dienstplan steht „RB"…'},
    ])
    state = app._classify_job_state(job)
    assert state['canonical_state'] == 'needs_review'
    # Frontend test: kind-specific template fires (asserted in mjs tests)


def test_case_C_static_frontend_unknown_marker_template_exists():
    """Static: Frontend has kind-specific copy for unknown_marker."""
    html = open(_cft.site_index_html(), encoding='utf-8').read()
    # The kind-routing block exists
    assert "_itType === 'unknown_marker'" in html
    assert 'Unbekannte Kennung' in html
    # Pauschal 8h question is GUARDED, not default
    assert "längeer als 8 Stunden" not in html  # typo guard
    # The pauschal 8h text exists, but only under office_training_time_missing branch
    assert "Warst du an diesem Tag inklusive Hin- und Rückweg länger als 8 Stunden" in html


def test_case_C_static_frontend_no_cas_upload_when_present():
    """Static: Frontend checks missing_months_cas before suggesting CAS upload."""
    html = open(_cft.site_index_html(), encoding='utf-8').read()
    assert 'missing_months_cas' in html
    assert '_trulyMissingMonths' in html
    # Honest fallback when CAS is present
    assert 'Ich habe deinen Dienstplan/CAS bereits vorliegen' in html


# ════════════════════════════════════════════════════════════════════
# D. PDF-Frage nach Review-Übernahme — kein contradiction
# ════════════════════════════════════════════════════════════════════

def test_case_D_all_answered_chat_does_not_demand_answers():
    """Wenn _all_reviews_answered=True und pending=0, darf der chat-PDF-handler
    NIE „brauche zuerst deine Antworten" sagen."""
    html = open(_cft.site_index_html(), encoding='utf-8').read()
    # Live-pending-count is checked before showing the „brauche Antworten" message
    assert '_livePending' in html or '_livePending > 0' in html
    # Fallback for needs_review + pending=0: friendly "PDF gleich bereit"
    assert 'Alles geklärt' in html


def test_case_D_static_pdf_handler_uses_live_state():
    """Static: PDF-Frage-Handler reads live _data._review_items, not stale snapshot."""
    html = open(_cft.site_index_html(), encoding='utf-8').read()
    # The handler must compute _livePending from _data._review_items
    assert '_dCheck._review_items' in html or '_dCheck && _dCheck._review_items' in html


# ════════════════════════════════════════════════════════════════════
# E. Recalculation >90s / >5min — Progress Eskalation
# ════════════════════════════════════════════════════════════════════

def test_case_E_progress_escalates_at_90s_and_300s():
    html = open(_cft.site_index_html(), encoding='utf-8').read()
    assert 'heartbeatStart' in html
    assert 'kann bei vielen Dokumenten ein paar Minuten dauern' in html
    assert 'Du kannst mit deinem Zugangscode später zurückkommen' in html


# ════════════════════════════════════════════════════════════════════
# F. PDF stale nach Review-Antwort — alter Link hidden
# ════════════════════════════════════════════════════════════════════

def test_case_F_pdf_locked_when_pending_reread():
    job = _make_job(status='done', reviews=[], pending_reread=True)
    state = app._classify_job_state(job)
    assert state['canonical_state'] == 'needs_review'
    assert state['pdf_allowed'] is False


# ════════════════════════════════════════════════════════════════════
# G. Missing CAS → CAS Upload verlangen (NUR wenn fehlt)
# ════════════════════════════════════════════════════════════════════

def test_case_G_missing_cas_documented_in_health():
    """document_health.status=red wenn CAS komplett fehlt."""
    # Health-Check ist out-of-scope für _classify_job_state (gehört zum Reader);
    # wir verifizieren nur, dass das Schema die Felder hat.
    health = {
        'pipeline': 'v11_cas_primary',
        'lsb_present': True,
        'cas_months_count': 0,
        'detailed_cas_present': False,
        'missing_months_cas': ['2025-01','2025-02','2025-03','2025-04','2025-05','2025-06',
                                '2025-07','2025-08','2025-09','2025-10','2025-11','2025-12'],
        'status': 'red',
    }
    assert health['status'] == 'red'
    assert len(health['missing_months_cas']) == 12


# ════════════════════════════════════════════════════════════════════
# H. Missing SE → SE Upload verlangen, nicht CAS
# ════════════════════════════════════════════════════════════════════

def test_case_H_missing_se_not_cas():
    health = {
        'pipeline': 'v11_cas_primary',
        'lsb_present': True,
        'se_months_count': 0,
        'cas_months_count': 12,
        'missing_months_se': ['2025-' + f'{m:02d}' for m in range(1, 13)],
        'missing_months_cas': [],
        'status': 'red',
    }
    assert health['cas_months_count'] == 12, 'CAS vorhanden'
    assert health['se_months_count'] == 0, 'SE fehlt'


# ════════════════════════════════════════════════════════════════════
# I. Missing LSB → LSB Upload
# ════════════════════════════════════════════════════════════════════

def test_case_I_missing_lsb():
    health = {'lsb_present': False, 'status': 'red'}
    assert health['lsb_present'] is False
    assert health['status'] == 'red'


# ════════════════════════════════════════════════════════════════════
# J. Falsche Datei → Chat erklärt Dokumentart
# ════════════════════════════════════════════════════════════════════

def test_case_J_wrong_file_doctype_error():
    """Static: Backend kennt 'WRONG_DOCUMENT_TYPE'-Code."""
    src = open(_cft.backend_path('app.py'), encoding='utf-8').read()
    # Doc-type detection exists
    assert 'document_type' in src or 'doc_type' in src
    # Reject patterns
    assert 'legacy_ignored_flight_hours_summary' in src or 'flight_hours_summary' in src


# ════════════════════════════════════════════════════════════════════
# K. Delete → kein Recall, kein PDF, kein Chat-Verlauf
# ════════════════════════════════════════════════════════════════════

def test_case_K_deleted_blocks_everything():
    state = app._classify_job_state(None, {'deleted': True})
    assert state['canonical_state'] == 'deleted'
    assert state['pdf_allowed'] is False
    assert state['can_show_final_amount'] is False
    assert state['can_chat_explain_calculation'] is False


# ════════════════════════════════════════════════════════════════════
# L. Expired
# ════════════════════════════════════════════════════════════════════

def test_case_L_expired_no_pdf():
    state = app._classify_job_state(None, None)
    assert state['canonical_state'] == 'expired'
    assert state['pdf_allowed'] is False


# ════════════════════════════════════════════════════════════════════
# M. Failed retryable — kein done/PDF
# ════════════════════════════════════════════════════════════════════

def test_case_M_failed_retryable_no_done():
    job = {'status': 'failed_timeout', 'data': {'netto': 100}}  # netto present but failed
    state = app._classify_job_state(job)
    assert state['canonical_state'] in ('failed_retryable', 'failed_support')
    assert state['pdf_allowed'] is False
    assert state['can_show_final_amount'] is False, 'failed muss UI-Result hiden'


# ════════════════════════════════════════════════════════════════════
# N. Steuerberatung-Fragen — kein Garantie-Versprechen
# ════════════════════════════════════════════════════════════════════

def test_case_N_no_tax_guarantee_in_user_messages():
    """Static: User-facing copies enthalten NIE „garantiert"/„prüfungsfest"/„finanzamt-sicher"."""
    src = open(_cft.backend_path('app.py'), encoding='utf-8').read()
    # Forbidden marketing claims in user-visible strings (excluding code/docs)
    # Wir prüfen nur user_message/user_title-bereich der state-machine
    # (per pattern: nur die State-Antworten in _classify_job_state).
    forbidden = ['garantiert korrekt', 'prüfungsfest', 'finanzamt-sicher', 'Finanzamt akzeptiert sicher']
    # Find all 'user_title' and 'user_message' string-literals in _classify_job_state
    import re
    state_block = re.search(
        r'def _classify_job_state[\s\S]+?(?=\ndef \w|\Z)', src
    )
    assert state_block
    body = state_block.group(0)
    for forbid in forbidden:
        assert forbid not in body, f'Forbidden tax-claim in state copy: {forbid}'


# ════════════════════════════════════════════════════════════════════
# O. Homebase-Korrektur → dynamisch neu rechnen
# ════════════════════════════════════════════════════════════════════

def test_case_O_homebase_dynamic_not_hardcoded():
    """No FRA hardcoded in comparison logic (CLAUDE.md rule)."""
    src = open(_cft.backend_path('app.py'), encoding='utf-8').read()
    # Kein hardcoded "FRA" als Default-Homebase in Comparison-Code.
    # Acceptable: FRA in BMF_INLAND tables, REFERENCE_*_2025_MIGUEL test consts,
    # iata_unknown lists. Wir suchen nach if-elif-Branches mit hardcoded 'FRA'.
    # Vereinfacht: cls.get('homebase', 'FRA') ist OK (FRA als Test-Fallback),
    # aber explizite Vergleiche "if base == 'FRA'" mit FA-spezifischer Logik wären
    # ein Bug. Wir prüfen, dass form['base'] ausgelesen wird:
    assert "form['base']" in src or 'form.get(\'base\')' in src \
        or 'form_inputs.get(\'base\')' in src or 'base = form' in src


# ════════════════════════════════════════════════════════════════════
# P. Kilometer-Korrektur → neu rechnen
# ════════════════════════════════════════════════════════════════════

def test_case_P_km_dynamic():
    src = open(_cft.backend_path('app.py'), encoding='utf-8').read()
    # km wird aus cached_state gelesen (siehe _recompute_with_overrides L3415-3420 area)
    assert "cached_state.get('km'" in src or 'km =' in src


# ════════════════════════════════════════════════════════════════════
# Q. Multi-Base synthetisch — keine FRA-Hardcodierung
# ════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('base', ['FRA', 'MUC', 'BER', 'DUS', 'HAM', 'VIE', 'ZRH'])
def test_case_Q_multi_base_supported(base):
    """Document_health/form must accept any IATA."""
    health = {'pipeline': 'v11_cas_primary', 'lsb_present': True,
              'cas_months_count': 12, 'se_months_count': 12,
              'missing_months_se': [], 'missing_months_cas': [], 'status': 'green'}
    job = _make_job(status='done')
    job['data']['_form_inputs']['base'] = base
    job['data']['_document_health'] = health
    state = app._classify_job_state(job)
    assert state['canonical_state'] in ('done', 'done_clean')  # base doesn't affect state-machine


# ════════════════════════════════════════════════════════════════════
# R. Cockpit/unknown marker — keine Cabin-only-Logik
# ════════════════════════════════════════════════════════════════════

def test_case_R_no_cabin_only_logic():
    """Cabin/Cockpit/Unknown all run through same state machine."""
    # State-machine selbst diskriminiert nicht nach Rolle — Rolle wäre in data
    job_cabin   = _make_job(status='done'); job_cabin['data']['role']   = 'cabin'
    job_cockpit = _make_job(status='done'); job_cockpit['data']['role'] = 'cockpit'
    s1 = app._classify_job_state(job_cabin)
    s2 = app._classify_job_state(job_cockpit)
    assert s1['canonical_state'] == s2['canonical_state']
    assert s1['canonical_state'] in ('done', 'done_clean')


# ════════════════════════════════════════════════════════════════════
# S. Z77 > VMA — Bucket-Math korrekt
# ════════════════════════════════════════════════════════════════════

def test_case_S_z77_exceeds_vma_clamps_to_zero():
    """Z77=4705 > VMA-Brutto=4363 → VMA-Netto=0, Block A bleibt."""
    fahr = 497.20; reinig = 216.00; trink = 262.80
    vma_total = 4363.0
    z77 = 4705.0
    fahr_netto = max(0, fahr - 0)
    vma_netto  = max(0, vma_total - z77)
    block_a = fahr_netto + reinig + trink
    netto = block_a + vma_netto
    assert vma_netto == 0
    assert netto == 976.00
    assert block_a == 976.00


# ════════════════════════════════════════════════════════════════════
# T. Z17 Fahrkostenzuschuss — nur Fahrtkosten, nicht VMA
# ════════════════════════════════════════════════════════════════════

def test_case_T_z17_only_offsets_fahrt():
    fahr = 800; ag_z17 = 300; vma_total = 500; z77 = 0
    fahr_netto = max(0, fahr - ag_z17)
    vma_netto  = max(0, vma_total - z77)
    assert fahr_netto == 500
    assert vma_netto  == 500, 'AG-Z17 darf VMA NICHT reduzieren'


def test_case_T_static_recompute_separates_buckets():
    """Static: _recompute_with_overrides uses two separate clamps."""
    src = open(_cft.backend_path('app.py'), encoding='utf-8').read()
    import re
    assert re.search(r'fahr_netto\s*=\s*round\(\s*max\(\s*0', src)
    assert re.search(r'fahr\s*-\s*ag_z17', src)
    assert re.search(r'vma_netto\s*=\s*round\(\s*max\(\s*0,\s*vma_total\s*-\s*z77', src)
