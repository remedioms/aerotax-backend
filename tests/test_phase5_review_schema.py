"""Phase 5 — Review-Item Schema Tests.

Verifiziert dass `_build_review_items` jetzt das erweiterte Schema liefert:
- source_type
- source_excerpt
- why_not_resolved
- suggested_answer
- confidence
- affected_days
"""
import os
import sys
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import app as app_module


class TestPhase5Schema(unittest.TestCase):

    def _build_items(self):
        cls_stub = {'office_training_time_missing_candidates': [
            {'datum': '2025-04-09', 'marker': 'D4 SCHULUNG',
             'activity_type': 'training', 'money_impact_estimate': 14.0},
        ]}
        return app_module._build_review_items(cls_stub)

    def test_review_item_contains_source_type(self):
        items = self._build_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].get('source_type'), 'CAS')

    def test_review_item_contains_source_excerpt(self):
        items = self._build_items()
        self.assertIn('D4 SCHULUNG', items[0].get('source_excerpt', ''))

    def test_review_item_contains_why_not_resolved(self):
        items = self._build_items()
        self.assertIn('keine Uhrzeit', items[0].get('why_not_resolved', ''))

    def test_review_item_contains_affected_days(self):
        items = self._build_items()
        self.assertEqual(items[0].get('affected_days'), ['2025-04-09'])

    def test_review_item_contains_suggested_answer_field_default_none(self):
        """suggested_answer-Feld existiert + ist None (Phase 6 wird es füllen)."""
        items = self._build_items()
        self.assertIn('suggested_answer', items[0])
        self.assertIsNone(items[0]['suggested_answer'])

    def test_review_item_contains_confidence_field(self):
        items = self._build_items()
        self.assertIn('confidence', items[0])
        # Default 0.0 — KI-Suggestion füllt Phase 6 mit echtem Wert
        self.assertEqual(items[0]['confidence'], 0.0)

    def test_review_item_schema_complete(self):
        """Alle Phase-5-Pflichtfelder vorhanden."""
        items = self._build_items()
        required = ['id', 'type', 'severity', 'datum', 'marker',
                    'activity_type', 'source_type', 'source_excerpt',
                    'why_not_resolved', 'suggested_answer', 'confidence',
                    'affected_days', 'question', 'options',
                    'money_impact_estimate', 'status', 'user_answer']
        for k in required:
            self.assertIn(k, items[0], f'Phase-5 Pflichtfeld {k} fehlt')


if __name__ == '__main__':
    unittest.main(verbosity=2)
