"""BH-CORE-001 Phase 6b — Counter-Finalisierung im Tour-First-Pfad.

Tests verifizieren gezielte Counter-Fixes:
- Cluster C3: Tour-Start mit late-evening-briefing (>=18:00) → Z73 statt Z76
- Cluster C5/C8: Non-tour Inland-Schulungstag mit duty>=480 → Z72
- Z72 NUR bei inland-only routing (Foreign-routing → Issue)

Counter aus normalized_tours sauber:
- arbeitstag-Logik
- Z72/Z73/Z76-Klassifikation
- keine Tibor-Hardcoding
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
    not hasattr(app_module, '_classify_days_from_normalized_tours'),
    reason='Phase 6b erfordert Tour-First-Classifier',
)


def _day(datum, **kw):
    base = {
        'datum': datum, 'activity_type': 'tour', 'routing': [],
        'layover_ort': '', 'overnight_after_day': False,
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


def _matched(datum, dp, se=None):
    return {'datum': datum, 'dp': dp, 'se': se or _se()}


def _classify(matched, anfahrten=None):
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025, known_anfahrten=anfahrten,
    )
    return app_module._classify_days_from_normalized_tours(
        tours, year=2025, homebase='FRA',
    )


# ════════════════════════════════════════════════════════════════════════════
# C3: Tour-Start late-evening → Z73 statt Z76
# ════════════════════════════════════════════════════════════════════════════

def test_tour_start_late_evening_briefing_is_z73_not_z76():
    """Tour-Start mit start_time 19:00 → Z73 Inland-Anreise, weil crew
    übernachtet in DE nach late briefing/positioning."""
    matched = [
        _matched('2025-04-08', _day(
            '2025-04-08', routing=['FRA','ICN'], layover_ort='ICN',
            overnight_after_day=True, starts_at_homebase=True,
            start_time='19:30', duty_duration_minutes=290,
            raw_marker='90064 P1', has_fl=True),
            se=_se()),
        _matched('2025-04-09', _day(
            '2025-04-09', routing=[], layover_ort='ICN',
            overnight_after_day=True, raw_marker='X'),
            se=_se(stfrei_ort='ICN', stfrei_total=42.0,
                   stfrei_inland=False, count=1)),
    ]
    result = _classify(matched)
    by = {d['datum']: d for d in result['tage_detail']}
    assert by['2025-04-08']['klass'] == 'Z73', (
        f'Late-evening tour-start (19:30): erwartet Z73, war '
        f'{by["2025-04-08"]["klass"]} mit reason='
        f'{by["2025-04-08"]["reason"]}'
    )


def test_tour_start_morning_briefing_is_z76():
    """Tour-Start mit start_time 10:55 (morning) → Z76 An/Ab foreign."""
    matched = [
        _matched('2025-01-03', _day(
            '2025-01-03', routing=['FRA','BLR'], layover_ort='BLR',
            overnight_after_day=True, starts_at_homebase=True,
            start_time='10:55', duty_duration_minutes=785,
            raw_marker='31591 P1', has_fl=True),
            se=_se(stfrei_ort='BLR', stfrei_total=42.0,
                   stfrei_inland=False, count=1)),
        _matched('2025-01-04', _day(
            '2025-01-04', routing=[], layover_ort='BLR',
            overnight_after_day=True, raw_marker='X'),
            se=_se(stfrei_ort='BLR', stfrei_total=42.0,
                   stfrei_inland=False, count=1)),
    ]
    result = _classify(matched)
    by = {d['datum']: d for d in result['tage_detail']}
    assert by['2025-01-03']['klass'] == 'Z76', (
        f'Morning tour-start: erwartet Z76, war {by["2025-01-03"]["klass"]}'
    )


# ════════════════════════════════════════════════════════════════════════════
# C5/C8: Non-tour Inland Schulungstag → Z72
# ════════════════════════════════════════════════════════════════════════════

def test_non_tour_inland_schulung_8h_is_z72():
    """Schulungstag (kein routing, duty>=480min, am Homebase) → Z72."""
    matched = [
        _matched('2025-03-18', _day(
            '2025-03-18', activity_type='schulung',
            routing=[], starts_at_homebase=True, ends_at_homebase=True,
            duty_duration_minutes=510, raw_marker='EH 4 SECCRM 4',
            has_fl=False), se=_se()),
    ]
    result = _classify(matched)
    by = {d['datum']: d for d in result['tage_detail']}
    assert by['2025-03-18']['klass'] == 'Z72', (
        f'EH/SECCRM Schulung: erwartet Z72, war {by["2025-03-18"]["klass"]}'
    )
    assert by['2025-03-18']['counted_as_workday'] is True


# ════════════════════════════════════════════════════════════════════════════
# C5: Non-tour mit duty>=480 + Foreign-routing → KEIN Z72 (Issue stattdessen)
# ════════════════════════════════════════════════════════════════════════════

def test_non_tour_with_foreign_routing_is_not_z72():
    """Non-tour Tag mit routing zeigt foreign (z.B. OTP→FRA→LHR) →
    NICHT Z72 — Tour-Boundary-Issue. KEIN Workday-Count, keine fälschliche
    Inland-Klassifikation."""
    matched = [
        _matched('2025-07-03', _day(
            '2025-07-03', activity_type='same_day',
            routing=['OTP','FRA','LHR'], starts_at_homebase=True,
            ends_at_homebase=True, start_time='03:00',
            duty_duration_minutes=485, raw_marker='129023 PU / Tag 3',
            has_fl=False), se=_se()),
    ]
    result = _classify(matched)
    by = {d['datum']: d for d in result['tage_detail']}
    assert by['2025-07-03']['klass'] != 'Z72', (
        f'Non-tour mit foreign routing darf NICHT Z72 sein, war '
        f'{by["2025-07-03"]["klass"]}'
    )


# ════════════════════════════════════════════════════════════════════════════
# Non-tour mit duty < 480 → Office, kein Z72
# ════════════════════════════════════════════════════════════════════════════

def test_non_tour_short_duty_is_office_not_z72():
    """Non-tour Inland mit duty < 480min → Office (NO_VMA), kein Z72."""
    matched = [
        _matched('2025-09-30', _day(
            '2025-09-30', routing=[], starts_at_homebase=True,
            ends_at_homebase=True, duty_duration_minutes=240,
            raw_marker='OFFICE_4H'), se=_se()),
    ]
    result = _classify(matched)
    by = {d['datum']: d for d in result['tage_detail']}
    assert by['2025-09-30']['klass'] in ('Office', 'Frei'), (
        f'Short-duty office: erwartet Office/Frei, war '
        f'{by["2025-09-30"]["klass"]}'
    )


# ════════════════════════════════════════════════════════════════════════════
# Bangalore-Tour weiterhin korrekt
# ════════════════════════════════════════════════════════════════════════════

def test_bangalore_4day_tour_still_z76():
    """Bangalore 4-Tage-Tour bleibt alle Z76 nach Phase 6b."""
    se_blr = _se(stfrei_ort='BLR', stfrei_total=42.0,
                 stfrei_inland=False, count=1)
    matched = [
        _matched('2025-01-03', _day(
            '2025-01-03', routing=['FRA','BLR'], layover_ort='BLR',
            overnight_after_day=True, starts_at_homebase=True,
            start_time='10:55', duty_duration_minutes=785,
            raw_marker='31591 P1', has_fl=True), se=se_blr),
        _matched('2025-01-04', _day(
            '2025-01-04', routing=[], layover_ort='BLR',
            overnight_after_day=True, raw_marker='X'), se=se_blr),
        _matched('2025-01-05', _day(
            '2025-01-05', routing=['BLR','FRA'], layover_ort='BLR',
            overnight_after_day=True, start_time='23:28',
            duty_duration_minutes=31, raw_marker='755 LH755-1',
            has_fl=True), se=se_blr),
        _matched('2025-01-06', _day(
            '2025-01-06', routing=['BLR','FRA'], starts_at_homebase=True,
            ends_at_homebase=True, start_time='00:00',
            duty_duration_minutes=561, raw_marker='755 LH755-1')),
    ]
    result = _classify(matched)
    by = {d['datum']: d for d in result['tage_detail']}
    for dt in ('2025-01-03','2025-01-04','2025-01-05','2025-01-06'):
        assert by[dt]['klass'] == 'Z76', (
            f'BLR {dt}: erwartet Z76, war {by[dt]["klass"]} '
            f'mit reason={by[dt]["reason"]}'
        )


# ════════════════════════════════════════════════════════════════════════════
# Anti-Tibor-Hardcoding: Z72-Schulungs-Regel gilt allgemein
# ════════════════════════════════════════════════════════════════════════════

def test_z72_inland_schulung_works_for_any_marker():
    """Egal welcher Marker (PU, CRM, TRG, SCHULUNG) — entscheidend ist:
    am Homebase + has_real_duty + duty>=480 + KEIN foreign routing → Z72."""
    for marker in ('PU TRG', 'CRM Refresher', 'SCHULUNG', 'GENERIC_8H'):
        matched = [
            _matched('2025-08-15', _day(
                '2025-08-15', routing=[], starts_at_homebase=True,
                ends_at_homebase=True, duty_duration_minutes=485,
                raw_marker=marker), se=_se()),
        ]
        result = _classify(matched)
        by = {d['datum']: d for d in result['tage_detail']}
        assert by['2025-08-15']['klass'] == 'Z72', (
            f'Marker {marker}: erwartet Z72 (allgemeine Schulungs-Regel), '
            f'war {by["2025-08-15"]["klass"]}'
        )
