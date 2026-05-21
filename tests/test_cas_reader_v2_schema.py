"""R3 — Reader V2 Schema-Validator-Tests."""
import os
import sys
import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import app as app_module

pytestmark = pytest.mark.skipif(
    not hasattr(app_module, '_cas_reader_v2_validate_schema'),
    reason='Reader V2 schema-validator missing',
)


def _valid_v2_output(**overrides):
    base = {
        'datum': '2025-06-15', 'raw_marker': 'X', 'activity_type': 'tour',
        'routing': ['FRA'], 'start_time': '', 'end_time': '',
        'duty_duration_minutes': 0, 'has_fl': False,
        'starts_at_homebase': True, 'ends_at_homebase': True,
        'overnight_after_day': False, 'layover_ort': '',
        'tour_id_candidate': '', 'position_in_tour': '',
        'tour_context': 'homebase_free',
        'continuation_from_prev_day': False,
        'continuation_to_next_day': False,
        'reader_confidence': 0.85,
        'raw_evidence_excerpt': '',
        'needs_context_resolution': False,
        'warnings': [],
    }
    base.update(overrides)
    return base


# ════════════════════════════════════════════════════════════════════════════
# Pflichtfelder
# ════════════════════════════════════════════════════════════════════════════

def test_v2_schema_all_required_fields_present():
    valid, issues = app_module._cas_reader_v2_validate_schema(_valid_v2_output())
    assert valid is True, issues


def test_v2_schema_missing_field_fails():
    output = _valid_v2_output()
    output.pop('reader_confidence')
    valid, issues = app_module._cas_reader_v2_validate_schema(output)
    assert valid is False
    assert any('MISSING_FIELDS' in i for i in issues)


# ════════════════════════════════════════════════════════════════════════════
# tour_context Enum
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('ctx', [
    'tour_start', 'tour_mid', 'tour_end', 'same_day_tour',
    'homebase_free', 'homebase_standby', 'hotel_standby', 'inland_standby',
    'office', 'training', 'positioning', 'unknown',
])
def test_v2_schema_tour_context_enum_valid(ctx):
    output = _valid_v2_output(tour_context=ctx)
    valid, issues = app_module._cas_reader_v2_validate_schema(output)
    assert valid is True, issues


def test_v2_schema_tour_context_enum_strict_rejects_invalid():
    output = _valid_v2_output(tour_context='invented_context')
    valid, issues = app_module._cas_reader_v2_validate_schema(output)
    assert valid is False
    assert any('INVALID_TOUR_CONTEXT' in i for i in issues)


# ════════════════════════════════════════════════════════════════════════════
# No forbidden tax fields
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('forbidden_key', [
    'amount', 'eur', 'euro', 'tagesatz', 'tax',
    'steuer', 'rate', 'betrag', 'pauschale', 'vma',
])
def test_v2_schema_no_forbidden_tax_fields(forbidden_key):
    output = _valid_v2_output()
    output[forbidden_key] = 28.0
    valid, issues = app_module._cas_reader_v2_validate_schema(output)
    assert valid is False
    assert 'FORBIDDEN_TAX_FIELD' in issues


def test_v2_schema_no_forbidden_in_nested():
    output = _valid_v2_output()
    output['warnings'] = [{'tagesatz': 28.0}]
    valid, issues = app_module._cas_reader_v2_validate_schema(output)
    assert valid is False
    assert 'FORBIDDEN_TAX_FIELD' in issues


# ════════════════════════════════════════════════════════════════════════════
# Confidence range
# ════════════════════════════════════════════════════════════════════════════

def test_v2_schema_reader_confidence_range_too_high():
    output = _valid_v2_output(reader_confidence=1.5)
    valid, issues = app_module._cas_reader_v2_validate_schema(output)
    assert valid is False
    assert any('CONFIDENCE_OUT_OF_RANGE' in i for i in issues)


def test_v2_schema_reader_confidence_range_negative():
    output = _valid_v2_output(reader_confidence=-0.1)
    valid, issues = app_module._cas_reader_v2_validate_schema(output)
    assert valid is False


# ════════════════════════════════════════════════════════════════════════════
# warnings is list[str]
# ════════════════════════════════════════════════════════════════════════════

def test_v2_schema_warnings_is_list():
    output = _valid_v2_output(warnings=['DUTY_OVER_FTL', 'MARKER_AMBIGUOUS'])
    valid, issues = app_module._cas_reader_v2_validate_schema(output)
    assert valid is True


def test_v2_schema_warnings_not_list_fails():
    output = _valid_v2_output(warnings='DUTY_OVER_FTL')   # string, not list
    valid, issues = app_module._cas_reader_v2_validate_schema(output)
    assert valid is False
    assert any('WARNINGS_NOT_LIST' in i for i in issues)


# ════════════════════════════════════════════════════════════════════════════
# raw_evidence_excerpt ≤ 200 chars
# ════════════════════════════════════════════════════════════════════════════

def test_v2_schema_raw_evidence_excerpt_max_200():
    output = _valid_v2_output(raw_evidence_excerpt='X' * 201)
    valid, issues = app_module._cas_reader_v2_validate_schema(output)
    assert valid is False
    assert any('EXCERPT_TOO_LONG' in i for i in issues)


def test_v2_schema_raw_evidence_excerpt_200_ok():
    output = _valid_v2_output(raw_evidence_excerpt='X' * 200)
    valid, issues = app_module._cas_reader_v2_validate_schema(output)
    assert valid is True
