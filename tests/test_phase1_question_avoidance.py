"""Phase 1 — Cluster A (CAS-Zeit deterministisch) + Cluster C2 (SE-Override Z73/Z74→Z76).

Echte Logic-Tests: konstruieren matched-Day-Fixtures + rufen
`_deterministic_classify_v7` auf + prüfen reales Klassifikations-Output.

User-Anweisung 2026-05-14: „Tests nicht nur Static-String-Checks. Mindestens
einige echte Logic-/Fixture-Tests, die zeigen, dass die Klassifikation
wirklich anders rauskommt."
"""
import os
import re
import sys
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# conftest.py sorgt für AEROTAX_ALLOW_BOOT_WITHOUT_KEY=1
import app as app_module


# ─── Fixture-Helpers ─────────────────────────────────────────────────────────

def make_dp(datum, activity_type='office', **kwargs):
    """Konstruiert einen DP-Tag mit defaults (vorberichtet — _enrich überspringen).
    activity_type ∈ {office, training, tour, standby, frei, urlaub, krank, same_day}"""
    return {
        'datum': datum,
        'activity_type': activity_type,
        'routing':              kwargs.get('routing', ['FRA']),
        'layover_ort':          kwargs.get('layover_ort', ''),
        'layover_inland':       kwargs.get('layover_inland', None),
        'overnight_after_day':  kwargs.get('overnight_after_day', False),
        'start_time':           kwargs.get('start_time', ''),
        'end_time':             kwargs.get('end_time', ''),
        'duty_duration_minutes': kwargs.get('duty_duration_minutes', 0),
        'raw_marker':           kwargs.get('raw_marker', ''),
        'has_fl':               kwargs.get('has_fl', False),
        'is_workday':           kwargs.get('is_workday', True),
        'requires_commute':     kwargs.get('requires_commute', True),
        'starts_at_homebase':   kwargs.get('starts_at_homebase', True),
        'ends_at_homebase':     kwargs.get('ends_at_homebase', True),
        'raw_lines':            kwargs.get('raw_lines', []),
        'confidence':           kwargs.get('confidence', 0.9),
    }


def make_se(stfrei_total=0.0, stfrei_ort='', stfrei_inland=None,
            zwoelftel=0, count=0):
    return {
        'stfrei_total': float(stfrei_total),
        'stfrei_ort':   stfrei_ort,
        'stfrei_inland': stfrei_inland,
        'zwoelftel':    zwoelftel,
        'lines':        [],
        'count':        count,
    }


def make_matched(datum, dp_kwargs=None, se_kwargs=None):
    return {
        'datum': datum,
        'dp': make_dp(datum, **(dp_kwargs or {})),
        'se': make_se(**(se_kwargs or {})),
    }


def find_day(result, datum):
    """Hole tag_detail-Eintrag aus _deterministic_classify_v7 result."""
    for t in result.get('tage_detail', []):
        if t.get('datum') == datum:
            return t
    return None


# ─── Cluster A: CAS-Zeit deterministisch → kein review-item ─────────────────

class TestClusterA_CASTimePresent(unittest.TestCase):
    """Wenn CAS Uhrzeit hat, darf kein office_training_time_missing-Item entstehen."""

    def test_em_with_time_no_review_question(self):
        """EM mit start='07:30' end='11:00' duty=210 → KEIN review-item."""
        matched = [
            make_matched('2025-01-10', {
                'activity_type': 'training',
                'raw_marker':    'EM',
                'start_time':    '07:30',
                'end_time':      '11:00',
                'duty_duration_minutes': 210,
                'routing': ['FRA'],
            }),
        ]
        result = app_module._deterministic_classify_v7(matched, year=2025, homebase='FRA')
        items = result.get('_office_training_time_missing_candidates', []) \
            or result.get('office_training_time_missing_candidates', [])
        # Kein Item für 2025-01-10 (Zeit ist da)
        dates_with_items = [it['datum'] for it in items]
        self.assertNotIn('2025-01-10', dates_with_items,
            f'EM mit Zeit erzeugt fälschlich review-item: {items}')

    def test_em_with_time_uses_z72_logic_deterministically(self):
        """EM mit Zeit → klass nach 8h-Logic. 3:30h = <8h → Office, kein Z72."""
        matched = [
            make_matched('2025-01-10', {
                'activity_type': 'training',
                'raw_marker':    'EM',
                'start_time':    '07:30',
                'end_time':      '11:00',
                'duty_duration_minutes': 210,  # 3:30h
                'routing': ['FRA'],
            }),
        ]
        result = app_module._deterministic_classify_v7(matched, year=2025, homebase='FRA')
        d = find_day(result, '2025-01-10')
        self.assertIsNotNone(d)
        # 3:30h ist <8h → kein Z72
        self.assertEqual(d['klass'], 'Office',
            f"EM 3:30h sollte Office bleiben (kein Z72 unter 8h), got {d['klass']}")

    def test_standby_with_time_no_review_question(self):
        """SB_S mit Zeit 14:00-22:00 (8h) → KEIN review-item.
        Standby-Pfad ist eh nicht im office_training_cluster, aber als sanity-check."""
        matched = [
            make_matched('2025-02-01', {
                'activity_type': 'standby',
                'raw_marker':    'SB_S',
                'start_time':    '14:00',
                'end_time':      '22:00',
                'duty_duration_minutes': 480,
                'routing': ['FRA'],
            }),
        ]
        result = app_module._deterministic_classify_v7(matched, year=2025, homebase='FRA')
        items = result.get('_office_training_time_missing_candidates', []) \
            or result.get('office_training_time_missing_candidates', [])
        dates = [it['datum'] for it in items]
        self.assertNotIn('2025-02-01', dates,
            f'Standby mit Zeit erzeugt review-item: {items}')

    def test_flight_with_briefing_no_office_time_question(self):
        """Flugtag mit Briefing-Zeit → kein office_training-item (gehört zu tour-Pfad)."""
        matched = [
            make_matched('2025-03-15', {
                'activity_type': 'tour',
                'start_time':    '06:00',
                'end_time':      '14:00',
                'duty_duration_minutes': 480,
                'overnight_after_day': True,
                'layover_ort':   'LON',
                'has_fl':        True,
                'routing':       ['FRA', 'LON'],
            }),
            # 2. Tag damit Tour-Cluster funktioniert
            make_matched('2025-03-16', {
                'activity_type': 'tour',
                'start_time':    '08:00',
                'end_time':      '15:00',
                'duty_duration_minutes': 420,
                'overnight_after_day': False,
                'layover_ort':   '',
                'has_fl':        True,
                'routing':       ['LON', 'FRA'],
                'ends_at_homebase': True,
            }),
        ]
        result = app_module._deterministic_classify_v7(matched, year=2025, homebase='FRA')
        items = result.get('_office_training_time_missing_candidates', []) \
            or result.get('office_training_time_missing_candidates', [])
        dates = [it['datum'] for it in items]
        self.assertNotIn('2025-03-15', dates)
        self.assertNotIn('2025-03-16', dates)

    def test_office_without_time_creates_review_item(self):
        """Negativ-Test: office OHNE Zeit + ohne ORTSTAG-Marker → review-item entsteht."""
        matched = [
            make_matched('2025-04-01', {
                'activity_type': 'office',
                'raw_marker':    'OFFC',  # nicht ORTSTAG/FRS
                'start_time':    '',
                'end_time':      '',
                'duty_duration_minutes': 0,
                'routing': ['FRA'],
            }),
        ]
        result = app_module._deterministic_classify_v7(matched, year=2025, homebase='FRA')
        items = result.get('_office_training_time_missing_candidates', []) \
            or result.get('office_training_time_missing_candidates', [])
        dates = [it['datum'] for it in items]
        self.assertIn('2025-04-01', dates,
            f'Office OHNE Zeit + OHNE passive-Marker sollte review-item erzeugen, items: {items}')


# ─── Cluster C2: CAS-foreign-Layover schlägt SE-Inland-Stempel ──────────────

class TestClusterC2_SEOverride(unittest.TestCase):
    """SE-Stempel sagt Inland, CAS-Layover ist Ausland → CAS gewinnt → Z76."""

    def _build_foreign_tour(self, mid_day_datum, mid_layover, mid_se_stfrei_ort='MUC'):
        """Baut eine 4-tägige Auslandstour:
        Day 1: FRA→DEST1 (anreise, overnight)
        Day 2: <mid_day_datum> mit layover=<mid_layover>, SE stfrei_ort=<mid_se_stfrei_ort>
        Day 3: DEST2→DEST3 (mid)
        Day 4: Heimkehr
        Tour cluster ist foreign (overnight layovers nicht-inland).
        """
        return [
            make_matched('2025-09-25', {
                'activity_type': 'tour',
                'routing':       ['FRA', 'KRK'],
                'overnight_after_day': True,
                'layover_ort':   'KRK',  # Krakau = Polen = AUSLAND
                'start_time':    '08:00',
                'end_time':      '16:00',
                'duty_duration_minutes': 480,
                'has_fl':        True,
            }, se_kwargs={'count': 1, 'stfrei_total': 30.0, 'stfrei_ort': 'KRK',
                          'stfrei_inland': False}),
            make_matched(mid_day_datum, {
                'activity_type': 'tour',
                'routing':       ['KRK', mid_layover],
                'overnight_after_day': True,
                'layover_ort':   mid_layover,
                'start_time':    '09:00',
                'end_time':      '15:00',
                'duty_duration_minutes': 360,
                'has_fl':        True,
            }, se_kwargs={'count': 1, 'stfrei_total': 14.0,
                          'stfrei_ort': mid_se_stfrei_ort,
                          'stfrei_inland': True}),  # SE-Stempel INLAND
            make_matched('2025-09-28', {
                'activity_type': 'tour',
                'routing':       [mid_layover, 'FRA'],
                'overnight_after_day': False,
                'layover_ort':   '',
                'start_time':    '10:00',
                'end_time':      '18:00',
                'duty_duration_minutes': 480,
                'has_fl':        True,
                'ends_at_homebase': True,
            }, se_kwargs={'count': 1, 'stfrei_total': 30.0,
                          'stfrei_ort': mid_layover,
                          'stfrei_inland': False}),
        ]

    def test_se_foreign_overrides_z74_to_z76(self):
        """09-26 layover='IST' (Türkei), SE stfrei_ort='MUC' inland=True
        → klass=Z76 (statt Z74)."""
        matched = self._build_foreign_tour('2025-09-26', 'IST', mid_se_stfrei_ort='MUC')
        result = app_module._deterministic_classify_v7(matched, year=2025, homebase='FRA')
        d = find_day(result, '2025-09-26')
        self.assertIsNotNone(d)
        self.assertEqual(d['klass'], 'Z76',
            f"Expected Z76 (CAS-Layover IST überstimmt SE-Stempel MUC), got klass={d['klass']} reason={d.get('begruendung','')[:200]}")
        # Reason-Text muss den Override-Hinweis enthalten
        reason = d.get('begruendung', '') or d.get('classifier_result', {}).get('reason', '')
        self.assertIn('IST', reason)

    def test_se_foreign_overrides_z73_to_z76(self):
        """09-27-style Anreise/Abreise mit layover='AGP' (Spanien), SE stfrei_ort='DUS' inland=True
        → klass=Z76."""
        # 09-27 ist im Original „Z73 An/Ab" — wir bauen das Setup so dass der Tag
        # ein Mid-Day mit is_anreise/is_abreise=False ist. Anreise ist Tag 1.
        # AGP-Variante: Tour-mid mit AGP-layover.
        matched = self._build_foreign_tour('2025-09-27', 'AGP', mid_se_stfrei_ort='DUS')
        result = app_module._deterministic_classify_v7(matched, year=2025, homebase='FRA')
        d = find_day(result, '2025-09-27')
        self.assertIsNotNone(d)
        self.assertEqual(d['klass'], 'Z76',
            f"Expected Z76 (CAS AGP überstimmt SE DUS), got klass={d['klass']}")

    def test_se_foreign_override_no_double_count(self):
        """Override-Tag taucht genau 1× in tage_detail auf + 1× im Z76-Topf."""
        matched = self._build_foreign_tour('2025-09-26', 'IST', mid_se_stfrei_ort='MUC')
        result = app_module._deterministic_classify_v7(matched, year=2025, homebase='FRA')
        # Genau 1× in tage_detail
        days_2025_09_26 = [t for t in result.get('tage_detail', []) if t.get('datum') == '2025-09-26']
        self.assertEqual(len(days_2025_09_26), 1,
            f'Day 2025-09-26 sollte genau 1× in tage_detail sein, got {len(days_2025_09_26)}')
        # Rescue-entry: nur 1× (kein doppelter rescues.append)
        rescues = result.get('_rescues', []) or result.get('rescues', [])
        c2_rescues_2026 = [r for r in rescues
                           if r.get('datum') == '2025-09-26'
                           and r.get('rescue_type') == 'cas_foreign_layover_over_se_inland_stamp']
        self.assertLessEqual(len(c2_rescues_2026), 1,
            f'Cluster-C2 rescue darf max 1× pro Tag sein, got {len(c2_rescues_2026)}')

    def test_se_inland_does_not_force_z76_at_homebase(self):
        """SE-Inland + CAS-Layover='FRA' (Homebase) → KEIN Z76-Override
        (FRA-Homebase-Stempel auf Auslandstour ist ein existing-pattern,
        nicht ein neuer Cluster-C2-Case)."""
        # Setup: 2-tägige Tour, mid-day mit layover='FRA' (Homebase, nicht foreign)
        matched = [
            make_matched('2025-09-25', {
                'activity_type': 'tour',
                'routing': ['FRA', 'KRK'],
                'overnight_after_day': True,
                'layover_ort': 'KRK',
                'has_fl': True,
                'start_time': '08:00', 'end_time': '16:00',
                'duty_duration_minutes': 480,
            }, se_kwargs={'count': 1, 'stfrei_total': 30.0, 'stfrei_ort': 'KRK',
                          'stfrei_inland': False}),
            make_matched('2025-09-26', {
                'activity_type': 'tour',
                'routing': ['KRK', 'FRA'],
                'overnight_after_day': True,
                'layover_ort': 'FRA',  # Homebase
                'has_fl': True,
                'start_time': '10:00', 'end_time': '14:00',
                'duty_duration_minutes': 240,
            }, se_kwargs={'count': 1, 'stfrei_total': 14.0, 'stfrei_ort': 'FRA',
                          'stfrei_inland': True}),
            make_matched('2025-09-27', {
                'activity_type': 'tour',
                'routing': ['FRA'],
                'overnight_after_day': False,
                'layover_ort': '',
                'has_fl': False,
                'ends_at_homebase': True,
            }),
        ]
        result = app_module._deterministic_classify_v7(matched, year=2025, homebase='FRA')
        d = find_day(result, '2025-09-26')
        self.assertIsNotNone(d)
        # NICHT Cluster-C2-Override (layover=FRA = Homebase, NICHT foreign-target)
        # Erwartet: bestehender Z76-Pfad mit „Homebase-Stempel" — Z76 ja, aber NICHT
        # via cluster_c2-rescue
        rescues = result.get('_rescues', []) or result.get('rescues', [])
        c2_rescues = [r for r in rescues if r.get('rescue_type') == 'cas_foreign_layover_over_se_inland_stamp']
        c2_for_today = [r for r in c2_rescues if r.get('datum') == '2025-09-26']
        self.assertEqual(c2_for_today, [],
            f'Homebase-Layover darf NICHT als Cluster-C2-Override behandelt werden, got {c2_for_today}')

    def test_pure_inland_layover_stays_z73_or_z74(self):
        """Reiner Inland-Cluster (DUS-Mid-Tour): Z73/Z74 bleibt erhalten, kein C2-Override."""
        matched = [
            make_matched('2025-06-10', {
                'activity_type': 'tour',
                'routing': ['FRA', 'MUC'],
                'overnight_after_day': True,
                'layover_ort': 'MUC',  # Inland
                'has_fl': True,
                'start_time': '08:00', 'end_time': '16:00',
                'duty_duration_minutes': 480,
            }, se_kwargs={'count': 1, 'stfrei_total': 14.0, 'stfrei_ort': 'MUC',
                          'stfrei_inland': True}),
            make_matched('2025-06-11', {
                'activity_type': 'tour',
                'routing': ['MUC', 'DUS'],
                'overnight_after_day': True,
                'layover_ort': 'DUS',  # Inland
                'has_fl': True,
                'start_time': '10:00', 'end_time': '14:00',
                'duty_duration_minutes': 240,
            }, se_kwargs={'count': 1, 'stfrei_total': 14.0, 'stfrei_ort': 'DUS',
                          'stfrei_inland': True}),
            make_matched('2025-06-12', {
                'activity_type': 'tour',
                'routing': ['DUS', 'FRA'],
                'overnight_after_day': False,
                'layover_ort': '',
                'has_fl': True,
                'ends_at_homebase': True,
                'start_time': '08:00', 'end_time': '12:00',
                'duty_duration_minutes': 240,
            }),
        ]
        result = app_module._deterministic_classify_v7(matched, year=2025, homebase='FRA')
        d = find_day(result, '2025-06-11')
        self.assertIsNotNone(d)
        # Pure Inland-Cluster: klass darf NICHT Z76 sein
        self.assertNotEqual(d['klass'], 'Z76',
            f'Pure Inland (DUS-Layover, Cluster Inland) darf NICHT Z76 sein, got klass={d["klass"]}')


# ─── Static-Source-Checks (Backup) ──────────────────────────────────────────

class TestSourceInvariants(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.src = open(os.path.join(ROOT_DIR, 'app.py')).read()

    def test_has_time_evidence_used_in_review_skip(self):
        """Cluster A: has_time_evidence_rev wird im Review-Skip-Check genutzt."""
        self.assertIn('has_time_evidence_rev', self.src)
        # Check muss start_time / end_time einschließen
        self.assertRegex(
            self.src,
            r'has_time_evidence_rev\s*=[\s\S]{0,500}start_time[\s\S]{0,500}end_time',
            'has_time_evidence_rev muss start_time UND end_time prüfen'
        )

    def test_cluster_c2_override_block_present(self):
        """Cluster C2: Override-Block ist im Source."""
        self.assertIn('v15 Phase-1 Cluster C2', self.src)
        self.assertIn('cas_foreign_layover_over_se_inland_stamp', self.src)


if __name__ == '__main__':
    unittest.main(verbosity=2)
