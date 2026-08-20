"""Fail-closed Sol fallback for a previously unknown flight-log layout."""

import json
import os
import sys

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools", "logbook-parsers"))

import logbook_watchdog as w  # noqa: E402


SOURCE = """Acme Flight History
Date format YYYY-MM-DD | Block time HH:MM
2026-08-01 LH400 FRA JFK 08:15 A350 D-AIXA FO 1 0 02:15
"""


def _item(**changes):
    item = {
        "date_text": "2026-08-01",
        "flight_no": "LH400",
        "from_iata": "FRA",
        "to_iata": "JFK",
        "block_time": "08:15",
        "aircraft_type": "A350",
        "registration": "D-AIXA",
        "role": "FO",
        "landings_day_text": "1",
        "landings_night_text": "0",
        "night_time": "02:15",
        "source_evidence": (
            "2026-08-01 LH400 FRA JFK 08:15 A350 D-AIXA FO 1 0 02:15"),
    }
    item.update(changes)
    return item


class _Response:
    def __init__(self, items):
        self.payload = json.dumps({"output_text": json.dumps({"legs": items})})

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload.encode()


def test_source_guard_builds_only_literal_verified_leg():
    legs, dropped = w._logbook_ai_validate_items([_item()], SOURCE)
    assert dropped == 0 and len(legs) == 1
    public = w._logbook_ai_public_leg(legs[0])
    assert public == {
        "date": "2026-08-01",
        "flight": "LH400",
        "from": "FRA",
        "to": "JFK",
        "block_min": 495,
        "type": "A350",
        "reg": "D-AIXA",
        "role": "FO",
        "ldg_day": 1,
        "ldg_night": 0,
        "night_min": 135,
    }


@pytest.mark.parametrize("change", [
    {"registration": "D-FAKE"},
    {"block_time": "09:15"},
    {"source_evidence": "2026-08-01 LH400 FRA JFK"},
    {"from_iata": "MUC"},
])
def test_source_guard_rejects_unprinted_or_incomplete_facts(change):
    legs, dropped = w._logbook_ai_validate_items([_item(**change)], SOURCE)
    assert legs == [] and dropped == 1


def test_ambiguous_numeric_date_is_rejected_without_format_header():
    source = "04/05/2026 LH400 FRA JFK 08:15"
    item = _item(
        date_text="04/05/2026", aircraft_type=None, registration=None,
        role=None, landings_day_text=None, landings_night_text=None,
        night_time=None, source_evidence=source)
    legs, dropped = w._logbook_ai_validate_items([item], source)
    assert legs == [] and dropped == 1


def test_unknown_text_logbook_is_read_twice_with_sol_xhigh(monkeypatch, tmp_path):
    path = tmp_path / "unknown.csv"
    path.write_text(SOURCE)
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((json.loads(request.data), timeout))
        return _Response([_item()])

    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-key")
    monkeypatch.delenv("AEROX_LOGBOOK_OPENAI_MODEL", raising=False)
    monkeypatch.delenv("AEROX_LOGBOOK_OPENAI_EFFORT", raising=False)
    monkeypatch.delenv("AEROX_ROSTER_OPENAI_MODEL", raising=False)
    monkeypatch.delenv("AEROX_ROSTER_OPENAI_EFFORT", raising=False)
    monkeypatch.setattr(w.urllib.request, "urlopen", fake_urlopen)

    result, error = w._try_openai_logbook(str(path), "AT-private-user")

    assert error is None
    name, legs, sims, report = result
    assert name == "openai_verified_logbook" and sims == []
    assert legs[0]["flight"] == "LH400" and legs[0]["block_min"] == 495
    assert report["independent_reads"] == 2
    assert report["source_evidence_guard"] is True
    assert report["store"] is False
    assert len(calls) == 2
    for body, timeout in calls:
        assert body["model"] == "gpt-5.6-sol"
        assert body["reasoning"] == {"effort": "xhigh"}
        assert body["store"] is False
        assert body["text"]["format"]["strict"] is True
        assert body["safety_identifier"].startswith("ax-logbook-")
        assert "AT-private-user" not in json.dumps(body)
        assert timeout == w.LOGBOOK_AI_TIMEOUT_SECONDS


def test_disagreeing_reads_return_review_reason(monkeypatch, tmp_path):
    path = tmp_path / "unknown.csv"
    path.write_text(SOURCE)
    answers = [[_item()], []]

    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-key")
    monkeypatch.setattr(
        w.urllib.request, "urlopen",
        lambda _request, timeout: _Response(answers.pop(0)))

    result, error = w._try_openai_logbook(str(path), "AT-private-user")

    assert result is None and error == "independent_reads_disagree"


def test_duplicate_identity_without_departure_time_is_rejected():
    source = SOURCE + SOURCE.splitlines()[-1] + "\n"
    items = [_item(), _item()]
    legs, dropped = w._logbook_ai_validate_items(items, source)
    assert legs == [] and dropped == 1
