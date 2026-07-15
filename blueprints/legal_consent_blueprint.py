"""Versioned, account-bound legal-consent API.

This module deliberately uses header-only authentication.  The legacy global
auth gate in app.py keys off tokens embedded in URLs, so these routes validate
the Authorization Bearer themselves.  Consent persistence is fail-closed: an
unavailable ledger returns 503, never a synthetic acceptance.

Wiring (kept out of app.py while another security change edits that file):

    ('blueprints.legal_consent_blueprint', 'legal_consent_bp'),

Add that tuple to app.py's blueprint registration list after applying
20260714_legal_consent_ledger.sql.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, jsonify, request


legal_consent_bp = Blueprint("legal_consent", __name__)

_MANIFEST_PATH = Path(__file__).resolve().parent.parent / "data" / "legal_consent_manifest.json"


def _load_manifest() -> dict[str, Any]:
    with _MANIFEST_PATH.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    documents = manifest.get("documents")
    if not isinstance(documents, list) or not documents:
        raise RuntimeError("legal consent manifest has no documents")
    canonical = json.dumps(documents, sort_keys=True, separators=(",", ":"))
    calculated = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(str(manifest.get("manifest_hash") or ""), calculated):
        raise RuntimeError("legal consent manifest hash mismatch")
    return manifest


CURRENT_LEGAL_MANIFEST = _load_manifest()


def _app_attr(name: str, default=None):
    try:
        import app as app_module

        return getattr(app_module, name, default)
    except Exception:
        return default


def _log():
    try:
        return current_app.logger
    except RuntimeError:
        return logging.getLogger("legal_consent")


def _bearer_token() -> str | None:
    value = request.headers.get("Authorization", "") or ""
    parts = value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token if token else None


def _auth_result():
    """Return (token, email, response).

    Uses app.py's tri-state validator so a cold Supabase outage is a retryable
    503, not a false acceptance or a false logout.
    """
    token = _bearer_token()
    if token is None:
        return None, None, (jsonify({"ok": False, "error": "authorization_required"}), 401)

    validator = _app_attr("_validate_token")
    if not callable(validator):
        return None, None, (
            jsonify({"ok": False, "error": "auth_store_unavailable"}),
            503,
            {"Retry-After": "5"},
        )
    try:
        result = validator(token)
    except Exception as exc:
        _log().warning("[legal-consent] auth validator unavailable: %s", type(exc).__name__)
        return None, None, (
            jsonify({"ok": False, "error": "auth_store_unavailable"}),
            503,
            {"Retry-After": "5"},
        )

    state_name = str(getattr(getattr(result, "state", None), "name", "")).upper()
    if state_name == "UNAVAILABLE":
        return None, None, (
            jsonify({"ok": False, "error": "auth_store_unavailable"}),
            503,
            {"Retry-After": "5"},
        )
    if state_name != "VALID":
        return None, None, (jsonify({"ok": False, "error": "invalid_token"}), 401)
    return token, getattr(result, "email", None), None


def _get_sb():
    return bool(_app_attr("SB_AVAILABLE", False)), _app_attr("sb")


def _ledger_unavailable():
    return (
        jsonify({"ok": False, "error": "legal_ledger_unavailable"}),
        503,
        {"Retry-After": "10"},
    )


def _account_id_for_token(sb, token: str) -> str | None:
    result = (
        sb.table("auth_users")
        .select("account_id")
        .eq("token", token)
        .limit(1)
        .execute()
    )
    rows = getattr(result, "data", None) or []
    if not rows:
        return None
    account_id = rows[0].get("account_id")
    return str(account_id) if account_id else None


def _public_manifest() -> dict[str, Any]:
    return {
        "version": CURRENT_LEGAL_MANIFEST["manifest_version"],
        "hash": CURRENT_LEGAL_MANIFEST["manifest_hash"],
        "documents": CURRENT_LEGAL_MANIFEST["documents"],
    }


def _accepted_document_ids(rows: list[dict[str, Any]]) -> list[str]:
    expected = {
        (doc["id"], doc["version"], doc["hash"])
        for doc in CURRENT_LEGAL_MANIFEST["documents"]
    }
    present = {
        (row.get("document_id"), row.get("document_version"), row.get("document_hash"))
        for row in rows
    }
    if not expected.issubset(present):
        return []
    return [doc["id"] for doc in CURRENT_LEGAL_MANIFEST["documents"]]


@legal_consent_bp.get("/api/legal-consent/status")
def legal_consent_status():
    token, _email, auth_error = _auth_result()
    if auth_error is not None:
        return auth_error
    available, sb = _get_sb()
    if not available or sb is None:
        return _ledger_unavailable()
    try:
        account_id = _account_id_for_token(sb, token)
        if account_id is None:
            return jsonify({"ok": False, "error": "account_not_found"}), 401
        response = (
            sb.table("user_legal_consents")
            .select("document_id,document_version,document_hash,accepted_at")
            .eq("account_id", account_id)
            .eq("manifest_hash", CURRENT_LEGAL_MANIFEST["manifest_hash"])
            .execute()
        )
        rows = getattr(response, "data", None) or []
        accepted_ids = _accepted_document_ids(rows)
        expected_count = len(CURRENT_LEGAL_MANIFEST["documents"])
        return jsonify(
            {
                "ok": True,
                "accepted": len(accepted_ids) == expected_count,
                "accepted_documents": accepted_ids,
                "current_manifest": _public_manifest(),
            }
        )
    except Exception as exc:
        _log().warning("[legal-consent] status unavailable: %s", type(exc).__name__)
        return _ledger_unavailable()


@legal_consent_bp.post("/api/legal-consent/accept")
def legal_consent_accept():
    token, _email, auth_error = _auth_result()
    if auth_error is not None:
        return auth_error
    body = request.get_json(silent=True) or {}
    supplied_version = str(body.get("manifest_version") or "")
    supplied_hash = str(body.get("manifest_hash") or "").lower()
    if not (
        hmac.compare_digest(supplied_version, CURRENT_LEGAL_MANIFEST["manifest_version"])
        and hmac.compare_digest(supplied_hash, CURRENT_LEGAL_MANIFEST["manifest_hash"])
    ):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "legal_manifest_outdated",
                    "current_manifest": _public_manifest(),
                }
            ),
            409,
        )

    locale = str(body.get("locale") or "")[:35] or None
    app_build = str(body.get("app_build") or "")[:64] or None
    available, sb = _get_sb()
    if not available or sb is None:
        return _ledger_unavailable()
    try:
        response = sb.rpc(
            "accept_legal_manifest",
            {
                "p_user_token": token,
                "p_manifest_version": CURRENT_LEGAL_MANIFEST["manifest_version"],
                "p_manifest_hash": CURRENT_LEGAL_MANIFEST["manifest_hash"],
                "p_documents": CURRENT_LEGAL_MANIFEST["documents"],
                "p_locale": locale,
                "p_app_build": app_build,
            },
        ).execute()
        return jsonify(
            {
                "ok": True,
                "accepted": True,
                "inserted": getattr(response, "data", 0) or 0,
                "current_manifest": _public_manifest(),
            }
        )
    except Exception as exc:
        _log().warning("[legal-consent] accept unavailable: %s", type(exc).__name__)
        return _ledger_unavailable()

