"""P0 #95 — PII bleibt persistiert (kein [redacted].pdf nach Restart).

User-Schmerz: PDF heißt `[redacted].pdf` nach Container-Restart, Chat zeigt
„Hallo [redacted]" — weil _save_job_to_disk PII redactete vor Persist.

Fix: PII bleibt persistent in Supabase/Disk (sicher: Service-Role-only,
RLS, encryption at rest, Cleanup nach 24h). _redact_pii bleibt aktiv für
Logs/Audit/Stdout — nur Persist-Path nicht mehr.
"""
import os
import re
import sys
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


class TestPiiPersist(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.src = open(os.path.join(ROOT_DIR, 'app.py')).read()

    def test_save_job_does_not_redact_pii(self):
        """_save_job_to_disk redacted KEIN PII mehr."""
        m = re.search(
            r'def _save_job_to_disk\([^)]*\):(.*?)(?=\ndef |\nclass )',
            self.src,
            re.DOTALL
        )
        self.assertIsNotNone(m, '_save_job_to_disk nicht gefunden')
        body = m.group(1)
        # Kein _redact_pii-Call mehr im body
        self.assertNotRegex(
            body, r'^\s*j_safe\s*=\s*_redact_pii',
            'P0 #95: _save_job_to_disk darf KEIN _redact_pii mehr aufrufen — '
            'sonst [redacted].pdf nach Restart'
        )

    def test_save_job_persists_data_field(self):
        """_save_job_to_disk persistiert weiterhin via sb.table('jobs').upsert."""
        m = re.search(
            r'def _save_job_to_disk\([^)]*\):(.*?)(?=\ndef |\nclass )',
            self.src,
            re.DOTALL
        )
        body = m.group(1)
        self.assertIn("sb.table('jobs').upsert", body,
            'Supabase-Upsert muss erhalten bleiben')
        self.assertIn("'data':       j_safe", body)

    def test_redact_pii_helper_still_exists(self):
        """_redact_pii bleibt für Logs/Audit verfügbar — andere Caller dürfen weiter nutzen."""
        self.assertIn('def _redact_pii(obj):', self.src,
            '_redact_pii muss für Logs/Audit erhalten bleiben')

    def test_audit_log_still_redacts_pii(self):
        """Audit-Log (`_audit` / stdout-print) ruft weiter _redact_pii."""
        # Suche im Bereich `_safe_data = _redact_pii(data)` (Z.~1887 alt)
        self.assertRegex(
            self.src,
            r'_safe_data\s*=\s*_redact_pii',
            'Audit-Log muss weiter PII redacten in stdout-print-Pfad'
        )

    def test_pii_keys_set_unchanged(self):
        """_PII_KEYS Set existiert weiter (mit Standard-DSGVO-Keys)."""
        # _PII_KEYS Block muss vorhanden bleiben
        self.assertIn('_PII_KEYS = {', self.src)
        self.assertIn("'name'", self.src)
        self.assertIn("'vorname'", self.src)
        self.assertIn("'nachname'", self.src)

    def test_save_uses_app_logger_not_print(self):
        """_save_job_to_disk nutzt app.logger statt print für Error-Cases."""
        m = re.search(
            r'def _save_job_to_disk\([^)]*\):(.*?)(?=\ndef |\nclass )',
            self.src,
            re.DOTALL
        )
        body = m.group(1)
        # Kein nackter print() mehr für Error-Cases
        print_lines = re.findall(r'^\s+print\(', body, re.MULTILINE)
        self.assertEqual(
            print_lines, [],
            f'_save_job_to_disk darf kein print() haben. Found: {print_lines}'
        )


if __name__ == '__main__':
    unittest.main(verbosity=2)
