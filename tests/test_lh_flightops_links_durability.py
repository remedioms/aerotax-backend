"""Durabler _links-Cache (Owner 18.08.2026).

`folinks_<token>.json` liegt auf der ungemounteten Container-Disk und war
nach JEDEM Deploy leer → 404-`no_access_code`-Wellen, bis jeder User sein
Tages-Fenster live nachgeladen hatte. Jetzt spiegelt `_links_save` nach
Supabase (`flightops_links_cache`, Muster flightops_crew_cache) und
`_links_load` rehydriert die Disk aus SB, wenn sie leer ist (frischer
Container). Fehlt die Tabelle/SB: exakt altes Disk-Verhalten, fail-open.
"""
import os

import pytest

os.environ.setdefault("AEROTAX_ALLOW_BOOT_WITHOUT_KEY", "1")

import app as A
from blueprints import lh_flightops as fo

TOKEN = 'AT-LINKS-DURABLE'
LINKS = [{'service': 'crewlist',
          'params': {'flightDesignator': 'LH400',
                     'flightDate': '2026-08-18Z', 'accessCode': 'AC1'}}]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(fo, '_flow_dir', lambda: str(tmp_path))
    fo._links_memo.clear()
    fo._links_tbl_state[0], fo._links_tbl_state[1] = 0.0, True
    yield
    fo._links_memo.clear()
    fo._links_tbl_state[0], fo._links_tbl_state[1] = 0.0, True


def test_disk_roundtrip_ohne_sb_unveraendert():
    # SB_AVAILABLE ist im Test-Env False → reines Disk-Verhalten wie vorher.
    fo._links_save(TOKEN, LINKS)
    assert os.path.exists(fo._links_path(TOKEN))
    assert fo._links_load(TOKEN) == LINKS


def test_save_spiegelt_nach_sb(monkeypatch):
    puts = []
    monkeypatch.setattr(fo, '_links_sb_put',
                        lambda tok, links: puts.append((tok, links)) or True)
    fo._links_save(TOKEN, LINKS)
    assert puts == [(TOKEN, LINKS)]


def test_deploy_wipe_rehydriert_aus_sb(monkeypatch):
    """DER Kernfall: Disk leer (frischer Container nach Deploy), SB hat den
    Stand → load liefert ihn UND schreibt die Disk zurück (Hot-Path lokal)."""
    monkeypatch.setattr(fo, '_links_sb_get',
                        lambda tok: list(LINKS) if tok == TOKEN else None)
    assert not os.path.exists(fo._links_path(TOKEN))
    assert fo._links_load(TOKEN) == LINKS
    assert os.path.exists(fo._links_path(TOKEN))     # Disk-Rehydrat
    # Danach trägt die Disk allein — auch wenn SB wieder wegfällt.
    fo._links_memo.clear()
    monkeypatch.setattr(fo, '_links_sb_get', lambda tok: None)
    assert fo._links_load(TOKEN) == LINKS


def test_tabelle_fehlt_fail_open_und_backoff(monkeypatch):
    """PostgREST-404/fehlende Tabelle: kein Crash, altes Disk-Verhalten,
    und 5 min Ruhe statt SB-Fehler auf jedem Request."""
    calls = []

    class _FailingSb:
        def table(self, name):
            calls.append(name)
            raise RuntimeError('relation "flightops_links_cache" does not exist')

    monkeypatch.setattr(A, 'SB_AVAILABLE', True)
    monkeypatch.setattr(A, 'sb', _FailingSb(), raising=False)
    assert fo._links_load(TOKEN) == []                # fail-open
    assert fo._links_tbl_state[1] is False
    assert len(calls) == 1
    fo._links_memo.clear()
    assert fo._links_load(TOKEN) == []                # Backoff: kein 2. Call
    assert len(calls) == 1


def test_leere_liste_ueberschreibt_sb_nicht(monkeypatch):
    """Füllen-nie-überschreiben: ein frischer Container ohne Disk-Bestand darf
    den durablen SB-Stand nicht mit [] plattmachen."""
    calls = []

    class _RecordingSb:
        def table(self, name):
            calls.append(name)
            raise AssertionError('darf nicht erreicht werden')

    monkeypatch.setattr(A, 'SB_AVAILABLE', True)
    monkeypatch.setattr(A, 'sb', _RecordingSb(), raising=False)
    assert fo._links_sb_put(TOKEN, []) is False
    assert calls == []


def test_memo_deckelt_sb_reads(monkeypatch):
    reads = []
    monkeypatch.setattr(fo, '_links_sb_get',
                        lambda tok: reads.append(tok) or None)
    assert fo._links_load(TOKEN) == []
    assert fo._links_load(TOKEN) == []
    # Zweiter Load im Memo-Fenster: kein weiterer SB-Read.
    assert len(reads) == 1


def test_resolve_link_params_nutzt_sb_stand_nach_deploy(monkeypatch):
    """End-to-End am eigentlichen Symptom: accessCode-Resolve OHNE Live-Call,
    obwohl die Disk (Container frisch) leer ist."""
    monkeypatch.setattr(fo, '_links_sb_get', lambda tok: list(LINKS))
    monkeypatch.setattr(fo, 'duty_events',
                        lambda *a, **k: pytest.fail('kein Live-Call nötig'))
    p = fo._resolve_link_params(TOKEN, 'crewlist', 'LH400', '2026-08-18')
    assert p['accessCode'] == 'AC1'
