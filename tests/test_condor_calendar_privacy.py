import pytest

import app as app_module
from blueprints import hotel_rooms_blueprint as hotel_rooms


CONDOR_ICS = """BEGIN:VCALENDAR\r
VERSION:2.0\r
X-WR-CALNAME:Kai Personal Roster\r
BEGIN:VEVENT\r
UID:personal-feed-402644F\r
DTSTART:20260806T201500Z\r
DTEND:20260807T073500Z\r
SUMMARY:DE2360 FRA-BKK\r
LOCATION:FRA - BKK\r
ATTENDEE;CN=HAEBEL KAI:mailto:kai@example.invalid\r
DESCRIPTION:CP 402644F HAEBEL\\, KAI (FRA)\\nFO 374486H CLEMENS\\, ROBERT (FRA)\\nHotel\\nSheraton Royal Orchid\\nBangkok\\n+66 2 2660123\r
END:VEVENT\r
END:VCALENDAR\r
"""


@pytest.fixture
def client():
    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client()


def _allow_token(monkeypatch):
    monkeypatch.setattr(
        app_module, '_validate_token',
        lambda token: app_module._TokenValidationResult(
            app_module._TokenValidationState.VALID, 'condor@test.invalid'))


def test_condor_ics_sanitizer_removes_people_hotel_and_private_uid():
    clean = app_module._condor_ics_privacy_sanitize(CONDOR_ICS)

    assert 'SUMMARY:DE2360 FRA-BKK' in clean
    assert 'LOCATION:FRA - BKK' in clean
    assert 'UID:aerox-condor-' in clean
    assert 'DESCRIPTION' not in clean
    assert 'ATTENDEE' not in clean
    assert 'X-WR-CALNAME' not in clean
    for secret in ('402644F', '374486H', 'HAEBEL', 'Sheraton',
                   'personal-feed-402644F'):
        assert secret not in clean


def test_condor_privacy_detection_accepts_cube_host_or_profile(monkeypatch):
    assert app_module._condor_calendar_privacy_required(
        'AT-any', 'https://calendar.cube.aero/private.ics') is True

    monkeypatch.setattr(
        app_module, '_profile_load',
        lambda token: {'profile': {'airline': 'Condor'}})
    assert app_module._condor_calendar_privacy_required('AT-condor') is True


def test_condor_direct_import_persists_only_sanitized_events(monkeypatch):
    saved = {}
    profile_payload = {'profile': {'airline': 'Condor'}}
    monkeypatch.setattr(app_module, '_profile_load', lambda token: profile_payload)
    monkeypatch.setattr(app_module, '_profile_load_from_disk',
                        lambda token: {'profile': {'airline': 'Condor'}})
    monkeypatch.setattr(
        app_module, '_profile_save',
        lambda token, profile, full_disk_payload=None:
            saved.update({'profile': profile,
                          'disk': full_disk_payload}) or True)
    monkeypatch.setattr(app_module, '_ical_briefings_load', lambda token: {})
    monkeypatch.setattr(
        app_module, '_ical_briefings_save',
        lambda token, briefings: saved.update({'briefings': briefings}) or True)
    monkeypatch.setattr(
        app_module, '_reconcile_month_briefings',
        lambda *args, **kwargs: {'feed_dates': 1, 'cleared': 0,
                                 'removed_dates': [], 'window': 'test'})

    response = app_module.app.test_client().post(
        '/api/user/calendar-feed/condor-privacy-test/import',
        json={'ics_text': CONDOR_ICS})

    assert response.status_code == 200, response.get_json()
    feed = (saved.get('profile') or {}).get('calendar_feed') or {}
    assert feed.get('url') == ''
    assert not feed.get('pickup_ical_url')
    persisted = str(feed.get('events') or [])
    for secret in ('402644F', '374486H', 'HAEBEL', 'Sheraton',
                   'personal-feed-402644F'):
        assert secret not in persisted
    assert 'DE2360' in persisted


def test_condor_hotel_rating_persists_the_real_hotel_name(
        client, monkeypatch):
    """Owner 2026-08-08: „solange du dafuer sorgst das nur condor crew mit valid
    ical link die hotels sehen ist das doch alles in ordnung." Der echte Name
    wird gespeichert (wie bei LH) — das Stations-Gate bleibt die Bedingung, s.
    test_condor_hotel_rating_rejects_station_outside_own_roster."""
    captured = []
    _allow_token(monkeypatch)
    # Frühere Full-Suite-Tests reloaden ``app``; den Endpoint-Gate deshalb an
    # seiner direkten Blueprint-Grenze injizieren statt eine evtl. alte
    # app-Modulinstanz zu patchen. Profil-/Roster-Erkennung ist oben separat
    # als pure Helper-Grenze abgedeckt.
    monkeypatch.setattr(hotel_rooms, '_condor_rating_station_allowed',
                        lambda token, iata: iata == 'MIA')
    monkeypatch.setattr(hotel_rooms, '_rate_limited',
                        lambda *args, **kwargs: False)
    monkeypatch.setattr(hotel_rooms, '_sb_insert_report',
                        lambda row: captured.append(dict(row)) or True)
    monkeypatch.setattr(hotel_rooms, '_disk_load', lambda name: [])
    monkeypatch.setattr(hotel_rooms, '_disk_save', lambda name, rows: True)

    response = client.post('/api/hotel-rooms/AT-condor/report',
                           headers={'Authorization': 'Bearer AT-condor'}, json={
        'hotel_name': 'Pullman Miami Airport',
        'hotel_iata': 'MIA',
        'room_number_low': 412,
        'overall_rating': 5,
        'breakfast_rating': 4,
        'note': 'Zimmer zum Innenhof, ruhig.',
        'renovated_year': 2024,
    })

    assert response.status_code == 200
    assert len(captured) == 1
    row = captured[0]
    assert row['hotel_name'] == 'Pullman Miami Airport'
    assert row['hotel_iata'] == 'MIA'
    assert row['overall_rating'] == 5
    assert row['breakfast_rating'] == 4
    # Volle Parität mit LH: Zimmer-Bezug, Notiz und Baujahr bleiben erhalten.
    assert row['room_number_low'] == 412
    assert row['note'] == 'Zimmer zum Innenhof, ruhig.'
    assert row['renovated_year'] == 2024
    # Die ausgelieferte Zeile traegt KEINEN Airline-Tag und kein Melder-Token —
    # die Zuordnung „Condor schlaeft hier" entsteht ausschliesslich im
    # gegateten Crew-Hotel-Verzeichnis, nie in dieser oeffentlichen Liste.
    public = str(response.get_json()['report'])
    assert 'ondor' not in public
    assert 'reported_by_token' not in public


def test_condor_hotel_rating_requires_station(client, monkeypatch):
    """Ohne IATA gibt es nichts gegen den Roster zu pruefen → 400, nie offen."""
    _allow_token(monkeypatch)
    monkeypatch.setattr(hotel_rooms, '_condor_rating_station_allowed',
                        lambda token, iata: True)
    monkeypatch.setattr(hotel_rooms, '_rate_limited',
                        lambda *args, **kwargs: False)

    response = client.post('/api/hotel-rooms/AT-condor/report',
                           headers={'Authorization': 'Bearer AT-condor'}, json={
        'hotel_name': 'Pullman Miami Airport',
        'overall_rating': 5,
    })

    assert response.status_code == 400


def test_condor_hotel_rating_rejects_station_outside_own_roster(
        client, monkeypatch):
    _allow_token(monkeypatch)
    monkeypatch.setattr(hotel_rooms, '_condor_rating_station_allowed',
                        lambda token, iata: iata == 'MIA')
    monkeypatch.setattr(hotel_rooms, '_rate_limited',
                        lambda *args, **kwargs: False)

    response = client.post('/api/hotel-rooms/AT-condor/report',
                           headers={'Authorization': 'Bearer AT-condor'}, json={
        'hotel_name': 'Secret iCal Hotel',
        'hotel_iata': 'LAX',
        'overall_rating': 5,
    })

    assert response.status_code == 403
    assert response.get_json()['error'] == 'station_not_in_own_roster'
