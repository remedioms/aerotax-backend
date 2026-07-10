"""Family-Watch: EIN kohärenter AKTUELLER Leg (Root-Fix 2026-07-10).

Owner-Bug (Tibor-iPad, TestFlight 10:59): Family-Karte zeigte „Fliegt gerade —
BCN → FRA · Ankunft 15:20" während Tibor auf einem 3-Leg-Tag (BCN-FRA-ARN-FRA)
GERADE FRA→ARN flog (LH802, 10:25–12:30). Die Karte war ein FELD-MIX aus zwei
Legs: Route = Ketten-Enden (chain[0]/chain[-1] ≙ erster Abflug/letzte Station),
Ankunft = DIENST-Ende (= Ankunft des LETZTEN Legs 15:20). Der zeitlich aktuelle
Leg fehlte komplett.

Fix: Pro-Leg-Sektoren (ical_sectors, echt-UTC) → _pick_current_sector wählt
zeitbasiert GENAU EINEN Leg (dep ≤ now < arr_eff; est/Delay bevorzugt, +40 min
Puffer ohne Beobachtung, beobachtete Landung beendet sofort). Route, Zeiten,
Delay, Phase UND Live-Fix kommen alle aus DIESEM Leg. Davor/zwischen den Legs:
„wartet" auf den nächsten Leg (flying_now=False), nach dem letzten: gelandet.

Tibors Tag (2026-07-10, Lokalzeiten CEST = UTC+2; ARN ebenfalls CEST):
  LH1139 BCN→FRA 06:40–08:55  (04:40–06:55Z)
  LH802  FRA→ARN 10:25–12:30  (08:25–10:30Z)
  LH803  ARN→FRA 13:10–15:20  (11:10–13:20Z)
Drei Zeitpunkte: 09:30 → gelandet BCN→FRA / wartet auf LH802;
10:59 → fliegt FRA→ARN, Ankunft 12:30; 14:00 → fliegt ARN→FRA, Ankunft 15:20.
"""
import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import datetime as dt
import sys
import time
import types
from unittest.mock import MagicMock

import pytest

import app as A
import blueprints.adsb_blueprint as ADSB
import blueprints.aerox_data_blueprint as BPD
import blueprints.family_watch as FW

TODAY = dt.datetime.now().date().isoformat()
CHAIN = ['BCN', 'FRA', 'ARN', 'FRA']


def _utc(h, m, day=10):
    return dt.datetime(2026, 7, day, h, m, tzinfo=dt.timezone.utc)


# Tibors Sektoren, echt-UTC (Lokal CEST −2h).
_TIBOR_SECTORS = [
    {'flight': 'LH1139', 'from': 'BCN', 'to': 'FRA',
     'dep_iso': '2026-07-10T04:40:00Z', 'arr_iso': '2026-07-10T06:55:00Z'},
    {'flight': 'LH802', 'from': 'FRA', 'to': 'ARN',
     'dep_iso': '2026-07-10T08:25:00Z', 'arr_iso': '2026-07-10T10:30:00Z'},
    {'flight': 'LH803', 'from': 'ARN', 'to': 'FRA',
     'dep_iso': '2026-07-10T11:10:00Z', 'arr_iso': '2026-07-10T13:20:00Z'},
]


def _times():
    t = FW._day_sectors_aligned(CHAIN, _TIBOR_SECTORS)
    assert t is not None
    return t


# ── _day_sectors_aligned: nur exakt passende Sektoren ------------------------

def test_aligned_rejects_mismatched_chain():
    assert FW._day_sectors_aligned(['BCN', 'FRA', 'OSL', 'FRA'],
                                   _TIBOR_SECTORS) is None


def test_aligned_rejects_wrong_count_or_bad_times():
    assert FW._day_sectors_aligned(CHAIN, _TIBOR_SECTORS[:2]) is None
    broken = [dict(s) for s in _TIBOR_SECTORS]
    broken[1]['arr_iso'] = ''
    assert FW._day_sectors_aligned(CHAIN, broken) is None


# ── _pick_current_sector: Tibors drei Zeitpunkte ------------------------------

def test_0930_local_landed_leg1_waits_for_lh802():
    # 09:30 Lokal = 07:30Z. BCN→FRA ist GELANDET (Board-Obs) → kein Puffer-
    # Weiterfliegen; die Karte wartet auf LH802 (FRA→ARN, dep 10:25 Lokal).
    legs_live = [{'flight': 'LH1139', 'leg_index': 0,
                  'status': 'Landed 08:52', 'delay_min': -3}]
    state, idx, dep, arr, arr_est = FW._pick_current_sector(
        _times(), legs_live, _utc(7, 30))
    assert state == 'pre' and idx == 1
    assert dep == _utc(8, 25) and arr == _utc(10, 30)


def test_0930_local_without_landing_obs_stays_in_leg1_buffer():
    # Ohne Beobachtung hält der 40-min-Puffer den Leg (Delay-Toleranz):
    # 07:30Z < 06:55Z+40min → ehrlich noch „fliegt BCN→FRA".
    state, idx, _dep, _arr, _est = FW._pick_current_sector(
        _times(), None, _utc(7, 30))
    assert state == 'inflight' and idx == 0
    # Nach Ablauf des Puffers (07:40Z) → wartet auf Leg 2.
    state2, idx2, _d, _a, _e = FW._pick_current_sector(
        _times(), None, _utc(7, 40))
    assert state2 == 'pre' and idx2 == 1


def test_1059_local_flying_fra_arn_arrival_1230():
    # DER Live-Bug-Zeitpunkt: 10:59 Lokal = 08:59Z → LH802 FRA→ARN läuft,
    # Ankunft 10:30Z (12:30 Lokal) — NICHT „BCN→FRA · Ankunft 15:20".
    state, idx, dep, arr, _est = FW._pick_current_sector(
        _times(), None, _utc(8, 59))
    assert state == 'inflight' and idx == 1
    assert CHAIN[idx] == 'FRA' and CHAIN[idx + 1] == 'ARN'
    assert arr == _utc(10, 30)


def test_1400_local_flying_arn_fra_arrival_1520():
    # 14:00 Lokal = 12:00Z → LH803 ARN→FRA, Ankunft 13:20Z (15:20 Lokal).
    state, idx, _dep, arr, _est = FW._pick_current_sector(
        _times(), None, _utc(12, 0))
    assert state == 'inflight' and idx == 2
    assert CHAIN[idx] == 'ARN' and CHAIN[idx + 1] == 'FRA'
    assert arr == _utc(13, 20)


def test_after_last_leg_plus_buffer_done():
    state, idx, _dep, arr, _est = FW._pick_current_sector(
        _times(), None, _utc(14, 30))
    assert state == 'done' and idx == 2 and arr == _utc(13, 20)


def test_observed_delay_extends_leg_window_est_preferred():
    # LH802 +45 beobachtet → um 10:45Z (Plan-arr 10:30Z, Puffer wäre auch da,
    # aber est zählt als ECHTES Fenster) noch inflight MIT est-Ankunft.
    legs_live = [{'flight': 'LH802', 'leg_index': 1,
                  'status': 'estimated', 'arr_delay_min': 45}]
    state, idx, _dep, arr, arr_est = FW._pick_current_sector(
        _times(), legs_live, _utc(10, 45))
    assert state == 'inflight' and idx == 1
    assert arr_est == _utc(11, 15)


# ── Loader-Wiring: alle Status-Felder aus DEMSELBEN Leg -----------------------

class _Q:
    def __init__(self, rows):
        self._rows = rows

    def __getattr__(self, _name):
        return lambda *a, **k: self

    def execute(self):
        return types.SimpleNamespace(data=self._rows)


class _SB:
    def __init__(self, rows):
        self._rows = rows

    def table(self, _name):
        return _Q(self._rows)


def _rel_sectors(now, offsets):
    """Sektoren relativ zu now: offsets = [(dep_min, arr_min), …] (Minuten)."""
    def _z(mins):
        return (now + dt.timedelta(minutes=mins)).strftime(
            '%Y-%m-%dT%H:%M:%SZ')
    fls = ['LH1139', 'LH802', 'LH803']
    return [{'flight': fls[i], 'from': CHAIN[i], 'to': CHAIN[i + 1],
             'dep_iso': _z(d), 'arr_iso': _z(a)}
            for i, (d, a) in enumerate(offsets)]


def _obs_for(fno_obs):
    def _obs(fno, date=None, dep_iata=None, arr_iata=None, free_only=None):
        return fno_obs.get(str(fno).upper())
    return _obs


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    _prev_app_mod = sys.modules.get('app')
    sys.modules['app'] = A
    ADSB._CACHE.clear()
    FW._LIVE_FIX_MEMO.clear()
    with ADSB._BACKOFF['lock']:
        ADSB._BACKOFF['until'] = 0.0
    monkeypatch.setattr(ADSB, '_rate_limited', lambda **k: False)
    monkeypatch.setattr(ADSB, '_sb_client', lambda: (None, False))
    monkeypatch.setattr(ADSB, '_backfill_cache_from_sb', lambda h: None)
    monkeypatch.setattr(ADSB, '_touch_watch', lambda *a, **k: None)
    monkeypatch.setattr(ADSB, '_warm_persist_from_opensky_row',
                        lambda *a, **k: None)
    monkeypatch.setattr(ADSB, '_fetch_opensky', lambda h: None)
    monkeypatch.setattr(ADSB, '_fetch_adsb_lol', lambda h: None)
    monkeypatch.setattr(ADSB, '_fetch_fr24', lambda h, callsign=None: None)
    monkeypatch.setattr(BPD, '_paid_budget_ok', lambda: True)
    monkeypatch.setattr(BPD, '_budget_key_inc', MagicMock())
    monkeypatch.setattr(BPD, '_paid_budget_inc', MagicMock())
    monkeypatch.setattr(BPD, '_sb_day_reg',
                        lambda fn, d: (None, None, None, None))
    yield
    ADSB._CACHE.clear()
    FW._LIVE_FIX_MEMO.clear()
    if _prev_app_mod is not None:
        sys.modules['app'] = _prev_app_mod


def _tibor_env(monkeypatch, offsets, fno_obs):
    now = dt.datetime.now(dt.timezone.utc)
    secs = _rel_sectors(now, offsets)
    rows = [{
        'datum': TODAY,
        'ical_summary': ('LH1139 BCN-FRA 06:40, LH802 FRA-ARN 10:25, '
                         'LH803 ARN-FRA 13:10'),
        'ical_location': 'FRA, BCN-FRA-ARN-FRA',
        'ical_start': (now - dt.timedelta(hours=5)).strftime(
            '%Y-%m-%dT%H:%M:%S+00:00'),
        'ical_end': (now + dt.timedelta(hours=5)).strftime(
            '%Y-%m-%dT%H:%M:%S+00:00'),
        'raw_event': {'ical_sectors': secs},
    }]
    monkeypatch.setattr(FW, '_get_sb', lambda: (True, _SB(rows)))
    monkeypatch.setattr(FW, '_load_crew_profile',
                        lambda t: {'homebase': 'FRA'})
    monkeypatch.setattr(A, '_profile_load', lambda t: {}, raising=False)
    monkeypatch.setattr(
        A, '_roster_snapshot_read',
        lambda t: {'tage': [{'datum': TODAY, 'reader_facts':
                             {'flight_numbers': ['LH1139', 'LH802', 'LH803']}}]},
        raising=False)
    monkeypatch.setattr(A, '_flight_obs_merged', _obs_for(fno_obs),
                        raising=False)
    return now, secs


def _fr24_row(age_s=90):
    ts = time.time() - age_s
    return ['3c675c', 'DLH4RJ', None, ts, ts, 17.9, 55.6, 10000.0, False,
            230.0, 20.0, None, None, None, None, False, 0]


def test_loader_1059_shows_current_leg_fra_arn_not_daymix(monkeypatch):
    # now liegt IM zweiten Leg (FRA→ARN läuft) → ALLE Felder aus diesem Leg.
    now, secs = _tibor_env(
        monkeypatch,
        offsets=[(-240, -120), (-30, 90), (150, 270)],
        fno_obs={
            'LH1139': {'status': 'Landed', 'delay_min': -3, 'reg': 'D-AIXA'},
            'LH802': {'status': 'En Route', 'delay_min': 5, 'reg': 'D-AIWA'},
        })
    monkeypatch.setattr(ADSB, '_fetch_fr24',
                        lambda h, callsign=None: _fr24_row())
    seen_regs = []
    monkeypatch.setattr(ADSB, 'resolve_reg_to_hex',
                        lambda r: seen_regs.append(r) or '3c675c')
    import blueprints.warehouse_reader as WR
    monkeypatch.setattr(WR, 'route_for_flight', lambda **k: None)

    st = FW._load_crew_status_for_family('tok-tibor-inflight', {'next_flight'})
    assert st['flying_now'] is True
    # DER Fix: Route = aktueller Leg (FRA→ARN), NICHT Ketten-Enden (BCN→FRA).
    assert st['today_dep_iata'] == 'FRA'
    assert st['today_arr_iata'] == 'ARN'
    # Ankunft = Ankunft DIESES Legs (now+90min), NICHT das Dienst-Ende.
    assert st['today_arr_iso'] == secs[1]['arr_iso']
    assert st['today_dep_iso'] == secs[1]['dep_iso']
    # Delay + est-Ankunft ebenfalls aus DIESEM Leg (+5 beobachtet).
    assert st['today_delay_min'] == 5
    arr_dt = FW._parse_iso(secs[1]['arr_iso'])
    assert FW._parse_iso(st['today_arr_est_iso']) == (
        arr_dt + dt.timedelta(minutes=5))
    # Phase aus der Obs DIESES Legs (En Route → airborne, nicht letztes Leg).
    assert st['flight_phase'] == 'airborne'
    # Live-Fix pingt die Reg des GEWÄHLTEN Legs (D-AIWA), nicht die von Leg 1.
    assert st['live_lat'] is not None
    assert seen_regs == ['D-AIWA']
    # Tour-Label bleibt die volle Kette (Struktur unangetastet).
    assert st['today_route_label'] is not None
    BPD._budget_key_inc.assert_not_called()


def test_loader_0930_between_legs_waits_not_flying(monkeypatch):
    # Leg 1 GELANDET, Leg 2 startet erst in 45 min → flying_now=False, die
    # Karte zeigt den KOMMENDEN Leg (FRA→ARN) statt „Fliegt gerade BCN→FRA".
    now, secs = _tibor_env(
        monkeypatch,
        offsets=[(-240, -120), (45, 170), (210, 330)],
        fno_obs={
            'LH1139': {'status': 'Landed', 'delay_min': -3, 'reg': 'D-AIXA'},
            'LH802': {'status': 'Scheduled', 'reg': 'D-AIWA'},
        })
    fr24 = MagicMock(return_value=_fr24_row())
    monkeypatch.setattr(ADSB, '_fetch_fr24', fr24)

    st = FW._load_crew_status_for_family('tok-tibor-waiting', {'next_flight'})
    assert st['flying_now'] is False
    assert st['today_dep_iata'] == 'FRA'
    assert st['today_arr_iata'] == 'ARN'
    assert st['today_dep_iso'] == secs[1]['dep_iso']
    assert st['today_arr_iso'] == secs[1]['arr_iso']
    # Am Boden → kein Live-Fix (keine Geister-Position aus Leg 1).
    assert st['live_lat'] is None
    fr24.assert_not_called()


def test_loader_after_all_legs_landed_at_homebase(monkeypatch):
    # Alle Legs (+Puffer) geflogen, Dienst-Fenster läuft noch (Debriefing) →
    # gelandet-Zustand: nicht mehr „Fliegt gerade", Ziel FRA = Homebase.
    _now, secs = _tibor_env(
        monkeypatch,
        offsets=[(-540, -420), (-360, -240), (-200, -80)],
        fno_obs={'LH803': {'status': 'Landed', 'delay_min': 2,
                           'reg': 'D-AIWA'}})
    st = FW._load_crew_status_for_family('tok-tibor-done',
                                         {'next_flight', 'layover_place'})
    assert st['flying_now'] is False
    assert st['today_dep_iata'] == 'ARN'
    assert st['today_arr_iata'] == 'FRA'
    assert st['today_arr_iso'] == secs[2]['arr_iso']
    assert st['home_now'] is True


def test_loader_without_sectors_keeps_legacy_day_window(monkeypatch):
    # Kein ical_sectors (weder raw_event noch Snapshot) → EXAKT das alte
    # Verhalten: Ketten-Enden + Dienst-Fenster (kein Regressions-Risiko).
    now, _secs = _tibor_env(
        monkeypatch,
        offsets=[(-240, -120), (-30, 90), (150, 270)],
        fno_obs={})
    rows = [{
        'datum': TODAY,
        'ical_summary': ('LH1139 BCN-FRA 06:40, LH802 FRA-ARN 10:25, '
                         'LH803 ARN-FRA 13:10'),
        'ical_location': 'FRA, BCN-FRA-ARN-FRA',
        'ical_start': (now - dt.timedelta(hours=5)).strftime(
            '%Y-%m-%dT%H:%M:%S+00:00'),
        'ical_end': (now + dt.timedelta(hours=5)).strftime(
            '%Y-%m-%dT%H:%M:%S+00:00'),
    }]
    monkeypatch.setattr(FW, '_get_sb', lambda: (True, _SB(rows)))
    monkeypatch.setattr(A, '_roster_snapshot_read', lambda t: {},
                        raising=False)

    st = FW._load_crew_status_for_family('tok-tibor-legacy', {'next_flight'})
    assert st['flying_now'] is True
    assert st['today_dep_iata'] == 'BCN'
    assert st['today_arr_iata'] == 'FRA'
