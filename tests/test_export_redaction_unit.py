"""DSGVO-Export-Redaction — Unit-Regression (Owner-Audit 2026-07-15).

Fund 143 der 190-Punkte-Liste: `export['friends']` war eine Liste NACKTER
Friend-Tokens (= lebende Bearer-Credentials); `_redact_export_secrets`
maskiert nur dict-Werte anhand ihres Keys, blanke Listen-Strings liefen
unmaskiert durch. Diese Tests pinnen (a) den neuen pseudonymen
Friends-Export (`_export_friend_entries`) und (b) die iCal-Feed-URL-
Redaction (Fund 142: bisher ohne Regressionstest).
"""
import app as app_module


def test_export_friend_entries_never_contain_raw_tokens():
    raw = ["AT-SECRETFRIENDTOKEN-tibor", "AT-OTHERSECRET-julien"]
    out = app_module._export_friend_entries(raw)
    assert len(out) == 2
    dumped = repr(out)
    for tok in raw:
        assert tok not in dumped, f"raw friend token leaked: {tok}"
        # Auch kein Klartext-Präfix (dient anderswo als Ownership-Schlüssel).
        assert tok[:8] not in dumped
    for entry in out:
        assert entry["friend_token"] == "[redacted]"
        assert len(entry["friend_id"]) == 12
    # Pseudonym ist stabil (gleicher Input ⇒ gleiche Kennung) …
    assert app_module._export_friend_entries(raw) == out
    # … und pro Freund verschieden.
    assert out[0]["friend_id"] != out[1]["friend_id"]


def test_export_friend_entries_handles_empty_and_none():
    assert app_module._export_friend_entries(None) == []
    assert app_module._export_friend_entries([]) == []


def test_redact_masks_ical_feed_url_but_keeps_avatar_url():
    export = {
        "profile": {
            "calendar_feed": {
                "url": "https://crew-portal.example/ical?secret=SUPERSECRET",
                "events": [{"summary": "LH123"}],
            },
            "avatar_url": "https://cdn.example/avatar.png",
        },
    }
    red = app_module._redact_export_secrets(export)
    assert red["profile"]["calendar_feed"]["url"] == "[redacted]"
    assert red["profile"]["avatar_url"] == "https://cdn.example/avatar.png"
    assert red["profile"]["calendar_feed"]["events"] == [{"summary": "LH123"}]


def test_redact_masks_token_keys_recursively_in_lists():
    export = {
        "wall_posts": [
            {"author_token": "AT-AUTHORSECRET", "text": "hi"},
            {"nested": {"friend_token": "AT-NESTEDSECRET"}},
        ],
        "meta": {"token": "AT-OWNSECRET"},
    }
    red = app_module._redact_export_secrets(export)
    assert red["wall_posts"][0]["author_token"] == "[redacted]"
    assert red["wall_posts"][0]["text"] == "hi"
    assert red["wall_posts"][1]["nested"]["friend_token"] == "[redacted]"
    assert red["meta"]["token"] == "[redacted]"
