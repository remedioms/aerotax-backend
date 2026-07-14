"""
Radar-Fixes 2026-07-05 (Owner-Beweisfoto EZY29CT + Radar-Zeiten + Audit):

1. EZY-LEG-PICK — _adb_pick_active_leg wählt bei Mehr-Leg-Registrierungen das
   aktive Leg nach ZEIT-PRIORITÄT actual > revised/predicted > scheduled aus
   dem bezahlten Payload (EZY29CT flog LGW→SKG, wir zeigten das frühere
   LGW→ACE als „bestätigt"). Ein Leg ohne actual-Ankunft bleibt aktiv, auch
   wenn sched_arr vorbei ist; ein SPÄTERES, lt. eigener Zeiten abgehobenes Leg
   gewinnt. Und: das Geometrie-REJECT-Gate in _resolve_live_route läuft auch
   für confidence='confirmed' — passt kein Leg, gibt es KEINE Route (404).

2. /api/ax/callsign liefert sched_dep/est_dep/sched_arr/est_arr (station-
   lokal) — NUR echte Werte, unbekannte Felder fehlen.

3. /api/ax/callsign + /api/ax/radar-enrich sind per-IP rate-limitiert
   (großzügig fürs App-Polling, gegen anonyme Budget-Drains).

Läuft OHNE app.py-Boot (Blueprint standalone + Monkeypatch):
    pytest tests/aerox/test_radar_leg_pick_times_ratelimit.py -v
"""
from __future__ import annotations

import os
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("AEROTAX_ALLOW_BOOT_WITHOUT_KEY", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from blueprints import aerox_data_blueprint as axd  # noqa: E402

NOW = 1_760_000_000.0          # fixer „jetzt"-Anker (UTC) für alle Zeit-Tests
H = 3600.0

_APS = {
    'LGW': {'iata': 'LGW', 'icao': 'EGKK', 'name': 'Gatwick', 'city': 'London',
            'country': 'GB', 'lat': 51.148, 'lon': -0.190},
    'ACE': {'iata': 'ACE', 'icao': 'GCRR', 'name': 'Lanzarote', 'city': 'Lanzarote',
            'country': 'ES', 'lat': 28.945, 'lon': -13.605},
    'SKG': {'iata': 'SKG', 'icao': 'LGTS', 'name': 'Thessaloniki',
            'city': 'Thessaloniki', 'country': 'GR', 'lat': 40.520, 'lon': 22.971},
}


def _fake_airport_row(code):
    return _APS.get((code or '').strip().upper())


def _utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%d %H:%MZ')


def _local(ts):
    # Payload-typische station-lokale Form (Offset egal für die Tests).
    return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%d %H:%M+01:00')


def _mv(iata, icao, sched=None, revised=None, runway=None):
    d = {'airport': {'iata': iata, 'icao': icao}}
    if sched is not None:
        d['scheduledTime'] = {'utc': _utc(sched), 'local': _local(sched)}
    if revised is not None:
        d['revisedTime'] = {'utc': _utc(revised), 'local': _local(revised)}
    if runway is not None:
        d['runwayTime'] = {'utc': _utc(runway), 'local': _local(runway)}
    return d


def _leg(src, dst, dep, arr, status='Unknown', reg='G-UZHA'):
    return {'departure': _mv(src, _APS[src]['icao'], **dep),
            'arrival': _mv(dst, _APS[dst]['icao'], **arr),
            'status': status, 'aircraft': {'reg': reg}}


# ─────────────────────────────────────────────────────────────────
# 1. _adb_pick_active_leg — Zeit-Priorität aus dem Payload
# ─────────────────────────────────────────────────────────────────

def test_ezy_multileg_picks_current_not_first_reg_match():
    """EZY29CT-Fall: LGW→ACE früher am Tag (actual-Ankunft liegt zurück),
    LGW→SKG abgehoben und offen. Beide Legs = gleiche Reg → der alte
    First-reg-Match nahm ACE. Zeit-Priorität muss SKG wählen."""
    flights = [
        _leg('LGW', 'ACE',
             dep={'sched': NOW - 8 * H, 'runway': NOW - 7.5 * H},
             arr={'sched': NOW - 4.2 * H, 'runway': NOW - 4 * H},
             status='Arrived'),
        _leg('LGW', 'SKG',
             dep={'sched': NOW - 2 * H, 'runway': NOW - 1.6 * H},
             arr={'sched': NOW + 1 * H}),
    ]
    route, amb = axd._adb_pick_active_leg(flights, 'EZY29CT', 'G-UZHA',
                                          track=None, now=NOW)
    assert route is not None
    assert (route['src'], route['dst']) == ('LGW', 'SKG')
    assert amb is False


def test_leg_without_actual_arrival_stays_active_after_sched_arr():
    """Owner: „nach soll zeit wenn verspätung oder irreg nicht das es aus der
    soll zeit wegfällt" — sched_arr vorbei + KEINE actual-Ankunft = Leg bleibt
    aktiv; das künftige Rück-Leg übernimmt NICHT."""
    flights = [
        _leg('LGW', 'ACE',
             dep={'sched': NOW - 5.5 * H, 'runway': NOW - 5 * H},
             arr={'sched': NOW - 1 * H}),               # überfällig, kein actual
        _leg('ACE', 'LGW',
             dep={'sched': NOW + 1 * H},
             arr={'sched': NOW + 5 * H}),
    ]
    route, amb = axd._adb_pick_active_leg(flights, 'EZY29CT', 'G-UZHA',
                                          track=None, now=NOW)
    assert (route['src'], route['dst']) == ('LGW', 'ACE')
    assert amb is False


def test_later_leg_already_airborne_wins_over_stale_open_leg():
    """Hat ein SPÄTERES Leg lt. eigenen actual/est-Zeiten schon abgehoben,
    gewinnt es — auch wenn dem früheren Leg die actual-Ankunft fehlt."""
    flights = [
        _leg('LGW', 'ACE',
             dep={'sched': NOW - 6.5 * H, 'runway': NOW - 6 * H},
             arr={'sched': NOW - 2.5 * H}),             # Datenlücke: kein actual
        _leg('LGW', 'SKG',
             dep={'sched': NOW - 1.4 * H, 'revised': NOW - 1 * H},
             arr={'sched': NOW + 1.6 * H}),
    ]
    route, _amb = axd._adb_pick_active_leg(flights, 'EZY29CT', 'G-UZHA',
                                           track=None, now=NOW)
    assert (route['src'], route['dst']) == ('LGW', 'SKG')


def test_all_legs_completed_returns_last_landed():
    flights = [
        _leg('LGW', 'ACE',
             dep={'runway': NOW - 10 * H}, arr={'runway': NOW - 6 * H}),
        _leg('ACE', 'LGW',
             dep={'runway': NOW - 5 * H}, arr={'runway': NOW - 1 * H}),
    ]
    route, _amb = axd._adb_pick_active_leg(flights, 'EZY29CT', 'G-UZHA',
                                           track=None, now=NOW)
    assert (route['src'], route['dst']) == ('ACE', 'LGW')


def test_payload_without_times_falls_back_to_status(monkeypatch):
    monkeypatch.setattr(axd, '_airport_row', _fake_airport_row)
    flights = [
        {'departure': {'airport': {'iata': 'LGW', 'icao': 'EGKK'}},
         'arrival': {'airport': {'iata': 'ACE', 'icao': 'GCRR'}},
         'status': 'Arrived', 'aircraft': {'reg': 'G-UZHA'}},
        {'departure': {'airport': {'iata': 'LGW', 'icao': 'EGKK'}},
         'arrival': {'airport': {'iata': 'SKG', 'icao': 'LGTS'}},
         'status': 'EnRoute', 'aircraft': {'reg': 'G-UZHA'}},
    ]
    route, amb = axd._adb_pick_active_leg(flights, 'EZY29CT', 'G-UZHA',
                                          track=None, now=NOW)
    assert (route['src'], route['dst']) == ('LGW', 'SKG')
    assert amb is False


def test_adb_flight_to_route_carries_real_local_times_only():
    f = _leg('LGW', 'SKG',
             dep={'sched': NOW - 2 * H, 'runway': NOW - 1.6 * H},
             arr={'sched': NOW + 1 * H})
    r = axd._adb_flight_to_route(f, 'EZY29CT')
    assert r['sched_dep'] == _local(NOW - 2 * H).replace(' ', 'T')
    assert r['est_dep'] == _local(NOW - 1.6 * H).replace(' ', 'T')  # actual > revised
    assert r['sched_arr'] == _local(NOW + 1 * H).replace(' ', 'T')
    assert 'est_arr' not in r          # keine echte est-Ankunft im Payload → fehlt


# ─────────────────────────────────────────────────────────────────
# 1b. _resolve_live_route — Reject-Gate auch für confirmed
# ─────────────────────────────────────────────────────────────────

def _silence_cascade(monkeypatch, cache=None, obs=None):
    monkeypatch.setattr(axd, '_airport_row', _fake_airport_row)
    monkeypatch.setattr(axd, '_cache_get',
                        lambda table, col, key: dict(cache) if (
                            cache and key.startswith('EZY29CT@')) else None)
    monkeypatch.setattr(axd, '_route_from_obs',
                        lambda cs: dict(obs) if obs else None)
    monkeypatch.setattr(axd, '_route_from_warehouse', lambda hexid, reg: None)
    monkeypatch.setattr(axd, '_opensky_route', lambda hexid: None)
    monkeypatch.setattr(axd, '_paid_budget_ok', lambda: False)
    monkeypatch.setattr(axd, '_record_resolved_route',
                        lambda *a, **k: None)


_CACHED_ACE = {'src': 'LGW', 'dst': 'ACE', 'callsign': 'EZY29CT',
               'source': 'aerodatabox', 'confidence': 'confirmed'}

# Live-Position über Nord-Italien, Kurs Richtung SKG (Südost) — klarer
# Widerspruch (>115°) zur behaupteten ACE-Route (Südwest), fern beider Enden.
_POS = (45.0, 8.0)
_TRACK_TO_ACE = axd._bearing_deg(_POS[0], _POS[1], 28.945, -13.605)
_TRACK_AWAY = (_TRACK_TO_ACE + 130.0) % 360.0


def test_resolve_rejects_confirmed_cached_leg_on_clear_contradiction(monkeypatch):
    """Gecachtes „confirmed" ACE-Leg + Live-Track Richtung SKG → Gate verwirft,
    Kaskade hat nichts Besseres → KEINE Route (statt der falschen)."""
    _silence_cascade(monkeypatch, cache=_CACHED_ACE)
    r = axd._resolve_live_route('EZY29CT', lat=_POS[0], lon=_POS[1],
                                track=_TRACK_AWAY)
    assert r is None


def test_resolve_keeps_confirmed_cached_leg_when_geometry_fits(monkeypatch):
    _silence_cascade(monkeypatch, cache=_CACHED_ACE)
    r = axd._resolve_live_route('EZY29CT', lat=_POS[0], lon=_POS[1],
                                track=_TRACK_TO_ACE)
    assert r is not None and r['dst'] == 'ACE'
    assert r['confidence'] == 'confirmed'


def test_resolve_rejected_cache_falls_through_to_matching_board_leg(monkeypatch):
    """Stale ACE-Cache wird verworfen, die eigene Tafel kennt das echte
    SKG-Leg → das wird geliefert (Kaskade läuft weiter, kein 404)."""
    _silence_cascade(monkeypatch, cache=_CACHED_ACE,
                     obs={'src': 'LGW', 'dst': 'SKG', 'callsign': 'EZY29CT',
                          'gate': '55B'})
    r = axd._resolve_live_route('EZY29CT', lat=_POS[0], lon=_POS[1],
                                track=_TRACK_AWAY)
    assert r is not None and r['dst'] == 'SKG'
    assert r['source'] == 'aerox_board' and r['confidence'] == 'confirmed'


def test_resolve_without_live_geometry_keeps_cached_leg(monkeypatch):
    """Reject-only: ohne Position/Kurs ist nichts widerlegbar → confirmed
    Cache wird weiter geliefert (Geometrie ist nie Quelle, nur Veto)."""
    _silence_cascade(monkeypatch, cache=_CACHED_ACE)
    r = axd._resolve_live_route('EZY29CT')
    assert r is not None and r['dst'] == 'ACE'


# ─────────────────────────────────────────────────────────────────
# 2. /api/ax/callsign — echte sched/est-Zeiten (station-lokal)
# ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def client():
    import flask
    app = flask.Flask(__name__)
    app.register_blueprint(axd.aerox_data_bp)
    return app.test_client()


def test_ax_callsign_adds_merged_board_times(client, monkeypatch):
    monkeypatch.setattr(axd, '_ax_rate_limited', lambda *a, **k: False)
    monkeypatch.setattr(axd, '_airport_row', _fake_airport_row)
    monkeypatch.setattr(axd, '_callsign_to_iata_flightno', lambda cs: 'U229CT')
    monkeypatch.setattr(
        axd, '_resolve_live_route',
        lambda cs, **kw: {'src': 'LGW', 'dst': 'SKG', 'callsign': cs,
                          'source': 'aerox_board', 'confidence': 'confirmed'})
    seen = {}

    def fake_merged(fn, dep_iata=None, arr_iata=None, free_only=False, **kw):
        seen.update(fn=fn, dep=dep_iata, arr=arr_iata, free_only=free_only)
        return {'sched_dep': '2026-07-05T18:35', 'esti_dep': '2026-07-05T19:05',
                'sched_arr': '2026-07-06T00:20', 'esti_arr': None}

    monkeypatch.setattr(
        axd, '_life_app',
        lambda name, default=None: fake_merged
        if name == '_flight_obs_merged' else default)

    r = client.get('/api/ax/callsign/EZY29CT')
    assert r.status_code == 200
    body = r.get_json()
    assert body['sched_dep'] == '2026-07-05T18:35'
    assert body['est_dep'] == '2026-07-05T19:05'
    assert body['sched_arr'] == '2026-07-06T00:20'
    assert 'est_arr' not in body                  # unbekannt → Feld fehlt
    assert seen['fn'] == 'U229CT'
    assert seen['free_only'] is True              # strukturell spend-frei
    assert seen['dep'] == 'LGW' and seen['arr'] == 'SKG'


def test_ax_callsign_prefers_times_from_resolved_leg(client, monkeypatch):
    """Trägt das aufgelöste Leg (AeroDataBox-Payload/Tafel) schon alle vier
    Zeiten, wird der Board-Merge nicht bemüht."""
    monkeypatch.setattr(axd, '_ax_rate_limited', lambda *a, **k: False)
    monkeypatch.setattr(axd, '_airport_row', _fake_airport_row)
    monkeypatch.setattr(
        axd, '_resolve_live_route',
        lambda cs, **kw: {'src': 'LGW', 'dst': 'SKG', 'callsign': cs,
                          'source': 'aerodatabox', 'confidence': 'confirmed',
                          'sched_dep': '2026-07-05T18:35+01:00',
                          'est_dep': '2026-07-05T19:02+01:00',
                          'sched_arr': '2026-07-06T00:20+03:00',
                          'est_arr': '2026-07-06T00:41+03:00'})

    def boom(name, default=None):        # Merge DARF nicht angefasst werden
        raise AssertionError('merged fallback should not run')
    monkeypatch.setattr(axd, '_life_app', boom)

    body = client.get('/api/ax/callsign/EZY29CT').get_json()
    assert body['sched_dep'] == '2026-07-05T18:35+01:00'
    assert body['est_arr'] == '2026-07-06T00:41+03:00'


def test_ax_callsign_no_route_is_404_not_a_guess(client, monkeypatch):
    monkeypatch.setattr(axd, '_ax_rate_limited', lambda *a, **k: False)
    monkeypatch.setattr(axd, '_resolve_live_route', lambda cs, **kw: None)
    r = client.get('/api/ax/callsign/EZY29CT')
    assert r.status_code == 404


def test_route_from_obs_passes_real_board_times(monkeypatch):
    rows = [{'date': axd._today_utc(), 'airport': 'LGW', 'dest_iata': 'SKG',
             'gate': '55B', 'terminal': None, 'sched': '18:35', 'esti': '19:05'}]

    class _Q:
        def select(self, *a, **k): return self
        def eq(self, *a): return self
        def gte(self, *a): return self
        def order(self, *a, **k): return self
        def limit(self, n): return self
        def execute(self): return types.SimpleNamespace(data=rows)

    class _SB:
        def table(self, name): return _Q()

    monkeypatch.setattr(axd, '_sb', lambda: _SB())
    monkeypatch.setattr(axd, '_airline_row', lambda code: {'iata': 'U2'})
    r = axd._route_from_obs('EZY29CT')
    assert r is not None and r['dst'] == 'SKG'
    assert r['sched_dep'] == '18:35' and r['est_dep'] == '19:05'

    rows[0]['esti'] = None               # Tafel kennt keine est-Zeit → Feld fehlt
    r2 = axd._route_from_obs('EZY29CT')
    assert 'est_dep' not in r2 and r2['sched_dep'] == '18:35'


# ─────────────────────────────────────────────────────────────────
# 3. Rate-Limit (per IP) auf callsign + radar-enrich
# ─────────────────────────────────────────────────────────────────

def test_ax_callsign_rate_limited_429(client, monkeypatch):
    monkeypatch.setattr(axd, '_ax_rate_limited', lambda *a, **k: True)
    r = client.get('/api/ax/callsign/EZY29CT')
    assert r.status_code == 429
    assert r.get_json()['error'] == 'rate_limited'


def test_ax_radar_enrich_rate_limited_429(client, monkeypatch):
    monkeypatch.setattr(axd, '_ax_rate_limited', lambda *a, **k: True)
    r = client.post('/api/ax/radar-enrich', json={'hexes': ['3c675a']})
    assert r.status_code == 429


def test_ax_radar_enrich_passes_when_not_limited(client, monkeypatch):
    monkeypatch.setattr(axd, '_ax_rate_limited', lambda *a, **k: False)
    monkeypatch.setattr(axd, '_sb', lambda: None)
    r = client.post('/api/ax/radar-enrich', json={'hexes': ['3c675a']})
    assert r.status_code == 200
    assert r.get_json()['ok'] is True


def test_ax_radar_enrich_forwards_arr_delay_when_no_est(client, monkeypatch):
    """EINE Wahrheit mit dem crew_state-Resolver (Owner 2026-07-13): kennt die
    ARR-Obs eine Verspätungs-ZAHL, aber KEINE eigene esti-Uhrzeit, muss der
    Radar-Callout `arr_delay_min` mitbekommen — sonst rechnet iOS
    est_arr−sched_arr=0 und zeigt fälschlich „pünktlich". Genau der Fall, in
    dem `_eff_arr` auf sched+arr_delay_min zurückfällt."""
    monkeypatch.setattr(axd, '_ax_rate_limited', lambda *a, **k: False)
    # flights-Row: Abflug im Live-Fenster, ABER ohne Ankunftszeiten
    # (sched_arr/est_arr fehlen → ARR-Obs-Fallback greift).
    _now = datetime.now(timezone.utc)
    _dep_iso = _now.strftime('%Y-%m-%dT%H:%M:%SZ')
    flights_rows = [{'hex': '3c675a', 'op_flight_no': 'LH400',
                     'origin': 'FRA', 'destination': 'SKG',
                     'gate': 'A26', 'status': 'Departed', 'tail': None,
                     'sched_dep': _dep_iso, 'est_dep': _dep_iso,
                     'sched_arr': None, 'est_arr': None}]
    # ARR-Obs: Verspätung bekannt (+45), aber KEINE esti-Uhrzeit.
    arr_rows = [{'airport': 'SKG#ARR', 'flight': 'LH400',
                 'sched': (_now + timedelta(hours=2)).isoformat(),
                 'esti': None, 'max_delay_min': 45,
                 'date': axd._today_utc()}]

    class _Q:
        def __init__(self, data): self._data = data
        def select(self, *a, **k): return self
        def in_(self, *a, **k): return self
        def gte(self, *a, **k): return self
        def order(self, *a, **k): return self
        def limit(self, n): return self
        def execute(self):
            return types.SimpleNamespace(data=self._data)

    class _SB:
        def table(self, name):
            return _Q(flights_rows if name == 'flights' else arr_rows)

    monkeypatch.setattr(axd, '_sb', lambda: _SB())
    r = client.post('/api/ax/radar-enrich', json={'hexes': ['3c675a']})
    assert r.status_code == 200
    body = r.get_json()
    entry = body['routes'].get('3c675a')
    assert entry is not None
    # Delay-Zahl durchgereicht (nicht erfunden), obwohl keine est_arr existiert.
    assert entry.get('arr_delay_min') == 45
    assert 'est_arr' not in entry            # ehrlich: keine erfundene Uhrzeit


def _enrich_sb(flights_rows, arr_rows=None):
    class _Q:
        def __init__(self, data): self._data = data
        def select(self, *a, **k): return self
        def in_(self, *a, **k): return self
        def gte(self, *a, **k): return self
        def order(self, *a, **k): return self
        def limit(self, n): return self
        def execute(self):
            return types.SimpleNamespace(data=self._data)

    class _SB:
        def table(self, name):
            return _Q(flights_rows if name == 'flights' else (arr_rows or []))
    return _SB()


def test_ax_radar_enrich_drops_stale_rotation_tail_hex_mismatch(client, monkeypatch):
    """Stale-Rotations-Falle (2026-07-14, D-AIZD hex 3c6744): eine flights-Row
    trägt den abgefragten hex, aber den Tail einer VORTAGES-Rotation (DAIZC,
    dessen echter hex 3c6743 ist). Der Callout klebte so LH1212/GVA auf
    D-AIZD statt LH1346/WAW. Löst der Row-Tail auf einen ANDEREN echten hex
    auf → Row verwerfen (nicht anheften)."""
    monkeypatch.setattr(axd, '_ax_rate_limited', lambda *a, **k: False)
    _dep_iso = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    flights_rows = [{'hex': '3c6744', 'op_flight_no': 'LH1212',
                     'origin': 'FRA', 'destination': 'GVA',
                     'gate': 'A32', 'status': 'Boarding', 'tail': 'DAIZC',
                     'sched_dep': _dep_iso, 'est_dep': _dep_iso,
                     'sched_arr': None, 'est_arr': None}]
    monkeypatch.setattr(axd, '_sb', lambda: _enrich_sb(flights_rows))
    # DAIZC → echter hex 3c6743 (≠ abgefragter 3c6744).
    import blueprints.adsb_blueprint as _adsb
    monkeypatch.setattr(_adsb, '_baked_hex_for_reg',
                        lambda reg: '3c6743' if (reg or '').upper() == 'DAIZC' else None)
    r = client.post('/api/ax/radar-enrich', json={'hexes': ['3c6744']})
    assert r.status_code == 200
    body = r.get_json()
    # Kein LH1212/GVA-Eintrag auf D-AIZDs hex — der Tap-Resolver liefert
    # danach das korrekte LH1346/WAW.
    assert body['routes'].get('3c6744') is None
    assert body['count'] == 0


def test_ax_radar_enrich_keeps_matching_tail_hex(client, monkeypatch):
    """Fail-CLOSED-Gegenprobe: stimmt der Tail-hex mit dem abgefragten hex
    überein, bleibt die Row (der Normalfall darf NIE fallen)."""
    monkeypatch.setattr(axd, '_ax_rate_limited', lambda *a, **k: False)
    _dep_iso = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    flights_rows = [{'hex': '3c6744', 'op_flight_no': 'LH1346',
                     'origin': 'FRA', 'destination': 'WAW',
                     'gate': 'A17', 'status': 'Boarding', 'tail': 'DAIZD',
                     'sched_dep': _dep_iso, 'est_dep': _dep_iso,
                     'sched_arr': _dep_iso, 'est_arr': _dep_iso}]
    monkeypatch.setattr(axd, '_sb', lambda: _enrich_sb(flights_rows))
    import blueprints.adsb_blueprint as _adsb
    monkeypatch.setattr(_adsb, '_baked_hex_for_reg',
                        lambda reg: '3c6744' if (reg or '').upper() == 'DAIZD' else None)
    body = client.post('/api/ax/radar-enrich',
                       json={'hexes': ['3c6744']}).get_json()
    entry = body['routes'].get('3c6744')
    assert entry is not None
    assert entry['flight_no'] == 'LH1346' and entry['dst'] == 'WAW'


def test_ax_radar_enrich_keeps_row_when_tail_hex_unknown(client, monkeypatch):
    """Fail-OPEN: kennt die Referenz-DB den Tail-hex nicht (None), bleibt die
    Row — eine Referenz-Lücke darf keine echte Route verlieren."""
    monkeypatch.setattr(axd, '_ax_rate_limited', lambda *a, **k: False)
    _dep_iso = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    flights_rows = [{'hex': 'abc123', 'op_flight_no': 'XY99',
                     'origin': 'FRA', 'destination': 'JFK',
                     'gate': 'B12', 'status': 'Boarding', 'tail': 'N123ZZ',
                     'sched_dep': _dep_iso, 'est_dep': _dep_iso,
                     'sched_arr': _dep_iso, 'est_arr': _dep_iso}]
    monkeypatch.setattr(axd, '_sb', lambda: _enrich_sb(flights_rows))
    import blueprints.adsb_blueprint as _adsb
    monkeypatch.setattr(_adsb, '_baked_hex_for_reg', lambda reg: None)
    body = client.post('/api/ax/radar-enrich',
                       json={'hexes': ['abc123']}).get_json()
    assert body['routes'].get('abc123') is not None


def test_ax_radar_enrich_replaces_stale_previous_day_arrival(client, monkeypatch):
    """Live-Regression (2026-07-14, D-AIZD/LH1346): Route/Tail und heutiger
    Abflug waren korrekt, aber die flights-Row trug sched_arr vom Vortag. Die
    unmögliche Ankunft muss fallen und aus der heutigen ARR-Obs ersetzt werden."""
    monkeypatch.setattr(axd, '_ax_rate_limited', lambda *a, **k: False)
    now = datetime.now(timezone.utc)
    today = now.strftime('%Y-%m-%d')
    yesterday = (now - timedelta(days=1)).strftime('%Y-%m-%d')
    dep_iso = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    flights_rows = [{'hex': '3c6744', 'op_flight_no': 'LH1346',
                     'origin': 'FRA', 'destination': 'WAW',
                     'gate': 'A17', 'status': 'Gate open', 'tail': 'DAIZD',
                     'sched_dep': dep_iso, 'est_dep': dep_iso,
                     'sched_arr': yesterday + 'T09:00:00+02:00',
                     'est_arr': None}]
    expected_arr = now + timedelta(hours=2)
    arr_rows = [{'airport': 'WAW#ARR', 'flight': 'LH1346',
                 'sched': expected_arr.isoformat(),
                 'esti': None, 'max_delay_min': 0, 'date': today}]
    monkeypatch.setattr(axd, '_sb', lambda: _enrich_sb(flights_rows, arr_rows))
    import blueprints.adsb_blueprint as _adsb
    monkeypatch.setattr(_adsb, '_baked_hex_for_reg', lambda reg: '3c6744')

    body = client.post('/api/ax/radar-enrich',
                       json={'hexes': ['3c6744']}).get_json()
    entry = body['routes']['3c6744']
    assert entry['flight_no'] == 'LH1346' and entry['dst'] == 'WAW'
    actual_arr = datetime.fromisoformat(entry['sched_arr'].replace('Z', '+00:00'))
    assert abs((actual_arr - expected_arr).total_seconds()) < 1
    assert yesterday not in entry['sched_arr']


def test_ax_radar_enrich_does_not_reinsert_only_stale_arrival(client, monkeypatch):
    """Prod-Gegenprobe nach dem ersten Fix: Fehlt die heutige ARR-Obs komplett,
    darf der [gestern, heute]-Fallback die verworfene Vortageszeit nicht erneut
    einsetzen (D-AIZD lieferte sonst nach Deploy weiter 13.07. zu dep 14.07.)."""
    monkeypatch.setattr(axd, '_ax_rate_limited', lambda *a, **k: False)
    now = datetime.now(timezone.utc)
    yesterday = (now - timedelta(days=1)).strftime('%Y-%m-%d')
    dep_iso = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    flights_rows = [{'hex': '3c6744', 'op_flight_no': 'LH1346',
                     'origin': 'FRA', 'destination': 'WAW',
                     'gate': 'A17', 'status': 'Gate open', 'tail': 'DAIZD',
                     'sched_dep': dep_iso, 'est_dep': dep_iso,
                     'sched_arr': yesterday + 'T09:00:00+02:00',
                     'est_arr': None}]
    stale_arr_rows = [{'airport': 'WAW#ARR', 'flight': 'LH1346',
                       'sched': '09:00', 'esti': None,
                       'max_delay_min': 0, 'date': yesterday}]
    monkeypatch.setattr(
        axd, '_sb', lambda: _enrich_sb(flights_rows, stale_arr_rows))
    import blueprints.adsb_blueprint as _adsb
    monkeypatch.setattr(_adsb, '_baked_hex_for_reg', lambda reg: '3c6744')

    body = client.post('/api/ax/radar-enrich',
                       json={'hexes': ['3c6744']}).get_json()
    entry = body['routes']['3c6744']
    assert entry['flight_no'] == 'LH1346' and entry['dst'] == 'WAW'
    assert 'sched_arr' not in entry
    assert 'est_arr' not in entry
    assert 'arr_delay_min' not in entry


def test_rate_limit_wired_through_adsb_pattern(client, monkeypatch):
    """_ax_rate_limited delegiert an das adsb_blueprint-Muster (per-IP) mit dem
    richtigen Endpoint-Bucket — via Stub-Modul, ohne app.py zu booten."""
    calls = []
    stub = types.ModuleType('blueprints.adsb_blueprint')

    def fake_rate_limited(*, ip=None, token=None, endpoint='', limit=0,
                          window_sec=0):
        calls.append({'ip': ip, 'endpoint': endpoint, 'limit': limit,
                      'window_sec': window_sec})
        return True

    stub._rate_limited = fake_rate_limited
    stub._req_ip = lambda req: '203.0.113.7'
    monkeypatch.setitem(sys.modules, 'blueprints.adsb_blueprint', stub)

    r = client.get('/api/ax/callsign/EZY29CT')
    assert r.status_code == 429
    assert calls and calls[0]['endpoint'] == 'ax_callsign'
    assert calls[0]['ip'] == '203.0.113.7'
    assert calls[0]['limit'] >= 60      # großzügig fürs legitime App-Polling

    r2 = client.post('/api/ax/radar-enrich', json={'hexes': ['abc123']})
    assert r2.status_code == 429
    assert calls[-1]['endpoint'] == 'ax_radar_enrich'


def test_ax_rate_limited_fails_open_without_request_context():
    assert axd._ax_rate_limited('ax_callsign', 120, 60) is False


# ─────────────────────────────────────────────────────────────────
# 4. own=1-Paid-Gate braucht Bearer (Sweep 2026-07-10, Klasse A)
# ─────────────────────────────────────────────────────────────────

def _capture_allow_paid(monkeypatch):
    seen = {}

    def fake_resolve(cs, **kw):
        seen['allow_paid'] = kw.get('allow_paid')
        return {'src': 'LGW', 'dst': 'SKG', 'callsign': cs,
                'source': 'aerox_board', 'confidence': 'confirmed',
                'sched_dep': '2026-07-05T18:35+01:00',
                'sched_arr': '2026-07-06T00:20+03:00'}
    monkeypatch.setattr(axd, '_ax_rate_limited', lambda *a, **k: False)
    monkeypatch.setattr(axd, '_airport_row', _fake_airport_row)
    monkeypatch.setattr(axd, '_resolve_live_route', fake_resolve)
    monkeypatch.setattr(axd, '_life_app', lambda name, default=None: default)
    return seen


def test_ax_callsign_own_without_bearer_is_free(client, monkeypatch):
    """own=1 allein schaltet kein Paid mehr — anonymes curl darf nie Credits
    ziehen (Muster vom uflight-Gate)."""
    seen = _capture_allow_paid(monkeypatch)
    r = client.get('/api/ax/callsign/EZY29CT?own=1')
    assert r.status_code == 200
    assert seen['allow_paid'] is False


def test_ax_callsign_own_with_bearer_allows_paid(client, monkeypatch):
    """Mit Bearer-Header (App-Client) bleibt own=1 der bezahlte Notnagel."""
    seen = _capture_allow_paid(monkeypatch)
    r = client.get('/api/ax/callsign/EZY29CT?own=1',
                   headers={'Authorization': 'Bearer AT-TEST'})
    assert r.status_code == 200
    assert seen['allow_paid'] is True


# ─────────────────────────────────────────────────────────────────
# 5. ax_schedule: Rate-Limit + Negativ-Cache + route-history-Fallback
# ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clear_schedule_neg():
    axd._SCHEDULE_NEG.clear()
    yield
    axd._SCHEDULE_NEG.clear()


def test_ax_schedule_rate_limited_429(client, monkeypatch):
    monkeypatch.setattr(axd, '_ax_rate_limited', lambda *a, **k: True)
    r = client.get('/api/ax/schedule/FRA/LIS')
    assert r.status_code == 429
    assert r.get_json()['error'] == 'rate_limited'


def _schedule_env(monkeypatch, http_result):
    """AviationStack-Key da, Budget frei, SB-Cache leer, _http_json gestubbt."""
    calls = {'http': 0}
    monkeypatch.setattr(axd, '_ax_rate_limited', lambda *a, **k: False)
    monkeypatch.setenv('AVIATIONSTACK_KEY', 'k-test')
    monkeypatch.setattr(axd, '_budget_remaining', lambda m: (80, 10))
    monkeypatch.setattr(axd, '_budget_inc', lambda m, u: None)
    monkeypatch.setattr(axd, '_cache_get', lambda *a, **k: None)
    monkeypatch.setattr(axd, '_cache_put', lambda *a, **k: None)

    def fake_http(url, timeout=8):
        calls['http'] += 1
        return http_result
    monkeypatch.setattr(axd, '_http_json', fake_http)
    return calls


def _hist_payload():
    return {'ok': True, 'total': 2, 'recent_days': [
        {'date': '2026-07-09', 'count': 2, 'flights': [
            {'flight': 'LH400', 'airline': 'Lufthansa',
             'sched': '2026-07-09T10:05:00', 'sched_arr': '2026-07-09T12:55:00',
             'obs': 'both', 'dep_delay_min': 5, 'arr_delay_min': 12,
             'cancelled': False, 'status': 'ontime'},
            # reine arr-Row: 'sched' ist die ANKUNFTS-Zeit → darf nie als
            # Abflugzeit im Fallback landen.
            {'flight': 'UA961', 'airline': 'United',
             'sched': '2026-07-09T13:10:00', 'obs': 'arr',
             'arr_delay_min': None, 'cancelled': False, 'status': 'unknown'},
        ]}]}


def test_ax_schedule_error_falls_back_and_neg_caches(client, monkeypatch):
    """AviationStack-Fehler → route-history-Fallback statt leer, UND der tote
    Call wird 45 min negativ gecacht (zweiter Request rennt NICHT erneut rein)."""
    calls = _schedule_env(monkeypatch, None)          # _http_json → error
    monkeypatch.setattr(axd, '_life_app',
                        lambda name, default=None:
                        (lambda *a, **k: None) if name == 'ax_route_history'
                        else default)
    monkeypatch.setattr(axd, '_detail_subcall',
                        lambda app_obj, path, view, *a: _hist_payload())

    r = client.get('/api/ax/schedule/FRA/JFK')
    assert r.status_code == 200
    body = r.get_json()
    assert body['source'] == 'route-history'
    assert calls['http'] == 1
    lh = next(f for f in body['flights'] if f['flight'] == 'LH400')
    assert lh['dep_scheduled'] == '2026-07-09T10:05:00'
    assert lh['arr_scheduled'] == '2026-07-09T12:55:00'
    assert lh['dep_delay'] == 5 and lh['arr_delay'] == 12
    ua = next(f for f in body['flights'] if f['flight'] == 'UA961')
    assert ua['dep_scheduled'] is None                # arr-Row ≠ Abflugzeit
    assert ua['arr_scheduled'] == '2026-07-09T13:10:00'

    # Zweiter Request: Negativ-Cache greift → KEIN weiterer externer Call.
    r2 = client.get('/api/ax/schedule/FRA/JFK')
    assert r2.status_code == 200
    assert r2.get_json()['source'] == 'route-history'
    assert calls['http'] == 1


def test_ax_schedule_no_budget_falls_back_to_route_history(client, monkeypatch):
    """Kein Key/Budget → statt 'budget-exhausted'+leer kommen die eigenen
    Warehouse-Beobachtungen (free-first)."""
    monkeypatch.setattr(axd, '_ax_rate_limited', lambda *a, **k: False)
    monkeypatch.delenv('AVIATIONSTACK_KEY', raising=False)
    monkeypatch.setattr(axd, '_budget_remaining', lambda m: (0, 90))
    monkeypatch.setattr(axd, '_cache_get', lambda *a, **k: None)
    monkeypatch.setattr(axd, '_life_app',
                        lambda name, default=None:
                        (lambda *a, **k: None) if name == 'ax_route_history'
                        else default)
    monkeypatch.setattr(axd, '_detail_subcall',
                        lambda app_obj, path, view, *a: _hist_payload())
    body = client.get('/api/ax/schedule/FRA/JFK').get_json()
    assert body['source'] == 'route-history'
    assert body['count'] == 2

    # Fallback auch leer → ehrlich 'budget-exhausted' mit leerer Liste.
    monkeypatch.setattr(axd, '_detail_subcall',
                        lambda app_obj, path, view, *a: {'ok': True,
                                                         'recent_days': []})
    body2 = client.get('/api/ax/schedule/FRA/LIS').get_json()
    assert body2['source'] == 'budget-exhausted' and body2['flights'] == []


# ─────────────────────────────────────────────────────────────────
# 6. _cache_get: Staleness-Gate (Sweep 2026-07-10, Klasse B)
# ─────────────────────────────────────────────────────────────────

def _fake_sb_row(payload, updated_at):
    class _Q:
        def select(self, *a, **k): return self
        def eq(self, *a): return self
        def limit(self, n): return self
        def execute(self):
            return types.SimpleNamespace(
                data=[{'payload': payload, 'updated_at': updated_at}])

    class _SB:
        def table(self, name): return _Q()
    return _SB()


def _iso_days_ago(days):
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(days=days)) \
        .strftime('%Y-%m-%dT%H:%M:%SZ')


def test_cache_get_fresh_entry_hits(monkeypatch):
    p = {'src': 'FRA', 'dst': 'SPU', 'confidence': 'confirmed'}
    monkeypatch.setattr(axd, '_sb',
                        lambda: _fake_sb_row(p, _iso_days_ago(30)))
    assert axd._cache_get('ax_route_cache', 'flight', 'LH1412') == p


def test_cache_get_expired_confirmed_is_miss(monkeypatch):
    """>90 Tage alte Route = Miss (Flugnummern werden saisonal umgeroutet —
    LH1412-Vorfall): Free-Kaskade läuft neu, upsert überschreibt."""
    p = {'src': 'FRA', 'dst': 'SPU', 'confidence': 'confirmed'}
    monkeypatch.setattr(axd, '_sb',
                        lambda: _fake_sb_row(p, _iso_days_ago(120)))
    assert axd._cache_get('ax_route_cache', 'flight', 'LH1412') is None


def test_cache_get_estimated_expires_after_14_days(monkeypatch):
    """Geratene Routen (confidence != confirmed) verfallen schon nach 14 Tagen."""
    p = {'src': 'FRA', 'dst': 'SPU', 'confidence': 'estimated'}
    monkeypatch.setattr(axd, '_sb',
                        lambda: _fake_sb_row(p, _iso_days_ago(20)))
    assert axd._cache_get('ax_route_cache', 'flight', 'LH1412') is None
    monkeypatch.setattr(axd, '_sb',
                        lambda: _fake_sb_row(p, _iso_days_ago(5)))
    assert axd._cache_get('ax_route_cache', 'flight', 'LH1412') == p


def test_cache_get_max_age_none_keeps_old_semantics(monkeypatch):
    """max_age_days=None (ax_schedule_cache mit eigener _fetched-Logik) und
    Legacy-Rows ohne updated_at bleiben gültig — kein Massen-Miss."""
    p = {'flights': [], 'count': 0, '_fetched': 1}
    monkeypatch.setattr(axd, '_sb',
                        lambda: _fake_sb_row(p, _iso_days_ago(400)))
    assert axd._cache_get('ax_schedule_cache', 'route', 'FRA-LIS#cs3',
                          max_age_days=None) == p
    monkeypatch.setattr(axd, '_sb', lambda: _fake_sb_row(p, None))
    assert axd._cache_get('ax_route_cache', 'flight', 'LH1412') == p
