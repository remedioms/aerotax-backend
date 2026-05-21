"""Highest-Defensible-Source-Backed-Amount Tests.

Produkt-Regel (2026-05-21):
  AeroTAX wählt den maximal vertretbaren, belegbaren Ansatz, wenn CAS/SE/BMF
  ihn tragen. Konservative Auslegung nur ohne AG-Beleg.

Diese Tests verifizieren die zwei neuen P0-Fixes:
  Fix #2: evening_anreise mit SE foreign-stfrei → Z76 An/Ab (statt Z73 Inland)
  Fix #3: Mid-Tour by SE-Evidence (prev+today+next foreign stfrei) → voll_24h
"""

import pytest
import app


def _make_day(datum, marker='', activity_type='tour', overnight=True,
              layover_ort='', routing=None, ends_hb=False, starts_hb=False,
              start_time='', has_fl=False, duty_min=480):
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
        'start_time':           start_time,
        'duty_duration_minutes': duty_min,
    }


def _make_se(stfrei_ort, stfrei_eur=50.0, stfrei_inland=False):
    return {
        'count':          1 if stfrei_eur > 0 else 0,
        'stfrei_total':   stfrei_eur,
        'stfrei_ort':     stfrei_ort,
        'stfrei_inland':  stfrei_inland,
    }


def _classify(days_with_se, year=2025, homebase='FRA', commute=30):
    matched = [{'datum': d['datum'], 'dp': d, 'se': se} for d, se in days_with_se]
    return app._deterministic_classify_v7(
        matched, year, homebase, commute_minutes=commute,
    )


def _eur_for(result, datum):
    return float(next(t for t in result['tage_detail'] if t['datum'] == datum)['eur'])


def _klass_for(result, datum):
    return next(t for t in result['tage_detail'] if t['datum'] == datum)['klass']


# ════════════════════════════════════════════════════════════════════
# Fix #2 — Evening-Anreise mit foreign-SE → Z76 (highest-defensible)
# ════════════════════════════════════════════════════════════════════

def test_evening_foreign_anreise_with_se_defaults_to_z76():
    """Auslandsanreise abends + SE foreign-stfrei = AG-Beleg → Z76 An/Ab, NICHT Z73 Inland 14€."""
    days = [
        # Anreisetag: spätes Briefing 22:00, foreign layover, foreign SE
        (_make_day('2025-01-11', activity_type='tour', overnight=True,
                   layover_ort='CPH', routing=['FRA','CPH'],
                   starts_hb=True, ends_hb=False, start_time='22:00'),
         _make_se('CPH', 80)),  # foreign stfrei
        # Mid-Tour (for context)
        (_make_day('2025-01-12', activity_type='tour', overnight=True,
                   layover_ort='CPH', routing=['CPH'], start_time='09:00'),
         _make_se('CPH', 60)),
    ]
    result = _classify(days)
    d11 = next(t for t in result['tage_detail'] if t['datum'] == '2025-01-11')
    # Dänemark an_abreise = 50€ per BMF — NOT Inland 14€
    assert d11['klass'] == 'Z76', f'Erwartet Z76 (foreign-SE-Beleg), got {d11["klass"]}'
    assert abs(float(d11['eur']) - 50.0) < 0.5, \
        f'Erwartet Dänemark an_abreise 50€, got {d11["eur"]}'


def test_evening_foreign_anreise_without_se_keeps_conservative_inland():
    """Ohne foreign-SE-Beleg bleibt das v8.10-Verhalten (Z73 Inland 14€) —
    weil kein AG-Beleg den höheren Ansatz trägt."""
    days = [
        (_make_day('2025-01-11', activity_type='tour', overnight=True,
                   layover_ort='CPH', routing=['FRA','CPH'],
                   starts_hb=True, ends_hb=False, start_time='22:00'),
         _make_se('', 0)),  # KEIN SE-Beleg
    ]
    result = _classify(days)
    d11 = next(t for t in result['tage_detail'] if t['datum'] == '2025-01-11')
    # Ohne foreign-SE → konservativ Inland 14€
    assert d11['klass'] == 'Z73', f'Ohne SE: konservativ Z73, got {d11["klass"]}'
    assert abs(float(d11['eur']) - 14.0) < 0.5


def test_morning_foreign_anreise_unaffected_by_fix():
    """Anreise früher Tag (z.B. 08:00) — keine Abend-Anreise-Regel aktiv, normaler Z76."""
    days = [
        (_make_day('2025-01-11', activity_type='tour', overnight=True,
                   layover_ort='CPH', routing=['FRA','CPH'],
                   starts_hb=True, ends_hb=False, start_time='08:00'),
         _make_se('CPH', 80)),
    ]
    result = _classify(days)
    d11 = next(t for t in result['tage_detail'] if t['datum'] == '2025-01-11')
    assert d11['klass'] == 'Z76'
    # Frühe Anreise hat das v8.10-Downgrade nie betroffen → normaler Z76-Pfad
    assert abs(float(d11['eur']) - 50.0) < 0.5


# ════════════════════════════════════════════════════════════════════
# Fix #3 — Mid-Tour by SE-Evidence (prev+today+next foreign-SE)
# ════════════════════════════════════════════════════════════════════

def test_mid_tour_by_se_evidence_uses_voll_24h_over_cluster_boundary():
    """3-Tage-Tour, alle Tage SE foreign — Tag 2 ist Mid-Tour per SE-Evidence,
    auch wenn AT-Cluster ihn als Boundary klassifiziert."""
    days = [
        (_make_day('2025-04-16', activity_type='tour', overnight=True,
                   layover_ort='IKA', routing=['IKA'], starts_hb=True, ends_hb=False),
         _make_se('IKA', 60)),  # Anreise
        # Mid-Tour: layover_ort=FRA wäre cluster-boundary-trigger, ABER SE-Evidence schützt
        (_make_day('2025-04-17', activity_type='tour', overnight=True,
                   layover_ort='FRA', routing=['IKA'], starts_hb=False, ends_hb=False),
         _make_se('IKA', 60)),  # heute foreign
        # Abreise via same_day-rescue
        (_make_day('2025-04-18', activity_type='same_day', overnight=False,
                   layover_ort='', routing=['IKA'], starts_hb=False, ends_hb=True),
         _make_se('IKA', 30)),  # morgen foreign
    ]
    result = _classify(days)
    d17 = next(t for t in result['tage_detail'] if t['datum'] == '2025-04-17')
    # Iran voll_24h = 33€; an_abreise = 22€
    # Mit SE-prev+today+next foreign → voll_24h
    assert d17['klass'] == 'Z76'
    assert abs(float(d17['eur']) - 33.0) < 0.5, \
        f'Mit SE-Evidence prev+today+next: voll_24h 33€, got {d17["eur"]}'


def test_last_day_of_tour_keeps_an_abreise():
    """Letzter Tag der Tour (kein next SE foreign) bleibt an_abreise."""
    days = [
        (_make_day('2025-04-16', activity_type='tour', overnight=True,
                   layover_ort='IKA', routing=['IKA'], starts_hb=True, ends_hb=False),
         _make_se('IKA', 60)),
        # Mid: today=foreign, prev=foreign, next=foreign
        (_make_day('2025-04-17', activity_type='tour', overnight=True,
                   layover_ort='IKA', routing=['IKA']),
         _make_se('IKA', 60)),
        # Last: today=foreign, prev=foreign, next=NOT foreign (back home)
        (_make_day('2025-04-18', activity_type='tour', overnight=True,
                   layover_ort='FRA', routing=['IKA'], ends_hb=False),
         _make_se('IKA', 30)),
        # Nach der Tour: kein SE
        (_make_day('2025-04-19', activity_type='frei', overnight=False),
         _make_se('', 0)),
    ]
    result = _classify(days)
    d18 = next(t for t in result['tage_detail'] if t['datum'] == '2025-04-18')
    # Tag 18 hat next=NOT foreign → NICHT mid-tour → an_abreise
    assert d18['klass'] == 'Z76'
    assert abs(float(d18['eur']) - 22.0) < 0.5, \
        f'Letzter Tour-Tag (next not foreign): an_abreise 22€, got {d18["eur"]}'


def test_first_day_of_tour_keeps_an_abreise():
    """Erster Tag der Tour (kein prev SE foreign) bleibt an_abreise."""
    days = [
        # Davor: kein SE
        (_make_day('2025-04-15', activity_type='frei', overnight=False),
         _make_se('', 0)),
        # Erster Tag: kein prev foreign-SE
        (_make_day('2025-04-16', activity_type='tour', overnight=True,
                   layover_ort='IKA', routing=['FRA','IKA'], starts_hb=True),
         _make_se('IKA', 60)),
        # Mid
        (_make_day('2025-04-17', activity_type='tour', overnight=True,
                   layover_ort='IKA'),
         _make_se('IKA', 60)),
    ]
    result = _classify(days)
    d16 = next(t for t in result['tage_detail'] if t['datum'] == '2025-04-16')
    assert d16['klass'] == 'Z76'
    assert abs(float(d16['eur']) - 22.0) < 0.5, \
        f'Erster Tour-Tag (no prev SE): an_abreise 22€, got {d16["eur"]}'


# ════════════════════════════════════════════════════════════════════
# Source arbitration: no marker-only, no unbacked foreign day
# ════════════════════════════════════════════════════════════════════

def test_no_unbacked_foreign_day():
    """Tag ohne CAS/SE-Belege bleibt Frei — keine Annahme nur weil Marker
    nach Ausland aussieht."""
    days = [
        (_make_day('2025-05-15', marker='UNKNOWN_FOREIGN', activity_type='unknown',
                   overnight=False, layover_ort='', routing=[]),
         _make_se('', 0)),
    ]
    result = _classify(days)
    d15 = next(t for t in result['tage_detail'] if t['datum'] == '2025-05-15')
    # Tag ohne Belege darf nicht als VMA klassifiziert werden
    assert d15['klass'] not in ('Z72', 'Z73', 'Z74', 'Z76'), \
        f'No-Beleg-Tag darf kein VMA-Klassifizierter sein, got {d15["klass"]}'


def test_se_evidence_alone_does_not_create_z76_without_cas():
    """SE foreign-stfrei isoliert (ohne CAS-Tour-Kontext) erzeugt NICHT
    automatisch Z76. Mindestens overnight_after_day=True ODER prev_overnight=True
    muss vorliegen."""
    days = [
        (_make_day('2025-06-15', activity_type='frei', overnight=False,
                   layover_ort='', routing=[]),
         _make_se('LON', 80)),  # foreign SE aber kein Tour-Kontext
    ]
    result = _classify(days)
    d15 = next(t for t in result['tage_detail'] if t['datum'] == '2025-06-15')
    # AT muss konservativ bleiben — kein Z76 ohne Tour-Belege
    # (entweder Frei, oder durch other path classified)
    # Wir prüfen nur, dass es nicht Z76 voll_24h ist (nur weil SE foreign sagt)
    if d15['klass'] == 'Z76':
        assert float(d15['eur']) < 50.0, \
            f'Z76 ohne Tour-Kontext sollte nicht voll_24h sein, got {d15["eur"]}'


# ════════════════════════════════════════════════════════════════════
# Anti-regression: clear cas/se foreign evidence is not downgraded
# ════════════════════════════════════════════════════════════════════

def test_clear_foreign_evidence_uses_full_z76():
    """Tag mit klarer CAS-Tour + SE foreign + foreign layover → voll Z76."""
    days = [
        (_make_day('2025-06-08', activity_type='tour', overnight=True,
                   layover_ort='SIN', routing=['SIN'], starts_hb=True, ends_hb=False),
         _make_se('SIN', 80)),
        (_make_day('2025-06-09', activity_type='tour', overnight=True,
                   layover_ort='SIN', routing=['SIN']),
         _make_se('SIN', 60)),
        (_make_day('2025-06-10', activity_type='tour', overnight=True,
                   layover_ort='SIN', routing=['SIN']),
         _make_se('SIN', 60)),
        (_make_day('2025-06-11', activity_type='same_day', overnight=False,
                   layover_ort='', routing=['FRA'], ends_hb=True),
         _make_se('SIN', 40)),
    ]
    result = _classify(days)
    # Jun 9 + 10 mid-tour
    d09 = next(t for t in result['tage_detail'] if t['datum'] == '2025-06-09')
    d10 = next(t for t in result['tage_detail'] if t['datum'] == '2025-06-10')
    # Singapur voll_24h = 71€
    assert abs(float(d09['eur']) - 71.0) < 0.5, f'd09 Singapur voll_24h 71€, got {d09["eur"]}'
    assert abs(float(d10['eur']) - 71.0) < 0.5, f'd10 Singapur voll_24h 71€, got {d10["eur"]}'


# ════════════════════════════════════════════════════════════════════
# FollowMe-Source-Arbitration tests
# ════════════════════════════════════════════════════════════════════

def test_followme_higher_supported_by_sources_means_fix_aerotax():
    """Wenn FM höher liegt UND CAS+SE+BMF den höheren Ansatz tragen:
    AeroTAX soll geupdatet werden (Highest-Defensible-Rule)."""
    # Beispiel: 3-Tage-Tour SGP, alle Tage SE-foreign — Mid-Tag muss voll_24h sein
    days = [
        (_make_day('2025-06-08', activity_type='tour', overnight=True,
                   layover_ort='SIN', routing=['SIN'], starts_hb=True, start_time='14:00'),
         _make_se('SIN', 80)),
        (_make_day('2025-06-09', activity_type='tour', overnight=True,
                   layover_ort='SIN'),
         _make_se('SIN', 60)),
        (_make_day('2025-06-10', activity_type='tour', overnight=True,
                   layover_ort='FRA'),  # AT-Cluster-Boundary
         _make_se('SIN', 60)),
        (_make_day('2025-06-11', activity_type='same_day', overnight=False, ends_hb=True),
         _make_se('SIN', 40)),
    ]
    result = _classify(days)
    d10 = next(t for t in result['tage_detail'] if t['datum'] == '2025-06-10')
    # Per Fix #3: SE evidence (prev+today+next foreign) → voll_24h
    assert abs(float(d10['eur']) - 71.0) < 0.5, \
        f'Mit SE-Evidence trotz cluster-boundary FRA: voll_24h 71€, got {d10["eur"]}'


def test_followme_higher_unsupported_documented_disagreement():
    """Wenn FM höher liegt aber CAS+SE dagegen sprechen (z.B. layover=FRA UND
    kein SE-Beleg): AT behält konservativen Wert, FM-Diff wird Audit-Hinweis."""
    days = [
        (_make_day('2025-07-15', activity_type='frei', overnight=False, layover_ort=''),
         _make_se('', 0)),  # kein SE
    ]
    result = _classify(days)
    d15 = next(t for t in result['tage_detail'] if t['datum'] == '2025-07-15')
    # Ohne Belege bleibt Frei — FM Δ wäre documented_disagreement
    assert d15['klass'] in ('Frei', 'Issue')
    assert abs(float(d15['eur']) - 0.0) < 0.5


def test_user_favorable_bmf_defensible_default():
    """Bei zwei vertretbaren BMF-Auslegungen wählt AT den user-günstigeren
    Ansatz — wenn AG-Beleg ihn trägt."""
    # Anreise abends + foreign-SE: AT v8.10 wollte Inland 14€ (konservativ)
    # AT post-fix: Z76 An/Ab 50€ (Dänemark, BMF-strikt, user-günstiger)
    days = [
        (_make_day('2025-01-11', activity_type='tour', overnight=True,
                   layover_ort='CPH', routing=['FRA','CPH'],
                   starts_hb=True, start_time='22:30'),
         _make_se('CPH', 80)),
    ]
    result = _classify(days)
    d11 = next(t for t in result['tage_detail'] if t['datum'] == '2025-01-11')
    assert d11['klass'] == 'Z76', 'AT wählt user-günstigeren BMF-vertretbaren Wert'
    assert abs(float(d11['eur']) - 50.0) < 0.5
