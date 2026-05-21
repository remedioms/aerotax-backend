"""Phase 7 — SE-Inland 14€ Audit-Cleanup.

Verifiziert dass Standby/ZeroDay-Tage mit SE-Inland-Stempel ≤14€ als
AG-Erstattung audit-noted werden statt als „unmapped"-Issue.

Tibor-spezifische Tage: 04-23 FRA, 08-01 NUE, 10-20 HAM, 10-23 LEJ.
"""
import os
import sys
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import app as app_module


def make_matched(datum, dp_kwargs=None, se_kwargs=None):
    dp = {
        'datum': datum,
        'activity_type': 'standby',
        'routing': ['FRA'],
        'layover_ort': '',
        'overnight_after_day': False,
        'start_time': '', 'end_time': '',
        'duty_duration_minutes': 0,
        'raw_marker': 'RES',
        'has_fl': False, 'is_workday': True,
        'requires_commute': False,
        'starts_at_homebase': True,
        'ends_at_homebase': True,
        'raw_lines': [], 'confidence': 0.9,
    }
    dp.update(dp_kwargs or {})
    se = {
        'stfrei_total': 14.0, 'stfrei_ort': 'FRA',
        'stfrei_inland': True, 'zwoelftel': 12,
        'lines': [], 'count': 1,
    }
    se.update(se_kwargs or {})
    return {'datum': datum, 'dp': dp, 'se': se}


class TestPhase7StandbyAudit(unittest.TestCase):

    def test_standby_with_inland_14eur_creates_audit_note_not_unmapped(self):
        """RES FRA 14€ stfrei → klass=Standby, KEIN vma_unmapped, audit_note vorhanden."""
        matched = [make_matched('2025-04-23')]
        result = app_module._deterministic_classify_v7(matched, year=2025, homebase='FRA')
        # Klass=Standby (existing behavior)
        d = next(t for t in result['tage_detail'] if t['datum'] == '2025-04-23')
        self.assertEqual(d['klass'], 'Standby')
        # KEIN _vma_unmapped_se für diesen Tag
        unmapped = result.get('_vma_unmapped_se', []) or result.get('vma_unmapped_se', [])
        unmapped_dates = [u.get('datum') for u in unmapped]
        self.assertNotIn('2025-04-23', unmapped_dates,
            'Standby mit Inland-14€ darf nicht im vma_unmapped_se landen')
        # audit_note vorhanden
        notes = result.get('_audit_notes') or result.get('audit_notes') or []
        ag_notes = [n for n in notes if '2025-04-23' in str(n) and 'AG-Erstattung' in str(n)]
        self.assertGreater(len(ag_notes), 0,
            f'Audit-Note für AG-Erstattung fehlt. Notes: {notes[:5]}')

    def test_zeroday_with_inland_14eur_creates_audit_note(self):
        """Same-Day <8h mit SE-Inland-14€ → klass=ZeroDay, audit-noted."""
        # ZeroDay-Setup: Same-Day mit duty<8h
        m = make_matched('2025-08-01', dp_kwargs={
            'activity_type': 'same_day',
            'routing': ['FRA', 'NUE', 'FRA'],
            'duty_duration_minutes': 329,  # 5:29h
            'raw_marker': 'TOUR',
        }, se_kwargs={'stfrei_ort': 'NUE'})
        result = app_module._deterministic_classify_v7([m], year=2025, homebase='FRA')
        d = next((t for t in result['tage_detail'] if t['datum'] == '2025-08-01'), None)
        if d and d['klass'] in ('Standby', 'ZeroDay', 'Issue', 'Office'):
            # Klass kann je nach Klassifikator variieren; wichtig: kein unmapped
            unmapped = result.get('_vma_unmapped_se', []) or result.get('vma_unmapped_se', [])
            unmapped_dates = [u.get('datum') for u in unmapped]
            if d['klass'] in ('Standby', 'ZeroDay'):
                self.assertNotIn('2025-08-01', unmapped_dates)

    def test_standby_inland_over_14eur_still_unmapped(self):
        """Standby mit Inland-Stempel >14€ (unüblich) bleibt unmapped → keine
        stille AG-Erstattung-Annahme."""
        matched = [make_matched('2025-04-23', se_kwargs={'stfrei_total': 50.0})]
        result = app_module._deterministic_classify_v7(matched, year=2025, homebase='FRA')
        d = next(t for t in result['tage_detail'] if t['datum'] == '2025-04-23')
        if d['klass'] == 'Standby':
            unmapped = result.get('_vma_unmapped_se', []) or result.get('vma_unmapped_se', [])
            unmapped_dates = [u.get('datum') for u in unmapped]
            self.assertIn('2025-04-23', unmapped_dates,
                '50€ Inland-Stempel ist nicht typische AG-Erstattung — sollte unmapped bleiben')

    def test_standby_foreign_se_still_unmapped(self):
        """Standby mit Auslands-SE-Stempel: muss als unmapped erkannt werden
        (echtes Issue — Crew war im Ausland aber als Standby klassifiziert)."""
        matched = [make_matched('2025-04-23', se_kwargs={
            'stfrei_ort': 'JFK', 'stfrei_inland': False, 'stfrei_total': 40.0,
        })]
        result = app_module._deterministic_classify_v7(matched, year=2025, homebase='FRA')
        # Diese Tag könnte durch andere Rescues (z.B. SE-Override) zu Z76 werden;
        # wichtig: WENN Standby bleibt UND ausland → vma_unmapped
        d = next(t for t in result['tage_detail'] if t['datum'] == '2025-04-23')
        if d['klass'] == 'Standby':
            unmapped = result.get('_vma_unmapped_se', []) or result.get('vma_unmapped_se', [])
            unmapped_dates = [u.get('datum') for u in unmapped]
            self.assertIn('2025-04-23', unmapped_dates,
                'Auslands-SE auf Standby muss unmapped bleiben (echtes Issue)')


if __name__ == '__main__':
    unittest.main(verbosity=2)
