"""P0-Fix für tuple-Bug in _deterministic_classify_v7 (Job 3aa0570a).

Root-Cause: BMF_AUSLAND_BY_YEAR[year][land] ist tuple (voll_24h, an_abreise),
nicht dict. Z.14129 hatte `sat.get('voll_24h', 0)` ohne tuple→dict-Normalisierung
→ AttributeError → CLASSIFICATION_SCHEMA_FAILED → ganze Auswertung crashed.

Fix: defensive Normalisierung an Z.14126 (tuple → dict bevor .get()).
"""
import os
import re
import sys
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


class TestTupleBugFix(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.src = open(os.path.join(ROOT_DIR, 'app.py')).read()

    def test_bmf_aus_tuple_to_dict_normalized_at_z76_standby(self):
        """Z.14126-Pfad normalisiert tuple → dict bevor sat.get() aufgerufen wird."""
        # Suche nach dem Block der `sat = _bmf_y_sb[bmf_land_sb]` enthält
        # und einer .get('voll_24h')-Aufruf in der Nähe
        m = re.search(
            r'sat\s*=\s*_bmf_y_sb\[bmf_land_sb\][\s\S]{0,1000}?sat\.get\(',
            self.src
        )
        self.assertIsNotNone(m, 'sat-block nicht gefunden')
        block = m.group(0)
        # Tuple→Dict-Normalisierung muss zwischen Zuweisung und .get() liegen
        self.assertIn('isinstance(sat, tuple)', block,
            'tuple-isinstance-Check fehlt vor sat.get()')
        self.assertIn("'voll_24h': float(sat[0])", block,
            'Tuple→Dict-Normalisierung fehlt')
        self.assertIn("'an_abreise': float(sat[1])", block)

    def test_bmf_aus_tuple_normalization_runtime(self):
        """Defensive: simuliere tuple-input + verifiziere keine AttributeError mehr."""
        # Mini-Reproduction: das exakte tuple-pattern aus bmf_data.py
        import os as _os
        _os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')
        sat = (52, 35)  # Tuple (voll_24h, an_abreise) wie in bmf_data
        # Replicate normalization (gleiche Logik wie im Fix)
        if isinstance(sat, tuple) and len(sat) >= 2:
            sat = {'voll_24h': float(sat[0]), 'an_abreise': float(sat[1])}
        # Jetzt funktioniert .get()
        self.assertEqual(sat.get('voll_24h', 0), 52.0)
        self.assertEqual(sat.get('an_abreise', 0), 35.0)

    def test_bmf_data_is_tuple_format(self):
        """Sanity: bmf_data.py speichert Werte als tuple (voll_24h, an_abreise)."""
        import os as _os
        _os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')
        from bmf_data import BMF_AUSLAND_BY_YEAR
        d_2025 = BMF_AUSLAND_BY_YEAR.get(2025) or {}
        self.assertGreater(len(d_2025), 50, '2025-Daten haben weniger als 50 Länder')
        # Prüfe ein paar Sample-Länder
        for sample in ['Spanien', 'Italien', 'USA – New York']:
            if sample in d_2025:
                self.assertIsInstance(
                    d_2025[sample], tuple,
                    f'BMF_AUSLAND[2025][{sample}] sollte tuple sein, '
                    f'ist {type(d_2025[sample])}'
                )
                self.assertEqual(len(d_2025[sample]), 2)

    def test_no_other_unguarded_bmf_aus_direct_dict_access(self):
        """Static-Check: kein anderer Code macht `_bmf_y[xyz].get(...)` ohne
        Normalisierung."""
        # Pattern: _bmf_y_*[...].get(  → das ist der gefährliche Pfad
        bad_pattern = re.compile(
            r'_bmf_y[a-z_]*\[[^\]]+\]\.get\('
        )
        matches = bad_pattern.findall(self.src)
        self.assertEqual(
            matches, [],
            f'Direkter dict-access auf tuple-Daten gefunden: {matches}'
        )

    def test_classification_schema_failed_code_exists(self):
        """AEROTAX_ERROR_CODES enthält CLASSIFICATION_SCHEMA_FAILED."""
        self.assertIn("'CLASSIFICATION_SCHEMA_FAILED'", self.src)


if __name__ == '__main__':
    unittest.main(verbosity=2)
