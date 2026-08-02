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
import re
from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, jsonify, request


legal_consent_bp = Blueprint("legal_consent", __name__)

_MANIFEST_PATH = Path(__file__).resolve().parent.parent / "data" / "legal_consent_manifest.json"


def _verify_manifest(manifest: dict[str, Any], label: str) -> dict[str, Any]:
    documents = manifest.get("documents")
    if not isinstance(documents, list) or not documents:
        raise RuntimeError(f"{label} has no documents")
    canonical = json.dumps(documents, sort_keys=True, separators=(",", ":"))
    calculated = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(str(manifest.get("manifest_hash") or ""), calculated):
        raise RuntimeError(f"{label} hash mismatch")
    return manifest


def _load_manifest() -> dict[str, Any]:
    with _MANIFEST_PATH.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    return _verify_manifest(manifest, "legal consent manifest")


CURRENT_LEGAL_MANIFEST = _load_manifest()


# ── Zurueckgezogene Aenderung: August-2026-Manifest ─────────────────────────
#
# OWNER-ENTSCHEID 2026-08-02: Die AGB-Aenderung vom 02.08. (zusaetzlicher
# Abschnitt „Eigene Lernkarten & freiwillige Einreichung", Fusszeile „Stand:
# August 2026") wird ZURUECKGEZOGEN. Niemand soll deswegen erneut zustimmen
# muessen — die Juni-Zustimmung gilt unveraendert weiter. Kanonisch ist
# deshalb wieder `data/legal_consent_manifest.json` mit 2026-06.
#
# Das Problem, das diese Tabelle loest: die TestFlight-Builds 285/286 tragen
# das August-Manifest FEST im Binary und vergleichen die /status-Antwort
# exakt auf Version UND Hash (`LegalConsentCoordinator.resolve` in
# AeroTax/AeroTax/Storage/LegalConsentStore.swift). Ein blosser Rueckbau auf
# Juni wuerde genau diese Nutzer dauerhaft vor die Rechts-Wand stellen,
# obwohl ihre Zustimmung im Ledger liegt.
#
# Deshalb gilt das August-Manifest als AEQUIVALENT zum Juni-Manifest. Das ist
# keine Fiktion: die zurueckgezogene Aenderung war rein additiv, der
# Juni-Text gilt in vollem Umfang unveraendert weiter, und die
# Datenschutzerklaerung ist in beiden Manifesten bitgleich dieselbe.
#
# Es wird dabei NIE eine Zustimmung erfunden — es wird nur eine echte,
# vorhandene Zustimmung als weiterhin gueltig ANERKANNT.
_WITHDRAWN_AUGUST_2026_MANIFEST: dict[str, Any] = {
    "manifest_version": "2026-08",
    "manifest_hash": "e6440848caf20faacdeaae062dc44e74053a5896a6dfa3e454cdb3e45ebe1f3e",
    "source_provenance": (
        "Zurueckgezogene AGB-Aenderung vom 2026-08-02 (Owner-Entscheid: keine "
        "Rezustimmung). Bleibt hier stehen, weil die Builds 285/286 sie im "
        "Binary tragen."
    ),
    "documents": [
        {
            "id": "terms-of-service",
            "version": "2026-08",
            "hash": "d09ac20a989abbb47da6a65f37ba5b0dec4a5fefd7f231e70aac888205f2da6f",
        },
        {
            "id": "privacy-policy",
            "version": "2026-06",
            "hash": "4907813a93513e8238f2dfde26b0b914e507b2925c57d288b7aa5dd4923dd02a",
        },
    ],
    # Nur diese beiden bereits verteilten Builds trugen das August-Manifest.
    # Build 287+ enthält nach dem Owner-Rollback wieder das Juni-Manifest.
    "min_client_build": 285,
    "max_client_build": 286,
}
_verify_manifest(_WITHDRAWN_AUGUST_2026_MANIFEST, "withdrawn august manifest")

# Reihenfolge = Vorrang. Kanonisch zuerst.
EQUIVALENT_LEGAL_MANIFESTS: list[dict[str, Any]] = [
    CURRENT_LEGAL_MANIFEST,
    _WITHDRAWN_AUGUST_2026_MANIFEST,
]

_UA_BUILD_RE = re.compile(r"AeroX/(\d{1,6})")


def _client_build() -> int | None:
    """Build-Nummer des anfragenden Clients aus dem User-Agent.

    CFNetwork sendet `AeroX/<CFBundleVersion> CFNetwork/… Darwin/…`. Das ist
    bei GET /status die EINZIGE Information darueber, welches Manifest der
    Client im Binary traegt — der Request hat weder Body noch Query.
    """
    match = _UA_BUILD_RE.search(request.headers.get("User-Agent", "") or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _client_manifest() -> dict[str, Any]:
    """Das Manifest, das der anfragende Build erwartet.

    Im Zweifel (unbekannter/fehlender User-Agent) das kanonische — ein
    falsch geratenes Alias-Echo wuerde die Wand erst recht ausloesen.
    """
    build = _client_build()
    if build is None:
        return CURRENT_LEGAL_MANIFEST
    for manifest in EQUIVALENT_LEGAL_MANIFESTS:
        minimum = manifest.get("min_client_build")
        maximum = manifest.get("max_client_build")
        if (
            minimum is not None
            and build >= minimum
            and (maximum is None or build <= maximum)
        ):
            return manifest
    return CURRENT_LEGAL_MANIFEST


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


def _public_manifest(manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = manifest or CURRENT_LEGAL_MANIFEST
    return {
        "version": manifest["manifest_version"],
        "hash": manifest["manifest_hash"],
        "documents": manifest["documents"],
    }


def _manifest_is_satisfied(manifest: dict[str, Any], rows: list[dict[str, Any]]) -> bool:
    expected = {
        (doc["id"], doc["version"], doc["hash"]) for doc in manifest["documents"]
    }
    present = {
        (row.get("document_id"), row.get("document_version"), row.get("document_hash"))
        for row in rows
    }
    return expected.issubset(present)


def _satisfied_manifest(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Erstes Manifest der Aequivalenzklasse, dessen Dokumente vollstaendig
    im Ledger liegen — oder None. Eine Zustimmung zu IRGENDEINEM davon zaehlt
    (Owner-Entscheid 2026-08-02, siehe oben)."""
    for manifest in EQUIVALENT_LEGAL_MANIFESTS:
        if _manifest_is_satisfied(manifest, rows):
            return manifest
    return None


@legal_consent_bp.get("/api/legal-consent/manifest")
def legal_consent_manifest():
    """AUTH-FREI: das aktuell servierte Rechts-Manifest (Version/Hash/Dokumente).

    Fuer das Ship-Gate `ios/AeroTax/scripts/check-legal-manifest.sh`
    (Vorfall 2026-08-02: iOS-Manifest-Bump ohne Server-Haelfte = 409-Wand;
    der Vertrag lebt in zwei Repos ohne Kopplung). Das Manifest ist ohnehin
    oeffentlich — es steckt wortgleich in jedem App-Binary; hier steht nur,
    welchen Stand der SERVER kennt."""
    return jsonify({"ok": True, "manifest": _public_manifest()})


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
            .in_(
                "manifest_hash",
                [m["manifest_hash"] for m in EQUIVALENT_LEGAL_MANIFESTS],
            )
            .execute()
        )
        rows = getattr(response, "data", None) or []
        satisfied = _satisfied_manifest(rows)
        # Dem Client wird das Manifest zurueckgespiegelt, das SEIN Build im
        # Binary traegt. `LegalConsentCoordinator.resolve` vergleicht Version
        # UND Hash exakt gegen die eigenen Konstanten und wirft bei jeder
        # Abweichung die Rechts-Wand hoch — auch bei `accepted: true`. Ohne
        # dieses Echo wuerde der Rueckbau auf Juni die Builds 285/286 genau
        # so aussperren, wie es der August-Bump mit allen Alt-Builds tat.
        client_manifest = _client_manifest()
        return jsonify(
            {
                "ok": True,
                "accepted": satisfied is not None,
                "accepted_documents": (
                    [doc["id"] for doc in client_manifest["documents"]]
                    if satisfied is not None
                    else []
                ),
                "current_manifest": _public_manifest(client_manifest),
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
    # Jedes Manifest der Aequivalenzklasse wird angenommen — verbucht wird
    # GENAU das, was der Nutzer gesehen und getippt hat, nie ein anderes.
    target = None
    for manifest in EQUIVALENT_LEGAL_MANIFESTS:
        if hmac.compare_digest(
            supplied_version, manifest["manifest_version"]
        ) and hmac.compare_digest(supplied_hash, manifest["manifest_hash"]):
            target = manifest
            break
    if target is None:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "legal_manifest_outdated",
                    "current_manifest": _public_manifest(_client_manifest()),
                }
            ),
            409,
        )

    locale = str(body.get("locale") or "")[:35] or None
    app_build = str(body.get("app_build") or "")[:64] or None
    if target is not CURRENT_LEGAL_MANIFEST:
        # Alias-Vermerk fuer die Revision: diese Zeile entstand aus einem
        # echten Tap auf ein zurueckgezogenes Manifest, das als aequivalent
        # zum kanonischen anerkannt wurde. `acceptance_source` setzt die RPC
        # selbst, `app_build` ist das einzige freie Audit-Feld.
        app_build = (
            f"{app_build or 'unknown'} "
            f"alias:{CURRENT_LEGAL_MANIFEST['manifest_version']}"
        )[:64]
    available, sb = _get_sb()
    if not available or sb is None:
        return _ledger_unavailable()
    try:
        response = sb.rpc(
            "accept_legal_manifest",
            {
                "p_user_token": token,
                "p_manifest_version": target["manifest_version"],
                "p_manifest_hash": target["manifest_hash"],
                "p_documents": target["documents"],
                "p_locale": locale,
                "p_app_build": app_build,
            },
        ).execute()
        return jsonify(
            {
                "ok": True,
                "accepted": True,
                "inserted": getattr(response, "data", 0) or 0,
                "current_manifest": _public_manifest(target),
            }
        )
    except Exception as exc:
        _log().warning("[legal-consent] accept unavailable: %s", type(exc).__name__)
        return _ledger_unavailable()
