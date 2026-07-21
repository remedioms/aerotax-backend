"""ITA-Airways-Roster (ER-Duty/gigatools iCloud-Feed) — Parser + Pipeline.

Format-Wahrheit (echter Feed 2026-07-21, 1385 Events): Flüge „AZ650 FCO YYZ"
(space-separiert, KEIN Bindestrich), Zeiten = WANDUHR der jeweiligen Station,
aber mit TZID=Europe/Rome fehl-etikettiert (AZ614 FCO–BOS wäre sonst 3h57).
Deckt: I1 Stations-Zeit-Fix, I2 Report→Briefing-Token, I3 Pickup-Token,
I4 Layover-Synthese, I5 Hotel-Erkennung, I6 RISERVA→standby, Sektoren
(inkl. Reg aus DESCRIPTION, DH-Handling) und die Relevanz-Auswahl.
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as backend  # noqa: E402

_FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'fixtures', 'ita_synthetic.ics')


def _pipeline():
    events = backend._parse_ics_to_events(open(_FIXTURE).read())
    events = backend._swissify_roster_events(events)
    events = backend._itaify_roster_events(events)
    briefings, _ = backend._ics_events_to_briefings(events)
    backend._attach_sectors(briefings, events)
    return events, briefings


# ── Parser-Bausteine ─────────────────────────────────────────────────────────

def test_ita_flight_regex_variants():
    assert backend._ics_parse_ita_flight('AZ650 FCO YYZ') == ('AZ650', 'FCO', 'YYZ', False)
    assert backend._ics_parse_ita_flight('DH AZ1740 FCO CTA') == ('AZ1740', 'FCO', 'CTA', True)
    assert backend._ics_parse_ita_flight('DH AF809 FDF CDG') == ('AF809', 'FDF', 'CDG', True)
    # Boden-Transfer + LH-Prosa + SWISS matchen NICHT.
    assert backend._ics_parse_ita_flight('DH SUPERF LIN FCO')[1] is None
    assert backend._ics_parse_ita_flight('LH 390: FRA-LUX')[1] is None
    assert backend._ics_parse_ita_flight('LX1270 ZRH 1236 CPH 1413 32B')[1] is None


def test_ita_desc_reg():
    assert backend._ita_desc_reg('339/EIEJO') == 'EI-EJO'
    assert backend._ita_desc_reg('32N/EIHJD') == 'EI-HJD'
    # Kein EI-Prefix / kein Muster → nie raten.
    assert backend._ita_desc_reg('32N/DAIXS') is None
    assert backend._ita_desc_reg('Reporting time: 11:15') is None


def test_ev_is_flight_leg_ita():
    assert backend._ev_is_flight_leg('AZ650 FCO YYZ')
    assert backend._ev_is_flight_leg('AZ650 FCO YYZ (Tag 1/2)')
    assert not backend._ev_is_flight_leg('DH AZ1740 FCO CTA')   # Deadhead ≠ Block
    assert not backend._ev_is_flight_leg('RISERVA LINEA')
    assert not backend._ev_is_flight_leg('LAYOVER · CHELSEA HOTEL Toronto')


# ── I1: Stations-Zeit-Fix (die Kern-Falle) ───────────────────────────────────

def test_retiming_yyz_flight_true_utc():
    events, _ = _pipeline()
    out = next(e for e in events if (e.get('summary') or '').startswith('AZ650'))
    # Dep 10:15 Rome-Wanduhr = 08:15Z; Arr 14:05 TORONTO-Wanduhr (EDT) = 18:05Z.
    assert out['start_iso'] == '2026-07-22T08:15:00Z'
    assert out['end_iso'] == '2026-07-22T18:05:00Z'
    # Block ≈ 9h50 — unter Rome-Fehlinterpretation wären es 3h50.
    assert backend._iso_minutes_between(out['start_iso'], out['end_iso']) == 590


def test_retiming_redeye_return_multiday():
    events, briefings = _pipeline()
    back = next(e for e in events if (e.get('summary') or '').startswith('AZ651'))
    # Dep 17:00 Toronto = 21:00Z; Arr 07:40 Rome (Folgetag) = 05:40Z.
    assert back['start_iso'] == '2026-07-23T21:00:00Z'
    assert back['end_iso'] == '2026-07-24T05:40:00Z'
    # Wanduhr-Buckets: Abflugtag 23., Ankunftstag 24. (Tag 2/2 beim Briefing).
    assert back['start'] == '2026-07-23' and back['end'] == '2026-07-24'
    assert '(Tag 2/2)' in (briefings['2026-07-24'].get('ical_summary') or '')


# ── I2/I3: Report- und Pickup-Token ──────────────────────────────────────────

def test_report_becomes_briefing_token():
    _, briefings = _pipeline()
    s22 = briefings['2026-07-22'].get('ical_summary') or ''
    assert '08:30 LT Briefing FCO' in s22
    s23 = briefings['2026-07-23'].get('ical_summary') or ''
    # Report YYZ 15:45 = Toronto-Wanduhr → Token trägt die Wanduhr-Zeit.
    assert '15:45 LT Briefing YYZ' in s23


def test_pickup_token_with_wall_time():
    _, briefings = _pipeline()
    assert 'Pickup 13:40' in (briefings['2026-07-23'].get('ical_summary') or '')


# ── I4/I5: Layover-Synthese + Hotel ──────────────────────────────────────────

def test_layover_synth_sets_ort_and_hotel_prefix():
    events, briefings = _pipeline()
    assert briefings['2026-07-22'].get('ical_layover_ort') == 'YYZ'
    assert briefings['2026-07-23'].get('ical_layover_ort') == 'YYZ'
    # Heimkehr-Morgen (24.) bekommt KEINEN Layover-Ort (noon-span-Regel).
    assert briefings.get('2026-07-24', {}).get('ical_layover_ort') != 'YYZ'
    hotel = next(e for e in events if 'CHELSEA' in (e.get('summary') or ''))
    assert hotel['summary'].startswith('LAYOVER · ')
    # Hotel-Ende (Pickup-Morgen) darf das Duty-Fenster NICHT aufblähen:
    assert not backend._ev_extends_duty(hotel['summary'])


def test_hotel_does_not_inflate_duty_end():
    _, briefings = _pipeline()
    # Tag 22: Duty-Ende = Flug-Ankunft 18:05Z, NICHT Hotel-Ende am 23.
    assert (briefings['2026-07-22'].get('ical_end_iso') or '') == '2026-07-22T18:05:00Z'


# ── I6: RISERVA → standby ────────────────────────────────────────────────────

def test_riserva_is_standby():
    _, briefings = _pipeline()
    assert briefings['2026-07-26'].get('ical_klass') == 'standby'


# ── Sektoren ─────────────────────────────────────────────────────────────────

def test_sectors_with_tail_and_dh():
    _, briefings = _pipeline()
    secs22 = briefings['2026-07-22'].get('ical_sectors') or []
    assert [(s['flight'], s['from'], s['to']) for s in secs22] == [('AZ650', 'FCO', 'YYZ')]
    assert secs22[0].get('tail') == 'EI-EJO'
    # Red-Eye-Rückflug keyed auf den ABFLUG-Wanduhr-Tag (23.), nicht UTC-Folgetag.
    secs23 = briefings['2026-07-23'].get('ical_sectors') or []
    assert [(s['flight'], s['from'], s['to']) for s in secs23] == [('AZ651', 'YYZ', 'FCO')]
    assert secs23[0].get('tail') == 'EI-TYH'
    # DH AZ = echter Sektor (Crew sitzt drin, DH-Marker fällt wie im LH-Pfad);
    # DH SUPERF (Boden) erzeugt NIE einen Pseudo-Flug-Sektor.
    secs27 = briefings.get('2026-07-27', {}).get('ical_sectors') or []
    assert [(s['flight'], s['from'], s['to']) for s in secs27] == [('AZ1740', 'FCO', 'CTA')]


def test_block_minutes_only_operating_legs():
    _, briefings = _pipeline()
    assert briefings['2026-07-22'].get('block_minutes') == 590
    # DH-Tag: kein Block.
    assert (briefings.get('2026-07-27', {}).get('block_minutes') or 0) == 0


def test_legs_list_for_ita_day():
    _, briefings = _pipeline()
    legs = briefings['2026-07-22'].get('legs') or []
    assert any(l.get('flight') == 'AZ650' and l.get('from') == 'FCO'
               and l.get('to') == 'YYZ' for l in legs)


# ── Gate: kein Einfluss auf fremde Feeds ─────────────────────────────────────

def test_itaify_noop_for_lh_and_swiss():
    lh = [{'summary': 'LH 390: FRA-LUX', 'location': 'FRA',
           'start_iso': '2026-07-22T08:00:00Z', 'end_iso': '2026-07-22T09:00:00Z',
           'start': '2026-07-22', 'end': '2026-07-22'}]
    before = [dict(e) for e in lh]
    assert backend._itaify_roster_events(lh) == before
    sw = [{'summary': 'LX1270 ZRH 1236 CPH 1413 32B', 'location': '',
           'start_iso': '2026-07-22T10:36:00Z', 'end_iso': '2026-07-22T12:13:00Z',
           'start': '2026-07-22', 'end': '2026-07-22'}]
    before_sw = [dict(e) for e in sw]
    assert backend._itaify_roster_events(sw) == before_sw


# ── Relevanz-Auswahl (Jahres-Feeds) ──────────────────────────────────────────

def _mk_ev(day_offset):
    d = (datetime.now() + timedelta(days=day_offset)).strftime('%Y-%m-%d')
    return {'summary': f'AZ100 FCO LIN', 'start': d, 'end': d,
            'start_iso': f'{d}T08:00:00Z', 'end_iso': f'{d}T09:00:00Z'}


def test_select_relevant_keeps_current_over_ancient():
    # 400 Alt-Events (vor 2 Jahren) VOR 50 aktuellen im Feed — der alte
    # Prefix-Slice hätte alle aktuellen verworfen.
    ancient = [_mk_ev(-700 - i) for i in range(400)]
    current = [_mk_ev(i) for i in range(50)]
    sel = backend._select_relevant_feed_events(ancient + current, 300)
    assert len(sel) == 300
    sel_days = {e['start'] for e in sel}
    for e in current:
        assert e['start'] in sel_days
    # Rest-Plätze = JÜNGSTE Vergangenheit (die ältesten fliegen raus).
    oldest_kept = min(e['start'] for e in sel)
    assert oldest_kept > ancient[-1]['start'] or len(ancient) <= 250


def test_select_relevant_noop_under_cap():
    evs = [_mk_ev(i) for i in range(10)]
    assert backend._select_relevant_feed_events(list(evs), 300) == evs
