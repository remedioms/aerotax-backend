"""BH-003a — Echte Tour-Heimkehr-Tage Issue → Z76 An/Ab.

User-Beweis (Tibor Bangalore-Tour 2025-01-03 bis 2025-01-06):
  Golden:  01-06 = Z76 Indien-Bangalore 28€ (is_abreise=True, pos 4/4)
  IST:     01-06 = Issue mit reason="Heimkehr aus Vortag-Tour"

Discriminator gegen falsche-positive (05-23/06-03/10-28 Frei-Tage):
  duty_duration_minutes >= 480  (8h echte Abwesenheit)
  + routing[0] == prev.layover_ort  (Direkt-Rückflug)
  + routing[-1] == homebase
  + ends_at_homebase=True
  + prev.layover_ort != Inland

Quelle: tests/fixtures/tibor_aerotax_v11_raw_initial.json
Branch in Code: app.py `_deterministic_classify_v7` same_day + prev_overnight + keine
                aktive Auslands-SE → Issue. Wird BH-003a-Guard vor Issue-Fallback geprüft.
"""
import os
import sys
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import app as app_module


def _make_day(datum, dp_kwargs=None, se_kwargs=None):
    """Liefert ein matched-day-dict im Format das _deterministic_classify_v7 erwartet."""
    dp = {
        'datum': datum,
        'activity_type': 'tour',
        'routing': [],
        'layover_ort': '',
        'overnight_after_day': False,
        'start_time': '', 'end_time': '',
        'duty_duration_minutes': 0,
        'raw_marker': '',
        'has_fl': False, 'is_workday': True,
        'requires_commute': False,
        'starts_at_homebase': False,
        'ends_at_homebase': False,
        'raw_lines': [], 'confidence': 0.9,
    }
    dp.update(dp_kwargs or {})
    se = {
        'stfrei_total': 0.0,
        'stfrei_ort':   '',
        'stfrei_inland': None,
        'zwoelftel':    0,
        'lines':        [],
        'count':        0,
    }
    se.update(se_kwargs or {})
    return {'datum': datum, 'dp': dp, 'se': se}


def _bangalore_tour():
    """Repliziert Tibor's Bangalore-Tour 01-03 bis 01-06 aus der fixture."""
    return [
        # 01-03 Tour-Start An (FRA→BLR)
        _make_day('2025-01-03', dp_kwargs={
            'activity_type': 'tour',
            'routing': ['FRA', 'BLR'],
            'layover_ort': 'BLR',
            'overnight_after_day': True,
            'starts_at_homebase': True, 'ends_at_homebase': False,
            'start_time': '10:55', 'end_time': '23:59',
            'duty_duration_minutes': 785,
            'raw_marker': '31591 P1',
            'has_fl': True, 'requires_commute': True,
        }),
        # 01-04 Mitte (X-marker, kein Routing — BH-003c, NICHT BH-003a)
        _make_day('2025-01-04', dp_kwargs={
            'activity_type': 'frei',  # Reader heuristic
            'routing': [],
            'layover_ort': 'BLR',
            'overnight_after_day': True,
            'starts_at_homebase': False, 'ends_at_homebase': False,
            'raw_marker': 'X',
        }),
        # 01-05 Mitte → spätes Briefing (Z73-Override-Bug, anderer Fix)
        _make_day('2025-01-05', dp_kwargs={
            'activity_type': 'tour',
            'routing': ['BLR', 'FRA'],
            'layover_ort': 'BLR',
            'overnight_after_day': True,
            'starts_at_homebase': False, 'ends_at_homebase': False,
            'start_time': '23:28', 'end_time': '23:59',
            'duty_duration_minutes': 31,
            'raw_marker': '755 LH755-1',
            'has_fl': True, 'requires_commute': False,
        }),
        # 01-06 Heimkehr (BH-003a Zieltag)
        # has_fl=False laut fixture — Heimkehr-Tag ohne neues Flugbriefing
        _make_day('2025-01-06', dp_kwargs={
            'activity_type': 'same_day',
            'routing': ['BLR', 'FRA'],
            'layover_ort': '',
            'overnight_after_day': False,
            'starts_at_homebase': True, 'ends_at_homebase': True,
            'start_time': '00:00', 'end_time': '09:21',
            'duty_duration_minutes': 561,
            'raw_marker': '755 LH755-1',
            'has_fl': False, 'requires_commute': True,
        }),
    ]


class TestBH003aIssueReturnDayZ76(unittest.TestCase):

    # ─── Positive: 01-06 wird Z76 An/Ab ──────────────────────────────────

    def test_bh003a_2025_01_06_issue_return_day_becomes_z76_abreise(self):
        """01-06 BLR→FRA, duty 561min, ends_at_homebase=True, prev BLR overnight
        → Z76 (statt Issue)."""
        matched = _bangalore_tour()
        result = app_module._deterministic_classify_v7(matched, year=2025, homebase='FRA')
        d = next(t for t in result['tage_detail'] if t['datum'] == '2025-01-06')
        self.assertEqual(d['klass'], 'Z76',
            f'01-06 muss Z76 sein, war {d.get("klass")} reason={d.get("classifier_result",{}).get("reason","")}')

    def test_bh003a_uses_prev_layover_ort_blr_for_bmf_india(self):
        """BMF-Lookup nutzt prev.layover_ort=BLR → Indien-Bangalore."""
        matched = _bangalore_tour()
        result = app_module._deterministic_classify_v7(matched, year=2025, homebase='FRA')
        d = next(t for t in result['tage_detail'] if t['datum'] == '2025-01-06')
        cr = d.get('classifier_result') or {}
        bmf_land = cr.get('bmf_land', '')
        # Indien-Bangalore (Golden) ODER „Indien" (fallback general) — beide akzeptabel
        self.assertIn('Indien', bmf_land,
            f'BMF-Land muss Indien sein (aus BLR), war: {bmf_land}')
        # Betrag > 0 (an_abreise-Satz)
        self.assertGreater(cr.get('amount', 0) or 0, 0)

    # ─── Guards: Negative-Tage müssen Issue/Frei bleiben ────────────────

    def test_bh003c_applies_to_2025_05_23_via_followme_soft_rescue(self):
        """05-23 LAD-Route, BH-003b-Strict-Guards greifen nicht (kein routing[-1]=FRA),
        ABER BH-003c (FollowMe-Soft-Rescue) feuert: prev war LAD-Z76-Auslands-Layover,
        heute Issue mit Heimkehr-Reason → Z76 An/Ab Angola.

        Policy-Wechsel 2026-05-22: User hat sich für FollowMe entschieden ("macht ja
        Sinn"). Doku: docs/FOLLOWME_AEROTAX_TIBOR_2025_DAY_DIFF.md Pattern A."""
        matched = [
            _make_day('2025-05-22', dp_kwargs={
                'activity_type': 'tour', 'routing': ['LAD', 'FRA'],
                'layover_ort': 'LAD', 'overnight_after_day': True,
                'starts_at_homebase': False, 'ends_at_homebase': False,
                'start_time': '21:00', 'end_time': '23:59',
                'duty_duration_minutes': 179,
                'raw_marker': '103703 P1', 'has_fl': True,
            }),
            _make_day('2025-05-23', dp_kwargs={
                'activity_type': 'same_day', 'routing': ['LAD'],
                'overnight_after_day': False,
                'starts_at_homebase': True, 'ends_at_homebase': True,
                'start_time': '00:00', 'end_time': '05:30',
                'duty_duration_minutes': 330,  # < 480 — BH-003b nicht
                'raw_marker': '103703 P1', 'has_fl': False,
            }),
        ]
        result = app_module._deterministic_classify_v7(matched, year=2025, homebase='FRA')
        d = next(t for t in result['tage_detail'] if t['datum'] == '2025-05-23')
        # BH-003c FollowMe-Rescue: prev LAD foreign-layover → today Z76 An/Ab Angola
        self.assertEqual(d['klass'], 'Z76',
            f'05-23 muss via BH-003c FollowMe-Soft-Rescue Z76 werden. War: {d["klass"]}')

    def test_bh003c_applies_to_2025_06_03_via_followme_soft_rescue(self):
        """06-03 SOF→FRA→LHR, BH-003b-Strict greift nicht (routing endet LHR, nicht FRA),
        aber BH-003c feuert: prev war SOF-Z76 → today Z76 An/Ab Bulgarien.

        Policy-Wechsel 2026-05-22 zu FollowMe (siehe Pattern A im Doku)."""
        matched = [
            _make_day('2025-06-02', dp_kwargs={
                'activity_type': 'tour', 'routing': ['GOT', 'FRA', 'SOF'],
                'layover_ort': 'SOF', 'overnight_after_day': True,
                'starts_at_homebase': False, 'ends_at_homebase': False,
                'duty_duration_minutes': 1189,
                'raw_marker': '126533 PU', 'has_fl': True,
            }),
            _make_day('2025-06-03', dp_kwargs={
                'activity_type': 'same_day',
                'routing': ['SOF', 'FRA', 'LHR'],
                'overnight_after_day': False,
                'starts_at_homebase': True, 'ends_at_homebase': True,
                'start_time': '03:20', 'end_time': '11:05',
                'duty_duration_minutes': 465,
                'raw_marker': '126533 PU', 'has_fl': False,
            }),
        ]
        result = app_module._deterministic_classify_v7(matched, year=2025, homebase='FRA')
        d = next(t for t in result['tage_detail'] if t['datum'] == '2025-06-03')
        self.assertEqual(d['klass'], 'Z76',
            f'06-03 muss via BH-003c FollowMe-Soft-Rescue Z76 werden. War: {d["klass"]}')

    def test_bh003b_z76_via_routing_evidence_alone_2025_10_28(self):
        """2026-05-21: BH-003a → BH-003b. Routing-Evidence (TLV layover →
        TLV→FRA → ends_at_homebase) genügt JETZT für Z76, auch wenn Sonnet
        duty<480 gelesen hat. User-Feedback: zu viele „Mischfall"-Issues."""
        matched = [
            _make_day('2025-10-27', dp_kwargs={
                'activity_type': 'tour', 'routing': ['TLV'],
                'layover_ort': 'TLV', 'overnight_after_day': True,
                'starts_at_homebase': False, 'ends_at_homebase': False,
                'raw_marker': 'X',
            }),
            _make_day('2025-10-28', dp_kwargs={
                'activity_type': 'same_day', 'routing': ['TLV', 'FRA'],
                'overnight_after_day': False,
                'starts_at_homebase': True, 'ends_at_homebase': True,
                'start_time': '03:15', 'end_time': '07:55',
                'duty_duration_minutes': 280,  # < 480 — Sonnet-Lesefehler
                'raw_marker': '32935 PU', 'has_fl': False,
            }),
        ]
        result = app_module._deterministic_classify_v7(matched, year=2025, homebase='FRA')
        d = next(t for t in result['tage_detail'] if t['datum'] == '2025-10-28')
        self.assertEqual(d['klass'], 'Z76',
            f'10-28 muss Z76 werden (Routing-Evidence TLV→FRA komplett). War: {d["klass"]}')

    def test_bh003a_does_not_apply_to_x_marker_without_routing_2025_01_04(self):
        """01-04 X-marker, kein routing, activity_type='frei' → klass=Frei,
        NICHT same_day-Branch → BH-003a-Guard nie erreicht. Bleibt Frei."""
        matched = _bangalore_tour()
        result = app_module._deterministic_classify_v7(matched, year=2025, homebase='FRA')
        d = next(t for t in result['tage_detail'] if t['datum'] == '2025-01-04')
        self.assertNotEqual(d['klass'], 'Z76',
            f'01-04 darf durch BH-003a NICHT Z76 werden (X-Marker). War: {d["klass"]}')

    # ─── Guard-Sub-Bedingungen ──────────────────────────────────────────

    def test_bh003b_no_longer_requires_duty_480(self):
        """2026-05-21: BH-003b lockert G6 (duty>=480) — Routing-Evidence allein
        reicht. Test umgekehrt: duty=300, aber JFK-Layover + JFK→FRA + ends_hb
        → muss Z76 werden."""
        matched = [
            _make_day('2025-04-04', dp_kwargs={
                'activity_type': 'tour', 'routing': ['JFK', 'FRA'],
                'layover_ort': 'JFK', 'overnight_after_day': True,
                'duty_duration_minutes': 600, 'has_fl': True,
            }),
            _make_day('2025-04-05', dp_kwargs={
                'activity_type': 'same_day', 'routing': ['JFK', 'FRA'],
                'overnight_after_day': False,
                'starts_at_homebase': True, 'ends_at_homebase': True,
                'start_time': '00:00', 'end_time': '05:00',
                'duty_duration_minutes': 300,  # < 480 — egal, Routing-Evidence gilt
                'raw_marker': '999', 'has_fl': False,
            }),
        ]
        result = app_module._deterministic_classify_v7(matched, year=2025, homebase='FRA')
        d = next(t for t in result['tage_detail'] if t['datum'] == '2025-04-05')
        self.assertEqual(d['klass'], 'Z76', f'duty 300<480 aber Routing-Evidence komplett → Z76. War: {d["klass"]}')

    def test_bh003c_overrides_strict_routing_requirement(self):
        """BH-003b-Strict braucht routing[0]==prev.layover_ort. Wenn das fehlt,
        greift seit 2026-05-22 die FollowMe-Soft-Variante BH-003c — Vortag-
        Auslands-Layover allein genügt für Z76 An/Ab.

        Vorher (alt-konservativ): wenn routing[0]=DEL ≠ prev.layover_ort=BLR
        → Issue. Jetzt: BH-003c rescued zu Z76 Indien-Bangalore."""
        matched = [
            _make_day('2025-04-04', dp_kwargs={
                'activity_type': 'tour', 'routing': ['BLR'],
                'layover_ort': 'BLR', 'overnight_after_day': True,
                'duty_duration_minutes': 800, 'has_fl': True,
            }),
            _make_day('2025-04-05', dp_kwargs={
                'activity_type': 'same_day',
                'routing': ['DEL', 'FRA'],
                'overnight_after_day': False,
                'starts_at_homebase': True, 'ends_at_homebase': True,
                'duty_duration_minutes': 600,
                'raw_marker': '999', 'has_fl': False,
            }),
        ]
        result = app_module._deterministic_classify_v7(matched, year=2025, homebase='FRA')
        d = next(t for t in result['tage_detail'] if t['datum'] == '2025-04-05')
        self.assertEqual(d['klass'], 'Z76',
            f'BH-003c muss zuschlagen (prev BLR foreign-layover). War: {d["klass"]}')

    def test_bh003c_overrides_ends_at_homebase_requirement(self):
        """BH-003b-Strict braucht ends_at_homebase=True. BH-003c (FollowMe) ist
        toleranter — Vortag-Layover-Land genügt. Policy 2026-05-22."""
        matched = [
            _make_day('2025-04-04', dp_kwargs={
                'activity_type': 'tour', 'routing': ['BLR'],
                'layover_ort': 'BLR', 'overnight_after_day': True,
                'duty_duration_minutes': 800, 'has_fl': True,
            }),
            _make_day('2025-04-05', dp_kwargs={
                'activity_type': 'same_day', 'routing': ['BLR', 'FRA'],
                'overnight_after_day': False,
                'starts_at_homebase': True, 'ends_at_homebase': False,
                'duty_duration_minutes': 600,
                'raw_marker': '999', 'has_fl': False,
            }),
        ]
        result = app_module._deterministic_classify_v7(matched, year=2025, homebase='FRA')
        d = next(t for t in result['tage_detail'] if t['datum'] == '2025-04-05')
        self.assertEqual(d['klass'], 'Z76',
            f'BH-003c muss zuschlagen trotz ends_hb=False. War: {d["klass"]}')

    def test_bh003c_does_NOT_apply_when_prev_layover_is_homebase(self):
        """BH-003c hat H3-Guard: prev.layover_ort != homebase. Schützt gegen
        Heimat-Zirkel."""
        matched = [
            _make_day('2025-04-04', dp_kwargs={
                'activity_type': 'tour', 'routing': ['FRA'],
                'layover_ort': 'FRA',  # HOMEBASE — kein echter Auslands-Layover
                'overnight_after_day': True,
                'duty_duration_minutes': 800, 'has_fl': True,
            }),
            _make_day('2025-04-05', dp_kwargs={
                'activity_type': 'same_day', 'routing': ['FRA'],
                'overnight_after_day': False,
                'starts_at_homebase': True, 'ends_at_homebase': True,
                'duty_duration_minutes': 200,
                'raw_marker': '999', 'has_fl': False,
            }),
        ]
        result = app_module._deterministic_classify_v7(matched, year=2025, homebase='FRA')
        d = next(t for t in result['tage_detail'] if t['datum'] == '2025-04-05')
        self.assertNotEqual(d['klass'], 'Z76',
            f'prev.layover=homebase → BH-003c darf NICHT zuschlagen. War: {d["klass"]}')

    def test_bh003c_does_NOT_apply_when_prev_layover_is_inland(self):
        """BH-003c hat H2-Guard: prev.layover_ort kein Inland-Code (kein Ausland)."""
        matched = [
            _make_day('2025-04-04', dp_kwargs={
                'activity_type': 'tour', 'routing': ['MUC'],
                'layover_ort': 'MUC',  # Inland
                'overnight_after_day': True,
                'duty_duration_minutes': 600, 'has_fl': True,
            }),
            _make_day('2025-04-05', dp_kwargs={
                'activity_type': 'same_day', 'routing': ['MUC'],
                'overnight_after_day': False,
                'starts_at_homebase': True, 'ends_at_homebase': True,
                'duty_duration_minutes': 300,
                'raw_marker': '999', 'has_fl': False,
            }),
        ]
        result = app_module._deterministic_classify_v7(matched, year=2025, homebase='FRA')
        d = next(t for t in result['tage_detail'] if t['datum'] == '2025-04-05')
        self.assertNotEqual(d['klass'], 'Z76',
            f'prev.layover=Inland(MUC) → BH-003c darf NICHT Z76 setzen. War: {d["klass"]}')

    # ─── No-side-effect ─────────────────────────────────────────────────

    def test_bh003a_does_not_change_hotel_count(self):
        """01-06 ist Heimkehr (ends_at_homebase=True) → counts_as_hotel_nacht=False.
        Hotel-Counter darf nicht steigen."""
        matched = _bangalore_tour()
        result = app_module._deterministic_classify_v7(matched, year=2025, homebase='FRA')
        d = next(t for t in result['tage_detail'] if t['datum'] == '2025-01-06')
        cr = d.get('classifier_result') or {}
        self.assertFalse(cr.get('counted_as_hotel_nacht'),
            'Heimkehr-Tag (ends_at_homebase=True) darf nicht als Hotel zählen')

    def test_bh003a_no_double_count_tage_detail(self):
        """tage_detail enthält 01-06 genau 1× nach Klassifikation."""
        matched = _bangalore_tour()
        result = app_module._deterministic_classify_v7(matched, year=2025, homebase='FRA')
        n_01_06 = sum(1 for t in result['tage_detail'] if t['datum'] == '2025-01-06')
        self.assertEqual(n_01_06, 1, f'01-06 darf nur 1× im tage_detail sein, war {n_01_06}')

    def test_bh003a_issue_count_reduced_by_one(self):
        """Mit Bangalore-Tour: 01-06 als Issue ist weg → genau 0 Issue-Tage."""
        matched = _bangalore_tour()
        result = app_module._deterministic_classify_v7(matched, year=2025, homebase='FRA')
        issue_days = [t for t in result['tage_detail'] if t['klass'] == 'Issue']
        # 01-06 darf nicht in Issue-Liste sein
        issue_dates = [t['datum'] for t in issue_days]
        self.assertNotIn('2025-01-06', issue_dates,
            f'01-06 darf nicht Issue sein. Issues: {issue_dates}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
