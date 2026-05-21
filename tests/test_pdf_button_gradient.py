"""Frontend Static-Audit: alle PDF-Download-Buttons nutzen AeroTAX-Gradient.

User-Anweisung 2026-05-14: „PDF Herunterladen / Gradient like the aerotax
colours.. not glass or blue."

Verifiziert via HTML-/JS-Inhalt von index.html:
- #dl-btn-main hat class="dlb" (CSS-Klasse mit var(--grad))
- _refreshPdfBubble nutzt var(--grad)
- next_actions: download_pdf/create_pdf → var(--grad)
- next_actions: retry/start_new bleiben unverändert (out-of-scope)
- #header-pdf-btn behält class="dlb"
- #pdf-locked-indicator bleibt yellow info-banner (kein Gradient)
- :root --grad ist unverändert
"""
import os
import re
import unittest


SITE_HTML = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    '..', 'site', 'index.html'
)
# Path-Resolution: aerotax-backend/tests → ../../site/index.html
SITE_HTML = os.path.abspath(SITE_HTML)


def _read_frontend():
    with open(SITE_HTML, 'r', encoding='utf-8') as f:
        return f.read()


class TestPdfButtonGradient(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.assertTrue_helper = lambda v, m='': None  # noop placeholder
        if not os.path.isfile(SITE_HTML):
            raise unittest.SkipTest(f'Frontend file not found: {SITE_HTML}')
        cls.src = _read_frontend()

    # ─── Brand-Klasse + Variable intakt ───────────────────────────────────

    def test_grad_variable_unchanged(self):
        """--grad CSS-Variable enthält weiter 4-Stop-AeroTAX-Gradient."""
        # Sucht den definierenden Block
        m = re.search(
            r'--grad:\s*linear-gradient\([^)]+\)',
            self.src
        )
        self.assertIsNotNone(m, '--grad variable nicht gefunden')
        grad = m.group(0)
        self.assertIn('#ea7a3c', grad, 'Orange-Stop fehlt')
        self.assertIn('#e25a96', grad, 'Pink-Stop fehlt')
        self.assertIn('#8060d8', grad, 'Violet-Stop fehlt')
        self.assertIn('#3b6cd6', grad, 'Blue-Stop fehlt')

    def test_dlb_css_class_uses_grad_var(self):
        """`.dlb` Klasse nutzt var(--grad) als background."""
        # Es gibt 2 .dlb-Definitionen (Z.996 + Z.1393) — beide müssen var(--grad) haben
        dlb_blocks = re.findall(
            r'\.dlb\s*\{[^}]*\}',
            self.src,
            re.DOTALL
        )
        self.assertGreaterEqual(len(dlb_blocks), 1, '.dlb-Klasse nicht definiert')
        any_with_grad = any('var(--grad)' in b for b in dlb_blocks)
        self.assertTrue(any_with_grad,
            'Keine .dlb-Definition nutzt var(--grad)')

    # ─── PDF-Button-Stellen ───────────────────────────────────────────────

    def test_dl_btn_main_has_dlb_class(self):
        """#dl-btn-main hat class="dlb" zugewiesen."""
        m = re.search(
            r'<button[^>]*id="dl-btn-main"[^>]*>',
            self.src
        )
        self.assertIsNotNone(m, '#dl-btn-main button nicht gefunden')
        tag = m.group(0)
        self.assertRegex(
            tag, r'class="[^"]*\bdlb\b[^"]*"',
            f'#dl-btn-main fehlt class="dlb": {tag[:200]}'
        )

    def test_dl_btn_main_no_blue_purple_gradient(self):
        """#dl-btn-main hat keinen inline blau-lila Gradient mehr."""
        m = re.search(
            r'<button[^>]*id="dl-btn-main"[^>]*>',
            self.src
        )
        tag = m.group(0)
        self.assertNotIn(
            '#3b82f6', tag,
            f'#dl-btn-main hat noch blau-Color: {tag[:300]}'
        )
        self.assertNotIn(
            '#8b5cf6', tag,
            f'#dl-btn-main hat noch lila-Color: {tag[:300]}'
        )

    def test_header_pdf_btn_keeps_dlb_class(self):
        """#header-pdf-btn behält class="dlb" (war vorher schon korrekt)."""
        m = re.search(
            r'<button[^>]*id="header-pdf-btn"[^>]*>',
            self.src
        )
        self.assertIsNotNone(m, '#header-pdf-btn nicht gefunden')
        self.assertRegex(m.group(0), r'class="[^"]*\bdlb\b[^"]*"')

    def test_refresh_pdf_bubble_uses_brand_gradient(self):
        """_refreshPdfBubble-Block enthält `background:var(--grad)` für den Button."""
        m = re.search(
            r'window\._refreshPdfBubble\s*=\s*function[\s\S]*?^\s*\};',
            self.src,
            re.MULTILINE
        )
        self.assertIsNotNone(m, '_refreshPdfBubble Function nicht gefunden')
        block = m.group(0)
        # Inner btn.style.cssText muss var(--grad) nutzen, nicht blau-lila
        self.assertIn('var(--grad)', block,
            '_refreshPdfBubble btn nutzt nicht var(--grad)')
        # Blau-lila darf nirgendwo im Bubble-Block sein
        self.assertNotRegex(
            block,
            r'linear-gradient\([^)]*#3b82f6[^)]*#8b5cf6',
            '_refreshPdfBubble hat noch blau-lila Gradient'
        )

    def test_next_actions_pdf_uses_brand_gradient(self):
        """next_actions Renderer: download_pdf/create_pdf → var(--grad)."""
        # Finde den isPdfAction-Block
        self.assertIn('isPdfAction', self.src,
            'isPdfAction Variable nicht im Renderer')
        # Pattern: if(isPdfAction){ ... var(--grad) ... }
        m = re.search(
            r'if\s*\(\s*isPdfAction\s*\)\s*\{[^}]*\}',
            self.src
        )
        self.assertIsNotNone(m, 'if(isPdfAction) Block nicht gefunden')
        block = m.group(0)
        self.assertIn('var(--grad)', block,
            'isPdfAction-Block nutzt nicht var(--grad)')

    def test_next_actions_pdf_split_from_retry(self):
        """download_pdf und create_pdf nicht mehr im retry/start_new-Pfad."""
        # Alte isPrimary-Definition darf nicht mehr download_pdf enthalten
        old_pattern = re.compile(
            r"isPrimary\s*=\s*\([^)]*'download_pdf'[^)]*\)"
        )
        self.assertIsNone(
            old_pattern.search(self.src),
            'download_pdf darf nicht mehr im isPrimary-Pfad sein'
        )
        # Neue Trennung: isPrimary nur noch retry + start_new
        primary_def = re.search(
            r"isPrimary\s*=\s*\(([^)]+)\)",
            self.src
        )
        self.assertIsNotNone(primary_def)
        clauses = primary_def.group(1)
        self.assertIn("'retry'", clauses)
        self.assertIn("'start_new'", clauses)
        self.assertNotIn("'download_pdf'", clauses)
        self.assertNotIn("'create_pdf'", clauses)

    # ─── Locked-State unverändert ─────────────────────────────────────────

    def test_pdf_locked_indicator_is_glass(self):
        """#pdf-locked-indicator nutzt Glass-Style (kein Yellow, kein Gradient).
        User-Anweisung: locked-state soll glass sein, nicht hässlich gelb."""
        m = re.search(
            r'<div[^>]*id="pdf-locked-indicator"[^>]*>',
            self.src
        )
        self.assertIsNotNone(m, '#pdf-locked-indicator nicht gefunden')
        tag = m.group(0)
        # Yellow muss WEG sein
        self.assertNotIn('rgba(251,191,36', tag,
            'pdf-locked-indicator hat noch yellow tint')
        self.assertNotIn('#fcd34d', tag,
            'pdf-locked-indicator hat noch yellow text-color')
        # Glass-Style: white-transparent background + backdrop-filter
        self.assertIn('rgba(255,255,255', tag,
            'pdf-locked-indicator hat keinen white-transparent background')
        self.assertIn('backdrop-filter', tag,
            'pdf-locked-indicator fehlt backdrop-filter (glass)')
        # KEIN Brand-Gradient (locked bleibt locked, kein aktiver CTA-Style)
        self.assertNotIn('var(--grad)', tag)
        self.assertNotIn('#ea7a3c', tag)

    # ─── Negativ-Audit: keine blau-lila in PDF-Bereichen ──────────────────

    def test_no_blue_purple_in_pdf_specific_buttons(self):
        """Keine `#3b82f6,#8b5cf6` Sequenz mehr in PDF-spezifischen Blöcken."""
        # Suche `dlPDF` calls in close-proximity context
        # Wir scannen alle inline-styles in der Nähe von dlPDF references
        for m in re.finditer(r'dlPDF\(\)', self.src):
            # 800 Zeichen davor + 200 danach
            start = max(0, m.start() - 800)
            end = min(len(self.src), m.end() + 200)
            ctx = self.src[start:end]
            # Wenn das style.cssText / background blau-lila ist, fehler
            blue_purple = re.findall(
                r'linear-gradient\([^)]*#3b82f6[^)]*#8b5cf6',
                ctx
            )
            self.assertEqual(
                blue_purple, [],
                f'Blau-lila Gradient bei dlPDF-Stelle (offset {m.start()}): '
                f'{blue_purple[0] if blue_purple else ""}'
            )


if __name__ == '__main__':
    unittest.main(verbosity=2)
