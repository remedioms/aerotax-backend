# Family-Watch: die Legacy-Felder flight_phase (_canonical_flight_phase) und
# flying_now (_flight_window_state) beziehen ihre Wahrheit jetzt aus der
# FlightState-Engine — DIESELBE Wahrheit wie crew_state/flights_live, auch OHNE
# Live-Fix (Funkloch/Ozean), board-only.
#
# Wurzel (Owner/Fable 2026-07-13, DESIGN §5): ohne Live-Fix blieb die Family-
# Karte am board-tokenisierten _status_phase_of hängen — und der mappte einen
# reinen DEP-seitigen „Abgeflogen"/„Departed" (off-block) FÄLSCHLICH auf
# 'airborne' (Geister-„fliegt gerade"). Die Engine ist seiten-bewusst
# (classify_board_status): „Abgeflogen" am ABFLUG = TAXI_OUT = grounded, NIE
# airborne. Zusätzlich: Landung-Monotonie/PLAUSI im _flight_window_state — eine
# beobachtete „gelandet" darf physisch nicht vor dem eff. Abflug liegen
# (Bogus-Landung eines stale Umlaufs derselben Flugnummer → verworfen).
import datetime as dt

from blueprints.family_watch import (_canonical_flight_phase,
                                      _phase_from_engine_board_only,
                                      _flight_window_state)


def _leg(status=None, **kw):
    base = {'flight': 'LH716', 'dep_iata': 'FRA', 'arr_iata': 'HND',
            'leg_index': 0}
    base.update(kw)
    if status is not None:
        base['status'] = status
    return base


# --- _phase_from_engine_board_only: der Ghost-Gate (side-aware) --------------

def test_dep_abgeflogen_is_grounded_not_airborne():
    # DER Ghost-Bug: dep-seitiges „Abgeflogen" = off-block, NICHT airborne.
    # Engine → TAXI_OUT → 'grounded'. (Legacy _status_phase_of(..,'arr') hätte
    # hier fälschlich 'airborne' geliefert — der Geister-Flieger.)
    assert _phase_from_engine_board_only(_leg('Abgeflogen')) == 'grounded'
    assert _phase_from_engine_board_only(_leg('Departed')) == 'grounded'


def test_enroute_is_airborne():
    assert _phase_from_engine_board_only(_leg('En Route')) == 'airborne'
    assert _phase_from_engine_board_only(_leg('Airborne')) == 'airborne'


def test_arr_landed_is_landed():
    assert _phase_from_engine_board_only(_leg('Landed 21:10')) == 'landed'
    # arr-seitiges „At Gate" am ZIEL = gelandet.
    assert _phase_from_engine_board_only(_leg('At Gate')) == 'landed'


def test_cancelled_is_cancelled():
    assert _phase_from_engine_board_only(_leg('Cancelled')) == 'cancelled'
    # cancelled auch über das explizite Flag (nicht nur den Status-String).
    assert _phase_from_engine_board_only(
        _leg('Scheduled', cancelled=True)) == 'cancelled'


def test_soft_scheduled_gives_no_engine_signal():
    # Reine Vorabflug-Schätzungen tragen KEIN Engine-Phasensignal → None,
    # der Aufrufer (_canonical_flight_phase) fällt auf das Legacy-Verhalten.
    assert _phase_from_engine_board_only(_leg('Estimated 12:40')) is None
    assert _phase_from_engine_board_only(_leg('Scheduled')) is None
    assert _phase_from_engine_board_only(_leg()) is None       # kein Status
    assert _phase_from_engine_board_only(None) is None


# --- _canonical_flight_phase: Engine primär, Legacy-Fallback ------------------

def test_canonical_uses_engine_for_ghost_case():
    # Über den öffentlichen Einstieg: kein Geister-'airborne' mehr für „Abgeflogen".
    assert _canonical_flight_phase([_leg('Abgeflogen')]) == 'grounded'
    assert _canonical_flight_phase([_leg('En Route')]) == 'airborne'
    assert _canonical_flight_phase([_leg('Landed 21:10')]) == 'landed'


def test_canonical_falls_back_to_legacy_when_engine_silent():
    # Engine liefert None (soft) → Legacy _status_phase_of übernimmt UNVERÄNDERT
    # (konservativ, kein Bruch der bisherigen Anzeige für weiche Stati).
    from blueprints.warehouse_reader import _status_phase_of
    for st in ('Scheduled', 'Estimated 12:40', 'Boarding'):
        assert (_canonical_flight_phase([_leg(st)])
                == _status_phase_of(st, 'arr'))


def test_canonical_empty_is_none():
    assert _canonical_flight_phase([]) is None
    assert _canonical_flight_phase(None) is None


# --- _flight_window_state: Landung-PLAUSI/Monotonie --------------------------

def _d(h, m=0, day=5):
    return dt.datetime(2026, 7, day, h, m, tzinfo=dt.timezone.utc)


_FLIGHT_DAY = {
    'datum': '2026-07-05', 'chain': ['FRA', 'HND'],
    'st': _d(12, 0), 'en': _d(21, 55),
    'st_iso': '2026-07-05T12:00:00Z', 'en_iso': '2026-07-05T21:55:00Z',
    'first': 'FRA', 'is_flight': True,
}


def test_bogus_landing_before_departure_is_rejected():
    # Eine „gelandet"-Zeile, während JETZT noch VOR dem (Plan-)Abflug liegt, ist
    # physisch unmöglich (stale Umlauf-Row derselben Flugnummer). Sie darf das
    # Fenster NICHT beenden — die Karte kippt nicht vorzeitig auf „gelandet".
    legs = [{'flight': 'LH716', 'delay_min': 0, 'status': 'Landed 03:00'}]
    now = _d(11, 30)                         # 30 min VOR Plan-Abflug 12:00
    flying, en_eff, landed = _flight_window_state(_FLIGHT_DAY, legs, now)
    assert landed is False                   # Bogus-Landung verworfen
    # (flying bleibt False, weil now < st — aber NICHT wegen der Landung.)


def test_bogus_landing_before_effective_departure_with_delay():
    # Selbst mit +90 min Abflug-Verspätung: um 13:00 ist der eff. Abflug (13:30)
    # noch nicht erreicht → eine „gelandet"-Zeile bleibt eine Bogus-Landung.
    legs = [{'flight': 'LH716', 'dep_delay_min': 90, 'delay_min': 90,
             'status': 'Landed'}]
    now = _d(13, 0)                          # eff. Abflug = 12:00 + 90 = 13:30
    flying, en_eff, landed = _flight_window_state(_FLIGHT_DAY, legs, now)
    assert landed is False


def test_plausible_landing_after_departure_ends_window():
    # Nach dem eff. Abflug ist eine „gelandet"-Zeile echt → Fenster beendet
    # (Landung-Monotonie: sobald plausibel gelandet, bleibt es beendet).
    legs = [{'flight': 'LH716', 'delay_min': 0, 'status': 'Landed 21:10'}]
    flying, en_eff, landed = _flight_window_state(_FLIGHT_DAY, legs, _d(21, 20))
    assert flying is False and landed is True


def test_no_landing_signal_plain_plan():
    # Kein Landungssignal → reiner Plan, kein Gate-Effekt.
    legs = [{'flight': 'LH716', 'delay_min': 30, 'status': 'estimated'}]
    flying, en_eff, landed = _flight_window_state(_FLIGHT_DAY, legs, _d(15, 0))
    assert flying is True and landed is False
    assert en_eff == _FLIGHT_DAY['en'] + dt.timedelta(minutes=30)
