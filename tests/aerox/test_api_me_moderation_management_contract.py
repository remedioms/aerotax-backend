"""Static contract gates for credential-free Android moderation management.

These guards deliberately inspect the route implementation rather than use a
live account: they make a regression to an owner/target credential in Android
URLs or request bodies visible in ordinary local CI as well.
"""
from pathlib import Path


APP = Path(__file__).resolve().parents[2] / 'app.py'


def _route_source(name):
    source = APP.read_text(encoding='utf-8')
    marker = f'def {name}():'
    start = source.index(marker)
    next_route = source.find('\n@app.route(', start + len(marker))
    return source[start:] if next_route < 0 else source[start:next_route]


def test_android_moderation_management_has_static_header_only_routes():
    source = APP.read_text(encoding='utf-8')
    for path in (
        "/api/me/moderation/blocks",
        "/api/me/moderation/mutes",
        "/api/me/moderation/unblock",
        "/api/me/moderation/unmute",
    ):
        assert f"@app.route('{path}'" in source
    for name in (
        'me_moderation_blocks',
        'me_moderation_mutes',
        'me_moderation_unblock',
        'me_moderation_unmute',
    ):
        assert '_header_only_owner()' in _route_source(name)


def test_android_moderation_removal_accepts_only_an_opaque_public_reference():
    source = APP.read_text(encoding='utf-8')
    helper_start = source.index('def _me_moderation_remove(')
    helper_end = source.index('\n@app.route(', helper_start)
    helper = source[helper_start:helper_end]
    assert "set(body) - {'target_ref'}" in helper
    assert "if request.args:" in helper
    assert "AXU-[A-Za-z0-9_-]" in helper
    assert "_token_from_public_user_ref(ref)" in helper
    assert "target_token" not in helper


def test_android_moderation_list_does_not_serialize_internal_tokens():
    source = APP.read_text(encoding='utf-8')
    start = source.index('def _me_moderation_users(')
    end = source.index('\ndef _me_moderation_remove(', start)
    helper = source[start:end]
    assert "item = {'ref': ref}" in helper
    assert "_public_user_ref(raw)" in helper
    assert "'token'" not in helper
    assert "if request.args:" in _route_source('me_moderation_blocks')
    assert "if request.args:" in _route_source('me_moderation_mutes')
