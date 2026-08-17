"""Android Sign in with Apple callback and audience contract."""

from urllib.parse import parse_qs, urlsplit

import app as backend


def test_apple_audiences_include_native_and_configured_service_ids(monkeypatch):
    monkeypatch.setenv('APPLE_SERVICE_ID', 'de.aerosteuer.aerox.web')
    monkeypatch.setenv(
        'APPLE_ALLOWED_AUDIENCES',
        'de.aerosteuer.aerox.staging, de.aerosteuer.aerox.preview')

    assert backend._apple_audience_allowed(backend.APPLE_BUNDLE_ID)
    assert backend._apple_audience_allowed('de.aerosteuer.aerox.web')
    assert backend._apple_audience_allowed([
        'unrelated.example', 'de.aerosteuer.aerox.staging'])
    assert not backend._apple_audience_allowed('unrelated.example')
    assert not backend._apple_audience_allowed(None)


def test_android_callback_round_trips_only_expected_form_fields():
    state = 'state-1234567890-abcdef'
    user = '{"email":"relay@example.test"}'

    response = backend.app.test_client().post(
        '/callbacks/sign_in_with_apple',
        data={
            'code': 'one-time-code',
            'id_token': 'header.payload.signature',
            'state': state,
            'user': user,
            'ignored': 'must-not-be-forwarded',
        },
        content_type='application/x-www-form-urlencoded',
    )

    assert response.status_code == 303
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.headers['Referrer-Policy'] == 'no-referrer'
    location = response.headers['Location']
    parsed = urlsplit(location)
    assert parsed.scheme == 'intent'
    assert parsed.netloc == 'callback'
    assert parsed.fragment == (
        'Intent;package=de.aerosteuer.aerox;scheme=signinwithapple;end')
    assert parse_qs(parsed.query) == {
        'code': ['one-time-code'],
        'id_token': ['header.payload.signature'],
        'state': [state],
        'user': [user],
    }
    assert 'ignored' not in location


def test_android_callback_forwards_apple_error_to_close_browser():
    response = backend.app.test_client().post(
        '/callbacks/sign_in_with_apple',
        data={
            'error': 'user_cancelled_authorize',
            'state': 'state-1234567890-abcdef',
        },
        content_type='application/x-www-form-urlencoded',
    )

    assert response.status_code == 303
    assert 'error=user_cancelled_authorize' in response.headers['Location']


def test_android_callback_rejects_unbound_or_incomplete_success():
    client = backend.app.test_client()

    missing_state = client.post(
        '/callbacks/sign_in_with_apple',
        data={'code': 'code', 'id_token': 'header.payload.signature'},
        content_type='application/x-www-form-urlencoded',
    )
    missing_token = client.post(
        '/callbacks/sign_in_with_apple',
        data={'code': 'code', 'state': 'state-1234567890-abcdef'},
        content_type='application/x-www-form-urlencoded',
    )
    json_body = client.post(
        '/callbacks/sign_in_with_apple',
        json={'code': 'code', 'state': 'state-1234567890-abcdef'},
    )

    assert missing_state.status_code == 400
    assert missing_state.get_json()['error'] == 'invalid_state'
    assert missing_token.status_code == 400
    assert missing_token.get_json()['error'] == 'incomplete_callback'
    assert json_body.status_code == 415
    assert json_body.get_json()['error'] == 'invalid_content_type'


def test_android_callback_rejects_duplicate_security_fields():
    response = backend.app.test_client().post(
        '/callbacks/sign_in_with_apple',
        data=(
            'code=first&code=second&id_token=header.payload.signature&'
            'state=state-1234567890-abcdef'
        ),
        content_type='application/x-www-form-urlencoded',
    )

    assert response.status_code == 400
    assert response.get_json()['error'] == 'duplicate_callback_field'
