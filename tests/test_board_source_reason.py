"""Tafel-Fehlervertrag: WARUM eine Tafel leer bleibt (Owner 2026-07-28,
„LAX meldet source_unavailable, SFO liefert ein volles Board").

Der User-Pfad der Tafel ist FREE-FIRST (`allow_paid=False`) — AeroDataBox wird
dort strukturell nie gefragt. Für Flughäfen OHNE freie Quelle (LAX/ORD/MIA/IAH/
SEA) existiert also gar keine Quelle, die „gerade nicht erreichbar" sein könnte.
Die Antwort muss das ehrlich unterscheiden, sonst suggeriert sie einen
transienten Ausfall und der Client retryt endlos.

Offline: die Quellen-Helfer werden gemonkeypatcht, kein Netz, kein Supabase.
"""
import os
import sys

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as backend  # noqa: E402

TOKEN = 'AT-TEST-BOARD-REASON'


def _board(monkeypatch, iata, ftype='departure'):
    """Tafel-Abfrage mit ALLEN Quellen tot → der Fehler-Zweig ist erreichbar."""
    monkeypatch.setattr(backend, '_native_board_cached',
                        lambda ap, ft, allow_paid=True: (None, None))
    monkeypatch.setattr(backend, '_board_rows_from_obs_for_date',
                        lambda *a, **k: [])
    monkeypatch.setattr(backend, '_fetch_opensky_board', lambda *a, **k: None)
    backend._BOARD_LAST_GOOD.clear()
    with backend.app.test_request_context(
            f'/api/airport/{TOKEN}/board?airport={iata}&type={ftype}'):
        rv = backend.airport_board(TOKEN)
    body = rv[0] if isinstance(rv, tuple) else rv
    return body.get_json()


def test_lax_meldet_keine_freie_quelle(monkeypatch):
    j = _board(monkeypatch, 'LAX')
    assert j['ok'] is False and j['error'] == 'source_unavailable'
    assert j['reason'] == 'no_free_source'
    # Kein Verweis mehr auf den AeroDataBox-Key: der User-Pfad zahlt nie.
    assert 'AeroDataBox' not in j['message']


def test_sfo_ist_abgedeckt_und_meldet_ausfall(monkeypatch):
    """SFO liefert der NAS-Scraper (→ airport_delay_obs). Liefert er mal nichts,
    ist das ein AUSFALL, kein Abdeckungsloch — erneut versuchen lohnt."""
    j = _board(monkeypatch, 'SFO')
    assert j['reason'] == 'source_down'


def test_nativer_scraper_meldet_ausfall(monkeypatch):
    j = _board(monkeypatch, 'MUC')
    assert j['reason'] == 'source_down'


def test_us_ziele_ohne_freie_quelle_sind_ehrlich(monkeypatch):
    """Die LH-US-Ziele ohne freie Tafel-Quelle — Abdeckungs-Inventar als Test
    festgehalten, damit ein späterer Scraper-Ausbau hier auffällt."""
    for ap in ('LAX', 'ORD', 'MIA', 'IAH', 'SEA'):
        assert _board(monkeypatch, ap)['reason'] == 'no_free_source', ap
    for ap in ('JFK', 'EWR', 'DEN', 'BOS'):
        assert _board(monkeypatch, ap)['reason'] == 'source_down', ap
