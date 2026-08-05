"""Regression: Performance-Caps dürfen das Reconcile-Fenster nicht kürzen.

Vor dem Fix bekam `_reconcile_month_briefings` dieselbe auf 200 Events gekappte
Liste wie der teure Briefing-Neuaufbau. Bei mehr als 200 aktuellen Events endet
die Relevanz-Auswahl chronologisch früher als der echte Feed. Der Zukunfts-
Geist-Prune interpretierte echte Dienste hinter diesem künstlichen `fmax` als
storniert und löschte sie. Im URL-Pfad lag davor zusätzlich ein 300er-Cap.

Vertrag: Aufbau/Snapshot dürfen gekappt bleiben; die billige Datums-Abdeckung
für Reconcile muss in URL-/Direkt-ICS- und EventKit-Pfad vollständig sein.
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as backend


def _d(offset):
    return (datetime.now() + timedelta(days=offset)).strftime('%Y-%m-%d')


def _event(offset):
    day = _d(offset)
    return {
        'summary': f'OFF {offset}',
        'location': '',
        'start': day,
        'end': day,
        'start_iso': f'{day}T08:00:00Z',
        'end_iso': f'{day}T09:00:00Z',
        '_multiday_dates': [day],
    }


def _ics(count):
    rows = ['BEGIN:VCALENDAR', 'VERSION:2.0']
    for i in range(count):
        day8 = _d(i).replace('-', '')
        rows.extend([
            'BEGIN:VEVENT',
            f'UID:cap-{i}@example.test',
            f'DTSTART:{day8}T080000Z',
            f'DTEND:{day8}T090000Z',
            f'SUMMARY:OFF {i}',
            'END:VEVENT',
        ])
    rows.append('END:VCALENDAR')
    return '\r\n'.join(rows)


def _patch_persistence(monkeypatch):
    saved = {}
    monkeypatch.setattr(backend, '_profile_load', lambda _t: {})
    monkeypatch.setattr(backend, '_profile_load_from_disk', lambda _t: {})
    monkeypatch.setattr(
        backend, '_profile_save',
        lambda _t, profile, full_disk_payload=None: saved.update(
            {'profile': profile, 'disk': full_disk_payload}))
    monkeypatch.setattr(backend, '_ical_briefings_load', lambda _t: {})
    monkeypatch.setattr(
        backend, '_ical_briefings_save',
        lambda _t, briefings: saved.update({'briefings': briefings}) or True)
    return saved


def _spy_reconcile(monkeypatch):
    seen = {}

    # Signatur spiegelt die ECHTE (inkl. prev_feed_min/-_at der Edge-Jump-
    # Erkennung) — ein veralteter Mock wäre ein Test-Infra-Bug.
    def spy(_token, _briefings, feed_events, full_clean=False,
            prev_feed_min=None, prev_feed_min_at=None):
        seen['events'] = list(feed_events)
        seen['full_clean'] = full_clean
        seen['prev_feed_min'] = prev_feed_min
        seen['prev_feed_min_at'] = prev_feed_min_at
        dates = sorted(
            d for ev in feed_events
            for d in (ev.get('_multiday_dates') or [ev.get('start')]) if d)
        return {'feed_dates': len(set(dates)), 'cleared': 0, 'removed_dates': [],
                'window': f'{dates[0]}..{dates[-1]}' if dates else None}

    monkeypatch.setattr(backend, '_reconcile_month_briefings', spy)
    return seen


def test_direct_ics_reconcile_sees_events_beyond_300_and_200_caps(monkeypatch):
    """Der Direkt-/URL-Pfad reicht alle 350 normalisierten Events ans
    Reconcile, obwohl Snapshot (300) und Briefing-Aufbau (200) begrenzt sind."""
    saved = _patch_persistence(monkeypatch)
    seen = _spy_reconcile(monkeypatch)

    response = backend.app.test_client().post(
        '/api/user/calendar-feed/cap-url-test/import',
        json={'ics_text': _ics(350), 'source': 'ics_direct'})

    assert response.status_code == 200, response.get_json()
    assert len(seen.get('events') or []) == 350
    assert max(ev.get('start') for ev in seen['events']) == _d(349)
    assert len((saved.get('profile') or {}).get('calendar_feed', {}).get('events') or []) == 300
    assert len(saved.get('briefings') or {}) == 200


def test_eventkit_reconcile_sees_events_beyond_200_cap(monkeypatch):
    """Auch der EKEventStore-Pfad verwendet alle 350 Eingabe-Events als
    Abdeckung, während nur 200 davon den Briefing-Neuaufbau durchlaufen."""
    saved = _patch_persistence(monkeypatch)
    seen = _spy_reconcile(monkeypatch)
    events = [{k: v for k, v in _event(i).items()
               if k in ('summary', 'location', 'start_iso', 'end_iso')}
              for i in range(350)]

    response = backend.app.test_client().post(
        '/api/user/calendar-events/cap-ek-test/upload',
        json={'events': events})

    assert response.status_code == 200, response.get_json()
    assert len(seen.get('events') or []) == 350
    assert max(ev.get('start') for ev in seen['events']) == _d(349)
    assert len(saved.get('briefings') or {}) == 200


def test_full_coverage_keeps_real_capped_day_and_prunes_true_ghost(monkeypatch):
    """Semantik des Fixes: ein echter Feed-Tag hinter Position 200 bleibt;
    ein Tag wirklich hinter dem vollständigen Feed-Horizont wird weiter
    gelöscht (Golden-Truth-Regel bleibt erhalten)."""
    monkeypatch.setattr(backend, 'SB_AVAILABLE', False)
    full = [_event(i) for i in range(220)]
    selected = backend._select_relevant_feed_events(full, 200)
    assert max(ev['start'] for ev in selected) == _d(199)

    real_but_capped = _d(215)
    true_ghost = _d(230)
    old = lambda label: {
        'ical_summary': label,
        'ical_location': 'FRA',
        'ical_start_iso': f'{real_but_capped}T08:00:00Z',
    }
    briefings = {
        real_but_capped: old('ECHTER DIENST HINTER CAP'),
        true_ghost: old('TATSAECHLICHER GEIST'),
    }
    briefings, _ = backend._ics_events_to_briefings(selected,
                                                     existing=briefings)

    dbg = backend._reconcile_month_briefings(
        'TESTTOKEN_NOSB', briefings, full)

    assert dbg['window'].endswith(_d(219))
    assert real_but_capped in briefings
    assert true_ghost not in briefings
