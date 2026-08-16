"""
/api/ax/transit — GEGENRICHTUNG „Feierabend" (2026-07-26).

Neuer optionaler Query-Param `departure=<ISO mit TZ>`: Flughafen → Zuhause nach
der Landung. Bekannt ist die ABFAHRTSZEIT (Dienstende), gesucht ist die FRÜHESTE
Verbindung, die NICHT VOR dieser Zeit losgeht.

Getestet wird die echte Endpoint-Kette gegen einen gemockten RMV-mgate-Provider
(keyless, Rhein-Main/FRA) — Muster wie
tests/aerox/test_rmv_mgate_and_unregister_binding.py: `requests.get`/`requests.post`
werden per monkeypatch ersetzt, kein Live-HTTP.

Regressions-Anker: `arrival` GEWINNT, wenn beide Params gesetzt sind → der
bestehende Smart-Pickup-Pfad (späteste pünktliche Ankunft) bleibt unverändert.

Run:
    AEROTAX_ALLOW_BOOT_WITHOUT_KEY=1 pytest tests/test_ax_transit_departure.py -v
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("AEROTAX_ALLOW_BOOT_WITHOUT_KEY", "1")

# ── Koordinaten (echt) ────────────────────────────────────────────
FRA_LAT, FRA_LON = 50.0379, 8.5622        # FRA Terminal-Zentrum (Vorfeld!)
HOME_LAT, HOME_LON = 50.1109, 8.6821      # Wohnung Frankfurt-Innenstadt
# _AIRPORT_RAIL_SNAP: FRA Terminal → „Frankfurt Flughafen Regionalbahnhof"
SNAP_LAT, SNAP_LON = 50.05130, 8.57175
SNAP_LABEL = "Frankfurt Flughafen Regionalbahnhof"
SNAP_X, SNAP_Y = 8571750, 50051300
HOME_X, HOME_Y = 8682100, 50110900


@pytest.fixture(scope="module")
def appmod():
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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


def _mgate(connections, loc_names):
    """Baut eine minimale, aber echt-geformte HAFAS-mgate-TripSearch-Antwort.
    `connections` = Liste von (walk_start, walk_end, train_arr, final_arr, prodX)
    als HHMMSS-Strings; Datum fix 2026-07-06."""
    return {
        "ver": "1.18", "err": "OK",
        "svcResL": [{"err": "OK", "res": {
            "common": {
                "locL": [{"name": n, "crd": {"x": x, "y": y}}
                         for (n, x, y) in loc_names],
                "prodL": [
                    {"name": "Fußweg"},
                    {"name": "S8", "nameS": "S8", "cls": 8},
                    {"name": "S9", "nameS": "S9", "cls": 8},
                ],
                "polyL": [],
            },
            "outConL": [{
                "date": "20260706",
                "secL": [
                    {"type": "WALK",
                     "dep": {"locX": 0, "dTimeS": w0},
                     "arr": {"locX": 1, "aTimeS": w1}},
                    {"type": "JNY",
                     "dep": {"locX": 1, "dTimeS": w1, "dPlatfS": "1"},
                     "arr": {"locX": 2, "aTimeS": ta},
                     "jny": {"prodX": px}},
                    {"type": "WALK",
                     "dep": {"locX": 2, "dTimeS": ta},
                     "arr": {"locX": 3, "aTimeS": fa}},
                ],
            } for (w0, w1, ta, fa, px) in connections],
        }}],
    }


# ── departure-Richtung: FRA Regionalbf → Hbf → Zuhause ────────────
_DEP_LOCS = [
    ("Frankfurt Flughafen Terminal 1", 8562200, 50037900),
    ("Frankfurt (Main) Flughafen Regionalbahnhof", SNAP_X, SNAP_Y),
    ("Frankfurt (Main) Hauptbahnhof", 8663767, 50107158),
    ("Zuhause", HOME_X, HOME_Y),
]
# Zielzeit der Tests = 16:00 lokal (= 14:00Z).
#   A  losgehen 15:40 → 20 min ZU FRÜH (unzulässig), käme aber am frühesten an
#   B  losgehen 16:05 → zulässig, Ankunft 16:50   ← ERWARTETE WAHL
#   C  losgehen 16:30 → zulässig, Ankunft 17:30
#   D  losgehen 17:00 → zulässig, Ankunft 18:05
_DEP_CONNS = [
    ("154000", "155000", "161000", "162000", 1),   # A
    ("160500", "161500", "164000", "165000", 1),   # B
    ("163000", "164000", "172000", "173000", 2),   # C
    ("170000", "171000", "180000", "180500", 1),   # D
]
FAKE_DEP = _mgate(_DEP_CONNS, _DEP_LOCS)

# ── arrival-Richtung (Regression): Zuhause → Hbf → FRA Regionalbf ─
_ARR_LOCS = [
    ("Zuhause", HOME_X, HOME_Y),
    ("Frankfurt (Main) Hauptbahnhof", 8663767, 50107158),
    ("Frankfurt (Main) Flughafen Regionalbahnhof", SNAP_X, SNAP_Y),
    ("Frankfurt Flughafen Terminal 1", 8562200, 50037900),
]
# Zielzeit 16:00 lokal: P 14:05, Q 15:05, R 16:00 (späteste pünktliche ← WAHL),
# S 16:35 (zu spät, fliegt raus).
_ARR_CONNS = [
    ("130000", "131000", "140000", "140500", 1),   # P
    ("140000", "141000", "150000", "150500", 1),   # Q
    ("145000", "150000", "155500", "160000", 2),   # R
    ("152000", "153000", "163000", "163500", 1),   # S
]
FAKE_ARR = _mgate(_ARR_CONNS, _ARR_LOCS)


def _patch(monkeypatch, payload, seen=None):
    """RMV-mgate mocken; jeder GET (HAPI-nearbystops etc.) ist ein Testfehler."""
    import requests as _req
    monkeypatch.delenv("RMV_ACCESS_ID", raising=False)

    def fake_post(url, json=None, timeout=None, headers=None, **kw):
        assert "rmv.de/auskunft/bin/jp/mgate.exe" in url, url
        if seen is not None:
            seen.append(json)
        return _FakeResp(payload)

    def fake_get(url, params=None, timeout=None, headers=None, **kw):
        raise AssertionError(f"unexpected GET provider call: {url}")

    monkeypatch.setattr(_req, "post", fake_post)
    monkeypatch.setattr(_req, "get", fake_get)


def _q(**kw):
    return "&".join(f"{k}={v}" for k, v in kw.items())


# ─────────────────────────────────────────────────────────────────
# 1) departure-Modus: nicht vor der Zielzeit los, früheste Ankunft
# ─────────────────────────────────────────────────────────────────
def test_departure_mode_picks_earliest_arrival_not_before_target(client, monkeypatch):
    bodies = []
    _patch(monkeypatch, FAKE_DEP, bodies)

    r = client.get("/api/ax/transit?" + _q(
        from_lat=FRA_LAT, from_lon=FRA_LON, to_lat=HOME_LAT, to_lon=HOME_LON,
        departure="2026-07-06T14:00:00Z", debug=1))
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is True and d["found"] is True
    assert d["source"] == "rmv_mgate"
    assert d["mode"] == "departure"
    assert d["departure_target"] == "2026-07-06T14:00:00Z"

    # Verbindung B: losgehen 16:05, ÖPNV 16:15, zuhause 16:50.
    assert d["leave_at"] == "2026-07-06T16:05:00+02:00"
    assert d["first_dep"] == "2026-07-06T16:15:00+02:00"
    assert d["last_arr"] == "2026-07-06T16:50:00+02:00"
    assert d["walk_to_stop_min"] == 10
    assert d["first_stop"] == "Frankfurt (Main) Flughafen Regionalbahnhof"

    # Provider-Anfrage: depart-after + Zeit-Anker aus `departure`
    assert len(bodies) == 1
    req = bodies[0]["svcReqL"][0]["req"]
    assert req["outFrwd"] is True
    assert req["outDate"] == "20260706" and req["outTime"] == "160000"
    # ORIGIN (Flughafen) gesnappt auf den Regionalbahnhof …
    assert req["depLocL"][0]["lid"] == f"A=2@O=Zuhause@X={SNAP_X}@Y={SNAP_Y}@"
    # … ZIEL (Wohnung) NIE gesnappt.
    assert req["arrLocL"][0]["lid"] == f"A=2@O=Flughafen@X={HOME_X}@Y={HOME_Y}@"
    assert d["debug"]["efa"]["origin_rail_snap"] == SNAP_LABEL
    assert "airport_rail_snap" not in d["debug"]["efa"]


# ─────────────────────────────────────────────────────────────────
# 2) zu frühe Verbindung wird NICHT gewählt (auch wenn sie früher ankommt)
# ─────────────────────────────────────────────────────────────────
def test_departure_mode_rejects_connection_leaving_before_target(client, monkeypatch):
    _patch(monkeypatch, FAKE_DEP)

    r = client.get("/api/ax/transit?" + _q(
        from_lat=FRA_LAT, from_lon=FRA_LON, to_lat=HOME_LAT, to_lon=HOME_LON,
        departure="2026-07-06T14:00:00Z"))
    d = r.get_json()
    assert d["found"] is True
    # A geht 15:40 los (20 min vor Zielzeit) und wäre 16:20 zuhause = früheste
    # Ankunft überhaupt — darf trotzdem NICHT gewählt werden.
    assert d["leave_at"] != "2026-07-06T15:40:00+02:00"
    assert d["last_arr"] != "2026-07-06T16:20:00+02:00"
    for alt in d.get("alternatives") or []:
        assert alt["leave_at"] != "2026-07-06T15:40:00+02:00"


# ─────────────────────────────────────────────────────────────────
# 3) REGRESSION arrival: späteste pünktliche Ankunft, alles wie bisher
# ─────────────────────────────────────────────────────────────────
def test_arrival_mode_unchanged_latest_on_time_arrival(client, monkeypatch):
    bodies = []
    _patch(monkeypatch, FAKE_ARR, bodies)

    r = client.get("/api/ax/transit?" + _q(
        from_lat=HOME_LAT, from_lon=HOME_LON, to_lat=FRA_LAT, to_lon=FRA_LON,
        arrival="2026-07-06T14:00:00Z"))
    d = r.get_json()
    assert d["found"] is True and d["mode"] == "arrival"
    assert d["arrival_target"] == "2026-07-06T14:00:00Z"
    assert d["departure_target"] is None
    # R: los 14:50, Ankunft 16:00 = genau die Zielzeit (späteste pünktliche).
    assert d["leave_at"] == "2026-07-06T14:50:00+02:00"
    assert d["last_arr"] == "2026-07-06T16:00:00+02:00"

    req = bodies[0]["svcReqL"][0]["req"]
    assert req["outFrwd"] is False                     # arrive-by
    assert req["outTime"] == "160000"
    assert req["depLocL"][0]["lid"] == f"A=2@O=Zuhause@X={HOME_X}@Y={HOME_Y}@"
    assert req["arrLocL"][0]["lid"] == f"A=2@O=Flughafen@X={SNAP_X}@Y={SNAP_Y}@"

    # alternatives: späteste Ankunft ZUERST (R 16:00 → Q 15:05 → P 14:05)
    alts = d["alternatives"]
    assert [a["last_arr"] for a in alts] == [
        "2026-07-06T16:00:00+02:00",
        "2026-07-06T15:05:00+02:00",
        "2026-07-06T14:05:00+02:00",
    ]


def test_arrival_mode_rejects_previous_day_as_false_on_time(client, monkeypatch):
    """Regression 2026-08-16: Sonntagabend darf nicht Montagmorgen gewinnen.

    Manche HAFAS-Antworten enthalten bei einer Folgetag-Suche nur die letzten
    Verbindungen des laufenden Tages. Ohne eine untere Arrive-by-Grenze ist
    jede davon formal pünktlich und die API empfiehlt eine Anreise 15 Stunden
    zu früh. Ein solcher Provider-Treffer muss als nicht gefunden gelten, damit
    der Client seine ehrliche MapKit-/Auto-Kaskade benutzt.
    """
    _patch(monkeypatch, FAKE_ARR)

    # Provider-Fixture: Montag, 06.07., letzte Ankunft 16:35 lokal.
    # Gesucht: Dienstag 08:55 lokal (= 06:55Z), also >14 Stunden später.
    r = client.get("/api/ax/transit?" + _q(
        from_lat=HOME_LAT, from_lon=HOME_LON, to_lat=FRA_LAT, to_lon=FRA_LON,
        arrival="2026-07-07T06:55:00Z"))
    assert r.status_code == 200
    d = r.get_json()
    assert d == {
        "ok": True,
        "found": False,
        "reason": "no_journey_near_arrival_target",
    }


# ─────────────────────────────────────────────────────────────────
# 4) beide Params → arrival gewinnt
# ─────────────────────────────────────────────────────────────────
def test_arrival_wins_when_both_params_given(client, monkeypatch):
    bodies = []
    _patch(monkeypatch, FAKE_ARR, bodies)

    r = client.get("/api/ax/transit?" + _q(
        from_lat=HOME_LAT, from_lon=HOME_LON, to_lat=FRA_LAT, to_lon=FRA_LON,
        arrival="2026-07-06T14:00:00Z", departure="2026-07-06T06:00:00Z"))
    d = r.get_json()
    assert d["mode"] == "arrival"
    assert d["arrival_target"] == "2026-07-06T14:00:00Z"
    assert d["departure_target"] == "2026-07-06T06:00:00Z"   # additiv, nur Echo
    # identisches Ergebnis wie ohne `departure`
    assert d["leave_at"] == "2026-07-06T14:50:00+02:00"
    assert d["last_arr"] == "2026-07-06T16:00:00+02:00"
    req = bodies[0]["svcReqL"][0]["req"]
    assert req["outFrwd"] is False and req["outTime"] == "160000"
    # Ziel-Snap greift (arrival), Origin-Snap NICHT
    assert req["arrLocL"][0]["lid"] == f"A=2@O=Flughafen@X={SNAP_X}@Y={SNAP_Y}@"


# ─────────────────────────────────────────────────────────────────
# 5) alternatives im departure-Modus: Ankunft AUFSTEIGEND
# ─────────────────────────────────────────────────────────────────
def test_departure_mode_alternatives_sorted_by_earliest_arrival(client, monkeypatch):
    _patch(monkeypatch, FAKE_DEP)

    r = client.get("/api/ax/transit?" + _q(
        from_lat=FRA_LAT, from_lon=FRA_LON, to_lat=HOME_LAT, to_lon=HOME_LON,
        departure="2026-07-06T14:00:00Z"))
    d = r.get_json()
    alts = d["alternatives"]
    # nur die zulässigen B/C/D, aufsteigend nach Ankunft; A (zu früh) fehlt.
    assert [a["last_arr"] for a in alts] == [
        "2026-07-06T16:50:00+02:00",
        "2026-07-06T17:30:00+02:00",
        "2026-07-06T18:05:00+02:00",
    ]
    assert [a["leave_at"] for a in alts] == [
        "2026-07-06T16:05:00+02:00",
        "2026-07-06T16:30:00+02:00",
        "2026-07-06T17:00:00+02:00",
    ]
    # die Default-Empfehlung steht vorne
    assert alts[0]["last_arr"] == d["last_arr"]


# ─────────────────────────────────────────────────────────────────
# 6) Randfall: KEINE Verbindung geht nach der Zielzeit → Fallback
# ─────────────────────────────────────────────────────────────────
def test_departure_mode_fallback_picks_latest_leave_time(client, monkeypatch):
    _patch(monkeypatch, FAKE_DEP)

    # 18:00Z = 20:00 lokal: alle vier Verbindungen (15:40–17:00) gehen VOR der
    # Zielzeit los (Provider hat depart-after ignoriert).
    r = client.get("/api/ax/transit?" + _q(
        from_lat=FRA_LAT, from_lon=FRA_LON, to_lat=HOME_LAT, to_lon=HOME_LON,
        departure="2026-07-06T18:00:00Z"))
    d = r.get_json()
    assert d["found"] is True and d["mode"] == "departure"
    # D: späteste Losgeh-Zeit = am wenigsten „zu früh".
    assert d["leave_at"] == "2026-07-06T17:00:00+02:00"
    assert d["last_arr"] == "2026-07-06T18:05:00+02:00"
    # keine zulässigen Kandidaten → keine Alternativen-Liste
    assert "alternatives" not in d
