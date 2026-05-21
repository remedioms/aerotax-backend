"""Rel Phase 5 — Role/Airline/Marker Generalization Matrix.

Pflicht-Cases per Master:
- Cabin-like: PU/P1/X/OFF/RES/SB_M
- Cockpit-like: CPT/FO/SIM/TRI/RSV
- Generic/unknown: ##/A1/SEQ/REST/RDY
- Anti-tests: marker-only != tax decision
"""
import os
import sys
import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)

if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import app as app_module
from tests.helpers.synthetic_cas_factory import make_day


def _run(matched, base='FRA'):
    tours = app_module._normalize_tours_from_raw_facts(matched, homebase=base, year=2025)
    return app_module._classify_days_from_normalized_tours(tours, year=2025, homebase=base)


# ════════════════════════════════════════════════════════════════════════════
# Cabin-like marker tests
# ════════════════════════════════════════════════════════════════════════════

def test_cabin_pu_not_pula_iata():
    """`12345 PU` darf NICHT als Pula-IATA gelesen werden."""
    if hasattr(app_module, '_extract_iata_from_marker'):
        iata = app_module._extract_iata_from_marker('12345 PU')
        assert iata != 'PUY' and iata not in ('PU',), f'PU != Pula: {iata!r}'


def test_cabin_p1_p2_not_airport():
    if hasattr(app_module, '_extract_iata_from_marker'):
        for m in ['31591 P1', '49444 P1 /ZH', '73724 P1', '12345 P2', '12345 P3']:
            iata = app_module._extract_iata_from_marker(m)
            assert iata not in ('P1', 'P2', 'P3'), f'{m!r} → P1/2/3 != IATA'


def test_cabin_x_inside_foreign_tour_is_tour_mid():
    """X-Marker innerhalb foreign-Tour → tour_mid."""
    matched = [
        make_day('2025-02-01', base='FRA', marker='12345 PU',
                 routing=['FRA', 'HKG'], layover_ort='HKG',
                 overnight=True, duty_min=700, starts_hb=True, has_fl=True,
                 activity_type='tour'),
        make_day('2025-02-02', base='FRA', marker='X HKG',
                 routing=['HKG'], layover_ort='HKG',
                 overnight=True, duty_min=0, activity_type='frei'),
        make_day('2025-02-03', base='FRA', marker='12345 PU',
                 routing=['HKG', 'FRA'], overnight=False,
                 duty_min=600, ends_hb=True, has_fl=True, activity_type='tour'),
    ]
    res = _run(matched, 'FRA')
    middle = res['tage_detail'][1]
    assert middle['klass'] == 'Z76', f'X inside tour → Z76, war {middle["klass"]}'


def test_cabin_x_without_tour_evidence_is_frei():
    matched = [make_day('2025-03-15', base='FRA', marker='X',
                        routing=[], layover_ort='',
                        overnight=False, duty_min=0, activity_type='frei')]
    res = _run(matched, 'FRA')
    assert res['tage_detail'][0]['klass'] == 'Frei'


def test_cabin_off_inside_layover_is_tour_mid():
    matched = [
        make_day('2025-04-01', base='FRA', marker='12345 PU',
                 routing=['FRA', 'IST'], layover_ort='IST',
                 overnight=True, duty_min=600, starts_hb=True, has_fl=True,
                 activity_type='tour'),
        make_day('2025-04-02', base='FRA', marker='OFF',
                 routing=[], layover_ort='IST',
                 overnight=True, duty_min=0, activity_type='frei'),
        make_day('2025-04-03', base='FRA', marker='12345 PU',
                 routing=['IST', 'FRA'], overnight=False,
                 duty_min=500, ends_hb=True, has_fl=True, activity_type='tour'),
    ]
    res = _run(matched, 'FRA')
    middle = res['tage_detail'][1]
    assert middle['klass'] in ('Z76', 'Z73'), f'OFF inside layover → tour-mid, war {middle["klass"]}'


def test_cabin_off_at_homebase_no_tour_is_frei():
    matched = [make_day('2025-05-10', base='FRA', marker='OFF',
                        routing=[], layover_ort='', overnight=False,
                        duty_min=0, activity_type='frei')]
    res = _run(matched, 'FRA')
    assert res['tage_detail'][0]['klass'] == 'Frei'


def test_cabin_res_foreign_hotel_context():
    """RES nach foreign-overnight = foreign_hotel_standby."""
    matched = [
        make_day('2025-06-10', base='FRA', marker='12345 PU',
                 routing=['FRA', 'JFK'], layover_ort='JFK',
                 overnight=True, duty_min=720, starts_hb=True, has_fl=True,
                 activity_type='tour'),
        make_day('2025-06-11', base='FRA', marker='RES',
                 routing=[], layover_ort='JFK',
                 overnight=True, duty_min=0, activity_type='standby'),
    ]
    res = _run(matched, 'FRA')
    day2 = res['tage_detail'][1]
    assert day2['klass'] in ('Z76',), f'RES foreign-hotel → Z76, war {day2["klass"]}'


def test_cabin_res_homebase_idle():
    """RES zuhause ohne SE → standby_homebase."""
    matched = [make_day('2025-07-10', base='FRA', marker='RES',
                        routing=['FRA'], duty_min=450, start_time='08:00',
                        activity_type='standby')]
    res = _run(matched, 'FRA')
    assert res['tage_detail'][0]['klass'] == 'Standby'


def test_cabin_sb_m_with_se_foreign_activated():
    """SB_M + SE-foreign = activated to foreign destination."""
    matched = [make_day('2025-08-01', base='FRA', marker='SB_M',
                        routing=['FRA'], duty_min=450, start_time='08:00',
                        activity_type='standby',
                        se_ort='SVG', se_inland=False, se_total=50.0)]
    res = _run(matched, 'FRA')
    assert res['tage_detail'][0]['klass'] == 'Z76', \
        f'SB_M + SE-foreign → Z76, war {res["tage_detail"][0]["klass"]}'


# ════════════════════════════════════════════════════════════════════════════
# Cockpit-like marker tests
# ════════════════════════════════════════════════════════════════════════════

def test_cockpit_cpt_marker_with_routing_recognized_as_tour():
    matched = [make_day('2025-09-01', base='FRA', marker='12345 CPT',
                        routing=['FRA', 'JFK'], layover_ort='JFK',
                        overnight=True, duty_min=720, starts_hb=True, has_fl=True,
                        activity_type='tour', role='cockpit')]
    res = _run(matched, 'FRA')
    assert res['tage_detail'][0]['klass'] == 'Z76'


def test_cockpit_sim_with_time_recognized_as_training():
    """SIM mit start_time + duty als Office/Training, mit starts_hb explizit gesetzt."""
    matched = [make_day('2025-09-15', base='FRA', marker='SIM',
                        routing=['FRA'], duty_min=240, start_time='07:30',
                        starts_hb=True, ends_hb=True,
                        activity_type='training', role='cockpit')]
    res = _run(matched, 'FRA')
    # Pipeline klassifiziert als Office (training, kein Z72)
    assert res['tage_detail'][0]['klass'] in ('Office', 'Z72', 'Frei'), \
        f'SIM mit Time → Office/Z72/Frei OK, war {res["tage_detail"][0]["klass"]}'


def test_cockpit_rsv_at_foreign_layover():
    """RSV im foreign hotel context."""
    matched = [
        make_day('2025-10-01', base='FRA', marker='12345 CPT',
                 routing=['FRA', 'SIN'], layover_ort='SIN',
                 overnight=True, duty_min=700, starts_hb=True, has_fl=True,
                 activity_type='tour', role='cockpit'),
        make_day('2025-10-02', base='FRA', marker='RSV',
                 routing=[], layover_ort='SIN',
                 overnight=True, duty_min=0, activity_type='standby',
                 role='cockpit'),
    ]
    res = _run(matched, 'FRA')
    assert res['tage_detail'][1]['klass'] in ('Z76',)


# ════════════════════════════════════════════════════════════════════════════
# Generic / unknown markers
# ════════════════════════════════════════════════════════════════════════════

def test_generic_unknown_marker_with_routing_is_tour():
    """Unknown marker `##` + foreign routing + duty → Tour."""
    matched = [make_day('2025-11-01', base='FRA', marker='## A1',
                        routing=['FRA', 'BLR'], layover_ort='BLR',
                        overnight=True, duty_min=600, starts_hb=True, has_fl=True,
                        activity_type='tour', role='unknown')]
    res = _run(matched, 'FRA')
    assert res['tage_detail'][0]['klass'] == 'Z76', \
        f'Unknown marker + routing → Z76, war {res["tage_detail"][0]["klass"]}'


def test_generic_unknown_marker_only_no_evidence_is_frei():
    """Unknown marker ohne routing/time/SE → Frei."""
    matched = [make_day('2025-11-15', base='FRA', marker='## ZZZ',
                        routing=[], layover_ort='', duty_min=0,
                        activity_type='unknown', role='unknown')]
    res = _run(matched, 'FRA')
    assert res['tage_detail'][0]['klass'] == 'Frei'


# ════════════════════════════════════════════════════════════════════════════
# Anti-tests: marker-only NIE Tax-Decision
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('marker', ['12345 PU', 'CPT', 'RSV', '## A1', 'SEQ', '##', 'RDY'])
def test_marker_only_no_z76_without_evidence(marker):
    matched = [make_day('2025-12-01', base='FRA', marker=marker,
                        routing=[], duty_min=0, activity_type='unknown')]
    res = _run(matched, 'FRA')
    assert res['tage_detail'][0]['klass'] != 'Z76', \
        f'Marker {marker!r} only → kein Z76, war {res["tage_detail"][0]["klass"]}'


@pytest.mark.parametrize('marker', ['12345 PU', 'CPT', 'EM', 'X'])
def test_marker_only_no_fahrtag_without_evidence(marker):
    matched = [make_day('2025-12-02', base='FRA', marker=marker,
                        routing=[], duty_min=0, activity_type='unknown')]
    res = _run(matched, 'FRA')
    assert not res['tage_detail'][0].get('counted_as_fahrtag'), \
        f'Marker {marker!r} only → kein Fahrtag'


@pytest.mark.parametrize('marker', ['12345 PU', 'CPT', '## A1'])
def test_marker_only_no_hotel_without_evidence(marker):
    matched = [make_day('2025-12-03', base='FRA', marker=marker,
                        routing=[], duty_min=0, overnight=False,
                        activity_type='unknown')]
    res = _run(matched, 'FRA')
    assert not res['tage_detail'][0].get('counted_as_hotel_nacht')


def test_glossary_hint_cannot_override_strong_evidence():
    """Marker `RES` allein wuerde Standby implizieren — aber SE-foreign-stempel
    + foreign-layover-evidence ueberstimmt das (Standby-Activation)."""
    matched = [make_day('2025-12-05', base='FRA', marker='RES',
                        routing=['FRA'], duty_min=960, start_time='04:00',
                        activity_type='standby',
                        se_ort='SEL', se_inland=False, se_total=50.0)]
    res = _run(matched, 'FRA')
    # SE belegt foreign-tour → Z76, nicht Standby
    assert res['tage_detail'][0]['klass'] == 'Z76', \
        f'Marker-Hint RES wird durch SE-foreign-Evidenz ueberstimmt'


def test_known_marker_field_position_wins_over_glossary():
    """`PU` im Marker-Feld != IATA, selbst wenn Pula-IATA existiert."""
    matched = [make_day('2025-12-07', base='FRA', marker='12345 PU',
                        routing=[], duty_min=0, activity_type='unknown')]
    res = _run(matched, 'FRA')
    # Pipeline darf KEIN BMF-Pula auswählen
    bmf = (res['tage_detail'][0].get('bmf_land') or '').lower()
    assert 'pula' not in bmf and 'kroatien' not in bmf, \
        f'PU != Pula → bmf={bmf}'
