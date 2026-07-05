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
                        lambda reg, want_route=True:
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

    def fake_machine_live(reg, want_route=True):
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
                        lambda reg, want_route=True: (None, None, None, None))
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
                        lambda reg, want_route=True: (None, None, None, None))
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
