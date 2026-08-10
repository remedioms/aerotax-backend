"""Durable manual logbook import job contract.

The Supabase inbox row is the client-visible source of truth. Owner mail is
only redundant operator notification and can never turn an ephemeral upload
into a successful, trackable job.
"""
import base64
import os
import sys

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import app as A


def _client():
    return A.app.test_client()


def _post(client, token, filename, blob, mail_ok=True, store_ok=101,
          monkeypatch=None):
    sent = {}

    def fake_mail(tok, fn, data, note, stored=False):
        sent.update({'token': tok, 'filename': fn, 'bytes': len(data),
                     'note': note, 'stored_flag': stored})
        return mail_ok

    monkeypatch.setattr(A, '_logbook_import_mail', fake_mail)
    monkeypatch.setattr(A, '_logbook_upload_store',
                        lambda tok, fn, data, note: store_ok)
    r = client.post(f'/api/user/logbook/{token}/import-upload', json={
        'filename': filename,
        'data_b64': base64.b64encode(blob).decode(),
    })
    return r, sent


def test_upload_csv_sends_mail_and_acks(monkeypatch):
    A._LOGBOOK_IMPORT_TS.clear()
    r, sent = _post(_client(), 'tok_upload_1', 'LogTenExport.csv',
                    b'Date,From,To\n2019-01-01,FRA,JFK\n', monkeypatch=monkeypatch)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['ok'] is True
    assert r.get_json()['job_id'] == 101
    assert r.get_json()['status'] == 'pending'
    assert sent['filename'] == 'LogTenExport.csv'
    assert sent['bytes'] > 0


def test_upload_ok_when_only_sb_store_succeeds(monkeypatch):
    # Mail down, aber SB-Upload-Store hat die Datei → Upload gilt (durabel).
    A._LOGBOOK_IMPORT_TS.clear()
    r, sent = _post(_client(), 'tok_upload_sb', 'Export.csv', b'a,b,c',
                    mail_ok=False, store_ok=202, monkeypatch=monkeypatch)
    assert r.status_code == 200 and r.get_json()['ok'] is True
    assert r.get_json()['job_id'] == 202
    assert sent['stored_flag'] is True     # Mail weiss vom SB-Store


def test_upload_ok_when_mail_fails_but_durable_job_exists(monkeypatch):
    """Mail is only operator notification; the durable job is the contract."""
    A._LOGBOOK_IMPORT_TS.clear()
    r, _ = _post(_client(), 'tok_upload_2', 'export.csv', b'x,y\n',
                 mail_ok=False, monkeypatch=monkeypatch)
    assert r.status_code == 200
    assert r.get_json()['job_id'] == 101


def test_upload_rejected_without_durable_job_id(monkeypatch):
    A._LOGBOOK_IMPORT_TS.clear()
    r, _ = _post(_client(), 'tok_upload_nojob', 'export.csv', b'x,y\n',
                 mail_ok=True, store_ok=None, monkeypatch=monkeypatch)
    assert r.status_code == 502
    assert r.get_json()['ok'] is False


def test_upload_rejects_unsupported_extension(monkeypatch):
    A._LOGBOOK_IMPORT_TS.clear()
    r, sent = _post(_client(), 'tok_upload_3', 'malware.exe', b'MZ',
                    monkeypatch=monkeypatch)
    assert r.status_code == 415
    assert not sent, 'Mail darf bei abgelehnter Datei nie rausgehen'


def test_upload_rejects_oversize(monkeypatch):
    A._LOGBOOK_IMPORT_TS.clear()
    big = b'0' * (A._LOGBOOK_IMPORT_MAX_BYTES + 1)
    r, sent = _post(_client(), 'tok_upload_4', 'big.csv', big,
                    monkeypatch=monkeypatch)
    assert r.status_code == 413
    assert not sent


def test_upload_throttle_5_per_day(monkeypatch):
    A._LOGBOOK_IMPORT_TS.clear()
    c = _client()
    for i in range(5):
        r, _ = _post(c, 'tok_upload_5', f'e{i}.csv', b'a,b\n',
                     monkeypatch=monkeypatch)
        assert r.status_code == 200, (i, r.get_json())
    r, sent = _post(c, 'tok_upload_5', 'e6.csv', b'a,b\n',
                    monkeypatch=monkeypatch)
    assert r.status_code == 429


def test_upload_no_file_400(monkeypatch):
    A._LOGBOOK_IMPORT_TS.clear()
    r = _client().post('/api/user/logbook/tok_upload_6/import-upload',
                       json={'filename': 'x.csv', 'data_b64': ''})
    assert r.status_code == 400


class _StatusQuery:
    def __init__(self, row):
        self.row = row
        self.filters = []

    def select(self, _fields):
        return self

    def eq(self, field, value):
        self.filters.append((field, value))
        return self

    def limit(self, _count):
        return self

    def execute(self):
        return type('Result', (), {'data': [self.row] if self.row else []})()


def test_upload_status_is_owner_scoped_and_returns_durable_fields(monkeypatch):
    row = {'id': 44, 'status': 'completed', 'filename': 'old.csv',
           'created_at': '2026-08-10T10:00:00Z',
           'completed_at': '2026-08-10T10:02:00Z'}
    monkeypatch.setattr(A, 'SB_AVAILABLE', True)
    query = _StatusQuery(row)
    monkeypatch.setattr(A, 'sb', type('SB', (), {
        'table': staticmethod(lambda _name: query),
    })())
    monkeypatch.setattr(A, '_supabase_execute_with_timeout',
                        lambda _name, fn, timeout_s=8: (fn(), False))
    r = _client().get('/api/user/logbook/tok_upload_status/import-upload/44')
    assert r.status_code == 200, r.get_json()
    assert r.get_json() == {'ok': True, 'job_id': 44, 'status': 'completed',
                            'filename': 'old.csv',
                            'created_at': '2026-08-10T10:00:00Z',
                            'completed_at': '2026-08-10T10:02:00Z'}
    assert query.filters == [('id', 44), ('token', 'tok_upload_status')]


def test_upload_status_hides_foreign_or_missing_job(monkeypatch):
    monkeypatch.setattr(A, 'SB_AVAILABLE', True)
    monkeypatch.setattr(A, 'sb', type('SB', (), {
        'table': staticmethod(lambda _name: _StatusQuery(None)),
    })())
    monkeypatch.setattr(A, '_supabase_execute_with_timeout',
                        lambda _name, fn, timeout_s=8: (fn(), False))
    r = _client().get('/api/user/logbook/tok_upload_status/import-upload/999')
    assert r.status_code == 404
    assert r.get_json()['error'] == 'not_found'
