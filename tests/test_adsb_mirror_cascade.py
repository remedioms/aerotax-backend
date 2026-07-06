"""Mirror-Kaskade fürs freie ADSBX-v2-Netz (Deep-Research 2026-07-06).

adsb.fi und airplanes.live sind live-verifizierte, schema-identische Gratis-
Mirrors von adsb.lol. Verhaltens-Contract von _adsb_v2_try_hosts:
  (a) Erster Host mit nicht-leerem `ac` gewinnt — spätere Hosts werden NICHT
      mehr angefragt (1-req/s-Etikette).
  (b) adsb.lol-Miss (leeres ac) → nächster Host wird versucht (Coverage der
      Community-Netze unterscheidet sich — das ist der ganze Sinn).
  (c) 429 setzt per-Host-Cooldown → Host wird bis zum Ablauf übersprungen.
  (d) Alle Hosts leer-aber-erreichbar → None (echtes „nirgends gesehen").
  (e) Alle Hosts down → _AdsbLolError (Quell-Ausfall, kein Miss).
"""
import json
import io
import urllib.error
from unittest.mock import patch

import pytest

import app  # noqa: F401 — Blueprint-Registrierung MUSS vor dem Direkt-Import laufen
import blueprints.adsb_blueprint as ADSB


def _resp(payload):
    class _R:
        def __init__(self, data): self._d = json.dumps(data).encode()
        def read(self): return self._d
        def __enter__(self): return self
        def __exit__(self, *a): return False
    return _R(payload)


AC = {"hex": "3c65aa", "flight": "DLH716  ", "r": "D-AIXP", "lat": 20.7,
      "lon": 88.0, "alt_baro": 39000, "gs": 480.0, "track": 95.0,
      "seen": 0.5, "seen_pos": 0.1}


@pytest.fixture(autouse=True)
def _clean_cooldowns():
    ADSB._ADSB_MIRROR_COOLDOWN.clear()
    yield
    ADSB._ADSB_MIRROR_COOLDOWN.clear()


def test_first_host_hit_stops_cascade():
    calls = []
    def fake_open(req, timeout=None):
        calls.append(req.full_url)
        return _resp({"ac": [AC]})
    with patch.object(ADSB.urllib.request, 'urlopen', side_effect=fake_open):
        row = ADSB._fetch_adsb_lol("3c65aa")
    assert row is not None
    assert len(calls) == 1
    assert 'adsb.lol' in calls[0]


def test_lol_miss_falls_through_to_adsb_fi():
    calls = []
    def fake_open(req, timeout=None):
        calls.append(req.full_url)
        if 'adsb.lol' in req.full_url:
            return _resp({"ac": [], "msg": "No error"})
        return _resp({"ac": [AC]})
    with patch.object(ADSB.urllib.request, 'urlopen', side_effect=fake_open):
        row = ADSB._fetch_adsb_lol("3c65aa")
    assert row is not None
    assert len(calls) == 2
    assert 'adsb.fi' in calls[1]
    # Normalisierung unverändert: Reg in [2], lat/lon in [6]/[5]
    assert row[2] == 'D-AIXP'
    assert abs(row[6] - 20.7) < 1e-9 and abs(row[5] - 88.0) < 1e-9


def test_429_sets_cooldown_and_skips_host():
    calls = []
    def fake_open(req, timeout=None):
        calls.append(req.full_url)
        if 'adsb.lol' in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 429, 'rate', {}, io.BytesIO(b''))
        return _resp({"ac": [AC]})
    with patch.object(ADSB.urllib.request, 'urlopen', side_effect=fake_open):
        row1 = ADSB._fetch_adsb_lol("3c65aa")
        row2 = ADSB._fetch_adsb_lol("3c65aa")
    assert row1 is not None and row2 is not None
    assert ADSB._ADSB_MIRROR_COOLDOWN.get('adsb.lol', 0) > 0
    # 2. Aufruf überspringt adsb.lol komplett (Cooldown aktiv)
    lol_calls = [c for c in calls if 'adsb.lol' in c]
    assert len(lol_calls) == 1


def test_all_empty_is_real_miss_not_error():
    with patch.object(ADSB.urllib.request, 'urlopen',
                      side_effect=lambda req, timeout=None: _resp({"ac": []})):
        assert ADSB._fetch_adsb_lol("3c65aa") is None
        assert ADSB._fetch_adsb_lol_point(50.0, 8.5, 25) == []


def test_all_down_raises_upstream_error():
    def fake_open(req, timeout=None):
        raise urllib.error.URLError('down')
    with patch.object(ADSB.urllib.request, 'urlopen', side_effect=fake_open):
        with pytest.raises(ADSB._AdsbLolError):
            ADSB._fetch_adsb_lol("3c65aa")


def test_point_uses_adsb_fi_path_schema():
    calls = []
    def fake_open(req, timeout=None):
        calls.append(req.full_url)
        if 'adsb.lol' in req.full_url:
            return _resp({"ac": []})
        return _resp({"ac": [AC]})
    with patch.object(ADSB.urllib.request, 'urlopen', side_effect=fake_open):
        rows = ADSB._fetch_adsb_lol_point(50.03, 8.57, 25)
    assert len(rows) == 1
    # adsb.fi hat das abweichende /lat/../lon/../dist/..-Schema
    assert '/lat/50.03/lon/8.57/dist/25' in calls[1]
