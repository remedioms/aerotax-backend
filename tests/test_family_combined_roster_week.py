"""Privacy and partial-failure coverage for the Family combined-week contract."""
import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import datetime as dt

import app as A
from blueprints import family_watch as FW


FAMILY = 'AT-FAM-TEST'
CREW_A = 'AT-CREW-A-PRIVATE'
CREW_B = 'AT-CREW-B-PRIVATE'


def _allowed_capability():
    return FAMILY, None


def test_combined_week_reads_only_server_authorised_crews(monkeypatch):
    """The query cannot smuggle an arbitrary crew selector into the batch."""
    seen = []

    def load(crew_token, days_limit):
        seen.append((crew_token, days_limit))
        return [{
            'datum': '2026-08-17', 'routing': 'FRA-SFO', 'eur': 999.0,
            'token': crew_token, 'start_time': '10:00',
        }]

    monkeypatch.setattr(FW, 'family_bearer_capability', _allowed_capability)
    monkeypatch.setattr(FW, '_fw_today', lambda: dt.date(2026, 8, 17))
    monkeypatch.setattr(FW, '_resolve_crews_for_family',
                        lambda family: [CREW_A])
    monkeypatch.setattr(FW, '_load_crew_roster_days', load)

    A.app.testing = True
    response = A.app.test_client().get(
        '/api/me/family-roster/week?days=99&crew=AT-NOT-A-CONNECTION',
        headers={'Authorization': f'Bearer {FAMILY}'},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body['ok'] is True and body['count'] == 1
    # A client selector is ignored; the bounded batch is allowed to reach the
    # server's +63-day ceiling, never an arbitrary crew or range.
    assert seen == [(CREW_A, 64)]
    row = body['crew'][0]
    assert row['shared'] is True and row['available'] is True
    assert row['days'] == [{
        'datum': '2026-08-17', 'routing': 'FRA-SFO', 'start_time': '10:00',
    }]
    encoded = response.get_data(as_text=True)
    assert CREW_A not in encoded
    assert 'eur' not in row['days'][0]
    assert 'token' not in row['days'][0]


def test_combined_week_keeps_authorised_rows_on_partial_source_failure(monkeypatch):
    def load(crew_token, _days_limit):
        if crew_token == CREW_B:
            raise RuntimeError('transient source failure')
        return [{'datum': '2026-08-18', 'klass': 'OFF'}]

    monkeypatch.setattr(FW, 'family_bearer_capability', _allowed_capability)
    monkeypatch.setattr(FW, '_fw_today', lambda: dt.date(2026, 8, 17))
    monkeypatch.setattr(FW, '_resolve_crews_for_family',
                        lambda family: [CREW_A, CREW_B])
    monkeypatch.setattr(FW, '_load_crew_roster_days', load)

    A.app.testing = True
    response = A.app.test_client().get(
        '/api/me/family-roster/week?days=7',
        headers={'Authorization': f'Bearer {FAMILY}'},
    )

    assert response.status_code == 200
    rows = response.get_json()['crew']
    assert len(rows) == 2
    assert rows[0]['available'] is True
    assert rows[0]['days'] == [{'datum': '2026-08-18', 'klass': 'OFF'}]
    assert rows[1]['shared'] is True
    assert rows[1]['available'] is False
    assert rows[1]['days'] == []


def test_combined_week_accepts_the_bounded_ios_window_without_crew_fanout(monkeypatch):
    seen = []

    def load(crew_token, days_limit):
        seen.append((crew_token, days_limit))
        return [
            {'datum': '2026-07-27', 'routing': 'FRA-LHR'},
            {'datum': '2026-10-18', 'routing': 'LHR-FRA'},
            {'datum': '2026-10-19', 'routing': 'OUTSIDE'},
        ]

    monkeypatch.setattr(FW, 'family_bearer_capability', _allowed_capability)
    monkeypatch.setattr(FW, '_fw_today', lambda: dt.date(2026, 8, 17))
    monkeypatch.setattr(FW, '_resolve_crews_for_family', lambda _: [CREW_A])
    monkeypatch.setattr(FW, '_load_crew_roster_days', load)

    A.app.testing = True
    response = A.app.test_client().get(
        '/api/me/family-roster/week?start_offset=-21&days=84',
        headers={'Authorization': f'Bearer {FAMILY}'},
    )

    assert response.status_code == 200
    # -21 + 84 days ends at +62. The iOS strip still has the +63 geometry,
    # whose final cell remains empty until a future contract expands it.
    assert seen == [(CREW_A, 63)]
    assert response.get_json()['crew'][0]['days'] == [
        {'datum': '2026-07-27', 'routing': 'FRA-LHR'},
        {'datum': '2026-10-18', 'routing': 'LHR-FRA'},
    ]


def test_combined_week_requires_the_existing_family_capability(monkeypatch):
    monkeypatch.setattr(
        FW, 'family_bearer_capability',
        lambda: (None, (A.jsonify({'ok': False, 'error': 'unauthorized'}), 401)),
    )
    A.app.testing = True
    response = A.app.test_client().get('/api/me/family-roster/week')
    assert response.status_code == 401
    assert response.get_json()['error'] == 'unauthorized'
