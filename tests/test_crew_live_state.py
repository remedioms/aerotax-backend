"""Crew-Live-State Neubau (blueprints/crew_live_state, 2026-07-10) —
EINE Wahrheit für Familie/Freunde/Crew inkl. SERVERSEITIGEM Text.

Deckt ab (Auftrag):
  • Tibors 3-Leg-Tag (BCN-FRA-ARN-FRA) zu 5 Zeitpunkten:
      07:00 fliegt BCN→FRA · 09:30 gelandet/wartet · 10:59 fliegt FRA→ARN ·
      12:45 gelandet ARN/wartet · 14:00 fliegt ARN→FRA (+ Feierabend danach)
  • kein-Dienst-Tag → „Basis Frankfurt" (NUR dann), Standby, Layover-Ruhetag
  • aircraft_live-Gegencheck schlägt die Uhr in BEIDE Richtungen:
      airborne nach Plan-Ankunft → flying; am Boden nahe dep im Fenster →
      pre_flight (kein Geister-Flieger)
  • Board-Obs: Landung beendet Leg sofort, Delay verschiebt Fenster, cancelled
  • pick_fresher_sectors: frisches Briefing SCHLÄGT stalen Snapshot
  • Consumer-Wiring: friends-today (crew_state im Payload) + family_watch
    (_load_crew_status_for_family setzt crew_state) — beides ADDITIV.

KEIN echter Netz-/DB-Zugriff: obs/live sind injizierte Fakes bzw. gepatcht.
"""
import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

from datetime import date as _date
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from blueprints.crew_live_state import (
    resolve_crew_live_state, pick_fresher_sectors,
    STATE_HOME, STATE_STANDBY, STATE_PRE_FLIGHT, STATE_FLYING,
    STATE_LANDED, STATE_LAYOVER, CONF_OBSERVED, CONF_PLAN,
)

CITY = {'FRA': 'Frankfurt', 'ARN': 'Stockholm', 'BCN': 'Barcelona'}

# Tibors 3-Leg-Tag (alle Zeiten echt-UTC, wie ical_sectors sie tragen).
SECTORS = [
    {'flight': 'LH1139', 'from': 'BCN', 'to': 'FRA',
     'dep_iso': '2026-07-09T06:40:00Z', 'arr_iso': '2026-07-09T08:55:00Z'},
    {'flight': 'LH802', 'from': 'FRA', 'to': 'ARN',
     'dep_iso': '2026-07-09T10:10:00Z', 'arr_iso': '2026-07-09T12:30:00Z'},
    {'flight': 'LH803', 'from': 'ARN', 'to': 'FRA',
     'dep_iso': '2026-07-09T13:10:00Z', 'arr_iso': '2026-07-09T15:20:00Z'},
]


def _utc(h, m, day=9):
    return datetime(2026, 7, day, h, m, tzinfo=timezone.utc)


def _obs(by_flight):
    def _lookup(fno, frm, to):
        return by_flight.get(fno)
    return _lookup


def _live(by_flight):
    def _lookup(fno, frm, to):
        return by_flight.get(fno)
    return _lookup


def _resolve(now, obs=None, live=None, sectors=SECTORS, **kw):
    kw.setdefault('homebase', 'FRA')
    kw.setdefault('city_lookup', lambda c: CITY.get(c))
    return resolve_crew_live_state(sectors, _obs(obs or {}), _live(live or {}),
                                   now, **kw)


# ── Tibors Tag: 5 Zeitpunkte ─────────────────────────────────────────────────

def test_0700_fliegt_leg1_beobachtet():
    r = _resolve(_utc(7, 0), obs={'LH1139': {'status': 'airborne'}})
    assert r['state'] == STATE_FLYING
    assert r['leg_index'] == 0
    assert r['confidence'] == CONF_OBSERVED
    assert r['text']['title'] == 'Fliegt gerade'
    assert r['text']['subtitle'] == 'BCN → FRA · Ankunft 08:55'
    assert r['current_leg']['flight_no'] == 'LH1139'
    assert r['current_leg']['dep'] == 'BCN' and r['current_leg']['arr'] == 'FRA'


def test_0700_fliegt_leg1_nur_plan():
    r = _resolve(_utc(7, 0))
    assert r['state'] == STATE_FLYING
    assert r['confidence'] == CONF_PLAN
    assert r['text']['subtitle'] == 'BCN → FRA · Ankunft 08:55'


def test_0930_gelandet_wartet_auf_leg2():
    # 09:30 liegt noch IM 40-min-Delay-Puffer von Leg 1 (08:55+40=09:35) —
    # die beobachtete Landung (Board schlägt Uhr) beendet den Leg trotzdem.
    r = _resolve(_utc(9, 30), obs={'LH1139': {'status': 'Gelandet'}})
    assert r['state'] == STATE_LANDED
    assert r['leg_index'] == 1
    assert r['text']['title'] == 'Gelandet in Frankfurt'
    assert r['text']['subtitle'] == 'Wartet auf LH802 · 10:10'
    assert r['current_leg']['flight_no'] == 'LH802'
    assert r['confidence'] == CONF_OBSERVED


def test_1059_fliegt_leg2():
    r = _resolve(_utc(10, 59), obs={'LH1139': {'status': 'Gelandet'}})
    assert r['state'] == STATE_FLYING
    assert r['leg_index'] == 1
    assert r['text']['title'] == 'Fliegt gerade'
    assert r['text']['subtitle'] == 'FRA → ARN · Ankunft 12:30'


def test_1245_gelandet_arn_wartet_auf_leg3():
    r = _resolve(_utc(12, 45), obs={'LH1139': {'status': 'Gelandet'},
                                    'LH802': {'status': 'landed'}})
    assert r['state'] == STATE_LANDED
    assert r['leg_index'] == 2
    assert r['text']['title'] == 'Gelandet in Stockholm'
    assert r['text']['subtitle'] == 'Wartet auf LH803 · 13:10'


def test_1400_fliegt_leg3():
    r = _resolve(_utc(14, 0), obs={'LH1139': {'status': 'Gelandet'},
                                   'LH802': {'status': 'landed'}})
    assert r['state'] == STATE_FLYING
    assert r['leg_index'] == 2
    assert r['text']['subtitle'] == 'ARN → FRA · Ankunft 15:20'


def test_1630_frisch_gelandet_homebase_feierabend():
    obs = {'LH1139': {'status': 'Gelandet'}, 'LH802': {'status': 'landed'},
           'LH803': {'status': 'arrived'}}
    r = _resolve(_utc(16, 30), obs=obs)
    assert r['state'] == STATE_LANDED
    assert r['text']['title'] == 'Gelandet in Frankfurt'
    assert r['text']['subtitle'] == 'Feierabend'
    assert r['confidence'] == CONF_OBSERVED


def test_1800_feierabend_home():
    obs = {'LH1139': {'status': 'Gelandet'}, 'LH802': {'status': 'landed'},
           'LH803': {'status': 'arrived'}}
    r = _resolve(_utc(18, 0), obs=obs)
    assert r['state'] == STATE_HOME
    assert r['text']['title'] == 'Feierabend'


def test_tagesende_outstation_layover():
    # Gleicher Tag, aber letzter Leg endet NICHT an der Homebase → Layover.
    secs = SECTORS[:2]   # Tag endet in ARN
    obs = {'LH1139': {'status': 'Gelandet'}, 'LH802': {'status': 'landed'}}
    r = _resolve(_utc(18, 0), obs=obs, sectors=secs)
    assert r['state'] == STATE_LAYOVER
    assert r['text']['title'] == 'Layover Stockholm'


# ── kein Dienst / standby / Ruhetag ──────────────────────────────────────────

def test_kein_dienst_basis_frankfurt():
    r = _resolve(_utc(12, 0), sectors=[])
    assert r['state'] == STATE_HOME
    assert r['text']['title'] == 'Basis Frankfurt'
    assert r['current_leg'] is None and r['position'] is None


def test_kein_dienst_ohne_homebase():
    r = _resolve(_utc(12, 0), sectors=[], homebase=None)
    assert r['state'] == STATE_HOME
    assert r['text']['title'] == 'Kein Dienst'


def test_ruhetag_layover():
    r = _resolve(_utc(12, 0), sectors=[], layover_iata='BCN')
    assert r['state'] == STATE_LAYOVER
    assert r['text']['title'] == 'Layover Barcelona'


def test_standby():
    r = _resolve(_utc(12, 0), sectors=[], duty='standby')
    assert r['state'] == STATE_STANDBY
    assert r['text']['title'] == 'Standby'
    assert r['text']['subtitle'] == 'Basis Frankfurt'


# ── aircraft_live-Beweis schlägt die Uhr — in BEIDE Richtungen ───────────────

def test_airborne_beweis_schlaegt_uhr_nach_plan_ankunft():
    # 16:05 > Plan-Ankunft 15:20 + 40-min-Puffer → Uhr sagt „geflogen"; der
    # GRATIS-Store beweist: die Maschine fliegt NOCH → flying (kein Teleport).
    obs = {'LH1139': {'status': 'Gelandet'}, 'LH802': {'status': 'landed'}}
    live = {'LH803': {'lat': 57.1, 'lon': 15.2, 'ts': 1783000000.0,
                      'source': 'aircraft_live', 'on_ground': False,
                      'near_dep': False, 'near_arr': False}}
    r = _resolve(_utc(16, 5), obs=obs, live=live)
    assert r['state'] == STATE_FLYING
    assert r['leg_index'] == 2
    assert r['confidence'] == CONF_OBSERVED
    assert r['position'] is not None
    assert r['position']['lat'] == 57.1
    assert r['position']['source'] == 'aircraft_live'


def test_boden_nahe_dep_schlaegt_plan_fenster():
    # 07:30 liegt IM Plan-Fenster von Leg 1 — aber die Maschine steht
    # nachweislich noch am Boden in BCN → wartet (kein Geister-Flieger).
    live = {'LH1139': {'lat': 41.3, 'lon': 2.08, 'ts': 1783000000.0,
                       'source': 'aircraft_live', 'on_ground': True,
                       'near_dep': True, 'near_arr': False}}
    r = _resolve(_utc(7, 30), live=live)
    assert r['state'] == STATE_PRE_FLIGHT
    assert r['leg_index'] == 0
    assert r['confidence'] == CONF_OBSERVED
    assert r['text']['title'] == 'Wartet auf LH1139 · 06:40'
    assert r['text']['subtitle'] == 'BCN → FRA'
    assert r['position'] is None   # am Boden = keine Live-Flug-Position


def test_boden_nahe_arr_im_fenster_frueher_gelandet():
    # Im Plan-Fenster von Leg 1, Maschine steht schon in FRA → Leg beendet,
    # Crew wartet auf Leg 2.
    live = {'LH1139': {'lat': 50.03, 'lon': 8.56, 'ts': 1783000000.0,
                       'source': 'aircraft_live', 'on_ground': True,
                       'near_dep': False, 'near_arr': True}}
    r = _resolve(_utc(8, 30), live=live)
    assert r['state'] == STATE_LANDED
    assert r['leg_index'] == 1
    assert r['text']['subtitle'] == 'Wartet auf LH802 · 10:10'


# ── Board-Obs: Delay & Cancelled ─────────────────────────────────────────────

def test_beobachteter_delay_verschiebt_ankunft():
    # 09:00 > Plan-Ankunft 08:55, aber beobachtete +30 min → fliegt noch,
    # Text zeigt die delay-korrigierte Ankunft.
    r = _resolve(_utc(9, 0), obs={'LH1139': {'arr_delay_min': 30}})
    assert r['state'] == STATE_FLYING
    assert r['text']['subtitle'] == 'BCN → FRA · Ankunft 09:25'


def test_beobachteter_dep_delay_haelt_am_boden():
    # 06:50 > Plan-Abflug 06:40, aber beobachtete +45 min am Abflug →
    # wartet noch in BCN (eff_dep 07:25), observed.
    r = _resolve(_utc(6, 50), obs={'LH1139': {'dep_delay_min': 45}})
    assert r['state'] == STATE_PRE_FLIGHT
    assert r['confidence'] == CONF_OBSERVED
    assert r['text']['title'] == 'Wartet auf LH1139 · 07:25'


def test_cancelled_pinnt_an_den_abflughafen():
    r = _resolve(_utc(9, 0), obs={'LH1139': {'cancelled': True}})
    assert r['state'] == STATE_PRE_FLIGHT
    assert r['text']['title'] == 'LH1139 annulliert'
    assert r['text']['subtitle'] == 'In Barcelona'


# ── pick_fresher_sectors: Briefing schlägt stalen Snapshot ──────────────────

_SNAP = [{'flight': 'LH1', 'from': 'FRA', 'to': 'BCN',
          'dep_iso': '2026-07-09T06:00:00Z'}]
_BRIEF = [{'flight': 'LH2', 'from': 'FRA', 'to': 'ARN',
           'dep_iso': '2026-07-09T09:00:00Z'}]


def test_briefing_frischer_schlaegt_snapshot():
    secs, src = pick_fresher_sectors(_SNAP, '2026-07-08T10:00:00',
                                     _BRIEF, '2026-07-09T05:00:00+00:00')
    assert src == 'briefing' and secs == _BRIEF


def test_snapshot_frischer_bleibt():
    secs, src = pick_fresher_sectors(_SNAP, '2026-07-09T10:00:00',
                                     _BRIEF, '2026-07-08T05:00:00+00:00')
    assert src == 'snapshot' and secs == _SNAP


def test_snapshot_ohne_ts_briefing_gewinnt():
    secs, src = pick_fresher_sectors(_SNAP, None, _BRIEF,
                                     '2026-07-09T05:00:00+00:00')
    assert src == 'briefing' and secs == _BRIEF


def test_nur_snapshot_vorhanden():
    secs, src = pick_fresher_sectors(_SNAP, '2026-07-09T10:00:00', None, None)
    assert src == 'snapshot' and secs == _SNAP


def test_nur_briefing_vorhanden():
    secs, src = pick_fresher_sectors(None, None, _BRIEF, None)
    assert src == 'briefing' and secs == _BRIEF


def test_beide_ohne_ts_snapshot_default():
    secs, src = pick_fresher_sectors(_SNAP, None, _BRIEF, None)
    assert src == 'snapshot' and secs == _SNAP


# ── Consumer-Wiring (ADDITIV, gemockt — kein Netz/DB) ────────────────────────

import app as A                                          # noqa: E402
import blueprints.family_watch as FW                     # noqa: E402


@pytest.fixture(autouse=True)
def _pin_app_module():
    # sys.modules['app']-Pin gegen test_calculation-Reimport-Kontamination
    # (gleiches Muster wie test_leg_tail_enrichment).
    import sys
    prev = sys.modules.get('app')
    sys.modules['app'] = A
    A._FRIENDS_TODAY_MEMO.clear()
    yield
    A._FRIENDS_TODAY_MEMO.clear()
    if prev is not None:
        sys.modules['app'] = prev


def _iso_z(d):
    return d.strftime('%Y-%m-%dT%H:%M:%SZ')


def test_friends_today_payload_traegt_crew_state():
    from datetime import timedelta
    fr = 'AT-CREWSTATE-TEST-1'
    today = _date.today().isoformat()
    now = datetime.now(timezone.utc)
    day = {
        'datum': today, 'klass': 'Z72', 'marker': 'FLUG',
        'routing': 'FRA-ARN',
        'reader_facts': {},           # reiner iCal-Freund: KEINE flight_numbers
        'ical_sectors': [{'flight': 'LH802', 'from': 'FRA', 'to': 'ARN',
                          'dep_iso': _iso_z(now - timedelta(hours=1)),
                          'arr_iso': _iso_z(now + timedelta(hours=1))}],
    }
    with patch.object(A, '_friends_load', return_value={'friends': [fr]}), \
         patch.object(A, '_profiles_load_bulk',
                      return_value={fr: {'name': 'Tibor', 'homebase': 'FRA'}}), \
         patch.object(A, '_maybe_refresh_calendar_feed'), \
         patch.object(A, '_roster_snapshot_read',
                      return_value={'taken_at': _iso_z(now), 'tage': [day]}), \
         patch.object(A, '_friend_briefing_day_sectors',
                      return_value=(None, None)), \
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
    assert cs is not None, 'crew_state fehlt im friends-today-Payload'
    assert cs['state'] == STATE_FLYING
    assert cs['text']['title'] == 'Fliegt gerade'
    assert cs['current_leg']['flight_no'] == 'LH802'
    # Altfelder unverändert vorhanden (ADDITIV, alte Builds brechen nicht).
    for k in ('routing', 'layover', 'flights_live', 'flight_numbers'):
        assert k in data['friends_today'][0]


def test_family_status_traegt_crew_state_ohne_sb():
    # SB down → Roster-Zweig läuft nicht (prim/active_day undefiniert) —
    # der crew_state-Block darf trotzdem nie werfen und liefert den ehrlichen
    # Leg-losen Zustand (Basis/kein Dienst).
    with patch.object(FW, '_get_sb', return_value=(False, None)), \
         patch.object(FW, '_load_crew_profile',
                      return_value={'homebase': 'FRA'}), \
         patch.object(A, '_profile_load', return_value={}):
        status = FW._load_crew_status_for_family('AT-CREWSTATE-TEST-2',
                                                 {'next_flight'})
    cs = status.get('crew_state')
    assert cs is not None, 'crew_state fehlt im Family-Status'
    assert cs['state'] == STATE_HOME
    assert cs['text']['title'].startswith('Basis')


def test_family_crew_state_privacy_gate():
    # Ohne next_flight-Grant bleibt crew_state None (Defense-in-Depth).
    with patch.object(FW, '_get_sb', return_value=(False, None)), \
         patch.object(FW, '_load_crew_profile',
                      return_value={'homebase': 'FRA'}), \
         patch.object(A, '_profile_load', return_value={}):
        status = FW._load_crew_status_for_family('AT-CREWSTATE-TEST-3', set())
    assert status.get('crew_state') is None


# ── Nachfix 2026-07-10: Frische-Vergleich in BEIDE Richtungen korrekt ────────
# Fund 1: user_ical_briefings.updated_at fror auf der Erst-Import-Zeit ein
#   (nur `default now()` beim INSERT, kein UPDATE-Trigger, Upsert-Payload ohne
#   updated_at → PostgREST ON CONFLICT DO UPDATE bumpte es nie).
# Fund 2: In-Memory-_store-Pfad in get_friends_today ließ snap_ts=None →
#   pick_fresher_sectors ließ JEDES (auch uraltes) Briefing gewinnen.


def test_ical_briefings_upsert_schreibt_updated_at():
    """Upsert-Payload MUSS updated_at tragen, sonst bleibt die Spalte für
    existierende (token,datum)-Rows für immer auf der Erst-Import-Zeit."""
    captured = []

    class _Exec:
        def execute(self):
            return None

    class _Tbl:
        def upsert(self, rows, on_conflict=None):
            assert on_conflict == 'token,datum'
            captured.extend(rows)
            return _Exec()

    class _Sb:
        def table(self, name):
            assert name == 'user_ical_briefings'
            return _Tbl()

    events = {
        '2026-07-10': {'ical_summary': 'FLUG FRA-ARN',
                       'ical_sectors': [{'flight': 'LH802'}]},
        '2026-07-11': {'ical_summary': 'FREI'},
    }
    with patch.object(A, 'SB_AVAILABLE', True), patch.object(A, 'sb', _Sb()):
        ok = A._ical_briefings_save_to_supabase('AT-UPDATEDAT-TEST', events)
    assert ok is True
    assert len(captured) == 2
    for row in captured:
        assert 'updated_at' in row, 'updated_at fehlt im Upsert-Payload'
        ts = datetime.fromisoformat(row['updated_at'])
        assert ts.tzinfo is not None, 'updated_at muss aware-UTC sein'


def _instore_friends_today(fr, store_sectors, snap_taken_at,
                           brief_sectors, brief_ts, today):
    """friends-today mit tagen aus dem In-Memory-_store (kein Snapshot-Fallback)."""
    day = {'datum': today, 'klass': 'Z72', 'marker': 'FLUG',
           'routing': 'FRA-XXX', 'reader_facts': {},
           'ical_sectors': store_sectors}
    store = {fr: {'result_data': {'_tage_detail': [day]}}}
    with patch.object(A, '_friends_load', return_value={'friends': [fr]}), \
         patch.object(A, '_profiles_load_bulk',
                      return_value={fr: {'name': 'Tibor', 'homebase': 'FRA'}}), \
         patch.object(A, '_maybe_refresh_calendar_feed'), \
         patch.object(A, '_roster_snapshot_read',
                      return_value={'taken_at': snap_taken_at, 'tage': []}), \
         patch.object(A, '_friend_briefing_day_sectors',
                      return_value=(brief_sectors, brief_ts)), \
         patch.object(A, '_flight_obs_merged', return_value=None), \
         patch('blueprints.aerox_data_blueprint._aircraft_live_pos',
               return_value=(None, None, None, None)), \
         patch.dict(A._store, store, clear=True):
        with A.app.test_request_context(
                f'/api/user/friends-today/{fr}?datum={today}'):
            resp = A.get_friends_today(fr)
    return resp.get_json()


def test_friends_today_instore_frisch_schlaegt_stales_briefing():
    """In-Memory-Pfad: frisch gepushte Daten (Snapshot-taken_at = jetzt) dürfen
    NICHT gegen ein tagealtes Briefing verlieren (vorher: snap_ts=None →
    Briefing gewann IMMER, weil updated_at NOT NULL ist)."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    today = _date.today().isoformat()
    fresh = [{'flight': 'LH802', 'from': 'FRA', 'to': 'ARN',
              'dep_iso': _iso_z(now - timedelta(hours=1)),
              'arr_iso': _iso_z(now + timedelta(hours=1))}]
    stale = [{'flight': 'LH999', 'from': 'FRA', 'to': 'OSL',
              'dep_iso': _iso_z(now - timedelta(hours=1)),
              'arr_iso': _iso_z(now + timedelta(hours=1))}]
    data = _instore_friends_today(
        'AT-INSTORE-FRESH-1', fresh, _iso_z(now),
        stale, _iso_z(now - timedelta(days=3)), today)
    assert data['count'] == 1
    cs = data['friends_today'][0].get('crew_state')
    assert cs is not None
    assert cs['state'] == STATE_FLYING
    assert cs['current_leg']['flight_no'] == 'LH802', \
        'stales Briefing hat frische In-Memory-Daten überstimmt'


def test_friends_today_instore_frischeres_briefing_gewinnt_weiter():
    """Gegenrichtung bleibt intakt: ein Briefing das FRISCHER als der Snapshot
    ist, schlägt die In-Memory-Daten weiterhin (Kern des Original-Fixes)."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    today = _date.today().isoformat()
    old = [{'flight': 'LH802', 'from': 'FRA', 'to': 'ARN',
            'dep_iso': _iso_z(now - timedelta(hours=1)),
            'arr_iso': _iso_z(now + timedelta(hours=1))}]
    fresher = [{'flight': 'LH777', 'from': 'FRA', 'to': 'MAD',
                'dep_iso': _iso_z(now - timedelta(hours=1)),
                'arr_iso': _iso_z(now + timedelta(hours=1))}]
    data = _instore_friends_today(
        'AT-INSTORE-FRESH-2', old, _iso_z(now - timedelta(days=2)),
        fresher, _iso_z(now), today)
    assert data['count'] == 1
    cs = data['friends_today'][0].get('crew_state')
    assert cs is not None
    assert cs['current_leg']['flight_no'] == 'LH777'


def test_airborne_obs_ohne_ankunftsboard_kippt_zum_naechsten_leg():
    """Tibor-Livefall 13:39: LH802 dep-seitig 'Abgeflogen' (ARN hat kein
    Ankunfts-Board -> nie 'landed'), Plan-Ankunft 10:30Z lange vorbei,
    LH803 laeuft laengst -> der Resolver muss auf Leg 3 weiterruecken."""
    import datetime as dt
    from blueprints.crew_live_state import resolve_crew_live_state
    tz = dt.timezone.utc
    day = dt.date(2026, 7, 10)

    def iso(h, m):
        return dt.datetime(day.year, day.month, day.day, h, m, tzinfo=tz).isoformat()

    sectors = [
        {'flight': 'LH1139', 'from': 'BCN', 'to': 'FRA', 'dep_iso': iso(4, 40), 'arr_iso': iso(6, 55)},
        {'flight': 'LH802', 'from': 'FRA', 'to': 'ARN', 'dep_iso': iso(8, 25), 'arr_iso': iso(10, 30)},
        {'flight': 'LH803', 'from': 'ARN', 'to': 'FRA', 'dep_iso': iso(11, 10), 'arr_iso': iso(13, 20)},
    ]
    obs = {
        'LH1139': {'status': 'baggage delivery finished'},
        'LH802': {'status': 'Abgeflogen'},          # dep-seitig, ewig ohne arr-Board
        'LH803': {'status': 'gestartet'},
    }
    now = dt.datetime(2026, 7, 10, 11, 39, tzinfo=tz)   # 13:39 CEST
    res = resolve_crew_live_state(
        sectors,
        obs_lookup=lambda fn, d, a: obs.get(fn),
        live_lookup=lambda fn, d, a: None,
        now=now)
    assert res['state'] == 'flying'
    assert res['current_leg']['flight_no'] == 'LH803'
    assert res['current_leg']['dep'] == 'ARN' and res['current_leg']['arr'] == 'FRA'
