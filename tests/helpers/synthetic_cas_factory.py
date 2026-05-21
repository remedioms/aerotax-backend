"""Synthetic CAS Factory — generiert CAS-Tag-Daten ohne PII fuer Test-Validation.

Pro Tag:
  - datum
  - base (Homebase IATA)
  - role: cabin | cockpit | unknown
  - airline_style: lh | generic | unknown
  - marker_style: lh_cabin | lh_cockpit | generic | unknown_symbol | no_marker
  - dp: structured tag-facts (routing/layover/overnight/duty/etc.)
  - se: optional SE-stempel
  - expected_classification: dict {klass, fahrtag, hotel, role, standby_context}

Diese Factory baut den `matched_days`-Input fuer
`_normalize_tours_from_raw_facts(matched_days, homebase=..., year=...)`.

KEINE PII. Verwendet Generic-Namen, kein Tibor-Daten.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any


def make_day(
    datum: str,
    base: str = 'FRA',
    role: str = 'cabin',
    airline_style: str = 'lh',
    marker_style: str = 'lh_cabin',
    *,
    marker: str = '',
    routing: list | None = None,
    layover_ort: str = '',
    overnight: bool = False,
    duty_min: int = 0,
    start_time: str = '',
    end_time: str = '',
    has_fl: bool = False,
    starts_hb: bool = False,
    ends_hb: bool = False,
    activity_type: str = 'frei',
    se_ort: str = '',
    se_inland: bool | None = None,
    se_total: float = 0.0,
    expected_klass: str = '',
    expected_role: str = '',
    expected_fahrtag: bool = False,
    expected_hotel: bool = False,
    expected_standby_context: str = '',
    note: str = '',
) -> dict:
    """Baut einen synthetischen matched_day."""
    se_count = 1 if (se_ort or se_total > 0 or se_inland is not None) else 0
    se = {
        'count': se_count,
        'stfrei_ort': se_ort or '',
        'stfrei_inland': se_inland,
        'stfrei_total': float(se_total),
        'zwoelftel': 1 if se_count else 0,
        'lines': [],
    }
    dp = {
        'datum': datum,
        'activity_type': activity_type,
        'routing': list(routing or []),
        'layover_ort': layover_ort,
        'overnight_after_day': bool(overnight),
        'start_time': start_time,
        'end_time': end_time,
        'duty_duration_minutes': int(duty_min),
        'raw_marker': marker,
        'has_fl': has_fl,
        'is_workday': activity_type in ('tour', 'same_day', 'office', 'training', 'standby'),
        'requires_commute': starts_hb,
        'starts_at_homebase': bool(starts_hb),
        'ends_at_homebase': bool(ends_hb),
        'raw_lines': [],
        'confidence': 0.9,
    }
    return {
        'datum': datum,
        'dp': dp,
        'se': se,
        '_synth_meta': {
            'base': base,
            'role': role,
            'airline_style': airline_style,
            'marker_style': marker_style,
            'expected_klass': expected_klass,
            'expected_role': expected_role,
            'expected_fahrtag': expected_fahrtag,
            'expected_hotel': expected_hotel,
            'expected_standby_context': expected_standby_context,
            'note': note,
        },
    }


def tour_3day_foreign(start_date: str, base: str, foreign_iata: str,
                      marker_prefix: str = '12345', role: str = 'cabin') -> list:
    """3-Tage-Foreign-Tour: base→foreign overnight→foreign→base.

    Day 1: tour_start. Day 2: tour_mid. Day 3: tour_end same_day.
    """
    y, m, d = [int(x) for x in start_date.split('-')]
    d0 = date(y, m, d)
    marker_role = 'PU' if role == 'cabin' else 'CPT' if role == 'cockpit' else 'A1'
    return [
        make_day((d0 + timedelta(days=0)).isoformat(), base=base, role=role,
                 marker=f'{marker_prefix} {marker_role}',
                 routing=[base, foreign_iata], layover_ort=foreign_iata,
                 overnight=True, duty_min=600, start_time='06:00',
                 starts_hb=True, has_fl=True, activity_type='tour',
                 expected_klass='Z76', expected_role='tour_start',
                 expected_fahrtag=True, note=f'{base}→{foreign_iata} foreign tour-start'),
        make_day((d0 + timedelta(days=1)).isoformat(), base=base, role=role,
                 marker=f'{marker_prefix} {marker_role} (Day 2)',
                 routing=[foreign_iata], layover_ort=foreign_iata,
                 overnight=True, duty_min=0,
                 activity_type='tour',
                 expected_klass='Z76', expected_role='tour_mid',
                 expected_hotel=True, note='foreign layover-off-day'),
        make_day((d0 + timedelta(days=2)).isoformat(), base=base, role=role,
                 marker=f'{marker_prefix} {marker_role}',
                 routing=[foreign_iata, base], layover_ort='',
                 overnight=False, duty_min=500, end_time='17:00',
                 ends_hb=True, has_fl=True, activity_type='tour',
                 expected_klass='Z76', expected_role='tour_end',
                 note='foreign tour-end same-day return'),
    ]


def standby_inland(datum: str, base: str, role: str = 'cabin') -> dict:
    """Standby zuhause ohne SE-Aktivierung."""
    marker_role = 'RES' if role == 'cabin' else 'RSV' if role == 'cockpit' else 'R'
    return make_day(datum, base=base, role=role,
                    marker=marker_role,
                    routing=[base], layover_ort='',
                    overnight=False, duty_min=450, start_time='08:00',
                    activity_type='standby',
                    expected_klass='Standby',
                    expected_standby_context='homebase_idle',
                    note='Standby zuhause ohne Aktivierung')


def standby_activated_foreign(datum: str, base: str, foreign_iata: str,
                              role: str = 'cabin') -> dict:
    """Standby-Activation foreign — SE-stempel belegt."""
    marker_role = 'RES' if role == 'cabin' else 'RSV' if role == 'cockpit' else 'R'
    return make_day(datum, base=base, role=role,
                    marker=marker_role,
                    routing=[base], layover_ort='',
                    overnight=False, duty_min=960, start_time='04:00',
                    activity_type='standby',
                    se_ort=foreign_iata, se_inland=False, se_total=50.0,
                    expected_klass='Z76',
                    expected_role='tour_mid',
                    expected_standby_context='airport_standby_after_return',
                    note=f'RES + SE foreign ({foreign_iata}) — activated')


def training_inland(datum: str, base: str, marker: str = 'EM',
                    role: str = 'cabin', duty_min: int = 240) -> dict:
    """Inland-Training mit Anfahrt zur Homebase."""
    return make_day(datum, base=base, role=role,
                    marker=marker,
                    routing=[base], layover_ort='',
                    overnight=False, duty_min=duty_min, start_time='07:30',
                    activity_type='training',
                    expected_klass='Office',
                    expected_fahrtag=True,
                    note=f'Training-Marker {marker} mit Anfahrt')


def inland_same_day_tour(datum: str, base: str, inland_dest: str,
                        role: str = 'cabin', duty_min: int = 500) -> dict:
    """Inland-Same-Day-Tour ≥8h."""
    marker_role = 'PU' if role == 'cabin' else 'CPT' if role == 'cockpit' else 'A1'
    return make_day(datum, base=base, role=role,
                    marker=f'99999 {marker_role}',
                    routing=[base, inland_dest, base], layover_ort='',
                    overnight=False, duty_min=duty_min, start_time='05:30',
                    starts_hb=True, ends_hb=True, has_fl=True,
                    activity_type='same_day',
                    expected_klass='Z72',
                    expected_role='same_day',
                    expected_fahrtag=True,
                    note=f'{base}↔{inland_dest} inland same-day ≥8h')


def office_homebase_passive(datum: str, base: str,
                            marker: str = 'ORTSTAG', role: str = 'cabin') -> dict:
    """Passive Homebase ohne echte Dienstreise."""
    return make_day(datum, base=base, role=role,
                    marker=marker,
                    routing=[base], layover_ort='',
                    overnight=False, duty_min=0,
                    activity_type='office',
                    expected_klass='Office',
                    note=f'Passive Office-Marker {marker}')


def frei_day(datum: str, base: str, marker: str = '==',
            role: str = 'cabin') -> dict:
    """Freier Tag, kein Tour-Kontext."""
    return make_day(datum, base=base, role=role,
                    marker=marker,
                    routing=[], layover_ort='',
                    overnight=False, duty_min=0,
                    activity_type='frei',
                    expected_klass='Frei',
                    note=f'Frei-Tag mit Marker {marker}')


def unknown_marker_with_evidence(datum: str, base: str, foreign_iata: str,
                                  marker: str = '##') -> dict:
    """Unknown marker, but clear tour-evidence (routing/time/duty)."""
    return make_day(datum, base=base, role='unknown',
                    marker=marker, airline_style='unknown', marker_style='unknown_symbol',
                    routing=[base, foreign_iata], layover_ort=foreign_iata,
                    overnight=True, duty_min=600, start_time='06:00',
                    starts_hb=True, has_fl=True, activity_type='tour',
                    expected_klass='Z76',
                    expected_role='tour_start',
                    expected_fahrtag=True,
                    note=f'Unknown marker {marker} but clear tour-evidence')


def unknown_marker_no_evidence(datum: str, base: str, marker: str = '##') -> dict:
    """Unknown marker without any tour-evidence — needs_context_resolution."""
    return make_day(datum, base=base, role='unknown',
                    marker=marker, airline_style='unknown', marker_style='unknown_symbol',
                    routing=[], layover_ort='',
                    overnight=False, duty_min=0,
                    activity_type='unknown',
                    expected_klass='Frei',
                    note=f'Unknown marker {marker} ohne Evidenz')


# ════════════════════════════════════════════════════════════════════════════
# Pre-built Scenario Library (60+ Szenarien)
# ════════════════════════════════════════════════════════════════════════════

def scenario_fra_cabin_bangalore_tour():
    return tour_3day_foreign('2025-03-10', 'FRA', 'BLR', '12345', 'cabin')

def scenario_muc_cabin_tlv_tour():
    return tour_3day_foreign('2025-04-15', 'MUC', 'TLV', '23456', 'cabin')

def scenario_dus_cabin_jfk_tour():
    return tour_3day_foreign('2025-05-20', 'DUS', 'JFK', '34567', 'cabin')

def scenario_ber_cockpit_cdg_tour():
    return tour_3day_foreign('2025-06-10', 'BER', 'CDG', '45678', 'cockpit')

def scenario_ham_cabin_lhr_tour():
    return tour_3day_foreign('2025-07-05', 'HAM', 'LHR', '56789', 'cabin')

def scenario_cgn_cockpit_ams_tour():
    return tour_3day_foreign('2025-08-12', 'CGN', 'AMS', '67890', 'cockpit')

def scenario_str_cabin_ist_tour():
    return tour_3day_foreign('2025-09-15', 'STR', 'IST', '78901', 'cabin')

def scenario_vie_cockpit_jfk_tour():
    return tour_3day_foreign('2025-10-10', 'VIE', 'JFK', '89012', 'cockpit')

def scenario_zrh_cabin_dxb_tour():
    return tour_3day_foreign('2025-11-05', 'ZRH', 'DXB', '90123', 'cabin')

def scenario_other_base_unknown_airline():
    """Custom base ABC with unknown airline markers."""
    return [
        make_day('2025-12-01', base='ABC', role='unknown',
                 airline_style='unknown', marker_style='unknown_symbol',
                 marker='Z9 ##', routing=['ABC', 'XYZ'], layover_ort='XYZ',
                 overnight=True, duty_min=550, start_time='07:00',
                 starts_hb=True, has_fl=True, activity_type='tour',
                 expected_klass='Z76' if False else '',  # unknown country
                 expected_role='tour_start',
                 note='Unknown base ABC + unknown airline'),
    ]


def scenario_marker_only_no_routing(base='FRA'):
    """Test: Marker ohne Routing/Time → kein Z76."""
    return [
        make_day('2025-01-15', base=base,
                 marker='12345 PU', routing=[], layover_ort='',
                 overnight=False, duty_min=0, activity_type='frei',
                 expected_klass='Frei',
                 note='Marker-only ohne Evidenz → Frei'),
    ]


def scenario_routing_only_no_marker(base='FRA', foreign='LHR'):
    """Test: Routing ohne Marker → Tour-Continuity via Routing."""
    return [
        make_day('2025-02-10', base=base,
                 marker='', routing=[base, foreign], layover_ort=foreign,
                 overnight=True, duty_min=400, start_time='06:00',
                 starts_hb=True, has_fl=True, activity_type='tour',
                 expected_klass='Z76',
                 expected_role='tour_start',
                 note='Routing-only ohne Marker → Tour erkannt'),
    ]


def scenario_x_inside_real_tour():
    """X-Marker als Layover-OFF-Day innerhalb echter Tour."""
    return [
        make_day('2025-02-01', base='FRA',
                 marker='12345 PU', routing=['FRA', 'HKG'], layover_ort='HKG',
                 overnight=True, duty_min=700, start_time='10:00',
                 starts_hb=True, has_fl=True, activity_type='tour',
                 expected_klass='Z76', expected_role='tour_start'),
        make_day('2025-02-02', base='FRA',
                 marker='X HKG', routing=['HKG'], layover_ort='HKG',
                 overnight=True, duty_min=0, activity_type='frei',
                 expected_klass='Z76', expected_role='tour_mid',
                 expected_hotel=True,
                 note='X-Layover-OFF inside tour'),
        make_day('2025-02-03', base='FRA',
                 marker='12345 PU', routing=['HKG', 'FRA'], layover_ort='',
                 overnight=False, duty_min=600, ends_hb=True, has_fl=True,
                 activity_type='tour',
                 expected_klass='Z76', expected_role='tour_end'),
    ]


def scenario_x_outside_tour():
    """X-Marker ohne Tour-Kontext → Frei."""
    return [
        make_day('2025-03-15', base='FRA',
                 marker='X', routing=[], layover_ort='',
                 overnight=False, duty_min=0, activity_type='frei',
                 expected_klass='Frei',
                 note='X-Marker ohne Tour-Kontext'),
    ]


def scenario_res_homebase_no_se():
    """RES zuhause ohne SE → standby_homebase."""
    return [
        standby_inland('2025-04-10', 'FRA', 'cabin'),
    ]


def scenario_res_foreign_hotel():
    """RES nach foreign-overnight = foreign_hotel_standby."""
    return [
        make_day('2025-04-20', base='FRA',
                 marker='12345 PU', routing=['FRA', 'JFK'], layover_ort='JFK',
                 overnight=True, duty_min=720, start_time='11:00',
                 starts_hb=True, has_fl=True, activity_type='tour',
                 expected_klass='Z76', expected_role='tour_start'),
        make_day('2025-04-21', base='FRA',
                 marker='RES', routing=[], layover_ort='JFK',
                 overnight=True, duty_min=0, activity_type='standby',
                 expected_klass='Z76', expected_role='tour_mid',
                 expected_hotel=True, expected_standby_context='foreign_hotel_standby',
                 note='RES im foreign Hotel'),
    ]


def scenario_sb_m_after_return():
    """SB_M am Tag nach Rueckkehr — airport_standby_after_return."""
    return [
        make_day('2025-05-01', base='FRA',
                 marker='12345 PU', routing=['LHR', 'FRA'], layover_ort='',
                 overnight=False, duty_min=500, ends_hb=True, has_fl=True,
                 activity_type='tour',
                 expected_klass='Z76', expected_role='tour_end'),
        make_day('2025-05-02', base='FRA',
                 marker='SB_M', routing=['FRA'], layover_ort='',
                 overnight=False, duty_min=450, start_time='08:00',
                 activity_type='standby',
                 expected_klass='Standby',
                 expected_standby_context='homebase_idle',
                 note='SB_M am Tag nach Rueckkehr'),
    ]


def scenario_training_em_em_em():
    """3× EM-Training mit Anfahrt zur Homebase."""
    return [
        training_inland('2025-06-10', 'FRA', 'EM', 'cabin', 210),
        training_inland('2025-06-11', 'FRA', 'EH 4 SECCRM 4', 'cabin', 240),
        training_inland('2025-06-12', 'MUC', 'TK', 'cockpit', 240),
    ]


def scenario_inland_same_day_z72():
    """Inland-Same-Day >=8h Z72."""
    return [
        inland_same_day_tour('2025-07-15', 'FRA', 'MUC', 'cabin', 510),
        inland_same_day_tour('2025-07-16', 'MUC', 'BER', 'cockpit', 540),
        inland_same_day_tour('2025-07-17', 'DUS', 'HAM', 'cabin', 480),
    ]


def scenario_inland_short_lt_8h_no_z72():
    """Inland-Same-Day <8h → kein Z72."""
    return [
        make_day('2025-08-10', base='FRA',
                 marker='99999 PU', routing=['FRA', 'CGN', 'FRA'],
                 overnight=False, duty_min=400, start_time='09:00',
                 starts_hb=True, ends_hb=True, has_fl=True,
                 activity_type='same_day',
                 expected_klass='Frei',  # <8h kein Z72
                 note='Inland <8h kein Z72'),
    ]


def scenario_multi_stop_via_homebase():
    """Multi-Stop via Homebase aber endet NICHT zuhause."""
    return [
        make_day('2025-09-01', base='FRA',
                 marker='12345 PU', routing=['MUC', 'FRA', 'LHR'], layover_ort='LHR',
                 overnight=True, duty_min=700, start_time='08:00',
                 starts_hb=False, has_fl=True, activity_type='tour',
                 expected_klass='Z76', expected_role='tour_start',
                 note='Multi-Stop MUC→FRA→LHR with base=FRA: FRA ist Transit'),
    ]


def scenario_ntf_overrides_pub():
    """NTF-Update ueberschreibt PUB."""
    return [
        make_day('2025-10-05', base='FRA',
                 marker='12345 PU', routing=['FRA', 'CDG'], layover_ort='CDG',
                 overnight=True, duty_min=400, start_time='07:00',
                 starts_hb=True, has_fl=True, activity_type='tour',
                 expected_klass='Z76', expected_role='tour_start',
                 note='Tag aus PUB + NTF-Override muss letzten Wert nehmen'),
    ]


def scenario_phantom_isolated_equals():
    """`==` ohne Tour-Kontext — Phantom-Removal-Test."""
    return [
        frei_day('2025-11-01', 'FRA', '=='),
        frei_day('2025-11-02', 'MUC', 'OFF'),
        frei_day('2025-11-03', 'DUS', ''),
    ]


def scenario_accidental_flight_hours_in_cas_slot():
    """User-Fehler: Flugstundenuebersicht in CAS-Slot — muss refused werden."""
    # Tested via classify_uploaded_pdf_doc_type, not matched_days
    return None  # Filed under fuzz tests


# ════════════════════════════════════════════════════════════════════════════
# All scenarios library
# ════════════════════════════════════════════════════════════════════════════

ALL_SCENARIOS = [
    # Bases × foreign-tour (9 base scenarios = 9×3 = 27 days)
    ('fra_cabin_blr', scenario_fra_cabin_bangalore_tour, 'FRA'),
    ('muc_cabin_tlv', scenario_muc_cabin_tlv_tour, 'MUC'),
    ('dus_cabin_jfk', scenario_dus_cabin_jfk_tour, 'DUS'),
    ('ber_cockpit_cdg', scenario_ber_cockpit_cdg_tour, 'BER'),
    ('ham_cabin_lhr', scenario_ham_cabin_lhr_tour, 'HAM'),
    ('cgn_cockpit_ams', scenario_cgn_cockpit_ams_tour, 'CGN'),
    ('str_cabin_ist', scenario_str_cabin_ist_tour, 'STR'),
    ('vie_cockpit_jfk', scenario_vie_cockpit_jfk_tour, 'VIE'),
    ('zrh_cabin_dxb', scenario_zrh_cabin_dxb_tour, 'ZRH'),
    # Other base + unknown airline (1 day)
    ('other_base_unknown', scenario_other_base_unknown_airline, 'ABC'),
    # Marker semantics (10 scenarios)
    ('marker_only_no_routing', lambda: scenario_marker_only_no_routing('FRA'), 'FRA'),
    ('routing_only_no_marker', lambda: scenario_routing_only_no_marker('FRA', 'LHR'), 'FRA'),
    ('x_inside_real_tour', scenario_x_inside_real_tour, 'FRA'),
    ('x_outside_tour', scenario_x_outside_tour, 'FRA'),
    ('res_homebase_no_se', scenario_res_homebase_no_se, 'FRA'),
    ('res_foreign_hotel', scenario_res_foreign_hotel, 'FRA'),
    ('sb_m_after_return', scenario_sb_m_after_return, 'FRA'),
    ('training_em_em_em', scenario_training_em_em_em, 'FRA'),  # mixed bases inside
    ('inland_same_day_z72', scenario_inland_same_day_z72, 'FRA'),  # mixed
    ('inland_short_no_z72', scenario_inland_short_lt_8h_no_z72, 'FRA'),
    # Multi-stop / phantom
    ('multi_stop_via_homebase', scenario_multi_stop_via_homebase, 'FRA'),
    ('ntf_overrides_pub', scenario_ntf_overrides_pub, 'FRA'),
    ('phantom_isolated_equals', scenario_phantom_isolated_equals, 'FRA'),  # 3 days mixed bases
]


def get_all_scenarios():
    """Returns list of (name, days_list, base) for all scenarios."""
    out = []
    for name, factory, base in ALL_SCENARIOS:
        days = factory()
        if days is None:
            continue
        out.append((name, days, base))
    return out


def count_scenarios_and_days():
    out = get_all_scenarios()
    total_days = sum(len(d) for _, d, _ in out)
    return {'scenarios': len(out), 'total_days': total_days}
