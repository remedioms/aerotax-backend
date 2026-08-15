import os
import urllib.error
import urllib.request

import app  # noqa: F401 -- finish app initialization before blueprint import
from blueprints import adsb_blueprint as adsb


def _reset_cache():
    adsb._OAUTH_CACHE.update(token=None, expires_at=0.0, fail_until=0.0)


def test_shared_rejected_fingerprint_skips_network(monkeypatch):
    monkeypatch.setenv("OPENSKY_CLIENT_ID", "client")
    monkeypatch.setenv("OPENSKY_CLIENT_SECRET", "secret")
    expected = adsb.hashlib.sha256(b"client\0secret").hexdigest()
    monkeypatch.setattr(adsb, "_poll_state_get", lambda key: (
        {"credential_key": expected} if key == "oauth_rejected_credentials" else None))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: (
        (_ for _ in ()).throw(AssertionError("network must not be reached"))))
    _reset_cache()

    assert adsb._opensky_oauth_token() is None


def test_unauthorized_credentials_are_shared_without_storing_secret(monkeypatch):
    monkeypatch.setenv("OPENSKY_CLIENT_ID", "client")
    monkeypatch.setenv("OPENSKY_CLIENT_SECRET", "secret")
    monkeypatch.setattr(adsb, "_poll_state_get", lambda _key: None)
    saved = {}
    monkeypatch.setattr(adsb, "_poll_state_put",
                        lambda key, value: saved.update({key: value}))

    def unauthorized(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", unauthorized)
    _reset_cache()

    assert adsb._opensky_oauth_token() is None
    rejection = saved["oauth_rejected_credentials"]
    assert rejection["credential_key"] != "secret"
    assert "secret" not in str(rejection)
