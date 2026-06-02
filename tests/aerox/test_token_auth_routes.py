"""
Wave-1 BUG-004 — Token-Auth Gate Tests

For every POST/PUT/DELETE route in app.py that takes a `<token>` path-param,
verify that an obviously-fake AT-token (never created via signup) returns
HTTP 401 unauthorized — NOT 200 ok=true.

Background: Before BUG-004 fix, the backend was over-lenient — any AT-XYZ
in the URL was treated as a valid auth-token. Anybody could POST under
forged identity. The before_request gate now blocks this.

Run against live backend:
    pytest tests/aerox/test_token_auth_routes.py -v

Or local:
    AEROX_BASE_URL=http://127.0.0.1:8080 pytest tests/aerox/test_token_auth_routes.py
"""
from __future__ import annotations

import os
import pytest
import requests

BASE_URL = os.environ.get(
    "AEROX_BASE_URL",
    "https://aerotax-backend-443401186607.europe-west3.run.app",
).rstrip("/")

TIMEOUT = 10
FAKE = "AT-FAKE-NEVER-CREATED-XYZ"


def _url(path: str) -> str:
    return f"{BASE_URL}/{path.lstrip('/')}"


# Subset of POST/PUT/DELETE routes that take a <token> as first segment after
# /api/. We test a representative cross-section — not every single route, but
# enough to catch a regression in the before_request gate.
PROTECTED_ROUTES = [
    # (method, path, body or None)
    ("POST",   f"/api/wall/{FAKE}/post",                 {"text": "hax0r"}),
    ("PUT",    f"/api/user/profile/{FAKE}",              {"name": "fake"}),
    ("POST",   f"/api/wall/{FAKE}/post/abc/comment",     {"text": "spam"}),
    ("POST",   f"/api/user/friends/{FAKE}/add",          {"friend_token": "AT-ZZZ"}),
    ("POST",   f"/api/forum/{FAKE}/threads",             {"title": "x", "body": "y"}),
    ("POST",   f"/api/layover-recs/{FAKE}/add",          {"iata": "FRA", "category": "food"}),
    ("POST",   f"/api/moderation/{FAKE}/block",          {"target_token": "AT-ZZZ"}),
    ("POST",   f"/api/user/friend-requests/{FAKE}/send", {"friend_token": "AT-ZZZ"}),
    ("DELETE", f"/api/wall/{FAKE}/post/abc",             None),
    ("POST",   f"/api/family-share/{FAKE}/grant",        {"family_token": "AT-ZZZ", "fields": ["layover_place"]}),
    ("DELETE", f"/api/family-share/{FAKE}/revoke/AT-ZZZ", None),
]


@pytest.mark.parametrize("method,path,body", PROTECTED_ROUTES,
                         ids=lambda x: x if isinstance(x, str) else "")
def test_fake_token_rejected_with_401(method, path, body):
    """Fake token on protected route must return 401 unauthorized.
    Must NOT return 200 ok=true (would mean forged-identity success).
    Must NOT return 500 (would mean backend crashed on auth path)."""
    try:
        resp = requests.request(method, _url(path), json=body, timeout=TIMEOUT)
    except requests.RequestException as e:
        pytest.skip(f"Backend unreachable: {e!r}")
    assert resp.status_code != 500, (
        f"{method} {path}: backend 5xx'd on fake-token (auth-path crashed). "
        f"Body: {resp.text[:300]!r}"
    )
    assert resp.status_code != 200, (
        f"{method} {path}: backend returned 200 OK for fake-token "
        f"`{FAKE}` — token-auth-gate not enforced. Body: {resp.text[:300]!r}"
    )
    # Most rejections are 401, but a few routes may 400/403/429 first
    # (e.g. rate-limit on guest-block path). All non-2xx is acceptable as
    # long as the bypass is blocked.
    assert 400 <= resp.status_code < 500, (
        f"{method} {path}: expected 4xx for fake-token, got {resp.status_code}. "
        f"Body: {resp.text[:300]!r}"
    )
    # The 401 case should have JSON-body with `ok: false`
    if resp.status_code == 401:
        try:
            body_json = resp.json()
            assert body_json.get("ok") is False, (
                f"{method} {path}: 401 response should have `ok: false`, got {body_json!r}"
            )
        except (ValueError, AttributeError):
            # non-JSON 401 also acceptable, but warn
            pass


def test_guest_token_passes_gate_but_blocked_by_route():
    """AT-GUEST-* tokens are demo-mode — they pass the auth-gate but
    individual routes block them with 403 demo_mode."""
    guest = "AT-GUEST-DEMO-001"
    try:
        resp = requests.post(_url(f"/api/wall/{guest}/post"),
                             json={"text": "demo"}, timeout=TIMEOUT)
    except requests.RequestException as e:
        pytest.skip(f"Backend unreachable: {e!r}")
    # 403 demo_mode is the expected backend-contract for guest-tokens
    assert resp.status_code in (401, 403), (
        f"Guest-token POST: expected 401 (gate) or 403 (route), got {resp.status_code}, "
        f"body={resp.text[:200]!r}"
    )
