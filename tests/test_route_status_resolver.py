# ═══════════════════════════════════════════════════════════════
#  Increment 2 — EINE Quelle für ROUTE und STATUS eines Fluges (LH506-Frage)
#  (blueprints.warehouse_reader.route_for_flight / status_for_flight)
#
#  Owner-Ziel (2026-07-06): „Wenn ich LH506 anschaue — woher kommen die Daten?
#  Alles muss aus EINER Quelle, konsistent." Route/Status/Suche ziehen NEU
#  free-first aus UNSEREN Tabellen; bezahlt (AeroDataBox/AviationStack) nur als
#  letzter, budget-gedeckelter Notnagel. Diese Suite beweist die harten Punkte:
#
#   (a) route_for_flight nimmt Board/Warehouse/fr24 VOR bezahlt; ein Board-/
#       Warehouse-/fr24-Treffer erreicht die bezahlten Tiers NIE.
#       for_search=True nutzt das LEG-ZEITFENSTER-Gate (voriges/gelandetes Leg
#       wird abgelehnt), NICHT das Positions-Gate.
#   (b) status_for_flight: Board 'landed' ist autoritativ (schlägt sogar ADS-B
#       airborne); on_ground am ZIEL nach airborne = landed; on_ground am ORIGIN
#       vor Abflug = taxi-out (grounded), NICHT landed; delay kommt IMMER aus
#       _flight_obs_merged mit delay_known-Flag (unbekannt ≠ pünktlich).
#   (c) KEIN Paid-Call wenn allow_paid=False (Route) bzw. free_only-Merge (Status).
#
#  KEIN echter Netz-/DB-Zugriff: der aerox_data_blueprint-Helferzoo und die
#  app-Helfer (_flight_obs_merged via _life_app) werden gemockt.
# ═══════════════════════════════════════════════════════════════
import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

import app  # noqa: F401 — Blueprint-Registrierung vor Direkt-Import
import blueprints.aerox_data_blueprint as D
from blueprints import warehouse_reader as WR


# ═══════════════════════════════════════════════════════════════
#  Isolations-Fixture: JEDE freie/bezahlte Quelle „leer/kein Signal" als Default.
#  Jeder Test überschreibt nur die eine Quelle, die er prüft. So kann kein Test
#  versehentlich extern/bezahlt gehen. Gibt einen Container mit dem gemockten
#  _flight_obs_merged zurück (Status-Tests setzen dessen return_value/prüfen Args).
# ═══════════════════════════════════════════════════════════════
class _Ctx:
    pass


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    ctx = _Ctx()

    # ── Route-Kaskade: alle FREIEN Quellen als sauberer Miss ──
    monkeypatch.setattr(D, '_today_utc', lambda: '2026-07-06')
    monkeypatch.setattr(D, '_cache_get', lambda *a, **k: None)
    monkeypatch.setattr(D, '_route_from_obs', lambda cs: None)
    monkeypatch.setattr(D, '_route_from_warehouse', lambda *a, **k: None)
    monkeypatch.setattr(D, '_route_from_fr24', lambda *a, **k: None)
    # (_free_generic_route wurde 2026-07-09 gelöscht — 0 Aufrufer, Owner hatte
    #  die adsbdb/adsb.lol/hexdb-Generik schon 2026-07-03 deaktiviert.)
    # Tier-0 aircraft_live (Ultraplan Phase 1) — default sauberer Miss.
    monkeypatch.setattr(D, '_aircraft_live_flight', lambda *a, **k: None)
    # Gate für den Nicht-Suche-Pfad: standardmäßig „widerspricht nicht" → True.
    monkeypatch.setattr(D, '_geometry_allows_route', lambda *a, **k: True)
    # Persistenz-Write ist ein No-op im Test (kein SB).
    monkeypatch.setattr(D, '_record_resolved_route', MagicMock())

    # ── BEZAHLT: Budget offen, aber Provider als scharf beobachtete Mocks ──
    monkeypatch.setattr(D, '_paid_budget_ok', lambda: True)
    monkeypatch.setattr(D, '_aerodatabox_route', MagicMock(return_value=None))
    monkeypatch.setattr(D, '_aviationstack_route', MagicMock(return_value=None))

    # ── Status-Kaskade: Callsign→IATA-Flugnr + Geo-Helfer + Board-Merge ──
    monkeypatch.setattr(D, '_callsign_to_iata_flightno', lambda cs: 'LH506')
    _coords = {'FRA': (50.03, 8.55), 'MUC': (48.35, 11.79),
               'HND': (35.55, 139.78), 'JFK': (40.64, -73.78)}
    monkeypatch.setattr(D, '_iata_latlon', lambda code: _coords.get((code or '').upper()))
    # _gc_km: 0 wenn der Query-Punkt EXAKT auf dem Airport liegt, sonst weit weg.
    monkeypatch.setattr(D, '_gc_km',
                        lambda a, b, c, d: 0.0 if (a, b) == (c, d) else 9999.0)

    # _flight_obs_merged (aus app.py) wird via _life_app gezogen — hier gemockt.
    merged = MagicMock(return_value=None)
    ctx.merged = merged

    def _life_app(name, default=None):
        if name == '_flight_obs_merged':
            return merged
        if name == '_board_local_to_utc_iso':
            # None → _leg_time_epoch nutzt str(val); Tests liefern volle ISO-TS.
            return None
        return default

    monkeypatch.setattr(D, '_life_app', _life_app)
    return ctx


def _iso(dt):
    return dt.isoformat()


# ═══════════════════════════════════════════════════════════════
#  (a) ROUTE: frei VOR bezahlt
# ═══════════════════════════════════════════════════════════════

def test_aircraft_live_route_is_tier0_beats_board_and_warehouse(monkeypatch):
    """aircraft_live (echter Funkname + frische origin/dest, Ultraplan Phase 1) ist
    Tier 0: schlägt Board UND Warehouse — löst LH1412=DLH8UA-Falschroute an der
    Wurzel (Radar-Tap hatte aircraft_live gar nicht in der Kaskade)."""
    monkeypatch.setattr(D, '_aircraft_live_flight',
                        lambda *a, **k: {'dep_iata': 'FRA', 'arr_iata': 'BEG',
                                         'reg': 'DAINY'})
    # Board + Warehouse würden eine ANDERE (falsche) Route liefern — dürfen aber
    # gar nicht erst gefragt werden, weil Tier 0 vorher trifft.
    board = MagicMock(return_value={'src': 'FRA', 'dst': 'SPU'})
    wh = MagicMock(return_value={'src': 'FRA', 'dst': 'SPU'})
    monkeypatch.setattr(D, '_route_from_obs', board)
    monkeypatch.setattr(D, '_route_from_warehouse', wh)
    route = WR.route_for_flight(callsign='DLH8UA', reg='DAINY')
    assert route['src'] == 'FRA' and route['dst'] == 'BEG'   # nicht SPU
    assert route['source'] == 'aircraft_live'
    board.assert_not_called()
    wh.assert_not_called()


def test_board_route_beats_paid_and_never_queries_bare_cs(monkeypatch):
    """Ein Board-Treffer (airport_delay_obs) wird geliefert und die BEZAHLTEN
    Tiers werden NIE angefasst — obwohl allow_paid=True und Budget offen ist.
    Zusätzlich: der NACKTE-CS-Key wird NIE für den Cache-Lookup benutzt."""
    seen_keys = []
    monkeypatch.setattr(D, '_cache_get',
                        lambda table, col, key: seen_keys.append(key) or None)
    monkeypatch.setattr(D, '_route_from_obs',
                        lambda cs: {'src': 'MUC', 'dst': 'FRA'})

    route = WR.route_for_flight(callsign='LH506', hex='3c6dd9',
                                lat=48.0, lon=11.0, allow_paid=True)

    assert route is not None
    assert route['source'] == 'aerox_board'
    assert route['confidence'] == 'confirmed'        # EIN confidence-Feld
    # Bezahlt NIE erreicht.
    D._aerodatabox_route.assert_not_called()
    D._aviationstack_route.assert_not_called()
    # Cache nur date-gekeyt (CS@YYYYMMDD), nie der mehrdeutige nackte CS.
    assert 'LH506@20260706' in seen_keys
    assert 'LH506' not in seen_keys


def test_warehouse_route_beats_paid(monkeypatch):
    """Board leer, aber Flight-Warehouse (flights) trifft → bezahlt NIE."""
    monkeypatch.setattr(D, '_route_from_warehouse',
                        lambda h, r: {'src': 'FRA', 'dst': 'HND'})

    route = WR.route_for_flight(callsign='LH506', hex='3c6dd9',
                                lat=45.0, lon=60.0, allow_paid=True)

    assert route['src'] == 'FRA' and route['dst'] == 'HND'
    assert route['confidence'] == 'confirmed'
    D._aerodatabox_route.assert_not_called()
    D._aviationstack_route.assert_not_called()


def test_fr24_route_beats_paid_and_is_estimated(monkeypatch):
    """Cache/Board/Warehouse leer, fr24_live (gratis) trifft → bezahlt NIE.
    fr24 ist die GRATIS-Generik → confidence 'estimated' (nicht 'confirmed')."""
    monkeypatch.setattr(D, '_route_from_fr24',
                        lambda cs, h: {'src': 'FRA', 'dst': 'JFK'})

    route = WR.route_for_flight(callsign='LH506', hex='3c6dd9',
                                lat=45.0, lon=-30.0, allow_paid=True)

    assert route['src'] == 'FRA' and route['dst'] == 'JFK'
    assert route['confidence'] == 'estimated'
    D._aerodatabox_route.assert_not_called()
    D._aviationstack_route.assert_not_called()


# ═══════════════════════════════════════════════════════════════
#  (a) ROUTE-SUCHE: Leg-Zeitfenster-Gate statt Positions-Gate
# ═══════════════════════════════════════════════════════════════

def test_for_search_rejects_previous_leg_by_time_window(monkeypatch):
    """for_search=True (keine Live-Position): eine Cache-Row, deren Leg LÄNGST
    gelandet ist (voriges Leg), wird vom Leg-Zeitfenster-Gate abgelehnt. Da alle
    anderen Quellen leer sind und allow_paid=False → das Ergebnis ist None
    (das veraltete Leg wird NICHT als „jetzt" ausgegeben)."""
    past = {'src': 'FRA', 'dst': 'HND',
            'est_dep': '2020-01-01T10:00:00+00:00',
            'est_arr': '2020-01-01T22:00:00+00:00'}
    monkeypatch.setattr(D, '_cache_get',
                        lambda table, col, key: dict(past)
                        if key == 'LH506@20260706' else None)

    route = WR.route_for_flight(callsign='LH506', for_search=True,
                                allow_paid=False)

    assert route is None      # voriges Leg abgelehnt, kein anderer Treffer


def test_for_search_rejects_previous_leg_free_shape_via_board_arr(_isolated, monkeypatch):
    """ECHTE freie Shape (fr24/Board-Route): der Cache-Treffer trägt NUR est_dep
    (Abflug ~7 h her, same-day), KEIN arr. Das Leg-Fenster-Gate kann aus der Row
    ALLEIN nichts widerlegen (Abflug < 18 h her). Erst die aus _flight_obs_merged
    nachgezogene, bereits VERGANGENE Ankunftszeit lässt das gelandete voriGe Leg
    fallen → Ergebnis None (das veraltete Leg wird NICHT gezeigt).

    Das ist der Fall, den der handgebaute est_arr-Test (oben) maskierte."""
    now = datetime.now(timezone.utc)
    # Freie Shape: nur Abflug, KEIN arr — genau wie fr24/Board-Route liefern.
    free_leg = {'src': 'FRA', 'dst': 'HND',
                'est_dep': _iso(now - timedelta(hours=7))}
    monkeypatch.setattr(D, '_cache_get',
                        lambda table, col, key: dict(free_leg)
                        if key == 'LH506@20260706' else None)
    # Board kennt die ECHTE (längst vergangene) Ankunft → Leg ist gelandet.
    _isolated.merged.return_value = {
        'sched_arr': _iso(now - timedelta(hours=4)),
        'esti_arr': _iso(now - timedelta(hours=4)),
        'dep_iata': 'FRA', 'arr_iata': 'HND',
    }

    route = WR.route_for_flight(callsign='LH506', for_search=True,
                                allow_paid=False)

    assert route is None      # gelandetes Leg via Board-arr abgelehnt


def test_for_search_free_shape_kept_when_board_arr_still_future(_isolated, monkeypatch):
    """Gegenprobe zur freien Shape: nur est_dep (vor 1 h), Board-arr liegt noch in
    der Zukunft (+1 h) → das Leg ist gerade unterwegs → Gate akzeptiert es."""
    now = datetime.now(timezone.utc)
    free_leg = {'src': 'FRA', 'dst': 'HND',
                'est_dep': _iso(now - timedelta(hours=1))}
    monkeypatch.setattr(D, '_cache_get',
                        lambda table, col, key: dict(free_leg)
                        if key == 'LH506@20260706' else None)
    _isolated.merged.return_value = {
        'sched_arr': _iso(now + timedelta(hours=1)),
        'esti_arr': _iso(now + timedelta(hours=1)),
        'dep_iata': 'FRA', 'arr_iata': 'HND',
    }

    route = WR.route_for_flight(callsign='LH506', for_search=True,
                               allow_paid=False)

    assert route is not None
    assert route['src'] == 'FRA' and route['dst'] == 'HND'


def test_for_search_accepts_current_leg_by_time_window(monkeypatch):
    """Gegenprobe: dieselbe Cache-Quelle, aber das Leg-Zeitfenster schließt JETZT
    ein (Abflug -1h, Ankunft +1h) → das Gate akzeptiert die Row."""
    now = datetime.now(timezone.utc)
    cur = {'src': 'FRA', 'dst': 'HND',
           'est_dep': _iso(now - timedelta(hours=1)),
           'est_arr': _iso(now + timedelta(hours=1))}
    monkeypatch.setattr(D, '_cache_get',
                        lambda table, col, key: dict(cur)
                        if key == 'LH506@20260706' else None)

    route = WR.route_for_flight(callsign='LH506', for_search=True,
                                allow_paid=False)

    assert route is not None
    assert route['src'] == 'FRA' and route['dst'] == 'HND'
    assert route['confidence'] == 'confirmed'         # Cache = confirmed


def test_leg_window_gate_unit_previous_vs_current():
    """Direkt am Gate: Vergangenes Leg → False, aktuelles Leg → True; ohne
    ableitbare Zeiten (fr24/generisch) → im Zweifel True (nicht verstecken)."""
    now = datetime.now(timezone.utc)
    prev = {'src': 'FRA', 'dst': 'HND',
            'est_dep': '2020-01-01T10:00:00+00:00',
            'est_arr': '2020-01-01T22:00:00+00:00'}
    cur = {'src': 'FRA', 'dst': 'HND',
           'est_dep': _iso(now - timedelta(hours=1)),
           'est_arr': _iso(now + timedelta(hours=1))}
    future = {'src': 'FRA', 'dst': 'HND',
              'est_dep': _iso(now + timedelta(hours=8)),
              'est_arr': _iso(now + timedelta(hours=20))}
    assert WR._leg_window_allows(prev) is False        # längst gelandet
    assert WR._leg_window_allows(cur) is True          # jetzt unterwegs
    assert WR._leg_window_allows(future) is False       # klar in der Zukunft
    assert WR._leg_window_allows({'src': 'FRA', 'dst': 'HND'}) is True  # keine Zeiten


# ═══════════════════════════════════════════════════════════════
#  (c) ROUTE: KEIN Paid-Call wenn allow_paid=False / Budget zu
# ═══════════════════════════════════════════════════════════════

def test_route_allow_paid_false_never_calls_paid():
    """Alle freien Quellen leer (Default-Fixture) + allow_paid=False → weder
    AeroDataBox noch AviationStack werden angefasst; Ergebnis None."""
    route = WR.route_for_flight(callsign='LH506', hex='3c6dd9',
                                lat=45.0, lon=10.0, allow_paid=False)

    assert route is None
    D._aerodatabox_route.assert_not_called()
    D._aviationstack_route.assert_not_called()


def test_route_paid_skipped_when_budget_closed(monkeypatch):
    """allow_paid=True, aber Tages-Budget erschöpft (_paid_budget_ok False) →
    der bezahlte Notnagel bleibt zu."""
    monkeypatch.setattr(D, '_paid_budget_ok', lambda: False)

    route = WR.route_for_flight(callsign='LH506', hex='3c6dd9',
                                lat=45.0, lon=10.0, allow_paid=True)

    assert route is None
    D._aerodatabox_route.assert_not_called()
    D._aviationstack_route.assert_not_called()


def test_route_paid_used_only_as_last_resort(monkeypatch):
    """Alle freien Quellen leer + allow_paid=True + Budget offen → GENAU der
    budget-gedeckelte Notnagel (AeroDataBox) liefert und wird als 'confirmed'
    zurückgegeben."""
    monkeypatch.setattr(D, '_aerodatabox_route',
                        MagicMock(return_value={'src': 'FRA', 'dst': 'HND',
                                                'reg': 'D-AIXA'}))

    route = WR.route_for_flight(callsign='LH506', hex='3c6dd9',
                                lat=45.0, lon=60.0, allow_paid=True)

    assert route is not None
    assert route['src'] == 'FRA' and route['dst'] == 'HND'
    assert route['confidence'] == 'confirmed'
    D._aerodatabox_route.assert_called_once()
    # AviationStack wird gar nicht mehr gebraucht (ADB traf).
    D._aviationstack_route.assert_not_called()


# ═══════════════════════════════════════════════════════════════
#  (b) STATUS: Board autoritativ + on_ground-Kontext + delay_known
# ═══════════════════════════════════════════════════════════════

def test_status_board_landed_is_authoritative(_isolated):
    """Board sagt 'Landed' am ZIEL → phase 'landed', source 'board', AUCH wenn
    ADS-B on_ground=False (airborne) hereinkommt. Gate/Zeiten von der ANKUNFTS-
    Seite. delay aus dem Merge mit delay_known=True."""
    _isolated.merged.return_value = {
        'status_arr': 'Landed 14:23', 'status_dep': 'Departed',
        'cancelled': False, 'delay_known': True, 'delay_min': 12,
        'gate_arr': 'B23', 'terminal_arr': '1', 'sched_arr': '14:10',
        'esti_arr': '14:23', 'dep_iata': 'MUC', 'arr_iata': 'FRA',
    }

    out = WR.status_for_flight(callsign='LH506', origin='MUC', dest='FRA',
                               on_ground=False)   # ADS-B würde 'airborne' sagen

    assert out['phase'] == 'landed'          # Board schlägt ADS-B airborne
    assert out['source'] == 'board'
    assert out['gate'] == 'B23'
    assert out['terminal'] == '1'
    assert out['sched'] == '14:10'
    assert out['delay_min'] == 12
    assert out['delay_known'] is True


def test_status_board_airborne_shows_without_position(_isolated):
    """Board-'Departed' (airborne) zeigt auch OHNE jede Position (on_ground=None)."""
    _isolated.merged.return_value = {
        'status_dep': 'Departed', 'status_arr': None, 'cancelled': False,
        'delay_known': True, 'delay_min': 5, 'gate_dep': 'A15',
        'sched_dep': '13:00', 'esti_dep': '13:05',
        'dep_iata': 'FRA', 'arr_iata': 'HND',
    }

    out = WR.status_for_flight(callsign='LH506', origin='FRA', dest='HND',
                               on_ground=None)

    assert out['phase'] == 'airborne'
    assert out['source'] == 'board'
    assert out['act'] == '13:05'             # Ist-Abflug = beobachtetes esti_dep
    assert out['delay_min'] == 5


def test_status_on_ground_at_dest_after_airborne_is_landed(_isolated):
    """Kein Board-Signal → ADS-B-Kontext: on_ground am ZIEL (nach airborne) =
    gelandet. Position liegt EXAKT auf dem Ziel-Airport (FRA), nicht am Origin."""
    _isolated.merged.return_value = None     # Board liefert keine Phase

    out = WR.status_for_flight(callsign='LH506', origin='MUC', dest='FRA',
                               on_ground=True, lat=50.03, lon=8.55)  # = FRA

    assert out['phase'] == 'landed'
    assert out['source'] == 'adsb'


def test_status_on_ground_at_origin_before_departure_is_taxi_out(_isolated):
    """Kern-Regel: on_ground am ORIGIN vor Abflug = taxi-out → 'grounded',
    NICHT 'landed'. Position liegt EXAKT auf dem Abflug-Airport (MUC)."""
    _isolated.merged.return_value = None

    out = WR.status_for_flight(callsign='LH506', origin='MUC', dest='FRA',
                               on_ground=True, lat=48.35, lon=11.79)  # = MUC

    assert out['phase'] == 'grounded'
    assert out['phase'] != 'landed'
    assert out['source'] == 'adsb'


def test_status_on_ground_unknown_position_is_grounded_not_landed(_isolated):
    """on_ground, aber Position weder eindeutig am Ziel noch am Start → am Boden,
    aber NICHT sicher gelandet → 'grounded'."""
    _isolated.merged.return_value = None

    out = WR.status_for_flight(callsign='LH506', origin='MUC', dest='FRA',
                               on_ground=True, lat=10.0, lon=10.0)  # nirgends

    assert out['phase'] == 'grounded'


def test_status_airborne_from_adsb_without_board(_isolated):
    """Kein Board, on_ground=False → 'airborne' (source adsb)."""
    _isolated.merged.return_value = None

    out = WR.status_for_flight(callsign='LH506', origin='FRA', dest='HND',
                               on_ground=False)

    assert out['phase'] == 'airborne'
    assert out['source'] == 'adsb'


def test_status_at_gate_on_dep_is_grounded(_isolated):
    """status_dep='At Gate' (am ORIGIN) → grounded, NICHT gelandet (seiten-bewusst).
    Harte Boden-Beobachtung → bleibt grounded (kein ADS-B im Spiel)."""
    _isolated.merged.return_value = {
        'status_dep': 'At Gate', 'status_arr': None, 'cancelled': False,
        'delay_known': False, 'delay_min': None,
        'gate_dep': 'A15', 'sched_dep': '13:00',
        'dep_iata': 'FRA', 'arr_iata': 'HND',
    }

    out = WR.status_for_flight(callsign='LH506', origin='FRA', dest='HND',
                               on_ground=None)

    assert out['phase'] == 'grounded'
    assert out['source'] == 'board'


def test_status_on_ground_dep_side_is_grounded(_isolated):
    """status_dep='On Ground' am ORIGIN → grounded (taxi-out), nicht gelandet."""
    _isolated.merged.return_value = {
        'status_dep': 'On Ground', 'status_arr': None, 'cancelled': False,
        'delay_known': False, 'delay_min': None,
        'dep_iata': 'FRA', 'arr_iata': 'HND',
    }

    out = WR.status_for_flight(callsign='LH506', origin='FRA', dest='HND')

    assert out['phase'] == 'grounded'
    assert out['source'] == 'board'


def test_status_at_gate_on_arr_is_landed(_isolated):
    """status_arr='At Gate' (am ZIEL) → landed (seiten-bewusst: Ankunfts-Gate)."""
    _isolated.merged.return_value = {
        'status_dep': 'Departed', 'status_arr': 'At Gate', 'cancelled': False,
        'delay_known': True, 'delay_min': 3,
        'gate_arr': 'B23', 'sched_arr': '14:10', 'esti_arr': '14:13',
        'dep_iata': 'MUC', 'arr_iata': 'FRA',
    }

    out = WR.status_for_flight(callsign='LH506', origin='MUC', dest='FRA')

    assert out['phase'] == 'landed'
    assert out['source'] == 'board'
    assert out['gate'] == 'B23'


def test_status_on_ground_arr_side_is_landed(_isolated):
    """status_arr='On Ground' am ZIEL → landed."""
    _isolated.merged.return_value = {
        'status_dep': 'Departed', 'status_arr': 'On Ground', 'cancelled': False,
        'delay_known': False, 'delay_min': None,
        'dep_iata': 'MUC', 'arr_iata': 'FRA',
    }

    out = WR.status_for_flight(callsign='LH506', origin='MUC', dest='FRA')

    assert out['phase'] == 'landed'
    assert out['source'] == 'board'


def test_status_stale_scheduled_yields_to_fresh_airborne(_isolated):
    """Owner-Regel: ein STALE Board 'Scheduled' + FRISCHE ADS-B on_ground=False
    (airborne) → airborne (source adsb), NICHT grounded. Der weiche pre-departure-
    Status darf den frischen Fix nicht auf grounded zurückwerfen."""
    _isolated.merged.return_value = {
        'status_dep': 'Scheduled', 'status_arr': None, 'cancelled': False,
        'delay_known': False, 'delay_min': None,
        'dep_iata': 'FRA', 'arr_iata': 'HND',
    }

    out = WR.status_for_flight(callsign='LH506', origin='FRA', dest='HND',
                               on_ground=False)   # frisches ADS-B airborne

    assert out['phase'] == 'airborne'
    assert out['source'] == 'adsb'


def test_status_stale_boarding_yields_to_fresh_airborne(_isolated):
    """Wie oben mit 'Boarding' — auch das weiche Boarding weicht frischem airborne."""
    _isolated.merged.return_value = {
        'status_dep': 'Boarding', 'status_arr': 'Estimated', 'cancelled': False,
        'delay_known': False, 'delay_min': None,
        'dep_iata': 'FRA', 'arr_iata': 'HND',
    }

    out = WR.status_for_flight(callsign='LH506', origin='FRA', dest='HND',
                               on_ground=False)

    assert out['phase'] == 'airborne'
    assert out['source'] == 'adsb'


def test_status_hard_on_ground_dep_keeps_grounded_despite_airborne_flag(_isolated):
    """Gegenprobe zur Weich/Hart-Grenze: eine HARTE Boden-Beobachtung
    (status_dep='On Ground') bleibt grounded — nur WEICHE pre-departure-Stati
    weichen frischem airborne, harte Beobachtungen bleiben autoritativ."""
    _isolated.merged.return_value = {
        'status_dep': 'On Ground', 'status_arr': None, 'cancelled': False,
        'delay_known': False, 'delay_min': None,
        'dep_iata': 'FRA', 'arr_iata': 'HND',
    }

    out = WR.status_for_flight(callsign='LH506', origin='FRA', dest='HND',
                               on_ground=False)

    assert out['phase'] == 'grounded'
    assert out['source'] == 'board'


# ═══════════════════════════════════════════════════════════════
#  (b) STATUS: delay aus _flight_obs_merged + delay_known-Flag
# ═══════════════════════════════════════════════════════════════

def test_status_delay_unknown_is_not_on_time(_isolated):
    """delay_known=False (nur-vor-Abflug-0 o.Ä.) → delay_min bleibt None, NICHT 0.
    Unbekannt ≠ pünktlich."""
    _isolated.merged.return_value = {
        'status_dep': None, 'status_arr': None, 'cancelled': False,
        'delay_known': False, 'delay_min': 0,
        'dep_iata': 'FRA', 'arr_iata': 'HND',
    }

    out = WR.status_for_flight(callsign='LH506', origin='FRA', dest='HND',
                               on_ground=None)

    assert out['delay_known'] is False
    assert out['delay_min'] is None          # NICHT 0 → nicht „pünktlich"


def test_status_delay_known_passthrough(_isolated):
    """delay_known=True → delay_min wird durchgereicht (auch ohne Board-Phase)."""
    _isolated.merged.return_value = {
        'status_dep': None, 'status_arr': None, 'cancelled': False,
        'delay_known': True, 'delay_min': 42,
        'dep_iata': 'FRA', 'arr_iata': 'HND',
    }

    out = WR.status_for_flight(callsign='LH506', origin='FRA', dest='HND',
                               on_ground=None)

    assert out['delay_known'] is True
    assert out['delay_min'] == 42
    assert out['phase'] == 'unknown'         # kein Board-Status, kein ADS-B


def test_status_board_cancelled(_isolated):
    """cancelled-Flag im Merge → phase 'cancelled', source 'board'."""
    _isolated.merged.return_value = {
        'status_dep': 'Cancelled', 'status_arr': None, 'cancelled': True,
        'delay_known': False, 'delay_min': None,
        'dep_iata': 'FRA', 'arr_iata': 'HND',
    }

    out = WR.status_for_flight(callsign='LH506', origin='FRA', dest='HND')

    assert out['phase'] == 'cancelled'
    assert out['source'] == 'board'


# ═══════════════════════════════════════════════════════════════
#  (c) STATUS: KEIN Paid — free_only-Merge im Default
# ═══════════════════════════════════════════════════════════════

def test_status_default_uses_free_only_merge(_isolated):
    """allow_paid=False (Default) → der Merge wird mit free_only=True gerufen
    (STRUKTURELL spend-frei: kein AeroDataBox-Spend im Status-Pfad)."""
    _isolated.merged.return_value = None

    WR.status_for_flight(callsign='LH506', origin='FRA', dest='HND',
                         allow_paid=False)

    _isolated.merged.assert_called_once()
    assert _isolated.merged.call_args.kwargs.get('free_only') is True


def test_status_allow_paid_lifts_free_only(_isolated):
    """allow_paid=True hebt das free_only-Gate (erlaubt die bezahlten Board-Zweige
    des Merges) → free_only=False."""
    _isolated.merged.return_value = None

    WR.status_for_flight(callsign='LH506', origin='FRA', dest='HND',
                         allow_paid=True)

    assert _isolated.merged.call_args.kwargs.get('free_only') is False
