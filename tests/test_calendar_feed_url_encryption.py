import base64
import json

import app as A
from blueprints import calendar_sweep


def _payload():
    return {
        'token': 'fixture-user',
        'profile': {
            'calendar_feed': {
                'url': 'https://calendar.example/crew/private-duty-token.ics',
                'pickup_ical_url': 'https://calendar.example/crew/pickup-secret.ics',
                'imported_at': '2026-08-03T00:00:00',
            },
            'calendar_feed_2': {
                'url': 'https://calendar.example/crew/private-off-token.ics',
            },
        },
    }


def test_calendar_feed_urls_are_encrypted_at_rest_and_roundtrip():
    plain = _payload()
    stored = A._calendar_feed_urls_encrypt_payload(plain)
    serialized = json.dumps(stored, sort_keys=True)

    assert 'calendar.example' not in serialized
    assert 'private-duty-token' not in serialized
    primary = stored['profile']['calendar_feed']
    secondary = stored['profile']['calendar_feed_2']
    assert 'url' not in primary
    assert 'pickup_ical_url' not in primary
    assert 'url' not in secondary
    assert primary['url_enc'].startswith('AXF1-')
    assert primary['pickup_ical_url_enc'].startswith('AXF1-')
    assert secondary['url_enc'].startswith('AXF1-')

    loaded = A._calendar_feed_urls_decrypt_payload(stored)
    assert loaded['profile']['calendar_feed']['url'] == (
        plain['profile']['calendar_feed']['url'])
    assert loaded['profile']['calendar_feed']['pickup_ical_url'] == (
        plain['profile']['calendar_feed']['pickup_ical_url'])
    assert loaded['profile']['calendar_feed_2']['url'] == (
        plain['profile']['calendar_feed_2']['url'])


def test_unchanged_profile_save_reuses_existing_ciphertext():
    stored_once = A._calendar_feed_urls_encrypt_payload(_payload())
    in_memory = A._calendar_feed_urls_decrypt_payload(stored_once)
    stored_twice = A._calendar_feed_urls_encrypt_payload(in_memory)
    assert stored_twice == stored_once


def test_tampered_or_wrong_field_ciphertext_fails_closed():
    encrypted = A._calendar_feed_encrypt_value(
        'https://calendar.example/private.ics', 'url')
    token = encrypted[len(A._CALENDAR_FEED_URL_PREFIX):]
    token += '=' * (-len(token) % 4)
    raw = bytearray(base64.urlsafe_b64decode(token))
    raw[len(raw) // 2] ^= 0x01
    tampered = (A._CALENDAR_FEED_URL_PREFIX
                + base64.urlsafe_b64encode(bytes(raw)).decode().rstrip('='))
    assert A._calendar_feed_decrypt_value(tampered, 'url') == ''
    assert A._calendar_feed_decrypt_value(encrypted, 'pickup_ical_url') == ''


def test_profile_disk_writer_never_receives_plain_feed_url(monkeypatch):
    captured = {}
    monkeypatch.setattr(A, '_profile_save_to_supabase', lambda token, profile: False)
    monkeypatch.setattr(A, '_user_profile_path', lambda token: '/tmp/profile-fixture.json')
    monkeypatch.setattr(
        A, '_atomic_write_json',
        lambda path, payload: captured.update(path=path, payload=payload))

    profile = _payload()['profile']
    assert A._profile_save('fixture-user', profile) is True
    serialized = json.dumps(captured['payload'], sort_keys=True)
    assert 'calendar.example' not in serialized
    assert captured['payload']['profile']['calendar_feed']['url_enc'].startswith(
        'AXF1-')


def test_calendar_sweep_reads_encrypted_narrow_projection(monkeypatch):
    # Der Volltest lädt ``app`` für Isolation teilweise neu. Eine explizite,
    # stabile Recovery-Secret simuliert hier die Produktion und sorgt dafür,
    # dass der bereits importierte Sweep-Blueprint denselben Schlüsselraum
    # benutzt wie das neu geladene App-Modul.
    monkeypatch.setenv('RECOVERY_SECRET', 'r' * 64)
    url = 'https://calendar.example/crew/duty.ics'
    encrypted = A._calendar_feed_encrypt_value(url, 'url')
    candidate = calendar_sweep._row_to_candidate({
        'token': 'fixture-user',
        'feed_url': None,
        'feed_url_enc': encrypted,
        'feed_imported_at': '2026-08-03T00:00:00',
        'feed2_url': None,
        'feed2_url_enc': None,
    })
    assert candidate['url'] == url
