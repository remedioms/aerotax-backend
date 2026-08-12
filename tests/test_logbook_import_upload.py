"""Durable manual logbook import job contract.

The Supabase inbox row is the client-visible source of truth. Owner mail is
only redundant operator notification and can never turn an ephemeral upload
into a successful, trackable job.
"""
import base64
import datetime as _dt
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


def _capture_pushes(monkeypatch):
    """Sammelt jeden Outbox-Auftrag der Route (Enqueue statt echtem Versand)."""
    pushes = []

    def fake_notify(tok, title, body, data=None, thread_id=None, badge=None,
                    category=None, idempotency_key=None, actor_token=None):
        pushes.append({'token': tok, 'title': title, 'body': body,
                       'data': data, 'idempotency_key': idempotency_key})
        return 'outbox-1'

    monkeypatch.setattr(A, '_push_notify_async', fake_notify)
    return pushes


def _outbox_key(push):
    """Der echte Dedupe-Schlüssel der Outbox — inkl. Titel/Body/data-Hash."""
    return A._push_outbox_key(push['token'], push['title'], push['body'],
                              push['data'], None, None, None,
                              idempotency_key=push['idempotency_key'])


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


def test_upload_throttle_per_day_and_honest_wait(monkeypatch):
    """Deckel 30/24h (5 war fürs Nachtragen alter Monate zu knapp) und der
    429 sagt, WIE LANGE zu warten ist — der Client bricht sonst fail-fast ab,
    ohne dem User eine Wiedervorlage nennen zu können."""
    assert A._LOGBOOK_IMPORT_MAX_PER_DAY == 30
    A._LOGBOOK_IMPORT_TS.clear()
    c = _client()
    for i in range(A._LOGBOOK_IMPORT_MAX_PER_DAY):
        r, _ = _post(c, 'tok_upload_5', f'e{i}.csv', b'a,b\n',
                     monkeypatch=monkeypatch)
        assert r.status_code == 200, (i, r.get_json())
    r, _sent = _post(c, 'tok_upload_5', 'e31.csv', b'a,b\n',
                     monkeypatch=monkeypatch)
    assert r.status_code == 429
    body = r.get_json()
    assert body['error'] == 'too_many_uploads'
    assert body['limit'] == 30
    # Wartezeit ist echt (der älteste Upload fällt nach 24h aus dem Fenster)
    # und steht auch als Standard-Header drin.
    assert 0 < body['retry_after_s'] <= 86400
    assert body['retry_after_min'] >= 1
    assert r.headers['Retry-After'] == str(body['retry_after_s'])
    assert '30' in body['message']


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


# ── Ankunfts-Push („angekommen — wird verarbeitet") ────────────────────────

def test_accepted_upload_enqueues_one_short_arrival_push(monkeypatch):
    A._LOGBOOK_IMPORT_TS.clear()
    pushes = _capture_pushes(monkeypatch)
    r, _ = _post(_client(), 'tok_push_1', 'Export.csv', b'a,b\n',
                 monkeypatch=monkeypatch)
    assert r.status_code == 200
    assert len(pushes) == 1
    push = pushes[0]
    assert push['token'] == 'tok_push_1'
    assert push['title'] == 'Flugbuch-Import angekommen'
    assert push['body'] == 'Wird verarbeitet.'
    assert push['data'] == {
        'type': 'logbook_import_received',
        'localization_key': 'logbook_import_received',
        'deep_link': 'aerox://more/logbook',
    }
    # Kein datei-spezifisches Feld im Payload: job_id im `data` würde den
    # Outbox-Hash pro Datei ändern und die Dedupe still aushebeln.
    assert 'job_id' not in push['data']
    assert push['idempotency_key'].startswith(
        'logbook-import-received:tok_push_1:')


def test_arrival_push_is_localized_for_all_supported_languages():
    copy = A._PUSH_SYSTEM_COPY['logbook_import_received']
    assert set(copy) == set(A._PUSH_LANGUAGES)
    for lang, (title, body) in copy.items():
        assert title and body, lang
        # „was kurzes" (Owner): Ankunft ist eine Zeile, kein Aufsatz.
        assert len(title) <= 60 and len(body) <= 60, lang
    data = {'type': 'logbook_import_received',
            'localization_key': 'logbook_import_received'}
    assert A._push_localize_system_copy(
        'Flugbuch-Import angekommen', 'Wird verarbeitet.', data, 'en') == (
        'Logbook import received', 'Processing now.')


def test_failure_push_copy_is_short_and_actionable_in_all_languages():
    """Der Wächter pusht bei `failed` nur den Schlüssel — der Text kommt hier
    aus _PUSH_SYSTEM_COPY (siehe logbook_watchdog._push_failed)."""
    copy = A._PUSH_SYSTEM_COPY['logbook_import_failed']
    assert set(copy) == set(A._PUSH_LANGUAGES)
    assert copy['de'] == ('Flugbuch-Import fehlgeschlagen',
                          'Bitte lade die Datei noch einmal hoch.')
    for lang, (title, body) in copy.items():
        assert title and body, lang
        assert len(title) <= 60 and len(body) <= 60, lang
    data = {'type': 'logbook_import_failed',
            'localization_key': 'logbook_import_failed'}
    assert A._push_localize_system_copy(*copy['de'], data, 'fr') == copy['fr']
    # Der Fertig-Push bleibt unangetastet — drei Zustände, drei Texte.
    assert set(A._PUSH_SYSTEM_COPY['logbook_import_completed']) == set(
        A._PUSH_LANGUAGES)


def test_batch_of_files_dedupes_to_a_single_arrival_push(monkeypatch):
    """Fünf Monatsübersichten in einem Rutsch = EIN Push, nicht fünf."""
    A._LOGBOOK_IMPORT_TS.clear()
    pushes = _capture_pushes(monkeypatch)
    # Uhr festnageln: sonst hinge der Test daran, ob die echte Uhr während des
    # Laufs zufällig über eine Fenstergrenze springt.
    real_push = A._logbook_import_received_push
    fixed = _dt.datetime(2026, 8, 12, 10, 3, tzinfo=_dt.timezone.utc)
    monkeypatch.setattr(A, '_logbook_import_received_push',
                        lambda tok, now=None: real_push(tok, now=fixed))
    c = _client()
    for i in range(5):
        r, _ = _post(c, 'tok_push_batch', f'monat{i}.csv', b'a,b\n',
                     monkeypatch=monkeypatch)
        assert r.status_code == 200, (i, r.get_json())
    assert len(pushes) == 5, 'jede Datei stellt in die Outbox ein …'
    # … und die Outbox liefert genau EINEN aus: identischer Idempotenz-Key.
    assert len({p['idempotency_key'] for p in pushes}) == 1
    assert len({_outbox_key(p) for p in pushes}) == 1


def test_arrival_push_key_is_per_user_and_per_upload_session():
    """10-Minuten-Fenster: ein Schub = ein Push, ein späterer Upload meldet
    sich WIEDER (Owner 12.08.: „nicht jede Stunde eine Push")."""
    base = _dt.datetime(2026, 8, 12, 10, 2, tzinfo=_dt.timezone.utc)
    keys = {}
    for label, tok, now in (
            ('a_session', 'tok_a', base),
            ('a_session_late', 'tok_a', base.replace(minute=9, second=59)),
            ('a_next_session', 'tok_a', base + _dt.timedelta(minutes=10)),
            ('a_afternoon', 'tok_a', base + _dt.timedelta(hours=4)),
            ('b_session', 'tok_b', base)):
        captured = []
        original = A._push_notify_async
        A._push_notify_async = (
            lambda tok_, title, body, data=None, thread_id=None, badge=None,
            category=None, idempotency_key=None, actor_token=None:
            captured.append(idempotency_key))
        try:
            A._logbook_import_received_push(tok, now=now)
        finally:
            A._push_notify_async = original
        keys[label] = captured[0]
    assert keys['a_session'] == keys['a_session_late'] == (
        'logbook-import-received:tok_a:2026-08-12-1000')
    # Zweiter Upload später → eigener Push, nicht verschluckt.
    assert keys['a_next_session'] == (
        'logbook-import-received:tok_a:2026-08-12-1010')
    assert keys['a_afternoon'] == (
        'logbook-import-received:tok_a:2026-08-12-1400')
    assert keys['b_session'] == 'logbook-import-received:tok_b:2026-08-12-1000'


def test_rejected_uploads_never_push(monkeypatch):
    A._LOGBOOK_IMPORT_TS.clear()
    pushes = _capture_pushes(monkeypatch)
    c = _client()
    # Falsches Format …
    assert _post(c, 'tok_push_rej', 'malware.exe', b'MZ',
                 monkeypatch=monkeypatch)[0].status_code == 415
    # … zu groß …
    assert _post(c, 'tok_push_rej', 'big.csv',
                 b'0' * (A._LOGBOOK_IMPORT_MAX_BYTES + 1),
                 monkeypatch=monkeypatch)[0].status_code == 413
    # … keine Datei …
    assert c.post('/api/user/logbook/tok_push_rej/import-upload',
                  json={'filename': 'x.csv', 'data_b64': ''}).status_code == 400
    # … und kein durabler Job (SB-Store down).
    assert _post(c, 'tok_push_rej', 'export.csv', b'x,y\n', store_ok=None,
                 monkeypatch=monkeypatch)[0].status_code == 502
    assert pushes == [], 'abgelehnter Upload darf nie „angekommen" pushen'


def test_arrival_push_failure_never_breaks_the_upload(monkeypatch):
    A._LOGBOOK_IMPORT_TS.clear()

    def boom(*_a, **_kw):
        raise RuntimeError('outbox down')

    monkeypatch.setattr(A, '_push_notify_async', boom)
    r, sent = _post(_client(), 'tok_push_boom', 'export.csv', b'a,b\n',
                    monkeypatch=monkeypatch)
    assert r.status_code == 200 and r.get_json()['job_id'] == 101
    assert sent['filename'] == 'export.csv'
