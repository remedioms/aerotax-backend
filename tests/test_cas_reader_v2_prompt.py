"""R3 — CAS Reader V2 Prompt-Tests (Mock-only, kein Live-Call).

Spec: docs/CAS_READER_PROMPT_V2_SPEC.md
"""
import os
import sys
import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import app as app_module

pytestmark = pytest.mark.skipif(
    not hasattr(app_module, '_cas_reader_v2_build_prompt')
    or not hasattr(app_module, '_cas_reader_v2_mock_dispatch'),
    reason='Reader V2 noch nicht implementiert',
)


def _mock(marker='', routing=None, layover='', overnight=False,
          has_fl=False, start='', duty=0, prev_day=None, next_day=None,
          datum='2025-06-15', excerpt=''):
    return app_module._cas_reader_v2_mock_dispatch(
        day_excerpt=excerpt, prev_day=prev_day, next_day=next_day,
        homebase='FRA', marker_hint=marker, routing_hint=routing or [],
        layover_hint=layover, overnight_hint=overnight, has_fl_hint=has_fl,
        start_time_hint=start, duty_hint=duty, datum=datum,
    )


# ════════════════════════════════════════════════════════════════════════════
# 1. Prompt enthält Crew-Code-Vokabular (PU=Purser, NICHT Pula)
# ════════════════════════════════════════════════════════════════════════════

def test_v2_prompt_contains_crew_code_vocabulary():
    p = app_module._cas_reader_v2_build_prompt(
        day_excerpt='12345 PU Tag 2', homebase='FRA',
    )
    assert 'PU' in p
    assert 'Purser' in p
    assert 'NICHT Pula' in p
    assert 'P1' in p
    assert 'Roster' in p or 'Sequenz' in p


# ════════════════════════════════════════════════════════════════════════════
# 2. PU allein wird NICHT als Pula interpretiert
# ════════════════════════════════════════════════════════════════════════════

def test_v2_prompt_pu_not_pula_anti_naive():
    r = _mock(marker='PU', routing=['FRA'], layover='')
    # Reader-V2-Mock setzt KEIN tour_context='positioning' weil Marker PU
    # alleine ist Crew-Position-Code. Routing zeigt nur FRA → kein Tour-Tag.
    assert 'PUY' not in str(r.get('routing', []))
    assert r.get('layover_ort') != 'PUY'
    # Mock erkennt: kein Tour-Kontext, kein PU → frei oder ambig
    # (kein Tour-Klassifikator-Output mit Pula)


# ════════════════════════════════════════════════════════════════════════════
# 3. X innerhalb foreign-Tour → tour_mid
# ════════════════════════════════════════════════════════════════════════════

def test_v2_prompt_x_inside_tour_not_frei():
    r = _mock(
        marker='X', routing=[], layover='BLR', overnight=True,
        prev_day={'routing': ['FRA','BLR'], 'layover_ort': 'BLR',
                  'overnight_after_day': True},
    )
    assert r['tour_context'] == 'tour_mid'
    assert r['activity_type'] == 'tour'
    assert r['continuation_from_prev_day'] is True


def test_v2_prompt_x_zuhause_ohne_tour_frei():
    r = _mock(
        marker='X', routing=['FRA'], layover='', overnight=False,
        prev_day={'routing': ['FRA'], 'layover_ort': '',
                  'overnight_after_day': False},
    )
    assert r['tour_context'] == 'homebase_free'
    assert r['activity_type'] == 'frei'


# ════════════════════════════════════════════════════════════════════════════
# 4. OFF im foreign Layover → tour_mid
# ════════════════════════════════════════════════════════════════════════════

def test_v2_prompt_off_inside_foreign_layover_not_frei():
    r = _mock(
        marker='OFF', routing=[], layover='', overnight=False,
        prev_day={'routing': ['FRA','JFK'], 'layover_ort': 'JFK',
                  'overnight_after_day': True},
    )
    assert r['tour_context'] in ('tour_mid', 'hotel_standby')
    assert r['continuation_from_prev_day'] is True


# ════════════════════════════════════════════════════════════════════════════
# 5. RES nach foreign-overnight → hotel_standby
# ════════════════════════════════════════════════════════════════════════════

def test_v2_prompt_res_hotel_not_home_standby():
    r = _mock(
        marker='RES', routing=[], layover='',
        prev_day={'routing': ['FRA','ICN'], 'layover_ort': 'ICN',
                  'overnight_after_day': True},
    )
    assert r['tour_context'] == 'hotel_standby'
    assert r['continuation_from_prev_day'] is True


# ════════════════════════════════════════════════════════════════════════════
# 6. RES nach Inland-Übernachtung → inland_standby
# ════════════════════════════════════════════════════════════════════════════

def test_v2_prompt_res_inland_overnight_z73_hint():
    r = _mock(
        marker='RES', routing=[],
        prev_day={'routing': ['FRA','MUC'], 'layover_ort': 'MUC',
                  'overnight_after_day': True},
    )
    assert r['tour_context'] == 'inland_standby'


def test_v2_prompt_res_zuhause_homebase_standby():
    r = _mock(marker='RES', routing=['FRA'],
              prev_day={'routing':['FRA'], 'overnight_after_day': False})
    assert r['tour_context'] == 'homebase_standby'


# ════════════════════════════════════════════════════════════════════════════
# 7. Sequence-ID nicht als Flight-Number
# ════════════════════════════════════════════════════════════════════════════

def test_v2_prompt_sequence_id_not_flight_number():
    r = _mock(marker='12345 P1', routing=['FRA','BLR'], layover='BLR',
              overnight=True)
    assert r['tour_id_candidate'] == '12345'
    # KEIN Flight-Number-Feld im Output
    assert 'flight_number' not in r
    # routing zeigt foreign-Ziel BLR
    assert 'BLR' in r['routing']


# ════════════════════════════════════════════════════════════════════════════
# 8. Day-Suffix in position_in_tour
# ════════════════════════════════════════════════════════════════════════════

def test_v2_prompt_day_suffix_position_in_tour():
    r = _mock(marker='12345 P1 / Tag 3', routing=['JFK','FRA'],
              prev_day={'overnight_after_day': True, 'layover_ort': 'JFK'})
    assert r['position_in_tour'] == '3'
    assert r['tour_id_candidate'] == '12345'


def test_v2_prompt_day_suffix_day_n():
    r = _mock(marker='57783 P1 Day 2', routing=[],
              prev_day={'overnight_after_day': True, 'layover_ort': 'JFK'})
    assert r['position_in_tour'] == '2'


# ════════════════════════════════════════════════════════════════════════════
# 9. duty > FTL → warning + confidence ≤ 0.70
# ════════════════════════════════════════════════════════════════════════════

def test_v2_prompt_duty_over_ftl_warning():
    r = _mock(marker='12345 PU', routing=['FRA','GOT'], layover='GOT',
              overnight=True, duty=1450)
    assert 'DUTY_OVER_FTL' in r['warnings']
    assert r['reader_confidence'] <= 0.70


# ════════════════════════════════════════════════════════════════════════════
# 10. raw_evidence_excerpt ≤ 200 + PII-safe
# ════════════════════════════════════════════════════════════════════════════

def test_v2_prompt_raw_evidence_excerpt_short():
    long_excerpt = 'X' * 500
    r = _mock(marker='OFF', excerpt=long_excerpt)
    assert len(r['raw_evidence_excerpt']) <= 200


# ════════════════════════════════════════════════════════════════════════════
# 11. IATA nur aus routing/SE/layover, NICHT aus Marker-Suffix
# ════════════════════════════════════════════════════════════════════════════

def test_v2_prompt_iata_only_from_routing_not_marker_suffix():
    # Marker '12345 PU' — PU ist Crew-Position, kein IATA.
    # Routing leer → kein IATA-Extraktion.
    r = _mock(marker='12345 PU', routing=[], layover='')
    assert r['routing'] == []
    assert r['layover_ort'] == ''
    # tour_id_candidate hat die 12345
    assert r['tour_id_candidate'] == '12345'


# ════════════════════════════════════════════════════════════════════════════
# 12. ORTSTAG → office, NICHT Z76
# ════════════════════════════════════════════════════════════════════════════

def test_v2_prompt_ortstag_office_not_z76():
    r = _mock(marker='ORTSTAG', routing=['FRA'])
    assert r['tour_context'] == 'office'
    assert r['activity_type'] == 'office'
    # KEINE Tax-Felder
    for k in ('amount','eur','euro','tagesatz','tax','steuer','betrag'):
        assert k not in r


# ════════════════════════════════════════════════════════════════════════════
# 13. EM mit Zeit → training
# ════════════════════════════════════════════════════════════════════════════

def test_v2_prompt_em_training_with_time():
    r = _mock(marker='EM /1', routing=['FRA'], start='08:00', duty=480)
    assert r['tour_context'] == 'training'
    assert r['activity_type'] == 'training'
    assert r['reader_confidence'] >= 0.85


# ════════════════════════════════════════════════════════════════════════════
# 14. Unknown marker → warning + low confidence
# ════════════════════════════════════════════════════════════════════════════

def test_v2_prompt_marker_ambiguous_warning():
    r = _mock(marker='ZZZ_UNKNOWN', routing=['FRA'])
    assert 'MARKER_AMBIGUOUS' in r['warnings']
    assert r['reader_confidence'] < 0.70


def test_v2_prompt_empty_marker_and_routing_needs_context():
    r = _mock(marker='', routing=[])
    assert r['needs_context_resolution'] is True
    assert 'CONTEXT_INSUFFICIENT' in r['warnings']
    assert r['reader_confidence'] <= 0.50


# ════════════════════════════════════════════════════════════════════════════
# 15. Bangalore-Tour 4-Day pattern
# ════════════════════════════════════════════════════════════════════════════

def test_v2_bangalore_tour_start():
    r = _mock(marker='31591 P1', routing=['FRA','BLR'], layover='BLR',
              overnight=True, has_fl=True, duty=785, start='10:55')
    assert r['tour_context'] == 'tour_start'
    assert r['activity_type'] == 'tour'


def test_v2_bangalore_x_mid():
    r = _mock(marker='X', routing=[], layover='BLR', overnight=True,
              prev_day={'routing':['FRA','BLR'], 'layover_ort':'BLR',
                        'overnight_after_day': True})
    assert r['tour_context'] == 'tour_mid'
