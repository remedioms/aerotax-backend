"""Daily Briefing (Florians Spez) — jede Regel des README gegen ECHTE
(anonymisierte) PROD-Payloads vom 27.07.2026 (Umlauf 183706 FRA, A320):
tests/fixtures/daily_briefing_*.json. Namen/PKs sind deterministisch
pseudonymisiert, Flugnummern/Stationen/Zeiten/Attribute sind original.
Kein Live-Call."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from blueprints import daily_briefing as db

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')


def _fx(name):
    with open(os.path.join(FIX, f'daily_briefing_{name}.json')) as f:
        return json.load(f)


ROT = _fx('rotation')
DE = _fx('duty_events')
CL_075 = _fx('crewlist_LH075')
CL_278 = _fx('crewlist_LH278')
LD_075 = _fx('legdetails_LH075')
LD_278 = _fx('legdetails_LH278')

DATE = '2026-07-27'

# Verzeichnis-Zeilen wie live aus crew_hotel_directory (LUFTHANSA, 27.07.).
DIRECTORY = [
    {'iata': 'DUS', 'base': None, 'hotel': 'NH City Nord', 'transfer_min': 20, 'votes': 1},
    {'iata': 'DUS', 'base': None, 'hotel': 'Clayton Hotel Düsseldorf (ehem. Nikko)', 'transfer_min': 30, 'votes': 1},
    {'iata': 'BUD', 'base': None, 'hotel': 'Hilton Garden Inn Budapest City Centre', 'transfer_min': 0, 'votes': 1},
    {'iata': 'VCE', 'base': None, 'hotel': 'Leonardo Royal Hotel Venice Mestre', 'transfer_min': 15, 'votes': 1},
    {'iata': 'ZAG', 'base': None, 'hotel': 'Sheraton Zagreb', 'transfer_min': 30, 'votes': 1},
    {'iata': 'BCN', 'base': None, 'hotel': 'Hotel A', 'transfer_min': 20, 'votes': 1},
    {'iata': 'BCN', 'base': None, 'hotel': 'Hotel B', 'transfer_min': 25, 'votes': 1},
]


def _details_for(leg):
    if leg['flight'] == 'LH075':
        return LD_075
    if leg['flight'] == 'LH278':
        return LD_278
    return None


# ── Rotations-Normalisierung ────────────────────────────────────────────────
def test_rotation_shifts_parses_real_payload():
    shifts = db.rotation_shifts(ROT)
    assert len(shifts) == 5
    assert all(s['rotation'] == '183706' for s in shifts)
    s2 = db.shift_for_date(shifts, DATE)
    assert s2 is not None and s2['shift_no'] == 2
    legs = db.day_legs(s2, DATE)
    assert [l['flight'] for l in legs] == ['LH075', 'LH278', 'LH279', 'LH1342']
    # hotelName am HINFLUG, pickupTime am RÜCKFLUG (nie am selben Leg)
    assert legs[-1]['hotel_name'] == 'IntercityHotel Budapest'
    assert legs[0]['pickup_utc'] == '2026-07-27T11:50:00Z'


def test_day_legs_keeps_post_midnight_legs_of_same_shift():
    shifts = db.rotation_shifts(ROT)
    sh = db.shift_for_date(shifts, DATE)
    # synthetisches Red-Eye-Leg nach Mitternacht in derselben Schicht
    sh2 = dict(sh)
    sh2['legs'] = sh['legs'] + [{**sh['legs'][-1],
                                 'flight': 'LH9999', 'dep': 'BUD', 'arr': 'FRA',
                                 'dep_iso': '2026-07-28T00:30:00Z',
                                 'arr_iso': '2026-07-28T02:00:00Z'}]
    legs = db.day_legs(sh2, DATE)
    assert legs[-1]['flight'] == 'LH9999'   # gehört zum Duty-Tag 27.


def test_next_shift_after_same_rotation():
    shifts = db.rotation_shifts(ROT)
    sh = db.shift_for_date(shifts, DATE)
    nxt = db.next_shift_after(shifts, sh)
    assert nxt['shift_no'] == 3
    assert nxt['legs'][0]['flight'] == 'LH1339'    # BUD-Rückflug am 28.


# ── A/C Changes ─────────────────────────────────────────────────────────────
def test_ac_change_flag_sits_on_previous_leg():
    """Live bewiesen: aircraftChanged am Leg i = Wechsel NACH Leg i.
    LH075 (True, DAIZJ) → LH278 (DAILF): Eintrag für LH278."""
    shifts = db.rotation_shifts(ROT)
    legs = db.day_legs(db.shift_for_date(shifts, DATE), DATE)
    out = db.build_ac_changes(legs, _details_for)
    assert len(out) == 1
    e = out[0]
    assert e['flight'] == 'LH278'
    assert e['reg_short'] == '-ILF'
    assert e['arr_position'] == 'A21' and e['dep_position'] == 'D05'
    assert e['gap_min'] == 80
    assert e['line'] == 'A/C Change | LH278: FRA - LIN | -ILF: A21 ➜ D05 in 1:20'


def test_ac_change_dropped_without_positions():
    """Florians Fallback: fehlen die Leg-Details, wird der Eintrag weggelassen."""
    shifts = db.rotation_shifts(ROT)
    legs = db.day_legs(db.shift_for_date(shifts, DATE), DATE)
    assert db.build_ac_changes(legs, lambda leg: None) == []
    # nur eine Seite fehlt → ebenfalls weglassen
    assert db.build_ac_changes(
        legs, lambda leg: LD_075 if leg['flight'] == 'LH075' else None) == []


def test_ac_change_not_invented_without_flag():
    shifts = db.rotation_shifts(ROT)
    legs = [dict(l, ac_changed=False)
            for l in db.day_legs(db.shift_for_date(shifts, DATE), DATE)]
    assert db.build_ac_changes(legs, _details_for) == []


# ── Long Transits ───────────────────────────────────────────────────────────
def test_long_transit_at_exactly_80_minutes_counts():
    """LH075→LH278: 14:00Z→15:20Z = exakt 80 min → Long Transit (>= 80)."""
    shifts = db.rotation_shifts(ROT)
    legs = db.day_legs(db.shift_for_date(shifts, DATE), DATE)
    out = db.build_long_transits(legs, _details_for)
    assert len(out) == 1
    e = out[0]
    assert e['flight'] == 'LH278' and e['duration_min'] == 80
    # 14:00Z in FRA = 16:00 Ortszeit (Sommer)
    assert e['line'] == 'Long Transit | LH278: FRA - LIN | at 16:00 for 1:20 @ D05'


def test_long_transit_below_threshold_not_shown():
    shifts = db.rotation_shifts(ROT)
    legs = db.day_legs(db.shift_for_date(shifts, DATE), DATE)
    # Übergang LIN→FRA (75 min) und FRA→BUD-Vorlauf (75 min) sind < 80
    flights = [e['flight'] for e in db.build_long_transits(legs, _details_for)]
    assert 'LH279' not in flights and 'LH1342' not in flights


def test_ac_change_and_long_transit_same_transition_both_appear():
    """Florian: A/C Change und Long Transit können für DENSELBEN Übergang
    gleichzeitig erscheinen (LH075→LH278: Flag + 80 min)."""
    shifts = db.rotation_shifts(ROT)
    legs = db.day_legs(db.shift_for_date(shifts, DATE), DATE)
    assert db.build_ac_changes(legs, _details_for)[0]['flight'] == 'LH278'
    assert db.build_long_transits(legs, _details_for)[0]['flight'] == 'LH278'


def test_long_transit_dropped_without_departure_position():
    shifts = db.rotation_shifts(ROT)
    legs = db.day_legs(db.shift_for_date(shifts, DATE), DATE)
    no_pos = dict(LD_278)
    no_pos['departurePosition'] = None
    out = db.build_long_transits(
        legs, lambda leg: no_pos if leg['flight'] == 'LH278' else _details_for(leg))
    assert out == []


# ── Crew-Listen-Normalisierung ──────────────────────────────────────────────
def test_norm_crewlist_real_shape():
    crew = db.norm_crewlist(CL_075)
    assert len(crew) == 7
    cp = crew[0]
    assert cp['pos'] == 'CP' and cp['pos_raw'] == 'CP/TC'   # 'CP/TC' → CP
    dh = [e for e in crew if e['duty'] == 'DH']
    assert len(dh) == 1 and dh[0]['to']['flight'] == 'LH118'
    # LH-Ein-Element-Skalar-Issue: crewMembers als EIN Objekt statt Liste
    solo = dict(CL_075)
    solo['crewMembers'] = CL_075['crewMembers'][0]
    assert len(db.norm_crewlist(solo)) == 1


def test_crew_sort_order_cp_fo_ac_p1_fb_ak_then_lastname():
    mk = lambda pos, last: {'pos': pos, 'last': last, 'first': 'X'}
    rows = [mk('FB', 'B'), mk('AK', 'A'), mk('CP', 'Z'), mk('P1', 'A'),
            mk('FB', 'A'), mk('FO', 'M'), mk('AC', 'K')]
    ordered = sorted(rows, key=db._crew_sort_key)
    assert [(r['pos'], r['last']) for r in ordered] == [
        ('CP', 'Z'), ('FO', 'M'), ('AC', 'K'), ('P1', 'A'),
        ('FB', 'A'), ('FB', 'B'), ('AK', 'A')]


# ── Crew Changes: Symbolik + Regeln ─────────────────────────────────────────
def _leg(flight='LH278', dep='FRA', arr='LIN', dep_iso='2026-07-27T15:20:00Z'):
    return {'flight': flight, 'dep': dep, 'arr': arr, 'dep_iso': dep_iso,
            'arr_iso': dep_iso}


def _member(pk, pos='FB', duty='OD', ex=None, to=None, last='MUSTER', first='ANNA'):
    return {'pk': pk, 'pos': pos, 'pos_raw': pos, 'last': last, 'first': first,
            'duty': duty, 'ex': ex, 'to': to}


_NO_ROUTE = lambda f, d: None


def test_crew_change_real_transition_outgoing_at_homebase():
    """Echter Übergang LH075→LH278: 5 bleiben, 2 gehen (1× OD ohne Weiterflug
    → ⚐, 1× DH mit Weiterflug LH118 am selben Tag → ✈︎ LH118). Kein Incoming."""
    prev = db.norm_crewlist(CL_075)
    nxt = db.norm_crewlist(CL_278)
    blk = db.crew_change_block(prev, nxt, _leg(), DATE, _NO_ROUTE)
    assert blk is not None
    assert blk['ref'] == 'LH278: FRA - LIN'
    assert blk['incoming'] == []
    assert len(blk['outgoing']) == 2
    syms = {e['symbol'] for e in blk['outgoing']}
    assert syms == {db.SYM_BASE_END, db.SYM_CONN}
    dh = [e for e in blk['outgoing'] if '(DH)' in e['line']][0]
    assert '✈︎ LH118' in dh['line']       # Ort nicht auflösbar → Flugnummer-Fallback


def test_crew_change_homebase_route_resolution_adds_ort():
    prev = db.norm_crewlist(CL_075)
    nxt = db.norm_crewlist(CL_278)
    blk = db.crew_change_block(prev, nxt, _leg(), DATE,
                               lambda f, d: ('FRA', 'MUC') if f == 'LH118' else None)
    dh = [e for e in blk['outgoing'] if '(DH)' in e['line']][0]
    assert '✈︎ LH118 - MUC' in dh['line']   # Homebase-Outgoing: Ankunftsort


def test_crew_change_incoming_symbols_all_four_cases():
    a = [_member('P1')]
    # Homebase + Vorflug am selben Tag → ✈︎ Flugnummer - Abflugort
    b = [_member('P1'), _member('P2', ex={'flight': 'LH074', 'date': DATE})]
    blk = db.crew_change_block(a, b, _leg(), DATE, lambda f, d: ('DUS', 'FRA'))
    assert blk['incoming'][0]['line'].endswith('✈︎ LH074 - DUS')
    # Homebase ohne Vorflug → ◎
    b2 = [_member('P1'), _member('P3')]
    blk = db.crew_change_block(a, b2, _leg(), DATE, _NO_ROUTE)
    assert blk['incoming'][0]['symbol'] == db.SYM_BASE_START
    # Außenstation + Vorflug → nur ✈︎ (ohne Flugnummer)
    out_leg = _leg(dep='LIN', arr='FRA', flight='LH279',
                   dep_iso='2026-07-27T17:30:00Z')
    b3 = [_member('P1'), _member('P4', ex={'flight': 'LH9', 'date': DATE})]
    blk = db.crew_change_block(a, b3, out_leg, DATE, _NO_ROUTE)
    assert blk['incoming'][0]['line'].endswith(db.SYM_CONN)
    # Außenstation ohne Vorflug → ⌂
    b4 = [_member('P1'), _member('P5')]
    blk = db.crew_change_block(a, b4, out_leg, DATE, _NO_ROUTE)
    assert blk['incoming'][0]['symbol'] == db.SYM_HOTEL


def test_crew_change_next_day_connection_does_not_count_as_same_day():
    """Florian: entscheidend ist derselbe KALENDERTAG — Weiterflug am Folgetag
    zählt nicht und führt an der Außenstation zu ⌂."""
    out_leg = _leg(dep='LIN', arr='FRA', flight='LH279',
                   dep_iso='2026-07-27T17:30:00Z')
    a = [_member('P1'), _member('P6', to={'flight': 'LH9', 'date': '2026-07-28'})]
    b = [_member('P1')]
    blk = db.crew_change_block(a, b, out_leg, DATE, _NO_ROUTE)
    assert blk['outgoing'][0]['symbol'] == db.SYM_HOTEL


def test_crew_change_status_change_appears_on_both_sides():
    a = [_member('P1', duty='DH', to={'flight': 'LH279', 'date': DATE})]
    b = [_member('P1', duty='OD', ex={'flight': 'LH075', 'date': DATE})]
    blk = db.crew_change_block(a, b, _leg(), DATE, _NO_ROUTE)
    assert len(blk['outgoing']) == 1 and len(blk['incoming']) == 1
    assert '(DH ➜ OD)' in blk['outgoing'][0]['line']
    assert '(DH ➜ OD)' in blk['incoming'][0]['line']


def test_crew_change_status_and_person_change_same_transition():
    a = [_member('P1', duty='DH'), _member('P2', last='WOLF')]
    b = [_member('P1', duty='OD'), _member('P3', last='KLEIN')]
    blk = db.crew_change_block(a, b, _leg(), DATE, _NO_ROUTE)
    assert len(blk['outgoing']) == 2      # Statuswechsel + echter Abgang
    assert len(blk['incoming']) == 2      # Statuswechsel + echter Zugang


def test_crew_change_return_label_when_next_leg_other_day():
    """Return-Block über den Layover hinweg: LH1342 (27.) → LH1339 (28.)."""
    ret_leg = _leg(flight='LH1339', dep='BUD', arr='FRA',
                   dep_iso='2026-07-28T12:25:00Z')
    a = [_member('P1'), _member('P2', last='WOLF')]
    b = [_member('P1')]
    blk = db.crew_change_block(a, b, ret_leg, DATE, _NO_ROUTE,
                               from_next_shift=True)
    assert blk['ref'] == 'Return @ 28JUL'
    assert blk['is_return'] is True
    # Außenstation BUD, kein Weiterflug am selben Tag → ⌂
    assert blk['outgoing'][0]['symbol'] == db.SYM_HOTEL


def test_crew_change_none_when_identical():
    a = [_member('P1'), _member('P2')]
    assert db.crew_change_block(a, list(a), _leg(), DATE, _NO_ROUTE) is None


def test_crew_change_missing_list_yields_none():
    assert db.crew_change_block(None, [_member('P1')], _leg(), DATE, _NO_ROUTE) is None
    assert db.crew_change_block([_member('P1')], None, _leg(), DATE, _NO_ROUTE) is None


# ── FDZ-/RZ-Toleranz ────────────────────────────────────────────────────────
def test_tolerances_real_shift2_attributes():
    """Echte Schicht 2 (27.07.): MTV 840−600=4:00, EASA 690−570=2:00;
    RZ MTV 795−600=3:15; RZ EASA ehrlich None (kein LAW_RZ_ACTUAL, keine
    belegte EASA-Debrief-Annahme)."""
    shifts = db.rotation_shifts(ROT)
    sh = db.shift_for_date(shifts, DATE)
    tol = db._tolerances(sh['attributes'])
    assert tol['crew_category'] == 'CAB'
    assert tol['fdz']['mtv_min'] == 240 and tol['fdz']['easa_min'] == 120
    assert tol['fdz']['line'] == 'FDZ-Toleranz | MTV 4:00 / EASA 2:00'
    assert tol['rz']['mtv_min'] == 195
    assert tol['rz']['easa_min'] is None
    assert tol['rz']['easa_unavailable_reason'] == 'easa_debrief_assumption_unavailable'
    assert tol['rz']['line'] == 'RZ-Toleranz | MTV 3:15 / EASA n/a'


def test_tolerances_zero_values_mean_not_recorded():
    # DH-only-Schicht (Fixture-Doku-Beispiel): MTV_FDZ/MAX = 0 → None, LAW zählt
    attrs = {'CAB_MTV_RZ': 600, 'CAB_MTV_FDZ': 0, 'CAB_LAW_MAX': 1020,
             'CAB_LAW_RZ': 860, 'CAB_MTV_RZ_ACTUAL': 1005, 'CAB_MTV_MAX': 0,
             'CAB_LAW_FDZ': 700}
    tol = db._tolerances(attrs)
    assert tol['fdz']['mtv_min'] is None
    assert tol['fdz']['easa_min'] == 320
    assert tol['rz']['mtv_min'] == 405


def test_tolerances_cockpit_prefix_and_missing():
    assert db._tolerances({}) is None
    tol = db._tolerances({'COC_MTV_FDZ': 600, 'COC_MTV_MAX': 840,
                          'COC_LAW_FDZ': 570, 'COC_LAW_MAX': 690})
    assert tol['crew_category'] == 'COC'
    assert tol['fdz']['mtv_min'] == 240


def test_tolerances_easa_rz_activates_only_with_documented_assumption(monkeypatch):
    shifts = db.rotation_shifts(ROT)
    sh = db.shift_for_date(shifts, DATE)
    monkeypatch.setattr(db, '_EASA_DEBRIEF_MIN', 30)
    tol = db._tolerances(sh['attributes'])
    # Annahme == MTV-Debrief (30) → Ist-Ruhe identisch; gegen LAW_RZ 610
    assert tol['rz']['easa_min'] == 795 - 610
    assert tol['rz']['easa_unavailable_reason'] is None


# ── Transferzeit: Florians vier Regeln ──────────────────────────────────────
def test_transfer_rule1_exact_match_wins_at_multi_hotel_station():
    """DUS hat ZWEI Hotels — LHs Klarname trifft eindeutig das Clayton
    (Verzeichnis-Schreibweise mit '(ehem. Nikko)' weicht ab)."""
    m = db.transfer_match('DUS', 'Clayton Hotel Düsseldorf', DIRECTORY)
    assert m['transfer_min'] == 30 and m['marker'] == '' and m['reason'] == 'exact'


def test_transfer_rule1_spelling_variant_still_matches():
    """VCE: LH 'Leonardo Royal Venice Mestre' vs Verzeichnis
    'Leonardo Royal Hotel Venice Mestre' → nach Abzug des generischen 'Hotel'
    sind die Token-Mengen GLEICH (nicht bloss Teilmenge — die reicht bewusst
    nicht, siehe test_transfer_subset_name_is_not_a_match)."""
    m = db.transfer_match('VCE', 'Leonardo Royal Venice Mestre', DIRECTORY)
    assert m['transfer_min'] == 15 and m['reason'] == 'exact'


def test_transfer_rule2_no_entry_for_destination():
    m = db.transfer_match('NRT', 'Hilton Narita', DIRECTORY)
    assert m['transfer_min'] is None and m['reason'] == 'no_entry'


def test_transfer_rule3_single_hotel_name_mismatch_gets_star():
    """ZAG: kein LH-Name, genau EIN Verzeichnis-Hotel → Destinations-Zeit mit *."""
    m = db.transfer_match('ZAG', None, DIRECTORY)
    assert m['transfer_min'] == 30 and m['marker'] == '*'
    assert m['reason'] == 'destination_general'
    # dito mit abweichendem Namen
    m2 = db.transfer_match('ZAG', 'Westin Zagreb', DIRECTORY)
    assert m2['transfer_min'] == 30 and m2['marker'] == '*'


def test_transfer_rule4_multi_hotel_requires_unique_match():
    """BCN hat zwei Hotels: ohne eindeutigen Treffer N/A — nie raten."""
    m = db.transfer_match('BCN', 'Irgendein Neues Hotel', DIRECTORY)
    assert m['transfer_min'] is None and m['reason'] == 'ambiguous_multi_hotel'
    m2 = db.transfer_match('BCN', None, DIRECTORY)
    assert m2['transfer_min'] is None


def test_transfer_zero_minutes_is_no_time_recorded():
    """BUD: Crowdsource-Default transfer_min=0 heißt „keine Zeit hinterlegt"
    → N/A, nicht '0:00'. LHs 'IntercityHotel Budapest' trifft den BUD-Eintrag
    'Hilton Garden Inn …' NICHT — es ist der Regel-3-Rückfall, kein Treffer.
    (Die frühere Fassung dieses Tests war vacuous: ihr `or m['marker'] is None`
    war bei transfer_min=None immer wahr und konnte nie rot werden — genau
    dadurch blieb die Verschmelzung unten unbemerkt.)"""
    m = db.transfer_match('BUD', 'IntercityHotel Budapest', DIRECTORY)
    assert m['transfer_min'] is None
    assert m['matched'] is False
    assert m['reason'] == 'destination_general_no_time'
    # Ein ECHTER Namens-Treffer ohne hinterlegte Zeit ist der andere Fall:
    m2 = db.transfer_match('BUD', 'Hilton Garden Inn Budapest City Centre',
                           DIRECTORY)
    assert m2['matched'] is True and m2['reason'] == 'no_time_recorded'
    assert m2['transfer_min'] is None


def test_zero_minute_entry_never_merges_two_houses(sync_env):
    """BLOCKER-Regression (adversarialer Review 27.07.), scharf auf dem ECHTEN
    Prod-Payload: BUD trägt genau EINEN Verzeichnis-Eintrag ('Hilton Garden Inn
    Budapest City Centre') mit transfer_min=0 — dem Crowdsource-Default, den
    jede frisch angelegte Station hat. LH nennt das Haus 'IntercityHotel
    Budapest'.

    Vorher lieferte transfer_match dafür reason='no_time_recorded', was
    official_name_action wie einen Namens-Treffer las: LHs Klarname wurde als
    official_name auf die HILTON-Zeile geschrieben und danach airline-weit
    als deren Anzeigename ausgeliefert — zwei verschiedene Häuser verschmolzen,
    ausgelöst von einer Crowd-Null."""
    m = db.transfer_match('BUD', 'IntercityHotel Budapest', DIRECTORY)
    assert db.official_name_action('IntercityHotel Budapest', m) == ('report', True)
    fake = sync_env([])
    assert db._sync_official_name('AT-U', 'BUD', 'IntercityHotel Budapest',
                                  DIRECTORY) == 'reported'
    assert fake.ops == []          # kein Update, kein Insert


def test_parenthesis_content_can_be_the_distinguishing_feature():
    """BLOCKER-Regression: das Klammer-Strippen löschte genau das Token, das
    zwei Häuser unterscheidet. 'Mercure Hotel Frankfurt (Airport)' und
    '… (City)' galten als dasselbe Haus → richtiger Name, FALSCHE Fahrtzeit,
    ohne '*'-Markierung. Genau der Fehler, vor dem der Owner gewarnt hat."""
    d = [{'iata': 'FRA', 'hotel': 'Mercure Hotel Frankfurt (Airport)',
          'transfer_min': 15, 'votes': 1}]
    m = db.transfer_match('FRA', 'Mercure Hotel Frankfurt (City)', d)
    assert m['matched'] is False and m['reason'] == 'destination_general'
    assert m['marker'] == '*'          # sichtbar nur ein Richtwert
    assert db.official_name_action('Mercure Hotel Frankfurt (City)', m) == \
        ('report', True)
    # Trägt nur EINE Seite eine Klammer, ist sie eine Notiz — Fall 1 bleibt heil.
    assert db._same_house('Clayton Hotel Düsseldorf',
                          'Clayton Hotel Düsseldorf (ehem. Nikko)')
    # Gleicher Klammer-Inhalt auf beiden Seiten bleibt selbstverständlich Treffer.
    assert db._same_house('Mercure Frankfurt (Airport)',
                          'Mercure Hotel Frankfurt (Airport)')


def test_purely_generic_names_never_match_each_other():
    """Ein Name, der nur aus generischen Wörtern besteht, hat eine LEERE
    Token-Menge. Zwei leere Mengen sind gleich — ohne Riegel matchte 'The Hotel'
    auf jeden ebenso generischen Eintrag und erbte dessen Fahrtzeit."""
    assert db._hotel_tokens('The Hotel') == set()
    assert not db._same_house('The Hotel', 'Hotels')
    d = [{'iata': 'XXX', 'hotel': 'Hotels', 'transfer_min': 40, 'votes': 1}]
    m = db.transfer_match('XXX', 'The Hotel', d)
    assert m['matched'] is False and m['transfer_min'] == 40 and m['marker'] == '*'


# ── Hotel-Block + Pick-up am ENDE der Hotelphase ────────────────────────────
def _shift_and_legs():
    shifts = db.rotation_shifts(ROT)
    sh = db.shift_for_date(shifts, DATE)
    legs = db.day_legs(sh, DATE)
    all_legs = [lg for s in shifts for lg in s['legs']]
    return sh, legs, all_legs


def test_hotel_block_real_bud_night():
    sh, legs, all_legs = _shift_and_legs()
    hb = db.hotel_block(sh, legs, all_legs, DIRECTORY, [('2026-07-27', 'BUD')])
    assert hb is not None
    assert hb['station'] == 'BUD'
    assert hb['hotel'] == 'IntercityHotel Budapest'      # LHs Klarname primär
    assert hb['transfer_min'] is None                    # 0-min-Eintrag → N/A
    assert hb['return_flight'] == 'LH1339'
    assert hb['pickup_utc'] == '2026-07-28T10:55:00Z'
    assert hb['pickup_local'] == '12:55'                 # BUD Sommer +2
    assert hb['pickup_day'] == '2026-07-28'
    assert hb['line'] == 'Hotel | IntercityHotel Budapest (N/A) | PU @ 12:55lcl'


def test_hotel_pickup_taken_from_end_of_multi_day_layover():
    """Florians Kernpunkt (und Tibors Tag-2/2-Fehlerklasse): bei mehrtägigem
    Layover zählt der Abholtag am ENDE der Hotelphase. Konstruktion: 3 Nächte
    BUD, Rückflug erst am 30. — der Pick-up muss der des 30. sein, nicht der
    eines erfundenen ersten Tages."""
    sh, legs, _ = _shift_and_legs()
    all_legs = list(legs) + [{
        'flight': 'LH1339', 'dep': 'BUD', 'arr': 'FRA',
        'dep_iso': '2026-07-30T12:25:00Z', 'arr_iso': '2026-07-30T14:10:00Z',
        'reg': 'DAIRO', 'ac_changed': False, 'transit_min': 0, 'duty_code': '',
        'hotel_name': None, 'pickup_utc': '2026-07-30T10:55:00Z',
        'pickup_lt': '2026-07-30T12:55'}]
    hb = db.hotel_block(sh, legs, all_legs, DIRECTORY,
                        [('2026-07-27', 'BUD'), ('2026-07-28', 'BUD'),
                         ('2026-07-29', 'BUD')])
    assert hb['pickup_day'] == '2026-07-30'
    assert hb['pickup_utc'] == '2026-07-30T10:55:00Z'


def test_hotel_block_none_without_hotel_evidence():
    """Letzter Tag (30.07.): Rotation endet FRA, kein Hotel → kein Block."""
    shifts = db.rotation_shifts(ROT)
    sh = db.shift_for_date(shifts, '2026-07-30')
    legs = db.day_legs(sh, '2026-07-30')
    all_legs = [lg for s in shifts for lg in s['legs']]
    assert db.hotel_block(sh, legs, all_legs, DIRECTORY, []) is None


def test_hotel_block_zag_without_lh_name_uses_directory_and_star():
    """ZAG-Nacht (29.07.): LH liefert KEINEN hotelName (live so!) — Hotel-Event
    beweist die Nacht, das Verzeichnis liefert Name + Zeit mit *."""
    shifts = db.rotation_shifts(ROT)
    sh = db.shift_for_date(shifts, '2026-07-29')
    legs = db.day_legs(sh, '2026-07-29')
    all_legs = [lg for s in shifts for lg in s['legs']]
    hb = db.hotel_block(sh, legs, all_legs, DIRECTORY, [('2026-07-29', 'ZAG')])
    assert hb is not None and hb['station'] == 'ZAG'
    assert hb['hotel'] == 'Sheraton Zagreb' and hb['hotel_source'] == 'directory'
    assert hb['transfer_min'] == 30 and hb['transfer_marker'] == '*'
    assert '0:30*' in hb['line']
    # Rückflug LH1405 am 30. trägt (noch) keinen Pickup → ehrlich n/a
    assert hb['pickup_local'] is None and 'PU n/a' in hb['line']


def test_hotel_block_missing_pickup_is_honest_na():
    sh, legs, _ = _shift_and_legs()
    stripped = [dict(l, pickup_utc=None, pickup_lt=None) for l in legs]
    all_legs = [dict(l, pickup_utc=None, pickup_lt=None)
                for s in db.rotation_shifts(ROT) for l in s['legs']]
    hb = db.hotel_block(sh, stripped, all_legs, DIRECTORY, [('2026-07-27', 'BUD')])
    assert hb['pickup_local'] is None and hb['line'].endswith('PU n/a')


# ── Briefing Room ───────────────────────────────────────────────────────────
def test_briefing_room_only_when_day_starts_with_briefing():
    """26.07. beginnt mit dem Briefing-Duty-Event → Room; Check-in liefert
    einen echten Raum."""
    evs = db.duty_day_events(DE, '2026-07-26')
    assert evs[0]['type'] == 'briefing'
    dec = db.briefing_room_decision(evs, lambda ev: {'briefingRoom': 'B4.123'})
    assert dec == {'room': 'B4.123', 'has_briefing': True, 'room_known': True}


def test_briefing_room_cabin_od_when_room_na_or_missing():
    """LH liefert briefingRoom teils literal 'N/A' (live DUS) → Cabin OD."""
    evs = db.duty_day_events(DE, '2026-07-26')
    dec = db.briefing_room_decision(evs, lambda ev: {'briefingRoom': 'N/A'})
    assert dec['room'] == 'Cabin OD' and dec['room_known'] is False
    dec2 = db.briefing_room_decision(evs, lambda ev: None)
    assert dec2['room'] == 'Cabin OD'


def test_briefing_room_none_on_layover_morning():
    """27.07. beginnt (nach Hotel-Skip) mit dem FLUG LH075 → keine Raumangabe.
    Deckt auch „Dienst beginnt an einer Außenstation ohne Briefing" ab."""
    evs = db.duty_day_events(DE, DATE)
    dec = db.briefing_room_decision(evs, lambda ev: {'briefingRoom': 'X1'})
    assert dec == {'room': None, 'has_briefing': False}


def test_briefing_room_skips_leading_hotel_event():
    evs = [{'type': 'hotel', 'start': None, 'from': 'DUS', 'to': None, 'details': None},
           {'type': 'briefing', 'start': '2026-07-27T11:55:00Z', 'from': 'DUS',
            'to': None, 'details': 'Briefing'},
           {'type': 'flight', 'start': '2026-07-27T13:10:00Z', 'from': 'DUS',
            'to': 'FRA', 'details': 'LH075'}]
    dec = db.briefing_room_decision(evs, lambda ev: {'briefingRoom': ''})
    assert dec['has_briefing'] is True and dec['room'] == 'Cabin OD'


# ── Duty-Events-Selektoren ──────────────────────────────────────────────────
def test_rotation_ids_and_hotel_days_from_real_duty_events():
    assert db.rotation_ids_for_date(DE, DATE) == ['183706']
    hd = db.hotel_days(DE)
    assert ('2026-07-27', 'BUD') in hd and ('2026-07-26', 'DUS') in hd
    assert db.rotation_ids_for_date(DE, '2026-08-01') == []   # OFF-Tag


# ── Assembly: Reihenfolge, Weglassen, Fehlerverhalten ───────────────────────
def _fetchers(crewlist=None, legdetails=None, checkin=None, route=None):
    return {'crewlist': crewlist or (lambda leg: CL_278),
            'legdetails': legdetails or _details_for,
            'checkin': checkin or (lambda ev: None),
            'resolve_route': route or (lambda f, d: None)}


def _cl_router(leg):
    if leg['flight'] == 'LH075':
        return CL_075
    if leg['flight'] == 'LH278':
        return CL_278
    # übrige Legs: identische Crew wie LH278 → kein Change-Block, kein Fehler
    return CL_278


def test_assemble_full_briefing_block_order_and_text():
    briefing, errors = db.assemble_briefing(
        DATE, DE, ROT, _fetchers(crewlist=_cl_router), DIRECTORY)
    assert errors == []
    assert briefing['rotation'] == '183706'
    text = briefing['text'].splitlines()
    assert text[0] == 'Daily Briefing 27JUL'         # kein Room am Layover-Morgen
    # Reihenfolge: A/C → Long Transit → Crew → FDZ → RZ → Hotel
    idx = {k: next(i for i, l in enumerate(text) if l.startswith(k))
           for k in ('A/C Change', 'Long Transit', 'Crew Change',
                     'FDZ-Toleranz', 'RZ-Toleranz', 'Hotel |')}
    assert (idx['A/C Change'] < idx['Long Transit'] < idx['Crew Change']
            < idx['FDZ-Toleranz'] < idx['RZ-Toleranz'] < idx['Hotel |'])
    assert briefing['fdz']['line'] == 'FDZ-Toleranz | MTV 4:00 / EASA 2:00'
    assert briefing['rz']['mtv_min'] == 195
    assert briefing['hotel']['station'] == 'BUD'
    # Crew-Vergleich umfasst den Return-Übergang LH1342→LH1339 (28.); die
    # Fixture-Crews dort sind identisch → kein Block. Der echte
    # LH075→LH278-Block ist der einzige.
    assert [b['flight'] for b in briefing['crew_changes']] == ['LH278']


def test_assemble_rz_and_hotel_omitted_without_hotel():
    """30.07. endet an der Homebase: RZ-Block und Hotel-Block fallen weg,
    FDZ bleibt (immer)."""
    briefing, errors = db.assemble_briefing(
        '2026-07-30', DE, ROT, _fetchers(), DIRECTORY)
    assert briefing['hotel'] is None and briefing['rz'] is None
    assert 'RZ-Toleranz' not in briefing['text']
    assert 'Hotel |' not in briefing['text']
    assert 'FDZ-Toleranz' in briefing['text']


def test_assemble_crewlist_failure_is_visible_not_silent():
    """Fehlerverhalten: eine nicht ladbare Crew-Liste erscheint als Fehler mit
    Phasen-Zuordnung, complete=False — kein stilles „keine Änderungen"."""
    briefing, errors = db.assemble_briefing(
        DATE, DE, ROT, _fetchers(crewlist=lambda leg: None), DIRECTORY)
    assert briefing is not None
    assert errors and all(e['phase'] == 'crewlist' for e in errors)
    assert briefing['crew_changes'] == []


def test_assemble_none_when_rotation_lacks_the_date():
    briefing, errors = db.assemble_briefing('2026-08-15', DE, ROT,
                                            _fetchers(), DIRECTORY)
    assert briefing is None
    assert errors[0]['phase'] == 'rotation_day_match'


def test_assemble_header_with_room_on_briefing_day():
    briefing, _ = db.assemble_briefing(
        '2026-07-26', DE, ROT,
        _fetchers(checkin=lambda ev: {'briefingRoom': 'C2.007'}), DIRECTORY)
    assert briefing['text'].splitlines()[0] == 'Daily Briefing 26JUL | Room C2.007'


# ── Format-Helper ───────────────────────────────────────────────────────────
def test_fmt_hm_and_daylabel_and_regshort():
    assert db._fmt_hm(80) == '1:20'
    assert db._fmt_hm(0) == '0:00'
    assert db._fmt_hm(-20) == '-0:20'
    assert db._fmt_hm(None) == 'n/a'
    assert db._day_label('2026-07-02') == '02JUL'
    assert db._reg_short('DAILF') == '-ILF'
    assert db._reg_short('D-AIZJ') == '-IZJ'
    assert db._reg_short('') is None


# ── Endpoint-Verhalten (Flask, LH gemockt) ──────────────────────────────────
@pytest.fixture()
def client(monkeypatch):
    import app as backend
    from blueprints import lh_flightops as fo
    monkeypatch.setattr(fo, 'flightops_connected', lambda t: True)
    monkeypatch.setattr(fo, '_rot_hour_used', lambda: 0)
    monkeypatch.setattr(fo, 'duty_events', lambda t, a, b: DE)
    monkeypatch.setattr(fo, 'crew_rotation', lambda t, *r: ROT)
    monkeypatch.setattr(fo, '_resolve_link_params',
                        lambda *a, **k: {'accessCode': 'X'})
    monkeypatch.setattr(fo, 'crew_list',
                        lambda t, f, d, dep, arr, ac: (
                            CL_075 if f == 'LH075' else CL_278))
    monkeypatch.setattr(fo, 'flight_leg_details',
                        lambda t, f, d=None, dep=None, arr=None: (
                            LD_075 if f == 'LH075'
                            else LD_278 if f == 'LH278' else {}))
    monkeypatch.setattr(backend, '_viewer_airline_and_calendar',
                        lambda t: ('Lufthansa', True))
    monkeypatch.setattr(backend, '_crew_hotel_dir_serve', lambda a: DIRECTORY)
    monkeypatch.setattr(backend, '_flight_obs_merged',
                        lambda *a, **k: None, raising=False)
    # BUG004-Gate: Pfad-Token als bekannt gelten lassen (das Binding Bearer==
    # Pfad-Token bleibt aktiv und wird vom _Client unten erfüllt).
    monkeypatch.setattr(backend, '_validate_token', lambda t: backend._TokenValidationResult(
        backend._TokenValidationState.VALID))
    db._cache.clear()

    class _Client:
        """Test-Client, der das Bearer==Pfad-Token-Binding (BUG004-Gate)
        automatisch erfüllt — der Endpoint ist ein PII-GET."""

        def __init__(self, c):
            self._c = c

        def get(self, path, **kw):
            tok = path.rsplit('/', 1)[-1].split('?')[0]
            headers = dict(kw.pop('headers', {}) or {})
            headers.setdefault('Authorization', f'Bearer {tok}')
            return self._c.get(path, headers=headers, **kw)
    return _Client(backend.app.test_client())


def test_endpoint_full_briefing_real_day(client):
    r = client.get(f'/api/ax/daily-briefing/AT-TEST?date={DATE}')
    assert r.status_code == 200
    d = r.get_json()
    assert d['ok'] and d['available'] and d['complete']
    b = d['briefing']
    assert b['rotation'] == '183706'
    assert b['ac_changes'][0]['line'].startswith('A/C Change | LH278')
    assert b['hotel']['station'] == 'BUD'
    assert d['lh_calls']['duty_events'] == 1
    assert d['lh_calls']['rotation'] == 1
    assert d['lh_calls']['crewlist'] == 5     # 4 Tages-Legs + 1 Return-Leg


def test_endpoint_no_rotation_is_honest_nothing(client):
    r = client.get('/api/ax/daily-briefing/AT-TEST?date=2026-08-01')
    d = r.get_json()
    assert d['ok'] is True and d['available'] is False
    assert d['reason'] == 'no_rotation'
    assert 'briefing' not in d


def test_endpoint_not_connected_401(client, monkeypatch):
    from blueprints import lh_flightops as fo
    monkeypatch.setattr(fo, 'flightops_connected', lambda t: False)
    db._cache.clear()
    r = client.get(f'/api/ax/daily-briefing/AT-TEST?date={DATE}')
    assert r.status_code == 401
    assert r.get_json()['phase'] == 'auth'


def test_endpoint_duty_events_failure_maps_phase(client, monkeypatch):
    from blueprints import lh_flightops as fo
    monkeypatch.setattr(fo, 'duty_events', lambda t, a, b: None)
    db._cache.clear()
    r = client.get(f'/api/ax/daily-briefing/AT-TEST?date={DATE}')
    assert r.status_code == 502
    assert r.get_json()['phase'] == 'duty_events'


def test_endpoint_rotation_failure_no_partial_result(client, monkeypatch):
    from blueprints import lh_flightops as fo
    monkeypatch.setattr(fo, 'crew_rotation', lambda t, *r: None)
    db._cache.clear()
    r = client.get(f'/api/ax/daily-briefing/AT-TEST?date={DATE}')
    assert r.status_code == 502
    assert r.get_json()['phase'] == 'rotation'


def test_endpoint_quota_brake_defers(client, monkeypatch):
    from blueprints import lh_flightops as fo
    monkeypatch.setattr(fo, '_rot_hour_used', lambda: 900)
    db._cache.clear()
    r = client.get(f'/api/ax/daily-briefing/AT-TEST?date={DATE}')
    assert r.status_code == 503
    assert r.get_json()['error'] == 'lh_quota_deferred'


def test_endpoint_cache_serves_second_request_without_calls(client, monkeypatch):
    from blueprints import lh_flightops as fo
    r1 = client.get(f'/api/ax/daily-briefing/AT-TEST?date={DATE}')
    assert r1.status_code == 200
    calls = {'n': 0}

    def _boom(*a, **k):
        calls['n'] += 1
        raise AssertionError('LH-Call trotz Cache')
    monkeypatch.setattr(fo, 'duty_events', _boom)
    r2 = client.get(f'/api/ax/daily-briefing/AT-TEST?date={DATE}')
    assert r2.status_code == 200 and calls['n'] == 0


def test_endpoint_invalid_date_400(client):
    r = client.get('/api/ax/daily-briefing/AT-TEST?date=27.07.2026')
    assert r.status_code == 400


# ── Regressionen aus dem adversarialen Review (27.07.2026) ──────────────────
def test_endpoint_is_pii_gated_no_bearer_no_data(client, monkeypatch):
    """BLOCKER-Finding 1: der GET liefert Crew-Namen/PKs — ohne Bearer==Pfad-
    Token (BUG004-Gate, prod = ENFORCE) darf NICHTS kommen."""
    import app as backend
    assert backend._bug004_get_route_needs_auth('/api/ax/daily-briefing/AT-X')
    monkeypatch.setattr(backend, '_BUG004_REQUIRE_TOKEN_BINDING', True)
    raw = backend.app.test_client()   # ohne Authorization-Header
    r = raw.get(f'/api/ax/daily-briefing/AT-TEST?date={DATE}')
    assert r.status_code == 401
    r2 = raw.get(f'/api/ax/daily-briefing/AT-TEST?date={DATE}',
                 headers={'Authorization': 'Bearer AT-ANDERER'})
    assert r2.status_code == 401
    assert r2.get_json()['error'] == 'token_binding_mismatch'


def _redeye_shifts():
    """Schicht 1 beginnt am 27. und hat ein Red-Eye-Leg 00:30Z am 28.;
    Schicht 2 ist die echte Duty des 28."""
    mk = lambda flight, dep, arr, dep_iso, arr_iso: {
        'flight': flight, 'dep': dep, 'arr': arr, 'dep_iso': dep_iso,
        'arr_iso': arr_iso, 'reg': 'DAIXX', 'ac_changed': False,
        'transit_min': 0, 'duty_code': '', 'hotel_name': None,
        'pickup_utc': None, 'pickup_lt': None}
    s1 = {'rotation': 'R1', 'homebase': 'FRA', 'shift_no': 1,
          'begin': '2026-07-27T20:00:00Z', 'end': '2026-07-28T03:00:00Z',
          'briefing_cab': '2026-07-27T20:00:00Z', 'briefing_coc': None,
          'attributes': {'CAB_MTV_FDZ': 400, 'CAB_MTV_MAX': 840,
                         'CAB_LAW_FDZ': 380, 'CAB_LAW_MAX': 660,
                         'CAB_MTV_RZ': 600, 'CAB_MTV_RZ_ACTUAL': 700},
          'legs': [mk('LH900', 'FRA', 'LIS', '2026-07-27T21:00:00Z', '2026-07-27T23:30:00Z'),
                   mk('LH901', 'LIS', 'FRA', '2026-07-28T00:30:00Z', '2026-07-28T03:00:00Z')]}
    s2 = {'rotation': 'R1', 'homebase': 'FRA', 'shift_no': 2,
          'begin': '2026-07-28T15:00:00Z', 'end': '2026-07-28T23:00:00Z',
          'briefing_cab': '2026-07-28T15:00:00Z', 'briefing_coc': None,
          'attributes': {'CAB_MTV_FDZ': 500, 'CAB_MTV_MAX': 840,
                         'CAB_LAW_FDZ': 480, 'CAB_LAW_MAX': 700,
                         'CAB_MTV_RZ': 600, 'CAB_MTV_RZ_ACTUAL': 800},
          'legs': [mk('LH910', 'FRA', 'BCN', '2026-07-28T16:00:00Z', '2026-07-28T18:00:00Z')]}
    return [s1, s2]


def test_redeye_next_day_prefers_shift_that_begins_that_day():
    """BLOCKER-Finding 2: am 28. gewinnt die Schicht, die am 28. BEGINNT —
    nicht die Red-Eye-Schicht des Vortags mit ihrem 00:30Z-Tail-Leg."""
    shifts = _redeye_shifts()
    sh = db.shift_for_date(shifts, '2026-07-28')
    assert sh['shift_no'] == 2
    assert [l['flight'] for l in db.day_legs(sh, '2026-07-28')] == ['LH910']
    # der 27. bleibt bei Schicht 1 inkl. Nach-Mitternacht-Leg
    sh27 = db.shift_for_date(shifts, '2026-07-27')
    assert sh27['shift_no'] == 1
    assert [l['flight'] for l in db.day_legs(sh27, '2026-07-27')] == ['LH900', 'LH901']


def test_redeye_same_shift_transition_is_not_a_return():
    """Finding 2b: LH900→LH901 (00:30Z am Folgetag, DIESELBE Schicht) ist eine
    Fortsetzung — kein 'Return @'-Label. Return nur zum Leg der NÄCHSTEN Schicht."""
    a = [_member('P1'), _member('P2', last='WOLF')]
    b = [_member('P1')]
    leg901 = {'flight': 'LH901', 'dep': 'LIS', 'arr': 'FRA',
              'dep_iso': '2026-07-28T00:30:00Z', 'arr_iso': '2026-07-28T03:00:00Z'}
    blk = db.crew_change_block(a, b, leg901, '2026-07-27', _NO_ROUTE,
                               from_next_shift=False)
    assert blk['ref'] == 'LH901: LIS - FRA' and blk['is_return'] is False
    blk2 = db.crew_change_block(a, b, leg901, '2026-07-27', _NO_ROUTE,
                                from_next_shift=True)
    assert blk2['ref'] == 'Return @ 28JUL'


def test_empty_crewlist_is_error_not_phantom_exodus():
    """MAJOR-Finding 3: eine leere Crew-Liste ist ein Datenloch — sie darf
    nicht als „alle steigen aus" gerendert werden."""
    def _cl(leg):
        if leg['flight'] == 'LH075':
            return CL_075
        if leg['flight'] == 'LH278':
            return CL_278
        return {'crewMembers': []}     # LH279 & Co: leer
    briefing, errors = db.assemble_briefing(
        DATE, DE, ROT, _fetchers(crewlist=_cl), DIRECTORY)
    assert [b['flight'] for b in briefing['crew_changes']] == ['LH278']
    empty = [e for e in errors if e['error'] == 'crewlist_empty']
    assert empty and all(e['phase'] == 'crewlist' for e in empty)


def test_transfer_subset_name_is_not_a_match():
    """MAJOR-Finding 4: 'Hilton Frankfurt Airport' darf NICHT als das
    'Hilton Garden Inn Frankfurt Airport' durchgehen. Ein-Hotel-Destination →
    Destinations-Zeit mit *, nie ein stiller 'exact'-Treffer."""
    d = [{'iata': 'FRA', 'hotel': 'Hilton Garden Inn Frankfurt Airport',
          'transfer_min': 25, 'votes': 1}]
    m = db.transfer_match('FRA', 'Hilton Frankfurt Airport', d)
    assert m['reason'] == 'destination_general' and m['marker'] == '*'
    # beide Häuser im Verzeichnis: der exakte Token-Treffer gewinnt eindeutig
    d2 = d + [{'iata': 'FRA', 'hotel': 'Hilton Frankfurt Airport',
               'transfer_min': 15, 'votes': 1}]
    m2 = db.transfer_match('FRA', 'Hilton Frankfurt Airport', d2)
    assert m2['transfer_min'] == 15 and m2['reason'] == 'exact'


def test_pure_hotel_day_serves_reduced_briefing():
    """MAJOR-Finding 5: reiner Hoteltag mitten im Layover → Header + Hotel/
    Pick-up (vom ECHTEN Rückflug-Leg am Ende der Phase) + RZ der laufenden
    Ruhe; FDZ ehrlich n/a."""
    mk = lambda **kw: {'reg': 'DAIXX', 'ac_changed': False, 'transit_min': 0,
                       'duty_code': '', 'hotel_name': None,
                       'pickup_utc': None, 'pickup_lt': None, **kw}
    rot = {'rotations': [{'rotationNumber': '9', 'homebase': 'FRA', 'shifts': [
        {'shiftNumber': 1, 'shiftBegin': '2026-07-27T10:00:00Z',
         'shiftEnd': '2026-07-27T22:00:00Z',
         'briefingBeginCab': '2026-07-27T10:00:00Z',
         'attributes': {'CAB_MTV_RZ': 600, 'CAB_MTV_RZ_ACTUAL': 3000,
                        'CAB_MTV_FDZ': 500, 'CAB_MTV_MAX': 840},
         'legs': [{'flightDesignator': 'LH500', 'departureAirport': 'FRA',
                   'arrivalAirport': 'BUD', 'depatureDate': '2026-07-27T18:00:00Z',
                   'arrivalDate': '2026-07-27T20:00:00Z',
                   'hotelName': 'IntercityHotel Budapest'}]},
        {'shiftNumber': 2, 'shiftBegin': '2026-07-30T10:00:00Z',
         'shiftEnd': '2026-07-30T16:00:00Z',
         'briefingBeginCab': '2026-07-30T10:00:00Z',
         'attributes': {},
         'legs': [{'flightDesignator': 'LH501', 'departureAirport': 'BUD',
                   'arrivalAirport': 'FRA', 'depatureDate': '2026-07-30T11:00:00Z',
                   'arrivalDate': '2026-07-30T13:00:00Z',
                   'pickupTime': '2026-07-30T09:00:00Z'}]}]}]}
    de = {'rosterDays': [
        {'day': '2026-07-28', 'events': [
            {'eventCategory': 'HOTEL', 'eventType': 'HOTEL',
             'startLocation': 'BUD', 'endLocation': None,
             'eventAttributes': [{'rotationId': '9'}]}]}]}
    assert db.rotation_ids_for_date(de, '2026-07-28') == ['9']
    briefing, errors = db.assemble_briefing('2026-07-28', de, rot,
                                            _fetchers(), DIRECTORY)
    assert errors == [] and briefing['hotel_day'] is True
    assert briefing['hotel']['station'] == 'BUD'
    assert briefing['hotel']['pickup_day'] == '2026-07-30'   # ENDE der Phase
    assert briefing['hotel']['pickup_utc'] == '2026-07-30T09:00:00Z'
    assert briefing['rz']['mtv_min'] == 2400                 # laufende Ruhe
    assert briefing['fdz']['line'] == 'FDZ-Toleranz | MTV n/a / EASA n/a'
    assert briefing['crew_changes'] == [] and briefing['ac_changes'] == []


def test_hotel_block_uses_real_rotation_homebase():
    """MINOR-Finding 7: MUC-Crew mit Nightstop FRA behält den Hotel-Block —
    unterdrückt wird nur die ECHTE Homebase des Umlaufs."""
    mk = lambda **kw: {'reg': None, 'ac_changed': False, 'transit_min': 0,
                       'duty_code': '', 'hotel_name': None,
                       'pickup_utc': None, 'pickup_lt': None, **kw}
    sh = {'rotation': 'X', 'homebase': 'MUC', 'shift_no': 1, 'attributes': {}}
    legs = [mk(flight='LH100', dep='MUC', arr='FRA',
               dep_iso='2026-07-27T18:00:00Z', arr_iso='2026-07-27T19:00:00Z',
               hotel_name='Hilton Frankfurt City')]
    hb = db.hotel_block(sh, legs, legs, [], [('2026-07-27', 'FRA')])
    assert hb is not None and hb['station'] == 'FRA'
    # dieselbe Nacht an der eigenen Base MUC → kein Block
    legs2 = [mk(flight='LH101', dep='FRA', arr='MUC',
                dep_iso='2026-07-27T18:00:00Z', arr_iso='2026-07-27T19:00:00Z')]
    assert db.hotel_block(sh, legs2, legs2, [], [('2026-07-27', 'MUC')]) is None


def test_endpoint_cache_not_served_after_disconnect(client, monkeypatch):
    """MINOR-Finding 9: nach Grant-Widerruf serviert auch der Cache nichts mehr."""
    from blueprints import lh_flightops as fo
    r1 = client.get(f'/api/ax/daily-briefing/AT-TEST?date={DATE}')
    assert r1.status_code == 200
    monkeypatch.setattr(fo, 'flightops_connected', lambda t: False)
    r2 = client.get(f'/api/ax/daily-briefing/AT-TEST?date={DATE}')
    assert r2.status_code == 401


# ── LH-Klarname → Verzeichnis-Anreicherung (Owner-Zusatz 27.07.2026) ────────
def test_valid_lh_hotel_name_rejects_placeholders():
    assert db._valid_lh_hotel_name('Hyatt Regency Boston')
    assert db._valid_lh_hotel_name('AC by Marriott')
    # LH-interner Hotel-Code (Doku-Fixture-Shape) und Platzhalter fallen durch
    assert not db._valid_lh_hotel_name('H9941671')
    assert not db._valid_lh_hotel_name('N/A')
    assert not db._valid_lh_hotel_name('NA')
    assert not db._valid_lh_hotel_name('TBD')
    assert not db._valid_lh_hotel_name('---')
    assert not db._valid_lh_hotel_name('')
    assert not db._valid_lh_hotel_name(None)
    assert not db._valid_lh_hotel_name('X1')


def test_official_name_action_four_owner_cases():
    # Fall 1: Schreibweise weicht ab, gleiches Haus → enrich
    m = db.transfer_match('DUS', 'Clayton Hotel Düsseldorf', DIRECTORY)
    assert db.official_name_action('Clayton Hotel Düsseldorf', m) == ('enrich', False)
    # Fall 2: anderes Haus an Multi-Hotel-Station → NUR melden, nie schreiben
    m2 = db.transfer_match('BCN', 'Hilton Barcelona', DIRECTORY)
    assert db.official_name_action('Hilton Barcelona', m2) == ('report', True)
    # Fall 2b: anderes Haus an Ein-Hotel-Station → ebenfalls nur melden
    m2b = db.transfer_match('ZAG', 'Westin Zagreb', DIRECTORY)
    assert db.official_name_action('Westin Zagreb', m2b) == ('report', True)
    # Fall 3: Station ganz ohne Eintrag → suggest, kein Konflikt
    m3 = db.transfer_match('NRT', 'Hilton Narita', DIRECTORY)
    assert db.official_name_action('Hilton Narita', m3) == ('suggest', False)
    # Platzhalter → nie eine Aktion
    m4 = db.transfer_match('DUS', 'H9941671', DIRECTORY)
    assert db.official_name_action('H9941671', m4) == (None, False)


def test_official_name_action_idempotent_when_already_enriched():
    """Bereits angereicherter Eintrag (Serve substituiert hotel=official,
    official=True, hotel_crowd=Crowd-Name) → keine zweite Aktion."""
    enriched = [{'iata': 'VCE', 'hotel': 'Leonardo Royal Venice Mestre',
                 'hotel_crowd': 'Leonardo Royal Hotel Venice Mestre',
                 'official': True, 'transfer_min': 15, 'votes': 1}]
    m = db.transfer_match('VCE', 'Leonardo Royal Venice Mestre', enriched)
    assert m['reason'] == 'exact' and m['transfer_min'] == 15
    assert db.official_name_action('Leonardo Royal Venice Mestre', m) == (None, False)
    # Crowd-Name exakt gleich LHs Name, keine Anreicherung nötig
    same = [{'iata': 'BOS', 'hotel': 'Hyatt Regency Boston',
             'transfer_min': 20, 'votes': 1}]
    ms = db.transfer_match('BOS', 'Hyatt Regency Boston', same)
    assert db.official_name_action('Hyatt Regency Boston', ms) == (None, False)


class _FakeSB:
    """Aufzeichnender Supabase-Stub: Selects aus `select_rows`, Writes in `ops`."""

    def __init__(self, select_rows):
        self.select_rows = select_rows      # list[dict] für JEDEN Select (FIFO)
        self.ops = []

    def table(self, name):
        fake = self

        class _T:
            def __init__(self):
                self.op = ('select', None)
                self.payload = None
                self.filters = []

            def select(self, cols):
                self.op = ('select', cols)
                return self

            def update(self, payload):
                self.op = ('update', None)
                self.payload = payload
                return self

            def insert(self, payload):
                self.op = ('insert', None)
                self.payload = payload
                return self

            def eq(self, k, v):
                self.filters.append(('eq', k, v))
                return self

            def ilike(self, k, v):
                self.filters.append(('ilike', k, v))
                return self

            def limit(self, n):
                return self

            def execute(self):
                import types
                if self.op[0] == 'select':
                    rows = fake.select_rows.pop(0) if fake.select_rows else []
                    return types.SimpleNamespace(data=rows)
                fake.ops.append((name, self.op[0], self.payload, list(self.filters)))
                return types.SimpleNamespace(data=[])
        return _T()


@pytest.fixture()
def sync_env(monkeypatch):
    import app as backend
    monkeypatch.setattr(backend, '_viewer_airline_and_calendar',
                        lambda t: ('Lufthansa', True))
    monkeypatch.setattr(backend, 'SB_AVAILABLE', True, raising=False)
    db._dir_sync_memo.clear()

    def _install(select_rows):
        fake = _FakeSB(select_rows)
        monkeypatch.setattr(backend, 'sb', fake, raising=False)
        return fake
    return _install


def test_sync_enrich_writes_only_official_name_fields(sync_env):
    fake = sync_env([[{'id': 'r1', 'official_name': None,
                       'hotel': 'Clayton Hotel Düsseldorf (ehem. Nikko)'}]])
    out = db._sync_official_name('AT-U', 'DUS', 'Clayton Hotel Düsseldorf', DIRECTORY)
    assert out == 'enriched'
    assert len(fake.ops) == 1
    table, op, payload, filters = fake.ops[0]
    assert table == 'crew_hotel_directory' and op == 'update'
    assert set(payload.keys()) == {'official_name', 'official_name_source',
                                   'official_name_at'}
    assert payload['official_name'] == 'Clayton Hotel Düsseldorf'
    assert payload['official_name_source'] == 'lh_flightops'
    assert ('eq', 'id', 'r1') in filters      # exakt EIN adressierter Eintrag


def test_sync_enrich_skips_when_already_recorded(sync_env):
    fake = sync_env([[{'id': 'r1',
                       'official_name': 'Clayton Hotel Düsseldorf',
                       'hotel': 'Clayton Hotel Düsseldorf (ehem. Nikko)'}]])
    assert db._sync_official_name('AT-U', 'DUS', 'Clayton Hotel Düsseldorf',
                                  DIRECTORY) is None
    assert fake.ops == []


def test_sync_enrich_refuses_ambiguous_row_address(sync_env):
    """Zwei aktive Zeilen mit demselben Crowd-Namen → NICHT anfassen (nie zwei
    Häuser zusammenführen)."""
    fake = sync_env([[{'id': 'r1'}, {'id': 'r2'}]])
    assert db._sync_official_name('AT-U', 'DUS', 'Clayton Hotel Düsseldorf',
                                  DIRECTORY) is None
    assert fake.ops == []


def test_sync_suggest_unknown_hotel_no_invented_time(sync_env):
    fake = sync_env([[], []])     # Dedupe-Selects: hotel, official_name → leer
    out = db._sync_official_name('AT-U', 'NRT', 'Hilton Narita', DIRECTORY)
    assert out == 'suggested'
    assert len(fake.ops) == 1
    table, op, payload, _f = fake.ops[0]
    assert op == 'insert'
    assert payload['status'] == 'suggested'       # NIE auto-approve
    assert payload['transfer_min'] == 0           # keine erfundene Zeit
    assert payload['hotel'] == 'Hilton Narita'
    assert payload['official_name_source'] == 'lh_flightops'
    assert payload['airline'] == 'LUFTHANSA' and payload['iata'] == 'NRT'


def test_sync_suggest_deduped_no_second_write(sync_env):
    fake = sync_env([[{'id': 'x'}]])   # erster Dedupe-Select trifft schon
    assert db._sync_official_name('AT-U', 'NRT', 'Hilton Narita', DIRECTORY) is None
    assert fake.ops == []
    # und derselbe Name direkt nochmal → Memo blockt ohne jeden DB-Zugriff
    assert db._sync_official_name('AT-U', 'NRT', 'Hilton Narita', DIRECTORY) is None
    assert fake.ops == []


def test_sync_conflict_with_approved_is_reported_not_rewritten(sync_env, caplog):
    """Fall 4: LH-Name kollidiert mit dem approved-Bestand → NUR Konflikt-Log.
    Kein Update, und bewusst auch KEIN automatischer Vorschlag: ein an einer
    belegten Station eingefügter `suggested`-Eintrag wäre über die
    Vote-Promotion in /api/ax/crew-hotels/suggest beim ersten menschlichen Tap
    zu `approved` geworden und hätte den bisherigen Eintrag deaktiviert."""
    import logging
    fake = sync_env([])
    with caplog.at_level(logging.WARNING, logger='aerotax'):
        out = db._sync_official_name('AT-U', 'ZAG', 'Westin Zagreb', DIRECTORY)
    assert out == 'reported'
    assert any('hotel-name-conflict' in r.message for r in caplog.records)
    assert fake.ops == []            # gar kein Schreibzugriff


def test_sync_placeholder_name_never_writes(sync_env):
    fake = sync_env([])
    assert db._sync_official_name('AT-U', 'DUS', 'H9941671', DIRECTORY) is None
    assert db._sync_official_name('AT-U', 'DUS', 'N/A', DIRECTORY) is None
    assert fake.ops == []


def test_dir_serve_substitutes_official_name_for_display(monkeypatch):
    """Serve-Regel: official_name gewinnt die Anzeige (`hotel`), der Crowd-Name
    bleibt als hotel_crowd erhalten — iOS zeigt so den offiziellen Namen ohne
    App-Update; transfer_min/votes unverändert."""
    import app as backend
    rows = [{'iata': 'DUS', 'base': None,
             'hotel': 'Clayton Hotel Düsseldorf (ehem. Nikko)',
             'transfer_min': 30, 'votes': 1,
             'official_name': 'Clayton Hotel Düsseldorf'},
            {'iata': 'BUD', 'base': None,
             'hotel': 'Hilton Garden Inn Budapest City Centre',
             'transfer_min': 0, 'votes': 1, 'official_name': None}]
    fake = _FakeSB([rows])
    monkeypatch.setattr(backend, 'SB_AVAILABLE', True, raising=False)
    monkeypatch.setattr(backend, 'sb', fake, raising=False)
    out = backend._crew_hotel_dir_serve('LUFTHANSA')
    assert out[0]['hotel'] == 'Clayton Hotel Düsseldorf'
    assert out[0]['hotel_crowd'] == 'Clayton Hotel Düsseldorf (ehem. Nikko)'
    assert out[0]['official'] is True
    assert out[0]['transfer_min'] == 30 and out[0]['votes'] == 1
    assert out[1]['hotel'] == 'Hilton Garden Inn Budapest City Centre'
    assert 'official' not in out[1] and 'hotel_crowd' not in out[1]


# ── Die vier Owner-Fälle am HOTEL-BLOCK (Anzeige + Zeit gemeinsam) ──────────
# Die Einzelteile (transfer_match, official_name_action, _sync_official_name)
# sind oben je für sich geprüft. Hier zählt das ZUSAMMENSPIEL, denn genau da
# entsteht der teure Fehler: richtiger Name mit falscher Fahrtzeit.
def _mk_leg(**kw):
    base = {'flight': 'LH900', 'dep': 'FRA', 'arr': 'BOS',
            'dep_iso': '2026-07-27T10:00:00Z', 'arr_iso': '2026-07-27T18:00:00Z',
            'reg': 'DAIXX', 'ac_changed': False, 'transit_min': 0,
            'duty_code': '', 'hotel_name': None,
            'pickup_utc': None, 'pickup_lt': None}
    base.update(kw)
    return base


_FRA_SHIFT = {'homebase': 'FRA'}


def test_case1_spelling_variant_shows_lh_name_and_keeps_directory_time():
    """FALL 1 — abweichende Schreibweise, GLEICHES Haus: LHs Klarname gewinnt
    die Anzeige, die Verzeichnis-Zeit gilt unmarkiert, und der Eintrag wird
    angereichert (nicht ersetzt)."""
    leg = _mk_leg(arr='DUS', hotel_name='Clayton Hotel Düsseldorf')
    hb = db.hotel_block(_FRA_SHIFT, [leg], [leg], DIRECTORY, [])
    assert hb['hotel'] == 'Clayton Hotel Düsseldorf'      # LH gewinnt die ANZEIGE
    assert hb['hotel_source'] == 'lh'
    assert hb['transfer_min'] == 30                        # Zeit gilt …
    assert hb['transfer_marker'] == ''                     # … unmarkiert (sicher)
    assert hb['transfer_reason'] == 'exact'
    assert hb['line'].startswith('Hotel | Clayton Hotel Düsseldorf (0:30)')
    # …und genau das ist der Anreicherungs-Fall (ergänzen, nicht ersetzen)
    m = db.transfer_match('DUS', hb['hotel'], DIRECTORY)
    assert db.official_name_action(hb['hotel'], m) == ('enrich', False)


def test_case2_other_house_same_station_never_inherits_the_time():
    """FALL 2 — ANDERES Haus an derselben Station (Umbuchung / Station mit zwei
    Hotels): Name zeigen, Zeit NICHT übertragen. Florians Eskalation: EIN
    bekanntes Hotel → Destinations-Zeit mit '*'; MEHRERE → N/A."""
    # 2a: ZAG kennt genau ein Hotel (Sheraton) — LH bucht ins Westin um.
    leg = _mk_leg(arr='ZAG', hotel_name='Westin Zagreb')
    hb = db.hotel_block(_FRA_SHIFT, [leg], [leg], DIRECTORY, [])
    assert hb['hotel'] == 'Westin Zagreb'                  # Name zeigen
    assert hb['transfer_reason'] == 'destination_general'
    assert hb['transfer_marker'] == '*'                    # markiert = „nur Richtwert"
    assert '(0:30*)' in hb['line']
    # 2b: BCN kennt ZWEI Hotels — kein eindeutiger Treffer → ehrlich N/A.
    leg2 = _mk_leg(arr='BCN', hotel_name='Hilton Barcelona')
    hb2 = db.hotel_block(_FRA_SHIFT, [leg2], [leg2], DIRECTORY, [])
    assert hb2['hotel'] == 'Hilton Barcelona'
    assert hb2['transfer_reason'] == 'ambiguous_multi_hotel'
    assert hb2['transfer_min'] is None and hb2['transfer_marker'] is None
    assert '(N/A)' in hb2['line']
    # Unter KEINEN Umständen die Zeit eines der beiden anderen Häuser erben.
    assert hb2['transfer_min'] not in (20, 25)


def test_case3_unknown_hotel_shows_name_time_na_and_suggests(sync_env):
    """FALL 3 — Hotel völlig unbekannt: Name zeigen, Zeit N/A, `suggested`-
    Eintrag über den bestehenden Vorschlags-Weg, OHNE erfundene transfer_min."""
    leg = _mk_leg(arr='NRT', hotel_name='Hilton Narita')
    hb = db.hotel_block(_FRA_SHIFT, [leg], [leg], DIRECTORY, [])
    assert hb['hotel'] == 'Hilton Narita' and hb['hotel_source'] == 'lh'
    assert hb['transfer_min'] is None and hb['transfer_reason'] == 'no_entry'
    assert '(N/A)' in hb['line']
    fake = sync_env([[], []])
    assert db._sync_official_name('AT-U', 'NRT', hb['hotel'], DIRECTORY) == 'suggested'
    assert len(fake.ops) == 1
    _t, op, payload, _f = fake.ops[0]
    assert op == 'insert' and payload['status'] == 'suggested'
    assert payload['transfer_min'] == 0          # KEINE erfundene Fahrtzeit
    assert payload['official_name_source'] == 'lh_flightops'   # Herkunft erkennbar


def test_case4_approved_entry_is_reported_never_rewritten(sync_env, caplog):
    """FALL 4 — LH schickt DAUERHAFT einen anderen Namen als der freigegebene
    Eintrag (Hotelvertrag-Wechsel). Melden, nicht umschreiben: der approved-
    Eintrag wird über drei Läufe hinweg nie angefasst, nie zurückgestuft, und
    kein automatischer Pfad erzeugt je ein `approved`.

    `_crew_hotel_dir_serve` liefert ausschliesslich status='approved' + active
    — jede Zeile, die das Briefing zu sehen bekommt, ist per Konstruktion
    freigegeben."""
    import logging
    approved = [{'iata': 'ZAG', 'hotel': 'Sheraton Zagreb',
                 'transfer_min': 30, 'votes': 1}]
    for _ in range(3):                       # „dauerhaft" = mehrere Läufe
        db._dir_sync_memo.clear()
        fake = sync_env([])
        with caplog.at_level(logging.WARNING, logger='aerotax'):
            out = db._sync_official_name('AT-U', 'ZAG', 'Westin Zagreb', approved)
        assert out == 'reported'
        # ÜBERHAUPT kein Schreibzugriff — nicht nur „kein Update". Ein
        # automatischer `suggested`-Eintrag an einer belegten Station wäre über
        # die Vote-Promotion mittelbar doch zur Abstufung geworden.
        assert fake.ops == []
    assert any('hotel-name-conflict' in r.message for r in caplog.records)
    # Gegenprobe, damit der Test nicht vacuous ist: an einer LEEREN Station
    # schreibt derselbe Pfad sehr wohl — der Unterschied ist der Bestand.
    db._dir_sync_memo.clear()
    fake2 = sync_env([[], []])
    assert db._sync_official_name('AT-U', 'NRT', 'Westin Narita', []) == 'suggested'
    assert [op for (_t, op, _p, _f) in fake2.ops] == ['insert']


def test_same_divergent_name_twice_creates_only_one_suggestion(sync_env):
    """Derselbe abweichende Name zweimal → genau EIN Vorschlag. Wichtig, weil
    der `suggested`-Eintrag im approved-Serve nie auftaucht: der Abgleich läuft
    an jedem Folgetag erneut und muss am DB-Dedupe scheitern, nicht nur am
    Prozess-Memo (das nach 6 h abläuft / beim Deploy weg ist)."""
    fake = sync_env([[], []])
    assert db._sync_official_name('AT-U', 'NRT', 'Hilton Narita',
                                  DIRECTORY) == 'suggested'
    assert len([o for o in fake.ops if o[1] == 'insert']) == 1
    # Zweiter Lauf nach Memo-Ablauf: Dedupe trifft über die `hotel`-Spalte.
    db._dir_sync_memo.clear()
    fake2 = sync_env([[{'id': 's1'}]])
    assert db._sync_official_name('AT-U', 'NRT', 'Hilton Narita', DIRECTORY) is None
    assert fake2.ops == []
    # Dritter Lauf: Dedupe trifft erst über `official_name` (Crowd-Name wurde
    # inzwischen von Hand korrigiert) — auch dann kein zweiter Vorschlag.
    db._dir_sync_memo.clear()
    fake3 = sync_env([[], [{'id': 's1'}]])
    assert db._sync_official_name('AT-U', 'NRT', 'Hilton Narita', DIRECTORY) is None
    assert fake3.ops == []


# ── Der teure Fehler: zwei Häuser derselben Kette dürfen NIE verschmelzen ────
def test_two_hilton_houses_at_one_station_never_collapse():
    """Die zentrale Gefahr: „Hilton Garden Inn" und „Hilton Boston Back Bay"
    sind VERSCHIEDENE Häuser mit verschiedenen Fahrtzeiten. Ein zu grosszügiger
    Abgleich lieferte den richtigen Namen mit der falschen Zeit — schlimmer als
    gar keine Zeit, weil die Crew danach plant. Lieber zehn Treffer verpassen
    als einen falschen erzeugen."""
    d = [{'iata': 'BOS', 'hotel': 'Hilton Boston Back Bay',
          'transfer_min': 45, 'votes': 1},
         {'iata': 'BOS', 'hotel': 'Hilton Garden Inn Boston Logan',
          'transfer_min': 10, 'votes': 1}]
    # Jedes Haus trifft nur sich selbst — und zwar mit SEINER Zeit.
    assert db.transfer_match('BOS', 'Hilton Garden Inn Boston Logan', d) == {
        'row': d[1], 'transfer_min': 10, 'marker': '', 'matched': True,
        'reason': 'exact'}
    assert db.transfer_match('BOS', 'Hilton Boston Back Bay', d)['transfer_min'] == 45
    # Ein DRITTES Hilton erbt von keinem der beiden etwas.
    m3 = db.transfer_match('BOS', 'Hilton Boston Park Plaza', d)
    assert m3['transfer_min'] is None and m3['reason'] == 'ambiguous_multi_hotel'
    # Teilmengen-Falle, auch wenn nur EIN Haus im Verzeichnis steht: niemals
    # 'exact' — höchstens die als '*' markierte Destinations-Zeit.
    solo = [d[0]]
    m4 = db.transfer_match('BOS', 'Hilton Garden Inn', solo)
    assert m4['reason'] == 'destination_general' and m4['marker'] == '*'
    # …und am Block heisst das: LHs Name, aber sichtbar markierte Zeit.
    leg = _mk_leg(arr='BOS', hotel_name='Hilton Garden Inn')
    hb = db.hotel_block(_FRA_SHIFT, [leg], [leg], solo, [])
    assert hb['hotel'] == 'Hilton Garden Inn' and '(0:45*)' in hb['line']


def test_hotel_name_normalisation_diacritics_hyphen_and_prefix():
    """Schreibweisen, die dasselbe Haus meinen: Diakritika, Bindestrich,
    'Hotel'-Präfix. Sie dürfen einen Treffer nicht verhindern — mehr aber auch
    nicht (die Token-MENGE muss gleich bleiben, nicht nur überlappen)."""
    assert db._hotel_tokens('Hôtel Mercure Düsseldorf') == \
        db._hotel_tokens('Hotel Mercure Dusseldorf')
    assert db._hotel_tokens('IntercityHotel Wien-Schwechat') == \
        db._hotel_tokens('IntercityHotel Wien Schwechat')
    assert db._hotel_tokens('Hotel Sheraton Zagreb') == \
        db._hotel_tokens('Sheraton Zagreb')
    # Alle drei zusammen, als echter Treffer MIT Zeit:
    d = [{'iata': 'VIE', 'hotel': 'Hotel Mövenpick Wien-City',
          'transfer_min': 25, 'votes': 1}]
    m = db.transfer_match('VIE', 'Movenpick Wien City', d)
    assert m['reason'] == 'exact' and m['transfer_min'] == 25 and m['marker'] == ''
    # Ein zusätzliches Wort ist aber ein anderes Haus, kein Schreibfehler.
    m2 = db.transfer_match('VIE', 'Mövenpick Wien City Airport', d)
    assert m2['reason'] != 'exact'


def test_lh_placeholder_hotel_name_never_reaches_the_card():
    """LH schickt in Namensfeldern literal 'N/A' (beim briefingRoom live
    belegt) bzw. interne Codes. Ungegated stand „Hotel | N/A (0:30*)" auf der
    Karte: ein Platzhalter, der mit echter Fahrtzeit daneben wie ein Hotelname
    aussieht. Der Gate hängt VOR Anzeige, Rückfall-Scan und Hotel-Evidenz."""
    for bogus in ('N/A', 'NA', 'H9941671', 'TBD', '---', 'UNKNOWN'):
        leg = _mk_leg(arr='ZAG', hotel_name=bogus)
        hb = db.hotel_block(_FRA_SHIFT, [leg], [leg], DIRECTORY,
                            [('2026-07-27', 'ZAG')])
        assert hb is not None, bogus
        assert hb['hotel'] == 'Sheraton Zagreb'      # Verzeichnis-Rückfall
        assert hb['hotel_source'] == 'directory'
        assert bogus not in hb['line']
    # Ohne Hotel-Duty-Event ist ein Platzhalter auch KEIN Hotel-Beweis.
    leg = _mk_leg(arr='ZAG', hotel_name='N/A')
    assert db.hotel_block(_FRA_SHIFT, [leg], [leg], DIRECTORY, []) is None
    # Und der Rückfall-Scan über frühere Legs überspringt Platzhalter ebenfalls.
    early = _mk_leg(flight='LH100', arr='ZAG', hotel_name='H9941671',
                    dep_iso='2026-07-25T10:00:00Z', arr_iso='2026-07-25T12:00:00Z')
    late = _mk_leg(flight='LH101', arr='ZAG', hotel_name=None)
    hb2 = db.hotel_block(_FRA_SHIFT, [late], [early, late], DIRECTORY,
                         [('2026-07-27', 'ZAG')])
    assert hb2['hotel'] == 'Sheraton Zagreb' and hb2['hotel_source'] == 'directory'


def test_sync_is_fail_closed_without_recognised_airline(monkeypatch):
    """Ohne erkannte Airline (oder ohne Supabase) wird NICHTS geschrieben —
    dieselbe fail-closed-Linie wie _crew_hotel_dir_serve/_filter_crew_hotels.
    Der Cross-Airline-Leak vom 13.07. kam aus genau so einer fehlenden Sperre;
    ein Anreicherungs-Schreiber ohne Airline-Bindung träfe die falsche Airline."""
    import app as backend
    fake = _FakeSB([])
    monkeypatch.setattr(backend, 'sb', fake, raising=False)
    monkeypatch.setattr(backend, 'SB_AVAILABLE', True, raising=False)
    monkeypatch.setattr(backend, '_viewer_airline_and_calendar',
                        lambda t: ('', False))
    db._dir_sync_memo.clear()
    assert db._sync_official_name('AT-U', 'NRT', 'Hilton Narita', DIRECTORY) is None
    assert fake.ops == []
    monkeypatch.setattr(backend, '_viewer_airline_and_calendar',
                        lambda t: ('Lufthansa', True))
    monkeypatch.setattr(backend, 'SB_AVAILABLE', False, raising=False)
    db._dir_sync_memo.clear()
    assert db._sync_official_name('AT-U', 'NRT', 'Hilton Narita', DIRECTORY) is None
    assert fake.ops == []


def test_endpoint_enriches_only_lh_sourced_names(client, monkeypatch):
    """Am Endpoint: angereichert wird NUR, wenn der Name wirklich von LH kommt.
    Ein aus dem Verzeichnis gezogener Crowd-Name darf nie als „offizieller"
    Name zurückgeschrieben werden (das wäre eine erfundene LH-Bestätigung)."""
    seen = []
    monkeypatch.setattr(db, '_sync_official_name',
                        lambda tok, stn, name, d: seen.append((stn, name)))
    r = client.get(f'/api/ax/daily-briefing/AT-TEST?date={DATE}')
    assert r.status_code == 200
    assert r.get_json()['briefing']['hotel']['hotel_source'] == 'lh'
    assert seen == [('BUD', 'IntercityHotel Budapest')]
    # ZAG-Nacht: Name stammt aus dem Verzeichnis → kein Rückschreiben.
    db._cache.clear()
    seen.clear()
    r2 = client.get('/api/ax/daily-briefing/AT-TEST?date=2026-07-29')
    assert r2.status_code == 200
    assert r2.get_json()['briefing']['hotel']['hotel_source'] == 'directory'
    assert seen == []


def test_endpoint_without_airline_gets_no_directory_and_no_time(client, monkeypatch):
    """Fail-closed am Endpoint: ohne erkannte Airline wird das Verzeichnis gar
    nicht erst geladen — der Hotelname (LH) bleibt, die Fahrtzeit ist ehrlich
    N/A statt aus einer fremden Airline-Liste geraten."""
    import app as backend
    asked = []
    monkeypatch.setattr(backend, '_viewer_airline_and_calendar',
                        lambda t: ('', False))
    monkeypatch.setattr(backend, '_crew_hotel_dir_serve',
                        lambda a: asked.append(a) or DIRECTORY)
    db._cache.clear()
    r = client.get(f'/api/ax/daily-briefing/AT-TEST?date={DATE}')
    assert r.status_code == 200
    assert asked == []                        # gar nicht erst gefragt
    hb = r.get_json()['briefing']['hotel']
    assert hb['hotel'] == 'IntercityHotel Budapest'   # LH-Name bleibt
    assert hb['transfer_min'] is None                 # aber KEINE fremde Zeit


def test_sync_never_writes_into_a_foreign_airline_bucket(sync_env, monkeypatch):
    """`airline` stammt aus dem SELBSTGESETZTEN Profilfeld (`profile.airline`),
    der Endpoint gated nur auf einen gültigen FlightOps-Grant. Ein User mit
    LH-Grant und Profil „SWISS" hätte LH-Hotelnamen in den SWISS-Bucket
    geschrieben, wo echte SWISS-Crew sie zu sehen bekommt. Beim LESEN war die
    falsche Airline nur nutzlos — beim SCHREIBEN ist sie Datenvergiftung."""
    import app as backend
    for foreign in ('SWISS', 'Eurowings', 'ITA Airways', 'AeroWest'):
        db._dir_sync_memo.clear()
        fake = sync_env([[], []])
        monkeypatch.setattr(backend, '_viewer_airline_and_calendar',
                            lambda t, _a=foreign: (_a, True))
        assert db._sync_official_name('AT-U', 'NRT', 'Hilton Narita',
                                      DIRECTORY) is None
        assert fake.ops == [], foreign
    # LH-Group darf (Gegenprobe — sonst wäre der Test vacuous)
    for own in ('Lufthansa', 'Lufthansa City'):
        db._dir_sync_memo.clear()
        fake = sync_env([[], []])
        monkeypatch.setattr(backend, '_viewer_airline_and_calendar',
                            lambda t, _a=own: (_a, True))
        assert db._sync_official_name('AT-U', 'NRT', 'Hilton Narita',
                                      DIRECTORY) == 'suggested'
        assert [op for (_t, op, _p, _f) in fake.ops] == ['insert']


def test_machine_suggestion_is_not_attributed_to_a_human(sync_env):
    """Herkunft muss am Eintrag erkennbar bleiben. Mit einem User-Hash in
    `suggested_by` hätte die Vote-Promotion in /api/ax/crew-hotels/suggest die
    Maschinen-Zeile als „erste Stimme" gelesen — der ERSTE menschliche Tap
    hätte damit auf `approved` promotet (statt wie vorgesehen der zweite) und
    den bisherigen Eintrag deaktiviert."""
    import app as backend
    fake = sync_env([[], []])
    assert db._sync_official_name('AT-U', 'NRT', 'Hilton Narita',
                                  DIRECTORY) == 'suggested'
    _t, _op, payload, _f = fake.ops[0]
    assert payload['suggested_by'] == db._SUGGESTED_BY_MACHINE
    assert payload['suggested_by'] != backend._crew_hotel_token_hash('AT-U')
    assert payload['official_name_source'] == 'lh_flightops'   # Herkunft LH


def test_enrichment_survives_its_own_serve_substitution(sync_env):
    """Rückkopplung: der angereicherte Name wird vom Serve als `hotel`
    ausgeliefert und ist beim nächsten Lauf die Vergleichsbasis. Zeichen
    ausserhalb der Whitelist (typografisches ’, Halbgeviertstrich) wurden
    ersatzlos gelöscht und verklebten Wörter ('l’Opéra' → 'lOpéra') — der
    Treffer von gestern war am Folgetag keiner mehr, und aus einer sicheren
    Zeit wurde ein '*'-Richtwert plus Dauer-Konflikt-Log."""
    lh = 'Hôtel de l’Opéra Paris'
    crowd = [{'iata': 'CDG', 'hotel': 'Hotel de l Opera Paris',
              'transfer_min': 40, 'votes': 1}]
    m = db.transfer_match('CDG', lh, crowd)
    assert m['matched'] is True and m['transfer_min'] == 40
    fake = sync_env([[{'id': 'r1', 'official_name': None,
                       'hotel': 'Hotel de l Opera Paris'}]])
    assert db._sync_official_name('AT-U', 'CDG', lh, crowd) == 'enriched'
    written = fake.ops[0][2]['official_name']
    assert 'lOpéra' not in written and 'l Opéra' in written
    # Nächster Lauf: der Serve liefert den angereicherten Namen als `hotel`.
    served = [{'iata': 'CDG', 'hotel': written,
               'hotel_crowd': 'Hotel de l Opera Paris', 'official': True,
               'transfer_min': 40, 'votes': 1}]
    m2 = db.transfer_match('CDG', lh, served)
    assert m2['matched'] is True and m2['transfer_min'] == 40
    assert db.official_name_action(lh, m2) == (None, False)   # idempotent
