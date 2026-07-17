"""Paid-FR24-Eskalation für die In-Flight-Karte (/api/ax/flight-live).

Root-Cause (SWISS LX8 ZRH→ORD, 2026-07-17): US-Airport ohne Ankunfts-Board + 3 h
alte gRPC-Position → die App simulierte einen falschen, laufenden ETA („+21 min,
fliegt, Ankunft 16:16"), obwohl der Flug längst gelandet war. Die Eskalation macht
die Anzeige ehrlich (kein Live/ETA mehr) und füllt — NUR im unzuverlässigen Fall —
über EINEN bezahlten FR24-Call die echte Ist-Landung nach.

Getestet wird der gemeinsame Helper `_apply_paid_arrival_escalation` (den beide
Live-Consumer benutzen würden) plus die Prädikate `_est_arr_passed`/`_pos_is_stale`
— ohne vollen Flask-Request, mit gemockten FR24-Wachen (kein echtes Spending).
"""
import os
from datetime import datetime, timezone, timedelta

import blueprints.aerox_data_blueprint as bp


def _iso_min_ago(minutes):
    return (datetime.now(timezone.utc)
            - timedelta(minutes=minutes)).strftime('%Y-%m-%dT%H:%M:%SZ')


def _iso_arr(minutes_ago):
    """Absolut-UTC-ISO Ankunftszeit `minutes_ago` Minuten in der Vergangenheit."""
    return (datetime.now(timezone.utc)
            - timedelta(minutes=minutes_ago)).strftime('%Y-%m-%dT%H:%M:%SZ')


def _iso_arr_future(minutes_ahead):
    return (datetime.now(timezone.utc)
            + timedelta(minutes=minutes_ahead)).strftime('%Y-%m-%dT%H:%M:%SZ')


# ── Prädikat: _pos_is_stale ──────────────────────────────────────────────────

def test_pos_is_stale_none_is_stale():
    assert bp._pos_is_stale(None, 30) is True


def test_pos_is_stale_no_timestamp_is_stale():
    assert bp._pos_is_stale({'lat': 1.0, 'lon': 2.0}, 30) is True


def test_pos_is_stale_fresh_is_not_stale():
    assert bp._pos_is_stale({'seen_ts': _iso_min_ago(5)}, 30) is False


def test_pos_is_stale_old_iso_is_stale():
    assert bp._pos_is_stale({'seen_ts': _iso_min_ago(180)}, 30) is True


def test_pos_is_stale_epoch_ts_fresh():
    import time as _t
    assert bp._pos_is_stale({'ts': _t.time() - 60}, 30) is False


def test_pos_is_stale_epoch_ts_old():
    import time as _t
    assert bp._pos_is_stale({'ts': _t.time() - 3 * 3600}, 30) is True


def test_pos_is_stale_obs_ts_priority():
    # obs_ts (frisch) hat Vorrang vor seen_ts (alt)
    assert bp._pos_is_stale(
        {'obs_ts': _iso_min_ago(2), 'seen_ts': _iso_min_ago(500)}, 30) is False


# ── Prädikat: _est_arr_passed ────────────────────────────────────────────────

def test_est_arr_passed_iso_in_past():
    p = {'est_arr': _iso_arr(120), 'date': '2026-07-17', 'dest': {'iata': 'ORD'}}
    assert bp._est_arr_passed(p, 10) is True


def test_est_arr_passed_iso_in_future_false():
    p = {'est_arr': _iso_arr_future(60), 'date': '2026-07-17',
         'dest': {'iata': 'ORD'}}
    assert bp._est_arr_passed(p, 10) is False


def test_est_arr_passed_within_grace_false():
    # gerade 3 min vorbei, grace=10 → noch NICHT „komfortabel vorbei"
    p = {'est_arr': _iso_arr(3), 'date': '2026-07-17', 'dest': {'iata': 'ORD'}}
    assert bp._est_arr_passed(p, 10) is False


def test_est_arr_passed_falls_back_to_sched():
    p = {'sched_arr': _iso_arr(120), 'date': '2026-07-17',
         'dest': {'iata': 'ORD'}}
    assert bp._est_arr_passed(p, 10) is True


def test_est_arr_passed_missing_returns_false():
    # kein est/sched → unbekannt → NIE eskalieren
    assert bp._est_arr_passed({'date': '2026-07-17', 'dest': {'iata': 'ORD'}},
                              10) is False


def test_est_arr_passed_unparseable_returns_false():
    p = {'est_arr': 'garbage', 'date': '2026-07-17', 'dest': {'iata': 'ORD'}}
    assert bp._est_arr_passed(p, 10) is False


def test_est_arr_passed_bare_hhmm_needs_date_and_dest():
    # bare HH:MM ohne Datum/Ziel → nicht auflösbar → False (defensiv)
    assert bp._est_arr_passed({'est_arr': '14:16'}, 10) is False


# ── Eskalations-Harness ──────────────────────────────────────────────────────

def _install_mocks(monkeypatch, *, arr_obs_exists=False, enabled=True,
                   budget_ok=True, fill_returns=True):
    calls = {'fill': 0}

    def _fill(fn, date, dep, dest):
        calls['fill'] += 1
        return fill_returns

    monkeypatch.setattr(bp, '_fr24_arr_obs_exists',
                        lambda fn, d, arr: arr_obs_exists)
    monkeypatch.setattr(bp, '_fr24_arr_backfill_enabled', lambda: enabled)
    monkeypatch.setattr(bp, '_fr24_budget_ok', lambda: budget_ok)
    monkeypatch.setattr(bp, '_fr24_fill_missing_arrival', _fill)
    return calls


def _stale_inflight_payload():
    return {
        'ok': True, 'flight': 'LX8', 'date': '2026-07-17',
        'dest': {'iata': 'ORD'},
        'est_arr': _iso_arr(150),        # ETA 2,5 h vorbei
        'in_flight': True,
        'live': {'lat': 41.0, 'lon': -87.0},
        'progress': 0.9,
    }


def test_unreliable_case_flips_and_calls_paid_once(monkeypatch):
    """(a) stale + ETA vorbei + keine ARR-Obs → in_flight False, lost,
    arr_unreliable True, und EIN bezahlter Fill-Call."""
    monkeypatch.setenv('FLIGHTLIVE_PAID_ESCALATE', '1')
    calls = _install_mocks(monkeypatch, arr_obs_exists=False)
    pos = {'seen_ts': _iso_min_ago(180)}      # 3 h alt → stale
    payload = _stale_inflight_payload()

    def _merged(fn, date=None, dep_iata=None, arr_iata=None, free_only=True):
        return {'sched_arr': '16:00', 'esti_arr': '16:20', 'delay_known': True,
                'arr_delay_min': 20, 'status_arr': 'Gelandet'}

    handled = bp._apply_paid_arrival_escalation(
        payload, 'LX8', '2026-07-17', 'ZRH', 'ORD', pos, _merged)

    assert handled is True
    assert calls['fill'] == 1
    assert payload['in_flight'] is False
    assert payload['live'] is None
    assert payload['progress'] is None
    assert payload['live_status'] == 'lost'
    assert payload['arr_unreliable'] is True
    # nach dem paid-Write neu gemergte Ist-Zeiten durchgereicht
    assert payload['est_arr'] == '16:20'
    assert payload['status_arr'] == 'Gelandet'


def test_reliable_case_no_paid_and_stays_inflight(monkeypatch):
    """(b) frische Position + ETA in der Zukunft → kein paid, in_flight bleibt."""
    monkeypatch.setenv('FLIGHTLIVE_PAID_ESCALATE', '1')
    calls = _install_mocks(monkeypatch, arr_obs_exists=False)
    pos = {'seen_ts': _iso_min_ago(3)}        # frisch
    payload = {
        'ok': True, 'flight': 'LH400', 'date': '2026-07-17',
        'dest': {'iata': 'JFK'},
        'est_arr': _iso_arr_future(90),        # ETA in der Zukunft
        'in_flight': True, 'live': pos, 'progress': 0.4,
    }
    handled = bp._apply_paid_arrival_escalation(
        payload, 'LH400', '2026-07-17', 'FRA', 'JFK', pos, lambda *a, **k: {})
    assert handled is False
    assert calls['fill'] == 0
    assert payload['in_flight'] is True
    assert 'arr_unreliable' not in payload


def test_arr_obs_exists_short_circuits_no_paid(monkeypatch):
    """Existiert bereits eine ARR-Obs (Lücke gefüllt) → nicht unreliable, kein
    paid, in_flight bleibt (der Merge liefert die echten Zeiten separat)."""
    monkeypatch.setenv('FLIGHTLIVE_PAID_ESCALATE', '1')
    calls = _install_mocks(monkeypatch, arr_obs_exists=True)
    pos = {'seen_ts': _iso_min_ago(180)}
    payload = _stale_inflight_payload()
    handled = bp._apply_paid_arrival_escalation(
        payload, 'LX8', '2026-07-17', 'ZRH', 'ORD', pos, lambda *a, **k: {})
    assert handled is False
    assert calls['fill'] == 0
    assert payload['in_flight'] is True


def test_disabled_flag_no_op(monkeypatch):
    """FLIGHTLIVE_PAID_ESCALATE=0 → kompletter No-Op (auch der Ehrlich-Fallback
    unterbleibt)."""
    monkeypatch.setenv('FLIGHTLIVE_PAID_ESCALATE', '0')
    calls = _install_mocks(monkeypatch, arr_obs_exists=False)
    pos = {'seen_ts': _iso_min_ago(180)}
    payload = _stale_inflight_payload()
    handled = bp._apply_paid_arrival_escalation(
        payload, 'LX8', '2026-07-17', 'ZRH', 'ORD', pos, lambda *a, **k: {})
    assert handled is False
    assert calls['fill'] == 0
    assert payload['in_flight'] is True


def test_backfill_disabled_still_makes_honest(monkeypatch):
    """Escalation-Flag AN, aber FR24-Backfill-Flag AUS (prod-Default!) → KEIN paid
    Call, aber die unzuverlässige Anzeige wird trotzdem ehrlich gemacht."""
    monkeypatch.setenv('FLIGHTLIVE_PAID_ESCALATE', '1')
    calls = _install_mocks(monkeypatch, arr_obs_exists=False, enabled=False)
    pos = {'seen_ts': _iso_min_ago(180)}
    payload = _stale_inflight_payload()
    handled = bp._apply_paid_arrival_escalation(
        payload, 'LX8', '2026-07-17', 'ZRH', 'ORD', pos, lambda *a, **k: {})
    assert handled is True
    assert calls['fill'] == 0          # backfill disabled → kein Spending
    assert payload['in_flight'] is False
    assert payload['arr_unreliable'] is True
    assert payload['live_status'] == 'lost'


def test_lost_status_past_eta_escalates(monkeypatch):
    """FLIGHTSTATE-Engine sagte schon 'lost' (nicht mehr in_flight), ETA vorbei,
    keine ARR-Obs → gilt ebenfalls als unzuverlässig."""
    monkeypatch.setenv('FLIGHTLIVE_PAID_ESCALATE', '1')
    calls = _install_mocks(monkeypatch, arr_obs_exists=False)
    pos = {'seen_ts': _iso_min_ago(180)}
    payload = {
        'ok': True, 'flight': 'LX8', 'date': '2026-07-17',
        'dest': {'iata': 'ORD'}, 'est_arr': _iso_arr(150),
        'in_flight': False, 'live': None, 'progress': None,
        'live_status': 'lost',
    }
    handled = bp._apply_paid_arrival_escalation(
        payload, 'LX8', '2026-07-17', 'ZRH', 'ORD', pos, lambda *a, **k: {})
    assert handled is True
    assert calls['fill'] == 1
    assert payload['arr_unreliable'] is True


def test_no_dest_no_op(monkeypatch):
    monkeypatch.setenv('FLIGHTLIVE_PAID_ESCALATE', '1')
    calls = _install_mocks(monkeypatch, arr_obs_exists=False)
    pos = {'seen_ts': _iso_min_ago(180)}
    payload = _stale_inflight_payload()
    handled = bp._apply_paid_arrival_escalation(
        payload, 'LX8', '2026-07-17', 'ZRH', None, pos, lambda *a, **k: {})
    assert handled is False
    assert calls['fill'] == 0
