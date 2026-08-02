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
        self.in_filters = {}

    def select(self, _columns):
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def in_(self, key, values):
        self.in_filters[key] = [str(v) for v in values]
        return self

    def limit(self, _count):
        return self

    def execute(self):
        rows = [
            row for row in self.rows
            if all(str(row.get(k)) == str(v) for k, v in self.filters.items())
            and all(
                str(row.get(k)) in values for k, values in self.in_filters.items()
            )
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



# ── Zurueckgezogene August-Aenderung (Owner-Entscheid 2026-08-02) ───────────
#
# Die AGB-Aenderung wurde zurueckgenommen; niemand soll erneut zustimmen.
# Die TestFlight-Builds 285/286 tragen das August-Manifest aber fest im
# Binary. Diese Tests nageln fest, dass beide Generationen gleichzeitig
# funktionieren — ohne dass irgendwo eine Zustimmung erfunden wird.

UA_AUGUST_BUILD = "AeroX/286 CFNetwork/3860.600.12 Darwin/25.5.0"
UA_JUNE_BUILD = "AeroX/271 CFNetwork/3860.600.12 Darwin/25.5.0"


def _rows_for_withdrawn_manifest():
    manifest = L._WITHDRAWN_AUGUST_2026_MANIFEST
    return [
        {
            "account_id": ACCOUNT_ID,
            "manifest_hash": manifest["manifest_hash"],
            "document_id": doc["id"],
            "document_version": doc["version"],
            "document_hash": doc["hash"],
            "accepted_at": "2026-08-02T12:12:38Z",
        }
        for doc in manifest["documents"]
    ]


def test_withdrawn_manifest_hash_is_canonical_sha256_too():
    manifest = L._WITHDRAWN_AUGUST_2026_MANIFEST
    canonical = json.dumps(manifest["documents"], sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(canonical.encode()).hexdigest() == manifest["manifest_hash"]
    # Die Datenschutzerklaerung ist in beiden Manifesten bitgleich dieselbe —
    # nur die AGB trug den zurueckgezogenen Zusatz.
    privacy = [d for d in manifest["documents"] if d["id"] == "privacy-policy"][0]
    canonical_privacy = [
        d for d in L.CURRENT_LEGAL_MANIFEST["documents"] if d["id"] == "privacy-policy"
    ][0]
    assert privacy == canonical_privacy


def test_august_build_with_june_consent_sees_no_wall():
    """Der Kern des Owner-Entscheids: Build 286, Zustimmung von Juni im
    Ledger, KEINE neue Zeile — und trotzdem keine Rechts-Wand."""
    sb = _SB(_rows_for_current_manifest())
    with patch.object(L, "_auth_result", side_effect=_valid_auth), patch.object(
        L, "_get_sb", return_value=(True, sb)
    ):
        response = _client().get(
            "/api/legal-consent/status", headers={"User-Agent": UA_AUGUST_BUILD}
        )
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["accepted"] is True
    # Der Client vergleicht Version UND Hash gegen seine eigenen Konstanten.
    manifest = L._WITHDRAWN_AUGUST_2026_MANIFEST
    assert payload["current_manifest"]["version"] == manifest["manifest_version"]
    assert payload["current_manifest"]["hash"] == manifest["manifest_hash"]
    # Es wurde NICHTS geschrieben — Zustimmungen werden nicht fabriziert.
    assert sb.rpc_calls == []


def test_june_build_is_untouched_by_the_alias():
    sb = _SB(_rows_for_current_manifest())
    with patch.object(L, "_auth_result", side_effect=_valid_auth), patch.object(
        L, "_get_sb", return_value=(True, sb)
    ):
        response = _client().get(
            "/api/legal-consent/status", headers={"User-Agent": UA_JUNE_BUILD}
        )
    payload = response.get_json()
    assert payload["accepted"] is True
    assert payload["current_manifest"]["hash"] == L.CURRENT_LEGAL_MANIFEST["manifest_hash"]


def test_build_after_withdrawn_range_gets_canonical_manifest():
    """Build 287+ enthält nach dem Rollback wieder das Juni-Manifest.

    Eine offene Untergrenze (>= 285) lieferte ihm zuvor fälschlich August und
    löste dadurch bei jedem erneuten Status-Check die Consent-Wand aus.
    """
    sb = _SB(_rows_for_current_manifest())
    with patch.object(L, "_auth_result", side_effect=_valid_auth), patch.object(
        L, "_get_sb", return_value=(True, sb)
    ):
        response = _client().get(
            "/api/legal-consent/status",
            headers={"User-Agent": "AeroX/287 CFNetwork/1498.700.2 Darwin/23.6.0"},
        )
    payload = response.get_json()
    assert payload["accepted"] is True
    assert payload["current_manifest"]["version"] == "2026-06"
    assert payload["current_manifest"]["hash"] == L.CURRENT_LEGAL_MANIFEST["manifest_hash"]


def test_june_build_accepts_alias_only_ledger_as_valid():
    """Wer auf 285/286 getippt hat, hat eine August-Zeile im Ledger. Fuer
    einen Alt-Build darf das nicht wieder zur Wand werden."""
    sb = _SB(_rows_for_withdrawn_manifest())
    with patch.object(L, "_auth_result", side_effect=_valid_auth), patch.object(
        L, "_get_sb", return_value=(True, sb)
    ):
        response = _client().get(
            "/api/legal-consent/status", headers={"User-Agent": UA_JUNE_BUILD}
        )
    payload = response.get_json()
    assert payload["accepted"] is True
    assert payload["current_manifest"]["hash"] == L.CURRENT_LEGAL_MANIFEST["manifest_hash"]


def test_accept_of_withdrawn_manifest_is_booked_not_409():
    sb = _SB()
    manifest = L._WITHDRAWN_AUGUST_2026_MANIFEST
    with patch.object(L, "_auth_result", side_effect=_valid_auth), patch.object(
        L, "_get_sb", return_value=(True, sb)
    ):
        response = _client().post(
            "/api/legal-consent/accept",
            headers={"User-Agent": UA_AUGUST_BUILD},
            json={
                "manifest_version": manifest["manifest_version"],
                "manifest_hash": manifest["manifest_hash"],
                "locale": "de-DE",
                "app_build": "286",
            },
        )
    assert response.status_code == 200
    assert response.get_json()["accepted"] is True
    name, params = sb.rpc_calls[0]
    assert name == "accept_legal_manifest"
    # Verbucht wird, was der Nutzer wirklich gesehen hat — nicht das kanonische.
    assert params["p_manifest_version"] == manifest["manifest_version"]
    assert params["p_manifest_hash"] == manifest["manifest_hash"]
    assert params["p_documents"] == manifest["documents"]
    # Alias-Vermerk fuer die Revision.
    assert params["p_app_build"] == "286 alias:2026-06"
    assert len(params["p_app_build"]) <= 64


def test_unknown_manifest_still_409_for_august_build():
    sb = _SB()
    with patch.object(L, "_auth_result", side_effect=_valid_auth), patch.object(
        L, "_get_sb", return_value=(True, sb)
    ):
        response = _client().post(
            "/api/legal-consent/accept",
            headers={"User-Agent": UA_AUGUST_BUILD},
            json={"manifest_version": "2027-01", "manifest_hash": "0" * 64},
        )
    assert response.status_code == 409
    assert sb.rpc_calls == []
    # Auch die Absage spiegelt dem Client sein eigenes Manifest zurueck.
    assert (
        response.get_json()["current_manifest"]["hash"]
        == L._WITHDRAWN_AUGUST_2026_MANIFEST["manifest_hash"]
    )


def test_unknown_user_agent_falls_back_to_canonical_manifest():
    sb = _SB(_rows_for_current_manifest())
    with patch.object(L, "_auth_result", side_effect=_valid_auth), patch.object(
        L, "_get_sb", return_value=(True, sb)
    ):
        response = _client().get(
            "/api/legal-consent/status", headers={"User-Agent": "curl/8.7.1"}
        )
    assert (
        response.get_json()["current_manifest"]["hash"]
        == L.CURRENT_LEGAL_MANIFEST["manifest_hash"]
    )
