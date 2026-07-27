"""Latenz-Sweep 27.07.2026 — die Schutzplanken zu den vier Änderungen.

Gemessen wurde vorher an Produktion (10-min-Fenster über die Request-Logs):

  /api/forum/<tok>/threads      684 Aufrufe, ~971 ms, 23,3 % der Arbeitszeit
  /api/user/briefing/<tok>      Schnitt 2088 ms, ein Ausreisser mit 43,2 s
  /api/internal/lh-mqtt/topics  Schnitt 16,7 s, Maximum 37 s

Die Tests hier decken genau das ab, was dabei geändert wurde — und vor allem
die Fragen, die ein Caching-/ETag-Umbau aufwirft: kann ein Nutzer über einen
ETag an fremde Daten kommen, und kann ein verschobener Abruf jemandem einen
veralteten Stand zeigen.
"""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

os.environ.setdefault("AEROTAX_ALLOW_BOOT_WITHOUT_KEY", "1")

_THIS = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_THIS)
sys.path.insert(0, _REPO)

import app as _app  # noqa: E402


# ══════════════════════════════════════════════════════════════════════════
# 1 · ETag — Wirkung UND die Sicherheitsfrage
# ══════════════════════════════════════════════════════════════════════════

def _etag_of(payload, headers=None):
    with _app.app.test_request_context(headers=headers or {}):
        resp = _app._etag_json(payload)
        return resp.status_code, resp.headers.get('ETag'), resp


def test_etag_ist_stabil_solange_der_inhalt_stabil_ist():
    """Gleicher Inhalt ⇒ gleicher ETag, auch bei anderer Dict-Reihenfolge.
    Ein ETag, der sich ohne Inhaltsänderung ändert, bringt nichts."""
    a = {'count': 2, 'threads': [{'id': 'x', 'title': 'A'}]}
    b = {'threads': [{'title': 'A', 'id': 'x'}], 'count': 2}
    _, et_a, _ = _etag_of(a)
    _, et_b, _ = _etag_of(b)
    assert et_a and et_a == et_b


def test_etag_aendert_sich_wenn_der_inhalt_sich_aendert():
    _, et1, _ = _etag_of({'count': 1, 'threads': [{'id': 'x', 'like_count': 3}]})
    _, et2, _ = _etag_of({'count': 1, 'threads': [{'id': 'x', 'like_count': 4}]})
    assert et1 != et2


def test_if_none_match_liefert_304_ohne_body():
    payload = {'count': 1, 'threads': [{'id': 'x'}]}
    _, etag, _ = _etag_of(payload)
    status, etag2, resp = _etag_of(payload, headers={'If-None-Match': etag})
    assert status == 304
    assert etag2 == etag
    assert not resp.get_data()


def test_if_none_match_akzeptiert_weak_prefix_und_listen():
    payload = {'count': 1, 'threads': []}
    _, etag, _ = _etag_of(payload)
    for hdr in (f'W/{etag}', f'"deadbeef", {etag}', '*'):
        status, _, _ = _etag_of(payload, headers={'If-None-Match': hdr})
        assert status == 304, hdr


def test_etag_antwort_ist_niemals_geteilt_cachebar():
    """Ohne `private` würde der aerox-cdn-Worker die token-gebundene Antwort
    ablegen und anonym wieder ausliefern."""
    payload = {'count': 0, 'threads': []}
    _, etag, resp200 = _etag_of(payload)
    assert 'private' in (resp200.headers.get('Cache-Control') or '')
    _, _, resp304 = _etag_of(payload, headers={'If-None-Match': etag})
    assert 'private' in (resp304.headers.get('Cache-Control') or '')


def test_fremder_etag_liefert_niemals_ein_304_auf_anderen_inhalt():
    """DIE Sicherheitsfrage. User B schickt den ETag von User A. Solange sich
    die Inhalte unterscheiden, MUSS B seine eigene 200er-Antwort bekommen —
    ein 304 würde B dazu bringen, eine Kopie zu verwenden, die er nie
    ausgeliefert bekam."""
    a = {'count': 1, 'threads': [{'id': 't1', 'liked_by_me': True,
                                  'is_mine': True}]}
    b = {'count': 1, 'threads': [{'id': 't1', 'liked_by_me': False,
                                  'is_mine': False}]}
    _, etag_a, _ = _etag_of(a)
    status, etag_b, resp = _etag_of(b, headers={'If-None-Match': etag_a})
    assert status == 200
    assert etag_b != etag_a
    assert b'"liked_by_me": false' in resp.get_data() or \
           b'"liked_by_me":false' in resp.get_data()


def test_personalisierung_geht_in_den_etag_ein():
    """liked_by_me/is_mine sind Teil der Nutzlast ⇒ Teil des ETags. Sonst
    könnten zwei Nutzer denselben ETag auf unterschiedlichen Inhalt haben."""
    base = {'id': 't1', 'title': 'gleich'}
    _, et_mine, _ = _etag_of({'count': 1, 'threads': [dict(base, is_mine=True)]})
    _, et_other, _ = _etag_of({'count': 1, 'threads': [dict(base, is_mine=False)]})
    assert et_mine != et_other


def test_etag_gleich_heisst_inhalt_byte_gleich():
    """Der Umkehrschluss, auf dem die Sicherheit ruht: gleicher ETag tritt nur
    bei gleichem Inhalt auf — dann ist ein 304 folgenlos."""
    payload = {'count': 1, 'threads': [{'id': 't1', 'is_mine': False,
                                        'liked_by_me': False}]}
    _, et1, r1 = _etag_of(payload)
    _, et2, r2 = _etag_of(dict(payload))
    assert et1 == et2
    assert r1.get_data() == r2.get_data()


def test_etag_pfad_veraendert_serialisierbarkeit_nicht():
    """Was jsonify vorher ausliefern konnte, liefert _etag_json auch aus —
    inklusive der Typen, die reines json.dumps nicht kennt (datetime, UUID).
    Der ETag-Umbau darf keine Nutzlast neu zum Scheitern bringen."""
    import datetime as _dt
    import uuid as _uuid
    payload = {'count': 1, 'threads': [
        {'id': str(_uuid.uuid4()), 'ts': _dt.datetime(2026, 7, 27, 12, 0)}]}
    with _app.app.test_request_context():
        resp = _app._etag_json(payload)
    assert resp.status_code == 200
    assert resp.headers.get('ETag')


# ══════════════════════════════════════════════════════════════════════════
# 2 · Autor-Avatare: ein Batch statt N+1
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _clear_avatar_cache():
    _app._avatar_url_cache.clear()
    yield
    _app._avatar_url_cache.clear()


def test_avatare_gehen_in_genau_einem_batch_raus(monkeypatch):
    calls = []

    def _fake_bulk(tokens, include_metadata=False):
        calls.append(list(tokens))
        return {t: {'avatar_url': f'https://cdn/{t}.jpg'} for t in tokens}

    monkeypatch.setattr(_app, '_profiles_load_bulk', _fake_bulk)
    out = _app._author_avatar_urls(['a', 'b', 'c', 'a', 'b'])
    assert len(calls) == 1, 'N+1: es gab mehr als einen Lookup'
    assert sorted(calls[0]) == ['a', 'b', 'c'], 'Duplikate nicht entfernt'
    assert out['a'] == 'https://cdn/a.jpg'


def test_zweiter_aufruf_kommt_aus_dem_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(_app, '_profiles_load_bulk',
                        lambda toks, include_metadata=False: (
                            calls.append(list(toks)) or
                            {t: {'avatar_url': 'u'} for t in toks}))
    _app._author_avatar_urls(['a', 'b'])
    _app._author_avatar_urls(['a', 'b'])
    assert len(calls) == 1


def test_nur_die_fehlenden_token_werden_nachgeladen(monkeypatch):
    calls = []
    monkeypatch.setattr(_app, '_profiles_load_bulk',
                        lambda toks, include_metadata=False: (
                            calls.append(sorted(toks)) or
                            {t: {'avatar_url': 'u'} for t in toks}))
    _app._author_avatar_urls(['a'])
    _app._author_avatar_urls(['a', 'b'])
    assert calls == [['a'], ['b']]


def test_supabase_ausfall_wird_nicht_gecacht(monkeypatch):
    """Sonst würde ein einzelner Aussetzer 5 Minuten lang leere Avatare
    einfrieren."""
    state = {'fail': True}

    def _bulk(toks, include_metadata=False):
        if state['fail']:
            raise RuntimeError('sb down')
        return {t: {'avatar_url': 'u'} for t in toks}

    monkeypatch.setattr(_app, '_profiles_load_bulk', _bulk)
    assert _app._author_avatar_urls(['a']) == {'a': None}
    state['fail'] = False
    assert _app._author_avatar_urls(['a']) == {'a': 'u'}


def test_avatar_cache_wird_beim_profil_schreiben_verworfen(monkeypatch):
    monkeypatch.setattr(_app, '_profiles_load_bulk',
                        lambda toks, include_metadata=False: {
                            t: {'avatar_url': 'alt'} for t in toks})
    assert _app._author_avatar_urls(['a'])['a'] == 'alt'
    _app._avatar_cache_invalidate('a')
    monkeypatch.setattr(_app, '_profiles_load_bulk',
                        lambda toks, include_metadata=False: {
                            t: {'avatar_url': 'neu'} for t in toks})
    assert _app._author_avatar_urls(['a'])['a'] == 'neu'


def test_leere_eingabe_macht_keinen_lookup(monkeypatch):
    monkeypatch.setattr(_app, '_profiles_load_bulk',
                        lambda *a, **k: pytest.fail('kein Lookup erwartet'))
    assert _app._author_avatar_urls([]) == {}
    assert _app._author_avatar_urls([None, '']) == {}


# ══════════════════════════════════════════════════════════════════════════
# 3 · _profile_load — Request-Memo (Briefing-Pfad lud dasselbe Profil 2–3×)
# ══════════════════════════════════════════════════════════════════════════

def test_profil_wird_pro_request_nur_einmal_geladen(monkeypatch):
    reads = []
    monkeypatch.setattr(_app, '_profile_load_from_supabase',
                        lambda t: (reads.append(t) or {'airline': 'LH'}))
    monkeypatch.setattr(_app, '_profile_load_from_disk',
                        lambda t: {'token': t, 'profile': {}})
    with _app.app.test_request_context():
        for _ in range(4):
            got = _app._profile_load('AT-X')
            assert got['profile']['airline'] == 'LH'
    assert len(reads) == 1, f'{len(reads)} Supabase-Reads statt 1'


def test_memo_endet_mit_dem_request(monkeypatch):
    reads = []
    monkeypatch.setattr(_app, '_profile_load_from_supabase',
                        lambda t: (reads.append(t) or {'airline': 'LH'}))
    monkeypatch.setattr(_app, '_profile_load_from_disk',
                        lambda t: {'token': t, 'profile': {}})
    for _ in range(3):
        with _app.app.test_request_context():
            _app._profile_load('AT-X')
    assert len(reads) == 3, 'Memo hat einen Request überlebt'


def test_ohne_request_kontext_wird_nicht_gemerkt(monkeypatch):
    reads = []
    monkeypatch.setattr(_app, '_profile_load_from_supabase',
                        lambda t: (reads.append(t) or {'airline': 'LH'}))
    monkeypatch.setattr(_app, '_profile_load_from_disk',
                        lambda t: {'token': t, 'profile': {}})
    for _ in range(3):
        _app._profile_load('AT-X')
    assert len(reads) == 3


def test_verschiedene_token_teilen_sich_kein_memo(monkeypatch):
    """Fremddaten-Frage auf dem Memo-Pfad: der Key ist der Token."""
    monkeypatch.setattr(_app, '_profile_load_from_supabase',
                        lambda t: {'airline': 'LH' if t == 'AT-A' else 'OS'})
    monkeypatch.setattr(_app, '_profile_load_from_disk',
                        lambda t: {'token': t, 'profile': {}})
    with _app.app.test_request_context():
        assert _app._profile_load('AT-A')['profile']['airline'] == 'LH'
        assert _app._profile_load('AT-B')['profile']['airline'] == 'OS'
        assert _app._profile_load('AT-A')['profile']['airline'] == 'LH'


def test_aufrufer_bekommt_immer_ein_eigenes_objekt(monkeypatch):
    """Vorher gab jeder Aufruf ein frisches Dict. Wer das Ergebnis in-place
    verändert, darf keinen anderen Leser desselben Requests beeinflussen."""
    monkeypatch.setattr(_app, '_profile_load_from_supabase',
                        lambda t: {'airline': 'LH'})
    monkeypatch.setattr(_app, '_profile_load_from_disk',
                        lambda t: {'token': t, 'profile': {}})
    with _app.app.test_request_context():
        first = _app._profile_load('AT-X')
        first['profile']['airline'] = 'GESCHMIERT'
        second = _app._profile_load('AT-X')
        assert second['profile']['airline'] == 'LH'


def test_schreiben_macht_das_memo_ungueltig(monkeypatch):
    """Read-after-write im selben Request: wer speichert, muss danach seinen
    eigenen Stand lesen."""
    state = {'airline': 'LH'}
    monkeypatch.setattr(_app, '_profile_load_from_supabase',
                        lambda t: dict(state))
    monkeypatch.setattr(_app, '_profile_load_from_disk',
                        lambda t: {'token': t, 'profile': {}})
    monkeypatch.setattr(_app, 'SB_AVAILABLE', True)
    monkeypatch.setattr(_app, '_profile_metadata_merge_sb', lambda t, m: False)

    class _Tbl:
        def upsert(self, *a, **k):
            return self

        def select(self, *a, **k):
            return self

        def eq(self, *a, **k):
            return self

        def limit(self, *a, **k):
            return self

        def execute(self):
            return type('R', (), {'data': []})()

    monkeypatch.setattr(_app, 'sb', type('SB', (), {'table': lambda s, n: _Tbl()})())

    with _app.app.test_request_context():
        assert _app._profile_load('AT-X')['profile']['airline'] == 'LH'
        state['airline'] = 'OS'
        _app._profile_save_to_supabase('AT-X', {'airline': 'OS'})
        assert _app._profile_load('AT-X')['profile']['airline'] == 'OS'


# ══════════════════════════════════════════════════════════════════════════
# 4 · Boot-Assertion: kein stiller Verlust bezahlter Aufträge
# ══════════════════════════════════════════════════════════════════════════

def _assert_with(monkeypatch, *, disable, mode, allow=None, gunicorn=True):
    monkeypatch.setenv('AEROTAX_DISABLE_BG_THREADS', disable)
    monkeypatch.setattr(_app, 'AEROTAX_EXECUTION_MODE', mode)
    if allow is None:
        monkeypatch.delenv('AEROTAX_ALLOW_NO_CALC_WORKER', raising=False)
    else:
        monkeypatch.setenv('AEROTAX_ALLOW_NO_CALC_WORKER', allow)
    monkeypatch.setattr(_app, '_running_under_gunicorn', lambda: gunicorn)
    return _app._assert_calc_worker_sane


def test_thread_modus_ohne_worker_startet_nicht(monkeypatch):
    """Der Kern: das wäre ein stiller Verlust bezahlter Aufträge gewesen."""
    fn = _assert_with(monkeypatch, disable='1', mode='thread')
    with pytest.raises(RuntimeError) as ei:
        fn()
    msg = str(ei.value)
    assert 'AEROTAX_DISABLE_BG_THREADS' in msg
    assert 'AEROTAX_ALLOW_NO_CALC_WORKER' in msg, 'Meldung nennt den Ausweg nicht'
    assert 'lautlos' in msg or 'verliert' in msg, 'Meldung erklärt das Warum nicht'


def test_normaler_betrieb_startet(monkeypatch):
    _assert_with(monkeypatch, disable='0', mode='thread')()


def test_cloud_tasks_modus_braucht_keinen_thread(monkeypatch):
    _assert_with(monkeypatch, disable='1', mode='cloud_tasks')()


def test_sidecar_mit_ausdruecklichem_zugestaendnis_startet(monkeypatch):
    """Der aerotax-poll-Container fährt genau diese Kombination."""
    _assert_with(monkeypatch, disable='1', mode='thread', allow='1')()


def test_tests_und_cli_sind_nicht_betroffen(monkeypatch):
    """Ohne diesen Zweig wäre die halbe Testsuite nicht mehr importierbar."""
    _assert_with(monkeypatch, disable='1', mode='thread', gunicorn=False)()


def test_prozess_ohne_worker_nimmt_keine_auftraege_an(monkeypatch):
    """Das Zugeständnis oben darf nicht zur selben stillen Falle werden."""
    monkeypatch.setattr(_app, 'AEROTAX_EXECUTION_MODE', 'thread')
    monkeypatch.setattr(_app, '_running_under_gunicorn', lambda: True)
    monkeypatch.setenv('AEROTAX_DISABLE_BG_THREADS', '1')
    assert _app._calc_worker_available() is False
    monkeypatch.setenv('AEROTAX_DISABLE_BG_THREADS', '0')
    assert _app._calc_worker_available() is True


def test_testlauf_wird_nicht_abgewiesen(monkeypatch):
    """Die Testsuite setzt AEROTAX_DISABLE_BG_THREADS=1 zur Isolation und
    treibt die Queue selbst — dort waere ein 503 kein Schutz, nur kaputt."""
    monkeypatch.setattr(_app, 'AEROTAX_EXECUTION_MODE', 'thread')
    monkeypatch.setattr(_app, '_running_under_gunicorn', lambda: False)
    monkeypatch.setenv('AEROTAX_DISABLE_BG_THREADS', '1')
    assert _app._calc_worker_available() is True


def test_cloud_tasks_prozess_nimmt_auftraege_an(monkeypatch):
    monkeypatch.setattr(_app, 'AEROTAX_EXECUTION_MODE', 'cloud_tasks')
    monkeypatch.setenv('AEROTAX_DISABLE_BG_THREADS', '1')
    assert _app._calc_worker_available() is True


# ══════════════════════════════════════════════════════════════════════════
# 5 · lh-mqtt/topics — stale-while-revalidate + Single-Flight + Budget
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def mqtt():
    from blueprints import lh_mqtt as m
    with m._topics_lock:
        m._topics_memo['ts'] = 0.0
        m._topics_memo['topics'] = []
        m._topics_state['refreshing'] = False
    yield m
    with m._topics_lock:
        m._topics_memo['ts'] = 0.0
        m._topics_memo['topics'] = []
        m._topics_state['refreshing'] = False


def test_kalter_aufruf_rechnet_genau_einmal_trotz_paralleler_aufrufer(mqtt,
                                                                     monkeypatch):
    """Der Daemon bricht nach 30 s ab und wiederholt nach 20 s — vorher feuerte
    jeder Wiederholer dieselbe Rechnung erneut (dreimal live beobachtet)."""
    runs = []
    barrier = threading.Event()

    def _slow(budget_s=None):
        runs.append(1)
        barrier.wait(timeout=5)
        return ['prd/FlightUpdate/LH/LH400/2026-07-27'], True

    monkeypatch.setattr(mqtt, '_topics_compute', _slow)
    results = []

    def _caller():
        results.append(mqtt._topics_build_and_store(budget_s=1))

    threads = [threading.Thread(target=_caller) for _ in range(5)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    barrier.set()
    for t in threads:
        t.join(timeout=10)

    assert len(runs) == 1, f'{len(runs)} parallele Rechnungen statt 1'
    assert len(results) == 5
    assert all(r[0] == ['prd/FlightUpdate/LH/LH400/2026-07-27'] for r in results)
    assert sum(1 for r in results if r[1]) == 4, 'nur einer darf gerechnet haben'


def test_alter_schnappschuss_wird_sofort_ausgeliefert(mqtt, monkeypatch):
    """Kern des Latenz-Fixes: der Aufrufer wartet nie wieder auf die Rechnung."""
    with mqtt._topics_lock:
        mqtt._topics_memo['ts'] = time.time() - (mqtt._TOPICS_TTL_S + 60)
        mqtt._topics_memo['topics'] = ['prd/FlightUpdate/LH/LH1/2026-07-27']
    kicked = []
    monkeypatch.setattr(mqtt, '_topics_kick_refresh', lambda: kicked.append(1))
    monkeypatch.setattr(mqtt, '_secret_ok', lambda: True)
    monkeypatch.setattr(mqtt, '_topics_compute',
                        lambda budget_s=None: pytest.fail(
                            'darf im Request nicht rechnen'))
    with _app.app.test_request_context():
        resp = mqtt.lh_mqtt_topics()
    body = resp.get_json()
    assert body['memo'] == 'stale'
    assert body['topics'] == ['prd/FlightUpdate/LH/LH1/2026-07-27']
    assert kicked == [1], 'Hintergrund-Erneuerung nicht angestossen'


def test_frischer_schnappschuss_stoesst_nichts_an(mqtt, monkeypatch):
    with mqtt._topics_lock:
        mqtt._topics_memo['ts'] = time.time()
        mqtt._topics_memo['topics'] = ['prd/FlightUpdate/LH/LH1/2026-07-27']
    monkeypatch.setattr(mqtt, '_secret_ok', lambda: True)
    monkeypatch.setattr(mqtt, '_topics_kick_refresh',
                        lambda: pytest.fail('unnötige Erneuerung'))
    with _app.app.test_request_context():
        body = mqtt.lh_mqtt_topics().get_json()
    assert body['memo'] is True


def test_hintergrund_erneuerung_laeuft_nur_einmal_gleichzeitig(mqtt, monkeypatch):
    started = []
    gate = threading.Event()

    def _compute(budget_s=None):
        started.append(1)
        gate.wait(timeout=5)
        return ['x'], True

    monkeypatch.setattr(mqtt, '_topics_compute', _compute)
    for _ in range(4):
        mqtt._topics_kick_refresh()
    time.sleep(0.2)
    gate.set()
    time.sleep(0.4)
    assert len(started) == 1


def test_secret_gate_bleibt(mqtt, monkeypatch):
    monkeypatch.setattr(mqtt, '_secret_ok', lambda: False)
    with _app.app.test_request_context():
        resp, code = mqtt.lh_mqtt_topics()
    assert code == 403


def test_zeitbudget_stoppt_weitere_lh_calls(mqtt, monkeypatch):
    """Ohne Bremse lief der kalte Aufbau in den 30-s-Timeout des Daemons."""
    fetched = []

    def _fetch(flight, date, dep, arr):
        fetched.append(flight)
        time.sleep(0.05)
        return None, True

    monkeypatch.setattr(mqtt, '_fetch_leg_reg', _fetch)
    monkeypatch.setattr(mqtt, '_reg_cache_read', lambda keys: {})
    monkeypatch.setattr(mqtt, '_reg_cache_write', lambda fresh: None)
    monkeypatch.setattr(mqtt, '_leg_reg_gate_shut', lambda: False)
    monkeypatch.setattr(mqtt, '_reg_memo_get', lambda k, n: (False, None))
    monkeypatch.setattr(mqtt, '_reg_memo_put', lambda *a, **k: None)

    legs = [(f'LH{i}', '2026-07-27', 'FRA', 'MUC') for i in range(40)]
    out = mqtt._legs_regs(legs, deadline=time.time() + 0.15)
    assert len(fetched) < 40, 'Budget hat nicht gegriffen'
    assert len(out) == 40, 'jedes Leg braucht einen Eintrag'
    assert all(out[leg] is None for leg in legs[len(fetched):])


# ══════════════════════════════════════════════════════════════════════════
# 6 · Briefing — Zeitbudget der Live-Anreicherung
# ══════════════════════════════════════════════════════════════════════════

def _sectors(n):
    return [{'flight': f'LH{100 + i}', 'from': 'FRA', 'to': 'MUC',
             'dep_iso': None} for i in range(n)]


def test_anreicherung_bricht_am_budget_ab(monkeypatch):
    """Der p99-Fix: ein kalter Worker lud hier synchron ganze Airport-Boards."""
    seen = []

    def _slow_merge(fn, **kw):
        seen.append(fn)
        time.sleep(0.05)
        return None

    monkeypatch.setattr(_app, '_flight_obs_merged', _slow_merge)
    monkeypatch.setattr(_app, '_ax_codeshare_map', lambda: {})
    secs = _sectors(40)
    _app._enrich_leg_delays(secs, '2026-07-27', deadline=time.time() + 0.2)
    assert len(seen) < 40, 'Budget hat nicht gegriffen'
    assert len(seen) > 0, 'gar nichts angereichert'


def test_ohne_deadline_werden_alle_sektoren_angereichert(monkeypatch):
    """Default-Verhalten für alle bestehenden Aufrufer bleibt unverändert."""
    seen = []
    monkeypatch.setattr(_app, '_flight_obs_merged',
                        lambda fn, **kw: (seen.append(fn), None)[1])
    monkeypatch.setattr(_app, '_ax_codeshare_map', lambda: {})
    _app._enrich_leg_delays(_sectors(12), '2026-07-27')
    assert len(seen) == 12


def test_budget_erfindet_keine_werte(monkeypatch):
    """Ein abgeschnittener Sektor bleibt ROH — kein delay_known, keine 0.
    Lieber ein fehlendes Feld als ein erfundenes."""
    monkeypatch.setattr(_app, '_flight_obs_merged',
                        lambda fn, **kw: time.sleep(0.05) or None)
    monkeypatch.setattr(_app, '_ax_codeshare_map', lambda: {})
    secs = _sectors(40)
    _app._enrich_leg_delays(secs, '2026-07-27', deadline=time.time() + 0.15)
    untouched = [s for s in secs if 'delay_known' not in s]
    assert untouched, 'Test greift nicht — alle Sektoren wurden angefasst'
    for s in untouched:
        assert 'delay_min' not in s
        assert 'status' not in s


# ══════════════════════════════════════════════════════════════════════════
# 7 · Was der adversariale Review gefunden hat — Regressions-Riegel
# ══════════════════════════════════════════════════════════════════════════

def test_fresh_umgeht_den_request_memo(monkeypatch):
    """LH-FlightOps liest in `_valid_access` absichtlich ein zweites Mal, um
    die Token-Rotation eines ANDEREN Containers zu sehen. Läge der Memo
    dazwischen, schriebe der Race-Verlierer seinen verbrannten Refresh über
    den frischen des Gewinners — genau der Grant-Burn von Ende Juli."""
    state = {'refresh': 'RT-ALT'}
    reads = []
    monkeypatch.setattr(_app, '_profile_load_from_supabase',
                        lambda t: (reads.append(t) or
                                   {'flightops_tokens': dict(state)}))
    monkeypatch.setattr(_app, '_profile_load_from_disk',
                        lambda t: {'token': t, 'profile': {}})
    with _app.app.test_request_context():
        first = _app._profile_load('AT-X')['profile']['flightops_tokens']
        assert first['refresh'] == 'RT-ALT'
        state['refresh'] = 'RT-NEU'          # anderer Container rotiert
        stale = _app._profile_load('AT-X')['profile']['flightops_tokens']
        assert stale['refresh'] == 'RT-ALT', 'Memo greift wie vorgesehen'
        fresh = _app._profile_load('AT-X', fresh=True)
        assert fresh['profile']['flightops_tokens']['refresh'] == 'RT-NEU'
    assert len(reads) == 2


def test_tokens_load_reicht_fresh_durch(monkeypatch):
    from blueprints import lh_flightops as fo
    seen = []
    # `_tokens_load` macht `import app as _app` — es benutzt also das Modul aus
    # sys.modules, und test_calculation.py tauscht das dort aus. Genau dieselbe
    # Auflösung hier, sonst hängt der Test an der Testreihenfolge.
    app_mod = sys.modules['app']
    monkeypatch.setattr(app_mod, '_profile_load',
                        lambda t, fresh=False: (seen.append(fresh) or
                                                {'profile': {}}))
    fo._tokens_load('AT-X')
    fo._tokens_load('AT-X', fresh=True)
    assert seen == [False, True]


def test_invalidierung_ueberlebt_supabase_ausfall(monkeypatch):
    """Ohne Supabase kehrt _profile_save_to_supabase früh um — der Memo muss
    TROTZDEM fallen, sonst liest der Disk-Fallback im selben Request den Stand
    von vor dem Speichern."""
    monkeypatch.setattr(_app, 'SB_AVAILABLE', False)
    with _app.app.test_request_context():
        _app._profile_memo_put('AT-X', {'token': 'AT-X',
                                        'profile': {'airline': 'ALT'}})
        assert _app._profile_memo_get('AT-X') is not None
        _app._profile_save_to_supabase('AT-X', {'airline': 'NEU'})
        assert _app._profile_memo_get('AT-X') is None


def test_metadata_merge_invalidiert_ebenfalls(monkeypatch):
    """set_crew_note/_punct_persist_me schreiben direkt über die RPC, nicht
    über _profile_save_to_supabase."""
    monkeypatch.setattr(_app, '_social_rpc_call', lambda fn, p: (True, [1]))
    with _app.app.test_request_context():
        _app._profile_memo_put('AT-X', {'token': 'AT-X', 'profile': {}})
        _app._profile_metadata_merge_sb('AT-X', {'crew_note_text': 'hi'})
        assert _app._profile_memo_get('AT-X') is None


def test_503_ohne_worker_verbraucht_keine_zahlung(monkeypatch):
    """Der Riegel muss VOR dem Einlösen des Payment-Intents greifen. Ein 503
    nach dem Consume hätte das Geld genommen und nichts geliefert."""
    monkeypatch.setattr(_app, 'AEROTAX_EXECUTION_MODE', 'thread')
    monkeypatch.setattr(_app, '_running_under_gunicorn', lambda: True)
    monkeypatch.setenv('AEROTAX_DISABLE_BG_THREADS', '1')
    monkeypatch.setattr(_app, '_ip_rate_limited', lambda *a, **k: False)
    monkeypatch.setattr(
        _app, '_try_consume_payment_intent_supabase',
        lambda *a, **k: pytest.fail('Zahlung wurde trotz 503 eingeloest'))
    client = _app.app.test_client()
    resp = client.post('/api/process', data={'year': '2025', 'km': '10',
                                             'pi_id': 'pi_test123'})
    assert resp.status_code == 503
    assert resp.get_json().get('reason_code') == 'NO_CALC_WORKER'


def test_gekuerzte_topics_kuendigen_keine_abos(mqtt, monkeypatch):
    """Der Daemon meldet sich von allem ab, was in der Antwort fehlt. Eine vom
    Budget gekürzte Liste darf den alten Stand deshalb nicht ersetzen."""
    with mqtt._topics_lock:
        mqtt._topics_memo['ts'] = time.time() - 10_000
        mqtt._topics_memo['topics'] = ['topic/A', 'topic/B', 'topic/C']
    monkeypatch.setattr(mqtt, '_topics_compute',
                        lambda budget_s=None: (['topic/A'], False))
    topics, _ = mqtt._topics_build_and_store(budget_s=1)
    assert set(topics) == {'topic/A', 'topic/B', 'topic/C'}, \
        'gekuerzter Aufbau hat Abos gekuendigt'


def test_vollstaendiger_aufbau_ersetzt_den_stand(mqtt, monkeypatch):
    """Gegenprobe: sonst wuerden veraltete Topics ewig mitgeschleppt."""
    with mqtt._topics_lock:
        mqtt._topics_memo['ts'] = time.time() - 10_000
        mqtt._topics_memo['topics'] = ['topic/ALT']
    monkeypatch.setattr(mqtt, '_topics_compute',
                        lambda budget_s=None: (['topic/NEU'], True))
    topics, _ = mqtt._topics_build_and_store()
    assert topics == ['topic/NEU']


def test_gekuerzter_stand_gilt_sofort_als_erneuerungsbeduerftig(mqtt,
                                                               monkeypatch):
    monkeypatch.setattr(mqtt, '_topics_compute',
                        lambda budget_s=None: (['topic/A'], False))
    mqtt._topics_build_and_store(budget_s=1)
    ts, _ = mqtt._topics_snapshot()
    assert (time.time() - ts) > mqtt._TOPICS_TTL_S - mqtt._TOPICS_RETRY_AFTER_S


def test_leerer_roster_fetch_wird_nicht_als_stand_abgelegt(mqtt, monkeypatch):
    """_sector_rows schluckt Fehler und liefert []. Wuerde das als gueltiger
    Stand durchgehen, meldete sich der Daemon von ALLEN Topics ab."""
    with mqtt._topics_lock:
        mqtt._topics_memo['ts'] = time.time() - 10_000
        mqtt._topics_memo['topics'] = ['topic/A', 'topic/B']
    monkeypatch.setattr(mqtt, '_sector_rows', lambda dates: [])
    topics, _ = mqtt._topics_build_and_store(budget_s=1)
    assert set(topics) == {'topic/A', 'topic/B'}


def test_zu_alter_stand_wird_nicht_mehr_ausgeliefert(mqtt, monkeypatch):
    """Drei Worker + 300-s-Takt: ohne Obergrenze waere die Liste im Mittel
    eine Viertelstunde alt und die Worker liefen auseinander."""
    with mqtt._topics_lock:
        mqtt._topics_memo['ts'] = time.time() - (mqtt._TOPICS_MAX_STALE_S + 60)
        mqtt._topics_memo['topics'] = ['topic/ALT']
    monkeypatch.setattr(mqtt, '_secret_ok', lambda: True)
    monkeypatch.setattr(mqtt, '_topics_compute',
                        lambda budget_s=None: (['topic/NEU'], True))
    with _app.app.test_request_context():
        body = mqtt.lh_mqtt_topics().get_json()
    assert body['topics'] == ['topic/NEU'], 'zu alter Stand wurde ausgeliefert'


def test_fehlgeschlagener_thread_start_blockiert_nicht_dauerhaft(mqtt,
                                                                 monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError('kein Thread frei')
    monkeypatch.setattr(mqtt.threading, 'Thread', _boom)
    mqtt._topics_kick_refresh()
    assert mqtt._topics_state['refreshing'] is False, \
        'Flag haengt — dieser Worker wuerde nie wieder erneuern'


def test_budget_dichtet_keinen_tail_an(monkeypatch):
    """Die Turnaround-Weitergabe darf einem abgeschnittenen Leg kein
    Kennzeichen andichten, das nie gegen ein Board geprueft wurde."""
    calls = []
    monkeypatch.setattr(_app, '_flight_obs_merged',
                        lambda fn, **kw: time.sleep(0.05) or None)
    monkeypatch.setattr(_app, '_ax_codeshare_map', lambda: {})
    monkeypatch.setattr(_app, '_carry_forward_turnaround_tails',
                        lambda secs, homebase=None: calls.append(len(secs)))
    secs = _sectors(40)
    _app._enrich_leg_delays(secs, '2026-07-27', deadline=time.time() + 0.15)
    assert calls and calls[0] < 40, \
        'Weitergabe lief ueber nicht angereicherte Sektoren'


def test_ohne_budget_laeuft_die_weitergabe_ueber_alles(monkeypatch):
    calls = []
    monkeypatch.setattr(_app, '_flight_obs_merged', lambda fn, **kw: None)
    monkeypatch.setattr(_app, '_ax_codeshare_map', lambda: {})
    monkeypatch.setattr(_app, '_carry_forward_turnaround_tails',
                        lambda secs, homebase=None: calls.append(len(secs)))
    _app._enrich_leg_delays(_sectors(6), '2026-07-27')
    assert calls == [6]


def test_304_ist_als_json_markiert():
    """Sonst erbt der 304er Flasks HTML-Default, bekommt eine HTML-CSP
    angeklebt und NSURLCache markiert den gespeicherten JSON-Eintrag als
    HTML."""
    payload = {'count': 0, 'threads': []}
    _, etag, _ = _etag_of(payload)
    _, _, resp = _etag_of(payload, headers={'If-None-Match': etag})
    assert resp.status_code == 304
    assert 'application/json' in (resp.headers.get('Content-Type') or '')


def test_avatar_cache_bleibt_beschraenkt(monkeypatch):
    monkeypatch.setattr(_app, '_profiles_load_bulk',
                        lambda toks, include_metadata=False: {
                            t: {'avatar_url': 'u'} for t in toks})
    for chunk in range(7):
        _app._author_avatar_urls([f'tok-{chunk}-{i}' for i in range(1000)])
    assert len(_app._avatar_url_cache) <= 5000


def test_ohne_budget_bleibt_das_verhalten_unveraendert(mqtt, monkeypatch):
    fetched = []
    monkeypatch.setattr(mqtt, '_fetch_leg_reg',
                        lambda f, d, dep, arr: (fetched.append(f), ('D-AIXA', True))[1])
    monkeypatch.setattr(mqtt, '_reg_cache_read', lambda keys: {})
    monkeypatch.setattr(mqtt, '_reg_cache_write', lambda fresh: None)
    monkeypatch.setattr(mqtt, '_leg_reg_gate_shut', lambda: False)
    monkeypatch.setattr(mqtt, '_reg_memo_get', lambda k, n: (False, None))
    monkeypatch.setattr(mqtt, '_reg_memo_put', lambda *a, **k: None)
    legs = [(f'LH{i}', '2026-07-27', 'FRA', 'MUC') for i in range(5)]
    out = mqtt._legs_regs(legs)
    assert len(fetched) == 5
    assert all(v == 'D-AIXA' for v in out.values())
