"""MegaR Phase 2 — Dynamic Parameterization Tests.

Verifiziert dass Homebase / Marker / Role / Airline NICHT hardcoded sind.
Spec: docs/DYNAMIC_PARAMETERIZATION_AUDIT.md
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


def _read_backend():
    return open(os.path.join(ROOT_DIR, 'app.py'), encoding='utf-8').read()


def _make_day(datum, **kw):
    dp = {
        'datum': datum,
        'activity_type': kw.get('activity_type', 'frei'),
        'routing': kw.get('routing', []),
        'layover_ort': kw.get('layover_ort', ''),
        'overnight_after_day': kw.get('overnight_after_day', False),
        'start_time': kw.get('start_time', ''),
        'end_time': kw.get('end_time', ''),
        'duty_duration_minutes': kw.get('duty_duration_minutes', 0),
        'raw_marker': kw.get('raw_marker', ''),
        'has_fl': kw.get('has_fl', False),
        'is_workday': kw.get('is_workday', False),
        'requires_commute': kw.get('requires_commute', False),
        'starts_at_homebase': kw.get('starts_at_homebase', False),
        'ends_at_homebase': kw.get('ends_at_homebase', False),
        'raw_lines': [],
        'confidence': 0.9,
    }
    se = kw.get('se') or {'count': 0, 'stfrei_ort': '', 'stfrei_inland': None,
                          'stfrei_total': 0.0, 'zwoelftel': 0, 'lines': []}
    return {'datum': datum, 'dp': dp, 'se': se}


# ════════════════════════════════════════════════════════════════════════════
# Homebase NICHT hardcoded — funktioniert mit jedem BASE
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('base', ['FRA', 'MUC', 'HAM', 'DUS', 'BER'])
def test_normalize_tours_works_with_dynamic_homebase(base):
    """Tour-Normalisierung funktioniert mit jeder Homebase, nicht nur FRA."""
    matched = [
        # Tour-Start: <base> → BLR (foreign)
        _make_day('2025-03-15', activity_type='tour', routing=[base, 'BLR'],
                  layover_ort='BLR', overnight_after_day=True,
                  starts_at_homebase=True, has_fl=True,
                  duty_duration_minutes=600, raw_marker='12345 PU'),
        _make_day('2025-03-16', activity_type='tour', routing=['BLR', base],
                  layover_ort='', overnight_after_day=False,
                  ends_at_homebase=True, has_fl=True,
                  duty_duration_minutes=480, raw_marker='12345 PU'),
    ]
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase=base, year=2025)
    # Erste Tour muss starts_at_homebase=True haben für 2025-03-15
    found_start = False
    for t in tours:
        for d in t['days']:
            if d['datum'] == '2025-03-15' and d['role'] == 'tour_start':
                found_start = True
                break
    assert found_start, f'Tour-Start nicht erkannt mit base={base}'


def test_no_hardcoded_eq_fra_in_production_code():
    """app.py darf KEINE == 'FRA' Comparison im Produktiv-Code haben."""
    src = _read_backend()
    import re
    # Find: == 'FRA' or == "FRA"
    hits = re.findall(r"==\s*['\"]FRA['\"]", src)
    # Filter out comments / docstrings (rough heuristic)
    assert not hits, f'Hardcoded == FRA gefunden: {hits[:3]}'


def test_extract_homebase_handles_munich_base():
    """`_extract_homebase` mappt München → MUC."""
    assert app_module._extract_homebase('München') == 'MUC'
    assert app_module._extract_homebase('Munich (MUC)') == 'MUC'
    assert app_module._extract_homebase('MUC') == 'MUC'


def test_extract_homebase_handles_other_cities():
    assert app_module._extract_homebase('Hamburg') == 'HAM'
    assert app_module._extract_homebase('Berlin') == 'BER'
    assert app_module._extract_homebase('Düsseldorf') == 'DUS'
    assert app_module._extract_homebase('Stuttgart') == 'STR'


# ════════════════════════════════════════════════════════════════════════════
# Marker-Hardcoding: keine Marker-only Tax-Decision
# ════════════════════════════════════════════════════════════════════════════

def test_pu_marker_not_iata():
    """Marker 'PU' allein darf KEIN IATA-Match auslosen (anti-Pula)."""
    if not hasattr(app_module, '_extract_iata_from_marker'):
        pytest.skip('_extract_iata_from_marker nicht vorhanden')
    iata = app_module._extract_iata_from_marker('12345 PU')
    assert iata != 'PUY' and iata != 'PU', f'PU darf nicht zu Pula werden: {iata!r}'


def test_p1_marker_not_iata():
    """Marker P1 ist Position, nicht IATA."""
    if hasattr(app_module, '_extract_iata_from_marker'):
        iata = app_module._extract_iata_from_marker('31591 P1')
        assert iata != 'P1', 'P1 != IATA'


@pytest.mark.parametrize('marker', ['RES', 'SB_M', 'X', 'OFF', '==', ''])
def test_marker_alone_does_not_trigger_z76(marker):
    """Unbekannter/Standby-Marker allein ohne SE+Routing → keine Z76-Klassifikation."""
    matched = [
        _make_day('2025-06-10', activity_type='frei', raw_marker=marker,
                  routing=[] if marker in ('','==','OFF','X') else ['FRA'],
                  starts_at_homebase=False, ends_at_homebase=False,
                  overnight_after_day=False, duty_duration_minutes=0,
                  se={'count': 0, 'stfrei_ort': '', 'stfrei_inland': None,
                      'stfrei_total': 0.0, 'zwoelftel': 0, 'lines': []}),
    ]
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025)
    result = app_module._classify_days_from_normalized_tours(
        tours, year=2025, homebase='FRA')
    klass = result['tage_detail'][0]['klass']
    assert klass != 'Z76', f'Marker {marker!r} allein darf nicht zu Z76 fuehren — war {klass}'


def test_unknown_marker_with_foreign_routing_recognized_as_tour():
    """Unbekannter Marker + foreign routing + overnight + duty → Tour."""
    matched = [
        _make_day('2025-06-10', activity_type='tour',
                  raw_marker='9999 XYZUNKNOWN',  # made-up marker
                  routing=['FRA', 'BLR'], layover_ort='BLR',
                  overnight_after_day=True, starts_at_homebase=True,
                  has_fl=True, duty_duration_minutes=600),
    ]
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025)
    result = app_module._classify_days_from_normalized_tours(
        tours, year=2025, homebase='FRA')
    klass = result['tage_detail'][0]['klass']
    assert klass == 'Z76', f'Unknown marker + clear tour-evidence sollte Z76, war {klass}'


# ════════════════════════════════════════════════════════════════════════════
# Inland-IATA-Set semantik
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('iata', ['FRA', 'MUC', 'HAM', 'DUS', 'BER', 'STR', 'CGN', 'HAJ', 'LEJ', 'NUE'])
def test_inland_iata_set_includes_german_airports(iata):
    """Inland-IATA-Set enthaelt alle deutschen Flughaefen."""
    assert app_module._is_inland_code(iata), f'{iata} sollte Inland sein'


@pytest.mark.parametrize('iata', ['BLR', 'JFK', 'TLV', 'IST', 'CDG', 'LHR', 'AMS', 'KEF'])
def test_inland_iata_set_excludes_foreign_airports(iata):
    assert not app_module._is_inland_code(iata), f'{iata} sollte foreign sein'


# ════════════════════════════════════════════════════════════════════════════
# Tibor-Hardcoding-Scan
# ════════════════════════════════════════════════════════════════════════════

def test_no_tibor_personalnummer_in_app():
    src = _read_backend()
    # 99102 = Tibor's Personalnummer (siehe `Dienstplanauswertung_99102_2025.pdf`)
    assert '99102' not in src, 'Tibor-Personalnummer 99102 darf NICHT im Code stehen'


def test_no_tibor_hardcoded_logic_in_app():
    """app.py darf kein Tibor-spezifisches CODE-Verhalten haben.

    Tibor-Erwaehnungen in COMMENTS sind erlaubt (Audit-Trail, Begruendungen
    fuer Closeout-Fixes), aber keine if/elif/Dict-Lookups mit 'Tibor' als
    Schluessel oder Special-Case-Logic.
    """
    src = _read_backend()
    import re
    # Verbotene Pattern: if 'Tibor' in ... oder == 'Tibor' oder 99102 etc.
    forbidden_patterns = [
        r"if\s+['\"]tibor['\"]",
        r"==\s*['\"]tibor['\"]",
        r"['\"]tibor['\"]\s*:",  # dict key
        r"['\"]99102['\"]",  # Personalnummer
    ]
    for pat in forbidden_patterns:
        hits = re.findall(pat, src, re.IGNORECASE)
        assert not hits, f'Verbotene Tibor-Logic gefunden: {pat} → {hits[:3]}'
