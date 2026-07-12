"""Regressions-Sweep 2026-07-12 #1 — flown-track Schwellen-Angleich.

Fund: das arr-Anhängen unterblieb nur bei letztem echten Fix < 20 min
(_still_flying), in_flight galt aber bis < 30 min. Im 20–30-min-Fenster
(Crumb-Lücke bei fliegendem Flieger) enthielt DIESELBE Antwort einen
ts:null-Airport-Endpunkt UND in_flight=true — iOS setzte den ✈-Marker auf
den Airport-Punkt und der Radar-Joint zeichnete den Zickzack zurück zum
Live-Flieger (exakt das von Radar-Spur.md verbotene Muster, Fix 03fe16c).

Fix unter Test: das arr-Append-Gate nutzt jetzt EXAKT die in_flight-Formel
(frisch < 30 min, nicht grounded, > 8 km vorm Ziel) → arr-Append und
in_flight=True schließen sich gegenseitig aus.

KEIN echter Netz-/DB-Zugriff: _flown_track_db/_iata_latlon/Rate-Limiter
sind gepatcht.
"""
import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import time
from unittest.mock import patch

import pytest

import app as A
import blueprints.aerox_data_blueprint as BP
import blueprints.adsb_blueprint as ADSB

_LATLON = {'FRA': (50.033, 8.570), 'AMM': (31.7226, 35.9932)}


@pytest.fixture(autouse=True)
def _pin_app_module():
    import sys
    prev = sys.modules.get('app')
    sys.modules['app'] = A
    yield
    if prev is not None:
        sys.modules['app'] = prev


def _points(last_age_s, last_lat, alt=20000, gs=400, n=5):
    """n Crumbs Richtung FRA (konstante Länge 8.57), letzter Fix bei
    last_lat mit Alter last_age_s."""
    now = time.time()
    pts = []
    for i in range(n):
        frac = i / float(n - 1)
        pts.append({
            'lat': 49.0 + (last_lat - 49.0) * frac,
            'lon': 8.570,
            'alt': alt, 'gs': gs, 'trk': 0,
            'ts': now - last_age_s - (n - 1 - i) * 120,
        })
    return pts


def _call(points, reg):
    mock_db = lambda *a, **k: (list(points), reg, 'AMM', 'FRA', False)
    with patch.object(BP, '_flown_track_db', side_effect=mock_db), \
            patch.object(BP, '_iata_latlon',
                         side_effect=lambda c: _LATLON.get(c)), \
            patch.object(ADSB, '_rate_limited', return_value=False):
        with A.app.test_request_context(
                f'/api/ax/flown-track?reg={reg}&dep=AMM&arr=FRA'):
            resp = BP.ax_flown_track()
    if isinstance(resp, tuple):
        resp = resp[0]
    return resp.get_json()


def test_25min_alter_airborne_fix_kein_airport_append_und_in_flight():
    """DER Fund: letzter echter Fix 25 min alt (20–30-min-Fenster), Flieger
    50 km vor FRA in der Luft. Vor dem Fix: arr-Punkt (ts:null) angehängt
    UND in_flight=true in derselben Antwort."""
    body = _call(_points(last_age_s=25 * 60, last_lat=49.58), 'DXTRKA')
    assert body['ok'] is True
    assert body['in_flight'] is True, '25-min-Fix ist frisch (< 30 min)'
    assert body['points'][-1]['ts'] is not None, \
        'bei fliegendem Flieger darf KEIN ts:null-Airport-Punkt enden'


def test_gelandeter_flieger_verbindet_weiter_zum_airport():
    """Gegen-Regression: grounded-Fix (Taxi nach Landung, 5 km vor FRA-
    Zentrum) → Linie verbindet wie gehabt sauber zum Zielflughafen,
    in_flight bleibt False."""
    body = _call(_points(last_age_s=25 * 60, last_lat=49.988,
                         alt=0, gs=10), 'DXTRKB')
    assert body['in_flight'] is False
    assert body['points'][-1]['ts'] is None, \
        'gelandet → Airport-Endpunkt bleibt erhalten'


def test_stale_fix_appendet_und_ist_nicht_in_flight():
    """> 30 min alter Fix: beide Schwellen kippen GEMEINSAM — Append ja,
    in_flight nein (keine Widerspruchs-Antwort an der anderen Kante)."""
    body = _call(_points(last_age_s=40 * 60, last_lat=49.58), 'DXTRKC')
    assert body['in_flight'] is False
    assert body['points'][-1]['ts'] is None


def test_invariante_nie_beides():
    """Invariante aus Radar-Spur.md: in_flight=true und ts:null-Endpunkt
    schließen sich in JEDER Antwort aus."""
    for age_min, lat, alt, gs in ((5, 49.58, 20000, 400),
                                  (25, 49.58, 20000, 400),
                                  (25, 49.988, 0, 10),
                                  (40, 49.58, 20000, 400)):
        body = _call(_points(last_age_s=age_min * 60, last_lat=lat,
                             alt=alt, gs=gs),
                     f'DXTRK{age_min}{int(lat * 10) % 97}')
        assert not (body['in_flight']
                    and body['points'][-1]['ts'] is None), \
            f'Widerspruch bei age={age_min}min lat={lat}'
