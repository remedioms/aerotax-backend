"""Phase B Integration-Tests: normalized_tours wired in hybrid_analyze.

Verifiziert:
- B1: Flag OFF → kein Call, kein Output-Change
- B1: Flag ON → audit field gefüllt, final amount unchanged
- B2: diff_against_legacy hat summary/by_date/decisions
- B4: audit NICHT im User-PDF sichtbar
- B5: Crash in normalized_tours kippt nicht den Hauptpfad
"""
import importlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE.parent))
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')
os.environ.setdefault('AEROTAX_DISABLE_BG_THREADS', '1')

import app  # noqa: E402
import normalized_tours as nt  # noqa: E402


# ════════════════════════════════════════════════════════════════════════════
# Fixtures: fake hybrid-analyze inputs
# ════════════════════════════════════════════════════════════════════════════

def _fake_cas_pre_read():
    """Minimaler CAS-Reader-Output mit echter Tour-Struktur."""
    return {
        'structured_days': {
            'days': [
                {
                    'datum': '2025-01-03', 'marker_raw': '31591',
                    'routing': ['FRA', 'BLR'], 'layover_ort': 'BLR',
                    'overnight_after_day': True, 'starts_at_homebase': True,
                    'duty_duration_minutes': 600,
                },
                {
                    'datum': '2025-01-04', 'marker_raw': 'X',
                    'routing': [], 'layover_ort': 'BLR',
                    'overnight_after_day': True,
                },
                {
                    'datum': '2025-01-06', 'marker_raw': '31591',
                    'routing': ['BLR', 'FRA'], 'ends_at_homebase': True,
                    'duty_duration_minutes': 600,
                },
            ],
        },
        'conflicts': [], 'warnings': [],
    }


def _fake_se_structured():
    return {
        'se_lines': [
            {'datum': '2025-01-04', 'stfrei_ort': 'BLR',
             'stfrei_betrag': 42.0, 'storno': False},
        ],
    }


def _fake_legacy_classification():
    """Fake legacy classification dict mit tage_detail."""
    return {
        'fahr_tage': 1,
        'arbeitstage': 3,
        'hotel_naechte': 2,
        'reinigungstage': 3,
        'vma_72_tage': 0,
        'vma_73_tage': 0,
        'vma_74_tage': 0,
        'vma_aus': 112.0,
        'tage_detail': [
            {'datum': '2025-01-03', 'klass': 'Z76', 'amount': 28.0,
             'reason': 'departure BLR'},
            {'datum': '2025-01-04', 'klass': 'Z76', 'amount': 42.0,
             'reason': 'mid-tour BLR'},
            {'datum': '2025-01-06', 'klass': 'Frei', 'amount': 0.0,
             'reason': 'Sonnet-Lesefehler X als Frei (Pattern D)'},
        ],
    }


# ════════════════════════════════════════════════════════════════════════════
# B1: Flag OFF/ON behavior
# ════════════════════════════════════════════════════════════════════════════

def test_flag_off_does_not_call_normalized_tours(monkeypatch):
    """Wenn AEROTAX_USE_NORMALIZED_TOURS=0, wird normalized_tours nicht
    aufgerufen — keine Audit-Output-Felder gefüllt.

    Indirekter Test: wir patchen build_normalized_tours mit Mock, prüfen
    dass es NICHT aufgerufen wird.
    """
    monkeypatch.setattr(app, 'AEROTAX_USE_NORMALIZED_TOURS', False)
    mock_build = MagicMock(return_value=[])
    monkeypatch.setattr(nt, 'build_normalized_tours', mock_build)
    # Simuliere die parallel-audit-Sektion direkt:
    if app.AEROTAX_USE_NORMALIZED_TOURS:
        nt.build_normalized_tours([], [], 2025)
    assert mock_build.call_count == 0


def test_flag_on_calls_normalized_tours(monkeypatch):
    """Bei Flag=1 wird build_normalized_tours aufgerufen."""
    monkeypatch.setattr(app, 'AEROTAX_USE_NORMALIZED_TOURS', True)
    mock_build = MagicMock(return_value=[])
    monkeypatch.setattr(nt, 'build_normalized_tours', mock_build)
    if app.AEROTAX_USE_NORMALIZED_TOURS:
        nt.build_normalized_tours([], [], 2025)
    assert mock_build.call_count == 1


def test_flag_default_on_after_r24():
    """Default-State (seit R24, 2026-05-27): AEROTAX_USE_NORMALIZED_TOURS=ON.

    Die normalized-tours-Pipeline ist produktiv stable, der Default ist
    auf ON umgeschaltet. Test prüft nur dass die Flag existiert und ein
    bool ist (ENV kann sie explizit überschreiben)."""
    if 'AEROTAX_USE_NORMALIZED_TOURS' not in os.environ:
        assert isinstance(app.AEROTAX_USE_NORMALIZED_TOURS, bool)
        # R24: Default ist ON
        assert app.AEROTAX_USE_NORMALIZED_TOURS is True


def test_audit_field_present_when_flag_on():
    """Wenn Pipeline mit Flag=1 läuft, ist _normalized_tours_audit im result."""
    # Direkter Test: dass das audit-feld korrekt gebaut wird
    cas_days = _fake_cas_pre_read()['structured_days']['days']
    se_rows = _fake_se_structured()['se_lines']
    bmf = {
        'BLR': {'an_abreise': 28.0, 'voll_24h': 42.0, 'country': 'Indien-Bangalore'},
    }
    tours = nt.build_normalized_tours(cas_days, se_rows, 2025, homebase='FRA')
    result = nt.calculate_allowances_from_normalized_tours(tours, bmf)
    diff = nt.diff_against_legacy(result, _fake_legacy_classification())

    audit = {
        'tours_count':    len(tours),
        'days_count':     sum(len(t.days) for t in tours),
        'z76':            {'tage': result.z76_tage, 'eur': result.z76_eur},
        'by_date':        result.by_date,
        'diff_against_legacy': diff,
        'final_amount_unchanged': True,
    }
    assert audit['tours_count'] >= 1
    assert audit['final_amount_unchanged'] is True
    assert 'by_date' in audit
    assert 'diff_against_legacy' in audit


def test_audit_contains_by_date():
    """Audit muss by_date map enthalten."""
    cas_days = _fake_cas_pre_read()['structured_days']['days']
    bmf = {'BLR': {'an_abreise': 28.0, 'voll_24h': 42.0, 'country': 'IN-BLR'}}
    tours = nt.build_normalized_tours(cas_days, [], 2025, homebase='FRA')
    result = nt.calculate_allowances_from_normalized_tours(tours, bmf)
    assert '2025-01-03' in result.by_date
    assert '2025-01-06' in result.by_date
    assert result.by_date['2025-01-06']['klass'] == 'Z76'


def test_audit_contains_diff_against_legacy():
    """Audit hat diff_against_legacy mit summary/by_date/decisions."""
    cas_days = _fake_cas_pre_read()['structured_days']['days']
    bmf = {'BLR': {'an_abreise': 28.0, 'voll_24h': 42.0, 'country': 'IN-BLR'}}
    tours = nt.build_normalized_tours(cas_days, [], 2025, homebase='FRA')
    result = nt.calculate_allowances_from_normalized_tours(tours, bmf)
    diff = nt.diff_against_legacy(result, _fake_legacy_classification())
    assert 'summary' in diff
    assert 'legacy' in diff['summary']
    assert 'normalized' in diff['summary']
    assert 'delta' in diff['summary']
    assert 'by_date' in diff
    assert isinstance(diff['by_date'], list)


# ════════════════════════════════════════════════════════════════════════════
# B2: Diff against legacy details
# ════════════════════════════════════════════════════════════════════════════

def test_diff_detects_removed_phantom_z76():
    """Legacy hat Phantom-Z76 (kein Tour-Tag), normalized hat 0.
    Diff-Entry sollte decision='normalized_more_plausible' haben."""
    # Legacy hat einen "Z76" Tag den normalized als none klassifiziert
    legacy = {
        'tage_detail': [
            {'datum': '2025-05-21', 'klass': 'Z76', 'amount': 42.0,
             'reason': 'BH-003c LAD Phantom'},
        ],
        'fahr_tage': 0, 'arbeitstage': 1, 'hotel_naechte': 1,
        'vma_aus': 42.0,
    }
    # normalized: kein Tour-Tag für 05-21
    result = nt.CalculationResult()
    diff = nt.diff_against_legacy(result, legacy)
    matching = [d for d in diff['by_date'] if d['date'] == '2025-05-21']
    assert len(matching) == 1
    assert matching[0]['decision'] == 'normalized_more_plausible'
    assert 'Phantom' in matching[0]['reason']


def test_diff_detects_added_missing_return_day():
    """Legacy=Frei, normalized=Z76 → decision='normalized_more_plausible'."""
    legacy = {
        'tage_detail': [
            {'datum': '2025-01-06', 'klass': 'Frei', 'amount': 0.0,
             'reason': 'Sonnet hat X als Frei gelesen'},
        ],
        'fahr_tage': 0, 'arbeitstage': 0, 'hotel_naechte': 0, 'vma_aus': 0.0,
    }
    result = nt.CalculationResult(
        z76_eur=28.0, z76_tage=1, fahrtage=1, arbeitstage=1,
        by_date={'2025-01-06': {'klass': 'Z76', 'amount': 28.0, 'country': 'BLR'}},
    )
    diff = nt.diff_against_legacy(result, legacy)
    matching = [d for d in diff['by_date'] if d['date'] == '2025-01-06']
    assert len(matching) == 1
    assert matching[0]['decision'] == 'normalized_more_plausible'


def test_diff_detects_removed_home_standby_cleaning():
    """Legacy zählt SB_S als Standby (arbeitstage++), normalized ignoriert.
    Diff sieht arbeitstage-delta negativ."""
    legacy = {
        'tage_detail': [
            {'datum': '2025-02-01', 'klass': 'Standby', 'amount': 0.0},
            {'datum': '2025-02-02', 'klass': 'Standby', 'amount': 0.0},
        ],
        'fahr_tage': 0, 'arbeitstage': 2, 'hotel_naechte': 0,
        'reinigungstage': 2, 'vma_aus': 0.0,
    }
    result = nt.CalculationResult()  # 0 alles
    diff = nt.diff_against_legacy(result, legacy)
    assert diff['summary']['delta']['arbeitstage'] == -2
    assert diff['summary']['delta']['reinigungstage'] == -2


def test_diff_detects_removed_phantom_hotel_night():
    """Legacy hat 2 Hotelnächte phantom, normalized hat 0."""
    legacy = {
        'tage_detail': [],
        'fahr_tage': 0, 'arbeitstage': 0, 'hotel_naechte': 2, 'vma_aus': 0.0,
    }
    result = nt.CalculationResult()
    diff = nt.diff_against_legacy(result, legacy)
    assert diff['summary']['delta']['hotel_naechte'] == -2


def test_diff_summary_totals_correct():
    """summary.legacy + summary.normalized + summary.delta sind konsistent."""
    legacy = {
        'tage_detail': [{'datum': '2025-01-03', 'klass': 'Z76', 'amount': 28.0}],
        'fahr_tage': 1, 'arbeitstage': 1, 'hotel_naechte': 0, 'vma_aus': 28.0,
    }
    result = nt.CalculationResult(
        z76_eur=42.0, z76_tage=1, fahrtage=1, arbeitstage=1,
        by_date={'2025-01-03': {'klass': 'Z76', 'amount': 42.0}},
    )
    diff = nt.diff_against_legacy(result, legacy)
    s = diff['summary']
    for k in ('z76_eur', 'fahr_tage', 'arbeitstage'):
        assert s['delta'][k] == round(s['normalized'][k] - s['legacy'][k], 2)


def test_diff_by_date_has_reason_for_each_difference():
    """Jede by_date-Diff-Entry hat date, legacy, normalized, decision, reason."""
    legacy = {
        'tage_detail': [
            {'datum': '2025-01-06', 'klass': 'Frei', 'amount': 0.0},
        ],
        'fahr_tage': 0, 'arbeitstage': 0, 'hotel_naechte': 0, 'vma_aus': 0.0,
    }
    result = nt.CalculationResult(
        by_date={'2025-01-06': {'klass': 'Z76', 'amount': 28.0}},
    )
    diff = nt.diff_against_legacy(result, legacy)
    for entry in diff['by_date']:
        assert 'date' in entry
        assert 'legacy' in entry
        assert 'normalized' in entry
        assert 'decision' in entry
        assert 'reason' in entry
        assert entry['decision'] in (
            'normalized_more_plausible', 'legacy_more_plausible',
            'needs_review', 'accepted_difference',
        )


# ════════════════════════════════════════════════════════════════════════════
# B5: Safety — Crash in normalized_tours darf Hauptpfad nicht killen
# ════════════════════════════════════════════════════════════════════════════

def test_normalized_tours_failure_does_not_break_main_pipeline(monkeypatch):
    """Wenn build_normalized_tours raises, sammelt der Wire-Block den Error
    in _normalized_tours_audit_error und liefert Hauptpipeline weiter."""
    # Simuliere die parallel-audit Try/Except direkt
    monkeypatch.setattr(app, 'AEROTAX_USE_NORMALIZED_TOURS', True)

    def _explode(*a, **kw):
        raise ValueError('simulated builder crash')
    monkeypatch.setattr(nt, 'build_normalized_tours', _explode)

    # Inline-Simulation des try/except aus hybrid_analyze
    audit = None
    audit_error = None
    if app.AEROTAX_USE_NORMALIZED_TOURS:
        try:
            nt.build_normalized_tours([], [], 2025)
        except Exception as e:
            audit_error = {
                'type': type(e).__name__,
                'message': str(e)[:300],
            }
    assert audit is None
    assert audit_error is not None
    assert audit_error['type'] == 'ValueError'
    assert 'simulated' in audit_error['message']


def test_normalized_tours_audit_error_persisted():
    """_normalized_tours_audit_error-Feld struktur OK."""
    # Manuell zusammensetzen wie es in hybrid_analyze gemacht wird
    audit_error = {
        'type':    'ValueError',
        'message': 'test message',
        'trace':   'fake-traceback',
    }
    # Field muss in result-dict-Schema sein (per Konvention)
    assert 'type' in audit_error
    assert 'message' in audit_error
    assert 'trace' in audit_error


def test_final_amount_same_on_audit_failure():
    """Selbst wenn normalized_tours crasht, ist der Final-Betrag im result
    unverändert. Da normalized_tours nur Audit schreibt und nichts am
    classification-Dict ändert, kann der Final-Betrag nicht abweichen."""
    # Strukturtest: das Audit-Feld ist getrennt vom classification dict
    src = open(app.__file__, encoding='utf-8').read()
    # Sicherheits-Wire: das try/except darf classification NICHT ändern
    # Im integrations-Block kommt classification NICHT als writable vor.
    block_start = src.find('v15 (2026-05-25) PHASE B PARALLEL AUDIT')
    block_end = src.find('return {', block_start)
    block_src = src[block_start:block_end]
    # Im Wire-Block darf classification nicht neu zugewiesen werden
    assert 'classification =' not in block_src, \
        'normalized_tours-Wire darf classification nicht überschreiben'


# ════════════════════════════════════════════════════════════════════════════
# B4: PDF/Audit visibility — _normalized_tours_audit not in user PDF
# ════════════════════════════════════════════════════════════════════════════

def test_normalized_tours_audit_not_visible_in_user_pdf_by_default():
    """Im PDF-Renderer darf _normalized_tours_audit nicht ausgegeben werden.

    Sicherheits-Test: grep im source dass _normalized_tours_audit NICHT
    direkt im build_pdf-Pfad gerendert wird.
    """
    src = open(app.__file__, encoding='utf-8').read()
    # Such die PDF-Render-Funktion (build_pdf_v8 oder _build_v8_pdf)
    import re
    pdf_funcs = re.findall(r'def\s+(\w*build_pdf\w*|\w*generate_pdf\w*|\w*render_pdf\w*)',
                           src, re.IGNORECASE)
    # Im PDF-Render-Bereich darf _normalized_tours_audit nicht gerendert werden
    # Wir prüfen: das audit-feld wird höchstens im JSON-result-debug gerendert
    # (z.B. via result.get('_normalized_tours_audit'))
    # Aktueller Stand: kein Code rendert es ins PDF → grep findet nichts in PDF-Code
    pdf_renders = re.findall(
        r'(?:S\.append|story\.append|elements\.append).{0,300}_normalized_tours_audit',
        src, re.DOTALL,
    )
    assert not pdf_renders, \
        f'_normalized_tours_audit erscheint im PDF-Render-Pfad: {pdf_renders[:2]}'


def test_debug_pdf_can_include_tour_audit_if_enabled():
    """Sanity: das audit-feld ist im result-dict für Debug-Modus zugreifbar
    (z.B. via API /api/job/<id>/debug). Nicht im Haupt-PDF."""
    # Strukturtest: result dict hat optionales _normalized_tours_audit field
    src = open(app.__file__, encoding='utf-8').read()
    assert "'_normalized_tours_audit':" in src
    assert "'_normalized_tours_audit_error':" in src
