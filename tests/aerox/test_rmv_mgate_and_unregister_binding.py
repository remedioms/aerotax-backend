"""
Tests für zwei Fixes (2026-07-05):

1. /api/ax/transit RMV-mgate-Provider (Rhein-Main/Frankfurt, keyless):
   gemockter HAFAS-mgate-Roundtrip → normalisierte Legs (isWalk/line/from/to/
   fromLat/fromLon/dep/arr/platform/delayMin/path) in exakt der MVV-EFA-Form.
   Kein Live-HTTP — requests.get/post werden gepatcht.

2. /api/push/unregister-apns Bearer-Binding: ohne passenden Bearer darf der
   Endpoint KEINEN fremden Token de-registrieren (Push-Abschalt-DoS).

Run:
    AEROTAX_ALLOW_BOOT_WITHOUT_KEY=1 pytest tests/aerox/test_rmv_mgate_and_unregister_binding.py -v
"""
from __future__ import annotations

import json
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


# ─────────────────────────────────────────────────────────────────
# 1) RMV-mgate Transit-Provider (gemockt)
# ─────────────────────────────────────────────────────────────────

# Encoded-Polyline "wsypH_k{s@~AlF" = 2 Punkte (50.10764,8.66496) →
# (50.10716,8.66377) am Frankfurter Hbf — echter Ausschnitt aus einer
# Live-RMV-Antwort (YX-Order, delta, Präzision 1e5).
_POLY_ENC = "wsypH_k{s@~AlF"

_FAKE_MGATE = {
    "ver": "1.18", "err": "OK",
    "svcResL": [{"err": "OK", "res": {
        "common": {
            "locL": [
                {"name": "Zuhause", "crd": {"x": 8682100, "y": 50110900}},
                {"name": "Frankfurt (Main) Hauptbahnhof", "crd": {"x": 8663767, "y": 50107158}},
                {"name": "Frankfurt (Main) Flughafen Regionalbahnhof", "crd": {"x": 8571750, "y": 50051300}},
                {"name": "Flughafen", "crd": {"x": 8562200, "y": 50037900}},
            ],
            "prodL": [
                {"name": "Fußweg"},
                {"name": "S8", "nameS": "S8", "cls": 8},
            ],
            "polyL": [{"crdEncYX": _POLY_ENC, "delta": True, "dim": 2}],
        },
        "outConL": [{
            "date": "20260706",
            "secL": [
                {"type": "WALK",
                 "dep": {"locX": 0, "dTimeS": "081400"},
                 "arr": {"locX": 1, "aTimeS": "083400"}},
                {"type": "JNY",
                 "dep": {"locX": 1, "dTimeS": "083400", "dTimeR": "083600", "dPlatfS": "21"},
                 "arr": {"locX": 2, "aTimeS": "084800", "aTimeR": "085000"},
                 "jny": {"prodX": 1, "polyG": {"polyXL": [0]}}},
                {"type": "WALK",
                 "dep": {"locX": 2, "dTimeS": "085000"},
                 "arr": {"locX": 3, "aTimeS": "085200"}},
            ],
        }],
    }}],
}


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_transit_frankfurt_uses_rmv_mgate_and_normalizes_legs(client, monkeypatch):
    """FRA-Koordinaten OHNE RMV_ACCESS_ID → keyless mgate liefert die Kette;
    Legs kommen in der normalisierten MVV-EFA-Form inkl. Gleis/Echtzeit/Path."""
    import requests as _req

    monkeypatch.delenv("RMV_ACCESS_ID", raising=False)
    calls = {"post": [], "get": []}

    def fake_post(url, json=None, timeout=None, headers=None, **kw):
        calls["post"].append({"url": url, "body": json})
        assert "rmv.de/auskunft/bin/jp/mgate.exe" in url
        return _FakeResp(_FAKE_MGATE)

    def fake_get(url, params=None, timeout=None, headers=None, **kw):
        calls["get"].append(url)
        raise AssertionError(f"unexpected GET provider call: {url}")

    monkeypatch.setattr(_req, "post", fake_post)
    monkeypatch.setattr(_req, "get", fake_get)

    # 07:00Z = 09:00 lokal → 08:52-Ankunft ist pünktlich.
    r = client.get("/api/ax/transit?from_lat=50.1109&from_lon=8.6821"
                   "&to_lat=50.0379&to_lon=8.5622&arrival=2026-07-06T07:00:00Z")
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is True and d["found"] is True
    assert d["source"] == "rmv_mgate"
    assert len(calls["post"]) == 1 and not calls["get"]

    # Request-Body: AID-Auth, arrive-by, Fern (Bits 1+2) NICHT in der Maske.
    body = calls["post"][0]["body"]
    assert body["auth"]["type"] == "AID" and body["auth"]["aid"]
    req = body["svcReqL"][0]["req"]
    assert req["outFrwd"] is False
    assert req["jnyFltrL"][0]["value"] == 2044
    # Ziel wurde auf den FRA-Regionalbahnhof gesnappt (nicht Vorfeld-Zentrum).
    assert "X=8571750@Y=50051300" in req["arrLocL"][0]["lid"]

    legs = d["legs"]
    assert [l["mode"] for l in legs] == ["walk", "transit", "walk"]
    walk1, s8, walk2 = legs

    # Fußweg: keine Linie, kein Gleis, aber Zeiten (für leave_at/walk_min).
    assert walk1["line"] is None and walk1["platform"] is None
    assert walk1["dep"] == "2026-07-06T08:14:00+02:00"
    assert walk1["from"] == "Zuhause"

    # Transit-Leg: Linie, Gleis, Echtzeit-Verspätung, Plan-Zeiten, Koordinaten.
    assert s8["line"] == "S8"
    assert s8["platform"] == "21"
    assert s8["fern"] is False
    assert s8["dep"] == "2026-07-06T08:36:00+02:00"          # Echtzeit
    assert s8["dep_planned"] == "2026-07-06T08:34:00+02:00"  # Plan
    assert s8["arr"] == "2026-07-06T08:50:00+02:00"
    assert s8["delay_min"] == 2
    assert abs(s8["from_lat"] - 50.107158) < 1e-6
    assert abs(s8["from_lon"] - 8.663767) < 1e-6
    # Polyline dekodiert (YX-Order, delta, 1e5-Präzision): beide Punkte exakt.
    assert s8["path"] == [[50.10764, 8.66496], [50.10716, 8.66377]]

    assert walk2["to"] == "Flughafen"
    assert d["leave_at"] == "2026-07-06T08:14:00+02:00"
    assert d["last_arr"] == "2026-07-06T08:52:00+02:00"
    assert d["first_stop"] == "Frankfurt (Main) Hauptbahnhof"


def test_transit_region_gate_munich_does_not_call_rmv_mgate(client, monkeypatch):
    """München liegt außerhalb der Rhein-Main-Bbox → kein mgate-POST.
    (MVV-EFA-GET schlägt hier absichtlich fehl → ehrliches found=False.)"""
    import requests as _req

    monkeypatch.delenv("RMV_ACCESS_ID", raising=False)
    posts = []

    def fake_post(url, json=None, timeout=None, headers=None, **kw):
        posts.append(url)
        return _FakeResp(_FAKE_MGATE)

    def fake_get(url, params=None, timeout=None, headers=None, **kw):
        raise RuntimeError("provider down (test)")

    monkeypatch.setattr(_req, "post", fake_post)
    monkeypatch.setattr(_req, "get", fake_get)

    r = client.get("/api/ax/transit?from_lat=48.137&from_lon=11.575"
                   "&to_lat=48.3538&to_lon=11.7861&arrival=2026-07-06T07:00:00Z")
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is True and d["found"] is False
    assert posts == [], "RMV mgate must not be called outside Rhein-Main bbox"


def test_transit_mgate_kernel_error_is_graceful(client, monkeypatch):
    """HAFAS-Kernel-Fehler (z.B. H9220) → Provider-Chain geht weiter statt 500;
    ohne weitere Provider ehrliches found=False (App fällt auf Apple zurück)."""
    import requests as _req

    monkeypatch.delenv("RMV_ACCESS_ID", raising=False)
    err_payload = {"ver": "1.18", "err": "OK",
                   "svcResL": [{"err": "H9220", "errTxt": "no station nearby"}]}
    monkeypatch.setattr(_req, "post",
                        lambda *a, **kw: _FakeResp(err_payload))
    monkeypatch.setattr(_req, "get",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("down")))

    r = client.get("/api/ax/transit?from_lat=50.1109&from_lon=8.6821"
                   "&to_lat=50.0379&to_lon=8.5622&arrival=2026-07-06T07:00:00Z&debug=1")
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is True and d["found"] is False
    provs = {p["name"]: p for p in d["debug"]["providers"]}
    assert "rmv_mgate" in provs and "H9220" in provs["rmv_mgate"]["err"]


# ─────────────────────────────────────────────────────────────────
# 2) unregister-apns Bearer-Binding
# ─────────────────────────────────────────────────────────────────

@pytest.fixture
def push_store(appmod, monkeypatch):
    """In-Memory-Ersatz für _push_load/_push_save (kein Disk/SB-Zugriff)."""
    store = {"AT-VICTIM-TOKEN-1234": {
        "token": "AT-VICTIM-TOKEN-1234",
        "apns_token": "deadbeef", "push_token": "ExponentPushToken[x]",
    }}
    saves = []

    def fake_load(tok):
        return store.get(tok)

    def fake_save(tok, data):
        saves.append((tok, data))
        store[tok] = data
        return True

    monkeypatch.setattr(appmod, "_push_load", fake_load)
    monkeypatch.setattr(appmod, "_push_save", fake_save)
    return {"store": store, "saves": saves}


def test_unregister_apns_without_bearer_is_rejected(client, push_store):
    r = client.post("/api/push/unregister-apns",
                    json={"token": "AT-VICTIM-TOKEN-1234"})
    assert r.status_code == 401
    assert r.get_json()["error"] == "token_binding_required"
    assert push_store["saves"] == [], "registry must remain untouched"
    assert push_store["store"]["AT-VICTIM-TOKEN-1234"]["apns_token"] == "deadbeef"


def test_unregister_apns_with_foreign_bearer_is_rejected(client, push_store):
    r = client.post("/api/push/unregister-apns",
                    json={"token": "AT-VICTIM-TOKEN-1234"},
                    headers={"Authorization": "Bearer AT-ATTACKER-TOKEN-9999"})
    assert r.status_code == 401
    assert r.get_json()["error"] == "token_binding_required"
    assert push_store["saves"] == []
    assert push_store["store"]["AT-VICTIM-TOKEN-1234"]["apns_token"] == "deadbeef"


def test_unregister_apns_with_matching_bearer_clears_tokens(client, push_store):
    r = client.post("/api/push/unregister-apns",
                    json={"token": "AT-VICTIM-TOKEN-1234"},
                    headers={"Authorization": "Bearer AT-VICTIM-TOKEN-1234"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    assert len(push_store["saves"]) == 1
    saved = push_store["saves"][0][1]
    assert saved["apns_token"] == "" and saved["push_token"] == ""
    assert saved.get("unregistered_at")


def test_unregister_apns_unknown_token_with_bearer_is_noop(client, push_store):
    """Match ohne Registry-Eintrag bleibt ein ehrlicher noop (kein 500)."""
    r = client.post("/api/push/unregister-apns",
                    json={"token": "AT-NOBODY-HOME-0000"},
                    headers={"Authorization": "Bearer AT-NOBODY-HOME-0000"})
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is True and d.get("noop") is True
    assert push_store["saves"] == []
