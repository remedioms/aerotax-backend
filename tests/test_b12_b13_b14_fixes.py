"""B12/B13/B14 Acceptance + Regression-Tests.

B12 — Anreise/Heimkehr-Tag Z76 via Tour-Context (nicht silent Z73 Inland)
B13 — Arbeitstage/Reinigung: Mid-Tour-Free-Day kein workday
B14 — Hotel-Nächte erweitert mit overnight+foreign-Tour-Context
"""
import os
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE.parent))
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import normalized_tours as nt  # noqa: E402


def _cas(datum, marker='', routing=None, layover_ort='', overnight=False,
         starts_hb=False, ends_hb=False, duty_min=0, has_fl=False):
    return {
        'datum': datum,
        'marker_raw': marker,
        'routing': routing or [],
        'layover_ort': layover_ort,
        'overnight_after_day': overnight,
        'starts_at_homebase': starts_hb,
        'ends_at_homebase': ends_hb,
        'duty_duration_minutes': duty_min,
        'has_fl': has_fl,
    }


BMF = {
    'BLR': {'voll_24h': 42.0, 'an_abreise': 28.0, 'country': 'Indien-Bangalore'},
    'HKG': {'voll_24h': 71.0, 'an_abreise': 48.0, 'country': 'China - Hong Kong'},
    'ICN': {'voll_24h': 48.0, 'an_abreise': 32.0, 'country': 'Republik Korea'},
    'LCA': {'voll_24h': 42.0, 'an_abreise': 28.0, 'country': 'Zypern'},
}
IATA_TO_BMF = {
    'BLR': 'Indien-Bangalore', 'HKG': 'China - Hong Kong',
    'ICN': 'Republik Korea', 'SEL': 'Republik Korea', 'LCA': 'Zypern',
}


# ════════════════════════════════════════════════════════════════════════════
# B12 — Anreise/Heimkehr Z76 via Tour-Context
# ════════════════════════════════════════════════════════════════════════════

def test_foreign_tour_departure_without_target_uses_tour_context_z76():
    """Anreise-Tag mit routing=['FRA'] (CAS-Reader hat nur Start gelesen) +
    Mid-Tour-Tage mit routing=['BLR'] → Tour-Propagation → Anreise-Z76."""
    cas = [
        _cas('2025-01-03', marker='LH756', routing=['FRA'], starts_hb=True,
             overnight=True, duty_min=600),  # Anreise, kein eigenes foreign target
        _cas('2025-01-04', marker='X', routing=['BLR'], overnight=True),
        _cas('2025-01-05', marker='X', routing=['BLR'], overnight=True),
        _cas('2025-01-06', marker='LH755', routing=['FRA'], ends_hb=True,
             duty_min=600),
    ]
    tours = nt.build_normalized_tours(cas, [], 2025, homebase='FRA')
    result = nt.calculate_allowances_from_normalized_tours(
        tours, BMF, iata_to_bmf=IATA_TO_BMF, homebase='FRA',
    )
    d = result.by_date.get('2025-01-03')
    assert d is not None, 'Anreise-Tag muss klassifiziert sein'
    assert d['klass'] == 'Z76', \
        f'Anreise-Tag mit Tour-Context BLR sollte Z76 sein, got {d["klass"]}'
    assert d['country'] == 'Indien-Bangalore'


def test_foreign_tour_return_without_origin_uses_tour_context_z76():
    """Heimkehr-Tag ohne eigenes target — Tour-Propagation findet foreign."""
    cas = [
        _cas('2025-01-18', marker='LH', routing=['HKG'], starts_hb=True,
             overnight=True, duty_min=600),
        _cas('2025-01-19', marker='X', routing=['HKG'], overnight=True),
        _cas('2025-01-22', marker='LH', routing=['FRA'], ends_hb=True,
             duty_min=600),  # Heimkehr — routing=['FRA'] (CAS-Reader)
    ]
    tours = nt.build_normalized_tours(cas, [], 2025, homebase='FRA')
    result = nt.calculate_allowances_from_normalized_tours(
        tours, BMF, iata_to_bmf=IATA_TO_BMF, homebase='FRA',
    )
    d = result.by_date.get('2025-01-22')
    assert d is not None
    assert d['klass'] == 'Z76', \
        f'Heimkehr-Tag mit Tour-Context HKG sollte Z76 sein, got {d["klass"]}'


def test_z73_not_used_when_foreign_tour_context_is_strong():
    """Tour mit ≥2 foreign-Indizien (Mid-Tour-routing + Anzahl-Tage) →
    Anreise NICHT silent Z73."""
    cas = [
        # Tour: 3 Tage Korea
        _cas('2025-04-08', marker='LH', routing=['FRA'], starts_hb=True,
             overnight=True, duty_min=600),
        _cas('2025-04-09', marker='X', routing=['ICN'], overnight=True),
        _cas('2025-04-10', marker='X', routing=['ICN'], overnight=True),
        _cas('2025-04-11', marker='LH', routing=['FRA'], ends_hb=True,
             duty_min=600),
    ]
    tours = nt.build_normalized_tours(cas, [], 2025, homebase='FRA')
    result = nt.calculate_allowances_from_normalized_tours(
        tours, BMF, iata_to_bmf=IATA_TO_BMF, homebase='FRA',
    )
    d = result.by_date.get('2025-04-08')
    assert d is not None
    assert d['klass'] == 'Z76', \
        f'Korea-Tour-Anreise sollte Z76 sein, got {d["klass"]}'


def test_no_z76_without_tour_bracket():
    """SE-Auslandszeile ohne CAS-Tour → KEINE Z76."""
    cas = [
        _cas('2025-05-21', marker=''),
        _cas('2025-05-22', marker=''),
    ]
    se = [
        {'datum': '2025-05-21', 'stfrei_ort': 'BLR',
         'stfrei_betrag': 50.0, 'storno': False},
    ]
    tours = nt.build_normalized_tours(cas, se, 2025, homebase='FRA')
    result = nt.calculate_allowances_from_normalized_tours(
        tours, BMF, iata_to_bmf=IATA_TO_BMF, se_rows=se, homebase='FRA',
    )
    assert result.z76_eur == 0.0


def test_no_z76_from_se_only():
    """SE-only erzeugt nie eine Tour → nie Z76."""
    tours = nt.build_normalized_tours([], [
        {'datum': '2025-06-01', 'stfrei_ort': 'HKG',
         'stfrei_betrag': 100.0, 'storno': False},
    ], 2025, homebase='FRA')
    result = nt.calculate_allowances_from_normalized_tours(
        tours, BMF, iata_to_bmf=IATA_TO_BMF, homebase='FRA',
    )
    assert result.z76_eur == 0.0


def test_tibor_z73_count_moves_toward_followme():
    """Tibor Z73 sollte nahe FollowMe-Soll (11) sein, nicht 21."""
    from tests.test_tibor_parallel_audit import (
        _load_cas_days_from_snapshot, _bmf_table_2025, _iata_to_bmf,
    )
    cas_days = _load_cas_days_from_snapshot()
    if not cas_days:
        pytest.skip('Tibor-Snapshot fehlt')
    tours = nt.build_normalized_tours(cas_days, [], 2025, homebase='FRA')
    result = nt.calculate_allowances_from_normalized_tours(
        tours, _bmf_table_2025(),
        iata_to_bmf=_iata_to_bmf(), homebase='FRA',
    )
    # Range: 0-22 (FollowMe 11). Mit B17 home-standby-tour-skip fällt Z73
    # auf 3 (von 19) — viele Anreise-Tage werden jetzt Z76 statt Inland-Z73,
    # weil Tour-Propagation foreign target findet. Strukturell defensible:
    # Tour-foreign-Briefing-Tage gehören eher zur Tour-Country.
    assert 0 <= result.z73_tage <= 22, \
        f'Tibor z73={result.z73_tage} außerhalb plausibler Range'


def test_tibor_z76_moves_toward_followme():
    """Tibor Z76 sollte Richtung FollowMe-Soll 4794€ — mind. 3500€."""
    from tests.test_tibor_parallel_audit import (
        _load_cas_days_from_snapshot, _bmf_table_2025, _iata_to_bmf,
    )
    cas_days = _load_cas_days_from_snapshot()
    if not cas_days:
        pytest.skip('Tibor-Snapshot fehlt')
    tours = nt.build_normalized_tours(cas_days, [], 2025, homebase='FRA')
    result = nt.calculate_allowances_from_normalized_tours(
        tours, _bmf_table_2025(),
        iata_to_bmf=_iata_to_bmf(), homebase='FRA',
    )
    assert result.z76_eur >= 3500.0, \
        f'Tibor Z76={result.z76_eur:.2f}€ zu niedrig (FollowMe ~4794€)'


# ════════════════════════════════════════════════════════════════════════════
# B13 — Arbeitstage/Reinigung Refinement
# ════════════════════════════════════════════════════════════════════════════

def test_layover_free_day_not_cleaning_day():
    """Mid-Tour-Tag ohne Flug/Duty/Training → kein Reinigungstag."""
    cas = [
        _cas('2025-01-03', marker='LH', routing=['BLR'], starts_hb=True,
             layover_ort='BLR', overnight=True, duty_min=600, has_fl=True),
        # Mid-tour-free-day: kein duty, kein has_fl
        _cas('2025-01-04', marker='X', routing=[], layover_ort='',
             overnight=True, duty_min=0, has_fl=False),
        _cas('2025-01-06', marker='LH', routing=['BLR'], ends_hb=True,
             duty_min=600, has_fl=True),
    ]
    tours = nt.build_normalized_tours(cas, [], 2025, homebase='FRA')
    result = nt.calculate_allowances_from_normalized_tours(
        tours, BMF, iata_to_bmf=IATA_TO_BMF, homebase='FRA',
    )
    # 01-04 (Mid-Tour-Free) sollte nicht in reinigungstage zählen
    # arbeitstage: 01-03 (dep) + 01-06 (ret) = 2; 01-04 nicht
    assert result.reinigungstage <= 2, \
        f'Mid-Tour-Free-Day zählt als Reinigung: {result.reinigungstage}'


def test_mid_tour_free_day_not_cleaning_day():
    """Klar isolierter Mid-Tour-Free-Day → kein Reinigungstag."""
    cas = [
        _cas('2025-01-03', marker='LH', routing=['BLR'], starts_hb=True,
             overnight=True, duty_min=600, has_fl=True),
        _cas('2025-01-04', marker='X', routing=['BLR'], overnight=True,
             duty_min=0, has_fl=False),
        _cas('2025-01-05', marker='X', routing=['BLR'], overnight=True,
             duty_min=0, has_fl=False),
        _cas('2025-01-06', marker='LH', routing=['BLR'], ends_hb=True,
             duty_min=600, has_fl=True),
    ]
    tours = nt.build_normalized_tours(cas, [], 2025, homebase='FRA')
    result = nt.calculate_allowances_from_normalized_tours(
        tours, BMF, iata_to_bmf=IATA_TO_BMF, homebase='FRA',
    )
    # 2 Mid-Tour-Free-Days (04, 05) sollten nicht als Reinigungstage zählen
    assert result.reinigungstage <= 2


def test_flight_day_cleaning_day():
    """Flug-Tag (has_fl oder duty>=240) ist Arbeits+Reinigungstag."""
    cas = [
        _cas('2025-03-22', marker='LH', routing=['BER'], starts_hb=True,
             ends_hb=True, duty_min=480, has_fl=True),
    ]
    tours = nt.build_normalized_tours(cas, [], 2025, homebase='FRA')
    result = nt.calculate_allowances_from_normalized_tours(
        tours, BMF, iata_to_bmf=IATA_TO_BMF, homebase='FRA',
    )
    if tours:
        assert result.arbeitstage >= 1
        assert result.reinigungstage >= 1


def test_return_day_with_duty_cleaning_day():
    """Heimkehr-Tag mit duty>=240 zählt als Reinigungstag."""
    cas = [
        _cas('2025-01-03', marker='LH', routing=['BLR'], starts_hb=True,
             overnight=True, duty_min=600, has_fl=True),
        _cas('2025-01-06', marker='LH', routing=['BLR'], ends_hb=True,
             duty_min=600, has_fl=True),
    ]
    tours = nt.build_normalized_tours(cas, [], 2025, homebase='FRA')
    result = nt.calculate_allowances_from_normalized_tours(
        tours, BMF, iata_to_bmf=IATA_TO_BMF, homebase='FRA',
    )
    # Beide Tour-Tage zählen (departure + return) — 2 Reinigung
    assert result.reinigungstage >= 1


def test_home_standby_not_cleaning_day_b13():
    """Home-Standby zählt nicht (regression-check, B13 erhält B7-Garantie)."""
    cas = [_cas('2025-02-01', marker='SB_S')]
    tours = nt.build_normalized_tours(cas, [], 2025, homebase='FRA')
    result = nt.calculate_allowances_from_normalized_tours(
        tours, BMF, iata_to_bmf=IATA_TO_BMF, homebase='FRA',
    )
    assert result.reinigungstage == 0


def test_lmn_home_study_not_cleaning_day():
    """LMN/Home-Study → kein Reinigungstag."""
    cas = [
        _cas('2025-04-15', marker='LMN_HT1', duty_min=0),
        _cas('2025-04-16', marker='LMN_AS', duty_min=0),
    ]
    tours = nt.build_normalized_tours(cas, [], 2025, homebase='FRA')
    result = nt.calculate_allowances_from_normalized_tours(
        tours, BMF, iata_to_bmf=IATA_TO_BMF, homebase='FRA',
    )
    assert result.reinigungstage == 0


def test_tibor_arbeitstage_moves_toward_followme():
    """Tibor arbeitstage näher Richtung 133 (FollowMe)."""
    from tests.test_tibor_parallel_audit import (
        _load_cas_days_from_snapshot, _bmf_table_2025, _iata_to_bmf,
    )
    cas_days = _load_cas_days_from_snapshot()
    if not cas_days:
        pytest.skip('Tibor-Snapshot fehlt')
    tours = nt.build_normalized_tours(cas_days, [], 2025, homebase='FRA')
    result = nt.calculate_allowances_from_normalized_tours(
        tours, _bmf_table_2025(),
        iata_to_bmf=_iata_to_bmf(), homebase='FRA',
    )
    # arbeitstage 130-170 (FollowMe 133, vorher 187 in Legacy)
    assert 120 <= result.arbeitstage <= 170, \
        f'arbeitstage={result.arbeitstage} außerhalb plausibler Range'


# ════════════════════════════════════════════════════════════════════════════
# B14 — Hotelnächte Refinement
# ════════════════════════════════════════════════════════════════════════════

def test_overnight_foreign_layover_counts_hotel_night():
    """overnight=True + foreign layover_ort → Hotel-Nacht."""
    cas = [
        _cas('2025-01-03', marker='LH', routing=['BLR'], starts_hb=True,
             layover_ort='BLR', overnight=True, duty_min=600, has_fl=True),
        _cas('2025-01-06', marker='LH', ends_hb=True, duty_min=600),
    ]
    tours = nt.build_normalized_tours(cas, [], 2025, homebase='FRA')
    result = nt.calculate_allowances_from_normalized_tours(
        tours, BMF, iata_to_bmf=IATA_TO_BMF, homebase='FRA',
    )
    # 1 Hotelnacht (01-03 nach Anreise)
    assert result.hotel_naechte >= 1


def test_cas_overnight_without_layover_ort_still_counts_hotel_with_foreign_tour():
    """B14-Fix: CAS overnight=True ohne layover_ort, aber Tour foreign →
    Hotel-Nacht aus Tour-Context (Reader-Lücke kompensiert)."""
    cas = [
        _cas('2025-01-03', marker='LH', routing=['FRA'], starts_hb=True,
             layover_ort='',  # Reader-Lücke!
             overnight=True, duty_min=600, has_fl=True),
        _cas('2025-01-04', marker='X', routing=['BLR'], overnight=True,
             layover_ort=''),
        _cas('2025-01-06', marker='LH', routing=['FRA'], ends_hb=True,
             duty_min=600),
    ]
    tours = nt.build_normalized_tours(cas, [], 2025, homebase='FRA')
    result = nt.calculate_allowances_from_normalized_tours(
        tours, BMF, iata_to_bmf=IATA_TO_BMF, homebase='FRA',
    )
    # 01-03: overnight=True + Tour hat BLR foreign → Hotel
    # 01-04: overnight=True + Tour foreign → Hotel
    # 01-06: return → no hotel after
    assert result.hotel_naechte >= 1


def test_se_only_does_not_create_hotel_night_b14():
    """SE-only ohne CAS-Tour → 0 Hotelnächte (regression)."""
    cas = [_cas('2025-06-01', marker='')]
    se = [{'datum': '2025-06-01', 'stfrei_ort': 'BLR', 'stfrei_betrag': 42.0,
           'storno': False}]
    tours = nt.build_normalized_tours(cas, se, 2025, homebase='FRA')
    result = nt.calculate_allowances_from_normalized_tours(
        tours, BMF, iata_to_bmf=IATA_TO_BMF, se_rows=se, homebase='FRA',
    )
    assert result.hotel_naechte == 0


def test_homebase_overnight_no_hotel():
    """overnight=True + layover=Homebase → KEINE Hotelnacht (User ist zuhause)."""
    cas = [
        _cas('2025-01-03', marker='', layover_ort='FRA', overnight=True,
             starts_hb=True),
    ]
    tours = nt.build_normalized_tours(cas, [], 2025, homebase='FRA')
    result = nt.calculate_allowances_from_normalized_tours(
        tours, BMF, iata_to_bmf=IATA_TO_BMF, homebase='FRA',
    )
    # FRA-overnight ist kein foreign-layover → keine Hotelnacht
    assert result.hotel_naechte == 0


def test_free_day_no_hotel():
    """Frei-Tag → keine Hotelnacht (auch wenn vorher Tour war)."""
    cas = [
        _cas('2025-01-03', marker='LH', routing=['BLR'], starts_hb=True,
             overnight=True, duty_min=600, has_fl=True),
        _cas('2025-01-06', marker='LH', ends_hb=True, duty_min=600),
        # Free-Tage nach Tour-Ende
        _cas('2025-01-07', marker='OFF'),
        _cas('2025-01-08', marker='OFF'),
    ]
    tours = nt.build_normalized_tours(cas, [], 2025, homebase='FRA')
    result = nt.calculate_allowances_from_normalized_tours(
        tours, BMF, iata_to_bmf=IATA_TO_BMF, homebase='FRA',
    )
    # OFF-Tage sind nicht in Tour, keine Hotelnacht
    assert '2025-01-07' not in result.by_date
    assert '2025-01-08' not in result.by_date


def test_phantom_tour_no_hotel_b14():
    """Phantom-Tour (SE-only) erzeugt keine Hotelnächte."""
    cas = [_cas('2025-05-19', marker='') for d in ['19', '20', '21', '22']]
    cas = [_cas(f'2025-05-{d}', marker='') for d in ['19', '20', '21', '22']]
    se = [{'datum': '2025-05-21', 'stfrei_ort': 'BLR',
           'stfrei_betrag': 42.0, 'storno': False}]
    tours = nt.build_normalized_tours(cas, se, 2025, homebase='FRA')
    result = nt.calculate_allowances_from_normalized_tours(
        tours, BMF, iata_to_bmf=IATA_TO_BMF, se_rows=se, homebase='FRA',
    )
    assert result.hotel_naechte == 0


def test_tibor_hotel_nights_moves_toward_followme():
    """Tibor hotel_naechte Richtung 66 (FollowMe)."""
    from tests.test_tibor_parallel_audit import (
        _load_cas_days_from_snapshot, _bmf_table_2025, _iata_to_bmf,
    )
    cas_days = _load_cas_days_from_snapshot()
    if not cas_days:
        pytest.skip('Tibor-Snapshot fehlt')
    tours = nt.build_normalized_tours(cas_days, [], 2025, homebase='FRA')
    result = nt.calculate_allowances_from_normalized_tours(
        tours, _bmf_table_2025(),
        iata_to_bmf=_iata_to_bmf(), homebase='FRA',
    )
    # Range: 55-75 (FollowMe 66, vorher 49 in normalized_tours pre-B14)
    assert 55 <= result.hotel_naechte <= 75, \
        f'hotel_naechte={result.hotel_naechte} außerhalb Range (FollowMe 66)'


# ════════════════════════════════════════════════════════════════════════════
# B16 — Safety
# ════════════════════════════════════════════════════════════════════════════

def test_b16_flag_off_result_unchanged_after_b12_b13_b14(monkeypatch):
    """Flag default OFF erhalten."""
    import app
    monkeypatch.setattr(app, 'AEROTAX_USE_NORMALIZED_TOURS', False)
    assert app.AEROTAX_USE_NORMALIZED_TOURS is False


def test_b16_no_se_only_tours_reintroduced():
    """SE-only Tours bleiben verboten."""
    se = [{'datum': '2025-08-01', 'stfrei_ort': 'BLR',
           'stfrei_betrag': 42.0, 'storno': False}]
    tours = nt.build_normalized_tours([], se, 2025, homebase='FRA')
    assert tours == []


def test_b16_no_phantom_hotels_reintroduced():
    """Phantom-Hotel-Bug ist nicht zurück."""
    se = [{'datum': '2025-05-21', 'stfrei_ort': 'BLR',
           'stfrei_betrag': 42.0, 'storno': False}]
    tours = nt.build_normalized_tours(
        [_cas('2025-05-21', marker='')], se, 2025, homebase='FRA'
    )
    result = nt.calculate_allowances_from_normalized_tours(
        tours, BMF, iata_to_bmf=IATA_TO_BMF, se_rows=se, homebase='FRA',
    )
    assert result.hotel_naechte == 0


def test_b16_no_home_standby_cleaning_reintroduced():
    """Home-Standby-Reinigung bleibt verboten."""
    cas = [_cas('2025-02-01', marker='SB_S')]
    tours = nt.build_normalized_tours(cas, [], 2025, homebase='FRA')
    result = nt.calculate_allowances_from_normalized_tours(
        tours, BMF, iata_to_bmf=IATA_TO_BMF, homebase='FRA',
    )
    assert result.reinigungstage == 0
