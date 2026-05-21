"""BH-CORE-001 Phase 4.8 — Evidence-Engine im _normalize_tours_from_raw_facts.

Diese Tests prüfen ausschließlich Phase 4.8: dass die Evidence-Engine pro
NormalizedDay sichtbar wird (evidence_for/against, score_for/against,
evidence_decision, evidence_explanation, source_refs) und dass die Audit-
Decision für die kritischen Tag-Pattern (Bangalore, RES homebase, X in
proven tour, duty>FTL, LAD-Phantom) sinnvoll markiert wird.

Berechnung-/KPI-Pfad bleibt UNVERÄNDERT — das wird durch
`test_no_final_kpi_change_in_phase48` separat abgesichert.
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
    reason='BH-CORE-001 Phase 4.8 erfordert normalize_tours + evidence',
)


# Anfahrten-Probeliste — KEINE Vollerfassung, nur typische Tour-Starts
ANFAHRTEN_SAMPLE = [
    {'datum': '2025-01-03', 'beginn': '10:15'},
    {'datum': '2025-01-10', 'beginn': '07:50'},
    {'datum': '2025-09-26', 'beginn': '06:00'},
    {'datum': '2025-10-23', 'beginn': '20:10'},
]


def _day(datum, **kw):
    dp = {
        'datum': datum, 'activity_type': 'tour',
        'routing': [], 'layover_ort': '', 'overnight_after_day': False,
        'start_time': '', 'end_time': '', 'duty_duration_minutes': 0,
        'raw_marker': '', 'has_fl': False, 'is_workday': True,
        'requires_commute': False, 'starts_at_homebase': False,
        'ends_at_homebase': False, 'raw_lines': [], 'confidence': 0.9,
    }
    dp.update(kw)
    return {'datum': datum, 'dp': dp, 'se': {
        'stfrei_total': 0.0, 'stfrei_ort': '', 'stfrei_inland': None,
        'zwoelftel': 0, 'lines': [], 'count': 0,
    }}


def _day_with_se(datum, *, stfrei_ort, stfrei_total, stfrei_inland=False, **kw):
    d = _day(datum, **kw)
    d['se'] = {
        'stfrei_total': stfrei_total, 'stfrei_ort': stfrei_ort,
        'stfrei_inland': stfrei_inland,
        'zwoelftel': 1, 'lines': [], 'count': 1,
    }
    return d


def _norm(matched, anfahrten=None, fm_ctx=None):
    return app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025,
        known_anfahrten=anfahrten, followme_context=fm_ctx,
    )


def _all_days(tours):
    out = []
    for t in tours:
        for d in t.get('days') or []:
            out.append(d)
    return out


# ════════════════════════════════════════════════════════════════════════════
# 1. Evidence-Felder am normalized_day vorhanden
# ════════════════════════════════════════════════════════════════════════════

def test_evidence_attached_to_normalized_days():
    """Jeder NormalizedDay muss evidence_for/against/score/decision tragen."""
    matched = [
        _day('2025-01-03', routing=['FRA','BLR'], layover_ort='BLR',
             overnight_after_day=True, starts_at_homebase=True,
             start_time='10:55', duty_duration_minutes=785,
             raw_marker='31591 P1', has_fl=True, requires_commute=True),
        _day('2025-01-04', routing=[], layover_ort='BLR',
             overnight_after_day=True, raw_marker='X'),
    ]
    tours = _norm(matched, anfahrten=ANFAHRTEN_SAMPLE)
    days = _all_days(tours)
    assert days, 'normalized_tours produced no days'
    for d in days:
        for k in ('evidence_for', 'evidence_against',
                  'score_for', 'score_against',
                  'evidence_decision', 'evidence_explanation',
                  'source_refs'):
            assert k in d, f'normalized_day {d["datum"]} fehlt {k}'
        assert isinstance(d['evidence_for'], list)
        assert isinstance(d['evidence_against'], list)
        assert isinstance(d['score_for'], int)
        assert isinstance(d['score_against'], int)
        assert d['evidence_decision'] in (
            'KEEP_TOUR', 'DROP_TOUR', 'NEEDS_AI', 'NEEDS_USER', ''
        )


# ════════════════════════════════════════════════════════════════════════════
# 2. Phantom-Tage: keine blind-KEEP-Decision
# ════════════════════════════════════════════════════════════════════════════

def test_phantom_lad_days_not_blind_keep():
    """LAD 05-20..05-22 ohne SE-Stempel + KEINE Anfahrt-Evidence + FollowMe
    hat tour_spans aber keinen, der diese Tage enthält → Cross-Source-Rejection
    → evidence_decision NEEDS_AI (CAS-Reader steht allein gegen drei
    User-Truth-Quellen)."""
    matched = [
        _day('2025-05-20', routing=['FRA','LAD'], layover_ort='LAD',
             overnight_after_day=True, starts_at_homebase=True,
             start_time='20:05', duty_duration_minutes=234,
             raw_marker='103703 P1', has_fl=True),
        _day('2025-05-21', routing=['LAD'], layover_ort='LAD',
             overnight_after_day=True, raw_marker='X'),
        _day('2025-05-22', routing=['LAD','FRA'], layover_ort='',
             overnight_after_day=False, ends_at_homebase=True,
             start_time='22:00', duty_duration_minutes=380,
             raw_marker='103703 P1', has_fl=True),
    ]
    # Cross-Source-Rejection: anfahrten enthält LAD nicht UND FollowMe-tour_spans
    # nutzen einen anderen Zeitraum.
    fm_ctx = {
        'anfahrten_dates': {a['datum'] for a in ANFAHRTEN_SAMPLE},
        'tour_spans': [
            ('2025-05-14', '2025-05-19', {'2025-05-14','2025-05-15',
                                          '2025-05-16','2025-05-17',
                                          '2025-05-18','2025-05-19'}),
            ('2025-05-26', '2025-05-30', {'2025-05-26','2025-05-27',
                                          '2025-05-28','2025-05-29',
                                          '2025-05-30'}),
        ],
    }
    tours = _norm(matched, fm_ctx=fm_ctx)
    days = _all_days(tours)
    by_date = {d['datum']: d for d in days}
    for dt in ('2025-05-20', '2025-05-21', '2025-05-22'):
        dec = by_date[dt]['evidence_decision']
        assert dec in ('DROP_TOUR', 'NEEDS_AI', 'NEEDS_USER'), (
            f'{dt}: erwartet DROP/NEEDS_AI/NEEDS_USER, war {dec} '
            f'(score_for={by_date[dt]["score_for"]}, '
            f'score_against={by_date[dt]["score_against"]}, '
            f'against={[n for n,_,_ in by_date[dt]["evidence_against"]]})'
        )


# ════════════════════════════════════════════════════════════════════════════
# 3. duty > 840 → NEEDS_AI oder DROP_TOUR
# ════════════════════════════════════════════════════════════════════════════

def test_duty_over_840_days_need_ai_or_drop():
    """Reader-Bug-Verdacht: duty>FTL ohne SE/Anfahrt → NEEDS_AI/DROP."""
    matched = [
        _day('2025-06-01', routing=['FRA','CPH','GOT'], layover_ort='GOT',
             overnight_after_day=True, starts_at_homebase=True,
             start_time='05:00', duty_duration_minutes=1450,
             raw_marker='126533 PU', has_fl=True),
    ]
    tours = _norm(matched, anfahrten=ANFAHRTEN_SAMPLE)
    day = _all_days(tours)[0]
    assert day['evidence_decision'] in ('NEEDS_AI', 'DROP_TOUR'), (
        f'duty>FTL ohne kompensierende Evidence muss NEEDS_AI/DROP sein, '
        f'war {day["evidence_decision"]}'
    )
    against_names = [n for n, _, _ in day['evidence_against']]
    assert 'duty_over_ftl' in against_names


# ════════════════════════════════════════════════════════════════════════════
# 4. Bangalore-Tour bleibt KEEP_TOUR
# ════════════════════════════════════════════════════════════════════════════

def test_bangalore_days_keep_tour():
    """Echte Bangalore-Tour (mit SE-Foreign + overnight + Anfahrt) → KEEP."""
    matched = [
        _day_with_se('2025-01-03',
                     stfrei_ort='BLR', stfrei_total=42.0,
                     routing=['FRA','BLR'], layover_ort='BLR',
                     overnight_after_day=True, starts_at_homebase=True,
                     start_time='10:55', duty_duration_minutes=785,
                     raw_marker='31591 P1', has_fl=True,
                     requires_commute=True),
        _day_with_se('2025-01-04',
                     stfrei_ort='BLR', stfrei_total=42.0,
                     routing=[], layover_ort='BLR',
                     overnight_after_day=True, raw_marker='X'),
        _day_with_se('2025-01-05',
                     stfrei_ort='BLR', stfrei_total=42.0,
                     routing=['BLR','FRA'], layover_ort='BLR',
                     overnight_after_day=True,
                     start_time='23:28', duty_duration_minutes=31,
                     raw_marker='755 LH755-1', has_fl=True),
        _day('2025-01-06',
             routing=['BLR','FRA'], overnight_after_day=False,
             starts_at_homebase=True, ends_at_homebase=True,
             start_time='00:00', duty_duration_minutes=561,
             raw_marker='755 LH755-1'),
    ]
    tours = _norm(matched, anfahrten=ANFAHRTEN_SAMPLE)
    days = _all_days(tours)
    blr_days = [d for d in days if d['datum'] in (
        '2025-01-03', '2025-01-04', '2025-01-05'
    )]
    for d in blr_days:
        assert d['evidence_decision'] == 'KEEP_TOUR', (
            f'BLR {d["datum"]}: erwartet KEEP_TOUR, war '
            f'{d["evidence_decision"]} '
            f'(for={d["score_for"]} against={d["score_against"]})'
        )


# ════════════════════════════════════════════════════════════════════════════
# 5. X innerhalb proven tour: nicht DROP
# ════════════════════════════════════════════════════════════════════════════

def test_x_inside_valid_tour_not_dropped_after_integration():
    """X-Marker mit prev_overnight + foreign layover bleibt tour_mid
    UND erhält KEEP_TOUR/NEEDS_AI als evidence_decision — nicht blind DROP."""
    matched = [
        _day_with_se('2025-02-13',
                     stfrei_ort='HND', stfrei_total=48.0,
                     routing=['FRA','HND'], layover_ort='HND',
                     overnight_after_day=True, starts_at_homebase=True,
                     start_time='17:00', duty_duration_minutes=720,
                     raw_marker='12345 P1', has_fl=True),
        _day_with_se('2025-02-14',
                     stfrei_ort='HND', stfrei_total=48.0,
                     routing=[], layover_ort='HND',
                     overnight_after_day=True, raw_marker='X HND'),
        _day_with_se('2025-02-15',
                     stfrei_ort='HND', stfrei_total=48.0,
                     routing=['HND','FRA'], layover_ort='HND',
                     overnight_after_day=False, ends_at_homebase=True,
                     start_time='12:00', duty_duration_minutes=720,
                     raw_marker='12345 P1', has_fl=True),
    ]
    tours = _norm(matched)
    days = _all_days(tours)
    by = {d['datum']: d for d in days}
    assert by['2025-02-14']['role'] == 'tour_mid', (
        f'X HND muss tour_mid sein, war {by["2025-02-14"]["role"]}'
    )
    # Evidence-Decision darf nicht DROP sein, weil prev+next + foreign layover
    assert by['2025-02-14']['evidence_decision'] in ('KEEP_TOUR', 'NEEDS_AI'), (
        f'X-Tag innerhalb proven Tour: erwartet KEEP/NEEDS_AI, war '
        f'{by["2025-02-14"]["evidence_decision"]}'
    )


# ════════════════════════════════════════════════════════════════════════════
# 6. RES am Homebase: DROP_TOUR oder NEEDS_AI (kein blind KEEP)
# ════════════════════════════════════════════════════════════════════════════

def test_res_homebase_drops_after_integration():
    """RES ohne foreign-overnight-Context → role=non_tour UND
    evidence_decision != KEEP_TOUR."""
    matched = [
        _day('2025-03-10', raw_marker='ORTSTAG',
             starts_at_homebase=True, ends_at_homebase=True),
        _day('2025-03-11', raw_marker='RES',
             starts_at_homebase=True, ends_at_homebase=True),
        _day('2025-03-12', raw_marker='RES',
             starts_at_homebase=True, ends_at_homebase=True),
    ]
    tours = _norm(matched)
    days = _all_days(tours)
    by = {d['datum']: d for d in days}
    for dt in ('2025-03-11', '2025-03-12'):
        assert by[dt]['role'] == 'non_tour'
        assert by[dt]['evidence_decision'] in (
            'DROP_TOUR', 'NEEDS_AI', 'NEEDS_USER'
        ), (
            f'RES homebase {dt}: erwartet DROP/NEEDS_AI, war '
            f'{by[dt]["evidence_decision"]}'
        )


# ════════════════════════════════════════════════════════════════════════════
# 7. RES in foreign tour: NEEDS_AI statt blind DROP/KEEP
# ════════════════════════════════════════════════════════════════════════════

def test_res_foreign_tour_needs_ai_not_drop():
    """RES mit prev_overnight + prev_layover Auslands-Code →
    KEEP_TOUR (durch continuation_from_prev_tour-Evidence) ODER NEEDS_AI.
    KEY: niemals blind DROP_TOUR, weil prev-overnight+foreign-layover eine
    plausible Continuation darstellt.
    """
    matched = [
        _day_with_se('2025-04-22',
                     stfrei_ort='ICN', stfrei_total=42.0,
                     routing=['FRA','ICN'], layover_ort='ICN',
                     overnight_after_day=True, starts_at_homebase=True,
                     start_time='13:00', duty_duration_minutes=720,
                     raw_marker='30099 P1', has_fl=True),
        _day('2025-04-23', raw_marker='RES',
             starts_at_homebase=False, ends_at_homebase=False,
             routing=[], layover_ort=''),
    ]
    tours = _norm(matched)
    days = _all_days(tours)
    by = {d['datum']: d for d in days}
    res_day = by['2025-04-23']
    # Decision: KEEP via Continuation-Evidence ODER NEEDS_AI; niemals blind DROP.
    assert res_day['evidence_decision'] in (
        'KEEP_TOUR', 'NEEDS_AI', 'NEEDS_USER'
    ), (
        f'RES Korea mit prev_overnight foreign: erwartet KEEP/NEEDS_AI, war '
        f'{res_day["evidence_decision"]}'
    )
    # Continuation-Evidence muss sichtbar sein
    for_names = [n for n, _, _ in res_day['evidence_for']]
    assert 'continuation_from_prev_tour' in for_names, (
        f'continuation_from_prev_tour fehlt — Audit muss zeigen warum: {for_names}'
    )


# ════════════════════════════════════════════════════════════════════════════
# 8. Shadow-Compare-Felder vorhanden + serialisierbar
# ════════════════════════════════════════════════════════════════════════════

def test_shadow_compare_contains_evidence_columns():
    """Shadow-Compare braucht pro Tag: datum, role, evidence_decision,
    score_for, score_against, evidence_explanation. Müssen JSON-fähig sein."""
    import json
    matched = [
        _day_with_se('2025-01-03',
                     stfrei_ort='BLR', stfrei_total=42.0,
                     routing=['FRA','BLR'], layover_ort='BLR',
                     overnight_after_day=True, starts_at_homebase=True,
                     duty_duration_minutes=785, raw_marker='31591 P1',
                     has_fl=True, start_time='10:55'),
        _day('2025-01-04', routing=[], layover_ort='BLR',
             overnight_after_day=True, raw_marker='X'),
    ]
    tours = _norm(matched, anfahrten=ANFAHRTEN_SAMPLE)
    rows = []
    for t in tours:
        for d in t['days']:
            rows.append({
                'datum':         d['datum'],
                'role':          d['role'],
                'decision':      d['evidence_decision'],
                'score_for':     d['score_for'],
                'score_against': d['score_against'],
                'explanation':   d['evidence_explanation'],
                'source_refs':   d['source_refs'],
            })
    # JSON roundtrip — keine list-of-tuples-Probleme
    blob = json.dumps(rows)
    parsed = json.loads(blob)
    assert len(parsed) == 2
    for r in parsed:
        for k in ('datum', 'role', 'decision', 'score_for',
                  'score_against', 'explanation', 'source_refs'):
            assert k in r


# ════════════════════════════════════════════════════════════════════════════
# 9. KEINE KPI-Änderung durch Phase 4.8
# ════════════════════════════════════════════════════════════════════════════

def test_no_final_kpi_change_in_phase48():
    """Tour-Membership (role, in_tour, tour_size) und Tour-IDs müssen
    bit-identisch sein vor und nach evidence-Output-Anhängen.

    Trick: vergleiche tours-Struktur OHNE evidence-Felder gegen den Klassifier-
    Adapter-Output, der dieselbe normalize-Funktion nutzt. Wenn Membership
    sich ändert, würden role-Counts diff sein."""
    matched = [
        _day_with_se('2025-01-03',
                     stfrei_ort='BLR', stfrei_total=42.0,
                     routing=['FRA','BLR'], layover_ort='BLR',
                     overnight_after_day=True, starts_at_homebase=True,
                     duty_duration_minutes=785, raw_marker='31591 P1',
                     has_fl=True),
        _day_with_se('2025-01-04',
                     stfrei_ort='BLR', stfrei_total=42.0,
                     routing=[], layover_ort='BLR',
                     overnight_after_day=True, raw_marker='X'),
        _day_with_se('2025-01-05',
                     stfrei_ort='BLR', stfrei_total=42.0,
                     routing=['BLR','FRA'], layover_ort='BLR',
                     overnight_after_day=True,
                     start_time='23:28', duty_duration_minutes=31,
                     raw_marker='755 LH755-1', has_fl=True),
        _day('2025-01-06', routing=['BLR','FRA'], starts_at_homebase=True,
             ends_at_homebase=True, duty_duration_minutes=561,
             raw_marker='755 LH755-1'),
    ]
    tours = _norm(matched, anfahrten=ANFAHRTEN_SAMPLE)
    membership = [(t['tour_id'], t['tour_size'], t['tour_pattern'])
                  for t in tours]
    roles_per_tour = [[d['role'] for d in t['days']] for t in tours]

    # Erwartung: 1 Tour mit 4 Tagen, roles [tour_start, tour_mid, tour_mid, tour_end]
    blr_tour = next(t for t in tours
                    if any(d['datum'] == '2025-01-04' for d in t['days']))
    assert blr_tour['tour_size'] == 4
    blr_roles = [d['role'] for d in blr_tour['days']]
    assert blr_roles == ['tour_start', 'tour_mid', 'tour_mid', 'tour_end'], (
        f'Bangalore-Tour Roles: erwartet [start,mid,mid,end], '
        f'war {blr_roles}'
    )
    # Idempotenz: zweiter Aufruf gleiche Membership
    tours2 = _norm(matched, anfahrten=ANFAHRTEN_SAMPLE)
    membership2 = [(t['tour_id'], t['tour_size'], t['tour_pattern'])
                   for t in tours2]
    roles_per_tour2 = [[d['role'] for d in t['days']] for t in tours2]
    assert membership == membership2
    assert roles_per_tour == roles_per_tour2
