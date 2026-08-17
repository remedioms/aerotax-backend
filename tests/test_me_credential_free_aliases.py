"""Focused contract tests for the final owner-bound `/api/me` aliases."""

import app as A


OWNER = 'AT-1234567890abcdef'
FRIEND = 'AT-cafebabedeadbeef'


def _valid(_token):
    return A._TokenValidationResult(
        A._TokenValidationState.VALID, 'owner@example.test')


def _json(response):
    return A.app.make_response(response).get_json()


def test_new_aliases_are_header_only_and_cover_the_legacy_owner_routes():
    src = open(A.__file__).read()
    expected = {
        '/api/me/roster-pdf/import': "methods=['POST']",
        '/api/me/briefing': "methods=['GET']",
        "/api/me/briefing/<datum>": "methods=['PUT']",
        '/api/me/flight-notes': "methods=['GET']",
        "/api/me/flight-notes/<datum>": "methods=['GET', 'PUT']",
        '/api/me/voice-note': "methods=['GET']",
        "/api/me/voice-note/<datum>": "methods=['GET', 'POST', 'DELETE']",
        '/api/me/destination-notes': "methods=['GET']",
        "/api/me/destination-notes/<iata>": "methods=['GET', 'PUT']",
        '/api/me/trip-stats': "methods=['GET']",
        '/api/me/manual-pins': "methods=['GET', 'POST']",
        "/api/me/hangouts/<pin_id>": "methods=['GET']",
        "/api/me/hangouts/<pin_id>/join": "methods=['POST']",
        '/api/me/wall/feed': "methods=['GET']",
        '/api/me/wall/post': "methods=['POST']",
        "/api/me/wall/post/<post_id>/comments": "methods=['GET']",
    }
    for route, methods in expected.items():
        assert f"@app.route('{route}', {methods})" in src
    # There is intentionally no invented GET-day briefing contract.
    assert "@app.route('/api/me/briefing/<datum>', methods=['GET']" not in src


def test_owner_routes_dispatch_the_header_principal_not_a_path_credential(monkeypatch):
    monkeypatch.setattr(A, '_validate_token', _valid)
    seen = []
    monkeypatch.setattr(
        A, 'put_flight_note',
        lambda token, day: seen.append((token, day)) or A.jsonify({'ok': True}),
    )
    with A.app.test_request_context(
        '/api/me/flight-notes/2026-08-17', method='PUT',
        headers={'Authorization': f'Bearer {OWNER}'}, json={'note': 'x'},
    ):
        assert _json(A.me_flight_note('2026-08-17'))['ok'] is True
    assert seen == [(OWNER, '2026-08-17')]


def test_public_read_alias_redacts_foreign_credential(monkeypatch):
    monkeypatch.setattr(A, '_validate_token', _valid)
    monkeypatch.setattr(
        A, 'get_wall_feed',
        lambda token: A.jsonify({'posts': [{'author_token': FRIEND}]}),
    )
    with A.app.test_request_context(
        '/api/me/wall/feed', headers={'Authorization': f'Bearer {OWNER}'},
    ):
        body = _json(A.me_wall_feed())
    assert body['posts'][0]['author_token'].startswith('AXU-')
    assert FRIEND not in str(body)


def test_binary_voice_note_response_is_not_json_projected(monkeypatch):
    from flask import Response

    monkeypatch.setattr(A, '_validate_token', _valid)
    monkeypatch.setattr(
        A, 'get_voice_note',
        lambda token, day: Response(b'audio', mimetype='audio/mp4'),
    )
    with A.app.test_request_context(
        '/api/me/voice-note/2026-08-17',
        headers={'Authorization': f'Bearer {OWNER}'},
    ):
        response = A.app.make_response(A.me_voice_note('2026-08-17'))
    assert response.mimetype == 'audio/mp4'
    assert response.get_data() == b'audio'
