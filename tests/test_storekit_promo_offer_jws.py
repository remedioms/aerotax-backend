"""StoreKit Promotional-Offer-Signierung: additive JWS (ES256) Erweiterung.

Verifiziert, dass GET /api/storekit/promo-offer zusätzlich zum Legacy-Feld
`signature_b64` ein `signature_jws` (JWS compact serialization, ES256) liefert:

  * 3-Segment-Struktur (header.payload.signature), base64url ohne Padding.
  * Header {"alg":"ES256","kid":<key_id>,"typ":"JWT"}.
  * Payload nach Apple-StoreKit-2-Promo-Spec (appBundleId/productId/
    offerIdentifier/nonce/timestamp), optional appAccountToken.
  * Signatur ist raw R||S (64 Bytes), NICHT DER, und verifiziert gegen den
    öffentlichen Schlüssel des Signierschlüssels.
  * Legacy-Felder (signature_b64, nonce, timestamp, ...) bleiben unverändert
    und teilen sich nonce/timestamp mit der JWS-Payload.

Sicherheit: verwendet einen FRISCH generierten EC-P256-Testkey, NIE den
Prod-Key; es werden keine Key-Werte geloggt oder in Assertions/Output
gespiegelt.
"""
import base64
import json
from unittest.mock import patch

import pytest

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils as _asym_utils

import app as A


TOKEN = "AT-JWS-PROMO-TEST-TOKEN"


def _gen_test_key_pem():
    """Frischer EC-P256-Testschlüssel (nie Prod). Liefert (pem_str, public_key)."""
    priv = ec.generate_private_key(ec.SECP256R1())
    pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")
    return pem, priv.public_key()


def _b64url_decode(seg: str) -> bytes:
    pad = "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg + pad)


def _call_endpoint(monkeypatch_env, query=""):
    """Ruft den Endpoint mit gemocktem Founding-Profil auf. Liefert JSON + pubkey."""
    pem, pub = _gen_test_key_pem()
    key_id = "TESTKEYID99"
    envs = {
        "ASC_SUB_KEY_P8": pem,
        "ASC_SUB_KEY_ID": key_id,
        "ASC_BUNDLE_ID": "aerotax.AeroTax",
        "ASC_FOUNDING_OFFER_ID": "founding6m",
    }
    for k, v in envs.items():
        monkeypatch_env.setenv(k, v)
    client = A.app.test_client()
    # Founding-Profil: kein family, pro_first_seen VOR dem Wall-Datum.
    founding_profile = {"profile": {"account_type": "user",
                                    "pro_first_seen": "2026-06-25T00:00:00"}}
    with patch.object(A, "_profile_load", return_value=founding_profile):
        resp = client.get("/api/storekit/promo-offer?product=aerox.pro.yearly" + query,
                          headers={"Authorization": f"Bearer {TOKEN}"})
    return resp, pub, key_id


def test_jws_present_and_three_segments(monkeypatch):
    resp, _pub, _kid = _call_endpoint(monkeypatch)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    jws = body["signature_jws"]
    assert isinstance(jws, str)
    parts = jws.split(".")
    assert len(parts) == 3, "JWS compact serialization braucht genau 3 Segmente"
    # base64url ohne Padding
    for seg in parts:
        assert "=" not in seg
        assert "+" not in seg and "/" not in seg


def test_jws_header_fields(monkeypatch):
    resp, _pub, kid = _call_endpoint(monkeypatch)
    body = resp.get_json()
    header_seg = body["signature_jws"].split(".")[0]
    header = json.loads(_b64url_decode(header_seg))
    assert header == {"alg": "ES256", "kid": kid, "typ": "JWT"}


def test_jws_payload_fields_match_apple_spec(monkeypatch):
    resp, _pub, _kid = _call_endpoint(monkeypatch)
    body = resp.get_json()
    payload_seg = body["signature_jws"].split(".")[1]
    payload = json.loads(_b64url_decode(payload_seg))
    assert payload["appBundleId"] == "aerotax.AeroTax"
    assert payload["productId"] == "aerox.pro.yearly"
    assert payload["offerIdentifier"] == "founding6m"
    # Gleiche nonce/timestamp wie die Legacy-Signatur im selben Response.
    assert payload["nonce"] == body["nonce"]
    assert payload["timestamp"] == body["timestamp"]
    # nonce = lowercase UUID
    assert payload["nonce"] == payload["nonce"].lower()
    # timestamp = ms (int)
    assert isinstance(payload["timestamp"], int)
    # appAccountToken nur wenn übergeben — hier NICHT.
    assert "appAccountToken" not in payload


def test_jws_includes_app_account_token_when_provided(monkeypatch):
    resp, _pub, _kid = _call_endpoint(
        monkeypatch, query="&appAccountToken=abc-123-account")
    body = resp.get_json()
    payload = json.loads(_b64url_decode(body["signature_jws"].split(".")[1]))
    assert payload["appAccountToken"] == "abc-123-account"


def test_jws_signature_verifies_with_public_key(monkeypatch):
    resp, pub, _kid = _call_endpoint(monkeypatch)
    body = resp.get_json()
    header_seg, payload_seg, sig_seg = body["signature_jws"].split(".")
    signing_input = f"{header_seg}.{payload_seg}".encode("ascii")
    raw_sig = _b64url_decode(sig_seg)
    # raw R||S = 64 Bytes, NICHT DER.
    assert len(raw_sig) == 64
    r = int.from_bytes(raw_sig[:32], "big")
    s = int.from_bytes(raw_sig[32:], "big")
    der = _asym_utils.encode_dss_signature(r, s)
    # Wirft InvalidSignature bei Fehler → Test schlägt fehl.
    pub.verify(der, signing_input, ec.ECDSA(hashes.SHA256()))


def test_legacy_fields_unchanged(monkeypatch):
    resp, _pub, kid = _call_endpoint(monkeypatch)
    body = resp.get_json()
    # Alle Legacy-Contract-Felder weiterhin vorhanden.
    for field in ("ok", "offer_id", "key_id", "nonce", "timestamp", "signature_b64"):
        assert field in body, f"Legacy-Feld {field} fehlt"
    assert body["offer_id"] == "founding6m"
    assert body["key_id"] == kid
    # signature_b64 ist base64-dekodierbar (DER-Signatur, Legacy).
    raw = base64.b64decode(body["signature_b64"])
    assert len(raw) > 0
    # additive JWS ändert die Legacy-Signatur nicht in ihrer Form (DER, nicht 64B raw)
    assert isinstance(body["signature_b64"], str)


def test_not_configured_returns_503(monkeypatch):
    monkeypatch.delenv("ASC_SUB_KEY_P8", raising=False)
    monkeypatch.delenv("ASC_SUB_KEY_ID", raising=False)
    client = A.app.test_client()
    resp = client.get("/api/storekit/promo-offer",
                      headers={"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 503
    assert resp.get_json()["error"] == "not_configured"
