"""BH-CORE-001 Phase 4.7 — General Evidence Engine.

Generelle Tests — NICHT Tibor-hardcoded. Testet `_score_tour_day_evidence`
gegen typische Crew-Pattern-Konstellationen.

Decision-Output:
  KEEP_TOUR     — starke FOR-evidence
  DROP_TOUR     — starke AGAINST-evidence
  NEEDS_AI      — uneindeutig, KI-Resolver nötig
  NEEDS_USER    — auch nach KI < 0.70 Konfidenz (Letzter Schritt)
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
    not hasattr(app_module, '_score_tour_day_evidence'),
    reason='BH-CORE-001 Phase 4.7 Evidence Engine not implemented'
)


def _day(**kw):
    base = {
        'datum': '2025-06-10', 'activity_type': 'tour',
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


# ─── Test 1: clear CAS+SE foreign tour keeps even if FollowMe absent ───────

def test_clear_cas_se_tour_keeps_even_if_followme_absent():
    """Routing FRA→JFK + foreign-SE-Stempel + overnight + plausible duty
    → KEEP_TOUR auch wenn FollowMe-anfahrten leer."""
    day = _day(datum='2025-08-15', routing=['FRA','JFK'], layover_ort='JFK',
               overnight_after_day=True, starts_at_homebase=True,
               start_time='13:00', duty_duration_minutes=600,
               has_fl=True, raw_marker='LH404')
    se = _se(stfrei_ort='JFK', stfrei_total=40.0, stfrei_inland=False, count=1)
    fm_ctx = {'anfahrten_dates': set()}   # FollowMe sagt nichts
    r = app_module._score_tour_day_evidence(day, se=se, followme_context=fm_ctx)
    assert r['decision'] == 'KEEP_TOUR', (
        f'CAS+SE klar foreign-tour → KEEP, war {r["decision"]} '
        f'(for={r["score_for"]}, against={r["score_against"]})'
    )


# ─── Test 2: FollowMe absent alone does NOT drop if CAS+SE strong ─────────

def test_followme_absent_alone_does_not_drop_if_cas_se_strong():
    """FollowMe-anfahrten enthält nicht das Datum, aber CAS+SE sind stark
    → trotzdem KEEP_TOUR (kein blinder Hard-Drop)."""
    day = _day(datum='2025-08-15', routing=['FRA','SIN'], layover_ort='SIN',
               overnight_after_day=True, starts_at_homebase=True,
               start_time='11:30', duty_duration_minutes=720,
               has_fl=True, raw_marker='LH778')
    se = _se(stfrei_ort='SIN', stfrei_total=48.0, stfrei_inland=False, count=1)
    fm_ctx = {'anfahrten_dates': {'2025-08-10','2025-08-20'}}
    r = app_module._score_tour_day_evidence(day, se=se, followme_context=fm_ctx)
    assert r['decision'] == 'KEEP_TOUR', (
        f'CAS+SE stark → FollowMe-absent darf nicht alleinig droppen. '
        f'War {r["decision"]} (for={r["score_for"]}, against={r["score_against"]})'
    )


# ─── Test 3: no SE + no anfahrt + implausible duty → DROP ─────────────────

def test_no_se_no_anfahrt_implausible_duty_drops():
    """Routing claimed aber kein SE, kein anfahrt-match, duty > FTL → DROP."""
    day = _day(datum='2025-06-01', routing=['FRA','CPH','GOT'], layover_ort='GOT',
               overnight_after_day=True, starts_at_homebase=True,
               start_time='05:55', duty_duration_minutes=1084,  # > 840 FTL
               raw_marker='126533 PU')
    se = _se()
    fm_ctx = {'anfahrten_dates': {'2025-06-04','2025-06-07'}}
    r = app_module._score_tour_day_evidence(day, se=se, followme_context=fm_ctx)
    assert r['decision'] in ('DROP_TOUR', 'NEEDS_AI'), (
        f'duty>FTL + no SE + no anfahrt → DROP/NEEDS_AI, war {r["decision"]}'
    )
    assert any(n == 'duty_over_ftl' for n, _, _ in r['evidence_against'])


# ─── Test 4: duty > 840 flags reader_bug ───────────────────────────────────

def test_duty_over_840_flags_reader_bug():
    """Egal welcher Kontext: duty > 840min → AGAINST-evidence eingetragen."""
    day = _day(datum='2025-09-25', routing=['FRA','BER','KRK'], layover_ort='KRK',
               overnight_after_day=True, starts_at_homebase=True,
               duty_duration_minutes=1059)
    r = app_module._score_tour_day_evidence(day)
    duty_warns = [e for e in r['evidence_against'] if e[0] == 'duty_over_ftl']
    assert duty_warns, f'duty {day["duty_duration_minutes"]} > FTL muss als evidence_against erscheinen'


# ─── Test 5: X inside proven tour keeps ──────────────────────────────────

def test_x_inside_proven_tour_keeps():
    """X-Marker mit prev.overnight=True + prev.layover=foreign → KEEP/NEEDS_AI."""
    day = _day(datum='2025-01-04', raw_marker='X', overnight_after_day=True,
               layover_ort='BLR')
    prev = _day(datum='2025-01-03', routing=['FRA','BLR'], layover_ort='BLR',
                overnight_after_day=True, starts_at_homebase=True,
                duty_duration_minutes=785)
    nxt = _day(datum='2025-01-05', routing=['BLR','FRA'], layover_ort='BLR',
               overnight_after_day=True, duty_duration_minutes=31)
    r = app_module._score_tour_day_evidence(day, prev_day=prev, next_day=nxt)
    assert r['decision'] in ('KEEP_TOUR', 'NEEDS_AI'), (
        f'X im foreign tour-context muss KEEP_TOUR oder NEEDS_AI sein, '
        f'war {r["decision"]}'
    )
    # Continuation evidence muss vorhanden sein
    assert any(n == 'continuation_from_prev_tour' for n, _, _ in r['evidence_for'])


# ─── Test 6: X outside tour drops ─────────────────────────────────────────

def test_x_outside_tour_drops():
    """X-Marker ohne Tour-Kontext (kein prev-overnight, kein routing) → DROP_TOUR."""
    day = _day(datum='2025-11-11', raw_marker='X',
               starts_at_homebase=False, ends_at_homebase=False)
    prev = _day(datum='2025-11-10', raw_marker='ORTSTAG',
                starts_at_homebase=True, ends_at_homebase=True)
    r = app_module._score_tour_day_evidence(day, prev_day=prev)
    assert r['decision'] in ('DROP_TOUR', 'NEEDS_AI'), (
        f'X ohne Tour-Kontext → DROP/NEEDS_AI, war {r["decision"]}'
    )


# ─── Test 7: RES inside foreign tour → KEEP/NEEDS_AI (standby_hotel) ──────

def test_res_inside_foreign_tour_keeps_or_needs_ai():
    """RES + prev.overnight=True + prev.layover=foreign → KEEP_TOUR oder NEEDS_AI."""
    day = _day(datum='2025-04-24', raw_marker='RES',
               overnight_after_day=True, layover_ort='ICN')
    prev = _day(datum='2025-04-23', routing=['FRA','ICN'], layover_ort='ICN',
                overnight_after_day=True, starts_at_homebase=True,
                duty_duration_minutes=600, has_fl=True)
    r = app_module._score_tour_day_evidence(day, prev_day=prev)
    assert r['decision'] in ('KEEP_TOUR', 'NEEDS_AI')


# ─── Test 8: RES homebase drops ──────────────────────────────────────────

def test_res_homebase_drops():
    """RES ohne overnight + zuhause-Kontext → DROP_TOUR."""
    day = _day(datum='2025-02-05', raw_marker='RES', routing=['FRA'],
               starts_at_homebase=True, ends_at_homebase=True,
               overnight_after_day=False)
    prev = _day(datum='2025-02-04', raw_marker='ORTSTAG',
                starts_at_homebase=True, ends_at_homebase=True)
    r = app_module._score_tour_day_evidence(day, prev_day=prev)
    assert r['decision'] in ('DROP_TOUR', 'NEEDS_AI')


# ─── Test 9: sequence-id marker alone not enough ─────────────────────────

def test_sequence_id_marker_alone_not_enough():
    """Marker `103703 P1` ohne SE/anfahrt/strong-evidence → DROP_TOUR oder NEEDS_AI."""
    day = _day(datum='2025-05-20', raw_marker='103703 P1',
               routing=['FRA','LAD'], layover_ort='LAD',
               overnight_after_day=True, starts_at_homebase=True,
               duty_duration_minutes=234)
    se = _se()   # kein SE
    fm_ctx = {'anfahrten_dates': set()}
    r = app_module._score_tour_day_evidence(day, se=se, followme_context=fm_ctx)
    # Pure sequence-id ohne SE + ohne anfahrt → AGAINST-evidence
    against_names = [n for n, _, _ in r['evidence_against']]
    assert 'sequence_id_marker_only' in against_names, (
        f'sequence-id-only-Marker muss als evidence_against erscheinen. Got: {against_names}'
    )


# ─── Test 10: day suffix helps determine tour boundary ────────────────────

def test_day_suffix_helps_determine_tour_boundary():
    """Marker mit 'Day 2' + Vortag-overnight + Vortag-layover → starke FOR-evidence."""
    day = _day(datum='2025-09-26', raw_marker='15688 PU (Day 2)',
               routing=['KRK','FRA','IST'], layover_ort='IST',
               overnight_after_day=True, duty_duration_minutes=600)
    prev = _day(datum='2025-09-25', routing=['FRA','BER','KRK'],
                layover_ort='KRK', overnight_after_day=True,
                starts_at_homebase=True, duty_duration_minutes=600)
    r = app_module._score_tour_day_evidence(day, prev_day=prev)
    for_names = [n for n, _, _ in r['evidence_for']]
    assert 'day_suffix_with_real_prev' in for_names, (
        f'day suffix mit echtem Vortag muss FOR-evidence sein. Got: {for_names}'
    )


# ─── Test 11: adjacent-date accounting prevents duplicate counting ────────

def test_adjacent_date_in_other_tour_drops():
    """Tag bereits in anderer Tour-Span → starke AGAINST-evidence."""
    day = _day(datum='2025-12-15', routing=['JFK','FRA'], layover_ort='JFK',
               overnight_after_day=True, raw_marker='57783 P1 Tag 2',
               duty_duration_minutes=184)
    prev = _day(datum='2025-12-14', routing=['FRA','JFK'], layover_ort='JFK',
                overnight_after_day=True, starts_at_homebase=True,
                duty_duration_minutes=889)
    fm_ctx = {
        'anfahrten_dates': {'2025-12-14'},
        # 12-15 NICHT in anderer Tour, ABER 12-14 ist eine geschlossene 1-Day-Tour
        # → 12-15 mit Day-2-Marker ist verdächtig: Tag bereits abgeschlossen.
        'day_in_other_span_dates': {'2025-12-14'},
        'tour_spans': [('2025-12-14','2025-12-14',{'2025-12-14'})],
    }
    r = app_module._score_tour_day_evidence(day, prev_day=prev, followme_context=fm_ctx)
    # Tag 12-15 hat Day-2-Marker und prev_overnight + prev_layover → starke FOR
    # ABER: anfahrten enthält nur 12-14, nicht 12-15. → followme-evidence-against.
    # Erwartung: NEEDS_AI oder DROP_TOUR (nicht KEEP)
    assert r['decision'] in ('NEEDS_AI', 'DROP_TOUR'), (
        f'12-15 ohne anfahrt + Tour-Span-Mismatch → NEEDS_AI/DROP, war {r["decision"]}'
    )


# ─── Test 12: reader misread creates NEEDS_AI not blind drop ──────────────

def test_reader_misread_creates_needs_ai_not_blind_drop():
    """duty knapp über FTL (zwischen 840 und 1000) aber Anfahrt-Evidence
    UND klares CAS-Routing → NEEDS_AI, NICHT DROP."""
    day = _day(datum='2025-12-14', routing=['FRA','JFK'], layover_ort='JFK',
               overnight_after_day=True, starts_at_homebase=True,
               start_time='09:10', duty_duration_minutes=889,  # knapp > 840
               has_fl=True, raw_marker='57783 P1')
    fm_ctx = {'anfahrten_dates': {'2025-12-14'}}
    r = app_module._score_tour_day_evidence(day, followme_context=fm_ctx)
    # Mit anfahrt + starkem routing + foreign-layover, trotz duty knapp>FTL
    assert r['decision'] in ('NEEDS_AI', 'KEEP_TOUR'), (
        f'Knapp-FTL mit starker Anfahrt-Evidence → NEEDS_AI/KEEP, '
        f'NICHT blind DROP. War {r["decision"]} '
        f'(for={r["score_for"]}, against={r["score_against"]})'
    )


# ─── Bonus: Bangalore-Tour Tag 01-04 ───────────────────────────────────────

def test_bangalore_x_in_tour_keeps():
    day = _day(datum='2025-01-04', raw_marker='X', overnight_after_day=True,
               layover_ort='BLR')
    prev = _day(datum='2025-01-03', routing=['FRA','BLR'], layover_ort='BLR',
                overnight_after_day=True, starts_at_homebase=True,
                duty_duration_minutes=785, has_fl=True, raw_marker='31591 P1')
    r = app_module._score_tour_day_evidence(day, prev_day=prev)
    assert r['decision'] in ('KEEP_TOUR', 'NEEDS_AI')


# ─── Bonus: Output Schema ────────────────────────────────────────────────

def test_evidence_engine_output_schema():
    day = _day(datum='2025-07-01', routing=['FRA','SIN'], overnight_after_day=True)
    r = app_module._score_tour_day_evidence(day)
    assert 'datum' in r
    assert 'evidence_for' in r
    assert 'evidence_against' in r
    assert 'score_for' in r
    assert 'score_against' in r
    assert 'decision' in r
    assert r['decision'] in ('KEEP_TOUR', 'DROP_TOUR', 'NEEDS_AI', 'NEEDS_USER')
    assert 'explanation' in r
    assert 'source_refs' in r


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
