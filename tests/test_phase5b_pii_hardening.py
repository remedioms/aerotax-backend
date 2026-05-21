"""BH-CORE-001 Phase 5b-Pre — PII-Hardening für KI-Resolver-Prompt-Builder.

Tests verifizieren:
- Whitelist entfernt PII-Felder aus dem KI-Prompt
- Plan-Fakten (datum, marker, routing, layover_ort, etc.) bleiben erhalten
- Session-Tokens / Payment-IDs / API-Keys werden entfernt
- Raw-PDF-Text / file_bytes / raw_lines werden entfernt
- Phase-5b-Kandidaten-Prompts enthalten keine PII

KEIN Live-Call. Nur Prompt-Builder-Hardening.
"""
import os
import sys
import json
import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import app as app_module

pytestmark = pytest.mark.skipif(
    not hasattr(app_module, '_ai_resolver_build_prompt')
    or not hasattr(app_module, '_ai_resolver_safe_context'),
    reason='Phase 5b PII-Hardening (_ai_resolver_safe_context) noch nicht implementiert',
)


def _day(datum, **kw):
    base = {
        'datum': datum, 'activity_type': 'tour', 'routing': [],
        'layover_ort': '', 'overnight_after_day': False,
        'start_time': '', 'end_time': '', 'duty_duration_minutes': 0,
        'raw_marker': '', 'has_fl': False, 'is_workday': True,
        'requires_commute': False, 'starts_at_homebase': False,
        'ends_at_homebase': False,
    }
    base.update(kw)
    return base


def _build(kind, context, fact='X'):
    return app_module._ai_resolver_build_prompt(kind, context, fact)


# ════════════════════════════════════════════════════════════════════════════
# 1. Whitelist entfernt PII-Felder aus dem Prompt
# ════════════════════════════════════════════════════════════════════════════

def test_ai_prompt_context_whitelist_removes_pii():
    """PII-Felder (name, mitarbeiter, pnr, email, address, iban, tax_id,
    birthdate, ...) sind im Prompt nicht enthalten."""
    ctx = {
        'day': _day('2025-12-14', routing=['FRA','JFK'], layover_ort='JFK'),
        'homebase': 'FRA',
        # PII die NICHT im Prompt landen darf:
        'name': 'Tibor Mustermann',
        'vorname': 'Tibor',
        'nachname': 'Mustermann',
        'mitarbeiter_nr': '12345',
        'personalnummer': '987654',
        'employee_id': 'EMP-001',
        'employee_name': 'Tibor Mustermann',
        'pnr': 'AB1234',
        'email': 'tibor@example.com',
        'phone': '+491701234567',
        'address': 'Musterstr 1, 60000 Frankfurt',
        'iban': 'DE99 1234 5678 9012 3456 78',
        'tax_id': '12345678901',
        'steuernummer': '12/345/67890',
        'birthdate': '1980-01-01',
        'date_of_birth': '1980-01-01',
        'anrede': 'Herr',
        'wohnort': 'Frankfurt',
    }
    prompt = _build('place_code', ctx, 'JFK')
    p_low = prompt.lower()
    pii_tokens = [
        'tibor', 'mustermann', '12345', '987654', 'emp-001',
        'ab1234', 'tibor@example.com', '+491701234567',
        'musterstr', 'frankfurt',
        'de99', '12345678901', '12/345/67890',
        '1980-01-01',
        'mitarbeiter_nr', 'personalnummer', 'employee_id',
        'employee_name', 'pnr', 'email', 'phone',
        'address', 'iban', 'tax_id', 'steuernummer',
        'birthdate', 'anrede', 'wohnort',
    ]
    for tok in pii_tokens:
        assert tok not in p_low, (
            f'PII-Token "{tok}" darf NICHT im Prompt erscheinen.\n'
            f'Prompt-Excerpt: {prompt[:400]}'
        )


# ════════════════════════════════════════════════════════════════════════════
# 2. Plan-Fakten bleiben erhalten
# ════════════════════════════════════════════════════════════════════════════

def test_ai_prompt_context_keeps_flight_plan_fields():
    """Plan-Fakten wie datum, marker, routing, layover_ort, start_time,
    duty_duration_minutes, overnight_after_day sind im Prompt vorhanden."""
    ctx = {
        'day': _day('2025-12-14', routing=['FRA','JFK'], layover_ort='JFK',
                    overnight_after_day=True, start_time='14:00',
                    duty_duration_minutes=600, raw_marker='57783 P1',
                    has_fl=True, starts_at_homebase=True),
        'homebase': 'FRA',
    }
    prompt = _build('place_code', ctx, '57783 P1')
    # Pflicht-Plan-Fakten müssen im Prompt sein
    assert '2025-12-14' in prompt
    assert 'JFK' in prompt
    assert 'FRA' in prompt
    assert '57783 P1' in prompt
    assert '14:00' in prompt
    assert '600' in prompt  # duty_duration_minutes
    # routing-array
    assert 'routing' in prompt.lower()
    # overnight-flag
    assert 'overnight' in prompt.lower()


# ════════════════════════════════════════════════════════════════════════════
# 3. Tokens / Payment-IDs werden entfernt
# ════════════════════════════════════════════════════════════════════════════

def test_ai_prompt_does_not_include_tokens_or_payment_ids():
    """Session-Tokens, API-Keys, Payment-IDs sind nicht im Prompt."""
    ctx = {
        'day': _day('2025-12-14', routing=['FRA','JFK']),
        'homebase': 'FRA',
        'session_token': 'sess_abc123xyz',
        'recovery_token': 'rec_def456',
        'access_token': 'eyJhbGc.tokenvalue.xyz',
        'auth_token': 'Bearer abc123',
        'payment_intent': 'pi_3ABC123XYZ',
        'payment_intent_id': 'pi_secret_xyz',
        'stripe_id': 'cus_abcdef',
        'stripe_session': 'cs_test_123',
        'supabase_user_id': 'usr-aaa-bbb',
        'user_id': '00000000-0000-0000-0000-000000000001',
        'api_token': 'sk-ant-secret-abc',
        'apikey': 'KEY12345',
    }
    prompt = _build('routing_consistency', ctx, 'test')
    p_low = prompt.lower()
    forbidden_substrings = [
        'sess_abc', 'rec_def', 'eyjhbgc', 'bearer abc',
        'pi_3abc', 'pi_secret', 'cus_abcdef', 'cs_test',
        'usr-aaa', '00000000-0000', 'sk-ant',
        'session_token', 'recovery_token', 'access_token',
        'auth_token', 'payment_intent', 'stripe', 'apikey',
        'api_token',
    ]
    for s in forbidden_substrings:
        assert s not in p_low, (
            f'Token/Secret-Substring "{s}" darf NICHT im Prompt erscheinen.\n'
            f'Prompt-Excerpt: {prompt[:400]}'
        )


# ════════════════════════════════════════════════════════════════════════════
# 4. Raw-PDF-Text / file_bytes werden entfernt
# ════════════════════════════════════════════════════════════════════════════

def test_ai_prompt_no_raw_pdf_text():
    """Raw-PDF-Text, pdf_bytes, file_content, raw_lines werden entfernt."""
    ctx = {
        'day': _day('2025-12-14', routing=['FRA','JFK']),
        'homebase': 'FRA',
        'raw_pdf_text': ('Tibor Mustermann, Mitarbeiter-Nr 12345, '
                         'Geburtsdatum 01.01.1980, Wohnort Frankfurt, '
                         'IBAN DE99 1234 5678 9012 3456 78.'),
        'pdf_bytes': b'%PDF-1.4\nstream\n...\nendstream\n%%EOF',
        'pdf_content': '...some PDF bytes...',
        'file_content': 'PDF content here with Tibor Mustermann inside',
        'filename': 'tibor_mustermann_lohnsteuer_2025.pdf',
        'original_filename': 'Lohnsteuer_Tibor_Mustermann.pdf',
        'pdf_filename': 'tibor.pdf',
    }
    prompt = _build('place_code', ctx, 'JFK')
    p_low = prompt.lower()
    # NOTE: 'lohnsteuer' bewusst NICHT in der Liste — erscheint legitim im
    # Crew-Kontext-System-Block ("Lohnsteuerunterlagen (LSB)") als Quellen-
    # Erwähnung, nicht als PII-Leak.
    forbidden = [
        'tibor', 'mustermann', '12345', '01.01.1980',
        'tibor_mustermann',
        'raw_pdf_text', 'pdf_bytes', 'pdf_content',
        'file_content', 'filename', 'pdf_filename',
        'endstream', '%pdf',
        # IBAN-Bestandteil
        'de99 1234', 'de99',
    ]
    for f in forbidden:
        assert f not in p_low, (
            f'Raw-PDF/File-Content-Token "{f}" darf NICHT im Prompt sein.\n'
            f'Prompt-Excerpt: {prompt[:600]}'
        )


def test_ai_prompt_no_raw_lines_in_day():
    """CAS day.raw_lines (rohzeilen) werden nicht in den Prompt durchgereicht."""
    ctx = {
        'day': {
            'datum': '2025-12-14', 'activity_type': 'tour',
            'routing': ['FRA','JFK'], 'layover_ort': 'JFK',
            'raw_marker': '57783 P1',
            # raw_lines kann komplette CAS-Zeilen enthalten — PII-Risiko
            'raw_lines': [
                'Mitarbeiter: Tibor Mustermann 12345',
                'Geburtsort: Frankfurt',
                'Personalnummer 987654',
            ],
        },
        'homebase': 'FRA',
    }
    prompt = _build('place_code', ctx, '57783 P1')
    p_low = prompt.lower()
    for tok in ('tibor', 'mustermann', '12345', 'personalnummer',
                'geburtsort', 'raw_lines'):
        assert tok not in p_low, (
            f'raw_lines-Inhalt "{tok}" darf NICHT im Prompt erscheinen'
        )


# ════════════════════════════════════════════════════════════════════════════
# 5. Phase-5b-Kandidaten produzieren saubere Prompts
# ════════════════════════════════════════════════════════════════════════════

def test_phase5b_candidates_prompt_safe():
    """Alle 8 vorbereiteten Phase-5b-Kandidaten-Prompts enthalten keine
    bekannten PII-Tokens und enthalten den Pflicht-Crew-Kontext + Anti-Tax-
    Warnung."""
    candidates = [
        ('place_code', _day('2025-12-14', routing=['FRA','JFK'],
                            layover_ort='JFK', overnight_after_day=True,
                            starts_at_homebase=True, start_time='14:00',
                            duty_duration_minutes=600,
                            raw_marker='57783 P1', has_fl=True)),
        ('tour_boundary', _day('2025-12-15', routing=['JFK','FRA'],
                               ends_at_homebase=True, start_time='02:00',
                               duty_duration_minutes=580,
                               raw_marker='57783 P1 Tag 2', has_fl=True)),
        ('routing_consistency', _day('2025-07-03',
                                     routing=['OTP','FRA','LHR'],
                                     start_time='06:00',
                                     duty_duration_minutes=720,
                                     raw_marker='129023 PU / Tag 3',
                                     has_fl=True)),
        ('standby_context', _day('2025-04-23', raw_marker='RES')),
        ('tour_boundary', _day('2025-07-29', routing=[],
                               layover_ort='RIX', overnight_after_day=True,
                               raw_marker='X RIX')),
        ('marker_semantics', _day('2025-01-04', routing=[],
                                  layover_ort='BLR',
                                  overnight_after_day=True, raw_marker='X')),
        ('tour_boundary', _day('2025-05-20', routing=['FRA','LAD'],
                               layover_ort='LAD', overnight_after_day=True,
                               starts_at_homebase=True, start_time='20:05',
                               duty_duration_minutes=234,
                               raw_marker='103703 P1', has_fl=True)),
        ('tour_boundary', _day('2025-10-26', routing=['FRA','TLV'],
                               layover_ort='TLV', overnight_after_day=True,
                               starts_at_homebase=True, start_time='17:00',
                               duty_duration_minutes=300,
                               raw_marker='32935 PU', has_fl=True)),
    ]
    # PII-Probe-Felder die wir bewusst NICHT in context geben — falls sie aber
    # versehentlich aus übergeordneten Stellen reinkämen (Pipeline-Bug), test
    # dass der Builder sie filtert.
    for kind, day in candidates:
        ctx = {
            'day': day,
            'homebase': 'FRA',
            # PII die der Builder filtern muss falls sie versehentlich
            # durchkäme
            'employee_id': 'EMP_X',
            'pnr': 'AB1234',
            'session_token': 'sess_xyz',
        }
        prompt = app_module._ai_resolver_build_prompt(
            kind, ctx, day.get('raw_marker') or ''
        )
        p_low = prompt.lower()
        # Pflicht-Crew-Kontext
        assert 'flugpersonal' in p_low or 'cockpit' in p_low or 'kabine' in p_low
        # Anti-Tax-Warnung
        assert 'kein' in p_low and ('eur' in p_low or 'steuer' in p_low
                                    or 'pauschal' in p_low)
        # PII nicht durchgekommen
        assert 'emp_x' not in p_low
        assert 'ab1234' not in p_low
        assert 'sess_xyz' not in p_low
        # Plan-Fakten erhalten
        assert day['datum'] in prompt
        if day.get('raw_marker'):
            assert day['raw_marker'] in prompt


# ════════════════════════════════════════════════════════════════════════════
# Bonus: _ai_resolver_safe_context-Direkt-Tests
# ════════════════════════════════════════════════════════════════════════════

def test_safe_context_removes_pii_recursively():
    """PII-Felder werden auch aus verschachtelten Dicts entfernt."""
    ctx = {
        'day': {
            'datum': '2025-01-03', 'routing': ['FRA','BLR'],
            'layover_ort': 'BLR',
            'employee_name': 'Tibor M.',  # innerhalb day
            'pnr': 'AB1234',
        },
        'se': {
            'stfrei_ort': 'BLR', 'stfrei_total': 42.0,  # tax-Wert: entfernt
            'count': 1, 'stfrei_inland': False,
            'iban': 'DE99...',                          # PII: entfernt
        },
        'homebase': 'FRA',
        'name': 'Tibor M.',  # top-level PII
    }
    safe = app_module._ai_resolver_safe_context(ctx)
    safe_str = json.dumps(safe, default=str).lower()
    assert 'tibor' not in safe_str
    assert 'ab1234' not in safe_str
    assert 'de99' not in safe_str
    # tax-Wert raus
    assert 'stfrei_total' not in safe_str
    assert '42.0' not in safe_str
    # Plan-Fakten erhalten
    assert 'blr' in safe_str
    assert 'fra' in safe_str
    # se-Booleanisiert
    assert safe['se'].get('se_has_allowance') is True
