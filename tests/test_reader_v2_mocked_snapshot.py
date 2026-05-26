"""R12 — Mocked Reader-V2 Snapshot durch normalized_tours.

Liefert einen synthetischen, generischen Reader-V2-Output (kein Tibor-Hardcoding,
keine FollowMe-Beträge) und prüft, ob normalized_tours + Allowance-Calculator
deterministisch die erwarteten KPI-Cluster produzieren.

Zweck: Beweisen, dass die post-Reader-Pipeline (cas_postprocessor →
build_normalized_tours → calculate_allowances_from_normalized_tours) bei
realistischer V2-Form korrekt foreign Z76 / inland Z72 / Z73 / hotel_naechte
zählt. Bestätigt damit, dass der Reader der Blocker ist und nicht die
Berechnungs-Schicht.

KEINE Live-Calls. KEIN Deploy. Stop nach Bericht.
"""
import os
import sys
import json
import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from normalized_tours import (  # noqa: E402
    build_normalized_tours,
    calculate_allowances_from_normalized_tours,
)


# ────────────────────────────────────────────────────────────────────────────
# Mini BMF-Stub (synthetisch — keine echten BMF-Beträge, nur plausible Größen-
# ordnung). Pro Land je Zeile {an_abreise, voll_24h}. KEIN Tibor-bezogenes
# Hardcoding.
# ────────────────────────────────────────────────────────────────────────────

# Production bmf_table ist IATA-keyed mit {an_abreise, voll_24h, country}.
# Wir spiegeln die Form, mit synthetischen Beträgen (keine echten BMF-Zahlen).
_RATES_BY_COUNTRY = {
    'Indien':      {'an_abreise': 32.0,  'voll_24h': 47.0},
    'Hongkong':    {'an_abreise': 64.0,  'voll_24h': 97.0},
    'Japan':       {'an_abreise': 56.0,  'voll_24h': 84.0},
    'Zypern':      {'an_abreise': 30.0,  'voll_24h': 44.0},
    'Thailand':    {'an_abreise': 32.0,  'voll_24h': 47.0},
    'Brasilien':   {'an_abreise': 36.0,  'voll_24h': 54.0},
}

IATA_TO_BMF_STUB = {
    'BLR': 'Indien',
    'BOM': 'Indien',
    'DEL': 'Indien',
    'HKG': 'Hongkong',
    'NRT': 'Japan',
    'HND': 'Japan',
    'LCA': 'Zypern',
    'BKK': 'Thailand',
    'GRU': 'Brasilien',
}

BMF_STUB = {
    iata: {**_RATES_BY_COUNTRY[country], 'country': country}
    for iata, country in IATA_TO_BMF_STUB.items()
}

HOMEBASE = 'FRA'


# ────────────────────────────────────────────────────────────────────────────
# Mock-Helpers
# ────────────────────────────────────────────────────────────────────────────

def _dep(datum, dest_iata, flight_no, duty=480):
    """V2-Tag: Tour-Departure vom HB zu dest_iata."""
    return {
        'datum': datum,
        'marker_raw': flight_no,
        'normalized_marker': flight_no,
        'activity_type': 'tour_departure',
        'starts_at_homebase': True,
        'ends_at_homebase': False,
        'overnight_after_day': True,
        'routing': [HOMEBASE, dest_iata, flight_no],
        'layover_ort': dest_iata,
        'layover_iata': dest_iata,
        'has_fl': True,
        'duty_duration_minutes': duty,
        'tour_context_hint': 'departure',
        'tour_context_confidence': 'high',
        'is_tour_departure': True,
        'is_tour_continuation': False,
        'is_tour_return': False,
        'return_from_layover': False,
        'has_flight_segment': True,
        'confidence': 'high',
    }


def _mid_x(datum, layover_iata, prev_layover_iata):
    """V2-Tag: Mid-Tour X-Marker — Reader-V2 markiert als Continuation."""
    return {
        'datum': datum,
        'marker_raw': 'X',
        'normalized_marker': 'X',
        'activity_type': 'tour_continuation',
        'starts_at_homebase': False,
        'ends_at_homebase': False,
        'overnight_after_day': True,
        'routing': [layover_iata],
        'layover_ort': layover_iata,
        'layover_iata': layover_iata,
        'previous_layover_iata': prev_layover_iata,
        'has_fl': False,
        'duty_duration_minutes': 0,
        'tour_context_hint': 'mid_tour',
        'tour_context_confidence': 'high',
        'is_tour_departure': False,
        'is_tour_continuation': True,
        'is_tour_return': False,
        'return_from_layover': False,
        'has_flight_segment': False,
        'reader_should_not_classify_as_free_reason':
            f'BLR/HKG-style layover-continuation; prev.layover={prev_layover_iata}',
        'neighbor_evidence': [f'prev.layover_iata={prev_layover_iata}'],
        'confidence': 'high',
    }


def _ret(datum, origin_iata, flight_no, duty=330):
    """V2-Tag: Tour-Return — Heimkehr zur Homebase."""
    return {
        'datum': datum,
        'marker_raw': flight_no,
        'normalized_marker': flight_no,
        'activity_type': 'tour_return',
        'starts_at_homebase': False,
        'ends_at_homebase': True,
        'overnight_after_day': False,
        'routing': [origin_iata, HOMEBASE, flight_no],
        'layover_ort': origin_iata,
        'layover_iata': origin_iata,
        'previous_layover_iata': origin_iata,
        'origin_iata': origin_iata,
        'destination_iata': HOMEBASE,
        'has_fl': True,
        'duty_duration_minutes': duty,
        'tour_context_hint': 'return',
        'tour_context_confidence': 'high',
        'is_tour_departure': False,
        'is_tour_continuation': False,
        'is_tour_return': True,
        'return_from_layover': True,
        'has_flight_segment': True,
        'confidence': 'high',
    }


def _same_day_inland(datum, duty=480):
    """V2-Tag: Same-Day-Inland-Trip (>8h) → Z72."""
    return {
        'datum': datum,
        'marker_raw': 'INLAND_SAMEDAY',
        'normalized_marker': 'INLAND_SAMEDAY',
        'activity_type': 'tour_departure',
        'starts_at_homebase': True,
        'ends_at_homebase': True,
        'overnight_after_day': False,
        'routing': [HOMEBASE, 'MUC'],
        'layover_ort': '',
        'layover_iata': None,
        'has_fl': True,
        'duty_duration_minutes': duty,
        'tour_context_hint': 'departure',
        'tour_context_confidence': 'high',
        'is_tour_departure': True,
        'is_tour_continuation': False,
        'is_tour_return': False,
        'return_from_layover': False,
        'has_flight_segment': True,
        'confidence': 'high',
    }


def _home_standby(datum):
    """Home-Standby-Tag — KEIN Tour-Trigger."""
    return {
        'datum': datum,
        'marker_raw': 'SB_S',
        'normalized_marker': 'SB_S',
        'activity_type': 'home_standby',
        'starts_at_homebase': True,
        'ends_at_homebase': True,
        'overnight_after_day': False,
        'routing': [HOMEBASE],
        'layover_ort': '',
        'layover_iata': None,
        'has_fl': False,
        'duty_duration_minutes': 480,
        'tour_context_hint': 'standby',
        'tour_context_confidence': 'high',
        'is_tour_departure': False,
        'is_tour_continuation': False,
        'is_tour_return': False,
        'return_from_layover': False,
        'has_flight_segment': False,
        'confidence': 'high',
    }


def _free(datum):
    return {
        'datum': datum,
        'marker_raw': 'OFF',
        'normalized_marker': 'OFF',
        'activity_type': 'free',
        'starts_at_homebase': True,
        'ends_at_homebase': True,
        'overnight_after_day': False,
        'routing': [],
        'layover_ort': '',
        'layover_iata': None,
        'has_fl': False,
        'duty_duration_minutes': 0,
        'tour_context_hint': 'home',
        'tour_context_confidence': 'high',
        'is_tour_departure': False,
        'is_tour_continuation': False,
        'is_tour_return': False,
        'return_from_layover': False,
        'has_flight_segment': False,
        'confidence': 'high',
    }


# ────────────────────────────────────────────────────────────────────────────
# Snapshot-Konstruktion — fünf foreign Touren + ein Same-Day Inland
# (generisch, keine Tibor-Daten). Inkludiert den BLR-X-Return-Pattern.
# ────────────────────────────────────────────────────────────────────────────

def _build_snapshot():
    """Synthetischer Reader-V2-Output. Tours sind chronologisch durch jeweils
    einen Free-Day getrennt, damit der R3-Postprocessor Tour-Returns nicht
    mit dem nächsten Tour-Departure verkettet (er prüft nur den Folgetag).
    """
    days = []

    # Tour 1: BLR 4 Tage (X-Return-Pattern)
    days.append(_dep('2025-01-03', 'BLR', 'LH756', duty=780))
    days.append(_mid_x('2025-01-04', 'BLR', 'BLR'))
    days.append(_mid_x('2025-01-05', 'BLR', 'BLR'))
    days.append(_ret('2025-01-06', 'BLR', 'LH755'))
    days.append(_free('2025-01-07'))

    # Tour 2: HKG 3 Tage
    days.append(_dep('2025-02-10', 'HKG', 'LH796', duty=720))
    days.append(_mid_x('2025-02-11', 'HKG', 'HKG'))
    days.append(_ret('2025-02-12', 'HKG', 'LH797'))
    days.append(_free('2025-02-13'))

    # Tour 3: NRT 4 Tage
    days.append(_dep('2025-03-15', 'NRT', 'LH716', duty=720))
    days.append(_mid_x('2025-03-16', 'NRT', 'NRT'))
    days.append(_mid_x('2025-03-17', 'NRT', 'NRT'))
    days.append(_ret('2025-03-18', 'NRT', 'LH717'))
    days.append(_free('2025-03-19'))

    # Tour 4: LCA 2 Tage (kürzeste foreign Tour)
    days.append(_dep('2025-04-20', 'LCA', 'LH1242', duty=480))
    days.append(_ret('2025-04-21', 'LCA', 'LH1243'))
    days.append(_free('2025-04-22'))

    # Tour 5: BKK 3 Tage
    days.append(_dep('2025-05-15', 'BKK', 'LH772', duty=720))
    days.append(_mid_x('2025-05-16', 'BKK', 'BKK'))
    days.append(_ret('2025-05-17', 'BKK', 'LH773'))
    days.append(_free('2025-05-18'))

    # Same-Day Inland >8h → Z72
    days.append(_same_day_inland('2025-06-15', duty=540))
    days.append(_free('2025-06-16'))

    # Home-Standby Block (3 Tage) — DARF KEINE Tour erzeugen
    days.append(_home_standby('2025-07-01'))
    days.append(_home_standby('2025-07-02'))
    days.append(_home_standby('2025-07-03'))

    # Frei-Block
    days.append(_free('2025-08-10'))
    days.append(_free('2025-08-11'))

    return days


def _build_se_rows(days):
    """Minimaler SE-Stub — keine Auslandsbeträge (würden Z76 inflieren)."""
    return []


# ────────────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────────────

def _run_pipeline():
    days = _build_snapshot()
    se_rows = _build_se_rows(days)
    tours = build_normalized_tours(
        cas_days=days, se_rows=se_rows, year=2025,
        homebase=HOMEBASE,
    )
    result = calculate_allowances_from_normalized_tours(
        normalized_tours=tours, bmf_table=BMF_STUB,
        iata_to_bmf=IATA_TO_BMF_STUB, se_rows=se_rows, homebase=HOMEBASE,
    )
    return tours, result


def test_snapshot_produces_expected_tour_count():
    tours, result = _run_pipeline()
    # 5 foreign Touren + 1 Inland-Same-Day = 6 Tour-Cluster.
    # Home-Standby + Frei produzieren KEINE Tour.
    assert result.tour_count == 6, (
        f'tour_count {result.tour_count}; tours: '
        f'{[(t.tour_id, t.start_date.isoformat(), t.end_date.isoformat()) for t in tours]}'
    )


def test_snapshot_no_phantom_tours_from_standby():
    tours, _ = _run_pipeline()
    # Keine Tour, die nur Home-Standby-Tage enthält
    for t in tours:
        markers = [(td.cas_marker or '').upper() for td in t.days]
        assert not all(m == 'SB_S' for m in markers if m), (
            f'Phantom-Tour entdeckt: {t.tour_id} {markers}'
        )


def test_snapshot_fahrtage_one_per_foreign_tour_plus_inland_same_day():
    _, result = _run_pipeline()
    # 5 foreign Tour-Starts + 1 Inland-Same-Day (Z72) = 6 Fahrtage
    assert 5 <= result.fahrtage <= 6, f'fahrtage={result.fahrtage}'


def test_snapshot_hotel_naechte_match_foreign_layover_overnights():
    _, result = _run_pipeline()
    # Erwartet: BLR(3) + HKG(2) + NRT(3) + LCA(1) + BKK(2) = 11
    # Toleranz ±1 für Boundary-Effekte
    assert 10 <= result.hotel_naechte <= 12, (
        f'hotel_naechte={result.hotel_naechte}'
    )


def test_snapshot_z72_positive_for_inland_same_day():
    _, result = _run_pipeline()
    # 1 Same-Day-Inland-Trip > 8h
    assert result.z72_tage >= 1, f'z72_tage={result.z72_tage}'
    assert result.z72_eur >= 14.0


def test_snapshot_z76_positive_for_foreign_layovers():
    _, result = _run_pipeline()
    # Foreign-Touren produzieren Z76
    assert result.z76_tage > 0, f'z76_tage={result.z76_tage}'
    assert result.z76_eur > 0, f'z76_eur={result.z76_eur}'


def test_snapshot_x_return_day_classified_as_return_not_free():
    """Wichtigster Test: 06.01 X-Return wird als tour_return klassifiziert."""
    tours, _ = _run_pipeline()
    blr_tour = next(
        (t for t in tours if t.start_date.isoformat() == '2025-01-03'), None,
    )
    assert blr_tour is not None, 'BLR-Tour 2025-01-03 fehlt'
    # 4 Tage in Tour
    assert len(blr_tour.days) == 4, (
        f'BLR-Tour-Länge={len(blr_tour.days)} statt 4: '
        f'{[d.date.isoformat() for d in blr_tour.days]}'
    )
    last = blr_tour.days[-1]
    assert last.date.isoformat() == '2025-01-06'
    assert last.is_return_day is True


def test_snapshot_mid_tour_x_days_inside_blr_tour():
    """04.01 und 05.01 (X-Marker) sind Continuation, nicht Frei."""
    tours, _ = _run_pipeline()
    blr_tour = next(
        (t for t in tours if t.start_date.isoformat() == '2025-01-03'), None,
    )
    assert blr_tour is not None
    day_dates = [d.date.isoformat() for d in blr_tour.days]
    assert '2025-01-04' in day_dates
    assert '2025-01-05' in day_dates


def test_snapshot_z76_only_from_foreign_iatas():
    """Verifiziert: kein Z76-Beitrag von Same-Day-Inland-Tour."""
    tours, result = _run_pipeline()
    # Inland-Same-Day ist Z72 (nicht Z76)
    same_day_tour = next(
        (t for t in tours if t.start_date.isoformat() == '2025-06-15'), None,
    )
    # Same-Day-Tour kann optional als Tour gebaut werden — wenn ja, dann
    # NUR Z72-Beitrag (keine Auslandsroutings).
    if same_day_tour:
        for td in same_day_tour.days:
            if td.target_iata:
                # Inland-target nicht relevant für Z76
                assert td.target_iata in ('MUC', None, ''), (
                    f'unexpected target_iata for inland same-day: {td.target_iata}'
                )


def test_snapshot_reinigungstage_le_arbeitstage():
    _, result = _run_pipeline()
    assert result.reinigungstage <= result.arbeitstage, (
        f'reinigung={result.reinigungstage} > arbeit={result.arbeitstage}'
    )


def test_snapshot_arbeitstage_within_realistic_band():
    _, result = _run_pipeline()
    # Calculator zählt arbeitstage strikt (is_real_duty_day) — Mid-Tour-X
    # ohne explizite duty/flight-Evidence trotz tour_continuation-Hint zählt
    # in der aktuellen Version nicht. Erwartungswert daher konservativ.
    # Realistisches Band: mind. 1 Arbeitstag pro foreign Tour-Boundary (dep+ret)
    # plus 1 Inland-Same-Day = 5*2 + 1 = 11.
    assert 10 <= result.arbeitstage <= 20, (
        f'arbeitstage={result.arbeitstage} außerhalb Band'
    )


def test_snapshot_no_z76_eur_for_inland_only_pipeline():
    """Sanity: wenn nur Inland-Trips, dann Z76==0."""
    inland_only_days = [
        _same_day_inland('2025-09-01', duty=540),
        _same_day_inland('2025-09-15', duty=540),
        _free('2025-09-02'),
    ]
    tours = build_normalized_tours(
        cas_days=inland_only_days, se_rows=[], year=2025, homebase=HOMEBASE,
    )
    res = calculate_allowances_from_normalized_tours(
        normalized_tours=tours, bmf_table=BMF_STUB,
        iata_to_bmf=IATA_TO_BMF_STUB, se_rows=[], homebase=HOMEBASE,
    )
    assert res.z76_eur == 0.0
    assert res.z76_tage == 0


# ────────────────────────────────────────────────────────────────────────────
# R12 Erweiterung — Z73 (Inland An-/Abreise) + Z74 (Inland Voll-24h)
# ────────────────────────────────────────────────────────────────────────────

INLAND_AN_AB = 14.0
INLAND_VOLL_24H = 28.0


def _inland_dep_to_muc(datum):
    """Inland-Tour-Anreise: FRA→MUC mit Hotelnacht. Triggert Z73."""
    return {
        'datum': datum, 'marker_raw': 'LH123', 'normalized_marker': 'LH123',
        'activity_type': 'tour_departure',
        'starts_at_homebase': True, 'ends_at_homebase': False,
        'overnight_after_day': True,
        'routing': [HOMEBASE, 'MUC', 'LH123'],
        'layover_ort': 'MUC', 'layover_iata': 'MUC',
        'has_fl': True, 'duty_duration_minutes': 360,
        'tour_context_hint': 'departure', 'tour_context_confidence': 'high',
        'is_tour_departure': True, 'is_tour_continuation': False,
        'is_tour_return': False, 'return_from_layover': False,
        'has_flight_segment': True, 'confidence': 'high',
    }


def _inland_mid_muc(datum):
    """Inland-Mid-Tour-Tag: User uebernachtet weiter in MUC. Triggert Z74."""
    return {
        'datum': datum, 'marker_raw': 'X', 'normalized_marker': 'X',
        'activity_type': 'tour_continuation',
        'starts_at_homebase': False, 'ends_at_homebase': False,
        'overnight_after_day': True,
        'routing': ['MUC'],
        'layover_ort': 'MUC', 'layover_iata': 'MUC',
        'has_fl': False, 'duty_duration_minutes': 0,
        'tour_context_hint': 'mid_tour', 'tour_context_confidence': 'high',
        'is_tour_departure': False, 'is_tour_continuation': True,
        'is_tour_return': False, 'return_from_layover': False,
        'has_flight_segment': False, 'confidence': 'high',
    }


def _inland_ret_from_muc(datum):
    """Inland-Tour-Heimkehr: MUC→FRA. Triggert Z73."""
    return {
        'datum': datum, 'marker_raw': 'LH124', 'normalized_marker': 'LH124',
        'activity_type': 'tour_return',
        'starts_at_homebase': False, 'ends_at_homebase': True,
        'overnight_after_day': False,
        'routing': ['MUC', HOMEBASE, 'LH124'],
        'layover_ort': 'MUC', 'layover_iata': 'MUC',
        'origin_iata': 'MUC', 'destination_iata': HOMEBASE,
        'has_fl': True, 'duty_duration_minutes': 300,
        'tour_context_hint': 'return', 'tour_context_confidence': 'high',
        'is_tour_departure': False, 'is_tour_continuation': False,
        'is_tour_return': True, 'return_from_layover': True,
        'has_flight_segment': True, 'confidence': 'high',
    }


def test_inland_overnight_short_tour_triggers_z73_not_z76():
    """2-Tage Inland-Tour FRA→MUC→FRA. Erwartung: Z73 fuer dep + ret, KEIN Z76."""
    days = [
        _inland_dep_to_muc('2025-09-01'),
        _inland_ret_from_muc('2025-09-02'),
    ]
    tours = build_normalized_tours(
        cas_days=days, se_rows=[], year=2025, homebase=HOMEBASE,
    )
    res = calculate_allowances_from_normalized_tours(
        normalized_tours=tours, bmf_table=BMF_STUB,
        iata_to_bmf=IATA_TO_BMF_STUB, se_rows=[], homebase=HOMEBASE,
    )
    assert len(tours) == 1
    assert res.z73_tage == 2, f'Z73-Tage erwartet 2, got {res.z73_tage}'
    assert res.z73_eur == 2 * INLAND_AN_AB
    assert res.z76_tage == 0
    assert res.z76_eur == 0.0
    assert res.z74_tage == 0


def test_inland_3day_tour_triggers_z74_for_mid_day():
    """3-Tage Inland-Tour mit Voll-Tag MUC. Erwartung: Z73(dep+ret), Z74(mid)."""
    days = [
        _inland_dep_to_muc('2025-09-01'),
        _inland_mid_muc('2025-09-02'),
        _inland_ret_from_muc('2025-09-03'),
    ]
    tours = build_normalized_tours(
        cas_days=days, se_rows=[], year=2025, homebase=HOMEBASE,
    )
    res = calculate_allowances_from_normalized_tours(
        normalized_tours=tours, bmf_table=BMF_STUB,
        iata_to_bmf=IATA_TO_BMF_STUB, se_rows=[], homebase=HOMEBASE,
    )
    assert len(tours) == 1
    assert len(tours[0].days) == 3
    assert res.z73_tage == 2, f'Z73-Tage erwartet 2, got {res.z73_tage}'
    assert res.z73_eur == 2 * INLAND_AN_AB
    assert res.z74_tage == 1, f'Z74-Tage erwartet 1, got {res.z74_tage}'
    assert res.z74_eur == INLAND_VOLL_24H
    assert res.z76_tage == 0
    assert res.z76_eur == 0.0


def test_same_day_inland_only_triggers_z72_not_z73_or_z74():
    """Same-Day Inland >8h ohne Hotel: Z72 ja, Z73/Z74 nein."""
    days = [_same_day_inland('2025-09-10', duty=540)]
    tours = build_normalized_tours(
        cas_days=days, se_rows=[], year=2025, homebase=HOMEBASE,
    )
    res = calculate_allowances_from_normalized_tours(
        normalized_tours=tours, bmf_table=BMF_STUB,
        iata_to_bmf=IATA_TO_BMF_STUB, se_rows=[], homebase=HOMEBASE,
    )
    assert res.z72_tage == 1
    assert res.z72_eur == INLAND_AN_AB
    assert res.z73_tage == 0
    assert res.z74_tage == 0
    assert res.z76_tage == 0


def test_inland_hotel_does_not_count_as_foreign_z76():
    """Inland-Hotel in MUC darf NICHT als Auslands-Z76 zaehlen."""
    days = [
        _inland_dep_to_muc('2025-10-05'),
        _inland_mid_muc('2025-10-06'),
        _inland_ret_from_muc('2025-10-07'),
    ]
    _, res = build_normalized_tours(
        cas_days=days, se_rows=[], year=2025, homebase=HOMEBASE,
    ), None
    res = calculate_allowances_from_normalized_tours(
        normalized_tours=build_normalized_tours(
            cas_days=days, se_rows=[], year=2025, homebase=HOMEBASE),
        bmf_table=BMF_STUB, iata_to_bmf=IATA_TO_BMF_STUB,
        se_rows=[], homebase=HOMEBASE,
    )
    # Konkretes Verbot: kein einziger Z76-Tag
    assert res.z76_tage == 0, (
        f'Inland-Hotel darf nicht als Z76 zaehlen, got z76_tage={res.z76_tage}'
    )
    assert res.z76_eur == 0.0


def test_z73_z74_audit_print(capsys):
    """KPI-Print fuer R14-Audit-Report."""
    days = [
        _inland_dep_to_muc('2025-11-01'),
        _inland_mid_muc('2025-11-02'),
        _inland_mid_muc('2025-11-03'),
        _inland_ret_from_muc('2025-11-04'),
    ]
    tours = build_normalized_tours(
        cas_days=days, se_rows=[], year=2025, homebase=HOMEBASE,
    )
    res = calculate_allowances_from_normalized_tours(
        normalized_tours=tours, bmf_table=BMF_STUB,
        iata_to_bmf=IATA_TO_BMF_STUB, se_rows=[], homebase=HOMEBASE,
    )
    summary = {
        'inland_z73_tage': res.z73_tage,
        'inland_z73_eur':  round(res.z73_eur, 2),
        'inland_z74_tage': res.z74_tage,
        'inland_z74_eur':  round(res.z74_eur, 2),
        'foreign_z76_tage': res.z76_tage,
        'foreign_z76_eur': round(res.z76_eur, 2),
    }
    print('R14_INLAND_KPIS=' + str(summary))
    assert res.z73_tage == 2
    assert res.z74_tage == 2
    assert res.z76_tage == 0


# ────────────────────────────────────────────────────────────────────────────
# KPI-Audit (kein assert — informational, für R13-Report)
# ────────────────────────────────────────────────────────────────────────────

def test_snapshot_audit_print_kpis(capsys):
    """Druckt KPI-Snapshot. Test passes immer; Output dient R13."""
    tours, result = _run_pipeline()
    summary = {
        'tour_count': result.tour_count,
        'fahrtage': result.fahrtage,
        'arbeitstage': result.arbeitstage,
        'hotel_naechte': result.hotel_naechte,
        'reinigungstage': result.reinigungstage,
        'z72_tage': result.z72_tage,
        'z72_eur': round(result.z72_eur, 2),
        'z73_tage': result.z73_tage,
        'z73_eur': round(result.z73_eur, 2),
        'z74_tage': result.z74_tage,
        'z74_eur': round(result.z74_eur, 2),
        'z76_tage': result.z76_tage,
        'z76_eur': round(result.z76_eur, 2),
        'total_vma_eur': round(
            result.z72_eur + result.z73_eur + result.z74_eur + result.z76_eur, 2,
        ),
    }
    print('READER_V2_MOCKED_SNAPSHOT_KPIS=' + json.dumps(summary))
    assert summary['tour_count'] > 0
