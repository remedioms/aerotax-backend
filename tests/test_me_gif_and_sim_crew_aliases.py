"""Bearer-only aliases for the remaining GIF and simulator-crew clients."""

import sys

import app as A
from blueprints import gif_search_blueprint as gif
from blueprints import lh_flightops as lh


OWNER = 'AT-1234567890abcdef'
FRIEND = 'AT-cafebabedeadbeef'


def _valid(_token):
    return A._TokenValidationResult(
        A._TokenValidationState.VALID, 'owner@example.test')


def test_gif_me_aliases_reuse_the_header_authenticated_handlers(monkeypatch):
    seen = []
    monkeypatch.setattr(gif, 'gif_search', lambda token: seen.append(
        ('search', token)) or A.jsonify({'ok': True, 'items': []}))
    monkeypatch.setattr(gif, 'gif_import', lambda token: seen.append(
        ('import', token)) or A.jsonify({'ok': True, 'url': '/api/wall/image/x/a.gif'}))
    with A.app.test_request_context('/api/me/gif-search'):
        assert A.app.make_response(gif.me_gif_search()).get_json()['ok'] is True
    with A.app.test_request_context('/api/me/gif-search/import', method='POST'):
        assert A.app.make_response(gif.me_gif_import()).get_json()['ok'] is True
    assert seen == [('search', ''), ('import', '')]


def test_me_sim_crew_uses_owner_header_and_publicizes_response(monkeypatch):
    # lh_flightops imports app inside the alias so the test must patch the same
    # module object even after a preceding app-module reload test.
    monkeypatch.setitem(sys.modules, 'app', A)
    monkeypatch.setattr(A, '_validate_token', _valid)
    seen = []
    monkeypatch.setattr(
        lh, 'flightops_sim_crewlist',
        lambda token: seen.append(token) or A.jsonify({
            'ok': True,
            'crew': [{'aerox': {'token': FRIEND}}],
        }),
    )
    with A.app.test_request_context(
        '/api/me/lh/flightops/sim-crewlist', method='POST',
        headers={'Authorization': f'Bearer {OWNER}'}, json={'date': '2026-08-17'},
    ):
        body = A.app.make_response(lh.me_flightops_sim_crewlist()).get_json()
    assert seen == [OWNER]
    assert body['crew'][0]['aerox']['token'].startswith('AXU-')
    assert FRIEND not in str(body)


def test_me_sim_crew_rejects_query_and_extra_body_fields(monkeypatch):
    monkeypatch.setitem(sys.modules, 'app', A)
    monkeypatch.setattr(A, '_validate_token', _valid)
    headers = {'Authorization': f'Bearer {OWNER}'}
    with A.app.test_request_context(
        '/api/me/lh/flightops/sim-crewlist?token=x', method='POST',
        headers=headers, json={'date': '2026-08-17'},
    ):
        assert A.app.make_response(lh.me_flightops_sim_crewlist()).status_code == 400
    with A.app.test_request_context(
        '/api/me/lh/flightops/sim-crewlist', method='POST', headers=headers,
        json={'date': '2026-08-17', 'token': 'x'},
    ):
        assert A.app.make_response(lh.me_flightops_sim_crewlist()).status_code == 400
