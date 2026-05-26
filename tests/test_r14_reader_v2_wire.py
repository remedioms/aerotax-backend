"""R14 — Tests fuer Reader-V2 Feature-Flag-Verkabelung in app.py.

Kein Live-Call. Anthropic-Client wird gemocked; Tests verifizieren:
  - Flag aus → V2-Prompt NICHT angehaengt, _v2_active=False.
  - Flag an  → V2-Prompt angehaengt, _v2_active=True.
  - Validator wird vom Caller aufgerufen.
  - Ungueltige V2-Days → Validator-Errors, kein silent accept.
"""
import os
import sys
import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import app  # noqa: E402


# ────────────────────────────────────────────────────────────────────────────
# Anthropic-Mock — simuliert eine Sonnet-Antwort mit submit_cas_days-Tool-Use
# ────────────────────────────────────────────────────────────────────────────

class _FakeUsage:
    def __init__(self, in_t=100, out_t=200):
        self.input_tokens = in_t
        self.output_tokens = out_t


class _FakeToolUseBlock:
    def __init__(self, name='submit_cas_days', input_data=None):
        self.type = 'tool_use'
        self.name = name
        self.input = input_data or {}


class _FakeResp:
    def __init__(self, days=None, warnings=None, stop_reason='end_turn'):
        self.stop_reason = stop_reason
        self.usage = _FakeUsage()
        self.content = [
            _FakeToolUseBlock(input_data={
                'days': days or [],
                'warnings': warnings or [],
            }),
        ]


class _FakeMessages:
    def __init__(self, parent):
        self._parent = parent

    def create(self, **kwargs):
        # Speichere den Prompt zur Inspektion durch Tests
        self._parent.last_call_kwargs = kwargs
        msgs = kwargs.get('messages', [])
        if msgs and isinstance(msgs[0].get('content'), list):
            for block in msgs[0]['content']:
                if isinstance(block, dict) and block.get('type') == 'text':
                    self._parent.last_prompt_text = block.get('text', '')
                    break
        return _FakeResp(days=[
            {'date': '2025-01-15', 'activity_type': 'flight', 'marker': 'LH756',
             'start_time': '10:15', 'end_time': '', 'location': 'FRA',
             'flights': ['LH756'], 'overnight_after_day': True,
             'layover_ort': 'BLR', 'confidence': 'high', 'raw_excerpt': ''},
        ])


class _FakeAnthropic:
    def __init__(self, api_key=None, timeout=None):
        self.last_call_kwargs = None
        self.last_prompt_text = ''
        self.messages = _FakeMessages(self)


@pytest.fixture
def fake_anthropic(monkeypatch):
    """Patcht anthropic.Anthropic in app.py auf eine Mock-Klasse, die den
    Prompt zur Inspektion speichert."""
    import anthropic as _real_anthropic

    fake = {'instance': None}

    def _factory(api_key=None, timeout=None):
        inst = _FakeAnthropic(api_key=api_key, timeout=timeout)
        fake['instance'] = inst
        return inst

    monkeypatch.setattr(app, 'anthropic', type('m', (), {'Anthropic': _factory}))
    # ANTHROPIC_KEY muss truthy sein, damit der Code nicht frueh None returnt
    monkeypatch.setattr(app, 'ANTHROPIC_KEY', 'sk-mock-key')
    # _extract_cas_text + _is_cas_text_sufficient muessen text_ok=True liefern,
    # damit der text-path aktiv ist (kein PDF-Read-Versuch).
    monkeypatch.setattr(app, '_extract_cas_text', lambda b: 'fake CAS text 2025\n01.01\tOFF')
    monkeypatch.setattr(app, '_is_cas_text_sufficient', lambda t: (True, 'mock'))
    return fake


# ────────────────────────────────────────────────────────────────────────────
# R14.D — Feature-Flag-Wire in _sonnet_read_cas_single_pdf
# ────────────────────────────────────────────────────────────────────────────

def test_v2_flag_off_does_not_append_v2_prompt(fake_anthropic, monkeypatch):
    monkeypatch.delenv('AEROTAX_CAS_READER_V2', raising=False)
    result = app._sonnet_read_cas_single_pdf(
        pdf_bytes=b'fake', year=2025, homebase='FRA',
        source_filename='cas_test.pdf',
    )
    assert result is not None
    assert result.get('_v2_active') is False
    assert result.get('_v2_prompt_appended') is False
    prompt_text = fake_anthropic['instance'].last_prompt_text
    assert 'CAS READER V2' not in prompt_text
    assert 'REGEL 1' not in prompt_text


def test_v2_flag_on_appends_v2_prompt(fake_anthropic, monkeypatch):
    monkeypatch.setenv('AEROTAX_CAS_READER_V2', '1')
    result = app._sonnet_read_cas_single_pdf(
        pdf_bytes=b'fake', year=2025, homebase='FRA',
        source_filename='cas_test.pdf',
    )
    assert result is not None
    assert result.get('_v2_active') is True
    assert result.get('_v2_prompt_appended') is True
    prompt_text = fake_anthropic['instance'].last_prompt_text
    assert 'CAS READER V2' in prompt_text
    assert 'REGEL 1' in prompt_text
    assert 'tour_context_hint' in prompt_text


def test_v2_flag_off_keeps_legacy_prompt_intact(fake_anthropic, monkeypatch):
    """Default-Pfad bleibt unveraendert: alter Prompt wird verwendet."""
    monkeypatch.delenv('AEROTAX_CAS_READER_V2', raising=False)
    app._sonnet_read_cas_single_pdf(
        pdf_bytes=b'fake', year=2025, homebase='FRA',
        source_filename='cas_test.pdf',
    )
    prompt_text = fake_anthropic['instance'].last_prompt_text
    # Legacy-Schluessel-Marker
    assert 'Lufthansa CAS/Dienstplan/Roster' in prompt_text
    assert 'submit_cas_days' in prompt_text


def test_v2_flag_default_is_off():
    """Ohne env-var ist V2 aus (per Spec)."""
    from cas_reader_v2_spec import is_v2_enabled
    saved = os.environ.pop('AEROTAX_CAS_READER_V2', None)
    try:
        assert is_v2_enabled() is False
    finally:
        if saved is not None:
            os.environ['AEROTAX_CAS_READER_V2'] = saved


# ────────────────────────────────────────────────────────────────────────────
# R14.E — Validator-Hook
# ────────────────────────────────────────────────────────────────────────────

def _valid_v2_day():
    return {
        'datum': '2025-03-15', 'raw_marker': '755',
        'normalized_marker': '755', 'activity_type': 'tour_return',
        'starts_at_homebase': False, 'ends_at_homebase': True,
        'overnight_after_day': False,
        'routing_iatas': ['BLR', 'FRA'], 'flight_numbers': ['LH755'],
        'origin_iata': 'BLR', 'destination_iata': 'FRA',
        'layover_iata': 'BLR',
        'tour_context_hint': 'return', 'tour_context_confidence': 'high',
        'is_tour_departure': False, 'is_tour_continuation': False,
        'is_tour_return': True, 'return_from_layover': True,
        'has_flight_segment': True, 'confidence': 'high', 'warnings': [],
    }


def test_validator_hook_off_when_flag_off(monkeypatch):
    monkeypatch.delenv('AEROTAX_CAS_READER_V2', raising=False)
    v = app._validate_cas_v2_postprocessed_response([_valid_v2_day()])
    assert v['used'] is False
    assert v['errors'] == []


def test_validator_hook_on_with_valid_days(monkeypatch):
    monkeypatch.setenv('AEROTAX_CAS_READER_V2', '1')
    v = app._validate_cas_v2_postprocessed_response([_valid_v2_day()])
    assert v['used'] is True
    assert v['errors'] == []
    assert v['days_total'] == 1


def test_validator_hook_flags_invalid_v2_output_not_silent(monkeypatch, capsys):
    monkeypatch.setenv('AEROTAX_CAS_READER_V2', '1')
    bad_day = _valid_v2_day()
    bad_day['routing_iatas'] = ['LH756']  # Flight-Number in IATAs → Fehler
    del bad_day['layover_iata']            # required-field-fehler
    v = app._validate_cas_v2_postprocessed_response([bad_day])
    assert v['used'] is True
    assert v['errors'], 'Validator muss Errors zurueckmelden'
    out = capsys.readouterr().out
    # Errors werden geloggt (kein silent accept)
    assert 'CAS-Reader-V2-Validator' in out


def test_validator_hook_handles_empty_list(monkeypatch):
    monkeypatch.setenv('AEROTAX_CAS_READER_V2', '1')
    v = app._validate_cas_v2_postprocessed_response([])
    assert v['used'] is True
    assert v['days_total'] == 0
    assert v['errors'] == []


def test_validator_hook_handles_none(monkeypatch):
    monkeypatch.setenv('AEROTAX_CAS_READER_V2', '1')
    v = app._validate_cas_v2_postprocessed_response(None)
    assert v['used'] is True
    assert v['days_total'] == 0
