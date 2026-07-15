"""Regression: Crew-Live darf bei einer Lücke im Round-robin-Harvester weder
verschwinden noch über einen veralteten Roster-Tail zu einem fremden Flug springen.
Alle getesteten Nachladepfade sind der anonyme FR24-gRPC-Korridor (kein paid API).
"""
from types import SimpleNamespace

import app
import blueprints.adsb_blueprint as ADSB
import blueprints.aerox_data_blueprint as DATA
import blueprints.fr24_grpc as GRPC


STALE = {
    'reg': 'DABYH', 'reg_display': 'D-ABYH', 'callsign': 'DLH7K',
    'flight': 'LH422', 'lat': 51.16, 'lon': -59.56, 'track': 252,
    'gs_kt': 470, 'alt_ft': 38000, 'origin': 'FRA', 'dest': 'BOS',
    'ac_type': 'B748', 'on_ground': False,
    'seen_ts': '2026-07-14T17:26:14Z', 'updated_at': '2026-07-14T17:26:17Z',
}


class _Query:
    def __init__(self, owner):
        self.owner = owner

    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def gt(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self

    def execute(self):
        return SimpleNamespace(data=[dict(STALE)])

    def upsert(self, payload, on_conflict=None):
        self.owner.writes.append((payload, on_conflict))
        return _WriteResult()


class _WriteResult:
    def execute(self):
        return SimpleNamespace(data=[])


class _SB:
    def __init__(self): self.writes = []
    def table(self, _name): return _Query(self)


def _airports(code):
    return {'FRA': (50.03, 8.57), 'BOS': (42.36, -71.01)}.get(code)


def test_targeted_free_corridor_refreshes_and_warms_store(monkeypatch):
    sb = _SB()
    calls = []
    live = {
        'lat': 44.20, 'lon': -67.85, 'track': 250, 'alt': 38000,
        'speed': 472, 'route_from': 'FRA', 'route_to': 'BOS',
        'reg': 'D-ABYH', 'callsign': 'DLH7K', 'obs_ts': 1784061240,
    }
    monkeypatch.setattr(DATA, '_sb', lambda: sb)
    monkeypatch.setattr(DATA, '_iata_latlon', _airports)
    monkeypatch.setattr(GRPC, 'inbound_by_route',
                        lambda *a, **k: calls.append((a, k)) or dict(live))
    DATA._FREE_CREW_LIVE_MEMO.clear()

    pos, route, reg, typ = DATA._free_crew_live_pos('LH422', 'FRA', 'BOS')
    assert (pos['lat'], pos['lon']) == (44.20, -67.85)
    assert pos['source'] == 'fr24_grpc_corridor'
    assert pos['seen_ts'] and route == ('FRA', 'BOS')
    assert reg == 'D-ABYH' and typ == 'B748'
    assert len(calls) == 1
    assert sb.writes and sb.writes[0][0]['flight'] == 'LH422'
    assert sb.writes[0][0]['reg'] == 'DABYH'

    # Nico + Julien sitzen im selben Flug: zweiter Personen-Lookup ist Memo,
    # kein zweiter externer Call.
    again = DATA._free_crew_live_pos('LH422', 'FRA', 'BOS')
    assert again[0]['lon'] == -67.85 and len(calls) == 1


def test_wrong_route_candidate_never_replaces_last_known(monkeypatch):
    sb = _SB()
    monkeypatch.setattr(DATA, '_sb', lambda: sb)
    monkeypatch.setattr(DATA, '_iata_latlon', _airports)
    monkeypatch.setattr(GRPC, 'inbound_by_route', lambda *a, **k: {
        'lat': 25.78, 'lon': -80.37, 'track': 220, 'alt': 4000,
        'speed': 190, 'route_from': 'FRA', 'route_to': 'MIA',
        'reg': 'D-ABYF', 'callsign': 'DLH462', 'obs_ts': 1784061240,
    })
    DATA._FREE_CREW_LIVE_MEMO.clear()

    pos, route, reg, _ = DATA._free_crew_live_pos('LH422', 'FRA', 'BOS')
    assert (pos['lat'], pos['lon']) == (STALE['lat'], STALE['lon'])
    assert pos['source'] == 'aircraft_live_last_known'
    assert pos['seen_ts'] == STALE['seen_ts']
    assert route == ('FRA', 'BOS') and reg == 'D-ABYH'
    assert not sb.writes


def test_overview_merges_grpc_even_when_store_has_some_aircraft(monkeypatch):
    ADSB._AREA_CACHE.clear()
    ADSB._AREA_TILE_CACHE.clear()
    store = [
        {'reg': 'D-ABYH', 'callsign': 'DLH7K', 'flight': 'LH422',
         'lat': 51.16, 'lon': -59.56},
        {'reg': 'D-AIXA', 'callsign': 'DLH400', 'flight': 'LH400',
         'lat': 48.0, 'lon': -45.0},
    ]
    direct = [
        {'reg': 'D-ABYH', 'callsign': 'DLH7K', 'flight': 'DLH7K',
         'lat': 44.20, 'lon': -67.85},
        {'reg': 'D-AIXB', 'callsign': 'DLH410', 'flight': 'DLH410',
         'lat': 46.0, 'lon': -54.0},
    ]
    monkeypatch.setattr(ADSB, '_rate_limited', lambda **k: False)
    monkeypatch.setattr(ADSB, '_area_from_aircraft_live', lambda *a: list(store))
    monkeypatch.setattr(ADSB, '_area_from_fr24_grpc', lambda *a: list(direct))
    monkeypatch.setattr(ADSB, 'observe_adsb_positions', None, raising=False)

    client = app.app.test_client()
    body = client.get('/api/adsb/area?lat=51.16&lon=-59.56&radius=600').get_json()
    assert body['ok'] and body['source'] == 'aircraft_live+fr24_grpc'
    assert body['count'] == 3
    lh422 = [x for x in body['aircraft'] if x.get('reg') == 'D-ABYH']
    assert len(lh422) == 1 and lh422[0]['lon'] == -67.85

