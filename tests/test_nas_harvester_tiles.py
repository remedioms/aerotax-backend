"""Regressions-Sweep 2026-07-12 #13 — Kachel-Defaults vs. Doku-Pin.

Der Fund war Doku-Drift: Split-Kommentar/Commit-Message beschrieben Kacheln
(FRA/MUC …), die es im REPO-Default gar nicht gibt — die echte Belegung
lebt out-of-band in der NAS-Compose-Env (AIRPORT_TILES=10 Kacheln,
AIRPORT_FAST_N=3, AIRPORT_SLOW_EVERY=4; Übersee-Hubs im 240-s-Slow-Bucket
sind BEABSICHTIGT, Owner-Entscheidung 2026-07-12 abends). Der Fix ist
reiner Kommentar/Doku-Angleich (nas_harvester/ingest.py) — dieser Test
PINNT die Repo-Defaults, damit eine künftige Default-Änderung den
dokumentierten Zustand nicht wieder still auseinanderlaufen lässt.
"""
import nas_harvester.ingest as I


def test_repo_default_airport_tiles_sind_nur_die_4_uebersee_hubs():
    # JFK / GRU / BKK / ICN — Europa kommt in Prod NUR via Env dazu
    # (s. Kommentar über _DEFAULT_AIRPORT_TILES + in _poll_airport_tiles).
    assert len(I._DEFAULT_AIRPORT_TILES) == 4
    # Grobe Geo-Pins (n, s, w, e): New York, São Paulo, Bangkok, Seoul.
    lats = [t[0] for t in I._DEFAULT_AIRPORT_TILES]
    assert 40 < lats[0] < 42      # JFK
    assert -24 < lats[1] < -22    # GRU
    assert 13 < lats[2] < 15      # BKK
    assert 37 < lats[3] < 39      # ICN


def test_parse_airport_tiles_leer_faellt_auf_default():
    assert I._parse_airport_tiles('') == list(I._DEFAULT_AIRPORT_TILES)
    assert I._parse_airport_tiles(None) == list(I._DEFAULT_AIRPORT_TILES)


def test_parse_airport_tiles_env_ueberschreibt():
    got = I._parse_airport_tiles('50.2 49.8 8.3 8.8; 48.5 48.2 11.5 12.0')
    assert got == [(50.2, 49.8, 8.3, 8.8), (48.5, 48.2, 11.5, 12.0)]
