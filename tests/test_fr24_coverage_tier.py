"""FR24-Grauzonen-Tier: schließt das China/Russland/Ozean-Coverage-Loch GRATIS,
VOR dem bezahlten AeroDataBox-Tier.

Contract:
  (a) _fr24_row_to_opensky bildet die FR24-feed.js-Zeile korrekt aufs
      OpenSky-State-Row-Layout ab (reg in [2], lon [5]/lat [6], Einheiten).
  (b) In der Kaskade: freie Quellen (OpenSky+adsb.lol) leer, FR24 hat den
      Flieger → source='fr24', der BEZAHLTE Tier wird NICHT angefasst.
  (c) FR24 leer + targeted → erst dann der bezahlte Tier.
  (d) Round-Robin-Refresh ist rate-begrenzt (kein zweiter Fetch < REFRESH_MIN)
      und räumt Einträge > TTL.
"""
import time
from unittest.mock import patch, MagicMock

import pytest

import app  # noqa: F401 — Blueprint-Registrierung vor Direkt-Import
import blueprints.adsb_blueprint as ADSB


# Echte FR24-feed.js-Zeile (CLX über China, aus Live-Probe 2026-07-06)
FR24_ROW = ["4D0113", 30.37, 104.74, 174, 41100, 516, "", "F-BDWY1",
            "B748", "LX-VCJ", 1783329609, "NQZ", "HKG", "CV4327", 0, -64,
            "CLX4327", 0, "CLX"]


@pytest.fixture(autouse=True)
def _clean_fr24():
    ADSB._FR24["entries"].clear()
    ADSB._FR24["by_cs"].clear()
    ADSB._FR24["last_at"] = 0.0
    ADSB._FR24["cooldown_until"] = 0.0
    ADSB._FR24["tile_idx"] = 0
    yield
    ADSB._FR24["entries"].clear()
    ADSB._FR24["by_cs"].clear()


def test_row_normalization_units_and_reg():
    r = ADSB._fr24_row_to_opensky(FR24_ROW)
    assert r[0] == "4d0113"                       # hex lowercase
    assert r[1] == "CLX4327"                      # callsign
    assert r[2] == "LX-VCJ"                        # reg in [2] (fr24-whitelist)
    assert abs(r[5] - 104.74) < 1e-6              # lon
    assert abs(r[6] - 30.37) < 1e-6              # lat
    assert abs(r[7] - 41100 * 0.3048) < 0.5      # alt ft→m
    assert abs(r[9] - 516 * 0.514444) < 0.5      # gs kt→m/s
    assert r[10] == 174                           # track
    assert r[8] is False                          # on_ground


def test_row_without_position_is_none():
    bad = list(FR24_ROW); bad[1] = 0; bad[2] = 0
    assert ADSB._fr24_row_to_opensky(bad) is None


def _prime_fr24_with(row_hex="4d0113", cs="CLX4327"):
    r = ADSB._fr24_row_to_opensky(FR24_ROW)
    ADSB._FR24["entries"][row_hex] = (r, time.time())
    if cs:
        ADSB._FR24["by_cs"][cs] = row_hex
    ADSB._FR24["last_at"] = time.time()   # verhindert echten Netz-Refresh im Lookup


def test_cascade_fr24_fills_gap_before_paid(monkeypatch):
    # Freie Quellen leer (Coverage-Loch)
    monkeypatch.setattr(ADSB, "_fetch_opensky", MagicMock(return_value=None))
    monkeypatch.setattr(ADSB, "_fetch_adsb_lol", MagicMock(return_value=None))
    paid = MagicMock(return_value=(None, None, "should_not_be_called"))
    monkeypatch.setattr(ADSB, "_adb_position_attempt", paid)
    _prime_fr24_with()
    # Backoff aus, damit OpenSky-Zweig läuft
    ADSB._BACKOFF["until"] = 0
    row, source, obs_ts, tried = ADSB._live_position_cascade(
        "4d0113", targeted=True, callsign="CLX4327")
    assert source == "fr24"
    assert row is not None and row[2] == "LX-VCJ"
    paid.assert_not_called()               # FR24 gratis → bezahlt NICHT angefasst


def test_cascade_falls_to_paid_when_fr24_empty(monkeypatch):
    monkeypatch.setattr(ADSB, "_fetch_opensky", MagicMock(return_value=None))
    monkeypatch.setattr(ADSB, "_fetch_adsb_lol", MagicMock(return_value=None))
    monkeypatch.setattr(ADSB, "_fetch_fr24", MagicMock(return_value=None))
    adb_row = ADSB._fr24_row_to_opensky(FR24_ROW)
    paid = MagicMock(return_value=(adb_row, 1783329609, None))
    monkeypatch.setattr(ADSB, "_adb_position_attempt", paid)
    ADSB._BACKOFF["until"] = 0
    row, source, obs_ts, tried = ADSB._live_position_cascade("4d0113", targeted=True)
    assert source == "adb"
    paid.assert_called_once()


def test_cascade_no_paid_when_not_targeted(monkeypatch):
    monkeypatch.setattr(ADSB, "_fetch_opensky", MagicMock(return_value=None))
    monkeypatch.setattr(ADSB, "_fetch_adsb_lol", MagicMock(return_value=None))
    monkeypatch.setattr(ADSB, "_fetch_fr24", MagicMock(return_value=None))
    paid = MagicMock()
    monkeypatch.setattr(ADSB, "_adb_position_attempt", paid)
    ADSB._BACKOFF["until"] = 0
    row, source, obs_ts, tried = ADSB._live_position_cascade("4d0113", targeted=False)
    assert row is None
    paid.assert_not_called()


def test_refresh_is_rate_limited(monkeypatch):
    fetch = MagicMock(return_value=[ADSB._fr24_row_to_opensky(FR24_ROW)])
    monkeypatch.setattr(ADSB, "_fr24_fetch_tile", fetch)
    ADSB._FR24["last_at"] = 0.0
    ADSB._fr24_refresh_one_tile()          # 1. Fetch läuft
    ADSB._fr24_refresh_one_tile()          # 2. sofort danach → rate-limited, kein Fetch
    assert fetch.call_count == 1


def test_warm_from_distributed_store(monkeypatch):
    # Harvester-Flotte hat fr24_live gefüllt → Backend liest warm, harvestet
    # NICHT selbst und findet den Flieger.
    r = ADSB._fr24_row_to_opensky(FR24_ROW)
    fake_sb = MagicMock()
    (fake_sb.table.return_value.select.return_value.gt.return_value
     .limit.return_value.execute.return_value) = MagicMock(
        data=[{"hex": "4d0113", "callsign": "CLX4327", "row": r}])
    monkeypatch.setattr(ADSB, "_sb_client", lambda: (fake_sb, True))
    self_harvest = MagicMock()
    monkeypatch.setattr(ADSB, "_fr24_refresh_one_tile", self_harvest)
    ADSB._FR24["store_at"] = 0.0
    row = ADSB._fetch_fr24("4d0113")
    assert row is not None and row[2] == "LX-VCJ"
    self_harvest.assert_not_called()       # Store frisch → kein Selbst-Fetch


def test_self_harvest_when_store_cold(monkeypatch):
    # Kein Supabase (keine VMs deployed) → Backend springt selbst ein.
    monkeypatch.setattr(ADSB, "_sb_client", lambda: (None, False))
    self_harvest = MagicMock()
    monkeypatch.setattr(ADSB, "_fr24_refresh_one_tile", self_harvest)
    ADSB._FR24["store_at"] = 0.0
    ADSB._FR24["store_fresh_at"] = 0.0
    ADSB._fetch_fr24("4d0113")
    self_harvest.assert_called_once()


def test_entry_ttl_eviction(monkeypatch):
    fetch = MagicMock(return_value=[ADSB._fr24_row_to_opensky(FR24_ROW)])
    monkeypatch.setattr(ADSB, "_fr24_fetch_tile", fetch)
    # Ein uralter Fremd-Eintrag muss beim nächsten Refresh rausfliegen
    ADSB._FR24["entries"]["deadbe"] = (ADSB._fr24_row_to_opensky(FR24_ROW),
                                       time.time() - ADSB.FR24_ENTRY_TTL - 10)
    ADSB._FR24["last_at"] = 0.0
    ADSB._fr24_refresh_one_tile()
    assert "deadbe" not in ADSB._FR24["entries"]
    assert "4d0113" in ADSB._FR24["entries"]
