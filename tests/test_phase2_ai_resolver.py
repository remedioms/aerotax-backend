"""Phase 2 — KI-Resolver-Infrastruktur Tests.

Reine Infrastruktur-Tests mit Mock. KEINE echten Anthropic-API-Calls.
Verifiziert:
- JSON-Schema-Handling
- Anti-Tax-Sanitizer
- Confidence-Schwellen
- Cache
- Timeout/Retry
- Airline-Crew-Kontext im Prompt
- Audit ohne PII
"""
import json
import os
import sys
import unittest
from unittest import mock

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# conftest.py setzt AEROTAX_ALLOW_BOOT_WITHOUT_KEY=1
os.environ.setdefault('ANTHROPIC_API_KEY', 'sk-ant-test-dummy')
import app as app_module


# ─── Mock Anthropic Client ──────────────────────────────────────────────────

class MockMessage:
    def __init__(self, text):
        self.content = [mock.MagicMock(text=text)]


class MockAnthropicClient:
    """Programmierbarer Mock — returnt configured JSON-text aus messages.create."""
    def __init__(self, responses=None, raise_on_call=None):
        self.responses = responses if isinstance(responses, list) else [responses]
        self.raise_on_call = raise_on_call
        self.call_count = 0
        self.last_prompt = None
        self.messages = self

    def create(self, model=None, max_tokens=None, messages=None):
        self.call_count += 1
        self.last_prompt = messages[0]['content'] if messages else ''
        if self.raise_on_call:
            raise self.raise_on_call
        idx = min(self.call_count - 1, len(self.responses) - 1)
        return MockMessage(self.responses[idx])


def _clear_resolver_cache():
    app_module._ai_resolver_cache.clear()


# ─── JSON-Schema + Validation ───────────────────────────────────────────────

class TestJsonSchemaValidation(unittest.TestCase):

    def setUp(self):
        _clear_resolver_cache()

    def test_ai_resolution_json_schema_validated(self):
        """Valid JSON mit allen Pflichtfeldern → korrektes Result-Dict."""
        client = MockAnthropicClient(responses=[json.dumps({
            'resolved': True,
            'value': {'resolved_place': 'Chicago', 'country': 'USA'},
            'confidence': 0.95,
            'reason': 'Metro-Code CHI = Chicago',
            'evidence': ['CHI ist IATA Metro Area Code für Chicago'],
            'needs_review': False,
        })])
        r = app_module._resolve_uncertain_fact_with_ai(
            'place_code', {'code': 'CHI'},
            job_id='test', datum='2025-01-01', uncertain_fact='CHI',
            _anthropic_client=client,
        )
        self.assertTrue(r['resolved'])
        self.assertEqual(r['value']['resolved_place'], 'Chicago')
        self.assertEqual(r['value']['country'], 'USA')
        self.assertEqual(r['confidence'], 0.95)
        self.assertFalse(r['needs_review'])

    def test_ai_resolution_invalid_json_falls_back_to_review(self):
        """Broken JSON → resolved=False, needs_review=True."""
        client = MockAnthropicClient(responses=['this is not json {{{', 'still not json'])
        r = app_module._resolve_uncertain_fact_with_ai(
            'place_code', {'code': 'XYZ'},
            job_id='test', uncertain_fact='XYZ',
            _anthropic_client=client,
        )
        self.assertFalse(r['resolved'])
        self.assertTrue(r['needs_review'])
        self.assertIn('json_invalid', r['reason'])

    def test_ai_resolution_retry_once_only(self):
        """Bei JSON-invalid: max 1 retry → API max 2× total."""
        client = MockAnthropicClient(responses=['no json #1', 'no json #2'])
        app_module._resolve_uncertain_fact_with_ai(
            'place_code', {'code': 'XYZ'},
            uncertain_fact='XYZ', _anthropic_client=client,
        )
        self.assertLessEqual(client.call_count, 2,
            f'Max 2 API-Calls erwartet (1 + 1 retry), got {client.call_count}')


# ─── Anti-Tax-Sanitizer ────────────────────────────────────────────────────

class TestAntiTaxSanitizer(unittest.TestCase):

    def setUp(self):
        _clear_resolver_cache()

    def test_ai_resolution_does_not_include_tax_amount(self):
        """KI returnt amount-Feld → reject."""
        client = MockAnthropicClient(responses=[json.dumps({
            'resolved': True,
            'value': {'resolved_place': 'Chicago', 'amount': 40.0},
            'confidence': 0.95,
            'reason': '',
            'evidence': [],
            'needs_review': False,
        })])
        r = app_module._resolve_uncertain_fact_with_ai(
            'place_code', {'code': 'CHI'},
            uncertain_fact='CHI', _anthropic_client=client,
        )
        self.assertFalse(r['resolved'],
            'KI-Value mit "amount"-Feld muss rejected werden')
        self.assertTrue(r['needs_review'])
        self.assertIn('forbidden_key', r['reason'])

    def test_ai_resolution_rejects_eur_rate_betrag_fields(self):
        """Jedes verbotene Feld (eur, rate, betrag, euro, tagesatz) → reject."""
        for forbidden in ['eur', 'rate', 'betrag', 'euro', 'tagesatz',
                          'pauschale', 'tax', 'steuer', 'an_abreise', 'voll_24h']:
            _clear_resolver_cache()
            client = MockAnthropicClient(responses=[json.dumps({
                'resolved': True,
                'value': {'resolved_place': 'X', forbidden: 99.0},
                'confidence': 0.95,
                'reason': '', 'evidence': [], 'needs_review': False,
            })])
            r = app_module._resolve_uncertain_fact_with_ai(
                'place_code', {'code': 'X'},
                uncertain_fact='X', _anthropic_client=client,
            )
            self.assertFalse(r['resolved'],
                f'Feld "{forbidden}" muss rejected werden')
            self.assertIn(forbidden, r['reason'])


# ─── Confidence-Schwellen ───────────────────────────────────────────────────

class TestConfidenceThresholds(unittest.TestCase):

    def setUp(self):
        _clear_resolver_cache()

    def _call_with_confidence(self, conf, key='CHI'):
        _clear_resolver_cache()
        client = MockAnthropicClient(responses=[json.dumps({
            'resolved': True,
            'value': {'resolved_place': 'Chicago', 'country': 'USA'},
            'confidence': conf,
            'reason': 'test',
            'evidence': [],
            'needs_review': False,
        })])
        return app_module._resolve_uncertain_fact_with_ai(
            'place_code', {'code': key},
            uncertain_fact=key, _anthropic_client=client,
        )

    def test_ai_resolution_confidence_auto_threshold(self):
        """conf=0.95 (≥0.90) → resolved=True, needs_review=False."""
        r = self._call_with_confidence(0.95, key='C1')
        self.assertTrue(r['resolved'])
        self.assertFalse(r['needs_review'])

    def test_ai_resolution_confidence_review_threshold(self):
        """conf=0.80 (zwischen 0.70 und 0.90) → resolved=True, needs_review=True."""
        r = self._call_with_confidence(0.80, key='C2')
        self.assertTrue(r['resolved'])
        self.assertTrue(r['needs_review'])

    def test_ai_resolution_confidence_user_question_threshold(self):
        """conf=0.50 (<0.70) → resolved=False, needs_review=True."""
        r = self._call_with_confidence(0.50, key='C3')
        self.assertFalse(r['resolved'])
        self.assertTrue(r['needs_review'])

    def test_ai_resolution_threshold_boundary_0_90(self):
        """Genau 0.90 → auto-resolve."""
        r = self._call_with_confidence(0.90, key='C4')
        self.assertTrue(r['resolved'])
        self.assertFalse(r['needs_review'])

    def test_ai_resolution_threshold_boundary_0_70(self):
        """Genau 0.70 → review."""
        r = self._call_with_confidence(0.70, key='C5')
        self.assertTrue(r['resolved'])
        self.assertTrue(r['needs_review'])


# ─── Cache ─────────────────────────────────────────────────────────────────

class TestCache(unittest.TestCase):

    def setUp(self):
        _clear_resolver_cache()

    def test_ai_resolution_cached_per_job_day_kind(self):
        """Zweiter Call mit gleicher (job_id, datum, kind, context) → kein API-Call."""
        client = MockAnthropicClient(responses=[json.dumps({
            'resolved': True,
            'value': {'resolved_place': 'X'},
            'confidence': 0.95,
            'reason': 'r', 'evidence': [], 'needs_review': False,
        })])
        kwargs = dict(
            kind='place_code', context={'code': 'CACHED'},
            job_id='job-cache-1', datum='2025-01-01',
            uncertain_fact='CACHED', _anthropic_client=client,
        )
        r1 = app_module._resolve_uncertain_fact_with_ai(**kwargs)
        r2 = app_module._resolve_uncertain_fact_with_ai(**kwargs)
        self.assertEqual(r1['value'], r2['value'])
        self.assertEqual(client.call_count, 1,
            f'Cache miss — got {client.call_count} calls, expected 1')

    def test_ai_resolution_context_hash_stable(self):
        """Gleicher Context (auch unsortiert) → gleicher Hash → Cache hit."""
        h1 = app_module._ai_resolver_context_hash({'a': 1, 'b': 2, 'c': 3})
        h2 = app_module._ai_resolver_context_hash({'c': 3, 'a': 1, 'b': 2})
        self.assertEqual(h1, h2)

    def test_ai_resolution_cache_key_separated_by_job(self):
        """Verschiedene job_ids → unterschiedlicher Cache → beide Calls."""
        client = MockAnthropicClient(responses=[json.dumps({
            'resolved': True,
            'value': {'resolved_place': 'X'},
            'confidence': 0.95,
            'reason': '', 'evidence': [], 'needs_review': False,
        })])
        kwargs_base = dict(
            kind='place_code', context={'code': 'X'},
            datum='2025-01-01', uncertain_fact='X', _anthropic_client=client,
        )
        app_module._resolve_uncertain_fact_with_ai(job_id='job-A', **kwargs_base)
        app_module._resolve_uncertain_fact_with_ai(job_id='job-B', **kwargs_base)
        self.assertEqual(client.call_count, 2,
            f'Jobs müssen separate Cache-Slots haben')


# ─── Timeout / Error / Retry ────────────────────────────────────────────────

class TestErrorHandling(unittest.TestCase):

    def setUp(self):
        _clear_resolver_cache()

    def test_ai_resolution_timeout_falls_back_to_review(self):
        """API raises Timeout → review-fallback."""
        client = MockAnthropicClient(raise_on_call=TimeoutError('mock timeout'))
        r = app_module._resolve_uncertain_fact_with_ai(
            'place_code', {'code': 'X'},
            uncertain_fact='X', _anthropic_client=client,
        )
        self.assertFalse(r['resolved'])
        self.assertTrue(r['needs_review'])
        self.assertIn('api_error', r['reason'])

    def test_ai_resolution_unknown_kind_review_fallback(self):
        """Unbekannter kind → review-fallback ohne API-Call."""
        client = MockAnthropicClient(responses=[])
        r = app_module._resolve_uncertain_fact_with_ai(
            'banana_split', {},
            uncertain_fact='?', _anthropic_client=client,
        )
        self.assertFalse(r['resolved'])
        self.assertEqual(client.call_count, 0)
        self.assertIn('unknown_kind', r['reason'])

    def test_ai_resolution_api_error_no_retry(self):
        """API-Error (nicht JSON-Issue): kein retry → max 1 Call.
        Cost-Schutz."""
        client = MockAnthropicClient(raise_on_call=RuntimeError('API down'))
        app_module._resolve_uncertain_fact_with_ai(
            'place_code', {'code': 'X'},
            uncertain_fact='X', _anthropic_client=client,
        )
        self.assertLessEqual(client.call_count, 1)


# ─── Airline-Crew-Kontext im Prompt ─────────────────────────────────────────

class TestPromptContext(unittest.TestCase):

    def setUp(self):
        _clear_resolver_cache()

    def _capture_prompt(self, kind='place_code'):
        client = MockAnthropicClient(responses=[json.dumps({
            'resolved': True, 'value': {}, 'confidence': 0.5,
            'reason': '', 'evidence': [], 'needs_review': True,
        })])
        app_module._resolve_uncertain_fact_with_ai(
            kind, {'sample_marker': 'ORTSTAG'},
            uncertain_fact='ORTSTAG', _anthropic_client=client,
        )
        return client.last_prompt or ''

    def test_ai_prompt_contains_airline_crew_context(self):
        """Prompt enthält Airline-Crew-Kontext-Marker."""
        p = self._capture_prompt()
        self.assertIn('Flugpersonal', p)
        self.assertIn('Crew', p)

    def test_ai_prompt_contains_cockpit_cabin_context(self):
        """Prompt enthält Cockpit/Kabine."""
        p = self._capture_prompt()
        self.assertIn('Cockpit', p)
        self.assertIn('Kabine', p)

    def test_ai_prompt_mentions_lufthansa_or_similar(self):
        """Prompt nennt Lufthansa-ähnlichen Crew-Roster."""
        p = self._capture_prompt()
        self.assertIn('Lufthansa', p)

    def test_ai_prompt_for_marker_is_not_contextless(self):
        """marker_semantics-Prompt nicht kontextlos — enthält Crew + Roster-Hinweis."""
        p = self._capture_prompt(kind='marker_semantics')
        self.assertIn('Crew', p)
        self.assertIn('Roster', p)
        # Plus kind-specific Markers
        self.assertIn('ORTSTAG', p)
        self.assertIn('Nachbarzeilen', p)

    def test_ai_prompt_place_code_mentions_airport_city_metro(self):
        """place_code-Prompt nennt Airport/City/Metro-Code-Optionen."""
        p = self._capture_prompt(kind='place_code')
        for term in ('Airport', 'City', 'Metro', 'Layover'):
            self.assertIn(term, p, f'Term „{term}" fehlt in place_code-Prompt')

    def test_ai_prompt_cas_time_mentions_briefing_layover(self):
        """cas_time-Prompt nennt Briefing/Layover."""
        p = self._capture_prompt(kind='cas_time_extraction')
        self.assertIn('Briefing', p)
        self.assertIn('Crew', p)

    def test_ai_prompt_layover_place_mentions_tour_context(self):
        """layover_place-Prompt nennt Tour-Kontext + Nachbartage."""
        p = self._capture_prompt(kind='layover_place')
        self.assertIn('Tour', p)
        self.assertIn('Layover', p)

    def test_ai_prompt_includes_no_tax_amount_instruction(self):
        """Prompt enthält explizite Anweisung „KEINE Steuerbeträge nennen"."""
        p = self._capture_prompt()
        self.assertIn('KEINE Steuerbeträge', p)
        self.assertIn('Pauschalen', p)

    def test_ai_prompt_specifies_json_only_output(self):
        """Prompt verlangt strikt JSON."""
        p = self._capture_prompt()
        self.assertIn('JSON', p)


# ─── Audit / Logging ohne PII ───────────────────────────────────────────────

class TestNoPiiLogging(unittest.TestCase):

    def setUp(self):
        _clear_resolver_cache()

    def test_ai_resolution_logs_no_pii(self):
        """Logger-Output enthält keine raw PDF Bytes / Filenamen / personal data."""
        # PII-Test: context enthält fiktiv-personal-data, prüfen dass im log nicht erscheint
        canary = 'CANARY-Personennr-12345-Max-Mustermann-streetname'
        client = MockAnthropicClient(responses=[json.dumps({
            'resolved': True, 'value': {}, 'confidence': 0.95,
            'reason': 'ok', 'evidence': [], 'needs_review': False,
        })])
        with mock.patch.object(app_module.app.logger, 'info') as mi, \
             mock.patch.object(app_module.app.logger, 'warning') as mw, \
             mock.patch.object(app_module.app.logger, 'error') as me:
            app_module._resolve_uncertain_fact_with_ai(
                'place_code',
                {'code': 'X', 'personal_info': canary},
                job_id='job-pii', uncertain_fact='X',
                _anthropic_client=client,
            )
            all_log_args = []
            for m_obj in (mi, mw, me):
                for c in m_obj.call_args_list:
                    if c.args:
                        all_log_args.append(c.args[0])
            joined = ' '.join(str(a) for a in all_log_args)
            self.assertNotIn(canary, joined,
                f'PII canary leaked to log: {joined[:300]}')

    def test_ai_resolution_audit_includes_kind_and_confidence(self):
        """Audit-Log soll kind + confidence + resolved-Flag enthalten (für Debugging),
        ABER keine raw PII."""
        client = MockAnthropicClient(responses=[json.dumps({
            'resolved': True, 'value': {'resolved_place': 'Chicago'}, 'confidence': 0.95,
            'reason': '', 'evidence': [], 'needs_review': False,
        })])
        with mock.patch.object(app_module.app.logger, 'info') as mi:
            app_module._resolve_uncertain_fact_with_ai(
                'place_code', {'code': 'CHI'},
                uncertain_fact='CHI', _anthropic_client=client,
            )
            logged = ' '.join(c.args[0] for c in mi.call_args_list if c.args)
            self.assertIn('place_code', logged)
            self.assertIn('conf=', logged)


# ─── Beispiel-Outputs (Smoke-Demo) ──────────────────────────────────────────

class TestSampleOutputs(unittest.TestCase):
    """Konkrete Beispiele wie Phase 2 in der Praxis aussieht."""

    def setUp(self):
        _clear_resolver_cache()

    def test_sample_chi_resolves_to_chicago_usa(self):
        """Mock: CHI → Chicago/USA mit high confidence."""
        client = MockAnthropicClient(responses=[json.dumps({
            'resolved': True,
            'value': {'resolved_place': 'Chicago', 'country': 'USA',
                      'bmf_key': 'Vereinigte Staaten von Amerika (USA) – Chicago'},
            'confidence': 0.97,
            'reason': 'CHI ist offizieller IATA Metro Area Code für Chicago/USA',
            'evidence': ['SE-Stempel: stfrei_ort=CHI',
                         'CHI = Chicago Metro Area Code (umfasst ORD + MDW)'],
            'needs_review': False,
        })])
        r = app_module._resolve_uncertain_fact_with_ai(
            'place_code',
            {'code': 'CHI', 'context': 'Streckeneinsatz', 'date': '2025-05-17'},
            job_id='sample', uncertain_fact='CHI',
            _anthropic_client=client,
        )
        self.assertTrue(r['resolved'])
        self.assertFalse(r['needs_review'])
        self.assertEqual(r['value']['resolved_place'], 'Chicago')
        self.assertEqual(r['value']['country'], 'USA')
        # Wichtig: kein Steuerbetrag im value
        for forbidden in ('amount', 'eur', 'rate'):
            self.assertNotIn(forbidden, r['value'])

    def test_sample_ortstag_resolves_to_passive_grouped(self):
        """Mock: ORTSTAG-Marker bei Lufthansa-Crew → passive/zuhause-Kontext."""
        client = MockAnthropicClient(responses=[json.dumps({
            'resolved': True,
            'value': {'semantics': 'passive_home',
                      'description': 'ganztägiger Office/passive-Marker ohne aktive Dienstabwesenheit',
                      'typical_z72_applicable': False},
            'confidence': 0.86,
            'reason': 'ORTSTAG in Crew-Rosters ohne Briefingzeit ist üblich für '
                      'passive Office-Tage / Reserve zuhause',
            'evidence': ['Marker erscheint 28× ohne Uhrzeit',
                         'Kein SE-Spesen-Eintrag an diesen Tagen'],
            'needs_review': False,
        })])
        r = app_module._resolve_uncertain_fact_with_ai(
            'marker_semantics',
            {'marker': 'ORTSTAG', 'occurrences': 28,
             'neighbor_context': 'mostly_frei_or_office'},
            job_id='sample-2', uncertain_fact='ORTSTAG',
            _anthropic_client=client,
        )
        self.assertTrue(r['resolved'])
        # conf 0.86 < 0.90 → needs_review=True (Gruppen-Frage)
        self.assertTrue(r['needs_review'])
        self.assertEqual(r['value']['semantics'], 'passive_home')


if __name__ == '__main__':
    unittest.main(verbosity=2)
