"""BH-CORE-001 Phase 4.8b — Evidence Threshold Calibration.

Phase 4.8 ließ in 2 Boundary-Cases (12-15 JFK Tag 2, 07-03 OTP→FRA→LHR)
das KEEP_TOUR-Ergebnis durch reine Score-Diff durch. Phase 4.8b führt
einen Multi-Conflict-Override ein, der Cross-Source-Konflikte zwingend
zu NEEDS_AI verdichtet, wenn:
  - ≥2 starke Konflikt-Signale gleichzeitig feuern UND
  - keine SE-Auslands-Bestätigung kompensiert UND
  - der Tag tatsächlich eine echte CAS-Tour-Claim hat (score_for >= 6).

Tests generalisieren — keine Tibor-Hardcoded-Daten in Assertions.
"""
import os
import sys
import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import app as app_module

pytestmark = pytest.mark.skipif(
    not hasattr(app_module, '_normalize_tours_from_raw_facts')
    or not hasattr(app_module, '_score_tour_day_evidence'),
    reason='BH-CORE-001 Phase 4.8b erfordert normalize_tours + evidence',
)


def _day(datum, **kw):
    base = {
        'datum': datum, 'activity_type': 'tour',
        'routing': [], 'layover_ort': '', 'overnight_after_day': False,
        'start_time': '', 'end_time': '', 'duty_duration_minutes': 0,
        'raw_marker': '', 'has_fl': False, 'is_workday': True,
        'requires_commute': False, 'starts_at_homebase': False,
        'ends_at_homebase': False,
    }
    base.update(kw)
    return base


def _se(stfrei_ort='', stfrei_total=0.0, stfrei_inland=None, count=0):
    return {'stfrei_ort': stfrei_ort, 'stfrei_total': stfrei_total,
            'stfrei_inland': stfrei_inland, 'count': count,
            'zwoelftel': 0, 'lines': []}


# ════════════════════════════════════════════════════════════════════════════
# 1. JFK Tag 2 (12-15): day-suffix nach abgeschlossener 1-Day-Tour
# ════════════════════════════════════════════════════════════════════════════

def test_1215_jfk_tag2_conflict_needs_ai_not_keep():
    """Day-Suffix-Marker 'Tag 2' nach Vortag-Tour-Span, kein SE-Stempel,
    nicht in FollowMe-Span, no anfahrt → mindestens NEEDS_AI."""
    prev = _day('2025-12-14', routing=['FRA','JFK'], layover_ort='JFK',
                overnight_after_day=False, ends_at_homebase=False,
                start_time='14:00', duty_duration_minutes=600,
                raw_marker='57783 P1', has_fl=True)
    day = _day('2025-12-15', routing=['JFK','FRA'],
               overnight_after_day=False, ends_at_homebase=True,
               start_time='02:00', duty_duration_minutes=580,
               raw_marker='57783 P1 Tag 2', has_fl=True)
    fm_ctx = {
        'anfahrten_dates': {'2025-12-08', '2025-12-22'},
        'tour_spans': [
            ('2025-12-14', '2025-12-14', {'2025-12-14'}),  # 1-day tour
            ('2025-12-22', '2025-12-26', {'2025-12-22','2025-12-23',
                                           '2025-12-24','2025-12-25',
                                           '2025-12-26'}),
        ],
    }
    r = app_module._score_tour_day_evidence(
        day, prev_day=prev, se=_se(), followme_context=fm_ctx, homebase='FRA'
    )
    assert r['decision'] in ('NEEDS_AI', 'DROP_TOUR'), (
        f'12-15 JFK Tag 2: erwartet NEEDS_AI/DROP, war {r["decision"]} '
        f'(for={r["score_for"]}, against={r["score_against"]})'
    )
    # day_suffix_claims_completed_prev MUSS firen — der Tag ist genau die
    # Konstellation, für die das Signal designed ist.
    against_names = [n for n, _, _ in r['evidence_against']]
    assert 'day_suffix_claims_completed_prev' in against_names, (
        f'day_suffix_claims_completed_prev fehlt: {against_names}'
    )


# ════════════════════════════════════════════════════════════════════════════
# 2. OTP→FRA→LHR (07-03): Same-Day mit Homebase-Transit, endet foreign
# ════════════════════════════════════════════════════════════════════════════

def test_0703_otp_fra_lhr_transit_conflict_needs_ai_not_keep():
    """routing X→HB→Y mit Y!=HB foreign → kein sauberer Same-Day,
    transit_via_homebase_ends_foreign + followme_explicit_other_span → NEEDS_AI."""
    day = _day('2025-07-03', routing=['OTP','FRA','LHR'],
               starts_at_homebase=False, ends_at_homebase=False,
               start_time='06:00', duty_duration_minutes=720,
               raw_marker='129023 PU / Tag 3', has_fl=True)
    fm_ctx = {
        'anfahrten_dates': {'2025-07-02', '2025-07-09'},
        'tour_spans': [
            ('2025-07-02', '2025-07-02', {'2025-07-02'}),
            ('2025-07-09', '2025-07-12', {'2025-07-09','2025-07-10',
                                           '2025-07-11','2025-07-12'}),
        ],
    }
    r = app_module._score_tour_day_evidence(
        day, se=_se(), followme_context=fm_ctx, homebase='FRA'
    )
    assert r['decision'] in ('NEEDS_AI', 'DROP_TOUR'), (
        f'07-03 OTP→FRA→LHR: erwartet NEEDS_AI/DROP, war {r["decision"]} '
        f'(for={r["score_for"]}, against={r["score_against"]})'
    )
    against_names = [n for n, _, _ in r['evidence_against']]
    assert 'transit_via_homebase_ends_foreign' in against_names, (
        f'transit_via_homebase_ends_foreign fehlt: {against_names}'
    )


# ════════════════════════════════════════════════════════════════════════════
# 3. Starke SE-Auslands-Bestätigung overrided Konflikte zurück zu KEEP
# ════════════════════════════════════════════════════════════════════════════

def test_strong_se_foreign_can_override_conflict_to_keep():
    """Sauberer Foreign-Tour-Tag (FRA→JFK overnight, SE-Auslands-Stempel),
    aber FollowMe-tour_spans + anfahrten haben den Tag NICHT.
    SE-Auslands ist physische Quittung → Multi-Conflict-Override soll
    NICHT eingreifen, KEEP_TOUR per Natural-Diff bleibt.
    """
    day = _day('2025-08-15', routing=['FRA','JFK'], layover_ort='JFK',
               overnight_after_day=True, starts_at_homebase=True,
               start_time='13:00', duty_duration_minutes=600,
               raw_marker='LH404', has_fl=True)
    se = _se(stfrei_ort='JFK', stfrei_total=40.0, stfrei_inland=False, count=1)
    fm_ctx = {
        'anfahrten_dates': {'2025-08-10', '2025-08-22'},  # 08-15 fehlt absichtlich
        'tour_spans': [
            ('2025-08-10', '2025-08-12', {'2025-08-10','2025-08-11','2025-08-12'}),
            ('2025-08-22', '2025-08-24', {'2025-08-22','2025-08-23','2025-08-24'}),
        ],
    }
    r = app_module._score_tour_day_evidence(
        day, se=se, followme_context=fm_ctx, homebase='FRA'
    )
    assert r['decision'] == 'KEEP_TOUR', (
        f'SE-Auslands-Stempel + clean foreign routing soll KEEP halten, '
        f'auch wenn FollowMe-spans fehlen; war {r["decision"]} '
        f'(for={r["score_for"]}, against={r["score_against"]})'
    )
    # Belege: SE-Foreign-Stempel UND foreign_iata sind beide in evidence_for
    for_names = [n for n, _, _ in r['evidence_for']]
    assert 'se_foreign_stamp' in for_names
    assert 'foreign_iata_in_routing' in for_names


# ════════════════════════════════════════════════════════════════════════════
# 4. Bangalore nicht versehentlich durch Calibration downgegradet
# ════════════════════════════════════════════════════════════════════════════

def test_bangalore_not_downgraded_by_threshold_calibration():
    """Bangalore-Tour mit SE + foreign-routing + overnight: KEEP an allen 4 Tagen."""
    blr_03 = _day('2025-01-03', routing=['FRA','BLR'], layover_ort='BLR',
                  overnight_after_day=True, starts_at_homebase=True,
                  start_time='10:55', duty_duration_minutes=785,
                  raw_marker='31591 P1', has_fl=True)
    blr_04 = _day('2025-01-04', routing=[], layover_ort='BLR',
                  overnight_after_day=True, raw_marker='X')
    blr_05 = _day('2025-01-05', routing=['BLR','FRA'], layover_ort='BLR',
                  overnight_after_day=True, start_time='23:28',
                  duty_duration_minutes=31, raw_marker='755 LH755-1',
                  has_fl=True)
    blr_06 = _day('2025-01-06', routing=['BLR','FRA'], starts_at_homebase=True,
                  ends_at_homebase=True, start_time='00:00',
                  duty_duration_minutes=561, raw_marker='755 LH755-1')

    se_blr = _se(stfrei_ort='BLR', stfrei_total=42.0,
                 stfrei_inland=False, count=1)
    fm_ctx = {
        'anfahrten_dates': {'2025-01-03'},
        'tour_spans': [
            ('2025-01-03', '2025-01-06', {'2025-01-03','2025-01-04',
                                           '2025-01-05','2025-01-06'}),
        ],
    }

    r3 = app_module._score_tour_day_evidence(
        blr_03, next_day=blr_04, se=se_blr,
        followme_context=fm_ctx, homebase='FRA')
    r4 = app_module._score_tour_day_evidence(
        blr_04, prev_day=blr_03, next_day=blr_05, se=se_blr,
        followme_context=fm_ctx, homebase='FRA')
    r5 = app_module._score_tour_day_evidence(
        blr_05, prev_day=blr_04, next_day=blr_06, se=se_blr,
        followme_context=fm_ctx, homebase='FRA')
    r6 = app_module._score_tour_day_evidence(
        blr_06, prev_day=blr_05, se=_se(),
        followme_context=fm_ctx, homebase='FRA')

    for label, r in [('01-03', r3), ('01-04', r4), ('01-05', r5), ('01-06', r6)]:
        assert r['decision'] == 'KEEP_TOUR', (
            f'BLR {label}: erwartet KEEP_TOUR, war {r["decision"]} '
            f'(for={r["score_for"]}, against={r["score_against"]}, '
            f'expl={r["explanation"]})'
        )


# ════════════════════════════════════════════════════════════════════════════
# 5. Clear CAS+SE tour (FRA→JFK overnight + SE-stamp) bleibt KEEP
# ════════════════════════════════════════════════════════════════════════════

def test_clear_cas_se_tour_still_keep():
    """Foreign routing + SE-Auslands + overnight bleibt KEEP_TOUR auch wenn
    FollowMe leer ist (kein Konflikt-Override)."""
    day = _day('2025-08-15', routing=['FRA','JFK'], layover_ort='JFK',
               overnight_after_day=True, starts_at_homebase=True,
               start_time='13:00', duty_duration_minutes=600,
               raw_marker='LH404', has_fl=True)
    se = _se(stfrei_ort='JFK', stfrei_total=40.0, stfrei_inland=False, count=1)
    r = app_module._score_tour_day_evidence(
        day, se=se, followme_context={'anfahrten_dates': set()},
        homebase='FRA'
    )
    assert r['decision'] == 'KEEP_TOUR', (
        f'Clear CAS+SE tour: erwartet KEEP, war {r["decision"]}'
    )


# ════════════════════════════════════════════════════════════════════════════
# 6. X inside proven tour bleibt KEEP
# ════════════════════════════════════════════════════════════════════════════

def test_x_inside_valid_tour_still_keep():
    """X-Marker mit prev_overnight + foreign layover + SE → KEEP_TOUR
    auch nach Threshold-Calibration."""
    prev = _day('2025-02-13', routing=['FRA','HND'], layover_ort='HND',
                overnight_after_day=True, starts_at_homebase=True,
                start_time='17:00', duty_duration_minutes=720,
                raw_marker='12345 P1', has_fl=True)
    day = _day('2025-02-14', routing=[], layover_ort='HND',
               overnight_after_day=True, raw_marker='X HND')
    se = _se(stfrei_ort='HND', stfrei_total=48.0, stfrei_inland=False, count=1)
    r = app_module._score_tour_day_evidence(
        day, prev_day=prev, se=se, homebase='FRA'
    )
    assert r['decision'] == 'KEEP_TOUR', (
        f'X inside proven tour: erwartet KEEP, war {r["decision"]}'
    )


# ════════════════════════════════════════════════════════════════════════════
# 7. RES zuhause bleibt DROP (Multi-Conflict greift nicht ohne CAS-Claim)
# ════════════════════════════════════════════════════════════════════════════

def test_res_homebase_still_drop():
    """RES ohne Routing, ohne SE, am Homebase: FOR=0, AGAINST-Score-dominanz
    erzeugt natürliches DROP_TOUR — Multi-Conflict-Override greift nicht
    bei score_for < 6."""
    day = _day('2025-03-11', raw_marker='RES',
               starts_at_homebase=True, ends_at_homebase=True,
               routing=[], overnight_after_day=False)
    fm_ctx = {
        'anfahrten_dates': {'2025-03-15'},
        'tour_spans': [
            ('2025-03-15', '2025-03-17', {'2025-03-15','2025-03-16','2025-03-17'}),
        ],
    }
    r = app_module._score_tour_day_evidence(
        day, se=_se(), followme_context=fm_ctx, homebase='FRA'
    )
    assert r['decision'] in ('DROP_TOUR', 'NEEDS_AI'), (
        f'RES zuhause: erwartet DROP/NEEDS_AI, war {r["decision"]}'
    )
    # FOR-Score muss niedrig sein (kein Tour-Claim)
    assert r['score_for'] < 6, (
        f'RES zuhause FOR-Score muss < 6 sein, war {r["score_for"]}'
    )


# ════════════════════════════════════════════════════════════════════════════
# 8. Phantom LAD bleibt NEEDS_AI/DROP
# ════════════════════════════════════════════════════════════════════════════

def test_phantom_lad_still_needs_ai_or_drop():
    """Phantom-LAD-Tag mit foreign-routing aber no SE + nicht in FollowMe
    + no anfahrt → NEEDS_AI (Multi-Conflict)."""
    day = _day('2025-05-20', routing=['FRA','LAD'], layover_ort='LAD',
               overnight_after_day=True, starts_at_homebase=True,
               start_time='20:05', duty_duration_minutes=234,
               raw_marker='103703 P1', has_fl=True)
    fm_ctx = {
        'anfahrten_dates': {'2025-05-14', '2025-05-26'},
        'tour_spans': [
            ('2025-05-14', '2025-05-19', {'2025-05-14','2025-05-15',
                                           '2025-05-16','2025-05-17',
                                           '2025-05-18','2025-05-19'}),
            ('2025-05-26', '2025-05-30', {'2025-05-26','2025-05-27',
                                           '2025-05-28','2025-05-29',
                                           '2025-05-30'}),
        ],
    }
    r = app_module._score_tour_day_evidence(
        day, se=_se(), followme_context=fm_ctx, homebase='FRA'
    )
    assert r['decision'] in ('NEEDS_AI', 'DROP_TOUR'), (
        f'Phantom LAD: erwartet NEEDS_AI/DROP, war {r["decision"]}'
    )


# ════════════════════════════════════════════════════════════════════════════
# 9. KEINE finale KPI-Änderung durch Phase 4.8b
# ════════════════════════════════════════════════════════════════════════════

def test_no_final_kpi_change_phase48b():
    """Tour-Membership (role, in_tour, tour_size) muss durch Phase 4.8b
    UNVERÄNDERT bleiben — die neuen Konflikt-Signale beeinflussen nur
    evidence_decision (Audit-Layer), nicht die Tour-Building-Logik."""
    matched = [
        {'datum': '2025-01-03', 'dp': _day('2025-01-03',
                routing=['FRA','BLR'], layover_ort='BLR',
                overnight_after_day=True, starts_at_homebase=True,
                start_time='10:55', duty_duration_minutes=785,
                raw_marker='31591 P1', has_fl=True),
         'se': _se(stfrei_ort='BLR', stfrei_total=42.0,
                   stfrei_inland=False, count=1)},
        {'datum': '2025-01-04', 'dp': _day('2025-01-04',
                routing=[], layover_ort='BLR',
                overnight_after_day=True, raw_marker='X'),
         'se': _se(stfrei_ort='BLR', stfrei_total=42.0,
                   stfrei_inland=False, count=1)},
        {'datum': '2025-01-05', 'dp': _day('2025-01-05',
                routing=['BLR','FRA'], layover_ort='BLR',
                overnight_after_day=True, start_time='23:28',
                duty_duration_minutes=31, raw_marker='755 LH755-1',
                has_fl=True),
         'se': _se(stfrei_ort='BLR', stfrei_total=42.0,
                   stfrei_inland=False, count=1)},
        {'datum': '2025-01-06', 'dp': _day('2025-01-06',
                routing=['BLR','FRA'], starts_at_homebase=True,
                ends_at_homebase=True, start_time='00:00',
                duty_duration_minutes=561, raw_marker='755 LH755-1'),
         'se': _se()},
    ]
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025,
        known_anfahrten=[{'datum':'2025-01-03'}],
    )
    membership = [(t['tour_id'], t['tour_size'], t['tour_pattern'])
                  for t in tours]
    roles_per_tour = [[d['role'] for d in t['days']] for t in tours]

    blr_tour = next(t for t in tours
                    if any(d['datum'] == '2025-01-04' for d in t['days']))
    assert blr_tour['tour_size'] == 4
    blr_roles = [d['role'] for d in blr_tour['days']]
    assert blr_roles == ['tour_start', 'tour_mid', 'tour_mid', 'tour_end']

    # 2. Aufruf muss identisches Ergebnis liefern (idempotent)
    tours2 = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025,
        known_anfahrten=[{'datum':'2025-01-03'}],
    )
    membership2 = [(t['tour_id'], t['tour_size'], t['tour_pattern'])
                   for t in tours2]
    roles_per_tour2 = [[d['role'] for d in t['days']] for t in tours2]
    assert membership == membership2
    assert roles_per_tour == roles_per_tour2
