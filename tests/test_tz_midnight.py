"""Tests für tz_midnight — deterministische 24:00-Ortszeit-Logik."""
import os
import sys
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE.parent))
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import tz_midnight as tz  # noqa: E402


def test_zoneinfo_and_airport_db_available():
    assert tz._HAVE_ZONEINFO is True
    assert tz._airport_tz is not None


def test_local_arrival_date_longhaul_east_crosses_into_next_day():
    # FRA→BLR: dep 13:05 UTC am 16.12, arr 01:30 UTC (Folge-UTC-Tag), BLR UTC+5:30
    d = tz.local_arrival_date('2024-12-16', '13:05', 'FRA', '01:30', 'BLR')
    assert d == '2024-12-17', d


def test_local_arrival_date_longhaul_japan():
    # FRA→HND: dep 12:05 UTC am 19.01, arr 03:35 UTC, Tokyo UTC+9
    d = tz.local_arrival_date('2025-01-19', '12:05', 'FRA', '03:35', 'HND')
    assert d == '2025-01-20', d


def test_local_arrival_date_same_day_shorthaul():
    # FRA→MUC: dep 06:00 UTC, arr 07:00 UTC, beide Europe/Berlin
    d = tz.local_arrival_date('2025-05-05', '06:00', 'FRA', '07:00', 'MUC')
    assert d == '2025-05-05', d


def test_night_return_flight_true_when_lands_after_local_midnight():
    # BLR→FRA Nachtflug: dep 17:58 UTC (=23:28 local BLR) am 05.01,
    # arr 23:55 UTC (=00:55 local FRA am 06.01) → Heimkehr nach lokaler Mitternacht
    nr = tz.is_night_return_flight('2025-01-05', '17:58', 'BLR', '23:55', 'FRA')
    assert nr is True, nr


def test_no_night_return_for_daytime_shorthaul():
    nr = tz.is_night_return_flight('2025-05-05', '04:00', 'FRA', '06:00', 'MUC')
    assert nr is False, nr


def test_overnight_country_resolves_via_airport_db():
    oc = tz.overnight_country_for_day({'layover_iata': 'BLR'}, 'FRA')
    assert oc is not None
    assert oc['iso'] == 'IN'
    assert oc['is_foreign'] is True


def test_overnight_country_none_for_homebase():
    oc = tz.overnight_country_for_day({'layover_iata': 'FRA'}, 'FRA')
    assert oc is None


def test_graceful_none_on_unparseable_time():
    assert tz.local_arrival_date('2025-01-05', None, 'BLR', '03:00', 'FRA') is None
    assert tz.is_night_return_flight('2025-01-05', 'xx', 'BLR', '03:00', 'FRA') is None
