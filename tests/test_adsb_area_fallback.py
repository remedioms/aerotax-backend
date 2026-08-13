"""Mirror-Blackout 13.08.2026: alle drei ADS-B-Community-Quellen leer (HTTP 200,
0 Flieger) -> der Nah-Zoom muss auf die eigene aircraft_live-Ernte zurueckfallen
statt eine leere Karte zu liefern."""
import json
import pytest

import app as A
import blueprints.adsb_blueprint as bp


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(bp, "_rate_limited", lambda **k: False)
    monkeypatch.setattr(bp, "_area_cache_get", lambda k, ttl: None)
    monkeypatch.setattr(bp, "_area_tile_cache_get", lambda k: None)
    monkeypatch.setattr(bp, "_area_cache_put", lambda k, p: None)
    A.app.testing = True
    return A.app.test_client()


def test_leere_mirrors_fallen_auf_eigene_ernte(client, monkeypatch):
    monkeypatch.setattr(bp, "_fetch_adsb_point_merged", lambda la, lo, r: [])
    own = [{"hex": "3c6754", "flight": "DLH123", "lat": 50.1, "lon": 8.6,
            "alt": 30000, "speed": 400, "heading": 90, "on_ground": False,
            "reg": "D-AIXF", "type": "A359", "squawk": None}]
    monkeypatch.setattr(bp, "_area_from_aircraft_live", lambda la, lo, r: own)
    r = client.get("/api/adsb/area?lat=50.05&lon=8.60&radius=60")
    d = json.loads(r.data)
    assert r.status_code == 200
    assert len(d["aircraft"]) == 1
    assert d["source"] == "aircraft_live-fallback"


def test_wirklich_leerer_himmel_bleibt_leer(client, monkeypatch):
    monkeypatch.setattr(bp, "_fetch_adsb_point_merged", lambda la, lo, r: [])
    monkeypatch.setattr(bp, "_area_from_aircraft_live", lambda la, lo, r: [])
    r = client.get("/api/adsb/area?lat=0.0&lon=-140.0&radius=60")
    d = json.loads(r.data)
    assert r.status_code == 200
    assert d["aircraft"] == []


def test_laecherlich_wenige_mirror_treffer_werden_aufgefuellt(client, monkeypatch):
    mirror = [{"hex": "aaaaaa", "flight": "X1", "lat": 50.0, "lon": 8.5,
               "alt": 10000, "speed": 300, "heading": 10, "on_ground": False,
               "reg": "D-TEST", "type": "A320", "squawk": None}]
    own = [{"hex": "aaaaaa", "flight": "X1-ALT", "lat": 50.0, "lon": 8.5,
            "alt": 9000, "speed": 290, "heading": 10, "on_ground": False,
            "reg": "D-TEST", "type": "A320", "squawk": None},
           {"hex": "bbbbbb", "flight": "Y2", "lat": 50.2, "lon": 8.7,
            "alt": 20000, "speed": 350, "heading": 90, "on_ground": False,
            "reg": "D-ZWEI", "type": "A359", "squawk": None}]
    monkeypatch.setattr(bp, "_fetch_adsb_point_merged", lambda la, lo, r: list(mirror))
    monkeypatch.setattr(bp, "_area_from_aircraft_live", lambda la, lo, r: list(own))
    r = client.get("/api/adsb/area?lat=50.05&lon=8.60&radius=120&x=plausi")
    d = json.loads(r.data)
    assert r.status_code == 200
    flights = sorted(a["flight"] for a in d["aircraft"])
    assert flights == ["X1", "Y2"]  # Mirror gewinnt pro Identität, eigene füllt auf
    assert "aircraft_live" in d["source"]
