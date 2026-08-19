"""24-hour retry worker for unknown-airline onboarding sources."""

import os
import sys
from types import SimpleNamespace

from flask import Flask, jsonify

PARSERS = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                       'tools', 'logbook-parsers')
if PARSERS not in sys.path:
    sys.path.insert(0, PARSERS)

import airline_support_watchdog as worker  # noqa: E402


class _Query:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.updated = None
        self.or_value = None

    def select(self, *_args): return self
    def update(self, values): self.updated = values; return self
    def eq(self, *_args): return self
    def lt(self, *_args): return self
    def order(self, *_args, **_kwargs): return self
    def limit(self, *_args): return self
    def or_(self, value): self.or_value = value; return self
    def execute(self): return SimpleNamespace(data=self.rows)


class _SB:
    def __init__(self, query): self.query = query
    def table(self, _name): return self.query


def test_pending_query_includes_due_or_never_attempted_rows():
    query = _Query([{'id': 1}])
    backend = SimpleNamespace(sb=_SB(query))
    assert worker._pending_rows(backend) == [{'id': 1}]
    assert 'next_attempt_at.is.null' in query.or_value
    assert 'next_attempt_at.lte.' in query.or_value


def test_ical_retry_decrypts_and_uses_canonical_import_without_logging_url():
    flask_app = Flask(__name__)
    calls = []

    def run_import(token):
        from flask import request
        calls.append((token, request.get_json()['url']))
        return jsonify({'ok': True, 'events_count': 3})

    backend = SimpleNamespace(
        app=flask_app,
        _calendar_feed_decrypt_value=lambda value, field: (
            'https://private.example/roster.ics'
            if value == 'encrypted' and field == 'url' else ''),
        import_calendar_feed=run_import,
    )
    ok, _error = worker._retry_ical(backend, {
        'token': 'AT-OWNER', 'source_url_enc': 'encrypted'})
    assert ok is True
    assert calls == [('AT-OWNER', 'https://private.example/roster.ics')]


def test_ical_retry_schedule_alerts_and_reaches_final_attempt_inside_24_hours(
        monkeypatch):
    query = _Query([{'id': 7}])
    backend = SimpleNamespace(sb=_SB(query))
    alerts = []
    monkeypatch.setattr(worker, '_alert_owner',
                        lambda row, error: alerts.append((row['id'], error)))
    row = {'id': 7, 'attempt_count': 0, 'airline_name': 'Example',
           'source_kind': 'ical_url'}

    worker._mark_retry(backend, row, 'unsupported_pdf_format')

    assert query.updated['status'] == 'pending'
    assert query.updated['attempt_count'] == 1
    assert query.updated['next_attempt_at'] is not None
    assert alerts == [(7, 'unsupported_pdf_format')]
    assert sum(worker.BACKOFF_MINUTES) < 24 * 60


def test_first_pdf_retry_does_not_duplicate_upload_review_mail(monkeypatch):
    query = _Query([{'id': 9}])
    backend = SimpleNamespace(sb=_SB(query))
    alerts = []
    monkeypatch.setattr(worker, '_alert_owner',
                        lambda row, error: alerts.append((row['id'], error)))
    row = {'id': 9, 'attempt_count': 0, 'airline_name': 'Example',
           'source_kind': 'pdf'}

    worker._mark_retry(backend, row, 'unsupported_pdf_format')

    assert query.updated['attempt_count'] == 1
    assert query.updated['status'] == 'pending'
    assert alerts == []


def test_sixth_failure_moves_to_private_review_queue(monkeypatch):
    query = _Query([{'id': 8}])
    backend = SimpleNamespace(sb=_SB(query))
    alerts = []
    monkeypatch.setattr(worker, '_alert_owner',
                        lambda row, error: alerts.append(error))
    row = {'id': 8, 'attempt_count': 5, 'airline_name': 'Example',
           'source_kind': 'ical_url'}

    worker._mark_retry(backend, row, 'fetch_failed')

    assert query.updated['status'] == 'review'
    assert query.updated['attempt_count'] == 6
    assert query.updated['next_attempt_at'] is None
    assert alerts == ['fetch_failed']


def test_logbook_queue_excludes_roster_and_ai_learning_material(monkeypatch):
    import logbook_watchdog
    captured = []
    monkeypatch.setattr(logbook_watchdog, '_rest',
                        lambda method, query: captured.append(query) or [])
    logbook_watchdog._pending_rows()
    query = captured[0]
    assert logbook_watchdog.ROSTER_MARKER in query
    assert logbook_watchdog.ROSTER_AI_LEARN_MARKER in query
    assert 'note.is.null' in query
