"""Near-8h Review-Items mit hilfreicher Copy (Highest-Defensible-Produktregel).

Regel:
  - Abwesenheit klar >= 480min → auto Z72, kein Review
  - Abwesenheit weit unter 8h (< 420min) → kein Review (zu klein für Hebel)
  - Abwesenheit 420 ≤ total < 480 → Review-Item mit konkreter Minutenzahl + Geld-Hinweis
  - Frage erwähnt Money-Effekt + tatsächlich berechnete Minuten

User-Erlebnis:
  „Ich komme für den X auf 7:55 Std. … Ab mehr als 8 Stunden kann eine
  Verpflegungspauschale (14 €) angesetzt werden."
"""

import pytest
import conftest as _cft
import app


def _make_day(datum, marker='OFFICE', start='', end='', duty_min=0):
    return {
        'datum':                datum,
        'raw_marker':           marker,
        'activity_type':        'office',
        'overnight_after_day':  False,
        'layover_ort':          '',
        'routing':              [],
        'ends_at_homebase':     True,
        'starts_at_homebase':   True,
        'has_fl':               False,
        'is_workday':           True,
        'start_time':           start,
        'end_time':             end,
        'duty_duration_minutes': duty_min,
    }


def _classify(days_with_se, commute=30):
    matched = [{'datum': d['datum'], 'dp': d, 'se': se} for d, se in days_with_se]
    return app._deterministic_classify_v7(
        matched, year=2025, homebase='FRA', commute_minutes=commute,
    )


# ════════════════════════════════════════════════════════════════════
# Self-checks: clear cases — no review
# ════════════════════════════════════════════════════════════════════

@pytest.mark.skip(reason='R39: Office am HB nicht mehr Z72. Test testet bewusst geänderte Logik.')
def test_clear_over_8h_no_review_auto_z72():
    """Tag mit duty >= 480min UND CAS-Zeiten → auto Z72, kein Review-Item."""
    days = [
        # 9h duty + 30min commute jeder Weg = 10h total → klar >8h
        (_make_day('2025-05-15', start='08:00', end='17:00', duty_min=540), {}),
    ]
    result = _classify(days, commute=30)
    near_items = [c for c in (result.get('near_8h_review_candidates') or [])
                  if c['datum'] == '2025-05-15']
    # Klar über 8h → kein Review
    assert near_items == [], f'Klar über 8h darf kein Review erzeugen, got {near_items}'
    d_detail = next(t for t in result['tage_detail'] if t['datum'] == '2025-05-15')
    assert d_detail['klass'] == 'Z72'


def test_clear_under_8h_no_review():
    """Tag mit duty deutlich unter 8h (z.B. 4h + commute) → kein Review."""
    days = [
        # 3h duty + 30min commute = 4h total = weit unter 8h
        (_make_day('2025-05-16', start='09:00', end='12:00', duty_min=180), {}),
    ]
    result = _classify(days, commute=30)
    near_items = [c for c in (result.get('near_8h_review_candidates') or [])
                  if c['datum'] == '2025-05-16']
    # Weit unter 8h → kein Review (kein Money-Hebel)
    assert near_items == [], f'Weit unter 8h: kein Review, got {near_items}'


# ════════════════════════════════════════════════════════════════════
# Near-8h: contextual review with money mention
# ════════════════════════════════════════════════════════════════════

def test_near_8h_creates_review_candidate():
    """Tag mit 7h duty + 30min commute jeder Weg = 8h total (440 in middle?)
    fällt in 420..480 → Review-Item entsteht."""
    days = [
        # 7h duty + 30min commute hin/her = 480min — aber wir wollen IM Range
        # 6h45 duty + 30min commute = 7:45 (465min)
        (_make_day('2025-04-21', start='09:00', end='15:45', duty_min=405), {}),
    ]
    result = _classify(days, commute=30)
    near_items = [c for c in (result.get('near_8h_review_candidates') or [])
                  if c['datum'] == '2025-04-21']
    assert len(near_items) >= 1, f'Near-8h muss Review erzeugen, got {near_items}'
    cand = near_items[0]
    assert 'total_min_known' in cand
    assert 'minutes_to_8h' in cand
    assert cand['money_impact_estimate'] == 14.0


def test_near_8h_question_mentions_actual_minutes():
    """Review-Frage enthält die konkrete Minutenzahl, nicht generisch."""
    cls = {'near_8h_review_candidates': [{
        'datum': '2025-04-21',
        'total_min_known': 475,  # 7h55min
        'minutes_to_8h': 5,
        'commute_minutes_input': 30,
        'activity_type': 'office',
        'marker': 'OFFICE',
        'money_impact_estimate': 14.0,
        'time_source': 'cas_times',
    }]}
    items = app._build_review_items(cls)
    near = [it for it in items if it['type'] == 'near_8h_review']
    assert len(near) == 1
    q = near[0]['question']
    # Frage muss konkret sein: enthält "7:55" und "8 Stunden"
    assert '7:55' in q, f'Frage muss konkrete Std/Min enthalten, got: {q}'
    assert '8 Stunden' in q
    assert '14' in q or 'Verpflegungspauschale' in q, \
        f'Frage muss Money-Hinweis (14€/Verpflegungspauschale) erwähnen, got: {q}'


def test_near_8h_question_mentions_money_effect():
    """Review-Frage erwähnt Verpflegungspauschale / 14 € (Money-Hebel klar)."""
    cls = {'near_8h_review_candidates': [{
        'datum': '2025-04-21',
        'total_min_known': 465,
        'minutes_to_8h': 15,
        'commute_minutes_input': 0,  # keine Fahrtzeit angegeben
        'activity_type': 'office',
        'marker': '',
        'money_impact_estimate': 14.0,
        'time_source': 'cas_times',
    }]}
    items = app._build_review_items(cls)
    near = [it for it in items if it['type'] == 'near_8h_review']
    assert len(near) == 1
    q = near[0]['question']
    assert 'Verpflegungspauschale' in q
    assert '14' in q


def test_near_8h_options_include_yes_no_time_unsure():
    """Antwort-Optionen: Ja/Nein/Uhrzeiten/Unsicher."""
    cls = {'near_8h_review_candidates': [{
        'datum': '2025-04-21',
        'total_min_known': 465, 'minutes_to_8h': 15,
        'commute_minutes_input': 30,
        'activity_type': 'office', 'marker': '',
        'money_impact_estimate': 14.0, 'time_source': 'cas',
    }]}
    items = app._build_review_items(cls)
    near = [it for it in items if it['type'] == 'near_8h_review']
    opts = [o['value'] for o in near[0]['options']]
    assert 'yes' in opts and 'no' in opts and 'time' in opts and 'unsure' in opts


def test_near_8h_review_item_has_source_type_cas():
    """source_type = CAS (computed) — markiert dass die 8h-Schätzung aus
    AeroTAX-Computation stammt, nicht User-Angabe."""
    cls = {'near_8h_review_candidates': [{
        'datum': '2025-04-21',
        'total_min_known': 465, 'minutes_to_8h': 15,
        'commute_minutes_input': 30,
        'activity_type': 'office', 'marker': '',
        'money_impact_estimate': 14.0, 'time_source': 'cas',
    }]}
    items = app._build_review_items(cls)
    near = [it for it in items if it['type'] == 'near_8h_review']
    assert near[0]['source_type'] == 'CAS'
    assert 'audit_source' in near[0]


# ════════════════════════════════════════════════════════════════════
# Review-items prioritization by money_impact
# ════════════════════════════════════════════════════════════════════

def test_review_items_prioritized_by_money_effect():
    """High-money items kommen zuerst, low-money später."""
    cls = {
        'near_8h_review_candidates': [{
            'datum': '2025-04-21', 'total_min_known': 465, 'minutes_to_8h': 15,
            'commute_minutes_input': 30,
            'activity_type': 'office', 'marker': '',
            'money_impact_estimate': 14.0, 'time_source': 'cas',
        }],
        'office_training_time_missing_candidates': [
            {'datum': '2025-04-22', 'marker': 'Schulung',
             'activity_type': 'office', 'money_impact_estimate': 28.0},
        ],
    }
    items = app._build_review_items(cls)
    # Items mit money_impact 28 (office) sollten vor money_impact 14 (near_8h) kommen
    impacts = [float(it.get('money_impact_estimate', 0)) for it in items
               if it['status'] == 'pending']
    assert impacts == sorted(impacts, reverse=True), \
        f'Items müssen nach Money-Impact desc sortiert sein: {impacts}'


# ════════════════════════════════════════════════════════════════════
# Negativ: keine generischen "Was war an dem Tag?" Fragen
# ════════════════════════════════════════════════════════════════════

def test_no_generic_what_was_this_day_question():
    """Statik-Audit: app.py enthält keine generische 'Was war an dem Tag'-Frage."""
    src = open(_cft.backend_path('app.py'),
               encoding='utf-8').read()
    forbidden = [
        'Was war an diesem Tag',
        'Was war an dem Tag',
        'Bitte erkläre den Tag',
        'Bitte beschreibe den Tag',
    ]
    for f in forbidden:
        assert f not in src, f'Forbidden generische Frage: „{f}"'


def test_no_cas_upload_prompt_when_cas_present():
    """Statik-Audit: Chat darf nicht pauschal „lade CAS hoch" sagen wenn CAS
    vorhanden ist — Frontend hat den missing_months_cas-Filter."""
    html = open(_cft.site_index_html(),
                encoding='utf-8').read()
    # Muss Filter haben
    assert '_trulyMissingMonths' in html
    # Honest Fallback wenn CAS da
    assert 'Dienstplan/CAS bereits vorliegen' in html
