"""Über-Mitternacht-Spillover (Jennifer-Fall, T5 2026-07-12).

Fall exakt nachgebaut: Über-Nacht-Rückflug LH779 SIN→FRA,
dep 12.07 23:40 SIN-Ortszeit (= 2026-07-12T15:40:00Z), arr 13.07
(= 2026-07-13T04:25:00Z). Beobachtete Fehler:
  • VOR dem Abflug wurde zeitweise ein falsches Leg / „unterwegs nach
    Singapur" mit FRA-Pin gezeigt (falsche RICHTUNG — sie fliegt SIN→FRA).
  • NACH Berliner Mitternacht (Ankunftstag 13.07) sah friends-today nur noch
    den leglosen 13.07-Roster-Tag → „Basis Frankfurt", während die Maschine
    nachweislich noch über dem Ozean flog.

Fixes unter Test:
  1. resolve_crew_live_state auf dem 12.07-Tag: pre_flight pinnt an SIN
     (dep-Airport), Richtung im Text = „SIN → FRA" — nie „→ Singapur".
  2. yesterday_leg_reaches_into_today (PURES Vorab-Gate) + spillover_wins:
     der gestrige Über-Nacht-Leg gewinnt am Folgetag, solange er laut Plan
     noch fliegt/frisch gelandet ist — aber NIE gegen einen aktiven
     Heute-Zustand.
  3. get_friends_today-Wiring: crew_state am 13.07 = flying SIN→FRA
     (nicht home/Basis), Position/Text aus dem gestrigen Leg.
  4. crew_state.position trägt track/gs (Glyph-Rotation der Crew-Live-Karte).

KEIN echter Netz-/DB-Zugriff: obs/live sind injizierte Fakes bzw. gepatcht.
"""
import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import sys
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import app as A
from blueprints.crew_live_state import (
    resolve_crew_live_state, yesterday_leg_reaches_into_today, spillover_wins,
    STATE_HOME, STATE_PRE_FLIGHT, STATE_FLYING, STATE_LANDED, STATE_LAYOVER,
)


@pytest.fixture(autouse=True)
def _pin_app_module():
    prev = sys.modules.get('app')
    sys.modules['app'] = A
    A._FRIENDS_TODAY_MEMO.clear()
    yield
    A._FRIENDS_TODAY_MEMO.clear()
    if prev is not None:
        sys.modules['app'] = prev


# Jennifer: Über-Nacht-Rückflug (Roster-Tag 12.07), Ankunft am Folgetag.
JEN_SECTORS = [
    {'flight': 'LH779', 'from': 'SIN', 'to': 'FRA',
     'dep_iso': '2026-07-12T15:40:00Z', 'arr_iso': '2026-07-13T04:25:00Z'},
]

CITY = {'FRA': 'Frankfurt', 'SIN': 'Singapur'}


def _utc(day, h, m):
    return datetime(2026, 7, day, h, m, tzinfo=timezone.utc)


def _resolve(now, live=None, obs=None):
    return resolve_crew_live_state(
        JEN_SECTORS,
        (lambda *a: obs), (lambda *a: live), now,
        homebase='FRA', city_lookup=lambda c: CITY.get(c, c))


# ── 1) Abflugtag: Richtung + Pin ─────────────────────────────────────────────

def test_abflugtag_vor_dep_pre_flight_pin_sin_richtung_sin_fra():
    r = _resolve(_utc(12, 10, 0))
    assert r['state'] == STATE_PRE_FLIGHT
    assert r['current_leg']['dep'] == 'SIN'          # Pin = Abflug-Airport SIN
    assert r['current_leg']['arr'] == 'FRA'
    assert 'SIN → FRA' in (r['text']['subtitle'] or '')
    # Richtung darf NIE als Ziel „Singapur" formuliert sein.
    for t in (r['text']['title'] or '', r['text']['subtitle'] or ''):
        assert 'nach Singapur' not in t


def test_abflugtag_nach_dep_fliegt_sin_fra():
    r = _resolve(_utc(12, 20, 0))
    assert r['state'] == STATE_FLYING
    assert r['text']['title'] == 'Fliegt gerade'
    assert 'SIN → FRA' in (r['text']['subtitle'] or '')


# ── 2) Folgetag (nach Berliner Mitternacht): Resolver auf GESTERN-Sektoren ──

def test_folgetag_0200z_gestern_sektoren_noch_flying():
    r = _resolve(_utc(13, 2, 0))
    assert r['state'] == STATE_FLYING
    assert r['current_leg']['dep'] == 'SIN'
    assert r['current_leg']['arr'] == 'FRA'


def test_folgetag_position_traegt_track_und_gs():
    live = {'lat': 10.5, 'lon': 78.2, 'track': 305.0, 'gs': 487.0,
            'ts': '2026-07-13T01:58:00Z', 'on_ground': False,
            'near_dep': False, 'near_arr': False, 'source': 'aircraft_live'}
    r = _resolve(_utc(13, 2, 0), live=live)
    assert r['state'] == STATE_FLYING
    pos = r['position']
    assert pos is not None
    assert pos['track'] == 305.0          # Glyph-Rotation (T6)
    assert pos['gs'] == 487.0


# ── 3) Pures Vorab-Gate + Gewinner-Regel ─────────────────────────────────────

def test_gate_true_solange_leg_bis_heute_reicht():
    assert yesterday_leg_reaches_into_today(JEN_SECTORS, _utc(13, 2, 0)) is True
    # frisch gelandet (arr 04:25 + 40' Puffer + 90' Landed-Fenster ≈ 06:35)
    assert yesterday_leg_reaches_into_today(JEN_SECTORS, _utc(13, 6, 0)) is True


def test_gate_false_lange_nach_ankunft_und_vor_abflug():
    assert yesterday_leg_reaches_into_today(JEN_SECTORS, _utc(13, 12, 0)) is False
    assert yesterday_leg_reaches_into_today(JEN_SECTORS, _utc(12, 10, 0)) is False
    assert yesterday_leg_reaches_into_today([], _utc(13, 2, 0)) is False
    assert yesterday_leg_reaches_into_today(None, _utc(13, 2, 0)) is False


def test_spillover_wins_nur_gegen_inaktive_heute_zustaende():
    fly = {'state': STATE_FLYING}
    landed = {'state': STATE_LANDED}
    # Heute nichts Aktives → gestern flying/landed gewinnt.
    assert spillover_wins({'state': STATE_HOME}, fly) is True
    assert spillover_wins({'state': STATE_LAYOVER}, landed) is True
    assert spillover_wins(None, fly) is True
    # Heute AKTIV → gestern verliert immer.
    assert spillover_wins({'state': STATE_PRE_FLIGHT}, fly) is False
    assert spillover_wins({'state': STATE_FLYING}, fly) is False
    assert spillover_wins({'state': STATE_LANDED}, fly) is False
    # Gestern nichts Fliegendes → kein Spillover (staler Layover gewinnt nie).
    assert spillover_wins({'state': STATE_HOME}, {'state': STATE_LAYOVER}) is False
    assert spillover_wins({'state': STATE_HOME}, None) is False


# ── 4) friends-today-Wiring: Folgetag zeigt flying statt „Basis" ────────────

def _iso_z(d):
    return d.strftime('%Y-%m-%dT%H:%M:%SZ')


def test_friends_today_folgetag_spillover_zeigt_flying():
    fr = 'AT-SPILLOVER-TEST-1'
    now = datetime.now(timezone.utc)
    today = _date.today().isoformat()
    gestern = (_date.today() - timedelta(days=1)).isoformat()
    # Über-Nacht-Leg dem GESTRIGEN Roster-Tag zugeordnet, fliegt JETZT noch.
    day_y = {'datum': gestern, 'klass': 'Z72', 'marker': 'FLUG',
             'routing': 'SIN-FRA', 'reader_facts': {},
             'ical_sectors': [{'flight': 'LH779', 'from': 'SIN', 'to': 'FRA',
                               'dep_iso': _iso_z(now - timedelta(hours=6)),
                               'arr_iso': _iso_z(now + timedelta(hours=2))}]}
    # Heutiger (Ankunfts-)Tag: leglos — vor dem Fix wurde daraus „Basis".
    day_t = {'datum': today, 'klass': 'Z73', 'marker': 'FREI',
             'reader_facts': {}, 'ical_sectors': []}
    with patch.object(A, '_friends_load', return_value={'friends': [fr]}), \
         patch.object(A, '_profiles_load_bulk',
                      return_value={fr: {'name': 'Jennifer', 'homebase': 'FRA'}}), \
         patch.object(A, '_maybe_refresh_calendar_feed'), \
         patch.object(A, '_roster_snapshot_read',
                      return_value={'taken_at': _iso_z(now),
                                    'tage': [day_y, day_t]}), \
         patch.object(A, '_friend_briefing_day_sectors',
                      return_value=(None, None, None, None)), \
         patch.object(A, '_flight_obs_merged', return_value=None), \
         patch('blueprints.aerox_data_blueprint._aircraft_live_pos',
               return_value=(None, None, None, None)), \
         patch.dict(A._store, {}, clear=True):
        with A.app.test_request_context(
                f'/api/user/friends-today/{fr}?datum={today}'):
            resp = A.get_friends_today(fr)
    data = resp.get_json()
    assert data['count'] == 1
    cs = data['friends_today'][0].get('crew_state')
    assert cs is not None
    assert cs['state'] == STATE_FLYING, \
        'Über-Nacht-Leg von gestern muss den leglosen Heute-Tag überstimmen'
    assert cs['current_leg']['dep'] == 'SIN'
    assert cs['current_leg']['arr'] == 'FRA'


def test_friends_today_heute_aktiv_kein_spillover():
    """Hat HEUTE selbst einen aktiven Leg (pre_flight), bleibt der Heute-
    Zustand — der gestrige Über-Nacht-Leg (längst gelandet) mischt sich
    nicht ein."""
    fr = 'AT-SPILLOVER-TEST-2'
    now = datetime.now(timezone.utc)
    today = _date.today().isoformat()
    gestern = (_date.today() - timedelta(days=1)).isoformat()
    day_y = {'datum': gestern, 'klass': 'Z72', 'marker': 'FLUG',
             'routing': 'SIN-FRA', 'reader_facts': {},
             'ical_sectors': [{'flight': 'LH779', 'from': 'SIN', 'to': 'FRA',
                               'dep_iso': _iso_z(now - timedelta(hours=20)),
                               'arr_iso': _iso_z(now - timedelta(hours=8))}]}
    day_t = {'datum': today, 'klass': 'Z72', 'marker': 'FLUG',
             'routing': 'FRA-BCN', 'reader_facts': {},
             'ical_sectors': [{'flight': 'LH1138', 'from': 'FRA', 'to': 'BCN',
                               'dep_iso': _iso_z(now + timedelta(hours=3)),
                               'arr_iso': _iso_z(now + timedelta(hours=5))}]}
    with patch.object(A, '_friends_load', return_value={'friends': [fr]}), \
         patch.object(A, '_profiles_load_bulk',
                      return_value={fr: {'name': 'Jennifer', 'homebase': 'FRA'}}), \
         patch.object(A, '_maybe_refresh_calendar_feed'), \
         patch.object(A, '_roster_snapshot_read',
                      return_value={'taken_at': _iso_z(now),
                                    'tage': [day_y, day_t]}), \
         patch.object(A, '_friend_briefing_day_sectors',
                      return_value=(None, None, None, None)), \
         patch.object(A, '_flight_obs_merged', return_value=None), \
         patch('blueprints.aerox_data_blueprint._aircraft_live_pos',
               return_value=(None, None, None, None)), \
         patch.dict(A._store, {}, clear=True):
        with A.app.test_request_context(
                f'/api/user/friends-today/{fr}?datum={today}'):
            resp = A.get_friends_today(fr)
    cs = resp.get_json()['friends_today'][0].get('crew_state')
    assert cs is not None
    assert cs['state'] == STATE_PRE_FLIGHT
    assert cs['current_leg']['flight_no'] == 'LH1138'


# ══════════════════════════════════════════════════════════════════════════════
# Regressions-Sweep 2026-07-12 #6: Nacht-Turnaround + verspäteter
# Über-Nacht-Abflug — ein heutiger PLAN-ONLY-pre_flight (Abflug in der
# Zukunft) ist kein Beweis-Zustand und darf gegen ein gestriges FLYING
# verlieren; ein gestriges BEOBACHTET gepinntes pre_flight (Board grounded/
# delayed) gewinnt über einen leglosen Heute-Tag.
# ══════════════════════════════════════════════════════════════════════════════
from blueprints.crew_live_state import (          # noqa: E402
    today_blocks_spillover, CONF_PLAN, CONF_OBSERVED)


def _st(state, conf=None, dep=None, arr=None):
    d = {'state': state}
    if conf:
        d['confidence'] = conf
    if dep or arr:
        d['current_leg'] = {'dep_iso': dep, 'arr_iso': arr}
    return d


def test_plan_only_future_pre_flight_verliert_gegen_gestern_flying():
    now = _utc(13, 0, 10)
    # Nacht-Turnaround: Rückleg heute 01:15, reine Plan-Uhr → gestern-flying
    # (die Crew sitzt nachweislich noch im Hinflug) gewinnt.
    t = _st(STATE_PRE_FLIGHT, CONF_PLAN, dep='2026-07-13T01:15:00Z')
    assert spillover_wins(t, _st(STATE_FLYING), now=now) is True
    # … aber gestern-LANDED übernimmt NICHT: nach der Landung ist
    # „Wartet auf …" des Rücklegs der bessere Text.
    assert spillover_wins(t, _st(STATE_LANDED), now=now) is False


def test_pre_flight_blockt_weiter_wenn_beobachtet_oder_dep_vorbei():
    now = _utc(13, 0, 10)
    # Beobachteter pre_flight (Delay-Pin) bleibt ein Beweis-Zustand.
    t_obs = _st(STATE_PRE_FLIGHT, CONF_OBSERVED, dep='2026-07-13T01:15:00Z')
    assert spillover_wins(t_obs, _st(STATE_FLYING), now=now) is False
    # Plan-pre_flight mit VERGANGENEM dep (Fenster läuft) blockt ebenfalls.
    t_past = _st(STATE_PRE_FLIGHT, CONF_PLAN, dep='2026-07-12T23:00:00Z')
    assert spillover_wins(t_past, _st(STATE_FLYING), now=now) is False
    # Ohne now (alte Signatur) exakt das alte Verhalten.
    t_fut = _st(STATE_PRE_FLIGHT, CONF_PLAN, dep='2026-07-13T01:15:00Z')
    assert spillover_wins(t_fut, _st(STATE_FLYING)) is False


def test_today_blocks_spillover_kennt_die_ausnahme():
    now = _utc(13, 0, 10)
    assert today_blocks_spillover(_st(STATE_FLYING), now) is True
    assert today_blocks_spillover(_st(STATE_LANDED), now) is True
    assert today_blocks_spillover(_st(STATE_HOME), now) is False
    assert today_blocks_spillover(None, now) is False
    # Plan-only-pre_flight mit Zukunfts-dep blockt NICHT mehr …
    assert today_blocks_spillover(
        _st(STATE_PRE_FLIGHT, CONF_PLAN, dep='2026-07-13T01:15:00Z'),
        now) is False
    # … beobachteter bzw. dep-vorbei-pre_flight schon.
    assert today_blocks_spillover(
        _st(STATE_PRE_FLIGHT, CONF_OBSERVED, dep='2026-07-13T01:15:00Z'),
        now) is True
    assert today_blocks_spillover(
        _st(STATE_PRE_FLIGHT, CONF_PLAN, dep='2026-07-12T23:00:00Z'),
        now) is True


def test_verspaeteter_uebernacht_abflug_gewinnt_ueber_leglosen_tag():
    # Konsequenz B: Plan-dep gestern 23:40, real noch am Boden (Board-Pin,
    # CONF_OBSERVED) — um 00:30 zeigt der leglose Heute-Tag sonst „Basis
    # Frankfurt", während die Crew am Outstation-Gate wartet.
    now = _utc(13, 0, 30)
    y = _st(STATE_PRE_FLIGHT, CONF_OBSERVED,
            dep='2026-07-12T23:40:00Z', arr='2026-07-13T02:10:00Z')
    assert spillover_wins(_st(STATE_HOME), y, now=now) is True
    assert spillover_wins(None, y, now=now) is True
    # Reiner PLAN-pre_flight von gestern gewinnt NICHT (kein Beweis).
    y_plan = _st(STATE_PRE_FLIGHT, CONF_PLAN,
                 dep='2026-07-12T23:40:00Z', arr='2026-07-13T02:10:00Z')
    assert spillover_wins(_st(STATE_HOME), y_plan, now=now) is False
    # Fenster lange vorbei (staler Morgen-Leg-Pin) → gewinnt NICHT.
    y_stale = _st(STATE_PRE_FLIGHT, CONF_OBSERVED,
                  dep='2026-07-12T06:00:00Z', arr='2026-07-12T08:00:00Z')
    assert spillover_wins(_st(STATE_HOME), y_stale, now=now) is False
    # dep noch in der Zukunft (Leg gehört nicht in den Rückblick) → nein.
    y_fut = _st(STATE_PRE_FLIGHT, CONF_OBSERVED,
                dep='2026-07-13T06:00:00Z', arr='2026-07-13T08:00:00Z')
    assert spillover_wins(_st(STATE_HOME), y_fut, now=now) is False


def test_friends_today_nacht_turnaround_zeigt_flying_statt_wartet():
    """Wiring (Sweep #6, Konsequenz A): Hinflug gestern fliegt noch, Rückleg
    heute (Plan-Uhr, dep in der Zukunft) — vor dem Fix blockte der heutige
    pre_flight den Spillover komplett und der Feed zeigte „Wartet auf …",
    während die Crew nachweislich in der Luft ist."""
    fr = 'AT-SPILLOVER-TEST-3'
    now = datetime.now(timezone.utc)
    today = _date.today().isoformat()
    gestern = (_date.today() - timedelta(days=1)).isoformat()
    day_y = {'datum': gestern, 'klass': 'Z72', 'marker': 'FLUG',
             'routing': 'FRA-AMM', 'reader_facts': {},
             'ical_sectors': [{'flight': 'LH692', 'from': 'FRA', 'to': 'AMM',
                               'dep_iso': _iso_z(now - timedelta(hours=2)),
                               'arr_iso': _iso_z(now + timedelta(hours=1))}]}
    day_t = {'datum': today, 'klass': 'Z72', 'marker': 'FLUG',
             'routing': 'AMM-FRA', 'reader_facts': {},
             'ical_sectors': [{'flight': 'LH693', 'from': 'AMM', 'to': 'FRA',
                               'dep_iso': _iso_z(now + timedelta(hours=2)),
                               'arr_iso': _iso_z(now + timedelta(hours=6))}]}
    with patch.object(A, '_friends_load', return_value={'friends': [fr]}), \
         patch.object(A, '_profiles_load_bulk',
                      return_value={fr: {'name': 'Jennifer', 'homebase': 'FRA'}}), \
         patch.object(A, '_maybe_refresh_calendar_feed'), \
         patch.object(A, '_roster_snapshot_read',
                      return_value={'taken_at': _iso_z(now),
                                    'tage': [day_y, day_t]}), \
         patch.object(A, '_friend_briefing_day_sectors',
                      return_value=(None, None, None, None)), \
         patch.object(A, '_flight_obs_merged', return_value=None), \
         patch('blueprints.aerox_data_blueprint._aircraft_live_pos',
               return_value=(None, None, None, None)), \
         patch.dict(A._store, {}, clear=True):
        with A.app.test_request_context(
                f'/api/user/friends-today/{fr}?datum={today}'):
            resp = A.get_friends_today(fr)
    cs = resp.get_json()['friends_today'][0].get('crew_state')
    assert cs is not None
    assert cs['state'] == STATE_FLYING, \
        'gestriges noch-fliegendes Leg muss den Plan-only-pre_flight überstimmen'
    assert cs['current_leg']['flight_no'] == 'LH692'


# ══════════════════════════════════════════════════════════════════════════════
# Regressions-Sweep 2026-07-12 #8: das Vorab-Gate muss DIESELBE Sektor-Quelle
# sehen wie der Resolver (pick_fresher_sectors) — iCal-Freunde mit stalem
# Snapshot (Über-Nacht-Leg NUR im frischeren Briefing) verloren den Spillover
# und standen nach Mitternacht auf „Basis Frankfurt".
# ══════════════════════════════════════════════════════════════════════════════
def test_friends_today_spillover_aus_frischerem_briefing_ohne_snapshot_tag():
    fr = 'AT-SPILLOVER-TEST-4'
    now = datetime.now(timezone.utc)
    today = _date.today().isoformat()
    gestern = (_date.today() - timedelta(days=1)).isoformat()
    # Staler Snapshot: der GESTRIGE Tag fehlt KOMPLETT (day_y=None-Fall),
    # heute leglos. Das Über-Nacht-Leg existiert nur im frischeren Briefing.
    day_t = {'datum': today, 'klass': 'Z73', 'marker': 'FREI',
             'reader_facts': {}, 'ical_sectors': []}
    y_brief = [{'flight': 'LH779', 'from': 'SIN', 'to': 'FRA',
                'dep_iso': _iso_z(now - timedelta(hours=6)),
                'arr_iso': _iso_z(now + timedelta(hours=2))}]

    def _brief(_fr, datum):
        if datum == gestern:
            return y_brief, _iso_z(now), None, None
        return None, None, None, None

    with patch.object(A, '_friends_load', return_value={'friends': [fr]}), \
         patch.object(A, '_profiles_load_bulk',
                      return_value={fr: {'name': 'Jennifer', 'homebase': 'FRA'}}), \
         patch.object(A, '_maybe_refresh_calendar_feed'), \
         patch.object(A, '_roster_snapshot_read',
                      return_value={'taken_at': _iso_z(now - timedelta(days=3)),
                                    'tage': [day_t]}), \
         patch.object(A, '_friend_briefing_day_sectors',
                      side_effect=_brief), \
         patch.object(A, '_flight_obs_merged', return_value=None), \
         patch('blueprints.aerox_data_blueprint._aircraft_live_pos',
               return_value=(None, None, None, None)), \
         patch.dict(A._store, {}, clear=True):
        with A.app.test_request_context(
                f'/api/user/friends-today/{fr}?datum={today}'):
            resp = A.get_friends_today(fr)
    cs = resp.get_json()['friends_today'][0].get('crew_state')
    assert cs is not None
    assert cs['state'] == STATE_FLYING, \
        'Briefing-only Über-Nacht-Leg muss das Gate öffnen (Quelle wie Resolver)'
    assert cs['current_leg']['dep'] == 'SIN'
    assert cs['current_leg']['arr'] == 'FRA'


# ══════════════════════════════════════════════════════════════════════════════
# OVERNIGHT-CARRY (Julien/Tibor 2026-07-16): der noch aktive Über-Nacht-Leg des
# VORTAGS wird VOR die heutigen Sektoren in DENSELBEN Resolver gereicht
# (carry_over_legs → _crew_state_for_day carry_sectors/carry_datum), statt extern
# überstimmt zu werden. Fixt zwei Live-Bugs:
#   • Julien LH423 BOS→FRA (dep 15.07 22:15Z / arr 16.07 06:00Z, keyt auf dem
#     Abflugtag): nach Berliner Mitternacht fand der Resolver am 16. kein Leg und
#     fiel auf `home`/„Basis Frankfurt" — obwohl er nachweislich noch flog.
#   • Der carried Leg darf NICHT die HEUTIGE Instanz derselben täglichen
#     Flugnummer (16., „Scheduled") sehen (carry_datum-Dispatch) — die würde ihn
#     erneut auf pre_flight pinnen (Wrong-Day-Guard greift dort nicht).
#   • Reg aus dem Sektor-tail landet im current_leg (aircraft_live-Positions-Tier).
# ══════════════════════════════════════════════════════════════════════════════
from blueprints.crew_live_state import carry_over_legs          # noqa: E402


def test_carry_over_legs_fenster():
    now = _utc(16, 0, 30)                       # 16.07 00:30Z ≈ 01:15/02:30 CEST
    # LH423 dep 15.07 22:15Z / arr 16.07 06:00Z — noch unterwegs → getragen.
    active = [{'flight': 'LH423', 'from': 'BOS', 'to': 'FRA', 'tail': 'D-AIXH',
               'dep_iso': '2026-07-15T22:15:00Z', 'arr_iso': '2026-07-16T06:00:00Z'}]
    assert len(carry_over_legs(active, now)) == 1
    # arr 4 h vorbei → kein ewiges Gestern-Leg mehr (Fenster ≈3 h).
    landed = [{'flight': 'LH100', 'from': 'JFK', 'to': 'FRA', 'tail': 'D-A',
               'dep_iso': '2026-07-15T14:00:00Z', 'arr_iso': '2026-07-15T20:30:00Z'}]
    assert carry_over_legs(landed, now) == []
    # dep noch in der Zukunft → gehört nicht in den Rückblick.
    future = [{'flight': 'LH200', 'from': 'FRA', 'to': 'JFK',
               'dep_iso': '2026-07-16T08:00:00Z', 'arr_iso': '2026-07-16T16:00:00Z'}]
    assert carry_over_legs(future, now) == []
    assert carry_over_legs([], now) == []
    assert carry_over_legs(None, now) == []


def _julien_env(fr, day_y_secs, day_t_secs, obs_side_effect):
    now = datetime.now(timezone.utc)
    today = _date.today().isoformat()
    gestern = (_date.today() - timedelta(days=1)).isoformat()
    day_y = {'datum': gestern, 'klass': 'Z72', 'marker': 'FLUG',
             'reader_facts': {}, 'ical_sectors': day_y_secs}
    day_t = {'datum': today, 'klass': 'Z73', 'marker': '',
             'reader_facts': {}, 'ical_sectors': day_t_secs}
    with patch.object(A, '_friends_load', return_value={'friends': [fr]}), \
         patch.object(A, '_profiles_load_bulk',
                      return_value={fr: {'name': 'Julien', 'homebase': 'FRA'}}), \
         patch.object(A, '_maybe_refresh_calendar_feed'), \
         patch.object(A, '_roster_snapshot_read',
                      return_value={'taken_at': _iso_z(now),
                                    'tage': [day_y, day_t]}), \
         patch.object(A, '_friend_briefing_day_sectors',
                      return_value=(None, None, None, None)), \
         patch.object(A, '_flight_obs_merged', side_effect=obs_side_effect), \
         patch('blueprints.aerox_data_blueprint._aircraft_live_pos',
               return_value=(None, None, None, None)), \
         patch('blueprints.aerox_data_blueprint._free_crew_live_pos',
               return_value=(None, None, None, None)), \
         patch.dict(A._store, {}, clear=True):
        with A.app.test_request_context(
                f'/api/user/friends-today/{fr}?datum={today}'):
            resp = A.get_friends_today(fr)
    return resp.get_json()['friends_today'][0].get('crew_state')


def test_julien_overnight_carry_flying_trotz_daily_flight_obs_leak():
    """(a) Julien-Fall: LH423 keyt auf dem GESTRIGEN Abflugtag, fliegt JETZT noch,
    heute leglos. Selbst wenn der datums-agnostische Board-Match die HEUTIGE
    (Scheduled) Instanz derselben täglichen Flugnummer liefert, muss der carried
    Leg als FLYING erscheinen (carry_datum-Dispatch hält die 16.-Instanz fern) —
    nicht home/„Basis Frankfurt". Reg aus dem Sektor-tail durchgereicht."""
    now = datetime.now(timezone.utc)
    today = _date.today().isoformat()

    def _obs_daily_leak(fno, date=None, dep_iata=None, arr_iata=None, **kw):
        # Board hätte nur die HEUTIGE Instanz (Abflug erst am Abend, Scheduled);
        # der carried Leg fragt mit dem VORTAGS-Datum → dort nichts.
        if date == today:
            return {'status': 'Scheduled',
                    'sched_dep_iso': _iso_z(now + timedelta(hours=23)),
                    'sched_arr_iso': _iso_z(now + timedelta(hours=30))}
        return None

    cs = _julien_env(
        'AT-CARRY-JULIEN',
        [{'flight': 'LH423', 'from': 'BOS', 'to': 'FRA', 'tail': 'D-AIXH',
          'dep_iso': _iso_z(now - timedelta(hours=1)),
          'arr_iso': _iso_z(now + timedelta(hours=6))}],
        [], _obs_daily_leak)
    assert cs is not None
    assert cs['state'] == STATE_FLYING, \
        'Über-Nacht-Leg vom Abflugtag muss den leglosen Heute-Tag ersetzen'
    assert cs['current_leg']['flight_no'] == 'LH423'
    assert cs['current_leg']['dep'] == 'BOS'
    assert cs['current_leg']['reg'] == 'D-AIXH'


def test_julien_carry_endet_nach_ankunft_kein_ewiges_gestern():
    """(b) Nach arr+4 h kein Carry mehr — der Feed zeigt wieder den ehrlichen
    Heute-Zustand (home/„Basis Frankfurt"), nicht ewig das Gestern-Leg."""
    now = datetime.now(timezone.utc)
    cs = _julien_env(
        'AT-CARRY-LANDED',
        [{'flight': 'LH423', 'from': 'BOS', 'to': 'FRA', 'tail': 'D-AIXH',
          'dep_iso': _iso_z(now - timedelta(hours=10)),
          'arr_iso': _iso_z(now - timedelta(hours=4))}],
        [], lambda *a, **k: None)
    assert cs is not None
    assert cs['state'] == STATE_HOME
    assert (cs.get('current_leg') or {}).get('flight_no') != 'LH423'


def test_tibor_overnight_carry_enroute_ohne_obs_reg_aus_sektor():
    """(c) Tibor-Fall: LH455 SFO→FRA keyt gestern, dep ~1 h vorbei, arr in der
    Zukunft, SFO-Abflugboard NICHT geharvestet (keine Obs). Zeit-Physik ⇒
    enroute-artig (flying, Vertrauen plan) statt pre_flight; Reg (D-ABYT) aus dem
    ical-Sektor-tail durchgereicht, damit der aircraft_live-Positions-Tier greift."""
    now = datetime.now(timezone.utc)
    cs = _julien_env(
        'AT-CARRY-TIBOR',
        [{'flight': 'LH455', 'from': 'SFO', 'to': 'FRA', 'tail': 'D-ABYT',
          'dep_iso': _iso_z(now - timedelta(hours=1)),
          'arr_iso': _iso_z(now + timedelta(hours=10))}],
        [], lambda *a, **k: None)
    assert cs is not None
    assert cs['state'] == STATE_FLYING
    assert cs['confidence'] == CONF_PLAN
    assert cs['current_leg']['flight_no'] == 'LH455'
    assert cs['current_leg']['reg'] == 'D-ABYT'


def test_carry_greift_nicht_wenn_heute_aktiv():
    """Ein echter Heute-Zustand (pre_flight/flying) wird NIE vom Carry
    überschrieben — der Carry ist reiner leg-loser Fallback."""
    now = datetime.now(timezone.utc)
    cs = _julien_env(
        'AT-CARRY-TODAYACTIVE',
        # gestern: noch fliegend (würde sonst getragen) …
        [{'flight': 'LH999', 'from': 'JFK', 'to': 'FRA', 'tail': 'D-AOLD',
          'dep_iso': _iso_z(now - timedelta(hours=1)),
          'arr_iso': _iso_z(now + timedelta(hours=1))}],
        # … aber heute läuft ein EIGENER aktiver Leg.
        [{'flight': 'LH1138', 'from': 'FRA', 'to': 'BCN', 'tail': 'D-AINEW',
          'dep_iso': _iso_z(now - timedelta(minutes=20)),
          'arr_iso': _iso_z(now + timedelta(hours=2))}],
        lambda *a, **k: None)
    assert cs is not None
    assert cs['current_leg']['flight_no'] == 'LH1138', \
        'Heute-aktiver Leg darf nicht vom Gestern-Carry verdrängt werden'
