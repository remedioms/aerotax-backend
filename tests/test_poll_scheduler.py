"""Adaptives Airport-Polling — Takt-Matrix, Demand-Ermittlung, Due-Auswahl und
Write-on-change-Hash (blueprints/poll_scheduler, ?tier=auto auf /poll-boards).

Alles pure Funktionen + In-Process-Memos — kein Flask/SB nötig; der einzige
„SB"-Test nutzt einen minimalen Fake mit der supabase-py-Chain-API.
"""
from datetime import datetime, timedelta, timezone

import pytest

from blueprints import poll_scheduler as ps


NOW = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _reset_scheduler_memos():
    """Modul-Memos pro Test leeren (gleiche Isolation wie conftest für andere)."""
    ps._LAST_POLL.clear()
    ps._OBS_HASH_MEMO.clear()
    ps._QUIET_MEMO.clear()
    ps._QUIET_SKIP_SINCE.clear()
    ps._DEMAND_MEMO.update({'ts': 0.0,
                            'demanded': frozenset(ps.ALWAYS_DEMAND),
                            'events': {}})
    yield


# ── Takt-Matrix (poll_interval_min) ──────────────────────────────────────────

def test_takt_matrix():
    # Event-Fenster → 3 min, schlägt alles (auch Nacht: Red-Eye eines Users).
    assert ps.poll_interval_min(True, True, 14) == 3
    assert ps.poll_interval_min(False, True, 3) == 3
    # Hub (FRA/MUC) im Event-Fenster → 1 min (jeder Cron-Tick), auch nachts.
    assert ps.poll_interval_min(True, True, 14, is_hub=True) == 1
    assert ps.poll_interval_min(False, True, 3, is_hub=True) == 1
    # Hub OHNE Event-Fenster → kein Sondertakt (normal 5, da nachgefragt).
    assert ps.poll_interval_min(True, False, 14, is_hub=True) == 5
    # Nacht (0–5 lokal) übersteuert Demand UND Default → 30 min.
    assert ps.poll_interval_min(True, False, 0) == 30
    assert ps.poll_interval_min(False, False, 4) == 30
    # 5 Uhr ist schon NICHT mehr Nacht (Intervall [0, 5)).
    assert ps.poll_interval_min(True, False, 5) == 5
    # Nachgefragt → 5 min, sonst Default 10 min (= heutiger Cron).
    assert ps.poll_interval_min(True, False, 14) == 5
    assert ps.poll_interval_min(False, False, 14) == 10


# ── Event-Fenster ────────────────────────────────────────────────────────────

def test_event_window_pm45():
    t = NOW + timedelta(minutes=44)
    assert ps.in_event_window(NOW, [t]) is True
    assert ps.in_event_window(NOW, [NOW - timedelta(minutes=45)]) is True
    assert ps.in_event_window(NOW, [NOW + timedelta(minutes=46)]) is False
    assert ps.in_event_window(NOW, []) is False
    assert ps.in_event_window(NOW, None) is False


# ── Demand aus Roster-Sektoren ───────────────────────────────────────────────

def test_demand_from_sectors_within_3h():
    secs = [
        # Leg in 2h → BEIDE Seiten nachgefragt, Events an dep/arr-Seite.
        {'flight': 'LH1412', 'from': 'FRA', 'to': 'BUD',
         'dep_iso': (NOW + timedelta(hours=2)).isoformat(),
         'arr_iso': (NOW + timedelta(hours=3, minutes=30)).isoformat()},
        # Leg vor 17 Tagen (Geister-Falle aus dem MyPlane-Bug) → irrelevant.
        {'flight': 'LH582', 'from': 'CAI', 'to': 'FRA',
         'dep_iso': (NOW - timedelta(days=17)).isoformat(),
         'arr_iso': (NOW - timedelta(days=17) + timedelta(hours=4)).isoformat()},
    ]
    demanded, events = ps.demand_from_sectors(secs, NOW)
    assert demanded == {'FRA', 'BUD'}
    assert 'CAI' not in demanded
    assert len(events['FRA']) == 1 and len(events['BUD']) == 1
    # arr in 3:30h liegt AUSSERHALB ±3h, aber dep in 2h macht das Leg relevant.


def test_demand_parses_z_suffix_and_garbage():
    secs = [
        {'from': 'MUC', 'to': 'LIS',
         'dep_iso': (NOW + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ'),
         'arr_iso': 'kaputt'},
        'kein-dict', None,
        {'from': '', 'to': 'XXXX', 'dep_iso': NOW.isoformat()},  # keine 3-letter
    ]
    demanded, events = ps.demand_from_sectors(secs, NOW)
    assert demanded == {'MUC', 'LIS'}
    assert 'LIS' not in events  # arr unparsbar → keine Event-Zeit, nur Demand


# ── Due-Auswahl (airports_due) ───────────────────────────────────────────────

def _hour_noon(_ap):
    return 12


def test_airports_due_empty_memo_all_due():
    # Restart/leeres last-poll → ALLE sofort fällig (defensiv, ein voller Tick).
    due = ps.airports_due(['FRA', 'BER', 'LIS'], NOW, set(), {}, {}, _hour_noon)
    assert due == ['FRA', 'BER', 'LIS']


def test_airports_due_respects_intervals():
    last = {ap: NOW.timestamp() - 60 for ap in ('FRA', 'BER', 'LIS')}
    events = {'LIS': [NOW + timedelta(minutes=10)]}
    # 1 min her: nichts fällig — auch das 3-min-Event-Airport nicht.
    assert ps.airports_due(['FRA', 'BER', 'LIS'], NOW, {'FRA'}, events,
                           last, _hour_noon) == []
    # 3 min (minus Cron-Jitter) her: nur das Event-Airport (3-min-Takt).
    last = {ap: NOW.timestamp() - 170 for ap in ('FRA', 'BER', 'LIS')}
    assert ps.airports_due(['FRA', 'BER', 'LIS'], NOW, {'FRA'}, events,
                           last, _hour_noon) == ['LIS']
    # 5 min her: FRA (immer nachgefragt, 5-min-Takt) kommt dazu, BER (10) nicht.
    last = {ap: NOW.timestamp() - 300 for ap in ('FRA', 'BER', 'LIS')}
    assert ps.airports_due(['FRA', 'BER', 'LIS'], NOW, set(), events,
                           last, _hour_noon) == ['FRA', 'LIS']
    # 10 min her: alle.
    last = {ap: NOW.timestamp() - 600 for ap in ('FRA', 'BER', 'LIS')}
    assert ps.airports_due(['FRA', 'BER', 'LIS'], NOW, set(), events,
                           last, _hour_noon) == ['FRA', 'BER', 'LIS']


def test_airports_due_night_30min():
    def hour_night(_ap):
        return 2
    last = {'OSL': NOW.timestamp() - 600}  # 10 min her — tags fällig …
    assert ps.airports_due(['OSL'], NOW, set(), {}, last, hour_night) == []
    last = {'OSL': NOW.timestamp() - 1800}  # … nachts erst nach 30 min.
    assert ps.airports_due(['OSL'], NOW, set(), {}, last, hour_night) == ['OSL']


def test_hub_1min_event_every_tick_no_double_poll():
    # FRA (Hub) UND BER im Event-Fenster: nur FRA hat den 1-min-Takt.
    events = {'FRA': [NOW], 'BER': [NOW]}
    last = {'FRA': NOW.timestamp() - 60, 'BER': NOW.timestamp() - 60}
    # Regulärer Minuten-Tick (60 s her): FRA fällig, BER (3 min) nicht.
    assert ps.airports_due(['FRA', 'BER'], NOW, set(), events,
                           last, _hour_noon) == ['FRA']
    # Cron-Jitter (nur 55 s her): FRA trotzdem fällig — JEDER Tick pollt.
    last = {'FRA': NOW.timestamp() - 55}
    assert ps.airports_due(['FRA'], NOW, set(), events, last, _hour_noon) == ['FRA']
    # Zweiter Aufruf in DERSELBEN Minute (10 s her): kein Doppel-Poll.
    last = {'FRA': NOW.timestamp() - 10}
    assert ps.airports_due(['FRA'], NOW, set(), events, last, _hour_noon) == []


def test_hub_1min_needs_event_window():
    # FRA OHNE Event-Fenster: normaler 5-min-Demand-Takt, kein Minuten-Poll.
    last = {'FRA': NOW.timestamp() - 60}
    assert ps.airports_due(['FRA'], NOW, set(), {}, last, _hour_noon) == []


def _local_noon(_ap):
    # naive Airport-Lokalzeit (volle datetime — wie _airport_local_now).
    return datetime(2026, 7, 9, 14, 0)


def test_select_due_marks_last_poll_and_no_sb():
    # sb=None (SB down) → läuft trotzdem: FRA/MUC nachgefragt, Rest Default,
    # Quiet-Gate skippt OHNE Datenquelle nie.
    due, diag = ps.select_due_airports(['FRA', 'MUC', 'BER'], None,
                                       _local_noon, now_utc=NOW)
    assert due == ['FRA', 'MUC', 'BER']
    assert set(diag['demanded']) >= {'FRA', 'MUC'}
    assert diag['quiet'] == []
    # Direkt danach: nichts mehr fällig (last-poll wurde gestempelt).
    due2, _ = ps.select_due_airports(['FRA', 'MUC', 'BER'], None,
                                     _local_noon, now_utc=NOW)
    assert due2 == []


# ── Demand-Memo mit Fake-SB (supabase-py-Chain) ──────────────────────────────

class _FakeSB:
    """Minimaler supabase-py-Fake: table().select().in_().execute().data."""
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def table(self, _name):
        return self

    def select(self, _cols):
        return self

    def in_(self, _col, _vals):
        return self

    def range(self, _start, _end):
        return self

    def execute(self):
        self.calls += 1
        class R:  # noqa: N801
            data = self.rows
        return R()


def test_get_demand_reads_raw_event_sectors_and_memoizes():
    rows = [{'datum': '2026-07-09', 'raw_event': {'ical_sectors': [
        {'flight': 'LH1830', 'from': 'MUC', 'to': 'SKP',
         'dep_iso': (NOW + timedelta(hours=1)).isoformat(),
         'arr_iso': (NOW + timedelta(hours=2, minutes=30)).isoformat()},
    ]}}]
    fake = _FakeSB(rows)
    demanded, events = ps.get_demand(fake, NOW)
    assert {'MUC', 'SKP', 'FRA'} <= set(demanded)  # FRA/MUC immer dabei
    assert 'SKP' in events
    # Memo: zweiter Aufruf innerhalb der TTL macht KEINEN weiteren Query.
    ps.get_demand(fake, NOW)
    assert fake.calls == 1


# ── Quiet-Gate: keine geplante Bewegung → Skip ───────────────────────────────

class _FakeGateSB:
    """Fake für die Quiet-Gate-Probes: gte() markiert die Fenster-Query,
    execute() liefert je nach Query-Art window_hit bzw. has_rows_today."""
    def __init__(self, window_hit=False, has_rows_today=True):
        self.window_hit = window_hit
        self.has_rows_today = has_rows_today
        self.calls = 0
        self._is_window = False

    def table(self, _n): return self
    def select(self, _c): return self
    def in_(self, _c, _v): return self
    def eq(self, _c, _v): return self
    def lte(self, _c, _v): return self
    def limit(self, _n): return self

    def gte(self, _c, _v):
        self._is_window = True
        return self

    def execute(self):
        self.calls += 1
        hit = self.window_hit if self._is_window else self.has_rows_today
        self._is_window = False
        from types import SimpleNamespace
        return SimpleNamespace(data=[{'sched': '12:30'}] if hit else [])


def test_movement_window_segments():
    # Tags: EIN Segment [13:00, 16:00] am selben Datum.
    segs = ps.movement_window_segments(datetime(2026, 7, 9, 14, 0))
    assert segs == [('2026-07-09', '13:00', '16:00')]
    # Mitternachts-Überlauf: zwei Segmente (HH:MM sortiert lexikographisch).
    segs = ps.movement_window_segments(datetime(2026, 7, 9, 23, 30))
    assert segs == [('2026-07-09', '22:30', '23:59'),
                    ('2026-07-10', '00:00', '01:30')]


def test_quiet_gate_skips_airport_without_movement():
    fake = _FakeGateSB(window_hit=False, has_rows_today=True)
    quiet = ps.get_quiet_airports(fake, ['WAW'], _local_noon)
    assert quiet == {'WAW'}
    # airports_due: quiet → KOMPLETT geskippt, obwohl 10 min (Default) um sind.
    last = {'WAW': NOW.timestamp() - 600}
    assert ps.airports_due(['WAW'], NOW, set(), {}, last, _hour_noon,
                           quiet=quiet) == []
    # Memo: zweiter Tick innerhalb der 15-min-TTL probed NICHT erneut.
    calls = fake.calls
    assert ps.get_quiet_airports(fake, ['WAW'], _local_noon) == {'WAW'}
    assert fake.calls == calls


def test_quiet_gate_allows_with_movement_in_window():
    fake = _FakeGateSB(window_hit=True)
    assert ps.get_quiet_airports(fake, ['WAW'], _local_noon) == set()


def test_quiet_gate_cold_airport_never_starved():
    # Henne-Ei: GAR keine obs-Rows heute → kalt → normal pollen, nie skippen.
    fake = _FakeGateSB(window_hit=False, has_rows_today=False)
    assert ps.get_quiet_airports(fake, ['XYZ'], _local_noon) == set()


def test_quiet_gate_event_override():
    # Event-/Roster-aktive Airports (exclude) werden gar nicht erst geprobed.
    fake = _FakeGateSB(window_hit=False, has_rows_today=True)
    quiet = ps.get_quiet_airports(fake, ['WAW'], _local_noon, exclude={'WAW'})
    assert quiet == set() and fake.calls == 0
    # Belt&Braces in airports_due: selbst ein quiet-markierter Airport wird
    # im Event-Fenster gepollt (Event schlägt Gate).
    events = {'WAW': [NOW]}
    assert ps.airports_due(['WAW'], NOW, set(), events, {}, _hour_noon,
                           quiet={'WAW'}) == ['WAW']


def test_quiet_gate_60min_safety_net():
    fake = _FakeGateSB(window_hit=False, has_rows_today=True)
    assert ps.get_quiet_airports(fake, ['WAW'], _local_noon) == {'WAW'}
    # 61 min durchgehend geskippt → EIN Poll erlaubt (Fahrplan-Lücken-Netz) …
    ps._QUIET_SKIP_SINCE['WAW'] -= 61 * 60
    assert ps.get_quiet_airports(fake, ['WAW'], _local_noon) == set()
    # … danach greift das Gate wieder (Zähler neu gestartet).
    assert ps.get_quiet_airports(fake, ['WAW'], _local_noon) == {'WAW'}


def test_roster_active_airports_window():
    # Event in +90 min liegt im Fenster [−1h, +2h] → aktiv; +3h nicht.
    events = {'SKP': [NOW + timedelta(minutes=90)],
              'CAI': [NOW + timedelta(hours=3)],
              'LIS': [NOW - timedelta(minutes=59)]}
    assert ps.roster_active_airports(events, NOW) == {'SKP', 'LIS'}


# ── Write-on-change Hash-Memo ────────────────────────────────────────────────

def test_obs_hash_skip_and_rewrite_on_change():
    p = {'date': '2026-07-09', 'airport': 'FRA', 'flight': 'LH146',
         'sched': '16:50', 'max_delay_min': 5, 'cancelled': False,
         'status': 'Abgeflogen', 'gate': 'A58'}
    # Leeres Memo (Restart) → schreiben.
    assert ps.obs_write_needed(p) is True
    ps.obs_mark_written(p)
    # Identischer Inhalt → skippen (updated_at zählt bewusst NICHT).
    assert ps.obs_write_needed({**p, 'updated_at': 'egal'}) is False
    # Operatives Feld ändert sich → wieder schreiben.
    assert ps.obs_write_needed({**p, 'max_delay_min': 12}) is True
    # Anderer Row-Key (Ankunfts-Board) bleibt unabhängig.
    assert ps.obs_write_needed({**p, 'airport': 'FRA#ARR'}) is True


def test_obs_hash_not_marked_means_write():
    # obs_mark_written wird NUR nach SB-Erfolg gerufen — ohne Markierung muss
    # jeder Folgeversuch wieder schreiben (Requeue-/Retry-Pfad bleibt intakt).
    p = {'date': '2026-07-09', 'airport': 'MUC', 'flight': 'LH100',
         'sched': '08:00', 'max_delay_min': 0, 'cancelled': False}
    assert ps.obs_write_needed(p) is True
    assert ps.obs_write_needed(p) is True
