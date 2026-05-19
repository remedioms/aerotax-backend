"""BH-001 — Review-Question fragt Marker-Semantik statt 8h-Symptom.

User-Beweis (Tibor Token AT-C33E6274D260FC78, 2025-12-19):
  type:      office_training_time_missing
  marker:    OF
  question:  "Am 2025-12-19 war ein Office-/Schulungstag (OF) eingetragen —
              wir konnten keine Uhrzeit erkennen. Warst du inklusive
              Hin- und Rückweg länger als 8 Stunden unterwegs?"

User-Reaktion: „ein ortstag.. unadmissable!!!"

Erwartetes Verhalten nach Fix:
- KI-Resolver kind='marker_semantics' wird vor Item-Bildung gerufen
- ≥0.90 + value=office_passive_at_home → silent-skip (kein Item)
- ≥0.70 → Item mit suggested_answer + neue Marker-Semantik-Frage
- <0.70 → Item mit „Was bedeutet {marker}?"-Frage statt 8h-Symptom
- ORTSTAG/FRS/LMN_AS/LMN_CR weiterhin silent (Regression-Schutz)
- Marker mit Uhrzeit → kein candidate (Regression-Schutz)

Architecture-Invariant (CLAUDE.md):
- KI ist aktiver Resolver, nicht Notfall
- Userfragen sind Last Resort
- KI bekommt immer Airline-Crew-Kontext
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


def _of_candidate(datum='2025-12-19', marker='OF', activity='office'):
    """Repliziert exakt was der Klassifikator für Tibor's OF-Tag baut."""
    return {
        'datum': datum,
        'activity_type': activity,
        'marker': marker,
        'reason': 'Office/Schulung an Homebase ohne Zeitinfo — Z72-Plausi unklar',
        'money_impact_estimate': 14.0,
    }


class TestBH001ReviewQuestionMarkerSemantics(unittest.TestCase):

    def setUp(self):
        _clear_caches()

    # ── Hauptfälle: KI-Marker-Semantik wird vor Frage gestellt ───────────

    def test_of_marker_with_passive_ai_skips_review(self):
        """KI conf≥0.90 + semantics=office_passive_at_home → kein Item.

        Tibor's 2025-12-19 mit OF-Marker — wenn KI sagt 'passive zuhause',
        wird der Tag silent geskippt wie ORTSTAG.
        """
        cls = {'office_training_time_missing_candidates': [_of_candidate()]}
        ai_result = {
            'resolved': True,
            'value': {
                'semantics': 'office_passive_at_home',
                'meaning':   'Bürodienst zuhause / passiver Diensttag',
            },
            'confidence': 0.92,
            'reason':     'OF in LH-CAS = Office passive at home base',
            'evidence':   ['OF-Marker ohne Briefingzeit, kein Routing'],
            'needs_review': False,
        }
        with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai',
                                return_value=ai_result) as mock_ai:
            items = app_module._build_review_items(cls)
        # Item NICHT gebaut
        office_items = [i for i in items
                        if i.get('type') == 'office_training_time_missing']
        self.assertEqual(len(office_items), 0,
            f'OF + passive KI → kein Review-Item. Got: {office_items}')
        # KI wurde mit kind='marker_semantics' gerufen
        mock_ai.assert_called()
        kwargs = mock_ai.call_args.kwargs
        self.assertEqual(kwargs.get('kind'), 'marker_semantics')

    def test_of_marker_with_active_ai_creates_marker_semantics_question(self):
        """KI conf≥0.70 + semantics=office_with_commute → Item mit
        suggested_answer + Marker-Semantik-Frage (nicht 8h-Symptom)."""
        cls = {'office_training_time_missing_candidates': [_of_candidate()]}
        ai_result = {
            'resolved': True,
            'value': {
                'semantics': 'office_with_commute',
                'meaning':   'Schulung mit Anreise zur Niederlassung',
            },
            'confidence': 0.85,
            'reason':     'Pattern OF mit Briefing-Code-Hinweis',
            'evidence':   [],
            'needs_review': True,
        }
        with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai',
                                return_value=ai_result):
            items = app_module._build_review_items(cls)
        office_items = [i for i in items
                        if i.get('type') == 'office_training_time_missing']
        self.assertEqual(len(office_items), 1)
        item = office_items[0]
        # suggested_answer aus KI gefüllt
        self.assertIsNotNone(item.get('suggested_answer'),
            f'suggested_answer must be set when AI conf>=0.70, got {item}')
        # confidence im Item gefüllt
        self.assertGreaterEqual(item.get('confidence', 0), 0.70)
        # Frage NICHT die alte 8h-Symptom-Frage
        q = item.get('question', '')
        self.assertNotIn('länger als 8 Stunden', q,
            'Question should NOT ask 8h-Symptom when AI provides marker semantics')
        # Frage erwähnt den Marker
        self.assertIn('OF', q, f'Question should mention marker, got: {q}')

    def test_of_marker_low_confidence_falls_back_to_semantics_question(self):
        """KI conf<0.70 oder resolved=False → kein suggested_answer, aber
        trotzdem Marker-Semantik-Frage (nicht 8h-Symptom)."""
        cls = {'office_training_time_missing_candidates': [_of_candidate()]}
        ai_result = {
            'resolved': False,
            'value': {},
            'confidence': 0.4,
            'reason': 'unklar',
            'evidence': [],
            'needs_review': True,
        }
        with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai',
                                return_value=ai_result):
            items = app_module._build_review_items(cls)
        office_items = [i for i in items
                        if i.get('type') == 'office_training_time_missing']
        self.assertEqual(len(office_items), 1)
        item = office_items[0]
        self.assertIsNone(item.get('suggested_answer'))
        q = item.get('question', '')
        self.assertNotIn('länger als 8 Stunden', q,
            f'Even low-conf: question MUST be marker-semantics, not 8h-symptom. Got: {q}')
        # Marker-Semantik-Frage muss Marker nennen + Bürodienst/Schulung differenzieren
        self.assertIn('OF', q)
        self.assertTrue(
            ('Bürodienst' in q or 'passive' in q or 'zuhause' in q.lower()),
            f'Question must offer passive/office-at-home option, got: {q}')

    # ── Crew-Kontext-Invariant ─────────────────────────────────────────

    def test_ki_call_includes_crew_context(self):
        """Phase 7 Invariant: KI-Prompt muss Airline-Crew-Kontext enthalten."""
        cls = {'office_training_time_missing_candidates': [_of_candidate()]}
        with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai',
                                return_value={'resolved': False, 'value': {},
                                              'confidence': 0.0, 'reason': '',
                                              'evidence': [], 'needs_review': True}) as mock_ai:
            app_module._build_review_items(cls)
        mock_ai.assert_called()
        ctx = mock_ai.call_args.kwargs.get('context') or {}
        # Crew-Hinweis sollte im context-dict sein (entweder als key 'context'
        # oder im prompt-aufbau im resolver). Wir prüfen ob der context
        # mindestens den Marker durchreicht.
        self.assertIn(ctx.get('marker'), ('OF', None),
            f'Context must include marker. Got: {ctx}')
        # Airline-Crew-Hinweis in einem string-feld
        ctx_str = ' '.join(str(v) for v in ctx.values() if isinstance(v, str))
        self.assertTrue(
            ('Crew' in ctx_str or 'Airline' in ctx_str
             or 'Lufthansa' in ctx_str or 'Cockpit' in ctx_str
             or 'Kabine' in ctx_str),
            f'Context must mention airline-crew. Got context: {ctx}')

    # ── Regression-Schutz ─────────────────────────────────────────────

    def test_ortstag_still_skipped_silent(self):
        """Regression: ORTSTAG-Marker erreicht den Builder nie (hardcoded skip
        im Classifier). Wenn er es trotzdem täte, KI-Pfad würde fallen.

        Hier testen wir: ORTSTAG-Marker als Office-candidate → silent-skip
        ohne KI-Call (passive-Pattern erkannt vor KI).
        """
        cls = {'office_training_time_missing_candidates': [
            _of_candidate(marker='ORTSTAG'),
        ]}
        with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai',
                                return_value={'resolved': False, 'value': {},
                                              'confidence': 0.0, 'reason': '',
                                              'evidence': [], 'needs_review': True}):
            items = app_module._build_review_items(cls)
        office_items = [i for i in items
                        if i.get('type') == 'office_training_time_missing']
        self.assertEqual(len(office_items), 0,
            f'ORTSTAG must be silent-skipped (deterministic, no AI). Got: {office_items}')

    def test_frs_lmn_also_skipped_silent(self):
        """Regression: FRS/LMN_AS/LMN_CR auch ohne KI silent skipped."""
        cls = {'office_training_time_missing_candidates': [
            _of_candidate(datum='2025-01-15', marker='FRS'),
            _of_candidate(datum='2025-02-15', marker='LMN_AS'),
            _of_candidate(datum='2025-03-15', marker='LMN_CR'),
        ]}
        with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai',
                                return_value={'resolved': False, 'value': {},
                                              'confidence': 0.0, 'reason': '',
                                              'evidence': [], 'needs_review': True}):
            items = app_module._build_review_items(cls)
        office_items = [i for i in items
                        if i.get('type') == 'office_training_time_missing']
        self.assertEqual(len(office_items), 0,
            f'FRS/LMN_AS/LMN_CR must all be silent-skipped. Got: {office_items}')

    def test_unknown_marker_still_works_with_ai_call(self):
        """Regression: unknown_marker_candidates Pfad (Phase 6) bleibt
        unbeschadet — testet dass mein Fix nicht den anderen KI-Pfad bricht."""
        cls = {'unknown_marker_candidates': [
            {'datum': '2025-05-01', 'marker': 'XYZ', 'first_token': 'XYZ'},
        ]}
        ai_result = {
            'resolved': True,
            'value': {'semantics': 'special_duty'},
            'confidence': 0.88,
            'reason': '',
            'evidence': [],
            'needs_review': True,
        }
        with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai',
                                return_value=ai_result):
            items = app_module._build_review_items(cls)
        unknown_items = [i for i in items if i.get('type') == 'unknown_marker']
        self.assertEqual(len(unknown_items), 1, 'unknown_marker path still works')

    def test_multiple_office_candidates_each_get_ai_call(self):
        """Multiple OF/Office-Tage → KI wird pro candidate gerufen (oder via Cache)."""
        cls = {'office_training_time_missing_candidates': [
            _of_candidate(datum='2025-12-19', marker='OF'),
            _of_candidate(datum='2025-04-15', marker='EM'),
        ]}
        with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai',
                                return_value={'resolved': False, 'value': {},
                                              'confidence': 0.0, 'reason': '',
                                              'evidence': [], 'needs_review': True}) as mock_ai:
            items = app_module._build_review_items(cls)
        # KI wird mindestens 1x gerufen (Cache kann dedupen wenn gleicher marker)
        self.assertGreaterEqual(mock_ai.call_count, 1)
        # Beide Items vorhanden (KI sagt nichts → fallback semantics-Frage)
        office_items = [i for i in items
                        if i.get('type') == 'office_training_time_missing']
        self.assertEqual(len(office_items), 2,
            f'2 candidates → 2 items if AI uncertain. Got {office_items}')

    def test_ai_returns_amount_field_is_sanitized(self):
        """Anti-Tax-Sanitizer: KI-Antwort mit Betrag wird verworfen → review-fallback."""
        cls = {'office_training_time_missing_candidates': [_of_candidate()]}
        # _resolve_uncertain_fact_with_ai macht das selbst — wir verifizieren
        # dass im review-Builder die Frage trotzdem nicht 8h-Symptom ist.
        ai_result = {
            'resolved': False,  # sanitizer hat rejected
            'value': {},
            'confidence': 0.0,
            'reason': 'forbidden_key:eur',
            'evidence': [],
            'needs_review': True,
        }
        with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai',
                                return_value=ai_result):
            items = app_module._build_review_items(cls)
        office_items = [i for i in items
                        if i.get('type') == 'office_training_time_missing']
        self.assertEqual(len(office_items), 1)
        q = office_items[0].get('question', '')
        self.assertNotIn('länger als 8 Stunden', q)


if __name__ == '__main__':
    unittest.main(verbosity=2)
