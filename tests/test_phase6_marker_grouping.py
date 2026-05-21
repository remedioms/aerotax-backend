"""Phase 6 — Marker-Semantik-Resolver + Gruppen-Frage.

Verifiziert:
- unknown_marker_candidates werden nach first_token gruppiert
- KI-Marker-Semantik-Resolver wird aufgerufen mit Crew-Kontext
- suggested_answer gefüllt wenn KI conf≥0.70
- Gleiche markers an N Tagen → 1 Item statt N
"""
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


class TestMarkerGrouping(unittest.TestCase):

    def setUp(self):
        _clear_caches()

    def test_same_marker_28_days_creates_one_group_item(self):
        """28 unknown_marker_candidates mit gleichem first_token → 1 Item."""
        cls = {'unknown_marker_candidates': [
            {'datum': f'2025-01-{d:02d}', 'marker': 'ZZZ-NEW',
             'first_token': 'ZZZ-NEW'}
            for d in range(1, 29)  # 28 Tage
        ]}
        with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai',
                                return_value={'resolved': False, 'value': {},
                                              'confidence': 0.0, 'reason': '',
                                              'evidence': [], 'needs_review': True}):
            items = app_module._build_review_items(cls)
        # Genau 1 Item für unknown_marker (gruppiert)
        unknown_items = [i for i in items if i['type'] == 'unknown_marker']
        self.assertEqual(len(unknown_items), 1)
        self.assertEqual(len(unknown_items[0]['affected_days']), 28)
        self.assertTrue(unknown_items[0]['id'].startswith('unknown_marker:group:'))

    def test_different_markers_create_separate_groups(self):
        """3 verschiedene first_tokens → 3 separate Items."""
        cls = {'unknown_marker_candidates': [
            {'datum': '2025-01-01', 'marker': 'AAA', 'first_token': 'AAA'},
            {'datum': '2025-01-02', 'marker': 'AAA', 'first_token': 'AAA'},
            {'datum': '2025-01-03', 'marker': 'BBB', 'first_token': 'BBB'},
            {'datum': '2025-01-04', 'marker': 'CCC', 'first_token': 'CCC'},
        ]}
        with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai',
                                return_value={'resolved': False, 'value': {},
                                              'confidence': 0.0, 'reason': '',
                                              'evidence': [], 'needs_review': True}):
            items = app_module._build_review_items(cls)
        unknown = [i for i in items if i['type'] == 'unknown_marker']
        self.assertEqual(len(unknown), 3)
        tokens = sorted(i['first_token'] for i in unknown)
        self.assertEqual(tokens, ['AAA', 'BBB', 'CCC'])

    def test_ai_marker_semantics_called_with_crew_context(self):
        """KI wird mit kind='marker_semantics' aufgerufen."""
        cls = {'unknown_marker_candidates': [
            {'datum': '2025-01-01', 'marker': 'XYZ', 'first_token': 'XYZ'},
        ]}
        with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai',
                                return_value={'resolved': True,
                                              'value': {'semantics': 'office'},
                                              'confidence': 0.85,
                                              'reason': '', 'evidence': [],
                                              'needs_review': True}) as mock_ai:
            app_module._build_review_items(cls)
        mock_ai.assert_called()
        self.assertEqual(mock_ai.call_args.kwargs.get('kind'), 'marker_semantics')

    def test_ai_high_confidence_fills_suggested_answer(self):
        """KI conf≥0.70 → suggested_answer im Item gefüllt."""
        cls = {'unknown_marker_candidates': [
            {'datum': '2025-01-01', 'marker': 'XYZ', 'first_token': 'XYZ'},
        ]}
        ai_result = {
            'resolved': True,
            'value': {'semantics': 'Bürodienst-passive', 'meaning': 'Office at home'},
            'confidence': 0.85,
            'reason': 'XYZ in LH-Crew = Office', 'evidence': [],
            'needs_review': True,
        }
        with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai',
                                return_value=ai_result):
            items = app_module._build_review_items(cls)
        u = [i for i in items if i['type'] == 'unknown_marker'][0]
        self.assertEqual(u['suggested_answer'], 'Bürodienst-passive')
        self.assertEqual(u['confidence'], 0.85)

    def test_ai_low_confidence_no_suggestion(self):
        """KI conf<threshold + resolved=False → suggested_answer=None."""
        cls = {'unknown_marker_candidates': [
            {'datum': '2025-01-01', 'marker': 'XYZ', 'first_token': 'XYZ'},
        ]}
        ai_result = {
            'resolved': False, 'value': {}, 'confidence': 0.3,
            'reason': 'unklar', 'evidence': [], 'needs_review': True,
        }
        with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai',
                                return_value=ai_result):
            items = app_module._build_review_items(cls)
        u = [i for i in items if i['type'] == 'unknown_marker'][0]
        self.assertIsNone(u['suggested_answer'])

    def test_grouped_marker_status_marker_override(self):
        """`_marker:TOKEN` Override → group ist 'answered'."""
        cls = {'unknown_marker_candidates': [
            {'datum': '2025-01-01', 'marker': 'XYZ', 'first_token': 'XYZ'},
            {'datum': '2025-01-02', 'marker': 'XYZ', 'first_token': 'XYZ'},
        ]}
        overrides = {'_marker:XYZ': 'office'}
        with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai',
                                return_value={'resolved': False, 'value': {},
                                              'confidence': 0.0, 'reason': '',
                                              'evidence': [], 'needs_review': True}):
            items = app_module._build_review_items(cls, manual_day_overrides=overrides)
        u = [i for i in items if i['type'] == 'unknown_marker'][0]
        self.assertEqual(u['status'], 'answered')

    def test_grouped_marker_question_says_n_times(self):
        """Bei N>1: Frage erwähnt die Anzahl der Vorkommen."""
        cls = {'unknown_marker_candidates': [
            {'datum': f'2025-01-{d:02d}', 'marker': 'BUDDY',
             'first_token': 'BUDDY'} for d in range(1, 6)
        ]}
        with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai',
                                return_value={'resolved': False, 'value': {},
                                              'confidence': 0.0, 'reason': '',
                                              'evidence': [], 'needs_review': True}):
            items = app_module._build_review_items(cls)
        u = [i for i in items if i['type'] == 'unknown_marker'][0]
        self.assertIn('5×', u['question'])
        self.assertIn('BUDDY', u['question'])

    def test_single_marker_keeps_individual_question(self):
        """N=1: Einzelfrage (kein „N×")."""
        cls = {'unknown_marker_candidates': [
            {'datum': '2025-01-01', 'marker': 'ONE', 'first_token': 'ONE'},
        ]}
        with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai',
                                return_value={'resolved': False, 'value': {},
                                              'confidence': 0.0, 'reason': '',
                                              'evidence': [], 'needs_review': True}):
            items = app_module._build_review_items(cls)
        u = [i for i in items if i['type'] == 'unknown_marker'][0]
        self.assertNotIn('×', u['question'])
        self.assertIn('2025-01-01', u['question'])

    def test_marker_id_uses_group_prefix(self):
        """Group-Item hat id mit ':group:' prefix."""
        cls = {'unknown_marker_candidates': [
            {'datum': '2025-01-01', 'marker': 'GRP', 'first_token': 'GRP'},
            {'datum': '2025-01-02', 'marker': 'GRP', 'first_token': 'GRP'},
        ]}
        with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai',
                                return_value={'resolved': False, 'value': {},
                                              'confidence': 0.0, 'reason': '',
                                              'evidence': [], 'needs_review': True}):
            items = app_module._build_review_items(cls)
        u = [i for i in items if i['type'] == 'unknown_marker'][0]
        self.assertIn(':group:GRP', u['id'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
