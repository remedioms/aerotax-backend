"""
Tests für die Ausland-Stufen von /api/ax/transit (2026-08-22):

  ÖBB-HAFAS-mgate (AT) · transport.opendata.ch (CH) · Google Routes (weltweit)

Kein Live-HTTP — requests.get/post werden gepatcht. Geprüft wird, was beim
Bauen teuer gelernt wurde:

  · Reihenfolge: die deutschen Quellen kommen ZUERST, auch wenn die Koordinate
    in der (rechteckigen) AT-Bbox liegt — sonst bekäme Passau eine ÖBB-Auskunft.
  · Google ist die LETZTE Stufe und schweigt ohne `GOOGLE_MAPS_API_KEY`
    komplett (Verhalten vor der Änderung: App fällt auf Apple-ETA zurück).
  · Google-Fußwege haben keine eigenen Zeiten und müssen aus den Transit-Zeiten
    rekonstruiert werden; aufeinanderfolgende WALK-Steps werden zu EINEM Leg.
  · Der Flughafen-Bahnhof-Snap greift auch für die Auslands-Flughäfen (ohne ihn
    liefert Google für manche Airports gar keine Route).

Run:
    AEROTAX_ALLOW_BOOT_WITHOUT_KEY=1 pytest tests/aerox/test_transit_intl_providers.py -v
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("AEROTAX_ALLOW_BOOT_WITHOUT_KEY", "1")


@pytest.fixture(scope="module")
def appmod():
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    import app as _app
    return _app


@pytest.fixture(scope="module")
def client(appmod):
    return appmod.app.test_client()


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


# ── ÖBB: echte mgate-Form (wie RMV, andere AID) ──────────────────────────────
_FAKE_OEBB = {
    "ver": "1.88", "err": "OK",
    "svcResL": [{"err": "OK", "res": {
        "common": {
            "locL": [
                {"name": "Zuhause", "crd": {"x": 16373800, "y": 48208200}},
                {"name": "Wien Mitte", "crd": {"x": 16385000, "y": 48206000}},
                {"name": "Flughafen Wien", "crd": {"x": 16564270, "y": 48120230}},
            ],
            "prodL": [{"name": "Fußweg"}, {"name": "S7", "nameS": "S7", "cls": 32}],
            "polyL": [],
        },
        "outConL": [{
            "date": "20260823",
            "secL": [
                {"type": "WALK",
                 "dep": {"locX": 0, "dTimeS": "091000"},
                 "arr": {"locX": 1, "aTimeS": "092000"}},
                {"type": "JNY",
                 "dep": {"locX": 1, "dTimeS": "092000", "dPlatfS": "2"},
                 "arr": {"locX": 2, "aTimeS": "094500"},
                 "jny": {"prodX": 1}},
            ],
        }],
    }}],
}

# ── Schweiz: transport.opendata.ch (Offset OHNE Doppelpunkt!) ────────────────
_FAKE_CH = {
    "connections": [{
        "sections": [
            {"journey": None, "walk": {"duration": 300},
             "departure": {"station": {"name": "Bellevue",
                                       "coordinate": {"x": 47.3669, "y": 8.5453}},
                           "departure": "2026-08-23T09:05:00+0200"},
             "arrival": {"station": {"name": "Stadelhofen"},
                         "arrival": "2026-08-23T09:10:00+0200"}},
            {"journey": {"category": "S", "number": "5", "name": "18455"},
             "departure": {"station": {"name": "Stadelhofen",
                                       "coordinate": {"x": 47.3663, "y": 8.5482}},
                           "departure": "2026-08-23T09:12:00+0200",
                           "delay": 2, "platform": "3"},
             "arrival": {"station": {"name": "Zürich Flughafen"},
                         "arrival": "2026-08-23T09:35:00+0200"}},
        ],
    }],
}

# ── Google Routes: zwei WALK-Steps am Stück + eine Fahrt ─────────────────────
_FAKE_GOOGLE = {
    "routes": [{
        "duration": "2400s",
        "legs": [{"steps": [
            {"travelMode": "WALK", "staticDuration": "180s",
             "startLocation": {"latLng": {"latitude": 41.9010, "longitude": 12.5015}}},
            {"travelMode": "WALK", "staticDuration": "120s",
             "startLocation": {"latLng": {"latitude": 41.9005, "longitude": 12.5010}}},
            {"travelMode": "TRANSIT",
             "startLocation": {"latLng": {"latitude": 41.9000, "longitude": 12.5000}},
             "transitDetails": {
                 "stopDetails": {
                     "departureTime": "2026-08-23T07:20:00Z",
                     "arrivalTime": "2026-08-23T07:52:00Z",
                     "departureStop": {"name": "Roma Termini"},
                     "arrivalStop": {"name": "Fiumicino Aeroporto"}},
                 "transitLine": {"nameShort": "RV", "name": "Regionale Veloce",
                                 "vehicle": {"type": "HEAVY_RAIL"}}}},
            {"travelMode": "WALK", "staticDuration": "240s",
             "startLocation": {"latLng": {"latitude": 41.7934, "longitude": 12.2518}}},
        ]}],
    }],
}


def test_austria_uses_oebb_and_normalizes_legs(client, monkeypatch):
    """Wien → VIE: ÖBB-mgate liefert die Kette in der normalisierten Form."""
    import requests as _req

    monkeypatch.delenv("RMV_ACCESS_ID", raising=False)
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    posts = []

    def fake_post(url, json=None, timeout=None, headers=None, **kw):
        posts.append({"url": url, "body": json})
        assert "fahrplan.oebb.at/gate" in url
        return _FakeResp(_FAKE_OEBB)

    monkeypatch.setattr(_req, "post", fake_post)
    monkeypatch.setattr(_req, "get",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("down")))

    r = client.get("/api/ax/transit?from_lat=48.2082&from_lon=16.3738"
                   "&to_lat=48.1103&to_lon=16.5697&arrival=2026-08-23T08:00:00Z&debug=1")
    d = r.get_json()
    assert r.status_code == 200 and d["found"] is True
    assert d["source"] == "oebb"
    legs = d["legs"]
    assert [l["mode"] for l in legs] == ["walk", "transit"]
    assert legs[1]["line"] == "S7" and legs[1]["platform"] == "2"
    # ÖBB-Auth muss die dokumentierte AID tragen, sonst antwortet der Kern nicht.
    assert posts[0]["body"]["auth"]["aid"] == "5vHavmuWPWIfetEe"
    # Ziel wurde auf den Bahnhof gesnappt (Vorfeld hat keine Haltestelle).
    assert d["debug"]["efa"]["airport_rail_snap"] == "Flughafen Wien"


def test_swiss_opendata_normalizes_and_fixes_offset(client, monkeypatch):
    """Zürich → ZRH: CH-Provider; Linie = category+number, Offset `+0200`
    wird zu `+02:00` normalisiert, `delay` ist MINUTEN (nicht Zeitpunkt)."""
    import requests as _req

    monkeypatch.delenv("RMV_ACCESS_ID", raising=False)
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    seen = {}

    def fake_get(url, params=None, timeout=None, headers=None, **kw):
        if "transport.opendata.ch" not in url:
            raise RuntimeError("down")
        seen.update(params or {})
        return _FakeResp(_FAKE_CH)

    monkeypatch.setattr(_req, "get", fake_get)
    monkeypatch.setattr(_req, "post",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("down")))

    r = client.get("/api/ax/transit?from_lat=47.3669&from_lon=8.5453"
                   "&to_lat=47.4581&to_lon=8.5555&arrival=2026-08-23T08:00:00Z")
    d = r.get_json()
    assert r.status_code == 200 and d["source"] == "ch_opendata"
    legs = d["legs"]
    assert legs[1]["line"] == "S5"
    assert legs[1]["delay_min"] == 2
    # Offset-Normalisierung: sonst scheitert der Zeitvergleich stumm.
    assert legs[1]["dep_planned"].endswith("+02:00")
    # arrive-by muss nativ angefragt werden, nicht nachträglich gefiltert.
    assert str(seen.get("isArrivalTime")) == "1"


def test_google_is_last_and_silent_without_key(client, monkeypatch):
    """Ohne GOOGLE_MAPS_API_KEY darf KEIN Google-Call passieren — das Verhalten
    vor der Änderung (App fällt auf die Apple-ETA zurück) bleibt exakt."""
    import requests as _req

    monkeypatch.delenv("RMV_ACCESS_ID", raising=False)
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    urls = []

    def fake_any(url, *a, **kw):
        urls.append(url)
        raise RuntimeError("down")

    monkeypatch.setattr(_req, "get", fake_any)
    monkeypatch.setattr(_req, "post", fake_any)

    r = client.get("/api/ax/transit?from_lat=41.9010&from_lon=12.5015"
                   "&to_lat=41.8003&to_lon=12.2389&arrival=2026-08-23T08:00:00Z")
    d = r.get_json()
    assert d["ok"] is True and d["found"] is False
    assert not any("googleapis" in u for u in urls)


def test_google_merges_walks_and_reconstructs_times(client, monkeypatch):
    """Rom → FCO über Google: zwei WALK-Steps am Stück werden EIN Fuß-Leg
    (180+120=300s), dessen Zeiten aus der Abfahrt der Fahrt zurückgerechnet
    sind — Google liefert für Fußwege keine absoluten Zeiten."""
    import requests as _req

    monkeypatch.delenv("RMV_ACCESS_ID", raising=False)
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    seen = {}

    def fake_post(url, json=None, timeout=None, headers=None, **kw):
        if "routes.googleapis.com" not in url:
            raise RuntimeError("down")
        seen["body"] = json
        seen["headers"] = headers or {}
        return _FakeResp(_FAKE_GOOGLE)

    monkeypatch.setattr(_req, "post", fake_post)
    monkeypatch.setattr(_req, "get",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("down")))

    r = client.get("/api/ax/transit?from_lat=41.9010&from_lon=12.5015"
                   "&to_lat=41.8003&to_lon=12.2389&arrival=2026-08-23T08:00:00Z&debug=1")
    d = r.get_json()
    assert r.status_code == 200 and d["source"] == "google"
    legs = d["legs"]
    # 4 Steps → 3 Legs: EIN Fußweg vorne, die Fahrt, EIN Fußweg hinten.
    assert [l["mode"] for l in legs] == ["walk", "transit", "walk"]
    assert legs[1]["line"] == "RV"
    # 07:20Z minus 300s zusammengefasster Fußweg = 07:15Z → das ist leave_at.
    assert legs[0]["dep"].startswith("2026-08-23T07:15:00")
    assert d["leave_at"].startswith("2026-08-23T07:15:00")
    # Fußweg NACH der Fahrt dockt an deren Ankunft an.
    assert legs[2]["dep"].startswith("2026-08-23T07:52:00")
    # Key gehört in den Header, nie in die URL (sonst steht er in fremden Logs).
    assert seen["headers"].get("X-Goog-Api-Key") == "test-key"
    assert "arrivalTime" in seen["body"] and seen["body"]["travelMode"] == "TRANSIT"
    # Ziel-Snap auf den Bahnhof — ohne ihn antwortet Google für manche
    # Flughäfen mit GAR KEINER Route.
    assert seen["body"]["destination"]["location"]["latLng"]["latitude"] == 41.79344


def _reset_google_budget(appmod):
    """Zähler + Cache sind modul-global → zwischen Tests zurücksetzen, sonst
    verbraucht ein Test das Kontingent des nächsten."""
    appmod._GOOGLE_TRANSIT_BUDGET["day"] = None
    appmod._GOOGLE_TRANSIT_BUDGET["n"] = 0
    for k in [k for k in appmod._AVIATION_CACHE if str(k).startswith("gtransit:")]:
        appmod._AVIATION_CACHE.pop(k, None)


def test_google_cache_prevents_second_paid_call(appmod, client, monkeypatch):
    """Zweimal dieselbe Frage = EIN bezahlter Aufruf. Dieselbe Crew fährt jeden
    Dienst denselben Weg — ohne Cache zahlt man denselben Weg mehrfach."""
    import requests as _req

    _reset_google_budget(appmod)
    monkeypatch.delenv("RMV_ACCESS_ID", raising=False)
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    calls = {"n": 0}

    def fake_post(url, json=None, timeout=None, headers=None, **kw):
        if "routes.googleapis.com" not in url:
            raise RuntimeError("down")
        calls["n"] += 1
        return _FakeResp(_FAKE_GOOGLE)

    monkeypatch.setattr(_req, "post", fake_post)
    monkeypatch.setattr(_req, "get",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("down")))

    q = ("/api/ax/transit?from_lat=41.9010&from_lon=12.5015"
         "&to_lat=41.8003&to_lon=12.2389&arrival=2026-08-23T08:00:00Z&debug=1")
    first = client.get(q).get_json()
    second = client.get(q).get_json()
    assert first["found"] is True and second["found"] is True
    assert first["debug"]["efa"]["google_cache"] == "miss"
    assert second["debug"]["efa"]["google_cache"] == "hit"
    assert calls["n"] == 1, "zweite identische Anfrage darf nichts kosten"


def test_google_daily_cap_falls_back_silently(appmod, client, monkeypatch):
    """Ist das Tageslimit erreicht, verhält sich der Endpoint wie VOR der
    Google-Stufe: ehrliches found=False, damit die App die Apple-ETA zeigt.
    Der teure Fall ist ein RMV-Ausfall, bei dem alle DE-Anfragen hierher kippen."""
    import requests as _req

    _reset_google_budget(appmod)
    monkeypatch.delenv("RMV_ACCESS_ID", raising=False)
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    monkeypatch.setenv("AEROX_GOOGLE_TRANSIT_DAILY_CAP", "1")
    calls = {"n": 0}

    def fake_post(url, json=None, timeout=None, headers=None, **kw):
        if "routes.googleapis.com" not in url:
            raise RuntimeError("down")
        calls["n"] += 1
        return _FakeResp(_FAKE_GOOGLE)

    monkeypatch.setattr(_req, "post", fake_post)
    monkeypatch.setattr(_req, "get",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("down")))

    base = ("/api/ax/transit?from_lat=41.9010&from_lon=12.5015"
            "&to_lat=41.8003&to_lon=12.2389&debug=1&arrival=2026-08-23T08:%02d:00Z")
    assert client.get(base % 0).get_json()["found"] is True
    blocked = client.get(base % 30).get_json()          # andere Zeit → kein Cache
    assert blocked["found"] is False
    assert blocked["debug"]["efa"]["google_cache"] == "daily_cap"
    assert calls["n"] == 1, "über dem Limit darf kein Aufruf mehr rausgehen"


def test_unparseable_times_are_dropped_not_shown(client, monkeypatch):
    """Ein unparsebarer Zeit-String eines Providers darf NICHT als `leave_at`
    durchgereicht werden — die App würde „kaputt" als Abfahrtszeit anzeigen.
    Ein Fehler darf nicht wie ein Wert aussehen (gefunden 2026-08-22)."""
    import copy

    import requests as _req

    monkeypatch.delenv("RMV_ACCESS_ID", raising=False)
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    broken = copy.deepcopy(_FAKE_CH)
    broken["connections"][0]["sections"][1]["departure"]["departure"] = "kaputt"

    monkeypatch.setattr(_req, "get",
                        lambda url, **kw: (_FakeResp(broken) if "opendata.ch" in url
                                           else (_ for _ in ()).throw(RuntimeError("down"))))
    monkeypatch.setattr(_req, "post",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("down")))

    d = client.get("/api/ax/transit?from_lat=47.3669&from_lon=8.5453"
                   "&to_lat=47.4581&to_lon=8.5555"
                   "&arrival=2026-08-23T08:00:00Z&debug=1").get_json()
    assert d["found"] is False and d.get("leave_at") is None
    prov = [p for p in d["debug"]["providers"] if p["name"] == "ch_opendata"][0]
    assert prov["dropped_bad_times"] == 1


def test_reversed_times_are_dropped(client, monkeypatch):
    """Ankunft VOR Abfahrt ist kaputt, egal welcher Provider das liefert."""
    import copy

    import requests as _req

    monkeypatch.delenv("RMV_ACCESS_ID", raising=False)
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    broken = copy.deepcopy(_FAKE_CH)
    broken["connections"][0]["sections"][1]["arrival"]["arrival"] = "2026-08-23T08:00:00+0200"

    monkeypatch.setattr(_req, "get",
                        lambda url, **kw: (_FakeResp(broken) if "opendata.ch" in url
                                           else (_ for _ in ()).throw(RuntimeError("down"))))
    monkeypatch.setattr(_req, "post",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("down")))

    d = client.get("/api/ax/transit?from_lat=47.3669&from_lon=8.5453"
                   "&to_lat=47.4581&to_lon=8.5555"
                   "&arrival=2026-08-23T08:00:00Z").get_json()
    assert d["found"] is False


def test_german_sources_win_over_austria_bbox(client, monkeypatch):
    """Passau liegt in der rechteckigen AT-Bbox, ist aber Deutschland: die
    deutsche Quelle muss ZUERST gefragt werden, sonst bekommt Südost-Bayern
    eine ÖBB-Auskunft statt der besseren deutschen."""
    import requests as _req

    monkeypatch.setenv("RMV_ACCESS_ID", "test-id")
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    order = []

    def fake_get(url, params=None, timeout=None, headers=None, **kw):
        order.append(url)
        raise RuntimeError("down")

    def fake_post(url, json=None, timeout=None, headers=None, **kw):
        order.append(url)
        raise RuntimeError("down")

    monkeypatch.setattr(_req, "get", fake_get)
    monkeypatch.setattr(_req, "post", fake_post)

    client.get("/api/ax/transit?from_lat=48.5667&from_lon=13.4319"
               "&to_lat=48.3538&to_lon=11.7861&arrival=2026-08-23T08:00:00Z")
    rmv_i = next(i for i, u in enumerate(order) if "rmv.de" in u)
    oebb_i = next(i for i, u in enumerate(order) if "oebb.at" in u)
    assert rmv_i < oebb_i, f"deutsche Quelle muss vor ÖBB kommen: {order}"
