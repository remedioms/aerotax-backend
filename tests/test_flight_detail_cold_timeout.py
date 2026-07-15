"""Detail-Aggregat Kaltstart-Latenz — Timeout-Degradation + Memo-Nachwärmen.

Owner 2026-07-15 „Detail-Kaltstart ~8 s": der GRATIS FR24-gRPC-Zeiten-Korridor
(_flight_times_free_first → _grpc_times_free, 1–8 s) lag SYNCHRON auf dem
blockierenden resolve-Pfad VOR dem Fan-out = der Kaltstart-Pol.

Fix (nur Latenz-Umbau, KEIN Mehr-Spend):
  • resolve-Subcall setzt nogrpc=1 → _enrich_flight_status_with_obs lässt den
    synchronen gRPC-Zeiten-Nachschlag aus (Board-/Obs-Fills bleiben).
  • Das Aggregat holt dieselben Zeiten als EIGENEN parallelen Fan-out-Task MIT
    HARTEM 1.5-s-Timeout (Budget ab Task-Start). Kommt er rechtzeitig → mergen;
    sonst NICHT blocken (Feld null im Kaltstart) + Detached-Daemon wärmt das
    Detail-Memo nach → der NÄCHSTE (warme) Aufruf trägt die Zeiten.

Diese Tests decken die Degradation ab:
  1. langsame Zeiten-Quelle ⇒ Antwort < Zeitbudget, sched_arr null im Kaltstart
  2. Board-/Obs-Route trotzdem da (kein Feld dauerhaft verloren)
  3. Memo-Nachwärmung: nach Abschluss des Hintergrund-Tasks trägt der warme
     Aufruf sched_arr

KEIN echter Netz-/DB-Zugriff — alle Leaf-Quellen gemockt.
"""
import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import time

import pytest

import app as A
import blueprints.aerox_data_blueprint as BP


@pytest.fixture(autouse=True)
def _clear_memo():
    BP._LIFECYCLE_MEMO.clear()
    BP._FREE_TIMES_MEMO.clear()
    yield
    BP._LIFECYCLE_MEMO.clear()
    BP._FREE_TIMES_MEMO.clear()


@pytest.fixture
def client():
    A.app.testing = True
    return A.app.test_client()


class _J:
    def __init__(self, d):
        self._d = d

    def get_json(self, silent=True):
        return self._d


def _install(monkeypatch, grpc_delay=0.0, grpc_ret=None):
    """Stub alle Leaf-Quellen des Aggregats. grpc_delay simuliert den langsamen
    FR24-gRPC-Zeiten-Korridor; grpc_ret sein (verzögertes) Ergebnis (Epoch)."""
    calls = {'grpc': 0}

    # aircraft_live-Miss → Obs-Pfad wird genommen
    monkeypatch.setattr(BP, '_aircraft_live_flight', lambda *a, **k: None)
    monkeypatch.setattr(BP, '_aircraft_live_pos',
                        lambda *a, **k: (None, None, None, None))

    # Obs: Route bekannt, aber sched_arr FEHLT → triggert den Zeiten-Task
    def _facts(fn, date, dep_iata=None, arr_iata=None):
        return {'dep_iata': 'FRA', 'arr_iata': 'BEG', 'reg': 'D-AIXS',
                'type': 'A320', 'sched_dep': '2026-07-15T10:00:00+02:00'}
    monkeypatch.setattr(BP, '_flight_facts_from_obs', _facts)

    # der langsame gRPC-Zeiten-Korridor (Leaf in _flight_times_free_first)
    def _grpc(callsign, origin, dest):
        calls['grpc'] += 1
        if grpc_delay:
            time.sleep(grpc_delay)
        return grpc_ret
    monkeypatch.setattr(BP, '_grpc_times_free', _grpc)

    # Foto (external HTTP) — schnell + gecacht wegstubben
    monkeypatch.setattr(BP, '_cache_get', lambda *a, **k: None)
    monkeypatch.setattr(BP, '_cache_put', lambda *a, **k: None)
    monkeypatch.setattr(BP, '_http_json', lambda *a, **k: {'photos': [
        {'thumbnail_large': {'src': 'http://x/p.jpg'},
         'photographer': 'x', 'link': 'http://x'}]})

    # interne Views via _life_app
    real_life = BP._life_app

    def _fake_life(name, default=None):
        if name == 'ax_flight_info':
            return lambda *a, **k: _J({'found': True, 'origin': 'FRA',
                                       'dest': 'BEG', 'reg': 'D-AIXS'})
        if name == 'ax_route_history':
            return lambda *a, **k: _J({'ok': True, 'total': 3})
        if name == 'ax_flight_route':
            return lambda *a, **k: _J({'found': True})
        return real_life(name, default)
    monkeypatch.setattr(BP, '_life_app', _fake_life)

    # lokale Referenz-DB (0 ms)
    monkeypatch.setattr(BP, '_airport_row',
                        lambda c: {'iata': c, 'icao': None, 'name': c,
                                   'city': c, 'country': 'DE',
                                   'lat': 50.0, 'lon': 8.0})
    monkeypatch.setattr(BP, '_airline_row',
                        lambda c: {'name': 'LH', 'iata': 'LH', 'icao': 'DLH'})
    return calls


def test_slow_times_source_degrades_under_budget(client, monkeypatch):
    """Langsame Zeiten-Quelle (5 s) darf das Aggregat NICHT über das Budget
    halten: Antwort < 4 s, sched_arr null, aber Route/Info da."""
    _install(monkeypatch, grpc_delay=5.0,
             grpc_ret={'sched_dep': int(time.time()),
                       'sched_arr': int(time.time()) + 7200,
                       'eta': int(time.time()) + 7200})
    t = time.time()
    r = client.get('/api/ax/flight-detail/LH1412?date=2026-07-15&fresh=1')
    wall = time.time() - t
    assert r.status_code == 200
    j = r.get_json()
    # Antwort deutlich unter der 5-s-Quelle (hartes 1.5-s-Zeitbudget greift).
    assert wall < 4.0, f'aggregat blockierte {wall:.2f}s (Budget verletzt)'
    # Kein Feld dauerhaft verloren: Route/Reg aus Obs sind da.
    rf = j.get('resolve') or {}
    assert rf.get('dep_iata') == 'FRA' and rf.get('arr_iata') == 'BEG'
    assert rf.get('sched_dep')          # Board-Zeit da
    # sched_arr fehlt im KALTSTART (gRPC lief ins Timeout).
    assert not rf.get('sched_arr')
    # info/history/photo trotzdem befüllt (parallel, nicht vom Zeiten-Task blockiert).
    assert (j.get('info') or {}).get('found')
    assert (j.get('history') or {}).get('ok')
    assert j.get('photo')


def test_memo_warm_backfills_sched_arr(client, monkeypatch):
    """Nach Abschluss des Hintergrund-Tasks trägt der WARME Aufruf sched_arr —
    das Timeout-Feld geht nicht dauerhaft verloren."""
    _install(monkeypatch, grpc_delay=1.0,
             grpc_ret={'sched_dep': int(time.time()),
                       'sched_arr': int(time.time()) + 7200,
                       'eta': int(time.time()) + 7200})
    # Kaltstart: hartes Budget ~1.5 s, gRPC braucht 1.0 s → hier ggf. schon da
    # ODER knapp im Timeout. In JEDEM Fall muss der warme Aufruf sched_arr tragen.
    r1 = client.get('/api/ax/flight-detail/LH1412?date=2026-07-15&fresh=1')
    assert r1.status_code == 200
    # dem Hintergrund-Warm Zeit geben (gRPC 1 s + Merge).
    time.sleep(2.0)
    r2 = client.get('/api/ax/flight-detail/LH1412?date=2026-07-15')   # warm/memo
    assert r2.status_code == 200
    rf2 = (r2.get_json() or {}).get('resolve') or {}
    assert rf2.get('sched_arr'), 'warmer Aufruf hat sched_arr nicht (Memo nicht nachgewärmt)'


def test_complete_board_times_skip_grpc(client, monkeypatch):
    """Trägt das Board schon BEIDE Soll-Seiten, wird der gRPC-Zeiten-Task gar
    nicht erst gestartet (kein Call, kein Spend)."""
    calls = _install(monkeypatch, grpc_delay=5.0, grpc_ret=None)

    # Obs liefert diesmal BEIDE Zeiten → _need_times = False
    def _facts_full(fn, date, dep_iata=None, arr_iata=None):
        return {'dep_iata': 'FRA', 'arr_iata': 'BEG', 'reg': 'D-AIXS',
                'type': 'A320', 'sched_dep': '2026-07-15T10:00:00+02:00',
                'sched_arr': '2026-07-15T12:00:00+02:00'}
    monkeypatch.setattr(BP, '_flight_facts_from_obs', _facts_full)

    t = time.time()
    r = client.get('/api/ax/flight-detail/LH1412?date=2026-07-15&fresh=1')
    wall = time.time() - t
    assert r.status_code == 200
    j = r.get_json()
    rf = j.get('resolve') or {}
    assert rf.get('sched_dep') and rf.get('sched_arr')
    # gRPC-Zeiten-Task NICHT gestartet (Board vollständig) → schnell + 0 Calls.
    assert calls['grpc'] == 0
    assert wall < 3.0


def test_resolve_subcall_sets_nogrpc(client, monkeypatch):
    """Der resolve-Subcall des Aggregats setzt nogrpc=1 → der synchrone gRPC-
    Zeiten-Nachschlag im Enrich läuft NICHT (er wäre der blockierende Pol)."""
    seen = {'enrich_nogrpc': None}
    real_enrich = BP._enrich_flight_status_with_obs

    def _spy(flight, date=None, allow_paid=True, nogrpc=False):
        seen['enrich_nogrpc'] = nogrpc
        return real_enrich(flight, date=date, allow_paid=allow_paid,
                           nogrpc=nogrpc)
    monkeypatch.setattr(BP, '_enrich_flight_status_with_obs', _spy)
    _install(monkeypatch, grpc_delay=0.0, grpc_ret=None)

    r = client.get('/api/ax/flight-detail/LH1412?date=2026-07-15&fresh=1')
    assert r.status_code == 200
    # Der Aggregat-resolve-Subcall MUSS nogrpc=True durchgereicht haben.
    assert seen['enrich_nogrpc'] is True
