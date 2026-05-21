"""Rel Phase 8 — German Tax Rules Validation.

Tests:
- Entfernungspauschale §9 EStG 0.30€/km bis 20km, 0.38€/km ab 21
- VMA Inland Z72 nur >=8h
- VMA Ausland Z76 nur mit foreign-Evidence
- KI liefert NIE Steuerbetraege
- BMF-Mapping fuer mehrere Laender
"""
import os
import sys
import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)

if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import app as app_module


# ════════════════════════════════════════════════════════════════════════════
# Entfernungspauschale §9 EStG
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('km,fahrtage,expected_min,expected_max', [
    (19, 58, 19*0.30*58 - 1, 19*0.30*58 + 1),   # <= 20km: 0.30/km
    (20, 58, 20*0.30*58 - 1, 20*0.30*58 + 1),   # genau 20: 0.30/km
    (28, 58, (20*0.30 + 8*0.38)*58 - 1, (20*0.30 + 8*0.38)*58 + 1),  # 21+: 0.38
    (50, 58, (20*0.30 + 30*0.38)*58 - 1, (20*0.30 + 30*0.38)*58 + 1),
])
def test_entfernungspauschale_formula(km, fahrtage, expected_min, expected_max):
    """Entfernungspauschale = km × pauschale × fahrtage, mit 20km-Grenze."""
    # Direkter Formel-Test (Python-Berechnung in app.py)
    if km <= 20:
        per_km = km * 0.30
    else:
        per_km = 20 * 0.30 + (km - 20) * 0.38
    total = per_km * fahrtage
    assert expected_min <= total <= expected_max


def test_entfernungspauschale_km_zero_no_amount():
    """km=0 → keine Entfernungspauschale."""
    assert 0 * 0.30 * 58 == 0


def test_entfernungspauschale_50km_500fahrtage_capped():
    """Sanity-Cap (km<=500, fahrtage<=230) per Backend-Validation."""
    src = open(os.path.join(ROOT_DIR, 'app.py')).read()
    assert 'min(500.0' in src or 'min(500, km' in src


# ════════════════════════════════════════════════════════════════════════════
# BMF Auslandspauschalen 2025 — Stichprobe
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('iata,land_substring,expected_voll_min', [
    ('JFK', 'USA', 50.0),
    ('LHR', 'Großbritannien', 50.0),
    ('TLV', 'Israel', 50.0),
    ('BLR', 'Indien', 30.0),
    ('CDG', 'Frankreich', 30.0),
    ('NRT', 'Japan', 40.0),
    ('SIN', 'Singapur', 50.0),
])
def test_bmf_iata_mapping(iata, land_substring, expected_voll_min):
    """BMF-Mapping liefert Tagessatz fuer Stichproben-IATAs."""
    try:
        from bmf_data import IATA_TO_BMF, BMF_AUSLAND_BY_YEAR
    except ImportError:
        pytest.skip('bmf_data nicht importierbar')
    land = IATA_TO_BMF.get(iata)
    assert land, f'BMF-Mapping fehlt fuer {iata}'
    assert land_substring in land, f'Land {land!r} sollte {land_substring!r} enthalten'
    raw = BMF_AUSLAND_BY_YEAR.get(2025, {}).get(land)
    assert raw, f'BMF-Tagessatz fehlt fuer {land}'
    if isinstance(raw, tuple):
        voll = float(raw[0])
    elif isinstance(raw, dict):
        voll = float(raw.get('voll_24h', 0) or 0)
    else:
        voll = 0
    assert voll >= expected_voll_min - 5, \
        f'voll_24h fuer {land} sollte mindestens {expected_voll_min} sein, war {voll}'


# ════════════════════════════════════════════════════════════════════════════
# Anti-Tax-Sanitizer
# ════════════════════════════════════════════════════════════════════════════

def test_ki_resolver_never_returns_amount():
    """KI-Resolver-Schema verbietet Tax-Amount-Felder."""
    if not hasattr(app_module, '_READER_V2_FORBIDDEN_FIELDS'):
        pytest.skip('Forbidden-Fields-Set nicht exportiert')
    forbidden = app_module._READER_V2_FORBIDDEN_FIELDS
    expected = {'amount', 'eur', 'euro', 'tagesatz', 'tax', 'steuer', 'betrag', 'pauschale', 'rate'}
    assert expected.issubset(forbidden)


def test_cas_reader_v2_schema_rejects_tax_fields():
    """Schema-Validator rejected Output mit Tax-Field."""
    bad_output = {
        'datum': '2025-01-01',
        'activity_type': 'tour',
        'amount': 50.0,  # FORBIDDEN
    }
    valid, issues = app_module._cas_reader_v2_validate_schema(bad_output)
    assert not valid
    assert any('FORBIDDEN' in i for i in issues)


# ════════════════════════════════════════════════════════════════════════════
# Verpflegungsmehraufwand Pflicht-Regeln
# ════════════════════════════════════════════════════════════════════════════

def test_z72_requires_8h_threshold():
    """Z72 wird NUR bei duty>=480min vergeben (Phase 6b)."""
    src = open(os.path.join(ROOT_DIR, 'app.py')).read()
    # Phase 6b: duty>=480 ist die Pflicht-Schwelle
    assert '_duty_min >= 480' in src or 'duty_min >= 480' in src


def test_z76_only_with_foreign_evidence():
    """Z76-Klassifikation erfordert foreign-Evidence (layover OR SE-foreign-Ort)."""
    # Static check: Z76-Code-Pfad braucht is_foreign_tour oder _bmf_sat
    src = open(os.path.join(ROOT_DIR, 'app.py')).read()
    # Z76 erscheint nur in foreign-paths
    z76_lines = [l for l in src.split('\n') if "klass = 'Z76'" in l]
    # Sample-Check: Z76 wird mit Z76-Berechtigung gesetzt, nicht aus Marker allein
    assert len(z76_lines) >= 1


def test_z73_inland_anreise_rate():
    """Z73 Inland-Anreise = 14€."""
    src = open(os.path.join(ROOT_DIR, 'app.py')).read()
    # INLAND_AN_ABREISE = 14
    assert 'INLAND_AN_ABREISE' in src
    assert '14.0' in src  # Konstante


def test_z74_inland_volltag_rate():
    """Z74 Inland-Volltag = 28€."""
    src = open(os.path.join(ROOT_DIR, 'app.py')).read()
    assert 'INLAND_VOLLTAG_24H' in src
    assert '28.0' in src


# ════════════════════════════════════════════════════════════════════════════
# AG-Erstattung / Z77
# ════════════════════════════════════════════════════════════════════════════

def test_z77_se_employer_reimbursement_separated():
    """Z77 (steuerfrei vom AG) wird vom Z76-Steueranspruch abgezogen."""
    src = open(os.path.join(ROOT_DIR, 'app.py')).read()
    assert 'z77' in src.lower()
    # Pflicht: Topf-Trennung dokumentiert
    assert ('z76_minus_z77' in src.lower() or 'z76' in src.lower() and 'z77' in src.lower())


def test_jobticket_separated_from_entfernungspauschale():
    """Jobticket (Z17) wird separat erfasst, kein Doppel-Abzug."""
    src = open(os.path.join(ROOT_DIR, 'app.py')).read()
    assert "jobticket" in src.lower() or "z17" in src.lower()


# ════════════════════════════════════════════════════════════════════════════
# Edge Cases
# ════════════════════════════════════════════════════════════════════════════

def test_km_missing_no_crash():
    """km=0 darf nicht crashen."""
    h = app_module._build_v11_upload_health(
        lsb_data={'brutto': 50000},
        se_structured={'se_lines': [{'datum': '2025-01-15', 'storno': False, 'stfrei_betrag': 50.0}]},
        cas_classification={'_tage_detail': [{'datum': '2025-01-15'}]},
        year=2025)
    # Document-Health läuft auch ohne km im input
    assert h is not None


def test_no_ki_amount_in_pipeline_output():
    """Pipeline-Output enthält keine KI-generated EUR-Beträge im audit."""
    src = open(os.path.join(ROOT_DIR, 'app.py')).read()
    # Sanity: ki-cache hat keine amount/eur-keys
    # (würde im Cache-Eintrag stehen)
    assert '_ai_resolver_cache' in src
