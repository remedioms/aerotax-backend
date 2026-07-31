"""Refresher-Drain-Guard (Vorfall 2026-07-31 12:56).

Ein zweiter, parallel anlaufender Deploy feuerte den Drain (deploy-hetzner.sh
Schritt 2b) auf den 32 s zuvor gebooteten poll-Container — und erstellte ihn
danach NIE neu. Das Drain-Flag war ein Einweg-Schalter: der fo-refresher-Thread
beendete sich (`[fo-refresher] beendet (drain/exit)`), niemand rotierte mehr,
Wächter-Alarm, manueller `docker restart`.

Diese Zelle pinnt beide Leitplanken des Fixes:
  1. Der HTTP-Drain PAUSIERT (mit Auto-Resume), er tötet nicht mehr.
  2. Der Wächter belebt einen trotzdem toten Loop in-process wieder.
Und den Vertrag, der dabei NICHT fallen darf: während der Pause rotiert
definitiv nichts (Grant-Burn-Schutz).

Alles mit Mock-Zeit — kein echtes Warten, kein LH-Call.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from blueprints import lh_flightops as fo

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _clean_refresher_state(tmp_path_factory):
    """Modul-State ist prozessweit — vor UND nach jedem Test zurücksetzen
    (Suite-Ordnungs-Rot ist in dieser Datei sonst garantiert). Der Herzschlag
    landet in einer Test-eigenen Datei, nie im echten /tmp-Pfad."""
    box = tmp_path_factory.mktemp('beat')
    beat, pausef = str(box / 'refresher.beat'), str(box / 'refresher.pause')
    alt_b, alt_p = fo._REFRESHER_BEAT_FILE, fo._REFRESHER_PAUSE_FILE
    fo._REFRESHER_BEAT_FILE, fo._REFRESHER_PAUSE_FILE = beat, pausef

    def _reset():
        fo._refresher_state.update(active=False, drain=False, busy=False,
                                   last_tick=0.0, last=None, active_since=0.0,
                                   pause_until=0.0, paused=False,
                                   exiting=False, revived=0)
        fo._refresher_thread[0] = None
        fo._refresher_lock_fh[0] = None
        fo._pause_memo.update(read_at=0.0, until=0.0)
        for p in (beat, pausef):
            try:
                os.remove(p)
            except OSError:
                pass
    _reset()
    yield
    _reset()
    fo._REFRESHER_BEAT_FILE, fo._REFRESHER_PAUSE_FILE = alt_b, alt_p


# ── 1. Der Drain pausiert, statt zu töten ───────────────────────────────────

def test_drain_endpoint_pausiert_statt_zu_toeten(monkeypatch):
    """DER VORFALL: der Drain darf `drain` (den Todes-Schalter) nicht mehr
    setzen. Er setzt ein Pause-Fenster — der Loop lebt weiter."""
    import app as backend
    monkeypatch.delenv('ADSB_POLL_SECRET', raising=False)
    monkeypatch.setattr(fo.time, 'time', lambda: NOW)
    fo._refresh_all_state['drain'] = False
    fo._refresh_all_state['running'] = False

    d = backend.app.test_client().post(
        '/api/internal/flightops/refresh-drain').get_json()

    assert d['ok'] is True
    # Der harte Schalter bleibt AUS — nur der atexit-Zwilling darf ihn setzen.
    assert fo._refresher_state['drain'] is False
    assert d['refresher_paused_until'] == pytest.approx(NOW + 600)
    assert fo._refresher_paused(now=NOW) is True
    fo._refresh_all_state['drain'] = False


def test_pause_laeuft_nach_dem_fenster_von_allein_aus():
    """AUTO-RESUME: kommt kein Container-Recreate, nimmt DERSELBE Loop nach
    dem Fenster von selbst wieder auf (Deploy-Recreate dauert normal <5 min,
    das Fenster ist 10 min ⇒ sicher)."""
    fo._refresher_pause(now=NOW)
    assert fo._refresher_paused(now=NOW + 1) is True
    assert fo._refresher_paused(now=NOW + 599) is True
    assert fo._refresher_paused(now=NOW + 601) is False

    # Die Log-Flanke räumt den Zustand auf (Beleg im Container-Log).
    assert fo._refresher_pause_gate(now=NOW + 10) is True
    assert fo._refresher_state['paused'] is True
    assert fo._refresher_pause_gate(now=NOW + 601) is False
    assert fo._refresher_state['paused'] is False
    assert fo._refresher_state['pause_until'] == 0.0


def test_zweiter_drain_verlaengert_das_fenster_nie_verkuerzt():
    """Zwei Deploys hintereinander sollen sich nicht gegenseitig aufheben."""
    fo._refresher_pause(now=NOW)
    fo._refresher_pause(seconds=60, now=NOW + 5)          # kürzer ⇒ ignoriert
    assert fo._refresher_state['pause_until'] == pytest.approx(NOW + 600)
    fo._refresher_pause(now=NOW + 300)                     # später ⇒ gewinnt
    assert fo._refresher_state['pause_until'] == pytest.approx(NOW + 900)


def test_pause_gilt_container_weit_nicht_nur_im_antwortenden_prozess():
    """Der Drain kommt als HTTP-Request und landet auf IRGENDEINEM
    gunicorn-Worker — der Rotations-Thread lebt aber in genau EINEM. Stünde
    die Pause nur im Modul-State des Antwortenden, rotierte der Refresher
    munter weiter (dieselbe Fehlerklasse wie beim Wächter-Fehlalarm)."""
    fo._refresher_pause(now=NOW)
    # Fremder Prozess: Modul-State leer, nur die geteilte Datei ist da.
    fo._refresher_state['pause_until'] = 0.0
    fo._pause_memo.update(read_at=0.0, until=0.0)
    assert fo._refresher_paused(now=NOW + 60) is True
    assert fo._refresher_pause_until(NOW + 60) == pytest.approx(NOW + 600)
    fo._pause_memo.update(read_at=0.0, until=0.0)
    assert fo._refresher_paused(now=NOW + 601) is False


def test_pause_fenster_env_uebersteuerbar_aber_nie_aus(monkeypatch):
    """»Pause aus« gäbe es nicht — das wäre wieder der ewige Tod."""
    monkeypatch.setenv('LH_FLIGHTOPS_DRAIN_PAUSE_S', '120')
    assert fo._refresher_pause_s() == 120
    for bad in ('0', '-5', '99999', 'abc', ''):
        monkeypatch.setenv('LH_FLIGHTOPS_DRAIN_PAUSE_S', bad)
        assert fo._refresher_pause_s() == fo._REFRESHER_PAUSE_S


# ── 2. Grant-Burn-Schutz: während der Pause rotiert NICHTS ──────────────────

def _tick_env(monkeypatch, rotated):
    """Tick-Umgebung ohne LH: ein fälliger Grant, Rotation wird gezählt."""
    monkeypatch.setattr(fo, '_refresher_scan',
                        lambda: [('AT-DUE', {'refresh': 'R',
                                             'expires_at': 0})])
    monkeypatch.setattr(fo, '_refresher_refresh_grant',
                        lambda tok: rotated.append(tok) or 'ok')
    monkeypatch.setattr(fo.time, 'sleep', lambda s: None)
    monkeypatch.setattr(fo, '_refresher_demand', {'AT-DUE'})
    monkeypatch.setattr(fo, '_rot_gate', {})


def test_tick_rotiert_waehrend_der_pause_definitiv_nicht(monkeypatch):
    """DER VERTRAG, der nicht fallen darf: Pause heißt 0 LH-Rotationen.
    Nur der ewige Tod fällt weg, nicht der Grant-Burn-Schutz."""
    rotated = []
    _tick_env(monkeypatch, rotated)

    fo._refresher_pause(now=fo.time.time())
    fo._refresher_tick()
    assert rotated == []
    assert fo._refresher_state['last']['due'] == 1      # fällig war er sehr wohl

    # Nach Ablauf der Pause läuft genau derselbe Loop wieder an (abgelaufenes
    # Fenster in BEIDEN Quellen — Modul-State und geteilte Datei).
    fo._refresher_state['pause_until'] = 0.0
    with open(fo._REFRESHER_PAUSE_FILE, 'w') as f:
        f.write('{"until": 0}')
    fo._pause_memo.update(read_at=0.0, until=0.0)
    fo._refresher_tick()
    assert rotated == ['AT-DUE']


# ── 3. Der harte Schalter bleibt dem Prozess-Ende vorbehalten ───────────────

def test_exit_drain_bleibt_der_harte_schalter_und_sperrt_revive():
    """atexit/SIGTERM: DORT ist der Thread-Tod richtig (der Prozess geht
    ohnehin) — und der Wächter darf im Sterben nichts hochziehen."""
    class _Th:
        def __init__(self):
            self._alive = True

        def is_alive(self):
            return self._alive

        def join(self, timeout=None):
            self._alive = False
    fo._refresher_thread[0] = _Th()
    fo._refresher_exit_drain()
    assert fo._refresher_state['drain'] is True
    assert fo._refresher_state['exiting'] is True
    assert fo._refresher_revive(now=NOW + 10 ** 6) is False


# ── 4. Wächter: Thread tot + Container alt ⇒ in-process neu starten ─────────

def test_revive_nur_bei_altem_container(monkeypatch):
    """Ein junger Container steckt evtl. mitten in einem Deploy — dort darf
    der Wächter nicht gegen den Recreate anlaufen."""
    monkeypatch.setenv('LH_FLIGHTOPS_REFRESHER', '1')
    started = []
    monkeypatch.setattr(fo, '_maybe_start_refresher',
                        lambda: started.append(1) or 'TH')
    monkeypatch.setattr(fo, '_REFRESHER_BOOT_TS', NOW)

    assert fo._refresher_revive(now=NOW + 32) is False       # 32 s — der Vorfall
    assert started == []
    assert fo._refresher_revive(now=NOW + 6 * 60) is True
    assert started == [1]
    assert fo._refresher_state['revived'] == 1
    assert fo._refresher_state['drain'] is False
    assert fo._refresher_state['pause_until'] == 0.0


def test_revive_laesst_lebenden_loop_und_fremde_rolle_in_ruhe(monkeypatch):
    started = []
    monkeypatch.setattr(fo, '_maybe_start_refresher',
                        lambda: started.append(1) or 'TH')
    monkeypatch.setattr(fo, '_REFRESHER_BOOT_TS', NOW)

    monkeypatch.delenv('LH_FLIGHTOPS_REFRESHER', raising=False)
    assert fo._refresher_revive(now=NOW + 10 ** 6) is False   # falsche Rolle

    monkeypatch.setenv('LH_FLIGHTOPS_REFRESHER', '1')

    class _Alive:
        def is_alive(self):
            return True
    fo._refresher_thread[0] = _Alive()
    assert fo._refresher_revive(now=NOW + 10 ** 6) is False   # lebt noch
    assert started == []


def test_revive_setzt_die_rotations_bremse_nicht_zurueck(monkeypatch):
    """Wiederanlauf hebt genau die zwei Stillstands-Schalter auf — der
    exponentielle Rückzug pro Grant (_rot_gate) bleibt stehen, sonst wäre die
    Selbstheilung ein neuer Grant-Burn-Pfad."""
    monkeypatch.setenv('LH_FLIGHTOPS_REFRESHER', '1')
    monkeypatch.setattr(fo, '_maybe_start_refresher', lambda: 'TH')
    monkeypatch.setattr(fo, '_REFRESHER_BOOT_TS', NOW)
    monkeypatch.setattr(fo, '_rot_gate', {'AT-X': {'last': NOW, 'fails': 4}})

    assert fo._refresher_revive(now=NOW + 6 * 60) is True
    assert fo._rot_gate['AT-X']['fails'] == 4
    assert fo._rot_gate_ok('AT-X', NOW + 6 * 60) is False


# ── 5. Der Wächter urteilt aus dem HERZSCHLAG, nicht aus Modul-State ────────
# FEHLALARM 31.07. (live reproduziert): derselbe Wächter meldete auf :8081
# `reasons: []` (der Thread tickte nachweislich, scan=1015) und auf :8080
# einen ALARM — weil `_refresher_state['active']` MODUL-State ist und nur im
# thread-tragenden Prozess wahr. Ein Wächter, der falsch alarmiert, wird
# ignoriert und ist beim echten Ausfall wertlos.

def _watch_env(monkeypatch, tmp_path):
    import app as backend
    monkeypatch.setattr(fo, '_RELOGIN_WATCH_STATE', str(tmp_path / 'w.json'))
    monkeypatch.delenv('ADSB_POLL_SECRET', raising=False)
    monkeypatch.setattr(fo, '_relogin_count', lambda: 3)
    mails = []
    monkeypatch.setattr(fo, '_fo_watch_alert_mail',
                        lambda reasons, cnt, delta: mails.append(reasons) or True)
    return backend.app.test_client(), mails


def test_health_liest_den_herzschlag_prozessunabhaengig():
    now = 1_800_000_000.0
    assert fo._refresher_health(now=now)['state'] == 'never'
    fo._refresher_beat_write(now=now)
    assert fo._refresher_health(now=now + 60)['state'] == 'alive'
    assert fo._refresher_health(now=now + 60)['beat_age_s'] == 60.0
    # Ein langer Tick (gemessen 12,6 min für 236 Rotationen) ist noch am Leben.
    assert fo._refresher_health(now=now + 13 * 60)['state'] == 'alive'
    assert fo._refresher_health(now=now + 16 * 60)['state'] == 'stale'
    # Pause ist ein EIGENER Zustand, nicht „tot".
    fo._refresher_pause(now=now)
    fo._refresher_beat_write(now=now)
    assert fo._refresher_health(now=now + 60)['state'] == 'paused'


def test_waechter_meldet_ok_solange_der_herzschlag_schlaegt(monkeypatch,
                                                            tmp_path):
    """DER FEHLALARM: der Thread lebt in einem ANDEREN Prozess desselben
    Containers (gunicorn-Worker-Recycle) — `active` ist hier False, der
    Herzschlag aber frisch. Kein Alarm, keine Wiederbelebung."""
    c, mails = _watch_env(monkeypatch, tmp_path)
    monkeypatch.setenv('LH_FLIGHTOPS_REFRESHER', '1')
    monkeypatch.setattr(fo, '_REFRESHER_BOOT_TS', 0.0)   # uralter Prozess
    started = []
    monkeypatch.setattr(fo, '_maybe_start_refresher',
                        lambda: started.append(1) or 'TH')
    fo._refresher_beat_write()                            # fremder Prozess
    assert fo._refresher_state['active'] is False         # dieser hier: nichts

    d = c.post('/api/internal/flightops/relogin-watch').get_json()

    assert d['reasons'] == []
    assert mails == [] and started == []
    assert d['refresher']['state'] == 'alive'
    assert d['refresher']['active_in_this_process'] is False


def test_waechter_ohne_rolle_behauptet_nichts(monkeypatch, tmp_path):
    """LIVE REPRODUZIERT 31.07. 17:07 auf :8080: der Web-Container trägt die
    Refresher-Rolle ABSICHTLICH nicht und mailte trotzdem „NIEMAND rotiert".
    Er kann es gar nicht wissen (der Herzschlag ist Container-lokal) — also
    behauptet er nichts."""
    c, mails = _watch_env(monkeypatch, tmp_path)
    monkeypatch.delenv('LH_FLIGHTOPS_REFRESHER', raising=False)
    monkeypatch.setattr(fo, '_KEY', 'k')
    monkeypatch.setattr(fo, '_SECRET', 's')
    assert fo.flightops_configured() is True

    d = c.post('/api/internal/flightops/relogin-watch').get_json()

    assert d['reasons'] == []
    assert mails == []
    assert d['refresher']['state'] == 'not_my_role'


def test_waechter_meldet_toten_loop_und_belebt_ihn(monkeypatch, tmp_path):
    """Ende-zu-Ende: ein ALTER Herzschlag ist der echte Ausfall (Vorfall
    31.07. 12:56). Er bleibt ein gemeldeter Vorfall — wird aber zusätzlich
    geheilt, statt auf einen manuellen docker restart zu warten."""
    c, mails = _watch_env(monkeypatch, tmp_path)
    monkeypatch.setenv('LH_FLIGHTOPS_REFRESHER', '1')
    monkeypatch.setattr(fo, '_REFRESHER_BOOT_TS', 0.0)     # uralter Container
    started = []
    monkeypatch.setattr(fo, '_maybe_start_refresher',
                        lambda: started.append(1) or 'TH')
    fo._refresher_beat_write(now=fo.time.time() - 30 * 60)  # 30 min alt

    d = c.post('/api/internal/flightops/relogin-watch').get_json()

    assert any('steht' in r and 'WIEDERBELEBT' in r for r in d['reasons'])
    assert d['refresher']['state'] == 'stale'
    assert d['refresher']['revived_now'] is True
    assert started == [1] and mails


def test_waechter_alarmiert_nicht_wegen_einer_deploy_pause(monkeypatch,
                                                           tmp_path):
    """Eine laufende Deploy-Pause ist der GEPLANTE Zustand — der Wächter zeigt
    sie an, alarmiert aber nicht (sonst mailt jeder Deploy)."""
    c, mails = _watch_env(monkeypatch, tmp_path)
    monkeypatch.setenv('LH_FLIGHTOPS_REFRESHER', '1')
    monkeypatch.setattr(fo, '_REFRESHER_BOOT_TS', 0.0)
    fo._refresher_pause()
    fo._refresher_beat_write()

    d = c.post('/api/internal/flightops/relogin-watch').get_json()

    assert d['reasons'] == []
    assert mails == []
    assert d['refresher']['state'] == 'paused'
    assert d['refresher']['paused_until'] > 0


def test_waechter_boot_karenz_schweigt_beim_frischen_container(monkeypatch,
                                                               tmp_path):
    """Fehlalarm 27.07.: der Cron traf 38 s nach dem Containerstart. Ohne
    JEMALS einen Schlag gibt es nichts zu vergleichen."""
    c, mails = _watch_env(monkeypatch, tmp_path)
    monkeypatch.setenv('LH_FLIGHTOPS_REFRESHER', '1')
    monkeypatch.setattr(fo, '_REFRESHER_BOOT_TS', fo.time.time() - 38)

    d = c.post('/api/internal/flightops/relogin-watch').get_json()

    assert d['reasons'] == [] and mails == []
    assert d['refresher']['state'] == 'never'


def test_revive_greift_nicht_bei_frischem_fremden_herzschlag(monkeypatch):
    """Ein zweiter Loop im selben Container wäre bestenfalls ein
    flock-Wartezimmer, schlimmstenfalls ein zweiter Rotierer."""
    monkeypatch.setenv('LH_FLIGHTOPS_REFRESHER', '1')
    monkeypatch.setattr(fo, '_REFRESHER_BOOT_TS', 0.0)
    started = []
    monkeypatch.setattr(fo, '_maybe_start_refresher',
                        lambda: started.append(1) or 'TH')
    fo._refresher_beat_write()
    assert fo._refresher_revive(now=fo.time.time()) is False
    assert started == []
