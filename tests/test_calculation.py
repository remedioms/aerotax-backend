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
    cls = {'z76_eur': 4500, 'arbeitstage': 150, 'fahr_tage': 60,
           'hotel_naechte': 50, 'z72_tage': 5, 'z73_tage': 8}
    se = {'z77_total': 5000, 'auslandsspesen_total': 4000}
    issues = _detect_classification_issues(cls, se)
    # Z76 (4500) <= Z77 (5000) ✓; Z76 vs Auslandsspesen 4500 vs 4000 → diff 12.5%, < 40%
    # Z72=5 + Z73=8 + Hotel=50 → kein Anti-Muster
    assert len(issues) == 0, f"Erwartet 0 Issues, bekommen: {issues}"


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


# ── Anti-Muster: Z72 hoch + Z73=0 + viele Hotelnächte (v5.5 Tour-Guardrails) ──

def test_classification_issues_z72_high_z73_zero_with_hotels():
    """Klassisches Anti-Muster: viele Z72, 0 Z73, viele Hotelnächte → Inland-ÜN
    fehlklassifiziert. Beispiel aus realem Test v5.4 (Z72=38, Z73=0, Hotel=52)."""
    from app import _detect_classification_issues
    cls = {'z76_eur': 3856, 'arbeitstage': 144, 'fahr_tage': 71,
           'hotel_naechte': 52, 'z72_tage': 38, 'z73_tage': 0}
    se = {'z77_total': 4655, 'auslandsspesen_total': 4000}
    issues = _detect_classification_issues(cls, se)
    assert any('Anti-Muster' in i and 'Z72=38' in i and 'Z73=0' in i for i in issues), \
        f"Erwartetes Anti-Muster nicht gefunden in: {issues}"


def test_classification_issues_z72_low_z73_zero_no_antimuster():
    """Wenig Z72 + Z73=0 + wenig Hotels: kein Anti-Muster (z.B. Standby-Crew
    ohne Touren)."""
    from app import _detect_classification_issues
    cls = {'z76_eur': 100, 'arbeitstage': 60, 'fahr_tage': 30,
           'hotel_naechte': 5, 'z72_tage': 3, 'z73_tage': 0}
    se = {'z77_total': 200, 'auslandsspesen_total': 80}
    issues = _detect_classification_issues(cls, se)
    assert not any('Anti-Muster' in i for i in issues)


def test_classification_issues_z76_eur_implies_more_tage_than_hotels():
    """Z76 in EUR (geschätzt /50€) deutet auf mehr Auslandstage als Hotelnächte.
    Schwelle: Z76-Tage > 2× Hotelnächte → starker Mismatch."""
    from app import _detect_classification_issues
    # 5000€ ≈ 100 Auslandstage, aber nur 20 Hotelnächte → Verhältnis 5.0 → Issue
    cls = {'z76_eur': 5000, 'arbeitstage': 150, 'fahr_tage': 60,
           'hotel_naechte': 20, 'z72_tage': 5, 'z73_tage': 5}
    se = {'z77_total': 6000, 'auslandsspesen_total': 4500}
    issues = _detect_classification_issues(cls, se)
    assert any('Hotelnächte' in i and 'Auslandstage' in i for i in issues)


def test_classification_issues_z76_hotel_ratio_normal_no_warning():
    """Verhältnis Z76-Tage/Hotelnächte ~1.3 (normal): kein Warning."""
    from app import _detect_classification_issues
    # 4500€ ≈ 90 Auslandstage, 50 Hotelnächte → Verhältnis 1.8 (unter 2.0) → kein Issue
    cls = {'z76_eur': 4500, 'arbeitstage': 150, 'fahr_tage': 60,
           'hotel_naechte': 50, 'z72_tage': 5, 'z73_tage': 5}
    se = {'z77_total': 5000, 'auslandsspesen_total': 4000}
    issues = _detect_classification_issues(cls, se)
    assert not any('Auslandstage' in i and 'Hotelnächte' in i for i in issues)


def test_classification_issues_z72_zero_pendel_antipattern():
    """Pendel-Anti-Pattern v5.7: Z72 = 0 trotz vieler Arbeitstage und aktiver Auslandstouren.
    Aufgetreten bei v5.6 nach zu strikter Z72-Hard-Gate Interpretation."""
    from app import _detect_classification_issues
    cls = {'z76_eur': 4655, 'arbeitstage': 144, 'fahr_tage': 26,
           'hotel_naechte': 52, 'z72_tage': 0, 'z73_tage': 0}
    se = {'z77_total': 4655, 'auslandsspesen_total': 4441}
    issues = _detect_classification_issues(cls, se)
    assert any('Anti-Muster' in i and 'Z72 = 0' in i for i in issues), \
        f"Pendel-Anti-Pattern nicht gefunden in: {issues}"


def test_classification_issues_z72_zero_no_pendel_when_inactive():
    """Z72 = 0 ist OK bei wenig Arbeitstagen oder ohne Z76 (Standby-only Crew)."""
    from app import _detect_classification_issues
    cls = {'z76_eur': 200, 'arbeitstage': 80, 'fahr_tage': 20,
           'hotel_naechte': 5, 'z72_tage': 0, 'z73_tage': 0}
    se = {'z77_total': 250, 'auslandsspesen_total': 200}
    issues = _detect_classification_issues(cls, se)
    # Bei wenig AT (<100) oder Z76 < 1000€ kein Pendel-Trigger
    assert not any('Anti-Muster' in i and 'Z72 = 0' in i for i in issues)


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


# ── v6.0 Structured-Day-Pipeline Tests ────────────────────────────────────

def test_v6_inland_iata_codes():
    """Inland-IATA-Erkennung: deutsche Flughafen-Codes."""
    from app import _is_inland_code
    assert _is_inland_code('FRA')
    assert _is_inland_code('MUC')
    assert _is_inland_code('HAM')
    assert _is_inland_code('BER')
    assert _is_inland_code('DUS')
    assert _is_inland_code('CGN')
    # Auslandscodes
    assert not _is_inland_code('BLR')  # Indien
    assert not _is_inland_code('JFK')  # USA
    assert not _is_inland_code('CPH')  # Dänemark
    assert not _is_inland_code('VIE')  # Österreich
    # Edge: leer / None
    assert not _is_inland_code('')
    assert not _is_inland_code(None)


def test_v6_count_deterministic_basic():
    """Backend zählt Hotelnächte/Arbeitstage/Fahrtage deterministisch."""
    from app import _count_deterministic
    structured = {
        'days': [
            {'datum': '2025-01-03', 'activity_type': 'tour_start', 'overnight_after_day': True},
            {'datum': '2025-01-04', 'activity_type': 'tour_continuation', 'overnight_after_day': True},
            {'datum': '2025-01-05', 'activity_type': 'tour_continuation', 'overnight_after_day': True},
            {'datum': '2025-01-06', 'activity_type': 'tour_end', 'overnight_after_day': False},
            {'datum': '2025-01-10', 'activity_type': 'office', 'overnight_after_day': False},
            {'datum': '2025-01-11', 'activity_type': 'same_day', 'overnight_after_day': False},
            {'datum': '2025-01-12', 'activity_type': 'frei', 'overnight_after_day': False},
            {'datum': '2025-01-15', 'activity_type': 'standby', 'overnight_after_day': False},
        ]
    }
    counts = _count_deterministic(structured)
    # 4-Tages-Tour = 3 Hotelnächte (an Tag 1,2,3), Tag 4 kommt heim
    assert counts['hotel_naechte'] == 3
    # Arbeitstage: Tour (4) + Office (1) + Same-Day (1) + Standby (1) = 7
    assert counts['arbeitstage'] == 7
    # Fahrtage: Tour-Start (1) + Office (1) + Same-Day (1) = 3
    # (Tour-Continuation und Tour-End zählen nicht; Standby zuhause auch nicht)
    assert counts['fahr_tage'] == 3


def test_v6_count_deterministic_multi_stop_inland():
    """Multi-Stop-Tour mit Inland- und Ausland-Layover: Hotelnächte korrekt."""
    from app import _count_deterministic
    structured = {
        'days': [
            # FRA → BER (Inland-Übernachtung) → ZAG (Ausland) → ARN (Ausland) → Heimkehr
            {'datum': '2025-06-17', 'activity_type': 'tour_start', 'overnight_after_day': True,
             'layover_ort': 'BER', 'layover_inland': True},
            {'datum': '2025-06-18', 'activity_type': 'tour_continuation', 'overnight_after_day': True,
             'layover_ort': 'ZAG', 'layover_inland': False},
            {'datum': '2025-06-19', 'activity_type': 'tour_continuation', 'overnight_after_day': True,
             'layover_ort': 'ARN', 'layover_inland': False},
            {'datum': '2025-06-20', 'activity_type': 'tour_end', 'overnight_after_day': False},
        ]
    }
    counts = _count_deterministic(structured)
    # 3 Hotelnächte (BER, ZAG, ARN) — egal Inland oder Ausland, ALLE zählen
    assert counts['hotel_naechte'] == 3


def test_v6_validate_z72_with_overnight_impossible():
    """Z72 darf nicht klassifiziert werden wenn overnight_after_day=true."""
    from app import _validate_opus_against_structure
    classifications = [{'datum': '2025-01-03', 'klass': 'Z72', 'begruendung': '...'}]
    structured = {'days': [{'datum': '2025-01-03', 'overnight_after_day': True, 'has_fl': True,
                             'layover_ort': 'BLR', 'layover_inland': False}]}
    issues = _validate_opus_against_structure(classifications, structured)
    assert any('Z72' in i and 'unmöglich' in i for i in issues)


def test_v6_validate_z73_at_inland_layover():
    """Z73 ist konsistent wenn layover_inland=true."""
    from app import _validate_opus_against_structure
    classifications = [{'datum': '2025-03-05', 'klass': 'Z73', 'begruendung': 'MUC Schulung'}]
    structured = {'days': [{'datum': '2025-03-05', 'overnight_after_day': True, 'has_fl': True,
                             'layover_ort': 'MUC', 'layover_inland': True}]}
    issues = _validate_opus_against_structure(classifications, structured)
    # Sollte keine Z73-bezogenen Issues geben (konsistent)
    assert not any('Z73' in i for i in issues)


def test_v6_validate_z73_at_foreign_layover_warns():
    """Z73 bei Ausland-Layover → Warnung."""
    from app import _validate_opus_against_structure
    classifications = [{'datum': '2025-01-03', 'klass': 'Z73', 'begruendung': 'falsch'}]
    structured = {'days': [{'datum': '2025-01-03', 'overnight_after_day': True, 'has_fl': True,
                             'layover_ort': 'BLR', 'layover_inland': False}]}
    issues = _validate_opus_against_structure(classifications, structured)
    assert any('Z73' in i and 'prüfen' in i for i in issues)


def test_v6_validate_z76_at_inland_layover_warns():
    """Z76 bei Inland-Layover → Warnung."""
    from app import _validate_opus_against_structure
    classifications = [{'datum': '2025-03-05', 'klass': 'Z76', 'begruendung': 'falsch'}]
    structured = {'days': [{'datum': '2025-03-05', 'overnight_after_day': True, 'has_fl': True,
                             'layover_ort': 'MUC', 'layover_inland': True}]}
    issues = _validate_opus_against_structure(classifications, structured)
    assert any('Z76' in i and 'prüfen' in i for i in issues)


def test_v6_validate_z73_without_overnight_warns():
    """Z73/Z76 ohne Übernachtung → Issue."""
    from app import _validate_opus_against_structure
    classifications = [{'datum': '2025-01-11', 'klass': 'Z73', 'begruendung': 'falsch'}]
    structured = {'days': [{'datum': '2025-01-11', 'overnight_after_day': False, 'has_fl': False,
                             'layover_ort': '', 'layover_inland': None}]}
    issues = _validate_opus_against_structure(classifications, structured)
    assert any('ohne Übernachtung' in i for i in issues)


if __name__ == '__main__':
    import pytest
    sys.exit(pytest.main([__file__, '-v']))
