"""SSRF-/Größenregressionen für serverseitige Kalender-Downloads."""

import app as A


class _Response:
    def __init__(self, status=200, headers=None, chunks=()):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = list(chunks)
        self.closed = False

    def iter_content(self, chunk_size):
        yield from self._chunks

    def close(self):
        self.closed = True


def test_calendar_redirect_target_is_revalidated_before_second_get(monkeypatch):
    first = _Response(302, {'Location': 'https://169.254.169.254/latest/meta-data'})
    calls = []

    def fake_get(url, timeout=20):
        calls.append(url)
        return first

    monkeypatch.setattr(A, '_calendar_feed_http_get', fake_get)
    monkeypatch.setattr(
        A, '_is_private_or_local_ip',
        lambda host: host == '169.254.169.254')

    text, error = A._fetch_calendar_feed_text('https://calendar.example/roster.ics')

    assert text is None
    assert error == 'internal_host_blocked'
    assert calls == ['https://calendar.example/roster.ics']
    assert first.closed is True


def test_calendar_redirect_must_remain_https(monkeypatch):
    first = _Response(302, {'Location': 'http://calendar.example/insecure.ics'})
    monkeypatch.setattr(A, '_calendar_feed_http_get', lambda *a, **k: first)
    monkeypatch.setattr(A, '_is_private_or_local_ip', lambda host: False)

    assert A._fetch_calendar_feed_text('https://calendar.example/roster.ics') == (
        None, 'bad_url')


def test_calendar_stream_limit_applies_without_content_length(monkeypatch):
    response = _Response(200, chunks=(
        b'a' * (A._CALENDAR_FEED_MAX_BYTES // 2),
        b'b' * (A._CALENDAR_FEED_MAX_BYTES // 2 + 1),
    ))
    monkeypatch.setattr(A, '_calendar_feed_http_get', lambda *a, **k: response)
    monkeypatch.setattr(A, '_is_private_or_local_ip', lambda host: False)

    assert A._fetch_calendar_feed_text('https://calendar.example/roster.ics') == (
        None, 'response_too_large')
    assert response.closed is True


def test_calendar_success_is_decoded_and_closed(monkeypatch):
    response = _Response(200, {'Content-Length': '32'},
                         (b'BEGIN:VCALENDAR\r\n', b'END:VCALENDAR\r\n'))
    monkeypatch.setattr(A, '_calendar_feed_http_get', lambda *a, **k: response)
    monkeypatch.setattr(A, '_is_private_or_local_ip', lambda host: False)

    text, error = A._fetch_calendar_feed_text('https://calendar.example/roster.ics')

    assert error is None
    assert text == 'BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n'
    assert response.closed is True
