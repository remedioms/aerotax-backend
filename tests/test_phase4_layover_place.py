"""Phase 4 — Hotel-/Layover-Kontext-Resolver Tests.

Verifiziert die Kaskade `_infer_layover_ort_from_context`:
  1. routing[-1]
  2. SE-stfrei_ort
  3. prev-day routing
  4. next-day routing
  5. KI-Resolver kind='layover_place'

Plus Integration in `_deterministic_classify_v7`: overnight ohne layover_ort
→ Kaskade greift, audit-entry in rescues.
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

import app as app_module


def _clear_caches():
    app_module._ai_resolver_cache.clear()


def make_day(datum, routing=None, layover_ort='', overnight=False,
             marker='', activity_type='tour', stfrei_ort='', stfrei_inland=None):
    """Builds a matched-day-dict mit dp + se."""
    return {
        'datum': datum,
        'dp': {
            'datum': datum,
            'activity_type': activity_type,
            'routing': routing or [],
            'layover_ort': layover_ort,
            'overnight_after_day': overnight,
            'raw_marker': marker,
            'start_time': '',
            'end_time': '',
            'duty_duration_minutes': 0,
            'has_fl': True,
            'is_workday': True,
            'requires_commute': True,
            'starts_at_homebase': True,
            'ends_at_homebase': True,
            'raw_lines': [],
            'confidence': 0.9,
        },
        'se': {
            'stfrei_total': 0.0,
            'stfrei_ort':   stfrei_ort,
            'stfrei_inland': stfrei_inland,
            'zwoelftel':    0,
            'lines':        [],
            'count':        1 if stfrei_ort else 0,
        },
    }


# ─── Kaskade: routing[-1] ───────────────────────────────────────────────────

class TestRoutingEndpointFallback(unittest.TestCase):

    def setUp(self):
        _clear_caches()

    def test_layover_inferred_from_current_day_routing_endpoint(self):
        """routing=['FRA','LAD'], overnight=True, layover_ort='' → LAD."""
        sorted_days = [
            make_day('2025-05-22', routing=['FRA', 'LAD'], overnight=True),
        ]
        r = app_module._infer_layover_ort_from_context(
            0, sorted_days, _allow_ai_resolver=False)
        self.assertIsNotNone(r)
        self.assertEqual(r['ort'], 'LAD')
        self.assertEqual(r['source'], 'routing_endpoint')
        self.assertGreaterEqual(r['confidence'], 0.90)


# ─── Kaskade: SE-stfrei_ort ────────────────────────────────────────────────

class TestSEFallback(unittest.TestCase):

    def setUp(self):
        _clear_caches()

    def test_layover_inferred_from_se_stfrei_when_no_routing(self):
        """routing leer, SE-stfrei_ort='AGP' → AGP."""
        sorted_days = [
            make_day('2025-09-26', routing=[], overnight=True, stfrei_ort='AGP'),
        ]
        r = app_module._infer_layover_ort_from_context(
            0, sorted_days, _allow_ai_resolver=False)
        self.assertIsNotNone(r)
        self.assertEqual(r['ort'], 'AGP')
        self.assertEqual(r['source'], 'se_stfrei_ort')


# ─── Kaskade: Vortag routing ───────────────────────────────────────────────

class TestPrevDayFallback(unittest.TestCase):

    def setUp(self):
        _clear_caches()

    def test_layover_inferred_from_prev_day_routing(self):
        """Tag N hat routing leer + overnight. Vortag (auch overnight)
        hat routing=['FRA','BLR']. → BLR."""
        sorted_days = [
            make_day('2025-01-03', routing=['FRA', 'BLR'], overnight=True),
            make_day('2025-01-04', routing=[], overnight=True),  # mid-day, kein routing
        ]
        r = app_module._infer_layover_ort_from_context(
            1, sorted_days, _allow_ai_resolver=False)
        self.assertIsNotNone(r)
        self.assertEqual(r['ort'], 'BLR')
        self.assertEqual(r['source'], 'prev_day_routing')


# ─── Kaskade: Folgetag routing ─────────────────────────────────────────────

class TestNextDayFallback(unittest.TestCase):

    def setUp(self):
        _clear_caches()

    def test_layover_inferred_from_next_day_routing(self):
        """Tag N hat routing leer, kein SE. Folgetag hat routing=['BLR','FRA']
        → von BLR aus weg → Layover war BLR."""
        sorted_days = [
            make_day('2025-01-03', routing=[], overnight=True),  # routing leer
            make_day('2025-01-04', routing=['BLR', 'FRA'], overnight=False),  # heimflug von BLR
        ]
        r = app_module._infer_layover_ort_from_context(
            0, sorted_days, _allow_ai_resolver=False)
        self.assertIsNotNone(r)
        self.assertEqual(r['ort'], 'BLR')
        self.assertEqual(r['source'], 'next_day_routing')


# ─── Kein overnight → kein Resolver ─────────────────────────────────────────

class TestNoOvernight(unittest.TestCase):

    def setUp(self):
        _clear_caches()

    def test_no_inference_when_not_overnight(self):
        """Wenn overnight=False, soll Helper None returnen."""
        sorted_days = [
            make_day('2025-01-01', routing=['FRA', 'MUC'], overnight=False),
        ]
        r = app_module._infer_layover_ort_from_context(0, sorted_days,
                                                        _allow_ai_resolver=False)
        self.assertIsNone(r)


# ─── KI-Resolver-Fallback ───────────────────────────────────────────────────

class TestAiLayoverFallback(unittest.TestCase):
    """Kaskaden-Stufe 5: alle deterministic-Quellen leer → KI-Resolver."""

    def setUp(self):
        _clear_caches()

    def test_ai_called_when_all_deterministic_sources_empty(self):
        """Kein routing, kein SE, kein prev/next-routing → KI wird gerufen."""
        sorted_days = [
            make_day('2025-12-15', routing=[], overnight=True),
        ]
        mock_ai_result = {
            'resolved': True,
            'value': {'resolved_place': 'JFK', 'country': 'USA',
                      'iata': 'JFK'},
            'confidence': 0.92,
            'reason': 'Plan-Kontext zeigt Tour zu USA',
            'evidence': ['routing fehlt für 2025-12-15'],
            'needs_review': False,
        }
        with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai',
                                return_value=mock_ai_result) as mock_ai:
            r = app_module._infer_layover_ort_from_context(
                0, sorted_days, job_id='test',
                _allow_ai_resolver=True)
            self.assertIsNotNone(r)
            self.assertEqual(r['ort'], 'JFK')
            self.assertEqual(r['source'], 'ai_resolver')
            mock_ai.assert_called_once()
            # Verify kind='layover_place'
            self.assertEqual(mock_ai.call_args.kwargs.get('kind'), 'layover_place')

    def test_ai_low_confidence_returns_none(self):
        """KI-Resolver mit needs_review=True → kein auto-resolve, None."""
        sorted_days = [
            make_day('2025-12-15', routing=[], overnight=True),
        ]
        mock_ai_result = {
            'resolved': False,
            'value': {},
            'confidence': 0.5,
            'reason': 'unsicher', 'evidence': [],
            'needs_review': True,
        }
        with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai',
                                return_value=mock_ai_result):
            r = app_module._infer_layover_ort_from_context(
                0, sorted_days, _allow_ai_resolver=True)
            self.assertIsNone(r)


# ─── Integration in _deterministic_classify_v7 ──────────────────────────────

class TestIntegrationDeterministicClassifier(unittest.TestCase):
    """End-to-end: overnight + layover_ort='' im matched-day → klass nutzt
    abgeleiteten layover_ort + rescues-entry."""

    def setUp(self):
        _clear_caches()

    def test_classifier_uses_inferred_layover_for_classification(self):
        """Tour: 05-22 routing=['LAD'] overnight=True, layover_ort=''.
        Phase-4 Kaskade leitet 'LAD' aus routing[-1] ab → Klassifikation als
        Z76 Angola (statt Pauschal-Fallback 28€)."""
        matched = [
            # Anreise-Tag (foreign cluster wird durch routing+overnight gebaut)
            make_day('2025-05-21', routing=['FRA', 'LAD'], overnight=True,
                     activity_type='tour'),
            # Mid-day mit fehlendem layover_ort
            make_day('2025-05-22', routing=['LAD'], overnight=True,
                     activity_type='tour'),
            # Heimflug
            make_day('2025-05-23', routing=['LAD', 'FRA'], overnight=False,
                     activity_type='tour'),
        ]
        # Sicherstellen dass routing-Resolver fired (kein KI-Call nötig)
        result = app_module._deterministic_classify_v7(matched, year=2025, homebase='FRA')
        # Audit-Entry: rescues enthält layover_place_inferred
        rescues = result.get('_rescues') or result.get('rescues') or []
        layover_rescues = [r for r in rescues if r.get('rescue_type') == 'layover_place_inferred']
        self.assertGreater(len(layover_rescues), 0,
            f'Erwarte rescues mit type=layover_place_inferred, got {rescues[:3]}')

    def test_classifier_no_rescue_when_layover_already_set(self):
        """Wenn layover_ort schon gesetzt ist, soll keine Inferenz greifen
        (deterministic, unverändert)."""
        matched = [
            make_day('2025-05-21', routing=['FRA', 'IST'], overnight=True,
                     layover_ort='IST', activity_type='tour'),
            make_day('2025-05-22', routing=['IST', 'FRA'], overnight=False,
                     activity_type='tour'),
        ]
        result = app_module._deterministic_classify_v7(matched, year=2025, homebase='FRA')
        rescues = result.get('_rescues') or result.get('rescues') or []
        layover_rescues = [r for r in rescues if r.get('rescue_type') == 'layover_place_inferred']
        self.assertEqual(len(layover_rescues), 0,
            'Kein layover_place_inferred wenn layover_ort schon im Reader')


# ─── Cascade priority ──────────────────────────────────────────────────────

class TestCascadePriority(unittest.TestCase):

    def setUp(self):
        _clear_caches()

    def test_routing_endpoint_beats_se(self):
        """Wenn beide vorhanden, routing[-1] vor SE-stfrei."""
        sorted_days = [
            make_day('2025-01-01', routing=['FRA', 'AAA'], overnight=True,
                     stfrei_ort='BBB'),
        ]
        r = app_module._infer_layover_ort_from_context(0, sorted_days,
                                                        _allow_ai_resolver=False)
        self.assertEqual(r['ort'], 'AAA')
        self.assertEqual(r['source'], 'routing_endpoint')

    def test_se_beats_prev_day_routing(self):
        """SE vorhanden vor prev_day_routing."""
        sorted_days = [
            make_day('2025-01-01', routing=['FRA', 'XXX'], overnight=True),
            make_day('2025-01-02', routing=[], overnight=True, stfrei_ort='YYY'),
        ]
        r = app_module._infer_layover_ort_from_context(1, sorted_days,
                                                        _allow_ai_resolver=False)
        self.assertEqual(r['ort'], 'YYY')
        self.assertEqual(r['source'], 'se_stfrei_ort')


if __name__ == '__main__':
    unittest.main(verbosity=2)
