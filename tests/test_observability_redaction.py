from copy import deepcopy
import logging

from observability.redaction import (
    REDACTED,
    REDACTED_TOKEN,
    redact_mapping,
    redact_text,
    redact_url,
    RedactingLogFilter,
)
from observability.sentry_setup import _strip_sensitive


TOKEN = "AT-THIS-IS-A-LONG-LIVED-CREDENTIAL"


def test_request_path_and_query_credentials_are_fully_redacted():
    raw = f"https://api.example/api/user/profile/{TOKEN}?token={TOKEN}&limit=5"
    safe = redact_url(raw)
    assert TOKEN not in safe
    assert "token=[redacted]" in safe
    assert f"profile/{REDACTED_TOKEN}" in safe
    assert "limit=5" in safe


def test_bearer_and_embedded_path_are_redacted_from_free_text():
    safe = redact_text(f"GET /api/wall/{TOKEN}/feed Authorization: Bearer {TOKEN}")
    assert TOKEN not in safe
    assert "Bearer [redacted-token]" in safe


def test_mapping_redaction_is_recursive_and_does_not_mutate_input():
    raw = {
        "request": {"url": f"/api/user/profile/{TOKEN}", "headers": {
            "Authorization": f"Bearer {TOKEN}", "Accept": "application/json"
        }},
        "breadcrumbs": [{"message": f"path=/{TOKEN}"}],
        "token_prefix": TOKEN[:8],
    }
    before = deepcopy(raw)
    safe = redact_mapping(raw)
    assert raw == before
    assert TOKEN not in repr(safe)
    assert safe["request"]["headers"]["Authorization"] == REDACTED
    assert safe["token_prefix"] == REDACTED_TOKEN


def test_sentry_hook_scrubs_url_query_headers_breadcrumbs_and_exception_text():
    event = {
        "request": {
            "url": f"https://api.example/api/user/profile/{TOKEN}?token={TOKEN}",
            "query_string": f"token={TOKEN}",
            "headers": {"Authorization": f"Bearer {TOKEN}"},
        },
        "breadcrumbs": {"values": [{
            "message": f"GET /api/user/profile/{TOKEN}",
            "data": {"url": f"/api/wall/{TOKEN}/feed"},
        }]},
        "exception": {"values": [{"value": f"failure at /api/user/profile/{TOKEN}"}]},
    }
    safe = _strip_sensitive(event, {})
    assert TOKEN not in repr(safe)
    assert safe["request"]["headers"]["Authorization"] == REDACTED


def test_logging_filter_redacts_after_percent_formatting():
    record = logging.LogRecord(
        'werkzeug', logging.INFO, __file__, 1,
        'GET %s Authorization: Bearer %s',
        (f'/api/user/profile/{TOKEN}', TOKEN), None,
    )
    assert RedactingLogFilter().filter(record) is True
    rendered = record.getMessage()
    assert TOKEN not in rendered
    assert REDACTED_TOKEN in rendered


def test_historical_eight_character_token_prefix_is_redacted():
    assert redact_text(TOKEN[:8]) == REDACTED_TOKEN


def test_logging_filter_failure_emits_neutral_record(monkeypatch):
    import observability.redaction as redaction

    record = logging.LogRecord(
        'werkzeug', logging.INFO, __file__, 1, TOKEN, (), None,
    )

    def broken(_value):
        raise RuntimeError('scrubber unavailable')

    monkeypatch.setattr(redaction, 'redact_text', broken)
    assert redaction.RedactingLogFilter().filter(record) is True
    assert record.getMessage() == '[redacted-log-record]'


def test_unknown_object_is_never_forwarded_to_serializer():
    class SecretObject:
        def __str__(self):
            return f"Bearer {TOKEN}"

        def __repr__(self):
            return f"SecretObject({TOKEN})"

    raw = {"extra": {"mystery": SecretObject()}}
    safe = redact_mapping(raw)
    assert safe["extra"]["mystery"] == "[redacted-object]"
    assert TOKEN not in repr(safe)


def test_sentry_scrub_failure_drops_event(monkeypatch):
    import observability.sentry_setup as sentry_setup

    def broken(_event):
        raise RuntimeError("scrubber unavailable")

    monkeypatch.setattr(sentry_setup, "redact_mapping", broken)
    assert sentry_setup._strip_sensitive({"request": {"url": TOKEN}}, {}) is None
