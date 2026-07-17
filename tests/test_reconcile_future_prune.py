"""`_reconcile_month_briefings` Zukunfts-Geist-Prune (Jennifer Orhan 2026-07-18).

Ein stornierter/geänderter Zukunfts-Trip, der HINTER den geschrumpften Feed-
Horizont fällt (Feed endet 29.07, Geister-Tage 31.07/01.08), überlebte den
Reconcile, weil das Fenster nur [fmin..fmax] räumte. Jetzt: Zukunft hinter fmax
wird geräumt — ABER NUR Zukunft (>= heute), NIE Vergangenheit, und nur wenn der
Feed gesund bis mindestens heute reicht.
"""
import os
import sys
from datetime import datetime, timedelta

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app as A


def _d(offset):
    return (datetime.now() + timedelta(days=offset)).strftime('%Y-%m-%d')


def _ev(datum, summary):
    return {'summary': summary, 'location': '', 'start': datum,
            'start_iso': datum + 'T10:00:00', '_multiday_dates': [datum]}


def _briefing(summary):
    return {'ical_summary': summary, 'ical_location': 'X',
            'ical_start_iso': _d(0) + 'T10:00:00'}


def test_future_ghost_beyond_horizon_is_pruned_but_past_kept():
    # Gesunder Feed: deckt heute .. heute+5 (fmax = heute+5 >= heute).
    feed = [_ev(_d(0), 'Heute-Dienst'), _ev(_d(3), 'LH100 FRA-JFK'), _ev(_d(5), 'JFK-FRA')]
    briefings = {
        _d(-40): _briefing('Alt-Vergangenheit'),   # Historie → MUSS bleiben
        _d(0):   _briefing('Heute-Dienst'),         # im Feed → bleibt
        _d(3):   _briefing('LH100 FRA-JFK'),        # im Feed → bleibt
        _d(20):  _briefing('LH455 SFO-FRA'),        # Zukunfts-Geist hinter fmax → RAUS
    }
    A._reconcile_month_briefings('TESTTOKEN_NOSB', briefings, feed)
    assert _d(20) not in briefings, 'Zukunfts-Geist hinter Horizont muss geräumt werden'
    assert _d(-40) in briefings, 'Vergangenheit/Historie darf NIE geräumt werden'
    assert _d(0) in briefings and _d(3) in briefings, 'Feed-Tage bleiben'


def test_no_future_prune_when_feed_is_stale_only_past():
    # Ungesunder/abgelaufener Feed: fmax < heute → KEIN Zukunfts-Prune (sonst würde
    # ein abgeschnittener Feed echte Zukunft löschen).
    feed = [_ev(_d(-5), 'Alt'), _ev(_d(-3), 'Alt2')]
    briefings = {
        _d(10): _briefing('Echte Zukunft'),         # darf NICHT geräumt werden
    }
    A._reconcile_month_briefings('TESTTOKEN_NOSB', briefings, feed)
    assert _d(10) in briefings, 'bei abgelaufenem Feed keine Zukunft löschen'
