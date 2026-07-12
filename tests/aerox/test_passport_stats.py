"""Crew-Passport (Feature A, 2026-07-12) — Unit-Tests mit synthetischen Sektoren.

Getestet wird die reine Aggregation (_passport_stats_compute) plus der
Endpoint-Vertrag (Bearer-Pflicht, Range-Validierung, 60-s-Memo) — alles
in-process (Flask test_client), KEIN Netz, KEIN Supabase: die beiden
Briefings-Loader werden gemonkeypatcht, der route-history-Fallback ebenso.

Run:
    AEROTAX_ALLOW_BOOT_WITHOUT_KEY=1 pytest tests/aerox/test_passport_stats.py -v
"""
from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("AEROTAX_ALLOW_BOOT_WITHOUT_KEY", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import app as A  # noqa: E402

TOKEN = "PASSPORT-TEST-TOKEN"


def _sector(flight, frm, to, dep_iso, arr_iso):
    return {"flight": flight, "from": frm, "to": to,
            "dep_iso": dep_iso, "arr_iso": arr_iso}


@pytest.fixture
def synth_days(monkeypatch):
    """Synthetischer Roster: 3 Tage, 5 Legs über 2 Jahre.

    2026-07-01: FRA→JFK (LH400, 8h30) + JFK→FRA (LH401, KEINE arr_iso)
    2026-06-15: FRA→BKK (LH772, 10h45)
    2025-11-03: FRA→JFK (LH400, 8h20) + MUC→LHR (fehlende Flugnummer)
    """
    days = {
        "2026-07-01": {"ical_sectors": [
            _sector("LH400", "FRA", "JFK",
                    "2026-07-01T10:00:00+00:00", "2026-07-01T18:30:00+00:00"),
            _sector("LH401", "JFK", "FRA",
                    "2026-07-01T22:00:00+00:00", ""),
        ]},
        "2026-06-15": {"ical_sectors": [
            _sector("LH772", "FRA", "BKK",
                    "2026-06-15T14:00:00+00:00", "2026-06-16T00:45:00+00:00"),
        ]},
        "2025-11-03": {"ical_sectors": [
            _sector("LH400", "FRA", "JFK",
                    "2025-11-03T10:10:00+00:00", "2025-11-03T18:30:00+00:00"),
            _sector(None, "MUC", "LHR",
                    "2025-11-03T06:00:00+00:00", "2025-11-03T07:55:00+00:00"),
        ]},
        # Tag ohne Sektoren (Layover) — darf nichts beitragen.
        "2026-07-02": {"ical_summary": "LAYOVER JFK"},
    }
    monkeypatch.setattr(A, "_manual_briefings_load", lambda t: days)
    monkeypatch.setattr(A, "_ical_briefings_load", lambda t: {})
    # route-history-Fallback deterministisch: JFK→FRA kennt 430 min.
    monkeypatch.setattr(
        A, "_passport_route_duration_min",
        lambda frm, to, budget: 430 if (frm, to) == ("JFK", "FRA") else None)
    return days


@pytest.fixture
def client():
    return A.app.test_client()


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


# ── Aggregation ────────────────────────────────────────────────────────────

def test_compute_all_counts_km_minutes(synth_days):
    p = A._passport_stats_compute(TOKEN, "all")
    assert p["ok"] is True and p["has_data"] is True
    assert p["flights"] == 5
    # Distanz = Summe der Großkreise (aus derselben Referenz-DB berechnet —
    # keine hartkodierten km, nur Konsistenz mit dem Haversine-Helfer).
    ap = A._airports_compact_lookup()
    exp = 0.0
    for frm, to in (("FRA", "JFK"), ("JFK", "FRA"), ("FRA", "BKK"),
                    ("FRA", "JFK"), ("MUC", "LHR")):
        ca, cb = ap[frm], ap[to]
        exp += A._haversine_km(ca[0], ca[1], cb[0], cb[1])
    assert p["distance_km"] == round(exp)
    # FRA-JFK dürfte >5500 km sein — Plausibilität der Referenz-Koordinaten.
    assert p["distance_km"] > 10000
    # Minuten: 510 (FRA-JFK) + 430 (Fallback JFK-FRA) + 645 (BKK) + 500 + 115
    assert p["minutes_flown"] == 510 + 430 + 645 + 500 + 115
    assert p["legs_without_duration"] == 0
    assert p["first_date"] == "2025-11-03"
    assert p["last_date"] == "2026-07-01"
    assert p["years"] == ["2026", "2025"]


def test_compute_sets_airports_airlines_countries(synth_days):
    p = A._passport_stats_compute(TOKEN, "all")
    assert p["airports"] == sorted({"FRA", "JFK", "BKK", "MUC", "LHR"})
    assert p["airports_count"] == 5
    # Airline nur aus echter Flugnummer (MUC→LHR ohne Nummer zählt nicht).
    assert p["airlines"] == ["LH"]
    # Länder aus der Referenz-DB: DE, US, TH, GB.
    assert set(p["countries"]) == {"DE", "US", "TH", "GB"}
    assert p["countries_count"] == 4


def test_compute_routes_dedup_and_order(synth_days):
    p = A._passport_stats_compute(TOKEN, "all")
    routes = p["routes"]
    # FRA→JFK 2x = häufigste zuerst; Hin/Rück sind getrennte Routen.
    assert routes[0]["from"] == "FRA" and routes[0]["to"] == "JFK"
    assert routes[0]["n"] == 2
    assert len(routes) == 4
    for r in routes:
        for k in ("lat1", "lon1", "lat2", "lon2"):
            assert isinstance(r[k], float)


def test_compute_range_year_and_month(synth_days):
    y26 = A._passport_stats_compute(TOKEN, "2026")
    assert y26["flights"] == 3
    assert set(y26["countries"]) == {"DE", "US", "TH"}
    # years bleibt UNABHÄNGIG vom Range vollständig (Client-Pills).
    assert y26["years"] == ["2026", "2025"]
    m = A._passport_stats_compute(TOKEN, "2026-07")
    assert m["flights"] == 2
    assert m["first_date"] == m["last_date"] == "2026-07-01"


def test_compute_missing_arr_without_fallback_drops_minutes(synth_days, monkeypatch):
    # Fallback liefert nichts → Leg fällt EHRLICH aus der Zeit-Summe,
    # bleibt aber in Flüge/Distanz/Sets.
    monkeypatch.setattr(A, "_passport_route_duration_min",
                        lambda frm, to, budget: None)
    p = A._passport_stats_compute(TOKEN, "all")
    assert p["flights"] == 5
    assert p["minutes_flown"] == 510 + 645 + 500 + 115
    assert p["legs_without_duration"] == 1


def test_compute_empty_state(monkeypatch):
    monkeypatch.setattr(A, "_manual_briefings_load", lambda t: {})
    monkeypatch.setattr(A, "_ical_briefings_load", lambda t: {})
    p = A._passport_stats_compute(TOKEN, "all")
    assert p["has_data"] is False
    assert p["flights"] == 0 and p["routes"] == [] and p["years"] == []


def test_compute_ical_fills_gaps(monkeypatch):
    """Merge-Semantik wie get_briefings: manual-Sektoren gewinnen, iCal füllt."""
    manual = {"2026-07-01": {"ical_sectors": [
        _sector("LH100", "FRA", "MUC",
                "2026-07-01T08:00:00+00:00", "2026-07-01T09:00:00+00:00")]}}
    ical = {
        "2026-07-01": {"ical_sectors": [
            _sector("XX999", "AAA", "BBB", "", "")]},   # verliert gegen manual
        "2026-07-03": {"ical_sectors": [
            _sector("LH101", "MUC", "FRA",
                    "2026-07-03T10:00:00+00:00", "2026-07-03T11:00:00+00:00")]},
    }
    monkeypatch.setattr(A, "_manual_briefings_load", lambda t: manual)
    monkeypatch.setattr(A, "_ical_briefings_load", lambda t: ical)
    p = A._passport_stats_compute(TOKEN, "all")
    assert p["flights"] == 2
    assert p["airports"] == ["FRA", "MUC"]


# ── Endpoint-Vertrag ───────────────────────────────────────────────────────

def test_route_requires_bearer(client, synth_days):
    r = client.get(f"/api/user/passport-stats/{TOKEN}")
    assert r.status_code == 401
    r = client.get(f"/api/user/passport-stats/{TOKEN}",
                   headers={"Authorization": "Bearer WRONG-TOKEN"})
    assert r.status_code == 401


def test_route_ok_with_bearer(client, synth_days):
    r = client.get(f"/api/user/passport-stats/{TOKEN}?range=2026",
                   headers=_auth())
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True and body["flights"] == 3
    assert body["range"] == "2026"


def test_route_bad_range(client, synth_days):
    r = client.get(f"/api/user/passport-stats/{TOKEN}?range=letzte-woche",
                   headers=_auth())
    assert r.status_code == 400


def test_route_memo_60s(client, synth_days, monkeypatch):
    r = client.get(f"/api/user/passport-stats/{TOKEN}?range=all", headers=_auth())
    assert r.status_code == 200 and r.get_json()["flights"] == 5
    # Loader tauschen → Memo muss trotzdem den ersten Stand liefern.
    monkeypatch.setattr(A, "_manual_briefings_load", lambda t: {})
    r2 = client.get(f"/api/user/passport-stats/{TOKEN}?range=all", headers=_auth())
    assert r2.get_json()["flights"] == 5
    # Cache leeren → frischer Compute sieht den leeren Roster.
    A._PASSPORT_STATS_CACHE.clear()
    r3 = client.get(f"/api/user/passport-stats/{TOKEN}?range=all", headers=_auth())
    assert r3.get_json()["flights"] == 0


def test_pii_prefix_registered():
    """Der Passport trägt die komplette Roster-Historie → GET-PII-Gate."""
    assert "/api/user/passport-stats/" in A._BUG004_GET_PII_PREFIXES


# ── Friend-Passport (P3, Owner 2026-07-12) ─────────────────────────────────

FRIEND = "PASSPORT-FRIEND-TOKEN"


@pytest.fixture
def friend_setup(monkeypatch):
    """Freundschafts-Kante TOKEN→FRIEND + Roster des FREUNDES (2 Legs CGN↔PMI).

    Der EIGENE Roster bleibt leer — so beweist der OK-Test, dass wirklich die
    Sektoren des FREUNDES aggregiert werden (flights==2, CGN/PMI), nicht die
    eigenen. share_roster ist default an ({} = nicht explizit False)."""
    friend_days = {
        "2026-05-10": {"ical_sectors": [
            _sector("EW910", "CGN", "PMI",
                    "2026-05-10T06:00:00+00:00", "2026-05-10T08:20:00+00:00"),
            _sector("EW911", "PMI", "CGN",
                    "2026-05-10T09:10:00+00:00", "2026-05-10T11:30:00+00:00"),
        ]},
    }
    monkeypatch.setattr(A, "_manual_briefings_load",
                        lambda t: friend_days if t == FRIEND else {})
    monkeypatch.setattr(A, "_ical_briefings_load", lambda t: {})
    monkeypatch.setattr(A, "_passport_route_duration_min",
                        lambda frm, to, budget: None)
    monkeypatch.setattr(A, "_friends_load",
                        lambda t: {"friends": [FRIEND]} if t == TOKEN
                        else {"friends": []})
    monkeypatch.setattr(A, "_profile_load", lambda t: {})
    A._PASSPORT_STATS_CACHE.clear()
    return friend_days


def _friend_get(client, friend=FRIEND, rng="all", headers=None):
    return client.get(f"/api/user/friend-passport/{TOKEN}",
                      query_string={"friend": friend, "range": rng},
                      headers=_auth() if headers is None else headers)


def test_friend_route_requires_bearer(client, friend_setup):
    r = _friend_get(client, headers={})
    assert r.status_code == 401
    r = _friend_get(client, headers={"Authorization": "Bearer WRONG-TOKEN"})
    assert r.status_code == 401


def test_friend_route_missing_friend_param(client, friend_setup):
    r = client.get(f"/api/user/friend-passport/{TOKEN}", headers=_auth())
    assert r.status_code == 400
    assert r.get_json()["error"] == "missing_friend"


def test_friend_route_bad_range(client, friend_setup):
    r = _friend_get(client, rng="letzte-woche")
    assert r.status_code == 400


def test_friend_route_not_friends(client, friend_setup):
    r = _friend_get(client, friend="TOTALLY-UNKNOWN-TOKEN")
    assert r.status_code == 403
    body = r.get_json()
    assert body["error"] == "not_friends" and body["shared"] is False


def test_friend_route_not_shared(client, friend_setup, monkeypatch):
    """share_roster EXPLIZIT False → 403 not_shared (Privacy-Pfad wie
    friends-today/Leaderboard: Opt-out-Profile geben nichts preis)."""
    monkeypatch.setattr(A, "_profile_load",
                        lambda t: {"share_roster": False} if t == FRIEND else {})
    r = _friend_get(client)
    assert r.status_code == 403
    body = r.get_json()
    assert body["error"] == "not_shared" and body["shared"] is False


def test_friend_route_ok_returns_friend_stats(client, friend_setup):
    r = _friend_get(client, rng="all")
    assert r.status_code == 200
    body = r.get_json()
    # Payload = 1:1 der passport-stats-Vertrag (iOS-PassportStats-Codable).
    assert body["ok"] is True and body["has_data"] is True
    assert body["flights"] == 2
    assert body["airports"] == ["CGN", "PMI"]
    assert body["airlines"] == ["EW"]
    assert body["range"] == "all"
    # Range-Filter greift auch für Freunde.
    r2 = _friend_get(client, rng="2025")
    assert r2.get_json()["flights"] == 0


def test_friend_route_resolves_shortened_token(client, friend_setup):
    """PII-gekürzte friends-today-Variante (full[:16] + '…') wird über die
    eigene Freundschafts-Kante auf den vollen Token aufgelöst."""
    r = _friend_get(client, friend=FRIEND[:16] + "…")
    assert r.status_code == 200
    assert r.get_json()["flights"] == 2


def test_friend_route_shares_memo_with_owner_route(client, friend_setup):
    """Cache-Key ist der FREUND-Token → Owner- und Friend-Read teilen sich den
    60-s-Memo-Eintrag (kein Doppel-Compute)."""
    assert _friend_get(client).status_code == 200
    assert (FRIEND, "all") in A._PASSPORT_STATS_CACHE


def test_friend_pii_prefix_registered():
    assert "/api/user/friend-passport/" in A._BUG004_GET_PII_PREFIXES
