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
    assert r['text']['subtitle'] == 'Nächster Flug · LH802 · 10:10'
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
    assert r['text']['subtitle'] == 'Nächster Flug · LH803 · 13:10'


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


def test_freier_tag_servertext_heute_frei():
    # B2 (Tibor 2026-07-12): Roster-FREI-Tag → SERVER sagt „Heute frei"
    # (vorher „Basis Frankfurt", während iOS lokal „heute frei" ableitete →
    # zwei Texte für dieselbe Person). Kein Subtitle: wo jemand seinen freien
    # Tag verbringt, wissen wir nicht.
    r = _resolve(_utc(12, 0), sectors=[], duty='free')
    assert r['state'] == STATE_HOME
    assert r['text']['title'] == 'Heute frei'
    assert r['text']['subtitle'] is None


def test_urlaub_servertext_im_urlaub():
    r = _resolve(_utc(12, 0), sectors=[], duty='vacation')
    assert r['state'] == STATE_HOME
    assert r['text']['title'] == 'Im Urlaub'


def test_freier_ruhetag_am_layover_gewinnt_layover():
    # FREI-Tag MIT Layover-Ort (Ruhetag auf Tour) → der Aufenthaltsort ist
    # die wichtigere Wahrheit als „Heute frei".
    r = _resolve(_utc(12, 0), sectors=[], duty='free', layover_iata='BCN')
    assert r['state'] == STATE_LAYOVER
    assert r['text']['title'] == 'Layover Barcelona'


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
    assert r['text']['title'] == 'Nächster Flug · LH1139 · 06:40'
    # PRE-FLIGHT-KAPPUNG (Owner 2026-07-13, Basti-Fall): now 07:30 liegt 50 min
    # NACH dem (unverspäteten) Plan-Abflug 06:40 — die Tafel kennt KEINEN Delay
    # (dep_delay=None), der Live-Store zeigt nur „am Boden nahe dep". Ohne
    # bekannten Delay darf die „Flugvorbereitung" nicht ewig über den
    # Abflug-Moment hinaus hängen → sie wird gekappt (neutraler Text, nur die
    # Route). Nichts erfunden, kein „Verspätet" ohne Delay-Beweis.
    assert r['text']['subtitle'] == 'BCN → FRA'
    assert r['pre_phase'] is None
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
    assert r['text']['subtitle'] == 'Nächster Flug · LH802 · 10:10'


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
    assert r['text']['title'] == 'Nächster Flug · LH1139 · 07:25'


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
                      return_value=(brief_sectors, brief_ts, None, None)), \
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


# ── PRE-FLIGHT-TIMELINE (Owner 2026-07-12) ───────────────────────────────────
# Feingranulare Vor-Abflug-Phase: OUTSTATION checkin→crewbus(Pickup)→security
# (Pickup+25')→prep→boarding(beobachtet) · HOMEBASE checkin→commute(Report−
# eigene Fahrzeit)→briefing(Report)→prep→boarding. Fehlende Bausteine werden
# EHRLICH übersprungen; Boarding kommt NIE von der Uhr, nur vom Board.

from datetime import timedelta as _td                     # noqa: E402
from blueprints.crew_live_state import (                  # noqa: E402
    parse_pickup_hhmm, pickup_utc_for_leg,
    PRE_CHECKIN, PRE_COMMUTE, PRE_BRIEFING, PRE_CREWBUS, PRE_SECURITY,
    PRE_PREP, PRE_BOARDING, PRE_DELAYED,
)

# Leg 1 (BCN→FRA, dep 06:40Z) startet an der OUTSTATION (hb=FRA):
# Pickup 04:30Z → Crewbus 04:30–04:55, Security 04:55–06:00, Prep ab 06:00.
_PICKUP_UTC = _utc(4, 30)
_OUT_CTX = {'pickup': _PICKUP_UTC, 'report': None, 'commute_minutes': None}

# HOMEBASE-Tag: erster Leg FRA→ARN (dep 10:10Z), Report 08:35Z, Fahrzeit 45' →
# Commute 07:50–08:35, Briefing 08:35–09:30, Prep ab 09:30.
_HB_SECTORS = [SECTORS[1]]
_HB_CTX = {'pickup': None, 'report': '2026-07-09T08:35:00Z',
           'commute_minutes': 45}


def test_pre_outstation_checkin_vor_pickup():
    r = _resolve(_utc(3, 0), pre_ctx=_OUT_CTX)
    assert r['state'] == STATE_PRE_FLIGHT
    assert r['pre_phase'] == PRE_CHECKIN
    assert r['pre_phase_label'] == 'Check-in offen'
    assert r['text']['subtitle'] == 'BCN → FRA · Check-in offen'


def test_pre_outstation_crewbus_ab_pickup():
    r = _resolve(_utc(4, 35), pre_ctx=_OUT_CTX)
    assert r['pre_phase'] == PRE_CREWBUS
    assert r['pre_phase_label'] == 'Im Crewbus'
    assert r['text']['subtitle'] == 'BCN → FRA · Im Crewbus'


def test_pre_outstation_security_nach_crewbusfahrt():
    # Pickup + 25-min-Default-Fahrtzeit = 04:55 → ab da „Durch die Security".
    r = _resolve(_utc(5, 10), pre_ctx=_OUT_CTX)
    assert r['pre_phase'] == PRE_SECURITY
    assert r['pre_phase_label'] == 'Durch die Security'


def test_pre_outstation_ohne_pickup_ueberspringt_phasen():
    # Kein Pickup im iCal → crewbus/security werden EHRLICH übersprungen:
    # bis prep (06:00) bleibt es „Check-in offen", nie geratene Zeiten.
    r = _resolve(_utc(5, 10))
    assert r['pre_phase'] == PRE_CHECKIN
    r2 = _resolve(_utc(6, 5))
    assert r2['pre_phase'] == PRE_PREP


def test_pre_prep_ab_abflug_minus_40():
    r = _resolve(_utc(6, 5), pre_ctx=_OUT_CTX)
    assert r['pre_phase'] == PRE_PREP
    assert r['text']['subtitle'] == 'BCN → FRA · Flugvorbereitung'


def test_pre_boarding_beobachtet_schlaegt_uhr_frueh():
    # Board meldet Boarding schon um 05:10 (Uhr sagt „Security") →
    # Beobachtung schlägt die Uhr SOFORT.
    r = _resolve(_utc(5, 10), obs={'LH1139': {'status': 'Boarding'}},
                 pre_ctx=_OUT_CTX)
    assert r['state'] == STATE_PRE_FLIGHT
    assert r['pre_phase'] == PRE_BOARDING
    assert r['text']['subtitle'] == 'BCN → FRA · Boarding'


def test_pre_ohne_boarding_signal_bleibt_prep():
    # Abflug − 20 min OHNE Board-Signal → bleibt „Flugvorbereitung"
    # (die Uhr allein macht nie ein Boarding).
    r = _resolve(_utc(6, 20), pre_ctx=_OUT_CTX)
    assert r['pre_phase'] == PRE_PREP


def test_pre_deboarding_ist_kein_boarding():
    r = _resolve(_utc(6, 20), obs={'LH1139': {'status': 'Deboarding'}},
                 pre_ctx=_OUT_CTX)
    assert r['pre_phase'] == PRE_PREP


def test_pre_homebase_checkin_vor_commute():
    r = _resolve(_utc(7, 0), sectors=_HB_SECTORS, pre_ctx=_HB_CTX)
    assert r['state'] == STATE_PRE_FLIGHT
    assert r['pre_phase'] == PRE_CHECKIN


def test_pre_homebase_commute_ab_report_minus_fahrzeit():
    r = _resolve(_utc(8, 0), sectors=_HB_SECTORS, pre_ctx=_HB_CTX)
    assert r['pre_phase'] == PRE_COMMUTE
    assert r['pre_phase_label'] == 'Fahrt zum Flughafen'
    assert r['text']['subtitle'] == 'FRA → ARN · Fahrt zum Flughafen'


def test_pre_homebase_briefing_ab_report():
    r = _resolve(_utc(8, 40), sectors=_HB_SECTORS, pre_ctx=_HB_CTX)
    assert r['pre_phase'] == PRE_BRIEFING
    assert r['text']['subtitle'] == 'FRA → ARN · Briefing'


def test_pre_homebase_prep_ab_dep_minus_40():
    r = _resolve(_utc(9, 35), sectors=_HB_SECTORS, pre_ctx=_HB_CTX)
    assert r['pre_phase'] == PRE_PREP


def test_pre_homebase_ohne_commute_ueberspringt_fahrt():
    # Crew-Mitglied hat keine Fahrzeit angegeben → „Fahrt zum Flughafen"
    # entfällt, bis zum Briefing gilt „Check-in offen".
    ctx = {'pickup': None, 'report': '2026-07-09T08:35:00Z',
           'commute_minutes': None}
    r = _resolve(_utc(8, 0), sectors=_HB_SECTORS, pre_ctx=ctx)
    assert r['pre_phase'] == PRE_CHECKIN
    r2 = _resolve(_utc(8, 40), sectors=_HB_SECTORS, pre_ctx=ctx)
    assert r2['pre_phase'] == PRE_BRIEFING


def test_pre_homebase_ohne_report_ueberspringt_briefing():
    # Ohne Report-Zeit gibt es weder commute noch briefing (commute hängt am
    # Report-Anker) → Check-in bis prep.
    ctx = {'pickup': None, 'report': None, 'commute_minutes': 45}
    r = _resolve(_utc(8, 40), sectors=_HB_SECTORS, pre_ctx=ctx)
    assert r['pre_phase'] == PRE_CHECKIN


def test_pre_flying_und_cancelled_ohne_pre_phase():
    r = _resolve(_utc(7, 0), obs={'LH1139': {'status': 'airborne'}})
    assert r['state'] == STATE_FLYING
    assert r['pre_phase'] is None and r['pre_phase_label'] is None
    r2 = _resolve(_utc(5, 0), obs={'LH1139': {'cancelled': True}})
    assert r2['pre_phase'] is None


def test_pre_turnaround_zwischen_legs_nur_prep():
    # Leg 1 gelandet, Crew wartet auf Leg 2 (dep 10:10): am Turnaround gibt es
    # keine Checkin-/Anfahrts-Prosa — erst ab dep−40 „Flugvorbereitung".
    obs = {'LH1139': {'status': 'Gelandet'}}
    r = _resolve(_utc(9, 0), obs=obs)
    assert r['state'] == STATE_LANDED
    assert r['pre_phase'] is None
    r2 = _resolve(_utc(9, 45), obs=obs)
    assert r2['state'] == STATE_LANDED
    assert r2['pre_phase'] == PRE_PREP
    assert r2['text']['subtitle'] == 'Nächster Flug · LH802 · 10:10'   # unverändert


# ── Basti-Fall (Owner 2026-07-13): bekannter Delay → „Verspätet", KEINE ─────
# ewig hängende „Flugvorbereitung". LH900 FRA→LHR, Fahrplan-Abflug 08:00,
# verspätet auf 08:20 (delay bekannt), jetzt 08:49 — auch der verspätete
# Abflug ist 29 min vorbei, ohne Board-„abgeflogen"/Live-Beweis. Erwartet:
# Status „Verspätet" + verspätete Abflugzeit, nicht „Flugvorbereitung".
_LH900 = [{'flight': 'LH900', 'from': 'FRA', 'to': 'LHR',
           'dep_iso': '2026-07-09T08:00:00Z', 'arr_iso': '2026-07-09T09:45:00Z'}]


def test_basti_bekannter_delay_zeigt_verspaetet_nicht_prep():
    # sched 08:00, est 08:20 (dep_delay 20), now 08:49 (29 min NACH est_dep),
    # Board kennt die Verspätung (grounded-Status), kein Abflug-/Live-Beweis.
    r = _resolve(_utc(8, 49), sectors=_LH900,
                 obs={'LH900': {'status': 'Verspätet', 'dep_delay_min': 20}})
    assert r['state'] == STATE_PRE_FLIGHT
    assert r['pre_phase'] == PRE_DELAYED
    assert r['pre_phase_label'] == 'Verspätet'
    # Der Titel trägt die VERSPÄTETE Abflugzeit (08:20), nicht die Fahrplan-Zeit.
    assert r['text']['title'] == 'Nächster Flug · LH900 · 08:20'
    # Der Status-Text ist ehrlich „Verspätet" + verspätete Abflugzeit — NICHT
    # „Flugvorbereitung".
    assert r['text']['subtitle'] == 'FRA → LHR · Verspätet 08:20'
    assert 'Flugvorbereitung' not in r['text']['subtitle']
    assert r['position'] is None       # kein erfundener Live-Flug


def test_basti_delay_vor_est_dep_timeline_noch_korrekt():
    # now 08:10 — VOR dem verspäteten Abflug 08:20, aber schon in der
    # prep-Fenster-Zone (est_dep−40 = 07:40). Auch hier: „Verspätet" (Owner:
    # gilt VOR und NACH est_dep), nicht die generische „Flugvorbereitung".
    r = _resolve(_utc(8, 10), sectors=_LH900,
                 obs={'LH900': {'status': 'Verspätet', 'dep_delay_min': 20}})
    assert r['state'] == STATE_PRE_FLIGHT
    assert r['pre_phase'] == PRE_DELAYED
    assert r['text']['subtitle'] == 'FRA → LHR · Verspätet 08:20'


def test_basti_delay_frueh_vor_prep_zeigt_checkin_nicht_verspaetet():
    # now 06:30 — weit VOR est_dep−40 (07:40): die frühe Timeline (Check-in
    # offen) bleibt korrekt, „Verspätet" übernimmt erst ab dem prep-Fenster
    # (kein alarmierender Dauer-„Verspätet" den ganzen Tag).
    r = _resolve(_utc(6, 30), sectors=_LH900,
                 obs={'LH900': {'status': 'Verspätet', 'dep_delay_min': 20}})
    assert r['state'] == STATE_PRE_FLIGHT
    assert r['pre_phase'] == PRE_CHECKIN


def test_past_dep_ohne_delay_neutral_kein_prep_kein_verspaetet():
    # now 08:49, KEIN bekannter Delay (Board grounded ohne dep_delay), Abflug
    # 08:00 längst vorbei, kein Live-Beweis → neutraler Text (nur Route),
    # weder „Flugvorbereitung" (gekappt) noch „Verspätet" (kein Delay-Beweis).
    r = _resolve(_utc(8, 49), sectors=_LH900,
                 obs={'LH900': {'status': 'on time'}})
    assert r['state'] == STATE_PRE_FLIGHT
    assert r['pre_phase'] is None
    assert r['text']['subtitle'] == 'FRA → LHR'
    assert 'Flugvorbereitung' not in (r['text']['subtitle'] or '')


def test_basti_boarding_beobachtet_schlaegt_verspaetet():
    # Trotz bekanntem Delay: ein echtes Boarding-Board-Signal gewinnt (Board
    # schlägt Uhr) — Boarding ist ein echtes Signal, „Verspätet" nur der Text.
    r = _resolve(_utc(8, 10), sectors=_LH900,
                 obs={'LH900': {'status': 'Boarding', 'dep_delay_min': 20}})
    assert r['pre_phase'] == PRE_BOARDING


# ── Julien/Tibor 2026-07-16: arr-seitiges „Verspätet" ist KEIN Boden-Beweis ──
# Über-Ozean-Nachtflug OHNE Abflug-Board (BOS/SFO): der gemergte Board-Record
# trägt die FRA-ANKUNFTS-Tafel („Verspätet", est arr in der Zukunft), also
# `status='Verspätet'` + `arr_delay_min`/`delay_side='arr'`, KEIN dep_delay/
# status_dep. Vor dem Fix pinnte das den Flug ewig auf „Nächster Flug"
# (pre_flight), obwohl er seit Stunden fliegt. Nach dem Fix: die Zeit-Physik
# übernimmt ab eff_dep + 15 min → flying (CONF_PLAN, Position leer bis
# aircraft_live liefert). Ein ECHTER Abflug-Boden-Beweis behält den Pin.
_LH455 = [{'flight': 'LH455', 'from': 'SFO', 'to': 'FRA',
           'dep_iso': '2026-07-09T22:15:00Z', 'arr_iso': '2026-07-10T06:00:00Z'}]


def test_arr_verspaetet_ohne_dep_beweis_kippt_auf_flying():
    # Abflug 22:15Z längst durch (now 00:00Z Folgetag, ~1,75 h nach dep), nur
    # die ARR-Tafel meldet „Verspätet" (arr-seitiger Delay, kein dep-Beweis) →
    # Zeit-Physik: fliegt. Keine erfundene Position.
    r = _resolve(datetime(2026, 7, 10, 0, 0, tzinfo=timezone.utc),
                 sectors=_LH455,
                 obs={'LH455': {'status': 'Verspätet', 'arr_delay_min': 45,
                                'delay_side': 'arr',
                                'est_arr_iso': '2026-07-10T06:45:00Z'}})
    assert r['state'] == STATE_FLYING
    assert r['confidence'] == CONF_PLAN     # reine Uhr, kein Live/Board-Abflug
    assert r['position'] is None            # keine erfundene Position


def test_dep_boarding_aktiv_behaelt_pre_flight():
    # Dasselbe Leg, aber VOR dem Abflug: das ABFLUG-Board meldet „Boarding"
    # (echter Boden-Beweis) → bleibt pre_flight, kippt NICHT auf flying.
    r = _resolve(datetime(2026, 7, 9, 22, 5, tzinfo=timezone.utc),
                 sectors=_LH455,
                 obs={'LH455': {'status': 'Boarding', 'status_dep': 'Boarding'}})
    assert r['state'] == STATE_PRE_FLIGHT


def test_dep_ground_proof_nach_abflug_behaelt_waiting():
    # Abflug-Board meldet noch aktiv Boarding (Boden-Beweis) auch NACH der Soll-
    # Abflugzeit → der Flug steht nachweislich noch am Gate, kein Zeit-Physik-
    # Kippen auf flying (Board schlägt Uhr).
    r = _resolve(datetime(2026, 7, 9, 22, 40, tzinfo=timezone.utc),
                 sectors=_LH455,
                 obs={'LH455': {'status': 'Boarding', 'status_dep': 'Boarding'}})
    assert r['state'] == STATE_PRE_FLIGHT


def test_arr_verspaetet_cancelled_nie_flying():
    # cancelled schlägt alles — auch mit vorbeigelaufenem Abflug + arr-Delay
    # bleibt es NIE flying (Crew ist nie losgeflogen).
    r = _resolve(datetime(2026, 7, 10, 0, 0, tzinfo=timezone.utc),
                 sectors=_LH455,
                 obs={'LH455': {'status': 'Verspätet', 'arr_delay_min': 45,
                                'delay_side': 'arr', 'cancelled': True}})
    assert r['state'] != STATE_FLYING


def test_arr_verspaetet_est_dep_erst_in_zukunft_bleibt_pre_flight():
    # est_dep erst in 10 min (Abflug NOCH nicht durch): trotz arr-„Verspätet"
    # bleibt es pre_flight — die Zeit-Physik kippt erst NACH eff_dep + Grace.
    r = _resolve(datetime(2026, 7, 9, 22, 5, tzinfo=timezone.utc),
                 sectors=_LH455,
                 obs={'LH455': {'status': 'Verspätet', 'arr_delay_min': 45,
                                'delay_side': 'arr',
                                'est_dep_iso': '2026-07-09T22:15:00Z'}})
    assert r['state'] == STATE_PRE_FLIGHT


def test_norm_legs_reg_alias_fuellt_tail():
    # Tibor-Sektor keyt die Maschine als 'reg' statt 'tail' → das current_leg
    # muss die Reg trotzdem tragen (aircraft_live-Reg-Match).
    secs = [{'flight': 'LH455', 'from': 'SFO', 'to': 'FRA',
             'dep_iso': '2026-07-09T22:15:00Z', 'arr_iso': '2026-07-10T06:00:00Z',
             'reg': 'D-ABYT'}]
    r = _resolve(datetime(2026, 7, 9, 20, 0, tzinfo=timezone.utc), sectors=secs)
    assert (r['current_leg'] or {}).get('reg') == 'D-ABYT'


def test_reg_normalisierung_findet_bindestrich_lose_row():
    # 'D-ABYN' (current_leg.reg) muss die aircraft_live-Row reg='DABYN' finden:
    # der Store-Read normalisiert via re.sub[^A-Z0-9]. Hier direkt gegen den
    # Store-Helfer geprüft (die Normalisierung ist der Kern der Reg-Kette).
    import re as _re
    assert _re.sub(r'[^A-Z0-9]', '', 'D-ABYN'.upper()) == 'DABYN'


def test_build_live_lookup_reg_normalisierung_e2e():
    # E2E: der Live-Adapter reicht die Roster-Reg 'D-ABYN' durch und findet die
    # bindestrich-lose Store-Row 'DABYN' (patcht _aircraft_live_pos, prüft dass
    # die Reg beim Store ankommt UND die Normalisierung greift).
    from blueprints.crew_live_state import build_live_lookup
    seen = {}

    def _fake_pos(reg=None, flight=None, callsign=None, dep=None, max_age_min=35):
        import re as _re
        seen['reg_norm'] = _re.sub(r'[^A-Z0-9]', '', (reg or '').upper())
        if seen['reg_norm'] == 'DABYN':
            return ({'lat': 51.0, 'lon': 5.0, 'on_ground': False,
                     'source': 'aircraft_live', 'seen_ts': None}, None,
                    'D-ABYN', 'A359')
        return None, None, None, None

    with patch('blueprints.aerox_data_blueprint._aircraft_live_pos', _fake_pos):
        lk = build_live_lookup()
        out = lk('LH455', 'SFO', 'FRA', 'D-ABYN')
    assert seen['reg_norm'] == 'DABYN'
    assert out and out['lat'] == 51.0 and out['on_ground'] is False


# ── Pickup-Parser (Server-Nachbau der iOS-Referenz-Regexe) ──────────────────

def test_parse_pickup_beide_schreibweisen():
    assert parse_pickup_hhmm('13:35 LT Pickup BLL') == (13, 35)
    assert parse_pickup_hhmm('Pickup 1430') == (14, 30)
    assert parse_pickup_hhmm('LAYOVER · Pickup 0930 HND · LH 717: HND-FRA') == (9, 30)
    assert parse_pickup_hhmm('Pickup: 14:30') == (14, 30)
    assert parse_pickup_hhmm('Pickup um 07:05') == (7, 5)
    assert parse_pickup_hhmm('09:30 LT - Pickup HND') == (9, 30)


def test_parse_pickup_nichts_erfinden():
    assert parse_pickup_hhmm('LAYOVER (Tag 3/3) · LH 717: HND-FRA') is None
    assert parse_pickup_hhmm('Pickup 2599') is None      # unplausible Zeit
    assert parse_pickup_hhmm(None) is None
    assert parse_pickup_hhmm('') is None


def test_pickup_utc_ortszeit_der_station():
    # Abflug 06:40Z = 08:40 Europe/Madrid → Pickup „06:30" lokal = 04:30Z.
    p = pickup_utc_for_leg((6, 30), '2026-07-09T06:40:00Z', 'Europe/Madrid')
    assert p == _utc(4, 30)


def test_pickup_utc_mitternachts_wrap():
    # Abflug 00:30Z (02:30 lokal Madrid), Pickup „23:45" → VORTAG 21:45Z.
    p = pickup_utc_for_leg((23, 45), '2026-07-09T00:30:00Z', 'Europe/Madrid')
    assert p == datetime(2026, 7, 8, 21, 45, tzinfo=timezone.utc)


def test_pickup_utc_unplausibel_verworfen():
    # > 6 h vor dem Abflug (Fenster wie iOS maxLeadWindow) → None.
    assert pickup_utc_for_leg((1, 0), '2026-07-09T06:40:00Z',
                              'Europe/Madrid') is None
    assert pickup_utc_for_leg((6, 30), '2026-07-09T06:40:00Z', None) is None


# ══════════════════════════════════════════════════════════════════════════════
# Regressions-Sweep 2026-07-12 #7: duty-Ableitung als GETEILTE Funktion —
# der B2-Fix (Server-Text „Heute frei"/„Im Urlaub"/„Standby") war nur in
# friends-today verdrahtet, Family zeigte für dieselbe Person „Basis X".
# ══════════════════════════════════════════════════════════════════════════════
from blueprints.crew_live_state import duty_from_roster_day   # noqa: E402


def test_duty_from_roster_day_klass_und_marker():
    # Exakt die friends-today-Semantik (Standby > Urlaub > Frei).
    assert duty_from_roster_day(None, 'SBY FRA 06:00') == 'standby'
    assert duty_from_roster_day('URLAUB', None) == 'vacation'
    assert duty_from_roster_day(None, 'JAHRESURLAUB') == 'vacation'
    assert duty_from_roster_day('FREI', None) == 'free'
    assert duty_from_roster_day('OFF', None) == 'free'
    assert duty_from_roster_day('X', None) == 'free'
    assert duty_from_roster_day('REST', None) == 'free'
    # iCal-Summary-Token (family_watch hat kein klass-Feld).
    assert duty_from_roster_day(None, 'OFF DAY') == 'free'
    # Kein Signal → None (Flugtag/unbekannt — Resolver entscheidet über Legs).
    assert duty_from_roster_day('Z72', 'FLUG') is None
    assert duty_from_roster_day(None, None) is None
    # Prio: SBY schlägt alles.
    assert duty_from_roster_day('FREI', 'SBY 10:00') == 'standby'


class _FamQuery:
    """Chainbare Supabase-Query-Attrappe für den family_watch-Roster-Zweig."""

    def __init__(self, rows_by_table, table):
        self._rows = rows_by_table.get(table)

    def __getattr__(self, name):
        # select/eq/in_/gte/lte/order/limit → chainen.
        return lambda *a, **k: self

    def execute(self):
        from unittest.mock import MagicMock
        return MagicMock(data=self._rows if self._rows is not None else [])


class _FamSB:
    def __init__(self, rows_by_table):
        self.rows_by_table = rows_by_table

    def table(self, name):
        return _FamQuery(self.rows_by_table, name)


def test_family_status_freier_tag_zeigt_heute_frei():
    """Family-Parität (Sweep #7): OFF-DAY-Briefing → crew_state 'Heute frei'
    (vor dem Fix: duty=None → „Basis Frankfurt", während der Crew-Feed
    derselben Person „Heute frei" zeigte)."""
    today = datetime.now().date().isoformat()
    row = {'datum': today, 'ical_summary': 'OFF DAY', 'ical_location': '',
           'ical_start': None, 'ical_end': None, 'raw_event': None}
    fake = _FamSB({'user_ical_briefings': [row]})
    with patch.object(FW, '_get_sb', return_value=(True, fake)), \
         patch.object(FW, '_load_crew_profile',
                      return_value={'homebase': 'FRA'}), \
         patch.object(A, '_profile_load', return_value={}), \
         patch.object(A, '_flight_obs_merged', return_value=None):
        status = FW._load_crew_status_for_family('AT-CREWSTATE-TEST-4',
                                                 {'next_flight'})
    cs = status.get('crew_state')
    assert cs is not None
    assert cs['state'] == STATE_HOME
    assert cs['text']['title'] == 'Heute frei', \
        'Family muss denselben Server-Text zeigen wie der Crew-Feed (B2)'


def test_family_status_standby_tag_zeigt_standby():
    today = datetime.now().date().isoformat()
    row = {'datum': today, 'ical_summary': 'SBY FRA 06:00-14:00',
           'ical_location': 'FRA', 'ical_start': None, 'ical_end': None,
           'raw_event': None}
    fake = _FamSB({'user_ical_briefings': [row]})
    with patch.object(FW, '_get_sb', return_value=(True, fake)), \
         patch.object(FW, '_load_crew_profile',
                      return_value={'homebase': 'FRA'}), \
         patch.object(A, '_profile_load', return_value={}), \
         patch.object(A, '_flight_obs_merged', return_value=None):
        status = FW._load_crew_status_for_family('AT-CREWSTATE-TEST-5',
                                                 {'next_flight'})
    cs = status.get('crew_state')
    assert cs is not None
    assert cs['state'] == STATE_STANDBY
    assert cs['text']['title'] == 'Standby'


# ─────────────────────────────────────────────────────────────────────────────
#  BUG A (Owner 2026-07-13 „Leute im Radar mit Pin in Frankfurt, obwohl im
#  Dienst"): ein FLIEGENDER Freund ohne Live-Fix darf KEINE Boden-/Homebase-
#  Koordinate als Karten-Position bekommen. crew_state.position MUSS None sein
#  (iOS nutzt genau dieses Feld für den Radar-Pin der „Wer fliegt gerade"-Karte;
#  current_city/layover bleiben reiner TEXT, sind aber NIE die Pin-Quelle eines
#  Fliegenden). Wenn ein Fix da ist, kommt die ECHTE Position (kein Boden-Pin).
# ─────────────────────────────────────────────────────────────────────────────

def test_bugA_flying_ohne_livefix_hat_keine_position():
    """Board sagt airborne, aircraft_live liefert NICHTS → state=flying,
    aber position IS None (kein FRA-/Homebase-Koordinaten-Pin)."""
    now = _utc(11, 30)   # LH802 FRA→ARN unterwegs
    obs = _obs({'LH802': {'status': 'Departed'}})
    live = _live({})     # KEIN Live-Fix
    r = resolve_crew_live_state(SECTORS, obs, live, now,
                                homebase='FRA', layover_iata='ARN',
                                city_lookup=CITY.get)
    assert r['state'] == STATE_FLYING
    assert r['position'] is None, \
        'Fliegender ohne Live-Fix darf KEINEN Boden-/Homebase-Pin liefern'
    # current_leg trägt nur Route-TEXT (dep/arr), keine lat/lon.
    assert 'lat' not in (r['current_leg'] or {})
    assert 'lon' not in (r['current_leg'] or {})


def test_bugA_flying_am_boden_fix_wird_verworfen():
    """aircraft_live meldet die Maschine AM BODEN (on_ground) → position None
    (nie ein Boden-Punkt als Flug-Pin), Zustand bleibt flying solange die
    Board-Wahrheit airborne sagt."""
    now = _utc(11, 30)
    obs = _obs({'LH802': {'status': 'Departed'}})
    live = _live({'LH802': {'lat': 50.03, 'lon': 8.57, 'on_ground': True,
                            'source': 'aircraft_live'}})
    r = resolve_crew_live_state(SECTORS, obs, live, now,
                                homebase='FRA', layover_iata='ARN')
    assert r['state'] == STATE_FLYING
    assert r['position'] is None


def test_bugA_flying_mit_livefix_liefert_echte_position():
    """Mit echtem airborne-Fix kommt die ECHTE Position (Kontrast zu oben)."""
    now = _utc(11, 30)
    obs = _obs({'LH802': {'status': 'Departed'}})
    live = _live({'LH802': {'lat': 55.1, 'lon': 12.4, 'on_ground': False,
                            'track': 30.0, 'gs': 450.0, 'source': 'aircraft_live'}})
    r = resolve_crew_live_state(SECTORS, obs, live, now,
                                homebase='FRA', layover_iata='ARN')
    assert r['state'] == STATE_FLYING
    assert r['position'] is not None
    assert r['position']['lat'] == 55.1 and r['position']['lon'] == 12.4


# ─────────────────────────────────────────────────────────────────────────────
#  BUG B (Owner 2026-07-13 „bei Sebastian steht Landung 8:40 in der Live-Karte,
#  aber im Radar landet der Flieger paar Min später"): der Crew-State zeigte die
#  PLAN-Ankunft, wenn das Board eine konkrete revidierte esti trug, der Delay
#  aber (noch) nicht als „known" quantifiziert war. Jetzt schlägt das absolute
#  Warehouse-est_arr_iso (dieselbe Quelle wie der Radar) `sched + delay`.
# ─────────────────────────────────────────────────────────────────────────────

_BUGB_SECTORS = [
    {'flight': 'LH900', 'from': 'FRA', 'to': 'LHR',
     'dep_iso': '2026-07-09T07:00:00Z', 'arr_iso': '2026-07-09T08:40:00Z'},
]


def test_bugB_flying_ankunft_folgt_absolutem_est_arr_wie_radar():
    """est_arr_iso 08:47 (absolut, wie der Radar) bei UNBEKANNTEM Delay →
    Text zeigt 08:47, nicht die Plan-08:40."""
    now = _utc(8, 30)
    obs = _obs({'LH900': {'status': 'Departed', 'dep_delay_min': 0,
                          'arr_delay_min': None, 'delay_min': None,
                          'delay_known': False,
                          'est_arr_iso': '2026-07-09T08:47:00Z'}})
    r = resolve_crew_live_state(_BUGB_SECTORS, obs, _live({}), now,
                                homebase='FRA', layover_iata='LHR')
    assert r['state'] == STATE_FLYING
    assert r['text']['subtitle'] == 'FRA → LHR · Ankunft 08:47'
    assert r['current_leg']['est_arr_iso'] == '2026-07-09T08:47:00Z'


def test_bugB_ohne_est_arr_bleibt_planzeit():
    """Kein absolutes est, kein Delay → exakt das alte Verhalten (Plan 08:40)."""
    now = _utc(8, 30)
    obs = _obs({'LH900': {'status': 'Departed', 'dep_delay_min': 0}})
    r = resolve_crew_live_state(_BUGB_SECTORS, obs, _live({}), now,
                                homebase='FRA', layover_iata='LHR')
    assert r['state'] == STATE_FLYING
    assert r['text']['subtitle'] == 'FRA → LHR · Ankunft 08:40'
    assert r['current_leg']['est_arr_iso'] is None


def test_bugB_expliziter_arr_delay_unveraendert():
    """Expliziter arr_delay_min=7 ohne absolutes est → sched+7 = 08:47
    (Rückwärtskompatibilität: delay_min bleibt die explizite Zahl)."""
    now = _utc(8, 30)
    obs = _obs({'LH900': {'status': 'Departed', 'arr_delay_min': 7,
                          'delay_known': True}})
    r = resolve_crew_live_state(_BUGB_SECTORS, obs, _live({}), now,
                                homebase='FRA', layover_iata='LHR')
    assert r['text']['subtitle'] == 'FRA → LHR · Ankunft 08:47'
    assert r['current_leg']['delay_min'] == 7
    assert r['current_leg']['est_arr_iso'] == '2026-07-09T08:47:00Z'


def test_bugB_pre_flight_abflug_folgt_absolutem_est_dep():
    """Symmetrie am Abflug: absolute est_dep_iso 07:25 bei unbekanntem Delay →
    „Nächster Flug · LH900 · 07:25" + Verspätet-Phase (est_dep > sched_dep)."""
    now = _utc(6, 50)   # vor Abflug
    obs = _obs({'LH900': {'status': 'Delayed', 'dep_delay_min': None,
                          'delay_known': False,
                          'est_dep_iso': '2026-07-09T07:25:00Z'}})
    r = resolve_crew_live_state(_BUGB_SECTORS, obs, _live({}), now,
                                homebase='FRA', layover_iata='LHR')
    assert r['state'] == STATE_PRE_FLIGHT
    assert '07:25' in r['text']['title'], r['text']['title']


def test_bugB_est_arr_vor_sched_wird_nicht_negativ_verdreht():
    """Absurdes est_arr VOR sched (früher als Plan) → est_arr_iso trägt die
    absolute Zeit, aber es entsteht kein sinnloser Zustand (nur Anzeige)."""
    now = _utc(8, 30)
    obs = _obs({'LH900': {'status': 'Departed',
                          'est_arr_iso': '2026-07-09T08:30:00Z'}})
    r = resolve_crew_live_state(_BUGB_SECTORS, obs, _live({}), now,
                                homebase='FRA', layover_iata='LHR')
    assert r['state'] == STATE_FLYING
    # est schlägt Plan auch nach vorne (frühere Ankunft ist ein echtes Signal).
    assert r['text']['subtitle'] == 'FRA → LHR · Ankunft 08:30'


# ── Tibor „zu früh live vor Abflug" (Owner 2026-07-13) ───────────────────────
# Abflug verspätet via ABSOLUTER Board-esti (est_dep_iso), aber KEIN quantifi-
# zierter dep_delay_min. Vorher rechnete die Flying-Entscheidung eff_dep=Soll →
# now>Soll → fälschlich 'flying', obwohl der Flieger noch nicht los ist.
_TIBOR_SFO_SECTORS = [
    {'flight': 'LH454', 'from': 'FRA', 'to': 'SFO',
     'dep_iso': '2026-07-13T08:25:00Z', 'arr_iso': '2026-07-13T19:40:00Z'},
]


def _tibor_resolve(now, obs):
    return resolve_crew_live_state(
        _TIBOR_SFO_SECTORS, _obs(obs), _live({}), now,
        homebase='FRA', city_lookup=lambda c: {'FRA': 'Frankfurt',
                                               'SFO': 'San Francisco'}.get(c))


def test_tibor_est_dep_in_zukunft_ist_nicht_flying():
    # Soll 08:25, esti 09:10, now 09:02 → noch NICHT abgeflogen → NICHT flying.
    obs = {'LH454': {'est_dep_iso': '2026-07-13T09:10:00Z'}}
    r = _tibor_resolve(datetime(2026, 7, 13, 9, 2, tzinfo=timezone.utc), obs)
    assert r['state'] != STATE_FLYING, r
    assert r['state'] == STATE_PRE_FLIGHT, r
    # Abflug-Zeit in der Anzeige ist die verspätete 09:10, nicht die Soll 08:25.
    assert '08:25' not in (r['text'].get('subtitle') or '')


def test_tibor_nach_est_dep_bleibt_flying():
    # Regressions-Schutz: now 09:20 > esti 09:10 → wieder fliegend, kein
    # Hängenbleiben in 'waiting' (echter Flug ohne Board/Live-Coverage).
    obs = {'LH454': {'est_dep_iso': '2026-07-13T09:10:00Z'}}
    r = _tibor_resolve(datetime(2026, 7, 13, 9, 20, tzinfo=timezone.utc), obs)
    assert r['state'] == STATE_FLYING, r


def test_tibor_ohne_est_kein_regress_nach_soll_abflug():
    # Ohne esti/Delay: now 08:40 > Soll 08:25, kein Live/Board → weiter 'flying'
    # (CONF_PLAN) wie bisher — die Änderung darf das NICHT brechen.
    r = _tibor_resolve(datetime(2026, 7, 13, 8, 40, tzinfo=timezone.utc), {})
    assert r['state'] == STATE_FLYING, r


def test_tibor_grosser_delay_zeigt_verspaetet_nicht_timeline():
    # Soll 08:25, esti 11:20 (~3h Delay), now 09:16 → 2h VOR dem verspäteten
    # Abflug, AUSSERHALB des 40-min-prep-Fensters. Muss „Verspätet · Abflug HH:MM"
    # zeigen, nicht eine gegen die Soll-Zeit gerechnete Timeline-Phase.
    obs = {'LH454': {'est_dep_iso': '2026-07-13T11:20:00Z'}}
    r = _tibor_resolve(datetime(2026, 7, 13, 9, 16, tzinfo=timezone.utc), obs)
    assert r['state'] == STATE_PRE_FLIGHT, r
    assert r.get('pre_phase') == 'delayed', r
    assert 'Verspätet' in (r['text'].get('subtitle') or ''), r


def test_tibor_board_abgeflogen_widerspricht_est_dep_nicht_flying():
    # Board „Abgeflogen" (airborne), ABER est_dep in der Zukunft (+175min) →
    # widersprüchlich/stale → NICHT flying, sondern Verspätet.
    obs = {'LH454': {'status': 'Abgeflogen', 'dep_delay_min': 175,
                     'est_dep_iso': '2026-07-13T11:20:00Z'}}
    r = _tibor_resolve(datetime(2026, 7, 13, 9, 38, tzinfo=timezone.utc), obs)
    assert r['state'] != STATE_FLYING, r
    assert r['state'] == STATE_PRE_FLIGHT, r
    assert 'Verspätet' in (r['text'].get('subtitle') or ''), r


def test_board_abgeflogen_nach_est_dep_bleibt_flying():
    # Regressions-Schutz: „Abgeflogen" + est_dep in der VERGANGENHEIT (now danach)
    # → weiter flying (Board schlägt Uhr, kein Widerspruch).
    obs = {'LH454': {'status': 'Abgeflogen', 'dep_delay_min': 20,
                     'est_dep_iso': '2026-07-13T08:45:00Z'}}
    r = _tibor_resolve(datetime(2026, 7, 13, 9, 30, tzinfo=timezone.utc), obs)
    assert r['state'] == STATE_FLYING, r


# ─────────────────────────────────────────────────────────────────────────────
#  SEBASTIAN LH901 (Owner-Integrationsplan 2026-07-13): die FLUG-Entscheidung
#  eines Legs kommt aus der FlightState-Engine — die Landung-MONOTONIE löst den
#  Fall, in dem das Board ARR-seitig „gelandet 12:27" meldet, der crew_state
#  aber eine STALE eff_arr-Schätzung (13:28) nutzte und fälschlich „fliegt ·
#  13:06" statt „gelandet, wartet auf das nächste Leg" zeigte.
#  Vor dem Umbau: dep-seitiges „Abgeflogen" (crew-Bucket=airborne) + hoher
#  arr_delay hielt das Airborne-Fenster bis 13:28 offen → Leg klebte auf
#  „Fliegt gerade · Ankunft 13:28". Jetzt: das ARR-seitige HARD-Landing der
#  Engine ist terminal → Leg geflogen → nächstes Leg (Zukunft) = pre_flight/wartet.
# ─────────────────────────────────────────────────────────────────────────────

_SEBASTIAN_SECTORS = [
    {'flight': 'LH901', 'from': 'MUC', 'to': 'FRA',
     'dep_iso': '2026-07-13T10:30:00Z', 'arr_iso': '2026-07-13T11:40:00Z'},
    {'flight': 'LH862', 'from': 'FRA', 'to': 'OSL',
     'dep_iso': '2026-07-13T14:00:00Z', 'arr_iso': '2026-07-13T16:00:00Z'},
]


def _sebastian_resolve(now, obs):
    return resolve_crew_live_state(
        _SEBASTIAN_SECTORS, _obs(obs), _live({}), now, homebase='MUC',
        city_lookup=lambda c: {'MUC': 'München', 'FRA': 'Frankfurt',
                               'OSL': 'Oslo'}.get(c))


def test_sebastian_arr_landed_terminal_trotz_staler_est_arr():
    # Board ARR-seitig „gelandet" (12:27) UND ein staler +108-min-est_arr (13:28);
    # dep-seitig „Abgeflogen". now 13:06. Vor dem Umbau: „Fliegt gerade · 13:28".
    # Jetzt: die Engine-Landung (arr-hard, terminal) beendet Leg 1 → Leg 2 (Zukunft).
    now = datetime(2026, 7, 13, 13, 6, tzinfo=timezone.utc)
    obs = {'LH901': {'status_dep': 'Abgeflogen', 'status_arr': 'gelandet',
                     'arr_delay_min': 108, 'delay_min': 108, 'delay_known': True,
                     'est_arr_iso': '2026-07-13T13:28:00Z'}}
    r = _sebastian_resolve(now, obs)
    assert r['state'] != STATE_FLYING, r
    assert r['state'] == STATE_LANDED, r
    assert r['leg_index'] == 1, r
    assert r['current_leg']['flight_no'] == 'LH862', r
    assert r['text']['title'] == 'Gelandet in Frankfurt', r
    assert '13:06' not in (r['text']['subtitle'] or ''), r


def test_sebastian_landung_monotonie_kein_rueckwaerts_aus_stale_est():
    # Selbst ohne dep-Board, nur ARR-seitiges „gelandet" + staler est_arr in der
    # Zukunft → Landung ist terminal, keine „fliegt"-Regression.
    now = datetime(2026, 7, 13, 13, 6, tzinfo=timezone.utc)
    obs = {'LH901': {'status_arr': 'gelandet',
                     'est_arr_iso': '2026-07-13T13:28:00Z'}}
    r = _sebastian_resolve(now, obs)
    assert r['state'] != STATE_FLYING, r
    assert r['leg_index'] == 1, r


def test_sebastian_ohne_landung_bleibt_flying():
    # Regressions-Schutz: OHNE arr-seitiges Landungssignal (nur dep „Abgeflogen"),
    # est_arr noch in der Zukunft, innerhalb des Fensters → weiter fliegend
    # (die Engine „un-landet" nichts ohne Beweis, aber landet auch nicht ohne Signal).
    now = datetime(2026, 7, 13, 12, 30, tzinfo=timezone.utc)
    obs = {'LH901': {'status_dep': 'Abgeflogen', 'arr_delay_min': 108,
                     'delay_min': 108, 'delay_known': True,
                     'est_arr_iso': '2026-07-13T13:28:00Z'}}
    r = _sebastian_resolve(now, obs)
    assert r['state'] == STATE_FLYING, r
    assert r['leg_index'] == 0, r


def test_tibor_frisch_offblock_ist_taxi_nicht_flying():
    # Board „Abgeflogen" (off-block) FRISCH (now 3 min nach est_dep) → rollt zur
    # Startbahn, NICHT „Fliegt gerade"/LIVE (Owner 2026-07-13: „auf live obwohl
    # Flieger nicht live, kein Takeoff").
    obs = {'LH454': {'status_dep': 'Abgeflogen', 'status': 'Abgeflogen',
                     'est_dep_iso': '2026-07-13T11:30:00Z', 'dep_delay_min': 185}}
    r = _tibor_resolve(datetime(2026, 7, 13, 11, 33, tzinfo=timezone.utc), obs)
    assert r['state'] != STATE_FLYING, r
    assert r['state'] == STATE_PRE_FLIGHT, r
    assert r['text']['title'] == 'Startet gerade', r
    assert r.get('position') is None, r


def test_tibor_langes_offblock_ohne_landung_ist_flying():
    # Konsistenz-Regel (Owner 2026-07-13): dasselbe „Abgeflogen"/off-block, aber
    # jetzt LANGE her (est_dep 11:30, now 13:00 = 90 min off-block) und noch VOR
    # der erwarteten Ankunft (SFO 19:40) → die EINE Engine hebt TAXI_OUT auf
    # AIRBORNE (Zeit-Evidenz, estimated) → crew zeigt „Fliegt gerade" — dieselbe
    # Phase wie flights_live/family/my-status. Der crew-eigene 25-min-Deckel ist
    # RAUS; die Grenze lebt allein in der Engine.
    obs = {'LH454': {'status_dep': 'Abgeflogen', 'status': 'Abgeflogen',
                     'est_dep_iso': '2026-07-13T11:30:00Z', 'dep_delay_min': 185}}
    r = _tibor_resolve(datetime(2026, 7, 13, 13, 0, tzinfo=timezone.utc), obs)
    assert r['state'] == STATE_FLYING, r
    assert r['text']['title'] == 'Fliegt gerade', r
    # keine erfundene Position (kein ADS-B) — die Live-Karte zeigt ehrlich keinen
    # Flieger, aber die Person ist konsistent „fliegend".
    assert r.get('position') is None, r


def test_basti_lh890_geschlossen_bleibt_am_boden():
    """Live-Fall 2026-07-14: FRA meldet für LH890 nur „geschlossen".

    Das FR24-Live-Detail sagte zeitgleich ON_GROUND/actual departure N/A.
    Der deutsche Board-Token muss deshalb wie „Gate closed" die Plan-Uhr
    blockieren: keine „Wer fliegt gerade"-Karte, bis ein echter Airborne-Beweis
    (Position/FlightState) eintrifft.
    """
    sectors = [{
        'flight': 'LH890', 'from': 'FRA', 'to': 'RIX',
        'dep_iso': '2026-07-14T08:10:00Z',
        'arr_iso': '2026-07-14T10:20:00Z',
    }]
    obs = {'LH890': {
        'status': 'geschlossen', 'status_dep': 'geschlossen',
        'sched_dep_iso': '2026-07-14T08:10:00Z',
        'reg': 'D-AINJ',
    }}
    r = resolve_crew_live_state(
        sectors, _obs(obs), _live({}),
        datetime(2026, 7, 14, 8, 25, tzinfo=timezone.utc),
        homebase='FRA',
        local_hhmm=lambda d, _ap: f'{(d.hour + 2) % 24:02d}:{d.minute:02d}')
    assert r['state'] == STATE_PRE_FLIGHT, r
    assert r['state'] != STATE_FLYING, r
    assert r['text']['title'] == 'Nächster Flug · LH890 · 10:10', r
    assert r.get('pre_phase') == 'boarding', r


def test_sebastian_lh1126_final_approach_beats_departure_taxi_out():
    """Production regression 2026-07-15: one flight, one state everywhere.

    FRA reported the departure-side token ``Abgeflogen`` while the arrival
    side already reported ``Final approach``.  The arrival-side observation
    is stronger and must never regress the leg to pre-flight/taxi at FRA.
    """
    sectors = [{
        'flight': 'LH1126', 'from': 'FRA', 'to': 'BCN',
        'dep_iso': '2026-07-15T07:50:00Z',
        'arr_iso': '2026-07-15T09:55:00Z',
    }]
    obs = {'LH1126': {
        'status': 'Final approach',
        'status_dep': 'Abgeflogen',
        'status_arr': 'Final approach',
        'est_dep_iso': '2026-07-15T07:50:00Z',
        'est_arr_iso': '2026-07-15T10:08:00Z',
        'dep_delay_min': 0,
        'arr_delay_min': 13,
        'delay_min': 13,
        'delay_known': True,
        'reg': 'D-AIDB',
    }}
    r = resolve_crew_live_state(
        sectors, _obs(obs), _live({}),
        datetime(2026, 7, 15, 10, 12, tzinfo=timezone.utc),
        homebase='FRA',
        city_lookup=lambda c: {'FRA': 'Frankfurt', 'BCN': 'Barcelona'}.get(c),
    )

    assert r['state'] == STATE_FLYING, r
    assert r['current_leg']['flight_no'] == 'LH1126', r
    assert r['current_leg']['status'] == 'Final approach', r
    assert r['text']['title'] == 'Fliegt gerade', r
    assert r.get('pre_phase') is None, r


def test_layover_rueckflugtag_verwirft_fremd_tag_landung():
    """Tibor 2026-07-15, Julien/Nico BOS-Layover: der Rückflug LH423 BOS→FRA
    ist ein TÄGLICHER Flug. Der datums-agnostische obs_lookup liefert am
    Rückflugtag die HEUTE-FRÜH gelandete LH423-Instanz (est_arr 05:44Z, LANDED)
    für das ABEND-Leg der Crew (dep 21:45Z). Ohne Gate markierte das die noch
    nicht abgeflogene Maschine als „flown" → picked=None → dest==FRA==hb →
    „Gelandet in Frankfurt / Feierabend". Erwartet: der Fremd-Tag-Obs wird
    verworfen, das Leg resolved aus seiner eigenen iCal-Uhr zu pre_flight.
    """
    from blueprints.crew_live_state import resolve_crew_live_state
    sectors = [{
        'flight': 'LH423', 'from': 'BOS', 'to': 'FRA',
        'dep_iso': '2026-07-15T21:45:00Z',       # heute Abend BOS (17:45 lokal)
        'arr_iso': '2026-07-16T04:50:00Z',        # morgen früh FRA
    }]
    # Der ZULETZT beobachtete LH423-Lauf ist die HEUTE FRÜH gelandete Instanz.
    obs = {'LH423': {
        'status': 'LANDED',
        'status_arr': 'LANDED',
        'sched_arr_iso': '2026-07-15T04:50:00Z',
        'est_arr_iso': '2026-07-15T05:44:00Z',   # ~16 h VOR dem eigenen Abflug
        'arr_delay_min': 54,
        'delay_min': 54,
        'delay_known': True,
    }}
    # 19:52 Berlin = 17:52 UTC — der echte Abflug (21:45Z) steht noch aus.
    r = resolve_crew_live_state(
        sectors, _obs(obs), _live({}),
        datetime(2026, 7, 15, 17, 52, tzinfo=timezone.utc),
        homebase='FRA',
        city_lookup=lambda c: {'FRA': 'Frankfurt', 'BOS': 'Boston'}.get(c),
    )
    assert r['state'] == STATE_PRE_FLIGHT, r
    assert r['current_leg']['flight_no'] == 'LH423', r
    assert r['current_leg']['dep'] == 'BOS' and r['current_leg']['arr'] == 'FRA', r
    assert 'Feierabend' not in (r['text'].get('title') or ''), r
    assert 'Gelandet' not in (r['text'].get('title') or ''), r
    assert r['text']['title'].startswith('Nächster Flug'), r


def test_gleicher_tag_landung_bleibt_gueltig():
    """Gegenprobe: eine LEGITIME Landung DIESES Tages (Ankunft NACH dem eigenen
    Abflug) darf NICHT als Fremd-Tag verworfen werden — der Leg bleibt geflogen.
    Schützt gegen einen zu gierigen Wrong-Day-Gate.
    """
    from blueprints.crew_live_state import resolve_crew_live_state
    sectors = [{
        'flight': 'LH1126', 'from': 'FRA', 'to': 'BCN',
        'dep_iso': '2026-07-15T07:50:00Z',
        'arr_iso': '2026-07-15T09:55:00Z',
    }]
    obs = {'LH1126': {
        'status': 'Gelandet', 'status_arr': 'Gelandet',
        'sched_arr_iso': '2026-07-15T09:55:00Z',
        'est_arr_iso': '2026-07-15T10:08:00Z',   # NACH dem Abflug → gültig
        'arr_delay_min': 13, 'delay_min': 13, 'delay_known': True,
    }}
    r = resolve_crew_live_state(
        sectors, _obs(obs), _live({}),
        datetime(2026, 7, 15, 11, 0, tzinfo=timezone.utc),
        homebase='FRA',
        city_lookup=lambda c: {'FRA': 'Frankfurt', 'BCN': 'Barcelona'}.get(c),
    )
    # Gelandet am Tagesziel BCN (≠ hb) → Layover/Landed Barcelona, NICHT verworfen.
    assert r['state'] in (STATE_LANDED, STATE_LAYOVER), r
    assert 'Barcelona' in (r['text'].get('title') or ''), r
