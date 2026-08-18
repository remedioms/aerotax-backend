"""Freie Airlines: Doppel-Erkennung und Owner-Mails (Owner 2026-08-17).

Drei Zusagen, die hier festgenagelt sind:
1. Ein unter „Andere" getippter Name, den es schon gibt, landet NICHT in der
   Warteschlange — die App bekommt die bestehende Airline zurück.
2. Klappt der Import sofort, geht eine „hat geklappt"-Mail raus.
3. Die Owner-Mail enthält niemals Kalender-Link, PDF-Inhalt oder Token.
"""

import app as A


TOKEN = 'AT-1234567890abcdef'


def _no_catalog(monkeypatch):
    monkeypatch.setattr(A, '_airline_catalog_load', lambda: [])


def test_builtin_name_variants_resolve_to_the_existing_airline(monkeypatch):
    _no_catalog(monkeypatch)
    for typed, expected in (
            ('Lufthansa', 'Lufthansa'),
            ('  lufthansa  ', 'Lufthansa'),
            ('LH', 'Lufthansa'),
            ('DLH', 'Lufthansa'),
            ('Lufthansa Cargo', 'Lufthansa Cargo'),
            ('lufthansa city', 'Lufthansa City'),
            ('SWISS', 'Swiss'),
            ('LX', 'Swiss'),
            ('ita airways', 'ITA Airways'),
            ('AZ', 'ITA Airways'),
            ('3V', 'Aerologic')):
        normalized = A._airline_request_normalized_name(typed)
        assert A._airline_request_existing_name(typed, normalized) == expected, typed


def test_removed_chips_run_through_the_request_pipeline(monkeypatch):
    """Eurowings und Austrian sind keine eingebauten Airlines mehr (Owner
    2026-08-18): ihre Links funktionierten fast nie. Wer sie eingibt, muss den
    Probier-Prozess durchlaufen — NICHT als „known" abgefangen werden."""
    _no_catalog(monkeypatch)
    for typed in ('Eurowings', 'eurowings', 'EW', 'Austrian', 'OS', 'AUA'):
        normalized = A._airline_request_normalized_name(typed)
        assert A._airline_request_existing_name(typed, normalized) == '', typed


def test_removed_chip_counts_as_existing_once_relearned(monkeypatch):
    """Klappt eine Eurowings-Quelle später, steht sie im Katalog — ab dann
    greift die Doppel-Erkennung wieder."""
    monkeypatch.setattr(
        A, '_airline_catalog_load',
        lambda: [{'display_name': 'Eurowings'}])
    normalized = A._airline_request_normalized_name('eurowings')
    assert A._airline_request_existing_name(
        'eurowings', normalized) == 'Eurowings'


def test_catalog_airline_counts_as_existing(monkeypatch):
    monkeypatch.setattr(
        A, '_airline_catalog_load',
        lambda: [{'display_name': 'TAP Air Portugal'}])
    normalized = A._airline_request_normalized_name('tap air portugal')
    assert A._airline_request_existing_name(
        'tap air portugal', normalized) == 'TAP Air Portugal'


def test_genuinely_new_airline_is_not_swallowed(monkeypatch):
    _no_catalog(monkeypatch)
    normalized = A._airline_request_normalized_name('Norse Atlantic')
    assert A._airline_request_existing_name('Norse Atlantic', normalized) == ''


def test_known_airline_skips_queue_and_owner_mail(monkeypatch):
    _no_catalog(monkeypatch)
    monkeypatch.setattr(
        A, '_airline_request_store',
        lambda row: (_ for _ in ()).throw(AssertionError('must not queue')))
    monkeypatch.setattr(
        A, '_airline_request_owner_mail',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError('must not mail')))
    with A.app.test_request_context(
            f'/api/user/airline-request/{TOKEN}', method='POST',
            json={'airline_name': 'LH', 'source_kind': 'ical_url',
                  'source_url': 'https://private.example/roster.ics'}):
        response = A.submit_airline_support_request(TOKEN)
    payload = response.get_json()
    assert payload['ok'] is True
    assert payload['status'] == 'known'
    assert payload['known_airline'] == 'Lufthansa'


def test_immediate_success_sends_it_works_mail(monkeypatch):
    mails = []
    _no_catalog(monkeypatch)
    monkeypatch.setattr(A, '_airline_request_profile_feed',
                        lambda _token: {'url': 'https://private.example/r.ics',
                                        'events': [{'id': 1}, {'id': 2}]})
    monkeypatch.setattr(A, '_calendar_feed_encrypt_value',
                        lambda value, field: 'encrypted')
    monkeypatch.setattr(A, '_airline_request_store',
                        lambda row: dict(row, id=42))
    monkeypatch.setattr(A, '_airline_catalog_promote',
                        lambda *_args, **_kwargs: True)
    monkeypatch.setattr(A, '_airline_request_owner_mail',
                        lambda subject, text: mails.append((subject, text)))
    with A.app.test_request_context(
            f'/api/user/airline-request/{TOKEN}', method='POST',
            json={'airline_name': 'Norse Atlantic', 'homebase': 'OSL',
                  'source_kind': 'ical_url',
                  'source_url': 'https://private.example/r.ics'}):
        response = A.submit_airline_support_request(TOKEN)
    payload = response.get_json()
    assert payload['status'] == 'supported'
    assert len(mails) == 1
    subject, text = mails[0]
    assert 'Airline live' in subject and 'Norse Atlantic' in subject
    assert 'Dienstplan-Einträge: 2' in text
    # Niemals die Quelle selbst in der Mail.
    assert 'private.example' not in text and TOKEN not in text


def test_pending_request_stays_silent_until_the_watchdog_runs(monkeypatch):
    """Der Endpoint mailt NICHT bei `pending` — sonst käme die Meldung doppelt
    (der Wächter meldet beim ersten echten Fehlversuch)."""
    mails = []
    _no_catalog(monkeypatch)
    monkeypatch.setattr(A, '_airline_request_profile_feed', lambda _token: {})
    monkeypatch.setattr(A, '_calendar_feed_encrypt_value',
                        lambda value, field: 'encrypted')
    monkeypatch.setattr(A, '_airline_request_store', lambda row: dict(row, id=7))
    monkeypatch.setattr(A, '_airline_request_owner_mail',
                        lambda subject, text: mails.append(subject))
    with A.app.test_request_context(
            f'/api/user/airline-request/{TOKEN}', method='POST',
            json={'airline_name': 'Norse Atlantic', 'source_kind': 'ical_url',
                  'source_url': 'https://private.example/r.ics'}):
        response = A.submit_airline_support_request(TOKEN)
    assert response.get_json()['status'] == 'pending'
    assert mails == []
