"""LH-Open-API-Nachfrage-Diät (Owner 2026-07-30: „App meldet über dem Limit").

AUSGANGSMESSUNG (`ax_api_budget`, 2026-07-29 18 UTC … 2026-07-30 17 UTC):
    gesendet 19.140 · vom eigenen Gate ABGEWIESEN 29.562 · gewollt 48.702
    jede der 24 Stunden am Deckel · davon rein spekulative Warms 22.655
    darunter 1.009 abgewiesene mqtt_event/mqtt_inbound = Live-Activity-Updates,
    die der User auf dem Sperrbildschirm nicht bekommen hat.
Dem gegenüber: im ganzen Warm-Fenster (heute−1 … heute+2) existieren über alle
1.419 Roster zusammen nur 1.860 verschiedene LH-Group-Legs. Jedes Leg wurde
also ~26× pro Tag gekauft.

Diese Datei prüft die vier Regeln, die daraus folgen:
  1. gelandet ⇒ Fakten sind FINAL, werden nie wieder gewärmt
  2. derselbe Flug wird pro Stunde höchstens einmal spekulativ gewärmt
  3. bei knappem Budget verhungern Warms ZUERST, User-Pfade zuletzt
  4. was ein anderer Prozess schon bezahlt hat, wird nicht zweimal gekauft

Rein offline: kein Netz, kein Key, kein Supabase (`_shared_sb` ist unter pytest
hart None und wird hier gezielt auf ein Fake gesetzt).
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blueprints import lh_open_api as lh


HOUR = 3600


def _iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _reset(monkeypatch):
    """Alle prozess-lokalen Zustände auf Null — sonst färbt ein Test in den
    nächsten (die Ledger sind bewusst modulweit)."""
    lh._facts_memo.clear()
    lh._warm_hour_ledger.clear()
    lh._warm_inflight.clear()
    lh._warm_gate_closed_hour = -1
    lh._budget_buf.clear()
    lh._global_hour = 0
    lh._global_count = 0
    lh._global_local_since = 0
    lh._class_warned.clear()
    lh._hour_window = 0
    lh._hour_count = 0
    lh._rate_penalty_until = 0.0
    monkeypatch.setattr(lh, '_shared_sb', lambda: None)
    monkeypatch.setattr(lh, 'budget_inc', lambda *a, **k: None)


# ─────────────────────────────────────────────────────────────────────────────
# 1) FINAL — ein gelandeter Flug ändert sich nie wieder
# ─────────────────────────────────────────────────────────────────────────────

def test_landed_flight_is_final_via_lh_status():
    """LH sagt selbst „Flight Landed" und die Ankunft ist vorbei — das ist
    keine Heuristik, sondern die Aussage der operierenden Airline."""
    now = time.time()
    facts = {'sched_dep': _iso(now - 5 * HOUR), 'est_dep': _iso(now - 5 * HOUR),
             'sched_arr': _iso(now - 2 * HOUR), 'est_arr': _iso(now - 2 * HOUR),
             'dep_status': 'Flight Landed'}
    assert lh._facts_final(facts, now) is True


def test_long_after_arrival_is_final_even_without_status():
    now = time.time()
    facts = {'sched_dep': _iso(now - 12 * HOUR), 'est_dep': _iso(now - 12 * HOUR),
             'sched_arr': _iso(now - 9 * HOUR), 'est_arr': _iso(now - 9 * HOUR)}
    assert lh._facts_final(facts, now) is True


def test_delayed_flight_is_not_final_just_because_sched_arr_passed():
    """Der Klassiker, an dem eine reine Ankunfts-Prüfung scheitert: die
    SOLL-Ankunft ist längst vorbei, der Flug ist aber noch gar nicht weg."""
    now = time.time()
    facts = {'sched_dep': _iso(now - 8 * HOUR), 'est_dep': _iso(now + 1 * HOUR),
             'sched_arr': _iso(now - 7 * HOUR), 'est_arr': _iso(now + 3 * HOUR)}
    assert lh._facts_final(facts, now) is False


def test_airborne_longhaul_is_not_final_two_hours_after_takeoff():
    now = time.time()
    facts = {'sched_dep': _iso(now - 3 * HOUR), 'est_dep': _iso(now - 3 * HOUR),
             'sched_arr': _iso(now + 6 * HOUR), 'est_arr': _iso(now + 6 * HOUR),
             'dep_status': 'Flight Departed'}
    assert lh._facts_final(facts, now) is False


def test_final_facts_get_the_long_ttl():
    """Der eigentliche Hebel: statt 30 min (`_TTL_DONE`) bzw. 6 h
    (`_TTL_OTHER_DAY`) gilt eine finale Antwort 14 Tage."""
    now = time.time()
    day = time.strftime('%Y-%m-%d', time.gmtime(now))
    facts = {'sched_dep': _iso(now - 11 * HOUR), 'est_dep': _iso(now - 11 * HOUR),
             'sched_arr': _iso(now - 8 * HOUR), 'est_arr': _iso(now - 8 * HOUR)}
    assert lh._facts_ttl(day, facts, now) == lh._TTL_FINAL
    assert lh._TTL_FINAL > lh._TTL_OTHER_DAY


def test_landed_flight_is_never_warmed_again(monkeypatch):
    """Regel 1 am Warm-Eingang: liegen finale Fakten im Memo, wird gar nichts
    mehr eingereiht — kein Thread, kein Supabase-Read, kein LH-Call."""
    _reset(monkeypatch)
    now = time.time()
    day = time.strftime('%Y-%m-%d', time.gmtime(now))
    key = ('LH400', day, 'FRA', 'JFK')
    lh._facts_memo[key] = (now + lh._TTL_FINAL, {
        'sched_dep': _iso(now - 11 * HOUR), 'est_dep': _iso(now - 11 * HOUR),
        'sched_arr': _iso(now - 8 * HOUR), 'est_arr': _iso(now - 8 * HOUR),
        'dep_status': 'Flight Landed'}, True)
    queued = []
    monkeypatch.setattr(lh, '_warm_workers_start', lambda: queued.append('start'))
    skips = []
    monkeypatch.setattr(lh, '_warm_skip', lambda r: skips.append(r))
    lh._warm_async('LH400', day, 'FRA', 'JFK', 'obs_overlay')
    assert queued == []
    assert skips == ['final']


# ─────────────────────────────────────────────────────────────────────────────
# 2) DEDUP + HORIZONT — dieselbe Frage nicht 26× am Tag
# ─────────────────────────────────────────────────────────────────────────────

def test_same_flight_is_warmed_only_once_per_hour(monkeypatch):
    _reset(monkeypatch)
    now = time.time()
    day = time.strftime('%Y-%m-%d', time.gmtime(now))
    monkeypatch.setattr(lh, '_warm_workers_start', lambda: None)

    class _Q:
        def __init__(self):
            self.items = []

        def put_nowait(self, it):
            self.items.append(it)

    q = _Q()
    monkeypatch.setattr(lh, '_warm_q', q)
    skips = []
    monkeypatch.setattr(lh, '_warm_skip', lambda r: skips.append(r))
    for _ in range(12):
        lh._warm_inflight.clear()          # als wären die Warms fertig
        lh._warm_async('LH400', day, 'FRA', 'JFK', 'obs_overlay')
    assert len(q.items) == 1, 'ein Warm pro Stunde und Flug'
    assert skips.count('dedup_hour') == 11


def test_ledger_lets_the_flight_through_again_in_the_next_hour(monkeypatch):
    _reset(monkeypatch)
    now = time.time()
    key = ('LH400', '2026-07-30', 'FRA', 'JFK')
    assert lh._warm_ledger_ok(key, now) is True
    assert lh._warm_ledger_ok(key, now) is False
    assert lh._warm_ledger_ok(key, now + HOUR) is True


def test_operating_window_flights_bypass_the_hourly_dedup(monkeypatch):
    """AUSNAHME MIT ABSICHT: ein Flug im Betriebsfenster hat 120 s TTL, weil
    Gate und Ist-Zeiten auf offenen Karten frisch sein MÜSSEN. Ihn auf einen
    Warm pro Stunde zu drosseln wäre genau die sichtbare Verschlechterung,
    die es nicht geben darf."""
    _reset(monkeypatch)
    now = time.time()
    day = time.strftime('%Y-%m-%d', time.gmtime(now))
    key = ('LH400', day, 'FRA', 'JFK')
    lh._facts_memo[key] = (now + 120, {
        'sched_dep': _iso(now + 20 * 60), 'est_dep': _iso(now + 25 * 60),
        'sched_arr': _iso(now + 3 * HOUR), 'est_arr': _iso(now + 3 * HOUR)}, True)
    monkeypatch.setattr(lh, '_warm_workers_start', lambda: None)

    class _Q:
        def __init__(self):
            self.items = []

        def put_nowait(self, it):
            self.items.append(it)

    q = _Q()
    monkeypatch.setattr(lh, '_warm_q', q)
    for _ in range(3):
        lh._warm_inflight.clear()
        lh._warm_async('LH400', day, 'FRA', 'JFK', 'obs_overlay')
    assert len(q.items) == 3


def test_warm_horizon_rejects_history_and_far_future():
    now = time.time()
    import datetime as _dt
    today = _dt.datetime.utcfromtimestamp(now).date()

    def day(k):
        return (today + _dt.timedelta(days=k)).isoformat()

    assert lh._warm_horizon_ok(day(-1), now) is True
    assert lh._warm_horizon_ok(day(0), now) is True
    assert lh._warm_horizon_ok(day(2), now) is True
    assert lh._warm_horizon_ok(day(-2), now) is False   # Historie: Warehouse
    assert lh._warm_horizon_ok(day(3), now) is False
    assert lh._warm_horizon_ok('kaputt', now) is False


def test_time_horizon_drops_flights_that_are_long_gone():
    now = time.time()
    gone = {'sched_dep': _iso(now - 20 * HOUR), 'sched_arr': _iso(now - 15 * HOUR)}
    assert lh._warm_time_horizon_ok(gone, now) is False
    soon = {'sched_dep': _iso(now + 2 * HOUR), 'sched_arr': _iso(now + 5 * HOUR)}
    assert lh._warm_time_horizon_ok(soon, now) is True
    assert lh._warm_time_horizon_ok({}, now) is True


def test_history_day_is_not_warmed(monkeypatch):
    _reset(monkeypatch)
    import datetime as _dt
    now = time.time()
    old = (_dt.datetime.utcfromtimestamp(now).date()
           - _dt.timedelta(days=5)).isoformat()
    started = []
    monkeypatch.setattr(lh, '_warm_workers_start', lambda: started.append(1))
    skips = []
    monkeypatch.setattr(lh, '_warm_skip', lambda r: skips.append(r))
    lh._warm_async('LH400', old, 'FRA', 'JFK', 'obs_overlay')
    assert started == []
    assert skips == ['horizon']


# ─────────────────────────────────────────────────────────────────────────────
# 3) PRIORITÄT — bei knappem Budget verhungern Warms zuerst
# ─────────────────────────────────────────────────────────────────────────────

def test_caller_classes():
    assert lh._caller_class('warm_obs_overlay') == lh._CLASS_WARM
    assert lh._caller_class('warm') == lh._CLASS_WARM
    assert lh._caller_class('mqtt_leg_reg') == lh._CLASS_BG
    assert lh._caller_class('obs_merge') == lh._CLASS_USER
    assert lh._caller_class('mqtt_event') == lh._CLASS_USER
    assert lh._caller_class('mqtt_inbound') == lh._CLASS_USER
    # Unbekanntes gilt als user: ein neuer Aufrufer soll lieber zu VIEL
    # Budget bekommen als still hinter Spekulation zurückzustehen.
    assert lh._caller_class('brandneu') == lh._CLASS_USER
    assert lh._caller_class(None) == lh._CLASS_USER


def test_class_ceilings_are_ordered():
    assert (lh._CLASS_SHARE[lh._CLASS_WARM]
            < lh._CLASS_SHARE[lh._CLASS_BG]
            < lh._CLASS_SHARE[lh._CLASS_USER] <= 0.95)


def test_warm_starves_before_user_paths(monkeypatch):
    """DER Kern der Owner-Beschwerde: 1.009 abgewiesene Live-Activity-Calls
    in 24 h, während 9.986 Warms durchgingen. Bei 70 % Stundenstand muss der
    Warm draussen bleiben und der User-Pfad durchkommen."""
    _reset(monkeypatch)
    now = time.time()
    h = time.strftime('%Y%m%d%H', time.gmtime(now))
    lh.note_global_budget(h, int(lh._GLOBAL_HOUR_BUDGET * 0.70))
    assert lh._global_budget_check(now, caller='warm_obs_overlay') is False
    assert lh._global_budget_check(now, caller='mqtt_event') is True
    assert lh._global_budget_check(now, caller='obs_merge') is True
    assert lh._global_budget_check(now, caller='mqtt_leg_reg') is True


def test_background_starves_before_user_paths(monkeypatch):
    _reset(monkeypatch)
    now = time.time()
    h = time.strftime('%Y%m%d%H', time.gmtime(now))
    lh.note_global_budget(h, int(lh._GLOBAL_HOUR_BUDGET * 0.90))
    assert lh._global_budget_check(now, caller='warm_obs_merge') is False
    assert lh._global_budget_check(now, caller='mqtt_leg_reg') is False
    assert lh._global_budget_check(now, caller='mqtt_event') is True


def test_user_class_still_stops_before_the_hard_wall(monkeypatch):
    """Auch User-Calls bekommen KEINE 100 % — sonst überzieht der Key
    zwischen zwei 30-s-Flushes wieder über 1.000."""
    _reset(monkeypatch)
    now = time.time()
    h = time.strftime('%Y%m%d%H', time.gmtime(now))
    lh.note_global_budget(h, int(lh._GLOBAL_HOUR_BUDGET * 0.96))
    assert lh._global_budget_check(now, caller='mqtt_event') is False


def test_budget_ok_applies_the_class_cap_per_process(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(lh, '_MIN_INTERVAL', 0.0)
    lh._hour_window = int(time.time() // 3600)
    lh._hour_count = lh._class_cap(lh._HOUR_BUDGET, 'warm') + 1
    assert lh._budget_ok('warm_obs_overlay') is False
    assert lh._budget_ok('mqtt_event') is True


def test_get_passes_the_caller_into_the_gate(monkeypatch):
    """Regressions-Schranke: ginge der Aufrufer beim Gate verloren, wären
    alle Klassen wieder gleichberechtigt und der Umbau wirkungslos."""
    _reset(monkeypatch)
    seen = []
    monkeypatch.setattr(lh, '_token', lambda: 'tok')
    monkeypatch.setattr(lh, '_budget_ok',
                        lambda caller=None: seen.append(caller) or False)
    lh._get('/x', caller='warm_obs_overlay')
    assert seen == ['warm_obs_overlay']


def test_closed_warm_gate_stops_further_warms_for_the_hour(monkeypatch):
    """Die 12.669 abgewiesenen Warms in 24 h waren selbst Last und
    Log-Rauschen: ist die Warm-Decke einmal erreicht, gilt das für den REST
    der Stunde — kein weiterer Versuch, keine weitere Denial-Zeile."""
    _reset(monkeypatch)
    now = time.time()
    day = time.strftime('%Y-%m-%d', time.gmtime(now))
    lh._warm_gate_closed_hour = int(now // 3600)
    started = []
    monkeypatch.setattr(lh, '_warm_workers_start', lambda: started.append(1))
    skips = []
    monkeypatch.setattr(lh, '_warm_skip', lambda r: skips.append(r))
    lh._warm_async('LH400', day, 'FRA', 'JFK', 'obs_overlay')
    assert started == []
    assert skips == ['gate_closed']


# ─────────────────────────────────────────────────────────────────────────────
# 4) GETEILTER CACHE — was ein Prozess bezahlt hat, gilt für alle
# ─────────────────────────────────────────────────────────────────────────────

class _FakeTable:
    def __init__(self, store, log):
        self.store, self.log, self._keys = store, log, []

    def select(self, *a, **k):
        return self

    def in_(self, _col, keys):
        self._keys = list(keys)
        return self

    def upsert(self, rows, on_conflict=None):
        self.log.append(('upsert', list(rows)))
        for r in rows:
            self.store[r['call_key']] = r
        return self

    def execute(self):
        return type('R', (), {'data': [self.store[k] for k in self._keys
                                       if k in self.store]})()


class _FakeSB:
    def __init__(self):
        self.store, self.log = {}, []

    def table(self, _name):
        return _FakeTable(self.store, self.log)


def test_shared_cache_hit_costs_no_lh_call(monkeypatch):
    """Der Hebel gegen 4 Worker × Deploy-Reset: derselbe Flug wird nicht
    fünfmal gekauft."""
    _reset(monkeypatch)
    fake = _FakeSB()
    monkeypatch.setattr(lh, '_shared_sb', lambda: fake)
    now = time.time()
    fake.store[lh._shared_key('lhfacts', 'LH400', '2026-07-30', 'FRA', 'JFK')] = {
        'call_key': lh._shared_key('lhfacts', 'LH400', '2026-07-30', 'FRA', 'JFK'),
        'result': {'facts': {'reg': 'D-AIHY', 'gate': 'Z16'}, 'answered': True},
        'result_until': _iso(now + 3600), 'negative_until': None}
    got = []
    monkeypatch.setattr(lh, '_get', lambda *a, **k: got.append(a) or None)
    monkeypatch.setattr(lh, 'lh_open_configured', lambda: True)
    facts = lh.lh_flight_facts('LH400', '2026-07-30', 'FRA', 'JFK',
                               caller='obs_merge')
    assert facts == {'reg': 'D-AIHY', 'gate': 'Z16'}
    assert got == [], 'geteilter Treffer darf KEINEN LH-Call kosten'
    # und liegt danach auch im Prozess-Memo
    assert ('LH400', '2026-07-30', 'FRA', 'JFK') in lh._facts_memo


def test_expired_shared_row_is_a_miss(monkeypatch):
    _reset(monkeypatch)
    fake = _FakeSB()
    monkeypatch.setattr(lh, '_shared_sb', lambda: fake)
    now = time.time()
    k = lh._shared_key('lhfacts', 'LH400', '2026-07-30', 'FRA', 'JFK')
    fake.store[k] = {'call_key': k, 'result': {'facts': {'reg': 'X'},
                                               'answered': True},
                     'result_until': _iso(now - 60), 'negative_until': None}
    out = lh._shared_read('LH400', '2026-07-30', 'FRA', 'JFK')
    assert out['facts'] is None and out['ttl'] == 0


def test_force_bypasses_the_shared_cache(monkeypatch):
    """MQTT-Push heisst „hat sich GERADE geändert" — der Pfad muss frischer
    sein dürfen als jeder Cache."""
    _reset(monkeypatch)
    fake = _FakeSB()
    monkeypatch.setattr(lh, '_shared_sb', lambda: fake)
    now = time.time()
    k = lh._shared_key('lhfacts', 'LH400', '2026-07-30', 'FRA', 'JFK')
    fake.store[k] = {'call_key': k, 'result': {'facts': {'reg': 'ALT'},
                                               'answered': True},
                     'result_until': _iso(now + 3600), 'negative_until': None}
    monkeypatch.setattr(lh, 'lh_open_configured', lambda: True)
    calls = []
    monkeypatch.setattr(lh, '_get', lambda *a, **k2: calls.append(a) or None)
    lh.lh_flight_facts('LH400', '2026-07-30', 'FRA', 'JFK', force=True,
                       caller='mqtt_event')
    assert len(calls) == 1


def test_gaps_are_never_written_to_the_shared_cache(monkeypatch):
    """Ein 503 oder eine Throttle-Abweisung ist „wir wissen es nicht".
    Landete das als Antwort im geteilten Cache, gälte der Ausfall
    prozessübergreifend als Fakt."""
    _reset(monkeypatch)
    fake = _FakeSB()
    monkeypatch.setattr(lh, '_shared_sb', lambda: fake)
    lh._shared_write_facts([('LH400', '2026-07-30', 'FRA', 'JFK')],
                           {}, False, 3600)
    assert fake.log == []
    # und eine Mikro-TTL lohnt den Roundtrip nicht
    lh._shared_write_facts([('LH400', '2026-07-30', 'FRA', 'JFK')],
                           {'reg': 'D-AIHY'}, True, 5)
    assert fake.log == []
    lh._shared_write_facts([('LH400', '2026-07-30', 'FRA', 'JFK')],
                           {'reg': 'D-AIHY'}, True, 3600)
    assert len(fake.log) == 1


def test_shared_warm_block_survives_a_deploy(monkeypatch):
    """Der Grund für die persistente Sperre: nach einem Deploy sind alle
    Prozess-Ledger leer — ohne geteilte Sperre startet die abgewiesene Salve
    sofort neu."""
    _reset(monkeypatch)
    fake = _FakeSB()
    monkeypatch.setattr(lh, '_shared_sb', lambda: fake)
    lh._shared_write_warm_block('LH400', '2026-07-30', 'FRA', 'JFK', 1800,
                                'budget')
    out = lh._shared_read('LH400', '2026-07-30', 'FRA', 'JFK', with_warm=True)
    assert out['warm_block'] > 0
    # frischer Prozess: Ledger leer, Sperre trotzdem wirksam
    lh._warm_hour_ledger.clear()
    skips = []
    monkeypatch.setattr(lh, '_warm_skip', lambda r: skips.append(r))
    called = []
    monkeypatch.setattr(lh, 'lh_flight_facts',
                        lambda *a, **k: called.append(a))
    lh._warm_one('LH400', '2026-07-30', 'FRA', 'JFK', 'warm_obs_overlay')
    assert called == []
    assert skips == ['shared_block']


def test_warm_one_uses_the_shared_hit_instead_of_lh(monkeypatch):
    _reset(monkeypatch)
    fake = _FakeSB()
    monkeypatch.setattr(lh, '_shared_sb', lambda: fake)
    now = time.time()
    k = lh._shared_key('lhfacts', 'LH400', '2026-07-30', 'FRA', 'JFK')
    fake.store[k] = {'call_key': k,
                     'result': {'facts': {'reg': 'D-AIHY'}, 'answered': True},
                     'result_until': _iso(now + 1800), 'negative_until': None}
    called = []
    monkeypatch.setattr(lh, 'lh_flight_facts', lambda *a, **kw: called.append(a))
    skips = []
    monkeypatch.setattr(lh, '_warm_skip', lambda r: skips.append(r))
    lh._warm_one('LH400', '2026-07-30', 'FRA', 'JFK', 'warm_obs_overlay')
    assert called == []
    assert skips == ['shared_hit']
    assert lh._facts_memo[('LH400', '2026-07-30', 'FRA', 'JFK')][1] == {
        'reg': 'D-AIHY'}


def test_denied_warm_writes_a_block_and_closes_the_gate(monkeypatch):
    _reset(monkeypatch)
    fake = _FakeSB()
    monkeypatch.setattr(lh, '_shared_sb', lambda: fake)
    monkeypatch.setattr(lh, 'lh_flight_facts', lambda *a, **kw: {})
    monkeypatch.setattr(lh, 'last_call_denied', lambda: True)
    lh._warm_one('LH400', '2026-07-30', 'FRA', 'JFK', 'warm_obs_overlay')
    assert lh._warm_gate_closed_hour == int(time.time() // 3600)
    keys = [r['call_key'] for _op, rows in fake.log for r in rows]
    assert any(k.startswith('lhwarm:') for k in keys)


def test_seconds_to_next_hour_is_positive_and_bounded():
    now = time.time()
    s = lh._seconds_to_next_hour(now)
    assert 0 < s <= 3605


# ─────────────────────────────────────────────────────────────────────────────
# Sichtbare Flächen: der Umbau darf nichts wegnehmen
# ─────────────────────────────────────────────────────────────────────────────

def test_shared_cache_write_uses_the_same_ttl_as_the_process_memo(monkeypatch):
    """Wäre der geteilte Cache länger oder kürzer gültig als das lokale Memo,
    hinge die Frische davon ab, welcher gunicorn-Worker die Anfrage bekommt."""
    _reset(monkeypatch)
    fake = _FakeSB()
    monkeypatch.setattr(lh, '_shared_sb', lambda: fake)
    monkeypatch.setattr(lh, 'lh_open_configured', lambda: True)
    monkeypatch.setattr(lh, '_budget_ok', lambda *a, **k: True)
    monkeypatch.setattr(lh, '_token', lambda: 'tok')
    now = time.time()
    day = time.strftime('%Y-%m-%d', time.gmtime(now))
    monkeypatch.setattr(lh, '_get', lambda *a, **k: {
        'FlightStatusResource': {'Flights': {'Flight': {
            'Departure': {'AirportCode': 'FRA',
                          'ScheduledTimeLocal': {'DateTime': day + 'T10:00'},
                          'ScheduledTimeUTC': {'DateTime': day + 'T08:00Z'}},
            'Arrival': {'AirportCode': 'JFK',
                        'ScheduledTimeLocal': {'DateTime': day + 'T13:00'},
                        'ScheduledTimeUTC': {'DateTime': day + 'T17:00Z'}},
            'Equipment': {'AircraftCode': '346',
                          'AircraftRegistration': 'DAIHY'},
            'FlightStatus': {'Code': 'NA', 'Definition': 'No status'}}}}})
    lh.lh_flight_facts('LH400', day, 'FRA', 'JFK', caller='obs_merge')
    memo_exp = lh._facts_memo[('LH400', day, 'FRA', 'JFK')][0]
    rows = [r for _op, rr in fake.log for r in rr
            if r['call_key'].startswith('lhfacts:')]
    assert rows, 'Antwort muss prozessübergreifend abgelegt werden'
    from datetime import datetime as _dt
    shared_exp = _dt.fromisoformat(rows[0]['result_until']).timestamp()
    assert abs(shared_exp - memo_exp) < 5


def test_user_paths_are_untouched_when_budget_is_free(monkeypatch):
    """Ohne Knappheit verhält sich alles wie vorher — der Umbau greift nur,
    wenn es eng wird."""
    _reset(monkeypatch)
    now = time.time()
    h = time.strftime('%Y%m%d%H', time.gmtime(now))
    lh.note_global_budget(h, 10)
    for c in ('obs_merge', 'mqtt_event', 'mqtt_inbound', 'mqtt_leg_reg',
              'warm_obs_overlay', 'warm_obs_merge'):
        assert lh._global_budget_check(now, caller=c) is True


# ─────────────────────────────────────────────────────────────────────────────
# Aufräumen: der geteilte Cache darf nicht rein additiv wachsen
# ─────────────────────────────────────────────────────────────────────────────

class _FakeDelete:
    def __init__(self, log):
        self.log, self.f = log, {}

    def like(self, col, val):
        self.f['like'] = (col, val)
        return self

    def lt(self, col, val):
        self.f.setdefault('lt', []).append(col)
        return self

    def execute(self):
        self.log.append(dict(self.f))
        return type('R', (), {'data': []})()


class _FakeDelTable:
    def __init__(self, log):
        self.log = log

    def delete(self):
        return _FakeDelete(self.log)


class _FakeDelSB:
    def __init__(self):
        self.log = []

    def table(self, _n):
        return _FakeDelTable(self.log)


def test_prune_only_touches_our_own_keys(monkeypatch):
    """Der Reg-Cache des MQTT-Daemons (`lhreg:`) gehört lh_mqtt.py und hat
    eine eigene Politik — ihn mitzulöschen wäre eine stille Nebenwirkung."""
    _reset(monkeypatch)
    fake = _FakeDelSB()
    monkeypatch.setattr(lh, '_shared_sb', lambda: fake)
    lh._shared_prune()
    prefixes = [c['like'][1] for c in fake.log]
    assert prefixes == ['lhfacts:%', 'lhwarm:%']
    assert not any(p.startswith('lhreg') for p in prefixes)
    # und immer BEIDE Schranken: abgelaufen UND seit über einem Tag kalt
    for c in fake.log:
        assert len(c['lt']) == 2
        assert 'updated_at' in c['lt']


def test_prune_is_a_noop_without_a_client(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(lh, '_shared_sb', lambda: None)
    lh._shared_prune()          # darf nicht werfen
