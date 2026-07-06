# Family-Watch „Fliegt gerade"-Fenster — Root-Fix 2026-07-06.
#
# Owner-Bug (Device, 2026-07-06 12:44): Family sah „Fliegt gerade FRA→HND ·
# Ankunft 23:58" den GANZEN Layover-Tag in Tokio — weder die +75-Verspätung
# noch die Landung wurden bemerkt. Drei strukturelle Ursachen, hier fixiert:
#  1) Tour-Kette in ical_location machte auch reine Layover-Tage zu
#     „Flugtagen" (Ganztags-Fenster 00:00–23:5x).
#  2) Das Fenster kannte keine beobachtete Verspätung/Landung.
#  3) Red-Eye über UTC-Mitternacht (Vortags-Fenster) — im Loader abgedeckt,
#     die puren Bausteine hier.
import datetime as dt

from blueprints.family_watch import _parse_roster_day, _flight_window_state


def _d(h, m=0, day=6):
    return dt.datetime(2026, 7, day, h, m, tzinfo=dt.timezone.utc)


# --- _parse_roster_day: Flugtag vs. Layover-Tag -----------------------------

def test_layover_day_with_tour_chain_is_not_flight_day():
    # DER Bug: Ruhetag in Tokio, Location trägt die Tour-Kette, Fenster =
    # ganzer Tag → früher „Flugtag" mit Ankunft 23:58.
    day = _parse_roster_day({
        'datum': '2026-07-06',
        'ical_location': 'HND, FRA-HND',
        'ical_summary': 'Layover Tokio',
        'ical_start': '2026-07-06T00:00:00+00:00',
        'ical_end': '2026-07-06T23:58:00+00:00',
    })
    assert day['is_flight'] is False
    assert day['first'] == 'HND'          # Aufenthaltsort des Tages
    assert day['chain'] == ['FRA', 'HND']  # Kette bleibt lesbar, zählt nur nicht


def test_real_flight_day_from_summary_legs():
    day = _parse_roster_day({
        'datum': '2026-07-05',
        'ical_location': 'HND, FRA-HND',
        'ical_summary': 'LH716 FRA-HND 13:55',
        'ical_start': '2026-07-05T11:55:00+00:00',
        'ical_end': '2026-07-05T23:58:00+00:00',
    })
    assert day['is_flight'] is True
    assert day['chain'] == ['FRA', 'HND']


def test_long_duty_under_20h_still_flight_day():
    # 19h-Dienst (Ultra-Langstrecke + Briefing) bleibt ein Flugtag —
    # nur ECHTE Ganztags-Blöcke (≥20h) fallen raus.
    day = _parse_roster_day({
        'datum': '2026-07-05',
        'ical_location': 'SIN, FRA-SIN',
        'ical_summary': 'Dienst',
        'ical_start': '2026-07-05T02:00:00+00:00',
        'ical_end': '2026-07-05T21:00:00+00:00',
    })
    assert day['is_flight'] is True


# --- _flight_window_state: Plan + echte Beobachtung -------------------------

_FLIGHT_DAY = {
    'datum': '2026-07-05', 'chain': ['FRA', 'HND'],
    'st': _d(12, 0, day=5), 'en': _d(21, 55, day=5),
    'st_iso': '2026-07-05T12:00:00Z', 'en_iso': '2026-07-05T21:55:00Z',
    'first': 'FRA', 'is_flight': True,
}


def test_window_plain_plan_without_observation():
    flying, en_eff, landed = _flight_window_state(_FLIGHT_DAY, None, _d(15, 0, day=5))
    assert flying is True and landed is False
    assert en_eff == _FLIGHT_DAY['en']


def test_observed_delay_extends_window():
    # Plan-Ende 21:55, +75 beobachtet → um 22:30 ist er NOCH in der Luft
    # (früher: flying_now kippte um 21:55 auf False — „Verspätung nicht bemerkt").
    legs = [{'flight': 'LH716', 'delay_min': 75, 'status': 'estimated'}]
    flying, en_eff, landed = _flight_window_state(_FLIGHT_DAY, legs, _d(22, 30, day=5))
    assert flying is True
    assert en_eff == _FLIGHT_DAY['en'] + dt.timedelta(minutes=75)


def test_observed_landing_ends_window_early():
    # Board meldet gelandet → nicht mehr „Fliegt gerade", auch wenn das
    # Dienst-Fenster laut Plan noch läuft.
    legs = [{'flight': 'LH716', 'delay_min': 0, 'status': 'Landed 21:10'}]
    flying, en_eff, landed = _flight_window_state(_FLIGHT_DAY, legs, _d(21, 20, day=5))
    assert flying is False and landed is True


def test_after_effective_end_not_flying():
    legs = [{'flight': 'LH716', 'delay_min': 75, 'status': 'estimated'}]
    flying, _, _ = _flight_window_state(_FLIGHT_DAY, legs, _d(23, 30, day=5))
    assert flying is False


def test_missing_end_falls_back_to_10h_window():
    day = dict(_FLIGHT_DAY, en=None, en_iso=None)
    flying, en_eff, _ = _flight_window_state(day, None, _d(21, 0, day=5))
    assert flying is True
    assert en_eff == day['st'] + dt.timedelta(hours=10)
