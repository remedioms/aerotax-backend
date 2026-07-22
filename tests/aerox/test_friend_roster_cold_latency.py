"""friend-roster Kalt-Latenz: Batch-Prefetch + LH-cached_only (2026-07-22).

Root-Cause (Prod-Messung Hetzner, [req]-Logs + PostgREST-Timing):
get_friend_roster war backend-KALT 7–14 s, weil der Tages-Fan-out pro
VERGANGENEM Sektor (a) 3–5 EINZELNE airport_delay_obs-Reads machte (dep-Row,
arr-Row, Folgetags-Probe, Facts-Read über d±1; je ~100–130 ms Roundtrip →
2–5,5 s Wall-Clock trotz ThreadPool) und (b) im Facts-Fallback einen
BLOCKIERENDEN LH-Open-API-Call auslöste, dessen 5/s-Throttle alle Worker
global serialisiert (~0,2 s Spacing + RTT pro Flug → +5–12 s, solange das
Stunden-Budget offen ist).

Fix, hier festgenagelt (hermetisch, kein Netz):
  • get_friend_roster seedet VOR dem Fan-out einen Batch-Prefetch
    (_delay_obs_prefetch_batch, EIN or=(and(date.eq…,flight.in…))-Read).
  • _delay_obs_rows_for_date konsultiert den Prefetch und filtert lokal
    EXAKT wie die DB (airport eq, dest_iata eq, flight in Varianten).
  • _flight_facts_from_obs_uncached bedient seinen d±1-Read aus dem Prefetch
    (Sortierung/limit/Spalten wie die DB-Query).
  • _enrich_leg_delays ruft _flight_facts_from_obs mit lh_cached_only=True —
    LH-Miss blockiert nie mehr den Request-Thread.
"""
import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import datetime as _dt
import time

import app as A
import blueprints.aerox_data_blueprint as BP


# ── Fake-Supabase (Query-Chain wie supabase-py, zählt echte execute()) ───────

class _FakeQuery:
    def __init__(self, rows, calls):
        self._rows = rows
        self._calls = calls
        self.filters = {}

    def select(self, cols):
        self.filters['select'] = cols
        return self

    def or_(self, expr):
        self.filters['or'] = expr
        return self

    def eq(self, col, val):
        self.filters.setdefault('eq', {})[col] = val
        return self

    def in_(self, col, vals):
        self.filters.setdefault('in', {})[col] = list(vals)
        return self

    def order(self, col, desc=False):
        self.filters['order'] = (col, desc)
        return self

    def limit(self, n):
        self.filters['limit'] = n
        return self

    def range(self, a, b):
        self.filters['range'] = (a, b)
        return self

    def execute(self):
        self._calls.append(self.filters)

        class _R:
            data = self._rows
        return _R()


class _FakeSB:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def table(self, name):
        assert name == 'airport_delay_obs', name
        return _FakeQuery(self.rows, self.calls)


class _ForbiddenSB:
    """Wächter: jeder DB-Kontakt ist ein Test-Fail (Prefetch MUSS bedienen)."""

    def table(self, name):
        raise AssertionError('DB-Read trotz Prefetch: ' + name)


def _clear_prefetch():
    with A._DELAY_OBS_PREFETCH_LOCK:
        A._DELAY_OBS_PREFETCH.clear()


def _seed(date_str, fn, rows):
    with A._DELAY_OBS_PREFETCH_LOCK:
        A._DELAY_OBS_PREFETCH[(date_str, A._fn_norm(fn))] = (
            time.monotonic(), rows)


# ── Batch-Read + Konsult in _delay_obs_rows_for_date ─────────────────────────

def test_prefetch_batch_one_read_serves_dep_and_arr(monkeypatch):
    """EIN Batch-Read seedet; danach bedienen dep-/arr-Lookups OHNE weitere
    DB-Reads und filtern byte-identisch zur DB (airport eq, flight in)."""
    _clear_prefetch()
    d = '2026-07-15'
    rows = [
        {'date': d, 'airport': 'FRA', 'flight': 'LH919', 'dest_iata': 'GVA',
         'sched': '08:10', 'esti': '08:22', 'status': 'Abgeflogen'},
        {'date': d, 'airport': 'GVA#ARR', 'flight': 'LH919', 'dest_iata': 'FRA',
         'sched': '09:20', 'esti': '09:31', 'status': 'Gelandet'},
        # Padding-Variante — muss über die Varianten-Menge mitkommen.
        {'date': d, 'airport': 'FRA', 'flight': 'LH0919', 'dest_iata': 'GVA',
         'sched': '08:10', 'esti': None, 'status': 'Geplant'},
    ]
    fake = _FakeSB(rows)
    monkeypatch.setattr(A, 'sb', fake)
    monkeypatch.setattr(A, 'SB_AVAILABLE', True)

    A._delay_obs_prefetch_batch({(d, 'LH919'), (d, 'LH98')})
    assert len(fake.calls) == 1, fake.calls
    or_expr = fake.calls[0]['or']
    assert f'date.eq.{d}' in or_expr
    assert 'LH919' in or_expr and 'LH0919' in or_expr   # Varianten im in.(…)

    # Ab jetzt: DB verboten — der Prefetch muss liefern.
    monkeypatch.setattr(A, 'sb', _ForbiddenSB())
    dep = A._delay_obs_rows_for_date(d, 'FRA', flight='LH919')
    assert {r['flight'] for r in dep} == {'LH919', 'LH0919'}
    assert all(r['airport'] == 'FRA' for r in dep)
    arr = A._delay_obs_rows_for_date(d, 'GVA#ARR', flight='LH919')
    assert [r['sched'] for r in arr] == ['09:20']
    # dest_iata-Filter wie DB-eq (NULL/abweichend matcht nie).
    dep_dest = A._delay_obs_rows_for_date(d, 'FRA', dest_iata='ZRH',
                                          flight='LH919')
    assert dep_dest == []
    # Definitives „keine Rows": geseedet-leer → [] OHNE DB-Read.
    assert A._delay_obs_rows_for_date(d, 'FRA', flight='LH98') == []


def test_prefetch_miss_or_expiry_falls_back_to_single_read(monkeypatch):
    """Nicht geseedet ODER TTL abgelaufen → bisheriger Einzel-Read (Query mit
    date-eq + airport-eq + flight-in wie vor dem Fix)."""
    _clear_prefetch()
    d = '2026-07-15'
    fake = _FakeSB([])
    monkeypatch.setattr(A, 'sb', fake)
    monkeypatch.setattr(A, 'SB_AVAILABLE', True)

    A._delay_obs_rows_for_date(d, 'FRA', flight='LH919')
    assert len(fake.calls) == 1
    assert fake.calls[0]['eq'] == {'date': d, 'airport': 'FRA'}
    assert 'LH919' in fake.calls[0]['in']['flight']

    # abgelaufener Seed zählt als Miss
    _seed(d, 'LH919', [])
    with A._DELAY_OBS_PREFETCH_LOCK:
        k = (d, 'LH919')
        A._DELAY_OBS_PREFETCH[k] = (
            time.monotonic() - A._DELAY_OBS_PREFETCH_TTL - 1,
            A._DELAY_OBS_PREFETCH[k][1])
    A._delay_obs_rows_for_date(d, 'FRA', flight='LH919')
    assert len(fake.calls) == 2


def test_prefetch_rows_returns_copies():
    """Konsumenten mutieren ihre Rows — der Store darf nicht vergiftet werden."""
    _clear_prefetch()
    d = '2026-07-15'
    _seed(d, 'LH919', [{'date': d, 'airport': 'FRA', 'flight': 'LH919',
                        'sched': '08:10'}])
    got = A._delay_obs_prefetch_rows(d, 'LH919')
    got[0]['sched'] = 'MUTIERT'
    again = A._delay_obs_prefetch_rows(d, 'LH919')
    assert again[0]['sched'] == '08:10'


# ── Facts-Read (Blueprint) bedient sich aus dem Prefetch ─────────────────────

def test_facts_uncached_served_from_prefetch(monkeypatch):
    """_flight_facts_from_obs_uncached liest d−1/d/d+1 aus dem Prefetch und
    macht KEINEN eigenen SB-Read, wenn alle drei Tage geseedet sind."""
    _clear_prefetch()
    d = '2026-07-15'
    yday = '2026-07-14'
    nday = '2026-07-16'
    _seed(yday, 'LH882', [])
    _seed(nday, 'LH882', [])
    _seed(d, 'LH882', [
        {'date': d, 'airport': 'TLL', 'flight': 'LH882', 'dest_iata': 'FRA',
         'sched': '10:00', 'esti': '10:05', 'status': 'Abgeflogen',
         'max_delay_min': 5, 'cancelled': False, 'gate': None,
         'terminal': None, 'reg': None, 'type_code': None,
         'updated_at': '2026-07-15T11:00:00+00:00'},
        {'date': d, 'airport': 'FRA#ARR', 'flight': 'LH882', 'dest_iata': 'TLL',
         'sched': '12:30', 'esti': '12:41', 'status': 'Gelandet',
         'max_delay_min': 11, 'cancelled': False, 'gate': None,
         'terminal': None, 'reg': None, 'type_code': None,
         'updated_at': '2026-07-15T13:00:00+00:00'},
    ])
    monkeypatch.setattr(BP, '_sb', lambda: _ForbiddenSB())
    f = BP._flight_facts_from_obs_uncached('LH882', d, 'TLL', 'FRA')
    assert f, 'Prefetch-Rows müssen Facts liefern'
    assert f.get('dep_status') == 'Abgeflogen'
    assert f.get('arr_status') == 'Gelandet'
    assert f.get('arr_delay_min') == 11


def test_facts_reread_after_fr24_backfill_bypasses_prefetch(monkeypatch):
    """Corner-Case-Wächter: schreibt der FR24-Ankunfts-Backfill eine NEUE
    ARR-Row, muss der Re-Read sie sehen — der ≤60 s alte Prefetch-Seed (ohne
    die Row) wäre nachweislich stale und darf den Re-Read nicht bedienen."""
    _clear_prefetch()
    d = '2026-07-10'                      # sicher vergangen
    for dx in ('2026-07-09', d, '2026-07-11'):
        _seed(dx, 'LH882', [])
    # DB (nach dem Backfill-Write) kennt die frische ARR-Row.
    fresh = [{'date': d, 'airport': 'FRA#ARR', 'flight': 'LH882',
              'dest_iata': 'TLL', 'sched': '12:41', 'esti': '12:41',
              'status': 'Gelandet', 'max_delay_min': 0, 'cancelled': False,
              'gate': None, 'terminal': None, 'reg': None, 'type_code': None,
              'updated_at': '2026-07-10T13:00:00+00:00'}]
    fake = _FakeSB(fresh)
    monkeypatch.setattr(BP, '_sb', lambda: fake)
    monkeypatch.setattr(BP, '_fr24_fill_missing_arrival',
                        lambda *a, **k: True)
    f = BP._flight_facts_from_obs_uncached('LH882', d, 'TLL', 'FRA')
    assert fake.calls, 'Re-Read muss die DB treffen (Prefetch-Bypass)'
    assert f.get('arr_status') == 'Gelandet', f


def test_facts_uncached_partial_prefetch_uses_db(monkeypatch):
    """Fehlt auch nur EIN Tag im Prefetch, läuft die bisherige DB-Query —
    keine stillschweigend verkleinerte Kandidatenmenge."""
    _clear_prefetch()
    d = '2026-07-15'
    _seed(d, 'LH882', [])          # yday/nday fehlen absichtlich
    fake = _FakeSB([])
    monkeypatch.setattr(BP, '_sb', lambda: fake)
    BP._flight_facts_from_obs_uncached('LH882', d, 'TLL', 'FRA')
    assert len(fake.calls) >= 1
    assert fake.calls[0]['eq'] == {'flight': 'LH882'}
    assert set(fake.calls[0]['in']['date']) == {'2026-07-14', d, '2026-07-16'}


# ── LH Open API: nie blockierend im Fan-out ──────────────────────────────────

def test_facts_wrapper_passes_lh_cached_only(monkeypatch):
    """lh_cached_only wird 1:1 an lh_flight_facts durchgereicht (True = Miss
    blockiert nie); Default False bleibt byte-identisch zum alten Verhalten."""
    import blueprints.lh_open_api as LH
    seen = []

    def _fake_lh(fn, d, dep=None, arr=None, force=False, cached_only=False):
        seen.append(cached_only)
        return {}

    monkeypatch.setattr(LH, 'lh_flight_facts', _fake_lh)
    monkeypatch.setattr(LH, 'is_lh_group', lambda fn: True)
    monkeypatch.setattr(BP, '_flight_facts_from_obs_uncached',
                        lambda *a, **k: {'dep_status': 'x'})
    with BP._OBS_FACTS_LOCK:
        BP._OBS_FACTS_MEMO.clear()
    BP._flight_facts_from_obs('LH1', '2026-07-15', lh_cached_only=True)
    with BP._OBS_FACTS_LOCK:
        BP._OBS_FACTS_MEMO.clear()
    BP._flight_facts_from_obs('LH1', '2026-07-15')
    assert seen == [True, False], seen


def test_enrich_leg_delays_requests_lh_cached_only(monkeypatch):
    """Der Fan-out-Enricher (friend-roster/Briefings) fordert für den
    Vergangenheits-Facts-Fallback IMMER lh_cached_only=True an."""
    seen = []

    def _fake_facts(fn, d, dep_iata=None, arr_iata=None, lh_cached_only=False):
        seen.append(lh_cached_only)
        return {}

    monkeypatch.setattr(BP, '_flight_facts_from_obs', _fake_facts)
    monkeypatch.setattr(A, '_flight_obs_merged', lambda *a, **k: None)
    monkeypatch.setattr(A, '_ax_codeshare_map', lambda: {})
    d = (_dt.date.today() - _dt.timedelta(days=3)).isoformat()
    sector = {'flight': 'LH882', 'from': 'TLL', 'to': 'FRA',
              'dep_iso': f'{d}T10:00:00Z', 'arr_iso': f'{d}T12:30:00Z'}
    A._enrich_leg_delays([sector], d, free_only=True, past_horizon_h=24 * 35)
    assert seen and all(v is True for v in seen), seen


# ── Endpoint: Seeding VOR dem Fan-out ────────────────────────────────────────

def test_friend_roster_seeds_prefetch_before_fanout(monkeypatch):
    """get_friend_roster ruft _delay_obs_prefetch_batch mit den (Datum±,
    Operating-Flugnummer)-Paaren der Vergangenheits-Legs auf; tiefe
    Zukunfts-Legs (> morgen) werden NICHT vorgeladen (die liest der Enricher
    nie aus SB)."""
    me_tok, friend_tok = 'me_pf_tok', 'friend_pf_tok'
    A._FRIEND_ROSTER_MEMO.clear()
    past = _dt.date.today() - _dt.timedelta(days=4)
    deep_future = _dt.date.today() + _dt.timedelta(days=9)

    def _day(d, fn, frm, to):
        return {'datum': d.isoformat(), 'klass': 'Z73', 'marker': None,
                'routing': f'{frm}-{to}', 'eur': None,
                'reader_facts': {},
                'ical_sectors': [{'flight': fn, 'from': frm, 'to': to,
                                  'dep_iso': f'{d.isoformat()}T10:00:00Z',
                                  'arr_iso': f'{d.isoformat()}T12:00:00Z'}]}

    tage = [_day(past, 'EW9440', 'CGN', 'PMI'),
            _day(deep_future, 'EW9441', 'PMI', 'CGN')]
    monkeypatch.setattr(A, '_friends_load',
                        lambda t: {'friends': [friend_tok]})
    monkeypatch.setattr(A, '_maybe_refresh_calendar_feed', lambda *a, **k: None)
    A._store.pop(friend_tok, None)
    monkeypatch.setattr(A, '_roster_snapshot_read', lambda t: {'tage': tage})
    monkeypatch.setattr(A, '_profile_homebase_cached', lambda t: 'CGN')
    monkeypatch.setattr(A, '_ax_codeshare_map', lambda: {'EW9440': 'LH9440'})
    monkeypatch.setattr(A, '_enrich_leg_delays', lambda *a, **k: None)
    captured = []
    monkeypatch.setattr(A, '_delay_obs_prefetch_batch',
                        lambda pairs: captured.append(set(pairs)))

    client = A.app.test_client()
    r = client.get(f'/api/user/friend-roster/{me_tok}/{friend_tok}')
    assert r.status_code == 200, r.status_code
    assert captured, 'Batch-Prefetch muss vor dem Fan-out feuern'
    pairs = captured[0]
    # Vergangenheits-Leg: −1…+2 um den Leg-Tag, unter der OPERATING-Nummer.
    for off in (-1, 0, 1, 2):
        assert ((past + _dt.timedelta(days=off)).isoformat(),
                'LH9440') in pairs
    # tiefes Zukunfts-Leg: gar nicht vorladen.
    assert not any(fn == 'LH9441' for (_d, fn) in pairs), pairs


def test_friend_roster_payload_unchanged_by_prefetch(monkeypatch):
    """Payload-Äquivalenz-Wächter: gleiche Antwort mit und ohne geseedeten
    Prefetch (der Enricher ist hier neutralisiert; es geht um den Endpoint-
    Contract days/count/source, den das Seeding nicht anfassen darf)."""
    me_tok, friend_tok = 'me_pf2_tok', 'friend_pf2_tok'
    past = _dt.date.today() - _dt.timedelta(days=2)
    tage = [{'datum': past.isoformat(), 'klass': 'Z73', 'marker': 'FLIGHT',
             'routing': 'FRA-GVA', 'eur': None, 'reader_facts': {},
             'ical_sectors': [{'flight': 'LH919', 'from': 'FRA', 'to': 'GVA',
                               'dep_iso': f'{past.isoformat()}T08:00:00Z',
                               'arr_iso': f'{past.isoformat()}T09:20:00Z'}]}]
    monkeypatch.setattr(A, '_friends_load',
                        lambda t: {'friends': [friend_tok]})
    monkeypatch.setattr(A, '_maybe_refresh_calendar_feed', lambda *a, **k: None)
    A._store.pop(friend_tok, None)
    monkeypatch.setattr(A, '_roster_snapshot_read', lambda t: {'tage': tage})
    monkeypatch.setattr(A, '_profile_homebase_cached', lambda t: 'FRA')
    monkeypatch.setattr(A, '_ax_codeshare_map', lambda: {})
    monkeypatch.setattr(A, '_enrich_leg_delays', lambda *a, **k: None)
    client = A.app.test_client()

    A._FRIEND_ROSTER_MEMO.clear()
    _clear_prefetch()
    monkeypatch.setattr(A, '_delay_obs_prefetch_batch', lambda pairs: None)
    without_seed = client.get(
        f'/api/user/friend-roster/{me_tok}/{friend_tok}').get_json()

    A._FRIEND_ROSTER_MEMO.clear()
    _seed(past.isoformat(), 'LH919', [])
    with_seed = client.get(
        f'/api/user/friend-roster/{me_tok}/{friend_tok}').get_json()
    assert without_seed == with_seed
