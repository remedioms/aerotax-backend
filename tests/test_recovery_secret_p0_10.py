"""P0 #10 (P1 nach Triage) — RECOVERY_SECRET default empty.

Sicherstellt:
- Boot-Check failed wenn RECOVERY_SECRET fehlt in Production
- Boot-Check toleriert nur mit AEROTAX_ALLOW_BOOT_WITHOUT_KEY=1
- Boot-Check failed wenn Secret zu kurz (<32 chars)
- _recovery_pepper() returnt Wert wenn gesetzt
- _recovery_pepper() raised in Production wenn leer
- Alle 3 Use-Sites nutzen Helper (kein direkter env-read mehr)
- Admin-Endpoint rejected wrong/missing token (401)
- Admin-Endpoint accepts korrekten Token (200)
- Secret wird nirgendwo geloggt
"""
import os
import re
import sys
import unittest
from unittest import mock

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# conftest.py setzt AEROTAX_ALLOW_BOOT_WITHOUT_KEY=1 vor Test-Import
import app as app_module
from app import app as flask_app


# ─── Boot-Check ─────────────────────────────────────────────────────────────

class TestBootCheck(unittest.TestCase):

    def test_boot_fails_without_recovery_secret_in_production(self):
        """Production-Mode (kein flag, kein secret) → raise."""
        with mock.patch.dict(os.environ, {
            'AEROTAX_ALLOW_BOOT_WITHOUT_KEY': '',
            'RECOVERY_SECRET': '',
        }, clear=False):
            with self.assertRaises(RuntimeError) as ctx:
                app_module._validate_recovery_secret_on_boot()
            self.assertIn('RECOVERY_SECRET', str(ctx.exception))
            self.assertIn('missing', str(ctx.exception))

    def test_boot_allows_missing_secret_only_with_flag(self):
        """AEROTAX_ALLOW_BOOT_WITHOUT_KEY=1 → kein raise."""
        with mock.patch.dict(os.environ, {
            'AEROTAX_ALLOW_BOOT_WITHOUT_KEY': '1',
            'RECOVERY_SECRET': '',
        }, clear=False):
            try:
                app_module._validate_recovery_secret_on_boot()  # no raise
            except RuntimeError:
                self.fail('Boot should not raise with flag=1 + missing secret')

    def test_boot_fails_if_secret_too_short(self):
        """Secret mit <32 chars → raise auch ohne flag."""
        with mock.patch.dict(os.environ, {
            'AEROTAX_ALLOW_BOOT_WITHOUT_KEY': '',
            'RECOVERY_SECRET': 'a' * 10,
        }, clear=False):
            with self.assertRaises(RuntimeError) as ctx:
                app_module._validate_recovery_secret_on_boot()
            self.assertIn('too_short', str(ctx.exception))

    def test_boot_passes_with_valid_secret(self):
        """Secret >=32 chars → no raise auch ohne flag."""
        with mock.patch.dict(os.environ, {
            'AEROTAX_ALLOW_BOOT_WITHOUT_KEY': '',
            'RECOVERY_SECRET': 'x' * 64,
        }, clear=False):
            try:
                app_module._validate_recovery_secret_on_boot()
            except RuntimeError:
                self.fail('Boot should pass with 64-char secret')

    def test_boot_short_secret_with_flag_only_warns(self):
        """flag=1 + zu kurz → kein raise, nur warning."""
        with mock.patch.dict(os.environ, {
            'AEROTAX_ALLOW_BOOT_WITHOUT_KEY': '1',
            'RECOVERY_SECRET': 'short',
        }, clear=False):
            with mock.patch.object(app_module.app.logger, 'warning') as mw:
                app_module._validate_recovery_secret_on_boot()
                mw.assert_called_once()


# ─── Pepper-Helper ──────────────────────────────────────────────────────────

class TestRecoveryPepper(unittest.TestCase):

    def test_pepper_returns_value_when_set(self):
        with mock.patch.dict(os.environ, {
            'RECOVERY_SECRET': 'this-is-a-test-secret-of-sufficient-length-xx',
        }, clear=False):
            self.assertEqual(
                app_module._recovery_pepper(),
                'this-is-a-test-secret-of-sufficient-length-xx'
            )

    def test_pepper_raises_when_missing_in_production(self):
        """Kein secret + kein flag → raise."""
        with mock.patch.dict(os.environ, {
            'AEROTAX_ALLOW_BOOT_WITHOUT_KEY': '',
            'RECOVERY_SECRET': '',
        }, clear=False):
            with self.assertRaises(RuntimeError) as ctx:
                app_module._recovery_pepper()
            self.assertIn('not configured', str(ctx.exception))

    def test_pepper_allows_empty_in_test_mode(self):
        """Kein secret + flag=1 → return empty string, no raise."""
        with mock.patch.dict(os.environ, {
            'AEROTAX_ALLOW_BOOT_WITHOUT_KEY': '1',
            'RECOVERY_SECRET': '',
        }, clear=False):
            # Sollte nicht raisen
            self.assertEqual(app_module._recovery_pepper(), '')


# ─── Static-Source-Checks ───────────────────────────────────────────────────

class TestSourceInvariants(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.src = open(os.path.join(ROOT_DIR, 'app.py')).read()

    def test_no_direct_env_read_for_recovery_secret_outside_helpers(self):
        """Alle anderen Stellen müssen _recovery_pepper() nutzen — kein
        direkter `os.environ.get('RECOVERY_SECRET','')` mehr außer in
        _validate_recovery_secret_on_boot und _recovery_pepper selbst."""
        # Pattern: os.environ.get('RECOVERY_SECRET', '...') oder mit Default
        pattern = re.compile(r"os\.environ\.get\(\s*['\"]RECOVERY_SECRET['\"]")
        matches = [m.start() for m in pattern.finditer(self.src)]
        # Erlaubte Stellen: _validate_recovery_secret_on_boot + _recovery_pepper
        # Beide nutzen das Pattern jeweils 1×
        self.assertEqual(
            len(matches), 2,
            f'P0 #10: expected exactly 2 direct env-reads (in helpers only), '
            f'got {len(matches)}. Use _recovery_pepper() in all other places.'
        )
        # Verifiziere: beide Vorkommen sind innerhalb der Helper-Funktionen.
        # Schaue auf den letzten `def ...` davor (gibt es immer in Python).
        for m_pos in matches:
            line_no = self.src.count('\n', 0, m_pos) + 1
            context_before = self.src[:m_pos]
            # Letzte def-Definition vor dem Match
            last_def = re.findall(r'def (\w+)\s*\(', context_before)
            self.assertTrue(last_def, f'No def before line {line_no}')
            enclosing_fn = last_def[-1]
            self.assertIn(
                enclosing_fn,
                ('_validate_recovery_secret_on_boot', '_recovery_pepper'),
                f'Direct env-read at line {line_no} is in `{enclosing_fn}`, '
                f'must be only in the two helper functions'
            )

    def test_boot_check_called_at_import(self):
        """Source ruft _validate_recovery_secret_on_boot() beim Import auf."""
        # Suche nach top-level call (am Zeilenanfang ohne Einrückung)
        self.assertRegex(
            self.src,
            r'\n_validate_recovery_secret_on_boot\(\)\s*\n',
            'Boot-Check muss beim Import time aufgerufen werden'
        )


# ─── Admin-Endpoint-Tests ───────────────────────────────────────────────────

class TestAdminEndpoint(unittest.TestCase):

    def setUp(self):
        self.client = flask_app.test_client()

    def test_admin_rejects_missing_token(self):
        """Ohne X-Admin-Token → 401."""
        r = self.client.get('/api/admin/support-list')
        self.assertEqual(r.status_code, 401)

    def test_admin_rejects_wrong_token(self):
        """Falscher X-Admin-Token → 401."""
        with mock.patch.dict(os.environ, {
            'RECOVERY_SECRET': 'correct-secret-of-sufficient-length-32x'
        }, clear=False):
            r = self.client.get('/api/admin/support-list',
                                headers={'X-Admin-Token': 'wrong-token'})
            self.assertEqual(r.status_code, 401)

    def test_admin_accepts_valid_token(self):
        """Korrekter X-Admin-Token → 200 (oder zumindest nicht 401)."""
        secret = 'correct-secret-of-sufficient-length-32x'
        with mock.patch.dict(os.environ, {
            'RECOVERY_SECRET': secret
        }, clear=False):
            r = self.client.get('/api/admin/support-list',
                                headers={'X-Admin-Token': secret})
            self.assertNotEqual(r.status_code, 401,
                f'Valid token sollte nicht 401 sein, got: {r.status_code}')

    def test_admin_rejects_when_secret_unset_in_test_mode(self):
        """Im test-mode ohne secret: pepper returns ''; admin-check `not expected`
        bleibt fail-closed (401)."""
        with mock.patch.dict(os.environ, {
            'AEROTAX_ALLOW_BOOT_WITHOUT_KEY': '1',
            'RECOVERY_SECRET': '',
        }, clear=False):
            r = self.client.get('/api/admin/support-list',
                                headers={'X-Admin-Token': 'anything'})
            self.assertEqual(r.status_code, 401,
                'Admin muss fail-closed sein wenn pepper leer (auch in test-mode)')


# ─── Logging-Safety ──────────────────────────────────────────────────────────

class TestSecretNotLogged(unittest.TestCase):

    def test_boot_check_logs_no_secret(self):
        """Boot-Check (warning-Pfad) darf nie den Secret-Wert loggen.
        Wir testen mit einem unique-Token-Wert, der nicht im Status-Vokabular
        vorkommt (z.B. 'CANARYTOKEN12345' — kein substring von 'missing'/
        'too_short'/'test-mode')."""
        canary = 'CANARYxyzPEPPERvalueABCDEFG'  # 27 chars (< 32, triggert warning)
        with mock.patch.dict(os.environ, {
            'AEROTAX_ALLOW_BOOT_WITHOUT_KEY': '1',
            'RECOVERY_SECRET': canary,
        }, clear=False):
            with mock.patch.object(app_module.app.logger, 'warning') as mw:
                app_module._validate_recovery_secret_on_boot()
                logged = ' '.join(c.args[0] for c in mw.call_args_list)
                # Der canary-Wert darf NIE in Logs auftauchen
                self.assertNotIn(canary, logged,
                    f'Secret value leaked into log: {logged}')
                # Auch kein Prefix/Suffix
                self.assertNotIn(canary[:8], logged)
                self.assertNotIn(canary[-8:], logged)
                # Generell kein Wert nach "RECOVERY_SECRET=" sichtbar
                self.assertNotRegex(logged, r'RECOVERY_SECRET\s*=\s*[A-Za-z0-9]+')

    def test_pepper_helper_does_not_log(self):
        """_recovery_pepper darf NIE etwas loggen (auch nicht beim raise)."""
        with mock.patch.dict(os.environ, {
            'AEROTAX_ALLOW_BOOT_WITHOUT_KEY': '',
            'RECOVERY_SECRET': '',
        }, clear=False):
            with mock.patch.object(app_module.app.logger, 'info') as mi, \
                 mock.patch.object(app_module.app.logger, 'warning') as mw, \
                 mock.patch.object(app_module.app.logger, 'error') as me:
                try:
                    app_module._recovery_pepper()
                except RuntimeError:
                    pass
                mi.assert_not_called()
                mw.assert_not_called()
                me.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
