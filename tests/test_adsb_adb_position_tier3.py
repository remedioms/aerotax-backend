"""Tier 3 der ADS-B-Positions-Kaskade: bezahlte AeroDataBox-Live-Position.

Owner-Fall 2026-07-05: LH716 FRA→HND über China — FR24 zeigte den Flieger,
unsere App nicht (adsb.lol/OpenSky-Coverage-Lücke). Tier 3 läuft NUR:
  · bei gezielten Abfragen (?own=1 bzw. ?purpose=own|inbound),
  · wenn die freien Quellen keine Position lieferten,
  · solange das eigene Tages-Budget ('adb_position', Default 200/Tag) und der
    globale Paid-Guard frei sind.

KEIN echter Netz-/DB-Zugriff: freie Quellen, Supabase-Client, Budget-Helper
und der AeroDataBox-HTTP-Call werden gemockt.
"""
import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import sys
import time
from unittest.mock import MagicMock

import pytest

import app as A
import blueprints.adsb_blueprint as ADSB
import blueprints.aerox_data_blueprint as BPD


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    # SYS.MODULES-PIN (Order-Kontamination, Muster aus test_my_flight_status):
    # test_calculation.py tauscht sys.modules['app'] per Reimport-Trick aus —
    # für die Dauer jedes Tests dieses Files das eigene A-Modul pinnen.
    _prev_app_mod = sys.modules.get('app')
    sys.modules['app'] = A

    ADSB._CACHE.clear()
    with ADSB._BACKOFF['lock']:
        ADSB._BACKOFF['until'] = 0.0

    # Kein echtes Rate-Limit / Supabase / Watch-Set / Backfill.
    monkeypatch.setattr(ADSB, '_rate_limited', lambda **k: False)
    monkeypatch.setattr(ADSB, '_sb_client', lambda: (None, False))
    monkeypatch.setattr(ADSB, '_backfill_cache_from_sb', lambda h: None)
    monkeypatch.setattr(ADSB, '_touch_watch', lambda *a, **k: None)

    # Freie Quellen: sauberes „kein Signal" (Coverage-Lücke) als Default.
    # FR24-Grauzonen-Tier ebenfalls inert (sonst echter Netz-Call im Test) —
    # die Tier3-Tests prüfen genau den Fall, wo AUCH FR24 die Lücke nicht füllt.
    monkeypatch.setattr(ADSB, '_fetch_opensky', lambda h: None)
    monkeypatch.setattr(ADSB, '_fetch_adsb_lol', lambda h: None)
    monkeypatch.setattr(ADSB, '_fetch_fr24', lambda h, callsign=None: None)

    # ADB-Key vorhanden (langer Key = RapidAPI-Kanal); HTTP wird eh gemockt.
    monkeypatch.setenv('AERODATABOX_KEY', 'x' * 40)

    # Budget-Helper deterministisch: frei + Inkremente beobachtbar.
    monkeypatch.setattr(BPD, '_paid_budget_ok', lambda: True)
    monkeypatch.setattr(BPD, '_budget_key_used', lambda k: 0)
    monkeypatch.setattr(BPD, '_budget_key_inc', MagicMock())
    monkeypatch.setattr(BPD, '_paid_budget_inc', MagicMock())

    yield

    ADSB._CACHE.clear()
    if _prev_app_mod is not None:
        sys.modules['app'] = _prev_app_mod


@pytest.fixture
def client():
    A.app.testing = True
    return A.app.test_client()


def _adb_payload(reg='D-AIPA', age_s=60, with_location=True, with_ts=True):
    """AeroDataBox /flights/reg-Antwort (verifiziertes Payload-Format: location
    mit lat/lon + reportedAtUtc + Einheiten-Dicts)."""
    f = {
        'number': 'LH 716',
        'callSign': 'DLH716',
        'status': 'EnRoute',
        'aircraft': {'reg': reg},
        'departure': {'airport': {'iata': 'FRA'}},
        'arrival': {'airport': {'iata': 'HND'}},
    }
    if with_location:
        loc = {
            'lat': 47.31, 'lon': 91.55,
            'pressureAltitude': {'meter': 11277.6, 'feet': 37000.0},
            'altitude': {'meter': 11582.4, 'feet': 38000.0},
            'groundSpeed': {'kt': 487.0},
            'trueTrack': {'deg': 64.2},
        }
        if with_ts:
            loc['reportedAtUtc'] = time.strftime(
                '%Y-%m-%d %H:%MZ', time.gmtime(time.time() - age_s))
        f['location'] = loc
    return [f]


# ══════════════════════════════════════════════════════════════════════════════
# (a) own=1 + freie Quellen leer → ADB-Position kommt, source='adb', Budget zählt
# ══════════════════════════════════════════════════════════════════════════════
def test_own_flag_free_empty_adb_position_delivered(client, monkeypatch):
    http = MagicMock(return_value=_adb_payload(age_s=60))
    monkeypatch.setattr(ADSB, '_adb_position_http', http)

    r = client.get('/api/adsb/state?hex=3c64a8&reg=D-AIPA&own=1')
    assert r.status_code == 200
    b = r.get_json()
    assert b['source'] == 'adb'
    assert b['cached'] is False

    pos = b['position']
    assert pos[6] == 47.31 and pos[5] == 91.55        # lat/lon (OpenSky-Layout)
    assert pos[2] == 'D-AIPA'                          # reg wie bei adsb.lol
    assert pos[1] == 'DLH716'                          # callsign
    assert abs(pos[7] - 11582.4) < 0.1                 # altitude.meter bevorzugt
    assert abs(pos[9] - 487.0 * 0.514444) < 0.01       # kt → m/s
    assert pos[10] == 64.2                             # trueTrack.deg
    assert pos[8] is False                             # klar airborne (Höhe)

    # ECHTER Beobachtungs-Zeitstempel (reportedAtUtc), nicht "jetzt" gefälscht.
    assert abs(b['fetched_at'] - (time.time() - 60)) < 130
    assert abs(pos[3] - b['fetched_at']) < 1e-6

    # HTTP genau EIN Call, reg-gekeyt am heutigen UTC-Datum.
    assert http.call_count == 1
    reg_arg, date_arg = http.call_args.args
    assert reg_arg == 'D-AIPA'
    assert date_arg == time.strftime('%Y-%m-%d', time.gmtime())

    # Budget inkrementiert: eigener Tages-Key (1) + globales Paid-Konto (Tier2=2).
    key = 'adb_position:' + time.strftime('%Y%m%d', time.gmtime())
    BPD._budget_key_inc.assert_called_once_with(key, 1)
    BPD._paid_budget_inc.assert_called_once_with(units=2)

    # In den NORMALEN Cache geschrieben — mit dem Obs-Zeitstempel.
    entry = ADSB._CACHE.get('3c64a8')
    assert entry is not None and entry['source'] == 'adb'
    assert abs(entry['fetched_at'] - b['fetched_at']) < 1e-6

    # tried dokumentiert die volle Kaskade in Reihenfolge.
    assert [t['upstream'] for t in b['tried']] == ['opensky', 'adsb.lol',
                                                   'fr24', 'aerodatabox']


def test_purpose_inbound_also_enables_tier3(client, monkeypatch):
    http = MagicMock(return_value=_adb_payload(age_s=30))
    monkeypatch.setattr(ADSB, '_adb_position_http', http)
    r = client.get('/api/adsb/state?hex=3c64a8&reg=D-AIPA&purpose=inbound')
    assert r.get_json()['source'] == 'adb'
    assert http.call_count == 1


# ══════════════════════════════════════════════════════════════════════════════
# (b) own fehlt → KEIN ADB-Call (Radar-Sweeps zahlen nie)
# ══════════════════════════════════════════════════════════════════════════════
def test_without_own_no_adb_call(client, monkeypatch):
    http = MagicMock(return_value=_adb_payload())
    monkeypatch.setattr(ADSB, '_adb_position_http', http)

    r = client.get('/api/adsb/state?hex=3c64a8&reg=D-AIPA')
    assert r.status_code == 200
    b = r.get_json()
    http.assert_not_called()
    BPD._budget_key_inc.assert_not_called()
    BPD._paid_budget_inc.assert_not_called()
    # Altes Verhalten unverändert: freie Quellen sauber leer → position=null.
    assert b['position'] is None
    assert b['source'] != 'adb'
    assert 'aerodatabox' not in [t['upstream'] for t in b['tried']]


# ══════════════════════════════════════════════════════════════════════════════
# (c) Budget erschöpft → kein ADB-Call, alter Fallback
# ══════════════════════════════════════════════════════════════════════════════
def test_budget_exhausted_no_call_old_fallback(client, monkeypatch):
    monkeypatch.setattr(BPD, '_budget_key_used',
                        lambda k: ADSB._adb_position_daily_cap())
    http = MagicMock(return_value=_adb_payload())
    monkeypatch.setattr(ADSB, '_adb_position_http', http)

    r = client.get('/api/adsb/state?hex=3c64a8&reg=D-AIPA&own=1')
    assert r.status_code == 200
    b = r.get_json()
    http.assert_not_called()
    BPD._budget_key_inc.assert_not_called()
    assert b['position'] is None
    assert b['source'] != 'adb'
    adb_tried = [t for t in b['tried'] if t['upstream'] == 'aerodatabox']
    assert adb_tried and adb_tried[0]['ok'] is False
    assert adb_tried[0]['reason'] == 'budget_exhausted'


def test_global_paid_guard_blocks_too(client, monkeypatch):
    monkeypatch.setattr(BPD, '_paid_budget_ok', lambda: False)
    http = MagicMock(return_value=_adb_payload())
    monkeypatch.setattr(ADSB, '_adb_position_http', http)
    r = client.get('/api/adsb/state?hex=3c64a8&reg=D-AIPA&own=1')
    http.assert_not_called()
    assert r.get_json()['source'] != 'adb'


# ══════════════════════════════════════════════════════════════════════════════
# (d) ADB ohne location → alter Fallback (nichts erfinden)
# ══════════════════════════════════════════════════════════════════════════════
def test_adb_without_location_falls_through(client, monkeypatch):
    http = MagicMock(return_value=_adb_payload(with_location=False))
    monkeypatch.setattr(ADSB, '_adb_position_http', http)

    r = client.get('/api/adsb/state?hex=3c64a8&reg=D-AIPA&own=1')
    b = r.get_json()
    assert r.status_code == 200
    assert b['position'] is None
    assert b['source'] != 'adb'
    adb_tried = [t for t in b['tried'] if t['upstream'] == 'aerodatabox']
    assert adb_tried and adb_tried[0]['reason'] == 'no_location'


def test_adb_location_without_timestamp_rejected(client, monkeypatch):
    # Ohne reportedAtUtc keine Freshness-Garantie → Position NICHT verwenden.
    http = MagicMock(return_value=_adb_payload(with_ts=False))
    monkeypatch.setattr(ADSB, '_adb_position_http', http)
    r = client.get('/api/adsb/state?hex=3c64a8&reg=D-AIPA&own=1')
    b = r.get_json()
    assert b['position'] is None
    assert b['source'] != 'adb'
    adb_tried = [t for t in b['tried'] if t['upstream'] == 'aerodatabox']
    assert adb_tried and adb_tried[0]['reason'] == 'no_location'


def test_adb_stale_position_not_served_as_live(client, monkeypatch):
    # Position 25 min alt → NICHT als live liefern, Kaskade läuft weiter.
    http = MagicMock(return_value=_adb_payload(age_s=1500))
    monkeypatch.setattr(ADSB, '_adb_position_http', http)
    r = client.get('/api/adsb/state?hex=3c64a8&reg=D-AIPA&own=1')
    b = r.get_json()
    assert b['position'] is None
    assert b['source'] != 'adb'
    adb_tried = [t for t in b['tried'] if t['upstream'] == 'aerodatabox']
    assert adb_tried and adb_tried[0]['reason'].startswith('stale_position')


# ══════════════════════════════════════════════════════════════════════════════
# Randfälle: freie Quelle liefert / keine Registration auflösbar
# ══════════════════════════════════════════════════════════════════════════════
def test_free_source_hit_never_calls_adb_even_with_own(client, monkeypatch):
    live_row = ['3c64a8', 'DLH716', None, time.time(), time.time(),
                91.55, 47.31, 11000.0, False, 250.0, 64.0,
                None, None, None, None, False, 0]
    monkeypatch.setattr(ADSB, '_fetch_opensky', lambda h: live_row)
    http = MagicMock(return_value=_adb_payload())
    monkeypatch.setattr(ADSB, '_adb_position_http', http)

    r = client.get('/api/adsb/state?hex=3c64a8&reg=D-AIPA&own=1')
    b = r.get_json()
    assert b['source'] == 'opensky'
    http.assert_not_called()
    BPD._budget_key_inc.assert_not_called()


def test_unknown_registration_skips_tier3(client, monkeypatch):
    # Hex weder in der Backend-Map noch in SB auflösbar, keine reg im Request
    # → kein bezahlter Call ins Blaue.
    http = MagicMock(return_value=_adb_payload())
    monkeypatch.setattr(ADSB, '_adb_position_http', http)
    r = client.get('/api/adsb/state?hex=abc123&own=1')
    b = r.get_json()
    http.assert_not_called()
    BPD._budget_key_inc.assert_not_called()
    adb_tried = [t for t in b['tried'] if t['upstream'] == 'aerodatabox']
    assert adb_tried and adb_tried[0]['reason'] == 'no_registration'
