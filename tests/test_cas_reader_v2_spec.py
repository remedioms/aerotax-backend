"""R11 — Static-Tests fuer cas_reader_v2_spec (Prompt+Schema+Validator).

KEIN Live-Run. KEIN Sonnet-Call. Nur String-/Schema-Checks gegen die V2-Spec.
"""
import json
import os
import sys
import re
import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from cas_reader_v2_spec import (  # noqa: E402
    V2_PROMPT_INSTRUCTIONS,
    V2_REQUIRED_FIELDS_PER_DAY,
    V2_OPTIONAL_FIELDS_PER_DAY,
    V2_TOUR_CONTEXT_HINTS,
    V2_ACTIVITY_TYPES,
    get_v2_json_schema,
    validate_cas_reader_v2_day,
    validate_cas_reader_v2_response,
    is_v2_enabled,
)


# ----------------------------------------------------------------------------
# R11.A Prompt-Mention-Tests
# ----------------------------------------------------------------------------

def test_cas_reader_prompt_mentions_x_return_day():
    assert 'REGEL 1' in V2_PROMPT_INSTRUCTIONS
    assert 'tour_return' in V2_PROMPT_INSTRUCTIONS
    assert 'return_from_layover' in V2_PROMPT_INSTRUCTIONS


def test_cas_reader_prompt_mentions_empty_marker_continuation():
    assert 'REGEL 2' in V2_PROMPT_INSTRUCTIONS
    assert 'tour_continuation' in V2_PROMPT_INSTRUCTIONS


def test_cas_reader_prompt_mentions_ends_hb_conflict():
    assert 'REGEL 3' in V2_PROMPT_INSTRUCTIONS
    assert 'ends_at_homebase' in V2_PROMPT_INSTRUCTIONS


def test_cas_reader_prompt_mentions_briefing_time_extraction():
    assert 'REGEL 4' in V2_PROMPT_INSTRUCTIONS
    assert 'briefing_time' in V2_PROMPT_INSTRUCTIONS
    assert 'departure_time' in V2_PROMPT_INSTRUCTIONS


def test_cas_reader_prompt_mentions_destination_propagation():
    assert 'REGEL 5' in V2_PROMPT_INSTRUCTIONS
    assert 'destination_iata' in V2_PROMPT_INSTRUCTIONS


def test_cas_reader_prompt_mentions_overnight_handling():
    assert 'REGEL 6' in V2_PROMPT_INSTRUCTIONS
    assert 'overnight_after_day' in V2_PROMPT_INSTRUCTIONS


def test_cas_reader_prompt_mentions_routing_vs_flightnumbers():
    assert 'REGEL 7' in V2_PROMPT_INSTRUCTIONS
    assert 'routing_iatas' in V2_PROMPT_INSTRUCTIONS
    assert 'flight_numbers' in V2_PROMPT_INSTRUCTIONS


def test_cas_reader_prompt_mentions_tour_context_hint():
    assert 'REGEL 8' in V2_PROMPT_INSTRUCTIONS
    assert 'tour_context_hint' in V2_PROMPT_INSTRUCTIONS


def test_cas_reader_prompt_mentions_free_default_avoidance():
    assert 'REGEL 9' in V2_PROMPT_INSTRUCTIONS
    assert 'unknown_tour_context' in V2_PROMPT_INSTRUCTIONS


def test_cas_reader_prompt_mentions_reader_should_not_classify_as_free_reason():
    assert 'REGEL 10' in V2_PROMPT_INSTRUCTIONS
    assert 'reader_should_not_classify_as_free_reason' in V2_PROMPT_INSTRUCTIONS
    assert 'neighbor_evidence' in V2_PROMPT_INSTRUCTIONS


# ----------------------------------------------------------------------------
# R11.B No-Hardcoding-Audit
# ----------------------------------------------------------------------------

def test_prompt_contains_no_tibor_hardcoding():
    lower = V2_PROMPT_INSTRUCTIONS.lower()
    assert 'tibor' not in lower
    assert '99102' not in lower
    assert 'followme' not in lower
    assert 'follow-me' not in lower
    assert 'follow me' not in lower


def test_prompt_contains_no_date_literals():
    matches = re.findall(r'\b20\d{2}-\d{2}-\d{2}\b', V2_PROMPT_INSTRUCTIONS)
    assert matches == [], f'unexpected date literal(s): {matches}'


def test_prompt_contains_no_eur_amount_hardcoding():
    hits = re.findall(r'\d{2,5}\s?€', V2_PROMPT_INSTRUCTIONS)
    assert hits == [], f'unexpected € amount: {hits}'


# ----------------------------------------------------------------------------
# R11.C Schema-Tests
# ----------------------------------------------------------------------------

def test_v2_required_fields_complete():
    schema = get_v2_json_schema()
    day_required = schema['properties']['days']['items']['required']
    for f in V2_REQUIRED_FIELDS_PER_DAY:
        assert f in day_required, f'required field {f} missing from schema'


def test_v2_schema_serializable():
    schema = get_v2_json_schema()
    j = json.dumps(schema)
    assert 'days' in j
    assert 'tour_context_hint' in j


def test_v2_tour_context_hints_match_enum():
    schema = get_v2_json_schema()
    enum = schema['properties']['days']['items']['properties'][
        'tour_context_hint']['enum']
    assert set(enum) == set(V2_TOUR_CONTEXT_HINTS)


def test_v2_activity_types_match_enum():
    schema = get_v2_json_schema()
    enum = schema['properties']['days']['items']['properties'][
        'activity_type']['enum']
    assert set(enum) == set(V2_ACTIVITY_TYPES)


def test_v2_unknown_tour_context_in_activity_types():
    assert 'unknown_tour_context' in V2_ACTIVITY_TYPES


# ----------------------------------------------------------------------------
# R11.D Validator-Tests
# ----------------------------------------------------------------------------

def _minimal_valid_day():
    return {
        'datum': '2025-03-15',
        'raw_marker': '755',
        'normalized_marker': '755',
        'activity_type': 'tour_return',
        'starts_at_homebase': False,
        'ends_at_homebase': True,
        'overnight_after_day': False,
        'routing_iatas': ['BLR', 'FRA'],
        'flight_numbers': ['LH755'],
        'origin_iata': 'BLR',
        'destination_iata': 'FRA',
        'layover_iata': 'BLR',
        'tour_context_hint': 'return',
        'tour_context_confidence': 'high',
        'is_tour_departure': False,
        'is_tour_continuation': False,
        'is_tour_return': True,
        'return_from_layover': True,
        'has_flight_segment': True,
        'confidence': 'high',
        'warnings': [],
    }


def test_validator_passes_minimal_valid_day():
    v = validate_cas_reader_v2_day(_minimal_valid_day())
    assert v['errors'] == []


def test_validator_flags_missing_required_field():
    day = _minimal_valid_day()
    del day['routing_iatas']
    v = validate_cas_reader_v2_day(day)
    assert any('routing_iatas' in e for e in v['errors'])


def test_validator_flags_flight_number_in_routing_iatas():
    day = _minimal_valid_day()
    day['routing_iatas'] = ['LH756', 'BLR']
    v = validate_cas_reader_v2_day(day)
    assert any('flight_number_in_routing_iatas' in e for e in v['errors'])


def test_validator_flags_non_iata_token_in_routing_iatas():
    day = _minimal_valid_day()
    day['routing_iatas'] = ['31591', 'BLR']
    v = validate_cas_reader_v2_day(day)
    assert any('routing_iatas_invalid_iata' in e for e in v['errors'])


def test_validator_warns_tour_return_without_origin():
    day = _minimal_valid_day()
    day['origin_iata'] = None
    v = validate_cas_reader_v2_day(day)
    assert any('tour_return_without_origin' in w for w in v['warnings'])


def test_validator_warns_overnight_without_layover():
    day = _minimal_valid_day()
    day['overnight_after_day'] = True
    day['layover_iata'] = None
    v = validate_cas_reader_v2_day(day)
    assert any('overnight_true_without_layover_iata' in w for w in v['warnings'])


def test_validator_errors_unknown_tour_context_without_warning():
    day = _minimal_valid_day()
    day['activity_type'] = 'unknown_tour_context'
    day['warnings'] = []
    v = validate_cas_reader_v2_day(day)
    assert any('unknown_tour_context_without_warning' in e for e in v['errors'])


def test_validator_response_aggregates_per_day():
    resp = {'days': [_minimal_valid_day(), _minimal_valid_day()]}
    v = validate_cas_reader_v2_response(resp)
    assert v['errors'] == []


def test_validator_response_rejects_missing_days():
    v = validate_cas_reader_v2_response({})
    assert any('response_missing_days' in e for e in v['errors'])


# ----------------------------------------------------------------------------
# R11.E Feature-Flag
# ----------------------------------------------------------------------------

def test_v2_flag_on_by_default(monkeypatch):
    """R24 (2026-05-27): Default-ON nach Tibor-Validation. Rollback via
    explizitem AEROTAX_CAS_READER_V2=0."""
    monkeypatch.delenv('AEROTAX_CAS_READER_V2', raising=False)
    assert is_v2_enabled() is True


def test_v2_flag_explicit_zero_disables(monkeypatch):
    monkeypatch.setenv('AEROTAX_CAS_READER_V2', '0')
    assert is_v2_enabled() is False


def test_v2_flag_respects_env(monkeypatch):
    monkeypatch.setenv('AEROTAX_CAS_READER_V2', '1')
    assert is_v2_enabled() is True
    monkeypatch.setenv('AEROTAX_CAS_READER_V2', '0')
    assert is_v2_enabled() is False


# ----------------------------------------------------------------------------
# R11.F BLR Golden Reader-Fixture
# ----------------------------------------------------------------------------

def _load_blr_fixture():
    fixture_path = os.path.join(
        THIS_DIR, 'fixtures', 'cas_reader_v2_blr_golden.json',
    )
    with open(fixture_path, encoding='utf-8') as f:
        return json.load(f)


def test_blr_golden_fixture_validates_against_v2_schema():
    days = _load_blr_fixture()
    v = validate_cas_reader_v2_response({'days': days})
    assert v['errors'] == [], f'BLR fixture has schema errors: {v["errors"]}'


def test_blr_golden_fixture_has_x_return_healing():
    days = _load_blr_fixture()
    by_date = {d['datum']: d for d in days}
    assert '2025-01-06' in by_date
    d = by_date['2025-01-06']
    assert d['is_tour_return'] is True
    assert d['return_from_layover'] is True
    assert d['origin_iata'] == 'BLR'
    assert d['destination_iata'] == 'FRA'


def test_blr_golden_fixture_mid_tour_x_not_free():
    days = _load_blr_fixture()
    by_date = {d['datum']: d for d in days}
    for ds in ('2025-01-04', '2025-01-05'):
        d = by_date[ds]
        assert d['activity_type'] != 'free', (
            f'{ds}: V2-Reader darf X nicht free klassifizieren'
        )
        assert d['is_tour_continuation'] is True
        assert d['layover_iata'] == 'BLR'
