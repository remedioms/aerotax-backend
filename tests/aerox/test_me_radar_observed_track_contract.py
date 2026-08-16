"""Contract guard for the Android-only observed-track write path."""

from pathlib import Path


BLUEPRINT = (
    Path(__file__).resolve().parents[2]
    / 'blueprints'
    / 'aerox_data_blueprint.py'
)


def test_me_observed_track_is_header_only_and_reuses_validation():
    source = BLUEPRINT.read_text(encoding='utf-8')
    marker = "@aerox_data_bp.route('/api/me/radar/observed-track', methods=['POST'])"
    start = source.index(marker)
    end = source.find('\n@aerox_data_bp.route(', start + len(marker))
    route = source[start:] if end < 0 else source[start:end]

    assert "def ax_observed_track_me():" in route
    assert 'from app import _header_only_owner' in route
    assert '_header_only_owner()' in route
    assert 'return ax_observed_track()' in route
    assert '<token>' not in route
    assert 'if request.args:' in route
    assert "set(body) - {'reg', 'points'}" in route
    assert "not 4 <= len(points) <= 100" in route
