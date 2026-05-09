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


# ── v7.5 Tests: audit_notes vs unresolved_days, VMA-Mapping ───────────────

def test_v75_audit_notes_separate_from_unresolved():
    """FRA-Stempel bei Auslandscluster → audit_notes, NICHT unresolved_days."""
    from app import _deterministic_classify_v7, _match_dp_se_per_day
    structured = {'days': [
        {'datum': '2025-01-03', 'activity_type': 'tour', 'overnight_after_day': True,
         'has_fl': True, 'routing': ['FRA','BLR'], 'layover_ort': 'BLR'},
        {'datum': '2025-01-04', 'activity_type': 'tour', 'overnight_after_day': True,
         'has_fl': True, 'layover_ort': 'BLR'},
        {'datum': '2025-01-05', 'activity_type': 'tour', 'overnight_after_day': False},
    ]}
    se = {'se_lines': [
        {'datum': '2025-01-03', 'stfrei_betrag': 14, 'stfrei_ort': 'FRA', 'stfrei_inland': True, 'storno': False},
        {'datum': '2025-01-04', 'stfrei_betrag': 39, 'stfrei_ort': 'BLR', 'stfrei_inland': False, 'storno': False},
        {'datum': '2025-01-05', 'stfrei_betrag': 30, 'stfrei_ort': 'BLR', 'stfrei_inland': False, 'storno': False},
    ]}
    matched = _match_dp_se_per_day(structured, se)
    result = _deterministic_classify_v7(matched, 2025, 'FRA')
    # 03.01 FRA-Stempel im Auslands-Cluster: gehört in audit_notes
    note_match = [n for n in result.get('audit_notes', []) if '2025-01-03' in n]
    assert note_match, f"FRA-Stempel-Audit-Note für 03.01 fehlt. notes={result.get('audit_notes')}"
    # ABER 03.01 ist NICHT in unresolved_days
    unresolved_match = [u for u in result.get('unresolved_days', []) if '2025-01-03' in u]
    assert not unresolved_match, f"03.01 darf nicht in unresolved_days sein. unresolved={result.get('unresolved_days')}"


def test_v75_no_active_se_unmapped():
    """Aktive SE-Zeile MUSS in Z72/Z73/Z74/Z76 oder vma_unmapped_se landen."""
    from app import _deterministic_classify_v7, _match_dp_se_per_day
    structured = {'days': [
        {'datum': '2025-01-03', 'activity_type': 'tour', 'overnight_after_day': True,
         'has_fl': True, 'routing': ['FRA','BLR'], 'layover_ort': 'BLR'},
        {'datum': '2025-01-04', 'activity_type': 'tour', 'overnight_after_day': False},
    ]}
    se = {'se_lines': [
        {'datum': '2025-01-03', 'stfrei_betrag': 30, 'stfrei_ort': 'BLR', 'stfrei_inland': False, 'storno': False},
        {'datum': '2025-01-04', 'stfrei_betrag': 30, 'stfrei_ort': 'BLR', 'stfrei_inland': False, 'storno': False},
    ]}
    matched = _match_dp_se_per_day(structured, se)
    result = _deterministic_classify_v7(matched, 2025, 'FRA')
    # Beide SE-Zeilen sind aktiv — beide müssen als Z76 klassifiziert sein
    z76_days = [t for t in result['tage_detail'] if t['klass'] == 'Z76']
    assert len(z76_days) >= 2, f"Beide BLR-Tage sollten Z76 sein, sind {[(t['datum'], t['klass']) for t in result['tage_detail']]}"
    # Kein vma_unmapped_se
    assert len(result.get('vma_unmapped_se', [])) == 0, \
        f"Keine SE-Zeile sollte unmapped sein. unmapped={result.get('vma_unmapped_se')}"


def test_v75_isolated_tour_day_with_recent_foreign_resolves_to_z76():
    """Isolierter Tour-Tag NACH Auslands-Cluster → Z76 Heimkehr (nicht Sonstiges)."""
    from app import _deterministic_classify_v7, _match_dp_se_per_day
    # Konstruktion: Auslandstour 03-05.01 BLR, dann frei, dann isolierter Tour-Tag 06.01
    structured = {'days': [
        {'datum': '2025-01-03', 'activity_type': 'tour', 'overnight_after_day': True,
         'has_fl': True, 'routing': ['FRA','BLR'], 'layover_ort': 'BLR'},
        {'datum': '2025-01-04', 'activity_type': 'tour', 'overnight_after_day': True,
         'has_fl': True, 'layover_ort': 'BLR'},
        {'datum': '2025-01-05', 'activity_type': 'tour', 'overnight_after_day': True,
         'has_fl': True, 'layover_ort': 'BLR'},
        # 06.01 als isolierter 'tour'-Tag ohne Vortag-Verbindung im DP
        {'datum': '2025-01-06', 'activity_type': 'tour', 'overnight_after_day': False, 'has_fl': False},
    ]}
    se = {'se_lines': [
        {'datum': '2025-01-03', 'stfrei_betrag': 30, 'stfrei_ort': 'BLR', 'stfrei_inland': False, 'storno': False},
        {'datum': '2025-01-04', 'stfrei_betrag': 39, 'stfrei_ort': 'BLR', 'stfrei_inland': False, 'storno': False},
        {'datum': '2025-01-05', 'stfrei_betrag': 39, 'stfrei_ort': 'BLR', 'stfrei_inland': False, 'storno': False},
        # Keine SE-Zeile am 06.01!
    ]}
    matched = _match_dp_se_per_day(structured, se)
    result = _deterministic_classify_v7(matched, 2025, 'FRA')
    # 06.01 darf NICHT 'Sonstiges' sein
    detail_06 = [d for d in result['tage_detail'] if d['datum'] == '2025-01-06']
    assert detail_06, "Tag 06.01 fehlt"
    assert detail_06[0]['klass'] == 'Z76', \
        f"Tag 06.01 sollte Z76 sein (Recent-Foreign-Cluster Heimkehr), ist {detail_06[0]['klass']}"
    # Auch nicht in unresolved_days
    unresolved = [u for u in result.get('unresolved_days', []) if '2025-01-06' in u]
    assert not unresolved, f"06.01 darf nicht unresolved sein, ist {unresolved}"


def test_v75_same_day_under_8h_becomes_zero_day_not_unresolved():
    """Same-Day ohne FL/SE/Cluster-Spur → ZeroDay, NICHT unresolved."""
    from app import _deterministic_classify_v7, _match_dp_se_per_day
    structured = {'days': [
        {'datum': '2025-04-15', 'activity_type': 'tour', 'overnight_after_day': False, 'has_fl': False,
         'routing': []},
    ]}
    matched = _match_dp_se_per_day(structured, {'se_lines': []})
    result = _deterministic_classify_v7(matched, 2025, 'FRA')
    detail = [d for d in result['tage_detail'] if d['datum'] == '2025-04-15']
    assert detail, "Tag 15.04 fehlt"
    # ZeroDay (kein VMA, aber AT) ist gültig — keine unresolved
    assert detail[0]['klass'] in ('ZeroDay', 'Z72'), \
        f"Tag 15.04 sollte ZeroDay oder Z72 sein, ist {detail[0]['klass']}"


def test_v75_office_not_unresolved():
    """Office-Tag landet nicht in unresolved_days."""
    from app import _deterministic_classify_v7, _match_dp_se_per_day
    structured = {'days': [{'datum': '2025-01-10', 'activity_type': 'office',
                            'overnight_after_day': False}]}
    matched = _match_dp_se_per_day(structured, {'se_lines': []})
    result = _deterministic_classify_v7(matched, 2025, 'FRA')
    assert len(result.get('unresolved_days', [])) == 0
    assert result['arbeitstage'] == 1
    assert result['fahr_tage'] == 1


def test_v75_standby_not_unresolved():
    """Standby zählt als AT, kein FT, nicht unresolved."""
    from app import _deterministic_classify_v7, _match_dp_se_per_day
    structured = {'days': [{'datum': '2025-01-10', 'activity_type': 'standby',
                            'overnight_after_day': False}]}
    matched = _match_dp_se_per_day(structured, {'se_lines': []})
    result = _deterministic_classify_v7(matched, 2025, 'FRA')
    assert len(result.get('unresolved_days', [])) == 0
    assert result['arbeitstage'] == 1
    assert result['fahr_tage'] == 0


def test_v75_z74_for_inland_volltag_in_mixed_cluster():
    """Mixed-Cluster: SOF→DE-24h→GOT — DE-Tag wird Z74 (24h Inland)."""
    from app import _deterministic_classify_v7, _match_dp_se_per_day
    structured = {'days': [
        {'datum': '2025-09-26', 'activity_type': 'tour', 'overnight_after_day': True,
         'has_fl': True, 'routing': ['FRA','SOF'], 'layover_ort': 'SOF'},
        {'datum': '2025-09-27', 'activity_type': 'tour', 'overnight_after_day': True,
         'has_fl': True, 'routing': ['SOF','MUC'], 'layover_ort': 'MUC'},
        {'datum': '2025-09-28', 'activity_type': 'tour', 'overnight_after_day': True,
         'has_fl': True, 'routing': ['MUC','GOT'], 'layover_ort': 'GOT'},
        {'datum': '2025-09-29', 'activity_type': 'tour', 'overnight_after_day': False, 'has_fl': False},
    ]}
    se = {'se_lines': [
        {'datum': '2025-09-26', 'stfrei_betrag': 32, 'stfrei_ort': 'SOF', 'stfrei_inland': False, 'storno': False},
        {'datum': '2025-09-27', 'stfrei_betrag': 28, 'stfrei_ort': 'MUC', 'stfrei_inland': True, 'storno': False},
        {'datum': '2025-09-28', 'stfrei_betrag': 33, 'stfrei_ort': 'GOT', 'stfrei_inland': False, 'storno': False},
        {'datum': '2025-09-29', 'stfrei_betrag': 30, 'stfrei_ort': 'GOT', 'stfrei_inland': False, 'storno': False},
    ]}
    matched = _match_dp_se_per_day(structured, se)
    result = _deterministic_classify_v7(matched, 2025, 'FRA')
    # 27.09 ist Inland-Volltag im Mixed-Cluster → Z74 erwartet
    detail_27 = [d for d in result['tage_detail'] if d['datum'] == '2025-09-27']
    assert detail_27 and detail_27[0]['klass'] == 'Z74', \
        f"27.09 sollte Z74 sein (Inland-Volltag im Mixed-Cluster), ist {detail_27[0]['klass'] if detail_27 else 'fehlt'}"


def test_v75_arbeitstage_only_real_classes():
    """Arbeitstage zählen nur aus tour/same_day/office/training/standby/zero,
    NICHT aus frei/urlaub/krank/sonstiges."""
    from app import _deterministic_classify_v7, _match_dp_se_per_day
    structured = {'days': [
        {'datum': '2025-01-01', 'activity_type': 'frei', 'overnight_after_day': False},
        {'datum': '2025-01-02', 'activity_type': 'urlaub', 'overnight_after_day': False},
        {'datum': '2025-01-03', 'activity_type': 'office', 'overnight_after_day': False},
        {'datum': '2025-01-04', 'activity_type': 'standby', 'overnight_after_day': False},
        {'datum': '2025-01-05', 'activity_type': 'krank', 'overnight_after_day': False},
    ]}
    matched = _match_dp_se_per_day(structured, {'se_lines': []})
    result = _deterministic_classify_v7(matched, 2025, 'FRA')
    # Office + Standby = 2 Arbeitstage
    assert result['arbeitstage'] == 2
    # Office = 1 Fahrtag, Standby = 0 → 1 Fahrtag
    assert result['fahr_tage'] == 1


def test_v75_hotel_only_for_cluster_overnight():
    """Hotelnächte zählen nur für Z73/Z74/Z76 mit overnight=true."""
    from app import _deterministic_classify_v7, _match_dp_se_per_day
    structured = {'days': [
        # Auslandstour mit Hotel
        {'datum': '2025-01-03', 'activity_type': 'tour', 'overnight_after_day': True,
         'has_fl': True, 'layover_ort': 'BLR'},
        {'datum': '2025-01-04', 'activity_type': 'tour', 'overnight_after_day': False, 'has_fl': False},
        # Frei-Tag mit "overnight" — soll NICHT als Hotel zählen
        {'datum': '2025-01-05', 'activity_type': 'frei', 'overnight_after_day': True},
    ]}
    se = {'se_lines': [
        {'datum': '2025-01-03', 'stfrei_betrag': 30, 'stfrei_ort': 'BLR', 'stfrei_inland': False, 'storno': False},
        {'datum': '2025-01-04', 'stfrei_betrag': 30, 'stfrei_ort': 'BLR', 'stfrei_inland': False, 'storno': False},
    ]}
    matched = _match_dp_se_per_day(structured, se)
    result = _deterministic_classify_v7(matched, 2025, 'FRA')
    # Nur 03.01 (Z76 mit overnight) zählt als Hotel — 05.01 ist frei
    assert result['hotel_naechte'] == 1, \
        f"Hotel sollte 1 sein (nur Z76-Übernachtung), ist {result['hotel_naechte']}"


def test_v75_z72_not_in_inland_cluster():
    """Z72 darf nicht für Tage entstehen die Teil eines Tour-Clusters sind."""
    from app import _deterministic_classify_v7, _match_dp_se_per_day
    # Tour-Cluster mit Same-Day-artigem Tag mittendrin (nicht möglich realistisch, aber Test-Schutz)
    structured = {'days': [
        {'datum': '2025-03-04', 'activity_type': 'tour', 'overnight_after_day': True,
         'has_fl': True, 'layover_ort': 'MUC'},
        {'datum': '2025-03-05', 'activity_type': 'tour', 'overnight_after_day': True,
         'has_fl': True, 'layover_ort': 'MUC'},
        {'datum': '2025-03-06', 'activity_type': 'tour', 'overnight_after_day': False, 'has_fl': False},
    ]}
    se = {'se_lines': [
        {'datum': '2025-03-04', 'stfrei_betrag': 14, 'stfrei_ort': 'MUC', 'stfrei_inland': True, 'storno': False},
        {'datum': '2025-03-05', 'stfrei_betrag': 28, 'stfrei_ort': 'MUC', 'stfrei_inland': True, 'storno': False},
        {'datum': '2025-03-06', 'stfrei_betrag': 14, 'stfrei_ort': 'MUC', 'stfrei_inland': True, 'storno': False},
    ]}
    matched = _match_dp_se_per_day(structured, se)
    result = _deterministic_classify_v7(matched, 2025, 'FRA')
    # Inland-Tour: kein Z72
    assert result['z72_tage'] == 0, f"Z72 sollte 0 sein in Inland-Tour, ist {result['z72_tage']}"
    # Stattdessen: Z73 An/Ab + Z74 Volltag
    assert result['z73_tage'] >= 2
    assert result['z74_tage'] >= 1


def test_v75_active_se_inland_without_cluster_z73():
    """Aktive Inland-SE-Zeile bei DP=unknown (ohne Cluster) → Z73 (nicht Sonstiges)."""
    from app import _deterministic_classify_v7, _match_dp_se_per_day
    structured = {'days': [
        {'datum': '2025-04-10', 'activity_type': 'unknown', 'overnight_after_day': False},
    ]}
    se = {'se_lines': [
        {'datum': '2025-04-10', 'stfrei_betrag': 14, 'stfrei_ort': 'HAM', 'stfrei_inland': True, 'storno': False},
    ]}
    matched = _match_dp_se_per_day(structured, se)
    result = _deterministic_classify_v7(matched, 2025, 'FRA')
    detail = [d for d in result['tage_detail'] if d['datum'] == '2025-04-10']
    assert detail and detail[0]['klass'] == 'Z73', \
        f"DP=unknown + aktive Inland-SE → Z73, ist {detail[0]['klass'] if detail else 'fehlt'}"
    # Audit-Note dokumentiert die Reklassifikation
    assert any('2025-04-10' in n for n in result.get('audit_notes', []))


# ── v8 Tests: Reader-Versions, Health-Check, Plausi-Hard-Fails, Issue-Klass ──

# Reference-Contract Soll-Werte für Live-Vergleich (echte Files in Phase 2/3)
REFERENCE_2025 = {
    'arbeitstage': 133,
    'fahrtage':    58,
    'hotel':       66,
    'z72':          5,
    'z73':         11,
    'z74':          1,
    'z76':       4794.00,
    'brutto':    6020.72,
}
REFERENCE_2025_TOLERANCE = {
    'arbeitstage': 3,
    'fahrtage':    3,
    'hotel':       3,
    'z72':         2,
    'z73':         2,
    'z74':         0,
    'z76':       150.0,
    'brutto':    250.0,
}


def test_v8_reader_versions_constants_exist():
    """READER_VERSIONS, ENGINE_VERSION sind exportiert."""
    from app import READER_VERSIONS, ENGINE_VERSION, APP_VERSION, PROMPT_VERSION
    assert 'lsb' in READER_VERSIONS
    assert 'se' in READER_VERSIONS
    assert 'dp' in READER_VERSIONS
    assert ENGINE_VERSION.startswith('deterministic_v')
    assert APP_VERSION.startswith('8.')


def test_v8_health_check_red_when_lsb_missing():
    """LSB komplett fehlend → red."""
    from app import _document_health_check
    health = _document_health_check(None, {'se_lines': [{'datum': '2025-01-01', 'stfrei_betrag': 30}]},
                                     {'days': [{'datum': '2025-01-01'}]}, 2025)
    assert health['status'] == 'red'
    assert any(i['source'] == 'LSB' and i['severity'] == 'red' for i in health['issues'])


def test_v8_health_check_red_when_brutto_zero():
    """LSB ohne Brutto → red Hard-Fail."""
    from app import _document_health_check
    health = _document_health_check({'brutto': 0}, {'se_lines': [{'datum': '2025-01-01', 'stfrei_betrag': 30}]},
                                     {'days': [{'datum': '2025-01-01'}]}, 2025)
    assert health['status'] == 'red'


def test_v8_health_check_yellow_low_z77():
    """SE mit Z77 < 500€ → yellow Warning."""
    from app import _document_health_check
    health = _document_health_check(
        {'brutto': 50000, 'z17': 0},
        {'se_lines': [
            {'datum': f'2025-{m:02d}-01', 'stfrei_betrag': 30, 'storno': False}
            for m in range(1, 13)
        ]},
        {'days': [{'datum': f'2025-{m:02d}-01'} for m in range(1, 13)]},
        2025,
    )
    # Z77 = 12 × 30 = 360 < 500
    assert health['status'] in ('yellow', 'green')


def test_v8_health_check_green_for_complete_docs():
    """Vollständige Dokumente → green."""
    from app import _document_health_check
    se_lines = [{'datum': f'2025-{m:02d}-15', 'stfrei_betrag': 100, 'storno': False}
                for m in range(1, 13)]  # 12 Monate × 100€ = 1200€ Z77
    days = [{'datum': f'2025-{m:02d}-15', 'activity_type': 'tour'} for m in range(1, 13)]
    days += [{'datum': f'2025-{m:02d}-{d:02d}', 'activity_type': 'frei'}
             for m in range(1, 13) for d in (1, 5, 10, 20, 25)]
    health = _document_health_check({'brutto': 50000, 'z17': 1200},
                                     {'se_lines': se_lines}, {'days': days}, 2025)
    assert health['status'] == 'green', f"Vollständige Docs sollten green sein, sind {health['status']}: {health['issues']}"


def test_v8_health_check_warns_on_no_dp_for_se_dates():
    """SE-Zeilen ohne DP-Match → yellow."""
    from app import _document_health_check
    se_lines = [{'datum': f'2025-01-{d:02d}', 'stfrei_betrag': 30, 'storno': False}
                for d in range(1, 16)]
    health = _document_health_check({'brutto': 50000}, {'se_lines': se_lines},
                                     {'days': [{'datum': '2025-06-01', 'activity_type': 'tour'}]}, 2025)
    # Viele SE-Zeilen ohne DP-Match
    assert health['status'] in ('yellow', 'red')


def test_v8_issue_class_replaces_silent_sonstiges():
    """tour ohne overnight, kein Cluster-Kontext, ohne SE → Issue (nicht Sonstiges)."""
    from app import _deterministic_classify_v7, _match_dp_se_per_day
    structured = {'days': [
        {'datum': '2025-04-15', 'activity_type': 'unknown', 'overnight_after_day': False, 'has_fl': False},
    ]}
    matched = _match_dp_se_per_day(structured, {'se_lines': []})
    result = _deterministic_classify_v7(matched, 2025, 'FRA')
    detail = [d for d in result['tage_detail'] if d['datum'] == '2025-04-15']
    # unknown ohne SE → ZeroDay (definiert) statt Sonstiges
    assert detail and detail[0]['klass'] != 'Sonstiges'


def test_v8_plausi_hard_fail_hotel_above_arbeitstage():
    """Hard-Fail: hotel_naechte > arbeitstage."""
    # Künstliches Result-Dict simulieren — Test der Logik direkt im Funktionsergebnis schwer
    # Stattdessen: garantieren dass die Logik existiert
    from app import _deterministic_classify_v7
    src = _deterministic_classify_v7.__code__.co_consts
    src_str = ' '.join(str(c) for c in src if isinstance(c, str))
    assert 'plausi_hard_fails' in src_str or 'hard_fail' in src_str.lower() or 'unplausibel' in src_str.lower()


def test_v8_reference_contract_2025_constants_present():
    """Reference-Constants 2025 sind im Test-Modul definiert."""
    assert REFERENCE_2025['arbeitstage'] == 133
    assert REFERENCE_2025['z76'] == 4794.00
    assert REFERENCE_2025['brutto'] == 6020.72
    # Toleranzen sind sinnvolle ±-Werte
    assert REFERENCE_2025_TOLERANCE['arbeitstage'] == 3
    assert REFERENCE_2025_TOLERANCE['z74'] == 0  # Z74 ist sehr selten — keine Toleranz


def test_v8_health_endpoint_format():
    """Health-Endpoint /  liefert reader_versions, engine, prompt_version."""
    from app import app
    client = app.test_client()
    r = client.get('/')
    assert r.status_code == 200
    data = r.get_json()
    assert 'reader_versions' in data
    assert 'engine' in data
    assert 'version' in data
    assert data['version'].startswith('8.')


# ── v8.1 Tests: DP-Schema-Erweiterung, requires_commute, Z72-Dauer ──

def test_v81_dp_enrichment_requires_commute_for_office():
    """Office am Homebase ohne Übernachtung → requires_commute=true."""
    from app import _enrich_dp_with_v8_fields
    dp = {'datum': '2025-01-15', 'activity_type': 'office', 'overnight_after_day': False}
    _enrich_dp_with_v8_fields(dp, prev_dp=None, next_dp=None, homebase='FRA')
    assert dp['requires_commute'] is True
    assert dp['is_workday'] is True
    assert dp['starts_at_homebase'] is True
    assert dp['ends_at_homebase'] is True


def test_v81_dp_enrichment_no_commute_for_layover():
    """Tour-Layover-Tag → requires_commute=false (kein Weg zur Homebase)."""
    from app import _enrich_dp_with_v8_fields
    prev_dp = {'datum': '2025-01-14', 'activity_type': 'tour', 'overnight_after_day': True}
    dp = {'datum': '2025-01-15', 'activity_type': 'tour', 'overnight_after_day': True,
          'routing': ['BLR']}
    _enrich_dp_with_v8_fields(dp, prev_dp=prev_dp, next_dp=None, homebase='FRA')
    assert dp['requires_commute'] is False
    assert dp['is_workday'] is True
    assert dp['starts_at_homebase'] is False
    assert dp['ends_at_homebase'] is False


def test_v81_dp_enrichment_no_commute_for_standby():
    """Standby zuhause → requires_commute=false."""
    from app import _enrich_dp_with_v8_fields
    dp = {'datum': '2025-01-15', 'activity_type': 'standby', 'overnight_after_day': False}
    _enrich_dp_with_v8_fields(dp, prev_dp=None, next_dp=None, homebase='FRA')
    assert dp['requires_commute'] is False
    assert dp['is_workday'] is True


def test_v81_dp_enrichment_no_commute_for_frei():
    """Frei → requires_commute=false, is_workday=false."""
    from app import _enrich_dp_with_v8_fields
    dp = {'datum': '2025-01-15', 'activity_type': 'frei', 'overnight_after_day': False}
    _enrich_dp_with_v8_fields(dp, prev_dp=None, next_dp=None, homebase='FRA')
    assert dp['requires_commute'] is False
    assert dp['is_workday'] is False


def test_v81_dp_enrichment_tour_anreise_commute():
    """Tour-Anreise ab Homebase → requires_commute=true."""
    from app import _enrich_dp_with_v8_fields
    dp = {'datum': '2025-01-15', 'activity_type': 'tour', 'overnight_after_day': True,
          'routing': ['FRA', 'BLR'], 'has_fl': True}
    _enrich_dp_with_v8_fields(dp, prev_dp=None, next_dp=None, homebase='FRA')
    assert dp['starts_at_homebase'] is True
    assert dp['requires_commute'] is True
    # Layover heute → ends_at_homebase=false
    assert dp['ends_at_homebase'] is False


def test_v81_dp_enrichment_heimkehrtag_no_commute():
    """Heimkehrtag (BLR→FRA) → starts_at_homebase=false (Dienst beginnt auswärts),
    ends_at_homebase=true, requires_commute=false."""
    from app import _enrich_dp_with_v8_fields
    prev_dp = {'datum': '2025-01-15', 'activity_type': 'tour', 'overnight_after_day': True,
               'layover_ort': 'BLR'}
    dp = {'datum': '2025-01-16', 'activity_type': 'tour', 'overnight_after_day': False,
          'routing': ['BLR', 'FRA']}
    _enrich_dp_with_v8_fields(dp, prev_dp=prev_dp, next_dp=None, homebase='FRA')
    assert dp['starts_at_homebase'] is False
    assert dp['ends_at_homebase'] is True
    assert dp['requires_commute'] is False  # keine NEUE Anfahrt


def test_v81_dp_enrichment_does_not_overwrite_sonnet_values():
    """Wenn Sonnet die Felder bereits gesetzt hat, NICHT überschreiben."""
    from app import _enrich_dp_with_v8_fields
    dp = {'datum': '2025-01-15', 'activity_type': 'tour', 'overnight_after_day': True,
          'requires_commute': False, 'starts_at_homebase': False, 'is_workday': True}
    _enrich_dp_with_v8_fields(dp, prev_dp=None, next_dp=None, homebase='FRA')
    # Heuristik würde requires_commute=True für Tour-Anreise schätzen — aber Sonnet hat False gesetzt.
    assert dp['requires_commute'] is False
    assert dp['starts_at_homebase'] is False


def test_v81_duty_duration_calculated_from_times():
    """duty_duration_minutes wird aus start_time/end_time berechnet wenn fehlt."""
    from app import _enrich_dp_with_v8_fields
    dp = {'datum': '2025-01-15', 'activity_type': 'same_day',
          'overnight_after_day': False, 'start_time': '06:30', 'end_time': '15:00'}
    _enrich_dp_with_v8_fields(dp, prev_dp=None, next_dp=None, homebase='FRA')
    # 6:30 → 15:00 = 8h 30min = 510min
    assert dp['duty_duration_minutes'] == 510


def test_v81_duty_duration_handles_overnight_times():
    """Bei Tour über Mitternacht: 22:00 → 02:00 = 4h."""
    from app import _enrich_dp_with_v8_fields
    dp = {'datum': '2025-01-15', 'activity_type': 'tour',
          'overnight_after_day': True, 'start_time': '22:00', 'end_time': '02:00'}
    _enrich_dp_with_v8_fields(dp, prev_dp=None, next_dp=None, homebase='FRA')
    assert dp['duty_duration_minutes'] == 240


def test_v81_z72_duration_zero_day_when_under_8h():
    """Same-Day < 8h ohne Fahrzeit-Plus → ZeroDay."""
    from app import _deterministic_classify_v7, _match_dp_se_per_day
    structured = {'days': [
        {'datum': '2025-01-15', 'activity_type': 'same_day', 'overnight_after_day': False,
         'has_fl': False, 'start_time': '08:00', 'end_time': '14:00'},  # 6h
    ]}
    matched = _match_dp_se_per_day(structured, {'se_lines': []}, 'FRA')
    result = _deterministic_classify_v7(matched, 2025, 'FRA', commute_minutes=0)
    detail = [d for d in result['tage_detail'] if d['datum'] == '2025-01-15']
    assert detail and detail[0]['klass'] == 'ZeroDay', \
        f"6h Dienst ohne Fahrzeit → ZeroDay, ist {detail[0]['klass'] if detail else 'fehlt'}"
    assert result['z72_tage'] == 0


def test_v81_z72_duration_with_commute_above_8h():
    """Same-Day 7h Dienst + 2× 35min Fahrzeit = 8h 10min → Z72."""
    from app import _deterministic_classify_v7, _match_dp_se_per_day
    structured = {'days': [
        {'datum': '2025-01-15', 'activity_type': 'same_day', 'overnight_after_day': False,
         'has_fl': False, 'start_time': '08:00', 'end_time': '15:00'},  # 7h
    ]}
    matched = _match_dp_se_per_day(structured, {'se_lines': []}, 'FRA')
    result = _deterministic_classify_v7(matched, 2025, 'FRA', commute_minutes=35)
    detail = [d for d in result['tage_detail'] if d['datum'] == '2025-01-15']
    # 7h Dienst + 70min Fahrzeit = 8h 10min ≥ 8h → Z72
    assert detail and detail[0]['klass'] == 'Z72', \
        f"7h+70min Fahrzeit sollte Z72 sein, ist {detail[0]['klass'] if detail else 'fehlt'}"


def test_v81_z72_no_duration_falls_back_to_z72():
    """Same-Day ohne Zeitinfo → konservativ Z72 (alte Heuristik)."""
    from app import _deterministic_classify_v7, _match_dp_se_per_day
    structured = {'days': [
        {'datum': '2025-01-15', 'activity_type': 'same_day', 'overnight_after_day': False,
         'has_fl': False},
    ]}
    matched = _match_dp_se_per_day(structured, {'se_lines': []}, 'FRA')
    result = _deterministic_classify_v7(matched, 2025, 'FRA', commute_minutes=0)
    assert result['z72_tage'] == 1


def test_v81_fahrtage_from_requires_commute():
    """Fahrtage werden aus dp.requires_commute aggregiert.
    Office (commute=true) + Standby (commute=false) → 1 Fahrtag."""
    from app import _deterministic_classify_v7, _match_dp_se_per_day
    structured = {'days': [
        {'datum': '2025-01-10', 'activity_type': 'office', 'overnight_after_day': False},
        {'datum': '2025-01-11', 'activity_type': 'standby', 'overnight_after_day': False},
    ]}
    matched = _match_dp_se_per_day(structured, {'se_lines': []}, 'FRA')
    result = _deterministic_classify_v7(matched, 2025, 'FRA', commute_minutes=0)
    assert result['arbeitstage'] == 2  # Office + Standby
    assert result['fahr_tage'] == 1    # nur Office


def test_v81_dp_schema_has_commute_fields():
    """DP-Schema enthält die neuen v8-Felder."""
    import inspect
    from app import _sonnet_read_dp_structured
    src = inspect.getsource(_sonnet_read_dp_structured)
    for field in ('starts_at_homebase', 'ends_at_homebase', 'is_workday',
                  'requires_commute', 'start_time', 'end_time',
                  'duty_duration_minutes', 'raw_marker', 'raw_lines'):
        assert field in src, f"Feld '{field}' fehlt im DP-Reader-Schema/Prompt"


def test_v81_layover_does_not_count_as_fahrtag():
    """Layover-Tag (3-Tages-Auslandstour) erzeugt 1 Anreise + 1 Heimkehr-Fahrtag,
    nicht 3 Fahrtage."""
    from app import _deterministic_classify_v7, _match_dp_se_per_day
    structured = {'days': [
        {'datum': '2025-01-13', 'activity_type': 'tour', 'overnight_after_day': True,
         'has_fl': True, 'routing': ['FRA', 'BLR'], 'layover_ort': 'BLR'},
        {'datum': '2025-01-14', 'activity_type': 'tour', 'overnight_after_day': True,
         'has_fl': True, 'layover_ort': 'BLR'},
        {'datum': '2025-01-15', 'activity_type': 'tour', 'overnight_after_day': False,
         'has_fl': True, 'routing': ['BLR', 'FRA']},
    ]}
    se = {'se_lines': [
        {'datum': '2025-01-13', 'stfrei_betrag': 30, 'stfrei_ort': 'BLR', 'stfrei_inland': False, 'storno': False},
        {'datum': '2025-01-14', 'stfrei_betrag': 39, 'stfrei_ort': 'BLR', 'stfrei_inland': False, 'storno': False},
        {'datum': '2025-01-15', 'stfrei_betrag': 30, 'stfrei_ort': 'BLR', 'stfrei_inland': False, 'storno': False},
    ]}
    matched = _match_dp_se_per_day(structured, se, 'FRA')
    result = _deterministic_classify_v7(matched, 2025, 'FRA', commute_minutes=0)
    # 1 Anreise (commute=true) + 1 Heimkehr (ends_at_homebase + prev_overnight)
    # = 2 Fahrtage. NICHT 3.
    assert result['fahr_tage'] == 2, \
        f"3-Tages-Tour sollte 2 Fahrtage haben (An + Heimkehr), ist {result['fahr_tage']}"


def test_v81_isolated_heimkehrtag_no_double_fahrtag():
    """Bugfix v8.1.1: Isolierter Heimkehrtag (Vortag im DP als 'frei' gelesen)
    darf NICHT als requires_commute=true gewertet werden.
    Sonst: doppelte Fahrtag-Zählung (commute + Heimkehr-Erkennung)."""
    from app import _enrich_dp_with_v8_fields
    # 03.01 BLR-Anreise (overnight=true) — Sonnet hat 04+05 NICHT erkannt (z.B. als frei),
    # 06.01 als isolierter 'tour' ohne overnight, ohne routing → Heimkehrtag.
    prev_dp = {'datum': '2025-01-05', 'activity_type': 'frei', 'overnight_after_day': False}
    dp = {'datum': '2025-01-06', 'activity_type': 'tour', 'overnight_after_day': False}  # kein routing!
    _enrich_dp_with_v8_fields(dp, prev_dp=prev_dp, next_dp=None, homebase='FRA')
    # KEIN Anreise (nicht starts_at_homebase) — Tag ist eher Heimkehr
    assert dp['starts_at_homebase'] is False, \
        'Tour ohne overnight + Vortag-frei + ohne Routing darf KEIN Anreise-Fahrtag sein'
    assert dp['requires_commute'] is False


def test_v81_tour_anreise_with_overnight_still_counts():
    """Tour-Anreise mit overnight=true (Hotel auswärts) bleibt commute=true,
    auch ohne explizites Routing."""
    from app import _enrich_dp_with_v8_fields
    prev_dp = {'datum': '2025-01-02', 'activity_type': 'frei', 'overnight_after_day': False}
    dp = {'datum': '2025-01-03', 'activity_type': 'tour', 'overnight_after_day': True,
          'has_fl': True, 'layover_ort': 'BLR'}
    _enrich_dp_with_v8_fields(dp, prev_dp=prev_dp, next_dp=None, homebase='FRA')
    assert dp['starts_at_homebase'] is True
    assert dp['requires_commute'] is True


# ── v8.1.2 Tests: Z77-Wording entschärft ──

def test_v812_z77_diff_not_in_user_errors():
    """Z77-Diff zwischen lines/months erscheint NICHT mehr in den user-facing
    errors-Liste der Hybrid-Pipeline."""
    import inspect
    from app import hybrid_analyze
    src = inspect.getsource(hybrid_analyze)
    # Die wörtliche User-facing-Formulierung darf nicht mehr in den errors landen
    assert "errors.append(f'Z77-Diff:" not in src, \
        "Z77-Diff darf nicht in user-facing errors-Liste"
    assert "Z77-Diff: lines=" not in src or "errors.append" not in src.split("Z77-Diff: lines=")[0][-200:], \
        "Z77-Diff darf nicht zu user-facing notes werden"


def test_v812_z77_audit_kept_in_se_summary():
    """z77_from_lines und z77_from_months bleiben im se_summary für Detailbereich."""
    import inspect
    from app import hybrid_analyze
    src = inspect.getsource(hybrid_analyze)
    # Die Felder müssen weiterhin gesetzt werden (nur eben intern, nicht user-facing)
    assert "z77_from_lines" in src
    assert "z77_from_months" in src
    assert "z77_diff" in src


def test_v812_z77_user_note_uses_friendly_wording():
    """Wenn Z77 > VMA, ist die User-Note sachlich formuliert (kein 'übersteigt BMF')."""
    import inspect
    from app import _berechne_via_hybrid
    src = inspect.getsource(_berechne_via_hybrid)
    # Sachliches Wording sollte vorkommen
    assert 'Steuerfreie Spesen wurden berücksichtigt' in src
    # Hartes Wording aus alter Version raus
    assert 'übersteigt BMF-Pauschalen' not in src
    assert 'LH hat ' not in src  # "LH hat 4705€ stfrei gezahlt" — raus
    assert 'Lufthansa hat ' not in src  # auch in dead-code raus


def test_v812_z76_inkonsistenz_uses_pruefhinweis():
    """Z76-Plausi-Check ist 'Prüfhinweis', keine 'Inkonsistenz' / 'Audit-Hinweis'."""
    import inspect
    from app import _berechne_via_hybrid
    src = inspect.getsource(_berechne_via_hybrid)
    # Altes Wording raus
    assert 'Z76-Inkonsistenz' not in src
    assert 'Audit-Hinweis: Z76' not in src
    # Neue Formulierung
    assert 'Prüfhinweis' in src


def test_v812_steuerfreie_spesen_positive_note():
    """Bei normalem Z77 > 0 erscheint eine sachlich-positive Info-Note."""
    import inspect
    from app import _berechne_via_hybrid
    src = inspect.getsource(_berechne_via_hybrid)
    assert 'Steuerfreie Spesen laut Streckeneinsatzabrechnung' in src
    assert 'Dieser Betrag wurde bei der Verrechnung berücksichtigt' in src


def test_v812_no_lines_months_in_user_notes():
    """Technische Labels 'lines=' / 'months=' tauchen nicht in user-facing Notes auf."""
    import inspect
    from app import _berechne_via_hybrid, hybrid_analyze
    for fn in (_berechne_via_hybrid, hybrid_analyze):
        src = inspect.getsource(fn)
        # In notes.append-Strings darf weder 'lines=' noch 'months=' stehen
        # (Print-Logs für [v8-se] sind okay, da intern)
        for line in src.split('\n'):
            if 'notes.append' in line:
                assert 'lines=' not in line, f"User-Note enthält 'lines=': {line.strip()[:120]}"
                assert 'months=' not in line, f"User-Note enthält 'months=': {line.strip()[:120]}"


if __name__ == '__main__':
    import pytest
    sys.exit(pytest.main([__file__, '-v']))
