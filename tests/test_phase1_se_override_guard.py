"""BH-CORE-001 SE-Override Guard tightening.

Aktuelle Logik: `Frei → Z76` bei jedem SE-Stempel-foreign-Tag.
Neue Logik (BH-CORE-001 §6.2): SE-Override braucht zusätzliche Evidenz:
  - prev.overnight=True UND prev.layover_ort=foreign  ODER
  - routing mit Auslands-IATA UND has_fl=True  ODER
  - KI-Resolver tour_context conf >= 0.85 mit role=tour_*

09-27 Anti-Pattern: SE-Inland-Stempel DUS darf NICHT durch CAS-Foreign AGP überschrieben
werden ohne starke Continuation-Evidence (Vortag NICHT overnight=foreign + Folgetag back
zu Homebase).

Phase-0-Status: RED — `_classify_days_from_normalized_tours` existiert nicht.
"""
import os
import sys
import pytest
from unittest import mock

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import app as app_module


pytestmark = pytest.mark.skipif(
    not hasattr(app_module, '_classify_days_from_normalized_tours'),
    reason='BH-CORE-001 Phase 3 noch nicht implementiert: '
           '_classify_days_from_normalized_tours fehlt'
)


def _make_day(datum, **kwargs):
    dp = {
        'datum': datum,
        'activity_type': 'frei',
        'routing': [], 'layover_ort': '', 'overnight_after_day': False,
        'start_time': '', 'end_time': '', 'duty_duration_minutes': 0,
        'raw_marker': '', 'has_fl': False, 'is_workday': True,
        'requires_commute': False, 'starts_at_homebase': False,
        'ends_at_homebase': False, 'raw_lines': [], 'confidence': 0.9,
    }
    dp.update(kwargs)
    se = {
        'stfrei_total': 0.0, 'stfrei_ort': '', 'stfrei_inland': None,
        'zwoelftel': 0, 'lines': [], 'count': 0,
    }
    se.update(kwargs.pop('se', {}) if 'se' in kwargs else {})
    return {'datum': datum, 'dp': dp, 'se': se}


def _run_pipeline(matched, year=2025, homebase='FRA'):
    """Runs normalized_tours → classify pipeline."""
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase=homebase, year=year
    )
    return app_module._classify_days_from_normalized_tours(
        tours, year=year, homebase=homebase
    )


@pytest.mark.skip(
    reason='FinalFix 7 (2026-05-20): Per Master „SE Tour belegt → AeroTAX muss fixen" '
           'wird SE-Foreign-Stempel allein als hinreichend für Z76-Klassifikation '
           'akzeptiert (CLOSEOUT1 §1 Decision B). Dieser defensive Test ist obsolet. '
           'Siehe docs/FINAL_REST_CLUSTER_REPORT.md Fix 7.'
)
def test_se_override_requires_prev_overnight_foreign():
    """SE-Ausland-Stempel allein ohne Tour-Evidence darf KEIN Frei→Z76 triggern."""
    matched = [
        # Vortag: Frei zuhause, kein overnight
        _make_day('2025-10-14', activity_type='frei', raw_marker='ORTSTAG',
                  starts_at_homebase=True, ends_at_homebase=True,
                  overnight_after_day=False),
        # Heute: Frei mit SE-Auslands-Stempel ABER ohne Tour-Indikatoren
        _make_day('2025-10-15', activity_type='frei', raw_marker='X',
                  starts_at_homebase=False, ends_at_homebase=False,
                  overnight_after_day=False, layover_ort='',
                  se={'stfrei_ort': 'MRS', 'stfrei_inland': False,
                      'stfrei_total': 36.0, 'count': 1}),
    ]
    result = _run_pipeline(matched)
    days = {t['datum']: t for t in result['tage_detail']}
    # SE-Override darf NICHT triggern — bleibt Frei oder non_tour
    assert days['2025-10-15']['klass'] != 'Z76', (
        'SE-Override ohne Tour-Evidence darf nicht Frei→Z76 setzen'
    )


def test_se_override_with_prev_overnight_foreign_works():
    """SE-Ausland-Stempel + prev.overnight=foreign → Frei→Z76 OK."""
    matched = [
        _make_day('2025-10-14', activity_type='tour', routing=['FRA','SEA'],
                  layover_ort='SEA', overnight_after_day=True,
                  starts_at_homebase=True, has_fl=True),
        _make_day('2025-10-15', activity_type='frei', raw_marker='OFF',
                  overnight_after_day=True, layover_ort='SEA',
                  se={'stfrei_ort': 'SEA', 'stfrei_inland': False,
                      'stfrei_total': 40.0, 'count': 1}),
    ]
    result = _run_pipeline(matched)
    days = {t['datum']: t for t in result['tage_detail']}
    # Mit Tour-Continuation darf Z76 gesetzt werden
    assert days['2025-10-15']['klass'] == 'Z76'


@pytest.mark.skip(reason='BH-CORE-001 Phase 4: 09-27 Inland-Detection nach Tour-End-Analyse')
def test_se_inland_stamp_not_overridden_without_continuation():
    """09-27-Pattern: SE-Inland DUS + CAS-Foreign AGP, OHNE Vor/Folgetag-Continuation
    → bleibt Z74 Inland, NICHT Z76 AGP."""
    matched = [
        # 09-25 Tour-Start FRA→BER→KRK
        _make_day('2025-09-25', activity_type='tour',
                  routing=['FRA','BER','KRK'], layover_ort='KRK',
                  overnight_after_day=True, starts_at_homebase=True,
                  has_fl=True),
        # 09-26 layover IST aber SE-MUC (Inland-Stamp)
        _make_day('2025-09-26', activity_type='tour', routing=['KRK','IST'],
                  layover_ort='IST', overnight_after_day=True,
                  has_fl=True,
                  se={'stfrei_ort': 'MUC', 'stfrei_inland': True,
                      'stfrei_total': 14.0, 'count': 1}),
        # 09-27 layover AGP aber SE-DUS (Inland-Stamp), Folgetag = zuhause
        _make_day('2025-09-27', activity_type='tour', routing=['IST','AGP','DUS'],
                  layover_ort='AGP', overnight_after_day=True,
                  has_fl=True,
                  se={'stfrei_ort': 'DUS', 'stfrei_inland': True,
                      'stfrei_total': 28.0, 'count': 1}),
        _make_day('2025-09-28', activity_type='tour', routing=['DUS','FRA'],
                  overnight_after_day=False, ends_at_homebase=True,
                  has_fl=True),
    ]
    result = _run_pipeline(matched)
    days = {t['datum']: t for t in result['tage_detail']}
    # Golden sagt: Z74 Deutschland für 09-27
    assert days['2025-09-27']['klass'] == 'Z74', (
        f'09-27 mit SE-DUS-Inland muss Z74 sein (Tour endet zuhause am 28.), '
        f'war {days["2025-09-27"]["klass"]}'
    )


@pytest.mark.skip(
    reason='FinalFix 7 (2026-05-20): SE-Foreign-Stempel hat Top-Priority fuer '
           'bmf_place_code (Master „SE Tour belegt"). CAS-routing=[Hb] mit SE-foreign '
           'ist gerade der Foreign-Same-Day-Pattern (CLOSEOUT1 §1 Decision B). '
           'Defensive Guard obsolet. Siehe docs/FINAL_REST_CLUSTER_REPORT.md.'
)
def test_se_override_respects_routing_continuity():
    """Wenn routing zeigt nicht-foreign → kein rescue."""
    matched = [
        _make_day('2025-05-10', activity_type='frei', raw_marker='ORTSTAG',
                  starts_at_homebase=True, ends_at_homebase=True),
        # Heute: Frei mit SE-Auslands-Stamp ABER routing=Inland-only
        _make_day('2025-05-11', activity_type='frei', raw_marker='X',
                  starts_at_homebase=True, ends_at_homebase=True,
                  routing=['FRA'], overnight_after_day=False,
                  se={'stfrei_ort': 'LAX', 'stfrei_inland': False,
                      'stfrei_total': 40.0, 'count': 1}),
    ]
    result = _run_pipeline(matched)
    days = {t['datum']: t for t in result['tage_detail']}
    # routing zeigt nur FRA → kein Z76 (selbst mit SE-Stamp LAX)
    assert days['2025-05-11']['klass'] != 'Z76', (
        'Ohne foreign routing UND ohne overnight-prev darf SE-Stamp allein nicht Z76 triggern'
    )


@pytest.mark.skip(reason='BH-CORE-001 Phase 5: KI-Resolver-Integration')
def test_se_override_ki_low_conf_no_auto_rescue():
    """KI conf<0.85 → needs_review, kein auto-rescue."""
    matched = [
        _make_day('2025-06-30', activity_type='tour', routing=['FRA','TLV'],
                  layover_ort='TLV', overnight_after_day=True,
                  starts_at_homebase=True, has_fl=True),
        _make_day('2025-07-01', activity_type='frei', raw_marker='UNKNOWN',
                  overnight_after_day=True, layover_ort='TLV',
                  se={'stfrei_ort': 'XYZ', 'stfrei_inland': False,
                      'stfrei_total': 40.0, 'count': 1}),
    ]
    # Mock KI mit niedriger conf
    with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai',
                            return_value={'resolved': False, 'value': {},
                                          'confidence': 0.4, 'reason': 'unklar',
                                          'evidence': [], 'needs_review': True}):
        result = _run_pipeline(matched)
    days = {t['datum']: t for t in result['tage_detail']}
    # Mit conf<0.85 darf KI keinen auto-rescue triggern
    day = days['2025-07-01']
    assert day.get('needs_review') is True or day['klass'] != 'Z76', (
        f'KI conf<0.85 → needs_review=True ODER klass!=Z76. '
        f'Got klass={day["klass"]} needs_review={day.get("needs_review")}'
    )


def test_se_override_logs_evidence_in_audit_trail():
    """Rescue-audit-note enthält evidence list."""
    matched = [
        _make_day('2025-08-01', activity_type='tour', routing=['FRA','SVG'],
                  layover_ort='SVG', overnight_after_day=True,
                  starts_at_homebase=True, has_fl=True),
        _make_day('2025-08-02', activity_type='frei', raw_marker='OFF',
                  overnight_after_day=True, layover_ort='SVG',
                  se={'stfrei_ort': 'SVG', 'stfrei_inland': False,
                      'stfrei_total': 50.0, 'count': 1}),
    ]
    result = _run_pipeline(matched)
    rescues = result.get('_rescues') or result.get('rescues') or []
    se_rescues = [r for r in rescues
                  if 'se_override' in (r.get('rescue_type') or '').lower()
                  or 'frei_to_z76' in (r.get('rescue_type') or '').lower()]
    if se_rescues:
        # Mindestens 1 Rescue muss evidence-list enthalten
        for r in se_rescues:
            evidence = r.get('evidence') or r.get('reason') or ''
            assert evidence, (
                f'SE-Override-Rescue muss evidence haben: {r}'
            )


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
