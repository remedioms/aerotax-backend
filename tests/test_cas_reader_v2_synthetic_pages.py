"""Rel Phase 7 — CAS Reader V2 with Synthetic Pages.

15 Pflicht-Cases per Master:
- Simple FRA-base 3-day foreign tour
- MUC-base with FRA transit
- Cabin X layover day
- Cockpit rest day inside tour
- RES foreign hotel standby
- SB_M after return
- SIM/training with time
- ORTSTAG without time
- Inland same-day >8h
- Inland same-day <8h
- Multi-stop via homebase
- NTF overrides PUB
- Day 2/3 continuation
- Unknown marker with real flight
- Unknown marker without evidence
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


def _v2(datum, base='FRA', marker='', routing=None, layover='', overnight=False,
       has_fl=False, start='', duty=0, prev_day=None, next_day=None):
    return app_module._cas_reader_v2_mock_dispatch(
        day_excerpt='', prev_day=prev_day, next_day=next_day,
        homebase=base, marker_hint=marker,
        routing_hint=routing or [], layover_hint=layover,
        overnight_hint=overnight, has_fl_hint=has_fl,
        start_time_hint=start, duty_hint=duty, datum=datum,
    )


def _validate_schema(out):
    valid, issues = app_module._cas_reader_v2_validate_schema(out)
    return valid, issues


# ════════════════════════════════════════════════════════════════════════════
# Schema-Validation für alle synthetischen Pages
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('case', [
    {'datum': '2025-01-10', 'marker': '12345 PU', 'routing': ['FRA', 'BLR'],
     'layover': 'BLR', 'overnight': True, 'duty': 600, 'start': '06:00', 'base': 'FRA'},
    {'datum': '2025-04-15', 'marker': '23456 PU', 'routing': ['MUC', 'FRA', 'TLV'],
     'layover': 'TLV', 'overnight': True, 'duty': 700, 'start': '07:00', 'base': 'MUC'},
    {'datum': '2025-02-02', 'marker': 'X HKG', 'routing': ['HKG'],
     'layover': 'HKG', 'overnight': True, 'duty': 0, 'base': 'FRA'},
    {'datum': '2025-10-02', 'marker': 'RSV', 'routing': [],
     'layover': 'SIN', 'overnight': True, 'duty': 0, 'base': 'FRA'},
    {'datum': '2025-06-11', 'marker': 'RES', 'routing': [],
     'layover': 'JFK', 'overnight': True, 'duty': 0, 'base': 'FRA'},
    {'datum': '2025-05-02', 'marker': 'SB_M', 'routing': ['FRA'],
     'layover': '', 'overnight': False, 'duty': 450, 'start': '08:00', 'base': 'FRA'},
    {'datum': '2025-09-15', 'marker': 'SIM', 'routing': ['FRA'],
     'layover': '', 'overnight': False, 'duty': 240, 'start': '07:30', 'base': 'FRA'},
    {'datum': '2025-08-20', 'marker': 'ORTSTAG', 'routing': ['FRA'],
     'layover': '', 'overnight': False, 'duty': 0, 'base': 'FRA'},
    {'datum': '2025-07-15', 'marker': '99999 PU', 'routing': ['FRA', 'MUC', 'FRA'],
     'layover': '', 'overnight': False, 'duty': 510, 'start': '05:30', 'base': 'FRA'},
    {'datum': '2025-08-10', 'marker': '99999 PU', 'routing': ['FRA', 'CGN', 'FRA'],
     'layover': '', 'overnight': False, 'duty': 400, 'start': '09:00', 'base': 'FRA'},
    {'datum': '2025-09-01', 'marker': '12345 PU', 'routing': ['MUC', 'FRA', 'LHR'],
     'layover': 'LHR', 'overnight': True, 'duty': 700, 'start': '08:00', 'base': 'FRA'},
    {'datum': '2025-10-05', 'marker': '12345 PU', 'routing': ['FRA', 'CDG'],
     'layover': 'CDG', 'overnight': True, 'duty': 400, 'start': '07:00', 'base': 'FRA'},
    {'datum': '2025-09-26', 'marker': '15688 PU (Day 2)', 'routing': ['KRK', 'FRA', 'IST'],
     'layover': 'IST', 'overnight': True, 'duty': 355, 'base': 'FRA'},
    {'datum': '2025-11-01', 'marker': '## A1', 'routing': ['FRA', 'BLR'],
     'layover': 'BLR', 'overnight': True, 'duty': 600, 'start': '06:00', 'base': 'FRA'},
    {'datum': '2025-11-15', 'marker': '##', 'routing': [],
     'layover': '', 'overnight': False, 'duty': 0, 'base': 'FRA'},
])
def test_reader_v2_schema_valid(case):
    """Alle 15 synthetischen Pages produzieren schema-valides Output."""
    r = _v2(**{k: case.get(k) for k in
             ('datum', 'base', 'marker', 'routing', 'layover', 'overnight',
              'duty', 'start')})
    valid, issues = _validate_schema(r)
    assert valid, f'Schema invalid: {issues}'


# ════════════════════════════════════════════════════════════════════════════
# Anti-Tax-Sanitizer: keine EUR im Output
# ════════════════════════════════════════════════════════════════════════════

def test_reader_v2_never_returns_tax_amount():
    r = _v2('2025-03-15', base='FRA', marker='12345 PU',
            routing=['FRA', 'JFK'], layover='JFK', overnight=True,
            duty=720, start='10:00')
    # Anti-Tax: keine amount/eur/tagesatz im Output
    forbidden = {'amount', 'eur', 'euro', 'tagesatz', 'tax', 'steuer', 'betrag', 'pauschale', 'rate'}
    for k in r.keys():
        ks = str(k).lower()
        assert not any(f in ks for f in forbidden), \
            f'Forbidden tax-field im Reader-V2 Output: {k}'


# ════════════════════════════════════════════════════════════════════════════
# Tour-Context für klar belegte Faelle
# ════════════════════════════════════════════════════════════════════════════

def test_v2_tour_start_recognized():
    """FRA→BLR overnight + duty + start_time → tour_start."""
    r = _v2('2025-01-10', base='FRA', marker='12345 PU',
            routing=['FRA', 'BLR'], layover='BLR', overnight=True,
            duty=600, start='06:00',
            prev_day={'overnight_after_day': False, 'layover_ort': ''})
    assert r['tour_context'] in ('tour_start', 'tour_mid')


def test_v2_homebase_idle_for_res_no_se():
    """RES ohne SE-Stempel + ohne prev-foreign → homebase_idle/standby."""
    r = _v2('2025-04-10', base='FRA', marker='RES',
            routing=['FRA'], duty=450, start='08:00',
            prev_day={'overnight_after_day': False, 'layover_ort': ''})
    assert r['tour_context'] in ('homebase_standby', 'homebase_free', 'unknown')


def test_v2_needs_context_for_unknown_marker_alone():
    """Unknown marker ohne Evidence → needs_context_resolution=True."""
    r = _v2('2025-11-15', base='FRA', marker='##',
            routing=[], duty=0)
    assert r['needs_context_resolution'] is True


# ════════════════════════════════════════════════════════════════════════════
# PII-Sicherheit
# ════════════════════════════════════════════════════════════════════════════

def test_v2_output_no_pii_excerpt():
    """raw_evidence_excerpt darf <= 200 chars sein und nicht PII enthalten."""
    r = _v2('2025-01-10', base='FRA', marker='12345 PU',
            routing=['FRA', 'BLR'], layover='BLR', overnight=True)
    excerpt = r.get('raw_evidence_excerpt') or ''
    assert len(excerpt) <= 200
    # Keine Telefon-/PNR-Pattern
    import re
    assert not re.search(r'\+\d{8,}', excerpt), 'Telefonnummer in excerpt'
    assert not re.search(r'\b\d{6,}\b', excerpt[:50]), 'Lange Nummern early in excerpt'


def test_v2_anti_hallucination_no_phantom_iata():
    """Ohne routing/layover-Hint darf V2 keine IATA halluzinieren."""
    r = _v2('2025-06-15', base='FRA', marker='', routing=[], layover='')
    assert r['layover_ort'] == ''
    assert r['routing'] == []
    assert r['needs_context_resolution'] is True
