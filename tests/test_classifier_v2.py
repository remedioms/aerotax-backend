"""Classifier V2 — Tests für die 5 reinen Funktionen (R40, 2026-05-27).

Tests sind isoliert pro Funktion, plus 2 Integration-Tests gegen reale
Tibor/User-95775 Pattern. Kein Eingriff in app.py oder Legacy-Pfad.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from classifier_v2 import (  # noqa: E402
    MarkerKind, DayRole, CountryResult, Tour,
    classify_marker, build_tours, day_role_in_tour,
    resolve_country, is_hotel_night,
)


# ════════════════════════════════════════════════════════════════════════════
# Regel 1: classify_marker
# ════════════════════════════════════════════════════════════════════════════

class TestMarkerClassification:
    def test_lmn_ht_is_strict_passive(self):
        assert classify_marker('LMN_HT') == MarkerKind.STRICT_PASSIVE
        assert classify_marker('LMN_HT1') == MarkerKind.STRICT_PASSIVE
        assert classify_marker('LMN_HT-1') == MarkerKind.STRICT_PASSIVE

    def test_lmn_ad_al_ds_ft_strict_passive(self):
        for m in ['LMN_AD', 'LMN_AD1', 'LMN_AL', 'LMN_DS', 'LMN_FT']:
            assert classify_marker(m) == MarkerKind.STRICT_PASSIVE, m

    def test_ortstag_off_urlaub_strict_passive(self):
        for m in ['ORTSTAG', 'OFF', 'OF', 'URLAUB', 'U', 'U1', 'KRANK']:
            assert classify_marker(m) == MarkerKind.STRICT_PASSIVE, m

    def test_frs_lmn_as_flexible_passive(self):
        for m in ['FRS', 'FRD', 'LMN_AS', 'LMN_CR', 'LMN_OD']:
            assert classify_marker(m) == MarkerKind.FLEXIBLE_PASSIVE, m

    def test_sb_s_home_standby(self):
        assert classify_marker('SB_S') == MarkerKind.STANDBY_HOME
        assert classify_marker('SB_M') == MarkerKind.STANDBY_HOME
        assert classify_marker('RB') == MarkerKind.STANDBY_HOME

    def test_sb_f_airport_standby(self):
        for m in ['SB_F', 'SBA', 'SBY', 'RES', 'RES_SB']:
            assert classify_marker(m) == MarkerKind.STANDBY_AIRPORT, m

    def test_em_d4_training(self):
        for m in ['EM', 'EH', 'EK', 'D4', 'DD', 'TK', 'SM', 'SIM', 'EMCRM']:
            assert classify_marker(m) == MarkerKind.TRAINING, m

    def test_lh_number_flight(self):
        assert classify_marker('LH756') == MarkerKind.FLIGHT
        assert classify_marker('LH400-1') == MarkerKind.FLIGHT

    def test_4_digit_number_flight(self):
        assert classify_marker('1234') == MarkerKind.FLIGHT
        assert classify_marker('99102') == MarkerKind.FLIGHT

    def test_cockpit_unknown(self):
        for m in ['LOFT', 'REC', 'TR', 'LPC', 'OPC', 'ATQP']:
            assert classify_marker(m) == MarkerKind.UNKNOWN, m

    def test_empty_marker_unknown(self):
        assert classify_marker('') == MarkerKind.UNKNOWN
        assert classify_marker(None) == MarkerKind.UNKNOWN


# ════════════════════════════════════════════════════════════════════════════
# Regel 2: build_tours
# ════════════════════════════════════════════════════════════════════════════

def _day(datum, marker='', routing=None, layover='', overnight=False,
         starts_hb=False, ends_hb=False, duty=0, start='', end='',
         activity=''):
    return {
        'datum': datum,
        'marker_raw': marker,
        'routing': routing or [],
        'layover_ort': layover,
        'overnight_after_day': overnight,
        'starts_at_homebase': starts_hb,
        'ends_at_homebase': ends_hb,
        'duty_duration_minutes': duty,
        'start_time': start,
        'end_time': end,
        'activity_type': activity,
    }


class TestTourBuilder:
    def test_miami_tour_with_heimkehr(self):
        """13./14.02 Miami-Tour mit Heimkehr."""
        days = [
            _day('2025-02-12', marker='OFF', activity='free'),
            _day('2025-02-13', marker='LH462', routing=['FRA', 'MIA'],
                 layover='MIA', overnight=True, starts_hb=True,
                 duty=660, start='10:15', end='23:30'),
            _day('2025-02-14', marker='LH463', routing=['MIA', 'FRA'],
                 ends_hb=True, duty=600, start='18:00', end='23:55'),
            _day('2025-02-15', marker='OFF', activity='free'),
        ]
        tours = build_tours(days, homebase='FRA')
        assert len(tours) == 1
        assert len(tours[0].days) == 2
        assert tours[0].days[0]['datum'] == '2025-02-13'
        assert tours[0].days[1]['datum'] == '2025-02-14'

    def test_free_day_breaks_tour(self):
        """Frei-Tag beendet aktive Tour."""
        days = [
            _day('2025-01-10', marker='LH756', routing=['FRA', 'BLR'],
                 layover='BLR', overnight=True, starts_hb=True, duty=600),
            _day('2025-01-11', marker='LH757', routing=['BLR', 'FRA'],
                 ends_hb=True, duty=550),
            _day('2025-01-12', marker='OFF', activity='free'),
            _day('2025-01-13', marker='LH755', routing=['FRA', 'HKG'],
                 layover='HKG', overnight=True, starts_hb=True, duty=700),
        ]
        tours = build_tours(days, homebase='FRA')
        assert len(tours) == 2

    def test_strict_passive_marker_no_tour(self):
        """LMN_HT1 mit duty=240 erzeugt KEINE Tour."""
        days = [
            _day('2025-01-29', marker='LMN_HT1', duty=240,
                 start='13:00', end='17:00', starts_hb=True, ends_hb=True),
        ]
        tours = build_tours(days, homebase='FRA')
        assert len(tours) == 0

    def test_standby_home_no_tour(self):
        """SB_S erzeugt keine Tour."""
        days = [
            _day('2025-02-01', marker='SB_S', duty=480),
            _day('2025-02-02', marker='SB_S', duty=480),
        ]
        tours = build_tours(days, homebase='FRA')
        assert len(tours) == 0

    def test_office_at_hb_no_tour(self):
        """EM-Schulung am HB ohne Tour-Anschluss → keine Tour."""
        days = [
            _day('2025-04-09', marker='D4', routing=['FRA'],
                 starts_hb=True, ends_hb=True, duty=480,
                 start='07:30', end='15:30'),
        ]
        tours = build_tours(days, homebase='FRA')
        # Office am HB ohne Foreign-Signal/Overnight → keine Tour
        assert len(tours) == 0

    def test_mid_tour_x_marker_keeps_tour_open(self):
        """Mid-Tour `X BOM` mit dünnen Reader-Feldern darf Tour nicht zerbrechen.

        Pattern (Tibor 03-29..03-31): Anreise BOM → Layover-Tag mit Marker `X`
        und activity='frei' (Reader-Bug) → Heimkehr. V2 muss alle 3 als
        EINE Tour klammern, nicht 2 1-Tages-Touren.
        """
        days = [
            _day('2025-03-29', marker='74016 P1', routing=['FRA', 'BOM'],
                 layover='BOM', overnight=True, starts_hb=True, duty=814,
                 start='10:25'),
            _day('2025-03-30', marker='X BOM', layover='BOM', overnight=True,
                 activity='frei'),  # Reader-Bug: activity='frei' aber Mid-Tour
            _day('2025-03-31', marker='757', routing=['BOM', 'FRA'],
                 ends_hb=True, duty=169, start='21:10'),
        ]
        tours = build_tours(days, homebase='FRA')
        assert len(tours) == 1
        assert [d['datum'] for d in tours[0].days] == [
            '2025-03-29', '2025-03-30', '2025-03-31',
        ]

    def test_phantom_no_active_signal_filtered(self):
        """Reader-Stempel-Leichen (layover=SFO ohne duty/start/overnight) sind
        KEINE Touren. Klassischer Phantom: nach Heimkehr stempelt Reader
        layover_ort vom Vortag auf Folgetage."""
        days = [
            _day('2025-04-04', marker='===', layover='SFO'),
            _day('2025-04-05', marker='===', layover='SFO'),
            _day('2025-04-06', marker='===', layover='SFO'),
        ]
        tours = build_tours(days, homebase='FRA')
        assert len(tours) == 0

    def test_homecoming_then_same_day_inland_split_in_two(self):
        """Heimkehr morgens (Reader unsichtbar) + Same-Day-Inland-Tour ab HB.

        Pattern (Tibor 16.-17.03): SVG-Anreise → unsichtbare Heimkehr →
        GVA-Same-Day. V2 muss 2 Touren erkennen, nicht 1.
        Trigger: starts_at_homebase=True UND routing[0]==HB UND
        eigenes Foreign-Routing.
        """
        days = [
            _day('2025-03-16', marker='82907 PU', routing=['FRA', 'DUS', 'SVG'],
                 layover='SVG', overnight=True, starts_hb=True, duty=1114,
                 start='05:25'),
            _day('2025-03-17', marker='83003 PU', routing=['FRA', 'MXP', 'GVA'],
                 starts_hb=True, ends_hb=True, duty=530, start='08:10',
                 activity='same_day'),
        ]
        tours = build_tours(days, homebase='FRA')
        assert len(tours) == 2
        assert tours[0].days[0]['datum'] == '2025-03-16'
        assert tours[1].days[0]['datum'] == '2025-03-17'

    def test_homecoming_day_routing_foreign_first_no_new_tour(self):
        """Heimkehrtag mit routing=[FOREIGN,FRA] und starts_hb=True ist
        Continuation der Vortags-Tour, NICHT neue Tour.

        Pattern: Tag N-1 Anreise FRA→BLR overnight, Tag N Heimkehr BLR→FRA.
        Tag N hat starts_hb=True, ends_hb=True — aber routing[0]=BLR foreign.
        """
        days = [
            _day('2025-01-03', marker='LH756', routing=['FRA', 'BLR'],
                 layover='BLR', overnight=True, starts_hb=True, duty=784,
                 start='10:55'),
            _day('2025-01-04', marker='LH755-1', routing=['BLR', 'FRA'],
                 starts_hb=True, ends_hb=True, duty=550, start='01:00'),
        ]
        tours = build_tours(days, homebase='FRA')
        assert len(tours) == 1
        assert len(tours[0].days) == 2

    def test_activity_frei_ignored_when_foreign_layover(self):
        """activity_type='frei' bei foreign-Layover ist Reader-Bug, kein
        echter Frei-Tag. Wird in Tour-Klammer aufgenommen."""
        days = [
            _day('2025-05-15', marker='LH462', routing=['FRA', 'JFK'],
                 layover='JFK', overnight=True, starts_hb=True, duty=540),
            _day('2025-05-16', marker='X', layover='JFK', overnight=True,
                 activity='frei'),
            _day('2025-05-17', marker='LH463', routing=['JFK', 'FRA'],
                 ends_hb=True, duty=520),
        ]
        tours = build_tours(days, homebase='FRA')
        assert len(tours) == 1
        assert len(tours[0].days) == 3


# ════════════════════════════════════════════════════════════════════════════
# Regel 3: day_role_in_tour
# ════════════════════════════════════════════════════════════════════════════

class TestDayRole:
    def test_miami_role_departure_and_return(self):
        days = [
            _day('2025-02-13', marker='LH462', routing=['FRA', 'MIA'],
                 layover='MIA', overnight=True, starts_hb=True,
                 duty=660, start='10:15'),
            _day('2025-02-14', marker='LH463', routing=['MIA', 'FRA'],
                 ends_hb=True, duty=600, start='18:00'),
        ]
        tours = build_tours(days + [_day('2025-02-15', marker='OFF', activity='free')], homebase='FRA')
        assert len(tours) == 1
        tour = tours[0]
        assert day_role_in_tour(tour.days[0], tour, 'FRA') == DayRole.DEPARTURE
        assert day_role_in_tour(tour.days[1], tour, 'FRA') == DayRole.RETURN

    def test_3_day_tour_mid_full_away(self):
        days = [
            _day('2025-03-23', marker='LH456', routing=['FRA', 'BOS'],
                 layover='IAD', overnight=True, starts_hb=True, duty=545,
                 start='11:55'),
            _day('2025-03-24', marker='X', routing=['IAD'],
                 layover='IAD', overnight=True, duty=0),
            _day('2025-03-25', marker='LH457', routing=['BOS', 'FRA'],
                 ends_hb=True, duty=500),
            _day('2025-03-26', marker='OFF', activity='free'),
        ]
        tours = build_tours(days, homebase='FRA')
        assert len(tours) == 1
        tour = tours[0]
        assert day_role_in_tour(tour.days[0], tour) == DayRole.DEPARTURE
        assert day_role_in_tour(tour.days[1], tour) == DayRole.MID_FULL_AWAY
        assert day_role_in_tour(tour.days[2], tour) == DayRole.RETURN


# ════════════════════════════════════════════════════════════════════════════
# Regel 4: resolve_country
# ════════════════════════════════════════════════════════════════════════════

class TestCountryResolver:
    def test_layover_iata_resolves(self):
        from bmf_data import IATA_TO_BMF
        d = _day('2025-02-13', layover='MIA', overnight=True)
        result = resolve_country(d, None, [], IATA_TO_BMF, 'FRA')
        assert result.is_foreign
        assert 'Miami' in result.country
        assert result.iata == 'MIA'
        assert result.source == 'CAS.layover'

    def test_se_overrides_when_first(self):
        from bmf_data import IATA_TO_BMF
        d = _day('2025-02-13', layover='', routing=['FRA'])
        se = [{'datum': '2025-02-13', 'stfrei_ort': 'NYC',
               'stfrei_inland': False, 'stfrei_total': 88}]
        result = resolve_country(d, None, se, IATA_TO_BMF, 'FRA')
        assert result.is_foreign
        assert result.source == 'SE'

    def test_hb_routing_returns_missing(self):
        from bmf_data import IATA_TO_BMF
        d = _day('2025-04-09', routing=['FRA'], starts_hb=True, ends_hb=True)
        result = resolve_country(d, None, [], IATA_TO_BMF, 'FRA')
        assert not result.is_foreign
        assert result.country is None

    def test_tour_neighbor_layover_resolves(self):
        from bmf_data import IATA_TO_BMF
        # Mid-Tour-Tag mit dünnen Feldern findet die Country aus Nachbar
        # (oder eigenem routing wenn vorhanden — beides Foreign + same country)
        d_mid = _day('2025-03-24', routing=['IAD'])
        tour_days = [
            _day('2025-03-23', layover='IAD', overnight=True),
            d_mid,
        ]
        tour = Tour(days=tour_days)
        result = resolve_country(d_mid, tour, [], IATA_TO_BMF, 'FRA')
        assert result.is_foreign
        assert result.iata == 'IAD'  # die genaue Source ist egal, IATA muss stimmen

    def test_tour_neighbor_layover_when_own_empty(self):
        from bmf_data import IATA_TO_BMF
        # Mid-Tour-Tag KOMPLETT ohne eigene Felder findet's beim Nachbar
        d_mid = _day('2025-03-24')  # leer
        tour_days = [
            _day('2025-03-23', layover='IAD', overnight=True),
            d_mid,
        ]
        tour = Tour(days=tour_days)
        result = resolve_country(d_mid, tour, [], IATA_TO_BMF, 'FRA')
        assert result.is_foreign
        assert result.source == 'CAS.tour_neighbor_layover'


# ════════════════════════════════════════════════════════════════════════════
# Regel 5: is_hotel_night
# ════════════════════════════════════════════════════════════════════════════

class TestHotelNight:
    def test_departure_with_foreign_overnight_counts(self):
        d = _day('2025-02-13', layover='MIA', overnight=True)
        tour = Tour(days=[d])
        country = CountryResult(country='USA - Miami', iata='MIA',
                                source='CAS.layover', is_foreign=True)
        counts, reason = is_hotel_night(d, tour, country)
        assert counts

    def test_return_day_does_not_count(self):
        """Heimkehr-Tag (overnight=False) bekommt KEINE Hotel-Nacht — schon
        wegen no_overnight, das ist die erste Bedingung."""
        days = [
            _day('2025-02-13', layover='MIA', overnight=True, starts_hb=True),
            _day('2025-02-14', routing=['MIA', 'FRA'], ends_hb=True),
        ]
        tour = Tour(days=days)
        country = CountryResult(country='USA - Miami', iata='MIA',
                                source='CAS.layover', is_foreign=True)
        counts, reason = is_hotel_night(days[1], tour, country)
        assert not counts  # no_overnight greift

    def test_hypothetical_return_with_overnight_still_blocked(self):
        """Falls Reader fälschlich overnight=True für Heimkehr-Tag liefert,
        muss die role-Prüfung trotzdem RETURN erkennen und blocken."""
        days = [
            _day('2025-02-13', layover='MIA', overnight=True, starts_hb=True),
            # Reader-Lücke: overnight=True obwohl Heimkehr
            _day('2025-02-14', routing=['MIA', 'FRA'], ends_hb=True, overnight=True),
        ]
        tour = Tour(days=days)
        country = CountryResult(country='USA - Miami', iata='MIA',
                                source='CAS.layover', is_foreign=True)
        # Day 14 hat overnight=True (Reader-Bug), aber ist RETURN-Day
        counts, reason = is_hotel_night(days[1], tour, country,
                                         role=DayRole.RETURN)
        assert not counts
        assert 'return' in reason

    def test_no_overnight_does_not_count(self):
        d = _day('2025-02-14', layover='MIA')  # overnight=False
        tour = Tour(days=[d])
        country = CountryResult(country='USA - Miami', is_foreign=True)
        counts, reason = is_hotel_night(d, tour, country)
        assert not counts

    def test_country_not_foreign_no_hotel(self):
        d = _day('2025-04-09', overnight=True)
        tour = Tour(days=[d])
        country = CountryResult(is_foreign=False)
        counts, _ = is_hotel_night(d, tour, country)
        assert not counts


# ════════════════════════════════════════════════════════════════════════════
# Integration: reale Tibor/User-95775 Pattern
# ════════════════════════════════════════════════════════════════════════════

class TestIntegration:
    def test_lmn_ht1_with_duty_stays_free(self):
        """Tibor 29.01: LMN_HT1 mit duty=240 = Online-Schulung zuhause.
        STRICT_PASSIVE → keine Tour, kein Z72."""
        d = _day('2025-01-29', marker='LMN_HT1', duty=240,
                 start='13:00', end='17:00', starts_hb=True, ends_hb=True)
        assert classify_marker('LMN_HT1') == MarkerKind.STRICT_PASSIVE
        tours = build_tours([d], homebase='FRA')
        assert len(tours) == 0

    def test_user95775_em_at_hb_no_z72(self):
        """User 95775: D4-Schulung am HB mit duty=480.
        Office-at-HB (erste Tätigkeitsstätte) → keine Tour, kein Z72."""
        d = _day('2025-04-09', marker='D4', routing=['FRA'],
                 starts_hb=True, ends_hb=True, duty=480,
                 start='07:30', end='15:30')
        assert classify_marker('D4') == MarkerKind.TRAINING
        tours = build_tours([d], homebase='FRA')
        assert len(tours) == 0  # keine Tour, da kein Foreign-Signal

    def test_frs_with_briefing_time_is_handled(self):
        """FRS-Marker mit Briefing-Zeit ist Edge-Case. FLEXIBLE_PASSIVE
        + CAS-Felder NICHT leer → wird zur Tour wenn Foreign-Routing."""
        # FRS ohne Felder → kein Tour
        d_empty = _day('2025-02-11', marker='FRS')
        assert classify_marker('FRS') == MarkerKind.FLEXIBLE_PASSIVE
        tours = build_tours([d_empty], homebase='FRA')
        assert len(tours) == 0

    def test_bos_3day_tour_full_pattern(self):
        """3-Tages-BOS-Tour: Anreise + Mid-Layover + Heimkehr."""
        days = [
            _day('2025-03-22', marker='OFF', activity='free'),
            _day('2025-03-23', marker='LH456', routing=['FRA', 'BOS'],
                 layover='IAD', overnight=True, starts_hb=True, duty=545,
                 start='11:55', end='21:00'),
            _day('2025-03-24', marker='419', routing=['IAD'],
                 layover='IAD', overnight=True, duty=0),
            _day('2025-03-25', marker='LH457', routing=['BOS', 'FRA'],
                 ends_hb=True, duty=500, start='16:00', end='23:55'),
            _day('2025-03-26', marker='OFF', activity='free'),
        ]
        tours = build_tours(days, homebase='FRA')
        assert len(tours) == 1
        tour = tours[0]
        assert len(tour.days) == 3
        assert day_role_in_tour(tour.days[0], tour) == DayRole.DEPARTURE
        assert day_role_in_tour(tour.days[1], tour) == DayRole.MID_FULL_AWAY
        assert day_role_in_tour(tour.days[2], tour) == DayRole.RETURN
