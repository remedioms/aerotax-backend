"""P0 #75 — CAS/SE pdfplumber silent-fails (bare `except: pass`).

User-Schmerz: pdfplumber-fail in CAS/SE-Reader → Reader liefert leere/teils
Daten, Sonnet bekommt unvollständigen Kontext → falsche Auswertung (z.B.
Z76 Ausland -1493€ FollowMe-Diff).

Fix: alle 3 CAS/SE-pdfplumber-except in dp-prompt-builder + se-fallback-reader
loggen jetzt strukturiert via app.logger.warning mit idx/size/err_type.
"""
import os
import re
import sys
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


class TestCasSilentFailFix(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.src = open(os.path.join(ROOT_DIR, 'app.py')).read()

    def test_se_fallback_reader_logs_pdf_fail(self):
        """se-fallback-reader (Z.~8113) loggt pdfplumber-Fail."""
        self.assertIn('[se-fallback-reader] pdf_read_fail', self.src,
            'se-fallback-reader: log fehlt')

    def test_dp_prompt_builder_logs_se_fail(self):
        """dp-prompt-builder (Z.~8430) loggt SE-Read-Fail."""
        self.assertIn('[dp-prompt-builder] se_read_fail', self.src,
            'dp-prompt-builder: SE-read fail-log fehlt')

    def test_dp_prompt_builder_logs_cas_fail(self):
        """dp-prompt-builder (Z.~8443) loggt CAS-Read-Fail."""
        self.assertIn('[dp-prompt-builder] cas_read_fail', self.src,
            'dp-prompt-builder: CAS-read fail-log fehlt')

    def test_no_bare_except_pass_in_pdfplumber_blocks(self):
        """Static-Check: keine `except: pass` direkt nach `pdfplumber.open` mehr."""
        # Pattern: pdfplumber.open(...) Block bis except: pass — schau auf next 5 Zeilen
        bad_pattern = re.compile(
            r'with\s+pdfplumber\.open\([^)]*\)\s+as\s+\w+:'
            r'[\s\S]{0,400}?'
            r'^\s+except\s*:\s*pass\s*$',
            re.MULTILINE
        )
        matches = bad_pattern.findall(self.src)
        # Es gibt evtl noch Stellen mit größerem Block - prüfen wir konservativ
        # Nur strikt pdfplumber→except:pass direkt
        # Falls solche bestehen: report
        self.assertLessEqual(
            len(matches), 0,
            f'Noch {len(matches)} `pdfplumber.open ... except: pass` Patterns. '
            f'P0 #75 verlangt strukturiertes Logging.'
        )

    def test_safe_logging_no_pdf_bytes(self):
        """Logger-Calls in CAS/SE-Readern enthalten keine Raw-Bytes/PDF-Text.
        Marker existiert + Whitelist-Fields werden genutzt."""
        for marker in ['pdf_read_fail', 'se_read_fail', 'cas_read_fail']:
            self.assertIn(marker, self.src, f'{marker} log fehlt')
        # In den 3 angepassten warning-Blöcken darf kein extract_text(), kein
        # base64-encode, kein filename oder vollständiges raw-Bytes-Argument
        # auftauchen.
        for marker in ['pdf_read_fail', 'se_read_fail', 'cas_read_fail']:
            # Suche das f-string-Template um den marker herum (200 chars window)
            idx = self.src.find(marker)
            ctx = self.src[max(0, idx-200):idx+400]
            self.assertNotIn('data_b64', ctx, f'{marker}: data_b64 im Log-Block')
            self.assertNotIn('extract_text', ctx, f'{marker}: extract_text im Log-Block')
            self.assertNotIn('f.filename', ctx, f'{marker}: filename im Log-Block')


if __name__ == '__main__':
    unittest.main(verbosity=2)
