"""Homebase Safety Tests (2026-05-22 Release-Blocker-Fix).

Verifiziert:
1. _extract_homebase fallbackt NICHT auf FRA bei leer/unbekannt
2. _is_supported_homebase erkennt DE-Bases als supported
3. _classify_job_state pivot auf needs_document_attention bei missing/unsupported
4. VIE/ZRH werden als unsupported markiert
5. Frontend Dropdown bietet VIE/ZRH nur disabled
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import app  # noqa: E402
import conftest as _cft  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# FIX 1 — _extract_homebase
# ─────────────────────────────────────────────────────────────────────────────

def test_empty_homebase_does_not_default_to_fra():
    """Leerer Input → None, NICHT FRA."""
    assert app._extract_homebase('') is None
    assert app._extract_homebase(None) is None


def test_unknown_homebase_does_not_default_to_fra():
    """Unbekannter Stadtname → None, NICHT FRA."""
    assert app._extract_homebase('Mars') is None
    assert app._extract_homebase('Atlantis (XYZ)') == 'XYZ'  # 3-Letter-Code matcht weiterhin


def test_supported_de_bases_are_extracted():
    """Alle 13 supported DE-Bases werden korrekt extrahiert."""
    for base, expected in [
        ('Frankfurt (FRA)', 'FRA'),
        ('München (MUC)', 'MUC'),
        ('Düsseldorf (DUS)', 'DUS'),
        ('Berlin (BER)', 'BER'),
        ('Hamburg (HAM)', 'HAM'),
        ('Stuttgart (STR)', 'STR'),
        ('Köln/Bonn (CGN)', 'CGN'),
        ('Hannover (HAJ)', 'HAJ'),
        ('Nürnberg (NUE)', 'NUE'),
        ('Leipzig (LEJ)', 'LEJ'),
        ('Bremen (BRE)', 'BRE'),
    ]:
        assert app._extract_homebase(base) == expected, f'{base} → {expected}'


def test_vie_zrh_extracted_but_not_supported():
    """VIE/ZRH werden erkannt, aber _is_supported_homebase returns False."""
    assert app._extract_homebase('Wien (VIE)') == 'VIE'
    assert app._extract_homebase('Zürich (ZRH)') == 'ZRH'
    assert app._is_supported_homebase('VIE') is False
    assert app._is_supported_homebase('ZRH') is False


def test_supported_de_bases_pass_is_supported():
    for iata in ('FRA', 'MUC', 'BER', 'DUS', 'HAM', 'STR', 'CGN', 'HAJ',
                 'NUE', 'LEJ', 'BRE'):
        assert app._is_supported_homebase(iata), f'{iata} sollte supported sein'


def test_none_or_empty_is_not_supported():
    assert app._is_supported_homebase(None) is False
    assert app._is_supported_homebase('') is False


# ─────────────────────────────────────────────────────────────────────────────
# FIX 2 — _classify_job_state pivot auf needs_document_attention
# ─────────────────────────────────────────────────────────────────────────────

def _make_done_job(homebase_audit=None):
    return {
        'status': 'done',
        'data': {
            'netto': 1000.0,
            'brutto': 50000.0,
            '_homebase_audit': homebase_audit or {},
            '_review_items': [],
            '_unresolved_days': [],
            '_vma_unmapped_se': [],
        },
    }


def test_missing_homebase_creates_needs_document_attention():
    job = _make_done_job({'raw_input': '', 'iata': None, 'supported': False, 'reason': 'missing'})
    state = app._classify_job_state(job)
    assert state['canonical_state'] == 'needs_document_attention'
    assert state['reason_code'] == 'MISSING_HOMEBASE'
    assert state['pdf_allowed'] is False


def test_unknown_homebase_creates_needs_document_attention():
    job = _make_done_job({'raw_input': 'Mars', 'iata': None, 'supported': False, 'reason': 'unknown'})
    state = app._classify_job_state(job)
    assert state['canonical_state'] == 'needs_document_attention'
    assert state['reason_code'] == 'MISSING_HOMEBASE'


def test_vie_homebase_blocks_with_unsupported_country_code():
    job = _make_done_job({
        'raw_input': 'Wien (VIE)', 'iata': 'VIE',
        'supported': False, 'reason': 'unsupported_country',
    })
    state = app._classify_job_state(job)
    assert state['canonical_state'] == 'needs_document_attention'
    assert state['reason_code'] == 'UNSUPPORTED_HOMEBASE'
    assert state['pdf_allowed'] is False
    assert 'VIE' in state['user_message'] or 'AT/CH' in state['user_message']


def test_zrh_homebase_blocks_with_unsupported_country_code():
    job = _make_done_job({
        'raw_input': 'Zürich (ZRH)', 'iata': 'ZRH',
        'supported': False, 'reason': 'unsupported_country',
    })
    state = app._classify_job_state(job)
    assert state['canonical_state'] == 'needs_document_attention'
    assert state['reason_code'] == 'UNSUPPORTED_HOMEBASE'


def test_fra_homebase_passes_through_to_done_clean():
    job = _make_done_job({
        'raw_input': 'Frankfurt (FRA)', 'iata': 'FRA',
        'supported': True, 'reason': 'ok',
    })
    state = app._classify_job_state(job)
    assert state['canonical_state'] in ('done', 'done_clean')
    assert state['pdf_allowed'] is True


def test_muc_homebase_passes_through_to_done_clean():
    job = _make_done_job({
        'raw_input': 'München (MUC)', 'iata': 'MUC',
        'supported': True, 'reason': 'ok',
    })
    state = app._classify_job_state(job)
    assert state['canonical_state'] in ('done', 'done_clean')
    assert state['pdf_allowed'] is True


def test_missing_homebase_pdf_not_allowed():
    """Pflicht-Test: bei missing homebase niemals pdf_allowed=True."""
    for reason in ('missing', 'unknown', 'unsupported_country'):
        job = _make_done_job({'raw_input': '', 'iata': None, 'supported': False, 'reason': reason})
        state = app._classify_job_state(job)
        assert state['pdf_allowed'] is False, f'reason={reason} darf nicht PDF erlauben'


def test_no_silent_fra_fallback_in_backend():
    """Static audit: _extract_homebase enthält keinen return 'FRA' Fallback mehr."""
    with open(os.path.join(os.path.dirname(__file__), '..', 'app.py'), encoding='utf-8') as f:
        src = f.read()
    fn_idx = src.find('def _extract_homebase(')
    # Function body bis zur nächsten def-Definition
    next_def = src.find('\ndef ', fn_idx + 1)
    fn_block = src[fn_idx:next_def]
    # In der Funktion darf KEIN `return 'FRA'` mehr stehen (war früher 2x)
    assert "return 'FRA'" not in fn_block, (
        'FRA-Fallback ist zurück! _extract_homebase darf bei missing/unknown None liefern.'
    )
    # Mindestens ein `return None` als Fallback-Path
    assert 'return None' in fn_block


# ─────────────────────────────────────────────────────────────────────────────
# FIX 3 — Frontend Homebase Dropdown
# ─────────────────────────────────────────────────────────────────────────────

# site-Repo-Pfad wird pro Test via _cft.site_index_html() aufgelöst (skippt wenn fehlend)


def test_frontend_vie_zrh_disabled():
    """VIE/ZRH NICHT mehr im Dropdown — wurden auf User-Anforderung entfernt
    (2026-05-24, Liquid-Glass-Cleanup). Backend rejected sie trotzdem via
    _is_supported_homebase, frontend zeigt sie nicht mehr als Option."""
    with open(_cft.site_index_html(), encoding='utf-8') as f:
        html = f.read()
    # Sicherheits-Check: keine VIE/ZRH-Options im Dropdown
    assert 'Wien (VIE)' not in html, 'VIE sollte nicht mehr im Dropdown sein'
    assert 'Zürich (ZRH)' not in html, 'ZRH sollte nicht mehr im Dropdown sein'


def test_frontend_supported_de_bases_present():
    """DE-Bases müssen im Dropdown stehen — supported homebases."""
    with open(_cft.site_index_html(), encoding='utf-8') as f:
        html = f.read()
    for base in ('Frankfurt (FRA)', 'München (MUC)', 'Berlin (BER)',
                 'Düsseldorf (DUS)', 'Hamburg (HAM)', 'Stuttgart (STR)',
                 'Köln/Bonn (CGN)', 'Hannover (HAJ)', 'Nürnberg (NUE)',
                 'Leipzig (LEJ)', 'Bremen (BRE)'):
        assert f'<option>{base}</option>' in html, f'{base} fehlt im Frontend'


def test_frontend_homebase_warning_text_removed():
    """Hinweis-Text unter Dropdown wurde auf User-Anforderung 2026-05-24
    entfernt (UI-Cleanup). Backend gibt die Validierung via canonical_state
    'needs_document_attention' weiter wenn unsupported base gewählt."""
    with open(_cft.site_index_html(), encoding='utf-8') as f:
        html = f.read()
    # Der alte Warning-Text wurde explizit entfernt
    assert 'Aktuell für deutsche Steuerlogik optimiert' not in html
    assert 'Wien und Zürich folgen in einer späteren Version' not in html


def test_frontend_anderer_flughafen_removed():
    """„Anderer Flughafen" raus — verhindert dass User unknown-input macht."""
    with open(_cft.site_index_html(), encoding='utf-8') as f:
        html = f.read()
    assert '<option>Anderer Flughafen</option>' not in html
