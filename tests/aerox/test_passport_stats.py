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
    out = {"flight": flight, "from": frm, "to": to,
           "dep_iso": dep_iso, "arr_iso": arr_iso}
    if arr_iso:
        out["est_arr_iso"] = arr_iso
        out["arr_measured"] = True
    return out


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
    # Flugbuch-Import default leer (deterministisch; einzelne Tests
    # überschreiben das gezielt).
    monkeypatch.setattr(A, "_logbook_import_load", lambda t: {})
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
    # SEIT P7 (Kevin, 2026-07-27): Hin und Rück sind EIN Bogen — FRA→JFK (2×)
    # + JFK→FRA (1×) = ein Eintrag mit n=3, Anzeige-Richtung = die häufigere.
    # Vorher verbrannten beide Richtungen je einen der 80 Karten-Slots und
    # seltene Fernstrecken fielen von der Weltkarte.
    assert routes[0]["from"] == "FRA" and routes[0]["to"] == "JFK"
    assert routes[0]["n"] == 3
    assert len(routes) == 3            # FRA↔JFK, FRA-BKK, MUC-LHR
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


def test_compute_includes_logbook_import(synth_days, monkeypatch):
    """Kevin 2026-07-25: importierte Karriere-Legs (ax_logbook_import) zählen
    in Passport/Statistik — Überlapp mit Roster-Legs zählt NICHT doppelt,
    block_min speist die Zeit-Summe ohne route-history-Lookup."""
    monkeypatch.setattr(A, "_logbook_import_load", lambda t: {"legs": [
        # Historischer Karriere-Leg (weit vor App-Nutzung).
        {"date": "2019-05-10", "flight": "LH500", "from": "FRA", "to": "GIG",
         "reg": "D-ABYT", "type": "B747", "block_min": 690},
        # Überlapp mit Roster-Leg 2026-07-01 LH400 FRA-JFK → Roster gewinnt.
        {"date": "2026-07-01", "flight": "LH400", "from": "FRA", "to": "JFK",
         "block_min": 999},
        # Kaputter Eintrag → still ignoriert.
        {"date": "kein-datum", "flight": "XX1", "from": "AAA", "to": "BBB"},
    ]})
    p = A._passport_stats_compute(TOKEN, "all")
    assert p["flights"] == 6                      # 5 Roster + 1 Import (Dedupe!)
    assert "2019" in p["years"]
    assert p["first_date"] == "2019-05-10"
    # Import-Blockzeit zählt; der Überlapp-Leg behält die Roster-510 (nicht 999).
    assert p["minutes_flown"] == 510 + 430 + 645 + 500 + 115 + 690


def test_compute_all_carryover_from_import_meta(synth_days, monkeypatch):
    """Christoph 2026-08: er sah bei sich 16.545 h (Client addierte den
    FCL.050-Übertrag aus get_logbook), Freunde sahen 6.020 h. Der Server
    liefert den Übertrag jetzt selbst — ADDITIV, minutes_flown bleibt die
    reine Leg-Summe (alte Clients addieren selbst, sonst Doppelzählung)."""
    monkeypatch.setattr(A, "_logbook_import_load", lambda t: {"meta": {
        "carryover_min": 631500, "carryover_ldg_day": 900,
        "carryover_ldg_night": 100}})
    p = A._passport_stats_compute(TOKEN, "all")
    assert p["carryover_min"] == 631500
    assert p["carryover_landings"] == 1000
    # NICHT eingerechnet — der Übertrag ist ein eigenes Feld.
    assert p["minutes_flown"] == 510 + 430 + 645 + 500 + 115


def test_compute_all_carryover_zero_without_import(synth_days):
    # Kein Import → Felder vorhanden, aber 0 (stabiler Client-Vertrag).
    p = A._passport_stats_compute(TOKEN, "all")
    assert p["carryover_min"] == 0
    assert p["carryover_landings"] == 0


def test_compute_carryover_nur_bei_range_all(synth_days, monkeypatch):
    """Jahres-/Monats-Payloads bleiben byte-identisch — der Übertrag ist eine
    Karriere-Summe und gehört zu keinem einzelnen Jahr."""
    monkeypatch.setattr(A, "_logbook_import_load",
                        lambda t: {"meta": {"carryover_min": 631500}})
    y = A._passport_stats_compute(TOKEN, "2026")
    assert "carryover_min" not in y and "carryover_landings" not in y


def test_compute_carryover_deckel_wie_get_logbook(synth_days, monkeypatch):
    # Nur 0 < c < 3.600.000 zählt (get_logbook-Deckel) — Müll wird 0.
    monkeypatch.setattr(A, "_logbook_import_load", lambda t: {"meta": {
        "carryover_min": 60000 * 60,          # == Deckel → raus
        "carryover_ldg_day": -5,               # negativ → raus
        "carryover_ldg_night": 100000}})       # == Deckel → raus
    p = A._passport_stats_compute(TOKEN, "all")
    assert p["carryover_min"] == 0
    assert p["carryover_landings"] == 0


def test_compute_ohne_import_unveraendert(synth_days):
    """Ohne Flugbuch-Import ändert der geteilte Merge NICHTS am Roster-Bild."""
    p_leer = A._passport_stats_compute(TOKEN, "all")
    assert p_leer["flights"] == 5
    assert p_leer["first_date"] == "2025-11-03"
    assert p_leer["last_date"] == "2026-07-01"
    assert p_leer["minutes_flown"] == 510 + 430 + 645 + 500 + 115


def test_import_ueberlebt_kaputten_tagessatz(synth_days, monkeypatch):
    """WURZELURSACHE (Owner-Mail 2026-07-28, „Passport zählt das Flugbuch nicht"):

    Ein Tagessatz mit `ical_sectors: null` (put_briefing speichert Client-JSON
    unverändert) ließ die alte Passport-Merge-Kopie beim ERSTEN Import-Leg
    dieses Tages in einen AttributeError laufen — gefangen von einem
    Sammel-`except` → der KOMPLETTE Import fiel still aus Passport/Statistik,
    der Zeitraum blieb beim Roster-Start stehen."""
    days = dict(synth_days)
    days["2026-07-05"] = {"ical_sectors": None}       # kaputter Tagessatz
    monkeypatch.setattr(A, "_manual_briefings_load", lambda t: days)
    monkeypatch.setattr(A, "_logbook_import_load", lambda t: {"legs": [
        {"date": "2026-07-05", "flight": "LH1", "from": "FRA", "to": "MUC",
         "block_min": 60},
        {"date": "2011-04-02", "flight": "LH500", "from": "FRA", "to": "GIG",
         "block_min": 690},
    ]})
    p = A._passport_stats_compute(TOKEN, "all")
    assert p["flights"] == 7                       # 5 Roster + BEIDE Import-Legs
    assert p["first_date"] == "2011-04-02"         # Zeitraum wächst ehrlich mit
    assert "2011" in p["years"]
    assert p["minutes_flown"] == 510 + 430 + 645 + 500 + 115 + 60 + 690


def test_import_in_icao_dedupt_gegen_roster_iata(synth_days, monkeypatch):
    """Flugbuch-Exporte loggen Plätze teils als ICAO. Das darf weder ein
    Duplikat des Roster-Legs erzeugen noch aus der Statistik fallen."""
    monkeypatch.setattr(A, "_logbook_import_load", lambda t: {"legs": [
        # identisch zum Roster-Leg 2026-07-01 LH400 FRA→JFK, nur ICAO
        {"date": "2026-07-01", "flight": "LH400", "from": "EDDF", "to": "KJFK",
         "block_min": 999},
        # historischer Leg, ebenfalls ICAO
        {"date": "2012-08-08", "flight": "LH510", "from": "EDDF", "to": "SBGR",
         "block_min": 700},
    ]})
    p = A._passport_stats_compute(TOKEN, "all")
    assert p["flights"] == 6                       # Duplikat zählt EINMAL
    assert p["minutes_flown"] == 510 + 430 + 645 + 500 + 115 + 700
    assert "GRU" in p["airports"] and "EDDF" not in p["airports"]
    assert p["first_date"] == "2012-08-08"


def test_passport_teilt_die_leg_quelle_mit_dem_flugbuch(synth_days, monkeypatch):
    """Passport und /api/user/logbook lesen DIESELBE gemergte Leg-Quelle —
    jedes Flugbuch-Leg ist im Passport gezählt (der Passport zählt zusätzlich
    Legs ohne Flugnummer, die im FCL.050-Buch nicht auftauchen)."""
    monkeypatch.setattr(A, "_logbook_import_load", lambda t: {"legs": [
        {"date": "2019-05-10", "flight": "LH500", "from": "FRA", "to": "GIG",
         "block_min": 690},
        {"date": "2018-03-01", "flight": "LH501", "from": "EDDF", "to": "KJFK",
         "block_min": 500},
    ]})
    monkeypatch.setattr(A, "_logbook_overlay_load", lambda t: {})
    monkeypatch.setattr(A, "_logbook_facts_load", lambda t: {})
    monkeypatch.setattr(A, "_logbook_enrich_async", lambda t, w: None)
    with A.app.test_request_context():
        rv = A.get_logbook(TOKEN)
    lb = rv.get_json() if hasattr(rv, "get_json") else rv[0].get_json()
    lb_keys = {e["key"] for e in lb["entries"]}
    pp_keys = {lg["key"] for lg in A._passport_legs(TOKEN)}
    assert lb_keys and lb_keys <= pp_keys
    p = A._passport_stats_compute(TOKEN, "all")
    # Passport zählt zusätzlich NUR das MUC-LHR-Leg ohne Flugnummer (fällt im
    # FCL.050-Buch durch require_flight). LH401 ohne arr_iso ist seit der
    # Altersregel (Owner 2026-08-05, Paula/Florian-Regression) im Flugbuch
    # enthalten: alle Fixture-Legs liegen jenseits des Beweis-Fensters, dort
    # IST der Roster die Historie — ein fehlendes arr_iso versteckt keinen
    # geflogenen Leg mehr.
    assert p["flights"] == len(pp_keys) == lb["totals"]["legs"] + 1


def test_compute_empty_state(monkeypatch):
    monkeypatch.setattr(A, "_manual_briefings_load", lambda t: {})
    monkeypatch.setattr(A, "_ical_briefings_load", lambda t: {})
    monkeypatch.setattr(A, "_logbook_import_load", lambda t: {})
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


def test_friend_route_carries_carryover(client, friend_setup, monkeypatch):
    """Genau das Ziel des Server-Felds: die Freundes-Sicht bekommt den
    FCL.050-Übertrag des FREUNDES mit (Christoph: 16.545 h vs. 6.020 h) —
    automatisch, weil owner- und friend-Route denselben Compute teilen."""
    monkeypatch.setattr(
        A, "_logbook_import_load",
        lambda t: {"meta": {"carryover_min": 631500}} if t == FRIEND else {})
    r = _friend_get(client, rng="all")
    assert r.status_code == 200
    body = r.get_json()
    assert body["carryover_min"] == 631500
    assert body["carryover_landings"] == 0


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


# ── P7 (Kevin 3b, 2026-07-27): Kappungs-Eventualitäten ─────────────────────

def _many_routes_days(n_pairs, with_bogus=False):
    """Ein Tag pro Richtungs-Paar: Hub FRA → n_pairs verschiedene Ziele
    (echte IATA-Codes aus der Referenz-DB, damit Koordinaten existieren)."""
    ap = A._airports_compact_lookup()
    dests = [c for c in sorted(ap.keys())
             if len(c) == 3 and c != 'FRA' and ap[c][0] and ap[c][1]]
    days = {}
    for i, dst in enumerate(dests[:n_pairs]):
        d = f'2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}'
        days.setdefault(d, {'ical_sectors': []})['ical_sectors'].append(
            _sector('LH1', 'FRA', dst, f'{d}T08:00:00+00:00',
                    f'{d}T10:00:00+00:00'))
    if with_bogus:
        # Route zu einem Code OHNE Koordinaten: darf keinen Slot verbrennen.
        days['2026-01-02'] = {'ical_sectors': [
            _sector('LH2', 'FRA', 'QQX', '2026-01-02T08:00:00+00:00',
                    '2026-01-02T09:00:00+00:00')] * 99}
    return days


def _patch_days(monkeypatch, days):
    monkeypatch.setattr(A, '_manual_briefings_load', lambda t: days)
    monkeypatch.setattr(A, '_ical_briefings_load', lambda t: {})
    monkeypatch.setattr(A, '_logbook_import_load', lambda t: {})
    monkeypatch.setattr(A, '_passport_route_duration_min',
                        lambda frm, to, budget: None)


def test_routes_genau_80_boegen_bleiben(monkeypatch):
    _patch_days(monkeypatch, _many_routes_days(80))
    p = A._passport_stats_compute(TOKEN, 'all')
    assert len(p['routes']) == 80


def test_routes_ueber_120_abdeckung_garantiert(monkeypatch):
    # Kevins Live-Fall: >120 Bögen — Top-120 nach Häufigkeit, danach bekommt
    # jeder noch fehlende Flughafen seinen Bogen (SYD-Garantie), Grenze 200.
    _patch_days(monkeypatch, _many_routes_days(140))
    p = A._passport_stats_compute(TOKEN, 'all')
    assert len(p['routes']) == 140            # alle 140 <= 200: alles sichtbar
    aps = {x['from'] for x in p['routes']} | {x['to'] for x in p['routes']}
    assert len(aps) == 141                    # JEDER Airport auf der Karte
    assert p['airports_count'] == 141         # Karte == Kennzahl

def test_route_ohne_koordinaten_verbrennt_keinen_slot(monkeypatch):
    # 80 echte Ziele + eine 99×-geflogene Phantom-Route (kein Koordinaten-
    # Eintrag): vorher fraß sie einen der 80 Slots UND fehlte trotzdem auf
    # der Karte; jetzt wird VOR der Kappung gefiltert → alle 80 echten bleiben.
    _patch_days(monkeypatch, _many_routes_days(80, with_bogus=True))
    p = A._passport_stats_compute(TOKEN, 'all')
    assert len(p['routes']) == 80
    assert all(r['to'] != 'QQX' for r in p['routes'])
