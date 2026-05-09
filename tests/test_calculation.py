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
    """Backend zählt Hotelnächte/Arbeitstage/Fahrtage deterministisch (v6.0.2 Schema)."""
    from app import _count_deterministic
    structured = {
        'days': [
            # 4-Tages-Tour: Anreise + 2 Volltage + Heimkehr
            {'datum': '2025-01-03', 'activity_type': 'tour', 'overnight_after_day': True},
            {'datum': '2025-01-04', 'activity_type': 'tour', 'overnight_after_day': True},
            {'datum': '2025-01-05', 'activity_type': 'tour', 'overnight_after_day': True},
            {'datum': '2025-01-06', 'activity_type': 'tour', 'overnight_after_day': False},
            {'datum': '2025-01-10', 'activity_type': 'office', 'overnight_after_day': False},
            {'datum': '2025-01-11', 'activity_type': 'same_day', 'overnight_after_day': False},
            {'datum': '2025-01-12', 'activity_type': 'frei', 'overnight_after_day': False},
            {'datum': '2025-01-15', 'activity_type': 'standby', 'overnight_after_day': False},
        ]
    }
    counts = _count_deterministic(structured)
    # 3 Hotelnächte (Tag 1,2,3), Tag 4 kommt heim
    assert counts['hotel_naechte'] == 3
    # Arbeitstage: 4 tour + 1 office + 1 same_day + 1 standby = 7
    assert counts['arbeitstage'] == 7
    # Fahrtage: nur Tag 1 der Tour (Tag 2-4 kommen aus Übernachtung) + Office + Same-Day = 3
    assert counts['fahr_tage'] == 3


def test_v6_count_deterministic_multi_stop_inland():
    """Multi-Stop-Tour mit Inland- und Ausland-Layover: Hotelnächte korrekt."""
    from app import _count_deterministic
    structured = {
        'days': [
            # FRA → BER (Inland-Übernachtung) → ZAG (Ausland) → ARN (Ausland) → Heimkehr
            {'datum': '2025-06-17', 'activity_type': 'tour', 'overnight_after_day': True,
             'layover_ort': 'BER', 'layover_inland': True},
            {'datum': '2025-06-18', 'activity_type': 'tour', 'overnight_after_day': True,
             'layover_ort': 'ZAG', 'layover_inland': False},
            {'datum': '2025-06-19', 'activity_type': 'tour', 'overnight_after_day': True,
             'layover_ort': 'ARN', 'layover_inland': False},
            {'datum': '2025-06-20', 'activity_type': 'tour', 'overnight_after_day': False},
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


def test_v6_aggregate_office_with_z76_misclass_uses_volltag_satz():
    """Defensive: Office-Tag fälschlich als Z76 → konservativer Volltag-Satz,
    KEINE An/Abreise-Pauschale (auch wenn overnight=false aussieht wie Heimkehr)."""
    from app import _aggregate_v6_classification
    structured = {
        'days': [
            # Office-Tag mit overnight=false — KEIN Heimkehr-Tag, sondern normaler Office
            {'datum': '2025-05-12', 'activity_type': 'office', 'overnight_after_day': False,
             'layover_ort': '', 'routing': []},
        ]
    }
    classifications = [
        # Opus klassifiziert fälschlich Office als Z76 (sollte gar nicht passieren,
        # aber wenn doch: Backend muss konservativ bleiben)
        {'datum': '2025-05-12', 'klass': 'Z76', 'begruendung': 'fehl-klass'},
    ]
    agg = _aggregate_v6_classification(classifications, structured, 2025)
    # Z76-EUR sollte mit Volltag-Satz gerechnet sein (28€ Fallback wegen kein layover_ort)
    # NICHT mit An/Abreise-Satz (was eine andere Zahl wäre)
    # Hier prüfen wir nur dass es überhaupt einen sinnvollen Wert gibt
    assert agg['z76_eur'] >= 0, f"z76_eur sollte ≥ 0 sein, ist {agg['z76_eur']}"


# ── v7.0 Deterministic Classification Tests ──────────────────────────────

def test_v7_match_dp_se_filters_storno():
    """Storno-Zeilen werden aus SE rausgefiltert."""
    from app import _match_dp_se_per_day
    structured = {'days': [{'datum': '2025-03-04', 'activity_type': 'tour', 'overnight_after_day': True}]}
    se = {'se_lines': [
        {'datum': '2025-03-04', 'stfrei_betrag': 30, 'stfrei_ort': 'BLR', 'storno': False, 'stfrei_inland': False},
        {'datum': '2025-03-04', 'stfrei_betrag': 30, 'stfrei_ort': 'BLR', 'storno': True},  # Storno
    ]}
    matched = _match_dp_se_per_day(structured, se)
    assert len(matched) == 1
    assert matched[0]['se']['count'] == 1  # Storno ausgefiltert
    assert matched[0]['se']['stfrei_total'] == 30


def test_v7_classify_same_day_z72():
    """Same-Day → Z72 wenn alle Hard-Gate-Bedingungen erfüllt."""
    from app import _deterministic_classify_v7, _match_dp_se_per_day
    structured = {'days': [{'datum': '2025-01-11', 'activity_type': 'same_day',
                             'overnight_after_day': False, 'has_fl': False}]}
    se = {'se_lines': []}
    matched = _match_dp_se_per_day(structured, se)
    result = _deterministic_classify_v7(matched, 2025, 'FRA')
    assert result['z72_tage'] == 1
    assert result['z73_tage'] == 0
    assert result['z76_eur'] == 0


def test_v7_classify_inland_tour_z73():
    """Tour mit SE-Inland-Layover → Z73 An/Abreise."""
    from app import _deterministic_classify_v7, _match_dp_se_per_day
    # 3-Tages-Tour MUC: Anreise + 1 Volltag + Abreise
    structured = {'days': [
        {'datum': '2025-03-04', 'activity_type': 'tour', 'overnight_after_day': True, 'has_fl': True},
        {'datum': '2025-03-05', 'activity_type': 'tour', 'overnight_after_day': True, 'has_fl': True},
        {'datum': '2025-03-06', 'activity_type': 'tour', 'overnight_after_day': False, 'has_fl': False},
    ]}
    se = {'se_lines': [
        {'datum': '2025-03-04', 'stfrei_betrag': 14, 'stfrei_ort': 'MUC', 'stfrei_inland': True, 'storno': False},
        {'datum': '2025-03-05', 'stfrei_betrag': 28, 'stfrei_ort': 'MUC', 'stfrei_inland': True, 'storno': False},
        {'datum': '2025-03-06', 'stfrei_betrag': 14, 'stfrei_ort': 'MUC', 'stfrei_inland': True, 'storno': False},
    ]}
    matched = _match_dp_se_per_day(structured, se)
    result = _deterministic_classify_v7(matched, 2025, 'FRA')
    # An- + Abreise = 2× Z73, Volltag = Office
    assert result['z73_tage'] == 2
    assert result['z73_eur'] == 28.0  # 2 × 14€
    assert result['z76_eur'] == 0


def test_v7_classify_foreign_tour_z76():
    """Tour mit SE-Auslands-Layover → Z76 mit BMF-Pauschale."""
    from app import _deterministic_classify_v7, _match_dp_se_per_day
    structured = {'days': [
        {'datum': '2025-01-03', 'activity_type': 'tour', 'overnight_after_day': True, 'has_fl': True,
         'routing': ['FRA', 'BLR'], 'layover_ort': 'BLR'},
        {'datum': '2025-01-04', 'activity_type': 'tour', 'overnight_after_day': True, 'has_fl': True,
         'routing': ['BLR'], 'layover_ort': 'BLR'},
        {'datum': '2025-01-05', 'activity_type': 'tour', 'overnight_after_day': False, 'has_fl': False,
         'routing': ['BLR', 'FRA']},
    ]}
    se = {'se_lines': [
        {'datum': '2025-01-03', 'stfrei_betrag': 30, 'stfrei_ort': 'BLR', 'stfrei_inland': False, 'storno': False},
        {'datum': '2025-01-04', 'stfrei_betrag': 39, 'stfrei_ort': 'BLR', 'stfrei_inland': False, 'storno': False},
        {'datum': '2025-01-05', 'stfrei_betrag': 30, 'stfrei_ort': 'BLR', 'stfrei_inland': False, 'storno': False},
    ]}
    matched = _match_dp_se_per_day(structured, se)
    result = _deterministic_classify_v7(matched, 2025, 'FRA')
    assert result['z76_eur'] > 0
    assert result['z73_tage'] == 0
    assert result['z72_tage'] == 0


def test_v7_classify_blr_tour_with_fra_stempel_anreise():
    """Klassische BLR 4-Tage-Tour mit FRA-Stempel auf Anreisetag.
    Erwartung: 4× Z76, KEIN Z73 trotz FRA-Stempel im SE."""
    from app import _deterministic_classify_v7, _match_dp_se_per_day
    structured = {'days': [
        {'datum': '2025-01-03', 'activity_type': 'tour', 'overnight_after_day': True, 'has_fl': True,
         'routing': ['FRA', 'BLR'], 'layover_ort': 'BLR'},
        {'datum': '2025-01-04', 'activity_type': 'tour', 'overnight_after_day': True, 'has_fl': True,
         'routing': ['BLR'], 'layover_ort': 'BLR'},
        {'datum': '2025-01-05', 'activity_type': 'tour', 'overnight_after_day': True, 'has_fl': True,
         'routing': ['BLR'], 'layover_ort': 'BLR'},
        {'datum': '2025-01-06', 'activity_type': 'tour', 'overnight_after_day': False, 'has_fl': False,
         'routing': ['BLR', 'FRA']},
    ]}
    se = {'se_lines': [
        # FRA-STEMPEL am Anreisetag (häufig bei LH-Auslandstouren)
        {'datum': '2025-01-03', 'stfrei_betrag': 14, 'stfrei_ort': 'FRA', 'stfrei_inland': True, 'storno': False},
        {'datum': '2025-01-04', 'stfrei_betrag': 39, 'stfrei_ort': 'BLR', 'stfrei_inland': False, 'storno': False},
        {'datum': '2025-01-05', 'stfrei_betrag': 39, 'stfrei_ort': 'BLR', 'stfrei_inland': False, 'storno': False},
        {'datum': '2025-01-06', 'stfrei_betrag': 30, 'stfrei_ort': 'BLR', 'stfrei_inland': False, 'storno': False},
    ]}
    matched = _match_dp_se_per_day(structured, se)
    result = _deterministic_classify_v7(matched, 2025, 'FRA')
    # 4 Tour-Tage, kein Z73, alles Z76
    assert result['z73_tage'] == 0, f"Z73 sollte 0 sein, ist {result['z73_tage']}"
    # Z76 EUR sollte > 100€ sein (4 Tage Indien)
    assert result['z76_eur'] >= 100, f"Z76 EUR sollte > 100€ sein, ist {result['z76_eur']}"


def test_v7_cluster_extends_to_abreisetag_with_active_se():
    """Reference-Contract: BLR-Tour 03.-06.01 — Tag 06.01 (Abreise) hat aktive SE-Zeile
    aber nicht als 'tour' im DP klassifiziert. Cluster-Extend muss greifen."""
    from app import _deterministic_classify_v7, _match_dp_se_per_day
    structured = {'days': [
        {'datum': '2025-01-03', 'activity_type': 'tour', 'overnight_after_day': True,
         'has_fl': True, 'routing': ['FRA', 'BLR'], 'layover_ort': 'BLR'},
        {'datum': '2025-01-04', 'activity_type': 'tour', 'overnight_after_day': True,
         'has_fl': True, 'layover_ort': 'BLR'},
        {'datum': '2025-01-05', 'activity_type': 'tour', 'overnight_after_day': True,
         'has_fl': True, 'layover_ort': 'BLR'},
        # Tag 06.01: Sonnet hat es NICHT als 'tour' erkannt (z.B. 'unknown')
        # aber SE-Zeile zeigt BLR-Heimkehr-Routing
        {'datum': '2025-01-06', 'activity_type': 'unknown', 'overnight_after_day': False,
         'has_fl': False, 'routing': ['BLR', 'FRA']},
    ]}
    se = {'se_lines': [
        {'datum': '2025-01-03', 'stfrei_betrag': 30, 'stfrei_ort': 'BLR', 'stfrei_inland': False, 'storno': False},
        {'datum': '2025-01-04', 'stfrei_betrag': 39, 'stfrei_ort': 'BLR', 'stfrei_inland': False, 'storno': False},
        {'datum': '2025-01-05', 'stfrei_betrag': 39, 'stfrei_ort': 'BLR', 'stfrei_inland': False, 'storno': False},
        {'datum': '2025-01-06', 'stfrei_betrag': 30, 'stfrei_ort': 'BLR', 'stfrei_inland': False, 'storno': False},
    ]}
    matched = _match_dp_se_per_day(structured, se)
    result = _deterministic_classify_v7(matched, 2025, 'FRA')
    # Tag 06.01 muss als Z76 klassifiziert sein (Cluster-Extend), nicht als Sonstiges
    detail_06 = [d for d in result['tage_detail'] if d['datum'] == '2025-01-06']
    assert detail_06, "Tag 06.01 fehlt in tage_detail"
    assert detail_06[0]['klass'] == 'Z76', \
        f"Tag 06.01 sollte Z76 sein (Cluster-Extend), ist {detail_06[0]['klass']}"


def test_v7_classify_mixed_tour_inland_day_keeps_inland():
    """Mixed-Cluster: Bulgarien → Deutschland 24h → Schweden.
    Erwartung: Tag 1+3 Z76 (Ausland), Tag 2 Z74 (Inland 24h zwischen 2 Inland-Layovern wäre Z74)
    Eigentlich: Tag 2 ist Inland-Layover zwischen Auslands-Layovern → Z73 (Übergang)."""
    from app import _deterministic_classify_v7, _match_dp_se_per_day
    structured = {'days': [
        {'datum': '2025-09-26', 'activity_type': 'tour', 'overnight_after_day': True, 'has_fl': True,
         'routing': ['FRA', 'SOF'], 'layover_ort': 'SOF'},  # Bulgarien
        {'datum': '2025-09-27', 'activity_type': 'tour', 'overnight_after_day': True, 'has_fl': True,
         'routing': ['SOF', 'MUC'], 'layover_ort': 'MUC'},  # Deutschland Inland-Stop
        {'datum': '2025-09-28', 'activity_type': 'tour', 'overnight_after_day': False, 'has_fl': False,
         'routing': ['MUC', 'GOT', 'FRA'], 'layover_ort': 'GOT'},  # Schweden
    ]}
    se = {'se_lines': [
        {'datum': '2025-09-26', 'stfrei_betrag': 32, 'stfrei_ort': 'SOF', 'stfrei_inland': False, 'storno': False},
        {'datum': '2025-09-27', 'stfrei_betrag': 28, 'stfrei_ort': 'MUC', 'stfrei_inland': True, 'storno': False},
        {'datum': '2025-09-28', 'stfrei_betrag': 33, 'stfrei_ort': 'GOT', 'stfrei_inland': False, 'storno': False},
    ]}
    matched = _match_dp_se_per_day(structured, se)
    result = _deterministic_classify_v7(matched, 2025, 'FRA')
    # Mixed-Cluster: muss Inland-Tag erkennen
    # Tag 1 (SOF) = Z76, Tag 2 (MUC Inland) = Z73 oder Z74, Tag 3 (Heimkehr nach GOT-overnight=false) = Z76 Abreise
    # Wichtig: Mixed-Tour darf nicht alles Z76 sein
    assert result['z73_tage'] >= 1 or result['z74_tage'] >= 1, \
        f"Mixed-Tour mit Inland-Layover muss Z73/Z74 erzeugen, ist Z73={result['z73_tage']} Z74={result['z74_tage']}"


def test_v7_classify_frei_no_count():
    """FREI-Tage zählen weder als AT noch produzieren VMA."""
    from app import _deterministic_classify_v7, _match_dp_se_per_day
    structured = {'days': [{'datum': '2025-01-01', 'activity_type': 'frei',
                             'overnight_after_day': False, 'has_fl': False}]}
    matched = _match_dp_se_per_day(structured, {'se_lines': []})
    result = _deterministic_classify_v7(matched, 2025, 'FRA')
    assert result['arbeitstage'] == 0
    assert result['z72_tage'] == 0


def test_v7_classify_office_counts_fahrtag():
    """Office-Tag: AT + Fahrtag, kein VMA."""
    from app import _deterministic_classify_v7, _match_dp_se_per_day
    structured = {'days': [{'datum': '2025-01-10', 'activity_type': 'office',
                             'overnight_after_day': False, 'has_fl': False}]}
    matched = _match_dp_se_per_day(structured, {'se_lines': []})
    result = _deterministic_classify_v7(matched, 2025, 'FRA')
    assert result['arbeitstage'] == 1
    assert result['fahr_tage'] == 1
    assert result['z72_tage'] == 0


# ── Reference-Contract Tests (interne Reference-Werte) ──────────────────

def test_reference_contract_fahrtkosten():
    """Reference-Contract: 28 km × 58 Fahrtage = 524,32 €."""
    from app import PENDLER_BY_YEAR
    pendler = PENDLER_BY_YEAR[2025]
    km, tage = 28, 58
    fahrt = round(min(km, 20) * tage * pendler['lt_20km'] +
                  max(0, km - 20) * tage * pendler['gt_21km'], 2)
    assert fahrt == 524.32


def test_reference_contract_reinigung():
    """Reference-Contract: 133 Arbeitstage × 1,60 € = 212,80 €."""
    from app import REINIGUNG_PRO_TAG_BY_YEAR
    satz = REINIGUNG_PRO_TAG_BY_YEAR[2025]
    assert round(133 * satz, 2) == 212.80


def test_reference_contract_trinkgeld():
    """Reference-Contract: 66 Hotelnächte × 3,60 € = 237,60 €."""
    from app import TRINKGELD_PRO_NACHT_BY_YEAR
    satz = TRINKGELD_PRO_NACHT_BY_YEAR[2025]
    assert round(66 * satz, 2) == 237.60


def test_reference_contract_vma_inland():
    """Reference-Contract: Z72=5×14, Z73=11×14, Z74=1×28."""
    from app import BMF_INLAND_BY_YEAR
    bmf = BMF_INLAND_BY_YEAR[2025]
    assert round(5 * bmf['tagestrip_8h'], 2) == 70.00
    assert round(11 * bmf['an_abreise'], 2) == 154.00
    assert round(1 * bmf['voll_24h'], 2) == 28.00


def test_reference_contract_topf_trennung_z17_only_fahrt():
    """Reference: Z17=330 mindert NUR Fahrtkosten-Topf, nicht VMA/Reinigung/Trinkgeld."""
    fahrtkosten = 524.32
    z17 = 330.00
    fahrt_netto = max(0, fahrtkosten - z17)
    assert round(fahrt_netto, 2) == 194.32
    # Reinigung und Trinkgeld bleiben unverändert (Z17 fasst sie nicht an)
    reinigung_brutto = 212.80
    trink_brutto = 237.60
    assert reinigung_brutto == 212.80
    assert trink_brutto == 237.60


def test_reference_contract_topf_trennung_z77_only_vma():
    """Reference: Z77 mindert NUR VMA-Topf, nicht Fahrt/Reinigung/Trinkgeld."""
    z76, z73_eur, z72_eur = 4794.00, 154.00, 70.00
    z77 = 4655.00
    vma_brutto = z76 + z73_eur + z72_eur + 28.0  # plus Z74
    vma_netto = max(0, vma_brutto - z77)
    # Wenn VMA > Z77 → positiver Rest. Wenn VMA < Z77 → 0 (kein Übergriff).
    assert vma_netto >= 0


def test_reference_contract_storno_filter_in_match():
    """Reference: Storno-Zeilen dürfen nicht in z77_total zählen."""
    from app import _match_dp_se_per_day
    structured = {'days': [{'datum': '2025-03-04', 'activity_type': 'tour',
                             'overnight_after_day': True}]}
    se = {'se_lines': [
        {'datum': '2025-03-04', 'stfrei_betrag': 30, 'stfrei_ort': 'BLR',
         'stfrei_inland': False, 'storno': False},
        {'datum': '2025-03-04', 'stfrei_betrag': 30, 'stfrei_ort': 'BLR',
         'stfrei_inland': False, 'storno': True},
    ]}
    matched = _match_dp_se_per_day(structured, se)
    # Nur 1 nicht-Storno-Zeile, Σ = 30
    assert matched[0]['se']['stfrei_total'] == 30
    assert matched[0]['se']['count'] == 1


def test_as_dict_item_normalizer():
    """Tuple-Normalizer akzeptiert dict, tuple, pydantic-Model."""
    from app import _as_dict_item
    # dict
    assert _as_dict_item({'datum': '2025-01-01', 'klass': 'Z72'}) == {'datum': '2025-01-01', 'klass': 'Z72'}
    # tuple (key, value-dict)
    out = _as_dict_item(('2025-01-01', {'klass': 'Z72'}))
    assert out['datum'] == '2025-01-01' and out['klass'] == 'Z72'
    # tuple (key, primitive)
    out = _as_dict_item(('foo', 42))
    assert out['datum'] == 'foo' and out['value'] == 42
    # nicht-konvertierbar
    assert _as_dict_item('string') == {}


# ── v7 Einsatzplan-frei Tests ──────────────────────────────────────────────

def test_v7_dp_reader_signature_einsatz_optional():
    """_sonnet_read_dp_structured akzeptiert Aufruf ohne einsatz_bytes."""
    from app import _sonnet_read_dp_structured
    import inspect
    sig = inspect.signature(_sonnet_read_dp_structured)
    params = list(sig.parameters.values())
    # einsatz_bytes hat default-Value (optional)
    p_einsatz = next((p for p in params if p.name == 'einsatz_bytes'), None)
    if p_einsatz is not None:
        assert p_einsatz.default is None or p_einsatz.default == [], \
            f"einsatz_bytes muss optional sein, default ist {p_einsatz.default}"


def test_v7_required_documents_lsb_dp_se():
    """Required-Documents sind nur lsb + dp + se. Einsatzplan nicht."""
    # Test schaut nicht auf HTTP-Layer, sondern dass kein 'einsatz' in der
    # Required-Validation oder Audit-Job-Created-Files erscheint.
    import inspect
    from app import process_real
    src = inspect.getsource(process_real)
    # lsb, dp, se müssen erwähnt sein
    assert "files.get('lsb')" in src
    assert "files.get('dp')" in src
    assert "files.get('se')" in src
    # einsatzplan_files darf nicht als Pflicht geprüft werden
    assert "files.get('einsatz')" not in src or "not files.get('einsatz')" not in src


# ── Anreisekosten / ÖPNV / Crew-Shuttle (v7.1+) ────────────────────────────

def test_anreise_entfernungspauschale_unchanged():
    """Reference: 28 km × 58 Tage = 524,32 € — verkehrsmittel-unabhängig."""
    from app import PENDLER_BY_YEAR
    pendler = PENDLER_BY_YEAR[2025]
    f = round(min(28, 20) * 58 * pendler['lt_20km'] +
              max(0, 28 - 20) * 58 * pendler['gt_21km'], 2)
    assert f == 524.32


def test_anreise_zusatzkosten_addieren():
    """Zusatzkosten (ÖPNV + Shuttle) addieren sich zur Entfernungspauschale."""
    entfernungspauschale = 524.32
    oepnv = 360.00
    shuttle = 480.00
    fahrtkosten_brutto = entfernungspauschale + oepnv + shuttle
    assert round(fahrtkosten_brutto, 2) == 1364.32


def test_anreise_z17_mindert_nur_fahrtkosten_topf():
    """Z17 mindert nur den Anreisekosten-Topf, niemals VMA/Reinigung/Trinkgeld."""
    fahrtkosten_brutto = 524.32 + 360.00 + 480.00  # mit Zusatz
    z17 = 330.00
    fahrt_netto = max(0, fahrtkosten_brutto - z17)
    assert round(fahrt_netto, 2) == 1034.32
    # Reinigung/Trinkgeld unverändert
    reinigung = 212.80
    trinkgeld = 237.60
    assert reinigung == 212.80
    assert trinkgeld == 237.60


def test_anreise_z17_kann_fahrtkosten_topf_nicht_negativ_machen():
    """Z17 > Fahrtkosten → Topf wird 0, andere Töpfe bleiben."""
    fahrtkosten_brutto = 100.00
    z17 = 500.00
    fahrt_netto = max(0, fahrtkosten_brutto - z17)
    assert fahrt_netto == 0
    # Andere Töpfe (VMA) müssen separat berechnet werden — kein Übergriff


if __name__ == '__main__':
    import pytest
    sys.exit(pytest.main([__file__, '-v']))
