"""Flugbuch-Import-Upload (Owner-Wunsch 2026-07-22, Thomas-Rust-Anfrage).

User lädt den Export seiner bisherigen Logbuch-App hoch; die Datei geht als
Resend-Mail-Anhang an den Owner. Ehrlichkeit: angenommen NUR wenn die Mail
raus ist (Disk ist ephemer — die Mail IST der Transportweg).
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


def _post(client, token, filename, blob, mail_ok=True, monkeypatch=None):
    sent = {}

    def fake_mail(tok, fn, data, note):
        sent.update({'token': tok, 'filename': fn, 'bytes': len(data),
                     'note': note})
        return mail_ok

    monkeypatch.setattr(A, '_logbook_import_mail', fake_mail)
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
    assert sent['filename'] == 'LogTenExport.csv'
    assert sent['bytes'] > 0


def test_upload_rejected_when_mail_fails(monkeypatch):
    """Mail nicht raus → KEIN ok (kein stilles Schlucken der Datei)."""
    A._LOGBOOK_IMPORT_TS.clear()
    r, _ = _post(_client(), 'tok_upload_2', 'export.csv', b'x,y\n',
                 mail_ok=False, monkeypatch=monkeypatch)
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
