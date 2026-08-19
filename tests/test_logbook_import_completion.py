"""Verified terminalization of a durable manual logbook import."""
import importlib.util
import json
import os

import pytest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'tools', 'logbook-parsers', 'upsert_logbook.py')
SPEC = importlib.util.spec_from_file_location('upsert_logbook_tool', SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class _Cursor:
    def __init__(self, fetches):
        self.fetches = list(fetches)
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((' '.join(sql.split()), params))

    def fetchone(self):
        return self.fetches.pop(0)


def test_completed_upload_is_idempotent_and_never_enqueues_again():
    cur = _Cursor([('AT-owner', 'completed')])

    assert MODULE._complete_upload_after_verified(cur, 7, 'AT-owner') is False
    assert len(cur.calls) == 1
    assert 'for update' in cur.calls[0][0].lower()


def test_upload_completion_rejects_cross_account_job():
    cur = _Cursor([('AT-other', 'pending')])

    with pytest.raises(RuntimeError, match='gehört nicht'):
        MODULE._complete_upload_after_verified(cur, 8, 'AT-owner')
    assert len(cur.calls) == 1


def test_pending_upload_completes_and_enqueues_one_scoped_push():
    cur = _Cursor([('AT-owner', 'pending'), (9,)])

    assert MODULE._complete_upload_after_verified(cur, 9, 'AT-owner') is True
    assert len(cur.calls) == 3
    update_sql, update_params = cur.calls[1]
    assert "status='completed'" in update_sql
    assert 'error_code=null' in update_sql
    assert 'data_b64=null' in update_sql
    assert 'payload_purged_at=now()' in update_sql
    assert update_params == (9, 'AT-owner')

    push_sql, push_params = cur.calls[2]
    assert 'enqueue_push_outbox' in push_sql
    assert push_params[0] == 'logbook-import-completed:9'
    assert push_params[1] == 'AT-owner'
    payload = json.loads(push_params[2])
    assert payload['data'] == {
        'type': 'logbook_import_completed',
        # L10n-Durchgang 2026-08-12: der Push trägt den Schlüssel, damit die
        # App Titel/Text in der Gerätesprache rendert (wie die übrigen Pushes).
        'localization_key': 'logbook_import_completed',
        'job_id': 9,
        'deep_link': 'aerox://more/logbook',
    }


def test_upload_id_argument_must_be_positive_and_is_removed_from_metadata():
    parsed = MODULE._args([
        'upsert_logbook.py', 'parsed.json', 'AT-owner', 'Import',
        '{"source":"manual"}', '--upload-id', '12',
    ])
    assert parsed == ('parsed.json', 'AT-owner', 'Import',
                      {'source': 'manual'}, [12])

    repeated = MODULE._args([
        'upsert_logbook.py', 'parsed.json', 'AT-owner', 'Import',
        '--upload-id', '12', '--upload-id', '13', '--upload-id', '12',
    ])
    assert repeated[-1] == [12, 13]

    with pytest.raises(SystemExit, match='positiv'):
        MODULE._args(['upsert_logbook.py', 'a.json', 'AT-owner', 'Import',
                      '--upload-id', '0'])


def test_duplicate_upload_batch_completes_all_rows_with_one_push():
    cur = _Cursor([
        ('AT-owner', 'review'), (12,),
        ('AT-owner', 'review'), (13,),
    ])

    completed = MODULE._complete_upload_batch_after_verified(
        cur, [13, 12, 13], 'AT-owner')

    assert completed == [12, 13]
    pushes = [call for call in cur.calls
              if 'enqueue_push_outbox' in call[0]]
    assert len(pushes) == 1
    assert pushes[0][1][0] == 'logbook-import-completed:13'
    assert json.loads(pushes[0][1][2])['data']['job_id'] == 13


def test_batch_push_anchor_is_a_newly_completed_upload():
    cur = _Cursor([
        ('AT-owner', 'review'), (12,),
        ('AT-owner', 'completed'),
    ])

    completed = MODULE._complete_upload_batch_after_verified(
        cur, [12, 13], 'AT-owner')

    assert completed == [12]
    pushes = [call for call in cur.calls
              if 'enqueue_push_outbox' in call[0]]
    assert len(pushes) == 1
    assert pushes[0][1][0] == 'logbook-import-completed:12'
