"""Frontend Static-Audit: Non-Done-State darf keine Done-Card-Sections rendern.

User-Report 2026-05-14: bei canonical_state=failed_support sah User parallel:
- Banner „Auswertung fehlgeschlagen"
- Header „AUSWERTUNG ABGESCHLOSSEN"
- Hero „EINZUTRAGENDER GESAMTBETRAG —"
- Collapsibles „Berechnung im Detail" / „Nachweis & Rechenweg" / „Hochgeladene Dokumente"

Fix: render() in index.html early-returnt bei nicht-done/nicht-needs_review.
"""
import os
import re
import unittest


SITE_HTML = os.path.abspath(os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    '..', 'site', 'index.html'
))


class TestFailedStateUiGate(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(SITE_HTML):
            raise unittest.SkipTest(f'Frontend file not found: {SITE_HTML}')
        cls.src = open(SITE_HTML, encoding='utf-8').read()

    def test_non_done_gate_present(self):
        """Source enthält V15 Non-Done-Gate-Block in render()."""
        self.assertIn('V15 Non-Done-Gate', self.src,
            'Non-Done-Gate Marker fehlt in render()')
        self.assertIn('_doneLike', self.src,
            'Variable _doneLike (Done-Like-Check) fehlt')

    def test_gate_checks_status_kind(self):
        """Gate prüft status_kind === 'done' oder === 'needs_review'."""
        # Block extrahieren
        m = re.search(
            r'V15 Non-Done-Gate[\s\S]*?if\s*\(\s*!_doneLike\s*\)',
            self.src
        )
        self.assertIsNotNone(m, 'Gate-Block nicht gefunden')
        block = m.group(0)
        self.assertIn("'done'", block)
        self.assertIn("'needs_review'", block)

    def test_gate_hides_amount_display(self):
        """Gate hidet result-netto-display + result-amount-label.

        2026-05-19 Modernisierung: Gate ruft `_hardHideResultSections` helper
        statt ad-hoc-Liste. Invariante: helper-Call IM Gate-Body + helper-
        Definition enthält die zu versteckenden IDs.
        """
        m = re.search(
            r'if\s*\(\s*!_doneLike\s*\)\s*\{([\s\S]*?)return;\s*\}',
            self.src
        )
        self.assertIsNotNone(m, 'Gate-Body mit return; nicht gefunden')
        body = m.group(1)
        # Gate ruft Helper (neue Form) ODER hat alte ad-hoc-Liste
        helper_called = '_hardHideResultSections' in body
        legacy_list = ('result-netto-display' in body
                       and 'result-amount-label' in body)
        self.assertTrue(helper_called or legacy_list,
            'Gate muss Done-Sections hiden (Helper oder Liste)')
        # Falls Helper: Helper-Definition muss IDs hiden
        if helper_called:
            helper_def = re.search(
                r'window\._hardHideResultSections\s*=\s*function[\s\S]*?\n\};',
                self.src
            )
            self.assertIsNotNone(helper_def, '_hardHideResultSections def fehlt')
            helper_body = helper_def.group(0)
            self.assertIn('result-netto-display', helper_body)
            self.assertIn('result-amount-label', helper_body)
            self.assertIn('result-amount-subtext', helper_body)

    def test_gate_hides_collapsibles(self):
        """Gate hidet alle <details> im p-result (Collapsibles).

        2026-05-19 Modernisierung: querySelector kann im Gate oder im Helper sein.
        """
        m = re.search(
            r'if\s*\(\s*!_doneLike\s*\)\s*\{([\s\S]*?)return;\s*\}',
            self.src
        )
        body = m.group(1)
        if '_hardHideResultSections' in body:
            # Helper-Pfad — check helper-def
            helper_def = re.search(
                r'window\._hardHideResultSections\s*=\s*function[\s\S]*?\n\};',
                self.src
            )
            self.assertIn("querySelectorAll('details')",
                helper_def.group(0) if helper_def else '',
                'Helper muss <details> ausblenden')
        else:
            self.assertIn("querySelectorAll('details')", body,
                'Gate muss <details> ausblenden')

    def test_gate_returns_early(self):
        """Gate beendet render() mit return — keine Done-Renders danach."""
        m = re.search(
            r'if\s*\(\s*!_doneLike\s*\)\s*\{([\s\S]*?return;[\s\S]*?)\}',
            self.src
        )
        self.assertIsNotNone(m, 'Early-return aus render() fehlt')

    def test_gate_keeps_chat_inline_host(self):
        """2026-05-19 Acceptance-Wechsel (BH-001 / State-Machine-Fix):

        Bei failed_* / fetch_error / expired / deleted darf chat NICHT
        sichtbar bleiben (User-Regel: failed = nur Fehlerkarte + Retry/Support).
        Test prüft jetzt: Gate ruft `_hardHideResultSections({hideChat:true})`
        ODER hidet `chat-inline-host` direkt.

        Bei needs_review (Done-Pfad): chat bleibt sichtbar — andere Tests prüfen das.
        """
        m = re.search(
            r'if\s*\(\s*!_doneLike\s*\)\s*\{([\s\S]*?)return;\s*\}',
            self.src
        )
        body = m.group(1)
        # Entweder Helper mit hideChat:true ODER direktes chat-inline-host hide
        helper_call = re.search(r'_hardHideResultSections\s*\(\s*\{[^}]*hideChat\s*:\s*true', body)
        direct_hide = "chat-inline-host" in body
        self.assertTrue(helper_call or direct_hide,
            'failed-State muss chat verstecken (Helper mit hideChat:true oder direkt)')

    def test_gate_sets_banner_title_from_ui_state(self):
        """Gate setzt rtag-year aus _uiState.banner_title."""
        m = re.search(
            r'if\s*\(\s*!_doneLike\s*\)\s*\{([\s\S]*?)return;\s*\}',
            self.src
        )
        body = m.group(1)
        self.assertIn('banner_title', body,
            'Gate muss _uiState.banner_title für rtag-year nutzen')

    def test_gate_overrides_rname(self):
        """Gate setzt rname auf user_title/banner_title (statt 'Werbungskosten-Auswertung')."""
        m = re.search(
            r'if\s*\(\s*!_doneLike\s*\)\s*\{([\s\S]*?)return;\s*\}',
            self.src
        )
        body = m.group(1)
        self.assertIn('rname', body, 'Gate muss rname überschreiben')
        self.assertIn('user_title', body)


if __name__ == '__main__':
    unittest.main(verbosity=2)
