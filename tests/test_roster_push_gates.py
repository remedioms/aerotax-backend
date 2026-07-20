"""Dienstplan-Push: konkreter Inhalt + Vergangenheits-/Pickup-Gates (Flo Z,
FO, 2026-07-20):

  (a) „WAS sich geändert hat, sieht man nicht" → der Push-Body nennt jetzt die
      erste konkrete Änderung („Mi 22.07: LH440 FRA-IAH neu" / „Di 21.07:
      Briefing 09:40 → 10:15") + „(+N weitere)". Formatter:
      _roster_changes_push_body / _roster_change_push_line. Max ~120 Zeichen.
  (b) „Push kommt auch, wenn die Tour vorbei ist" → zwei Push-Gates in
      take_roster_snapshot (die in-App-Liste /api/user/roster-changes zeigt die
      Changes weiterhin):
        • _roster_change_is_past: Tage VOR heute (Homebase-lokal) pushen nicht.
        • _roster_change_is_pickup_prune: NUR-Pickup-Abbau (LH räumt die
          PU-Zeit nach der Tour aus MyTime) pushed nicht; neue/geänderte
          Pickup-Zeit bleibt push-würdig.

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

from datetime import date, timedelta
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
# Gate 2: Pickup-Abbau (_roster_change_is_pickup_prune)
# ══════════════════════════════════════════════════════════════════════════════
def _day_with_pickup(pickup_marker='Pickup 1330', start='13:30',
                     dep='2026-07-22T12:00:00Z'):
    return {'datum': '2026-07-22', 'klass': 'Flug', 'routing': 'FRA-IAH',
            'marker': pickup_marker,
            'ical_sectors': [_sector(dep=dep)],
            'reader_facts': {'start_time': start, 'end_time': '19:00',
                             'layover_ort': 'IAH'}}


def test_pickup_prune_only_is_not_pushworthy():
    # LH räumt die PU-Zeit ab: Marker verliert 'Pickup 1330', Start fällt von
    # der Pickup- (13:30) auf die Briefing-Zeit (14:30) zurück — sonst nichts.
    old = _day_with_pickup()
    new = _day_with_pickup(pickup_marker='', start='14:30')
    assert A._roster_change_is_pickup_prune(
        {'kind': 'modified', 'old': old, 'new': new}) is True


def test_pickup_prune_with_flight_change_is_pushworthy():
    old = _day_with_pickup()
    new = _day_with_pickup(pickup_marker='', start='14:30',
                           dep='2026-07-22T13:00:00Z')     # Leg verschoben
    assert A._roster_change_is_pickup_prune(
        {'kind': 'modified', 'old': old, 'new': new}) is False


def test_pickup_prune_with_other_time_change_is_pushworthy():
    old = _day_with_pickup()
    new = _day_with_pickup(pickup_marker='', start='14:30')
    new['reader_facts']['end_time'] = '20:15'              # Dienstende geändert
    assert A._roster_change_is_pickup_prune(
        {'kind': 'modified', 'old': old, 'new': new}) is False


def test_new_or_changed_pickup_stays_pushworthy():
    # Pickup NEU (kommender Tag): old ohne, new mit → Gate greift NICHT.
    old = _day_with_pickup(pickup_marker='', start='14:30')
    new = _day_with_pickup()
    assert A._roster_change_is_pickup_prune(
        {'kind': 'modified', 'old': old, 'new': new}) is False
    # Pickup GEÄNDERT: 13:30 → 14:00 → Gate greift NICHT.
    assert A._roster_change_is_pickup_prune(
        {'kind': 'modified', 'old': _day_with_pickup(),
         'new': _day_with_pickup(pickup_marker='Pickup 1400',
                                 start='14:00')}) is False


def test_pickup_prune_defensive():
    assert A._roster_change_is_pickup_prune({'kind': 'added', 'new': {}}) is False
    assert A._roster_change_is_pickup_prune({}) is False
    assert A._roster_change_is_pickup_prune(None) is False


def test_rc_pickup_hhmm_sources():
    assert A._rc_pickup_hhmm({'pickup': '9:05'}) == '09:05'
    assert A._rc_pickup_hhmm({'marker': 'Pickup 1430'}) == '14:30'
    assert A._rc_pickup_hhmm({'ical_summary': '09:30 LT Pickup HND'}) == '09:30'
    assert A._rc_pickup_hhmm({'marker': 'Briefing 0900'}) == ''
    assert A._rc_pickup_hhmm(None) == ''


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


def test_pickup_prune_recorded_but_not_pushed(tmp_path):
    d = (date.today() + timedelta(days=1)).isoformat()
    old = dict(_day_with_pickup(), datum=d)
    new = dict(_day_with_pickup(pickup_marker='', start='14:30'), datum=d)
    r, push, changes_file = _post(tmp_path, old=[old], new=[new])
    assert r.status_code == 200
    assert r.get_json()['changes_count'] == 1
    assert len(json.loads(changes_file.read_text())['pending']) == 1
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
