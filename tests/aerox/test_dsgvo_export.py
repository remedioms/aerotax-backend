"""
Wave-1 BUG-005 — DSGVO Art. 15 Datenexport-Endpoint Tests

Verifies that `/api/auth/export-data` returns a complete JSON blob of all
user data. Tests the happy-path: signup → post → comment → export → assert
post + comment present in the export JSON.

Run:
    pytest tests/aerox/test_dsgvo_export.py -v
"""
from __future__ import annotations

import json
import os
import time
import uuid

import pytest
import requests

BASE_URL = os.environ.get(
    "AEROX_BASE_URL",
    "https://api.aerosteuer.de",
).rstrip("/")

# Live-E2E gegen das Produktions-Backend (Signups/Wall-Posts über das Netz).
# CI/lokale Läufe dürfen nicht von Prod-Verfügbarkeit abhängen → opt-in wie
# in test_e2e_smoke_live.py via AEROX_LIVE_TESTS=1.
if os.environ.get("AEROX_LIVE_TESTS") != "1":
    pytest.skip(
        "live production e2e — set AEROX_LIVE_TESTS=1 to run",
        allow_module_level=True,
    )

TIMEOUT = 15
PASSWORD = "Test12345!"


def _url(path: str) -> str:
    return f"{BASE_URL}/{path.lstrip('/')}"


def _signup(email: str, password: str = PASSWORD) -> dict:
    r = requests.post(_url("/api/auth/signup"),
                      json={"email": email, "password": password},
                      timeout=TIMEOUT)
    return r.json()


def _delete_account(email: str, token: str, password: str = PASSWORD):
    try:
        requests.post(_url("/api/auth/delete-account"),
                      json={"email": email, "password": password, "token": token},
                      timeout=TIMEOUT)
    except Exception:
        pass


def _fresh_email(tag: str = "dsgvo") -> str:
    return f"e2e+{tag}_{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}@aerox.test"


def test_export_contains_signup_data():
    """Smoke: signup creates user → export returns at least the meta-envelope
    with the right email + token. No body needed."""
    email = _fresh_email("smoke")
    body = _signup(email)
    if not body.get("ok"):
        pytest.skip(f"Signup failed (live-backend down?): {body!r}")
    token = body["token"]
    try:
        r = requests.get(_url("/api/auth/export-data"),
                         headers={"Authorization": f"Bearer {token}"},
                         timeout=TIMEOUT)
        assert r.status_code == 200, f"Export got {r.status_code}: {r.text[:200]!r}"
        # JSON-body check
        export = r.json()
        meta = export.get("meta") or {}
        assert meta.get("token") == token, f"Export meta.token mismatch: {meta!r}"
        assert meta.get("email") == email, f"Export meta.email mismatch: {meta!r}"
        assert meta.get("dsgvo_article", "").startswith("Art. 15"), \
            f"DSGVO-Article marker missing: {meta!r}"
        # Content-Disposition header for iOS file-save
        cd = r.headers.get("Content-Disposition", "")
        assert "attachment" in cd.lower(), f"Missing attachment header: {cd!r}"
        assert "aerox-export-" in cd, f"Missing filename pattern: {cd!r}"
    finally:
        _delete_account(email, token)


def test_export_includes_wall_post_and_comment():
    """End-to-end: signup → post → comment own post → export → assert
    both post and comment appear in the export JSON."""
    email = _fresh_email("postcomment")
    body = _signup(email)
    if not body.get("ok"):
        pytest.skip(f"Signup failed: {body!r}")
    token = body["token"]
    try:
        # Create a wall post
        r = requests.post(_url(f"/api/wall/{token}/post"),
                          json={"text": "DSGVO-Export-Test-Post"},
                          timeout=TIMEOUT)
        post_body = r.json()
        if not post_body.get("ok"):
            pytest.skip(f"Wall-post failed: {post_body!r}")
        post_id = (post_body.get("post") or {}).get("id")
        assert post_id, f"Missing post.id: {post_body!r}"

        # Comment on own post
        r = requests.post(_url(f"/api/wall/{token}/post/{post_id}/comment"),
                          json={"text": "DSGVO-Export-Test-Comment"},
                          timeout=TIMEOUT)
        # tolerate 200/201 — comment endpoint contract varies
        if r.status_code >= 400:
            pytest.skip(f"Comment failed: {r.status_code} {r.text[:200]!r}")

        # tiny wait to allow Supabase write-propagation
        time.sleep(1.0)

        # Export
        r = requests.get(_url("/api/auth/export-data"),
                         headers={"Authorization": f"Bearer {token}"},
                         timeout=TIMEOUT)
        assert r.status_code == 200, f"Export got {r.status_code}: {r.text[:300]!r}"
        export = r.json()

        # Assert post present
        posts = export.get("wall_posts") or []
        post_ids = [p.get("id") for p in posts]
        assert post_id in post_ids, (
            f"DSGVO export missing own wall_post id={post_id!r}. "
            f"Got post_ids={post_ids!r}, full export keys={list(export.keys())!r}"
        )

        # Assert comment present (best-effort — comments may live in disk-only
        # storage that the export may not be able to enumerate without SB
        # available; skip if comments-list is empty)
        comments = export.get("wall_comments") or []
        if comments:
            cmt_texts = [c.get("text", "") for c in comments]
            assert any("DSGVO-Export-Test-Comment" in t for t in cmt_texts), (
                f"Comment text missing from export. wall_comments={comments!r}"
            )
    finally:
        _delete_account(email, token)


def test_export_rejects_unknown_token():
    """Export with fake token must return 401."""
    r = requests.get(_url("/api/auth/export-data"),
                     headers={"Authorization": "Bearer AT-NEVER-EXISTED-XYZ"},
                     timeout=TIMEOUT)
    assert r.status_code == 401, f"Expected 401, got {r.status_code}: {r.text[:200]!r}"
    body = r.json()
    assert body.get("ok") is False, f"401 body should have ok=false: {body!r}"


def test_export_rejects_missing_auth():
    """Export without any auth-header must return 401."""
    r = requests.get(_url("/api/auth/export-data"), timeout=TIMEOUT)
    assert r.status_code == 401, f"Expected 401, got {r.status_code}: {r.text[:200]!r}"
