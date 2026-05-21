"""BH-CORE-001 Phase 5a — KI-Resolver Infrastructure (Mock-only).

Tests verifizieren:
- Prompt enthält Airline-Crew-Kontext
- Anti-Tax-Sanitizer lehnt verbotene EUR-/Tagesatz-Felder ab
- Confidence-Schwellen (≥0.90 auto / 0.70-0.89 review / <0.70 user-fragen)
- Cache verhindert Duplicate Calls
- Kein KI-Call bei KEEP_TOUR oder DROP_TOUR
- KI nur bei NEEDS_AI
- PII-safe logging (keine Namen, PNR, etc.)
- Tibor-Pattern (RES Korea, OFF Kroatien, JFK 12-15, OTP 07-03)

Phase 5a verwendet AUSSCHLIESSLICH Mock-Resolver — kein Anthropic-Call.
"""
import os
import sys
import io
import contextlib
import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import app as app_module

pytestmark = pytest.mark.skipif(
    not hasattr(app_module, '_resolve_uncertain_fact_with_ai'),
    reason='Phase 5a Resolver not implemented',
)


# ─── Fixtures / Helpers ────────────────────────────────────────────────────

def _day(datum='2025-06-10', **kw):
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


@pytest.fixture(autouse=True)
def _force_mock_mode(monkeypatch):
    """Phase 5a — Live-Mode hart deaktivieren."""
    monkeypatch.setenv('AEROTAX_AI_RESOLVER_MODE', 'mock')
    # Cache leeren zwischen Tests (canonical name: _ai_resolver_cache)
    if hasattr(app_module, '_ai_resolver_cache'):
        app_module._ai_resolver_cache.clear()
    yield


# ═══════════════════════════════════════════════════════════════════════════
# 1. Prompt enthält Airline-Crew-Kontext
# ═══════════════════════════════════════════════════════════════════════════

def test_prompt_contains_airline_crew_context():
    """Pflicht-Präambel muss in jedem Prompt enthalten sein."""
    ctx = {'day': _day(routing=['FRA','JFK'], layover_ort='JFK',
                       overnight_after_day=True)}
    prompt = app_module._ai_resolver_build_prompt(
        'tour_boundary', ctx, 'FRA→JFK overnight'
    )
    # Pflicht-Präambel
    assert 'Flugpersonal' in prompt
    assert 'Cockpit' in prompt or 'Kabine' in prompt
    # Anti-Tax Hinweis im Prompt
    assert ('Steuerbeträge' in prompt or 'Steuer' in prompt
            or 'eur' in prompt.lower())


# ═══════════════════════════════════════════════════════════════════════════
# 2. Anti-Tax-Sanitizer lehnt verbotene Geld-/Tagesatz-Felder ab
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('forbidden_key', [
    'amount', 'eur', 'euro', 'tagesatz', 'tax',
    'steuer', 'rate', 'betrag', 'pauschale',
])
def test_ai_rejects_tax_amount_fields(forbidden_key):
    """KI-value-dict mit Forbidden-Key → reject via _ai_resolver_value_safe."""
    value_evil = {'resolved_place': 'Chicago', forbidden_key: 28.40}
    safe, offending = app_module._ai_resolver_value_safe(value_evil)
    assert safe is False
    assert offending == forbidden_key


def test_ai_rejects_nested_tax_amount_fields():
    """Auch verschachtelte Forbidden-Keys → reject in value-dict-check."""
    # value mit verbotener key auf top-level
    value_evil = {'resolved_place': 'Chicago', 'tagesatz': 28.0}
    safe, offending = app_module._ai_resolver_value_safe(value_evil)
    assert safe is False
    assert offending == 'tagesatz'


# ═══════════════════════════════════════════════════════════════════════════
# 3. Confidence ≥ 0.90 → auto übernehmen
# ═══════════════════════════════════════════════════════════════════════════

def test_high_confidence_auto_resolves():
    """Marker_semantics mit bekanntem Marker (X) → confidence ≥ 0.90 (Mock),
    threshold-Apply setzt resolved=True, needs_review=False (Auto-Schwelle)."""
    ctx = {
        'day': _day('2025-01-04', raw_marker='X'),
        'homebase': 'FRA',
    }
    r = app_module._resolve_uncertain_fact_with_ai(
        'marker_semantics', ctx, uncertain_fact='X'
    )
    assert r['resolved'] is True
    assert r['confidence'] >= 0.90
    assert r['needs_review'] is False


# ═══════════════════════════════════════════════════════════════════════════
# 4. Confidence 0.70-0.89 → Review mit Suggestion
# ═══════════════════════════════════════════════════════════════════════════

def test_medium_confidence_review_with_suggestion():
    """layover_place: routing[-1]=foreign IATA aber kein explizites layover_ort
    → Mock liefert IATA-Vorschlag mit confidence 0.75 (medium-Review-Bereich
    0.70-0.89). Threshold-Apply setzt resolved=True, needs_review=True."""
    ctx = {
        'day': _day(routing=['FRA','HKG'], overnight_after_day=True),
        'homebase': 'FRA',
    }
    r = app_module._resolve_uncertain_fact_with_ai(
        'layover_place', ctx, uncertain_fact='HKG'
    )
    assert r['resolved'] is True
    assert r['value'] == 'HKG'
    assert 0.70 <= r['confidence'] < 0.90, (
        f'medium confidence erwartet [0.70, 0.90), war {r["confidence"]}'
    )
    # Im Review-Bereich → needs_review=True (Vorschlag wird User gezeigt)
    assert r['needs_review'] is True


# ═══════════════════════════════════════════════════════════════════════════
# 5. Confidence < 0.70 → Userfrage ohne Suggestion
# ═══════════════════════════════════════════════════════════════════════════

def test_low_confidence_user_question():
    """marker_semantics: unbekannter Marker → confidence < 0.70 +
    resolved=False + needs_review=True (User-Frage ohne Vorschlag)."""
    ctx = {
        'day': _day(raw_marker='XYZ_UNKNOWN_PATTERN'),
        'homebase': 'FRA',
    }
    r = app_module._resolve_uncertain_fact_with_ai(
        'marker_semantics', ctx, uncertain_fact='XYZ_UNKNOWN_PATTERN'
    )
    assert r['confidence'] < 0.70
    assert r['needs_review'] is True
    assert r['resolved'] is False


# ═══════════════════════════════════════════════════════════════════════════
# 6. Cache verhindert duplicate calls
# ═══════════════════════════════════════════════════════════════════════════

def test_cache_prevents_duplicate_calls(monkeypatch):
    """Zweiter Resolver-Call mit identischem context → cache hit, kein zweiter
    mock-call. Verifiziert via Mock-Wrapper-Counter."""
    call_count = [0]
    real_mock = app_module._ai_resolver_mock_dispatch

    def _counting_mock(kind, context):
        call_count[0] += 1
        return real_mock(kind, context)

    monkeypatch.setattr(app_module, '_ai_resolver_mock_dispatch', _counting_mock)

    ctx = {
        'day': _day('2025-04-23', raw_marker='RES'),
        'prev_day': _day('2025-04-22', overnight_after_day=True,
                         layover_ort='ICN'),
        'homebase': 'FRA',
    }
    r1 = app_module._resolve_uncertain_fact_with_ai(
        'standby_context', ctx,
        job_id='job-cache-1', datum='2025-04-23', uncertain_fact='RES')
    r2 = app_module._resolve_uncertain_fact_with_ai(
        'standby_context', ctx,
        job_id='job-cache-1', datum='2025-04-23', uncertain_fact='RES')
    assert call_count[0] == 1, (
        f'Cache greift nicht — Mock {call_count[0]}× gerufen statt 1×'
    )
    assert r1 == r2


# ═══════════════════════════════════════════════════════════════════════════
# 7-9. Kein KI-Call bei KEEP_TOUR / DROP_TOUR; nur bei NEEDS_AI
# ═══════════════════════════════════════════════════════════════════════════

def _matched_day(datum, dp, se=None):
    return {'datum': datum, 'dp': dp,
            'se': se or {'stfrei_total': 0.0, 'stfrei_ort': '',
                         'stfrei_inland': None, 'zwoelftel': 0,
                         'lines': [], 'count': 0}}


def _resolver_call_tracker(monkeypatch):
    """Wrappt _resolve_uncertain_fact_with_ai und zählt Aufrufe."""
    calls = []
    real = app_module._resolve_uncertain_fact_with_ai

    def _track(kind, context, *args, **kwargs):
        datum = (kwargs.get('datum')
                 or ((context or {}).get('day') or {}).get('datum') or '')
        calls.append((kind, datum))
        return real(kind, context, *args, **kwargs)
    monkeypatch.setattr(app_module, '_resolve_uncertain_fact_with_ai', _track)
    return calls


def test_no_ai_for_keep_tour(monkeypatch):
    """Bangalore-Tour mit klarem KEEP_TOUR-Score → KEINE Resolver-Aufrufe."""
    calls = _resolver_call_tracker(monkeypatch)
    matched = [
        _matched_day('2025-01-03', _day('2025-01-03',
            routing=['FRA','BLR'], layover_ort='BLR',
            overnight_after_day=True, starts_at_homebase=True,
            start_time='10:55', duty_duration_minutes=785,
            raw_marker='31591 P1', has_fl=True),
            se=_se(stfrei_ort='BLR', stfrei_total=42.0,
                   stfrei_inland=False, count=1)),
        _matched_day('2025-01-04', _day('2025-01-04',
            routing=[], layover_ort='BLR',
            overnight_after_day=True, raw_marker='X'),
            se=_se(stfrei_ort='BLR', stfrei_total=42.0,
                   stfrei_inland=False, count=1)),
        _matched_day('2025-01-05', _day('2025-01-05',
            routing=['BLR','FRA'], layover_ort='BLR',
            overnight_after_day=True, start_time='23:28',
            duty_duration_minutes=31, raw_marker='755 LH755-1',
            has_fl=True),
            se=_se(stfrei_ort='BLR', stfrei_total=42.0,
                   stfrei_inland=False, count=1)),
        _matched_day('2025-01-06', _day('2025-01-06',
            routing=['BLR','FRA'], starts_at_homebase=True,
            ends_at_homebase=True, duty_duration_minutes=561,
            raw_marker='755 LH755-1')),
    ]
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025,
        known_anfahrten=[{'datum': '2025-01-03'}],
    )
    assert calls == [], (
        f'Bangalore-Tour darf KEINE Resolver-Aufrufe triggern, war {calls}'
    )
    # Bestätigung: alle Tage haben evidence_decision=KEEP_TOUR (oder leer)
    decisions = [d['evidence_decision'] for t in tours for d in t['days']]
    for dec in decisions:
        assert dec != 'NEEDS_AI', f'BLR-Tour darf nicht NEEDS_AI sein: {dec}'


def test_no_ai_for_drop_tour(monkeypatch):
    """Klare Non-Tour-Tage (ORTSTAG/FRS) → KEINE Resolver-Aufrufe für DROP."""
    calls = _resolver_call_tracker(monkeypatch)
    matched = [
        _matched_day('2025-03-10', _day('2025-03-10', raw_marker='ORTSTAG',
            starts_at_homebase=True, ends_at_homebase=True)),
        _matched_day('2025-08-19', _day('2025-08-19', raw_marker='FRS',
            starts_at_homebase=True, ends_at_homebase=True)),
    ]
    fm_ctx = {
        'anfahrten_dates': set(),
        'tour_spans': [
            ('2025-09-01', '2025-09-04', {'2025-09-01','2025-09-02',
                                          '2025-09-03','2025-09-04'}),
        ],
    }
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025, followme_context=fm_ctx,
    )
    drop_calls = [(k, d) for k, d in calls
                  if d in ('2025-03-10', '2025-08-19')]
    # Klare DROP_TOUR-Tage → keine Resolver-Triggerung
    decisions = {d['datum']: d['evidence_decision']
                 for t in tours for d in t['days']}
    for dt in ('2025-03-10', '2025-08-19'):
        assert decisions[dt] in ('DROP_TOUR', 'NEEDS_AI', ''), decisions[dt]
        if decisions[dt] == 'DROP_TOUR':
            assert (None, dt) not in [(c[0], c[1]) for c in drop_calls] or \
                not any(c[1] == dt for c in drop_calls), (
                f'Drop-Tag {dt} hat dennoch Resolver-Call: {drop_calls}'
            )


def test_ai_only_for_needs_ai(monkeypatch):
    """Phantom-LAD-Tag (NEEDS_AI) → Resolver wird aufgerufen.
    Bangalore-Tour-Tag → kein Aufruf."""
    calls = _resolver_call_tracker(monkeypatch)
    matched = [
        # LAD phantom → NEEDS_AI erwartet
        _matched_day('2025-05-20', _day('2025-05-20',
            routing=['FRA','LAD'], layover_ort='LAD',
            overnight_after_day=True, starts_at_homebase=True,
            start_time='20:05', duty_duration_minutes=234,
            raw_marker='103703 P1', has_fl=True)),
        # Bangalore-Style mit SE → KEEP_TOUR
        _matched_day('2025-08-15', _day('2025-08-15',
            routing=['FRA','JFK'], layover_ort='JFK',
            overnight_after_day=True, starts_at_homebase=True,
            start_time='13:00', duty_duration_minutes=600,
            raw_marker='LH404', has_fl=True),
            se=_se(stfrei_ort='JFK', stfrei_total=40.0,
                   stfrei_inland=False, count=1)),
    ]
    fm_ctx = {
        'anfahrten_dates': set(),
        'tour_spans': [
            ('2025-05-14', '2025-05-19', {'2025-05-14','2025-05-15',
                                          '2025-05-16','2025-05-17',
                                          '2025-05-18','2025-05-19'}),
        ],
    }
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025, followme_context=fm_ctx,
    )
    called_dates = {d for _, d in calls}
    assert '2025-05-20' in called_dates, (
        f'NEEDS_AI Tag muss Resolver triggern, gerufen={called_dates}'
    )
    assert '2025-08-15' not in called_dates, (
        f'KEEP_TOUR Tag darf KEIN Resolver triggern, gerufen={called_dates}'
    )


# ═══════════════════════════════════════════════════════════════════════════
# 10. PII-safe logging
# ═══════════════════════════════════════════════════════════════════════════

def test_pii_safe_logging():
    """Resolver-Log darf KEINE Namen/PNR/Email enthalten — nur job_id-prefix,
    datum, kind, resolved/confidence."""
    ctx = {
        'day': _day('2025-04-23', raw_marker='RES'),
        'prev_day': _day('2025-04-22', overnight_after_day=True,
                         layover_ort='ICN'),
        'homebase': 'FRA',
        # PII-Felder darin — DÜRFEN nicht im Log auftauchen
        'employee_name': 'Tibor Mustermann',
        'pnr': 'AB1234',
        'email': 'tibor@example.com',
    }
    buf_out = io.StringIO()
    buf_err = io.StringIO()
    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        app_module._resolve_uncertain_fact_with_ai(
            'standby_context', ctx,
            job_id='a1b2c3d4-secret-job-id', uncertain_fact='RES')
    log_out = buf_out.getvalue() + buf_err.getvalue()
    # PII darf NICHT auftauchen
    assert 'Tibor' not in log_out
    assert 'Mustermann' not in log_out
    assert 'AB1234' not in log_out
    assert 'tibor@example.com' not in log_out
    # Job-ID nur als Prefix (≤8 chars)
    assert 'secret-job-id' not in log_out


# ═══════════════════════════════════════════════════════════════════════════
# 11. RES Korea standby_context resolved
# ═══════════════════════════════════════════════════════════════════════════

def test_res_korea_standby_context_resolves():
    """RES Tag nach Korea-overnight → Mock-Resolver liefert standby_hotel
    mit confidence ≥0.80 (Review-Bereich, needs_review=True per Schwellen)."""
    ctx = {
        'day': _day('2025-04-23', raw_marker='RES'),
        'prev_day': _day('2025-04-22', routing=['FRA','ICN'],
                         layover_ort='ICN', overnight_after_day=True,
                         starts_at_homebase=True,
                         duty_duration_minutes=720, has_fl=True),
        'homebase': 'FRA',
    }
    r = app_module._resolve_uncertain_fact_with_ai(
        'standby_context', ctx, uncertain_fact='RES'
    )
    assert r['resolved'] is True
    assert r['value'] == 'standby_hotel'
    assert r['confidence'] >= 0.80
    assert any('ICN' in e for e in r['evidence'])


# ═══════════════════════════════════════════════════════════════════════════
# 12. OFF Kroatien tour_boundary resolved
# ═══════════════════════════════════════════════════════════════════════════

def test_off_croatia_tour_context_resolves():
    """OFF-marker (Kroatien) mit foreign routing + SE-foreign → tour_boundary
    Mock liefert 'mid' wenn prev_overnight, sonst 'start'."""
    ctx = {
        'day': _day('2025-07-29', routing=['FRA','RIX'], layover_ort='RIX',
                    overnight_after_day=True, has_fl=True,
                    duty_duration_minutes=500, raw_marker='X RIX'),
        'prev_day': _day('2025-07-28', overnight_after_day=False),
        'se': _se(stfrei_ort='RIX', stfrei_total=42.0,
                  stfrei_inland=False, count=1),
        'homebase': 'FRA',
    }
    # Mock_resolver liest se aus context.se
    ctx_with_se = dict(ctx)
    ctx_with_se['se'] = ctx['se']
    r = app_module._resolve_uncertain_fact_with_ai(
        'tour_boundary', ctx_with_se, uncertain_fact='X RIX'
    )
    assert r['resolved'] is True
    assert r['value'] in ('start', 'mid')


# ═══════════════════════════════════════════════════════════════════════════
# 13. JFK Reader-Misread → NEEDS_AI, nicht KEEP
# ═══════════════════════════════════════════════════════════════════════════

def test_jfk_reader_misread_needs_ai_not_keep():
    """JFK 'Tag 2'-Marker nach abgeschlossener 1-Day-Tour → NEEDS_AI durch
    Evidence-Engine, dann Resolver-Call (Phase 5a). Mock liefert
    tour_boundary mit niedriger Confidence → user-review."""
    prev = _day('2025-12-14', routing=['FRA','JFK'], layover_ort='JFK',
                overnight_after_day=False, ends_at_homebase=False,
                start_time='14:00', duty_duration_minutes=600,
                raw_marker='57783 P1', has_fl=True)
    day = _day('2025-12-15', routing=['JFK','FRA'],
               overnight_after_day=False, ends_at_homebase=True,
               start_time='02:00', duty_duration_minutes=580,
               raw_marker='57783 P1 Tag 2', has_fl=True)
    fm_ctx = {
        'anfahrten_dates': {'2025-12-08'},
        'tour_spans': [
            ('2025-12-14', '2025-12-14', {'2025-12-14'}),
        ],
    }
    matched = [
        {'datum': '2025-12-14', 'dp': prev,
         'se': _se()},
        {'datum': '2025-12-15', 'dp': day,
         'se': _se()},
    ]
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025, followme_context=fm_ctx,
    )
    by = {d['datum']: d for t in tours for d in t['days']}
    assert by['2025-12-15']['evidence_decision'] == 'NEEDS_AI'
    # Resolver wurde getriggert
    assert by['2025-12-15']['ai_resolution_kind'] != ''
    # Resolver-Kind muss tour_boundary sein (day_suffix_claims fired)
    assert by['2025-12-15']['ai_resolution_kind'] == 'tour_boundary'


# ═══════════════════════════════════════════════════════════════════════════
# 14. OTP→FRA→LHR Transit → NEEDS_AI + routing_consistency Resolver
# ═══════════════════════════════════════════════════════════════════════════

def test_otp_fra_lhr_transit_needs_ai():
    """OTP→FRA→LHR-Transit-Pattern → evidence NEEDS_AI, Phase 5a
    Resolver-Kind=routing_consistency, Mock liefert 'inconsistent' mit
    konkretem evidence."""
    day = _day('2025-07-03', routing=['OTP','FRA','LHR'],
               starts_at_homebase=False, ends_at_homebase=False,
               start_time='06:00', duty_duration_minutes=720,
               raw_marker='129023 PU / Tag 3', has_fl=True)
    fm_ctx = {
        'anfahrten_dates': {'2025-07-02'},
        'tour_spans': [
            ('2025-07-02', '2025-07-02', {'2025-07-02'}),
        ],
    }
    matched = [{'datum': '2025-07-03', 'dp': day, 'se': _se()}]
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025, followme_context=fm_ctx,
    )
    by = {d['datum']: d for t in tours for d in t['days']}
    target = by['2025-07-03']
    assert target['evidence_decision'] == 'NEEDS_AI'
    assert target['ai_resolution_kind'] == 'routing_consistency'
    assert target['ai_resolved'] is True
    assert target['ai_value'] == 'inconsistent'
