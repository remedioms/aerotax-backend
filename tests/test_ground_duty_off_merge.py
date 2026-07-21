"""Tanja-Fix (2026-07-21): „Off Day · Mandatory Training" darf nicht OFF sein.

WURZEL: myTime legt an EM-/Trainings-Tagen ZWEI VEVENTs an („Off Day" +
„Mandatory Training", 06:00–14:30). Der Import merged sie mit „·" in EINEN
`ical_summary`. Der get_friend_roster-FALLBACK (reiner Kalender-Freund, kein
Snapshot) stempelte via `'OFF DAY' in up` den ganzen DIENST-Tag als
klass='OFF' — iOS zeigte „frei", das Training war unsichtbar. Live-Beleg:
user_ical_briefings AT-912350A9D42C493A, datum 2026-07-27.

Fix: `_summary_has_ground_duty` — das Off-Segment stempelt nur, wenn KEIN
anderes Segment ein eigener Boden-Dienst ist (Training/Sim/Check/EM/Medical/
Pickup/Standby/Office bzw. getimtes Segment). iOS-Spiegel:
RosterEventClassifier.hasGroundDutyEvidence.
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
    """Fallback-Pfad (kein Snapshot) — identisch zum Layover-Fallback-Test."""
    me_tok, friend_tok = 'me_tok', 'friend_tok'
    # 60s-Endpoint-Memo leeren — sonst serviert der zweite Test die gecachte
    # Antwort des ersten (gleiche Token-Kombination).
    A._FRIEND_ROSTER_MEMO.clear()
    monkeypatch.setattr(A, '_friends_load', lambda t: {'friends': [friend_tok]})
    monkeypatch.setattr(A, '_maybe_refresh_calendar_feed', lambda *a, **k: None)
    A._store.pop(friend_tok, None)
    monkeypatch.setattr(A, '_roster_snapshot_read', lambda t: {})
    monkeypatch.setattr(A, '_ical_briefings_load', lambda t: briefs)
    monkeypatch.setattr(A, '_profile_homebase_cached', lambda t: 'FRA')
    monkeypatch.setattr(A, '_enrich_leg_delays', lambda *a, **k: None)
    client = A.app.test_client()
    r = client.get(f'/api/user/friend-roster/{me_tok}/{friend_tok}')
    assert r.status_code == 200, r.status_code
    data = r.get_json()
    assert data.get('source') == 'ical_briefings', data.get('source')
    return {d['datum']: d for d in (data.get('days') or [])}


def _today_plus(days):
    return (_dt.date.today() + _dt.timedelta(days=days)).isoformat()


# ── _summary_has_ground_duty (Segment-Logik) ────────────────────────────────

def test_off_plus_training_is_ground_duty():
    assert A._summary_has_ground_duty('OFF DAY · MANDATORY TRAINING')


def test_pure_off_variants_are_no_ground_duty():
    for s in ('OFF DAY', 'OFF DAY (OF)', 'OFF DAY (ORTSTAG)',
              'OFF DAY (OF) · OFF DAY (ORTSTAG)', 'DAY OFF', 'FREE DAY',
              'REST DAY', 'RECOVERY (REC)', 'ABSENCE (U)', 'OFF', 'X', ''):
        assert not A._summary_has_ground_duty(s), s


def test_timed_recurrent_and_em_code_are_ground_duty():
    assert A._summary_has_ground_duty('OFF DAY · REC A320 0900 1700')
    assert A._summary_has_ground_duty('OFF DAY · EM')


def test_layover_segment_is_no_ground_duty():
    assert not A._summary_has_ground_duty('OFF DAY · LAYOVER [AMS] (TAG 2/2)')


# ── get_friend_roster Fallback: klass-Stempel ───────────────────────────────

def test_off_plus_training_day_is_not_klass_off(monkeypatch):
    """Der Tanja-Tag: klass darf NICHT 'OFF' sein; marker + Zeiten bleiben
    erhalten, damit der iOS-Classifier den Dienst 1:1 zeigt."""
    d = _today_plus(6)
    briefs = {
        d: {
            'ical_summary': 'Off Day · Mandatory Training',
            'ical_location': 'FRA',
            'ical_start_iso': _iso(d, '06:00'),
            'ical_end_iso': _iso(d, '14:30'),
        }
    }
    days = _drive(monkeypatch, briefs)
    assert d in days
    assert days[d]['klass'] is None, days[d]['klass']
    assert days[d]['marker'] == 'Off Day · Mandatory Training'
    assert days[d]['start_time'] == '06:00', days[d]['start_time']


def test_pure_off_day_stays_klass_off(monkeypatch):
    d = _today_plus(5)
    briefs = {d: {'ical_summary': 'Off Day', 'ical_location': 'FRA'}}
    days = _drive(monkeypatch, briefs)
    assert d in days
    assert days[d]['klass'] == 'OFF', days[d]['klass']
