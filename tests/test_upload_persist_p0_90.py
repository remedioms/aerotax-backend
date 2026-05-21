"""P0 #90 — Upload-Persistenz darf nie silent failen.

Sicherstellt:
- _save_uploaded_files_supabase raised UploadPersistError statt return False
- /api/upload-files antwortet 503 + structured JSON wenn Persist fail
- /api/process consumed PI nur wenn Files in Supabase persistiert sind
- /api/process erstellt KEIN Job bei pre-persist-Fail
- /api/process enqueued KEIN Cloud Task bei pre-persist-Fail
- Worker liefert UPLOAD_FILES_MISSING (nicht UPLOAD_EXPIRED) wenn ref existiert
  aber Supabase-Load leer
- Safe logging: keine PDF-Bytes, kein Base64, keine Klarnamen in Logs
"""
import io
import os
import re
import sys
import unittest
from unittest import mock

# Stelle sicher, dass die Test gegen die lokale app.py läuft
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# Force cloud_tasks-Mode für Pre-Persist-Tests
os.environ.setdefault('AEROTAX_EXECUTION_MODE', 'cloud_tasks')
# Stripe nicht initialisieren — wir mocken alle Stripe-Calls
os.environ.setdefault('STRIPE_SECRET_KEY', 'sk_test_dummy')
os.environ.setdefault('STRIPE_WEBHOOK_SECRET', 'whsec_dummy')

import app as app_module
from app import app as flask_app


# ─── Unit-Tests: _save_uploaded_files_supabase ──────────────────────────────

class TestSaveUploadedFilesSupabase(unittest.TestCase):
    """Unit-Tests für die persist-Funktion selbst."""

    def setUp(self):
        self.ref = 'test-ref-12345678'
        self.files = {'lsb': [(b'PDFDATA', 'lsb.pdf')]}

    def test_raises_when_supabase_unavailable(self):
        with mock.patch.object(app_module, 'SB_AVAILABLE', False):
            with self.assertRaises(app_module.UploadPersistError) as ctx:
                app_module._save_uploaded_files_supabase(self.ref, self.files)
            self.assertIn('supabase_unavailable', str(ctx.exception))

    def test_raises_when_ref_missing(self):
        with mock.patch.object(app_module, 'SB_AVAILABLE', True):
            with self.assertRaises(app_module.UploadPersistError) as ctx:
                app_module._save_uploaded_files_supabase('', self.files)
            self.assertIn('missing_ref', str(ctx.exception))

    def test_raises_when_files_empty(self):
        with mock.patch.object(app_module, 'SB_AVAILABLE', True):
            with self.assertRaises(app_module.UploadPersistError) as ctx:
                app_module._save_uploaded_files_supabase(self.ref, {})
            self.assertIn('empty_files_dict', str(ctx.exception))

    def test_raises_on_supabase_insert_503(self):
        """Supabase-503 muss raise auslösen, nicht return False."""
        fake_sb = mock.MagicMock()
        fake_sb.table.return_value.insert.return_value.execute.side_effect = (
            Exception('APIError: 503 service unavailable')
        )
        with mock.patch.object(app_module, 'SB_AVAILABLE', True), \
             mock.patch.object(app_module, 'sb', fake_sb):
            with self.assertRaises(app_module.UploadPersistError) as ctx:
                app_module._save_uploaded_files_supabase(self.ref, self.files)
            self.assertIn('supabase_insert_failed', str(ctx.exception))

    def test_returns_true_on_success(self):
        """Happy-path: returnt True."""
        fake_sb = mock.MagicMock()
        with mock.patch.object(app_module, 'SB_AVAILABLE', True), \
             mock.patch.object(app_module, 'sb', fake_sb):
            result = app_module._save_uploaded_files_supabase(self.ref, self.files)
            self.assertTrue(result)

    def test_no_silent_return_false_in_save_uploaded_files_supabase(self):
        """Static-Check: Funktion enthält keinen 'return False'-Pfad mehr.
        Matched nur tatsächliche Statements (eingerückt + Zeilenanfang), nicht
        Docstring-Erwähnungen."""
        src = open(os.path.join(ROOT_DIR, 'app.py')).read()
        m = re.search(
            r'def _save_uploaded_files_supabase\(.*?\):(.*?)(?=\ndef |\nclass )',
            src,
            re.DOTALL,
        )
        self.assertIsNotNone(m, 'Funktion nicht gefunden')
        body = m.group(1)
        # Match Statement: ^<whitespace>return False$ (Zeilenanfang + reines Statement)
        stmt_matches = re.findall(r'^\s+return False\s*$', body, re.MULTILINE)
        self.assertEqual(
            stmt_matches, [],
            f'P0 #90: kein return-False-Statement erlaubt. Found: {stmt_matches}'
        )

    def test_no_except_print_only(self):
        """Static-Check: kein except: print() ohne raise."""
        src = open(os.path.join(ROOT_DIR, 'app.py')).read()
        m = re.search(
            r'def _save_uploaded_files_supabase\(.*?\):(.*?)(?=\ndef |\nclass )',
            src,
            re.DOTALL,
        )
        body = m.group(1)
        # Pattern: `except ...: \n print(...)` ohne nachfolgendes raise innerhalb
        # gleicher Einrückung. Stark vereinfacht: print() darf existieren
        # (logger.warning), aber bare `print(` (statt logger) darf nicht da sein.
        print_lines = [l for l in body.split('\n')
                       if re.match(r'^\s+print\(', l)]
        self.assertEqual(
            print_lines, [],
            f'P0 #90: print() statt logger erlaubt nicht. Found: {print_lines}'
        )


# ─── Logger-Safe-Logging-Check ──────────────────────────────────────────────

class TestLoggerNoRawFileBytes(unittest.TestCase):
    """Stellt sicher, dass Logger keine PDF-Bytes/Base64/Namen ausgibt."""

    def test_logger_no_raw_file_bytes_in_persist(self):
        """Logger-Output darf keine Bytes/Base64 enthalten."""
        ref = 'logsafe-ref-9988'
        files = {'lsb': [(b'%PDF-1.4\n%confidential bytes here\n', 'Max_Mustermann.pdf')]}
        fake_sb = mock.MagicMock()
        with mock.patch.object(app_module, 'SB_AVAILABLE', True), \
             mock.patch.object(app_module, 'sb', fake_sb), \
             mock.patch.object(app_module.app.logger, 'info') as mock_info, \
             mock.patch.object(app_module.app.logger, 'warning') as mock_warn, \
             mock.patch.object(app_module.app.logger, 'error') as mock_err:
            app_module._save_uploaded_files_supabase(ref, files)
            all_calls = (
                [c.args[0] for c in mock_info.call_args_list] +
                [c.args[0] for c in mock_warn.call_args_list] +
                [c.args[0] for c in mock_err.call_args_list]
            )
            joined = ' '.join(all_calls)
            self.assertNotIn('%PDF', joined, 'PDF-Magic-Bytes geloggt')
            self.assertNotIn('confidential', joined, 'Rohe Bytes geloggt')
            self.assertNotIn('Max_Mustermann', joined, 'Klarname geloggt')
            self.assertNotIn('Mustermann', joined, 'Klarname geloggt')
            self.assertNotIn(ref, joined, 'Vollständiger ref geloggt')
            self.assertIn(ref[:8], joined, 'Gekürzter ref muss da sein')

    def test_logger_no_raw_bytes_on_error(self):
        """Bei Fehler darf Exception-Inhalt nicht alles leaken."""
        ref = 'errlog-1234'
        files = {'lsb': [(b'super-secret-pdf-content', 'private_steuer.pdf')]}
        fake_sb = mock.MagicMock()
        fake_sb.table.return_value.insert.return_value.execute.side_effect = (
            Exception('Connection refused — server private_steuer.pdf')
        )
        with mock.patch.object(app_module, 'SB_AVAILABLE', True), \
             mock.patch.object(app_module, 'sb', fake_sb), \
             mock.patch.object(app_module.app.logger, 'error') as mock_err:
            try:
                app_module._save_uploaded_files_supabase(ref, files)
            except app_module.UploadPersistError:
                pass
            all_calls = ' '.join(c.args[0] for c in mock_err.call_args_list)
            self.assertNotIn('super-secret', all_calls)
            self.assertNotIn('private_steuer.pdf', all_calls)
            self.assertIn('Exception', all_calls)  # nur error_type


# ─── Endpoint-Tests: /api/upload-files ──────────────────────────────────────

class TestUploadFilesEndpoint(unittest.TestCase):
    """Tests für /api/upload-files Endpoint mit gemocktem Supabase."""

    def setUp(self):
        self.client = flask_app.test_client()
        # Saubere _store
        self.ref = 'test-up-' + os.urandom(4).hex()
        app_module._store[self.ref] = {
            'form':     {},
            'files':    {},
            'paid':     False,
            'expires':  app_module.datetime.utcnow() + app_module.timedelta(hours=4),
            'kind':     'preupload',
        }

    def tearDown(self):
        app_module._store.pop(self.ref, None)

    def _upload(self):
        return self.client.post('/api/upload-files', data={
            'ref': self.ref,
            'lsb': (io.BytesIO(b'%PDF-test-lsb'), 'lsb.pdf'),
            'se':  (io.BytesIO(b'%PDF-test-se'),  'se.pdf'),
            'cas': (io.BytesIO(b'%PDF-test-cas'), 'cas.pdf'),
        }, content_type='multipart/form-data')

    def test_upload_persist_failure_blocks_upload_files(self):
        """Wenn Supabase-Persist fail → /api/upload-files antwortet 503."""
        with mock.patch.object(app_module, '_save_uploaded_files_supabase',
                                side_effect=app_module.UploadPersistError('mocked_fail')):
            r = self._upload()
            self.assertEqual(r.status_code, 503)

    def test_upload_persist_failure_returns_structured_503(self):
        """503 muss reason_code, user_message, retryable, next_actions liefern."""
        with mock.patch.object(app_module, '_save_uploaded_files_supabase',
                                side_effect=app_module.UploadPersistError('mocked_fail')):
            r = self._upload()
            j = r.get_json()
            self.assertEqual(j['reason_code'], 'UPLOAD_PERSIST_FAILED')
            self.assertFalse(j['ok'])
            self.assertTrue(j['retryable'])
            self.assertIn('user_title', j)
            self.assertIn('user_message', j)
            self.assertIn('Zahlung', j['user_message'])
            self.assertIsInstance(j['next_actions'], list)
            self.assertTrue(len(j['next_actions']) >= 1)

    def test_upload_persist_success_unchanged(self):
        """Happy-path: 200 + status:ok."""
        with mock.patch.object(app_module, '_save_uploaded_files_supabase',
                                return_value=True):
            r = self._upload()
            self.assertEqual(r.status_code, 200)
            j = r.get_json()
            self.assertEqual(j['status'], 'ok')
            self.assertGreater(j['count'], 0)


# ─── Endpoint-Tests: /api/process ───────────────────────────────────────────

class TestProcessPrePersist(unittest.TestCase):
    """Tests für /api/process Pre-Persist VOR PI-Consume.

    2026-05-19 Modernisierung: Pre-Persist greift nur wenn
    AEROTAX_EXECUTION_MODE='cloud_tasks'. Test patcht jetzt explizit.
    """

    def setUp(self):
        self.client = flask_app.test_client()
        self.ref = 'test-proc-' + os.urandom(4).hex()
        self.pi_id = 'pi_test_' + os.urandom(8).hex()
        # Files in _store (Pflicht-Set lsb/se/cas)
        app_module._store[self.ref] = {
            'form':     {},
            'files':    {
                'lsb': [(b'%PDF-lsb', 'lsb.pdf')],
                'se':  [(b'%PDF-se',  'se.pdf')],
                'cas': [(b'%PDF-cas', 'cas.pdf')],
            },
            'paid':     True,  # Webhook hat schon ack
            'expires':  app_module.datetime.utcnow() + app_module.timedelta(hours=4),
            'kind':     'preupload',
        }
        # Pre-Persist wird nur im cloud_tasks-Mode ausgeführt
        self._original_exec_mode = app_module.AEROTAX_EXECUTION_MODE
        app_module.AEROTAX_EXECUTION_MODE = 'cloud_tasks'
        # Clear PI cache + Job cache
        app_module._consumed_payment_intents.clear()
        with app_module._jobs_lock:
            self._jobs_snapshot = set(app_module._jobs.keys())

    def tearDown(self):
        app_module._store.pop(self.ref, None)
        app_module._consumed_payment_intents.pop(self.pi_id, None)
        app_module.AEROTAX_EXECUTION_MODE = self._original_exec_mode
        # neu hinzugekommene Jobs aufräumen
        with app_module._jobs_lock:
            for jid in list(app_module._jobs.keys()):
                if jid not in self._jobs_snapshot:
                    app_module._jobs.pop(jid, None)

    def _process(self):
        return self.client.post('/api/process', data={
            'ref':                self.ref,
            'payment_intent_id':  self.pi_id,
            'year':               '2025',
            'base':               'Frankfurt (FRA)',
            'anreise':            'auto',
        }, content_type='multipart/form-data')

    def test_process_prepersist_failure_does_not_consume_pi(self):
        """Wenn pre-persist fail → PI bleibt NICHT in _consumed_payment_intents."""
        with mock.patch.object(app_module, '_save_uploaded_files_supabase',
                                side_effect=app_module.UploadPersistError('mocked')):
            r = self._process()
            self.assertEqual(r.status_code, 503)
            self.assertNotIn(self.pi_id, app_module._consumed_payment_intents,
                'PI darf bei pre-persist-fail nicht consumed sein')

    def test_process_prepersist_failure_returns_structured_error(self):
        with mock.patch.object(app_module, '_save_uploaded_files_supabase',
                                side_effect=app_module.UploadPersistError('mocked')):
            r = self._process()
            j = r.get_json()
            self.assertEqual(j['reason_code'], 'UPLOAD_PERSIST_FAILED')
            self.assertFalse(j['ok'])

    def test_process_prepersist_failure_does_not_create_job(self):
        """Job-Dict darf bei pre-persist-fail nicht angelegt sein."""
        with mock.patch.object(app_module, '_save_uploaded_files_supabase',
                                side_effect=app_module.UploadPersistError('mocked')):
            r = self._process()
            self.assertEqual(r.status_code, 503)
            with app_module._jobs_lock:
                new_jobs = [j for j in app_module._jobs.keys()
                            if j not in self._jobs_snapshot]
            self.assertEqual(new_jobs, [],
                'Kein Job darf bei pre-persist-fail entstehen')

    def test_process_prepersist_failure_does_not_enqueue_task(self):
        """Cloud-Task-Enqueue darf nicht aufgerufen werden bei pre-persist-fail."""
        with mock.patch.object(app_module, '_save_uploaded_files_supabase',
                                side_effect=app_module.UploadPersistError('mocked')), \
             mock.patch.object(app_module, '_enqueue_cloud_task') as mock_enqueue:
            r = self._process()
            self.assertEqual(r.status_code, 503)
            mock_enqueue.assert_not_called()

    def test_promo_process_prepersist_failure_does_not_start_job(self):
        """Promo-Pfad: kein Job bei pre-persist-fail."""
        # Promo-Code via env
        with mock.patch.dict(os.environ, {'PROMO_CODES': 'TESTPROMO123'}), \
             mock.patch.object(app_module, '_save_uploaded_files_supabase',
                                side_effect=app_module.UploadPersistError('mocked')):
            r = self.client.post('/api/process', data={
                'ref':         self.ref,
                'promo_code':  'TESTPROMO123',
                'year':        '2025',
                'base':        'Frankfurt (FRA)',
            }, content_type='multipart/form-data')
            self.assertEqual(r.status_code, 503)
            with app_module._jobs_lock:
                new_jobs = [j for j in app_module._jobs.keys()
                            if j not in self._jobs_snapshot]
            self.assertEqual(new_jobs, [])


# ─── Worker-Tests ────────────────────────────────────────────────────────────

class TestWorkerMissingFiles(unittest.TestCase):
    """Worker-Pfad bei fehlenden uploaded_files."""

    def setUp(self):
        self.client = flask_app.test_client()
        self.job_id = 'jobtest-' + os.urandom(4).hex()
        self.ref = 'wref-' + os.urandom(4).hex()
        with app_module._jobs_lock:
            app_module._jobs[self.job_id] = {
                'status':   'queued',
                'progress': 0,
                'form':     {'ref': self.ref, 'year': 2025},
                'session_token': 'test-session-tok',
            }
        self._jobs_snapshot_keys = list(app_module._jobs.keys())

    def tearDown(self):
        with app_module._jobs_lock:
            app_module._jobs.pop(self.job_id, None)

    def test_worker_missing_uploaded_files_has_clear_reason_code(self):
        """Worker liefert UPLOAD_FILES_MISSING (nicht UPLOAD_EXPIRED)."""
        with mock.patch.object(app_module, '_load_uploaded_files_supabase',
                                return_value={}), \
             mock.patch.object(app_module, '_verify_internal_task_auth',
                                return_value=True):
            r = self.client.post('/api/internal/process-job',
                                  json={'job_id': self.job_id, 'attempt': 1},
                                  headers={'Authorization': 'Bearer fake-oidc'})
            j = r.get_json() or {}
            # Endpoint returnt 200 + reason_code (Cloud-Tasks-Convention)
            self.assertEqual(j.get('reason_code'), 'UPLOAD_FILES_MISSING',
                f'Expected UPLOAD_FILES_MISSING, got: {j}')


# ─── AEROTAX_ERROR_CODES-Tests ──────────────────────────────────────────────

class TestErrorCodes(unittest.TestCase):
    def test_user_message_upload_persist_failed(self):
        ec = app_module.AEROTAX_ERROR_CODES.get('UPLOAD_PERSIST_FAILED')
        self.assertIsNotNone(ec)
        self.assertIn('Zahlung', ec['user_message'])
        self.assertTrue(ec['retryable'])
        self.assertIsInstance(ec.get('next_actions'), list)

    def test_user_message_upload_files_missing(self):
        ec = app_module.AEROTAX_ERROR_CODES.get('UPLOAD_FILES_MISSING')
        self.assertIsNotNone(ec)
        self.assertIn('Support', ec['user_message'])
        self.assertTrue(ec['retryable'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
