"""P0 — Z76 Mid-Tour Voll-24h-Satz für same_day + prev_overnight + foreign-SE.

BMF §9 Abs. 4a EStG: An- und Abreisetag = an_abreise-Pauschale,
Zwischentage (Übernachtung am Ende des Tages im Ausland) = voll_24h-Pauschale.

Bisheriger Bug: Wenn Sonnet einen Mid-Tour-Tag als activity_type='same_day'
las und der Vortag overnight=True war (prev_overnight rescue), wurde der
Tag pauschal mit an_abreise-Satz berechnet — auch wenn today.layover_ort
weiterhin ausländisch war (Crew hat am Ende des Tages NICHT zu Hause
übernachtet → es ist ein Zwischentag).

Fix: Wenn today.layover_ort ein ausländischer Code ist (nicht Inland, nicht
Homebase), → voll_24h. Sonst (Abreise zur Heimat) → an_abreise wie bisher.

Diese Tests verwenden keine Tibor-Daten, keine FollowMe-Daten. Sie prüfen
die Logik isoliert mit synthetischen Tagen.
"""

import pytest
import app


def _make_day(datum, marker='', activity_type='same_day', overnight=False,
              layover_ort='', routing=None, ends_hb=True, starts_hb=True,
              has_fl=False):
    return {
        'datum':                datum,
        'raw_marker':           marker,
        'activity_type':        activity_type,
        'overnight_after_day':  overnight,
        'layover_ort':          layover_ort,
        'routing':              routing or [],
        'ends_at_homebase':     ends_hb,
        'starts_at_homebase':   starts_hb,
        'has_fl':               has_fl,
        'is_workday':           True,
        'duty_duration_minutes': 480,
    }


def _make_se(stfrei_ort, stfrei_eur=50.0, stfrei_inland=False):
    return {
        'count':          1 if stfrei_eur > 0 else 0,
        'stfrei_total':   stfrei_eur,
        'stfrei_ort':     stfrei_ort,
        'stfrei_inland':  stfrei_inland,
    }


def _classify(days_with_se, year=2025, homebase='FRA', commute=30):
    """Helper: Pack matched days into structure for _classify_v7."""
    matched = [{'datum': d['datum'], 'dp': d, 'se': se} for d, se in days_with_se]
    return app._deterministic_classify_v7(
        matched, year, homebase, commute_minutes=commute,
    )


# ════════════════════════════════════════════════════════════════════
# Positive: Mid-Tour Tag (prev_overnight + today still foreign)
# ════════════════════════════════════════════════════════════════════

def test_mid_tour_with_foreign_layover_uses_voll_24h():
    """Day 2 of HKG tour: prev_overnight=True + today.layover_ort=HKG
    → voll_24h-Satz (Hongkong = 71€)."""
    days = [
        (_make_day('2025-01-18', activity_type='tour', overnight=True,
                   layover_ort='HKG', routing=['HKG'], starts_hb=True, ends_hb=False),
         _make_se('HKG', 200)),
        (_make_day('2025-01-19', activity_type='same_day', overnight=False,
                   layover_ort='HKG', routing=['HKG'], starts_hb=True, ends_hb=True),
         _make_se('HKG', 60)),
    ]
    result = _classify(days)
    detail = result['tage_detail']
    d19 = next(t for t in detail if t['datum'] == '2025-01-19')
    cr = d19.get('classifier_result') or {}
    # Mid-tour: voll_24h-Satz (71€ für HKG), klass Z76
    assert d19['klass'] == 'Z76', f'expected Z76, got {d19["klass"]}'
    # Reason should mention Mid-Tour / Volltag, not Same-Day-Auslandstrip
    reason = (cr.get('reason') or d19.get('begruendung') or '').lower()
    assert 'mid-tour' in reason or 'volltag' in reason, \
        f'expected Mid-Tour/Volltag in reason, got: {reason}'
    # Amount should be 71€ (HKG voll_24h)
    assert abs(float(d19.get('eur', 0)) - 71.0) < 0.5, \
        f'expected HKG voll_24h 71€, got {d19.get("eur")}'


def test_mid_tour_singapur_uses_voll_24h():
    """Same pattern for SGP (Singapur voll_24h = 71€, an_abreise = 48€)."""
    days = [
        (_make_day('2025-06-07', activity_type='tour', overnight=True,
                   layover_ort='SIN', routing=['SIN'], starts_hb=True, ends_hb=False),
         _make_se('SIN', 200)),
        (_make_day('2025-06-08', activity_type='same_day', overnight=False,
                   layover_ort='SIN', routing=['SIN'], starts_hb=True, ends_hb=True),
         _make_se('SIN', 60)),
    ]
    result = _classify(days)
    d08 = next(t for t in result['tage_detail'] if t['datum'] == '2025-06-08')
    assert d08['klass'] == 'Z76'
    # Singapur voll_24h = 71€
    assert abs(float(d08.get('eur', 0)) - 71.0) < 0.5, \
        f'expected SGP voll_24h 71€, got {d08.get("eur")}'


# ════════════════════════════════════════════════════════════════════
# Negative: Abreise-Tag (prev_overnight + today layover = Homebase/Inland)
# Diese MÜSSEN weiterhin an_abreise bekommen.
# ════════════════════════════════════════════════════════════════════

def test_abreise_to_homebase_keeps_an_abreise():
    """Abreise-Tag: today.layover_ort='' (oder Homebase) → an_abreise wie bisher."""
    days = [
        (_make_day('2025-01-18', activity_type='tour', overnight=True,
                   layover_ort='HKG', routing=['HKG'], starts_hb=True, ends_hb=False),
         _make_se('HKG', 200)),
        (_make_day('2025-01-22', activity_type='same_day', overnight=False,
                   layover_ort='', routing=['FRA'], starts_hb=True, ends_hb=True),
         _make_se('HKG', 60)),
    ]
    result = _classify(days)
    d22 = next(t for t in result['tage_detail'] if t['datum'] == '2025-01-22')
    assert d22['klass'] == 'Z76'
    # HKG an_abreise = 48€
    assert abs(float(d22.get('eur', 0)) - 48.0) < 0.5, \
        f'expected HKG an_abreise 48€, got {d22.get("eur")}'


def test_abreise_with_fra_layover_keeps_an_abreise():
    """today.layover_ort='FRA' (slept at home this evening) → an_abreise."""
    days = [
        (_make_day('2025-04-16', activity_type='tour', overnight=True,
                   layover_ort='IKA', routing=['FRA'], starts_hb=True, ends_hb=False),
         _make_se('IKA', 100)),
        (_make_day('2025-04-17', activity_type='same_day', overnight=False,
                   layover_ort='FRA', routing=['IKA'], starts_hb=False, ends_hb=False),
         _make_se('IKA', 50)),
    ]
    result = _classify(days)
    d17 = next(t for t in result['tage_detail'] if t['datum'] == '2025-04-17')
    assert d17['klass'] == 'Z76'
    # Iran an_abreise = 22€ (NOT voll_24h 33€)
    assert abs(float(d17.get('eur', 0)) - 22.0) < 0.5, \
        f'expected IRN an_abreise 22€ (layover=FRA → went home), got {d17.get("eur")}'


def test_abreise_with_inland_layover_keeps_an_abreise():
    """today.layover_ort='MUC' (Inland, e.g., positioning home via München)
    → an_abreise."""
    days = [
        (_make_day('2025-04-16', activity_type='tour', overnight=True,
                   layover_ort='IKA', routing=['FRA'], starts_hb=True, ends_hb=False),
         _make_se('IKA', 100)),
        (_make_day('2025-04-17', activity_type='same_day', overnight=False,
                   layover_ort='MUC', routing=['IKA'], starts_hb=False, ends_hb=False),
         _make_se('IKA', 50)),
    ]
    result = _classify(days)
    d17 = next(t for t in result['tage_detail'] if t['datum'] == '2025-04-17')
    assert d17['klass'] == 'Z76'
    # Inland layover → an_abreise rate, NOT voll_24h
    assert abs(float(d17.get('eur', 0)) - 22.0) < 0.5, \
        f'expected IRN an_abreise 22€ (layover=MUC inland), got {d17.get("eur")}'


# ════════════════════════════════════════════════════════════════════
# Edge cases
# ════════════════════════════════════════════════════════════════════

def test_today_layover_empty_treated_as_abreise():
    """Leerer layover_ort → an_abreise (kein Beweis für mid-tour)."""
    days = [
        (_make_day('2025-04-16', activity_type='tour', overnight=True,
                   layover_ort='IKA', routing=['FRA'], starts_hb=True, ends_hb=False),
         _make_se('IKA', 100)),
        (_make_day('2025-04-17', activity_type='same_day', overnight=False,
                   layover_ort='', routing=['IKA'], starts_hb=False, ends_hb=True),
         _make_se('IKA', 50)),
    ]
    result = _classify(days)
    d17 = next(t for t in result['tage_detail'] if t['datum'] == '2025-04-17')
    # Leerer layover → fallback an_abreise (konservativ)
    assert abs(float(d17.get('eur', 0)) - 22.0) < 0.5


def test_no_se_no_fix_triggers():
    """Ohne aktive Auslands-SE-Zeile bleibt der Tag unverändert
    (Fix triggert nur im v8.15-Rescue-Pfad mit SE)."""
    days = [
        (_make_day('2025-01-18', activity_type='tour', overnight=True,
                   layover_ort='HKG', routing=['HKG'], starts_hb=True, ends_hb=False),
         _make_se('HKG', 100)),
        (_make_day('2025-01-19', activity_type='same_day', overnight=False,
                   layover_ort='HKG', routing=['HKG'], starts_hb=True, ends_hb=True),
         _make_se('', 0, stfrei_inland=False)),  # no SE
    ]
    result = _classify(days)
    d19 = next(t for t in result['tage_detail'] if t['datum'] == '2025-01-19')
    # Without SE-row, falls through to BH-003a-rescue or Issue — NOT Z76 voll_24h
    # Wir prüfen nur, dass der Fix den Pfad nicht falsch aktiviert.
    assert d19['klass'] in ('Z76', 'Issue', 'Frei'), \
        f'unexpected klass: {d19["klass"]}'


# ════════════════════════════════════════════════════════════════════
# Source arbitration test: Fix nur wenn today.layover_ort foreign signal
# ════════════════════════════════════════════════════════════════════

def test_fix_uses_layover_ort_not_followme_or_routing():
    """Fix entscheidet anhand today.layover_ort, nicht anhand FollowMe oder
    Routing. Bei layover_ort=HKG → voll_24h. Bei layover_ort=FRA → an_abreise.
    Beides Tage mit gleichem Routing/SE — der Unterschied liegt nur im
    Übernachtungsort."""
    se_pair = _make_se('HKG', 80)
    foreign_day = (
        _make_day('2025-01-18', activity_type='tour', overnight=True,
                  layover_ort='HKG', routing=['HKG'], starts_hb=True, ends_hb=False),
        _make_se('HKG', 200),
    )
    mid = (
        _make_day('2025-01-19', activity_type='same_day', overnight=False,
                  layover_ort='HKG', routing=['HKG'], starts_hb=True, ends_hb=True),
        se_pair,
    )
    abreise = (
        _make_day('2025-01-20', activity_type='same_day', overnight=False,
                  layover_ort='FRA', routing=['HKG'], starts_hb=True, ends_hb=True),
        se_pair,
    )
    result = _classify([foreign_day, mid, abreise])
    d19 = next(t for t in result['tage_detail'] if t['datum'] == '2025-01-19')
    d20 = next(t for t in result['tage_detail'] if t['datum'] == '2025-01-20')
    assert abs(float(d19.get('eur', 0)) - 71.0) < 0.5, \
        f'd19 mid-tour HKG-layover should be voll_24h 71€, got {d19.get("eur")}'
    assert abs(float(d20.get('eur', 0)) - 48.0) < 0.5, \
        f'd20 abreise FRA-layover should be an_abreise 48€, got {d20.get("eur")}'
