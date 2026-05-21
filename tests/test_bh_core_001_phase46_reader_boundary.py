"""BH-CORE-001 Phase 4.6 Stufe 1+2 — Reader-Plausi + Tour-Boundary.

Stufe 1 — Reader-Plausibilität:
  A) duty_duration_minutes > 840 → reader_warning, kein Tour-Start
  B) Day-Suffix (Tag N, Day N, N≥2) → Continuation des Vortags, NICHT eigener Tour-Start
  C) Routing-Time-Cross-Check → Reader-Bug-flag
  D) Marker-Sequence-ID nicht als Tour-Beweis (numerischer Pattern)

Stufe 2 — Tour-Boundary mit normal_anfahrten_einzelliste:
  Tour-Start nur wenn Datum in Anfahrt-Liste ODER alternative-Evidence.

Deterministisch, OHNE KI, OHNE User-Review.
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
    not hasattr(app_module, '_normalize_tours_from_raw_facts'),
    reason='BH-CORE-001 Phase 1 not implemented'
)


# 2026-05-19 User-Korrektur: hard-anfahrt-drop war Tibor-Overfitting.
# Phase 4.7 Evidence Engine ersetzt diese Tests. Tests die hard-drop
# testen sind jetzt obsolet; siehe tests/test_evidence_engine.py.
_OBSOLETE_BY_47 = pytest.mark.skip(
    reason='Replaced by Phase 4.7 Evidence Engine (test_evidence_engine.py). '
           'Hard-anfahrt-drop war Tibor-Overfitting.'
)

# 2026-05-20 v11 Closeout: Phase E erweitert auf 3-source CAS-only Override
# (cas_at=tour + foreign-route + foreign-layover + overnight). Plus Day-Suffix-
# with-Tour-Evidence retroactive Day-1-Activation. Tests die FTL-strict-drop
# fuer Day-1 fordern, sind obsolet — der neue Closeout-Pfad erkennt die Tour
# trotz duty>FTL wenn andere CAS-Evidence stark ist (Master „CAS+SE+Plausi sind
# Primaerquelle"). Siehe docs/CLOSEOUT1_DISAGREEMENT_AUDIT.md.
_OBSOLETE_BY_CLOSEOUT = pytest.mark.skip(
    reason='Closeout 2026-05-20: Phase E + Day-Suffix-Continuation ueberschreiben '
           'FTL-strict-drop wenn andere CAS-Evidence vorhanden. Master „CAS+SE sind '
           'Primaerquelle". Siehe docs/CLOSEOUT1_DISAGREEMENT_AUDIT.md.'
)


# ── Tibor's normal_anfahrten_einzelliste — Tour-Starts mit Datum ──────────
# Synthese aus tests/fixtures/followme_golden_tibor_2025.json
TIBOR_ANFAHRTEN_2025 = [
    {'datum': '2025-01-03', 'beginn': '10:15'},
    {'datum': '2025-01-10', 'beginn': '07:50'},
    {'datum': '2025-01-11', 'beginn': '05:45'},
    {'datum': '2025-01-18', 'beginn': '19:20'},
    {'datum': '2025-02-10', 'beginn': '04:45'},
    {'datum': '2025-05-14', 'beginn': '08:15'},
    {'datum': '2025-05-26', 'beginn': '14:50'},
    {'datum': '2025-06-04', 'beginn': '07:20'},
    {'datum': '2025-09-20', 'beginn': '07:10'},
    {'datum': '2025-09-26', 'beginn': '06:00'},
    {'datum': '2025-10-23', 'beginn': '20:10'},
    {'datum': '2025-10-31', 'beginn': '07:00'},
    {'datum': '2025-12-14', 'beginn': '08:30'},
    # ... (gesamt 53 in Golden; hier nur relevante für die Tests)
]


def _make_day(datum, **kwargs):
    dp = {
        'datum': datum,
        'activity_type': 'tour',
        'routing': [], 'layover_ort': '', 'overnight_after_day': False,
        'start_time': '', 'end_time': '', 'duty_duration_minutes': 0,
        'raw_marker': '', 'has_fl': False, 'is_workday': True,
        'requires_commute': False, 'starts_at_homebase': False,
        'ends_at_homebase': False, 'raw_lines': [], 'confidence': 0.9,
    }
    dp.update(kwargs)
    return {'datum': datum, 'dp': dp, 'se': {
        'stfrei_total': 0.0, 'stfrei_ort': '', 'stfrei_inland': None,
        'zwoelftel': 0, 'lines': [], 'count': 0,
    }}


def _norm(matched, anfahrten=None):
    return app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025,
        known_anfahrten=anfahrten,
    )


# ═════════ A: LAD-Phantom-Tour ═════════

@_OBSOLETE_BY_47
def test_lad_0520_no_anfahrt_drop_from_tour():
    """05-20 routing=FRA→LAD overnight ABER nicht in Tibor's Anfahrten → DROP."""
    matched = [
        _make_day('2025-05-20', activity_type='tour', routing=['FRA','LAD'],
                  layover_ort='LAD', overnight_after_day=True,
                  starts_at_homebase=True, start_time='20:05', end_time='23:59',
                  duty_duration_minutes=234, raw_marker='103703 P1'),
    ]
    tours = _norm(matched, anfahrten=TIBOR_ANFAHRTEN_2025)
    # LAD-Tag darf NICHT als tour_start gezählt sein
    for t in tours:
        for d in t['days']:
            if d['datum'] == '2025-05-20':
                assert d['role'] == 'non_tour', (
                    f'05-20 darf kein tour_start sein (keine Tibor-Anfahrt), '
                    f'war role={d["role"]}'
                )


@_OBSOLETE_BY_47
def test_lad_0521_0523_follow_phantom_tour_drop():
    """05-20 LAD-Phantom + 05-21/22/23 Continuation → alle DROP (kein Tour)."""
    matched = [
        _make_day('2025-05-20', activity_type='tour', routing=['FRA','LAD'],
                  layover_ort='LAD', overnight_after_day=True,
                  starts_at_homebase=True, start_time='20:05',
                  duty_duration_minutes=234, raw_marker='103703 P1'),
        _make_day('2025-05-21', activity_type='tour', routing=['LAD'],
                  layover_ort='LAD', overnight_after_day=True,
                  duty_duration_minutes=270, raw_marker='103703 P1'),
        _make_day('2025-05-22', activity_type='tour', routing=['LAD','FRA'],
                  layover_ort='LAD', overnight_after_day=True,
                  duty_duration_minutes=179, raw_marker='103703 P1'),
        _make_day('2025-05-23', activity_type='same_day', routing=['LAD'],
                  starts_at_homebase=True, ends_at_homebase=True,
                  duty_duration_minutes=330, raw_marker='103703 P1'),
    ]
    tours = _norm(matched, anfahrten=TIBOR_ANFAHRTEN_2025)
    # Keine Tour mit Datum 05-20/21/22/23 als foreign Z76-Tour
    for t in tours:
        for d in t['days']:
            if d['datum'] in ('2025-05-20','2025-05-21','2025-05-22','2025-05-23'):
                assert d['role'] == 'non_tour', (
                    f'{d["datum"]} muss non_tour sein (LAD-Phantom-Drop)'
                )


# ═════════ B: Skandi duty > 840 ═════════

@_OBSOLETE_BY_CLOSEOUT
def test_skandi_0601_duty_gt_840_reader_bug_drop():
    """06-01 duty=1084 > 840 → READER_BUG, kein Tour-Start auch wenn andere Felder OK."""
    matched = [
        _make_day('2025-06-01', activity_type='tour', routing=['FRA','CPH','GOT'],
                  layover_ort='GOT', overnight_after_day=True,
                  starts_at_homebase=True, start_time='05:55',
                  duty_duration_minutes=1084,  # > 840 FTL-Limit
                  raw_marker='126533 PU'),
    ]
    tours = _norm(matched, anfahrten=TIBOR_ANFAHRTEN_2025)
    for t in tours:
        for d in t['days']:
            if d['datum'] == '2025-06-01':
                assert d['role'] == 'non_tour', (
                    f'06-01 duty 1084>840 → READER_BUG, kein tour_start'
                )
                # Optional: reader_warning gesetzt
                evidence_str = ' '.join(d.get('evidence') or [])
                assert ('duty' in evidence_str.lower() or 'plausib' in evidence_str.lower()
                        or 'reader' in evidence_str.lower()), (
                    f'evidence muss duty-Plausi-Fail dokumentieren: {evidence_str}'
                )


@_OBSOLETE_BY_CLOSEOUT
def test_skandi_0602_duty_gt_840_reader_bug_drop():
    """06-02 duty=1189 > 840 → READER_BUG, kein Tour-Mid (Vortag kein echter Tour-Start)."""
    matched = [
        _make_day('2025-06-01', activity_type='tour', routing=['FRA','CPH','GOT'],
                  layover_ort='GOT', overnight_after_day=True,
                  starts_at_homebase=True, duty_duration_minutes=1084,
                  raw_marker='126533 PU'),
        _make_day('2025-06-02', activity_type='tour', routing=['GOT','FRA','SOF'],
                  layover_ort='SOF', overnight_after_day=True,
                  duty_duration_minutes=1189, raw_marker='126533 PU'),
    ]
    tours = _norm(matched, anfahrten=TIBOR_ANFAHRTEN_2025)
    for t in tours:
        for d in t['days']:
            if d['datum'] == '2025-06-02':
                assert d['role'] == 'non_tour', (
                    f'06-02 duty 1189>840 + Vortag READER_BUG → non_tour'
                )


# ═════════ C: KRK 09-25 vs 09-26 Day-Suffix ═════════

@_OBSOLETE_BY_CLOSEOUT
def test_krk_0925_no_anfahrt_tour_starts_0926():
    """09-25 kein Anfahrt-Eintrag, aber 09-26 in Anfahrten (Tour 39).
    → 09-25 = non_tour, 09-26 = tour_start."""
    matched = [
        _make_day('2025-09-25', activity_type='tour', routing=['FRA','BER','KRK'],
                  layover_ort='KRK', overnight_after_day=True,
                  starts_at_homebase=True, start_time='06:20',
                  duty_duration_minutes=1059,  # > 840 → READER_BUG zusätzlich
                  raw_marker='15688 PU'),
        _make_day('2025-09-26', activity_type='tour', routing=['KRK','FRA','IST'],
                  layover_ort='IST', overnight_after_day=True,
                  duty_duration_minutes=600,  # plausibel
                  raw_marker='15688 PU (Day 2)',
                  # Tibor's Anfahrt am 09-26 06:00 → starts_hb=True
                  starts_at_homebase=True, ends_at_homebase=False,
                  has_fl=True),
    ]
    tours = _norm(matched, anfahrten=TIBOR_ANFAHRTEN_2025)
    days_by_date = {}
    for t in tours:
        for d in t['days']:
            days_by_date[d['datum']] = (t, d)
    d25_t, d25 = days_by_date['2025-09-25']
    d26_t, d26 = days_by_date['2025-09-26']
    assert d25['role'] == 'non_tour', (
        f'09-25 kein Anfahrt-Eintrag → non_tour, war {d25["role"]}'
    )
    assert d26['role'] in ('tour_start', 'tour_mid'), (
        f'09-26 mit Anfahrt → tour_start oder tour_mid, war {d26["role"]}'
    )


# ═════════ D: TLV-2 Phantom-Tour ═════════

@_OBSOLETE_BY_47
@_OBSOLETE_BY_47
def test_tlv_1026_no_anfahrt_drop_from_tour():
    """10-26 TLV behauptet aber keine Tibor-Anfahrt → DROP."""
    matched = [
        _make_day('2025-10-26', activity_type='tour', routing=['FRA','TLV'],
                  layover_ort='TLV', overnight_after_day=True,
                  starts_at_homebase=True, start_time='16:30',
                  duty_duration_minutes=449, raw_marker='32935 PU'),
    ]
    tours = _norm(matched, anfahrten=TIBOR_ANFAHRTEN_2025)
    for t in tours:
        for d in t['days']:
            if d['datum'] == '2025-10-26':
                assert d['role'] == 'non_tour'


@_OBSOLETE_BY_47
@_OBSOLETE_BY_47
def test_tlv_1027_1028_follow_phantom_tour_drop():
    """TLV-2-Cluster komplett: alle 3 Tage non_tour."""
    matched = [
        _make_day('2025-10-26', activity_type='tour', routing=['FRA','TLV'],
                  layover_ort='TLV', overnight_after_day=True,
                  starts_at_homebase=True, duty_duration_minutes=449,
                  raw_marker='32935 PU'),
        _make_day('2025-10-27', activity_type='frei', routing=['TLV'],
                  layover_ort='TLV', overnight_after_day=True,
                  raw_marker='X'),
        _make_day('2025-10-28', activity_type='same_day', routing=['TLV','FRA'],
                  starts_at_homebase=True, ends_at_homebase=True,
                  duty_duration_minutes=280, raw_marker='32935 PU'),
    ]
    tours = _norm(matched, anfahrten=TIBOR_ANFAHRTEN_2025)
    for t in tours:
        for d in t['days']:
            if d['datum'] in ('2025-10-26','2025-10-27','2025-10-28'):
                assert d['role'] == 'non_tour', f'{d["datum"]} muss non_tour'


# ═════════ E: JFK 12-15 Tour-Boundary-Bug ═════════

def test_jfk_1215_after_irland_tour_reader_bug_drop():
    """12-14 hat Anfahrt (Tour 51 Irland). 12-15 Marker 'Tag 2' aber
    Anfahrt war nur 12-14 → Tour endete 12-14. 12-15 = non_tour."""
    matched = [
        _make_day('2025-12-14', activity_type='tour', routing=['FRA','JFK'],
                  layover_ort='JFK', overnight_after_day=True,
                  starts_at_homebase=True, start_time='09:10',
                  duty_duration_minutes=889, raw_marker='57783 P1'),
        _make_day('2025-12-15', activity_type='tour', routing=['JFK','FRA'],
                  layover_ort='JFK', overnight_after_day=True,
                  start_time='20:55', duty_duration_minutes=184,
                  raw_marker='57783 P1 Tag 2'),
    ]
    tours = _norm(matched, anfahrten=TIBOR_ANFAHRTEN_2025)
    days_by_date = {}
    for t in tours:
        for d in t['days']:
            days_by_date[d['datum']] = (t, d)
    # 12-14 hat Anfahrt → tour_start
    assert days_by_date['2025-12-14'][1]['role'] in ('tour_start','tour_mid'), (
        '12-14 hat Anfahrt → muss Tour sein'
    )


# ═════════ F: Continuation darf nicht durch missing-anfahrt blockiert werden ═════════

def test_missing_anfahrt_does_not_block_continuation_if_prev_real_tour():
    """Wenn Vortag eine echte Tour ist (Anfahrt-day), darf der Folgetag
    auch ohne eigenen Anfahrt-Eintrag tour_mid sein."""
    matched = [
        # 01-03 Bangalore Anfahrt
        _make_day('2025-01-03', activity_type='tour', routing=['FRA','BLR'],
                  layover_ort='BLR', overnight_after_day=True,
                  starts_at_homebase=True, start_time='10:55',
                  duty_duration_minutes=785, raw_marker='31591 P1'),
        # 01-04 X-Marker, kein Anfahrt — aber Continuation
        _make_day('2025-01-04', activity_type='frei', layover_ort='BLR',
                  overnight_after_day=True, raw_marker='X'),
    ]
    tours = _norm(matched, anfahrten=TIBOR_ANFAHRTEN_2025)
    for t in tours:
        for d in t['days']:
            if d['datum'] == '2025-01-04':
                assert d['role'] == 'tour_mid', (
                    f'01-04 Continuation darf nicht blockiert werden, war {d["role"]}'
                )


# ═════════ G: Bangalore-Tour bleibt ═════════

def test_bangalore_0103_has_anfahrt_and_survives():
    """01-03 Bangalore IST in Tibor's Anfahrten → tour_start bleibt erhalten."""
    matched = [
        _make_day('2025-01-03', activity_type='tour', routing=['FRA','BLR'],
                  layover_ort='BLR', overnight_after_day=True,
                  starts_at_homebase=True, duty_duration_minutes=785,
                  raw_marker='31591 P1'),
    ]
    tours = _norm(matched, anfahrten=TIBOR_ANFAHRTEN_2025)
    for t in tours:
        for d in t['days']:
            if d['datum'] == '2025-01-03':
                assert d['role'] == 'tour_start'


def test_bh003a_0106_still_z76_abreise():
    """BH-003a Bangalore 01-06 muss weiterhin tour_end sein."""
    matched = [
        _make_day('2025-01-03', activity_type='tour', routing=['FRA','BLR'],
                  layover_ort='BLR', overnight_after_day=True,
                  starts_at_homebase=True, duty_duration_minutes=785,
                  raw_marker='31591 P1'),
        _make_day('2025-01-04', activity_type='frei', layover_ort='BLR',
                  overnight_after_day=True, raw_marker='X'),
        _make_day('2025-01-05', activity_type='tour', routing=['BLR','FRA'],
                  layover_ort='BLR', overnight_after_day=True,
                  duty_duration_minutes=31, raw_marker='755 LH755-1'),
        _make_day('2025-01-06', activity_type='same_day', routing=['BLR','FRA'],
                  starts_at_homebase=True, ends_at_homebase=True,
                  duty_duration_minutes=561, raw_marker='755 LH755-1'),
    ]
    tours = _norm(matched, anfahrten=TIBOR_ANFAHRTEN_2025)
    for t in tours:
        for d in t['days']:
            if d['datum'] == '2025-01-06':
                assert d['role'] == 'tour_end'


def test_x_inside_valid_tour_not_dropped():
    """X-Marker in einer Tour MIT Anfahrt-Vortag wird tour_mid."""
    matched = [
        _make_day('2025-01-03', activity_type='tour', routing=['FRA','BLR'],
                  layover_ort='BLR', overnight_after_day=True,
                  starts_at_homebase=True, duty_duration_minutes=785,
                  raw_marker='31591 P1'),
        _make_day('2025-01-04', activity_type='frei', layover_ort='BLR',
                  overnight_after_day=True, raw_marker='X HKG'),
    ]
    tours = _norm(matched, anfahrten=TIBOR_ANFAHRTEN_2025)
    for t in tours:
        for d in t['days']:
            if d['datum'] == '2025-01-04':
                assert d['role'] == 'tour_mid'


# ═════════ H: Phase 5 Out-of-Scope ═════════

def test_res_korea_not_handled_in_phase46():
    """RES Korea braucht KI, ist NICHT durch Phase 4.6 lösbar.
    Aktuell bleibt non_tour, kein crash."""
    matched = [
        _make_day('2025-04-23', activity_type='standby', routing=['FRA'],
                  raw_marker='RES'),
    ]
    tours = _norm(matched, anfahrten=TIBOR_ANFAHRTEN_2025)
    # Kein crash, kein review-Item, kein magisches Z76. Bleibt non_tour.
    for t in tours:
        for d in t['days']:
            if d['datum'] == '2025-04-23':
                assert d['role'] == 'non_tour'


def test_no_user_review_for_11_phantom_days():
    """KEIN needs_review für die 11 Phantom-Tage — deterministisch entschieden."""
    matched = [
        _make_day('2025-05-20', activity_type='tour', routing=['FRA','LAD'],
                  layover_ort='LAD', overnight_after_day=True,
                  starts_at_homebase=True, duty_duration_minutes=234,
                  raw_marker='103703 P1'),
        _make_day('2025-10-26', activity_type='tour', routing=['FRA','TLV'],
                  layover_ort='TLV', overnight_after_day=True,
                  starts_at_homebase=True, duty_duration_minutes=449,
                  raw_marker='32935 PU'),
    ]
    tours = _norm(matched, anfahrten=TIBOR_ANFAHRTEN_2025)
    for t in tours:
        for d in t['days']:
            assert d.get('needs_review') is False, (
                f'{d["datum"]} darf nicht needs_review sein (deterministisch)'
            )


def test_no_ai_required_for_11_phantom_days():
    """Phase 4.6 ruft KEINEN KI-Resolver für die 11 Phantom-Tage."""
    matched = [
        _make_day('2025-05-20', activity_type='tour', routing=['FRA','LAD'],
                  layover_ort='LAD', overnight_after_day=True,
                  starts_at_homebase=True, duty_duration_minutes=234,
                  raw_marker='103703 P1'),
    ]
    from unittest import mock
    with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai') as ai_spy:
        _norm(matched, anfahrten=TIBOR_ANFAHRTEN_2025)
    assert ai_spy.call_count == 0, (
        f'Phase 4.6 darf keinen KI-Call machen, hatte {ai_spy.call_count}'
    )


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
