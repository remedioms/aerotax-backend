"""route-history Tag-0-Gates (Owner-Screenshot 2026-07-12, LH781 SIN→FRA:
„MORGEN · 06:40 → 06:40 · pünktlich").

Root-Cause: der Tag-0-Bucket trägt das ORIGIN-lokale Heute (SIN = schon der
13.), die Live-Store-Rows beider Seiten aber den Betriebstag IHRER Station
(FRA-Ankünfte = noch der 12.) → heutige FRA-Ankünfte landeten im MORGEN-Bucket
(und doppelt nochmal im richtigen Tag aus der DB). Dazu: Board-Rows, deren
sched-Wandzeit an ihrer Station noch in der ZUKUNFT liegt, sind nicht
geflogen — sie gehören nicht in die Historie.

Deckt ab (ax_route_history, hermetisch — alle Quellen gemockt):
  • Datums-Gate: Store-Row mit fremdem sched-Datum fliegt aus Tag 0.
  • Zukunfts-Gate: noch nicht geflogene Rows (sched > Stations-jetzt) raus.
  • Vergangene heutige Rows bleiben drin.
"""
import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

from datetime import datetime
from unittest.mock import patch

import app as A


def _fake_local_now(code):
    """SIN ist schon am 13. (00:51), FRA noch am 12. (18:51)."""
    c = (code or '').split('#', 1)[0].upper()
    if c == 'SIN':
        return datetime(2026, 7, 13, 0, 51)
    return datetime(2026, 7, 12, 18, 51)


def _row(flight, dest, sched, delay=0, known=True):
    return {'flight': flight, 'airline': flight[:2], 'dest_iata': dest,
            'sched': sched, 'delay_min': (delay if known else None),
            'delay_known': known, 'cancelled': False, 'gaveup': False}


def _call_route_history(dep_store, arr_store, obs_by_date=None, days=2):
    """ax_route_history SIN→FRA mit gemockten Quellen aufrufen."""
    obs_by_date = obs_by_date or {}

    def fake_store(key):
        k = (key or '').upper()
        return arr_store if k.endswith('#ARR') else dep_store

    def fake_obs(date_str, airport, airline, dest_iata=None):
        # Optimierungs-Contract: vergangene Route-History-Tage filtern bereits
        # im Supabase-Query auf die bekannte Gegenseite (keine Hub-Vollseiten).
        expected = 'SIN' if (airport or '').upper().endswith('#ARR') else 'FRA'
        assert dest_iata == expected
        return obs_by_date.get(((airport or '').upper(), date_str), [])

    with patch.object(A, '_airport_local_now', side_effect=_fake_local_now), \
            patch.object(A, '_store_key_for',
                         side_effect=lambda ap, kind:
                         (ap + '#ARR') if kind == 'arrival' else ap), \
            patch.object(A, '_departed_rows_from_store',
                         side_effect=fake_store), \
            patch.object(A, '_board_rows_from_obs_for_date',
                         side_effect=fake_obs), \
            patch.object(A, '_ax_codeshare_map', return_value={}), \
            patch.object(A, '_route_track_flight_set', return_value=set()), \
            patch.object(A, '_sched_block_min', return_value=None):
        with A.app.test_request_context(
                f'/api/ax/route-history/SIN/FRA?days={days}'):
            resp = A.ax_route_history('SIN', 'FRA')
    if isinstance(resp, tuple):
        resp = resp[0]
    return resp.get_json()


def test_day0_drops_foreign_date_and_future_rows():
    # Tag 0 = 2026-07-13 (SIN-lokal). FRA-Arr-Store trägt die HEUTIGE (12.)
    # Landung → falsches Datum für Tag 0. SIN-Dep-Store trägt einen Flug,
    # der erst 23:40 (SIN) startet → Zukunft um 00:51.
    dep_store = [_row('LH781', 'FRA', '2026-07-13T23:40:00', known=False)]
    arr_store = [_row('LH781', 'SIN', '2026-07-12T06:40:00', delay=0)]
    d = _call_route_history(dep_store, arr_store)
    day_dates = [x['date'] for x in d['recent_days']]
    # Tag 0 (13.) hat nach den Gates KEINE Flüge → Bucket fehlt komplett.
    assert '2026-07-13' not in day_dates


def test_day0_keeps_flown_today_rows():
    # Ein SIN-Abflug HEUTE (13., 00:10 < jetzt 00:51) bleibt in Tag 0.
    dep_store = [_row('SQ25', 'FRA', '2026-07-13T00:10:00', delay=5)]
    d = _call_route_history(dep_store, [])
    day0 = next((x for x in d['recent_days'] if x['date'] == '2026-07-13'), None)
    assert day0 is not None
    assert [f['flight'] for f in day0['flights']] == ['SQ25']


def test_yesterday_arrival_still_listed_from_db_day():
    # Die heutige (12.) FRA-Landung erscheint über den DB-Tag (i>0) im
    # RICHTIGEN Bucket — kein Datenverlust, nur kein MORGEN-Geist.
    arr_store = [_row('LH781', 'SIN', '2026-07-12T06:40:00', delay=0)]
    obs = {('FRA#ARR', '2026-07-12'):
           [_row('LH781', 'SIN', '2026-07-12T06:40:00', delay=0)]}
    d = _call_route_history([], arr_store, obs_by_date=obs)
    day12 = next((x for x in d['recent_days'] if x['date'] == '2026-07-12'), None)
    assert day12 is not None
    assert [f['flight'] for f in day12['flights']] == ['LH781']
    # Und NICHT nochmal als Morgen-Bucket (Doppelzählung).
    assert '2026-07-13' not in [x['date'] for x in d['recent_days']]
