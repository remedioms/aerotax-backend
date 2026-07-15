import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from blueprints import legal_consent_blueprint as L


TOKEN = "AT-LEGAL-CONSENT-OWNER"
ACCOUNT_ID = "11111111-1111-4111-8111-111111111111"


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows):
        self.rows = rows
        self.filters = {}

    def select(self, _columns):
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def limit(self, _count):
        return self

    def execute(self):
        rows = [
            row for row in self.rows
            if all(str(row.get(k)) == str(v) for k, v in self.filters.items())
        ]
        return _Result(rows)


class _RPC:
    def __init__(self, parent, name, params):
        self.parent = parent
        self.name = name
        self.params = params

    def execute(self):
        self.parent.rpc_calls.append((self.name, self.params))
        return _Result(2)


class _SB:
    def __init__(self, consent_rows=None):
        self.auth_rows = [{"token": TOKEN, "account_id": ACCOUNT_ID}]
        self.consent_rows = consent_rows or []
        self.rpc_calls = []

    def table(self, name):
        if name == "auth_users":
            return _Query(self.auth_rows)
        if name == "user_legal_consents":
            return _Query(self.consent_rows)
        raise AssertionError(name)

    def rpc(self, name, params):
        return _RPC(self, name, params)


def _client():
    app = Flask(__name__)
    app.register_blueprint(L.legal_consent_bp)
    return app.test_client()


def _valid_auth():
    return TOKEN, "owner@example.test", None


def _rows_for_current_manifest():
    return [
        {
            "account_id": ACCOUNT_ID,
            "manifest_hash": L.CURRENT_LEGAL_MANIFEST["manifest_hash"],
            "document_id": doc["id"],
            "document_version": doc["version"],
            "document_hash": doc["hash"],
            "accepted_at": "2026-07-14T12:00:00Z",
        }
        for doc in L.CURRENT_LEGAL_MANIFEST["documents"]
    ]


def test_manifest_hash_is_canonical_sha256_of_existing_document_refs():
    documents = L.CURRENT_LEGAL_MANIFEST["documents"]
    canonical = json.dumps(documents, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(canonical.encode()).hexdigest() == L.CURRENT_LEGAL_MANIFEST["manifest_hash"]
    assert {doc["id"] for doc in documents} == {"terms-of-service", "privacy-policy"}


def test_header_only_auth_requires_bearer():
    client = _client()
    response = client.get("/api/legal-consent/status")
    assert response.status_code == 401
    assert response.get_json()["error"] == "authorization_required"


def test_status_requires_every_exact_document_in_current_manifest():
    sb = _SB(_rows_for_current_manifest()[:1])
    with patch.object(L, "_auth_result", side_effect=_valid_auth), patch.object(
        L, "_get_sb", return_value=(True, sb)
    ):
        response = _client().get("/api/legal-consent/status")
    assert response.status_code == 200
    assert response.get_json()["accepted"] is False
    assert response.get_json()["accepted_documents"] == []


def test_status_is_account_bound_and_accepts_complete_manifest():
    sb = _SB(_rows_for_current_manifest())
    with patch.object(L, "_auth_result", side_effect=_valid_auth), patch.object(
        L, "_get_sb", return_value=(True, sb)
    ):
        response = _client().get("/api/legal-consent/status")
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["accepted"] is True
    assert payload["current_manifest"]["hash"] == L.CURRENT_LEGAL_MANIFEST["manifest_hash"]


def test_accept_rejects_stale_or_tampered_manifest_before_database_write():
    sb = _SB()
    with patch.object(L, "_auth_result", side_effect=_valid_auth), patch.object(
        L, "_get_sb", return_value=(True, sb)
    ):
        response = _client().post(
            "/api/legal-consent/accept",
            json={"manifest_version": "old", "manifest_hash": "0" * 64},
        )
    assert response.status_code == 409
    assert response.get_json()["error"] == "legal_manifest_outdated"
    assert sb.rpc_calls == []


def test_accept_uses_server_manifest_and_never_client_supplied_documents():
    sb = _SB()
    manifest = L.CURRENT_LEGAL_MANIFEST
    with patch.object(L, "_auth_result", side_effect=_valid_auth), patch.object(
        L, "_get_sb", return_value=(True, sb)
    ):
        response = _client().post(
            "/api/legal-consent/accept",
            json={
                "manifest_version": manifest["manifest_version"],
                "manifest_hash": manifest["manifest_hash"],
                "documents": [{"id": "attacker-controlled"}],
                "locale": "de-DE",
                "app_build": "124",
            },
        )
    assert response.status_code == 200
    name, params = sb.rpc_calls[0]
    assert name == "accept_legal_manifest"
    assert params["p_user_token"] == TOKEN
    assert params["p_documents"] == manifest["documents"]
    assert all(doc["id"] != "attacker-controlled" for doc in params["p_documents"])


def test_ledger_outage_is_retryable_and_never_synthetic_acceptance():
    with patch.object(L, "_auth_result", side_effect=_valid_auth), patch.object(
        L, "_get_sb", return_value=(False, None)
    ):
        response = _client().get("/api/legal-consent/status")
    assert response.status_code == 503
    assert response.get_json() == {"ok": False, "error": "legal_ledger_unavailable"}
    assert response.headers["Retry-After"] == "10"


def test_auth_store_unavailable_is_503_not_invalid_token():
    state = SimpleNamespace(name="UNAVAILABLE")
    validator_result = SimpleNamespace(state=state, email=None)

    def app_attr(name, default=None):
        return (lambda _token: validator_result) if name == "_validate_token" else default

    with patch.object(L, "_app_attr", side_effect=app_attr):
        response = _client().get(
            "/api/legal-consent/status", headers={"Authorization": f"Bearer {TOKEN}"}
        )
    assert response.status_code == 503
    assert response.get_json()["error"] == "auth_store_unavailable"

