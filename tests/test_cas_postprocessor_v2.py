"""Reader-V2 Tests: cas_postprocessor + Tibor BLR Fixture (R3).

R1 — Schema-Felder müssen erzeugt werden
R2 — Healing-Regeln R1-R5
R3 — Tibor BLR-Tour 03-06.01.2025 — konkrete Fixture
R5 — No heuristic explosion / no hardcoding
"""
import os
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE.parent))
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import cas_postprocessor as cp  # noqa: E402
import normalized_tours as nt  # noqa: E402


def _cas(datum, marker='', routing=None, layover_ort='', overnight=False,
         starts_hb=False, ends_hb=False, duty_min=0, has_fl=False,
         activity_type=''):
    return {
        'datum': datum,
        'marker_raw': marker,
        'routing': routing or [],
        'layover_ort': layover_ort,
        'overnight_after_day': overnight,
        'starts_at_homebase': starts_hb,
        'ends_at_homebase': ends_hb,
        'duty_duration_minutes': duty_min,
        'has_fl': has_fl,
        'activity_type': activity_type,
    }


# ════════════════════════════════════════════════════════════════════════════
# R1 — Schema fields
# ════════════════════════════════════════════════════════════════════════════

def test_cas_reader_v2_schema_contains_tour_context_fields():
    """Jeder Tag bekommt die neuen V2-Felder."""
    days = [_cas('2025-01-03', marker='LH', routing=['BLR'])]
    out = cp.normalize_cas_days_v2(days, homebase='FRA')
    assert len(out) == 1
    d = out[0]
    required_fields = (
        'normalized_marker', 'routing_iatas', 'flight_numbers',
        'previous_layover_iata', 'next_layover_iata',
        'tour_context_hint', 'tour_context_confidence',
        'is_tour_continuation', 'is_tour_return', 'is_tour_departure',
        'return_from_layover', 'origin_iata', 'destination_iata',
        'reader_should_not_classify_as_free_reason',
        'neighbor_evidence', 'healed_by', 'warnings',
    )
    for f in required_fields:
        assert f in d, f'Field {f} missing in V2 output'


# ════════════════════════════════════════════════════════════════════════════
# R2 Regel 1: X-Return-Healing
# ════════════════════════════════════════════════════════════════════════════

def test_cas_reader_v2_x_marker_can_be_return_day():
    """X-Marker am Tag nach Auslands-Layover + routing=FRA → Heimkehr."""
    days = [
        _cas('2025-01-05', marker='X', routing=['HKG'], layover_ort='HKG',
             overnight=True),
        _cas('2025-01-06', marker='X', routing=['FRA'], activity_type='frei',
             ends_hb=True),
    ]
    out = cp.normalize_cas_days_v2(days, homebase='FRA')
    day_06 = out[1]
    assert day_06['is_tour_return'] is True
    assert day_06['return_from_layover'] is True
    assert day_06['origin_iata'] == 'HKG'
    assert day_06['destination_iata'] == 'FRA'
    assert 'rule1_x_return_healing' in day_06['healed_by']


def test_normalize_x_return_day_blr_2025_01_06():
    """Tibor BLR-Heimkehr-Tag heilt korrekt."""
    days = [
        # 05.01: BLR-Layover-Vortag (overnight, foreign layover)
        _cas('2025-01-05', marker='755', routing=['BLR'], layover_ort='BLR',
             overnight=True, duty_min=300),
        # 06.01: X-Marker (Sonnet hat es als frei gelesen), routing=FRA
        _cas('2025-01-06', marker='X', routing=['FRA'], activity_type='frei',
             ends_hb=True),
    ]
    out = cp.normalize_cas_days_v2(days, homebase='FRA')
    day_06 = out[1]
    # MUST heal as return-day
    assert day_06['is_tour_return'] is True
    assert day_06['return_from_layover'] is True
    assert day_06['origin_iata'] == 'BLR'


# ════════════════════════════════════════════════════════════════════════════
# R2 Regel 2: Empty-Marker-Continuation
# ════════════════════════════════════════════════════════════════════════════

def test_cas_reader_v2_empty_marker_between_tour_days_not_free():
    """Leerer Marker zwischen zwei Tour-Tagen → Tour-Continuation."""
    days = [
        _cas('2025-01-03', marker='31591', routing=['BLR'], layover_ort='BLR',
             overnight=True, duty_min=600, has_fl=True),
        _cas('2025-01-04', marker='', routing=['BLR'], activity_type='frei',
             overnight=True),  # leerer Marker mitten in Tour
        _cas('2025-01-05', marker='755', routing=['BLR'], layover_ort='BLR',
             overnight=True, duty_min=300, has_fl=True),
    ]
    out = cp.normalize_cas_days_v2(days, homebase='FRA')
    day_04 = out[1]
    assert day_04['is_tour_continuation'] is True
    assert day_04['activity_type'] == 'tour_continuation'
    assert 'rule2_empty_marker_continuation' in day_04['healed_by']


# ════════════════════════════════════════════════════════════════════════════
# R2 Regel 3: ends_hb-Correction
# ════════════════════════════════════════════════════════════════════════════

def test_cas_reader_v2_ends_hb_not_tour_end_if_next_day_continues():
    """Sonnet hat ends_hb=True bei Tour-Mid-Tag — wird korrigiert."""
    days = [
        _cas('2025-01-05', marker='LH', routing=['BLR'], layover_ort='BLR',
             overnight=True, starts_hb=True, ends_hb=True, duty_min=600,
             has_fl=True),  # ends_hb fälschlich True
        _cas('2025-01-06', marker='X', routing=['BLR'], layover_ort='BLR',
             overnight=True),  # Folgetag clearly tour-continuation
    ]
    out = cp.normalize_cas_days_v2(days, homebase='FRA')
    day_05 = out[0]
    # Aktive Korrektur: ends_at_homebase=False
    assert day_05.get('ends_at_homebase') is False
    assert day_05.get('ends_at_homebase_conflict') is True
    assert day_05.get('ends_at_homebase_original') is True
    assert 'rule3_ends_hb_correction' in day_05['healed_by']


# ════════════════════════════════════════════════════════════════════════════
# R2 Regel 4: Flight-numbers vs IATAs
# ════════════════════════════════════════════════════════════════════════════

def test_cas_reader_v2_routing_fra_on_return_day_means_home_return():
    """routing=['FRA'] auf Return-Day → Heimkehr-Indikator, kein Inland-Trip."""
    days = [
        _cas('2025-01-05', marker='LH', routing=['BLR'], layover_ort='BLR',
             overnight=True, duty_min=600),
        _cas('2025-01-06', marker='X', routing=['FRA'], activity_type='frei',
             ends_hb=True),
    ]
    out = cp.normalize_cas_days_v2(days, homebase='FRA')
    day_06 = out[1]
    # FRA in routing wird als HB-Return interpretiert → Tour-Return
    assert day_06['is_tour_return'] is True
    assert day_06['destination_iata'] == 'FRA'


def test_normalize_flight_numbers_not_iata():
    """Flugnummern LH756 / 31591 werden als flight_numbers, nicht als IATAs."""
    days = [_cas('2025-01-03', routing=['LH756', '31591', 'BLR'])]
    out = cp.normalize_cas_days_v2(days, homebase='FRA')
    d = out[0]
    assert 'BLR' in d['routing_iatas']
    assert 'LH756' in d['flight_numbers']
    assert '31591' in d['flight_numbers']
    assert 'LH756' not in d['routing_iatas']


# ════════════════════════════════════════════════════════════════════════════
# R2 Regel 5: Return-from-layover
# ════════════════════════════════════════════════════════════════════════════

def test_normalize_return_from_previous_layover():
    """Folgetag nach Auslands-Layover, der zur HB zurückkehrt → marked."""
    days = [
        _cas('2025-01-05', marker='X', routing=['HKG'], layover_ort='HKG',
             overnight=True),
        _cas('2025-01-06', marker='LH', routing=['HKG', 'FRA'], ends_hb=True,
             duty_min=600, has_fl=True),
    ]
    out = cp.normalize_cas_days_v2(days, homebase='FRA')
    day_06 = out[1]
    assert day_06['return_from_layover'] is True
    assert day_06['origin_iata'] == 'HKG'
    assert day_06['destination_iata'] == 'FRA'


# ════════════════════════════════════════════════════════════════════════════
# R2 Negativ-Tests (kein over-healing)
# ════════════════════════════════════════════════════════════════════════════

def test_normalize_free_day_with_no_neighbor_context_stays_free():
    """Frei-Tag ohne Nachbar-Tour-Kontext bleibt Frei."""
    days = [
        _cas('2025-01-01', marker='U', activity_type='urlaub'),
        _cas('2025-01-02', marker='U', activity_type='urlaub'),
    ]
    out = cp.normalize_cas_days_v2(days, homebase='FRA')
    for d in out:
        assert not d['is_tour_return']
        assert not d['is_tour_continuation']
        assert d['activity_type'] == 'urlaub'


def test_r0_passive_marker_unknown_heals_to_free():
    """R0 (2026-05-27): Passive LH-Marker (LMN_HT1, ORTSTAG, FRS, OF, OFF,
    LMN_AS, LMN_CR) ohne duty/has_fl/routing werden zu activity_type='free'
    geheilt — damit landen sie nicht als 'unbekannte Kennung' im Chat."""
    days = [
        _cas('2025-01-28', marker='OFF', activity_type='free'),
        _cas('2025-01-29', marker='LMN_HT1', activity_type='unknown'),
        _cas('2025-01-30', marker='OFF', activity_type='free'),
    ]
    out = cp.normalize_cas_days_v2(days, homebase='FRA')
    target = [d for d in out if d['datum'] == '2025-01-29'][0]
    assert target['activity_type'] == 'free'
    assert 'R0_passive_marker_to_free' in target.get('healed_by', [])


def test_r0_passive_marker_with_duty_does_not_heal():
    """Wenn passiver Marker doch duty/Flug-Signale hat (theoretisch), wird
    NICHT zu free geheilt — Reader hat dann etwas Aktives gelesen, das
    respektiert wird."""
    days = [
        _cas('2025-01-29', marker='LMN_HT1', activity_type='unknown',
             duty_min=600),
    ]
    out = cp.normalize_cas_days_v2(days, homebase='FRA')
    assert out[0]['activity_type'] == 'unknown'
    assert 'R0_passive_marker_to_free' not in out[0].get('healed_by', [])


def test_r0_already_free_passive_marker_skipped():
    """Passive Marker die schon free sind, werden nicht doppelt-geheilt."""
    days = [
        _cas('2025-01-29', marker='LMN_HT1', activity_type='free'),
    ]
    out = cp.normalize_cas_days_v2(days, homebase='FRA')
    assert out[0]['activity_type'] == 'free'
    assert 'R0_passive_marker_to_free' not in out[0].get('healed_by', [])


def test_normalize_home_standby_stays_home_standby():
    """Home-Standby bleibt Home-Standby (keine Tour-Heilung)."""
    days = [
        _cas('2025-02-01', marker='SB_S', duty_min=480),
        _cas('2025-02-02', marker='SB_S', duty_min=480),
    ]
    out = cp.normalize_cas_days_v2(days, homebase='FRA')
    for d in out:
        assert not d['is_tour_return']
        assert not d['is_tour_continuation']
        assert d.get('healed_by') == []


def test_cas_reader_v2_eh_seccrm_not_auto_free():
    """EH SECCRM ist Training-Marker, darf nicht automatisch frei werden."""
    days = [
        _cas('2025-03-18', marker='EH SECCRM', routing=['FRA'],
             duty_min=300, activity_type='office'),
    ]
    out = cp.normalize_cas_days_v2(days, homebase='FRA')
    d = out[0]
    # activity_type bleibt 'office' — kein Healing zu frei
    assert d['activity_type'] != 'frei'


# ════════════════════════════════════════════════════════════════════════════
# R3 — Tibor BLR Tour Fixture (03-06.01.2025)
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.xfail(reason='Reader liefert overnight=False/layover_ort=leer für '
                          'Mid-Tour-Tage — Rule1 braucht prev.overnight=True. '
                          'Lösung: Sonnet-Prompt-V2 muss overnight korrekt setzen.')
def test_tibor_blr_tour_reader_v2_builds_single_tour():
    """BLR-Tour 03-06.01: idealfall 4 Tage zu EINER Tour."""
    cas = [
        _cas('2025-01-03', marker='31591', routing=['FRA'],
             starts_hb=True, overnight=True, duty_min=784, has_fl=False),
        _cas('2025-01-04', marker='X', routing=['BLR'], overnight=False),
        _cas('2025-01-05', marker='755', routing=['BLR'],
             starts_hb=True, ends_hb=True, overnight=False, duty_min=561),
        _cas('2025-01-06', marker='X', routing=['FRA'], activity_type='frei',
             ends_hb=True),
    ]
    out = cp.normalize_cas_days_v2(cas, homebase='FRA')
    day_06 = out[3]
    assert day_06['is_tour_return'] is True


def test_tibor_blr_2025_01_06_not_free():
    """2025-01-06 BLR-Heimkehr darf nicht Frei sein."""
    days = [
        _cas('2025-01-05', marker='755', routing=['BLR'], layover_ort='BLR',
             overnight=True, duty_min=300, has_fl=True),
        _cas('2025-01-06', marker='X', routing=['FRA'], activity_type='frei'),
    ]
    out = cp.normalize_cas_days_v2(days, homebase='FRA')
    day_06 = out[1]
    assert day_06['is_tour_return'] is True
    # activity_type wurde zu 'tour_return' geheilt
    assert day_06['activity_type'] == 'tour_return'


def test_tibor_blr_return_day_has_origin_blr_destination_fra():
    """Return-Day-Origin = letzter Foreign-Layover, Destination = HB."""
    days = [
        _cas('2025-01-05', marker='LH', routing=['BLR'], layover_ort='BLR',
             overnight=True, duty_min=600, has_fl=True),
        _cas('2025-01-06', marker='X', routing=['FRA'], activity_type='frei'),
    ]
    out = cp.normalize_cas_days_v2(days, homebase='FRA')
    day_06 = out[1]
    assert day_06['origin_iata'] == 'BLR'
    assert day_06['destination_iata'] == 'FRA'


def test_tibor_blr_no_phantom_days():
    """Kein Phantom-Tag wird erzeugt — Anzahl Output = Anzahl Input."""
    cas = [_cas('2025-01-0' + str(i), marker='X') for i in range(1, 8)]
    out = cp.normalize_cas_days_v2(cas, homebase='FRA')
    assert len(out) == len(cas)


def test_tibor_blr_normalized_tours_z76_reasonable():
    """Nach Postprocessor + Builder: BLR-Tour ergibt Z76 > 0."""
    BMF = {
        'BLR': {'voll_24h': 42.0, 'an_abreise': 28.0, 'country': 'Indien-Bangalore'},
    }
    IATA = {'BLR': 'Indien-Bangalore'}
    cas = [
        _cas('2025-01-03', marker='31591', routing=['BLR'], layover_ort='BLR',
             starts_hb=True, overnight=True, duty_min=600, has_fl=True),
        _cas('2025-01-04', marker='X', routing=['BLR'], layover_ort='BLR',
             overnight=True),
        _cas('2025-01-05', marker='X', routing=['BLR'], layover_ort='BLR',
             overnight=True),
        _cas('2025-01-06', marker='X', routing=['FRA'], activity_type='frei',
             ends_hb=True),
    ]
    tours = nt.build_normalized_tours(cas, [], 2025, homebase='FRA')
    result = nt.calculate_allowances_from_normalized_tours(
        tours, BMF, iata_to_bmf=IATA, homebase='FRA',
    )
    assert result.z76_eur > 0, 'BLR-Tour muss Z76 erzeugen'


# ════════════════════════════════════════════════════════════════════════════
# R5 — No heuristic explosion / no hardcoding
# ════════════════════════════════════════════════════════════════════════════

def test_no_tibor_date_hardcoding_in_postprocessor():
    """cas_postprocessor.py darf KEINE Tibor-spezifischen Datumsangaben enthalten."""
    src = open(_HERE.parent / 'cas_postprocessor.py', encoding='utf-8').read()
    import re
    # Kein '2025-XX-XX' Date-Literal im executable Code (Docstrings/Comments OK)
    import ast
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            # ISO-Date-Literal?
            if re.match(r'^\d{4}-\d{2}-\d{2}$', node.value):
                pytest.fail(f'Date-Literal {node.value!r} in cas_postprocessor.py')


def test_no_followme_amount_hardcoding_in_postprocessor():
    """KEINE FollowMe-Beträge wie 4794, 5046 etc."""
    src = open(_HERE.parent / 'cas_postprocessor.py', encoding='utf-8').read()
    forbidden_amounts = ['4794', '5046', '4276', '4794.0']
    for amount in forbidden_amounts:
        # In code-Strings (not comments)
        import re
        # Strip comments
        code_lines = []
        for line in src.split('\n'):
            line_no_comment = line.split('#')[0]
            code_lines.append(line_no_comment)
        code = '\n'.join(code_lines)
        assert amount not in code, f'FollowMe-Betrag {amount} in Code'


def test_no_se_only_tour_created_by_reader_v2():
    """Reader-V2 darf KEINE Tour aus SE-only erzeugen."""
    # SE allein → Postprocessor erzeugt keine neue Tour-Tage
    cas_days = []  # leer
    se_rows = [{'datum': '2025-05-21', 'stfrei_ort': 'BLR',
                'stfrei_betrag': 42.0, 'storno': False}]
    out = cp.normalize_cas_days_v2(cas_days, homebase='FRA', se_rows=se_rows)
    assert out == []


def test_reader_v2_healing_rules_are_generic():
    """Healing rules dürfen keine User-spezifischen Hardcodings haben."""
    src = open(_HERE.parent / 'cas_postprocessor.py', encoding='utf-8').read()
    forbidden = ['tibor', 'TIBOR', 'miguel', 'MIGUEL', 'Quaas', '99102']
    for keyword in forbidden:
        assert keyword not in src, f'User-spezifisch {keyword!r} in Code'


def test_normalized_tours_branch_count_not_exploding():
    """Branch-Count im allowance-Calculator bleibt überschaubar."""
    src = open(_HERE.parent / 'normalized_tours.py', encoding='utf-8').read()
    # Zähle if/elif-Statements in calculate_allowances function
    import ast
    tree = ast.parse(src)
    target_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) \
                and node.name == 'calculate_allowances_from_normalized_tours':
            target_fn = node
            break
    if target_fn is None:
        pytest.skip('Function not found')
    if_count = sum(1 for n in ast.walk(target_fn) if isinstance(n, ast.If))
    # 30 ist tolerant — sollte unter 50 bleiben
    assert if_count < 50, \
        f'allowance-calc hat {if_count} if/elif — Heuristik-Explosion'
