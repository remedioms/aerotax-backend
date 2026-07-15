"""
Wave-1 Bug-Fix Local-Smoke (in-process Flask test_client).

This file runs the test_client (no live HTTP) to verify the 5 bug fixes
work synchronously. It does NOT replace the live e2e suite — it's a fast
canary that catches obvious regressions before deploy.

Run:
    AEROTAX_ALLOW_BOOT_WITHOUT_KEY=1 pytest tests/aerox/test_local_bugfix_smoke.py -v
"""
from __future__ import annotations

import json
import os
import time
import uuid

import pytest

os.environ.setdefault("AEROTAX_ALLOW_BOOT_WITHOUT_KEY", "1")


@pytest.fixture(scope="module")
def client():
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    import app as _app
    return _app.app.test_client()


@pytest.fixture
def user(client):
    """Throwaway user via signup; cleanup via delete-account in teardown."""
    email = f"localtest+{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}@aerox.test"
    pw = "Test12345!"
    r = client.post("/api/auth/signup", json={"email": email, "password": pw})
    assert r.status_code == 200, f"signup status {r.status_code}: {r.get_data(as_text=True)[:200]}"
    body = json.loads(r.get_data(as_text=True))
    assert body.get("ok") is True
    token = body["token"]
    yield {"email": email, "password": pw, "token": token}
    # cleanup
    try:
        client.post("/api/auth/delete-account",
                    json={"email": email, "password": pw, "token": token})
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────
# BUG-001: Wall-feed merge SB + Disk
# ─────────────────────────────────────────────────────────────────

def test_bug001_wall_post_visible_in_feed(client, user):
    """POST /api/wall/<token>/post → GET /api/wall/<token>/feed should contain the post."""
    token = user["token"]
    r = client.post(f"/api/wall/{token}/post", json={"text": "BUG001 smoke"})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = json.loads(r.get_data(as_text=True))
    assert body.get("ok") is True
    post_id = body["post"]["id"]

    # GET feed
    r = client.get(f"/api/wall/{token}/feed?limit=20")
    assert r.status_code == 200
    feed = json.loads(r.get_data(as_text=True))
    feed_ids = [p.get("id") for p in (feed.get("posts") or [])]
    assert post_id in feed_ids, f"Fresh post {post_id!r} not in feed: {feed_ids!r}"


# ─────────────────────────────────────────────────────────────────
# BUG-002: Family-Watch endpoints exist
# ─────────────────────────────────────────────────────────────────

def test_bug002_family_share_grant_then_list(client, user):
    token = user["token"]
    fam_token = "AT-FAM-" + uuid.uuid4().hex[:8].upper()
    r = client.post(f"/api/family-share/{token}/grant",
                    json={"family_token": fam_token, "relation": "partner",
                          "fields": ["layover_place", "current_city"]})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = json.loads(r.get_data(as_text=True))
    assert body.get("ok") is True
    assert "layover_place" in body.get("fields", [])

    r = client.get(f"/api/family-share/{token}/list")
    assert r.status_code == 200
    body = json.loads(r.get_data(as_text=True))
    grants = body.get("grants") or []
    assert any(g.get("family_token") == fam_token for g in grants), (
        f"Grant for {fam_token!r} not in list: {grants!r}"
    )


def test_bug002_family_watch_feed_returns_watched_envelope(client, user):
    # The endpoint must exist + return the right envelope shape.
    # FIX 2026-07-01: Seit 7dfead1 401t das Auth-Gate UNBEKANNTE AT-FAM-Tokens
    # (korrektes Prod-Verhalten) — der Test münzt deshalb einen ECHTEN scoped
    # Family-Token über den Pair-Code-Flow statt eines Random-Fakes.
    token = user["token"]
    r = client.post(f"/api/family/pair-code/{token}/create")
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    code = json.loads(r.get_data(as_text=True))["code"]
    r = client.post("/api/family/pair-code/redeem",
                    json={"code": code, "family_name": "Smoke Fam"})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    fam_token = json.loads(r.get_data(as_text=True))["family_token"]
    assert fam_token.startswith("AT-FAM-"), fam_token

    r = client.get(f"/api/family-watch/{fam_token}/feed")
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    body = json.loads(r.get_data(as_text=True))
    assert "watched" in body, f"Envelope key 'watched' missing: {body!r}"
    assert isinstance(body["watched"], list)


def test_bug002_family_share_revoke(client, user):
    token = user["token"]
    fam_token = "AT-FAM-" + uuid.uuid4().hex[:8].upper()
    # grant
    client.post(f"/api/family-share/{token}/grant",
                json={"family_token": fam_token, "fields": ["layover_place"]})
    # revoke
    r = client.delete(f"/api/family-share/{token}/revoke/{fam_token}")
    assert r.status_code == 200, r.get_data(as_text=True)
    body = json.loads(r.get_data(as_text=True))
    assert body.get("ok") is True


# ─────────────────────────────────────────────────────────────────
# BUG-003: Profile-PUT account_type whitelist
# ─────────────────────────────────────────────────────────────────

def _bearer(token):
    """iOS-Client-Kontrakt: owner-scoped Requests senden IMMER den eigenen
    Bearer. PUT /api/user/profile ist seit dem IDOR-Fix hart daran gebunden
    (token_binding_required, 401 ohne Header); der GET liefert nur MIT Bearer
    das Vollprofil (sonst Public-Whitelist-Projektion)."""
    return {"Authorization": f"Bearer {token}"}


def test_bug003_profile_put_persists_account_type(client, user):
    token = user["token"]
    r = client.put(f"/api/user/profile/{token}", json={
        "name": "Fam Watcher", "homebase": "FRA",
        "position": "CC", "airline": "DLH",
        "account_type": "family",
        "family_share_defaults": ["layover_place", "current_city"],
    }, headers=_bearer(token))
    assert r.status_code == 200, r.get_data(as_text=True)
    body = json.loads(r.get_data(as_text=True))
    assert body.get("ok") is True

    r = client.get(f"/api/user/profile/{token}", headers=_bearer(token))
    body = json.loads(r.get_data(as_text=True))
    prof = body.get("profile") or {}
    assert prof.get("account_type") == "family", f"account_type not persisted: {prof!r}"
    assert "layover_place" in (prof.get("family_share_defaults") or [])


def test_bug003_invalid_account_type_rejected(client, user):
    token = user["token"]
    r = client.put(f"/api/user/profile/{token}",
                   json={"account_type": "admin"},  # invalid
                   headers=_bearer(token))
    assert r.status_code == 400, r.get_data(as_text=True)
    body = json.loads(r.get_data(as_text=True))
    assert body.get("error") == "invalid_account_type"


# ─────────────────────────────────────────────────────────────────
# BUG-004: Token-Auth gate
# ─────────────────────────────────────────────────────────────────

def test_bug004_fake_token_wall_post_rejected(client):
    r = client.post("/api/wall/AT-FAKE-XYZ-NEVER/post", json={"text": "hax"})
    assert r.status_code == 401, f"Fake token POST should be 401, got {r.status_code}: {r.get_data(as_text=True)[:200]}"
    body = json.loads(r.get_data(as_text=True))
    assert body.get("ok") is False
    assert body.get("error") == "unauthorized"


def test_bug004_fake_token_profile_put_rejected(client):
    r = client.put("/api/user/profile/AT-FAKE-NOPE", json={"name": "x"})
    assert r.status_code == 401, r.get_data(as_text=True)


def test_bug004_real_token_passes(client, user):
    token = user["token"]
    r = client.post(f"/api/wall/{token}/post", json={"text": "real-user-post"})
    assert r.status_code == 200, r.get_data(as_text=True)


def test_bug004_get_with_fake_token_not_blocked(client):
    """GET methods are NOT auth-gated (intentional — feed-reads, friend-lookups
    happen before friendship is established)."""
    r = client.get("/api/wall/AT-FAKE-XYZ-GET/feed?limit=5")
    # may 200 (empty feed) or 4xx but NOT 401 from the auth-gate
    assert r.status_code != 401, "GET should not be auth-gated"


# ─────────────────────────────────────────────────────────────────
# BUG-005: DSGVO Export
# ─────────────────────────────────────────────────────────────────

def test_bug005_export_with_valid_token(client, user):
    token = user["token"]
    # Create a wall post first
    r = client.post(f"/api/wall/{token}/post", json={"text": "BUG005 export-test"})
    assert r.status_code == 200
    post_id = json.loads(r.get_data(as_text=True))["post"]["id"]

    r = client.get("/api/auth/export-data",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    cd = r.headers.get("Content-Disposition", "")
    assert "attachment" in cd.lower()
    assert "aerox-export-" in cd
    export = json.loads(r.get_data(as_text=True))
    # Credentials sind im Export maskiert (Owner-Fund 2026-07-15): der eigene
    # Auth-Token darf NICHT mehr im herunterladbaren JSON stehen.
    assert export.get("meta", {}).get("token") == "[redacted]"
    assert export.get("meta", {}).get("email") == user["email"]
    # Post should be in the export (content-IDs bleiben, nur Tokens maskiert)
    post_ids = [p.get("id") for p in (export.get("wall_posts") or [])]
    assert post_id in post_ids, f"Created post {post_id!r} not in export: {post_ids!r}"
    # KEIN Auth-Token (eigener oder fremder) darf irgendwo im Export auftauchen.
    def _no_token_leak(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k.lower().endswith("token") and isinstance(v, str) and v:
                    assert v == "[redacted]", f"token leak at {k}: {v!r}"
                _no_token_leak(v)
        elif isinstance(obj, list):
            for x in obj:
                _no_token_leak(x)
    _no_token_leak(export)


def test_bug005_export_missing_auth_returns_401(client):
    r = client.get("/api/auth/export-data")
    assert r.status_code == 401


def test_bug005_export_fake_token_returns_401(client):
    r = client.get("/api/auth/export-data",
                   headers={"Authorization": "Bearer AT-NEVER-EXISTED"})
    assert r.status_code == 401
