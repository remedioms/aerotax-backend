"""Local-only sanity check for the BUG-004 gate extension (GET PII routes).

Runs against the imported Flask app via test_client — no live backend
required. Validates that:
  - GET requests on PII-routes with fake AT-tokens → 401
  - GET requests on public-by-design routes → NOT 401
  - POST/PUT/DELETE gate is unchanged

This is the local complement to test_token_auth_routes.py which targets the
live Cloud-Run backend.
"""
from __future__ import annotations

import os
import sys
import pytest

os.environ.setdefault("AEROTAX_ALLOW_BOOT_WITHOUT_KEY", "1")

_THIS = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_THIS))
sys.path.insert(0, _REPO)

import app as backend  # noqa: E402

FAKE = "AT-FAKE-NEVER-CREATED-XYZ"


@pytest.fixture(scope="module")
def client():
    return backend.app.test_client()


PII_GET_ROUTES = [
    f"/api/user/history/{FAKE}",
    f"/api/user/voice-note/{FAKE}",
    f"/api/user/voice-note/{FAKE}/2026-06-02",
    f"/api/user/flight-notes/{FAKE}",
    f"/api/user/flight-notes/{FAKE}/2026-06-02",
    f"/api/user/flight-ops/{FAKE}",
    f"/api/user/briefing/{FAKE}",
    f"/api/user/roster-changes/{FAKE}",
    f"/api/user/logbook-html/{FAKE}",
    f"/api/user/ical/{FAKE}",
    f"/api/user/stats/{FAKE}",
    f"/api/user/trip-stats/{FAKE}",
    f"/api/user/marker-mapping/{FAKE}",
    f"/api/user/subscription/{FAKE}",
    f"/api/user/crew-aircraft/{FAKE}",
    f"/api/user/friend-requests/{FAKE}",
    f"/api/user/friend-roster/{FAKE}/AT-ZZZ",
    f"/api/user/friend-compare/{FAKE}/AT-ZZZ",
    f"/api/user/friends-homebases/{FAKE}",
    f"/api/user/friends-today/{FAKE}",
    f"/api/user/friends/{FAKE}/overlap",
    f"/api/crew-chat/{FAKE}/inbox",
    f"/api/crew-chat/{FAKE}/dm/AT-ZZZ",
    f"/api/crew-chat/{FAKE}/channel/CH-1",
    f"/api/moderation/{FAKE}/blocks",
    f"/api/moderation/{FAKE}/mutes",
    f"/api/lufthansa/status/{FAKE}",
]


@pytest.mark.parametrize("path", PII_GET_ROUTES, ids=lambda p: p)
def test_pii_get_route_returns_401(client, path):
    """Gate muss bei fake-Token greifen — auch fuer GET-Routen mit PII."""
    resp = client.get(path)
    assert resp.status_code == 401, (
        f"GET {path}: expected 401 from BUG-004 gate, got {resp.status_code} "
        f"body={resp.get_data(as_text=True)[:200]!r}"
    )
    j = resp.get_json(silent=True) or {}
    assert j.get("ok") is False, f"401 must have ok=false body, got {j!r}"


PUBLIC_GET_ROUTES = [
    f"/api/user/profile/{FAKE}",
    f"/api/wall/{FAKE}/feed",
    f"/api/forum/{FAKE}/threads",
    f"/api/layover-recs/discover/{FAKE}",
]


@pytest.mark.parametrize("path", PUBLIC_GET_ROUTES, ids=lambda p: p)
def test_public_get_route_not_401(client, path):
    """Public-by-design GETs duerfen NICHT vom Gate 401'd werden."""
    resp = client.get(path)
    assert resp.status_code != 401, (
        f"GET {path}: public-by-design but got 401 from BUG-004 gate. "
        f"Whitelist ist zu eng. body={resp.get_data(as_text=True)[:200]!r}"
    )


def test_post_gate_still_works(client):
    """Sanity: POST-Pfad funktioniert weiter unveraendert (Regression-Guard)."""
    resp = client.post(f"/api/wall/{FAKE}/post", json={"text": "x"})
    assert resp.status_code == 401, (
        f"POST gate broken: got {resp.status_code} "
        f"body={resp.get_data(as_text=True)[:200]!r}"
    )


def test_whitelisted_get_route_not_gated(client):
    """Tax-Job-Tokens (z.B. /api/session/) sind whitelisted und duerfen
    NIE durch das Gate fallen, auch wenn ihr Token-Prefix AT-... waere."""
    # /api/session/* ist whitelisted via _BUG004_WHITELIST_PREFIXES
    resp = client.get(f"/api/session/{FAKE}")
    # Wir erwarten 404 (kein Tax-Job mit dem Token) — nicht 401.
    assert resp.status_code != 401, (
        f"Whitelist-Route /api/session/<token> got 401 — Whitelist gebrochen."
    )
