"""Family-Watch Live-Positions-Fix (Owner-Korrektur 2026-07-06).

„die familie/freunde sind wichtiger als ich — ich sehe meinen flug kaum, da
ich arbeite. aber wenn es sein muss kann man mal 1 pingen um dann zu berechnen
und route korrigieren." — Hauptfall für den bezahlten Tier-3-Ping ist der VON
FAMILIE/FREUNDEN BEOBACHTETE Flug (purpose=watch), nicht der eigene.

ZWEITE Owner-Korrektur (2026-07-06, später): „familie könnte sogar kostenlos
bleiben — er muss halt nur richtig sein mit abflug und ankunft. aber freunde
kann man mal pingen, sehr überwacht" → der FAMILY-Pfad ruft die Kaskade mit
allow_paid=False (NIE ein ADB-Call); der bezahlte purpose=watch-Tier bleibt
den Freunde-Karten (HTTP-Route) vorbehalten.

Abgedeckt:
  (a) flying + Reg + freier Fix → live_*-Werte gesetzt, KEIN Paid-Call
  (b) FAMILY-frei-only: frei leer → KEIN ADB-Call, kein Fix; Fehlschlag
      memoisiert (zweiter Aufruf innerhalb 10 min = kein weiterer Kaskaden-Lauf)
  (c) keine Reg auflösbar → keine Felder, kein Paid-Call, Kaskade läuft nie
  (d) Fix älter 45 min → keine Felder
  (e) Mehr-Leg-Tag ohne eindeutigen aktiven Leg → kein Ping, keine Felder;
      MIT beobachteten Leg-Zeiten → eindeutiger Leg + dessen Reg
  (f) purpose=watch schaltet Tier 3 auch auf der HTTP-Route frei
  (g) Loader-Wiring: live_*-Felder nur unter dem next_flight-Grant

KEIN echtes Netz/SB: freie Quellen, Budget-Helper, Reg-Lookups und der
AeroDataBox-HTTP-Call werden gemockt (Muster: test_adsb_adb_position_tier3).
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


def _os_row(age_s=120, lat=47.31, lon=91.55, vel_ms=250.0, track=64.0):
    """OpenSky-State-Row (Layout wie _fetch_opensky/_fetch_adsb_lol)."""
    ts = time.time() - age_s
    return ['3c64a8', 'DLH716', None, ts, ts, lon, lat, 11000.0, False,
            vel_ms, track, None, None, None, None, False, 0]


def _adb_payload(reg='D-AIXP', age_s=60):
    """AeroDataBox /flights/reg-Antwort (verifiziertes Payload-Format)."""
    return [{
        'number': 'LH 716',
        'callSign': 'DLH716',
        'status': 'EnRoute',
        'aircraft': {'reg': reg},
        'location': {
            'lat': 47.31, 'lon': 91.55,
            'altitude': {'meter': 11582.4},
            'groundSpeed': {'kt': 487.0},
            'trueTrack': {'deg': 64.2},
            'reportedAtUtc': time.strftime(
                '%Y-%m-%d %H:%MZ', time.gmtime(time.time() - age_s)),
        },
    }]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    # SYS.MODULES-PIN (Order-Kontamination, Muster test_adsb_adb_position_tier3):
    # test_calculation.py tauscht sys.modules['app'] per Reimport-Trick aus.
    _prev_app_mod = sys.modules.get('app')
    sys.modules['app'] = A

    ADSB._CACHE.clear()
    FW._LIVE_FIX_MEMO.clear()
    with ADSB._BACKOFF['lock']:
        ADSB._BACKOFF['until'] = 0.0

    # Kein echtes Rate-Limit / SB / Watch-Set / Backfill / Warm-Persist.
    monkeypatch.setattr(ADSB, '_rate_limited', lambda **k: False)
    monkeypatch.setattr(ADSB, '_sb_client', lambda: (None, False))
    monkeypatch.setattr(ADSB, '_backfill_cache_from_sb', lambda h: None)
    monkeypatch.setattr(ADSB, '_touch_watch', lambda *a, **k: None)
    monkeypatch.setattr(ADSB, '_warm_persist_from_opensky_row',
                        lambda *a, **k: None)

    # Freie Quellen: Default sauber „kein Signal" (Coverage-Lücke).
    monkeypatch.setattr(ADSB, '_fetch_opensky', lambda h: None)
    monkeypatch.setattr(ADSB, '_fetch_adsb_lol', lambda h: None)

    # Reg→Hex ohne Backend-Map; ADB-Key vorhanden (HTTP eh gemockt).
    monkeypatch.setattr(ADSB, 'resolve_reg_to_hex', lambda r: '3c64a8')
    monkeypatch.setenv('AERODATABOX_KEY', 'x' * 40)

    # Budget deterministisch frei + Inkremente beobachtbar.
    monkeypatch.setattr(BPD, '_paid_budget_ok', lambda: True)
    monkeypatch.setattr(BPD, '_budget_key_used', lambda k: 0)
    monkeypatch.setattr(BPD, '_budget_key_inc', MagicMock())
    monkeypatch.setattr(BPD, '_paid_budget_inc', MagicMock())

    # Warehouse-Day-Reg default: ehrlich keine (Tests setzen bei Bedarf).
    monkeypatch.setattr(BPD, '_sb_day_reg',
                        lambda fn, d: (None, None, None, None))

    yield

    ADSB._CACHE.clear()
    FW._LIVE_FIX_MEMO.clear()
    if _prev_app_mod is not None:
        sys.modules['app'] = _prev_app_mod


# ══════════════════════════════════════════════════════════════════════════════
# (a) flying + Reg + freier Fix → Felder gesetzt, KEIN Paid-Call
# ══════════════════════════════════════════════════════════════════════════════
def test_free_fix_no_paid_call(monkeypatch):
    # Family ist targeted=False (allow_paid=False) → NUR Tabellen (Tier 1
    # fr24_live + Tier 2 aircraft_positions), kein externer Mirror. Die frei
    # gelieferte Position kommt aus der fr24_live-Tabelle (_fetch_fr24), nicht
    # mehr aus einem synchronen OpenSky-Ping (der ist aus dem User-Pfad raus).
    monkeypatch.setattr(ADSB, '_fetch_fr24',
                        lambda h, callsign=None: _os_row(age_s=120))
    monkeypatch.setattr(BPD, '_sb_day_reg',
                        lambda fn, d: ('D-AIXP', 'A359', 'FRA', 'HND'))
    http = MagicMock(return_value=_adb_payload())
    monkeypatch.setattr(ADSB, '_adb_position_http', http)

    fix = FW._flying_live_fix(['FRA', 'HND'], TODAY, ['LH716'], None)
    assert fix is not None
    assert fix['lat'] == 47.31 and fix['lon'] == 91.55
    assert fix['track'] == 64.0
    assert abs(fix['speed_kt'] - 250.0 / 0.514444) < 0.2
    assert fix['source'] == 'fr24'
    # ECHTER Beobachtungszeitpunkt (time_position der Row), nicht „jetzt".
    assert abs(fix['ts'] - (time.time() - 120)) < 5

    http.assert_not_called()
    BPD._budget_key_inc.assert_not_called()
    BPD._paid_budget_inc.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# (b) FAMILY IST KOSTENLOS: frei leer → KEIN ADB-Call, kein Fix; Memo trägt
# ══════════════════════════════════════════════════════════════════════════════
def test_family_free_only_no_adb_even_when_free_empty(monkeypatch):
    # Owner 2026-07-06: „familie könnte sogar kostenlos bleiben" — selbst in
    # der Coverage-Lücke (freie Quellen leer) zahlt der Family-Pfad NIE; die
    # Karte bleibt Plan-Interpolation (Zeiten sind delay-korrigiert, gratis).
    monkeypatch.setattr(BPD, '_sb_day_reg',
                        lambda fn, d: ('D-AIXP', 'A359', 'FRA', 'HND'))
    # Family liest NUR die Tabellen (targeted=False): Tier 1 = fr24_live-Store.
    # Leer → kein Fix; der bezahlte Tier wird NIE angefasst.
    fr24 = MagicMock(return_value=None)
    monkeypatch.setattr(ADSB, '_fetch_fr24', fr24)
    http = MagicMock(return_value=_adb_payload(reg='D-AIXP', age_s=60))
    monkeypatch.setattr(ADSB, '_adb_position_http', http)

    fix1 = FW._flying_live_fix(['FRA', 'HND'], TODAY, ['LH716'], None)
    assert fix1 is None
    assert fr24.call_count == 1             # freie Tabellen-Kaskade lief …
    http.assert_not_called()                # … Tier 4 (bezahlt) für Family NIE
    BPD._budget_key_inc.assert_not_called()
    BPD._paid_budget_inc.assert_not_called()

    # Fan-out-Kern: auch der FEHLSCHLAG ist memoisiert — 2. Watcher-Aufruf
    # innerhalb 10 min löst keinen weiteren Kaskaden-Lauf aus.
    fix2 = FW._flying_live_fix(['FRA', 'HND'], TODAY, ['LH716'], None)
    assert fix2 is None
    assert fr24.call_count == 1
    http.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# (c) keine Reg auflösbar → keine Felder, kein Paid-Call, Kaskade läuft NIE
# ══════════════════════════════════════════════════════════════════════════════
def test_no_registration_no_fix_no_calls(monkeypatch):
    opensky = MagicMock(return_value=_os_row())
    monkeypatch.setattr(ADSB, '_fetch_opensky', opensky)
    http = MagicMock(return_value=_adb_payload())
    monkeypatch.setattr(ADSB, '_adb_position_http', http)
    # _sb_day_reg liefert (None, …) — Fixture-Default. Keine Leg-Beobachtung.

    fix = FW._flying_live_fix(['FRA', 'HND'], TODAY, ['LH716'], None)
    assert fix is None
    opensky.assert_not_called()      # ohne Reg keine Kaskade (ehrlich)
    http.assert_not_called()
    BPD._budget_key_inc.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# (d) Beobachtung älter 45 min → keine Felder (Interpolation bleibt)
# ══════════════════════════════════════════════════════════════════════════════
def test_stale_observation_not_served(monkeypatch):
    monkeypatch.setattr(ADSB, '_fetch_opensky',
                        lambda h: _os_row(age_s=50 * 60))
    monkeypatch.setattr(BPD, '_sb_day_reg',
                        lambda fn, d: ('D-AIXP', 'A359', 'FRA', 'HND'))
    http = MagicMock(return_value=_adb_payload())
    monkeypatch.setattr(ADSB, '_adb_position_http', http)

    fix = FW._flying_live_fix(['FRA', 'HND'], TODAY, ['LH716'], None)
    assert fix is None
    # Freie Quelle HAT geliefert → nie ein Paid-Call obendrauf.
    http.assert_not_called()
    BPD._budget_key_inc.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# (e) Mehr-Leg-Tag: ohne eindeutigen aktiven Leg kein Ping; mit Zeiten eindeutig
# ══════════════════════════════════════════════════════════════════════════════
def test_multileg_ambiguous_no_ping(monkeypatch):
    opensky = MagicMock(return_value=_os_row())
    monkeypatch.setattr(ADSB, '_fetch_opensky', opensky)
    http = MagicMock(return_value=_adb_payload())
    monkeypatch.setattr(ADSB, '_adb_position_http', http)
    monkeypatch.setattr(BPD, '_sb_day_reg',
                        lambda fn, d: ('D-AIXP', 'A359', 'FRA', 'MUC'))
    # Zwei Legs, Beobachtungen OHNE parsebare Zeiten → nicht eindeutig.
    legs = [{'flight': 'LH100', 'leg_index': 0, 'delay_min': 5},
            {'flight': 'LH717', 'leg_index': 1, 'delay_min': None}]

    fix = FW._flying_live_fix(['FRA', 'MUC', 'HND'], TODAY,
                              ['LH100', 'LH717'], legs)
    assert fix is None
    opensky.assert_not_called()
    http.assert_not_called()
    BPD._budget_key_inc.assert_not_called()


def test_multileg_unambiguous_uses_observed_leg_times(monkeypatch):
    monkeypatch.setattr(ADSB, '_fetch_fr24',
                        lambda h, callsign=None: _os_row(age_s=60))
    sb_day_reg = MagicMock(return_value=(None, None, None, None))
    monkeypatch.setattr(BPD, '_sb_day_reg', sb_day_reg)
    seen_regs = []
    monkeypatch.setattr(ADSB, 'resolve_reg_to_hex',
                        lambda r: seen_regs.append(r) or '3c64a8')

    now = dt.datetime.now(dt.timezone.utc)

    def _iso(delta_min):
        return (now + dt.timedelta(minutes=delta_min)).strftime(
            '%Y-%m-%dT%H:%M:%SZ')

    # Leg 0 längst gelandet, Leg 1 läuft JETZT laut beobachteten Zeiten —
    # eindeutig → dessen beobachtete Reg wird gepingt (kein _sb_day_reg nötig).
    legs = [
        {'flight': 'LH100', 'leg_index': 0, 'reg': 'D-AIXX',
         'sched_dep': _iso(-300), 'sched_arr': _iso(-240)},
        {'flight': 'LH717', 'leg_index': 1, 'reg': 'D-AIXQ',
         'sched_dep': _iso(-60), 'esti_arr': _iso(+300)},
    ]
    fix = FW._flying_live_fix(['FRA', 'MUC', 'HND'], TODAY,
                              ['LH100', 'LH717'], legs)
    assert fix is not None and fix['source'] == 'fr24'
    assert seen_regs == ['D-AIXQ']       # Reg des EINDEUTIG aktiven Legs
    sb_day_reg.assert_not_called()       # Reg kam aus der Leg-Beobachtung


# ══════════════════════════════════════════════════════════════════════════════
# (f) purpose=watch schaltet Tier 3 auch auf der HTTP-Route frei (Gate own|inbound|watch)
# ══════════════════════════════════════════════════════════════════════════════
def test_purpose_watch_enables_tier3_on_route(monkeypatch):
    http = MagicMock(return_value=_adb_payload(reg='D-AIPA', age_s=30))
    monkeypatch.setattr(ADSB, '_adb_position_http', http)
    A.app.testing = True
    client = A.app.test_client()

    r = client.get('/api/adsb/state?hex=3c64a8&reg=D-AIPA&purpose=watch')
    assert r.status_code == 200
    assert r.get_json()['source'] == 'adb'
    assert http.call_count == 1

    # Gegenprobe: unbekannte purpose bleibt untargeted → kein Paid-Call.
    ADSB._CACHE.clear()
    r2 = client.get('/api/adsb/state?hex=3c64a8&reg=D-AIPA&purpose=radar')
    assert r2.get_json()['source'] != 'adb'
    assert http.call_count == 1


# ══════════════════════════════════════════════════════════════════════════════
# (g) Loader-Wiring: live_* nur unter dem next_flight-Grant (Privacy wie flying_now)
# ══════════════════════════════════════════════════════════════════════════════
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


def _loader_env(monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    rows = [{
        'datum': TODAY,
        'ical_summary': 'LH716 FRA-HND 13:55',
        'ical_location': 'HND, FRA-HND',
        'ical_start': (now - dt.timedelta(hours=2)).strftime(
            '%Y-%m-%dT%H:%M:%S+00:00'),
        'ical_end': (now + dt.timedelta(hours=6)).strftime(
            '%Y-%m-%dT%H:%M:%S+00:00'),
    }]
    monkeypatch.setattr(FW, '_get_sb', lambda: (True, _SB(rows)))
    monkeypatch.setattr(FW, '_load_crew_profile',
                        lambda t: {'homebase': 'FRA'})
    monkeypatch.setattr(A, '_profile_load', lambda t: {}, raising=False)
    monkeypatch.setattr(
        A, '_roster_snapshot_read',
        lambda t: {'tage': [{'datum': TODAY,
                             'reader_facts': {'flight_numbers': ['LH716']}}]},
        raising=False)
    monkeypatch.setattr(
        A, '_flight_obs_merged',
        lambda *a, **k: {'delay_min': 12, 'status': 'EnRoute',
                         'reg': 'D-AIXP'},
        raising=False)


def test_loader_sets_live_fields_under_next_flight_grant(monkeypatch):
    _loader_env(monkeypatch)
    monkeypatch.setattr(ADSB, '_fetch_fr24',
                        lambda h, callsign=None: _os_row(age_s=90))

    st = FW._load_crew_status_for_family('tok-live-fix-a', {'next_flight'})
    assert st['flying_now'] is True
    assert st['live_lat'] == 47.31 and st['live_lon'] == 91.55
    assert st['live_track'] == 64.0
    assert abs(st['live_speed_kt'] - 250.0 / 0.514444) < 0.2
    assert st['live_source'] == 'fr24'
    # Kanonischer echter Beobachtungszeitpunkt (UTC-Z, ~90 s alt).
    ts = FW._parse_iso(st['live_ts_iso'])
    age = (dt.datetime.now(dt.timezone.utc) - ts).total_seconds()
    assert 60 < age < 180
    BPD._budget_key_inc.assert_not_called()


def test_loader_without_grant_never_fixes_nor_pings(monkeypatch):
    _loader_env(monkeypatch)
    opensky = MagicMock(return_value=_os_row())
    monkeypatch.setattr(ADSB, '_fetch_opensky', opensky)
    http = MagicMock(return_value=_adb_payload())
    monkeypatch.setattr(ADSB, '_adb_position_http', http)

    st = FW._load_crew_status_for_family('tok-live-fix-b', {'layover_place'})
    for f in ('live_lat', 'live_lon', 'live_track', 'live_speed_kt',
              'live_ts_iso', 'live_source'):
        assert st[f] is None
    opensky.assert_not_called()
    http.assert_not_called()
    BPD._budget_key_inc.assert_not_called()
