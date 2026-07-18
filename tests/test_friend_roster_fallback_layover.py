"""get_friend_roster FALLBACK-Pfad: Nightstop wie im PRIMÄR-Pfad ableiten.

WURZEL (Audit 2026-07-18): Hat ein Freund KEINEN gepushten Roster-Snapshot
(reiner Kalender-Import), baut der Endpoint seinen Plan aus `_ical_briefings_load`.
Dieser FALLBACK servierte `layover_ort` roh aus `ical_layover_ort` — ohne den
Same-Day-Turnaround-Guard, den der PRIMÄR-Pfad über `_feed_nightstop_ort` hat.
Folge: ein Turnaround ZURÜCK zur Homebase (ZRH-LIS-ZRH) zeigte einen falschen
„Layover" für reine Kalender-Freunde.

Hier festgenagelt: der Fallback ruft jetzt `_feed_nightstop_ort(day, homebase=hb,
next_day=…)` — Same-Day-Return-Turnaround ⇒ kein Layover; echter Nightstop ⇒
Ziel-IATA; leg-lose LAYOVER-Zeile ⇒ roher `ical_layover_ort` bleibt erhalten.
"""
import os
import sys

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import datetime as _dt
import app as A


def _iso(d, hhmm):
    return f"{d}T{hhmm}:00Z"


def _drive(monkeypatch, briefs):
    """Ruft /api/user/friend-roster über den Fallback-Pfad (kein Snapshot) auf."""
    me_tok, friend_tok = 'me_tok', 'friend_tok'
    monkeypatch.setattr(A, '_friends_load', lambda t: {'friends': [friend_tok]})
    monkeypatch.setattr(A, '_maybe_refresh_calendar_feed', lambda *a, **k: None)
    # Kein in-memory Snapshot, kein persistenter Snapshot → Fallback greift.
    A._store.pop(friend_tok, None)
    monkeypatch.setattr(A, '_roster_snapshot_read', lambda t: {})
    monkeypatch.setattr(A, '_ical_briefings_load', lambda t: briefs)
    monkeypatch.setattr(A, '_profile_homebase_cached', lambda t: 'ZRH')
    # Live-Enricher neutralisieren (kein Netz im Test).
    monkeypatch.setattr(A, '_enrich_leg_delays', lambda *a, **k: None)
    client = A.app.test_client()
    r = client.get(f'/api/user/friend-roster/{me_tok}/{friend_tok}')
    assert r.status_code == 200, r.status_code
    data = r.get_json()
    assert data.get('source') == 'ical_briefings', data.get('source')
    return {d['datum']: d for d in (data.get('days') or [])}


def _today_plus(days):
    return (_dt.date.today() + _dt.timedelta(days=days)).isoformat()


def test_same_day_turnaround_to_homebase_is_no_layover(monkeypatch):
    """ZRH-LIS-ZRH, letzte Ankunft ZRH (=Homebase), am selben Tag → KEIN Layover.
    Der rohe ical_layover_ort (fälschlich 'LIS') darf NICHT als Nightstop erscheinen."""
    d = _today_plus(2)
    briefs = {
        d: {
            'ical_summary': 'LX2345 ZRH-LIS · LX2346 LIS-ZRH',
            'ical_layover_ort': 'LIS',   # falscher Roh-Wert
            'ical_sectors': [
                {'flight': 'LX2345', 'from': 'ZRH', 'to': 'LIS',
                 'dep_iso': _iso(d, '07:00'), 'arr_iso': _iso(d, '09:30')},
                {'flight': 'LX2346', 'from': 'LIS', 'to': 'ZRH',
                 'dep_iso': _iso(d, '10:30'), 'arr_iso': _iso(d, '13:00')},
            ],
        }
    }
    days = _drive(monkeypatch, briefs)
    assert d in days
    assert days[d]['layover_ort'] is None, days[d]['layover_ort']


def test_real_overnight_keeps_destination(monkeypatch):
    """ZRH-JFK, letzte Ankunft JFK (≠ Homebase), am selben Tag → Nightstop JFK."""
    d = _today_plus(3)
    briefs = {
        d: {
            'ical_summary': 'LX0016 ZRH-JFK',
            'ical_layover_ort': 'JFK',
            'ical_sectors': [
                {'flight': 'LX0016', 'from': 'ZRH', 'to': 'JFK',
                 'dep_iso': _iso(d, '13:00'), 'arr_iso': _iso(d, '20:00')},
            ],
        }
    }
    days = _drive(monkeypatch, briefs)
    assert d in days
    assert days[d]['layover_ort'] == 'JFK', days[d]['layover_ort']


def test_legless_layover_row_keeps_raw_layover_ort(monkeypatch):
    """Reine LAYOVER-Zeile ohne Sektoren → _feed_nightstop_ort fällt auf den
    rohen ical_layover_ort zurück (byte-kompatibel), Ort bleibt erhalten."""
    d = _today_plus(4)
    briefs = {
        d: {
            'ical_summary': 'LAYOVER (Tag 2/3)',
            'ical_location': 'BLR',
            'ical_layover_ort': 'BLR',
            'ical_sectors': [],
        }
    }
    days = _drive(monkeypatch, briefs)
    assert d in days
    assert days[d]['layover_ort'] == 'BLR', days[d]['layover_ort']
