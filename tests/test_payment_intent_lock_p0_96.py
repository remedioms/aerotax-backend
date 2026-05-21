"""P0 #96 — PaymentIntent Replay-Schutz Multi-Container.

Sicherstellt:
- `_try_consume_payment_intent_supabase` atomic-claim via Supabase Primary Key
- Conflict (23505 / duplicate-key) liefert `'already_used'` + existing record
- Supabase-down liefert `'lock_unavailable'` (fail-closed bei paid PI)
- `/api/process` Integration: 1. Request claimed, 2. Request 409
- L1-Cache `_consumed_payment_intents` short-circuits
- Promo + free_retry-Pfade umgehen Lock-Pfad
- Migration-SQL hat PRIMARY KEY constraint
- Lock-Status wird bei Job-Lifecycle-Events upgedatet
"""
import io
import os
import re
import sys
import unittest
from unittest import mock

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

os.environ.setdefault('AEROTAX_EXECUTION_MODE', 'cloud_tasks')
os.environ.setdefault('STRIPE_SECRET_KEY', 'sk_test_dummy')
os.environ.setdefault('STRIPE_WEBHOOK_SECRET', 'whsec_dummy')

import app as app_module
from app import app as flask_app


# ─── Unit-Tests: _try_consume_payment_intent_supabase ───────────────────────

class TestPaymentIntentConsumeHelper(unittest.TestCase):
    """Atomic claim logic — first wins, second gets conflict."""

    def setUp(self):
        self.pi = 'pi_test_' + os.urandom(8).hex()
        self.ref = 'ref_' + os.urandom(4).hex()
        self.job = 'job_' + os.urandom(4).hex()

    def test_payment_intent_consume_atomic_first_wins(self):
        """Erfolgreicher Insert → outcome='claimed'."""
        fake_sb = mock.MagicMock()
        with mock.patch.object(app_module, 'SB_AVAILABLE', True), \
             mock.patch.object(app_module, 'sb', fake_sb):
            outcome, existing = app_module._try_consume_payment_intent_supabase(
                self.pi, ref=self.ref, job_id=self.job
            )
            self.assertEqual(outcome, 'claimed')
            self.assertIsNone(existing)
            # Insert wurde mit korrekten Feldern aufgerufen
            insert_call = fake_sb.table.return_value.insert.call_args
            self.assertIsNotNone(insert_call)
            payload = insert_call[0][0]
            self.assertEqual(payload['payment_intent_id'], self.pi)
            self.assertEqual(payload['ref'], self.ref)
            self.assertEqual(payload['job_id'], self.job)
            self.assertEqual(payload['status'], 'claimed')

    def test_payment_intent_consume_atomic_second_rejected(self):
        """Insert raised 23505 → outcome='already_used' + existing record."""
        existing_row = {
            'payment_intent_id': self.pi,
            'ref': self.ref,
            'job_id': 'existing-job-id-789',
            'status': 'claimed',
        }
        fake_sb = mock.MagicMock()

        def insert_then_select(*a, **kw):
            # Erster Call ist .insert(...)
            raise Exception('duplicate key value violates unique constraint "payment_intent_consumptions_pkey" (23505)')

        fake_sb.table.return_value.insert.return_value.execute.side_effect = insert_then_select
        # Wenn 23505 erkannt → SELECT-Pfad triggert
        select_mock = mock.MagicMock()
        select_mock.data = [existing_row]
        fake_sb.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value = select_mock

        with mock.patch.object(app_module, 'SB_AVAILABLE', True), \
             mock.patch.object(app_module, 'sb', fake_sb):
            outcome, existing = app_module._try_consume_payment_intent_supabase(
                self.pi, ref=self.ref, job_id=self.job
            )
            self.assertEqual(outcome, 'already_used')
            self.assertEqual(existing, existing_row)

    def test_lock_insert_failure_returns_payment_lock_failed(self):
        """Anderer Exception (kein 23505) → outcome='lock_unavailable'."""
        fake_sb = mock.MagicMock()
        fake_sb.table.return_value.insert.return_value.execute.side_effect = (
            Exception('Connection refused — Supabase 503')
        )
        with mock.patch.object(app_module, 'SB_AVAILABLE', True), \
             mock.patch.object(app_module, 'sb', fake_sb):
            outcome, existing = app_module._try_consume_payment_intent_supabase(
                self.pi, ref=self.ref, job_id=self.job
            )
            self.assertEqual(outcome, 'lock_unavailable')
            self.assertIsNone(existing)

    def test_lock_supabase_unavailable_returns_lock_unavailable(self):
        with mock.patch.object(app_module, 'SB_AVAILABLE', False):
            outcome, _ = app_module._try_consume_payment_intent_supabase(
                self.pi, ref=self.ref, job_id=self.job
            )
            self.assertEqual(outcome, 'lock_unavailable')

    def test_lock_no_pi_id_returns_lock_unavailable(self):
        outcome, _ = app_module._try_consume_payment_intent_supabase(
            '', ref=self.ref, job_id=self.job
        )
        self.assertEqual(outcome, 'lock_unavailable')


# ─── Migration-Tests ────────────────────────────────────────────────────────

class TestMigration(unittest.TestCase):
    def test_migration_has_primary_key_on_payment_intent_id(self):
        path = os.path.join(
            ROOT_DIR, 'supabase_migrations',
            '20260514_payment_intent_consumptions.sql'
        )
        self.assertTrue(os.path.isfile(path), f'Migration fehlt: {path}')
        src = open(path).read()
        # Primary Key auf payment_intent_id
        self.assertRegex(
            src,
            r'payment_intent_id\s+text\s+primary\s+key',
            'PRIMARY KEY auf payment_intent_id muss in der Migration sein'
        )
        # RLS enabled
        self.assertIn('enable row level security', src.lower())

    def test_migration_has_status_field(self):
        path = os.path.join(
            ROOT_DIR, 'supabase_migrations',
            '20260514_payment_intent_consumptions.sql'
        )
        src = open(path).read()
        self.assertIn('status', src)
        self.assertIn("'claimed'", src)


# ─── /api/process Integration Tests ─────────────────────────────────────────

class TestProcessPaymentLock(unittest.TestCase):

    def setUp(self):
        self.client = flask_app.test_client()
        self.ref = 'pl-' + os.urandom(4).hex()
        self.pi_id = 'pi_lk_' + os.urandom(8).hex()
        app_module._store[self.ref] = {
            'form':     {},
            'files':    {
                'lsb': [(b'%PDF-lsb', 'lsb.pdf')],
                'se':  [(b'%PDF-se',  'se.pdf')],
                'cas': [(b'%PDF-cas', 'cas.pdf')],
            },
            'paid':     True,
            'expires':  app_module.datetime.utcnow() + app_module.timedelta(hours=4),
            'kind':     'preupload',
        }
        app_module._consumed_payment_intents.clear()
        with app_module._jobs_lock:
            self._jobs_snapshot = set(app_module._jobs.keys())

    def tearDown(self):
        app_module._store.pop(self.ref, None)
        app_module._consumed_payment_intents.pop(self.pi_id, None)
        with app_module._jobs_lock:
            for jid in list(app_module._jobs.keys()):
                if jid not in self._jobs_snapshot:
                    app_module._jobs.pop(jid, None)

    def _process(self, **extras):
        data = {
            'ref':                self.ref,
            'payment_intent_id':  self.pi_id,
            'year':               '2025',
            'base':               'Frankfurt (FRA)',
            'anreise':            'auto',
        }
        data.update(extras)
        return self.client.post('/api/process', data=data,
                                content_type='multipart/form-data')

    def test_parallel_process_same_pi_creates_one_job(self):
        """Multi-Container-Sim: beide Container haben in-memory `paid=True`,
        nur das Supabase-Lock entscheidet. Nach 1. Call wird `_store[ref].paid`
        in unserem Container auf False gesetzt — für Container-B-Sim muss
        es zurückgesetzt werden."""
        # 1. Aufruf: claimed
        with mock.patch.object(app_module, '_save_uploaded_files_supabase',
                                return_value=True), \
             mock.patch.object(app_module, '_try_consume_payment_intent_supabase',
                                return_value=('claimed', None)), \
             mock.patch.object(app_module, '_enqueue_cloud_task'):
            r1 = self._process()
            self.assertIn(r1.status_code, (200, 503),
                f'1st call: expected 200/503, got {r1.status_code}: {r1.get_json()}')
        # Simulate Container B: in-memory `paid` ist auf B noch True; auch
        # L1-Cache `_consumed_payment_intents` ist auf B leer.
        app_module._store[self.ref]['paid'] = True
        app_module._consumed_payment_intents.pop(self.pi_id, None)
        # 2. Aufruf (Container B): Supabase-Lock liefert already_used
        existing = {'payment_intent_id': self.pi_id, 'job_id': 'job-1-existing', 'status': 'claimed'}
        with mock.patch.object(app_module, '_save_uploaded_files_supabase',
                                return_value=True), \
             mock.patch.object(app_module, '_try_consume_payment_intent_supabase',
                                return_value=('already_used', existing)):
            r2 = self._process()
            self.assertEqual(r2.status_code, 409)
            j = r2.get_json()
            self.assertEqual(j['reason_code'], 'PAYMENT_ALREADY_USED')

    def test_parallel_process_same_pi_enqueues_one_task(self):
        """Bei 2. Call (already_used) darf KEIN Cloud-Task enqueued werden."""
        existing = {'payment_intent_id': self.pi_id, 'job_id': 'job-1', 'status': 'claimed'}
        with mock.patch.object(app_module, '_save_uploaded_files_supabase',
                                return_value=True), \
             mock.patch.object(app_module, '_try_consume_payment_intent_supabase',
                                return_value=('already_used', existing)), \
             mock.patch.object(app_module, '_enqueue_cloud_task') as mock_enq:
            r = self._process()
            self.assertEqual(r.status_code, 409)
            mock_enq.assert_not_called()

    def test_process_lock_survives_memory_restart(self):
        """Wenn L1-Cache leer (Container-Restart-Simulation) aber Supabase row
        existiert → 2. Call wird trotzdem rejected via L2-Lock."""
        app_module._consumed_payment_intents.clear()  # Sim Container-Restart
        existing = {'payment_intent_id': self.pi_id, 'job_id': 'job-pre-restart', 'status': 'claimed'}
        with mock.patch.object(app_module, '_save_uploaded_files_supabase',
                                return_value=True), \
             mock.patch.object(app_module, '_try_consume_payment_intent_supabase',
                                return_value=('already_used', existing)):
            r = self._process()
            self.assertEqual(r.status_code, 409)
            j = r.get_json()
            self.assertEqual(j['reason_code'], 'PAYMENT_ALREADY_USED')
            self.assertEqual(j.get('existing_job_id'), 'job-pre-restart')

    def test_payment_already_used_returns_structured_error(self):
        existing = {'payment_intent_id': self.pi_id, 'job_id': 'jx', 'status': 'done'}
        with mock.patch.object(app_module, '_save_uploaded_files_supabase',
                                return_value=True), \
             mock.patch.object(app_module, '_try_consume_payment_intent_supabase',
                                return_value=('already_used', existing)):
            r = self._process()
            j = r.get_json()
            self.assertEqual(j['reason_code'], 'PAYMENT_ALREADY_USED')
            self.assertFalse(j['ok'])
            self.assertIn('user_title', j)
            self.assertIn('user_message', j)
            self.assertIn('Zugangscode', j['user_message'])
            self.assertIsInstance(j['next_actions'], list)
            self.assertTrue(any(a['type'] == 'open_existing' for a in j['next_actions']))
            self.assertEqual(j.get('existing_job_id'), 'jx')
            self.assertEqual(j.get('existing_status'), 'done')

    def test_lock_unavailable_returns_payment_lock_failed(self):
        """Supabase down + paid PI → 503 PAYMENT_LOCK_FAILED (fail-closed)."""
        with mock.patch.object(app_module, '_save_uploaded_files_supabase',
                                return_value=True), \
             mock.patch.object(app_module, '_try_consume_payment_intent_supabase',
                                return_value=('lock_unavailable', None)):
            r = self._process()
            self.assertEqual(r.status_code, 503)
            j = r.get_json()
            self.assertEqual(j['reason_code'], 'PAYMENT_LOCK_FAILED')
            self.assertTrue(j['retryable'])

    def test_promo_flow_not_blocked_by_pi_lock(self):
        """Promo-Code Pfad → kein Lock-Check (pi_id leer/ignoriert)."""
        with mock.patch.dict(os.environ, {'PROMO_CODES': 'PROMOLOCKTEST'}), \
             mock.patch.object(app_module, '_save_uploaded_files_supabase',
                                return_value=True), \
             mock.patch.object(app_module, '_try_consume_payment_intent_supabase') as mock_lock, \
             mock.patch.object(app_module, '_enqueue_cloud_task'):
            r = self.client.post('/api/process', data={
                'ref':        self.ref,
                'promo_code': 'PROMOLOCKTEST',
                'year':       '2025',
                'base':       'Frankfurt (FRA)',
            }, content_type='multipart/form-data')
            # Promo Pfad: kein Lock-Aufruf — promo_code allein reicht
            mock_lock.assert_not_called()
            # Job sollte erstellt sein (200/503/202 alle akzeptabel — wichtig: kein 402/409)
            self.assertNotIn(r.status_code, (402, 409),
                f'Promo darf nicht durch PI-Lock blockiert werden, got {r.status_code}')

    def test_l1_cache_short_circuits_lock(self):
        """Wenn pi_id schon im L1-Cache → kein L2-Supabase-Call nötig."""
        app_module._consumed_payment_intents[self.pi_id] = app_module.datetime.utcnow()
        with mock.patch.object(app_module, '_save_uploaded_files_supabase',
                                return_value=True), \
             mock.patch.object(app_module, '_try_consume_payment_intent_supabase') as mock_l2:
            r = self._process()
            self.assertEqual(r.status_code, 409)
            mock_l2.assert_not_called()
            j = r.get_json()
            self.assertEqual(j['reason_code'], 'PAYMENT_ALREADY_USED')


# ─── Static-Source-Checks ───────────────────────────────────────────────────

class TestSourceInvariants(unittest.TestCase):
    """Static-Checks gegen app.py — wichtige Strukturen müssen erhalten bleiben."""

    @classmethod
    def setUpClass(cls):
        cls.src = open(os.path.join(ROOT_DIR, 'app.py')).read()

    def test_no_in_memory_consumed_payment_intents_required_for_cloud_tasks(self):
        """Source enthält Supabase-Lock-Call vor Job-Creation in /api/process."""
        idx = self.src.find("@app.route('/api/process'")
        end = idx + 30000  # /api/process ist groß
        block = self.src[idx:end]
        self.assertIn('_try_consume_payment_intent_supabase', block,
            'P0 #96: Supabase-Lock-Call muss in /api/process sein')
        # Lock-Check muss vor Job-Creation kommen (uuid.uuid4)
        lock_pos = block.find('_try_consume_payment_intent_supabase')
        # Im neuen Code ist `job_id = str(uuid.uuid4())` direkt davor (gleicher Block)
        # Wichtig: Lock-Check kommt vor PI-Consume in L1-Cache (line 'datetime.utcnow()')
        cache_pos = block.find('_consumed_payment_intents[pi_id] = datetime.utcnow()')
        self.assertLess(lock_pos, cache_pos,
            'Lock-Claim muss VOR L1-Cache-Fill kommen')

    def test_payment_already_used_uses_409(self):
        """409 (Conflict) ist semantisch korrekter HTTP-Code als 402."""
        # Suche nach jsonify({...PAYMENT_ALREADY_USED...}), 409
        pattern = re.compile(
            r"'PAYMENT_ALREADY_USED'.*?\}\)\s*,\s*(\d+)",
            re.DOTALL
        )
        matches = pattern.findall(self.src)
        # Mind. 1 Match muss mit Code 409 enden
        self.assertTrue(any(m == '409' for m in matches),
            f'PAYMENT_ALREADY_USED response sollte mit 409 enden. Matches: {matches}')

    def test_error_codes_have_required_keys(self):
        for code in ('PAYMENT_ALREADY_USED', 'PAYMENT_LOCK_FAILED'):
            ec = app_module.AEROTAX_ERROR_CODES.get(code)
            self.assertIsNotNone(ec, f'{code} fehlt in AEROTAX_ERROR_CODES')
            for k in ('user_title', 'user_message', 'retryable', 'support', 'next_actions'):
                self.assertIn(k, ec, f'{code} fehlt key {k}')


# ─── Lock-Status-Update-Tests ───────────────────────────────────────────────

class TestLockStatusUpdate(unittest.TestCase):

    def test_update_payment_intent_lock_status_calls_supabase(self):
        fake_sb = mock.MagicMock()
        with mock.patch.object(app_module, 'SB_AVAILABLE', True), \
             mock.patch.object(app_module, 'sb', fake_sb):
            app_module._update_payment_intent_lock_status(
                'pi_test', 'done', job_id='job-x'
            )
            update_call = fake_sb.table.return_value.update.call_args
            self.assertIsNotNone(update_call)
            payload = update_call[0][0]
            self.assertEqual(payload['status'], 'done')
            self.assertEqual(payload['job_id'], 'job-x')

    def test_update_silent_when_supabase_unavailable(self):
        """No raise wenn SB unavailable — best-effort."""
        with mock.patch.object(app_module, 'SB_AVAILABLE', False):
            # Should not raise
            app_module._update_payment_intent_lock_status('pi_test', 'done')

    def test_update_swallows_exceptions(self):
        """Update-Fehler darf nie raisen — Job-Lifecycle muss weiter."""
        fake_sb = mock.MagicMock()
        fake_sb.table.return_value.update.return_value.eq.return_value.execute.side_effect = (
            Exception('Supabase 500')
        )
        with mock.patch.object(app_module, 'SB_AVAILABLE', True), \
             mock.patch.object(app_module, 'sb', fake_sb):
            # Should not raise
            app_module._update_payment_intent_lock_status('pi_test', 'failed_retryable')


if __name__ == '__main__':
    unittest.main(verbosity=2)
