"""Pure Python Unit-Tests für AeroTax Berechnungs-Funktionen.
Keine KI-Calls, kein Netzwerk — nur Math + Konstanten + Invarianten.

Lokal ausführen:
    cd ~/Desktop/aerotax-backend && python3 -m pytest tests/ -v
"""
import os
import sys

# Unit-Tests dürfen beim Import von app.py keine Worker-/Cleanup-Threads starten.
os.environ.setdefault('AEROTAX_DISABLE_BG_THREADS', '1')

# app.py liegt im Parent-Dir
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


def _pendler_pauschale(km, fahr_tage, lt_20km=0.30, gt_21km=0.38):
    """Replikation der Backend-Formel für Test."""
    return round(min(km, 20) * fahr_tage * lt_20km +
                 max(0, km - 20) * fahr_tage * gt_21km, 2)


# ── Pendlerpauschale gestaffelt ────────────────────────────────────────────

def test_pendler_unter_20km():
    """Bei <= 20 km: nur lt_20km-Satz, kein gt_21km."""
    assert _pendler_pauschale(15, 100) == round(15 * 100 * 0.30, 2)


def test_pendler_grenzwert_20km():
    """Genau 20 km: alles zum 0,30er Satz."""
    assert _pendler_pauschale(20, 50) == round(20 * 50 * 0.30, 2)


def test_pendler_ueber_20km():
    """27 km × 71 Tage = 8,66€/Tag (20×0.30 + 7×0.38). Test-Case aus realem User."""
    expected = round(20 * 71 * 0.30 + 7 * 71 * 0.38, 2)
    assert _pendler_pauschale(27, 71) == expected
    # Sanity: pro Tag ergibt 8.66€
    assert round(_pendler_pauschale(27, 1), 2) == 8.66


def test_pendler_null_tage():
    """0 Fahrtage → 0€."""
    assert _pendler_pauschale(50, 0) == 0


# ── BMF-Pauschalen Inland ──────────────────────────────────────────────────

def test_bmf_inland_2025():
    """Inland-Pauschalen 2025: 14€ Tagestrip, 14€ An/Ab, 28€ Voll-24h."""
    from app import BMF_INLAND_BY_YEAR
    bmf = BMF_INLAND_BY_YEAR[2025]
    assert bmf['tagestrip_8h'] == 14.0
    assert bmf['an_abreise']  == 14.0
    assert bmf['voll_24h']    == 28.0


def test_bmf_jeder_jahr_komplett():
    """Jedes Jahr 2023-2026 muss alle 3 Inland-Sätze haben."""
    from app import BMF_INLAND_BY_YEAR
    for year in [2023, 2024, 2025, 2026]:
        bmf = BMF_INLAND_BY_YEAR[year]
        assert 'tagestrip_8h' in bmf
        assert 'an_abreise' in bmf
        assert 'voll_24h' in bmf
        assert bmf['voll_24h'] == 2 * bmf['tagestrip_8h']  # Invariante: 24h = 2× Tagestrip


# ── Math-Konsistenz-Check ──────────────────────────────────────────────────

def test_classification_issues_z76_higher_than_z77():
    """Z76 > Z77 muss als Audit-Warnsignal erkannt werden, aber nicht gedeckelt werden."""
    from app import _detect_classification_issues
    cls = {'z76_eur': 5000, 'arbeitstage': 150, 'fahr_tage': 60, 'hotel_naechte': 50}
    se = {'z77_total': 3000, 'auslandsspesen_total': 2500}
    issues = _detect_classification_issues(cls, se)
    assert any('Z76' in i and 'Z77' in i and 'Audit-Warnung' in i for i in issues)
    assert any('nicht automatisch' in i or 'nicht pauschal' in i for i in issues)


def test_classification_issues_no_issue_when_consistent():
    """Konsistente Werte → keine Issues."""
    from app import _detect_classification_issues
    cls = {'z76_eur': 4500, 'arbeitstage': 150, 'fahr_tage': 60, 'hotel_naechte': 50}
    se = {'z77_total': 5000, 'auslandsspesen_total': 4000}
    issues = _detect_classification_issues(cls, se)
    # Z76 (4500) <= Z77 (5000) ✓; Z76 vs Auslandsspesen 4500 vs 4000 → diff 12.5%, < 40%
    assert len(issues) == 0


def test_classification_issues_hotel_too_high():
    """Hotel > Arbeitstage → Issue."""
    from app import _detect_classification_issues
    cls = {'z76_eur': 1000, 'arbeitstage': 50, 'fahr_tage': 30, 'hotel_naechte': 80}
    se = {'z77_total': 2000, 'auslandsspesen_total': 800}
    issues = _detect_classification_issues(cls, se)
    assert any('Hotel' in i and 'Arbeitstage' in i for i in issues)


def test_classification_issues_fahr_too_high():
    """Fahr > Arbeitstage → Issue."""
    from app import _detect_classification_issues
    cls = {'z76_eur': 1000, 'arbeitstage': 50, 'fahr_tage': 100, 'hotel_naechte': 20}
    se = {'z77_total': 2000, 'auslandsspesen_total': 800}
    issues = _detect_classification_issues(cls, se)
    assert any('Fahr' in i and 'Arbeitstage' in i for i in issues)


# ── PII-Redaktion ──────────────────────────────────────────────────────────

def test_redact_pii_dict():
    from app import _redact_pii
    obj = {'identnr': '12345678901', 'brutto': 50000, 'name': 'Schumann'}
    out = _redact_pii(obj)
    assert out['identnr'] == '[redacted]'
    assert out['name'] == '[redacted]'
    assert out['brutto'] == 50000  # nicht-PII bleibt


def test_redact_pii_nested():
    from app import _redact_pii
    obj = {'data': {'personalnummer': 'P12345', 'foo': 'bar'}}
    out = _redact_pii(obj)
    assert out['data']['personalnummer'] == '[redacted]'
    assert out['data']['foo'] == 'bar'


def test_redact_pii_list_of_dicts():
    from app import _redact_pii
    obj = [{'identnr': 'X'}, {'brutto': 100}]
    out = _redact_pii(obj)
    assert out[0]['identnr'] == '[redacted]'
    assert out[1]['brutto'] == 100


def test_redact_pii_empty_value_stays_empty():
    """Leerer String/None bleibt empty (nicht '[redacted]')."""
    from app import _redact_pii
    obj = {'identnr': '', 'name': None}
    out = _redact_pii(obj)
    assert out['identnr'] == ''
    assert out['name'] is None


# ── Reinigung + Trinkgeld ──────────────────────────────────────────────────

def test_reinigung_pauschale():
    from app import REINIGUNG_PRO_TAG_BY_YEAR
    assert REINIGUNG_PRO_TAG_BY_YEAR[2025] == 1.60


def test_trinkgeld_pauschale():
    from app import TRINKGELD_PRO_NACHT_BY_YEAR
    assert TRINKGELD_PRO_NACHT_BY_YEAR[2025] == 3.60


# ── File-Validation Markers ────────────────────────────────────────────────

def test_validate_file_categories_empty():
    """Keine Files → keine Warnings."""
    from app import _validate_file_categories
    assert _validate_file_categories({}) == []


if __name__ == '__main__':
    import pytest
    sys.exit(pytest.main([__file__, '-v']))
