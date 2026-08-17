#!/usr/bin/env python3
"""Retry unknown-airline roster sources and promote verified carriers.

The first import already happens synchronously in the onboarding endpoint.
This small cron worker is the durable 24-hour fallback: it retries temporary
feed/AI failures from the service-key-only Supabase queue, alerts the owner on
the first unresolved attempt and promotes a carrier only after the normal
calendar pipeline persisted real events. It never invents parser output and it
never exposes or logs a calendar URL/PDF payload.
"""

import base64
import io
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

STATUS_PENDING = 'pending'
STATUS_PROCESSING = 'processing'
STATUS_SUPPORTED = 'supported'
STATUS_REVIEW = 'review'
MAX_ATTEMPTS = 6
# Last automatic attempt lands at roughly 23 h 45 min after submission.
BACKOFF_MINUTES = (15, 60, 180, 480, 690)


def _backend():
    import app
    return app


def _log(message):
    now = datetime.now(timezone.utc).isoformat()
    print(f'[airline-support-watchdog] {now} {message}', flush=True)


def _result_rows(result):
    return getattr(result, 'data', None) or []


def _pending_rows(backend):
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    result = (backend.sb.table('airline_support_requests')
              .select('id,token,airline_name,normalized_name,homebase,'
                      'source_kind,source_upload_id,source_filename,'
                      'source_url_enc,attempt_count,created_at')
              .eq('status', STATUS_PENDING)
              .or_(f'next_attempt_at.is.null,next_attempt_at.lte.{now}')
              .order('id').limit(40).execute())
    return _result_rows(result)


def _recover_stale(backend):
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime(
        '%Y-%m-%dT%H:%M:%SZ')
    result = (backend.sb.table('airline_support_requests')
              .update({'status': STATUS_PENDING})
              .eq('status', STATUS_PROCESSING)
              .lt('updated_at', cutoff).execute())
    rows = _result_rows(result)
    if rows:
        _log(f'recovered processing ids={[row.get("id") for row in rows]}')


def _claim(backend, row):
    now = datetime.now(timezone.utc).isoformat()
    result = (backend.sb.table('airline_support_requests')
              .update({'status': STATUS_PROCESSING, 'updated_at': now})
              .eq('id', row['id']).eq('status', STATUS_PENDING).execute())
    return bool(_result_rows(result))


def _response_payload(response):
    status = 200
    if isinstance(response, tuple):
        response, status = response[:2]
    try:
        payload = response.get_json() or {}
    except Exception:
        payload = {}
    return status, payload


def _retry_ical(backend, row):
    source_url = backend._calendar_feed_decrypt_value(
        row.get('source_url_enc'), 'url')
    if not source_url:
        return False, 'source_decryption_failed'
    # Direct internal dispatch: no second public API hop and no credential in
    # logs. The canonical feed importer still performs fetch/parse/persist.
    with backend.app.test_request_context(json={'url': source_url}):
        status, payload = _response_payload(
            backend.import_calendar_feed(row['token']))
    return bool(status == 200 and payload.get('ok')), (
        payload.get('error') or f'http_{status}')


def _pdf_source(backend, row):
    upload_id = row.get('source_upload_id')
    if not upload_id:
        return None
    result = (backend.sb.table('ax_logbook_upload')
              .select('id,token,filename,data_b64')
              .eq('id', upload_id).eq('token', row['token'])
              .limit(1).execute())
    rows = _result_rows(result)
    if not rows or not rows[0].get('data_b64'):
        return None
    try:
        blob = base64.b64decode(rows[0]['data_b64'], validate=True)
    except Exception:
        return None
    if not blob.startswith(b'%PDF-') or len(blob) > 8 * 1024 * 1024:
        return None
    return rows[0], blob


def _retry_pdf(backend, row):
    source = _pdf_source(backend, row)
    if source is None:
        return False, 'pdf_source_unavailable'
    upload, blob = source
    form = {
        'airline': row['airline_name'],
        'homebase': row.get('homebase') or '',
        'pdf': (io.BytesIO(blob), upload.get('filename') or 'roster.pdf'),
    }
    # The same deterministic-first + guarded-AI pipeline as the initial user
    # upload. The queue dedupe makes this retry idempotent.
    with backend.app.test_request_context(
            method='POST', data=form, content_type='multipart/form-data'):
        status, payload = _response_payload(
            backend.import_roster_pdf(row['token']))
    return bool(status == 200 and payload.get('ok')), (
        payload.get('error') or f'http_{status}')


def _stored_event_count(backend, token):
    feed = backend._airline_request_profile_feed(token)
    events = feed.get('events') if isinstance(feed, dict) else None
    return len(events) if isinstance(events, list) else 0


def _mark_supported(backend, row, events_count):
    now = datetime.now(timezone.utc).isoformat()
    (backend.sb.table('airline_support_requests').update({
        'status': STATUS_SUPPORTED,
        'events_count': events_count,
        'last_error': None,
        'next_attempt_at': None,
        'updated_at': now,
        'completed_at': now,
    }).eq('id', row['id']).eq('status', STATUS_PROCESSING).execute())
    backend._airline_catalog_promote(
        row['airline_name'], row['normalized_name'], row['source_kind'])


def _alert_owner(row, error):
    key = os.environ.get('RESEND_API_KEY', '').strip()
    if not key:
        _log(f'owner-alert skipped no-key id={row["id"]}')
        return
    body = (
        'Eine neue Airline braucht Parser-Aufmerksamkeit.\n\n'
        f'Airline: {row["airline_name"]}\n'
        f'Homebase: {row.get("homebase") or "—"}\n'
        f'Quelle: {row["source_kind"]}\n'
        f'Anfrage-ID: {row["id"]}\n'
        f'Fehlercode: {error}\n\n'
        'Die Quelle liegt privat in Supabase. Keine Zugangsdaten oder PDF-'
        'Inhalte werden in dieser Mail mitgesendet.')
    request = urllib.request.Request(
        'https://api.resend.com/emails', method='POST',
        data=json.dumps({
            'from': os.environ.get(
                'MAIL_FROM', 'AeroX <noreply@aerosteuer.de>'),
            'to': ['aerox@aerosteuer.de'],
            'subject': f'[AeroX] Neue Airline: {row["airline_name"]}',
            'text': body,
        }).encode(),
        headers={'Authorization': f'Bearer {key}',
                 'Content-Type': 'application/json',
                 'User-Agent': 'curl/8.0'})
    try:
        urllib.request.urlopen(request, timeout=30).read()
    except Exception as exc:
        _log(f'owner-alert failed id={row["id"]} error={type(exc).__name__}')


def _mark_retry(backend, row, error):
    attempts = int(row.get('attempt_count') or 0) + 1
    now = datetime.now(timezone.utc)
    terminal = attempts >= MAX_ATTEMPTS
    values = {
        'status': STATUS_REVIEW if terminal else STATUS_PENDING,
        'attempt_count': attempts,
        'last_error': str(error or 'unknown')[:240],
        'updated_at': now.isoformat(),
        'next_attempt_at': None,
    }
    if not terminal:
        delay = BACKOFF_MINUTES[min(attempts - 1, len(BACKOFF_MINUTES) - 1)]
        values['next_attempt_at'] = (now + timedelta(minutes=delay)).isoformat()
    (backend.sb.table('airline_support_requests').update(values)
     .eq('id', row['id']).eq('status', STATUS_PROCESSING).execute())
    if attempts == 1 or terminal:
        _alert_owner(row, error)


def process_row(backend, row):
    if not _claim(backend, row):
        return
    try:
        if row.get('source_kind') == 'ical_url':
            imported, error = _retry_ical(backend, row)
        elif row.get('source_kind') == 'pdf':
            imported, error = _retry_pdf(backend, row)
        else:
            imported, error = False, 'invalid_source_kind'
        event_count = _stored_event_count(backend, row['token'])
        if imported and event_count > 0:
            _mark_supported(backend, row, event_count)
            _log(f'supported id={row["id"]} airline={row["normalized_name"]}')
        else:
            _mark_retry(backend, row, error or 'no_calendar_events')
            _log(f'retry id={row["id"]} attempt={int(row.get("attempt_count") or 0) + 1}')
    except Exception as exc:  # fail closed; next cron run retries it
        _mark_retry(backend, row, type(exc).__name__)
        _log(f'exception id={row["id"]} error={type(exc).__name__}')


def main():
    backend = _backend()
    if not backend.SB_AVAILABLE:
        raise SystemExit('Supabase service client unavailable')
    _recover_stale(backend)
    rows = _pending_rows(backend)
    for row in rows:
        process_row(backend, row)
    _log(f'done rows={len(rows)}')


if __name__ == '__main__':
    main()
