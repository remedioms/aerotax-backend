"""Dienstplan-Push: konkreter Inhalt + Vergangenheits-/Pickup-Gates (Flo Z,
FO, 2026-07-20):

  (a) „WAS sich geändert hat, sieht man nicht" → der Push-Body nennt jetzt die
      erste konkrete Änderung („Mi 22.07: LH440 FRA-IAH neu" / „Di 21.07:
      Briefing 09:40 → 10:15") + „(+N weitere)". Formatter:
      _roster_changes_push_body / _roster_change_push_line. Max ~120 Zeichen.
  (b) „Push kommt auch, wenn die Tour vorbei ist" → Push-Gates in
      take_roster_snapshot:
        • _roster_change_is_past: Tage VOR heute (Homebase-lokal) pushen nicht.
        • _roster_change_is_push_worthy → _rc_meaningfully_modified: das EINE
          Substanz-Gate (2026-07-28). Die früheren Einzelfunktionen
          _roster_change_is_pickup_prune und _roster_change_is_blocktime_drift
          sind darin aufgegangen und ersatzlos entfallen.

KEIN echtes APNs/SB: _push_notify_async & Co. werden gemockt (Muster
test_duty_change_push.py).
"""
import json
import os
import sys

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

import app as A

TODAY = '2026-07-20'          # Montag (fixe Referenz für die Gate-Unit-Tests)
YESTERDAY = '2026-07-19'
TOMORROW = '2026-07-21'


def _sector(flight='LH 440', frm='FRA', to='IAH',
            dep='2026-07-22T08:00:00Z', arr='2026-07-22T18:30:00Z'):
    return {'flight': flight, 'from': frm, 'to': to,
            'dep_iso': dep, 'arr_iso': arr}


# ══════════════════════════════════════════════════════════════════════════════
# Diff-Formatter: _roster_change_push_line / _roster_changes_push_body
# ══════════════════════════════════════════════════════════════════════════════
def test_push_line_added_names_flight_and_route():
    ch = {'kind': 'added', 'datum': '2026-07-22',
          'new': {'ical_sectors': [_sector()], 'routing': 'FRA-IAH'}}
    assert A._roster_change_push_line(ch) == 'LH440 FRA-IAH neu'
    # 2026-07-22 ist ein Mittwoch.
    assert A._roster_changes_push_body([ch]) == 'Mi 22.07: LH440 FRA-IAH neu'


def test_push_line_added_without_sectors_falls_back():
    ch = {'kind': 'added', 'datum': '2026-07-22', 'new': {'routing': 'FRA-IAH'}}
    assert A._roster_change_push_line(ch) == 'FRA-IAH neu'
    assert A._roster_change_push_line({'kind': 'added', 'new': {}}) == 'Neuer Dienst'


def test_push_line_removed():
    ch = {'kind': 'removed', 'datum': '2026-07-23',
          'old': {'ical_sectors': [_sector()]}}
    # 2026-07-23 ist ein Donnerstag.
    assert A._roster_changes_push_body([ch]) == 'Do 23.07: Dienst entfernt'


def test_push_line_briefing_time_change():
    ch = {'kind': 'modified', 'datum': TOMORROW,
          'old': {'routing': 'FRA-JFK', 'reader_facts': {'start_time': '09:40'}},
          'new': {'routing': 'FRA-JFK', 'reader_facts': {'start_time': '10:15'}}}
    assert A._roster_change_push_line(ch) == 'Briefing 09:40 → 10:15'
    assert A._roster_changes_push_body([ch]) == 'Di 21.07: Briefing 09:40 → 10:15'


def test_push_line_route_change_beats_times():
    ch = {'kind': 'modified', 'datum': TOMORROW,
          'old': {'routing': 'FRA-JFK', 'reader_facts': {'start_time': '09:40'}},
          'new': {'routing': 'FRA-MIA', 'reader_facts': {'start_time': '10:15'}}}
    assert A._roster_change_push_line(ch) == 'Route FRA-JFK → FRA-MIA'


def test_push_line_leg_departure_time_change_local():
    old = {'ical_sectors': [_sector(dep='2026-07-22T08:00:00Z')]}
    new = {'ical_sectors': [_sector(dep='2026-07-22T08:30:00Z')]}
    line = A._roster_change_push_line(
        {'kind': 'modified', 'old': old, 'new': new})
    # FRA-lokal (UTC+2 im Juli): 10:00 → 10:30.
    assert line == 'LH440 Abflug 10:00 → 10:30'


def test_push_body_counts_further_changes():
    chs = [{'kind': 'removed', 'datum': '2026-07-22', 'old': {}},
           {'kind': 'removed', 'datum': '2026-07-23', 'old': {}},
           {'kind': 'removed', 'datum': '2026-07-24', 'old': {}}]
    body = A._roster_changes_push_body(chs)
    assert body == 'Mi 22.07: Dienst entfernt (+2 weitere)'


def test_push_body_capped_at_120_chars():
    long_route = '-'.join(['FRA', 'JFK', 'MIA', 'GRU', 'EZE', 'SCL'] * 6)
    ch = {'kind': 'modified', 'datum': '2026-07-22',
          'old': {'routing': long_route}, 'new': {'routing': long_route + '-BOG'}}
    body = A._roster_changes_push_body([ch])
    assert len(body) <= 120 and body.endswith('…')


def test_push_body_never_throws_on_garbage():
    assert isinstance(A._roster_changes_push_body(None), str)
    assert isinstance(A._roster_changes_push_body([{'kind': 'modified'}]), str)
    assert isinstance(A._roster_change_push_line({}), str)
    assert A._roster_change_push_line(
        {'kind': 'modified', 'old': None, 'new': None}) == 'Dienst geändert'


# ══════════════════════════════════════════════════════════════════════════════
# Gate 1: Vergangenheit (_roster_change_is_past)
# ══════════════════════════════════════════════════════════════════════════════
def test_past_gate_yesterday_is_past():
    assert A._roster_change_is_past({'datum': YESTERDAY}, TODAY) is True


def test_past_gate_today_and_future_are_not_past():
    assert A._roster_change_is_past({'datum': TODAY}, TODAY) is False
    assert A._roster_change_is_past({'datum': TOMORROW}, TODAY) is False


def test_past_gate_defensive_on_missing_data():
    assert A._roster_change_is_past({}, TODAY) is False
    assert A._roster_change_is_past({'datum': YESTERDAY}, None) is False
    assert A._roster_change_is_past(None, TODAY) is False


# ══════════════════════════════════════════════════════════════════════════════
# Gate 2: Pickup-Rauschen — jetzt Teil des EINEN Substanz-Gates
# (_roster_change_is_push_worthy → _rc_meaningfully_modified). Die früheren
# Einzelfunktionen _roster_change_is_pickup_prune / _..._is_blocktime_drift
# sind 2026-07-28 ersatzlos entfallen: drei Implementierungen derselben Frage
# widersprachen sich (Owner-Fall 30.07.).
# ══════════════════════════════════════════════════════════════════════════════
def _day_with_pickup(pickup_marker='Pickup 1330', start='13:30',
                     dep='2026-07-22T12:00:00Z'):
    return {'datum': '2026-07-22', 'klass': 'Flug', 'routing': 'FRA-IAH',
            'marker': pickup_marker,
            'ical_sectors': [_sector(dep=dep)],
            'reader_facts': {'start_time': start, 'end_time': '19:00',
                             'layover_ort': 'IAH'}}


def _mod(old, new):
    return {'kind': 'modified', 'datum': '2026-07-22', 'old': old, 'new': new}


def test_pickup_abbau_ist_kein_push():
    # LH räumt die PU-Zeit ab: Marker verliert 'Pickup 1330', Start fällt von
    # der Pickup- (13:30) auf die Briefing-Zeit (14:30) zurück — sonst nichts.
    old = _day_with_pickup()
    new = _day_with_pickup(pickup_marker='', start='14:30')
    assert A._roster_change_is_push_worthy(_mod(old, new)) is False


def test_pickup_abbau_mit_abflug_shift_bleibt_im_verlauf_pusht_aber_nicht():
    # GATE 4 (2026-07-28): PU-Abbau + 1 h Abflug-Shift bei IDENTISCHER
    # Leg-Struktur ist eine reine Zeit-Änderung → Verlauf ja, Push nein.
    old = _day_with_pickup()
    new = _day_with_pickup(pickup_marker='', start='14:30',
                           dep='2026-07-22T13:00:00Z')     # Leg verschoben
    assert A._rc_meaningfully_modified(old, new) is True
    assert A._roster_change_is_push_worthy(_mod(old, new)) is False


def test_pickup_abbau_mit_endzeit_pflege_bleibt_still():
    # `end_time` ist seit 2026-07-28 gar keine Substanz mehr (LH pflegt sie
    # nach jeder Landung) — zusammen mit dem PU-Abbau also weiterhin still.
    old = _day_with_pickup()
    new = _day_with_pickup(pickup_marker='', start='14:30')
    new['reader_facts']['end_time'] = '20:15'
    assert A._roster_change_is_push_worthy(_mod(old, new)) is False
    # Ein anderer Layover-Ort dagegen ist echte Substanz.
    lay = _day_with_pickup(pickup_marker='', start='14:30')
    lay['reader_facts'] = dict(lay['reader_facts'], layover_ort='BOS')
    assert A._roster_change_is_push_worthy(_mod(old, lay)) is True


def test_pickup_praesenz_flip_ist_still_pu_shift_nur_noch_im_verlauf():
    # OWNER-REGEL 2026-07-28: das Auftauchen/Verschwinden der PU-Zeit ohne
    # Zeitänderung am Dienst ist STILL (Flip-Flop-Signatur).
    ohne = _day_with_pickup(pickup_marker='', start='14:30')
    mit = _day_with_pickup()
    assert A._roster_change_is_push_worthy(_mod(ohne, mit)) is False
    assert A._roster_change_is_push_worthy(_mod(mit, ohne)) is False
    # Eine echte PU-VERSCHIEBUNG ≥ 5 min bleibt eine Verlauf-Änderung, pusht
    # seit GATE 4 aber nicht mehr (reine Zeit, Struktur identisch).
    pu_shift = _mod(_day_with_pickup(),
                    _day_with_pickup(pickup_marker='Pickup 1400',
                                     start='14:00'))
    assert A._rc_meaningfully_modified(pu_shift['old'], pu_shift['new']) is True
    assert A._roster_change_is_push_worthy(pu_shift) is False
    # … und eine 3-Minuten-PU-Korrektur ist schon im Verlauf keine Änderung
    # (Toleranz _RC_TIME_TOL_MIN).
    assert A._roster_change_is_push_worthy(
        _mod(_day_with_pickup(),
             _day_with_pickup(pickup_marker='Pickup 1333',
                              start='13:33'))) is False


def test_push_worthy_defensive():
    assert A._roster_change_is_push_worthy({}) is True       # fail-open
    assert A._roster_change_is_push_worthy(None) is True
    assert A._roster_change_is_push_worthy(
        {'kind': 'added', 'datum': '2026-07-22', 'new': {}}) is False


def test_rc_pickup_hhmm_sources():
    assert A._rc_pickup_hhmm({'pickup': '9:05'}) == '09:05'
    assert A._rc_pickup_hhmm({'marker': 'Pickup 1430'}) == '14:30'
    assert A._rc_pickup_hhmm({'ical_summary': '09:30 LT Pickup HND'}) == '09:30'
    assert A._rc_pickup_hhmm({'marker': 'Briefing 0900'}) == ''
    assert A._rc_pickup_hhmm(None) == ''


# ══════════════════════════════════════════════════════════════════════════════
# Gate 3: Blockzeiten-Drift (Florian 2026-07-24) — als Regel im Substanz-Gate
# ══════════════════════════════════════════════════════════════════════════════
def test_blocktime_drift_end_time_only_not_pushworthy():
    # LH pflegt Blockzeiten: Legs/PU/Briefing/erster Abflug identisch, nur das
    # Dienst-Ende driftet um Minuten → kein Push.
    old = _day_with_pickup()
    new = _day_with_pickup()
    new['reader_facts'] = dict(new['reader_facts'], end_time='19:12')
    assert A._roster_change_is_push_worthy(_mod(old, new)) is False


def test_blocktime_drift_arrival_iso_drift_not_pushworthy():
    # Nur die Ankunfts-ISO des Legs driftet (Struktur + Abflug gleich).
    old = _day_with_pickup()
    new = _day_with_pickup()
    new['ical_sectors'] = [_sector(dep='2026-07-22T12:00:00Z',
                                   arr='2026-07-22T18:47:00Z')]
    new['reader_facts'] = dict(new['reader_facts'], end_time='19:17')
    assert A._roster_change_is_push_worthy(_mod(old, new)) is False


def test_blocktime_drift_first_departure_change_nur_noch_verlauf():
    # Der Abflug verschiebt sich um 30 min: echte Änderung für den VERLAUF,
    # seit GATE 4 aber kein Push mehr (< 3 h, Struktur identisch).
    old = _day_with_pickup()
    new = _day_with_pickup(dep='2026-07-22T12:30:00Z')
    new['reader_facts'] = dict(new['reader_facts'], end_time='19:30')
    assert A._rc_meaningfully_modified(old, new) is True
    assert A._roster_change_is_push_worthy(_mod(old, new)) is False


def test_departure_drift_unter_toleranz_bleibt_still():
    # 3 Minuten Abflug-Korrektur = Zeitenpflege, kein Push (Owner-Regel:
    # erst ab 5 min). Vorher feuerte JEDE Minute.
    old = _day_with_pickup()
    new = _day_with_pickup(dep='2026-07-22T12:03:00Z')
    assert A._roster_change_is_push_worthy(_mod(old, new)) is False


def test_blocktime_drift_pickup_change_nur_noch_verlauf():
    old = _day_with_pickup()
    new = _day_with_pickup(pickup_marker='Pickup 1400', start='14:00')
    new['reader_facts'] = dict(new['reader_facts'], end_time='19:12')
    assert A._rc_meaningfully_modified(old, new) is True
    assert A._roster_change_is_push_worthy(_mod(old, new)) is False


def test_blocktime_drift_route_change_is_pushworthy():
    old = _day_with_pickup()
    new2 = _day_with_pickup()
    new2['routing'] = 'FRA-MIA'                            # Route geändert
    assert A._roster_change_is_push_worthy(_mod(old, new2)) is True


def test_briefing_drift_bei_stehender_pickup_bleibt_still():
    # P7-Entscheid bewahrt: steht die PU-Zeit, ist SIE die tragende Zeit —
    # eine Briefing-Minutenpflege darunter ist kein Push.
    old = _day_with_pickup()
    new = _day_with_pickup(start='14:00')
    assert A._roster_change_is_push_worthy(_mod(old, new)) is False


def test_blocktime_drift_leg_structure_change_is_pushworthy():
    old = _day_with_pickup()
    new = _day_with_pickup()
    new['ical_sectors'] = [_sector(flight='LH 441')]       # andere Flugnummer
    assert A._roster_change_is_push_worthy(_mod(old, new)) is True


def test_endzeit_pflege_ohne_legs_bleibt_still():
    # Loch B: auch OHNE Legs (Standby/Boden) ist reine end_time-Pflege still.
    old = {'datum': '2026-07-22', 'klass': 'Standby',
           'reader_facts': {'start_time': '09:00', 'end_time': '17:00'}}
    new = dict(old, reader_facts={'start_time': '09:00', 'end_time': '18:00'})
    assert A._roster_change_is_push_worthy(_mod(old, new)) is False
    # Der Standby-BEGINN steht im Verlauf, pusht seit GATE 4 aber nicht mehr:
    # der Dienst selbst (Standby, kein Flug) ist derselbe.
    shift = dict(old, reader_facts={'start_time': '11:00', 'end_time': '18:00'})
    assert A._rc_meaningfully_modified(old, shift) is True
    assert A._roster_change_is_push_worthy(_mod(old, shift)) is False


# ══════════════════════════════════════════════════════════════════════════════
# Gate 4: „nur die DIENST-Substanz pusht" (Owner-Entscheid 2026-07-28)
#
# Owner, wörtlich: „Das Einzige, was aufpoppen darf, ist wirklich, wenn ich einen
# komplett neuen Flug habe — keine Verspätungen, keine Gate-Wechsel, keine
# Briefing-/Abflugzeit-Verschiebungen. Uns interessiert nur, ob sich der DIENST
# geändert hat: wenn ich San Francisco hatte und jetzt LA habe, ist das wichtig.
# Oder eine riesige Verspätung (~4 h), WENN ich meinen Dienst noch nicht
# angetreten habe."
#
# Der VERLAUF bleibt vollständig — nur der Push wird gefiltert (wie Gate 3).
# ══════════════════════════════════════════════════════════════════════════════
_G4_DAY = '2026-08-02'


def _g4_day(flight='LH454', frm='FRA', to='SFO', dep='2026-08-02T09:00:00Z',
            arr='2026-08-02T20:00:00Z', start='08:00', layover='SFO',
            marker='08:00 LT Briefing FRA', klass='Flug'):
    return {'datum': _G4_DAY, 'klass': klass, 'routing': f'{frm}-{to}',
            'marker': marker,
            'ical_sectors': [{'flight': flight, 'from': frm, 'to': to,
                              'dep_iso': dep, 'arr_iso': arr}],
            'reader_facts': {'start_time': start, 'end_time': '20:30',
                             'layover_ort': layover}}


def _g4_mod(old, new):
    return {'kind': 'modified', 'datum': _G4_DAY, 'old': old, 'new': new}


# „Jetzt"-Zeitpunkte relativ zum Dienstbeginn (08:00 FRA-lokal = 06:00 UTC).
_G4_VOR_DIENST = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
_G4_IM_DIENST = datetime(2026, 8, 2, 7, 0, tzinfo=timezone.utc)


def test_gate4_briefing_shift_bleibt_im_verlauf_pusht_nicht():
    # Der Forum-Fall („Sa 08.08: Briefing 00:35 → 01:05"): Struktur identisch,
    # nur die Meldezeit rutscht → Verlauf ja, Push nein.
    old = _g4_day(start='00:35', marker='00:35 LT Briefing FRA')
    new = _g4_day(start='01:05', marker='01:05 LT Briefing FRA')
    assert A._rc_meaningfully_modified(old, new) is True
    assert A._roster_change_is_push_worthy(_g4_mod(old, new),
                                           now=_G4_VOR_DIENST) is False


def test_gate4_abflug_shift_unter_drei_stunden_pusht_nicht():
    # „LH454 Abflug 10:55 → 10:25" — 30 min, identische Legs → still, egal ob
    # der Dienst schon läuft oder nicht.
    old = _g4_day(dep='2026-08-02T08:55:00Z')
    new = _g4_day(dep='2026-08-02T08:25:00Z')
    assert A._rc_meaningfully_modified(old, new) is True
    for _now in (_G4_VOR_DIENST, _G4_IM_DIENST):
        assert A._roster_change_is_push_worthy(_g4_mod(old, new),
                                               now=_now) is False


def test_gate4_pingpong_ist_in_beide_richtungen_still():
    # Der Live-Fall vom 28.07.: zwei Pushes binnen 30 Sekunden, A→B und
    # B→A. Beide Richtungen sind reine Zeit → beide still. Es braucht KEINE
    # zusätzliche Dedup-Logik.
    a = _g4_day(dep='2026-08-02T08:55:00Z')
    b = _g4_day(dep='2026-08-02T08:25:00Z')
    assert A._roster_change_is_push_worthy(_g4_mod(a, b),
                                           now=_G4_VOR_DIENST) is False
    assert A._roster_change_is_push_worthy(_g4_mod(b, a),
                                           now=_G4_VOR_DIENST) is False
    # … und auch als 4-h-Ping-Pong (Ausnahme greift) bleibt es symmetrisch:
    # beide Richtungen pushen dann, aber DAS fängt die Flip-Flop-Hysterese ab.
    weit = _g4_day(dep='2026-08-02T13:00:00Z')
    assert A._roster_change_is_push_worthy(_g4_mod(a, weit),
                                           now=_G4_VOR_DIENST) is True
    assert A._roster_change_is_push_worthy(_g4_mod(weit, a),
                                           now=_G4_VOR_DIENST) is True


def test_gate4_gate_und_blockzeiten_bleiben_still():
    # Ankunft/Blockzeit/Dienstende — die klassische LH-Zeitenpflege.
    old = _g4_day()
    new = _g4_day(arr='2026-08-02T20:47:00Z')
    new['reader_facts'] = dict(new['reader_facts'], end_time='21:17')
    assert A._roster_change_is_push_worthy(_g4_mod(old, new),
                                           now=_G4_VOR_DIENST) is False


def test_gate4_anderes_ziel_pusht():
    # „Ich hatte San Francisco und jetzt habe ich LA" — genau das soll poppen.
    old = _g4_day(to='SFO', layover='SFO')
    new = _g4_day(to='LAX', layover='LAX')
    assert A._roster_change_is_push_worthy(_g4_mod(old, new),
                                           now=_G4_VOR_DIENST) is True
    # Auch wenn der Dienst schon läuft (Umleitung im Dienst = harte Info).
    assert A._roster_change_is_push_worthy(_g4_mod(old, new),
                                           now=_G4_IM_DIENST) is True


def test_gate4_andere_flugnummer_und_neuer_leg_pushen():
    old = _g4_day()
    assert A._roster_change_is_push_worthy(
        _g4_mod(old, _g4_day(flight='LH456')), now=_G4_VOR_DIENST) is True
    zwei_legs = _g4_day()
    zwei_legs['ical_sectors'] = list(zwei_legs['ical_sectors']) + [
        {'flight': 'LH455', 'from': 'SFO', 'to': 'FRA',
         'dep_iso': '2026-08-03T22:00:00Z', 'arr_iso': '2026-08-04T08:00:00Z'}]
    assert A._roster_change_is_push_worthy(_g4_mod(old, zwei_legs),
                                           now=_G4_VOR_DIENST) is True


def test_gate4_layover_und_routing_wechsel_pushen():
    old = _g4_day()
    lay = _g4_day()
    lay['reader_facts'] = dict(lay['reader_facts'], layover_ort='OAK')
    assert A._roster_change_is_push_worthy(_g4_mod(old, lay),
                                           now=_G4_VOR_DIENST) is True
    # Routing-Feld ohne Sektoren (Feeds ohne ical_sectors).
    o = {'datum': _G4_DAY, 'klass': 'Flug', 'routing': 'FRA-SFO'}
    n = {'datum': _G4_DAY, 'klass': 'Flug', 'routing': 'FRA-LAX'}
    assert A._roster_change_is_push_worthy(_g4_mod(o, n),
                                           now=_G4_VOR_DIENST) is True


def test_gate4_dienst_kommt_und_geht_pusht_weiter():
    # Flug wird Standby (Flug-Beleg verschwindet in einen dokumentierten Tag).
    old = _g4_day()
    standby = {'datum': _G4_DAY, 'klass': 'Standby', 'routing': '',
               'reader_facts': {'start_time': '08:00', 'end_time': '16:00'}}
    assert A._roster_change_is_push_worthy(_g4_mod(old, standby),
                                           now=_G4_VOR_DIENST) is True
    # Frei → Dienst.
    frei = {'datum': _G4_DAY, 'klass': 'FREI'}
    assert A._roster_change_is_push_worthy(_g4_mod(frei, standby),
                                           now=_G4_VOR_DIENST) is True


def test_gate4_riesenverspaetung_vor_dienstantritt_pusht():
    # 4 h später, Crew ist noch zu Hause → das ist die eine Zeit-Änderung,
    # die zählt.
    old = _g4_day(dep='2026-08-02T09:00:00Z')
    new = _g4_day(dep='2026-08-02T13:00:00Z')
    assert A._roster_change_is_push_worthy(_g4_mod(old, new),
                                           now=_G4_VOR_DIENST) is True


def test_gate4_riesenverspaetung_nach_dienstantritt_bleibt_still():
    # Dieselbe Verschiebung, aber der Dienst läuft schon (07:00 UTC >
    # Dienstbeginn 06:00 UTC) → die Crew steht am Flughafen und erfährt es dort.
    old = _g4_day(dep='2026-08-02T09:00:00Z')
    new = _g4_day(dep='2026-08-02T13:00:00Z')
    assert A._roster_change_is_push_worthy(_g4_mod(old, new),
                                           now=_G4_IM_DIENST) is False


def test_gate4_riesenverspaetung_schwelle_ist_180_minuten():
    old = _g4_day(dep='2026-08-02T09:00:00Z')
    knapp = _g4_day(dep='2026-08-02T11:59:00Z')          # 179 min
    genau = _g4_day(dep='2026-08-02T12:00:00Z')          # 180 min
    assert A._roster_change_is_push_worthy(_g4_mod(old, knapp),
                                           now=_G4_VOR_DIENST) is False
    assert A._roster_change_is_push_worthy(_g4_mod(old, genau),
                                           now=_G4_VOR_DIENST) is True


def test_gate4_grosse_vorverlegung_pusht_ebenfalls():
    # 3 h FRÜHER ist für die Anreise noch kritischer als 3 h später.
    old = _g4_day(dep='2026-08-02T13:00:00Z', start='12:00',
                  marker='12:00 LT Briefing FRA')
    new = _g4_day(dep='2026-08-02T09:00:00Z', start='12:00',
                  marker='12:00 LT Briefing FRA')
    assert A._roster_change_is_push_worthy(_g4_mod(old, new),
                                           now=_G4_VOR_DIENST) is True


def test_gate4_added_und_removed_unveraendert():
    # Gate 4 fasst added/removed NICHT an.
    assert A._roster_change_is_push_worthy(
        {'kind': 'added', 'datum': _G4_DAY, 'new': _g4_day()}) is True
    assert A._roster_change_is_push_worthy(
        {'kind': 'removed', 'datum': _G4_DAY, 'old': _g4_day()}) is True
    assert A._roster_change_is_push_worthy(
        {'kind': 'added', 'datum': _G4_DAY, 'new': {}}) is False


def test_gate4_defensiv():
    assert A._rc_push_duty_substance_changed(None, None) is False
    assert A._rc_push_duty_substance_changed({}, {}) is False
    assert A._rc_huge_delay_before_duty({}, {}) is False
    assert A._rc_huge_delay_before_duty(_g4_day(), {'ical_sectors': []}) is False
    # Naives „now" wird als UTC gelesen (kein TypeError beim Vergleich).
    assert A._roster_change_is_push_worthy(
        _g4_mod(_g4_day(dep='2026-08-02T09:00:00Z'),
                _g4_day(dep='2026-08-02T13:00:00Z')),
        now=datetime(2026, 8, 1, 12, 0)) is True


def test_rc_duty_start_utc_quellen():
    # 08:00 FRA-lokal (UTC+2 im August) = 06:00 UTC.
    assert A._rc_duty_start_utc(_g4_day()) == datetime(
        2026, 8, 2, 6, 0, tzinfo=timezone.utc)
    # Ohne start_time greift der Abflug als spätestmöglicher Dienstbeginn.
    ohne = _g4_day(start='', marker='')
    ohne['reader_facts'] = {'end_time': '20:30'}
    assert A._rc_duty_start_utc(ohne) == datetime(
        2026, 8, 2, 9, 0, tzinfo=timezone.utc)
    # Station außerhalb Europas: 22:00 lokal SFO (UTC-7) = 05:00 UTC am Folgetag.
    sfo = _g4_day(frm='SFO', to='FRA', start='22:00',
                  marker='22:00 LT Briefing SFO',
                  dep='2026-08-03T06:00:00Z', arr='2026-08-03T16:00:00Z')
    assert A._rc_duty_start_utc(sfo) == datetime(
        2026, 8, 3, 5, 0, tzinfo=timezone.utc)
    # Implausible Projektion (Beginn NACH dem Abflug) → Abflug gewinnt.
    spaet = _g4_day(start='23:00', marker='23:00 LT Briefing FRA')
    assert A._rc_duty_start_utc(spaet) == datetime(
        2026, 8, 2, 9, 0, tzinfo=timezone.utc)
    assert A._rc_duty_start_utc(None) is None
    assert A._rc_duty_start_utc({}) is None


# ══════════════════════════════════════════════════════════════════════════════
# Endpoint: take_roster_snapshot wendet die Gates NUR auf den Push an
# (Muster + Patches wie tests/test_duty_change_push.py::_snapshot_env)
# ══════════════════════════════════════════════════════════════════════════════
def _snapshot_env(tmp_path, old_tage):
    changes_file = tmp_path / 'roster_changes_test.json'
    push = MagicMock()
    return (
        patch.object(A, '_roster_snapshot_read',
                     return_value={'tage': old_tage} if old_tage else {}),
        patch.object(A, '_roster_snapshot_save', return_value=True),
        patch.object(A, '_roster_snapshot_path',
                     return_value=str(tmp_path / 'snap.json')),
        patch.object(A, '_roster_changes_path', return_value=str(changes_file)),
        patch.object(A, '_crew_flight_ingest', return_value=None),
        patch.object(A, '_push_notify_async', push),
        patch.object(A, '_profile_homebase_cached', return_value='FRA'),
        push,
        changes_file,
    )


def _tag(datum, klass='Flug', routing='FRA-JFK'):
    return {'datum': datum, 'klass': klass, 'routing': routing}


def _post(tmp_path, old, new):
    p1, p2, p3, p4, p5, p6, p7, push, changes_file = _snapshot_env(tmp_path, old)
    with p1, p2, p3, p4, p5, p6, p7:
        client = A.app.test_client()
        r = client.post('/api/user/roster-snapshot/testtoken123',
                        json={'tage': new})
    return r, push, changes_file


def test_past_change_recorded_but_not_pushed(tmp_path):
    d_past = (date.today() - timedelta(days=1)).isoformat()
    r, push, changes_file = _post(
        tmp_path,
        old=[_tag(d_past, routing='FRA-JFK')],
        new=[_tag(d_past, routing='FRA-MIA')])
    assert r.status_code == 200
    assert r.get_json()['changes_count'] == 1     # in-App-Liste behält den Change
    data = json.loads(changes_file.read_text())
    assert len(data['pending']) == 1
    assert push.call_count == 0                   # aber KEIN Push


def test_pickup_flip_weder_verlauf_noch_push(tmp_path):
    # OWNER-REGEL 2026-07-28: Rauschen darf jetzt auch NICHT MEHR in den
    # Verlauf („Dienstplan → Verlauf zeigt immer wieder Änderungen").
    d = (date.today() + timedelta(days=1)).isoformat()
    old = dict(_day_with_pickup(), datum=d)
    new = dict(_day_with_pickup(pickup_marker='', start='14:30'), datum=d)
    r, push, changes_file = _post(tmp_path, old=[old], new=[new])
    assert r.status_code == 200
    assert r.get_json()['changes_count'] == 0
    assert json.loads(changes_file.read_text())['pending'] == []
    assert push.call_count == 0


def test_blocktime_drift_weder_verlauf_noch_push(tmp_path):
    d = (date.today() + timedelta(days=2)).isoformat()
    old = dict(_day_with_pickup(), datum=d)
    new = dict(_day_with_pickup(), datum=d)
    new['reader_facts'] = dict(new['reader_facts'], end_time='19:20')
    r, push, changes_file = _post(tmp_path, old=[old], new=[new])
    assert r.status_code == 200
    assert r.get_json()['changes_count'] == 0
    assert json.loads(changes_file.read_text())['pending'] == []
    assert push.call_count == 0


def test_mixed_past_and_future_pushes_only_future(tmp_path):
    d_past = (date.today() - timedelta(days=1)).isoformat()
    d_fut = (date.today() + timedelta(days=2)).isoformat()
    r, push, _cf = _post(
        tmp_path,
        old=[_tag(d_past, routing='FRA-JFK'), _tag(d_fut, routing='FRA-JFK')],
        new=[_tag(d_past, routing='FRA-MIA'), _tag(d_fut, routing='FRA-GRU')])
    assert r.status_code == 200
    assert r.get_json()['changes_count'] == 2
    assert push.call_count == 1
    kwargs = push.call_args.kwargs
    # Nach dem Filter bleibt genau EINE push-würdige Änderung → deren Datum
    # ist die roster_change_id, und der Body nennt sie konkret.
    assert kwargs['data']['roster_change_id'] == d_fut
    body = push.call_args.args[2]
    assert 'Route FRA-JFK → FRA-GRU' in body
    assert 'weitere' not in body


def _fut_flug_tag(datum, dep_hhmm='09:00', flight='LH454', to='SFO'):
    """Flugtag an `datum` (Zukunft) mit UTC-Abflug `dep_hhmm` — die Endpoint-
    Tests laufen auf der echten Wanduhr, darum ein zukünftiger Tag: der Dienst
    hat garantiert noch nicht begonnen."""
    return {'datum': datum, 'klass': 'Flug', 'routing': f'FRA-{to}',
            'marker': '08:00 LT Briefing FRA',
            'ical_sectors': [{'flight': flight, 'from': 'FRA', 'to': to,
                              'dep_iso': f'{datum}T{dep_hhmm}:00Z',
                              'arr_iso': f'{datum}T20:00:00Z'}],
            'reader_facts': {'start_time': '08:00', 'end_time': '20:30',
                             'layover_ort': to}}


def test_gate4_zeitshift_steht_im_verlauf_pusht_aber_nicht(tmp_path):
    # KERN-INVARIANTE von Gate 4: die in-App-Liste bleibt VOLLSTÄNDIG, nur der
    # Push wird gefiltert (genau wie Gate 3 es handhabt).
    d = (date.today() + timedelta(days=3)).isoformat()
    r, push, changes_file = _post(
        tmp_path,
        old=[_fut_flug_tag(d, dep_hhmm='09:00')],
        new=[_fut_flug_tag(d, dep_hhmm='09:40')])       # 40 min später
    assert r.status_code == 200
    assert r.get_json()['changes_count'] == 1           # Verlauf: vollständig
    data = json.loads(changes_file.read_text())
    assert len(data['pending']) == 1
    assert data['pending'][0]['kind'] == 'modified'
    assert push.call_count == 0                          # aber KEIN Push


def test_gate4_pingpong_erzeugt_zwei_verlaufseintraege_null_pushes(tmp_path):
    # Der Live-Fall: 10:55 → 10:25 und 30 s später zurück. Beide Male Verlauf,
    # nie ein Push.
    d = (date.today() + timedelta(days=3)).isoformat()
    a, b = _fut_flug_tag(d, dep_hhmm='08:55'), _fut_flug_tag(d, dep_hhmm='08:25')
    for old, new in ((a, b), (b, a)):
        r, push, changes_file = _post(tmp_path, old=[old], new=[new])
        assert r.status_code == 200
        assert r.get_json()['changes_count'] == 1
        assert push.call_count == 0


def test_gate4_riesenverspaetung_pusht_am_endpoint(tmp_path):
    d = (date.today() + timedelta(days=3)).isoformat()
    r, push, _cf = _post(
        tmp_path,
        old=[_fut_flug_tag(d, dep_hhmm='09:00')],
        new=[_fut_flug_tag(d, dep_hhmm='13:00')])       # 4 h später
    assert r.status_code == 200 and push.call_count == 1


def test_gate4_zielwechsel_pusht_am_endpoint(tmp_path):
    d = (date.today() + timedelta(days=3)).isoformat()
    r, push, _cf = _post(
        tmp_path,
        old=[_fut_flug_tag(d, to='SFO')],
        new=[_fut_flug_tag(d, to='LAX')])
    assert r.status_code == 200 and push.call_count == 1


def test_future_change_push_body_is_concrete(tmp_path):
    d_fut = (date.today() + timedelta(days=2)).isoformat()
    r, push, _cf = _post(
        tmp_path,
        old=[_tag(d_fut, routing='FRA-JFK')],
        new=[_tag(d_fut, routing='FRA-MIA')])
    assert r.status_code == 200 and push.call_count == 1
    body = push.call_args.args[2]
    assert body == (f'{A._rc_datum_label(d_fut)}: Route FRA-JFK → FRA-MIA')
    assert len(body) <= 120
    assert push.call_args.kwargs['category'] == 'DUTY_CHANGE'
