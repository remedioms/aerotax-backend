"""Warehouse-Reg-Kette — Root-Cause-Fixes 2026-07-05.

Deckt ab (Owner-Debug „warum zeigt Tibor nicht auf jedem Leg ein Flugzeug"):
  • _fetch_fra_flights      — liest jetzt auch VERGANGENHEITS-Seiten (page=-1,
    -2, …) des now-relativen Fraport-Feeds. Vorher sah der Poller gelandete
    Ankünfte/abgeflogene Abflüge praktisch NIE (LH919: 2 Beobachtungen in 10
    Tagen; LH1455-Ankünfte fehlten) — der `passed`-Persist-Gate traf nur die
    Minuten-Lücke „sched gerade passiert, noch auf Seite 1".
  • _merge_into_delay_store — eine gelandete Vergangenheits-Ankunft ('FRA#ARR')
    wird persistiert (Reg + esti + Status → delay_known).
  • /api/ax/flight-info     — ARR-Row-Entspiegelung: gibt es nur die
    Ankunfts-Row ('<AP>#ARR'), sind origin/dest nicht mehr vertauscht
    (LH1455 wurde als „FRA→TIA" serviert, echt ist TIA→FRA).
  • _sb_day_reg             — Blueprint-Fallback: Tail direkt aus den
    SB-Tages-Rows (flight+date, airport-agnostisch) wie /flight-info.
  • /api/ax/flight-live     — Reg-Kaskade ?reg= → Dual-Side-Merge → SB-Tages-Rows;
    optionale dep_iata/arr_iata-Hints.
  • _build_inbound_chain    — Reg-Kaskade Merge → SB-Tages-Rows → Roster-Hint.

KEIN echter Netz-/DB-Zugriff: requests/sb/_machine_live werden gemockt.
"""
import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

import app as A
import blueprints.aerox_data_blueprint as BP


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _clear_caches():
    A._FLIGHT_MERGE_CACHE.clear()
    try:
        BP._LIFECYCLE_MEMO.clear()
    except Exception:
        pass
    yield
    A._FLIGHT_MERGE_CACHE.clear()
    try:
        BP._LIFECYCLE_MEMO.clear()
    except Exception:
        pass


@pytest.fixture
def client():
    A.app.testing = True
    return A.app.test_client()


class _Resp:
    def __init__(self, rows, status=200):
        self.status_code = status
        self._rows = rows

    def json(self):
        return {'data': self._rows}


def _raw(fnr, sched, esti=None, status='', reg='', iata='LHR', apname='London'):
    """Rohe Fraport-Feed-Zeile (Feld-Namen wie der echte JSON-Feed)."""
    return {'fnr': fnr, 'al': 'LH', 'alname': 'Lufthansa', 'iata': iata,
            'apname': apname, 'sched': sched, 'esti': esti, 'status': status,
            'gate': 'A13', 'terminal': '1', 'halle': '', 'reg': reg, 'ac': '32N'}


def _page_of(n, prefix, hh):
    """n Roh-Zeilen (volle Seite = 25) mit fortlaufenden Flugnummern."""
    return [_raw(f'{prefix} {i:03d}', f'2026-07-05T{hh}:{i % 60:02d}:00+0200')
            for i in range(n)]


def _fake_sb(rows):
    """Chainbares Fake-Supabase (table→select→eq→eq→order→limit→execute)."""
    q = MagicMock()
    q.select.return_value = q
    q.eq.return_value = q
    q.order.return_value = q
    q.limit.return_value = q
    q.execute.return_value = SimpleNamespace(data=rows)
    sbm = MagicMock()
    sbm.table.return_value = q
    return sbm


# ══════════════════════════════════════════════════════════════════════════════
# _fetch_fra_flights — Vergangenheits-Seiten
# ══════════════════════════════════════════════════════════════════════════════
def _run_fetch(pages, flight_type='arrival', max_pages=3, past_pages=6):
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        p = dict(params or {})
        calls.append((url, p))
        page = p.get('page', 1)
        return _Resp(pages.get(page, []))

    with patch('requests.get', side_effect=fake_get):
        out = A._fetch_fra_flights(flight_type, max_pages=max_pages,
                                   past_pages=past_pages)
    return out, calls


def test_fetch_fra_reads_negative_pages_chronologically():
    pages = {
        # Seite -2: <25 Zeilen → Feed-Anfang, danach KEIN -3-Call mehr.
        -2: [_raw('LH 1455', '2026-07-05T08:00:00+0200',
                  esti='2026-07-05T08:07:00+0200', status='landed',
                  reg='D-AIZI', iata='TIA', apname='Tirana')],
        -1: _page_of(25, 'LH 9', '09'),
        1: _page_of(25, 'LH 1', '10'),
        2: _page_of(3, 'LH 2', '11'),
    }
    out, calls = _run_fetch(pages)
    flights = [f['flight'] for f in out]
    # Vergangenheit VOR Zukunft, älteste Seite zuerst (chronologisch).
    assert flights[0] == 'LH1455'
    assert len(out) == 1 + 25 + 25 + 3
    # Landed-Ankunft trägt Reg + IST-Zeit — die Basis der Warehouse-Persistenz.
    landed = out[0]
    assert landed['reg'] == 'D-AIZI'
    assert landed['esti'] == '2026-07-05T08:07:00+0200'
    assert landed['status'] == 'Gelandet'
    requested_pages = [p.get('page', 1) for _u, p in calls]
    assert -1 in requested_pages and -2 in requested_pages
    assert -3 not in requested_pages          # Feed-Anfang respektiert
    # Arrivals-Filter auf JEDEM Call (auch den negativen Seiten).
    assert all(p.get('flighttype') == 'arrivals' for _u, p in calls)


def test_fetch_fra_default_has_no_past_pages():
    pages = {1: _page_of(5, 'LH 3', '12')}
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(dict(params or {}))
        return _Resp(pages.get((params or {}).get('page', 1), []))

    with patch('requests.get', side_effect=fake_get):
        out = A._fetch_fra_flights('departure', max_pages=2)
    # Rückwärtskompatibel: ohne past_pages KEIN negativer Call, Seite 1 ohne Param.
    assert all(c.get('page', 1) >= 1 for c in calls)
    assert 'page' not in calls[0]
    assert len(out) == 5


def test_fetch_fra_past_page_error_keeps_future():
    def fake_get(url, params=None, headers=None, timeout=None):
        page = (params or {}).get('page', 1)
        if page < 0:
            raise RuntimeError('past down')
        return _Resp(_page_of(4, 'LH 4', '13') if page == 1 else [])

    with patch('requests.get', side_effect=fake_get):
        out = A._fetch_fra_flights('departure', max_pages=2, past_pages=6)
    assert len(out) == 4                      # Zukunft degradiert nicht


def test_fra_board_cached_requests_past_pages():
    """Der Harvest-Pfad (_fra_board_cached, füttert auch FRA#ARR) MUSS die
    Vergangenheits-Seiten anfordern — sonst bleibt das Ankunfts-Warehouse leer."""
    A._AIRPORT_BOARD_CACHE.pop('FRA_arr', None)
    seen = {}

    def fake_fetch(flight_type, max_pages=3, past_pages=0):
        seen['past_pages'] = past_pages
        seen['max_pages'] = max_pages
        return []

    with patch.object(A, '_fetch_fra_flights', side_effect=fake_fetch):
        A._fra_board_cached('arrival')
    A._AIRPORT_BOARD_CACHE.pop('FRA_arr', None)
    assert seen['past_pages'] == A._FRA_PAST_PAGES > 0


# ══════════════════════════════════════════════════════════════════════════════
# _merge_into_delay_store — gelandete Vergangenheits-Ankunft wird persistiert
# ══════════════════════════════════════════════════════════════════════════════
def test_merge_persists_landed_past_arrival(monkeypatch):
    monkeypatch.setattr(A, '_delay_obs_flush_pending', lambda: None)
    monkeypatch.setattr(A, '_delay_store_load_from_sb', lambda *a, **k: None)
    writes = []
    monkeypatch.setattr(A, '_delay_obs_write_through',
                        lambda *a, **k: writes.append((a, k)))
    now_local = A._airport_local_now('FRA#ARR')
    sched = (now_local - timedelta(minutes=40)).strftime('%Y-%m-%dT%H:%M:00')
    esti = (now_local - timedelta(minutes=28)).strftime('%Y-%m-%dT%H:%M:00')
    date_str = now_local.strftime('%Y-%m-%d')
    row = {'flight': 'LH919', 'sched': sched, 'esti': esti, 'status': 'Gelandet',
           'reg': 'DAINX', 'aircraft': 'A20N', 'dest_iata': 'LHR',
           'dest_name': 'London-Heathrow', 'airline': 'LH', 'gate': '',
           'terminal': '1', 'delay_min': 12, 'cancelled': False}
    A._merge_into_delay_store([row], date_str, 'FRA#ARR')
    hhmm = sched[11:16]
    key = (date_str, 'FRA#ARR', 'LH919', hhmm)
    assert A._delay_store.get(key) == 12
    meta = A._delay_store_meta.get(key) or {}
    assert meta.get('reg') == 'DAINX'
    assert meta.get('delay_known') is True    # esti + Gelandet = wirklich bekannt
    assert writes                              # SB-Write-Through lief
    # Aufräumen (globale Stores nicht in andere Tests bluten lassen).
    A._delay_store.pop(key, None)
    A._delay_store_meta.pop(key, None)


# ══════════════════════════════════════════════════════════════════════════════
# _obs_service_day — Verkehrstag statt Poll-Tag (Folgetags-Kontaminations-Fix)
# ══════════════════════════════════════════════════════════════════════════════
from datetime import datetime as _DT


def test_service_day_full_date_wins_over_poll_day():
    # Fraport liefert volles Datum → das ist der Verkehrstag, Poll-Tag egal.
    assert A._obs_service_day('2026-07-15T08:45:00+0200', '2026-07-14') == '2026-07-15'
    assert A._obs_service_day('2026-07-16T05:10:00', '2026-07-16') == '2026-07-16'


def test_service_day_hhmm_only_stale_plan_row_rolls_to_next_day():
    # LH867-BEWEIS: nur HH:MM, sched 08:45 „Geplant", abends gepollt am 14.
    # → Row gehört zum 15. (Soll 08:45), NICHT zum 14.
    now = _DT(2026, 7, 14, 21, 24)
    assert A._obs_service_day('08:45', '2026-07-14', now, status='Geplant') == '2026-07-15'


def test_service_day_hhmm_actual_status_stays_today():
    # Echtes Ist (esti/gelandet) → bleibt beim heutigen Tag, wird NICHT verschoben.
    now = _DT(2026, 7, 14, 21, 24)
    assert A._obs_service_day('08:45', '2026-07-14', now, status='Gelandet') == '2026-07-14'
    assert A._obs_service_day('08:45', '2026-07-14', now, status='Geplant',
                              esti='2026-07-14T09:10:00') == '2026-07-14'


def test_service_day_hhmm_recent_plan_row_stays_today():
    # 20:00-Flug, jetzt 21:24 (nur ~1.4h her) → NICHT >6h → bleibt heute.
    now = _DT(2026, 7, 14, 21, 24)
    assert A._obs_service_day('20:00', '2026-07-14', now, status='Geplant') == '2026-07-14'


def test_merge_next_day_arrival_gets_keyed_under_its_own_day(monkeypatch):
    # END-TO-END: eine abends gepollte Folgetags-Ankunft (volles Datum im sched)
    # landet unter IHREM Datum, nicht unter dem Poll-Tag.
    monkeypatch.setattr(A, '_delay_obs_flush_pending', lambda: None)
    monkeypatch.setattr(A, '_delay_store_load_from_sb', lambda *a, **k: None)
    writes = []
    monkeypatch.setattr(A, '_delay_obs_write_through',
                        lambda *a, **k: writes.append(a))
    now_local = A._airport_local_now('FRA#ARR')
    poll_day = now_local.strftime('%Y-%m-%d')
    next_day = (now_local + timedelta(days=1)).strftime('%Y-%m-%d')
    # Folgetags-Frühflug, plan-artig, ohne esti — genau der LH867-Fall.
    sched = f'{next_day}T08:45:00'
    row = {'flight': 'LH867', 'sched': sched, 'esti': '', 'status': 'Geplant',
           'dest_iata': 'AMM', 'dest_name': 'Amman', 'airline': 'LH',
           'delay_min': 0, 'cancelled': False}
    A._merge_into_delay_store([row], poll_day, 'FRA#ARR')
    key_next = (next_day, 'FRA#ARR', 'LH867', '08:45')
    key_poll = (poll_day, 'FRA#ARR', 'LH867', '08:45')
    # Der Flug ist am Poll-Tag noch NICHT passiert → er wird (falls überhaupt)
    # unter dem FOLGETAG geführt, niemals unter dem Poll-Tag.
    assert key_poll not in A._delay_store
    # Wenn geschrieben, dann mit dem Folgetags-Datum.
    for w in writes:
        assert w[0] == next_day
    A._delay_store.pop(key_next, None)
    A._delay_store.pop(key_poll, None)
    A._delay_store_meta.pop(key_next, None)
    A._delay_store_meta.pop(key_poll, None)


# ══════════════════════════════════════════════════════════════════════════════
# /api/ax/flight-info — ARR-Row-Entspiegelung
# ══════════════════════════════════════════════════════════════════════════════
def _obs_row(airport, dest_iata, **kw):
    base = {'airport': airport, 'dest_iata': dest_iata, 'dest_name': 'Tirana',
            'flight': 'LH1455', 'date': '2026-07-01',
            'sched': '2026-07-01T18:05:00', 'esti': '2026-07-01T18:15:00',
            'status': 'Gelandet', 'reg': 'DAIZI', 'type_code': 'A21N',
            'max_delay_min': 10, 'cancelled': False, 'airline': 'LH',
            'gate': 'A13', 'terminal': '1', 'updated_at': '2026-07-01T20:00:00Z'}
    base.update(kw)
    return base


def _flight_info(client, flightno='LH1455', date='2026-07-01'):
    return client.get(f'/api/ax/flight-info/{flightno}?date={date}')


def test_flight_info_unmirrors_arrival_only_row(client):
    rows = [_obs_row('FRA#ARR', 'TIA')]      # NUR die Ankunfts-Row am Ziel FRA
    with patch.object(A, 'sb', _fake_sb(rows)), \
         patch.object(A, 'SB_AVAILABLE', True), \
         patch.object(A, '_flight_from_live_board', return_value=None), \
         patch.object(A, '_arrival_gate_terminal', return_value=(None, None)), \
         patch.object(A, '_flight_obs_merged', return_value=None):
        r = _flight_info(client)
    assert r.status_code == 200
    b = r.get_json()
    assert b['found'] is True
    # Vorher: origin=FRA, dest=TIA (gespiegelt/FALSCH). Echt: TIA → FRA.
    assert b['origin'] == 'TIA'
    assert b['dest'] == 'FRA'
    assert b['dest_name'] is None            # ARR-dest_name war der HERKUNFTS-Name
    assert b['reg'] == 'DAIZI'


def test_flight_info_dep_row_still_wins(client):
    rows = [_obs_row('FRA#ARR', 'TIA'),
            _obs_row('TIA', 'FRA', dest_name='Frankfurt')]
    with patch.object(A, 'sb', _fake_sb(rows)), \
         patch.object(A, 'SB_AVAILABLE', True), \
         patch.object(A, '_flight_from_live_board', return_value=None), \
         patch.object(A, '_arrival_gate_terminal', return_value=(None, None)), \
         patch.object(A, '_flight_obs_merged', return_value=None):
        r = _flight_info(client)
    b = r.get_json()
    # Abflug-Row (airport=Origin) bleibt die bevorzugte, ungespiegelte Quelle.
    assert b['origin'] == 'TIA'
    assert b['dest'] == 'FRA'
    assert b['dest_name'] == 'Frankfurt'


# ══════════════════════════════════════════════════════════════════════════════
# _sb_day_reg — Tail direkt aus den SB-Tages-Rows
# ══════════════════════════════════════════════════════════════════════════════
def _patch_life_app(monkeypatch, sbm, extra=None):
    extra = extra or {}

    def fake_life_app(name, default=None):
        if name == 'sb':
            return sbm
        return extra.get(name, default)

    monkeypatch.setattr(BP, '_life_app', fake_life_app)


def test_sb_day_reg_unmirrors_arr_row(monkeypatch):
    _patch_life_app(monkeypatch, _fake_sb([_obs_row('FRA#ARR', 'TIA')]))
    reg, tc, dep, arr = BP._sb_day_reg('LH1455', '2026-07-01')
    assert (reg, tc) == ('DAIZI', 'A21N')
    assert (dep, arr) == ('TIA', 'FRA')


def test_sb_day_reg_prefers_dep_row_reg(monkeypatch):
    rows = [_obs_row('FRA#ARR', 'TIA', reg='DWRONG'),
            _obs_row('TIA', 'FRA', reg='DAIZI', type_code='A21N')]
    _patch_life_app(monkeypatch, _fake_sb(rows))
    reg, tc, dep, arr = BP._sb_day_reg('LH1455', '2026-07-01')
    assert reg == 'DAIZI'                    # dep-Row-Reg schlägt arr-Row-Reg


def test_sb_day_reg_needs_date(monkeypatch):
    _patch_life_app(monkeypatch, _fake_sb([_obs_row('FRA#ARR', 'TIA')]))
    assert BP._sb_day_reg('LH1455', None) == (None, None, None, None)


# ══════════════════════════════════════════════════════════════════════════════
# /api/ax/flight-live — Reg-Kaskade + SB-Fallback
# ══════════════════════════════════════════════════════════════════════════════
def _live_pos():
    return {'lat': 50.0, 'lon': 8.5, 'alt': 30000, 'gs': 420.0,
            'track': 270.0, 'on_ground': False}


def test_flight_live_pulls_reg_from_sb_day_rows(client, monkeypatch):
    """Merged-Store leer (kein dep_iata → keine Store-Keys) → die SB-Tages-Row
    liefert den Tail, ADS-B findet die Maschine (Befund 1: reg:null obwohl das
    Warehouse den Tag kennt).

    _life_app wird KOMPLETT gefaked (nicht via patch.object(A, 'sb')):
    test_calculation.py löscht sys.modules['app'] und re-importiert app —
    danach zeigte `import app` in _life_app auf ein ANDERES Modul-Objekt als
    das hier importierte A, und der Patch griff je nach Suite-Reihenfolge nicht."""
    sb_rows = [_obs_row('FRA', 'HND', flight='LH716', date='2026-07-05',
                        reg='DABYO', type_code='B748', dest_name='Tokyo-Haneda')]
    _patch_life_app(monkeypatch, _fake_sb(sb_rows),
                    extra={'_flight_obs_merged': lambda *a, **k: None})
    monkeypatch.setattr(BP, '_machine_live',
                        lambda reg, want_route=True, targeted=False:
                        ('3c4b2f', 'DLH716', _live_pos(),
                         {'src': 'FRA', 'dst': 'HND', 'source': 'warehouse'}))
    r = client.get('/api/ax/flight-live/TESTTOKEN'
                   '?flight_no=LH716&date=2026-07-05')
    assert r.status_code == 200
    b = r.get_json()
    assert b['reg'] == 'DABYO'
    assert b['in_flight'] is True
    assert (b['dep'] or {}).get('iata') == 'FRA'
    assert (b['dest'] or {}).get('iata') == 'HND'


def test_flight_live_explicit_reg_hint_wins(client, monkeypatch):
    """?reg= (echter Roster-Tail) hat Vorrang — Owner-Algorithmus „Plan sagt
    D-ABYO → Reg→Hex→ADS-B findet ihn, wo auch immer er ist"."""
    seen = {}

    def fake_machine_live(reg, want_route=True, targeted=False):
        seen['reg'] = reg
        return ('3c4b2f', 'DLH511', _live_pos(), None)

    monkeypatch.setattr(BP, '_machine_live', fake_machine_live)
    _patch_life_app(monkeypatch, _fake_sb([]),
                    extra={'_flight_obs_merged': lambda *a, **k: None})
    r = client.get('/api/ax/flight-live/TESTTOKEN'
                   '?flight_no=LH716&date=2026-07-05&reg=D-ABYO'
                   '&dep_iata=FRA&arr_iata=HND')
    b = r.get_json()
    assert seen['reg'] == 'D-ABYO'           # Hint wurde getrackt, nie geraten
    assert b['reg'] == 'D-ABYO'
    assert b['in_flight'] is True
    # Leg-Airports aus dem Query dienen als Route-Fallback (keine Live-Route).
    assert (b['dep'] or {}).get('iata') == 'FRA'
    assert (b['dest'] or {}).get('iata') == 'HND'


def test_flight_live_no_sources_stays_honest(client, monkeypatch):
    monkeypatch.setattr(BP, '_machine_live',
                        lambda reg, want_route=True, targeted=False: (None, None, None, None))
    _patch_life_app(monkeypatch, _fake_sb([]),
                    extra={'_flight_obs_merged': lambda *a, **k: None})
    r = client.get('/api/ax/flight-live/TESTTOKEN'
                   '?flight_no=LH716&date=2026-07-05')
    b = r.get_json()
    assert b['reg'] is None                  # NIE raten
    assert b['in_flight'] is False


# ══════════════════════════════════════════════════════════════════════════════
# _build_inbound_chain — Reg-Kaskade Merge → SB-Tages-Rows → Roster-Hint
# ══════════════════════════════════════════════════════════════════════════════
def _patch_chain_deps(monkeypatch):
    monkeypatch.setattr(BP, '_machine_live',
                        lambda reg, want_route=True, targeted=False: (None, None, None, None))
    monkeypatch.setattr(BP, '_inbound_arr_row_by_reg', lambda dep, reg: None)
    monkeypatch.setattr(BP, '_reg_hex_typecode_free', lambda reg: (None, None))


def test_inbound_chain_reg_from_sb_beats_hint(monkeypatch):
    sb_rows = [_obs_row('FRA', 'HND', flight='LH716', date='2026-07-05',
                        reg='DABYO', type_code='B748')]
    _patch_life_app(monkeypatch, _fake_sb(sb_rows))
    _patch_chain_deps(monkeypatch)
    chain, forecast, my = BP._build_inbound_chain(
        'LH716', '2026-07-05', 'FRA', reg_hint='D-HINT')
    # Beobachtete Tages-Row (echtes Board) schlägt den Plan-Hint.
    assert chain['reg'] == 'DABYO'
    assert chain['aircraft_type'] == 'B748'


def test_inbound_chain_falls_back_to_roster_hint(monkeypatch):
    _patch_life_app(monkeypatch, _fake_sb([]))
    _patch_chain_deps(monkeypatch)
    chain, forecast, my = BP._build_inbound_chain(
        'LH716', '2026-07-05', 'FRA', reg_hint='D-ABYO')
    assert chain['reg'] == 'D-ABYO'          # echter Roster-Tail, nie geraten


def test_inbound_chain_no_sources_no_reg(monkeypatch):
    _patch_life_app(monkeypatch, _fake_sb([]))
    _patch_chain_deps(monkeypatch)
    chain, forecast, my = BP._build_inbound_chain('LH716', '2026-07-05', 'FRA')
    assert chain['reg'] is None
    assert forecast['confidence'] == 'keine'


# ══════════════════════════════════════════════════════════════════════════════
# FR24-gRPC-Korridor — Routen-Gate (Reg-Match + route_to)
# ══════════════════════════════════════════════════════════════════════════════
def _patch_corridor_deps(monkeypatch, corr):
    """Kette bis zum Korridor-Block: kein freies ADS-B, kein Snapshot, aber ein
    Ankunfts-Board kennt den Zubringer (inbound_origin=HND) — der Korridor ist
    der einzige Positions-Beschaffer. `corr` = gemockte FR24-Antwort."""
    _patch_life_app(monkeypatch, _fake_sb([]))
    monkeypatch.setattr(BP, '_machine_live',
                        lambda reg, want_route=True, targeted=False: (None, None, None, None))
    monkeypatch.setattr(BP, '_inbound_arr_row_by_reg',
                        lambda dep, reg: {'flight': 'LH717', 'dest_iata': 'HND',
                                          'sched': None, 'esti': None})
    monkeypatch.setattr(BP, '_reg_hex_typecode_free', lambda reg: (None, None))
    monkeypatch.setattr(BP, '_aircraft_live_pos',
                        lambda **kw: (None, None, None, None))
    monkeypatch.setattr(BP, '_iata_latlon',
                        lambda code: {'FRA': (50.03, 8.57),
                                      'HND': (35.55, 139.78)}.get(code))
    import blueprints.fr24_grpc as G
    monkeypatch.setattr(G, 'inbound_by_route', lambda *a, **k: corr)


def _corr_hit(**kw):
    base = {'reg': 'D-ABYO', 'lat': 55.0, 'lon': 60.0, 'track': 90,
            'alt': 38000, 'speed': 480, 'flight_stage': 'AIRBORNE',
            'eta': 1783000000, 'sched_arr': 1782998000, 'route_to': None}
    base.update(kw)
    return base


def test_corridor_reg_match_without_route_keeps_position(monkeypatch):
    """FR24 liefert manchmal KEINE Route mit (route_to=None) — der per Reg
    VERIFIZIERTE Treffer darf dann nicht verworfen werden, sonst verliert der
    Russland/Ozean-Zubringer still Position+ETA (Review-Regression 2026-07-09)."""
    _patch_corridor_deps(monkeypatch, _corr_hit(route_to=None))
    chain, forecast, my = BP._build_inbound_chain(
        'LH716', '2026-07-05', 'FRA', reg_hint='D-ABYO')
    assert chain['inbound_live'] is not None
    assert chain['inbound_live']['source'] == 'fr24_grpc_corridor'
    assert chain['inbound_est_arr'] is not None


def test_corridor_route_mismatch_rejects_position(monkeypatch):
    """Echter Routen-MISMATCH (Tail fliegt gerade einen anderen Leg im
    Korridor) → verwerfen: kein Geister-Zubringer."""
    _patch_corridor_deps(monkeypatch, _corr_hit(route_to='MUC'))
    chain, forecast, my = BP._build_inbound_chain(
        'LH716', '2026-07-05', 'FRA', reg_hint='D-ABYO')
    assert chain['inbound_live'] is None
    assert chain['inbound_est_arr'] is None


def test_corridor_route_match_still_accepts(monkeypatch):
    """route_to == mein Abflughafen → wie bisher übernehmen."""
    _patch_corridor_deps(monkeypatch, _corr_hit(route_to='FRA'))
    chain, forecast, my = BP._build_inbound_chain(
        'LH716', '2026-07-05', 'FRA', reg_hint='D-ABYO')
    assert chain['inbound_live'] is not None


# ══════════════════════════════════════════════════════════════════════════════
# FlightState-Engine-Gate für inbound_live (Owner 2026-07-13)
#   Der Zubringer-Leg läuft durch die REINE Engine — inbound_live wird NUR
#   gesetzt, wenn das Airborne-Gate bestanden hat. Ein rollender/pushback-
#   Zubringer (on_ground=false, alt=None, gs klein am Vorfeld) ist KEIN
#   „kommt-schon"-Signal mehr, obwohl die Live-Route on-route ist. Und die
#   forecast.confidence darf dann nicht „hoch" sein.
# ══════════════════════════════════════════════════════════════════════════════
def _patch_engine_gate_deps(monkeypatch, pos, route_dst='FRA'):
    """_machine_live liefert einen on-route Live-Fix (Route endet an dep=FRA),
    kein Ankunfts-Board (arr_row=None), kein Snapshot, keine SB-Rows. So ist der
    ADS-B-`pos`-Pfad + Engine-Gate der einzige Positions-Beschaffer."""
    _patch_life_app(monkeypatch, _fake_sb([]))
    _route = {'src': route_dst and 'HND', 'dst': route_dst}
    monkeypatch.setattr(BP, '_machine_live',
                        lambda reg, want_route=True, targeted=False:
                        ('3C6DXX', 'DLH716', pos, _route))
    monkeypatch.setattr(BP, '_inbound_arr_row_by_reg',
                        lambda dep, reg: {'flight': 'LH716', 'dest_iata': 'HND',
                                          'sched': None, 'esti': None})
    monkeypatch.setattr(BP, '_reg_hex_typecode_free', lambda reg: (None, None))
    monkeypatch.setattr(BP, '_aircraft_live_pos',
                        lambda **kw: (None, None, None, None))
    monkeypatch.setattr(BP, '_iata_latlon',
                        lambda code: {'FRA': (50.03, 8.57),
                                      'HND': (35.55, 139.78)}.get(code))
    monkeypatch.setattr(BP, '_iata_elev_ft', lambda code: None)
    # Kein FR24-Korridor (der würde sonst on-route greifen und die Position
    # unabhängig vom Engine-Gate füllen).
    import blueprints.fr24_grpc as G
    monkeypatch.setattr(G, 'inbound_by_route', lambda *a, **k: None)


def test_inbound_live_taxi_pushback_not_flying(monkeypatch):
    """§1.2-Antipattern-Fix: on-route Live-Fix, aber on_ground=false + alt=None +
    gs=15 am Vorfeld (Pushback/Taxi). Das rohe on_ground-Bit sagte früher „in der
    Luft & on-route" → inbound_live gesetzt, forecast 'hoch'. Die Engine gatet das
    weg: KEINE Live-Position, Prognose nicht 'hoch'."""
    taxi = {'lat': 50.03, 'lon': 8.57, 'track': 200, 'gs': 15,
            'alt': None, 'on_ground': False}
    _patch_engine_gate_deps(monkeypatch, taxi, route_dst='FRA')
    chain, forecast, my = BP._build_inbound_chain(
        'LH717', '2026-07-05', 'FRA', reg_hint='D-ABYO')
    assert chain['inbound_live'] is None          # Taxi ⇒ kein „kommt-schon"-Geist
    assert forecast['confidence'] != 'hoch'


def test_inbound_live_genuine_airborne_renders(monkeypatch):
    """Echter Reiseflug on-route (alt=35000, gs=440) → Airborne-Gate besteht →
    inbound_live wird gesetzt (kein Regress gegenüber vorher)."""
    cruise = {'lat': 48.5, 'lon': 20.0, 'track': 280, 'gs': 440,
              'alt': 35000, 'on_ground': False}
    _patch_engine_gate_deps(monkeypatch, cruise, route_dst='FRA')
    chain, forecast, my = BP._build_inbound_chain(
        'LH717', '2026-07-05', 'FRA', reg_hint='D-ABYO')
    assert chain['inbound_live'] is not None
    assert chain['inbound_live']['alt'] == 35000


def test_inbound_live_offroute_still_rejected(monkeypatch):
    """Route-Consistency-Vorfilter bleibt: fliegt der Tail gerade einen ANDEREN
    Leg (Live-Route endet nicht an dep) → Position verwerfen, selbst wenn echt
    airborne (Owner „stimmt nicht": D-ABYM über Miami)."""
    cruise = {'lat': 25.8, 'lon': -80.3, 'track': 90, 'gs': 450,
              'alt': 36000, 'on_ground': False}
    _patch_engine_gate_deps(monkeypatch, cruise, route_dst='MIA')  # dst != dep
    chain, forecast, my = BP._build_inbound_chain(
        'LH717', '2026-07-05', 'FRA', reg_hint='D-ABYO')
    assert chain['inbound_live'] is None


# ══════════════════════════════════════════════════════════════════════════════
# _inbound_arr_row_by_reg — Landung-Erkennung über die Engine-Klassifikation
#   + Ankunfts-Zeit-Plausi (bogus „gelandet" mit Zukunfts-Ankunft verworfen)
# ══════════════════════════════════════════════════════════════════════════════
def _patch_board_rows(monkeypatch, rows, to_utc=None):
    extra = {'_cached_board_rows': lambda ap, kind: rows}
    if to_utc is not None:
        extra['_board_local_to_utc_iso'] = to_utc
    _patch_life_app(monkeypatch, _fake_sb([]), extra=extra)


def test_inbound_arr_row_real_landed_hidden(monkeypatch):
    """Echtes „Gelandet" (Engine LANDED, hart) → Row wird ausgeblendet
    (schon da, kein kommender Zubringer)."""
    rows = [{'reg': 'D-ABYO', 'flight': 'LH716', 'dest_iata': 'HND',
             'sched': '10:00', 'esti': '10:05', 'status': 'Gelandet'}]
    _patch_board_rows(monkeypatch, rows)
    assert BP._inbound_arr_row_by_reg('FRA', 'D-ABYO') is None


def test_inbound_arr_row_expected_not_landed(monkeypatch):
    """„Arrival expected 14:05" enthält das Substring „arriv…" — der alte
    Substring-Check hätte es fälschlich als gelandet gewertet und den Zubringer
    ausgeblendet. Die Engine-Klassifikation liefert KEIN Landungssignal → Row
    bleibt als kommender Zubringer erhalten."""
    rows = [{'reg': 'D-ABYO', 'flight': 'LH716', 'dest_iata': 'HND',
             'sched': '14:00', 'esti': '14:05', 'status': 'Arrival expected'}]
    _patch_board_rows(monkeypatch, rows)
    r = BP._inbound_arr_row_by_reg('FRA', 'D-ABYO')
    assert r is not None and r['flight'] == 'LH716'


def test_inbound_arr_row_bogus_landed_future_arrival(monkeypatch):
    """Bogus-Landung: Status behauptet „Gelandet", die Ankunftszeit liegt aber
    noch klar in der ZUKUNFT (Board-Wanduhr → UTC weit voraus) → physisch
    unmöglich, „gelandet" verworfen → Row bleibt als Zubringer erhalten."""
    from datetime import datetime, timezone, timedelta
    # Ankunft ~2 h in der Zukunft, als UTC-ISO (das to_utc-Mock reicht sie durch).
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    rows = [{'reg': 'D-ABYO', 'flight': 'LH716', 'dest_iata': 'HND',
             'sched': '99:99', 'esti': '99:99', 'status': 'Gelandet'}]
    _patch_board_rows(monkeypatch, rows, to_utc=lambda hhmm, iata: future)
    r = BP._inbound_arr_row_by_reg('FRA', 'D-ABYO')
    assert r is not None and r['flight'] == 'LH716'


def test_inbound_arr_row_landed_past_arrival_hidden(monkeypatch):
    """Gegencheck: „Gelandet" mit Ankunft in der VERGANGENHEIT ist plausibel →
    Row wird korrekt ausgeblendet (die Plausi verwirft nur Zukunfts-Landungen)."""
    from datetime import datetime, timezone, timedelta
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    rows = [{'reg': 'D-ABYO', 'flight': 'LH716', 'dest_iata': 'HND',
             'sched': '08:00', 'esti': '08:05', 'status': 'Gelandet'}]
    _patch_board_rows(monkeypatch, rows, to_utc=lambda hhmm, iata: past)
    assert BP._inbound_arr_row_by_reg('FRA', 'D-ABYO') is None


def test_flight_info_p5_fill_skips_stale_facts(client):
    """P5-Merge-Fill: fällt _flight_facts_from_obs auf den VORTAG zurück
    (facts['stale']=True), darf die gestrige Geister-Ankunft NICHT in
    flight-info landen — auch wenn `out` selbst frisch ist."""
    rows = [_obs_row('TIA', 'FRA', dest_name='Frankfurt')]
    stale_ff = {'sched_arr': '2026-06-30T18:05:00+02:00',
                'est_arr': '2026-06-30T18:20:00+02:00',
                'arr_status': 'Gelandet', 'arr_delay_min': 15,
                'stale': True, 'obs_date': '2026-06-30'}
    with patch.object(A, 'sb', _fake_sb(rows)), \
         patch.object(A, 'SB_AVAILABLE', True), \
         patch.object(A, '_flight_from_live_board', return_value=None), \
         patch.object(A, '_arrival_gate_terminal', return_value=(None, None)), \
         patch.object(A, '_flight_obs_merged', return_value=None), \
         patch.object(BP, '_flight_facts_from_obs', return_value=stale_ff):
        r = _flight_info(client)
    b = r.get_json()
    assert b['found'] is True
    assert not b.get('esti_arr')
    assert not b.get('arr_status')


def test_flight_info_p5_fill_uses_fresh_facts(client):
    """Frische (nicht-stale) Obs-Fakten füllen die Ankunftsseite wie gedacht."""
    rows = [_obs_row('TIA', 'FRA', dest_name='Frankfurt')]
    fresh_ff = {'sched_arr': '2026-07-01T18:05:00+02:00',
                'est_arr': '2026-07-01T18:20:00+02:00',
                'arr_status': 'Gelandet', 'arr_delay_min': 15}
    with patch.object(A, 'sb', _fake_sb(rows)), \
         patch.object(A, 'SB_AVAILABLE', True), \
         patch.object(A, '_flight_from_live_board', return_value=None), \
         patch.object(A, '_arrival_gate_terminal', return_value=(None, None)), \
         patch.object(A, '_flight_obs_merged', return_value=None), \
         patch.object(BP, '_flight_facts_from_obs', return_value=fresh_ff):
        r = _flight_info(client)
    b = r.get_json()
    assert b.get('esti_arr') == '2026-07-01T18:20:00+02:00'
    assert b.get('arr_status') == 'Gelandet'
