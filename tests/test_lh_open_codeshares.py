"""Codeshares + Wet-Lease aus der SCHON BEZAHLTEN flightstatus-Response
(2026-07-31, Welle 0.6 „Verworfen-Felder-Fix").

`_leg_to_facts` warf `MarketingCarrierList` und `OperatingCarrier` bisher weg.
Beide liegen in derselben Antwort, die wir ohnehin für Zeiten/Gate/Reg kaufen —
sie auszulesen kostet NULL zusätzliche LH-Calls.

Rein offline: kein Netz, kein Key. Die Fixture-Blöcke spiegeln die echte
LH-Open-API-Shape (verifiziert 2026-07-21 in tests/test_lh_open_api.py) und
ergänzen nur die beiden bisher ignorierten Zweige.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blueprints import lh_open_api as lh


# LH400 FRA→JFK mit Codeshares — MarketingCarrierList als LISTE, FlightNumber
# als int (so liefert LH es im Regelfall). LH sich selbst mit drin.
FS_LH400_CODESHARE = {"FlightStatusResource": {"Flights": {"Flight": {
    "Departure": {"AirportCode": "FRA",
                  "ScheduledTimeLocal": {"DateTime": "2026-07-31T10:55"},
                  "ScheduledTimeUTC": {"DateTime": "2026-07-31T08:55Z"},
                  "Terminal": {"Name": "1", "Gate": "Z16"}},
    "Arrival": {"AirportCode": "JFK",
                "ScheduledTimeLocal": {"DateTime": "2026-07-31T13:35"},
                "ScheduledTimeUTC": {"DateTime": "2026-07-31T17:35Z"}},
    "MarketingCarrierList": {"MarketingCarrier": [
        {"AirlineID": "LH", "FlightNumber": 400},
        {"AirlineID": "UA", "FlightNumber": 8840},
        {"AirlineID": "AC", "FlightNumber": 9092},
        {"AirlineID": "UA", "FlightNumber": "08840"},   # Dublette, andere Form
    ]},
    "OperatingCarrier": {"AirlineID": "LH", "FlightNumber": 400},
    "Equipment": {"AircraftCode": "748", "AircraftRegistration": "DABYA"},
    "FlightStatus": {"Code": "NA", "Definition": "No status"}}}}}

# Wet-Lease: LH1234 wird von Air Dolomiti (EN) geflogen. Marketing-Liste als
# EINZELOBJEKT (LH liefert Ein-Element-Listen so aus) — Skalar-Härtung.
LEG_WETLEASE = {
    "Departure": {"AirportCode": "MUC"},
    "Arrival": {"AirportCode": "FLR"},
    "MarketingCarrierList": {"MarketingCarrier":
                             {"AirlineID": "LH", "FlightNumber": 1234}},
    "OperatingCarrier": {"AirlineID": "EN", "FlightNumber": 2345},
}


def test_codeshares_dedup_and_drop_own_flight():
    facts = lh._leg_to_facts(
        FS_LH400_CODESHARE["FlightStatusResource"]["Flights"]["Flight"],
        "LH400")
    # Eigener Flug raus, Dublette (400 vs '08840') zusammengefasst,
    # LH-Reihenfolge erhalten.
    assert facts["codeshares"] == ["UA8840", "AC9092"]
    # Operating == Marketing → KEIN operated_by (sonst „LH400 fliegt LH400").
    assert "operated_by" not in facts
    # Die bestehenden Felder bleiben unverändert.
    assert facts["reg"] == "D-ABYA" and facts["gate"] == "Z16"


def test_operated_by_only_on_wet_lease():
    facts = lh._leg_to_facts(LEG_WETLEASE, "LH1234")
    assert facts["operated_by"] == "EN2345"
    # Der eigene Marketing-Flug ist KEIN Codeshare → Liste leer → Key weg.
    assert "codeshares" not in facts


def test_no_flight_no_means_no_operated_by():
    """Ohne bekannte Marketing-Nummer gibt es nichts zu vergleichen — dann
    lieber gar keine Aussage als eine geratene."""
    facts = lh._leg_to_facts(LEG_WETLEASE)
    assert "operated_by" not in facts


def test_missing_blocks_are_silent():
    """Fehlende/kaputte Carrier-Blöcke dürfen weder werfen noch leere Keys
    erzeugen (Regel „keine Fake-Werte": kein `codeshares: []`)."""
    for leg in ({"Departure": {}, "Arrival": {}},
                {"MarketingCarrierList": None, "OperatingCarrier": None},
                {"MarketingCarrierList": {"MarketingCarrier": "kaputt"},
                 "OperatingCarrier": []},
                {"MarketingCarrierList": {"MarketingCarrier": [
                    {"AirlineID": "", "FlightNumber": 1}, {}, None]}}):
        f = lh._leg_to_facts(leg, "LH400")
        assert "codeshares" not in f and "operated_by" not in f


def test_designator_normalisation_is_scalar_hard():
    assert lh._norm_designator("LH 0400") == "LH400"
    assert lh._norm_designator("lh400") == "LH400"
    assert lh._norm_designator("4Y0136") == "4Y136"
    assert lh._norm_designator("LH400D") == "LH400D"     # Operational-Suffix
    assert lh._norm_designator("") is None
    assert lh._norm_designator(None) is None
    assert lh._carrier_designator({"AirlineID": "UA", "FlightNumber": 8840}) == "UA8840"
    assert lh._carrier_designator({"AirlineID": "UA", "FlightNumber": "08840"}) == "UA8840"
    # Carrier ohne Nummer bleibt der reine Carrier (halbe Wahrheit statt keine).
    assert lh._carrier_designator({"AirlineID": "UA"}) == "UA"
    assert lh._carrier_designator({"FlightNumber": 1}) is None
    assert lh._carrier_designator(None) is None


def test_flight_facts_passes_the_queried_number_through(monkeypatch):
    """Ende-zu-Ende durch `lh_flight_facts`: die ABGEFRAGTE Nummer muss beim
    Leg-Parser ankommen, sonst stünde der eigene Flug in seiner eigenen
    Codeshare-Liste."""
    monkeypatch.setattr(lh, "_KEY", "k")
    monkeypatch.setattr(lh, "_SECRET", "s")
    monkeypatch.setattr(lh, "_get", lambda path, caller=None: FS_LH400_CODESHARE)
    lh._facts_memo.clear()
    f = lh.lh_flight_facts("LH 400", "2026-07-31", "FRA", "JFK", caller="unit")
    assert f["codeshares"] == ["UA8840", "AC9092"]
    lh._facts_memo.clear()


def test_merge_carries_the_new_keys_into_the_facts_shape():
    from blueprints.aerox_data_blueprint import _merge_lh_into_facts
    obs = {"sched_dep": "OBS", "dep_iata": "FRA"}
    lh_facts = {"codeshares": ["UA8840"], "operated_by": "EN2345"}
    out = _merge_lh_into_facts(obs, lh_facts)
    assert out["codeshares"] == ["UA8840"]
    assert out["operated_by"] == "EN2345"
    # ADDITIV: alles Bestehende unverändert.
    assert out["sched_dep"] == "OBS" and out["dep_iata"] == "FRA"
    # Ohne LH-Werte entsteht KEIN leerer Key.
    assert _merge_lh_into_facts(obs, {"gate": "A1"}).get("codeshares") is None


def test_enrich_exposes_codeshares_to_the_detail_aggregate(monkeypatch):
    """Die Fakten-Keys sind ADDITIV — sichtbar werden sie erst durch die
    Whitelist in `_enrich_flight_status_with_obs`, die `resolve-flight`/
    `resolve-callsign` und damit `/api/ax/flight-detail/<flight>` speist."""
    from blueprints import aerox_data_blueprint as adb
    monkeypatch.setattr(adb, '_flight_facts_from_obs',
                        lambda *a, **k: {'codeshares': ['UA8840', 'AC9092'],
                                         'operated_by': 'EN2345',
                                         'sched_dep': '2026-07-31T10:55:00+02:00'})
    out = adb._enrich_flight_status_with_obs(
        {'flight': 'LH400', 'dep_iata': 'FRA', 'arr_iata': 'JFK',
         'sched_dep': 'X', 'sched_arr': 'Y'},
        date='2026-07-31', allow_paid=False, nogrpc=True)
    assert out['codeshares'] == ['UA8840', 'AC9092']
    assert out['operated_by'] == 'EN2345'
    # NIE überschreiben, nur füllen (FR24-Wahrheit bleibt oben).
    assert out['sched_dep'] == 'X'


def test_enrich_creates_no_empty_keys(monkeypatch):
    from blueprints import aerox_data_blueprint as adb
    monkeypatch.setattr(adb, '_flight_facts_from_obs',
                        lambda *a, **k: {'gate': 'A1'})
    out = adb._enrich_flight_status_with_obs(
        {'flight': 'LH400', 'sched_dep': 'X', 'sched_arr': 'Y'},
        date='2026-07-31', allow_paid=False, nogrpc=True)
    assert 'codeshares' not in out and 'operated_by' not in out


def test_merge_stale_board_still_passes_codeshares():
    """Stale-Board-Zweig gibt PUR LH weiter — die neuen Keys dürfen dabei nicht
    verloren gehen (und die internen Stale-Marker weiter nicht durchrutschen)."""
    from blueprints.aerox_data_blueprint import _merge_lh_into_facts
    out = _merge_lh_into_facts(
        {"stale": True},
        {"codeshares": ["UA8840"], "operated_by": "EN2345",
         "facts_stale": True, "facts_age_s": 42})
    assert out == {"codeshares": ["UA8840"], "operated_by": "EN2345"}
